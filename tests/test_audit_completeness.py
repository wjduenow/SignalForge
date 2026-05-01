"""Top-level audit-completeness AST scans (US-014 / DEC-013).

Walks Python source via :mod:`ast` and rejects forbidden construction
patterns that would bypass the audit-write seam in each layer. The
existing :func:`tests.safety.test_public_api.test_llm_request_construction_only_in_request_module`
covers Scan 1 (``LLMRequest`` outside ``signalforge.safety.request``);
this module adds the three remaining scans:

* **Scan 2** — ``AuditEvent(...)`` outside ``signalforge.safety.request``.
* **Scan 3** — ``anthropic.Anthropic(...)`` outside
  ``signalforge.llm._client``.
* **Scan 4** — ``LLMResponseEvent(...)`` outside
  ``signalforge.draft.audit``.

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
