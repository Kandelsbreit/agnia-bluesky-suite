from __future__ import annotations

import sys
from collections.abc import Callable
from tkinter import messagebox

import customtkinter as ctk

from app import autostart
from app.database import Database
from app.logging_setup import LOG_FILE, read_log_tail
from app.paths import data_dir, exports_dir
from app.ui.common import BLUE, GREEN, MUTED


class SettingsView(ctk.CTkFrame):
    def __init__(self, master, db: Database, on_changed: Callable[[], None], **kwargs):
        super().__init__(master, **kwargs)
        self.db = db
        self.on_changed = on_changed
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build()

    def _build(self) -> None:
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        scroll.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(scroll, text="Настройки", font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(2, 8)
        )

        startup = ctk.CTkFrame(scroll)
        startup.grid(row=1, column=0, sticky="ew", padx=8, pady=6)
        startup.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(startup, text="Windows и интерфейс", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(10, 4)
        )
        self.autostart_var = ctk.BooleanVar(value=self.db.get_bool("autostart"))
        self.start_minimized_var = ctk.BooleanVar(value=self.db.get_bool("start_minimized"))
        self.close_to_tray_var = ctk.BooleanVar(value=self.db.get_bool("close_to_tray", True))
        ctk.CTkCheckBox(startup, text="Автозапуск при входе в Windows", variable=self.autostart_var).grid(
            row=1, column=0, sticky="w", padx=12, pady=6
        )
        ctk.CTkCheckBox(startup, text="Запускать свёрнутой в трей", variable=self.start_minimized_var).grid(
            row=1, column=1, sticky="w", padx=12, pady=6
        )
        ctk.CTkCheckBox(startup, text="Закрывать окно в трей", variable=self.close_to_tray_var).grid(
            row=1, column=2, sticky="w", padx=12, pady=6
        )
        ctk.CTkLabel(startup, text="Тема:").grid(row=2, column=0, sticky="w", padx=12, pady=(8, 12))
        self.theme_var = ctk.StringVar(value=self.db.get_setting("theme", "dark"))
        theme = ctk.CTkOptionMenu(
            startup,
            values=["Тёмная", "Системная", "Светлая"],
            width=140,
            command=lambda value: self.theme_var.set(
                {"Тёмная": "dark", "Системная": "system", "Светлая": "light"}[value]
            ),
        )
        theme.grid(row=2, column=1, sticky="w", padx=12, pady=(8, 12))
        theme.set({"dark": "Тёмная", "system": "Системная", "light": "Светлая"}.get(self.theme_var.get(), "Тёмная"))

        automation = ctk.CTkFrame(scroll)
        automation.grid(row=2, column=0, sticky="ew", padx=8, pady=6)
        for column in range(6):
            automation.grid_columnconfigure(column, weight=1)
        ctk.CTkLabel(automation, text="Лайки и фолловинг", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=6, sticky="w", padx=12, pady=(10, 4)
        )
        self.entries: dict[str, ctk.CTkEntry] = {}
        fields = [
            ("like_min_delay", "Мин. пауза, сек.", "5"),
            ("like_max_delay", "Макс. пауза, сек.", "12"),
            ("like_limit", "Лайков за сессию", "100"),
            ("follow_limit", "Подписок за сессию", "30"),
            ("break_every_min", "Большая пауза после", "15"),
            ("break_every_max", "…до действий", "25"),
            ("break_duration_min", "Пауза от, сек.", "120"),
            ("break_duration_max", "Пауза до, сек.", "300"),
        ]
        for index, (key, label, default) in enumerate(fields):
            row = 1 + (index // 4) * 2
            column = (index % 4) * 2
            ctk.CTkLabel(automation, text=label).grid(row=row, column=column, sticky="w", padx=(12, 5), pady=(5, 2))
            entry = ctk.CTkEntry(automation, width=82)
            entry.insert(0, self.db.get_setting(key, default))
            entry.grid(row=row + 1, column=column, sticky="w", padx=(12, 10), pady=(2, 7))
            self.entries[key] = entry
        self.human_breaks_var = ctk.BooleanVar(value=self.db.get_bool("human_breaks", True))
        ctk.CTkCheckBox(
            automation,
            text="Включать длинные паузы между сериями действий",
            variable=self.human_breaks_var,
        ).grid(row=5, column=0, columnspan=6, sticky="w", padx=12, pady=(4, 12))

        save_row = ctk.CTkFrame(scroll, fg_color="transparent")
        save_row.grid(row=3, column=0, sticky="ew", padx=8, pady=6)
        ctk.CTkButton(save_row, text="Сохранить настройки", fg_color=GREEN, command=self.save).pack(side="left")
        self.save_status = ctk.CTkLabel(save_row, text="", text_color=GREEN)
        self.save_status.pack(side="left", padx=14)

        journal = ctk.CTkFrame(scroll)
        journal.grid(row=4, column=0, sticky="ew", padx=8, pady=6)
        journal.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(journal, text="Журнал", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 4)
        )
        buttons = ctk.CTkFrame(journal, fg_color="transparent")
        buttons.grid(row=0, column=1, sticky="e", padx=10, pady=(8, 2))
        ctk.CTkButton(buttons, text="Обновить", width=82, command=self.refresh_log).pack(side="left", padx=3)
        ctk.CTkButton(buttons, text="Файл журнала", width=105, fg_color=BLUE, command=self.open_log).pack(side="left", padx=3)
        ctk.CTkButton(buttons, text="Папка данных", width=105, command=lambda: autostart.open_folder(data_dir())).pack(
            side="left", padx=3
        )
        ctk.CTkButton(buttons, text="Экспорт", width=82, command=lambda: autostart.open_folder(exports_dir())).pack(
            side="left", padx=3
        )
        self.log_box = ctk.CTkTextbox(journal, height=230, wrap="none", font=ctk.CTkFont(family="Consolas", size=11))
        self.log_box.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(5, 12))
        self.refresh_log()

        ctk.CTkLabel(
            scroll,
            text=(
                "Очереди, история и расписание сохраняются в SQLite после каждого изменения. "
                "App Password в Windows защищён DPAPI текущего пользователя."
            ),
            text_color=MUTED,
            justify="left",
            wraplength=850,
        ).grid(row=5, column=0, sticky="w", padx=12, pady=(5, 12))

    def _number(self, key: str, *, integer: bool, minimum: float) -> str:
        raw = self.entries[key].get().strip().replace(",", ".")
        try:
            value = int(raw) if integer else float(raw)
        except ValueError as exc:
            raise ValueError(f"Проверьте числовое поле «{key}»") from exc
        if value < minimum:
            raise ValueError(f"Значение «{key}» должно быть не меньше {minimum:g}")
        return str(value)

    def save(self) -> None:
        try:
            values = {
                "like_min_delay": self._number("like_min_delay", integer=False, minimum=0.5),
                "like_max_delay": self._number("like_max_delay", integer=False, minimum=0.5),
                "like_limit": self._number("like_limit", integer=True, minimum=1),
                "follow_limit": self._number("follow_limit", integer=True, minimum=1),
                "break_every_min": self._number("break_every_min", integer=True, minimum=1),
                "break_every_max": self._number("break_every_max", integer=True, minimum=1),
                "break_duration_min": self._number("break_duration_min", integer=True, minimum=1),
                "break_duration_max": self._number("break_duration_max", integer=True, minimum=1),
            }
            if float(values["like_min_delay"]) > float(values["like_max_delay"]):
                raise ValueError("Минимальная пауза не может быть больше максимальной")
            if int(values["break_every_min"]) > int(values["break_every_max"]):
                raise ValueError("Минимальное число действий не может быть больше максимального")
            if int(values["break_duration_min"]) > int(values["break_duration_max"]):
                raise ValueError("Минимальная длинная пауза не может быть больше максимальной")
        except ValueError as exc:
            messagebox.showerror("Настройки", str(exc), parent=self)
            return

        old_autostart = self.db.get_bool("autostart")
        values.update(
            {
                "theme": self.theme_var.get(),
                "autostart": int(self.autostart_var.get()),
                "start_minimized": int(self.start_minimized_var.get()),
                "close_to_tray": int(self.close_to_tray_var.get()),
                "human_breaks": int(self.human_breaks_var.get()),
            }
        )
        if sys.platform == "win32" and (
            old_autostart != bool(self.autostart_var.get()) or self.autostart_var.get()
        ):
            ok, message = autostart.set_enabled(bool(self.autostart_var.get()), bool(self.start_minimized_var.get()))
            if not ok:
                messagebox.showerror("Автозапуск", message, parent=self)
                return
        self.db.set_settings(values)
        ctk.set_appearance_mode(self.theme_var.get())
        self.save_status.configure(text="Сохранено")
        self.after(2500, lambda: self.save_status.configure(text=""))
        self.on_changed()

    def append_log(self, line: str, _level: str = "info") -> None:
        try:
            self.log_box.insert("end", line + "\n")
            self.log_box.see("end")
            line_count = int(self.log_box.index("end-1c").split(".")[0])
            if line_count > 700:
                self.log_box.delete("1.0", "200.0")
        except Exception:
            pass

    def refresh_log(self) -> None:
        content = read_log_tail(500)
        self.log_box.delete("1.0", "end")
        self.log_box.insert("1.0", content or "Журнал пока пуст.")
        self.log_box.see("end")

    def open_log(self) -> None:
        try:
            if sys.platform == "win32":
                import os

                os.startfile(str(LOG_FILE))
            else:
                autostart.open_folder(LOG_FILE.parent)
        except OSError as exc:
            messagebox.showerror("Журнал", str(exc), parent=self)
