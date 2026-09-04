from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from app.bluesky import BlueskyError, BlueskyGateway
from app.utils import normalize_text, safe_filename


@dataclass(frozen=True)
class ExportOptions:
    replies: str = "exclude"  # exclude, include, separate
    self_threads_as_posts: bool = False
    deduplicate: bool = True
    oldest_first: bool = True
    date_from: date | None = None
    date_to: date | None = None
    queue_format: bool = True
    ai_export: bool = False


@dataclass
class ExportStats:
    fetched: int = 0
    posts: int = 0
    replies: int = 0
    reposts_skipped: int = 0
    replies_skipped: int = 0
    duplicates_skipped: int = 0
    date_skipped: int = 0
    empty_skipped: int = 0


@dataclass
class ExportResult:
    handle: str
    files: list[Path] = field(default_factory=list)
    posts: list[tuple[str, str]] = field(default_factory=list)  # ISO date, text
    stats: ExportStats = field(default_factory=ExportStats)
    cancelled: bool = False


def _is_repost(item: dict, did: str, handle: str) -> bool:
    if item.get("reason") is not None:
        return True
    author = (item.get("post") or {}).get("author") or {}
    author_did = str(author.get("did") or "")
    author_handle = str(author.get("handle") or "").lower()
    return bool(
        (did and author_did and did != author_did)
        or (handle and author_handle and handle.lower() != author_handle)
    )


def _is_reply(item: dict, did: str, allow_self_thread: bool) -> bool:
    post = item.get("post") or {}
    record = post.get("record") or {}
    if not record.get("reply") and not item.get("reply"):
        return False
    if allow_self_thread:
        reply = item.get("reply") or {}
        parent_author = ((reply.get("parent") or {}).get("author") or {}).get("did")
        if parent_author and parent_author == did:
            return False
    return True


def format_block(handle: str, text: str, queue_format: bool) -> str:
    return f"@account: {handle}\n\n{text}" if queue_format else text


def _write_blocks(path: Path, blocks: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n\n---\n\n".join(blocks)
    path.write_text(content + ("\n" if content else ""), encoding="utf-8", newline="\n")


def export_account(
    gateway: BlueskyGateway,
    actor: str,
    output_dir: Path,
    options: ExportOptions,
    *,
    cancel_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
    progress: Callable[[ExportStats, int, int], None] | None = None,
) -> ExportResult:
    notify = log or (lambda _message: None)
    profile = gateway.resolve_profile(actor)
    result = ExportResult(profile.handle)
    stats = result.stats
    notify(f"@{profile.handle}: найдено записей в профиле — {profile.posts_count}")

    posts: list[tuple[str, str]] = []
    replies: list[tuple[str, str]] = []
    seen_uris: set[str] = set()
    seen_posts: set[str] = set()
    seen_replies: set[str] = set()
    cursor: str | None = None
    page = 0

    while True:
        if cancel_event and cancel_event.is_set():
            result.cancelled = True
            break
        page += 1
        attempts = 0
        while True:
            try:
                feed, next_cursor = gateway.fetch_author_feed(profile.did, limit=100, cursor=cursor)
                break
            except BlueskyError as exc:
                attempts += 1
                if attempts >= 4 or not exc.retryable:
                    raise
                delay = exc.retry_after or min(30, attempts * 3)
                notify(f"@{profile.handle}: ошибка API, повтор через {delay} сек.: {exc}")
                if cancel_event and cancel_event.wait(delay):
                    result.cancelled = True
                    return result
                time.sleep(0 if cancel_event else delay)

        if not feed:
            break
        page_has_not_too_old = options.date_from is None
        for item in feed:
            if cancel_event and cancel_event.is_set():
                result.cancelled = True
                break
            stats.fetched += 1
            post = item.get("post") or {}
            uri = str(post.get("uri") or "")
            if uri and uri in seen_uris:
                continue
            if uri:
                seen_uris.add(uri)
            if _is_repost(item, profile.did, profile.handle):
                stats.reposts_skipped += 1
                continue
            record = post.get("record") or {}
            text = str(record.get("text") or "").strip()
            if not text:
                stats.empty_skipped += 1
                continue
            created_at = str(record.get("createdAt") or record.get("created_at") or post.get("indexedAt") or "")
            post_date = None
            try:
                post_date = datetime.fromisoformat(created_at.replace("Z", "+00:00")).date()
            except (TypeError, ValueError):
                pass
            if post_date:
                if options.date_to and post_date > options.date_to:
                    stats.date_skipped += 1
                    continue
                if options.date_from and post_date < options.date_from:
                    stats.date_skipped += 1
                    continue
                page_has_not_too_old = True

            reply = _is_reply(item, profile.did, options.self_threads_as_posts)
            normalized = " ".join(normalize_text(text).split())
            if reply and not options.ai_export:
                if options.replies == "exclude":
                    stats.replies_skipped += 1
                    continue
                if options.replies == "separate":
                    if options.deduplicate and normalized in seen_replies:
                        stats.duplicates_skipped += 1
                        continue
                    seen_replies.add(normalized)
                    replies.append((created_at, text))
                    stats.replies += 1
                    continue
            elif reply and options.ai_export:
                stats.replies_skipped += 1
                continue

            if options.deduplicate and normalized in seen_posts:
                stats.duplicates_skipped += 1
                continue
            seen_posts.add(normalized)
            posts.append((created_at, text))
            stats.posts += 1

        if result.cancelled:
            break
        if progress:
            progress(stats, page, profile.posts_count)
        if options.date_from and not page_has_not_too_old:
            break
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        if cancel_event and cancel_event.wait(0.08):
            result.cancelled = True
            break
        if not cancel_event:
            time.sleep(0.08)

    posts.sort(key=lambda item: item[0], reverse=not options.oldest_first)
    replies.sort(key=lambda item: item[0], reverse=not options.oldest_first)
    safe = safe_filename(profile.handle)
    queue_format = False if options.ai_export else options.queue_format
    posts_name = f"original_posts_{safe}.txt" if options.ai_export else f"{safe}_posts.txt"
    posts_path = output_dir / posts_name
    _write_blocks(posts_path, [format_block(profile.handle, text, queue_format) for _, text in posts])
    result.files.append(posts_path)
    result.posts = posts

    if options.replies == "separate" and replies and not options.ai_export:
        replies_path = output_dir / f"{safe}_replies.txt"
        _write_blocks(replies_path, [format_block(profile.handle, text, queue_format) for _, text in replies])
        result.files.append(replies_path)
    notify(
        f"@{profile.handle}: сохранено постов {stats.posts}, ответов {stats.replies}; "
        f"репостов исключено {stats.reposts_skipped}, дублей {stats.duplicates_skipped}"
    )
    return result


def write_combined_queue(
    path: Path,
    results: list[ExportResult],
    queue_format: bool = True,
    oldest_first: bool = True,
) -> Path:
    combined: list[tuple[str, str, str]] = []
    for result in results:
        combined.extend((created, result.handle, text) for created, text in result.posts)
    combined.sort(key=lambda item: item[0], reverse=not oldest_first)
    _write_blocks(path, [format_block(handle, text, queue_format) for _, handle, text in combined])
    return path
