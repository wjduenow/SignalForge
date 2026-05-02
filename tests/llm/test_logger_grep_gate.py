"""ANSI-safe lazy-format logger grep gate (US-014 / DEC-011).

Mirrors the safety layer's DEC-022 grep gate, extended to
:mod:`signalforge.llm`, :mod:`signalforge.draft`,
:mod:`signalforge.prune`, :mod:`signalforge.grade`, and
:mod:`signalforge.diff` (DEC-019 of #8). Every
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
_PRUNE_DIR = _REPO_ROOT / "src" / "signalforge" / "prune"
_GRADE_DIR = _REPO_ROOT / "src" / "signalforge" / "grade"
_DIFF_DIR = _REPO_ROOT / "src" / "signalforge" / "diff"

# Matches ``_LOGGER.<method>(f"...``, ``_LOGGER.<method>(f'...``,
# ``_LOGGER.<method>( f"...``, ``_LOGGER.<method>(rf"...``,
# ``_LOGGER.<method>(fr'...``, etc. on a single line.
#
# Catches every Python f-string form: any prefix permutation of `f` and
# `r` (case-insensitive), single OR double OR triple quotes, optional
# whitespace after the opening paren. Without this breadth a contributor
# could trivially bypass the DEC-011 gate by switching quote style.
_F_STRING_LOGGER_RE = re.compile(r"""_LOGGER\.\w+\(\s*(?:[fF][rR]?|[rR][fF])['"]""")


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


def test_no_f_string_logger_calls_in_llm_draft_prune_grade_or_diff_modules() -> None:
    """DEC-011 / DEC-019 of #8: no f-string-interpolated ``_LOGGER``
    calls in :mod:`signalforge.llm`, :mod:`signalforge.draft`,
    :mod:`signalforge.prune`, :mod:`signalforge.grade`, or
    :mod:`signalforge.diff`.

    Use lazy ``%s`` formatting with :func:`json.dumps` for any
    user-controlled string. ANSI escapes in column names / model ids
    inject directly into log viewers when interpolated via f-string;
    JSON encoding escapes them safely.
    """
    hits = (
        _scan_for_f_string_logger_calls(_LLM_DIR)
        + _scan_for_f_string_logger_calls(_DRAFT_DIR)
        + _scan_for_f_string_logger_calls(_PRUNE_DIR)
        + _scan_for_f_string_logger_calls(_GRADE_DIR)
        + _scan_for_f_string_logger_calls(_DIFF_DIR)
    )
    formatted = "\n".join(f"  {p}:{line}: {content}" for p, line, content in hits)
    assert not hits, (
        "Found f-string-interpolated _LOGGER calls in signalforge.llm / "
        "signalforge.draft / signalforge.prune / signalforge.grade / "
        "signalforge.diff (DEC-011 violation):\n"
        f"{formatted}\n"
        "Use lazy-format with json.dumps instead, e.g.:\n"
        '  _LOGGER.info("audit event: %s", json.dumps({"unique_id": ...}))'
    )


def test_grep_gate_regex_matches_planted_violation() -> None:
    """Self-check: the regex catches every f-string form. Without
    this we'd not notice if a refactor broke the regex silently.
    """
    bad_cases = (
        '_LOGGER.info(f"x={x}")',
        "_LOGGER.info(f'x={x}')",
        '_LOGGER.info( f"x={x}")',  # whitespace after paren
        '_LOGGER.info(rf"x={x}")',
        "_LOGGER.info(rf'x={x}')",
        '_LOGGER.info(fr"x={x}")',
        '_LOGGER.warning(F"x={x}")',  # uppercase F
    )
    for bad in bad_cases:
        assert _F_STRING_LOGGER_RE.search(bad), f"regex missed: {bad!r}"

    good_cases = (
        '_LOGGER.info("x: %s", json.dumps({"x": x}))',
        '_LOGGER.info("plain string with no interpolation")',
        # Don't match a random `_LOGGER.x(...)` followed by an f-string
        # NOT inside the call (this is implementation-dependent — the
        # regex is line-scoped so the first match wins).
    )
    for good in good_cases:
        assert not _F_STRING_LOGGER_RE.search(good), f"regex spurious-matched: {good!r}"
