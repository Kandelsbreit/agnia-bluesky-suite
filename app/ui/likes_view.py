from __future__ import annotations

import customtkinter as ctk

from app.automation import AutomationOptions, AutomationWorker, LikeAutomationManager
from app.database import Database
from app.ui.common import AMBER, GREEN, RED, AccountSelector, ui_call


class LikesView(ctk.CTkFrame):
    def __init__(self, master, db: Database, **kwargs):
        super().__init__(master, **kwargs)
        self.db = db
        self.manager = LikeAutomationManager(self.db, callback=self._manager_event)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._build()

    @property
    def worker(self) -> AutomationWorker | None:
        active = self.manager.active_account_ids()
        return self.manager.get_worker(active[0]) if active else None

    def is_busy(self, account_id: int) -> bool:
        return self.manager.is_running(account_id)

    def _build(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 6))
        ctk.CTkLabel(header, text="Автоматический лайкинг", font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text="Работает напрямую через Bluesky API параллельно для нескольких аккаунтов. Свои и повторные лайки пропускаются.",
            text_color="#8B98A8",
        ).pack(anchor="w", pady=(2, 0))

        form = ctk.CTkFrame(self)
        form.grid(row=1, column=0, sticky="ew", padx=18, pady=8)

        self.all_accounts_cb = ctk.CTkCheckBox(
            form,
            text="Все аккаунты одновременно (параллельно)",
            command=self._account_mode_toggled,
        )
        self.all_accounts_cb.grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 6))
        if self.db.get_bool("auto_like_all_accounts", True):
            self.all_accounts_cb.select()

        self.selector = AccountSelector(form, self.db)
        self.selector.grid(row=0, column=2, columnspan=2, sticky="w", padx=(14, 8), pady=(12, 6))

        ctk.CTkLabel(form, text="Источник:").grid(row=1, column=0, sticky="w", padx=(14, 6), pady=6)
        self.source = ctk.StringVar(value=self.db.get_setting("auto_like_source", "timeline"))
        source_menu = ctk.CTkOptionMenu(
            form,
            values=["Домашняя лента", "Рекомендации", "Поиск"],
            command=self._source_changed,
            width=170,
        )
        source_menu.grid(row=1, column=1, sticky="w", pady=6)
        mapping = {"timeline": "Домашняя лента", "discover": "Рекомендации", "search": "Поиск"}
        source_menu.set(mapping.get(self.source.get(), "Домашняя лента"))

        ctk.CTkLabel(form, text="Запрос:").grid(row=1, column=2, sticky="e", padx=(18, 6), pady=6)
        self.query_entry = ctk.CTkEntry(form, width=300, placeholder_text="#art или слова для поиска")
        self.query_entry.grid(row=1, column=3, sticky="ew", padx=(0, 14), pady=6)
        self.query_entry.insert(0, self.db.get_setting("auto_like_query", ""))
        form.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(form, text="Лайков за цикл:").grid(row=2, column=0, sticky="w", padx=(14, 6), pady=6)
        self.limit_entry = ctk.CTkEntry(form, width=90)
        self.limit_entry.grid(row=2, column=1, sticky="w", pady=6)
        self.limit_entry.insert(0, self.db.get_setting("like_limit", "100"))

        self.skip_replies = ctk.CTkCheckBox(form, text="Пропускать ответы")
        self.skip_replies.grid(row=2, column=2, sticky="w", padx=(18, 8), pady=6)
        if self.db.get_bool("auto_like_skip_replies", True):
            self.skip_replies.select()

        self.skip_reposts = ctk.CTkCheckBox(form, text="Пропускать репосты")
        self.skip_reposts.grid(row=2, column=3, sticky="w", pady=6)
        if self.db.get_bool("auto_like_skip_reposts", True):
            self.skip_reposts.select()

        self.continuous_cb = ctk.CTkCheckBox(
            form,
            text="Круглосуточный режим 24/7 (циклы с перерывом)",
        )
        self.continuous_cb.grid(row=3, column=0, columnspan=2, sticky="w", padx=14, pady=6)
        if self.db.get_bool("auto_like_247_mode", True):
            self.continuous_cb.select()

        interval_box = ctk.CTkFrame(form, fg_color="transparent")
        interval_box.grid(row=3, column=2, columnspan=2, sticky="w", padx=(18, 8), pady=6)
        ctk.CTkLabel(interval_box, text="Перерыв между циклами (мин):").pack(side="left", padx=(0, 6))
        self.cycle_interval_entry = ctk.CTkEntry(interval_box, width=60)
        self.cycle_interval_entry.pack(side="left")
        self.cycle_interval_entry.insert(0, self.db.get_setting("auto_like_cycle_interval", "60"))

        self.auto_start_cb = ctk.CTkCheckBox(
            form,
            text="Запускать автоматически при старте программы (для работы 24/7 без участия)",
        )
        self.auto_start_cb.grid(row=4, column=0, columnspan=4, sticky="w", padx=14, pady=(6, 12))
        if self.db.get_bool("auto_like_enabled", True):
            self.auto_start_cb.select()

        self._account_mode_toggled()

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=2, column=0, sticky="ew", padx=18, pady=6)
        self.start_button = ctk.CTkButton(controls, text="Начать", fg_color=GREEN, command=self.start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.pause_button = ctk.CTkButton(controls, text="Пауза", fg_color=AMBER, state="disabled", command=self.toggle_pause)
        self.pause_button.pack(side="left", padx=8)
        self.stop_button = ctk.CTkButton(controls, text="Стоп", fg_color=RED, state="disabled", command=self.stop)
        self.stop_button.pack(side="left", padx=8)
        self.progress = ctk.CTkProgressBar(controls)
        self.progress.pack(side="left", fill="x", expand=True, padx=(16, 8))
        self.progress.set(0)
        self.status = ctk.CTkLabel(controls, text="Готово", width=220, anchor="e")
        self.status.pack(side="right")

        self.log_box = ctk.CTkTextbox(self, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.grid(row=3, column=0, sticky="nsew", padx=18, pady=(6, 16))

    def _account_mode_toggled(self) -> None:
        if bool(self.all_accounts_cb.get()):
            self.selector.menu.configure(state="disabled")
        else:
            self.selector.menu.configure(state="normal")

    def _source_changed(self, label: str) -> None:
        self.source.set({"Домашняя лента": "timeline", "Рекомендации": "discover", "Поиск": "search"}[label])

    def refresh_accounts(self) -> None:
        self.selector.refresh()

    def _append(self, text: str) -> None:
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        try:
            line_count = int(self.log_box.index("end-1c").split(".")[0])
            if line_count > 400:
                self.log_box.delete("1.0", "100.0")
        except Exception:
            pass

    def _automation_options(self) -> AutomationOptions:
        try:
            limit = max(1, int(self.limit_entry.get()))
        except ValueError:
            limit = self.db.get_int("like_limit", 100)
        try:
            interval = max(1, int(self.cycle_interval_entry.get()))
        except ValueError:
            interval = self.db.get_int("auto_like_cycle_interval", 60)

        continuous = bool(self.continuous_cb.get())
        return AutomationOptions(
            limit=limit,
            min_delay=max(0.5, self.db.get_float("like_min_delay", 5.0)),
            max_delay=max(0.5, self.db.get_float("like_max_delay", 12.0)),
            human_breaks=self.db.get_bool("human_breaks", True),
            break_every_min=self.db.get_int("break_every_min", 15),
            break_every_max=self.db.get_int("break_every_max", 25),
            break_duration_min=self.db.get_int("break_duration_min", 120),
            break_duration_max=self.db.get_int("break_duration_max", 300),
            continuous=continuous,
            cycle_interval_minutes=interval,
            cycle_jitter_minutes=max(1, interval // 4),
        )

    def start(self, automatic: bool = False) -> None:
        if self.manager.is_running():
            return

        all_accounts = bool(self.all_accounts_cb.get())
        if all_accounts:
            accounts = self.db.get_accounts()
            target_ids = [int(a["id"]) for a in accounts]
            if not target_ids:
                self._append("Сначала добавьте аккаунт во вкладке «Аккаунты».")
                return
        else:
            account_id = self.selector.account_id
            if not account_id:
                self._append("Сначала добавьте аккаунт во вкладке «Аккаунты».")
                return
            target_ids = [account_id]

        if self.source.get() == "search" and not self.query_entry.get().strip():
            self._append("Для режима «Поиск» нужен запрос.")
            return

        options = self._automation_options()
        auto_enabled = bool(self.auto_start_cb.get())
        self.db.set_settings(
            {
                "auto_like_enabled": int(auto_enabled),
                "auto_like_all_accounts": int(all_accounts),
                "auto_like_account_id": target_ids[0] if (not all_accounts and target_ids) else "",
                "auto_like_source": self.source.get(),
                "auto_like_query": self.query_entry.get().strip(),
                "auto_like_skip_replies": int(bool(self.skip_replies.get())),
                "auto_like_skip_reposts": int(bool(self.skip_reposts.get())),
                "auto_like_247_mode": int(options.continuous),
                "auto_like_cycle_interval": options.cycle_interval_minutes,
                "like_limit": options.limit,
            }
        )
        self.progress.set(0)
        self.status.configure(text="Запуск...")
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="normal", text="Пауза")
        self.stop_button.configure(state="normal")

        started_count = self.manager.start_accounts(
            target_ids,
            options,
            source=self.source.get(),
            query=self.query_entry.get().strip(),
            skip_replies=bool(self.skip_replies.get()),
            skip_reposts=bool(self.skip_reposts.get()),
        )
        if automatic:
            self._append(f"Автозапуск: запущено {started_count} акк. в режиме {'24/7' if options.continuous else 'одна сессия'}.")
        else:
            self._append(f"Запущено {started_count} акк. в режиме {'24/7' if options.continuous else 'одна сессия'}.")

    def maybe_autostart(self) -> None:
        if not self.db.get_bool("auto_like_enabled", True):
            return
        if self.manager.is_running():
            return

        all_accounts = self.db.get_bool("auto_like_all_accounts", True)
        accounts = self.db.get_accounts()
        if not accounts:
            return

        if not all_accounts:
            try:
                account_id = int(self.db.get_setting("auto_like_account_id", "0"))
            except ValueError:
                return
            if not self.db.get_account(account_id):
                return
            self.selector.refresh(account_id)

        self.start(automatic=True)

    def _manager_event(self, event: dict) -> None:
        def update() -> None:
            kind = event.get("kind")
            if kind == "status":
                running = self.manager.is_running()
                if running:
                    self.start_button.configure(state="disabled")
                    self.pause_button.configure(state="normal")
                    self.stop_button.configure(state="normal")
                msg = event.get("message", "Работа")
                handle = event.get("handle")
                if handle:
                    self.status.configure(text=f"@{handle}: {msg}")
                else:
                    self.status.configure(text=msg)
            elif kind == "progress":
                current, total = event.get("current", 0), max(1, event.get("total", 1))
                self.progress.set(current / total)
                active = len(self.manager.active_account_ids())
                handle = event.get("handle", "")
                if active > 1:
                    self.status.configure(text=f"Активно {active} | @{handle} {current}/{total}")
                else:
                    self.status.configure(text=f"{current}/{total}")
            elif kind == "log":
                self._append(event.get("message", ""))
            elif kind == "finished":
                if not self.manager.is_running():
                    self.start_button.configure(state="normal")
                    self.pause_button.configure(state="disabled", text="Пауза")
                    self.stop_button.configure(state="disabled")
                    self.status.configure(text="Завершено")
                    stats = event.get("stats", {})
                    self._append(
                        f"Сессия завершена: лайков {stats.get('completed', 0)}, пропущено {stats.get('skipped', 0)}, "
                        f"ошибок {stats.get('errors', 0)}."
                    )
        ui_call(self, update)

    def toggle_pause(self) -> None:
        resumed = self.manager.toggle_pause_all()
        self.pause_button.configure(text="Пауза" if resumed else "Продолжить")

    def stop(self) -> None:
        self.status.configure(text="Остановка...")
        self.manager.stop_all()
