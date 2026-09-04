from __future__ import annotations

import hashlib
import random
import re
import threading
import time
import unicodedata
from datetime import UTC, datetime

try:
    import regex as _regex
except ImportError:  # pragma: no cover - dependency is present in release builds
    _regex = None


PROFILE_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?bsky\.app/profile/([^/?#]+)", re.IGNORECASE)
MAX_POST_GRAPHEMES = 300
MAX_POST_BYTES = 3000


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


def post_validation_error(text: str | None) -> str:
    value = normalize_text(text)
    if not value:
        return "Пустой текст поста"
    graphemes = count_graphemes(value)
    if graphemes > MAX_POST_GRAPHEMES:
        return f"Превышен лимит Bluesky: {graphemes}/{MAX_POST_GRAPHEMES} графем"
    byte_count = len(value.encode("utf-8"))
    if byte_count > MAX_POST_BYTES:
        return f"Превышен лимит Bluesky: {byte_count}/{MAX_POST_BYTES} байт UTF-8"
    return ""


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


TID_ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"
_TID_SET = set(TID_ALPHABET)


def is_valid_tid(key: str | None) -> bool:
    """Check if a string conforms to the 13-character AT Protocol TID format."""
    if not key or len(key) != 13:
        return False
    return all(c in _TID_SET for c in key)


_tid_lock = threading.Lock()
_last_micros = 0
_clock_id = 0


def new_record_key() -> str:
    """Canonical 13-character AT Protocol TID record key with monotonic clock."""
    global _last_micros, _clock_id
    with _tid_lock:
        now_micros = int(time.time() * 1_000_000) & 0x1FFFFFFFFFFFFF
        if now_micros <= _last_micros:
            _clock_id = (_clock_id + 1) & 0x3FF
            if _clock_id == 0:
                _last_micros = (_last_micros + 1) & 0x1FFFFFFFFFFFFF
            micros = _last_micros
        else:
            _last_micros = now_micros
            _clock_id = random.randint(0, 1023) & 0x3FF
            micros = now_micros
        clock = _clock_id

    val = (micros << 10) | clock
    chars = [TID_ALPHABET[(val >> (5 * (12 - i))) & 0x1F] for i in range(13)]
    return "".join(chars)



def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", value).strip(" ._")
    return cleaned or "bluesky_account"
