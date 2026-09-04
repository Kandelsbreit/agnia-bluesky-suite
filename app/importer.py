from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.database import Database
from app.logging_setup import get_logger
from app.utils import content_hash, count_graphemes, normalize_handle, post_validation_error

BLOCK_SEPARATOR = re.compile(r"(?m)^\s*---+\s*$")
ACCOUNT_TAG = re.compile(r"^\ufeff?@?account\s*:\s*(.+?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedPost:
    account_handle: str
    content: str
    raw_block: str
    char_count: int
    digest: str
    error: str = ""

    @property
    def valid(self) -> bool:
        return not self.error


@dataclass
class ImportResult:
    files: int = 0
    parsed: int = 0
    added: int = 0
    duplicates: int = 0
    errors: int = 0
    cancelled: bool = False
    added_per_account: dict[str, int] = field(default_factory=dict)


def parse_content(content: str) -> list[ParsedPost]:
    result: list[ParsedPost] = []
    for raw in BLOCK_SEPARATOR.split(content):
        block = raw.strip()
        if not block:
            continue
        account = ""
        body: list[str] = []
        for line in block.splitlines():
            match = ACCOUNT_TAG.match(line.strip())
            if match and not account:
                account = normalize_handle(match.group(1))
            else:
                body.append(line)
        text = "\n".join(body).strip()
        length = count_graphemes(text)
        error = ""
        if not account:
            error = "Не найдена метка @account: handle"
        else:
            error = post_validation_error(text)
        result.append(ParsedPost(account, text, block, length, content_hash(text), error))
    return result


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def import_files(
    paths: Iterable[str | Path],
    db: Database,
    *,
    progress: Callable[[int, int, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> ImportResult:
    files = [Path(path) for path in paths]
    summary = ImportResult(files=len(files))
    valid_items: list[dict[str, str]] = []
    valid_counts: dict[str, int] = {}

    for index, path in enumerate(files, start=1):
        if cancel_event and cancel_event.is_set():
            summary.cancelled = True
            break
        if progress:
            progress(index - 1, len(files), f"Чтение {path.name}")
        try:
            parsed = parse_content(read_text_file(path))
        except OSError as exc:
            summary.errors += 1
            db.add_import_error(path.name, "", "", f"Ошибка чтения: {exc}", 0)
            continue
        summary.parsed += len(parsed)
        for item in parsed:
            if not item.valid:
                summary.errors += 1
                db.add_import_error(
                    path.name,
                    item.account_handle,
                    item.raw_block,
                    item.error,
                    item.char_count,
                )
                continue
            valid_items.append({"account_handle": item.account_handle, "content": item.content})
            valid_counts[item.account_handle] = valid_counts.get(item.account_handle, 0) + 1

    if valid_items and not summary.cancelled:
        if progress:
            progress(len(files), len(files), f"Сохранение {len(valid_items)} постов")
        added, duplicates = db.enqueue_many(valid_items)
        summary.added = added
        summary.duplicates = duplicates
        # Exact per-account inserted counts can differ because of deduplication. Querying
        # every post would defeat bulk import, so report accepted candidates per account.
        summary.added_per_account = valid_counts
    get_logger().info(
        "Импорт: файлов=%s, распознано=%s, добавлено=%s, дубликатов=%s, ошибок=%s",
        summary.files,
        summary.parsed,
        summary.added,
        summary.duplicates,
        summary.errors,
    )
    return summary
