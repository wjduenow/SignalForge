"""Shared brand colour palette + truecolor primitives (issue #210).

The SignalForge Design System assigns the product a small, fixed palette
(``tokens/colors.css``). Two surfaces render in it: the diff renderer's
verdict-tier table/header (:mod:`signalforge.diff._renderers`, issue #209) and
the CLI's ``signalforge generate`` progress lines (:mod:`signalforge.cli`,
issue #210). This module is the **single home** for the brand hex values, the
24-bit SGR helper, and the ``COLORTERM`` truecolor-capability check so the two
consumers stay byte-consistent and neither duplicates the primitives.

Scope is deliberately narrow: low-level SGR strings + the capability check. The
*decision* of whether colour is on at all (NO_COLOR / FORCE_COLOR / isatty /
the diff renderer's ``respect_no_color_env`` precedence) stays with each
consumer — the diff renderer and the CLI gate colour differently (stdout vs
stderr TTY), so centralising the on/off decision here would be wrong. What is
shared is *which bytes* a given colour is, and *whether the terminal supports
24-bit*.

No I/O, no logging — pure constants + two pure functions.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Hue-less SGR controls (identical in 16-colour and truecolor modes).
# ---------------------------------------------------------------------------

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"

# ---------------------------------------------------------------------------
# 16-colour SGR fallbacks (every ANSI terminal renders these).
# ---------------------------------------------------------------------------

GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"


def truecolor_sgr(r: int, g: int, b: int) -> str:
    """Return a 24-bit foreground SGR escape for an RGB triple.

    ``\\x1b[38;2;R;G;Bm`` is the ISO 6429 / ECMA-48 direct-colour form
    supported by truecolor terminals. Consumers emit these only when
    :func:`colorterm_is_truecolor` (or their own explicit override) says the
    terminal advertises 24-bit support.
    """
    return f"\x1b[38;2;{r};{g};{b}m"


# ---------------------------------------------------------------------------
# Brand hex → truecolor SGR (mirrors ``tokens/colors.css`` of the Design
# System). One constant per Design-System colour the renderers consume.
# ---------------------------------------------------------------------------

SIGNAL = truecolor_sgr(0x2F, 0xCB, 0x7F)  # signal green #2FCB7F — kept
STEEL = truecolor_sgr(0x4F, 0x90, 0xF7)  # steel blue   #4F90F7 — kept-uncertain
NOISE = truecolor_sgr(0xFB, 0x5A, 0x60)  # noise red    #FB5A60 — dropped
FLAG = truecolor_sgr(0xF5, 0xA6, 0x23)  # flag amber   #F5A623 — flagged
SPARK = truecolor_sgr(0xFF, 0xC2, 0x4D)  # spark amber  #FFC24D — the ◆ glyph
FORGE = truecolor_sgr(0xFF, 0x6A, 0x3D)  # forge orange #FF6A3D — primary accent

# ``COLORTERM`` values that advertise 24-bit support (lower-cased).
_TRUECOLOR_COLORTERM_VALUES = frozenset({"truecolor", "24bit"})


def colorterm_is_truecolor(override: bool | None = None) -> bool:
    """Resolve whether the terminal supports the 24-bit brand palette.

    ``override`` (when not ``None``) wins — the diff renderer's ``truecolor``
    kwarg and the CLI's test seams thread an explicit value here for
    deterministic snapshots. Otherwise auto-detect via ``COLORTERM``:
    ``truecolor`` / ``24bit`` (case-insensitive) advertise 24-bit; any other
    value (or an unset var) selects the 16-colour fallback.

    Conservative by design — an unknown / unset ``COLORTERM`` degrades to the
    16-colour codes, which every ANSI terminal renders correctly.
    """
    if override is not None:
        return override
    return os.environ.get("COLORTERM", "").lower() in _TRUECOLOR_COLORTERM_VALUES


__all__ = (
    "BOLD",
    "CYAN",
    "DIM",
    "FLAG",
    "FORGE",
    "GREEN",
    "NOISE",
    "RED",
    "RESET",
    "SIGNAL",
    "SPARK",
    "STEEL",
    "YELLOW",
    "colorterm_is_truecolor",
    "truecolor_sgr",
)
