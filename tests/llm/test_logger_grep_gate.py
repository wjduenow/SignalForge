"""ANSI-safe lazy-format logger grep gate (US-014 / DEC-011).

Mirrors the safety layer's DEC-022 grep gate, extended to
:mod:`signalforge.llm` and :mod:`signalforge.draft`. Every
``_LOGGER.{info,warning,debug,error}`` call in those subpackages must
use lazy ``%s``-formatting with :func:`json.dumps` for any
user-controlled string — never f-string interpolation. A column name
or model id containing ANSI escapes (``\\x1b[31m...``) would inject into
log viewers when interpolated via f-string; JSON encoding handles this,
f-string does not.

This test is the cheap floor: a regex over the source. The runtime
equivalent in the safety layer is :mod:`tests.safety.test_audit`'s
control-character round-trip.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_LLM_DIR = _REPO_ROOT / "src" / "signalforge" / "llm"
_DRAFT_DIR = _REPO_ROOT / "src" / "signalforge" / "draft"

# Matches ``_LOGGER.<method>(f"...`` on a single line. The regex is
# deliberately loose on the method name (any word chars after the dot)
# because adding new logging methods (e.g. critical) shouldn't silently
# escape the gate.
_F_STRING_LOGGER_RE = re.compile(r'_LOGGER\.\w+\(f"')


def _scan_for_f_string_logger_calls(root: Path) -> list[tuple[Path, int, str]]:
    """Walk ``root.rglob('*.py')``; return ``(path, lineno, line)`` for
    every match of :data:`_F_STRING_LOGGER_RE`.
    """
    hits: list[tuple[Path, int, str]] = []
    for path in root.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _F_STRING_LOGGER_RE.search(line):
                hits.append((path, lineno, line.rstrip()))
    return hits


def test_no_f_string_logger_calls_in_llm_or_draft_modules() -> None:
    """DEC-011: no f-string-interpolated ``_LOGGER`` calls in
    :mod:`signalforge.llm` or :mod:`signalforge.draft`.

    Use lazy ``%s`` formatting with :func:`json.dumps` for any
    user-controlled string. ANSI escapes in column names / model ids
    inject directly into log viewers when interpolated via f-string;
    JSON encoding escapes them safely.
    """
    hits = _scan_for_f_string_logger_calls(_LLM_DIR) + _scan_for_f_string_logger_calls(_DRAFT_DIR)
    formatted = "\n".join(f"  {p}:{line}: {content}" for p, line, content in hits)
    assert not hits, (
        "Found f-string-interpolated _LOGGER calls in signalforge.llm / "
        "signalforge.draft (DEC-011 violation):\n"
        f"{formatted}\n"
        "Use lazy-format with json.dumps instead, e.g.:\n"
        '  _LOGGER.info("audit event: %s", json.dumps({"unique_id": ...}))'
    )


def test_grep_gate_regex_matches_planted_violation() -> None:
    """Self-check: the regex catches the canonical bad pattern. Without
    this we'd not notice if a refactor broke the regex silently.
    """
    bad = '_LOGGER.info(f"x={x}")'
    assert _F_STRING_LOGGER_RE.search(bad)

    good = '_LOGGER.info("x: %s", json.dumps({"x": x}))'
    assert not _F_STRING_LOGGER_RE.search(good)
