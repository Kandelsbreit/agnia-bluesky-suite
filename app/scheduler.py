from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.bluesky import BlueskyError, BlueskyGateway
from app.database import Database
from app.logging_setup import get_logger
from app.utils import is_valid_tid, new_record_key, parse_iso

BACKOFF_SECONDS = [60, 120, 300, 600, 1200, 1800]


class AccountQueueWorker:
    def __init__(
        self,
        account_id: int,
        db: Database,
        status_callback: Callable[[int], None] | None = None,
        gateway_factory: Callable[[str, str], BlueskyGateway] = BlueskyGateway,
    ):
        self.account_id = account_id
        self.db = db
        self.status_callback = status_callback
        self.gateway_factory = gateway_factory
        self.status = "Инициализация"
        self.next_run_timestamp: float | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._publish_now = threading.Event()
        self._thread: threading.Thread | None = None
        self._gateway: BlueskyGateway | None = None
        self._credential_fingerprint: tuple[str, str] | None = None
        self._last_reconcile_time: float = 0.0
        self._reconcile_interval: float = 600.0  # 10 minutes

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"queue-{self.account_id}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def publish_now(self) -> None:
        self._publish_now.set()
        self._wake.set()

    def reconcile_published_posts(self, force: bool = False) -> int:
        now = time.monotonic()
        if not force and (now - self._last_reconcile_time) < self._reconcile_interval:
            return 0
        self._last_reconcile_time = now
        account, password = self.db.get_account_secret(self.account_id)
        if not account or not password:
            return 0
        try:
            gateway = self._gateway_for(account["handle"], password)
            if hasattr(gateway, "get_author_recent_posts"):
                published = gateway.get_author_recent_posts(account["handle"], limit=100)
            else:
                published = []
            count = self.db.reconcile_queue_with_published(self.account_id, published)
            if count > 0:
                get_logger().info(
                    "[@%s] Автоматическая сверка: пропущено %d постов, уже опубликованных в Bluesky",
                    account["handle"],
                    count,
                )
                if self.status_callback:
                    try:
                        self.status_callback(self.account_id)
                    except Exception:
                        pass
            return count
        except Exception as exc:
            get_logger().warning(
                "[@%s] Ошибка автоматической сверки с Bluesky: %s",
                account.get("handle", self.account_id),
                exc,
            )
            return 0

    def _set_status(self, status: str) -> None:
        changed = status != self.status
        self.status = status
        if changed and self.status_callback:
            try:
                self.status_callback(self.account_id)
            except Exception:
                pass

    @staticmethod
    def calculate_interval(account: dict) -> float:
        base = max(1, int(account.get("interval_minutes") or 60)) * 60.0
        jitter = max(0, int(account.get("jitter_minutes") or 0)) * 60.0
        return max(30.0, base + random.uniform(-jitter, jitter))

    def _gateway_for(self, handle: str, password: str) -> BlueskyGateway:
        fingerprint = (handle, password)
        if self._gateway is None or self._credential_fingerprint != fingerprint:
            self._gateway = self.gateway_factory(handle, password)
            self._credential_fingerprint = fingerprint
        return self._gateway

    def _wait(self, seconds: float) -> None:
        self._wake.wait(timeout=max(0.05, seconds))
        self._wake.clear()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile_published_posts(force=(self._last_reconcile_time == 0.0))
                account, password = self.db.get_account_secret(self.account_id)
                if not account:
                    all_ids = {int(a["id"]) for a in self.db.get_accounts()}
                    if self.account_id not in all_ids:
                        break
                    self._wait(2)
                    continue
                item = self.db.next_queue_item(self.account_id)
                if not item:
                    self._publish_now.clear()
                    self.next_run_timestamp = None
                    self._set_status("Очередь пуста")
                    self.db.update_runtime(self.account_id, next_scheduled_at=None, retry_count=0, last_error="")
                    self._wait(3)
                    continue

                forced = self._publish_now.is_set()
                if account.get("queue_paused") and not forced:
                    self.next_run_timestamp = None
                    self._set_status("На паузе")
                    self._wait(2)
                    continue
                if not password:
                    self._publish_now.clear()
                    self.next_run_timestamp = None
                    self._set_status("Нужен App Password")
                    self._wait(5)
                    continue

                now = datetime.now(UTC)
                scheduled = parse_iso(account.get("next_scheduled_at"))
                if scheduled is None:
                    interval = self.calculate_interval(account)
                    if account.get("last_posted_at"):
                        last_posted = parse_iso(account["last_posted_at"])
                        scheduled = (last_posted + timedelta(seconds=interval)) if last_posted else now + timedelta(seconds=interval)
                    else:
                        scheduled = now + timedelta(seconds=interval)
                    self.db.update_runtime(self.account_id, next_scheduled_at=scheduled.isoformat())

                self.next_run_timestamp = scheduled.timestamp()
                remaining = self.next_run_timestamp - time.time()
                if remaining > 0 and not forced:
                    retry_count = int(account.get("retry_count") or 0)
                    self._set_status("Ожидание повтора" if retry_count else "Ожидание")
                    self._wait(min(1.0, remaining))
                    continue

                self._publish_now.clear()
                self._wake.clear()
                self._set_status("Публикация")

                existing_in_history = self.db.post_exists_in_history(self.account_id, item["content"])
                if existing_in_history:
                    fresh_account = self.db.get_account(self.account_id) or account
                    next_interval = self.calculate_interval(fresh_account)
                    next_time = datetime.now(UTC) + timedelta(seconds=next_interval)
                    self.db.complete_queue_item(
                        item["id"],
                        existing_in_history.get("post_uri") or "",
                        existing_in_history.get("post_cid") or "",
                        status="published",
                        snapshot=item,
                        next_scheduled_at=next_time.isoformat(),
                    )
                    get_logger().info(
                        "[@%s] Пост #%s уже был опубликован ранее (найден в истории). Дублирование предотвращено.",
                        account["handle"],
                        item["id"],
                    )
                    self._set_status("Дубликат пропущен")
                    self._wait(0.2)
                    continue

                gateway = self._gateway_for(account["handle"], password)
                existing_on_bluesky = None
                if hasattr(gateway, "check_recent_post"):
                    try:
                        existing_on_bluesky = gateway.check_recent_post(item["content"])
                    except Exception:
                        pass

                if existing_on_bluesky:
                    result = existing_on_bluesky
                    note = " (обнаружен в профиле Bluesky, дублирование предотвращено)"
                else:
                    rkey = item.get("record_key") or ""
                    if not is_valid_tid(rkey):
                        rkey = new_record_key()
                        self.db.update_queue_record_key(item["id"], rkey)
                    result = gateway.publish_text(item["content"], rkey)
                    note = " (восстановлено после обрыва)" if result.recovered_existing else ""
                fresh_account = self.db.get_account(self.account_id) or account
                next_interval = self.calculate_interval(fresh_account)
                next_time = datetime.now(UTC) + timedelta(seconds=next_interval)
                self.db.complete_queue_item(
                    item["id"],
                    result.uri,
                    result.cid,
                    snapshot=item,
                    next_scheduled_at=next_time.isoformat(),
                )
                self.db.update_connection(
                    self.account_id,
                    "Подключён",
                    gateway.profile.display_name if gateway.profile else "",
                    gateway.profile.did if gateway.profile else "",
                )
                self.db.record_activity(
                    self.account_id,
                    "queue_post",
                    "success",
                    target_key=result.uri,
                    message=f"Пост опубликован{note}",
                )
                get_logger().info("[@%s] Пост из очереди опубликован%s", account["handle"], note)
                self._set_status("Опубликовано")
                self._wait(0.2)
            except BlueskyError as exc:
                account = self.db.get_account(self.account_id) or {}
                retry_count = int(account.get("retry_count") or 0) + 1
                if not exc.retryable and not exc.auth_error:
                    self._publish_now.clear()
                    bad_item = self.db.next_queue_item(self.account_id)
                    if bad_item:
                        self.db.complete_queue_item(bad_item["id"], uri="", cid="", status="skipped")
                        get_logger().warning(
                            "[@%s] Пост #%s пропущен из-за ошибки: %s. Очередь продолжается.",
                            account.get("handle", self.account_id), bad_item["id"], exc,
                        )
                        self.db.record_activity(
                            self.account_id,
                            "queue_post",
                            "skipped",
                            message=f"Пост пропущен: {exc}",
                        )
                    interval = self.calculate_interval(account)
                    next_time = datetime.now(UTC) + timedelta(seconds=interval)
                    self.db.update_runtime(
                        self.account_id,
                        next_scheduled_at=next_time.isoformat(),
                        retry_count=0,
                        last_error=f"Пропущен пост: {exc}"[:1000],
                    )
                    self._set_status("Пост пропущен (ошибка)")
                    self._wait(2)
                    continue
                failed_item = self.db.next_queue_item(self.account_id)
                if failed_item:
                    self.db.mark_attempt_failed(failed_item["id"], str(exc))
                step = BACKOFF_SECONDS[min(retry_count - 1, len(BACKOFF_SECONDS) - 1)]
                if exc.auth_error:
                    self._gateway = None
                    self._credential_fingerprint = None
                    if not password:
                        step = max(step, 1800)
                        self.db.update_connection(self.account_id, "Нужен App Password")
                    else:
                        step = min(step, 30)
                if exc.retry_after:
                    step = max(step, exc.retry_after)
                step += random.randint(0, 5)
                retry_at = datetime.now(UTC) + timedelta(seconds=step)
                self.db.update_runtime(
                    self.account_id,
                    next_scheduled_at=retry_at.isoformat(),
                    retry_count=retry_count,
                    last_error=str(exc)[:1000],
                )
                self.db.record_activity(
                    self.account_id,
                    "queue_post",
                    "error",
                    message=f"{exc}; повтор через {step} сек.",
                )
                get_logger().error("Ошибка очереди аккаунта %s: %s", self.account_id, exc)
                self._set_status(f"Ошибка, повтор #{retry_count}")
                self._wait(min(2.0, step))
            except Exception as exc:
                get_logger().exception("Ошибка фонового потока очереди %s: %s", self.account_id, exc)
                self._set_status("Внутренняя ошибка")
                self._wait(3)
        self.next_run_timestamp = None
        self._set_status("Остановлен")


