from __future__ import annotations

import sqlite3


def test_accounts_are_normalized_encrypted_and_switchable(db):
    first = db.save_account("@First.BSky.Social", "first-password")
    second = db.save_account("second.bsky.social", "second-password", interval_minutes=90, jitter_minutes=4)
    assert first != second
    assert db.get_account("FIRST.BSKY.SOCIAL")["handle"] == "first.bsky.social"
    account, secret = db.get_account_secret(first)
    assert account["has_password"] is True
    assert secret == "first-password"
    db.set_active_account(second)
    assert db.get_active_account()["id"] == second


def test_updating_account_without_password_preserves_secret(db):
    account_id = db.save_account("one.test", "secret", interval_minutes=60)
    db.save_account("one.test", None, interval_minutes=75, jitter_minutes=5)
    account, secret = db.get_account_secret(account_id)
    assert account["interval_minutes"] == 75
    assert account["jitter_minutes"] == 5
    assert secret == "secret"


def test_queue_is_per_account_and_deduplicates_pending_and_history(db):
    first = db.save_account("one.test")
    second = db.save_account("two.test")
    first_item = db.enqueue_one(first, "Same text")
    assert first_item
    assert db.enqueue_one(first, " Same text ") is None
    assert db.enqueue_one(second, "Same text") is not None
    assert db.complete_queue_item(first_item, "at://post", "cid")
    assert db.enqueue_one(first, "Same text") is None
    assert db.queue_count(first) == 0
    assert db.queue_count(second) == 1
    assert db.post_exists(first, "Same text")
    assert db.post_exists(second, "Same text")
    assert not db.post_exists(first, "Different")


def test_bulk_enqueue_handles_thousands_and_duplicates(db):
    source = []
    for index in range(2500):
        source.append({"account_handle": f"account{index % 2}.test", "content": f"Post {index}"})
    source.extend(source[:25])
    added, duplicates = db.enqueue_many(source)
    assert added == 2500
    assert duplicates == 25
    accounts = db.get_accounts()
    assert len(accounts) == 2
    assert sum(db.queue_count(int(account["id"])) for account in accounts) == 2500


def test_queue_order_can_move_up_down_top_and_bottom(db):
    account = db.save_account("order.test")
    ids = [db.enqueue_one(account, text) for text in ("a", "b", "c")]
    assert db.move_queue_item(account, ids[2], "top")
    assert [row["content"] for row in db.get_queue(account)] == ["c", "a", "b"]
    assert db.move_queue_item(account, ids[2], "down")
    assert [row["content"] for row in db.get_queue(account)] == ["a", "c", "b"]
    assert db.move_queue_item(account, ids[0], "bottom")
    assert [row["content"] for row in db.get_queue(account)] == ["c", "b", "a"]


def test_failed_attempt_retains_item_and_error(db):
    account = db.save_account("retry.test")
    queue_id = db.enqueue_one(account, "Do not lose me")
    db.mark_attempt_failed(queue_id, "network down")
    item = db.next_queue_item(account)
    assert item["attempt_count"] == 1
    assert item["last_error"] == "network down"
    assert item["content"] == "Do not lose me"


def test_skipped_item_enters_history_and_dedup_set(db):
    account = db.save_account("skip.test")
    queue_id = db.enqueue_one(account, "skip this")
    assert db.complete_queue_item(queue_id, "", "", status="skipped")
    history = db.get_history(account)
    assert history[0]["status"] == "skipped"
    assert db.enqueue_one(account, "skip this") is None


def test_confirmed_publish_wins_over_concurrent_delete_or_skip(db):
    account = db.save_account("race.test")
    queue_id = db.enqueue_one(account, "in flight")
    snapshot = db.next_queue_item(account)
    assert db.complete_queue_item(queue_id, "", "", status="skipped")
    assert db.complete_queue_item(
        queue_id,
        "at://confirmed",
        "confirmed-cid",
        snapshot=snapshot,
    )
    history = db.get_history(account)
    assert history[0]["status"] == "published"
    assert history[0]["post_uri"] == "at://confirmed"
    assert db.queue_count(account) == 0


def test_manual_publish_enters_dedup_history_and_removes_matching_queue(db):
    account = db.save_account("manual.test")
    db.enqueue_one(account, "same manual text")
    db.record_published_post(
        account,
        "same manual text",
        "manual-key",
        "at://manual",
        "manual-cid",
    )
    assert db.queue_count(account) == 0
    assert db.enqueue_one(account, "same manual text") is None
    history = db.get_history(account)
    assert history[0]["record_key"] == "manual-key"
    assert history[0]["status"] == "published"


