from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from app.bluesky import BlueskyError, BlueskyGateway, shared_gateway
from app.database import Database
from app.logging_setup import get_logger
from app.utils import normalize_handle

EventCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class AutomationOptions:
    limit: int
    min_delay: float = 5.0
    max_delay: float = 12.0
    human_breaks: bool = True
    break_every_min: int = 15
    break_every_max: int = 25
    break_duration_min: int = 120
    break_duration_max: int = 300
    continuous: bool = False
    cycle_interval_minutes: int = 60
    cycle_jitter_minutes: int = 15


class AutomationWorker(threading.Thread):
    def __init__(
        self,
        db: Database,
        account_id: int,
        mode: str,
        options: AutomationOptions,
        *,
        source: str = "timeline",
        query: str = "",
        skip_replies: bool = True,
        skip_reposts: bool = True,
        targets: Iterable[str] = (),
        callback: EventCallback | None = None,
        gateway_factory: Callable[[str, str], BlueskyGateway] = BlueskyGateway,
    ):
        super().__init__(name=f"automation-{mode}-{account_id}", daemon=True)
        self.db = db
        self.account_id = account_id
        self.mode = mode
        self.options = options
        self.source = source
        self.query = query.strip()
        self.skip_replies = skip_replies
        self.skip_reposts = skip_reposts
        self.targets = list(targets)
        self.callback = callback
        self.gateway_factory = gateway_factory
        self._stop_event = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()

    @property
    def paused(self) -> bool:
        return not self._resume_event.is_set()

    def pause(self) -> None:
        self._resume_event.clear()
        self._emit("status", message="Пауза", paused=True)

    def resume(self) -> None:
        self._resume_event.set()
        self._emit("status", message="Работа продолжается", paused=False)

    def stop(self) -> None:
        self._stop_event.set()
        self._resume_event.set()

    def _emit(self, kind: str, **data: Any) -> None:
        event = {"kind": kind, "mode": self.mode, "account_id": self.account_id, **data}
        if self.callback:
            try:
                self.callback(event)
            except Exception:
                pass

    def _wait(self, seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return False
            while not self._resume_event.is_set():
                if self._stop_event.wait(0.25):
                    return False
            if self._stop_event.wait(min(0.25, max(0.0, deadline - time.monotonic()))):
                return False
        return True

    def _action_delay(self, completed: int, next_break: int) -> int:
        minimum = max(0.5, min(self.options.min_delay, self.options.max_delay))
        maximum = max(minimum, max(self.options.min_delay, self.options.max_delay))
        if self.options.human_breaks and completed >= next_break:
            pause_min = max(1, min(self.options.break_duration_min, self.options.break_duration_max))
            pause_max = max(pause_min, max(self.options.break_duration_min, self.options.break_duration_max))
            seconds = random.randint(pause_min, pause_max)
            self._emit("log", level="warning", message=f"Большая пауза: {seconds} сек.")
            self._wait(seconds)
            every_min = max(1, min(self.options.break_every_min, self.options.break_every_max))
            every_max = max(every_min, max(self.options.break_every_min, self.options.break_every_max))
            return completed + random.randint(every_min, every_max)
        delay = random.uniform(minimum, maximum)
        self._emit("log", level="info", message=f"Пауза {delay:.1f} сек.")
        self._wait(delay)
        return next_break

    def run(self) -> None:
        stats = {"completed": 0, "skipped": 0, "errors": 0, "stopped": False}
        self._emit("status", message="Запуск", running=True, paused=False)
        try:
            account, password = self.db.get_account_secret(self.account_id)
            if not account or not password:
                raise BlueskyError("Для выбранного аккаунта не сохранён App Password", auth_error=True)
            gateway = (
                shared_gateway(self.db, self.account_id)
                if self.gateway_factory is BlueskyGateway
                else self.gateway_factory(account["handle"], password)
            )
            profile = gateway.login()
            self.db.update_connection(self.account_id, "Подключён", profile.display_name, profile.did)
            if self.mode == "likes":
                self._run_likes(gateway, profile.did, stats)
            elif self.mode == "following":
                self._run_following(gateway, stats)
            else:
                raise ValueError(f"Unknown automation mode: {self.mode}")
        except BlueskyError as exc:
            stats["errors"] += 1
            if exc.auth_error:
                self.db.update_connection(self.account_id, "Ошибка авторизации")
            self._emit("log", level="error", message=str(exc))
        except Exception as exc:
            stats["errors"] += 1
            self._emit("log", level="error", message=f"Непредвиденная ошибка: {exc}")
        finally:
            stats["stopped"] = self._stop_event.is_set()
            self._emit("finished", stats=stats, running=False, paused=False)

    def _next_break(self) -> int:
        minimum = max(1, min(self.options.break_every_min, self.options.break_every_max))
        maximum = max(minimum, max(self.options.break_every_min, self.options.break_every_max))
        return random.randint(minimum, maximum)

    def _run_likes(self, gateway: BlueskyGateway, own_did: str, stats: dict[str, Any]) -> None:
        target = max(1, self.options.limit)
        self._emit(
            "log",
            level="info",
            message=f"[@{gateway.handle}] Лайкинг: источник={self.source}, лимит={target} за цикл",
        )

        while not self._stop_event.is_set():
            cursor = None
            next_break = self._next_break()
            failed_pages = 0
            cycle_completed = 0

            while cycle_completed < target and not self._stop_event.is_set():
                self._resume_event.wait()
                if self._stop_event.is_set():
                    break
                try:
                    if self.source == "discover":
                        feed, next_cursor = gateway.get_discover_feed(50, cursor)
                    elif self.source == "search":
                        if not self.query:
                            raise BlueskyError("Для поиска нужен хештег или текст запроса")
                        feed, next_cursor = gateway.search_posts(self.query, 50, cursor)
                    else:
                        feed, next_cursor = gateway.get_timeline(50, cursor)
                    failed_pages = 0
                except BlueskyError as exc:
                    stats["errors"] += 1
                    failed_pages += 1
                    self._emit("log", level="error", message=f"[@{gateway.handle}] Ошибка загрузки ленты: {exc}")
                    if exc.auth_error:
                        return
                    if failed_pages >= 5:
                        if self.options.continuous:
                            self._emit(
                                "log",
                                level="warning",
                                message=f"[@{gateway.handle}] Слишком много ошибок сети, ожидание 5 минут перед повтором...",
                            )
                            if not self._wait(300):
                                return
                            failed_pages = 0
                            continue
                        return
                    if not self._wait(exc.retry_after or 10):
                        return
                    continue

                for item in feed:
                    if self._stop_event.is_set() or cycle_completed >= target:
                        break
                    self._resume_event.wait()
                    if self._stop_event.is_set():
                        break
                    post = item.get("post") or {}
                    uri = str(post.get("uri") or "")
                    cid = str(post.get("cid") or "")
                    author = post.get("author") or {}
                    author_did = str(author.get("did") or "")
                    author_handle = str(author.get("handle") or "")
                    record = post.get("record") or {}
                    viewer = post.get("viewer") or {}

                    should_skip = (
                        not uri
                        or not cid
                        or author_did == own_did
                        or (self.skip_reposts and item.get("reason") is not None)
                        or (self.skip_replies and bool(record.get("reply")))
                        or bool(viewer.get("like"))
                        or self.db.action_was_successful(self.account_id, "like", uri)
                    )
                    if should_skip:
                        stats["skipped"] += 1
                        continue
                    try:
                        result = gateway.like(uri, cid)
                        if result.skipped:
                            stats["skipped"] += 1
                            self.db.record_activity(
                                self.account_id,
                                "like",
                                "skipped",
                                target_key=uri,
                                target_handle=author_handle,
                                message=result.message,
                            )
                            continue
                        stats["completed"] += 1
                        cycle_completed += 1
                        self.db.record_activity(
                            self.account_id,
                            "like",
                            "success",
                            target_key=uri,
                            target_handle=author_handle,
                            message=result.message,
                        )
                        get_logger().info("[@%s] Лайк @%s", gateway.handle, author_handle)
                        self._emit(
                            "progress",
                            current=cycle_completed,
                            total=target,
                            total_completed=stats["completed"],
                            handle=gateway.handle,
                            stats=dict(stats),
                        )
                        if cycle_completed < target:
                            next_break = self._action_delay(cycle_completed, next_break)
                    except BlueskyError as exc:
                        stats["errors"] += 1
                        self.db.record_activity(
                            self.account_id,
                            "like",
                            "error",
                            target_key=uri,
                            target_handle=author_handle,
                            message=str(exc),
                        )
                        self._emit(
                            "log",
                            level="error",
                            message=f"[@{gateway.handle}] Не удалось лайкнуть @{author_handle}: {exc}",
                        )
                        if exc.auth_error:
                            return
                        wait_time = exc.retry_after or (60 if exc.rate_limited else 3)
                        if wait_time >= 5:
                            self._emit(
                                "log",
                                level="warning",
                                message=f"[@{gateway.handle}] Пауза из-за ограничения частоты запросов: {int(wait_time)} сек.",
                            )
                        if not self._wait(wait_time):
                            return

                if not next_cursor or next_cursor == cursor:
                    self._emit("log", level="info", message="Лента просмотрена; цикл завершён.")
                    break
                cursor = next_cursor

            if not self.options.continuous or self._stop_event.is_set():
                break

            jitter = random.randint(
                -max(0, self.options.cycle_jitter_minutes), max(0, self.options.cycle_jitter_minutes)
            )
            rest_minutes = max(1, self.options.cycle_interval_minutes + jitter)
            rest_seconds = rest_minutes * 60
            wake_time = time.strftime("%H:%M", time.localtime(time.time() + rest_seconds))
            self._emit(
                "log",
                level="info",
                message=f"[@{gateway.handle}] Цикл завершён ({cycle_completed} лайков). Перерыв {rest_minutes} мин. (до {wake_time}).",
            )
            self._emit(
                "status",
                message=f"Отдых до {wake_time}",
                resting=True,
                handle=gateway.handle,
            )

            end_time = time.monotonic() + rest_seconds
            while time.monotonic() < end_time and not self._stop_event.is_set():
                self._resume_event.wait()
                if self._stop_event.is_set():
                    return
                remaining = int(end_time - time.monotonic())
                if remaining > 0 and remaining % 60 == 0:
                    mins_left = remaining // 60
                    self._emit(
                        "status",
                        message=f"Отдых (~{mins_left} мин)",
                        resting=True,
                        handle=gateway.handle,
                    )
                self._stop_event.wait(min(5.0, max(0.1, end_time - time.monotonic())))

            if self._stop_event.is_set():
                return
            self._emit(
                "log",
                level="info",
                message=f"[@{gateway.handle}] Перерыв окончен. Запуск нового цикла лайкинга.",
            )

    def _run_following(self, gateway: BlueskyGateway, stats: dict[str, Any]) -> None:
        unique_targets: list[str] = []
        seen: set[str] = set()
        for raw in self.targets:
            target = normalize_handle(raw)
            if target and target not in seen:
                seen.add(target)
                unique_targets.append(target)

        target_count = min(max(1, self.options.limit), len(unique_targets))
        next_break = self._next_break()
        self._emit("log", level="info", message=f"Фолловинг: целей={len(unique_targets)}, лимит={target_count}")

        for target in unique_targets:
            if self._stop_event.is_set() or stats["completed"] >= target_count:
                break
            self._resume_event.wait()
            if self._stop_event.is_set():
                break
            try:
                result = gateway.follow(target)
                key = result.target_key or target
                if result.skipped:
                    stats["skipped"] += 1
                    self.db.record_activity(
                        self.account_id,
                        "follow",
                        "skipped",
                        target_key=key,
                        target_handle=result.target_handle or target,
                        message=result.message,
                    )
                    self._emit("log", level="info", message=f"@{target}: {result.message}")
                    continue
                stats["completed"] += 1
                self.db.record_activity(
                    self.account_id,
                    "follow",
                    "success",
                    target_key=key,
                    target_handle=result.target_handle or target,
                    message=result.message,
                )
                get_logger().info("[@%s] Подписка на @%s", gateway.handle, result.target_handle or target)
                self._emit("progress", current=stats["completed"], total=target_count, stats=dict(stats))
                if stats["completed"] < target_count:
                    next_break = self._action_delay(stats["completed"], next_break)
            except BlueskyError as exc:
                stats["errors"] += 1
                self.db.record_activity(
                    self.account_id,
                    "follow",
                    "error",
                    target_key=target,
                    target_handle=target,
                    message=str(exc),
                )
                self._emit("log", level="error", message=f"Не удалось подписаться на @{target}: {exc}")
                if exc.auth_error:
                    break
                if not self._wait(exc.retry_after or (60 if exc.rate_limited else 4)):
                    break


class LikeAutomationManager:
    def __init__(
        self,
        db: Database,
        callback: EventCallback | None = None,
        gateway_factory: Callable[[str, str], BlueskyGateway] = BlueskyGateway,
    ):
        self.db = db
        self.callback = callback
        self.gateway_factory = gateway_factory
        self._workers: dict[int, AutomationWorker] = {}
        self._lock = threading.Lock()

    def is_running(self, account_id: int | None = None) -> bool:
        with self._lock:
            if account_id is not None:
                worker = self._workers.get(account_id)
                return worker is not None and worker.is_alive()
            return any(w.is_alive() for w in self._workers.values())

    def is_paused(self, account_id: int | None = None) -> bool:
        with self._lock:
            if account_id is not None:
                worker = self._workers.get(account_id)
                return bool(worker and worker.is_alive() and worker.paused)
            running = [w for w in self._workers.values() if w.is_alive()]
            return bool(running and all(w.paused for w in running))

    def active_account_ids(self) -> list[int]:
        with self._lock:
            return [aid for aid, w in self._workers.items() if w.is_alive()]

    def get_worker(self, account_id: int) -> AutomationWorker | None:
        with self._lock:
            return self._workers.get(account_id)

    def start_account(
        self,
        account_id: int,
        options: AutomationOptions,
        *,
        source: str = "timeline",
        query: str = "",
        skip_replies: bool = True,
        skip_reposts: bool = True,
    ) -> bool:
        with self._lock:
            existing = self._workers.get(account_id)
            if existing and existing.is_alive():
                return False
            worker = AutomationWorker(
                self.db,
                account_id,
                "likes",
                options,
                source=source,
                query=query,
                skip_replies=skip_replies,
                skip_reposts=skip_reposts,
                callback=self._handle_worker_event,
                gateway_factory=self.gateway_factory,
            )
            self._workers[account_id] = worker
            worker.start()
            return True

    def start_accounts(
        self,
        account_ids: list[int],
        options: AutomationOptions,
        *,
        source: str = "timeline",
        query: str = "",
        skip_replies: bool = True,
        skip_reposts: bool = True,
    ) -> int:
        started = 0
        for aid in account_ids:
            if self.start_account(
                aid,
                options,
                source=source,
                query=query,
                skip_replies=skip_replies,
                skip_reposts=skip_reposts,
            ):
                started += 1
        return started

    def pause_account(self, account_id: int) -> None:
        with self._lock:
            worker = self._workers.get(account_id)
            if worker and worker.is_alive():
                worker.pause()

    def resume_account(self, account_id: int) -> None:
        with self._lock:
            worker = self._workers.get(account_id)
            if worker and worker.is_alive():
                worker.resume()

    def toggle_pause_all(self) -> bool:
        with self._lock:
            running = [w for w in self._workers.values() if w.is_alive()]
            if not running:
                return False
            should_resume = all(w.paused for w in running)
            for w in running:
                if should_resume:
                    w.resume()
                else:
                    w.pause()
            return should_resume

    def stop_account(self, account_id: int) -> None:
        with self._lock:
            worker = self._workers.get(account_id)
            if worker:
                worker.stop()

    def stop_all(self) -> None:
        with self._lock:
            for worker in list(self._workers.values()):
                worker.stop()

    def _handle_worker_event(self, event: dict[str, Any]) -> None:
        if self.callback:
            try:
                self.callback(event)
            except Exception:
                pass
