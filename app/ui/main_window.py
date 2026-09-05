from __future__ import annotations

import sys

import customtkinter as ctk

from app import __version__
from app.database import Database
from app.logging_setup import get_logger, set_ui_callback
from app.paths import resource_path
from app.scheduler import QueueScheduler
from app.tray import TrayManager
from app.ui.accounts_view import AccountsView
from app.ui.export_view import ExportView
from app.ui.following_view import FollowingView
from app.ui.likes_view import LikesView
from app.ui.posting_view import PostingView
from app.ui.queue_view import QueueView
from app.ui.settings_view import SettingsView


class MainWindow(ctk.CTk):
    NAVIGATION = (
        ("likes", "Лайки"),
        ("following", "Фолловинг"),
        ("posting", "Постинг"),
        ("queue", "Очередь"),
        ("export", "Экспорт"),
        ("accounts", "Аккаунты"),
        ("settings", "Настройки"),
    )

    def __init__(self, db: Database, *, start_hidden: bool = False, enable_background: bool = True):
        ctk.set_appearance_mode(db.get_setting("theme", "dark"))
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.db = db
        self.start_hidden = start_hidden
        self.enable_background = enable_background
        self.scheduler = QueueScheduler(db)
        self.views: dict[str, ctk.CTkFrame] = {}
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        self.current_view = "queue"
        self._quitting = False
        self._tick_job: str | None = None

        self.title("Agnia Bluesky Suite")
        self.geometry("1180x780")
        self.minsize(980, 680)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            self.iconbitmap(str(resource_path("assets/icon.ico")))
        except Exception:
            pass

        self._build_sidebar()
        self._build_views()

        icon = resource_path("assets/icon.png")
        self.tray = TrayManager(icon, self.show_window, self.hide_window, self.quit_app)
        self.tray_started = self.tray.start() if self.enable_background else False
        set_ui_callback(lambda line, level: self.after(0, self.views["settings"].append_log, line, level))

        if self.enable_background and self.db.get_bool("auto_unpause_queues_on_start", True):
            self.db.unpause_all_queues()

        self.refresh_accounts()
        self.show_view("queue")
        if self.enable_background:
            self._schedule_tick()
            self.after(900, self.views["likes"].maybe_autostart)
        if self.start_hidden and sys.platform == "win32" and self.tray_started:
            self.after(0, self.hide_window)
        get_logger().info("Agnia Bluesky Suite %s запущена", __version__)

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(self, width=188, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_rowconfigure(9, weight=1)
        sidebar.grid_propagate(False)
        ctk.CTkLabel(
            sidebar,
            text="AGNIA\nBLUESKY SUITE",
            justify="left",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#68A9FF",
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(24, 22))
        for row, (key, label) in enumerate(self.NAVIGATION, start=1):
            button = ctk.CTkButton(
                sidebar,
                text=label,
                height=39,
                anchor="w",
                corner_radius=7,
                fg_color="transparent",
                hover_color=("#D9E8FA", "#26364A"),
                command=lambda target=key: self.show_view(target),
            )
            button.grid(row=row, column=0, sticky="ew", padx=12, pady=3)
            self.nav_buttons[key] = button

        self.sidebar_status = ctk.CTkLabel(
            sidebar,
            text="",
            justify="left",
            anchor="sw",
            wraplength=150,
            text_color="#8B98A8",
            font=ctk.CTkFont(size=11),
        )
        self.sidebar_status.grid(row=9, column=0, sticky="sew", padx=20, pady=(10, 4))
        ctk.CTkLabel(sidebar, text=f"v{__version__}", text_color="#667788", font=ctk.CTkFont(size=10)).grid(
            row=10, column=0, sticky="w", padx=20, pady=(2, 14)
        )

    def _build_views(self) -> None:
        container = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        container.grid(row=0, column=1, sticky="nsew")
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)

        self.views = {
            "likes": LikesView(container, self.db),
            "following": FollowingView(container, self.db),
            "posting": PostingView(container, self.db, self._queue_changed),
            "queue": QueueView(container, self.db, self.scheduler, self.refresh_accounts),
            "export": ExportView(container, self.db),
            "accounts": AccountsView(container, self.db, self.refresh_accounts, self._prepare_account_delete),
            "settings": SettingsView(container, self.db, self._settings_changed),
        }
        for view in self.views.values():
            view.grid(row=0, column=0, sticky="nsew")

    def show_view(self, key: str) -> None:
        if key not in self.views:
            return
        self.current_view = key
        self.views[key].tkraise()
        for name, button in self.nav_buttons.items():
            button.configure(fg_color=("#CFE4FF", "#1E4774") if name == key else "transparent")
        if key == "queue":
            self.views["queue"].refresh()
        elif key in {"likes", "following", "posting"}:
            self.views[key].refresh_accounts()
        elif key == "accounts":
            self.views["accounts"].refresh()
        elif key == "export":
            self.views["export"].refresh_accounts()
        elif key == "settings":
            self.views["settings"].refresh_log()

    def refresh_accounts(self) -> None:
        if self.enable_background:
            self.scheduler.sync_accounts()
            self.scheduler.wake_all()
        for key in ("likes", "following", "posting"):
            self.views[key].refresh_accounts()
        self.views["queue"].refresh_accounts()
        self.views["export"].refresh_accounts()
        self.views["accounts"].refresh()
        self._update_sidebar_status()

    def _queue_changed(self, account_id: int) -> None:
        self.scheduler.sync_accounts()
        self.scheduler.wake(account_id)
        self.views["queue"].refresh()
        self.views["queue"].refresh_accounts()
        self._update_sidebar_status()

    def _settings_changed(self) -> None:
        self.scheduler.wake_all()

    def _prepare_account_delete(self, account_id: int) -> tuple[bool, str]:
        if self.views["likes"].is_busy(account_id):
            return False, "Сначала остановите лайкинг для этого аккаунта."
        worker = self.views["following"].worker
        if worker and worker.account_id == account_id and worker.is_alive():
            return False, "Сначала остановите фолловинг для этого аккаунта."
        posting = self.views["posting"]
        if posting.busy and posting.busy_account_id == account_id:
            return False, "Дождитесь завершения ручной публикации."
        queue_worker = self.scheduler.worker(account_id)
        if queue_worker and queue_worker.status == "Публикация":
            return False, "Дождитесь завершения текущей публикации из очереди."
        self.scheduler.stop_account(account_id)
        return True, ""

    def _update_sidebar_status(self) -> None:
        stats = self.db.stats()
        accounts = self.db.get_accounts()
        connected = sum(1 for account in accounts if account.get("connection_status") == "Подключён")
        text = (
            f"Аккаунтов: {stats['accounts']}\n"
            f"Подключено: {connected}\n"
            f"В очередях: {stats['queued']}\n"
            f"Опубликовано: {stats['published']}"
        )
        self.sidebar_status.configure(text=text)
        self.tray.update_tooltip(f"в очередях {stats['queued']}") if hasattr(self, "tray") else None

    def _schedule_tick(self) -> None:
        if self._quitting:
            return
        if self.current_view == "queue":
            self.views["queue"].update_tick()
        self._update_sidebar_status()
        self._tick_job = self.after(1000, self._schedule_tick)

    def show_window(self) -> None:
        try:
            self.after(0, self._show_window_ui)
        except Exception:
            pass

    def _show_window_ui(self) -> None:
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def hide_window(self) -> None:
        try:
            self.after(0, self.withdraw)
        except Exception:
            pass

    def on_close(self) -> None:
        if sys.platform == "win32" and self.tray_started and self.db.get_bool("close_to_tray", True):
            self.hide_window()
        else:
            self.quit_app()

    def quit_app(self) -> None:
        if self._quitting:
            return
        self._quitting = True

        def finish() -> None:
            get_logger().info("Agnia Bluesky Suite завершает работу")
            set_ui_callback(None)
            self.views["likes"].stop()
            following_worker = self.views["following"].worker
            if following_worker and following_worker.is_alive():
                following_worker.stop()
            if self.views["export"].running:
                self.views["export"].stop()
            self.scheduler.stop_all()
            self.tray.stop()
            if self._tick_job:
                try:
                    self.after_cancel(self._tick_job)
                except Exception:
                    pass
            self.destroy()

        try:
            self.after(0, finish)
        except Exception:
            finish()
