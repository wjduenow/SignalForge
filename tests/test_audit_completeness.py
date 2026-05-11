"""Top-level audit-completeness AST scans (US-014 / DEC-013).

Walks Python source via :mod:`ast` and rejects forbidden construction
patterns that would bypass the audit-write seam in each layer. The
existing :func:`tests.safety.test_public_api.test_llm_request_construction_only_in_request_module`
covers Scan 1 (``LLMRequest`` outside ``signalforge.safety.request``);
this module adds the remaining scans:

* **Scan 2** — ``AuditEvent(...)`` outside ``signalforge.safety.request``.
* **Scan 3** — ``anthropic.Anthropic(...)`` outside
  ``signalforge.llm._client``.
* **Scan 4** — ``LLMResponseEvent(...)`` outside
  ``signalforge.draft.audit``.
* **Scan 5** — ``PruneEvent(...)`` outside ``signalforge.prune.audit``.
* **Scan 6** — ``GradeEvent(...)`` outside ``signalforge.grade.audit``.
* **Scan 7** — every ``class <Name>Error(...):`` in
  ``src/signalforge/*/errors.py`` (and ``src/signalforge/cli/errors.py``)
  appears as a key in
  :data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` — the
  load-bearing test that turns the four-tier exit-code taxonomy from a
  guideline into a contract (DEC-024 of #9).
* **Scan 8** — fail-closed writer shape across the five audit/sidecar
  writer modules (issue #38). No ``except`` handler may wrap a ``Try``
  whose body issues ``os.write`` / ``os.fsync``; every writer function
  must use a short-write loop. The propagation IS the defence
  (safety-layer.md DEC-011, repeated in prune/grade/diff rules).

Each scan is its own test with an explicit, justified exclusion list. The
scans are deterministic and cheap: each ``.py`` is read once via
:meth:`pathlib.Path.read_text`, parsed once with :func:`ast.parse`, and
walked via :func:`ast.walk`.

Scan 3 is stricter than the regex-level check in
``tests/llm/test_client_shim.py::test_anthropic_client_construction_only_in_shim``
— that test is the cheap floor; this is the load-bearing AST one.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SAFETY_DIR = _REPO_ROOT / "src" / "signalforge" / "safety"
_LLM_DIR = _REPO_ROOT / "src" / "signalforge" / "llm"
_DRAFT_DIR = _REPO_ROOT / "src" / "signalforge" / "draft"
_PRUNE_DIR = _REPO_ROOT / "src" / "signalforge" / "prune"
_GRADE_DIR = _REPO_ROOT / "src" / "signalforge" / "grade"
_SIGNALFORGE_DIR = _REPO_ROOT / "src" / "signalforge"


# ---------------------------------------------------------------------------
# AST visitor helpers
# ---------------------------------------------------------------------------


class _NameCallFinder(ast.NodeVisitor):
    """Records every ``Call(func=Name(id=<target>))`` in the visited tree."""

    def __init__(self, target: str) -> None:
        self._target = target
        self.calls: list[tuple[int, int]] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast API
        if isinstance(node.func, ast.Name) and node.func.id == self._target:
            self.calls.append((node.lineno, node.col_offset))
        self.generic_visit(node)


class _AttributeCallFinder(ast.NodeVisitor):
    """Records every ``Call(func=Attribute(value=Name(id=<obj>), attr=<attr>))``,
    accounting for `import <obj> as <alias>` aliasing AND
    `from <obj> import <attr>` direct-symbol imports.

    Used by Scan 3 to detect ``anthropic.Anthropic(...)`` — the SDK
    construction call shape that DEC-012 confines to ``_client.py``.
    Without alias-tracking the test could be bypassed by:
    ``import anthropic as a; a.Anthropic(...)`` or
    ``from anthropic import Anthropic; Anthropic(...)``.
    """

    def __init__(self, obj_name: str, attr_name: str) -> None:
        self._obj_name = obj_name
        self._attr_name = attr_name
        # Names that bind to ``<obj>`` in this module's scope. Always
        # includes the canonical name so unaliased imports work.
        self._obj_aliases: set[str] = {obj_name}
        # Names that bind directly to ``<obj>.<attr>`` in this module's
        # scope (via ``from <obj> import <attr>`` or its aliases).
        self._direct_aliases: set[str] = set()
        self.calls: list[tuple[int, int]] = []

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 — ast API
        for alias in node.names:
            if alias.name == self._obj_name:
                self._obj_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802 — ast API
        if node.module == self._obj_name:
            for alias in node.names:
                if alias.name == self._attr_name:
                    self._direct_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast API
        func = node.func
        # Attribute form: <obj_or_alias>.<attr>(...)
        if (
            (
                isinstance(func, ast.Attribute)
                and func.attr == self._attr_name
                and isinstance(func.value, ast.Name)
                and func.value.id in self._obj_aliases
            )
            or isinstance(func, ast.Name)
            and func.id in self._direct_aliases
        ):
            self.calls.append((node.lineno, node.col_offset))
        self.generic_visit(node)


def _scan_dir_for_name_calls(
    root: Path, *, target: str, excluded_relpaths: set[str]
) -> list[tuple[Path, int]]:
    """Walk ``root.rglob('*.py')``; collect ``Call(func=Name(id=target))``
    hits except in any file whose path relative to ``root`` (POSIX form)
    is in ``excluded_relpaths``.

    Path-based exclusion (vs basename) prevents accidental shadowing if
    a future nested module happens to share a name with a sanctioned
    seam (e.g. a hypothetical ``signalforge/safety/draft/request.py``
    must not auto-inherit the ``request.py`` exclusion).
    """
    hits: list[tuple[Path, int]] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in excluded_relpaths:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        finder = _NameCallFinder(target)
        finder.visit(tree)
        for line, _col in finder.calls:
            hits.append((path, line))
    return hits


def _scan_dir_for_attribute_calls(
    root: Path,
    *,
    obj_name: str,
    attr_name: str,
    excluded_relpaths: set[str],
) -> list[tuple[Path, int]]:
    """Walk ``root.rglob('*.py')``; collect ``<obj>.<attr>(...)`` hits —
    accounting for import aliasing — except in any file whose path
    relative to ``root`` (POSIX form) is in ``excluded_relpaths``.
    """
    hits: list[tuple[Path, int]] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in excluded_relpaths:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        finder = _AttributeCallFinder(obj_name, attr_name)
        finder.visit(tree)
        for line, _col in finder.calls:
            hits.append((path, line))
    return hits


# ---------------------------------------------------------------------------
# Scan 2 — AuditEvent only in safety.request
# ---------------------------------------------------------------------------


# DEC-014 / DEC-020(a): AuditEvent is constructed only inside
# build_llm_request — direct construction anywhere else bypasses the
# fail-closed audit-write seam (DEC-011 of safety-layer.md).
_SAFETY_AUDIT_EVENT_EXCLUSIONS: set[str] = {
    # request.py is the sole audit-write seam: build_llm_request constructs
    # AuditEvent and hands it to audit.write before returning the LLMRequest.
    "request.py",
}


def test_audit_event_construction_only_in_safety_request_module() -> None:
    """DEC-013: direct ``AuditEvent(...)`` outside
    ``signalforge.safety.request`` bypasses the fail-closed audit-write
    seam. The AST scan rejects any other location.
    """
    hits = _scan_dir_for_name_calls(
        _SAFETY_DIR, target="AuditEvent", excluded_relpaths=_SAFETY_AUDIT_EVENT_EXCLUSIONS
    )
    formatted = "\n".join(f"  {p}:{line}" for p, line in hits)
    assert not hits, (
        "AuditEvent constructed outside signalforge.safety.request:\n"
        f"{formatted}\n"
        "Construct only via build_llm_request — direct construction "
        "bypasses the fail-closed audit-write seam (DEC-011)."
    )


def test_audit_event_construction_in_safety_request_module_is_present() -> None:
    """Sanity: at least one ``AuditEvent(...)`` exists in
    ``signalforge.safety.request``. If this fails the scan above is no
    longer load-bearing.
    """
    request_path = _SAFETY_DIR / "request.py"
    tree = ast.parse(request_path.read_text(encoding="utf-8"))
    finder = _NameCallFinder("AuditEvent")
    finder.visit(tree)
    assert finder.calls, (
        "Expected AuditEvent(...) call in signalforge.safety.request — "
        "the AST-scan above is no longer load-bearing if the legitimate "
        "constructor disappears."
    )


# ---------------------------------------------------------------------------
# Scan 3 — anthropic.Anthropic only in llm._client
# ---------------------------------------------------------------------------


# DEC-012 / DEC-013: every Anthropic SDK ``# pyright: ignore`` and the
# SDK construction call itself live in ``_client.py``. Stricter than the
# regex check in tests/llm/test_client_shim.py — this scans the AST.
_LLM_ANTHROPIC_EXCLUSIONS: set[str] = {
    # _client.py is the sole SDK seam: it lazy-imports ``anthropic`` and
    # constructs ``anthropic.Anthropic(api_key=...)`` inside
    # ``_make_anthropic_client``.
    "_client.py",
}


def test_anthropic_client_construction_only_in_llm_client_shim() -> None:
    """DEC-013: ``anthropic.Anthropic(...)`` outside
    ``signalforge.llm._client`` violates the SDK-confinement convention.
    The AST scan is stricter than the regex check in
    ``tests/llm/test_client_shim.py`` (catches multi-line / commented
    forms the regex would miss).
    """
    hits = _scan_dir_for_attribute_calls(
        _LLM_DIR,
        obj_name="anthropic",
        attr_name="Anthropic",
        excluded_relpaths=_LLM_ANTHROPIC_EXCLUSIONS,
    )
    formatted = "\n".join(f"  {p}:{line}" for p, line in hits)
    assert not hits, (
        "anthropic.Anthropic(...) constructed outside "
        "signalforge.llm._client:\n"
        f"{formatted}\n"
        "Construct only via _make_anthropic_client — DEC-012 confines "
        "Anthropic-SDK noise to the shim."
    )


def test_anthropic_client_construction_in_llm_client_shim_is_present() -> None:
    """Sanity: at least one ``anthropic.Anthropic(...)`` in ``_client.py``.
    If this fails the scan above is no longer load-bearing.
    """
    client_path = _LLM_DIR / "_client.py"
    tree = ast.parse(client_path.read_text(encoding="utf-8"))
    finder = _AttributeCallFinder("anthropic", "Anthropic")
    finder.visit(tree)
    assert finder.calls, (
        "Expected anthropic.Anthropic(...) call in "
        "signalforge.llm._client — the AST-scan above is no longer "
        "load-bearing if the legitimate constructor disappears."
    )


# ---------------------------------------------------------------------------
# Scan 4 — LLMResponseEvent only in draft.audit
# ---------------------------------------------------------------------------


# DEC-013: LLMResponseEvent is constructed only inside the draft.audit
# module — the ``_build_response_event`` helper is the single audit-write
# seam the integration layer (draft.schema) calls. Constructing the event
# anywhere else bypasses the fail-closed JSONL writer.
_DRAFT_RESPONSE_EVENT_EXCLUSIONS: set[str] = {
    # audit.py is the sole audit-write seam: ``_build_response_event``
    # constructs the LLMResponseEvent and ``write_response_event`` is the
    # fail-closed JSONL writer (mirrors safety/audit.py from #4).
    "audit.py",
}


def test_llm_response_event_construction_only_in_draft_audit_module() -> None:
    """DEC-013: direct ``LLMResponseEvent(...)`` outside
    ``signalforge.draft.audit`` bypasses the fail-closed audit-write seam.
    """
    hits = _scan_dir_for_name_calls(
        _DRAFT_DIR,
        target="LLMResponseEvent",
        excluded_relpaths=_DRAFT_RESPONSE_EVENT_EXCLUSIONS,
    )
    formatted = "\n".join(f"  {p}:{line}" for p, line in hits)
    assert not hits, (
        "LLMResponseEvent constructed outside signalforge.draft.audit:\n"
        f"{formatted}\n"
        "Construct only via _build_response_event — direct construction "
        "bypasses the fail-closed JSONL audit writer."
    )


def test_llm_response_event_construction_in_draft_audit_module_is_present() -> None:
    """Sanity: at least one ``LLMResponseEvent(...)`` in
    ``signalforge.draft.audit``. If this fails the scan above is no longer
    load-bearing.
    """
    audit_path = _DRAFT_DIR / "audit.py"
    tree = ast.parse(audit_path.read_text(encoding="utf-8"))
    finder = _NameCallFinder("LLMResponseEvent")
    finder.visit(tree)
    assert finder.calls, (
        "Expected LLMResponseEvent(...) call in signalforge.draft.audit — "
        "the AST-scan above is no longer load-bearing if the legitimate "
        "constructor disappears."
    )


# ---------------------------------------------------------------------------
# Scan 5 — PruneEvent only in prune.audit
# ---------------------------------------------------------------------------


# DEC-018: PruneEvent is the prune-decision audit record; constructing it
# anywhere other than the audit-write seam is a bug — the corresponding
# event will never reach disk and the prune decision becomes unauditable.
# Mirrors Scan 4 (LLMResponseEvent only in draft.audit).
_PRUNE_EVENT_EXCLUSIONS: set[str] = {
    # audit.py is the sole audit-write seam: ``_build_prune_event``
    # constructs the PruneEvent and ``_write_prune_event`` is the
    # fail-closed JSONL writer (mirrors safety/audit.py from #4 and
    # draft/audit.py from #5).
    "audit.py",
}


def test_prune_event_construction_only_in_prune_audit_module() -> None:
    """DEC-018: direct ``PruneEvent(...)`` outside
    ``signalforge.prune.audit`` bypasses the fail-closed JSONL writer —
    the event would never reach disk and the prune decision would be
    unauditable.
    """
    hits = _scan_dir_for_name_calls(
        _PRUNE_DIR,
        target="PruneEvent",
        excluded_relpaths=_PRUNE_EVENT_EXCLUSIONS,
    )
    formatted = "\n".join(f"  {p}:{line}" for p, line in hits)
    assert not hits, (
        "PruneEvent constructed outside signalforge.prune.audit:\n"
        f"{formatted}\n"
        "Construct only via _build_prune_event — direct construction "
        "bypasses the fail-closed JSONL audit writer."
    )


def test_prune_event_construction_in_prune_audit_module_is_present() -> None:
    """Sanity: at least one ``PruneEvent(...)`` in
    ``signalforge.prune.audit``. If this fails the scan above is no longer
    load-bearing.
    """
    audit_path = _PRUNE_DIR / "audit.py"
    tree = ast.parse(audit_path.read_text(encoding="utf-8"))
    finder = _NameCallFinder("PruneEvent")
    finder.visit(tree)
    assert finder.calls, (
        "Expected PruneEvent(...) call in signalforge.prune.audit — "
        "the AST-scan above is no longer load-bearing if the legitimate "
        "constructor disappears."
    )


# ---------------------------------------------------------------------------
# Scan 6 — GradeEvent only in grade.audit
# ---------------------------------------------------------------------------


# DEC-029 of #7 / US-009: GradeEvent is the grading-decision audit record;
# constructing it anywhere other than the audit-write seam is a bug — the
# corresponding event will never reach disk and the grade decision becomes
# unauditable. Mirrors Scan 5 (PruneEvent only in prune.audit).
_GRADE_EVENT_EXCLUSIONS: set[str] = {
    # audit.py is the sole audit-write seam: ``_build_grade_event``
    # constructs the GradeEvent and ``write_grade_event`` is the
    # fail-closed JSONL writer (mirrors safety/audit.py from #4,
    # draft/audit.py from #5, and prune/audit.py from #6).
    "audit.py",
}


def test_grade_event_construction_only_in_grade_audit_module() -> None:
    """DEC-029 of #7: direct ``GradeEvent(...)`` outside
    ``signalforge.grade.audit`` bypasses the fail-closed JSONL writer —
    the event would never reach disk and the grade decision would be
    unauditable.
    """
    hits = _scan_dir_for_name_calls(
        _GRADE_DIR,
        target="GradeEvent",
        excluded_relpaths=_GRADE_EVENT_EXCLUSIONS,
    )
    formatted = "\n".join(f"  {p}:{line}" for p, line in hits)
    assert not hits, (
        "GradeEvent constructed outside signalforge.grade.audit:\n"
        f"{formatted}\n"
        "Construct only via _build_grade_event — direct construction "
        "bypasses the fail-closed JSONL audit writer."
    )


def test_grade_event_construction_in_grade_audit_module_is_present() -> None:
    """Sanity: at least one ``GradeEvent(...)`` in
    ``signalforge.grade.audit``. If this fails the scan above is no longer
    load-bearing.
    """
    audit_path = _GRADE_DIR / "audit.py"
    tree = ast.parse(audit_path.read_text(encoding="utf-8"))
    finder = _NameCallFinder("GradeEvent")
    finder.visit(tree)
    assert finder.calls, (
        "Expected GradeEvent(...) call in signalforge.grade.audit — "
        "the AST-scan above is no longer load-bearing if the legitimate "
        "constructor disappears."
    )


# ---------------------------------------------------------------------------
# Scan 7 — every ``*Error`` class declared in any
# ``src/signalforge/*/errors.py`` (plus ``src/signalforge/cli/errors.py``)
# must appear in
# :data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` (DEC-024 of #9).
# ---------------------------------------------------------------------------
#
# The four-tier exit-code taxonomy (DEC-008/DEC-019) is enforced by a
# single mapping table in ``signalforge.cli._helpers``. This scan walks
# every per-stage ``errors.py`` plus the CLI's own ``errors.py``,
# collects every ``class <Name>Error(<base>):`` declaration, and asserts
# the class is registered.
#
# Scan target verified: ``grep -rln '^class.*Error' src/signalforge/
# --include='*.py'`` returns exactly the eight per-stage ``errors.py``
# files (manifest, warehouse, safety, llm, draft, prune, grade, diff)
# plus the CLI's own ``errors.py`` (nine total — DEC-024).
#
# Excluded: the abstract per-stage base each layer's leaves inherit
# from. These bases exist as the typed-error catch-all but the mapping
# table relies on Python's MRO walk in
# :func:`map_exception_to_exit_code` to resolve a forward-compat
# subclass to its parent's tier — registering the bases is *also* fine
# (and the table currently does so), but the AST scan does not require
# them to be present, only that every concrete leaf is. If v0.2 adds a
# new abstract intermediate, extend this exclude list with a comment
# citing the design note.
_EXCEPTION_MAPPING_EXCLUDED_BASES: frozenset[str] = frozenset(
    {
        "ManifestError",
        "WarehouseError",
        "SafetyError",
        "LLMError",
        # NOTE: ``LLMHelperError`` is NOT excluded — even though it lives
        # one level below ``LLMError`` in the inheritance tree, it is
        # raised directly in ``signalforge.llm.client`` (three sites as of
        # #9), so it is a concrete leaf for taxonomy purposes and must
        # appear in ``_EXCEPTION_TO_EXIT_CODE``. Treating it as an
        # excluded base would create a gap where direct instantiations
        # have no defined exit code.
        "DraftError",
        "PruneError",
        "GradeError",
        "DiffError",
        "CliError",
    }
)


def _collect_error_class_declarations(
    paths: list[Path],
) -> list[tuple[Path, str]]:
    """Walk each ``.py`` in ``paths``; return ``(file, class_name)`` for
    every ``class <Name>Error(...):`` declaration. The class name is
    just the AST-level identifier — no module-attribute resolution
    happens here (the Scan-7 test does that against the live mapping).
    """
    found: list[tuple[Path, str]] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("Error"):
                found.append((path, node.name))
    return found


def _enumerate_error_module_paths() -> list[Path]:
    """The exact set of files Scan 7 walks: every per-stage
    ``errors.py`` under ``src/signalforge/*/errors.py`` plus the CLI's
    own ``errors.py``.
    """
    # ``Path.glob`` is non-recursive on the directory level here — every
    # stage's errors module lives one level under ``signalforge/``.
    paths = sorted(_SIGNALFORGE_DIR.glob("*/errors.py"))
    # ``cli/errors.py`` is already covered by the glob above (the CLI is
    # a stage subpackage), but assert defensively in case the layout
    # changes and a contributor moves the CLI to a sibling location.
    cli_errors = _SIGNALFORGE_DIR / "cli" / "errors.py"
    assert cli_errors in paths, (
        "Expected src/signalforge/cli/errors.py to be discovered by the "
        "*/errors.py glob — Scan 7 relies on every per-stage errors "
        "module being one level under src/signalforge/."
    )
    return paths


