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


class PermanentFailureGateway(SuccessfulGateway):
    calls = []

    def publish_text(self, text, record_key):
        self.__class__.calls.append((text, record_key))
        raise BlueskyError("invalid record", retryable=False)


def test_non_retryable_api_error_preserves_failed_post(db):
    PermanentFailureGateway.calls = []
    account = db.save_account("invalid.test", "password", interval_minutes=60, jitter_minutes=0)
    qid = db.enqueue_one(account, "Keep invalid item")
    worker = AccountQueueWorker(account, db, gateway_factory=PermanentFailureGateway)
    worker.start()
    try:
        worker.publish_now()
        assert wait_until(lambda: db.get_queue_item(qid)["state"] == "failed")
        assert db.queue_count(account) == 1
        assert db.get_history(account) == []
        assert "invalid record" in db.get_queue_item(qid)["last_error"]
        assert not bool(db.get_account(account)["queue_paused"])
    finally:
        worker.stop()


class DuplicateGateway(SuccessfulGateway):
    def check_recent_post(self, text):
        if text == "Already on Bluesky":
            return PublishResult("at://did/post/existing", "existing-cid", recovered_existing=True)
        return None


def test_deduplication_prevents_reposting_from_another_machine(db):
    account = db.save_account("dedup.test", "password", interval_minutes=60, jitter_minutes=0)
    db.enqueue_one(account, "Already on Bluesky")
    worker = AccountQueueWorker(account, db, gateway_factory=DuplicateGateway)
    worker.start()
    try:
        worker.publish_now()
        assert wait_until(lambda: db.queue_count(account) == 0)
        history = db.get_history(account)
        assert len(history) == 1
        assert history[0]["post_uri"] == "at://did/post/existing"
        assert history[0]["status"] == "published"
    finally:
        worker.stop()


class ReconcileGateway(SuccessfulGateway):
    def get_author_recent_posts(self, actor, limit=100):
        return [
            {
                "text": "Overnight published post",
                "uri": "at://did/app.bsky.feed.post/reconciled-key",
                "cid": "reconciled-cid",
                "created_at": "2026-09-05T01:00:00Z",
                "rkey": "reconciled-key",
            }
        ]


def test_worker_startup_and_scheduler_reconciliation(db):
    account = db.save_account("reconcile.worker.test", "password", interval_minutes=60, jitter_minutes=0)
    db.enqueue_one(account, "Overnight published post")
    db.enqueue_one(account, "Pending post")
    assert db.queue_count(account) == 2

    worker = AccountQueueWorker(account, db, gateway_factory=ReconcileGateway)
    reconciled = worker.reconcile_published_posts(force=True)
    assert reconciled == 1
    assert db.queue_count(account) == 1
    remaining = db.next_queue_item(account)
    assert remaining["content"] == "Pending post"

    history = db.get_history(account)
    assert len(history) == 1
    assert history[0]["content"] == "Overnight published post"
    assert history[0]["post_uri"] == "at://did/app.bsky.feed.post/reconciled-key"
