"""AST-scan: no direct ``print(..., file=sys.stderr)`` in ``signalforge.cli``.

Issue #60. Every stderr write from the CLI must route through
:func:`signalforge.cli._helpers.print_stderr` so the value passes
through :func:`signalforge._common.ansi_safety.strip_ansi_escapes`
before hitting the operator's terminal. A direct ``print(...,
file=sys.stderr)`` callsite anywhere else in ``signalforge.cli``
defeats the "escape at the sink" principle (.claude/rules/diff-renderer.md
DEC-007) for the CLI's stderr path.

AST-based per ``.claude/rules/testing-signal.md`` § "Source-scan gates:
AST over per-line regex (issue #45)". A per-line regex would miss the
multi-line bypass::

    print(
        format_error_to_stderr(exc),
        file=sys.stderr,
    )

and would also miss any callsite where the positional args contain a
nested ``)`` (e.g., ``"\\n".join(bullets)``). The AST walk catches every
quote style, prefix permutation, and whitespace / newline arrangement
uniformly.

The only allowed location for ``print(..., file=sys.stderr)`` is the
:func:`print_stderr` definition itself (it IS the sink). Every other
callsite must call ``print_stderr(...)`` instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CLI_SRC_ROOT = _REPO_ROOT / "src" / "signalforge" / "cli"

# The single allowed callsite — the body of ``print_stderr`` itself.
# Stored as a relative POSIX path so the assertion message is stable
# across OS path separators.
_ALLOWED_CALLSITE_PATH = "_helpers.py"


class _DirectStderrPrintVisitor(ast.NodeVisitor):
    """Collect ``Call`` nodes that resolve to ``print(..., file=sys.stderr)``.

    Match conditions:

    - ``func`` is ``Name(id='print')`` — the builtin :func:`print`.
    - At least one keyword arg has ``arg == 'file'`` AND its value,
      unparsed, equals ``"sys.stderr"``.

    The unparse-equality comparison covers every concrete syntactic
    form of ``sys.stderr`` the parser produces (the standard
    ``sys.stderr`` Attribute access). Aliased forms
    (``from sys import stderr; print(..., file=stderr)`` or
    ``import sys as _s; print(..., file=_s.stderr)``) are deliberately
    out of scope — the project's import convention is the unaliased
    form, and the AST scan targets the same surface the issue's
    regex targeted.
    """

    def __init__(self) -> None:
        self.violations: list[int] = []  # 1-based linenos

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast convention
        if _is_print_call(node.func) and _writes_to_sys_stderr(node):
            self.violations.append(node.lineno)
        self.generic_visit(node)


def _is_print_call(func: ast.expr) -> bool:
    """``True`` iff ``func`` is the builtin ``print`` (a bare ``Name``)."""
    return isinstance(func, ast.Name) and func.id == "print"


def _writes_to_sys_stderr(call: ast.Call) -> bool:
    """``True`` iff ``call`` has a ``file=sys.stderr`` keyword arg."""
    return any(kw.arg == "file" and ast.unparse(kw.value) == "sys.stderr" for kw in call.keywords)


def _scan_file(path: Path) -> list[tuple[Path, int]]:
    """Return ``(path, lineno)`` for every direct ``print(..., file=sys.stderr)``
    callsite in ``path``. Linenos are 1-based, consistent with other
    AST-scan tests in this repo.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _DirectStderrPrintVisitor()
    visitor.visit(tree)
    return [(path, lineno) for lineno in visitor.violations]


def _scan_cli_subpackage() -> list[tuple[Path, int]]:
    """Walk ``src/signalforge/cli/**/*.py`` and collect violations."""
    hits: list[tuple[Path, int]] = []
    for path in sorted(_CLI_SRC_ROOT.rglob("*.py")):
        hits.extend(_scan_file(path))
    return hits


def _is_allowed(path: Path) -> bool:
    """``True`` iff ``path`` is the single allowed callsite location.

    Only ``signalforge.cli._helpers`` may call ``print(..., file=sys.stderr)``
    — that module hosts :func:`print_stderr`, the sink itself.
    """
    return path.relative_to(_CLI_SRC_ROOT).as_posix() == _ALLOWED_CALLSITE_PATH


def test_no_direct_stderr_print_outside_helpers() -> None:
    """Issue #60: every stderr write in ``signalforge.cli`` must route
    through :func:`signalforge.cli._helpers.print_stderr`.

    AST-based. Catches single-line, multi-line, every quote / prefix
    permutation, and any arg-positioning the language allows. The only
    allowed callsite is the body of :func:`print_stderr` itself in
    ``signalforge.cli._helpers``; every other callsite must call
    ``print_stderr(...)`` so the ANSI-escape strip runs.
    """
    forbidden: list[tuple[Path, int]] = [
        (path, lineno) for path, lineno in _scan_cli_subpackage() if not _is_allowed(path)
    ]
    formatted = "\n".join(f"  {p}:{lineno}" for p, lineno in forbidden)
    assert not forbidden, (
        "Found direct print(..., file=sys.stderr) callsites in "
        "signalforge.cli outside _helpers.py (issue #60 violation):\n"
        f"{formatted}\n"
        "Use print_stderr(...) from signalforge.cli._helpers instead — "
        "it strips ANSI escapes at the sink so an upstream-controlled "
        "string carrying \\x1b[31m... cannot inject into the operator's "
        "terminal scrollback."
    )