def test_every_typed_error_is_in_exit_code_mapping_table() -> None:
    """DEC-024 of #9: every ``*Error`` class declared in any
    ``src/signalforge/*/errors.py`` must appear as a key in
    :data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`.

    The scan parses each errors module's AST, collects every
    ``class <Name>Error(...):`` declaration, and asserts the class is
    registered in the mapping. The mapping itself is class-identity
    keyed (``dict[type[BaseException], int]``); the AST sees only
    names, so the assertion compares by ``__name__`` string against the
    set of mapped class names. This is sufficient because every
    ``*Error`` declared in the project has a unique class name across
    stages — the AST scan itself enforces that a name introduced in any
    ``errors.py`` lands in the mapping, so a duplicate-name regression
    would surface as a missing-mapping failure for one of the two and
    fail loud.

    Excluded: the per-stage abstract base classes — see
    :data:`_EXCEPTION_MAPPING_EXCLUDED_BASES`. Subclasses of these
    bases inherit their tier via the MRO walk in
    :func:`map_exception_to_exit_code`, so registering the bases is
    optional for correctness; the scan only requires every concrete
    leaf to be explicitly listed.
    """
    from signalforge.cli._helpers import _EXCEPTION_TO_EXIT_CODE

    mapped_names = {cls.__name__ for cls in _EXCEPTION_TO_EXIT_CODE}
    error_paths = _enumerate_error_module_paths()
    declarations = _collect_error_class_declarations(error_paths)

    missing: list[tuple[Path, str]] = []
    for path, class_name in declarations:
        if class_name in _EXCEPTION_MAPPING_EXCLUDED_BASES:
            continue
        if class_name not in mapped_names:
            missing.append((path, class_name))

    if missing:
        formatted = "\n".join(f"  {path}: {cls}" for path, cls in missing)
        raise AssertionError(
            "The following typed exception classes are missing from "
            "signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE:\n"
            f"{formatted}\n"
            "Add each class to the mapping with the correct exit-code "
            "tier (1=load, 2=input, 3=API). See DEC-024 of "
            "plans/super/9-cli-entrypoint.md for the taxonomy and "
            ".claude/rules/cli-layer.md when it lands. The four-tier "
            "exit-code contract is load-bearing — the AST scan exists "
            "exactly to catch this drift."
        )