class QueueScheduler:
    def __init__(self, db: Database, status_callback: Callable[[int], None] | None = None):
        self.db = db
        self.status_callback = status_callback
        self.workers: dict[int, AccountQueueWorker] = {}
        self._lock = threading.RLock()

    def sync_accounts(self) -> None:
        with self._lock:
            ids = {int(account["id"]) for account in self.db.get_accounts()}
            for account_id in list(self.workers):
                if account_id not in ids:
                    self.workers.pop(account_id).stop()
            for account_id in ids:
                if account_id not in self.workers:
                    worker = AccountQueueWorker(account_id, self.db, self.status_callback)
                    self.workers[account_id] = worker
                    worker.start()

    def worker(self, account_id: int) -> AccountQueueWorker | None:
        return self.workers.get(account_id)

    def wake(self, account_id: int) -> None:
        worker = self.worker(account_id)
        if worker:
            worker.wake()

    def wake_all(self) -> None:
        for worker in list(self.workers.values()):
            worker.wake()

    def publish_now(self, account_id: int) -> None:
        worker = self.worker(account_id)
        if worker:
            worker.publish_now()

    def toggle_pause(self, account_id: int) -> bool:
        account = self.db.get_account(account_id)
        if not account:
            return False
        paused = not bool(account.get("queue_paused"))
        self.db.set_queue_paused(account_id, paused)
        self.wake(account_id)
        return paused

    def stop_all(self) -> None:
        with self._lock:
            for worker in list(self.workers.values()):
                worker.stop()
            self.workers.clear()

    def stop_account(self, account_id: int) -> None:
        with self._lock:
            worker = self.workers.pop(account_id, None)
        if worker:
            worker.stop()

    def reconcile_account(self, account_id: int) -> int:
        worker = self.worker(account_id)
        if worker:
            return worker.reconcile_published_posts(force=True)
        account, password = self.db.get_account_secret(account_id)
        if account and password:
            gw = BlueskyGateway(account["handle"], password)
            if hasattr(gw, "get_author_recent_posts"):
                posts = gw.get_author_recent_posts(account["handle"], limit=100)
            else:
                posts = []
            cnt = self.db.reconcile_queue_with_published(account_id, posts)
            if self.status_callback:
                try:
                    self.status_callback(account_id)
                except Exception:
                    pass
            return cnt
        return 0

    def reconcile_all(self) -> dict[int, int]:
        results: dict[int, int] = {}
        for account in self.db.get_accounts():
            aid = int(account["id"])
            results[aid] = self.reconcile_account(aid)
        return results

