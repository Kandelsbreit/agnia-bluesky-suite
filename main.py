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

    instance = None
    logger = None
    try:
        from app.backup import apply_staged_restore
        from app.instance import InstanceLock
        from app.paths import _migrate_legacy_db_if_needed, data_dir

        root = data_dir()
        instance = InstanceLock(root)
        if not instance.acquire():
            return 0
        _migrate_legacy_db_if_needed(root)
        apply_staged_restore(root)
        from app.database import Database
        from app.logging_setup import get_logger
        from app.ui.main_window import MainWindow

        logger = get_logger()
        db = Database()
        db.recover_interrupted()
        from app.backup import automatic_backup

        try:
            automatic_backup(db)
        except Exception:
            logger.exception("Не удалось создать автоматическую резервную копию")
        hidden = bool(options.tray or db.get_bool("start_minimized"))
        window = MainWindow(db, start_hidden=hidden)
    except Exception as exc:
        if logger:
            logger.exception("Критическая ошибка запуска: %s", exc)
        try:
            from tkinter import messagebox

            messagebox.showerror("Agnia Bluesky Suite", f"Программа не смогла запуститься: {exc}")
        except Exception:
            pass
        if instance:
            instance.release()
        return 1

    try:
        window.mainloop()
        return 0
    except Exception as exc:
        if "application has been destroyed" in str(exc).lower():
            return 0
        if logger:
            logger.exception("Ошибка главного цикла интерфейса: %s", exc)
        return 0
    finally:
        if instance:
            instance.release()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    exit_code = main()
    # A frozen windowed Tcl/Tk process can keep interpreter-shutdown hooks alive
    # even after the smoke window has been destroyed.  The smoke routine writes
    # its final checkpoint only after every assertion has passed, so terminate
    # explicitly once that checkpoint exists.  Normal application shutdown is
    # unaffected.
    if "--smoke-test" in sys.argv:
        os._exit(exit_code)
    raise SystemExit(exit_code)
