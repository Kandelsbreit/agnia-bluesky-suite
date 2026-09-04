from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from app.bluesky import BlueskyError, BlueskyGateway
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
        event = {"kind": kind, "mode": self.mode, **data}
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
            gateway = self.gateway_factory(account["handle"], password)
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
        cursor = None
        next_break = self._next_break()
        failed_pages = 0
        target = max(1, self.options.limit)
        self._emit("log", level="info", message=f"Лайкинг: источник={self.source}, цель={target}")

        while stats["completed"] < target and not self._stop_event.is_set():
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
                self._emit("log", level="error", message=f"Ошибка загрузки ленты: {exc}")
                if failed_pages >= 5 or not self._wait(exc.retry_after or 10):
                    break
                continue

            for item in feed:
                if self._stop_event.is_set() or stats["completed"] >= target:
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
                            self.account_id, "like", "skipped", target_key=uri,
                            target_handle=author_handle, message=result.message,
                        )
                        continue
                    stats["completed"] += 1
                    self.db.record_activity(
                        self.account_id, "like", "success", target_key=uri,
                        target_handle=author_handle, message=result.message,
                    )
                    get_logger().info("[@%s] Лайк @%s", gateway.handle, author_handle)
                    self._emit("progress", current=stats["completed"], total=target, stats=dict(stats))
                    if stats["completed"] < target:
                        next_break = self._action_delay(stats["completed"], next_break)
                except BlueskyError as exc:
                    stats["errors"] += 1
                    self.db.record_activity(
                        self.account_id, "like", "error", target_key=uri,
                        target_handle=author_handle, message=str(exc),
                    )
                    self._emit("log", level="error", message=f"Не удалось лайкнуть @{author_handle}: {exc}")
                    if exc.auth_error or not self._wait(exc.retry_after or 3):
                        if exc.auth_error:
                            return
                        break

            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

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
                        self.account_id, "follow", "skipped", target_key=key,
                        target_handle=result.target_handle or target, message=result.message,
                    )
                    self._emit("log", level="info", message=f"@{target}: {result.message}")
                    continue
                stats["completed"] += 1
                self.db.record_activity(
                    self.account_id, "follow", "success", target_key=key,
                    target_handle=result.target_handle or target, message=result.message,
                )
                get_logger().info("[@%s] Подписка на @%s", gateway.handle, result.target_handle or target)
                self._emit("progress", current=stats["completed"], total=target_count, stats=dict(stats))
                if stats["completed"] < target_count:
                    next_break = self._action_delay(stats["completed"], next_break)
            except BlueskyError as exc:
                stats["errors"] += 1
                self.db.record_activity(
                    self.account_id, "follow", "error", target_key=target,
                    target_handle=target, message=str(exc),
                )
                self._emit("log", level="error", message=f"Не удалось подписаться на @{target}: {exc}")
                if exc.auth_error:
                    break
                if not self._wait(exc.retry_after or 4):
                    break