def test_exit_code_mapping_has_at_least_one_entry_per_tier() -> None:
    """Sanity: each of the three tiers (1, 2, 3) has at least one entry
    in :data:`_EXCEPTION_TO_EXIT_CODE`. Guards against a mass-rename
    accidentally collapsing the taxonomy to a single tier.
    """
    from signalforge.cli._helpers import _EXCEPTION_TO_EXIT_CODE

    tiers = set(_EXCEPTION_TO_EXIT_CODE.values())
    assert tiers >= {1, 2, 3}, (
        f"Expected at least one entry for each tier in {{1, 2, 3}}; got "
        f"tiers={sorted(tiers)}. The four-tier exit-code taxonomy "
        "(DEC-008/DEC-019) requires all three error tiers to be "
        "represented."
    )


def test_scan_7_discovers_every_per_stage_errors_module() -> None:
    """Sanity: ``_enumerate_error_module_paths`` finds every per-stage
    ``errors.py`` in the project. If a future stage forgets to ship
    ``errors.py`` the scan would still pass (because there'd be nothing
    to walk for that stage); this test pins the expected set of nine
    modules.
    """
    paths = _enumerate_error_module_paths()
    rel_names = sorted(p.relative_to(_SIGNALFORGE_DIR).as_posix() for p in paths)
    assert rel_names == [
        "cli/errors.py",
        "diff/errors.py",
        "draft/errors.py",
        "grade/errors.py",
        "llm/errors.py",
        "manifest/errors.py",
        "prune/errors.py",
        "safety/errors.py",
        "warehouse/errors.py",
    ], (
        "Expected exactly nine per-stage errors.py modules (one per "
        "stage); got: "
        f"{rel_names}. If this changes, update Scan 7's expected set."
    )


