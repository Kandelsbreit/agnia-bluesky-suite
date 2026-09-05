from __future__ import annotations

import calendar
import json
import threading
import webbrowser
from datetime import UTC, datetime, timedelta
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from app.importer import import_files, parse_content, read_text_file
from app.ui.common import AccountSelector, ui_call
from app.ui.composer import Composer
from app.utils import parse_iso

PAGE_SIZE = 100
STATES = {"pending": "Ожидает", "sending": "Отправляется", "uncertain": "Проверка результата", "failed": "Ошибка"}


class QueueView(ctk.CTkFrame):
    def __init__(self, master, db, scheduler, on_accounts_changed, **kwargs):
        super().__init__(master, **kwargs)
        self.db = db
        self.scheduler = scheduler
        self.on_accounts_changed = on_accounts_changed
        self.page = 0
        self.cached_next_id = None
        self.import_running = False
        self._signature = None
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=8)
        self.selector = AccountSelector(top, db, on_change=self._account_changed)
        self.selector.pack(side="left")
        for label, command in [
            ("Календарь", self.show_calendar),
            ("История", self.show_history),
            ("Импорт TXT", self.import_txt),
        ]:
            ctk.CTkButton(top, text=label, width=110, command=command).pack(side="right", padx=3)
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        self.search = ctk.CTkEntry(toolbar, placeholder_text="Поиск по тексту", width=210)
        self.search.pack(side="left", padx=3)
        self.search.bind("<Return>", lambda e: self._search())
        ctk.CTkButton(toolbar, text="Найти", width=65, command=self._search).pack(side="left", padx=3)
        self.pause_button = ctk.CTkButton(toolbar, text="Пауза", width=95, command=self.toggle_pause)
        self.pause_button.pack(side="left", padx=3)
        self.sync_button = ctk.CTkButton(toolbar, text="Сверить с Bluesky", width=140, command=self.sync_with_bluesky)
        self.sync_button.pack(side="left", padx=3)
        ctk.CTkButton(toolbar, text="Ошибки импорта", width=140, command=self.show_errors).pack(side="right", padx=3)
        self.worker_status = ctk.CTkLabel(self, text="Готово", anchor="w")
        self.worker_status.grid(row=2, column=0, sticky="ew", padx=16, pady=4)
        self.tree = ttk.Treeview(
            self, columns=("state", "date", "media", "text"), show="headings", selectmode="extended"
        )
        for name, label, width in [
            ("state", "Состояние", 145),
            ("date", "Время", 140),
            ("media", "Медиа", 65),
            ("text", "Текст", 450),
        ]:
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=50, stretch=name == "text")
        self.tree.grid(row=3, column=0, sticky="nsew", padx=(12, 28), pady=6)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=3, column=0, sticky="nse", padx=12, pady=6)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Double-1>", lambda e: self.edit_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_selected_error())
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=4, column=0, sticky="ew", padx=12, pady=4)
        for label, command, width in [
            ("Изменить", self.edit_selected, 95),
            ("Повторить / сейчас", self.publish_now, 155),
            ("Пропустить", self.skip_next, 100),
            ("Удалить", self.delete_selected, 80),
            ("▲", lambda: self.move_selected("up"), 35),
            ("▼", lambda: self.move_selected("down"), 35),
        ]:
            ctk.CTkButton(controls, text=label, width=width, command=command).pack(side="left", padx=3)
        self.error_label = ctk.CTkLabel(self, text="", wraplength=880, anchor="w", justify="left", text_color="#E29A2D")
        self.error_label.grid(row=5, column=0, sticky="ew", padx=16, pady=4)
        pages = ctk.CTkFrame(self, fg_color="transparent")
        pages.grid(row=6, column=0, sticky="ew", padx=12, pady=8)
        ctk.CTkButton(pages, text="Назад", width=80, command=lambda: self.change_page(-1)).pack(side="left")
        self.page_label = ctk.CTkLabel(pages, text="")
        self.page_label.pack(side="left", expand=True)
        ctk.CTkButton(pages, text="Дальше", width=80, command=lambda: self.change_page(1)).pack(side="right")

    @property
    def account_id(self):
        return self.selector.account_id

    def _search(self):
        self.page = 0
        self.refresh()

    def _account_changed(self, aid):
        self.page = 0
        self.refresh()

    def refresh_accounts(self):
        self.selector.refresh(self.account_id)
        self.refresh()

    def selected(self):
        return [int(i) for i in self.tree.selection()]

    def refresh(self):
        selected = set(self.selected())
        rows, count = self.db.search_queue(self.account_id, self.search.get().strip(), PAGE_SIZE, self.page * PAGE_SIZE)
        total = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
        if self.page >= total:
            self.page = total - 1
            rows, count = self.db.search_queue(
                self.account_id, self.search.get().strip(), PAGE_SIZE, self.page * PAGE_SIZE
            )
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            date = parse_iso(row["scheduled_at"])
            media = json.loads(row["media_json"])
            self.tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    STATES.get(row["state"], row["state"]),
                    date.astimezone().strftime("%d.%m.%Y %H:%M") if date else "По интервалу",
                    len(media) or "",
                    row["content"].replace("\n", " ")[:240],
                ),
            )
            if row["id"] in selected:
                self.tree.selection_add(str(row["id"]))
        self.page_label.configure(text=f"Страница {self.page + 1}/{total} · Постов: {count}")
        item = self.db.next_queue_item(self.account_id) if self.account_id else None
        self.cached_next_id = item["id"] if item else None
        account = self.db.get_account(self.account_id) if self.account_id else None
        self.pause_button.configure(text="Продолжить" if account and account["queue_paused"] else "Пауза")
        self._signature = self.signature()
        self.show_selected_error()

    def signature(self):
        return (self.db.revision, self.account_id, self.page, self.search.get())

    def update_tick(self):
        if self.signature() != self._signature:
            self.refresh()
        w = self.scheduler.worker(self.account_id)
        if w:
            when = (
                datetime.fromtimestamp(w.next_run_timestamp).strftime("%d.%m %H:%M:%S") if w.next_run_timestamp else ""
            )
            self.worker_status.configure(text=f"{w.status}  {when}")

    def show_selected_error(self):
        ids = self.selected()
        row = self.db.get_queue_item(ids[0]) if ids else None
        self.error_label.configure(text=(row["last_error"] if row else "") or "")

    def change_page(self, delta):
        self.page = max(0, self.page + delta)
        self.refresh()

    def guard(self, action):
        try:
            action()
            self.scheduler.wake_all()
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Очередь", str(exc), parent=self)

    def edit_selected(self):
        ids = self.selected()
        if ids:
            self.edit_item(ids[0])

    def edit_item(self, qid):
        row = self.db.get_queue_item(qid)
        if not row:
            return
        if row["state"] in {"sending", "uncertain"} or row["attempt_count"] or row["record_json"]:
            messagebox.showinfo(
                "Отправка",
                "Сначала проверьте результат кнопкой «Повторить / сейчас». Отправлявшийся пост нельзя изменить.",
                parent=self,
            )
            return
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Пост #{qid}")
        dialog.geometry("800x700")
        dialog.transient(self.winfo_toplevel())
        composer = Composer(dialog)
        composer.pack(fill="both", expand=True, padx=10, pady=10)
        composer.load(row["content"], json.loads(row["media_json"]), row["scheduled_at"])

        def save():
            try:
                text, media, date = composer.value()
                self.db.edit_queue_item(qid, text, media, date)
                self.scheduler.wake(row["account_id"])
                self.refresh()
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror("Редактор", str(exc), parent=dialog)

        ctk.CTkButton(dialog, text="Сохранить", command=save).pack(pady=10)

    def publish_now(self):
        ids = self.selected() or ([self.cached_next_id] if self.cached_next_id else [])
        if ids and messagebox.askyesno(
            "Отправка", f"Отправить / проверить выбранные посты ({len(ids)}) сейчас?", parent=self
        ):
            self.guard(lambda: [self.db.request_send(q) for q in ids])

    def toggle_pause(self):
        if self.account_id:
            self.scheduler.toggle_pause(self.account_id)
            self.refresh()

    def skip_next(self):
        ids = self.selected()
        if ids and messagebox.askyesno(
            "Пропуск", f"Пропустить выбранные посты ({len(ids)})? Их можно вернуть из истории.", parent=self
        ):
            self.guard(lambda: [self.db.complete_queue_item(q, "", "", "skipped") for q in ids])

    def delete_selected(self):
        ids = self.selected()
        if ids and messagebox.askyesno("Удаление", f"Удалить выбранные посты ({len(ids)})?", parent=self):
            self.guard(lambda: self.db.bulk_delete(ids))

    def move_selected(self, direction):
        ids = self.selected()
        if ids:
            self.guard(lambda: self.db.move_queue_item(self.account_id, ids[0], direction))

    def import_txt(self):
        if self.import_running:
            return
        paths = filedialog.askopenfilenames(parent=self, filetypes=[("TXT", "*.txt")])
        if not paths:
            return
        self.import_running = True
        self.worker_status.configure(text="Проверка импорта…")

        def check():
            try:
                counts = {}
                bad = 0
                duplicates = 0
                seen = set()
                for path in paths:
                    for post in parse_content(read_text_file(__import__("pathlib").Path(path))):
                        if not post.valid:
                            bad += 1
                            continue
                        key = (post.account_handle, post.digest)
                        account = self.db.get_account(post.account_handle)
                        if key in seen or (account and self.db.post_exists(account["id"], post.content)):
                            duplicates += 1
                        else:
                            counts[post.account_handle] = counts.get(post.account_handle, 0) + 1
                        seen.add(key)
                summary = "\n".join(f"@{h}: {n} новых постов" for h, n in counts.items())
                summary += f"\nДубликатов: {duplicates}\nОшибок: {bad}\n\nДобавить корректные посты?"
                ui_call(self, lambda: self.confirm_import(paths, summary))
            except Exception as exc:
                ui_call(self, lambda exc=exc: self.import_done(None, str(exc)))

        threading.Thread(target=check, name="import-preview", daemon=True).start()

    def confirm_import(self, paths, summary):
        if not messagebox.askyesno("Предпросмотр импорта", summary, parent=self):
            self.import_running = False
            self.worker_status.configure(text="Импорт отменён")
            return

        def work():
            try:
                result = import_files(paths, self.db)
                ui_call(self, lambda: self.import_done(result, None))
            except Exception as exc:
                ui_call(self, lambda exc=exc: self.import_done(None, str(exc)))

        threading.Thread(target=work, name="queue-import", daemon=True).start()

    def import_done(self, result, error):
        self.import_running = False
        if error:
            messagebox.showerror("Импорт", error, parent=self)
        else:
            self.on_accounts_changed()
            self.refresh()
            messagebox.showinfo(
                "Импорт",
                f"Добавлено: {result.added}; дубликатов: {result.duplicates}; ошибок: {result.errors}",
                parent=self,
            )

    def sync_with_bluesky(self):
        aid = self.account_id
        if not aid:
            return
        self.sync_button.configure(state="disabled")

        def work():
            try:
                count = self.scheduler.reconcile_account(aid)
                ui_call(self, lambda: self.sync_done(f"Найдено совпадений среди последних 100 записей: {count}", False))
            except Exception as exc:
                ui_call(self, lambda exc=exc: self.sync_done(str(exc), True))

        threading.Thread(target=work, name="queue-reconcile", daemon=True).start()

    def sync_done(self, text, error):
        self.sync_button.configure(state="normal")
        self.refresh()
        (messagebox.showerror if error else messagebox.showinfo)("Сверка с Bluesky", text, parent=self)

    def show_history(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("История публикаций")
        dialog.geometry("900x620")
        dialog.transient(self.winfo_toplevel())
        search = ctk.CTkEntry(dialog, placeholder_text="Поиск по истории")
        search.pack(fill="x", padx=10, pady=8)
        tree = ttk.Treeview(dialog, columns=("date", "status", "text"), show="headings", selectmode="browse")
        tree.pack(fill="both", expand=True, padx=10)
        for col, title in [("date", "Дата"), ("status", "Статус"), ("text", "Текст")]:
            tree.heading(col, text=title)
        tree.column("text", width=500)
        rows = {}

        def refresh():
            rows.clear()
            tree.delete(*tree.get_children())
            for r in self.db.get_history(self.account_id, 100000):
                if search.get().casefold() in r["content"].casefold():
                    rows[str(r["id"])] = r
                    tree.insert(
                        "",
                        "end",
                        iid=str(r["id"]),
                        values=(
                            r["completed_at"][:19],
                            {"published": "Опубликован", "skipped": "Пропущен"}[r["status"]],
                            r["content"].replace("\n", " ")[:150],
                        ),
                    )

        def action(restore=False):
            selected = tree.selection()
            if not selected:
                return
            row = rows[selected[0]]
            try:
                if restore:
                    qid = self.db.restore_history(row["id"])
                    if qid is None:
                        raise ValueError("Пост уже есть в очереди")
                    self.scheduler.wake(row["account_id"])
                    self.refresh()
                    messagebox.showinfo("История", "Пост возвращён в очередь", parent=dialog)
                elif row["post_uri"].startswith("at://"):
                    parts = row["post_uri"][5:].split("/")
                    if len(parts) == 3:
                        webbrowser.open(f"https://bsky.app/profile/{parts[0]}/post/{parts[2]}")
            except Exception as exc:
                messagebox.showerror("История", str(exc), parent=dialog)

        bar = ctk.CTkFrame(dialog, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=8)
        ctk.CTkButton(bar, text="Вернуть пропущенный", command=lambda: action(True)).pack(side="left", padx=4)
        ctk.CTkButton(bar, text="Открыть в Bluesky", command=action).pack(side="left", padx=4)
        search.bind("<Return>", lambda e: refresh())
        refresh()

    def show_errors(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Ошибки импорта")
        dialog.geometry("800x550")
        box = ctk.CTkTextbox(dialog)
        box.pack(fill="both", expand=True, padx=10, pady=10)
        box.insert(
            "1.0",
            "\n\n".join(
                f"{r['file_name']} · {r['error_reason']}\n{r['raw_content']}" for r in self.db.get_import_errors()
            )
            or "Ошибок нет",
        )
        box.configure(state="disabled")

    def show_calendar(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Календарь публикаций")
        dialog.geometry("900x720")
        dialog.transient(self.winfo_toplevel())
        current = datetime.now()
        month = [current.year, current.month]
        title = ctk.CTkLabel(dialog, text="")
        title.pack(pady=8)
        nav = ctk.CTkFrame(dialog, fg_color="transparent")
        nav.pack()
        grid = ctk.CTkFrame(dialog)
        grid.pack(fill="x", padx=12, pady=10)
        for c in range(7):
            grid.grid_columnconfigure(c, weight=1)
        info = ctk.CTkLabel(
            dialog,
            text="Время местное. ~ — оценка по интервалу, без учёта jitter и ошибок. Пауза аккаунта сохраняется.",
            wraplength=850,
        )
        info.pack(pady=4)
        listing = ttk.Treeview(dialog, columns=("time", "account", "text"), show="headings")
        listing.pack(fill="both", expand=True, padx=12, pady=8)
        for col, label in [("time", "Время"), ("account", "Аккаунт"), ("text", "Пост")]:
            listing.heading(col, text=label)
        listing.column("text", width=450)
        entries = []

        def select(day):
            listing.delete(*listing.get_children())
            for when, estimated, account, row in sorted(entries, key=lambda x: x[0]):
                if (when.year, when.month, when.day) == (month[0], month[1], day):
                    listing.insert(
                        "",
                        "end",
                        iid=str(row["id"]),
                        values=(
                            ("~ " if estimated else "") + when.strftime("%H:%M"),
                            account["handle"],
                            row["content"].replace("\n", " ")[:140],
                        ),
                    )

        def render():
            entries.clear()
            for account in self.db.get_accounts():
                pointer = max(
                    datetime.now(UTC),
                    parse_iso(account["next_scheduled_at"])
                    or datetime.now(UTC) + timedelta(minutes=account["interval_minutes"]),
                )
                for row in self.db.get_queue(account["id"], 100000):
                    if row["state"] == "failed":
                        continue
                    fixed = parse_iso(row["scheduled_at"])
                    when = fixed or pointer
                    if not fixed:
                        pointer += timedelta(minutes=account["interval_minutes"])
                    entries.append((when.astimezone(), not bool(fixed), account, row))
            title.configure(text=f"{month[1]:02d}.{month[0]}")
            for widget in grid.winfo_children():
                widget.destroy()
            for i, label in enumerate(["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]):
                ctk.CTkLabel(grid, text=label).grid(row=0, column=i)
            for week, days in enumerate(calendar.monthcalendar(*month), start=1):
                for col, day in enumerate(days):
                    if day:
                        count = sum((e[0].year, e[0].month, e[0].day) == (*month, day) for e in entries)
                        ctk.CTkButton(
                            grid,
                            text=f"{day}" + (f" · {count}" if count else ""),
                            width=90,
                            height=36,
                            command=lambda d=day: select(d),
                        ).grid(row=week, column=col, sticky="ew", padx=3, pady=3)
            select(current.day if month == [current.year, current.month] else 1)

        def move(delta):
            number = month[0] * 12 + month[1] - 1 + delta
            month[:] = [number // 12, number % 12 + 1]
            render()

        ctk.CTkButton(nav, text="← Месяц", command=lambda: move(-1)).pack(side="left", padx=4)
        ctk.CTkButton(nav, text="Обновить", command=render).pack(side="left", padx=4)
        ctk.CTkButton(nav, text="Месяц →", command=lambda: move(1)).pack(side="left", padx=4)
        listing.bind(
            "<Double-1>", lambda e: self.edit_item(int(listing.selection()[0])) if listing.selection() else None
        )
        render()
