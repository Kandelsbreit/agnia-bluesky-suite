from __future__ import annotations

import json
import sqlite3
import threading
import zipfile
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image
from test_scheduler import wait_until

from app.backup import apply_staged_restore, create_backup, stage_restore
from app.bluesky import BlueskyError, BlueskyGateway, Profile, PublishResult
from app.database import Database
from app.exporter import ExportOptions, export_account
from app.instance import InstanceLock
from app.media import import_media, media_path, payload_hash
from app.scheduler import AccountQueueWorker
from app.security import SecretError, protect_secret, unprotect_secret


def test_repost_cannot_remove_own_post(db):
    aid = db.save_account("own.test", "password")
    db.enqueue_one(aid, "same")
    gw = BlueskyGateway("own.test", "password")
    gw.login = lambda: Profile("own.test", "did:plc:own", "Own")
    gw.fetch_author_feed = lambda *a, **k: (
        [
            {
                "reason": {"$type": "app.bsky.feed.defs#reasonRepost"},
                "post": {
                    "uri": "at://did:plc:other/app.bsky.feed.post/key",
                    "cid": "cid",
                    "author": {"did": "did:plc:other"},
                    "record": {"text": "same"},
                },
            }
        ],
        None,
    )
    assert gw.check_recent_post("same") is None
    assert db.reconcile_queue_with_published(aid, gw.get_author_recent_posts("own.test")) == 0
    assert db.queue_count(aid) == 1


def test_transient_request_error_does_not_report_empty_sync():
    gw = BlueskyGateway("own.test", "password")
    gw.login = lambda: Profile("own.test", "did:plc:own", "Own")

    def fail(*a, **k):
        raise BlueskyError("offline", retryable=True)

    gw.fetch_author_feed = fail
    with pytest.raises(BlueskyError, match="offline"):
        gw.get_author_recent_posts("own.test")


def test_claimed_post_cannot_be_changed_or_deleted(db):
    aid = db.save_account("own.test")
    qid = db.enqueue_one(aid, "A")
    other = db.enqueue_one(aid, "B")
    db.claim_queue_item(qid)
    for action in [
        lambda: db.delete_queue_item(qid),
        lambda: db.clear_queue(aid),
        lambda: db.bulk_delete([other, qid]),
        lambda: db.edit_queue_item(qid, "changed", [], None),
        lambda: db.complete_queue_item(qid, "", "", "skipped"),
    ]:
        with pytest.raises(ValueError):
            action()
    assert db.queue_count(aid) == 2
    assert db.claim_queue_item(qid) is None


def test_error_remains_attached_to_original_post_after_reorder(db):
    aid = db.save_account("own.test", "password")
    a = db.enqueue_one(aid, "A")
    b = db.enqueue_one(aid, "B")

    class Fail:
        profile = None

        def __init__(self, *args):
            pass

        def publish_text(self, text, key):
            db.move_queue_item(aid, b, "top")
            raise BlueskyError("invalid A", code="InvalidRecord")

    w = AccountQueueWorker(aid, db, gateway_factory=Fail)
    w.start()
    try:
        db.request_send(a)
        w.wake()
        assert wait_until(lambda: db.get_queue_item(a)["state"] == "failed")
        assert db.get_queue_item(b)["state"] == "pending"
        assert db.get_history(aid) == []
    finally:
        w.stop()


def test_export_past_month_reaches_older_pages(tmp_path):
    class Feed:
        calls = 0

        def resolve_profile(self, actor):
            return Profile("own.test", "did:plc:own", "Own", posts_count=2)

        def fetch_author_feed(self, *a, **k):
            self.calls += 1
            day = "2026-09-05" if self.calls == 1 else "2026-08-15"
            return (
                [
                    {
                        "post": {
                            "uri": str(self.calls),
                            "author": {"did": "did:plc:own"},
                            "record": {"text": day, "createdAt": day + "T00:00:00Z"},
                        }
                    }
                ],
                "older" if self.calls == 1 else None,
            )

    gw = Feed()
    result = export_account(
        gw, "own.test", tmp_path, ExportOptions(date_from=date(2026, 8, 1), date_to=date(2026, 8, 31))
    )
    assert gw.calls == 2
    assert result.posts == [("2026-08-15T00:00:00Z", "2026-08-15")]


def test_cancel_does_not_overwrite_previous_export(tmp_path):
    path = tmp_path / "own.test_posts.txt"
    path.write_text("previous")

    class Feed:
        def resolve_profile(self, actor):
            return Profile("own.test", "did:plc:own", "Own")

    cancel = threading.Event()
    cancel.set()
    result = export_account(Feed(), "own.test", tmp_path, ExportOptions(), cancel_event=cancel)
    assert result.cancelled
    assert path.read_text() == "previous"
    assert result.files == []


