from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import UTC, datetime

try:
    import regex as _regex
except ImportError:  # pragma: no cover - dependency is present in release builds
    _regex = None


PROFILE_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?bsky\.app/profile/([^/?#]+)", re.IGNORECASE)


def normalize_handle(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    match = PROFILE_URL_RE.search(text)
    if match:
        text = match.group(1)
    return text.lstrip("@").strip().lower()


def normalize_text(text: str | None) -> str:
    value = unicodedata.normalize("NFC", text or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return value


def content_hash(text: str | None) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def count_graphemes(text: str | None) -> int:
    value = unicodedata.normalize("NFC", text or "")
    if _regex is not None:
        return len(_regex.findall(r"\X", value))
    return len(value)


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def new_record_key() -> str:
    """Stable AT Protocol record key stored before a publication attempt."""
    return uuid.uuid4().hex


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", value).strip(" ._")
    return cleaned or "bluesky_account"

