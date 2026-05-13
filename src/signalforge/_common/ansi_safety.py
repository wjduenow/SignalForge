"""ANSI CSI escape stripper, shared across pipeline layers.

The diff renderer ingests strings that originated upstream from manifest
fields, LLM-drafted artifact text, prune ``why`` reasons, and grade
``reasoning``. Any of those can carry ANSI CSI escape sequences (e.g.
``\\x1b[31m...``) — either smuggled by an adversarial manifest or
synthesised by an LLM that decided to "format" its output. Rendered into
a Markdown diff and viewed in a terminal-aware previewer, those escapes
would inject color/cursor commands into the operator's screen.

The CLI shares the same threat surface for its stderr-print path
(model ids, file paths, formatted error messages produced by
``format_error_to_stderr``). ``signalforge.cli._helpers.print_stderr``
routes through this stripper so the "escape at the sink" principle
(``.claude/rules/diff-renderer.md`` DEC-007) applies to every stderr
write originating in ``signalforge.cli`` too.
"""

from __future__ import annotations

import re

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
"""ANSI CSI (Control Sequence Introducer) regex.

Matches ESC ``[`` followed by zero-or-more parameter bytes
(``0x30-0x3F`` — digits plus ``;<=>?``), zero-or-more intermediate
bytes (``0x20-0x2F`` — space plus ``!"#$%&'()*+,-./``), terminated by
exactly one final byte in ``0x40-0x7E`` (``@A...Z[\\]^_`a...z{|}~``).

This is the full ECMA-48 / ISO 6429 CSI grammar. Covers:

* SGR (color/style — ``\\x1b[31m``, ``\\x1b[1;31;4m``, reset
  ``\\x1b[0m``).
* Cursor-movement and screen-clearing (``\\x1b[2J``, ``\\x1b[H``).
* Tilde-terminated key/mode sequences such as ``\\x1b[3~`` (Delete)
  and bracketed-paste markers ``\\x1b[200~`` / ``\\x1b[201~`` —
  these terminate with ``~`` (``0x7E``), which the older
  letter-only regex missed.

Does NOT cover OSC (``\\x1b]...``), DCS (``\\x1bP...``), or other
non-CSI escapes — those are out of scope because the upstream sources
we worry about (manifests, LLM output) overwhelmingly emit only CSI
sequences when they emit anything. Extend the regex when a real-world
incident demonstrates otherwise.
"""


def strip_ansi_escapes(text: str) -> str:
    """Return ``text`` with all ANSI CSI escape sequences removed.

    Idempotent — calling on a string with no escapes returns it
    unchanged. Empty string returns empty string. The function does
    not coerce its input; callers that hold ``bytes`` must decode
    before calling.

    Used at the input boundary of every Markdown sink in
    ``signalforge.diff`` and at the stderr-print sink in
    ``signalforge.cli`` (via :func:`signalforge.cli._helpers.print_stderr`)
    so rendered output cannot smuggle terminal-control sequences via
    upstream-controlled strings.
    """
    return _ANSI_CSI_RE.sub("", text)
