from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from app.bluesky import BlueskyError, BlueskyGateway
from app.database import Database
from app.exporter import ExportOptions, export_account, write_combined_queue
from app.paths import exports_dir
from app.ui.common import BLUE, GREEN, RED, clear_children, ui_call


class ExportView(ctk.CTkFrame):
    def __init__(self, master, db: Database, **kwargs):
        super().__init__(master, **kwargs)
        self.db = db
        self.running = False
        self.cancel_event = threading.Event()
        self.account_vars: dict[int, ctk.BooleanVar] = {}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build()

    def _build(self) -> None:
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        ctk.CTkLabel(scroll, text="Экспорт постов", font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w", padx=8)
        ctk.CTkLabel(
            scroll,
            text="Только собственные текстовые посты; репосты всегда исключаются. Результат — UTF-8 TXT.",
            text_color="#8B98A8",
        ).pack(anchor="w", padx=8, pady=(2, 8))

        accounts_card = ctk.CTkFrame(scroll)
        accounts_card.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(accounts_card, text="Аккаунты", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=12, pady=(10, 4))
        self.accounts_frame = ctk.CTkFrame(accounts_card, fg_color="transparent")
        self.accounts_frame.pack(fill="x", padx=12, pady=(2, 10))

        options = ctk.CTkFrame(scroll)
        options.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(options, text="Фильтры", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=5, sticky="w", padx=12, pady=(10, 5)
        )
        ctk.CTkLabel(options, text="Ответы:").grid(row=1, column=0, sticky="w", padx=12, pady=6)
        self.replies = ctk.StringVar(value="exclude")
        ctk.CTkOptionMenu(
            options,
            values=["Исключить", "Включить", "Отдельный файл"],
            command=lambda value: self.replies.set(
                {"Исключить": "exclude", "Включить": "include", "Отдельный файл": "separate"}[value]
            ),
            width=160,
        ).grid(row=1, column=1, sticky="w", pady=6)
        self.self_threads = ctk.CTkCheckBox(options, text="Ответы себе считать продолжением треда")
        self.self_threads.grid(row=1, column=2, columnspan=3, sticky="w", padx=16, pady=6)

        self.deduplicate = ctk.CTkCheckBox(options, text="Удалять дубли")
        self.deduplicate.select()
        self.deduplicate.grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=6)
        self.oldest_first = ctk.CTkCheckBox(options, text="Сначала старые")
        self.oldest_first.select()
        self.oldest_first.grid(row=2, column=2, sticky="w", padx=16, pady=6)
        self.queue_format = ctk.CTkCheckBox(options, text="Формат @account: для очереди")
        self.queue_format.select()
        self.queue_format.grid(row=2, column=3, columnspan=2, sticky="w", padx=16, pady=6)

        self.date_range = ctk.CTkCheckBox(options, text="Диапазон дат", command=self._date_state)
        self.date_range.grid(row=3, column=0, sticky="w", padx=12, pady=(6, 12))
        self.date_from = ctk.CTkEntry(options, width=120, placeholder_text="ГГГГ-ММ-ДД", state="disabled")
        self.date_from.grid(row=3, column=1, sticky="w", pady=(6, 12))
        ctk.CTkLabel(options, text="—").grid(row=3, column=2, pady=(6, 12))
        self.date_to = ctk.CTkEntry(options, width=120, placeholder_text="ГГГГ-ММ-ДД", state="disabled")
        self.date_to.grid(row=3, column=3, sticky="w", pady=(6, 12))

        folder_card = ctk.CTkFrame(scroll)
        folder_card.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(folder_card, text="Папка:").pack(side="left", padx=(12, 6), pady=10)
        self.folder = ctk.CTkEntry(folder_card)
        self.folder.pack(side="left", fill="x", expand=True, pady=10)
        self.folder.insert(0, str(exports_dir()))
        ctk.CTkButton(folder_card, text="Обзор", width=80, command=self.browse).pack(side="left", padx=10)

        actions = ctk.CTkFrame(scroll, fg_color="transparent")
        actions.pack(fill="x", padx=8, pady=6)
        self.export_button = ctk.CTkButton(actions, text="Экспорт", fg_color=BLUE, command=lambda: self.start(False))
        self.export_button.pack(side="left", padx=(0, 8))
        self.ai_button = ctk.CTkButton(actions, text="Экспорт для AI", fg_color=GREEN, command=lambda: self.start(True))
        self.ai_button.pack(side="left", padx=8)
        self.stop_button = ctk.CTkButton(actions, text="Остановить", fg_color=RED, state="disabled", command=self.stop)
        self.stop_button.pack(side="left", padx=8)
        self.progress = ctk.CTkProgressBar(actions)
        self.progress.pack(side="left", fill="x", expand=True, padx=(16, 8))
        self.progress.set(0)
        self.status = ctk.CTkLabel(actions, text="Готово", width=170, anchor="e")
        self.status.pack(side="right")

        self.log_box = ctk.CTkTextbox(scroll, height=190, font=ctk.CTkFont(family="Consolas", size=11))
        self.log_box.pack(fill="both", expand=True, padx=8, pady=(4, 10))
        self.refresh_accounts()

    def refresh_accounts(self) -> None:
        previous = {account_id: var.get() for account_id, var in self.account_vars.items()}
        clear_children(self.accounts_frame)
        self.account_vars = {}
        accounts = self.db.get_accounts()
        if not accounts:
            ctk.CTkLabel(self.accounts_frame, text="Сначала добавьте аккаунты.", text_color="#8B98A8").pack(anchor="w")
            return
        for account in accounts:
            account_id = int(account["id"])
            variable = ctk.BooleanVar(value=previous.get(account_id, True))
            self.account_vars[account_id] = variable
            ctk.CTkCheckBox(self.accounts_frame, text=f"@{account['handle']}", variable=variable).pack(
                side="left", padx=(0, 18), pady=3
            )

    def _date_state(self) -> None:
        state = "normal" if self.date_range.get() else "disabled"
        self.date_from.configure(state=state)
        self.date_to.configure(state=state)

    def browse(self) -> None:
        folder = filedialog.askdirectory(parent=self, initialdir=self.folder.get() or str(exports_dir()))
        if folder:
            self.folder.delete(0, "end")
            self.folder.insert(0, folder)

    def _parse_date(self, value: str, label: str):
        if not value.strip():
            return None
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Дата «{label}» должна иметь формат ГГГГ-ММ-ДД") from exc

    def start(self, ai_export: bool) -> None:
        if self.running:
            return
        selected = [account_id for account_id, variable in self.account_vars.items() if variable.get()]
        if not selected:
            messagebox.showwarning("Экспорт", "Выберите хотя бы один аккаунт.", parent=self)
            return
        try:
            date_from = self._parse_date(self.date_from.get(), "От") if self.date_range.get() else None
            date_to = self._parse_date(self.date_to.get(), "До") if self.date_range.get() else None
            if date_from and date_to and date_from > date_to:
                raise ValueError("Дата «От» позже даты «До»")
        except ValueError as exc:
            messagebox.showerror("Диапазон дат", str(exc), parent=self)
            return
        output = Path(self.folder.get().strip() or str(exports_dir())).expanduser()
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Папка экспорта", str(exc), parent=self)
            return

        options = ExportOptions(
            replies="exclude" if ai_export else self.replies.get(),
            self_threads_as_posts=bool(self.self_threads.get()),
            deduplicate=bool(self.deduplicate.get()),
            oldest_first=bool(self.oldest_first.get()),
            date_from=date_from,
            date_to=date_to,
            queue_format=False if ai_export else bool(self.queue_format.get()),
            ai_export=ai_export,
        )
        self.running = True
        self.cancel_event.clear()
        self.export_button.configure(state="disabled")
        self.ai_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.set(0)
        self.log_box.delete("1.0", "end")
        self._append("Экспорт для AI" if ai_export else "Стандартный экспорт")

        def work() -> None:
            results = []
            failures = []
            for index, account_id in enumerate(selected):
                if self.cancel_event.is_set():
                    break
                account = self.db.get_account(account_id)
                if not account:
                    continue
                try:
                    result = export_account(
                        BlueskyGateway(account["handle"]),
                        account["handle"],
                        output,
                        options,
                        cancel_event=self.cancel_event,
                        log=self._append_threadsafe,
                        progress=lambda stats, page, total, i=index: self._progress(i, len(selected), stats.fetched, total),
                    )
                    results.append(result)
                except BlueskyError as exc:
                    failures.append(f"@{account['handle']}: {exc}")
                    self._append_threadsafe(f"Ошибка @{account['handle']}: {exc}")
            if not ai_export and len(results) > 1 and not self.cancel_event.is_set():
                write_combined_queue(output / "combined_queue_posts.txt", results, options.queue_format)
                self._append_threadsafe("Создан combined_queue_posts.txt")
            ui_call(self, lambda: self._finish(results, failures, output))

        threading.Thread(target=work, name="post-export", daemon=True).start()

    def _progress(self, account_index: int, account_total: int, fetched: int, estimated: int) -> None:
        within = min(1.0, fetched / estimated) if estimated else 0.2
        value = min(0.99, (account_index + within) / max(1, account_total))
        ui_call(self, lambda: self.progress.set(value))

    def _append(self, message: str) -> None:
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")

    def _append_threadsafe(self, message: str) -> None:
        ui_call(self, lambda: self._append(message))

    def _finish(self, results, failures, output: Path) -> None:
        self.running = False
        self.export_button.configure(state="normal")
        self.ai_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.progress.set(1 if results and not self.cancel_event.is_set() else 0)
        file_count = sum(len(result.files) for result in results)
        if len(results) > 1 and not self.cancel_event.is_set():
            file_count += 1
        if self.cancel_event.is_set():
            text = f"Остановлено; создано файлов: {file_count}"
        else:
            text = f"Готово; файлов: {file_count}, ошибок: {len(failures)}"
        self.status.configure(text=text, text_color=GREEN if not failures else RED)
        if results and not self.cancel_event.is_set():
            messagebox.showinfo("Экспорт завершён", f"Файлов: {file_count}\nПапка: {output}", parent=self)

    def stop(self) -> None:
        if self.running:
            self.cancel_event.set()
            self.status.configure(text="Остановка...")
