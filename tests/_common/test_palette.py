"""Tests for ``signalforge._common.palette`` (issue #210).

The shared brand palette is consumed by both the diff renderer's verdict-tier
table (issue #209) and the CLI's ``generate`` progress lines (issue #210). These
tests pin the brand hex bytes + the ``COLORTERM`` truecolor detection so a
regression in either is caught at the source rather than in each consumer's
snapshot.
"""

from __future__ import annotations

import pytest

from signalforge._common import palette


def test_truecolor_sgr_format() -> None:
    """``truecolor_sgr`` emits the ISO 6429 24-bit foreground form."""
    assert palette.truecolor_sgr(0, 0, 0) == "\x1b[38;2;0;0;0m"
    assert palette.truecolor_sgr(255, 194, 77) == "\x1b[38;2;255;194;77m"


def test_brand_hex_values() -> None:
    """The brand constants carry the Design-System hex (tokens/colors.css)."""
    assert palette.SIGNAL == "\x1b[38;2;47;203;127m"  # #2FCB7F
    assert palette.STEEL == "\x1b[38;2;79;144;247m"  # #4F90F7
    assert palette.NOISE == "\x1b[38;2;251;90;96m"  # #FB5A60
    assert palette.FLAG == "\x1b[38;2;245;166;35m"  # #F5A623
    assert palette.SPARK == "\x1b[38;2;255;194;77m"  # #FFC24D
    assert palette.FORGE == "\x1b[38;2;255;106;61m"  # #FF6A3D


def test_control_sgr_values() -> None:
    """Hue-less controls match the canonical SGR codes."""
    assert palette.RESET == "\x1b[0m"
    assert palette.BOLD == "\x1b[1m"
    assert palette.DIM == "\x1b[2m"


def test_colorterm_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit override short-circuits the COLORTERM check."""
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert palette.colorterm_is_truecolor(override=False) is False
    monkeypatch.delenv("COLORTERM", raising=False)
    assert palette.colorterm_is_truecolor(override=True) is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("truecolor", True),
        ("24bit", True),
        ("TrueColor", True),  # case-insensitive
        ("24BIT", True),
        ("256color", False),
        ("", False),
        ("yes", False),
    ],
)
def test_colorterm_detection(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    """``COLORTERM`` truecolor/24bit (any case) advertises 24-bit; else off."""
    monkeypatch.setenv("COLORTERM", value)
    assert palette.colorterm_is_truecolor() is expected


def test_colorterm_unset_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset COLORTERM degrades to the 16-colour fallback."""
    monkeypatch.delenv("COLORTERM", raising=False)
    assert palette.colorterm_is_truecolor() is False
