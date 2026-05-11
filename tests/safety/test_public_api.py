"""Public-API enforcement.

DEC-001: signalforge.safety re-exports the documented surface; underscore-
prefixed helpers stay reachable via dotted import only.

DEC-020(a): LLMRequest is constructed only via build_llm_request — direct
construction bypasses the audit log. AST-scan all safety modules except
request.py and assert no Call(func=Name(id='LLMRequest')) appears.
"""

from __future__ import annotations

import ast
from pathlib import Path

import signalforge.safety as safety_pkg

_DOCUMENTED_PUBLIC = (
    # Models
    "SamplingMode",
    "RedactionReason",
    "RedactionRecord",
    "AuditEvent",
    "LLMRequest",
    "SafetyPolicy",
    # Functions
    "load_safety_config",
    "build_llm_request",
    "aggregate_columns",
    "redact_rows",
    # Errors
    "SafetyError",
    "ConfigNotFoundError",
    "InvalidConfigError",
    "InvalidSamplingModeError",
    "InvalidPatternError",
    "ColumnNotInModelError",
    "AuditWriteError",
    "AuditRecordTooLargeError",
    "PolicyValidationError",
    "UnknownConfigKeyError",
)


def test_documented_surface_importable_from_package_root():
    for name in _DOCUMENTED_PUBLIC:
        assert hasattr(safety_pkg, name), f"signalforge.safety is missing {name!r}"


def test_all_lists_documented_surface():
    assert sorted(safety_pkg.__all__) == sorted(_DOCUMENTED_PUBLIC), (
        "signalforge.safety.__all__ does not match the documented surface"
    )


def test_private_helpers_not_in_dir():
    """Underscore-prefixed helpers are reachable via dotted import but not in dir().

    Note: ``_path_safety`` is checked separately — Python attaches imported
    submodules to their parent package's namespace regardless of what
    ``__init__.py`` does, so we assert it stays out of ``__all__`` (the
    ``from package import *`` surface) instead.
    """
    public = set(dir(safety_pkg))
    forbidden = {
        "_classify_column",
        "_compute_policy_hash",
        "_resolve_redact_patterns",
    }
    leaked = forbidden & public
    assert not leaked, f"private helpers leaked into public surface: {leaked}"
    assert "_path_safety" not in safety_pkg.__all__, (
        "_path_safety leaked into signalforge.safety.__all__"
    )


def test_classify_column_reachable_via_dotted_import():
    from signalforge.safety.redact import _classify_column  # noqa: F401


def test_compute_policy_hash_reachable_via_dotted_import():
    from signalforge.safety.policy import _compute_policy_hash  # noqa: F401


# --- AST audit-completeness scan ---


_SAFETY_DIR = Path("src/signalforge/safety")


class _LLMRequestCallFinder(ast.NodeVisitor):
    """Records every Call(func=Name(id='LLMRequest')) in the visited tree."""

    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    def visit_Call(self, node):  # noqa: N802
        if isinstance(node.func, ast.Name) and node.func.id == "LLMRequest":
            self.calls.append((node.lineno, node.col_offset))
        self.generic_visit(node)


def _collect_llm_request_calls_outside_request_module() -> list[tuple[Path, int]]:
    hits: list[tuple[Path, int]] = []
    for path in _SAFETY_DIR.rglob("*.py"):
        if path.name == "request.py":
            continue
        if path.name.startswith("_"):
            # Private helpers may legitimately not call LLMRequest, but check anyway
            pass
        tree = ast.parse(path.read_text(encoding="utf-8"))
        finder = _LLMRequestCallFinder()
        finder.visit(tree)
        for line, _col in finder.calls:
            hits.append((path, line))
    return hits


def test_llm_request_construction_only_in_request_module():
    """DEC-020(a): direct LLMRequest(...) construction outside request.py bypasses
    the audit log. AST-scan rejects any other location."""
    hits = _collect_llm_request_calls_outside_request_module()
    formatted = "\n".join(f"  {p}:{line}" for p, line in hits)
    assert not hits, (
        "LLMRequest constructed outside signalforge.safety.request:\n"
        f"{formatted}\n"
        "Construct only via build_llm_request — direct construction bypasses the audit log."
    )


def test_llm_request_construction_in_request_module_is_present():
    """Sanity: confirm at least one LLMRequest(...) call exists in request.py.
    If this fails, the AST-scan above is no longer meaningful."""
    request_path = _SAFETY_DIR / "request.py"
    tree = ast.parse(request_path.read_text(encoding="utf-8"))
    finder = _LLMRequestCallFinder()
    finder.visit(tree)
    assert finder.calls, (
        "Expected LLMRequest(...) call in signalforge.safety.request.py — "
        "the AST-scan test in test_llm_request_construction_only_in_request_module "
        "is no longer load-bearing if the legitimate constructor disappears."
    )


def test_llm_request_construction_negative_planted_violation_is_caught():
    """Negative test: a planted LLMRequest call in a string-fed AST tree is caught."""
    src = """
from signalforge.safety.models import LLMRequest
def make():
    return LLMRequest(
        model_unique_id="x",
        mode="schema-only",
        columns_sent=(),
        redactions=(),
        schema=(),
    )
"""
    tree = ast.parse(src)
    finder = _LLMRequestCallFinder()
    finder.visit(tree)
    assert len(finder.calls) == 1, "AST visitor failed to detect planted construction"
