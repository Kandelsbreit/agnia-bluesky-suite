from __future__ import annotations

import threading
from collections.abc import Callable
from tkinter import messagebox

import customtkinter as ctk

from app.bluesky import BlueskyError, BlueskyGateway
from app.database import Database
from app.ui.common import BLUE, GREEN, RED, clear_children, ui_call
from app.utils import normalize_handle


class AccountDialog(ctk.CTkToplevel):
    def __init__(self, parent, db: Database, account: dict | None, on_saved: Callable[[], None]):
        super().__init__(parent)
        self.db = db
        self.account = account
        self.on_saved = on_saved
        self.verified_profile = None
        self.title("Добавить аккаунт" if not account else f"Настройки @{account['handle']}")
        self.geometry("540x430")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._build()

    def _build(self) -> None:
        form = ctk.CTkFrame(self)
        form.pack(fill="both", expand=True, padx=18, pady=18)
        form.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(form, text="Профиль Bluesky", font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 14)
        )
        ctk.CTkLabel(form, text="Handle:").grid(row=1, column=0, sticky="w", padx=14, pady=7)
        self.handle = ctk.CTkEntry(form, placeholder_text="name.bsky.social")
        self.handle.grid(row=1, column=1, sticky="ew", padx=14, pady=7)
        if self.account:
            self.handle.insert(0, self.account["handle"])
            self.handle.configure(state="disabled")

        ctk.CTkLabel(form, text="App Password:").grid(row=2, column=0, sticky="w", padx=14, pady=7)
        self.password = ctk.CTkEntry(
            form,
            show="•",
            placeholder_text="Оставьте пустым, чтобы сохранить текущий" if self.account else "xxxx-xxxx-xxxx-xxxx",
        )
        self.password.grid(row=2, column=1, sticky="ew", padx=14, pady=7)

        ctk.CTkLabel(form, text="Интервал очереди:").grid(row=3, column=0, sticky="w", padx=14, pady=7)
        interval_row = ctk.CTkFrame(form, fg_color="transparent")
        interval_row.grid(row=3, column=1, sticky="w", padx=14, pady=7)
        self.interval = ctk.CTkEntry(interval_row, width=80)
        self.interval.insert(0, str(self.account.get("interval_minutes", 60) if self.account else 60))
        self.interval.pack(side="left")
        ctk.CTkLabel(interval_row, text="минут  ±").pack(side="left", padx=6)
        self.jitter = ctk.CTkEntry(interval_row, width=70)
        self.jitter.insert(0, str(self.account.get("jitter_minutes", 2) if self.account else 2))
        self.jitter.pack(side="left")
        ctk.CTkLabel(interval_row, text="минут jitter").pack(side="left", padx=6)

        ctk.CTkLabel(
            form,
            text="App Password шифруется Windows DPAPI и открывается только этим пользователем Windows.",
            text_color="#8B98A8",
            wraplength=460,
            justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=14, pady=(10, 6))
        self.status = ctk.CTkLabel(form, text="", wraplength=460)
        self.status.grid(row=5, column=0, columnspan=2, sticky="w", padx=14, pady=6)

        buttons = ctk.CTkFrame(form, fg_color="transparent")
        buttons.grid(row=6, column=0, columnspan=2, sticky="ew", padx=14, pady=(14, 12))
        self.test_button = ctk.CTkButton(buttons, text="Проверить вход", fg_color=BLUE, command=self.test)
        self.test_button.pack(side="left")
        ctk.CTkButton(buttons, text="Отмена", width=90, command=self.destroy).pack(side="right", padx=(8, 0))
        ctk.CTkButton(buttons, text="Сохранить", width=105, fg_color=GREEN, command=self.save).pack(side="right")

    def _values(self) -> tuple[str, str | None, int, int] | None:
        handle = normalize_handle(self.account["handle"] if self.account else self.handle.get())
        entered_password = self.password.get().strip()
        if not handle:
            messagebox.showwarning("Данные аккаунта", "Укажите handle.", parent=self)
            return None
        if not self.account and not entered_password:
            messagebox.showwarning("Данные аккаунта", "Укажите App Password.", parent=self)
            return None
        try:
            interval = max(1, int(self.interval.get().strip()))
            jitter = max(0, int(self.jitter.get().strip()))
        except ValueError:
            messagebox.showwarning("Интервал", "Интервал и jitter должны быть целыми числами.", parent=self)
            return None
        return handle, entered_password if entered_password else None, interval, jitter

    def test(self) -> None:
        values = self._values()
        if not values:
            return
        handle, password, _, _ = values
        if password is None and self.account:
            _, password = self.db.get_account_secret(int(self.account["id"]))
        if not password:
            self.status.configure(text="Нет App Password для проверки", text_color=RED)
            return
        self.test_button.configure(state="disabled")
        self.status.configure(text="Проверка подключения...", text_color="#DFA947")

        def work() -> None:
            try:
                profile = BlueskyGateway(handle, password or "").test_connection()
                self.verified_profile = profile
                if self.account:
                    self.db.update_connection(int(self.account["id"]), "Подключён", profile.display_name, profile.did)
                ui_call(self, lambda: self._test_done(True, f"Подключено: {profile.display_name} (@{profile.handle})"))
            except BlueskyError as exc:
                if self.account:
                    self.db.update_connection(int(self.account["id"]), "Ошибка авторизации" if exc.auth_error else "Ошибка сети")
                ui_call(self, lambda exc=exc: self._test_done(False, str(exc)))

        threading.Thread(target=work, name="account-login-test", daemon=True).start()

    def _test_done(self, success: bool, message: str) -> None:
        self.test_button.configure(state="normal")
        self.status.configure(text=("Успешно: " if success else "Ошибка: ") + message, text_color=GREEN if success else RED)

    def save(self) -> None:
        values = self._values()
        if not values:
            return
        handle, password, interval, jitter = values
        self.db.save_account(
            handle,
            password,
            display_name=self.verified_profile.display_name if self.verified_profile else None,
            did=self.verified_profile.did if self.verified_profile else None,
            interval_minutes=interval,
            jitter_minutes=jitter,
        )
        self.destroy()
        self.on_saved()


