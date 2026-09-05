from __future__ import annotations

from datetime import date

from app.bluesky import Profile
from app.exporter import ExportOptions, export_account, write_combined_queue


def feed_item(uri, text, created, *, author="did:me", handle="me.test", reason=None, reply=False, parent="did:other"):
    item = {
        "post": {
            "uri": uri,
            "author": {"did": author, "handle": handle},
            "record": {"text": text, "createdAt": created},
        }
    }
    if reason is not None:
        item["reason"] = reason
    if reply:
        item["post"]["record"]["reply"] = {"parent": {"uri": "at://parent"}}
        item["reply"] = {"parent": {"author": {"did": parent}}}
    return item


class ExportGateway:
    def __init__(self, pages):
        self.pages = pages

    def resolve_profile(self, _actor):
        return Profile("me.test", "did:me", "Me", posts_count=20)

    def fetch_author_feed(self, _actor, *, limit, cursor=None):
        index = int(cursor or 0)
        page = self.pages[index]
        return page, str(index + 1) if index + 1 < len(self.pages) else None


def test_export_filters_reposts_replies_dates_and_duplicates(tmp_path):
    gateway = ExportGateway(
        [
            [
                feed_item("at://1", "Keep", "2026-02-02T00:00:00Z"),
                feed_item("at://2", "Keep", "2026-02-01T00:00:00Z"),
                feed_item("at://3", "Repost", "2026-02-01T00:00:00Z", reason={"by": "x"}),
                feed_item("at://4", "Reply", "2026-02-01T00:00:00Z", reply=True),
                feed_item("at://5", "Old", "2025-01-01T00:00:00Z"),
            ]
        ]
    )
    result = export_account(
        gateway,
        "me.test",
        tmp_path,
        ExportOptions(replies="exclude", date_from=date(2026, 1, 1), date_to=date(2026, 12, 31)),
    )
    content = result.files[0].read_text(encoding="utf-8")
    assert "@account: me.test" in content and "Keep" in content
    assert "Reply" not in content and "Repost" not in content and "Old" not in content
    assert result.stats.posts == 1
    assert result.stats.duplicates_skipped == 1
    assert result.stats.reposts_skipped == 1
    assert result.stats.replies_skipped == 1


def test_export_can_keep_self_thread_and_split_other_replies(tmp_path):
    gateway = ExportGateway(
        [
            [
                feed_item("at://1", "Thread continuation", "2026-02-02T00:00:00Z", reply=True, parent="did:me"),
                feed_item("at://2", "Reply elsewhere", "2026-02-03T00:00:00Z", reply=True),
            ]
        ]
    )
    result = export_account(
        gateway,
        "me.test",
        tmp_path,
        ExportOptions(replies="separate", self_threads_as_posts=True),
    )
    assert len(result.files) == 2
    assert "Thread continuation" in result.files[0].read_text(encoding="utf-8")
    assert "Reply elsewhere" in result.files[1].read_text(encoding="utf-8")


def test_ai_export_is_one_plain_original_posts_file(tmp_path):
    gateway = ExportGateway(
        [
            [
                feed_item("at://1", "Original voice", "2026-02-02T00:00:00Z"),
                feed_item("at://2", "A reply", "2026-02-03T00:00:00Z", reply=True),
            ]
        ]
    )
    result = export_account(gateway, "me.test", tmp_path, ExportOptions(ai_export=True, replies="include"))
    assert result.files[0].name == "original_posts_me.test.txt"
    text = result.files[0].read_text(encoding="utf-8")
    assert text.strip() == "Original voice"
    assert "@account:" not in text and "A reply" not in text


def test_combined_queue_contains_each_account(tmp_path):
    first = export_account(
        ExportGateway([[feed_item("at://1", "First", "2026-02-02T00:00:00Z")]]),
        "me.test",
        tmp_path,
        ExportOptions(),
    )
    second_gateway = ExportGateway([[feed_item("at://2", "Second", "2026-02-03T00:00:00Z", handle="other.test")]])
    second_gateway.resolve_profile = lambda _actor: Profile("other.test", "did:me", "Other", posts_count=1)
    second = export_account(second_gateway, "other.test", tmp_path, ExportOptions())
    path = write_combined_queue(tmp_path / "combined.txt", [second, first])
    text = path.read_text(encoding="utf-8")
    assert text.index("@account: me.test") < text.index("@account: other.test")
    reversed_path = write_combined_queue(
        tmp_path / "combined-newest.txt",
        [second, first],
        oldest_first=False,
    )
    reversed_text = reversed_path.read_text(encoding="utf-8")
    assert reversed_text.index("@account: other.test") < reversed_text.index("@account: me.test")
