from __future__ import annotations

from types import SimpleNamespace

from app.automation import AutomationOptions, AutomationWorker, LikeAutomationManager
from app.bluesky import ActionResult


class LikesGateway:
    liked = []

    def __init__(self, handle, password):
        self.handle = handle

    def login(self):
        return SimpleNamespace(did="did:me", display_name="Me")

    def get_timeline(self, limit, cursor):
        def item(uri, did="did:other", *, viewer=None, reply=False, reason=None):
            value = {
                "post": {
                    "uri": uri,
                    "cid": "cid",
                    "author": {"did": did, "handle": "author.test"},
                    "record": {"text": "post"},
                    "viewer": viewer or {},
                }
            }
            if reply:
                value["post"]["record"]["reply"] = {"parent": {}}
            if reason:
                value["reason"] = reason
            return value

        return [
            item("at://own", "did:me"),
            item("at://liked", viewer={"like": "at://like"}),
            item("at://reply", reply=True),
            item("at://repost", reason={"by": "x"}),
            item("at://valid"),
        ], None

    def like(self, uri, cid):
        self.__class__.liked.append((uri, cid))
        return ActionResult(True, False, "ok", target_key=uri)


def test_likes_skip_own_reply_repost_and_existing(db):
    LikesGateway.liked = []
    account = db.save_account("me.test", "password")
    events = []
    worker = AutomationWorker(
        db,
        account,
        "likes",
        AutomationOptions(limit=1, human_breaks=False),
        skip_replies=True,
        skip_reposts=True,
        callback=events.append,
        gateway_factory=LikesGateway,
    )
    worker.start()
    worker.join(3)
    assert not worker.is_alive()
    assert LikesGateway.liked == [("at://valid", "cid")]
    assert db.action_was_successful(account, "like", "at://valid")
    finished = [event for event in events if event["kind"] == "finished"][0]
    assert finished["stats"]["completed"] == 1
    assert finished["stats"]["skipped"] == 4


class FollowGateway:
    followed = []

    def __init__(self, handle, password):
        self.handle = handle

    def login(self):
        return SimpleNamespace(did="did:me", display_name="Me")

    def follow(self, target):
        self.__class__.followed.append(target)
        if target == "existing.test":
            return ActionResult(False, True, "Уже есть подписка", target, "did:existing")
        return ActionResult(True, False, "ok", target, f"did:{target}")


def test_following_normalizes_deduplicates_and_records_journal(db):
    FollowGateway.followed = []
    account = db.save_account("me.test", "password")
    worker = AutomationWorker(
        db,
        account,
        "following",
        AutomationOptions(limit=1, human_breaks=False),
        targets=["@EXISTING.TEST", "existing.test", "https://bsky.app/profile/new.test"],
        gateway_factory=FollowGateway,
    )
    worker.start()
    worker.join(3)
    assert FollowGateway.followed == ["existing.test", "new.test"]
    activity = db.get_activity()
    assert {row["status"] for row in activity} == {"skipped", "success"}


class EmptyGateway(LikesGateway):
    def get_timeline(self, limit, cursor):
        return [], None


def test_stop_releases_paused_worker(db):
    account = db.save_account("me.test", "password")
    worker = AutomationWorker(
        db,
        account,
        "likes",
        AutomationOptions(limit=5),
        gateway_factory=EmptyGateway,
    )
    worker.pause()
    worker.start()
    worker.stop()
    worker.join(2)
    assert not worker.is_alive()


def test_like_automation_manager_multi_account(db):
    LikesGateway.liked = []
    account1 = db.save_account("user1.test", "pass1")
    account2 = db.save_account("user2.test", "pass2")
    events = []
    manager = LikeAutomationManager(db, callback=events.append, gateway_factory=LikesGateway)

    started = manager.start_accounts(
        [account1, account2],
        AutomationOptions(limit=1, human_breaks=False),
    )
    assert started == 2
    w1 = manager.get_worker(account1)
    w2 = manager.get_worker(account2)
    assert w1 is not None and w2 is not None
    w1.join(3)
    w2.join(3)
    assert not manager.is_running()
    assert db.action_was_successful(account1, "like", "at://valid")
    assert db.action_was_successful(account2, "like", "at://valid")


def test_like_automation_manager_pause_and_stop_all(db):
    account1 = db.save_account("user1.test", "pass1")
    account2 = db.save_account("user2.test", "pass2")
    manager = LikeAutomationManager(db, gateway_factory=EmptyGateway)

    started = manager.start_accounts(
        [account1, account2],
        AutomationOptions(limit=10, human_breaks=False),
    )
    assert started == 2
    assert manager.is_running()

    resumed = manager.toggle_pause_all()
    assert not resumed
    assert manager.is_paused()

    manager.stop_all()
    w1 = manager.get_worker(account1)
    w2 = manager.get_worker(account2)
    w1.join(2)
    w2.join(2)
    assert not manager.is_running()
