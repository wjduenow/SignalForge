"""Brand glyph audit — the rendered surfaces carry NO emoji (issue #212).

The SignalForge brand voice is explicit (README §2 / §4): *No emoji.* Terminal
texture comes from a small set of glyphs (``◆ ✓ — · … → └─ ↳``) instead. This
gate scans the actual RENDERED output of the two human-facing surfaces — the
diff renderer's table/diff/footer and the CLI's ``generate`` progress + run
footer — and asserts no emoji codepoint appears.

Why an output scan (not a source-literal AST scan): the brand glyphs the
renderers DO emit (``◆`` U+25C6, ``✓`` U+2713, ``…`` U+2026, ``└─`` U+2514/2500,
``↳`` U+21B3) sit in BMP symbol blocks adjacent to emoji, and rendered output
also embeds arbitrary user content (model ids, descriptions) that may carry
accented Latin text — neither of which is emoji. Keying on the emoji codepoint
ranges (below) catches a smuggled 🐰/✨/🎯 while leaving every legitimate glyph
and accented character alone. Mirrors the rendered-output discipline the diff
snapshot fixtures use.
"""

from __future__ import annotations

import pytest

from signalforge._common.ansi_safety import strip_ansi_escapes
from signalforge.cli._helpers import (
    ProgressStyle,
    build_run_footer,
    emit_progress_done,
    emit_progress_entry,
)
from tests.diff._snapshot_inputs import CASES, render_for_case

# Emoji / pictographic codepoint ranges. Deliberately EXCLUDES the BMP symbol
# blocks the brand legitimately uses — ✓ (U+2713), → (U+2192), ◆ (U+25C6),
# — (U+2014), … (U+2026), └ ─ (U+2514/2500), ↳ (U+21B3) — and ordinary
# accented Latin text (all < U+2600 / not pictographic).
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),  # all Supplementary-Plane emoji / pictograph blocks
    (0x2600, 0x26FF),  # Miscellaneous Symbols (☀ ⚠ ⭐ ☂ …)
    (0x2B00, 0x2BFF),  # Misc Symbols & Arrows (⭐ ⬛ ⬜ …)
)
# Individual emoji-presentation codepoints that live OUTSIDE the ranges above
# (mostly the Dingbats block, where ✓ is allowed but ✨/✅/❌/❤ are not).
_EMOJI_SINGLES: frozenset[int] = frozenset(
    {0xFE0F, 0x2728, 0x2705, 0x274C, 0x2764, 0x2049, 0x203C, 0x2122}
)


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return cp in _EMOJI_SINGLES or any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def _emoji_chars(text: str) -> list[str]:
    """Return the distinct emoji codepoints found in ``text`` (sorted)."""
    return sorted({ch for ch in text if _is_emoji(ch)})


# ---------------------------------------------------------------------------
# Self-check (planted violation) — the predicate must actually catch emoji
# AND must NOT false-positive on the brand's legitimate glyphs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("emoji", ["🐰", "✨", "🎯", "🚥", "✅", "❌", "⚠️", "❤️"])
def test_predicate_flags_emoji(emoji: str) -> None:
    assert _emoji_chars(emoji), f"{emoji!r} should be detected as emoji"


@pytest.mark.parametrize("glyph", ["◆", "✓", "—", "·", "…", "→", "└", "─", "↳"])
def test_predicate_allows_brand_glyphs(glyph: str) -> None:
    assert not _emoji_chars(glyph), f"{glyph!r} is a brand glyph, not emoji"


@pytest.mark.parametrize("accented", ["café", "Ünïcödë", "naïve", "Zürich"])
def test_predicate_allows_accented_text(accented: str) -> None:
    assert not _emoji_chars(accented), f"{accented!r} is plain text, not emoji"


# ---------------------------------------------------------------------------
# Diff renderer — every snapshot case (ansi / markdown / json surfaces).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_name", list(CASES.keys()))
def test_diff_renderer_output_has_no_emoji(case_name: str) -> None:
    """No rendered diff surface emits an emoji codepoint."""
    rendered = render_for_case(case_name)
    found = _emoji_chars(rendered)
    assert not found, f"diff case {case_name!r} emitted emoji {found}"


# ---------------------------------------------------------------------------
# CLI progress + run footer (the colour path carries the ◆ / ✓ glyphs).
# ---------------------------------------------------------------------------

_COLOR24 = ProgressStyle(color=True, truecolor=True)


def test_cli_progress_output_has_no_emoji(capsys: pytest.CaptureFixture[str]) -> None:
    emit_progress_entry(2, "draft", "calling LLM (model claude-sonnet-4-6)...", style=_COLOR24)
    emit_progress_done(3, "prune", 0.3, fact="8 kept · 2 dropped", style=_COLOR24)
    err = capsys.readouterr().err
    assert "◆" in err and "✓" not in err  # progress uses ◆, not ✓
    assert not _emoji_chars(err), f"progress emitted emoji {_emoji_chars(err)}"


def test_cli_footer_output_has_no_emoji() -> None:
    footer = build_run_footer(
        elapsed_seconds=312.0,
        written=["schema.yml (8 kept)", ".signalforge/diff.json", ".signalforge/grade.json"],
        dry_run=False,
        cost_clause="$0.13 Anthropic · <$0.01 OpenAI",
        style=_COLOR24,
    )
    assert "✓" in footer  # the footer's success glyph
    assert not _emoji_chars(footer), f"footer emitted emoji {_emoji_chars(footer)}"
    # also the plain (colour-off) form
    plain = strip_ansi_escapes(footer)
    assert not _emoji_chars(plain)