# ---------------------------------------------------------------------------
# Scan 8 — fail-closed writer shape across all six audit/sidecar writers
# (issue #38). Mirrors the AST defence in
# ``tests/diff/test_sidecar.py::test_sidecar_module_no_except_handler_around_write_fsync``
# but generalises it to every writer module: no ``except`` handler may wrap a
# ``Try`` block whose body issues ``os.write`` / ``os.fsync``. The propagation
# IS the defence (safety-layer.md DEC-011, repeated in prune/grade/diff rules).
# ---------------------------------------------------------------------------


# Each entry is ``(relative_module_path, expected_writer_count)`` —
# ``expected_writer_count`` is the number of writer functions in the
# module. One writer function contributes exactly one canonical
# ``Try`` block (``try / finally`` around ``os.close(fd)``) and exactly
# one ``While`` loop wrapping ``os.write``, so the same count is used
# by both Scan 8 tests. ``grade/audit.py`` is the only module with
# more than one writer (``write_grade_event`` + ``write_grading_report``).
_FAIL_CLOSED_WRITER_MODULES: tuple[tuple[str, int], ...] = (
    ("safety/audit.py", 1),
    ("draft/audit.py", 1),
    ("prune/audit.py", 1),
    ("grade/audit.py", 2),
    ("diff/_sidecar.py", 1),
)