def test_runtime_and_settings_survive_reopen(db):
    account = db.save_account("persist.test")
    db.enqueue_one(account, "persistent")
    db.set_settings({"theme": "light", "like_limit": 22})
    db.update_runtime(account, next_scheduled_at="2026-01-02T00:00:00+00:00", retry_count=3, last_error="x")
    reopened = type(db)(db.path)
    assert reopened.get_setting("theme") == "light"
    assert reopened.get_int("like_limit", 0) == 22
    assert reopened.queue_count(account) == 1
    assert reopened.get_account(account)["retry_count"] == 3


def test_database_enables_wal_and_foreign_keys(db):
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    with db._connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_account_delete_cascades_queue_and_selects_replacement(db):
    first = db.save_account("delete.test")
    second = db.save_account("remain.test")
    db.enqueue_one(first, "gone")
    db.set_active_account(first)
    db.delete_account(first)
    assert db.get_account(first) is None
    assert db.get_active_account()["id"] == second


def test_activity_history_is_scoped_by_account(db):
    first = db.save_account("first.test")
    second = db.save_account("second.test")
    db.record_activity(first, "like", "success", target_key="at://post/1")
    assert db.action_was_successful(first, "like", "at://post/1")
    assert not db.action_was_successful(second, "like", "at://post/1")


def test_heal_legacy_queue_keys_converts_uuids_and_unpauses(db):
    account = db.save_account("heal.test")
    with db._write() as conn:
        conn.execute(
            "INSERT INTO queue (account_id, position, record_key, content, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (account, 1, "08c529508337455f930c3af8219561dd", "legacy post", "hash123", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "UPDATE accounts SET queue_paused = 1, last_error = 'Invalid TID string' WHERE id = ?",
            (account,),
        )
    reopened = type(db)(db.path)
    item = reopened.next_queue_item(account)
    assert item is not None
    assert len(item["record_key"]) == 13
    assert item["record_key"] != "08c529508337455f930c3af8219561dd"
    acc = reopened.get_account(account)
    assert acc["queue_paused"] == 0


def test_post_exists_in_history_and_prune_queue(db):
    from app.utils import content_hash

    account = db.save_account("history.test")
    item_id = db.enqueue_one(account, "history-text")
    assert item_id is not None
    assert db.post_exists_in_history(account, "history-text") is None
    db.complete_queue_item(item_id, "at://post/1", "cid-1")
    assert db.post_exists_in_history(account, "history-text") is not None
    digest = content_hash("history-text")
    with db._write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO queue (account_id, position, record_key, content, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (account, 1, "3mupnujqcnfhv", "history-text", digest, "2026-01-01T00:00:00+00:00"),
        )
    reopened = type(db)(db.path)
    assert reopened.queue_count(account) == 0


def test_enqueue_one_at_top_vs_bottom(db):
    account = db.save_account("priority.test")
    id1 = db.enqueue_one(account, "bulk-1", at_top=False)
    id2 = db.enqueue_one(account, "bulk-2", at_top=False)
    assert id1 is not None and id2 is not None

    # First item should be bulk-1
    first = db.next_queue_item(account)
    assert first["content"] == "bulk-1"

    # Now add manual post with default at_top=True
    manual_id = db.enqueue_one(account, "urgent manual post", at_top=True)
    assert manual_id is not None

    # Next queue item must now be the urgent manual post!
    next_item = db.next_queue_item(account)
    assert next_item["id"] == manual_id
    assert next_item["content"] == "urgent manual post"


def test_reconcile_queue_with_published(db):
    account = db.save_account("reconcile.test")
    db.enqueue_one(account, "post-alpha", at_top=False)
    db.enqueue_one(account, "post-beta", at_top=False)
    db.enqueue_one(account, "post-gamma", at_top=False)
    assert db.queue_count(account) == 3

    published = [
        {
            "text": "post-beta",
            "uri": "at://did:plc:test/app.bsky.feed.post/3mupnujqcnfhv",
            "cid": "bafyreibeta",
            "created_at": "2026-09-05T08:00:00Z",
            "rkey": "3mupnujqcnfhv",
        }
    ]
    reconciled = db.reconcile_queue_with_published(account, published)
    assert reconciled == 1
    assert db.queue_count(account) == 2

    # Verify post-beta was moved to history
    history = db.get_history(account)
    assert len(history) == 1
    assert history[0]["content"] == "post-beta"
    assert history[0]["status"] == "published"
    assert history[0]["post_uri"] == "at://did:plc:test/app.bsky.feed.post/3mupnujqcnfhv"



