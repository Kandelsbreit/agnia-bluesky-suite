from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.bluesky import BlueskyError, BlueskyGateway, shared_gateway
from app.database import Database
from app.logging_setup import get_logger
from app.utils import parse_iso

BACKOFF_SECONDS = [60, 120, 300, 600, 1200, 1800]


class AccountQueueWorker:
    def __init__(
        self,
        account_id: int,
        db: Database,
        status_callback: Callable | None = None,
        gateway_factory: Callable = BlueskyGateway,
    ):
        self.account_id = account_id
        self.db = db
        self.status_callback = status_callback
        self.gateway_factory = gateway_factory
        self.status = "Инициализация"
        self.next_run_timestamp = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._publish_now = threading.Event()
        self._thread = None
        self._gateway = None
        self._credential_fingerprint = None
        self._last_reconcile_time = 0.0
        self._reconcile_interval = 600.0
        self._operation_lock = threading.RLock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"queue-{self.account_id}", daemon=True)
        self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    def wake(self):
        self._wake.set()

    def publish_now(self):
        item = self.db.next_queue_item(self.account_id)
        if item:
            self.db.request_send(item["id"])
        self._wake.set()

    def _set_status(self, status):
        changed = status != self.status
        self.status = status
        if changed and self.status_callback:
            self.status_callback(self.account_id)

    @staticmethod
    def calculate_interval(account):
        base = max(1, int(account.get("interval_minutes") or 60)) * 60
        jitter = max(0, int(account.get("jitter_minutes") or 0)) * 60
        return max(30, base + random.uniform(-jitter, jitter))

    def _gateway_for(self, handle, password):
        if self.gateway_factory is BlueskyGateway:
            return shared_gateway(self.db, self.account_id)
        if self._gateway is None or self._credential_fingerprint != (handle, password):
            self._gateway = self.gateway_factory(handle, password)
            self._credential_fingerprint = (handle, password)
        return self._gateway

    def _wait(self, seconds):
        self._wake.wait(max(0.05, seconds))
        self._wake.clear()

    def reconcile_published_posts(self, force=False):
        with self._operation_lock:
            now = time.monotonic()
            if not force and now - self._last_reconcile_time < self._reconcile_interval:
                return 0
            account, password = self.db.get_account_secret(self.account_id)
            if not account or not password:
                return 0
            gateway = self._gateway_for(account["handle"], password)
            if not hasattr(gateway, "get_author_recent_posts"):
                self._last_reconcile_time = now
                return 0
            posts = gateway.get_author_recent_posts(account["handle"], limit=100)
            posts = [p for p in posts if not p.get("has_embed") and not p.get("is_reply")]
            count = self.db.reconcile_queue_with_published(self.account_id, posts)
            self._last_reconcile_time = now
            return count

    def _loop(self):
        while not self._stop.is_set():
            item = None
            try:
                account, password = self.db.get_account_secret(self.account_id)
                if not account:
                    break
                item = self.db.next_queue_item(self.account_id)
                if not item:
                    self.next_run_timestamp = None
                    failed = self.db.failed_count(self.account_id)
                    self._set_status("Есть ошибки — проверьте очередь" if failed else "Очередь пуста")
                    self._wait(2)
                    continue
                forced = bool(item["send_now"]) or self._publish_now.is_set()
                if account["queue_paused"] and not forced:
                    self.next_run_timestamp = None
                    self._set_status("На паузе")
                    self._wait(1)
                    continue
                if not password:
                    self._set_status("Нужен App Password")
                    self._wait(5)
                    continue
                if (
                    time.monotonic() - self._last_reconcile_time >= self._reconcile_interval
                    and item["state"] == "pending"
                    and not forced
                ):
                    try:
                        self.reconcile_published_posts()
                    except Exception as exc:
                        get_logger().warning("Не удалось сверить очередь %s: %s", self.account_id, exc)
                        self._last_reconcile_time = time.monotonic()
                    item = self.db.next_queue_item(self.account_id)
                    if not item:
                        continue
                now = datetime.now(UTC)
                scheduled = parse_iso(item.get("scheduled_at")) or parse_iso(account.get("next_scheduled_at"))
                if scheduled is None:
                    scheduled = now + timedelta(seconds=self.calculate_interval(account))
                    self.db.update_runtime(self.account_id, next_scheduled_at=scheduled.isoformat())
                last_posted = parse_iso(account.get("last_posted_at"))
                next_account = parse_iso(account.get("next_scheduled_at"))
                if item.get("scheduled_at") and last_posted and scheduled < last_posted and next_account:
                    scheduled = max(scheduled, next_account)
                # Retry backoff always wins over a past calendar date.
                if item["state"] == "uncertain" and account.get("retry_count"):
                    scheduled = parse_iso(account.get("next_scheduled_at")) or scheduled
                self.next_run_timestamp = scheduled.timestamp()
                if scheduled > now and not forced:
                    self._set_status("Ожидание повтора" if item["state"] == "uncertain" else "Ожидание")
                    self._wait(min(1, (scheduled - now).total_seconds()))
                    continue
                self._publish_now.clear()
                with self._operation_lock:
                    if self._stop.is_set():
                        break
                    latest = self.db.get_account(self.account_id)
                    if latest and latest["queue_paused"] and not forced:
                        continue
                    item = self.db.claim_queue_item(item["id"])
                    if not item:
                        continue
                    self._set_status("Публикация")
                    gateway = self._gateway_for(account["handle"], password)
                    media = json.loads(item["media_json"])
                    existing = None
                    if (
                        not media
                        and not item["record_json"]
                        and item["attempt_count"] == 1
                        and hasattr(gateway, "check_recent_post")
                    ):
                        existing = gateway.check_recent_post(item["content"])
                    if existing:
                        result = existing
                    elif hasattr(gateway, "prepare_record"):
                        record = (
                            json.loads(item["record_json"])
                            if item["record_json"]
                            else gateway.prepare_record(item["content"], media)
                        )
                        if not item["record_json"]:
                            self.db.save_prepared_record(item["id"], record)
                            item["record_json"] = json.dumps(record, ensure_ascii=False)
                        result = gateway.publish_record(record, item["record_key"])
                    else:
                        result = gateway.publish_text(item["content"], item["record_key"])
                    next_time = datetime.now(UTC) + timedelta(
                        seconds=self.calculate_interval(self.db.get_account(self.account_id) or account)
                    )
                    self.db.complete_queue_item(
                        item["id"], result.uri, result.cid, snapshot=item, next_scheduled_at=next_time.isoformat()
                    )
                    self.db.record_activity(
                        self.account_id,
                        "queue_post",
                        "success",
                        target_key=result.uri,
                        message="Публикация подтверждена",
                    )
                    if gateway.profile:
                        self.db.update_connection(
                            self.account_id, "Подключён", gateway.profile.display_name, gateway.profile.did
                        )
                    self._set_status("Опубликовано")
            except BlueskyError as exc:
                if item and item.get("state") == "sending":
                    retry = exc.retryable
                    # No auto-login storm on a revoked password, and never discard a failed record.
                    self.db.fail_queue_item(
                        item["id"],
                        str(exc),
                        uncertain=retry,
                        definitive=not retry and exc.code not in {"RecordConflict"} and not exc.auth_error,
                    )
                    account = self.db.get_account(self.account_id) or {}
                    attempts = int(account.get("retry_count", 0)) + 1
                    delay = max(BACKOFF_SECONDS[min(attempts - 1, 5)], exc.retry_after or 0) + random.randint(0, 5)
                    self.db.update_runtime(
                        self.account_id,
                        next_scheduled_at=(datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
                        retry_count=attempts,
                        last_error=str(exc)[:1000],
                    )
                    if exc.auth_error:
                        self.db.set_queue_paused(self.account_id, True)
                        self.db.update_connection(self.account_id, "Ошибка авторизации")
                self._set_status("Ошибка — пост сохранён")
                get_logger().warning("Очередь аккаунта %s: %s", self.account_id, exc)
                self._wait(2)
            except Exception as exc:
                if item and item.get("state") == "sending":
                    self.db.fail_queue_item(
                        item["id"],
                        str(exc),
                        uncertain=False,
                        definitive=not bool(item.get("record_json")) and isinstance(exc, ValueError),
                    )
                self._set_status("Ошибка — пост сохранён")
                get_logger().exception("Ошибка очереди аккаунта %s", self.account_id)
                self._wait(3)
        self.next_run_timestamp = None
        self._set_status("Остановлен")


class QueueScheduler:
    def __init__(self, db, status_callback=None):
        self.db = db
        self.status_callback = status_callback
        self.workers = {}
        self._lock = threading.RLock()

    def sync_accounts(self):
        with self._lock:
            ids = {a["id"] for a in self.db.get_accounts()}
            for aid in list(self.workers):
                if aid not in ids:
                    self.stop_account(aid)
            for aid in ids:
                if aid not in self.workers:
                    w = AccountQueueWorker(aid, self.db, self.status_callback)
                    self.workers[aid] = w
                    w.start()

    def worker(self, account_id):
        return self.workers.get(account_id)

    def wake(self, account_id):
        if self.worker(account_id):
            self.worker(account_id).wake()

    def wake_all(self):
        for w in list(self.workers.values()):
            w.wake()

    def publish_now(self, account_id):
        if self.worker(account_id):
            self.worker(account_id).publish_now()

    def toggle_pause(self, account_id):
        a = self.db.get_account(account_id)
        if not a:
            return False
        paused = not a["queue_paused"]
        self.db.set_queue_paused(account_id, paused)
        self.wake(account_id)
        return paused

    def stop_all(self):
        workers = list(self.workers.values())
        for w in workers:
            w._stop.set()
            w.wake()
        for w in workers:
            w.stop(timeout=60)
        self.workers.clear()

    def stop_account(self, account_id):
        w = self.worker(account_id)
        if w:
            w.stop(timeout=30)
            if w._thread and w._thread.is_alive():
                raise ValueError("Дождитесь завершения запроса аккаунта")
            self.workers.pop(account_id, None)

    def reconcile_account(self, account_id):
        worker = self.worker(account_id) or AccountQueueWorker(account_id, self.db)
        return worker.reconcile_published_posts(force=True)

    def reconcile_all(self):
        return {a["id"]: self.reconcile_account(a["id"]) for a in self.db.get_accounts()}