def _body_calls_os_syscall(body: list[ast.stmt], names: tuple[str, ...]) -> bool:
    """Return True iff any node anywhere under ``body`` issues an
    ``os.<name>`` call (``os.write``, ``os.fsync``).

    Walks the full AST under each statement in ``body`` (via
    :func:`ast.walk`), so nested calls inside an ``if`` / ``while`` /
    ``try`` body also match — that's load-bearing for Scan 8 because the
    canonical writer wraps ``os.write`` inside a ``while`` loop AND the
    ``Try`` block guards both ``os.write`` (inside the loop) and a
    sibling ``os.fsync``. A shallow scan would miss the wrapped
    ``os.write`` and undercount the syscalls.
    """
    for node in body:
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "os"
                and sub.func.attr in names
            ):
                return True
    return False


def test_fail_closed_writers_have_no_except_around_write_fsync() -> None:
    """Issue #38 — every fail-closed writer module must propagate raw
    exceptions from ``os.write`` / ``os.fsync``. Any ``ast.Try`` whose body
    issues an ``os.write`` or ``os.fsync`` call must have ``handlers == []``
    (only ``finally`` is permitted, for the descriptor release).

    A ``try / except OSError`` around the syscalls would silently swallow
    the exact failure mode the fail-closed pattern exists to surface. The
    typed wrap belongs at the orchestrator boundary
    (``build_llm_request``, ``draft_from_request``, ``prune_tests``,
    ``grade_artifacts``, ``render_diff``), not inside the writer.

    Generalises the single-module scan from
    ``tests/diff/test_sidecar.py::test_sidecar_module_no_except_handler_around_write_fsync``
    to all five writer modules (six writer functions across them).
    """
    syscall_names = ("write", "fsync")
    failures: list[str] = []
    for rel_path, expected_writer_count in _FAIL_CLOSED_WRITER_MODULES:
        module_path = _SIGNALFORGE_DIR / rel_path
        tree = ast.parse(module_path.read_text(encoding="utf-8"))

        offending: list[int] = []
        syscall_try_count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if _body_calls_os_syscall(node.body, syscall_names):
                syscall_try_count += 1
                if node.handlers:
                    offending.append(node.lineno)

        # One canonical ``Try`` (``try / finally`` around ``os.close``)
        # per writer function in the module.
        if syscall_try_count != expected_writer_count:
            failures.append(
                f"{rel_path}: expected {expected_writer_count} Try block(s) "
                f"guarding os.write/os.fsync (one per writer function); "
                f"found {syscall_try_count}."
            )
        if offending:
            failures.append(
                f"{rel_path}: found except-handler(s) around os.write/os.fsync at "
                f"line(s) {offending}. The fail-closed contract requires "
                f"propagation, not suppression — only `try / finally` for "
                f"os.close is permitted around the syscalls."
            )

    assert failures == [], "\n".join(failures)


