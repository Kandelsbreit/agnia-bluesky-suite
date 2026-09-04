from __future__ import annotations

from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from app.automation import AutomationOptions, AutomationWorker
from app.database import Database
from app.importer import read_text_file
from app.ui.common import AMBER, GREEN, RED, AccountSelector, ui_call
from app.utils import normalize_handle


class FollowingView(ctk.CTkFrame):
    def __init__(self, master, db: Database, **kwargs):
        super().__init__(master, **kwargs)
        self.db = db
        self.worker: AutomationWorker | None = None
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build()

    def _build(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 6))
        ctk.CTkLabel(header, text="Массовый фолловинг", font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text="Для редкого ручного запуска. Уже подписанные и собственный профиль пропускаются.",
            text_color="#8B98A8",
        ).pack(anchor="w", pady=(2, 0))

        tools = ctk.CTkFrame(self)
        tools.grid(row=1, column=0, sticky="ew", padx=18, pady=8)
        self.selector = AccountSelector(tools, self.db)
        self.selector.pack(side="left", padx=12, pady=10)
        self.count_label = ctk.CTkLabel(tools, text="Целей: 0")
        self.count_label.pack(side="left", padx=12)
        ctk.CTkButton(tools, text="Импорт TXT", width=110, command=self.import_txt).pack(side="right", padx=12)

        body = ctk.CTkFrame(self)
        body.grid(row=2, column=0, sticky="nsew", padx=18, pady=6)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            body,
            text="По одному handle или ссылке bsky.app/profile/... на строку:",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))
        self.targets_box = ctk.CTkTextbox(body, wrap="none", font=ctk.CTkFont(family="Consolas", size=12))
        self.targets_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        self.targets_box.bind("<KeyRelease>", lambda _event: self._update_count())

        bottom = ctk.CTkFrame(body, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 10))
        ctk.CTkLabel(bottom, text="Лимит новых подписок:").pack(side="left")
        self.limit_entry = ctk.CTkEntry(bottom, width=70)
        self.limit_entry.insert(0, self.db.get_setting("follow_limit", "30"))
        self.limit_entry.pack(side="left", padx=(6, 14))
        self.start_button = ctk.CTkButton(bottom, text="Начать", fg_color=GREEN, command=self.start)
        self.start_button.pack(side="left", padx=5)
        self.pause_button = ctk.CTkButton(bottom, text="Пауза", fg_color=AMBER, state="disabled", command=self.toggle_pause)
        self.pause_button.pack(side="left", padx=5)
        self.stop_button = ctk.CTkButton(bottom, text="Стоп", fg_color=RED, state="disabled", command=self.stop)
        self.stop_button.pack(side="left", padx=5)
        self.progress = ctk.CTkProgressBar(bottom)
        self.progress.pack(side="left", fill="x", expand=True, padx=(14, 8))
        self.progress.set(0)
        self.status = ctk.CTkLabel(bottom, text="Готово", width=170, anchor="e")
        self.status.pack(side="right")

        self.log_box = ctk.CTkTextbox(self, height=120, font=ctk.CTkFont(family="Consolas", size=11))
        self.log_box.grid(row=3, column=0, sticky="ew", padx=18, pady=(6, 16))

    def refresh_accounts(self) -> None:
        self.selector.refresh()

    def targets(self) -> list[str]:
        result = []
        for line in self.targets_box.get("1.0", "end").splitlines():
            clean = line.strip()
            if clean and not clean.startswith("#"):
                result.append(clean)
        return result

    def _update_count(self) -> None:
        unique = {normalize_handle(value) for value in self.targets() if normalize_handle(value)}
        self.count_label.configure(text=f"Целей: {len(unique)}")

    def import_txt(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Список аккаунтов",
            filetypes=[("Текстовые файлы", "*.txt"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            content = read_text_file(Path(path))
            self.targets_box.delete("1.0", "end")
            self.targets_box.insert("1.0", content)
            self._update_count()
            self._append(f"Загружен файл {Path(path).name}.")
        except OSError as exc:
            self._append(f"Ошибка чтения: {exc}")

    def _options(self) -> AutomationOptions:
        try:
            limit = max(1, int(self.limit_entry.get()))
        except ValueError:
            limit = self.db.get_int("follow_limit", 30)
        return AutomationOptions(
            limit=limit,
            min_delay=max(0.5, self.db.get_float("like_min_delay", 5.0)),
            max_delay=max(0.5, self.db.get_float("like_max_delay", 12.0)),
            human_breaks=self.db.get_bool("human_breaks", True),
            break_every_min=self.db.get_int("break_every_min", 15),
            break_every_max=self.db.get_int("break_every_max", 25),
            break_duration_min=self.db.get_int("break_duration_min", 120),
            break_duration_max=self.db.get_int("break_duration_max", 300),
        )

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        account_id = self.selector.account_id
        targets = self.targets()
        if not account_id:
            self._append("Сначала добавьте аккаунт.")
            return
        if not targets:
            self._append("Список целей пуст.")
            return
        options = self._options()
        self.db.set_setting("follow_limit", options.limit)
        self.progress.set(0)
        self.worker = AutomationWorker(
            self.db,
            account_id,
            "following",
            options,
            targets=targets,
            callback=self._worker_event,
        )
        self.worker.start()

    def _append(self, message: str) -> None:
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")

    def _worker_event(self, event: dict) -> None:
        def update() -> None:
            kind = event.get("kind")
            if kind == "status":
                self.start_button.configure(state="disabled")
                self.pause_button.configure(state="normal")
                self.stop_button.configure(state="normal")
                self.status.configure(text=event.get("message", "Работа"))
            elif kind == "progress":
                current, total = event.get("current", 0), max(1, event.get("total", 1))
                self.progress.set(current / total)
                self.status.configure(text=f"{current}/{total}")
            elif kind == "log":
                self._append(event.get("message", ""))
            elif kind == "finished":
                stats = event.get("stats", {})
                self.start_button.configure(state="normal")
                self.pause_button.configure(state="disabled", text="Пауза")
                self.stop_button.configure(state="disabled")
                self.status.configure(text=f"Подписок {stats.get('completed', 0)}, ошибок {stats.get('errors', 0)}")
                self._append(
                    f"Готово: новых подписок {stats.get('completed', 0)}, "
                    f"пропущено {stats.get('skipped', 0)}, ошибок {stats.get('errors', 0)}."
                )
        ui_call(self, update)

    def toggle_pause(self) -> None:
        if not self.worker:
            return
        if self.worker.paused:
            self.worker.resume()
            self.pause_button.configure(text="Пауза")
        else:
            self.worker.pause()
            self.pause_button.configure(text="Продолжить")

    def stop(self) -> None:
        if self.worker:
            self.status.configure(text="Остановка...")
            self.worker.stop()
