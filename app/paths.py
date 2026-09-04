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


def data_dir() -> Path:
    override = os.getenv(DATA_DIR_ENV)
    if override:
        root = Path(override).expanduser().resolve()
    elif sys.platform == "win32":
        root = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_DIR_NAME
    else:
        root = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_DIR_NAME
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