def test_fail_closed_writers_use_short_write_loop() -> None:
    """Issue #38 — every fail-closed writer must loop on ``os.write``
    returns to recover from short writes (``EINTR`` on signal-interrupted
    calls; short returns on some filesystems / kernels). A single
    unlooped ``os.write`` can theoretically produce a partial JSONL
    record under signal-interruption load.

    Detection heuristic: an ``ast.While`` whose body issues an
    ``os.write`` call. Each writer module must contain at least one
    such ``While`` block. The ``grade/audit.py`` module has two writer
    functions and contains two such blocks.
    """
    failures: list[str] = []
    for rel_path, expected_writer_count in _FAIL_CLOSED_WRITER_MODULES:
        module_path = _SIGNALFORGE_DIR / rel_path
        tree = ast.parse(module_path.read_text(encoding="utf-8"))

        looped_write_count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.While):
                continue
            if _body_calls_os_syscall(node.body, ("write",)):
                looped_write_count += 1

        # One short-write ``While`` loop per writer function.
        if looped_write_count < expected_writer_count:
            failures.append(
                f"{rel_path}: expected at least {expected_writer_count} short-write "
                f"loop(s) (one per writer function); found {looped_write_count}. "
                f"A single unlooped os.write can produce a partial JSONL record "
                f"under EINTR / short-write conditions."
            )

    assert failures == [], "\n".join(failures)


