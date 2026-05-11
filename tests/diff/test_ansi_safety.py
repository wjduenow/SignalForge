"""Tests for signalforge.diff._ansi_safety (DEC-007 of issue #8)."""

from __future__ import annotations

from signalforge.diff._ansi_safety import strip_ansi_escapes


def test_empty_string_returns_empty() -> None:
    assert strip_ansi_escapes("") == ""


def test_no_escape_passthrough() -> None:
    plain = "customer_id is the primary key"
    assert strip_ansi_escapes(plain) == plain


def test_strips_simple_color_code() -> None:
    assert strip_ansi_escapes("\x1b[31mERROR\x1b[0m") == "ERROR"


def test_strips_compound_sgr_sequence() -> None:
    # Bold + red + underline.
    assert strip_ansi_escapes("\x1b[1;31;4mhello\x1b[0m world") == "hello world"


def test_strips_reset_only() -> None:
    assert strip_ansi_escapes("\x1b[0m") == ""


def test_strips_bare_csi_with_no_params() -> None:
    # ``\x1b[m`` is a valid SGR reset shorthand (no params).
    assert strip_ansi_escapes("a\x1b[mb") == "ab"


def test_strips_cursor_movement() -> None:
    # ``\x1b[2J`` clears screen; ``\x1b[H`` homes cursor.
    assert strip_ansi_escapes("\x1b[2J\x1b[Hclean") == "clean"


def test_strips_multiple_sequences_in_text() -> None:
    text = "\x1b[31mred\x1b[0m and \x1b[32mgreen\x1b[0m"
    assert strip_ansi_escapes(text) == "red and green"


def test_idempotent_on_already_stripped_text() -> None:
    plain = "no escapes here"
    once = strip_ansi_escapes(plain)
    twice = strip_ansi_escapes(once)
    assert once == twice == plain


def test_strips_tilde_terminated_key_sequence() -> None:
    # ``\x1b[3~`` is the Delete-key sequence; the broadened CSI regex
    # (final byte in ``@-~``, not just ``a-zA-Z``) covers tilde
    # terminators.
    assert strip_ansi_escapes("a\x1b[3~b") == "ab"


def test_strips_bracketed_paste_markers() -> None:
    # Bracketed-paste mode delimits pasted text with ``\x1b[200~``
    # (start) and ``\x1b[201~`` (end). Both must be stripped — a
    # smuggled ``[200~`` from upstream content could otherwise put a
    # downstream terminal into bracketed-paste mode.
    text = "\x1b[200~pasted\x1b[201~"
    assert strip_ansi_escapes(text) == "pasted"


def test_strips_csi_with_intermediate_byte() -> None:
    # Intermediate bytes (``0x20-0x2F``) are valid in the CSI grammar
    # — e.g. ``\x1b[1 q`` (cursor-shape DECSCUSR with a space
    # intermediate). Confirm the broadened regex covers it.
    assert strip_ansi_escapes("a\x1b[1 qb") == "ab"


def test_does_not_strip_lone_escape_byte() -> None:
    # A bare ESC without ``[`` is not a CSI sequence and is left alone.
    # The terminal will still render it weirdly — that's a separate
    # concern from the CSI defence.
    assert strip_ansi_escapes("\x1bX") == "\x1bX"


def test_does_not_strip_non_csi_escape() -> None:
    # OSC sequences (``\x1b]...``) are out of scope for v0.1.
    out = strip_ansi_escapes("\x1b]0;title\x07")
    assert "\x1b]" in out  # not stripped
