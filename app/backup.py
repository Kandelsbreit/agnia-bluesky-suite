from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"AGNIA-BACKUP-2\n"
MAX_ARCHIVE = 2 * 1024**3


def _derive(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode("utf-8"))


def create_backup(db, destination: Path, password: str = "") -> Path:
    root = db.path.parent
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agnia-backup-") as temp:
        copy = Path(temp) / "agnia_bluesky.db"
        with db._lock, db._connection() as source, closing(sqlite3.connect(copy)) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("База не прошла проверку целостности")
            refs = set()
            for table in ("queue", "post_history", "drafts"):
                columns = {r[1] for r in target.execute(f"PRAGMA table_info({table})")}
                if "media_json" in columns:
                    for row in target.execute(f"SELECT media_json FROM {table}"):
                        refs.update(m["file"] for m in json.loads(row[0]) if m.get("file"))
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(copy, "agnia_bluesky.db")
            key = root / ".secret_key"
            if key.exists():
                z.write(key, ".secret_key")
            for name in refs:
                path = root / "media" / name
                if Path(name).name != name or not path.is_file():
                    raise ValueError("Не найдено вложение для резервной копии")
                z.write(path, "media/" + name)
            z.writestr("manifest.json", json.dumps({"format": 2, "created_at": datetime.now(UTC).isoformat()}))
        raw = output.getvalue()
    if password:
        if len(password) < 8:
            raise ValueError("Пароль резервной копии: минимум 8 символов")
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        raw = MAGIC + salt + nonce + AESGCM(_derive(password, salt)).encrypt(nonce, raw, MAGIC)
    temp = destination.with_name(destination.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        with temp.open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return destination


def stage_restore(archive: Path, root: Path, password: str = "") -> None:
    if archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Архив слишком большой")
    raw = archive.read_bytes()
    if raw.startswith(MAGIC):
        start = len(MAGIC)
        salt = raw[start : start + 16]
        nonce = raw[start + 16 : start + 28]
        try:
            raw = AESGCM(_derive(password, salt)).decrypt(nonce, raw[start + 28 :], MAGIC)
        except Exception as exc:
            raise ValueError("Неверный пароль или повреждённый архив") from exc
    staged = root / "restore_pending"
    with tempfile.TemporaryDirectory(dir=root, prefix="restore-check-") as tmp:
        temp = Path(tmp)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_ARCHIVE:
                raise ValueError("Архив слишком большой")
            if len(z.namelist()) != len(set(z.namelist())):
                raise ValueError("Повторяющиеся файлы в архиве")
            for name in z.namelist():
                import re

                if name not in {"manifest.json", "agnia_bluesky.db", ".secret_key"} and not re.fullmatch(
                    r"media/[a-f0-9]{64}\.(jpg|png|mp4)", name
                ):
                    raise ValueError("Неподдерживаемый файл в архиве")
                dest = temp / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(name))
        if json.loads((temp / "manifest.json").read_text())["format"] != 2:
            raise ValueError("Неподдерживаемая версия резервной копии")
        database = temp / "agnia_bluesky.db"
        with closing(sqlite3.connect(database)) as c:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Повреждённая база данных")
            if c.execute("PRAGMA user_version").fetchone()[0] > 2:
                raise ValueError("Для этой копии нужна более новая версия программы")
            import base64

            from app.security import _decrypt_portable_v2

            for (cipher,) in c.execute("SELECT password_cipher FROM accounts"):
                if cipher.startswith("portable-v2:"):
                    _decrypt_portable_v2(base64.b64decode(cipher.split(":", 1)[1]), (temp / ".secret_key").read_bytes())
            for table in ("queue", "post_history", "drafts"):
                columns = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
                if "media_json" not in columns:
                    continue
                for (value,) in c.execute(f"SELECT media_json FROM {table}"):
                    for m in json.loads(value):
                        if m.get("file"):
                            import hashlib

                            file = temp / "media" / m["file"]
                            if (
                                file.parent != temp / "media"
                                or not file.is_file()
                                or hashlib.sha256(file.read_bytes()).hexdigest() != file.stem
                            ):
                                raise ValueError("Повреждено вложение в архиве")
        if staged.exists():
            shutil.rmtree(staged)
        shutil.copytree(temp, staged)
        (staged / "READY").write_text("2")


def apply_staged_restore(root: Path) -> bool:
    staged = root / "restore_pending"
    if not (staged / "READY").exists():
        return False
    # Keep source files until every replace is finished: interruption resumes on next launch.
    for source in sorted(staged.rglob("*")):
        if not source.is_file() or source.name in {"READY", "manifest.json"}:
            continue
        target = root / source.relative_to(staged)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.name == "agnia_bluesky.db":
            for suffix in ("-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)
        temp = target.with_name(target.name + ".restoring")
        shutil.copy2(source, temp)
        os.replace(temp, target)
    shutil.rmtree(staged)
    return True


def automatic_backup(db, *, force: bool = False) -> Path | None:
    folder = db.path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    existing = sorted(folder.glob("auto-*.zip"))
    if not force and existing and datetime.now().timestamp() - existing[-1].stat().st_mtime < 86400:
        return None
    path = folder / ("auto-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".zip")
    create_backup(db, path)
    for old in sorted(folder.glob("auto-*.zip"))[:-7]:
        old.unlink()
    return path
