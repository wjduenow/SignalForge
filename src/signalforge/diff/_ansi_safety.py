"""ANSI escape stripper for diff-renderer output (DEC-007).

The diff renderer ingests strings that originated upstream from manifest
fields, LLM-drafted artifact text, prune ``why`` reasons, and grade
``reasoning``. Any of those can carry ANSI CSI escape sequences (e.g.
``\\x1b[31m...``) — either smuggled by an adversarial manifest or
synthesised by an LLM that decided to "format" its output. Rendered into
a Markdown diff and viewed in a terminal-aware previewer, those escapes
would inject color/cursor commands into the operator's screen.

The defence is to strip them at the renderer's input boundary. Mirrors
the same threat surface the safety / draft / prune / grade layers
defend with their lazy-format JSON loggers (``safety-layer.md``
DEC-022) — there the JSON encoding handles control chars; here the
Markdown sink doesn't, so we strip explicitly before the value lands
in the output buffer.
"""

from __future__ import annotations

import re

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
"""ANSI CSI (Control Sequence Introducer) regex.

Matches ESC ``[`` followed by zero-or-more semicolon-separated decimal
parameters, terminated by an ASCII letter (the final byte). Covers
SGR (color/style — ``\\x1b[31m``, ``\\x1b[1;31;4m``, reset
``\\x1b[0m``), cursor-movement (``\\x1b[2J``, ``\\x1b[H``), and the
rest of the CSI family.

Does NOT cover OSC (``\\x1b]...``), DCS (``\\x1bP...``), or other
non-CSI escapes — those are out of scope for v0.1 because the
upstream sources we worry about (manifests, LLM output) overwhelmingly
emit only CSI sequences when they emit anything. Extend the regex
when a real-world incident demonstrates otherwise.
"""


def strip_ansi_escapes(text: str) -> str:
    """Return ``text`` with all ANSI CSI escape sequences removed.

    Idempotent — calling on a string with no escapes returns it
    unchanged. Empty string returns empty string. The function does
    not coerce its input; callers that hold ``bytes`` must decode
    before calling.

    Used at the input boundary of every Markdown sink in
    ``signalforge.diff`` so the rendered output cannot smuggle
    terminal-control sequences via upstream-controlled strings.
    """
    return _ANSI_CSI_RE.sub("", text)
