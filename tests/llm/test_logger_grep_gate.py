"""ANSI-safe lazy-format logger gate (US-014 / DEC-011) — AST-based.

Mirrors the safety layer's DEC-022 gate, extended to
:mod:`signalforge.llm`, :mod:`signalforge.draft`,
:mod:`signalforge.prune`, :mod:`signalforge.grade`,
:mod:`signalforge.diff` (DEC-019 of #8), :mod:`signalforge.cli`
(DEC-017 of #9), :mod:`signalforge.warehouse` (QG fix of #22), and
:mod:`signalforge.manifest` (issue #45 — gate doubles as a stage-0
"no logging" enforcer; per ``manifest-readers.md`` § "No logging /
metrics in stage-0 modules" the package should have no ``_LOGGER`` at
all, so any f-string ``_LOGGER`` call there is doubly wrong).

Every ``_LOGGER.{info,warning,debug,error,...}`` call in those
subpackages must use lazy ``%s``-formatting with :func:`json.dumps`
for any user-controlled string — never f-string interpolation. A
column name or model id containing ANSI escapes (``\\x1b[31m...``)
would inject into log viewers when interpolated via f-string; JSON
encoding handles this, f-string does not.

The historic implementation was a per-line :mod:`re` scan, which a
contributor could trivially bypass by splitting the call across
lines (issue #45)::

    _LOGGER.info(
        f"resolved project_dir: {project_dir}"  # NOT caught by per-line regex
    )

This module uses :mod:`ast` instead: it walks every
``Call(func=Attribute(value=Name('_LOGGER')))`` node and checks
whether any argument is an :class:`ast.JoinedStr` (f-string),
catching every quote style, prefix permutation, and whitespace /
newline arrangement uniformly.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_ROOT = _REPO_ROOT / "src" / "signalforge"

# Subpackages covered by the gate. Order is alphabetical for ease of
# diffing when a future stage extends the list.
_SCAN_SUBPACKAGES: tuple[str, ...] = (
    "cli",
    "demo",
    "diff",
    "draft",
    "grade",
    "llm",
    "manifest",
    "prune",
    "safety",
    "warehouse",
)


class _LoggerFStringVisitor(ast.NodeVisitor):
    """Collects ``Call`` nodes that interpolate an f-string into a
    ``_LOGGER.<method>(...)`` call.

    Match conditions:

    - ``func`` is ``Attribute(value=Name(id='_LOGGER'), attr=<any>)`` —
      any logger method (``info``, ``warning``, ``debug``, ``error``,
      ``exception``, ``critical``, ``log``).
    - Any positional arg OR keyword-arg value contains (recursively) an
      :class:`ast.JoinedStr`. Recursion catches f-strings nested inside
      tuples / lists / function calls that happen to be passed as args
      (rare, but the gate stays robust).

    A bare :class:`ast.JoinedStr` arg covers every f-string prefix
    permutation (``f"..."``, ``rf"..."``, ``fr"..."``, ``F"..."``,
    ``Rf"..."``, ...) — the parser normalises all of them into the
    same AST node.
    """

    def __init__(self) -> None:
        self.violations: list[int] = []  # 1-based linenos

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast convention
        if _is_logger_attribute_call(node.func) and _any_arg_contains_fstring(node):
            self.violations.append(node.lineno)
        self.generic_visit(node)


def _is_logger_attribute_call(func: ast.expr) -> bool:
    """``True`` iff ``func`` is ``_LOGGER.<method>``."""
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "_LOGGER"
    )


def _any_arg_contains_fstring(call: ast.Call) -> bool:
    """``True`` iff any positional or keyword arg of ``call`` contains
    an :class:`ast.JoinedStr` at any nesting depth.
    """
    candidates: list[ast.AST] = list(call.args)
    candidates.extend(kw.value for kw in call.keywords)
    for arg in candidates:
        for sub in ast.walk(arg):
            if isinstance(sub, ast.JoinedStr):
                return True
    return False


def _scan_file(path: Path) -> list[tuple[Path, int]]:
    """Return ``(path, lineno)`` for every f-string ``_LOGGER`` call
    in ``path``. Linenos are 1-based, consistent with other AST-scan
    tests in this repo (e.g. ``tests/safety/test_public_api.py``).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _LoggerFStringVisitor()
    visitor.visit(tree)
    return [(path, lineno) for lineno in visitor.violations]


def _scan_subpackage(name: str) -> list[tuple[Path, int]]:
    """Walk ``src/signalforge/<name>/**/*.py`` and collect violations."""
    root = _SRC_ROOT / name
    hits: list[tuple[Path, int]] = []
    for path in sorted(root.rglob("*.py")):
        hits.extend(_scan_file(path))
    return hits


