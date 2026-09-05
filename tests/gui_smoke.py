"""Offline real-Tk integration test. Run with xvfb-run -a python tests/gui_smoke.py."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run():
    with tempfile.TemporaryDirectory(prefix="agnia-gui-") as tmp:
        os.environ["AGNIA_BLUESKY_DATA_DIR"] = tmp
        from PIL import Image, ImageGrab

        from app.database import Database
        from app.media import import_media
        from app.ui.common import drain_ui_callbacks
        from app.ui.composer import Composer
        from app.ui.main_window import MainWindow

        db = Database()
        a = db.save_account("first.example.test")
        b = db.save_account("second.example.test")
        qid = db.enqueue_one(a, "Проверка очереди: текст, ссылка https://example.org и #Петербург")
        db.enqueue_one(a, "Пост по расписанию", scheduled_at=(datetime.now(UTC) + timedelta(days=1)).isoformat())
        db.enqueue_one(b, "Второй аккаунт")
        db.set_queue_paused(a, True)
        root = MainWindow(db, enable_background=False)
        errors = []
        root.report_callback_exception = lambda *args: errors.append(str(args[1]))

        def pump():
            for _ in range(5):
                root.update()
                drain_ui_callbacks()
                time.sleep(0.03)

        def descendants(parent):
            for child in parent.winfo_children():
                yield child
                yield from descendants(child)

        def close_dialogs():
            for w in list(descendants(root)):
                if w.winfo_exists() and w.winfo_class() == "Toplevel":
                    w.destroy()

        def screenshot(name):
            pump()
            folder = Path(os.getenv("AGNIA_GUI_SCREENSHOTS", str(Path(tmp) / "screenshots")))
            folder.mkdir(parents=True, exist_ok=True)
            ImageGrab.grab().save(folder / (name + ".png"))

        with (
            patch("tkinter.messagebox.askyesno", return_value=True),
            patch("tkinter.messagebox.showinfo"),
            patch("tkinter.messagebox.showerror") as showerror,
        ):
            for name, _ in root.NAVIGATION:
                root.show_view(name)
                pump()
            assert db.get_account(a)["queue_paused"]
            root.show_view("posting")
            posting = root.views["posting"]
            posting.composer.load("Черновик первого аккаунта")
            posting.save_draft()
            posting.selector.refresh(b)
            posting._switch(b)
            assert posting.composer.text() == ""
            posting.composer.load("Черновик второго аккаунта")
            posting.save_draft()
            posting.selector.refresh(a)
            posting._switch(a)
            assert posting.composer.text() == "Черновик первого аккаунта"
            photo = Path(tmp) / "image.png"
            Image.new("RGB", (400, 220), "steelblue").save(photo)
            media = import_media(photo)
            media["alt"] = "Синее тестовое изображение"
            posting.composer.load("Публикация с картинкой", [media])
            screenshot("posting")
            posting.composer.preview()
            pump()
            screenshot("preview")
            close_dialogs()
            posting.add_to_queue()
            pump()
            assert db.queue_count(a) == 3
            queue = root.views["queue"]
            root.show_view("queue")
            queue.selector.refresh(a)
            queue.refresh()
            pump()
            assert str(qid) in queue.tree.get_children()
            queue.tree.selection_set(str(qid))
            queue.edit_selected()
            pump()
            dialogs = [w for w in descendants(root) if w.winfo_class() == "Toplevel"]
            assert dialogs
            editor = next(w for d in dialogs for w in d.winfo_children() if isinstance(w, Composer))
            editor.load("Исправленный текст")
            # Invoke the dialog's Save button, not the database directly.
            buttons = [
                w for w in dialogs[-1].winfo_children() if hasattr(w, "cget") and "button" in type(w).__name__.lower()
            ]
            buttons[-1].invoke()
            pump()
            assert db.get_queue_item(qid)["content"] == "Исправленный текст"
            queue.search.insert(0, "ИСПРАВЛЕННЫЙ")
            queue._search()
            pump()
            assert queue.tree.get_children() == (str(qid),)
            queue.search.delete(0, "end")
            queue._search()
            screenshot("queue")
            queue.show_calendar()
            pump()
            screenshot("calendar")
            close_dialogs()
            queue.tree.selection_set(str(qid))
            queue.skip_next()
            pump()
            assert db.get_queue_item(qid) is None
            queue.show_history()
            pump()
            screenshot("history")
            close_dialogs()
            root.show_view("settings")
            pump()
            screenshot("settings")
            assert not errors, errors
            assert not showerror.called, showerror.call_args_list
        root._maintenance_stop.set()
        from app.logging_setup import set_ui_callback

        set_ui_callback(None)
        root.destroy()
        print(
            "GUI smoke passed: all 7 tabs, drafts, media preview, queue editor, search, skip, calendar, history, settings; no live API calls."
        )


if __name__ == "__main__":
    run()
