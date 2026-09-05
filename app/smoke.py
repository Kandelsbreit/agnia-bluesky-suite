from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def run_smoke_test() -> None:
    """Offline smoke test used against the packaged Windows executable."""
    progress = Path(tempfile.gettempdir(), "agnia-bluesky-smoke-progress.txt")

    def checkpoint(message: str) -> None:
        progress.write_text(message, encoding="utf-8")

    checkpoint("importing packaged dependencies")
    import atproto  # noqa: F401
    import customtkinter  # noqa: F401

    if sys.platform == "win32":
        import pystray  # noqa: F401

    from app.database import Database
    from app.importer import parse_content
    from app.paths import resource_path
    from app.ui.main_window import MainWindow

    checkpoint("checking database and queue")
    db = Database()
    first = db.save_account("first.example.test", None, interval_minutes=60, jitter_minutes=2)
    second = db.save_account("second.example.test", None, interval_minutes=90, jitter_minutes=3)
    assert first != second
    assert len(db.get_accounts()) == 2

    parsed = parse_content(
        "@account: first.example.test\n\nПервый тестовый пост.\n\n---\n\n"
        "@account: second.example.test\n\nВторой тестовый пост."
    )
    assert len(parsed) == 2 and all(item.valid for item in parsed)
    added, duplicates = db.enqueue_many(
        [{"account_handle": item.account_handle, "content": item.content} for item in parsed]
    )
    assert (added, duplicates) == (2, 0)
    added, duplicates = db.enqueue_many(
        [{"account_handle": item.account_handle, "content": item.content} for item in parsed]
    )
    assert (added, duplicates) == (0, 2)

    queued = db.next_queue_item(first)
    assert queued and queued["record_key"]
    assert db.complete_queue_item(int(queued["id"]), "at://smoke/post", "smoke-cid")
    assert db.enqueue_one(first, "Первый тестовый пост.") is None

    reopened = Database(db.path)
    assert reopened.queue_count(second) == 1
    assert reopened.stats()["published"] == 1
    for asset in ("assets/icon.ico", "assets/icon.png"):
        assert Path(resource_path(asset)).is_file(), asset

    # Do not construct a real Tk window in GitHub Actions. Hosted Windows runners
    # can run GUI processes in a non-interactive desktop session, where Tk may
    # block indefinitely even though the packaged application itself is valid.
    # Importing MainWindow above still verifies that the packaged Tcl/Tk,
    # CustomTkinter and all view modules can be resolved. Validate the expected
    # navigation layout without entering the GUI event loop.
    checkpoint("checking GUI class")
    assert tuple(key for key, _label in MainWindow.NAVIGATION) == (
        "likes",
        "following",
        "posting",
        "queue",
        "export",
        "accounts",
        "settings",
    )
    checkpoint("completed")