def test_print_stderr_sink_is_present_in_helpers() -> None:
    """Sanity: ``_helpers.py`` MUST contain exactly one direct
    ``print(..., file=sys.stderr)`` callsite — the body of
    :func:`print_stderr` itself. Without this assertion, a refactor
    that accidentally deleted the sink (renaming, inlining,
    rewriting via ``sys.stderr.write``) would pass the rejection
    test above silently, leaving every ``print_stderr`` caller
    routed through a no-op.
    """
    helpers_path = _CLI_SRC_ROOT / _ALLOWED_CALLSITE_PATH
    hits = _scan_file(helpers_path)
    assert len(hits) == 1, (
        "Expected exactly one print(..., file=sys.stderr) callsite in "
        f"{helpers_path} (the body of print_stderr), got {len(hits)}: "
        f"{[lineno for _, lineno in hits]}. If you intentionally "
        "rewrote the sink (e.g., via sys.stderr.write), update this "
        "test in lockstep."
    )


# ---------------------------------------------------------------------------
# Self-checks for the visitor. Without these, a refactor that broke the
# visitor (e.g., dropped the ast.walk recursion or stopped checking
# keyword args) would silently disable the gate.
# ---------------------------------------------------------------------------

_SINGLE_LINE_VIOLATIONS: tuple[str, ...] = (
    'print("x", file=sys.stderr)',
    "print('x', file=sys.stderr)",
    'print(  "x"  ,  file = sys.stderr  )',  # generous whitespace
    "print(format_error_to_stderr(exc), file=sys.stderr)",
    # Nested ``)`` in a positional arg — the form the issue-#60 regex
    # missed in ``lint.py`` (``"\n".join(bullets)``).
    'print(header + "\\n" + "\\n".join(bullets), file=sys.stderr)',
)

_MULTILINE_VIOLATION = """\
print(
    format_error_to_stderr(exc),
    file=sys.stderr,
)
"""

_MULTILINE_KWARG_VIOLATION = """\
print(
    "stuff",
    file=sys.stderr,
    flush=True,
)
"""

_GOOD_CASES: tuple[str, ...] = (
    # stdout print — no file kwarg.
    'print("x")',
    # Explicit stdout — out of scope.
    "print('x', file=sys.stdout)",
    # The sink's own helper — NOT print(..., file=sys.stderr); a call to
    # the wrapper. The visitor must NOT match this.
    'print_stderr("x")',
    # An aliased form is out of scope (the gate targets the literal
    # ``sys.stderr`` surface the issue's regex named).
    "from sys import stderr\nprint('x', file=stderr)",
)


def _visit_source(source: str) -> list[int]:
    tree = ast.parse(source)
    visitor = _DirectStderrPrintVisitor()
    visitor.visit(tree)
    return visitor.violations


def test_visitor_catches_single_line_callsites() -> None:
    """Sanity: every single-line ``print(..., file=sys.stderr)`` form
    is caught, including the nested-``)`` form that the historic
    regex missed.
    """
    for source in _SINGLE_LINE_VIOLATIONS:
        violations = _visit_source(source)
        assert violations, f"visitor missed: {source!r}"


def test_visitor_catches_multiline_callsite() -> None:
    """Issue #60: the multi-line bypass — ``print(`` and
    ``file=sys.stderr`` on different lines — must be caught.

    Run against a per-line regex implementation, this test would
    fail loud (the regex's ``[^)]*`` stops at the first ``)`` it
    sees, which is the closing paren of any nested call).
    """
    violations = _visit_source(_MULTILINE_VIOLATION)
    assert violations, (
        "visitor missed the multi-line print(..., file=sys.stderr) "
        "call (the bypass a per-line regex couldn't catch)"
    )


def test_visitor_catches_multiline_kwarg_callsite() -> None:
    """Defence-in-depth: a multi-line call with extra kwargs (e.g.,
    ``flush=True``) must also be caught.
    """
    violations = _visit_source(_MULTILINE_KWARG_VIOLATION)
    assert violations, (
        "visitor missed a multi-line print(...) with file=sys.stderr alongside other kwargs"
    )


def test_visitor_does_not_match_safe_calls() -> None:
    """Sanity: stdout prints, ``print_stderr(...)`` wrapper calls, and
    aliased ``file=stderr`` forms must NOT match. Without this we'd
    silently turn the gate into a noisy false-positive generator.
    """
    for source in _GOOD_CASES:
        violations = _visit_source(source)
        assert not violations, f"visitor false-matched: {source!r}"
