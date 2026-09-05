from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps

from app.paths import data_dir
from app.utils import content_hash, count_graphemes, normalize_text

MAX_IMAGE_BYTES = 1_000_000
MAX_VIDEO_BYTES = 50 * 1024 * 1024


def payload_hash(text: str, media: list) -> str:
    if not media:
        return content_hash(text)
    identity = [{k: v for k, v in m.items() if k not in {"name"}} for m in media]
    return hashlib.sha256(
        json.dumps([normalize_text(text), identity], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def media_path(item: dict) -> Path:
    name = item.get("file", "")
    if not re.fullmatch(r"[a-f0-9]{64}\.(jpg|png|mp4)", name):
        raise ValueError("Неверное имя вложения")
    return data_dir() / "media" / name


def validate_media(media: list, *, check_files: bool = True) -> None:
    if not isinstance(media, list) or len(media) > 4:
        raise ValueError("Можно прикрепить до четырёх изображений или одно видео / карточку ссылки")
    kinds = {m.get("kind") for m in media}
    if media and (
        not kinds <= {"image", "video", "link"} or len(kinds) > 1 or ("image" not in kinds and len(media) != 1)
    ):
        raise ValueError("Выберите изображения, одно видео или одну карточку ссылки")
    for m in media:
        if not isinstance(m.get("alt", ""), str) or count_graphemes(m.get("alt", "")) > 1000:
            raise ValueError("Описание вложения: максимум 1000 символов")
        if m["kind"] == "link":
            url = urlsplit(m.get("uri", ""))
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("Нужна корректная ссылка http:// или https://")
            if len(m["uri"]) > 2000 or len(m.get("title", "")) > 300 or len(m.get("description", "")) > 1000:
                raise ValueError("Сократите ссылку, заголовок (300) или описание (1000)")
        else:
            path = media_path(m)
            if check_files:
                if not path.is_file():
                    raise ValueError("Вложение отсутствует. Восстановите резервную копию или выберите файл заново.")
                raw = path.read_bytes()
                maximum = MAX_IMAGE_BYTES if m["kind"] == "image" else MAX_VIDEO_BYTES
                if len(raw) > maximum or hashlib.sha256(raw).hexdigest() != path.stem:
                    raise ValueError("Вложение повреждено или превышает лимит размера")


def import_media(path: str | Path) -> dict:
    source = Path(path)
    if source.suffix.lower() == ".mp4":
        if source.stat().st_size > MAX_VIDEO_BYTES:
            raise ValueError("Видео MP4 должно быть не больше 50 МиБ")
        raw = source.read_bytes()
        if len(raw) < 12 or raw[4:8] != b"ftyp":
            raise ValueError("Файл не является MP4")
        kind, suffix = "video", "mp4"
        dimensions = {}
    else:
        with Image.open(source) as original:
            if original.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Поддерживаются JPEG, PNG, WebP и видео MP4")
            img = ImageOps.exif_transpose(original)
            img.thumbnail((2048, 2048))
            if img.mode not in {"RGB", "RGBA"}:
                img = img.convert("RGBA" if "transparency" in img.info else "RGB")
            # Preserve alpha where possible; flatten on white only if compression is necessary.
            buffer = io.BytesIO()
            img.save(buffer, format="PNG", optimize=True)
            raw = buffer.getvalue()
            suffix = "png"
            if len(raw) > MAX_IMAGE_BYTES:
                if img.mode == "RGBA":
                    bg = Image.new("RGB", img.size, "white")
                    bg.paste(img, mask=img.getchannel("A"))
                    img = bg
                else:
                    img = img.convert("RGB")
                for quality in (90, 80, 65, 50, 35):
                    buffer = io.BytesIO()
                    img.save(buffer, format="JPEG", quality=quality, optimize=True)
                    raw = buffer.getvalue()
                    if len(raw) <= MAX_IMAGE_BYTES:
                        break
                suffix = "jpg"
            if len(raw) > MAX_IMAGE_BYTES:
                raise ValueError("Не удалось уменьшить картинку до 1 МБ. Выберите изображение меньшего размера.")
            dimensions = {"width": img.width, "height": img.height}
        kind = "image"
    folder = data_dir() / "media"
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(raw).hexdigest() + "." + suffix
    target = folder / name
    if not target.exists():
        temp = folder / (name + "." + secrets.token_hex(6) + ".tmp")
        try:
            with temp.open("xb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    return {"kind": kind, "file": name, "name": source.name, "alt": "", **dimensions}
