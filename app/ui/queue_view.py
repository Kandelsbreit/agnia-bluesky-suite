from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from tkinter import filedialog, messagebox

import customtkinter as ctk

from app.database import Database
from app.importer import import_files
from app.scheduler import QueueScheduler
from app.ui.common import AMBER, BLUE, GREEN, RED, AccountSelector, clear_children, ui_call
from app.utils import count_graphemes, format_duration

PAGE_SIZE = 40


class QueueView(ctk.CTkFrame):
    def __init__(
        self,
        master,
        db: Database,
        scheduler: QueueScheduler,
        on_accounts_changed: Callable[[], None],
        **kwargs,
    ):
        super().__init__(master, **kwargs)
        self.db = db
        self.scheduler = scheduler
        self.on_accounts_changed = on_accounts_changed
        self.page = 0
        self.cached_next_id: int | None = None
        self.import_running = False
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._build()

    def _build(self) -> None:
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 4))
        self.selector = AccountSelector(top, self.db, on_change=self._account_changed)
        self.selector.pack(side="left")
        self.import_button = ctk.CTkButton(top, text="Импорт TXT", fg_color=BLUE, command=self.import_txt)
        self.import_button.pack(side="right", padx=(8, 0))
        ctk.CTkButton(top, text="История", width=90, command=self.show_history).pack(side="right", padx=4)
        self.errors_button = ctk.CTkButton(top, text="Ошибки импорта", width=125, command=self.show_errors)
        self.errors_button.pack(side="right", padx=4)

        next_card = ctk.CTkFrame(self)
        next_card.grid(row=1, column=0, sticky="ew", padx=18, pady=8)
        next_card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(next_card, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 3))
        ctk.CTkLabel(header, text="Следующий пост", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        self.worker_status = ctk.CTkLabel(header, text="Нет аккаунта", text_color="#8B98A8")
        self.worker_status.pack(side="right")
        self.timer = ctk.CTkLabel(header, text="", text_color="#8FBFFF")
        self.timer.pack(side="right", padx=16)
        self.preview = ctk.CTkLabel(next_card, text="Очередь пуста", justify="left", anchor="w", wraplength=780)
        self.preview.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        buttons = ctk.CTkFrame(next_card, fg_color="transparent")
        buttons.grid(row=2, column=0, sticky="w", padx=12, pady=(3, 10))
        self.now_button = ctk.CTkButton(buttons, text="Опубликовать сейчас", fg_color=GREEN, command=self.publish_now)
        self.now_button.pack(side="left", padx=(0, 6))
        self.pause_button = ctk.CTkButton(buttons, text="Пауза", fg_color=AMBER, command=self.toggle_pause)
        self.pause_button.pack(side="left", padx=6)
        self.skip_button = ctk.CTkButton(buttons, text="Пропустить", width=105, command=self.skip_next)
        self.skip_button.pack(side="left", padx=6)
        self.delete_next_button = ctk.CTkButton(buttons, text="Удалить", width=90, fg_color=RED, command=self.delete_next)
        self.delete_next_button.pack(side="left", padx=6)

        list_header = ctk.CTkFrame(self, fg_color="transparent")
        list_header.grid(row=2, column=0, sticky="ew", padx=18, pady=(6, 0))
        self.count_label = ctk.CTkLabel(list_header, text="Очередь: 0", font=ctk.CTkFont(size=14, weight="bold"))
        self.count_label.pack(side="left")
        self.import_status = ctk.CTkLabel(list_header, text="", text_color="#8B98A8")
        self.import_status.pack(side="left", padx=16)
        ctk.CTkButton(list_header, text="Очистить очередь", width=125, fg_color=RED, command=self.clear_queue).pack(side="right")

        self.list_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list_frame.grid(row=3, column=0, sticky="nsew", padx=14, pady=6)
        self.list_frame.grid_columnconfigure(0, weight=1)

        pages = ctk.CTkFrame(self, fg_color="transparent")
        pages.grid(row=4, column=0, sticky="ew", padx=18, pady=(0, 12))
        self.prev_button = ctk.CTkButton(pages, text="Предыдущая", width=100, command=lambda: self.change_page(-1))
        self.prev_button.pack(side="left")
        self.page_label = ctk.CTkLabel(pages, text="Страница 1/1")
        self.page_label.pack(side="left", expand=True)
        self.next_button = ctk.CTkButton(pages, text="Следующая", width=100, command=lambda: self.change_page(1))
        self.next_button.pack(side="right")

    @property
    def account_id(self) -> int | None:
        return self.selector.account_id

    def refresh_accounts(self) -> None:
        selected = self.account_id
        self.selector.refresh(selected)
        self.refresh()

    def _account_changed(self, _account_id: int | None) -> None:
        self.page = 0
        self.refresh()

    def refresh(self) -> None:
        account_id = self.account_id
        clear_children(self.list_frame)
        errors = len(self.db.get_import_errors())
        self.errors_button.configure(text=f"Ошибки импорта ({errors})" if errors else "Ошибки импорта")
        if not account_id:
            self.preview.configure(text="Добавьте аккаунт или импортируйте TXT с метками @account:.")
            self.count_label.configure(text="Очередь: 0")
            self._set_actions(False)
            return
        account = self.db.get_account(account_id)
        count = self.db.queue_count(account_id)
        total_pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(self.page, total_pages - 1)
        self.page_label.configure(text=f"Страница {self.page + 1}/{total_pages}")
        self.prev_button.configure(state="normal" if self.page else "disabled")
        self.next_button.configure(state="normal" if self.page + 1 < total_pages else "disabled")
        self.count_label.configure(text=f"Очередь @{account['handle']}: {count}")
        self.pause_button.configure(text="Продолжить" if account.get("queue_paused") else "Пауза")

        next_item = self.db.next_queue_item(account_id)
        self.cached_next_id = int(next_item["id"]) if next_item else None
        if next_item:
            preview = next_item["content"]
            if len(preview) > 420:
                preview = preview[:420] + "…"
            if account.get("last_error"):
                error = str(account["last_error"])
                preview += f"\n\nПоследняя ошибка: {error[:240]}{'…' if len(error) > 240 else ''}"
            self.preview.configure(text=f"{preview}\n\n{count_graphemes(next_item['content'])}/300 графем")
            self._set_actions(True)
        else:
            self.preview.configure(text="Очередь этого аккаунта пуста.")
            self._set_actions(False)

        rows = self.db.get_queue(account_id, PAGE_SIZE, self.page * PAGE_SIZE)
        for index, item in enumerate(rows, start=self.page * PAGE_SIZE + 1):
            row = ctk.CTkFrame(self.list_frame)
            row.grid(row=index, column=0, sticky="ew", padx=3, pady=3)
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(row, text=f"#{index}", width=48, text_color="#8FBFFF").grid(row=0, column=0, padx=6, pady=6)
            snippet = item["content"].replace("\n", " ")
            if len(snippet) > 105:
                snippet = snippet[:105] + "…"
            ctk.CTkLabel(row, text=f"{snippet}  ({count_graphemes(item['content'])}/300)", anchor="w").grid(
                row=0, column=1, sticky="ew", padx=4
            )
            actions = ctk.CTkFrame(row, fg_color="transparent")
            actions.grid(row=0, column=2, padx=5, pady=4)
            ctk.CTkButton(actions, text="▲", width=30, command=lambda q=item["id"]: self.move(q, "up")).pack(side="left", padx=2)
            ctk.CTkButton(actions, text="▼", width=30, command=lambda q=item["id"]: self.move(q, "down")).pack(side="left", padx=2)
            ctk.CTkButton(
                actions, text="×", width=30, fg_color=RED, command=lambda q=item["id"]: self.delete_item(q)
            ).pack(side="left", padx=2)

    def _set_actions(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in (self.now_button, self.skip_button, self.delete_next_button):
            button.configure(state=state)

    def update_tick(self) -> None:
        account_id = self.account_id
        if not account_id:
            return
        current = self.db.next_queue_item(account_id)
        current_id = int(current["id"]) if current else None
        if current_id != self.cached_next_id:
            self.refresh()
            return
        worker = self.scheduler.worker(account_id)
        if not worker:
            return
        self.worker_status.configure(text=worker.status)
        if worker.status != "Публикация" and self.cached_next_id:
            self.now_button.configure(state="normal")
        account = self.db.get_account(account_id) or {}
        is_paused = bool(account.get("queue_paused"))
        wanted_text = "Продолжить" if is_paused else "Пауза"
        if self.pause_button.cget("text") != wanted_text:
            self.pause_button.configure(text=wanted_text)
        if worker.next_run_timestamp:
            remaining = worker.next_run_timestamp - time.time()
            clock = datetime.fromtimestamp(worker.next_run_timestamp).strftime("%H:%M:%S")
            self.timer.configure(text=f"{clock} · через {format_duration(remaining)}")
        else:
            self.timer.configure(text="")

    def change_page(self, delta: int) -> None:
        self.page = max(0, self.page + delta)
        self.refresh()

    def import_txt(self) -> None:
        if self.import_running:
            return
        paths = filedialog.askopenfilenames(
            parent=self,
            title="Импорт очереди Bluesky",
            filetypes=[("Текстовые файлы", "*.txt"), ("Все файлы", "*.*")],
        )
        if not paths:
            return
        self.import_running = True
        self.import_button.configure(state="disabled")
        self.import_status.configure(text="Импорт...")

        def progress(current: int, total: int, message: str) -> None:
            ui_call(self, lambda: self.import_status.configure(text=f"{message} ({current}/{total})"))

        def work() -> None:
            result = import_files(paths, self.db, progress=progress)

            def done() -> None:
                self.import_running = False
                self.import_button.configure(state="normal")
                self.import_status.configure(
                    text=f"Добавлено {result.added}; дублей {result.duplicates}; ошибок {result.errors}"
                )
                self.scheduler.sync_accounts()
                self.scheduler.wake_all()
                self.on_accounts_changed()
                self.refresh()

            ui_call(self, done)

        threading.Thread(target=work, name="queue-import", daemon=True).start()

    def publish_now(self) -> None:
        if not self.account_id:
            return
        if not self.cached_next_id:
            messagebox.showinfo("Очередь", "В очереди нет постов для публикации.", parent=self)
            return
        if messagebox.askyesno("Публикация", "Опубликовать верхний пост сейчас?", parent=self):
            self.now_button.configure(state="disabled")
            self.scheduler.publish_now(self.account_id)
            self.worker_status.configure(text="Публикация...")

    def toggle_pause(self) -> None:
        if self.account_id:
            paused = self.scheduler.toggle_pause(self.account_id)
            self.pause_button.configure(text="Продолжить" if paused else "Пауза")

    def skip_next(self) -> None:
        if self.account_id and self.cached_next_id and messagebox.askyesno(
            "Пропустить", "Убрать верхний пост без публикации и записать как пропущенный?", parent=self
        ):
            self.db.complete_queue_item(self.cached_next_id, "", "", "skipped")
            self.db.update_runtime(self.account_id, next_scheduled_at=None, retry_count=0, last_error="")
            self.scheduler.wake(self.account_id)
            self.refresh()

    def delete_next(self) -> None:
        if self.cached_next_id and messagebox.askyesno("Удаление", "Удалить верхний пост из очереди?", parent=self):
            self.delete_item(self.cached_next_id)

    def delete_item(self, queue_id: int) -> None:
        if self.account_id:
            was_next = queue_id == self.cached_next_id
            self.db.delete_queue_item(queue_id)
            if was_next:
                self.db.update_runtime(self.account_id, next_scheduled_at=None, retry_count=0, last_error="")
            self.scheduler.wake(self.account_id)
            self.refresh()

    def move(self, queue_id: int, direction: str) -> None:
        if self.account_id:
            self.db.move_queue_item(self.account_id, queue_id, direction)
            self.scheduler.wake(self.account_id)
            self.refresh()

    def clear_queue(self) -> None:
        if self.account_id and messagebox.askyesno(
            "Очистка", "Удалить все ожидающие посты выбранного аккаунта?", parent=self
        ):
            self.db.clear_queue(self.account_id)
            self.scheduler.wake(self.account_id)
            self.page = 0
            self.refresh()

    def _show_rows(self, title: str, rows: list[dict], formatter: Callable[[dict], str]) -> None:
        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.geometry("780x520")
        dialog.transient(self.winfo_toplevel())
        box = ctk.CTkTextbox(dialog, wrap="word", font=ctk.CTkFont(family="Consolas", size=11))
        box.pack(fill="both", expand=True, padx=12, pady=12)
        box.insert("1.0", "\n\n".join(formatter(row) for row in rows) or "Записей нет.")
        box.configure(state="disabled")

    def show_history(self) -> None:
        self._show_rows(
            "История очереди",
            self.db.get_history(self.account_id, 300) if self.account_id else self.db.get_history(None, 300),
            lambda row: f"{row['completed_at']} · @{row['account_handle']} · {row['status']}\n{row['content']}",
        )

    def show_errors(self) -> None:
        self._show_rows(
            "Ошибки импорта",
            self.db.get_import_errors(),
            lambda row: f"{row['file_name']} · @{row['account_handle']} · {row['error_reason']}\n{row['raw_content']}",
        )