# ---------------------------------------------------------------------------
# Negative test: confirm the AST visitors detect planted violations
# ---------------------------------------------------------------------------


def test_scan_visitors_catch_planted_violations() -> None:
    """Self-check: feed each visitor a synthetic source string with a
    planted construction call and confirm the call is detected. Without
    this we'd not notice if a refactor broke the visitors silently.
    """
    name_src = "def make():\n    return AuditEvent(timestamp=None)\n"
    name_finder = _NameCallFinder("AuditEvent")
    name_finder.visit(ast.parse(name_src))
    assert len(name_finder.calls) == 1

    attr_src = "import anthropic\n\nx = anthropic.Anthropic(api_key='x')\n"
    attr_finder = _AttributeCallFinder("anthropic", "Anthropic")
    attr_finder.visit(ast.parse(attr_src))
    assert len(attr_finder.calls) == 1

    response_src = "def make():\n    return LLMResponseEvent(model='x')\n"
    response_finder = _NameCallFinder("LLMResponseEvent")
    response_finder.visit(ast.parse(response_src))
    assert len(response_finder.calls) == 1

    prune_src = "def make():\n    return PruneEvent(model_unique_id='x')\n"
    prune_finder = _NameCallFinder("PruneEvent")
    prune_finder.visit(ast.parse(prune_src))
    assert len(prune_finder.calls) == 1

    grade_src = "def make():\n    return GradeEvent(model_unique_id='x')\n"
    grade_finder = _NameCallFinder("GradeEvent")
    grade_finder.visit(ast.parse(grade_src))
    assert len(grade_finder.calls) == 1

    # Scan 8 self-check: a synthetic Try-with-except around os.write must
    # be flagged. Guards against a future refactor that breaks
    # ``_body_calls_os_syscall`` (e.g. via aliased imports) silently.
    bad_src = (
        "import os\n"
        "def w(fd, data):\n"
        "    try:\n"
        "        os.write(fd, data)\n"
        "        os.fsync(fd)\n"
        "    except OSError:\n"
        "        pass\n"
    )
    bad_tree = ast.parse(bad_src)
    bad_count = 0
    for node in ast.walk(bad_tree):
        if isinstance(node, ast.Try) and _body_calls_os_syscall(node.body, ("write", "fsync")):
            assert node.handlers != [], "Self-check: planted Try should have an except handler"
            bad_count += 1
    assert bad_count == 1, "Self-check: should detect exactly one offending Try"

    good_src = (
        "import os\n"
        "def w(fd, data):\n"
        "    try:\n"
        "        os.write(fd, data)\n"
        "        os.fsync(fd)\n"
        "    finally:\n"
        "        os.close(fd)\n"
    )
    good_tree = ast.parse(good_src)
    good_count = 0
    for node in ast.walk(good_tree):
        if isinstance(node, ast.Try) and _body_calls_os_syscall(node.body, ("write", "fsync")):
            assert node.handlers == [], "Self-check: canonical Try has only finally"
            good_count += 1
    assert good_count == 1, "Self-check: should find exactly one canonical Try"
