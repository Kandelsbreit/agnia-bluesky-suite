from __future__ import annotations

from app.importer import import_files, parse_content, read_text_file


def test_parser_routes_blocks_and_accepts_at_prefix():
    parsed = parse_content("@account: @ONE.BSKY.SOCIAL\n\nHello\nworld\n\n---\n\naccount: two.test\n\nSecond")
    assert [(item.account_handle, item.content) for item in parsed] == [
        ("one.bsky.social", "Hello\nworld"),
        ("two.test", "Second"),
    ]


def test_parser_reports_missing_tag_empty_and_too_long():
    parsed = parse_content("No tag\n\n---\n\n@account: empty.test\n\n---\n\n@account: long.test\n\n" + "x" * 301)
    assert len(parsed) == 3
    assert "метка" in parsed[0].error
    assert "Пустой" in parsed[1].error
    assert "301/300" in parsed[2].error


def test_parser_uses_grapheme_limit_not_code_points():
    parsed = parse_content("@account: emoji.test\n\n" + "😀" * 300)
    assert parsed[0].valid and parsed[0].char_count == 300


def test_parser_enforces_atproto_utf8_byte_limit():
    parsed = parse_content("@account: emoji.test\n\n" + "👩‍💻" * 300)
    assert not parsed[0].valid
    assert "3300/3000 байт" in parsed[0].error


def test_read_text_file_supports_utf8_bom_and_cp1251(tmp_path):
    utf8 = tmp_path / "utf8.txt"
    utf8.write_bytes("Привет".encode("utf-8-sig"))
    cp = tmp_path / "cp.txt"
    cp.write_bytes("Привет".encode("cp1251"))
    assert read_text_file(utf8) == "Привет"
    assert read_text_file(cp) == "Привет"


def test_import_saves_valid_posts_and_detailed_errors(db, tmp_path):
    path = tmp_path / "batch.txt"
    path.write_text(
        "@account: one.test\n\nGood\n\n---\n\nMissing account\n\n---\n\n@account: one.test\n\nGood",
        encoding="utf-8",
    )
    result = import_files([path], db)
    assert result.parsed == 3
    assert result.added == 1
    assert result.duplicates == 1
    assert result.errors == 1
    assert db.queue_count(db.get_account("one.test")["id"]) == 1
    assert "метка" in db.get_import_errors()[0]["error_reason"]


def test_import_can_be_cancelled_before_read(db, tmp_path):
    import threading

    path = tmp_path / "batch.txt"
    path.write_text("@account: one.test\n\nGood", encoding="utf-8")
    event = threading.Event()
    event.set()
    result = import_files([path], db, cancel_event=event)
    assert result.cancelled
    assert result.added == 0