class AccountsView(ctk.CTkFrame):
    def __init__(self, master, db: Database, on_changed: Callable[[], None], **kwargs):
        super().__init__(master, **kwargs)
        self.db = db
        self.on_changed = on_changed
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build()

    def _build(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 6))
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left")
        ctk.CTkLabel(title_box, text="Аккаунты", font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            title_box,
            text="Можно добавить два, три и больше профилей. Все разделы используют этот общий список.",
            text_color="#8B98A8",
        ).pack(anchor="w")
        ctk.CTkButton(header, text="Добавить аккаунт", fg_color=BLUE, command=self.add).pack(side="right")
        self.list_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list_frame.grid(row=1, column=0, sticky="nsew", padx=14, pady=(4, 14))
        self.list_frame.grid_columnconfigure(0, weight=1)

    def refresh(self) -> None:
        clear_children(self.list_frame)
        accounts = self.db.get_accounts()
        active = self.db.get_active_account()
        active_id = int(active["id"]) if active else None
        if not accounts:
            ctk.CTkLabel(
                self.list_frame,
                text="Аккаунтов пока нет. Добавьте первый профиль Bluesky.",
                text_color="#8B98A8",
                font=ctk.CTkFont(size=14),
            ).grid(row=0, column=0, pady=50)
            return
        for row_index, account in enumerate(accounts):
            card = ctk.CTkFrame(self.list_frame)
            card.grid(row=row_index, column=0, sticky="ew", padx=4, pady=5)
            card.grid_columnconfigure(0, weight=1)
            name = f"@{account['handle']}"
            if account.get("display_name") and account["display_name"] != account["handle"]:
                name += f" · {account['display_name']}"
            if int(account["id"]) == active_id:
                name += " · активный"
            ctk.CTkLabel(card, text=name, font=ctk.CTkFont(size=14, weight="bold"), anchor="w").grid(
                row=0, column=0, sticky="ew", padx=12, pady=(10, 2)
            )
            status = account.get("connection_status") or "Не проверен"
            details = (
                f"Состояние: {status} · очередь: каждые {account['interval_minutes']} мин. "
                f"± {account['jitter_minutes']} мин. · пароль: {'есть' if account['has_password'] else 'нет'}"
            )
            ctk.CTkLabel(card, text=details, anchor="w", text_color="#8B98A8").grid(
                row=1, column=0, sticky="ew", padx=12, pady=(2, 10)
            )
            buttons = ctk.CTkFrame(card, fg_color="transparent")
            buttons.grid(row=0, column=1, rowspan=2, padx=10, pady=8)
            ctk.CTkButton(buttons, text="Активный", width=82, command=lambda a=account: self.activate(a)).pack(side="left", padx=3)
            ctk.CTkButton(buttons, text="Проверить", width=82, command=lambda a=account: self.test(a)).pack(side="left", padx=3)
            ctk.CTkButton(buttons, text="Изменить", width=82, command=lambda a=account: self.edit(a)).pack(side="left", padx=3)
            ctk.CTkButton(
                buttons, text="Удалить", width=74, fg_color=RED, command=lambda a=account: self.delete(a)
            ).pack(side="left", padx=3)

    def add(self) -> None:
        AccountDialog(self, self.db, None, self._saved)

    def edit(self, account: dict) -> None:
        AccountDialog(self, self.db, account, self._saved)

    def _saved(self) -> None:
        self.on_changed()
        self.refresh()

    def activate(self, account: dict) -> None:
        self.db.set_active_account(int(account["id"]))
        self.on_changed()
        self.refresh()

    def test(self, account: dict) -> None:
        account_id = int(account["id"])

        def work() -> None:
            current, password = self.db.get_account_secret(account_id)
            if not current or not password:
                self.db.update_connection(account_id, "Нужен App Password")
            else:
                try:
                    profile = BlueskyGateway(current["handle"], password).test_connection()
                    self.db.update_connection(account_id, "Подключён", profile.display_name, profile.did)
                except BlueskyError as exc:
                    self.db.update_connection(account_id, "Ошибка авторизации" if exc.auth_error else "Ошибка сети")
            ui_call(self, self.refresh)

        threading.Thread(target=work, name=f"account-check-{account_id}", daemon=True).start()

    def delete(self, account: dict) -> None:
        queued = self.db.queue_count(int(account["id"]))
        text = f"Удалить @{account['handle']}?"
        if queued:
            text += f"\nБудут удалены и {queued} ожидающих постов этого аккаунта."
        if messagebox.askyesno("Удаление аккаунта", text, parent=self):
            self.db.delete_account(int(account["id"]))
            self.on_changed()
            self.refresh()