def test_key_write_failure_does_not_save_password(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNIA_BLUESKY_DATA_DIR", str(tmp_path))
    with patch("app.security.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(SecretError, match="не сохранён"):
            protect_secret("dummy")
    assert not (tmp_path / ".secret_key").exists()


def test_missing_key_not_silently_regenerated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNIA_BLUESKY_DATA_DIR", str(tmp_path))
    stored = protect_secret("dummy")
    (tmp_path / ".secret_key").unlink()
    with pytest.raises(SecretError, match="отсутствует"):
        unprotect_secret(stored)
    assert not (tmp_path / ".secret_key").exists()


def test_encrypted_backup_roundtrip_including_media(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setenv("AGNIA_BLUESKY_DATA_DIR", str(root))
    db = Database(root / "agnia_bluesky.db")
    aid = db.save_account("own.test", "test-password")
    photo = tmp_path / "photo.png"
    Image.new("RGB", (10, 10), "red").save(photo)
    m = import_media(photo)
    db.enqueue_one(aid, "picture", media=[m])
    db.save_draft(aid, "draft", [m])
    archive = create_backup(db, tmp_path / "backup.agnia", "backup-password")
    assert not zipfile.is_zipfile(archive)
    target = tmp_path / "restored"
    target.mkdir()
    with pytest.raises(ValueError, match="пароль"):
        stage_restore(archive, target, "wrong")
    stage_restore(archive, target, "backup-password")
    assert apply_staged_restore(target)
    monkeypatch.setenv("AGNIA_BLUESKY_DATA_DIR", str(target))
    reopened = Database(target / "agnia_bluesky.db")
    assert reopened.get_account_secret(aid)[1] == "test-password"
    assert reopened.get_draft(aid)["content"] == "draft"
    assert media_path(json.loads(reopened.get_queue(aid)[0]["media_json"])[0]).exists()


def test_backup_includes_uncheckpointed_wal(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNIA_BLUESKY_DATA_DIR", str(tmp_path))
    db = Database(tmp_path / "agnia_bluesky.db")
    with sqlite3.connect(db.path) as keeper:
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        aid = db.save_account("own.test")
        db.enqueue_one(aid, "in WAL")
        archive = create_backup(db, tmp_path / "copy.zip")
        with zipfile.ZipFile(archive) as z:
            copied = tmp_path / "copied.db"
            copied.write_bytes(z.read("agnia_bluesky.db"))
        with sqlite3.connect(copied) as c:
            assert c.execute("SELECT content FROM queue").fetchone()[0] == "in WAL"


def test_backup_rejects_zip_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../escape", "bad")
    with pytest.raises(ValueError):
        stage_restore(archive, tmp_path)
    assert not (tmp_path.parent / "escape").exists()


def test_media_owned_copy_and_content_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNIA_BLUESKY_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (200, 100), "blue").save(path)
    media = import_media(path)
    path.unlink()
    assert media_path(media).exists()
    assert payload_hash("same", []) != payload_hash("same", [media])
    assert payload_hash("same", [media]) == payload_hash("same", [{**media, "name": "renamed"}])


def test_calendar_due_post_not_blocked_by_future_post(db):
    aid = db.save_account("own.test")
    future = datetime.now(UTC) + timedelta(days=2)
    past = datetime.now(UTC) - timedelta(minutes=1)
    db.enqueue_one(aid, "future", scheduled_at=future.isoformat())
    normal = db.enqueue_one(aid, "normal")
    due = db.enqueue_one(aid, "due", scheduled_at=past.isoformat())
    assert db.next_queue_item(aid)["id"] == due
    db.delete_queue_item(due)
    assert db.next_queue_item(aid)["id"] == normal


def test_interrupted_send_recovers_same_key_and_record(db):
    aid = db.save_account("own.test")
    qid = db.enqueue_one(aid, "durable", send_now=True)
    original = db.claim_queue_item(qid)
    record = {"text": "durable", "createdAt": "2026-09-05T01:00:00Z"}
    db.save_prepared_record(qid, record)
    reopened = Database(db.path)
    reopened.recover_interrupted()
    item = reopened.next_queue_item(aid)
    assert item["state"] == "uncertain"
    assert item["record_key"] == original["record_key"]
    assert json.loads(item["record_json"]) == record


def test_restore_skipped_media_post(db, tmp_path):
    photo = tmp_path / "a.png"
    Image.new("RGB", (10, 10)).save(photo)
    m = import_media(photo)
    aid = db.save_account("own.test")
    qid = db.enqueue_one(aid, "skip", media=[m])
    db.complete_queue_item(qid, "", "", "skipped")
    restored = db.restore_history(db.get_history(aid)[0]["id"])
    assert json.loads(db.get_queue_item(restored)["media_json"]) == [m]


def test_single_instance_lock(tmp_path):
    one = InstanceLock(tmp_path)
    two = InstanceLock(tmp_path)
    assert one.acquire()
    try:
        assert not two.acquire()
    finally:
        one.release()
    assert two.acquire()
    two.release()


def test_saved_media_record_is_reused_after_timeout(db):
    aid = db.save_account("own.test", "password")
    qid = db.enqueue_one(aid, "durable", send_now=True)

    class Fake:
        profile = SimpleNamespace(did="did:plc:own", display_name="Own")
        prepares = 0
        records = []

        def __init__(self, *args):
            pass

        def prepare_record(self, text, media):
            Fake.prepares += 1
            return {"text": text, "createdAt": "2026-09-05T00:00:00Z"}

        def publish_record(self, record, key):
            Fake.records.append((record, key))
            if len(Fake.records) == 1:
                raise BlueskyError("timeout", retryable=True)
            return PublishResult("at://did:plc:own/app.bsky.feed.post/" + key, "cid", True)

    w = AccountQueueWorker(aid, db, gateway_factory=Fake)
    w.start()
    try:
        assert wait_until(lambda: db.get_queue_item(qid)["state"] == "uncertain")
        db.request_send(qid)
        w.wake()
        assert wait_until(lambda: db.get_queue_item(qid) is None)
        assert Fake.prepares == 1
        assert Fake.records[0] == Fake.records[1]
    finally:
        w.stop()


def test_prepared_image_video_link_records_use_valid_sdk_models(tmp_path, monkeypatch):
    from atproto_client.models.blob_ref import BlobRef

    monkeypatch.setenv("AGNIA_BLUESKY_DATA_DIR", str(tmp_path / "data"))
    cid = "bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku"

    class Client:
        def login(self, *args):
            return SimpleNamespace(did="did:plc:own", handle="own.test")

        def get_current_time_iso(self):
            return "2026-09-05T00:00:00Z"

        def upload_blob(self, raw):
            mime = "video/mp4" if raw[4:8] == b"ftyp" else "image/png"
            blob = BlobRef(ref={"$link": cid}, mime_type=mime, size=len(raw))
            return SimpleNamespace(blob=blob)

    gw = BlueskyGateway("own.test", "password", client_factory=lambda **kw: Client())
    photo = tmp_path / "image.png"
    Image.new("RGB", (16, 12), "red").save(photo)
    image = import_media(photo)
    image["alt"] = "Alt text"
    rec = gw.prepare_record("Photo", [image])
    assert rec["embed"]["$type"] == "app.bsky.embed.images"
    assert rec["embed"]["images"][0]["alt"] == "Alt text"
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(32))
    rec = gw.prepare_record("Video", [import_media(video)])
    assert rec["embed"]["$type"] == "app.bsky.embed.video"
    rec = gw.prepare_record(
        "Link", [{"kind": "link", "uri": "https://example.com", "title": "Title", "description": "Description"}]
    )
    assert rec["embed"]["external"]["title"] == "Title"


def test_record_verification_rejects_mismatched_media():
    from test_bluesky import FakeClient

    client = FakeClient(record_exists=True)
    gateway = BlueskyGateway("me.test", "app-password", client_factory=lambda **kw: client)
    with pytest.raises(BlueskyError, match="вложения"):
        gateway._verify_record("existing", "Hello", {"text": "Hello", "embed": {"$type": "app.bsky.embed.external"}})
    assert client.created == []


def test_existing_database_is_backed_up_before_schema_upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNIA_BLUESKY_DATA_DIR", str(tmp_path))
    db = Database(tmp_path / "agnia_bluesky.db")
    aid = db.save_account("own.test")
    db.enqueue_one(aid, "Legacy")
    with sqlite3.connect(db.path) as c:
        c.execute("PRAGMA user_version=1")
    reopened = Database(db.path)
    assert reopened.queue_count(aid) == 1
    archives = list((tmp_path / "backups").glob("before-v1.3-*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as z:
        copied = tmp_path / "old.db"
        copied.write_bytes(z.read("agnia_bluesky.db"))
    with sqlite3.connect(copied) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 1


def test_request_rate_limit_date_header():
    from email.utils import format_datetime

    from test_bluesky import ResponseError

    from app.bluesky import _error_details

    exc = ResponseError("rate limited", 429, "RateLimitExceeded")
    exc.response.headers["Retry-After"] = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    details = _error_details(exc)
    assert 115 <= details.retry_after <= 120
    assert details.rate_limited and details.retryable
