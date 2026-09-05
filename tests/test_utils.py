from __future__ import annotations

from datetime import UTC

from app.utils import (
    content_hash,
    count_graphemes,
    format_duration,
    is_valid_tid,
    new_record_key,
    normalize_handle,
    normalize_text,
    parse_iso,
    safe_filename,
)


def test_normalize_handle_accepts_handle_at_and_profile_url():
    assert normalize_handle(" @Name.BSky.Social ") == "name.bsky.social"
    assert normalize_handle("https://bsky.app/profile/Name.Example/post/abc") == "name.example"


def test_normalize_text_and_hash_are_line_ending_stable():
    assert normalize_text("  one\r\ntwo  ") == "one\ntwo"
    assert content_hash("one\r\ntwo") == content_hash(" one\ntwo ")


def test_grapheme_counter_treats_combining_character_as_one():
    assert count_graphemes("e\u0301") == 1
    import app.utils as au

    if au._regex is not None:
        assert count_graphemes("👩‍💻") == 1


def test_parse_iso_normalizes_to_utc_and_rejects_bad_value():
    parsed = parse_iso("2026-01-02T03:04:05+03:00")
    assert parsed and parsed.tzinfo == UTC and parsed.hour == 0
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_record_keys_are_unique_and_safe():
    first, second = new_record_key(), new_record_key()
    assert first != second
    assert len(first) == 13
    assert len(second) == 13
    assert is_valid_tid(first)
    assert is_valid_tid(second)
    assert not is_valid_tid("08c529508337455f930c3af8219561dd")
    assert not is_valid_tid("short")
    assert not is_valid_tid(None)


def test_duration_and_filename_formatting():
    assert format_duration(65) == "01:05"
    assert format_duration(3661) == "01:01:01"
    assert safe_filename(" bad:/name*? ") == "bad__name"
