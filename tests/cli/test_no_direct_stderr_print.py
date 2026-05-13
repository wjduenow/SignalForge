"""AST-scan: no direct stderr writes in ``signalforge.cli``.

Issue #60. Every stderr write from the CLI must route through
:func:`signalforge.cli._helpers.print_stderr` so the value passes
through :func:`signalforge._common.ansi_safety.strip_ansi_escapes`
before hitting the operator's terminal. Two bypass forms exist and
the AST scan catches both:

1. ``print(..., file=sys.stderr)`` — the obvious form.
2. ``sys.stderr.write(...)`` / ``sys.stderr.flush()`` — the same
   leak vector via the file-object API. CodeRabbit caught a missed
   migration in the batch-summary path that used this form
   (issue #60 PR-review feedback); the gate now rejects it.

A direct stderr write anywhere outside ``_helpers.py`` defeats the
"escape at the sink" principle (.claude/rules/diff-renderer.md DEC-007)
for the CLI's stderr path.

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

The only allowed location for any of these forms is the
:func:`print_stderr` definition itself (it IS the sink). Every other
callsite must call ``print_stderr(...)`` instead.

``sys.stderr.isatty()`` is **read-only** introspection and is NOT a
leak vector — the gate ignores it explicitly. The gate matches only
the write-side methods (``write``, ``writelines``, ``flush``).
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


# Methods on ``sys.stderr`` that are write-side and therefore leak
# vectors. ``isatty`` / ``fileno`` / ``readable`` / ``writable`` are
# read-only introspection and stay out of the gate. If a future
# refactor reaches for another write-side method (``buffer.write``,
# ``raw.write``, ...), extend this tuple in lockstep.
_SYS_STDERR_WRITE_METHODS: frozenset[str] = frozenset({"write", "writelines", "flush"})


class _StderrBypassVisitor(ast.NodeVisitor):
    """Collect ``Call`` nodes that bypass :func:`print_stderr`.

    Two bypass shapes are flagged:

    1. ``print(..., file=sys.stderr)`` — match when ``func`` is
       ``Name(id='print')`` AND a keyword arg has ``arg == 'file'``
       whose value unparses to ``"sys.stderr"``.
    2. ``sys.stderr.write(...)`` / ``sys.stderr.writelines(...)`` /
       ``sys.stderr.flush()`` — match when ``func`` is
       ``Attribute(value=Attribute(value=Name('sys'), attr='stderr'),
       attr=<method>)`` where ``<method>`` is in
       :data:`_SYS_STDERR_WRITE_METHODS`.

    The unparse-equality comparison for the ``file=`` kwarg covers
    every concrete syntactic form of ``sys.stderr`` the parser produces.
    Aliased forms (``from sys import stderr; print(..., file=stderr)``
    or ``import sys as _s; _s.stderr.write(...)``) are deliberately
    out of scope — the project's import convention is the unaliased
    form, and the AST scan targets the same surface the issue's
    regex targeted.
    """

    def __init__(self) -> None:
        self.violations: list[int] = []  # 1-based linenos

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast convention
        if (_is_print_call(node.func) and _writes_to_sys_stderr(node)) or (
            _is_sys_stderr_write_method_call(node.func)
        ):
            self.violations.append(node.lineno)
        self.generic_visit(node)


def _is_print_call(func: ast.expr) -> bool:
    """``True`` iff ``func`` is the builtin ``print`` (a bare ``Name``)."""
    return isinstance(func, ast.Name) and func.id == "print"


def _writes_to_sys_stderr(call: ast.Call) -> bool:
    """``True`` iff ``call`` has a ``file=sys.stderr`` keyword arg."""
    return any(kw.arg == "file" and ast.unparse(kw.value) == "sys.stderr" for kw in call.keywords)


def _is_sys_stderr_write_method_call(func: ast.expr) -> bool:
    """``True`` iff ``func`` is ``sys.stderr.<write-method>``.

    Matches ``sys.stderr.write``, ``sys.stderr.writelines``,
    ``sys.stderr.flush`` — every other method (``isatty``, ``fileno``,
    ...) is read-only introspection and stays out of the gate.
    """
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _SYS_STDERR_WRITE_METHODS
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "stderr"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "sys"
    )


def _scan_file(path: Path) -> list[tuple[Path, int]]:
    """Return ``(path, lineno)`` for every direct ``print(..., file=sys.stderr)``
    callsite in ``path``. Linenos are 1-based, consistent with other
    AST-scan tests in this repo.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _StderrBypassVisitor()
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

    Only ``signalforge.cli._helpers`` may write to ``sys.stderr``
    directly — that module hosts :func:`print_stderr`, the sink.
    """
    return path.relative_to(_CLI_SRC_ROOT).as_posix() == _ALLOWED_CALLSITE_PATH


def test_no_direct_stderr_writes_outside_helpers() -> None:
    """Issue #60: every stderr write in ``signalforge.cli`` must route
    through :func:`signalforge.cli._helpers.print_stderr`.

    AST-based. Catches BOTH bypass forms — ``print(..., file=sys.stderr)``
    AND ``sys.stderr.write(...)`` / ``sys.stderr.flush()`` /
    ``sys.stderr.writelines(...)`` — at every quote / prefix
    permutation and any arg-positioning the language allows. The
    only allowed callsite is the body of :func:`print_stderr` itself
    in ``signalforge.cli._helpers``; every other callsite must call
    ``print_stderr(...)`` so the ANSI-escape strip runs.
    """
    forbidden: list[tuple[Path, int]] = [
        (path, lineno) for path, lineno in _scan_cli_subpackage() if not _is_allowed(path)
    ]
    formatted = "\n".join(f"  {p}:{lineno}" for p, lineno in forbidden)
    assert not forbidden, (
        "Found direct stderr writes in signalforge.cli outside _helpers.py "
        "(issue #60 violation):\n"
        f"{formatted}\n"
        "Use print_stderr(...) from signalforge.cli._helpers instead — it "
        "strips ANSI escapes at the sink so an upstream-controlled string "
        "carrying \\x1b[31m... cannot inject into the operator's terminal "
        "scrollback. Both forms count: print(..., file=sys.stderr) AND "
        "sys.stderr.write(...) / sys.stderr.flush()."
    )


def test_print_stderr_sink_is_present_in_helpers() -> None:
    """Sanity: ``_helpers.py`` MUST contain exactly one stderr-bypass
    hit — the body of :func:`print_stderr` itself, which is the
    intentional ``print(..., file=sys.stderr)`` callsite. Without
    this assertion, a refactor that accidentally deleted the sink
    (renaming, inlining, rewriting via ``sys.stderr.write``) would
    pass the rejection test above silently, leaving every
    ``print_stderr`` caller routed through a no-op.

    ``_helpers.py`` must NOT use the ``sys.stderr.write`` form
    internally — the gate would still count the call as a hit (the
    self-check counts every match form against the same total), but
    the sink contract says the wrapper IS the canonical
    ``print(..., file=sys.stderr)`` callsite, full stop.
    """
    helpers_path = _CLI_SRC_ROOT / _ALLOWED_CALLSITE_PATH
    hits = _scan_file(helpers_path)
    assert len(hits) == 1, (
        "Expected exactly one stderr-bypass hit in "
        f"{helpers_path} (the print(..., file=sys.stderr) call in "
        f"print_stderr), got {len(hits)}: "
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

_SYS_STDERR_WRITE_VIOLATIONS: tuple[str, ...] = (
    "sys.stderr.write(format_batch_summary(outcome))",
    "sys.stderr.flush()",
    "sys.stderr.writelines(['a\\n', 'b\\n'])",
    # Whitespace / paren variants.
    "sys.stderr  .  write(  msg  )",
)

_SYS_STDERR_WRITE_GOOD_CASES: tuple[str, ...] = (
    # Read-only introspection — not a leak vector; gate must NOT match.
    "sys.stderr.isatty()",
    "sys.stderr.fileno()",
)

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
    visitor = _StderrBypassVisitor()
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


def test_visitor_catches_sys_stderr_write_methods() -> None:
    """CodeRabbit's PR #88 catch: ``sys.stderr.write(...)`` /
    ``sys.stderr.flush()`` / ``sys.stderr.writelines(...)`` are the
    other bypass form. The gate must catch every write-side method on
    ``sys.stderr`` so the same regression cannot land twice.
    """
    for source in _SYS_STDERR_WRITE_VIOLATIONS:
        violations = _visit_source(source)
        assert violations, f"visitor missed: {source!r}"


def test_visitor_does_not_match_sys_stderr_introspection() -> None:
    """Sanity: ``sys.stderr.isatty()`` / ``sys.stderr.fileno()`` are
    read-only introspection — NOT leak vectors and the gate must not
    match them. Without this carve-out the ``should_emit_progress``
    TTY check in ``_helpers.py`` would trip the gate.
    """
    for source in _SYS_STDERR_WRITE_GOOD_CASES:
        violations = _visit_source(source)
        assert not violations, f"visitor false-matched: {source!r}"
