"""US-002 (#121) DEC-008 — `prune/` warehouse-SDK import confinement.

The prune layer is warehouse-agnostic *by construction*: it emits dialect-
correct SQL purely from the :class:`signalforge.warehouse.models.Dialect`
value object, never by importing a warehouse SDK or branching on a dialect
*name* (DEC-025). This AST-based scan enforces that no ``import snowflake`` /
``from snowflake`` / ``import google.cloud`` / ``from google.cloud``
statement appears anywhere under ``src/signalforge/prune/`` — a regression
that reached for a vendor SDK in the compiler would break the seam silently.

AST-based (not per-line regex) per ``testing-signal.md`` § "Source-scan
gates: AST over per-line regex": a multi-line / parenthesised / aliased
import is normalised to the same ``ast.Import`` / ``ast.ImportFrom`` node, so
the visitor cannot be bypassed by reformatting. A planted-violation
self-check proves the detector can actually fail.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PRUNE_DIR = Path(__file__).resolve().parents[2] / "src" / "signalforge" / "prune"

# Module prefixes that must never be imported under prune/. A module name
# matches when it equals the prefix or begins with ``<prefix>.`` (so
# ``google.cloud.bigquery`` matches ``google.cloud`` but ``googleftover``
# does not).
_FORBIDDEN_PREFIXES = ("snowflake", "google.cloud")


def _module_matches_forbidden(module: str | None) -> bool:
    if module is None:
        return False
    for prefix in _FORBIDDEN_PREFIXES:
        if module == prefix or module.startswith(prefix + "."):
            return True
    return False


def _forbidden_imports(source: str) -> list[str]:
    """Return a list of forbidden-import descriptions found in ``source``.

    Catches both ``import X`` / ``import X as Y`` (``ast.Import``) and
    ``from X import Y`` (``ast.ImportFrom``) forms, including relative-free
    dotted ``from google.cloud import bigquery`` AND the namespace-split
    ``from google import cloud`` form (where ``node.module`` is only a
    prefix-ancestor of the forbidden module and the imported *name* completes
    it).
    """
    hits: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_matches_forbidden(alias.name):
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # ``node.module`` is None for ``from . import x`` (relative);
            # such an import cannot name a forbidden vendor module, and
            # ``_module_matches_forbidden(None)`` short-circuits to False.
            if _module_matches_forbidden(node.module):
                names = ", ".join(a.name for a in node.names)
                hits.append(f"from {node.module} import {names}")
            elif node.module is not None:
                # Namespace-split: ``from google import cloud`` has
                # module="google" (not itself forbidden) but the imported
                # name "cloud" completes the forbidden ``google.cloud``.
                for alias in node.names:
                    if _module_matches_forbidden(f"{node.module}.{alias.name}"):
                        hits.append(f"from {node.module} import {alias.name}")
    return hits


def test_no_warehouse_sdk_import_under_prune() -> None:
    """No ``snowflake`` / ``google.cloud`` import anywhere under prune/."""
    offenders: list[str] = []
    for py in sorted(_PRUNE_DIR.rglob("*.py")):
        source = py.read_text(encoding="utf-8")
        for desc in _forbidden_imports(source):
            offenders.append(f"{py.relative_to(_PRUNE_DIR)}: {desc}")

    assert not offenders, (
        "signalforge/prune/ must stay warehouse-SDK-agnostic (DEC-008 of #121); "
        "found forbidden import(s):\n" + "\n".join(offenders)
    )


def test_detector_flags_planted_violations() -> None:
    """Self-check: the detector flags every forbidden import form.

    Without this, a refactor that broke the AST visitor would silently
    disable the guard at the moment a real violation needed catching.
    """
    planted = (
        "import snowflake\n"
        "import snowflake.connector\n"
        "import snowflake.connector as sf\n"
        "from snowflake import connector\n"
        "from snowflake.connector import connect\n"
        "import google.cloud\n"
        "from google.cloud import bigquery\n"
        "import google.cloud.bigquery as bq\n"
        # namespace-split: module='google', imported name completes 'google.cloud'
        "from google import cloud\n"
    )
    hits = _forbidden_imports(planted)
    # Every line above is a violation — 9 statements.
    assert len(hits) == 9, hits


def test_detector_does_not_false_positive() -> None:
    """Innocent imports and same-prefix-but-distinct module names are NOT
    flagged (the prefix match is boundary-anchored on a dotted component)."""
    innocent = (
        "import os\n"
        "from pathlib import Path\n"
        "import googleapiclient\n"  # starts with 'google' but not 'google.cloud'
        "from snowflakeish import thing\n"  # starts with 'snowflake' but not 'snowflake.'
        "from . import compiler\n"  # relative import, module is None
    )
    assert _forbidden_imports(innocent) == []
