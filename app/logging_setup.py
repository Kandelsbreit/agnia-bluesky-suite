from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.paths import logs_dir

LOGGER_NAME = "agnia_bluesky"
LOG_FILE = logs_dir() / "app.log"
_callback_lock = threading.Lock()
_ui_callback: Callable[[str, str], None] | None = None


class _UICallbackHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        with _callback_lock:
            callback = _ui_callback
        if callback:
            try:
                callback(self.format(record), record.levelname.lower())
            except Exception:
                pass


def configure_logging() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=4, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    ui_handler = _UICallbackHandler()
    ui_handler.setFormatter(formatter)
    logger.addHandler(ui_handler)
    return logger


def get_logger() -> logging.Logger:
    return configure_logging()


def set_ui_callback(callback: Callable[[str, str], None] | None) -> None:
    global _ui_callback
    with _callback_lock:
        _ui_callback = callback


def read_log_tail(max_lines: int = 300) -> str:
    try:
        lines = Path(LOG_FILE).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except OSError:
        return ""

