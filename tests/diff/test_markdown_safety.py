"""Tests for signalforge.diff._markdown_safety (DEC-008 of issue #8)."""

from __future__ import annotations

from signalforge.diff._markdown_safety import escape_markdown_scalar


def test_empty_string_returns_empty() -> None:
    assert escape_markdown_scalar("") == ""
    assert escape_markdown_scalar("", in_table_cell=True) == ""


def test_plain_text_passthrough() -> None:
    plain = "customer_id is the primary key"
    assert escape_markdown_scalar(plain) == plain
    assert escape_markdown_scalar(plain, in_table_cell=True) == plain


def test_escapes_backtick() -> None:
    assert escape_markdown_scalar("`code`") == "\\`code\\`"


def test_escapes_pipe_outside_table_with_backslash() -> None:
    assert escape_markdown_scalar("a|b") == "a\\|b"


def test_escapes_backslash() -> None:
    # One backslash in -> two out.
    assert escape_markdown_scalar("a\\b") == "a\\\\b"


def test_backslash_processed_before_other_escapes() -> None:
    # A trailing `\` must not "eat" the following escape's leading
    # backslash. Input ``\``+``\``+``\` `` would, if backtick were
    # escaped first, leave a stray backslash. Confirm backslash-first
    # gives the right shape.
    out = escape_markdown_scalar("\\`")
    # Expect: literal backslash escaped to ``\\`` then literal
    # backtick escaped to ``\``+`` ` `` -> ``\\\``+`` ` ``
    assert out == "\\\\\\`"


def test_table_cell_html_entity_encodes_pipe() -> None:
    out = escape_markdown_scalar("a|b", in_table_cell=True)
    assert out == "a&#124;b"
    # In table-cell mode, pipe is NOT backslash-escaped — entity only.
    assert "\\|" not in out


def test_table_cell_still_escapes_backtick_and_backslash() -> None:
    out = escape_markdown_scalar("a`b\\c", in_table_cell=True)
    assert out == "a\\`b\\\\c"


def test_table_cell_encodes_row_breaking_control_chars() -> None:
    out = escape_markdown_scalar("line1\nline2\tcol\r", in_table_cell=True)
    assert "\n" not in out
    assert "\r" not in out
    assert "\t" not in out
    assert "&#10;" in out
    assert "&#13;" in out
    assert "&#9;" in out


def test_non_table_mode_leaves_newlines_alone() -> None:
    # Outside a table cell, newlines are part of normal Markdown flow
    # and must not be entity-encoded.
    out = escape_markdown_scalar("line1\nline2")
    assert out == "line1\nline2"


def test_idempotent_on_plain_text() -> None:
    plain = "no special chars"
    once = escape_markdown_scalar(plain)
    twice = escape_markdown_scalar(once)
    assert once == twice == plain


def test_combined_special_chars_outside_table() -> None:
    # `|` and ` ` and `\` together — each escape lands once.
    out = escape_markdown_scalar("a|b`c\\d")
    assert out == "a\\|b\\`c\\\\d"


def test_combined_special_chars_in_table_cell() -> None:
    out = escape_markdown_scalar("a|b`c\\d", in_table_cell=True)
    assert out == "a&#124;b\\`c\\\\d"
