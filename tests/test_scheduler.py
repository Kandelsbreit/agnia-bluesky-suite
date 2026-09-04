from __future__ import annotations

import time
from types import SimpleNamespace

from app.bluesky import BlueskyError, PublishResult
from app.scheduler import AccountQueueWorker


def wait_until(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.03)
    return False


class SuccessfulGateway:
    calls = []

    def __init__(self, handle, password):
        self.handle = handle
        self.password = password
        self.profile = SimpleNamespace(display_name="Name", did="did:account")

    def publish_text(self, text, record_key):
        self.__class__.calls.append((text, record_key))
        return PublishResult(f"at://did/post/{record_key}", "cid")


def test_first_queue_item_waits_for_interval_until_publish_now(db):
    SuccessfulGateway.calls = []
    account = db.save_account("queue.test", "password", interval_minutes=60, jitter_minutes=0)
    queued = db.enqueue_one(account, "Post later")
    record_key = db.next_queue_item(account)["record_key"]
    worker = AccountQueueWorker(account, db, gateway_factory=SuccessfulGateway)
    worker.start()
    try:
        assert wait_until(lambda: worker.status == "Ожидание")
        assert db.queue_count(account) == 1
        assert SuccessfulGateway.calls == []
        worker.publish_now()
        assert wait_until(lambda: db.queue_count(account) == 0)
        assert SuccessfulGateway.calls == [("Post later", record_key)]
        history = db.get_history(account)
        assert history[0]["record_key"] == record_key
        assert history[0]["status"] == "published"
        runtime = db.get_account(account)
        assert runtime["last_posted_at"]
        assert runtime["next_scheduled_at"]
        assert runtime["retry_count"] == 0
        assert queued is not None
    finally:
        worker.stop()


class FailOnceGateway(SuccessfulGateway):
    calls = []

    def publish_text(self, text, record_key):
        self.__class__.calls.append((text, record_key))
        if len(self.__class__.calls) == 1:
            raise BlueskyError("temporary network", retryable=True)
        return PublishResult(f"at://did/post/{record_key}", "cid", recovered_existing=True)


def test_retry_keeps_post_and_reuses_record_key(db):
    FailOnceGateway.calls = []
    account = db.save_account("retry.test", "password", interval_minutes=60, jitter_minutes=0)
    db.enqueue_one(account, "Stable identity")
    key = db.next_queue_item(account)["record_key"]
    worker = AccountQueueWorker(account, db, gateway_factory=FailOnceGateway)
    worker.start()
    try:
        worker.publish_now()
        assert wait_until(lambda: (db.next_queue_item(account) or {}).get("attempt_count") == 1)
        assert db.queue_count(account) == 1
        assert "temporary network" in db.next_queue_item(account)["last_error"]
        worker.publish_now()
        assert wait_until(lambda: db.queue_count(account) == 0)
        assert FailOnceGateway.calls == [("Stable identity", key), ("Stable identity", key)]
    finally:
        worker.stop()


def test_paused_queue_does_not_publish_on_schedule(db):
    SuccessfulGateway.calls = []
    account = db.save_account("paused.test", "password", interval_minutes=1, jitter_minutes=0)
    db.enqueue_one(account, "Paused")
    db.set_queue_paused(account, True)
    worker = AccountQueueWorker(account, db, gateway_factory=SuccessfulGateway)
    worker.start()
    try:
        assert wait_until(lambda: worker.status == "На паузе")
        time.sleep(0.15)
        assert SuccessfulGateway.calls == []
        assert db.queue_count(account) == 1
    finally:
        worker.stop()


def test_missing_password_is_reported_without_losing_post(db):
    account = db.save_account("no-secret.test")
    db.enqueue_one(account, "Stay queued")
    worker = AccountQueueWorker(account, db, gateway_factory=SuccessfulGateway)
    worker.start()
    try:
        assert wait_until(lambda: worker.status == "Нужен App Password")
        assert db.queue_count(account) == 1
    finally:
        worker.stop()
