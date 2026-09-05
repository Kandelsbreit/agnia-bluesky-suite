from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger
from app.paths import database_path
from app.security import SecretError, protect_secret, unprotect_secret
from app.utils import (
    content_hash,
    new_record_key,
    normalize_handle,
    normalize_text,
    parse_iso,
    post_validation_error,
    utcnow_iso,
)

DEFAULT_SETTINGS: dict[str, str] = {
    "theme": "dark",
    "close_to_tray": "1",
    "start_minimized": "0",
    "autostart": "0",
    "like_min_delay": "5",
    "like_max_delay": "12",
    "human_breaks": "1",
    "break_every_min": "15",
    "break_every_max": "25",
    "break_duration_min": "120",
    "break_duration_max": "300",
    "like_limit": "100",
    "follow_limit": "30",
    "auto_like_enabled": "1",
    "auto_like_account_id": "",
    "auto_like_all_accounts": "1",
    "auto_like_account_ids": "",
    "auto_like_247_mode": "1",
    "auto_like_cycle_interval": "60",
    "auto_unpause_queues_on_start": "0",
    "auto_like_source": "timeline",
    "auto_like_query": "",
    "auto_like_skip_replies": "1",
    "auto_like_skip_reposts": "1",
}


class Database:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.revision = 0
        if self.path.exists() and self.path.stat().st_size:
            with closing(sqlite3.connect(self.path)) as existing:
                version = existing.execute("PRAGMA user_version").fetchone()[0]
                if version > 2:
                    raise ValueError("Для этой базы нужна более новая версия программы")
            if version < 2:
                from app.backup import create_backup

                create_backup(
                    self, self.path.parent / "backups" / ("before-v1.3-" + utcnow_iso().replace(":", "-") + ".zip")
                )
        self._init_schema()
        self._heal_legacy_queue_keys()
        self._migrate_outbox()
        self._prune_queue_already_in_history()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.create_function("casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self):
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
                self.revision += 1
            except Exception:
                connection.rollback()
                raise

    def _init_schema(self) -> None:
        with self._write() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    handle TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    did TEXT NOT NULL DEFAULT '',
                    password_cipher TEXT NOT NULL DEFAULT '',
                    interval_minutes INTEGER NOT NULL DEFAULT 60 CHECK(interval_minutes >= 1),
                    jitter_minutes INTEGER NOT NULL DEFAULT 2 CHECK(jitter_minutes >= 0),
                    queue_paused INTEGER NOT NULL DEFAULT 0,
                    next_scheduled_at TEXT,
                    last_posted_at TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    connection_status TEXT NOT NULL DEFAULT 'Не проверен',
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, content_hash),
                    UNIQUE(account_id, record_key)
                );
                CREATE INDEX IF NOT EXISTS idx_queue_account_position
                    ON queue(account_id, position, id);

                CREATE TABLE IF NOT EXISTS post_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('published', 'skipped')),
                    post_uri TEXT NOT NULL DEFAULT '',
                    post_cid TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL,
                    UNIQUE(account_id, content_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_history_account_date
                    ON post_history(account_id, completed_at DESC);

                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                    action_type TEXT NOT NULL,
                    target_key TEXT NOT NULL DEFAULT '',
                    target_handle TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_activity_account_type_target
                    ON activity(account_id, action_type, target_key);
                CREATE INDEX IF NOT EXISTS idx_activity_created
                    ON activity(created_at DESC);

                CREATE TABLE IF NOT EXISTS import_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_name TEXT NOT NULL DEFAULT '',
                    account_handle TEXT NOT NULL DEFAULT '',
                    raw_content TEXT NOT NULL DEFAULT '',
                    error_reason TEXT NOT NULL,
                    char_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                DEFAULT_SETTINGS.items(),
            )

    def _heal_legacy_queue_keys(self) -> int:
        """Upgrade any non-TID (e.g. 32-char UUID hex) keys to canonical 13-char TIDs."""
        updated = 0
        with self._write() as connection:
            rows = connection.execute(
                "SELECT id, record_key FROM queue WHERE length(record_key) != 13 AND attempt_count=0"
            ).fetchall()
            if rows:
                updates = [(new_record_key(), row["id"]) for row in rows]
                connection.executemany(
                    "UPDATE queue SET record_key=? WHERE id=?",
                    updates,
                )
                updated = len(updates)
            connection.execute(
                """
                UPDATE accounts SET queue_paused=0, last_error=''
                WHERE last_error LIKE '%Invalid TID string%'
                """
            )
        return updated

    def _migrate_outbox(self) -> None:
        with self._write() as connection:
            for table, columns in {
                "queue": {
                    "state": "TEXT NOT NULL DEFAULT 'pending'",
                    "media_json": "TEXT NOT NULL DEFAULT '[]'",
                    "scheduled_at": "TEXT",
                    "send_now": "INTEGER NOT NULL DEFAULT 0",
                    "record_json": "TEXT NOT NULL DEFAULT ''",
                },
                "post_history": {"media_json": "TEXT NOT NULL DEFAULT '[]'", "last_error": "TEXT NOT NULL DEFAULT ''"},
            }.items():
                existing = {r["name"] for r in connection.execute(f"PRAGMA table_info({table})")}
                for name, definition in columns.items():
                    if name not in existing:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS drafts (account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE, content TEXT NOT NULL DEFAULT '', media_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL)"
            )
            columns = {r["name"] for r in connection.execute("PRAGMA table_info(drafts)")}
            if "schedule_text" not in columns:
                connection.execute("ALTER TABLE drafts ADD COLUMN schedule_text TEXT NOT NULL DEFAULT ''")
            connection.execute("PRAGMA user_version=2")

    def recover_interrupted(self) -> None:
        # Called once after the process lock has been acquired, never by another DB reader.
        with self._write() as connection:
            connection.execute(
                "UPDATE queue SET state='uncertain', last_error='Отправка прервана. Будет проверен прежний ключ.' WHERE state='sending'"
            )

    def _prune_queue_already_in_history(self) -> int:
        """Remove any queue items that have already been published and exist in post_history."""
        with self._write() as connection:
            cursor = connection.execute(
                """
                DELETE FROM queue WHERE id IN (
                    SELECT q.id FROM queue q
                    JOIN post_history h ON q.account_id = h.account_id AND q.content_hash = h.content_hash AND h.status='published'
                )
                """
            )
            count = cursor.rowcount
            if count > 0:
                get_logger().info(
                    "Удалено %d постов из очереди, которые уже были ранее опубликованы (защита от дублирования)",
                    count,
                )
            return count

    # Settings
    def get_setting(self, key: str, default: str | None = None) -> str:
        fallback = DEFAULT_SETTINGS.get(key, "") if default is None else str(default)
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else fallback

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get_setting(key, "1" if default else "0").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get_setting(key, str(default)))
        except ValueError:
            return default

    def get_float(self, key: str, default: float) -> float:
        try:
            return float(self.get_setting(key, str(default)))
        except ValueError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        with self._write() as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def set_settings(self, values: dict[str, Any]) -> None:
        with self._write() as connection:
            connection.executemany(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(key, str(value)) for key, value in values.items()],
            )

    # Accounts
    @staticmethod
    def _public_account(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        cipher = item.pop("password_cipher", "")
        item["has_password"] = bool(cipher)
        return item

    def get_accounts(self) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT * FROM accounts ORDER BY created_at, id").fetchall()
            return [self._public_account(row) for row in rows]

    def get_account(self, account: int | str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            if isinstance(account, int):
                row = connection.execute("SELECT * FROM accounts WHERE id=?", (account,)).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM accounts WHERE handle=? COLLATE NOCASE", (normalize_handle(account),)
                ).fetchone()
            return self._public_account(row) if row else None

    def get_account_secret(self, account_id: int) -> tuple[dict[str, Any] | None, str]:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            return None, ""
        item = self._public_account(row)
        cipher = row["password_cipher"]
        if not cipher:
            return item, ""
        try:
            secret = unprotect_secret(cipher)
            return item, secret
        except (SecretError, ValueError, UnicodeError) as exc:
            get_logger().error("Не удалось расшифровать App Password для @%s: %s", item["handle"], exc)
            item["secret_error"] = str(exc)
            return item, ""

    def save_account(
        self,
        handle: str,
        password: str | None = None,
        *,
        display_name: str | None = None,
        did: str | None = None,
        interval_minutes: int = 60,
        jitter_minutes: int = 2,
    ) -> int:
        clean = normalize_handle(handle)
        if not clean:
            raise ValueError("Пустой handle аккаунта")
        interval = max(1, int(interval_minutes))
        jitter = max(0, int(jitter_minutes))
        now = utcnow_iso()
        cipher = protect_secret(password) if password is not None else None
        with self._write() as connection:
            existing = connection.execute("SELECT id FROM accounts WHERE handle=? COLLATE NOCASE", (clean,)).fetchone()
            if existing:
                fields = [
                    "interval_minutes=?",
                    "jitter_minutes=?",
                    "updated_at=?",
                ]
                params: list[Any] = [interval, jitter, now]
                if password is not None:
                    fields.append("password_cipher=?")
                    params.append(cipher or "")
                if display_name is not None:
                    fields.append("display_name=?")
                    params.append(display_name)
                if did is not None:
                    fields.append("did=?")
                    params.append(did)
                params.append(existing["id"])
                connection.execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id=?", params)
                return int(existing["id"])

            cursor = connection.execute(
                """
                INSERT INTO accounts(
                    handle, display_name, did, password_cipher, interval_minutes,
                    jitter_minutes, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (clean, display_name or clean, did or "", cipher or "", interval, jitter, now, now),
            )
            account_id = int(cursor.lastrowid)
            active = connection.execute("SELECT value FROM settings WHERE key='active_account_id'").fetchone()
            if not active or not active["value"]:
                connection.execute(
                    "INSERT INTO settings(key,value) VALUES('active_account_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(account_id),),
                )
            return account_id

    def ensure_account(self, handle: str) -> int:
        existing = self.get_account(handle)
        if existing:
            return int(existing["id"])
        return self.save_account(handle, None)

    def delete_account(self, account_id: int) -> None:
        with self._write() as connection:
            if connection.execute(
                "SELECT 1 FROM queue WHERE account_id=? AND state IN ('sending','uncertain')", (account_id,)
            ).fetchone():
                raise ValueError("У аккаунта есть незавершённая отправка. Сначала проверьте её результат.")
            connection.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            active = connection.execute("SELECT value FROM settings WHERE key='active_account_id'").fetchone()
            if active and active["value"] == str(account_id):
                replacement = connection.execute("SELECT id FROM accounts ORDER BY created_at, id LIMIT 1").fetchone()
                connection.execute(
                    "UPDATE settings SET value=? WHERE key='active_account_id'",
                    (str(replacement["id"]) if replacement else "",),
                )

    def set_active_account(self, account_id: int) -> None:
        if self.get_account(account_id):
            self.set_setting("active_account_id", account_id)

    def get_active_account(self) -> dict[str, Any] | None:
        try:
            active_id = int(self.get_setting("active_account_id", "0"))
        except ValueError:
            active_id = 0
        account = self.get_account(active_id) if active_id else None
        if account:
            return account
        accounts = self.get_accounts()
        return accounts[0] if accounts else None

    def update_connection(self, account_id: int, status: str, display_name: str = "", did: str = "") -> None:
        now = utcnow_iso()
        with self._write() as connection:
            connection.execute(
                """
                UPDATE accounts SET connection_status=?, last_checked_at=?,
                    display_name=CASE WHEN ?<>'' THEN ? ELSE display_name END,
                    did=CASE WHEN ?<>'' THEN ? ELSE did END,
                    updated_at=? WHERE id=?
                """,
                (status, now, display_name, display_name, did, did, now, account_id),
            )

    def set_queue_paused(self, account_id: int, paused: bool) -> None:
        with self._write() as connection:
            connection.execute(
                "UPDATE accounts SET queue_paused=?, updated_at=? WHERE id=?",
                (1 if paused else 0, utcnow_iso(), account_id),
            )

    def unpause_all_queues(self) -> int:
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE accounts SET queue_paused=0, updated_at=? WHERE queue_paused=1", (utcnow_iso(),)
            )
            return cursor.rowcount

    def update_runtime(self, account_id: int, **values: Any) -> None:
        allowed = {"next_scheduled_at", "last_posted_at", "retry_count", "last_error"}
        pairs = [(key, value) for key, value in values.items() if key in allowed]
        if not pairs:
            return
        sql = ", ".join(f"{key}=?" for key, _ in pairs)
        params = [value for _, value in pairs] + [utcnow_iso(), account_id]
        with self._write() as connection:
            connection.execute(f"UPDATE accounts SET {sql}, updated_at=? WHERE id=?", params)

    # Queue
    def enqueue_one(
        self,
        account: int | str,
        text: str,
        at_top: bool = False,
        *,
        media: list | None = None,
        scheduled_at: str | None = None,
        send_now: bool = False,
    ) -> int | None:
        account_id = account if isinstance(account, int) else self.ensure_account(account)
        normalized = text.strip()
        if not normalized:
            raise ValueError("Пустой текст поста")
        from app.media import payload_hash, validate_media

        validate_media(media or [])
        if post_validation_error(normalized):
            raise ValueError(post_validation_error(normalized))
        digest = payload_hash(normalized, media or [])
        if scheduled_at and not parse_iso(scheduled_at):
            raise ValueError("Неверная дата публикации")
        scheduled_at = parse_iso(scheduled_at).isoformat() if scheduled_at else None
        now = utcnow_iso()
        with self._write() as connection:
            duplicate = connection.execute(
                """
                SELECT 1 FROM queue WHERE account_id=? AND content_hash=?
                UNION ALL
                SELECT 1 FROM post_history WHERE account_id=? AND content_hash=? AND status='published'
                LIMIT 1
                """,
                (account_id, digest, account_id, digest),
            ).fetchone()
            if duplicate:
                return None
            if at_top:
                row = connection.execute(
                    "SELECT COALESCE(MIN(position), 1) AS min_position FROM queue WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                new_position = int(row["min_position"]) - 1
            else:
                row = connection.execute(
                    "SELECT COALESCE(MAX(position), 0) AS max_position FROM queue WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                new_position = int(row["max_position"]) + 1
            cursor = connection.execute(
                """
                INSERT INTO queue(account_id,content,content_hash,record_key,position,created_at,media_json,scheduled_at,send_now)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    account_id,
                    normalized,
                    digest,
                    new_record_key(),
                    new_position,
                    now,
                    json.dumps(media or [], ensure_ascii=False),
                    scheduled_at,
                    int(send_now),
                ),
            )
            return int(cursor.lastrowid)

    def post_exists(self, account_id: int, text: str) -> bool:
        digest = content_hash(text.strip())
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM queue WHERE account_id=? AND content_hash=?
                UNION ALL
                SELECT 1 FROM post_history WHERE account_id=? AND content_hash=? AND status='published'
                LIMIT 1
                """,
                (account_id, digest, account_id, digest),
            ).fetchone()
            return row is not None

    def post_exists_in_history(self, account_id: int, text: str) -> dict[str, Any] | None:
        digest = content_hash(text.strip())
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM post_history WHERE account_id=? AND content_hash=? AND status='published'",
                (account_id, digest),
            ).fetchone()
            return dict(row) if row else None

    def enqueue_many(self, items: Iterable[dict[str, str]]) -> tuple[int, int]:
        prepared = list(items)
        if not prepared:
            return 0, 0
        normalized_items = [
            (normalize_handle(item.get("account_handle")), (item.get("content") or "").strip()) for item in prepared
        ]
        normalized_items = [(handle, text) for handle, text in normalized_items if handle and text]
        if not normalized_items:
            return 0, 0

        now = utcnow_iso()
        with self._write() as connection:
            handles = sorted({handle for handle, _ in normalized_items})
            placeholders = ",".join("?" for _ in handles)
            rows = connection.execute(
                f"SELECT id,handle FROM accounts WHERE handle COLLATE NOCASE IN ({placeholders})", handles
            ).fetchall()
            account_ids = {str(row["handle"]).lower(): int(row["id"]) for row in rows}
            for handle in handles:
                if handle in account_ids:
                    continue
                cursor = connection.execute(
                    """INSERT INTO accounts(handle,display_name,created_at,updated_at)
                    VALUES(?,?,?,?)""",
                    (handle, handle, now, now),
                )
                account_ids[handle] = int(cursor.lastrowid)

            ids = sorted(account_ids.values())
            id_placeholders = ",".join("?" for _ in ids)
            existing_rows = connection.execute(
                f"""
                SELECT account_id,content_hash FROM queue WHERE account_id IN ({id_placeholders})
                UNION ALL
                SELECT account_id,content_hash FROM post_history WHERE account_id IN ({id_placeholders}) AND status='published'
                """,
                ids + ids,
            ).fetchall()
            seen = {(int(row["account_id"]), str(row["content_hash"])) for row in existing_rows}
            position_rows = connection.execute(
                f"""SELECT account_id,COALESCE(MAX(position),0) AS max_position FROM queue
                WHERE account_id IN ({id_placeholders}) GROUP BY account_id""",
                ids,
            ).fetchall()
            positions = {int(row["account_id"]): int(row["max_position"]) for row in position_rows}

            insert_rows: list[tuple[Any, ...]] = []
            duplicates = 0
            for handle, text in normalized_items:
                account_id = account_ids[handle]
                digest = content_hash(text)
                key = (account_id, digest)
                if key in seen:
                    duplicates += 1
                    continue
                seen.add(key)
                positions[account_id] = positions.get(account_id, 0) + 1
                insert_rows.append((account_id, text, digest, new_record_key(), positions[account_id], now))

            connection.executemany(
                """INSERT INTO queue(account_id,content,content_hash,record_key,position,created_at)
                VALUES(?,?,?,?,?,?)""",
                insert_rows,
            )
            active = connection.execute("SELECT value FROM settings WHERE key='active_account_id'").fetchone()
            if (not active or not active["value"]) and ids:
                connection.execute(
                    "INSERT INTO settings(key,value) VALUES('active_account_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(ids[0]),),
                )
            return len(insert_rows), duplicates

    def get_queue(self, account_id: int, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT q.*, a.handle AS account_handle FROM queue q
                JOIN accounts a ON a.id=q.account_id
                WHERE q.account_id=? ORDER BY q.position, q.id LIMIT ? OFFSET ?
                """,
                (account_id, max(1, limit), max(0, offset)),
            ).fetchall()
            return [dict(row) for row in rows]

    def queue_count(self, account_id: int) -> int:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM queue WHERE account_id=?", (account_id,)).fetchone()
            return int(row["count"])

    def next_queue_item(self, account_id: int) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM queue WHERE account_id=? AND state IN ('pending','uncertain')
                ORDER BY send_now DESC, CASE WHEN state='uncertain' THEN 0 WHEN scheduled_at<=? THEN 1 WHEN scheduled_at IS NULL THEN 2 ELSE 3 END,
                CASE WHEN scheduled_at IS NOT NULL THEN scheduled_at END, position,id LIMIT 1""",
                (account_id, utcnow_iso()),
            ).fetchone()
            return dict(row) if row else None

    def delete_queue_item(self, queue_id: int) -> bool:
        with self._write() as connection:
            self._editable(connection, [queue_id])
            cursor = connection.execute("DELETE FROM queue WHERE id=?", (queue_id,))
            return cursor.rowcount > 0

    def clear_queue(self, account_id: int) -> int:
        with self._write() as connection:
            self._editable(
                connection, [r[0] for r in connection.execute("SELECT id FROM queue WHERE account_id=?", (account_id,))]
            )
            cursor = connection.execute("DELETE FROM queue WHERE account_id=?", (account_id,))
            connection.execute(
                "UPDATE accounts SET next_scheduled_at=NULL,retry_count=0,last_error='' WHERE id=?",
                (account_id,),
            )
            return cursor.rowcount

    def move_queue_item(self, account_id: int, queue_id: int, direction: str) -> bool:
        with self._write() as connection:
            self._editable(connection, [queue_id])
            current = connection.execute(
                "SELECT id,position FROM queue WHERE account_id=? AND id=?", (account_id, queue_id)
            ).fetchone()
            if not current:
                return False
            position = int(current["position"])
            if direction == "up":
                other = connection.execute(
                    """SELECT id,position FROM queue WHERE account_id=? AND
                    (position<? OR (position=? AND id<?)) ORDER BY position DESC,id DESC LIMIT 1""",
                    (account_id, position, position, queue_id),
                ).fetchone()
            elif direction == "down":
                other = connection.execute(
                    """SELECT id,position FROM queue WHERE account_id=? AND
                    (position>? OR (position=? AND id>?)) ORDER BY position,id LIMIT 1""",
                    (account_id, position, position, queue_id),
                ).fetchone()
            elif direction in {"top", "bottom"}:
                aggregate = "MIN" if direction == "top" else "MAX"
                edge = connection.execute(
                    f"SELECT COALESCE({aggregate}(position),0) AS edge FROM queue WHERE account_id=?",
                    (account_id,),
                ).fetchone()["edge"]
                new_position = int(edge) - 1 if direction == "top" else int(edge) + 1
                connection.execute("UPDATE queue SET position=? WHERE id=?", (new_position, queue_id))
                return True
            else:
                return False
            if not other:
                return False
            self._editable(connection, [other["id"]])
            other_position = int(other["position"])
            if other_position == position:
                other_position = position - 1 if direction == "up" else position + 1
            connection.execute("UPDATE queue SET position=? WHERE id=?", (other_position, queue_id))
            connection.execute("UPDATE queue SET position=? WHERE id=?", (position, other["id"]))
            return True

    def mark_attempt_failed(self, queue_id: int, error: str) -> None:
        with self._write() as connection:
            connection.execute(
                """UPDATE queue SET attempt_count=attempt_count+1,last_attempt_at=?,last_error=? WHERE id=?""",
                (utcnow_iso(), error[:1000], queue_id),
            )

    def update_queue_record_key(self, queue_id: int, record_key: str) -> None:
        with self._write() as connection:
            connection.execute("UPDATE queue SET record_key=? WHERE id=?", (record_key, queue_id))

    def complete_queue_item(
        self,
        queue_id: int,
        uri: str,
        cid: str,
        status: str = "published",
        *,
        snapshot: dict[str, Any] | None = None,
        next_scheduled_at: str | None = None,
    ) -> bool:
        if status not in {"published", "skipped"}:
            raise ValueError("Unknown queue completion status")
        with self._write() as connection:
            stored = connection.execute("SELECT * FROM queue WHERE id=?", (queue_id,)).fetchone()
            if status == "skipped" and stored:
                self._editable(connection, [queue_id])
            row: sqlite3.Row | dict[str, Any] | None = snapshot or stored
            if not row:
                return False
            completed_at = utcnow_iso()
            rkey = row["record_key"]
            if uri:
                uri_key = uri.rstrip("/").split("/")[-1]
                if uri_key and uri_key != rkey:
                    rkey = uri_key
            values = (
                row["account_id"],
                row["content"],
                row["content_hash"],
                rkey,
                status,
                uri,
                cid,
                completed_at,
            )
            if status == "published":
                connection.execute(
                    """
                    INSERT INTO post_history(
                        account_id,content,content_hash,record_key,status,post_uri,post_cid,completed_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(account_id,content_hash) DO UPDATE SET
                        record_key=excluded.record_key,
                        status='published',
                        post_uri=excluded.post_uri,
                        post_cid=excluded.post_cid,
                        completed_at=excluded.completed_at
                    """,
                    values,
                )
                # The original row may have been deleted/re-added while the API call
                # was in flight. A confirmed publication must remove any such duplicate.
                connection.execute(
                    "DELETE FROM queue WHERE id=? OR (account_id=? AND content_hash=?)",
                    (queue_id, row["account_id"], row["content_hash"]),
                )
                if next_scheduled_at is not None:
                    connection.execute(
                        """
                        UPDATE accounts SET last_posted_at=?,next_scheduled_at=?,
                            retry_count=0,last_error='',updated_at=? WHERE id=?
                        """,
                        (completed_at, next_scheduled_at, completed_at, row["account_id"]),
                    )
            else:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO post_history(
                        account_id,content,content_hash,record_key,status,post_uri,post_cid,completed_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    values,
                )
                connection.execute("DELETE FROM queue WHERE id=?", (queue_id,))
            connection.execute(
                "UPDATE post_history SET media_json=?,last_error=? WHERE account_id=? AND content_hash=?",
                (
                    dict(row).get("media_json", "[]"),
                    dict(row).get("last_error", ""),
                    row["account_id"],
                    row["content_hash"],
                ),
            )
            return True

    def record_published_post(
        self,
        account_id: int,
        content: str,
        record_key: str,
        uri: str,
        cid: str,
    ) -> None:
        """Add a manual publication to the same deduplication history as queue posts."""
        normalized = content.strip()
        digest = content_hash(normalized)
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO post_history(
                    account_id,content,content_hash,record_key,status,post_uri,post_cid,completed_at
                ) VALUES(?,?,?,?, 'published',?,?,?)
                ON CONFLICT(account_id,content_hash) DO UPDATE SET
                    record_key=excluded.record_key,
                    status='published',
                    post_uri=excluded.post_uri,
                    post_cid=excluded.post_cid,
                    completed_at=excluded.completed_at
                """,
                (account_id, normalized, digest, record_key, uri, cid, utcnow_iso()),
            )
            connection.execute(
                "DELETE FROM queue WHERE account_id=? AND content_hash=?",
                (account_id, digest),
            )

    def reconcile_queue_with_published(
        self,
        account_id: int,
        published_posts: list[dict[str, Any]],
    ) -> int:
        """Reconcile queue items against already published posts from Bluesky or history.

        Removes matching queue items and records them in post_history.
        """
        reconciled_count = 0
        now = utcnow_iso()

        with self._write() as connection:
            queue_rows = connection.execute(
                "SELECT id, content, content_hash, record_key FROM queue WHERE account_id=? AND state='pending' AND attempt_count=0 AND media_json='[]'",
                (account_id,),
            ).fetchall()
            if not queue_rows:
                return 0

            queue_by_hash: dict[str, list[sqlite3.Row]] = {}
            queue_by_norm: dict[str, list[sqlite3.Row]] = {}
            for q in queue_rows:
                ch = str(q["content_hash"])
                queue_by_hash.setdefault(ch, []).append(q)
                nt = normalize_text(str(q["content"]))
                if nt:
                    queue_by_norm.setdefault(nt, []).append(q)

            matched_queue_ids: set[int] = set()
            for post in published_posts:
                text = str(post.get("text") or "").strip()
                if not text:
                    continue
                uri = str(post.get("uri") or "")
                cid = str(post.get("cid") or "")
                rkey = str(post.get("rkey") or "") or (uri.rstrip("/").split("/")[-1] if uri else "")
                created_at = str(post.get("created_at") or now)

                ch = content_hash(text)
                nt = normalize_text(text)

                matching = queue_by_hash.get(ch) or queue_by_norm.get(nt) or []
                for q in matching:
                    qid = int(q["id"])
                    if qid in matched_queue_ids:
                        continue
                    matched_queue_ids.add(qid)
                    final_rkey = rkey or str(q["record_key"])
                    connection.execute(
                        """
                        INSERT INTO post_history(
                            account_id,content,content_hash,record_key,status,post_uri,post_cid,completed_at
                        ) VALUES(?,?,?,?, 'published',?,?,?)
                        ON CONFLICT(account_id,content_hash) DO UPDATE SET
                            record_key=excluded.record_key,
                            status='published',
                            post_uri=excluded.post_uri,
                            post_cid=excluded.post_cid,
                            completed_at=excluded.completed_at
                        """,
                        (account_id, q["content"], q["content_hash"], final_rkey, uri, cid, created_at),
                    )
                    connection.execute("DELETE FROM queue WHERE id=?", (qid,))
                    reconciled_count += 1

            pruned_cursor = connection.execute(
                """
                DELETE FROM queue
                WHERE account_id=? AND content_hash IN (
                    SELECT content_hash FROM post_history WHERE account_id=? AND status='published'
                )
                """,
                (account_id, account_id),
            )
            reconciled_count += pruned_cursor.rowcount

        if reconciled_count > 0:
            self.record_activity(
                account_id,
                "reconcile",
                "success",
                message=f"Автоматически пропущено {reconciled_count} уже опубликованных постов",
            )
            get_logger().info(
                "[Account %s] Сверка с Bluesky: пропущено %d опубликованных постов из очереди",
                account_id,
                reconciled_count,
            )

        return reconciled_count

    def get_history(self, account_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            if account_id is None:
                rows = connection.execute(
                    """SELECT h.*,a.handle AS account_handle FROM post_history h JOIN accounts a ON a.id=h.account_id
                    ORDER BY h.completed_at DESC,h.id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT h.*,a.handle AS account_handle FROM post_history h JOIN accounts a ON a.id=h.account_id
                    WHERE h.account_id=? ORDER BY h.completed_at DESC,h.id DESC LIMIT ?""",
                    (account_id, limit),
                ).fetchall()
            return [dict(row) for row in rows]

    # Activity and automation history
    def record_activity(
        self,
        account_id: int | None,
        action_type: str,
        status: str,
        *,
        target_key: str = "",
        target_handle: str = "",
        message: str = "",
    ) -> None:
        with self._write() as connection:
            connection.execute(
                """INSERT INTO activity(account_id,action_type,target_key,target_handle,status,message,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (account_id, action_type, target_key, target_handle, status, message[:2000], utcnow_iso()),
            )

    def action_was_successful(self, account_id: int, action_type: str, target_key: str) -> bool:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT 1 FROM activity WHERE account_id=? AND action_type=? AND target_key=?
                AND status='success' LIMIT 1""",
                (account_id, action_type, target_key),
            ).fetchone()
            return row is not None

    def get_activity(self, limit: int = 300) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT x.*,a.handle AS account_handle FROM activity x LEFT JOIN accounts a ON a.id=x.account_id
                ORDER BY x.created_at DESC,x.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    # Import errors
    def add_import_error(
        self, file_name: str, account_handle: str, raw_content: str, reason: str, char_count: int
    ) -> None:
        with self._write() as connection:
            connection.execute(
                """INSERT INTO import_errors(file_name,account_handle,raw_content,error_reason,char_count,created_at)
                VALUES(?,?,?,?,?,?)""",
                (file_name, normalize_handle(account_handle), raw_content, reason, char_count, utcnow_iso()),
            )

    def get_import_errors(self, limit: int = 300) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM import_errors ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def clear_import_errors(self) -> None:
        with self._write() as connection:
            connection.execute("DELETE FROM import_errors")

    def stats(self) -> dict[str, int]:
        with self._lock, self._connection() as connection:
            return {
                "accounts": int(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]),
                "queued": int(connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0]),
                "published": int(
                    connection.execute("SELECT COUNT(*) FROM post_history WHERE status='published'").fetchone()[0]
                ),
                "activities": int(connection.execute("SELECT COUNT(*) FROM activity").fetchone()[0]),
            }

    @staticmethod
    def _editable(connection, ids: list[int]) -> None:
        for qid in ids:
            row = connection.execute("SELECT state,attempt_count,record_json FROM queue WHERE id=?", (qid,)).fetchone()
            if row and (row["state"] in {"sending", "uncertain"} or row["record_json"] or row["attempt_count"]):
                raise ValueError(
                    "Этот пост уже отправлялся. Сначала проверьте результат кнопкой «Повторить / проверить»."
                )

    def get_queue_item(self, queue_id: int) -> dict | None:
        with self._lock, self._connection() as c:
            r = c.execute("SELECT * FROM queue WHERE id=?", (queue_id,)).fetchone()
            return dict(r) if r else None

    def claim_queue_item(self, queue_id: int) -> dict | None:
        with self._write() as c:
            changed = c.execute(
                "UPDATE queue SET state='sending',attempt_count=attempt_count+1,last_attempt_at=? WHERE id=? AND state IN ('pending','uncertain')",
                (utcnow_iso(), queue_id),
            ).rowcount
            r = c.execute("SELECT * FROM queue WHERE id=?", (queue_id,)).fetchone() if changed else None
            return dict(r) if r else None

    def save_prepared_record(self, queue_id: int, record: dict) -> None:
        with self._write() as c:
            c.execute(
                "UPDATE queue SET record_json=? WHERE id=? AND state='sending'",
                (json.dumps(record, ensure_ascii=False), queue_id),
            )

    def fail_queue_item(self, queue_id: int, error: str, *, uncertain: bool, definitive: bool = False) -> None:
        with self._write() as c:
            c.execute(
                "UPDATE queue SET state=?,send_now=0,last_error=? WHERE id=?",
                ("uncertain" if uncertain else "failed", error[:1000], queue_id),
            )
            if definitive:
                c.execute("UPDATE queue SET record_json='',attempt_count=0 WHERE id=?", (queue_id,))

    def request_send(self, queue_id: int) -> None:
        with self._write() as c:
            c.execute(
                "UPDATE queue SET send_now=1,state=CASE WHEN state='failed' THEN 'uncertain' ELSE state END WHERE id=? AND state<>'sending'",
                (queue_id,),
            )

    def edit_queue_item(self, queue_id: int, text: str, media: list, scheduled_at: str | None) -> None:
        from app.media import payload_hash, validate_media

        error = post_validation_error(text)
        if error:
            raise ValueError(error)
        validate_media(media)
        parsed = parse_iso(scheduled_at) if scheduled_at else None
        if scheduled_at and not parsed:
            raise ValueError("Неверная дата")
        with self._write() as c:
            self._editable(c, [queue_id])
            row = c.execute("SELECT account_id FROM queue WHERE id=?", (queue_id,)).fetchone()
            if not row:
                raise ValueError("Пост уже удалён")
            digest = payload_hash(text, media)
            if c.execute(
                "SELECT 1 FROM post_history WHERE account_id=? AND content_hash=? AND status='published'",
                (row[0], digest),
            ).fetchone():
                raise ValueError("Пост уже опубликован")
            c.execute(
                "UPDATE queue SET content=?,content_hash=?,media_json=?,scheduled_at=?,record_key=?,state='pending',last_error='' WHERE id=?",
                (
                    text.strip(),
                    digest,
                    json.dumps(media, ensure_ascii=False),
                    parsed.isoformat() if parsed else None,
                    new_record_key(),
                    queue_id,
                ),
            )

    def bulk_delete(self, ids: list[int]) -> None:
        with self._write() as c:
            self._editable(c, ids)
            c.executemany("DELETE FROM queue WHERE id=?", [(qid,) for qid in ids])

    def restore_history(self, history_id: int) -> int | None:
        with self._lock, self._connection() as c:
            r = c.execute("SELECT * FROM post_history WHERE id=? AND status='skipped'", (history_id,)).fetchone()
        if not r:
            raise ValueError("Восстановить можно только пропущенный пост")
        return self.enqueue_one(r["account_id"], r["content"], media=json.loads(r["media_json"]))

    def save_draft(self, account_id: int, text: str, media: list, schedule_text: str = "") -> None:
        with self._write() as c:
            c.execute(
                "INSERT INTO drafts(account_id,content,media_json,updated_at,schedule_text) VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET content=excluded.content,media_json=excluded.media_json,updated_at=excluded.updated_at,schedule_text=excluded.schedule_text",
                (account_id, text, json.dumps(media, ensure_ascii=False), utcnow_iso(), schedule_text),
            )

    def get_draft(self, account_id: int) -> dict:
        with self._lock, self._connection() as c:
            r = c.execute("SELECT * FROM drafts WHERE account_id=?", (account_id,)).fetchone()
            return dict(r) if r else {"content": "", "media_json": "[]", "schedule_text": ""}

    def search_queue(self, account_id, query="", limit=100, offset=0):
        with self._lock, self._connection() as c:
            params = (account_id, query.casefold())
            total = c.execute(
                "SELECT COUNT(*) FROM queue WHERE account_id=? AND instr(casefold(content),?)>0", params
            ).fetchone()[0]
            rows = c.execute(
                "SELECT * FROM queue WHERE account_id=? AND instr(casefold(content),?)>0 ORDER BY position,id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows], total

    def failed_count(self, account_id):
        with self._lock, self._connection() as c:
            return c.execute(
                "SELECT COUNT(*) FROM queue WHERE account_id=? AND state='failed'", (account_id,)
            ).fetchone()[0]
