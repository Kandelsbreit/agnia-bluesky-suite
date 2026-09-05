from __future__ import annotations

import json
from tkinter import messagebox

import customtkinter as ctk

from app.ui.common import BLUE, GREEN, AccountSelector
from app.ui.composer import Composer


class PostingView(ctk.CTkFrame):
    def __init__(self, master, db, on_queue_changed, **kwargs):
        super().__init__(master, **kwargs)
        self.db = db
        self.on_queue_changed = on_queue_changed
        self.busy = False
        self.busy_account_id = None
        self._draft_account = None
        self._draft_job = None
        self._loading = False
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=12)
        self.selector = AccountSelector(top, db, on_change=self._switch)
        self.selector.pack(side="left")
        ctk.CTkLabel(top, text="Черновики сохраняются автоматически", text_color="gray60").pack(side="right")
        self.composer = Composer(self, on_change=self._changed)
        self.composer.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        self.text_box = self.composer.text_box
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=2, column=0, sticky="ew", padx=16, pady=12)
        self.publish_button = ctk.CTkButton(controls, text="Опубликовать сейчас", fg_color=GREEN, command=self.publish)
        self.publish_button.pack(side="left", padx=4)
        self.queue_button = ctk.CTkButton(controls, text="Добавить в очередь", fg_color=BLUE, command=self.add_to_queue)
        self.queue_button.pack(side="left", padx=4)
        ctk.CTkButton(controls, text="Очистить", width=80, command=self.clear).pack(side="left", padx=4)
        self.status = ctk.CTkLabel(self, text="Готово", wraplength=800)
        self.status.grid(row=3, column=0, sticky="ew", padx=16, pady=4)
        self._switch(self.selector.account_id)

    def _changed(self):
        if self._loading:
            return
        if self._draft_job:
            self.after_cancel(self._draft_job)
        self._draft_job = self.after(500, self.save_draft)

    def save_draft(self):
        self._draft_job = None
        if self._draft_account and self.db.get_account(self._draft_account):
            self.db.save_draft(
                self._draft_account, self.composer.text(), self.composer.media, self.composer.schedule.get()
            )

    def _switch(self, account_id):
        if account_id == self._draft_account:
            return
        if hasattr(self, "composer"):
            self.save_draft()
        self._draft_account = account_id
        if not hasattr(self, "composer"):
            return
        draft = self.db.get_draft(account_id) if account_id else {"content": "", "media_json": "[]"}
        self._loading = True
        self.composer.load(draft["content"], json.loads(draft["media_json"]))
        self.composer.schedule.insert(0, draft.get("schedule_text", ""))
        self._loading = False

    def refresh_accounts(self):
        self.selector.refresh()
        self._switch(self.selector.account_id)

    def _enqueue(self, now):
        aid = self.selector.account_id
        if not aid:
            messagebox.showwarning("Аккаунт", "Сначала добавьте аккаунт", parent=self)
            return
        try:
            text, media, scheduled = self.composer.value()
            account = self.db.get_account(aid)
            if now and not messagebox.askyesno(
                "Публикация", f"Отправить пост сейчас от @{account['handle']}?", parent=self
            ):
                return
            qid = self.db.enqueue_one(
                aid, text, at_top=now, media=media, scheduled_at=None if now else scheduled, send_now=now
            )
            if qid is None:
                raise ValueError("Этот пост уже есть в очереди или опубликован")
            self.on_queue_changed(aid)
            self.composer.load()
            self.save_draft()
            self.status.configure(
                text=f"Пост #{qid} сохранён. Результат отправки и повторы — в разделе «Очередь»."
                if now
                else f"Пост #{qid} добавлен в очередь."
            )
        except Exception as exc:
            messagebox.showerror("Публикация", str(exc), parent=self)

    def publish(self):
        self._enqueue(True)

    def add_to_queue(self):
        self._enqueue(False)

    def clear(self):
        self.composer.load()
        self.save_draft()