def test_no_f_string_logger_calls_across_subpackages() -> None:
    """DEC-011 / DEC-019 of #8 / DEC-017 of #9 / QG fix of #22 / issue
    #45: no f-string-interpolated ``_LOGGER`` calls in any covered
    subpackage.

    AST-based: catches single-line, multi-line, every quote / prefix
    permutation, and any future arg-positioning the language allows.

    Use lazy ``%s`` formatting with :func:`json.dumps` for any
    user-controlled string. ANSI escapes in column names / model ids
    inject directly into log viewers when interpolated via f-string;
    JSON encoding escapes them safely.
    """
    hits: list[tuple[Path, int]] = []
    for subpkg in _SCAN_SUBPACKAGES:
        hits.extend(_scan_subpackage(subpkg))

    formatted = "\n".join(f"  {p}:{lineno}" for p, lineno in hits)
    covered = " / ".join(f"signalforge.{name}" for name in _SCAN_SUBPACKAGES)
    assert not hits, (
        f"Found f-string-interpolated _LOGGER calls in {covered} "
        "(DEC-011 violation):\n"
        f"{formatted}\n"
        "Use lazy-format with json.dumps instead, e.g.:\n"
        '  _LOGGER.info("audit event: %s", json.dumps({"unique_id": ...}))'
    )


