from __future__ import annotations

import threading
from collections.abc import Callable
from tkinter import messagebox

import customtkinter as ctk

from app.bluesky import BlueskyError, BlueskyGateway
from app.database import Database
from app.ui.common import BLUE, GREEN, RED, AccountSelector, ui_call
from app.utils import MAX_POST_BYTES, MAX_POST_GRAPHEMES, count_graphemes, new_record_key, post_validation_error


class PostingView(ctk.CTkFrame):
    def __init__(self, master, db: Database, on_queue_changed: Callable[[int], None], **kwargs):
        super().__init__(master, **kwargs)
        self.db = db
        self.on_queue_changed = on_queue_changed
        self.busy = False
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build()

    def _build(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 6))
        ctk.CTkLabel(header, text="Ручная публикация", font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(header, text="Текстовый пост в выбранный аккаунт или добавление в его очередь.", text_color="#8B98A8").pack(anchor="w")

        top = ctk.CTkFrame(self)
        top.grid(row=1, column=0, sticky="ew", padx=18, pady=8)
        self.selector = AccountSelector(top, self.db)
        self.selector.pack(side="left", padx=12, pady=10)
        self.counter = ctk.CTkLabel(top, text="0/300", font=ctk.CTkFont(size=14, weight="bold"))
        self.counter.pack(side="right", padx=14)

        self.text_box = ctk.CTkTextbox(self, wrap="word", font=ctk.CTkFont(size=14))
        self.text_box.grid(row=2, column=0, sticky="nsew", padx=18, pady=6)
        self.text_box.bind("<KeyRelease>", lambda _event: self._update_counter())

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=3, column=0, sticky="ew", padx=18, pady=(6, 14))
        self.publish_button = ctk.CTkButton(controls, text="Опубликовать", fg_color=GREEN, command=self.publish)
        self.publish_button.pack(side="left", padx=(0, 8))
        self.queue_button = ctk.CTkButton(controls, text="Добавить в очередь", fg_color=BLUE, command=self.add_to_queue)
        self.queue_button.pack(side="left", padx=8)
        ctk.CTkButton(controls, text="Очистить", fg_color="#46505C", width=90, command=self.clear).pack(side="left", padx=8)
        self.status = ctk.CTkLabel(controls, text="Готово", anchor="e")
        self.status.pack(side="right", fill="x", expand=True)

    def refresh_accounts(self) -> None:
        self.selector.refresh()

    def _text(self) -> str:
        return self.text_box.get("1.0", "end-1c").strip()

    def _update_counter(self) -> None:
        text = self._text()
        count = count_graphemes(text)
        byte_count = len(text.encode("utf-8"))
        too_long = count > MAX_POST_GRAPHEMES or byte_count > MAX_POST_BYTES
        self.counter.configure(
            text=f"{count}/{MAX_POST_GRAPHEMES} · {byte_count}/{MAX_POST_BYTES} байт",
            text_color=RED if too_long else ("gray20", "gray90"),
        )

    def _validated(self) -> tuple[int, str] | None:
        account_id = self.selector.account_id
        text = self._text()
        if not account_id:
            messagebox.showwarning("Нет аккаунта", "Сначала добавьте аккаунт.", parent=self)
            return None
        if not text:
            messagebox.showwarning("Пустой пост", "Введите текст поста.", parent=self)
            return None
        validation_error = post_validation_error(text)
        if validation_error:
            messagebox.showerror("Слишком длинный пост", validation_error, parent=self)
            return None
        return account_id, text

    def publish(self) -> None:
        if self.busy:
            return
        checked = self._validated()
        if not checked:
            return
        account_id, text = checked
        if not messagebox.askyesno("Публикация", "Опубликовать этот пост сейчас?", parent=self):
            return
        self.busy = True
        self.publish_button.configure(state="disabled")
        self.status.configure(text="Публикация...")
        record_key = new_record_key()

        def work() -> None:
            try:
                account, password = self.db.get_account_secret(account_id)
                if not account or not password:
                    raise BlueskyError("У аккаунта нет сохранённого App Password", auth_error=True)
                gateway = BlueskyGateway(account["handle"], password)
                result = gateway.publish_text(text, record_key)
                self.db.record_published_post(account_id, text, record_key, result.uri, result.cid)
                self.db.record_activity(
                    account_id,
                    "manual_post",
                    "success",
                    target_key=result.uri,
                    message="Ручной пост опубликован",
                )
                if gateway.profile:
                    self.db.update_connection(account_id, "Подключён", gateway.profile.display_name, gateway.profile.did)
                ui_call(self, lambda: self._publish_done(True, f"Опубликовано: {result.uri}"))
            except BlueskyError as exc:
                self.db.record_activity(account_id, "manual_post", "error", message=str(exc))
                if exc.auth_error:
                    self.db.update_connection(account_id, "Ошибка авторизации")
                ui_call(self, lambda exc=exc: self._publish_done(False, str(exc)))

        threading.Thread(target=work, name="manual-post", daemon=True).start()

    def _publish_done(self, success: bool, message: str) -> None:
        self.busy = False
        self.publish_button.configure(state="normal")
        self.status.configure(text=message, text_color=GREEN if success else RED)
        if success:
            self.clear()
        else:
            messagebox.showerror("Ошибка Bluesky API", message, parent=self)

    def add_to_queue(self) -> None:
        checked = self._validated()
        if not checked:
            return
        account_id, text = checked
        queue_id = self.db.enqueue_one(account_id, text)
        if queue_id is None:
            self.status.configure(text="Такой пост уже есть в очереди или истории", text_color=RED)
            return
        self.db.record_activity(account_id, "queue_add", "success", target_key=str(queue_id), message="Добавлен вручную")
        self.on_queue_changed(account_id)
        self.status.configure(text="Добавлено в очередь", text_color=GREEN)
        self.clear(keep_status=True)

    def clear(self, keep_status: bool = False) -> None:
        self.text_box.delete("1.0", "end")
        self._update_counter()
        if not keep_status:
            self.status.configure(text="Готово", text_color=("gray20", "gray90"))
