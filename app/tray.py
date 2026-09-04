from __future__ import annotations

import sys
import threading
from collections.abc import Callable

from app.logging_setup import get_logger


class TrayManager:
    def __init__(self, icon_path, on_show: Callable[[], None], on_hide: Callable[[], None], on_quit: Callable[[], None]):
        self.icon_path = icon_path
        self.on_show = on_show
        self.on_hide = on_hide
        self.on_quit = on_quit
        self._icon = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if sys.platform != "win32" or self._thread:
            return
        try:
            import pystray
            from PIL import Image

            image = Image.open(self.icon_path)
            menu = pystray.Menu(
                pystray.MenuItem("Показать", lambda *_: self.on_show(), default=True),
                pystray.MenuItem("Скрыть", lambda *_: self.on_hide()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", lambda *_: self.on_quit()),
            )
            self._icon = pystray.Icon("AgniaBlueskySuite", image, "Agnia Bluesky Suite", menu)
            self._thread = threading.Thread(target=self._icon.run, name="system-tray", daemon=True)
            self._thread.start()
        except Exception as exc:
            get_logger().warning("Не удалось запустить системный трей: %s", exc)

    def update_tooltip(self, text: str) -> None:
        if self._icon:
            try:
                self._icon.title = f"Agnia Bluesky Suite — {text}"
            except Exception:
                pass

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
        self._icon = None
        self._thread = None

