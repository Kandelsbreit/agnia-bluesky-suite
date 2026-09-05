from __future__ import annotations

import queue
from collections.abc import Callable

import customtkinter as ctk

from app.database import Database

BLUE = "#1683FF"
GREEN = "#18B981"
RED = "#E84D6A"
AMBER = "#E29A2D"
MUTED = "#8B98A8"


class AccountSelector(ctk.CTkFrame):
    def __init__(
        self,
        master,
        db: Database,
        *,
        on_change: Callable[[int | None], None] | None = None,
        title: str = "Аккаунт:",
    ):
        super().__init__(master, fg_color="transparent")
        self.db = db
        self.on_change = on_change
        self._labels: dict[str, int] = {}
        self._selected_id: int | None = None
        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=(0, 8))
        self.variable = ctk.StringVar(value="")
        self.combo = ctk.CTkComboBox(
            self,
            width=260,
            state="readonly",
            variable=self.variable,
            values=["Нет аккаунтов"],
            command=self._changed,
        )
        self.combo.pack(side="left")
        self.refresh()

    @property
    def account_id(self) -> int | None:
        return self._selected_id

    @property
    def menu(self) -> ctk.CTkComboBox:
        return self.combo

    def set_state(self, state: str) -> None:
        self.combo.configure(state=state)

    def refresh(self, preferred_id: int | None = None) -> None:
        accounts = self.db.get_accounts()
        self._labels = {f"@{item['handle']}": int(item["id"]) for item in accounts}
        if not accounts:
            self.combo.configure(values=["Нет аккаунтов"])
            self.variable.set("Нет аккаунтов")
            self._selected_id = None
            return
        wanted = preferred_id
        if wanted is None:
            active = self.db.get_active_account()
            wanted = int(active["id"]) if active else int(accounts[0]["id"])
        labels = list(self._labels)
        selected_label = next((label for label, value in self._labels.items() if value == wanted), labels[0])
        self.combo.configure(values=labels)
        self.variable.set(selected_label)
        self._selected_id = self._labels[selected_label]

    def _changed(self, label: str) -> None:
        self._selected_id = self._labels.get(label)
        if self._selected_id:
            self.db.set_active_account(self._selected_id)
        if self.on_change:
            self.on_change(self._selected_id)


def clear_children(widget) -> None:
    for child in widget.winfo_children():
        child.destroy()


_callbacks = queue.SimpleQueue()


def ui_call(widget, callback: Callable[[], None]) -> None:
    # Tk calls only run on the main thread, including .after and winfo_exists.
    _callbacks.put((widget, callback))


def drain_ui_callbacks() -> None:
    for _ in range(300):
        try:
            widget, callback = _callbacks.get_nowait()
        except queue.Empty:
            return
        try:
            try:
                exists = widget.winfo_exists()
            except Exception:
                exists = False
            if exists:
                callback()
        except Exception:
            from app.logging_setup import get_logger

            get_logger().exception("Ошибка обновления интерфейса")