def _file_has_logging_or_logger_node(path: Path) -> str | None:
    """Return a short reason string if ``path``'s AST contains:

    - ``import logging`` or ``import logging as X``
    - ``from logging import ...``
    - any ``logging.<attr>`` reference (e.g. ``logging.getLogger``)
    - any ``Name(id='_LOGGER')`` reference (assignment or use)

    Otherwise return ``None``. An AST walk avoids substring
    false-positives (e.g. the literal token ``_LOGGER`` inside a
    docstring or comment) and catches indirection like ``from logging
    import getLogger; LOG = getLogger(__name__)`` that a raw token
    scan would miss.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "logging" or alias.name.startswith("logging.") for alias in node.names
        ):
            return "imports the logging module"
        if isinstance(node, ast.ImportFrom) and node.module == "logging":
            return "imports from the logging module"
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "logging"
        ):
            return f"references logging.{node.attr}"
        if isinstance(node, ast.Name) and node.id == "_LOGGER":
            return "references a _LOGGER name"
    return None


def test_manifest_subpackage_has_no_logger_at_all() -> None:
    """Issue #45 (sharpened from PR #75 review): ``signalforge.manifest``
    is a stage-0 reader/parser per ``manifest-readers.md`` § "No
    logging / metrics in stage-0 modules"; it must not import
    :mod:`logging`, reference :mod:`logging` attributes, or define
    ``_LOGGER``.

    The f-string gate above also covers ``manifest/``, but a future
    addition that introduced lazy-format ``_LOGGER`` calls would pass
    that gate while still violating the stage-0 rule. This second
    assertion enforces absence directly via an AST walk so the check
    catches every form of logging indirection (``import logging``,
    ``from logging import getLogger``, ``logging.getLogger(...)``,
    bare ``_LOGGER`` references) without false-positives on docstring
    mentions of the token.
    """
    manifest_root = _SRC_ROOT / "manifest"
    offenders: list[tuple[Path, str]] = []
    for path in sorted(manifest_root.rglob("*.py")):
        reason = _file_has_logging_or_logger_node(path)
        if reason is not None:
            offenders.append((path, reason))
    formatted = "\n".join(f"  {p} — {reason}" for p, reason in offenders)
    assert not offenders, (
        "signalforge.manifest is a stage-0 reader and must not import "
        "logging or define _LOGGER (manifest-readers.md § 'No logging "
        f"/ metrics in stage-0 modules'):\n{formatted}"
    )


# Self-checks for the visitor. Without these, a refactor that broke
# the visitor (e.g., dropped the ``ast.walk`` recursion or stopped
# checking keyword args) would silently disable the gate.

_SINGLE_LINE_VIOLATIONS: tuple[str, ...] = (
    '_LOGGER.info(f"x={x}")',
    "_LOGGER.info(f'x={x}')",
    '_LOGGER.info( f"x={x}")',  # whitespace after paren
    '_LOGGER.info(rf"x={x}")',
    "_LOGGER.info(rf'x={x}')",
    '_LOGGER.info(fr"x={x}")',
    '_LOGGER.warning(F"x={x}")',  # uppercase F
    '_LOGGER.debug(f"""multi\nline\nbody {x}""")',  # triple-quoted f-string
    '_LOGGER.exception(f"err {x}")',  # any logger method, not just info
    '_LOGGER.log(logging.INFO, f"msg {x}")',  # second positional arg
)

_MULTILINE_VIOLATION = """\
_LOGGER.info(
    f"resolved project_dir: {project_dir}"
)
"""

_MULTILINE_KWARG_VIOLATION = """\
_LOGGER.warning(
    "stuff",
    extra={"x": f"bad {y}"},
)
"""

_GOOD_CASES: tuple[str, ...] = (
    '_LOGGER.info("x: %s", json.dumps({"x": x}))',
    '_LOGGER.info("plain string with no interpolation")',
    '_LOGGER.warning("audit: %s", json.dumps(payload))',
    # An f-string OUTSIDE a _LOGGER call must NOT match.
    'msg = f"context {x}"\n_LOGGER.info("msg: %s", msg)',
    # `.format()` is still discouraged (not lazy), but it is NOT an
    # f-string — the gate intentionally targets f-strings only.
    '_LOGGER.info("x: {}".format(x))',
)


def _visit_source(source: str) -> list[int]:
    tree = ast.parse(source)
    visitor = _LoggerFStringVisitor()
    visitor.visit(tree)
    return visitor.violations


def _check_manifest_detector(source: str, tmp_path: Path, name: str) -> str | None:
    """Write ``source`` to a temp ``.py`` file and run the stage-0
    AST detector against it. Returns the reason string, or ``None``.
    """
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return _file_has_logging_or_logger_node(path)


def test_visitor_catches_single_line_fstring_calls() -> None:
    """Sanity: every single-line f-string ``_LOGGER`` form is caught."""
    for source in _SINGLE_LINE_VIOLATIONS:
        violations = _visit_source(source)
        assert violations, f"visitor missed: {source!r}"


def test_visitor_catches_multiline_fstring_call() -> None:
    """Issue #45: the multi-line bypass — ``_LOGGER.info(`` and the
    ``f"..."`` on different lines — must be caught.

    This is the regression test for the historic per-line regex
    bypass. Run against the prior implementation, this test would
    fail loud.
    """
    violations = _visit_source(_MULTILINE_VIOLATION)
    assert violations, (
        "visitor missed the multi-line f-string _LOGGER call "
        "(the bypass the per-line regex couldn't catch)"
    )


def test_visitor_catches_fstring_in_keyword_arg() -> None:
    """Defence-in-depth: an f-string passed via a keyword arg (e.g.
    ``extra={"x": f"..."}``) must also be caught. The historic regex
    accidentally covered this for single-line calls; the AST walk
    must keep covering it.
    """
    violations = _visit_source(_MULTILINE_KWARG_VIOLATION)
    assert violations, "visitor missed an f-string nested inside a keyword arg"


def test_visitor_does_not_match_safe_calls() -> None:
    """Sanity: lazy-format and non-``_LOGGER`` f-strings must not
    match. Without this we'd silently turn the gate into a noisy
    false-positive generator.
    """
    for source in _GOOD_CASES:
        violations = _visit_source(source)
        assert not violations, f"visitor false-matched: {source!r}"


def test_manifest_detector_catches_every_logging_indirection(tmp_path: Path) -> None:
    """PR #75 review: the manifest stage-0 check must catch every
    form of logging indirection, not just the literal token
    ``_LOGGER``. ``import logging``, ``from logging import getLogger``,
    a ``logging.<attr>`` reference, and a bare ``_LOGGER`` name all
    have to trip the detector.
    """
    cases = (
        ("import_logging", "import logging\n"),
        ("import_logging_as", "import logging as _log\n"),
        ("from_logging_import", "from logging import getLogger\n_LOG = getLogger(__name__)\n"),
        ("logging_getLogger", "import logging\n_LOG = logging.getLogger(__name__)\n"),
        ("bare_logger_assignment", "_LOGGER = object()\n"),
        ("bare_logger_call", "_LOGGER.info('x')\n"),
    )
    for name, source in cases:
        reason = _check_manifest_detector(source, tmp_path, name)
        assert reason is not None, f"detector missed {name!r}: {source!r}"


def test_manifest_detector_does_not_false_positive_on_token_in_docstring(
    tmp_path: Path,
) -> None:
    """PR #75 review: a docstring or comment mentioning the literal
    string ``_LOGGER`` (e.g. a rule citation) must NOT trip the
    detector. The historic substring check would have false-matched
    here; the AST walk fixes that.
    """
    docstring_only = (
        '"""Stage-0 reader; per manifest-readers.md must not define _LOGGER."""\n'
        "# `_LOGGER` is forbidden here — see the rule file.\n"
        "x = 1\n"
    )
    reason = _check_manifest_detector(docstring_only, tmp_path, "docstring_mention")
    assert reason is None, (
        "detector false-positive on a docstring / comment that mentions "
        f"the literal token _LOGGER: {reason!r}"
    )
