"""Unit tests for the ◆ glyphed progress emitters (issue #210).

Covers :func:`resolve_progress_style` (the colour decision), and the
plain-vs-colour surfaces of :func:`emit_progress_entry`,
:func:`emit_progress_done`, and :func:`emit_batch_progress_entry`. The
integration coverage lives in ``test_generate.py`` / ``test_batch_emission.py``;
this module pins the helpers in isolation.
"""

from __future__ import annotations

import pytest

from signalforge._common.ansi_safety import strip_ansi_escapes
from signalforge.cli._helpers import (
    ProgressStyle,
    emit_batch_progress_entry,
    emit_progress_done,
    emit_progress_entry,
    resolve_progress_style,
)


@pytest.fixture(autouse=True)
def _clean_color_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)


# ---------------------------------------------------------------------------
# resolve_progress_style — colour-precedence (mirrors the diff renderer).
# ---------------------------------------------------------------------------


def test_force_color_forces_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    assert resolve_progress_style(verbose=False).color is True


def test_force_color_beats_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    assert resolve_progress_style(verbose=False).color is True


def test_no_color_forces_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    assert resolve_progress_style(verbose=False).color is False


def test_isatty_decides_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    assert resolve_progress_style(verbose=False).color is True
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    assert resolve_progress_style(verbose=False).color is False


def test_verbose_does_not_force_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--verbose`` forces progress on but NOT colour (piped --verbose stays
    plain). Colour still rests on the env/TTY signals."""
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    assert resolve_progress_style(verbose=True).color is False


def test_truecolor_only_when_color_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("NO_COLOR", "1")  # colour off
    style = resolve_progress_style(verbose=False)
    assert style.color is False
    assert style.truecolor is False  # never truecolor when colour is off


def test_truecolor_detected_when_color_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLORTERM", "truecolor")
    style = resolve_progress_style(verbose=False)
    assert style.color is True
    assert style.truecolor is True


# ---------------------------------------------------------------------------
# emit_progress_* — plain path is byte-identical to pre-#210.
# ---------------------------------------------------------------------------

_PLAIN = ProgressStyle(color=False, truecolor=False)
_COLOR16 = ProgressStyle(color=True, truecolor=False)
_COLOR24 = ProgressStyle(color=True, truecolor=True)


def test_entry_plain_path_byte_identical(capsys: pytest.CaptureFixture[str]) -> None:
    emit_progress_entry(1, "safety", "building LLM request...", total=5, style=_PLAIN)
    assert capsys.readouterr().err == "[1/5] safety: building LLM request...\n"


def test_entry_plain_path_when_style_none(capsys: pytest.CaptureFixture[str]) -> None:
    emit_progress_entry(2, "draft", "calling LLM...", total=4)
    assert capsys.readouterr().err == "[2/4] draft: calling LLM...\n"


def test_done_plain_path_byte_identical(capsys: pytest.CaptureFixture[str]) -> None:
    emit_progress_done(3, "prune", 0.3, total=5, fact="ignored when plain", style=_PLAIN)
    assert capsys.readouterr().err == "[3/5] prune: done in 0.3s\n"


def test_batch_plain_path_byte_identical(capsys: pytest.CaptureFixture[str]) -> None:
    emit_batch_progress_entry("model.x.a", 1, 3, style=_PLAIN)
    assert capsys.readouterr().err == "[1/3] model.x.a\n"


# ---------------------------------------------------------------------------
# emit_progress_* — colour path: ◆ glyph, no colon, right-aligned fact.
# ---------------------------------------------------------------------------


def test_entry_color_path_has_glyph_and_no_colon(capsys: pytest.CaptureFixture[str]) -> None:
    emit_progress_entry(1, "safety", "building LLM request...", total=5, style=_COLOR16)
    err = capsys.readouterr().err
    assert "◆" in err
    plain = strip_ansi_escapes(err)
    assert plain.startswith("◆ [1/5] safety")  # padded, no colon
    assert ":" not in plain.split("safety")[0]  # no colon before the stage body


def test_done_color_path_right_aligns_fact(capsys: pytest.CaptureFixture[str]) -> None:
    emit_progress_done(2, "draft", 1.2, total=5, fact="claude-sonnet-4-6", style=_COLOR16)
    plain = strip_ansi_escapes(capsys.readouterr().err).rstrip("\n")
    assert "done in 1.2s" in plain
    assert plain.endswith("claude-sonnet-4-6")  # fact pushed to the right edge
    # at least the two-space minimum gap between timing and fact
    assert "  claude-sonnet-4-6" in plain


def test_done_color_path_no_fact_omits_padding(capsys: pytest.CaptureFixture[str]) -> None:
    emit_progress_done(1, "safety", 0.1, total=5, fact="", style=_COLOR16)
    plain = strip_ansi_escapes(capsys.readouterr().err).rstrip("\n")
    assert plain == "◆ [1/5] safety  done in 0.1s"


def test_glyph_uses_spark_truecolor_when_truecolor(capsys: pytest.CaptureFixture[str]) -> None:
    emit_progress_entry(1, "safety", "x", total=5, style=_COLOR24)
    assert "\x1b[38;2;255;194;77m◆\x1b[0m" in capsys.readouterr().err


def test_glyph_uses_yellow_fallback_when_not_truecolor(capsys: pytest.CaptureFixture[str]) -> None:
    emit_progress_entry(1, "safety", "x", total=5, style=_COLOR16)
    assert "\x1b[33m◆\x1b[0m" in capsys.readouterr().err


def test_batch_color_path_has_glyph(capsys: pytest.CaptureFixture[str]) -> None:
    emit_batch_progress_entry("model.x.a", 1, 3, style=_COLOR16)
    plain = strip_ansi_escapes(capsys.readouterr().err).rstrip("\n")
    assert plain == "◆ [1/3] model.x.a"


def test_color_path_strips_user_content_escapes(capsys: pytest.CaptureFixture[str]) -> None:
    """A model id smuggling SGR is stripped before the trusted glyph SGR is
    added — the ``allow_sgr=True`` sink does not re-introduce a strip, so the
    emitter must pre-strip user content itself."""
    emit_batch_progress_entry("model.\x1b[31mevil\x1b[0m.a", 1, 2, style=_COLOR16)
    err = capsys.readouterr().err
    assert "\x1b[31mevil" not in err  # smuggled red stripped
    assert "evil.a" in strip_ansi_escapes(err)  # literal text survives
