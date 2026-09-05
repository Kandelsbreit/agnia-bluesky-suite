from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from tkinter import filedialog, messagebox

import customtkinter as ctk

from app.media import import_media, validate_media
from app.ui.common import ui_call
from app.utils import count_graphemes, parse_iso, post_validation_error


class Composer(ctk.CTkFrame):
    def __init__(self, master, *, on_change=None, **kwargs):
        super().__init__(master, **kwargs)
        self.on_change = on_change
        self.media = []
        self.loading = False
        self._generation = 0
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.text_box = ctk.CTkTextbox(self, wrap="word", font=ctk.CTkFont(size=14), height=180)
        self.text_box.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self.text_box.bind("<KeyRelease>", lambda e: self.changed())
        self.counter = ctk.CTkLabel(self, text="0 / 300")
        self.counter.grid(row=1, column=0, sticky="e", padx=8)
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=2, column=0, sticky="ew", padx=8)
        self.add_button = ctk.CTkButton(buttons, text="Фото / видео", width=120, command=self.add_media)
        self.add_button.pack(side="left", padx=3)
        ctk.CTkButton(buttons, text="Карточка ссылки", width=140, command=self.add_link).pack(side="left", padx=3)
        ctk.CTkButton(buttons, text="Предпросмотр", width=130, command=self.preview).pack(side="left", padx=3)
        self.attachments = ctk.CTkScrollableFrame(self, height=95)
        self.attachments.grid(row=3, column=0, sticky="ew", padx=8, pady=6)
        self.attachments.grid_columnconfigure(0, weight=1)
        date_row = ctk.CTkFrame(self, fg_color="transparent")
        date_row.grid(row=4, column=0, sticky="ew", padx=8, pady=6)
        ctk.CTkLabel(date_row, text="Дата и время (местное):").pack(side="left", padx=3)
        self.schedule = ctk.CTkEntry(date_row, placeholder_text="ГГГГ-ММ-ДД ЧЧ:ММ", width=190)
        self.schedule.pack(side="left", padx=6)
        self.schedule.bind("<KeyRelease>", lambda e: self.changed())
        ctk.CTkLabel(date_row, text="Пусто — обычная очередь", text_color="gray60").pack(side="left", padx=3)

    def text(self):
        return self.text_box.get("1.0", "end-1c").strip()

    def changed(self):
        text = self.text()
        error = post_validation_error(text)
        self.counter.configure(
            text=f"{count_graphemes(text)} / 300 · {len(text.encode())} / 3000 байт",
            text_color="#E84D6A" if error and text else "gray60",
        )
        if self.on_change:
            self.on_change()

    def load(self, text="", media=None, scheduled_at=None):
        self._generation += 1
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", text)
        self.media = json.loads(json.dumps(media or []))
        self.schedule.delete(0, "end")
        if scheduled_at:
            self.schedule.insert(0, parse_iso(scheduled_at).astimezone().strftime("%Y-%m-%d %H:%M"))
        self.render_media()
        self.changed()

    def value(self):
        if self.loading:
            raise ValueError("Дождитесь подготовки вложения")
        text = self.text()
        error = post_validation_error(text)
        if error:
            raise ValueError(error)
        validate_media(self.media)
        raw = self.schedule.get().strip()
        scheduled = None
        if raw:
            try:
                scheduled = datetime.strptime(raw, "%Y-%m-%d %H:%M").astimezone(UTC).isoformat()
            except ValueError as exc:
                raise ValueError("Дата должна быть в формате ГГГГ-ММ-ДД ЧЧ:ММ") from exc
        return text, self.media, scheduled

    def render_media(self):
        for child in self.attachments.winfo_children():
            child.destroy()
        for i, m in enumerate(self.media):
            row = ctk.CTkFrame(self.attachments)
            row.grid(row=i, column=0, sticky="ew", pady=3)
            row.grid_columnconfigure(1, weight=1)
            label = m.get("name") or m.get("uri", "")
            ctk.CTkLabel(row, text=label[:35], width=180, anchor="w").grid(row=0, column=0, padx=6)
            entry = ctk.CTkEntry(row, placeholder_text="Описание изображения / видео (alt)")
            entry.insert(0, m.get("alt", ""))
            entry.grid(row=0, column=1, sticky="ew", padx=4)
            entry.bind("<KeyRelease>", lambda e, m=m, w=entry: self.set_alt(m, w.get()))
            if m["kind"] == "link":
                entry.configure(state="disabled")
            ctk.CTkButton(row, text="×", width=30, command=lambda i=i: self.remove_media(i)).grid(
                row=0, column=2, padx=4
            )

    def set_alt(self, item, value):
        item["alt"] = value
        self.changed()

    def remove_media(self, index):
        self.media.pop(index)
        self.render_media()
        self.changed()

    def add_media(self):
        if self.loading:
            return
        paths = filedialog.askopenfilenames(
            parent=self,
            title="Выбрать изображения или одно видео",
            filetypes=[("Медиа", "*.jpg *.jpeg *.png *.webp *.mp4")],
        )
        if not paths:
            return
        if len(paths) + len(self.media) > 4:
            messagebox.showerror("Вложения", "Можно добавить до четырёх изображений", parent=self)
            return
        self.loading = True
        self.add_button.configure(state="disabled", text="Подготовка…")
        original = json.loads(json.dumps(self.media))
        generation = self._generation

        def work():
            try:
                added = [import_media(p) for p in paths]
                combined = original + added
                validate_media(combined)
                ui_call(self, lambda: self.media_done(combined, None, generation))
            except Exception as exc:
                ui_call(self, lambda exc=exc: self.media_done(None, str(exc), generation))

        threading.Thread(target=work, name="media-import", daemon=True).start()

    def media_done(self, media, error, generation):
        self.loading = False
        self.add_button.configure(state="normal", text="Фото / видео")
        if generation != self._generation:
            return
        if error:
            messagebox.showerror("Вложения", error, parent=self)
        else:
            self.media = media
            self.render_media()
            self.changed()

    def add_link(self):
        if self.media:
            messagebox.showinfo("Карточка", "Сначала уберите остальные вложения", parent=self)
            return
        dialog = ctk.CTkToplevel(self)
        dialog.title("Карточка ссылки")
        dialog.geometry("560x300")
        dialog.transient(self.winfo_toplevel())
        fields = []
        for label in ["Ссылка https://…", "Заголовок", "Описание"]:
            ctk.CTkLabel(dialog, text=label).pack(anchor="w", padx=12)
            entry = ctk.CTkEntry(dialog)
            entry.pack(fill="x", padx=12, pady=3)
            fields.append(entry)

        def save():
            item = {
                "kind": "link",
                "uri": fields[0].get().strip(),
                "title": fields[1].get(),
                "description": fields[2].get(),
            }
            try:
                validate_media([item])
            except ValueError as exc:
                messagebox.showerror("Карточка", str(exc), parent=dialog)
                return
            self.media = [item]
            self.render_media()
            self.changed()
            dialog.destroy()

        ctk.CTkButton(dialog, text="Добавить", command=save).pack(pady=12)

    def preview(self):
        from PIL import Image

        from app.media import media_path

        dialog = ctk.CTkToplevel(self)
        dialog.title("Предпросмотр поста")
        dialog.geometry("620x650")
        dialog.transient(self.winfo_toplevel())
        frame = ctk.CTkScrollableFrame(dialog)
        frame.pack(fill="both", expand=True, padx=10, pady=10)
        ctk.CTkLabel(frame, text=self.text(), wraplength=550, justify="left", anchor="w").pack(
            fill="x", padx=10, pady=10
        )
        dialog.preview_images = []
        for m in self.media:
            if m["kind"] == "image":
                with Image.open(media_path(m)) as image:
                    image.thumbnail((480, 300))
                    photo = ctk.CTkImage(image.copy(), size=image.size)
                dialog.preview_images.append(photo)
                ctk.CTkLabel(frame, text="", image=photo).pack(pady=5)
                ctk.CTkLabel(frame, text=m.get("alt", ""), wraplength=540).pack()
            elif m["kind"] == "link":
                ctk.CTkLabel(
                    frame, text=f"{m.get('title', '')}\n{m['uri']}\n{m.get('description', '')}", wraplength=540
                ).pack(pady=10)
            else:
                ctk.CTkLabel(
                    frame, text="Видео MP4: " + m.get("name", "") + "\n" + m.get("alt", ""), wraplength=540
                ).pack(pady=10)
