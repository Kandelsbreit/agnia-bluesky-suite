from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
import tempfile
from pathlib import Path


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="AgniaBlueskySuite", add_help=not getattr(sys, "frozen", False))
    parser.add_argument("--tray", action="store_true", help="start with the main window hidden")
    parser.add_argument("--smoke-test", action="store_true", help="run an offline packaged-app test and exit")
    parser.add_argument("--version", action="store_true")
    return parser.parse_args(argv)


def _run_smoke() -> int:
    failure = Path(tempfile.gettempdir(), "agnia-bluesky-smoke-failure.txt")
    progress = Path(tempfile.gettempdir(), "agnia-bluesky-smoke-progress.txt")
    failure.unlink(missing_ok=True)
    progress.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="agnia-bsky-smoke-") as temporary:
        os.environ["AGNIA_BLUESKY_DATA_DIR"] = temporary
        try:
            from app.smoke import run_smoke_test

            run_smoke_test()
            progress.unlink(missing_ok=True)
            return 0
        except Exception:
            import traceback

            failure.write_text(traceback.format_exc(), encoding="utf-8")
            return 1


def main(argv: list[str] | None = None) -> int:
    options = _arguments(argv)
    if options.version:
        from app import __version__

        print(__version__)
        return 0
    if options.smoke_test:
        return _run_smoke()

    from app.database import Database
    from app.logging_setup import get_logger
    from app.ui.main_window import MainWindow

    logger = get_logger()
    try:
        db = Database()
        hidden = bool(options.tray or db.get_bool("start_minimized"))
        window = MainWindow(db, start_hidden=hidden)
        window.mainloop()
        return 0
    except Exception as exc:
        logger.exception("Критическая ошибка запуска: %s", exc)
        try:
            from tkinter import messagebox

            messagebox.showerror(
                "Agnia Bluesky Suite",
                "Программа не смогла запуститься. Подробности сохранены в журнале app.log.",
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
