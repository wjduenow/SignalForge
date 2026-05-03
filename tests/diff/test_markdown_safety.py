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


# ----- HTML metacharacter escaping (post-QG fix). -----


def test_escapes_script_tag_in_table_cell() -> None:
    """A drafted description containing ``<script>`` MUST NOT render as
    raw HTML. GFM parses raw tags directly; entity-encoding ``<`` / ``>``
    is the defence."""
    out = escape_markdown_scalar("<script>alert(1)</script>", in_table_cell=True)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&lt;/script&gt;" in out


def test_escapes_script_tag_outside_table_cell() -> None:
    """Defence-in-depth: HTML escape applies in BOTH table-cell and
    non-table-cell modes. Only fenced code blocks bypass this escaper."""
    out = escape_markdown_scalar("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_escapes_closing_details_tag() -> None:
    """``</details>`` is the canonical breakout payload — an LLM-generated
    description that closes a wrapping ``<details>`` element on a PR
    comment would terminate the reviewer's collapsible block early."""
    out = escape_markdown_scalar("</details>", in_table_cell=True)
    assert "</details>" not in out
    assert "&lt;/details&gt;" in out


def test_ampersand_escaped_first_no_double_encoding() -> None:
    """``&`` is processed before ``<``/``>`` so the ``&`` characters
    inside our own ``&lt;``/``&gt;`` entities aren't double-encoded
    into ``&amp;lt;``/``&amp;gt;``."""
    out = escape_markdown_scalar("a & b")
    # Single &amp; for the input ampersand; no &amp;amp; double-encoding.
    assert out == "a &amp; b"
    assert "&amp;amp;" not in out


def test_existing_amp_entity_in_input_double_encoded_intentionally() -> None:
    """Input that already contains ``&amp;`` is intentionally re-encoded
    to ``&amp;amp;``. The escaper treats input as a literal string, not
    as pre-encoded HTML — that's the load-bearing property: the rendered
    output is *always* the textual representation of the input bytes,
    never the HTML-decoded view of them.
    """
    out = escape_markdown_scalar("&amp;")
    assert out == "&amp;amp;"


def test_escapes_img_tag_with_attributes() -> None:
    """Tag attributes (``src=x``, ``onerror=...``) must also be defused
    by the HTML escape — the ``<`` / ``>`` neutralise the tag boundary;
    everything between is rendered as literal text."""
    out = escape_markdown_scalar("<img src=x>", in_table_cell=True)
    assert "<img" not in out
    assert "&lt;img src=x&gt;" in out


def test_html_escape_runs_before_backslash_escape() -> None:
    """The pass order is ``&`` → ``<`` / ``>`` → backslash. A backslash
    in the input does not interfere with the HTML pass."""
    out = escape_markdown_scalar("<a\\>")
    # ``&`` first (none); ``<`` → ``&lt;``; ``>`` → ``&gt;``;
    # backslash → ``\\``.
    assert out == "&lt;a\\\\&gt;"
