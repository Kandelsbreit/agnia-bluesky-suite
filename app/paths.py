from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "AgniaBlueskySuite"
DATA_DIR_ENV = "AGNIA_BLUESKY_DATA_DIR"


def resource_path(relative: str) -> Path:
    """Return an asset path in source and PyInstaller one-file builds."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


def app_dir() -> Path:
    """Directory containing the executable or main script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _is_dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / f".writable_test_{os.getpid()}"
        test_file.write_text("ok", encoding="ascii")
        test_file.unlink()
        return True
    except OSError:
        return False


def _legacy_system_data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_DIR_NAME
    return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_DIR_NAME


def _migrate_legacy_db_if_needed(target_dir: Path) -> None:
    target_db = target_dir / "agnia_bluesky.db"
    if target_db.exists():
        return
    legacy_dir = _legacy_system_data_dir()
    legacy_db = legacy_dir / "agnia_bluesky.db"
    if not legacy_db.exists():
        return
    try:
        if legacy_dir.resolve() == target_dir.resolve():
            return
    except OSError:
        pass
    import os
    import sqlite3

    temp = target_db.with_suffix(".migrating")
    try:
        with sqlite3.connect(legacy_db) as source, sqlite3.connect(temp) as dest:
            source.backup(dest)
            dest.execute("PRAGMA journal_mode=DELETE")
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise OSError("Исходная база повреждена")
        key = legacy_dir / ".secret_key"
        if key.exists():
            (target_dir / ".secret_key").write_bytes(key.read_bytes())
        import shutil

        if (legacy_dir / "media").exists():
            shutil.copytree(legacy_dir / "media", target_dir / "media", dirs_exist_ok=True)
        os.replace(temp, target_db)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def data_dir() -> Path:
    override = os.getenv(DATA_DIR_ENV)
    if override:
        root = Path(override).expanduser().resolve()
    else:
        exe_folder = app_dir()
        if (exe_folder / "agnia_bluesky.db").exists():
            root = exe_folder
        else:
            portable_data = exe_folder / "data"
            if _is_dir_writable(portable_data):
                root = portable_data
            else:
                root = _legacy_system_data_dir()

    root.mkdir(parents=True, exist_ok=True)
    return root


def database_path() -> Path:
    return data_dir() / "agnia_bluesky.db"


def logs_dir() -> Path:
    result = data_dir() / "logs"
    result.mkdir(parents=True, exist_ok=True)
    return result


def exports_dir() -> Path:
    result = data_dir() / "exports"
    result.mkdir(parents=True, exist_ok=True)
    return result
