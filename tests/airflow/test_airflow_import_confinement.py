"""US-004 (#230) DEC-008 — Apache Airflow import / type-ignore confinement.

Every ``from airflow ...`` / ``import airflow`` statement AND every
``airflow``-mentioning ``# type: ignore`` / ``# pyright: ignore`` directive in
the :mod:`signalforge.airflow` package must live ONLY in
``_airflow_compat.py`` — the one-shim-per-vendor seam (DEC-007). Mirrors the
spirit of ``tests/warehouse/test_snowflake_client_confinement.py`` and the
Anthropic / OpenAI / Gemini SDK confinement scans.

**This test is UNGATED (DEC-009).** It carries NO ``airflow`` pytest marker and
NEVER imports the real ``apache-airflow`` package — it reads source bytes only.
It runs in the default ``uv run pytest`` suite so the "core stays lean" seam is
enforced even with Airflow not installed.

Why a hybrid AST + ``tokenize`` scan rather than a plain line-scan (the
snowflake precedent's shape)? Two airflow-specific false-positive traps that a
lowercased substring scan would trip on, both load-bearing:

* The package itself is named ``signalforge.airflow`` — every legitimate
  intra-package import (``from signalforge.airflow.errors import ...``) mentions
  "airflow". An AST walk over :class:`ast.Import` / :class:`ast.ImportFrom`
  checks the *resolved module name* (``airflow`` / ``airflow.*`` vs.
  ``signalforge.airflow.*``), so the package path never false-positives.
* The skeleton's docstrings quote ````from airflow ...```` and
  ````# type: ignore```` as RST prose (e.g. ``__init__.py`` explains the seam).
  ``tokenize`` only yields real :data:`tokenize.COMMENT` tokens, so docstring
  text is never mistaken for a directive.

This is the AST-over-per-line-regex discipline ``testing-signal.md`` mandates
for "no X in module Y" source-scan gates. The mandatory planted-violation
self-check below drives the *same* helper so a refactor that breaks the scan
fails loud.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

_AIRFLOW_PKG_DIR = Path(__file__).resolve().parents[2] / "src" / "signalforge" / "airflow"
_SHIM_FILENAME = "_airflow_compat.py"


def _airflow_seam_lines(source: str) -> list[tuple[int, str]]:
    """Return sorted ``(lineno, stripped_text)`` for every airflow-seam line.

    A "seam line" is either:

    * a real ``import airflow`` / ``from airflow[...] import ...`` statement
      (detected via AST so ``signalforge.airflow.*`` package-path imports and
      docstring prose never match), OR
    * a ``# type: ignore`` / ``# pyright: ignore`` *comment* (via ``tokenize``,
      so docstring mentions are excluded) on a line that also mentions
      "airflow".

    Pure function over source text so the planted-violation self-check can drive
    it directly without touching disk.
    """
    lines = source.splitlines()
    hits: dict[int, str] = {}

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "airflow" or alias.name.startswith("airflow."):
                    hits[node.lineno] = lines[node.lineno - 1].strip()
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "airflow" or module.startswith("airflow."):
                hits[node.lineno] = lines[node.lineno - 1].strip()

    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            continue
        lowered_comment = tok.string.lower()
        if "type: ignore" not in lowered_comment and "pyright: ignore" not in lowered_comment:
            continue
        line_text = lines[tok.start[0] - 1]
        # Gate on the COMMENT token, not the full line: a benign
        # ``from signalforge.airflow ...  # type: ignore[...]`` mentions "airflow"
        # in the (import) code, not in the comment — that import is caught (or
        # correctly ignored as intra-package) by the AST branch above. This
        # branch exists only to catch an airflow-mentioning type-ignore that is
        # NOT on an import line, so the comment payload must mention airflow.
        if "airflow" in lowered_comment:
            hits[tok.start[0]] = line_text.strip()

    return sorted(hits.items())


def test_airflow_imports_confined_to_shim() -> None:
    """No ``.py`` under ``signalforge/airflow/`` other than the shim may carry
    an airflow import or an airflow-mentioning type/pyright ignore.
    """
    offenders: list[str] = []
    for py in sorted(_AIRFLOW_PKG_DIR.glob("*.py")):
        if py.name == _SHIM_FILENAME:
            continue
        for lineno, text in _airflow_seam_lines(py.read_text(encoding="utf-8")):
            offenders.append(f"{py.name}:{lineno}: {text}")

    assert not offenders, (
        "Apache Airflow imports / type-ignores must live only in "
        f"{_SHIM_FILENAME} (one-shim-per-vendor, DEC-007), but found:\n" + "\n".join(offenders)
    )


def test_shim_actually_carries_airflow_seam() -> None:
    """Sanity: the shim itself DOES carry at least one airflow seam line.

    Without this, the confinement scan above could pass vacuously after a
    refactor that dropped the seam (the ``testing-signal.md`` "shim carries the
    seam" requirement).
    """
    shim = _AIRFLOW_PKG_DIR / _SHIM_FILENAME
    assert _airflow_seam_lines(shim.read_text(encoding="utf-8")), (
        f"{_SHIM_FILENAME} should confine the Apache Airflow import/type-ignore "
        "seam; the confinement scan is only meaningful if the seam exists"
    )


def test_scan_flags_planted_import_violation() -> None:
    """Planted-violation self-check: a synthetic module with an airflow import
    OUTSIDE the shim is flagged by the same helper the real gate uses.

    Mandatory per ``testing-signal.md`` § "Source-scan gates" — without it, a
    refactor that broke the scan visitor would silently disable the gate at the
    exact moment a real violation needed catching.
    """
    planted = (
        '"""A stub operator module that wrongly imports airflow at module scope."""\n'
        "from __future__ import annotations\n"
        "\n"
        # Deliberately NO `# type: ignore` comment here: this planted line must be
        # caught by the AST import-detection branch ALONE, so a regression isolated
        # to that branch fails this test. The comment-scan branch is pinned
        # independently by test_scan_flags_planted_type_ignore_violation.
        "from airflow.models import BaseOperator\n"
        "\n"
        "\n"
        "class Bad(BaseOperator):\n"
        "    pass\n"
    )
    hits = _airflow_seam_lines(planted)
    assert hits, "planted `from airflow.models import BaseOperator` must be flagged"
    flagged_text = " ".join(text for _, text in hits)
    assert "airflow" in flagged_text.lower()


def test_scan_flags_planted_type_ignore_violation() -> None:
    """Planted-violation self-check (comment path): an airflow-mentioning
    ``# type: ignore`` comment NOT on an import line is still flagged.
    """
    planted = (
        "from __future__ import annotations\n"
        "\n"
        "from signalforge.airflow._airflow_compat import make_base_operator\n"
        "\n"
        "base = make_base_operator()  # type: ignore[misc]  # airflow base class\n"
    )
    hits = _airflow_seam_lines(planted)
    assert hits, "planted airflow-mentioning `# type: ignore` comment must be flagged"


def test_scan_ignores_package_path_and_docstrings() -> None:
    """The helper must NOT flag the two airflow-specific false-positive traps.

    Pins the exact discrimination the real ``signalforge.airflow`` modules rely
    on: intra-package ``signalforge.airflow.*`` imports and docstring prose that
    merely quotes ``from airflow ...`` / ``# type: ignore`` are legitimate and
    must stay un-flagged.
    """
    clean = (
        '"""Docstring that quotes a ``from airflow ...`` import and a\n'
        '``# type: ignore`` directive as prose — neither is a real statement."""\n'
        "from __future__ import annotations\n"
        "\n"
        "from signalforge.airflow.errors import AirflowConfigError\n"
        "from signalforge.airflow.hooks import SignalForgeHook\n"
        "\n"
        "_ = (AirflowConfigError, SignalForgeHook)\n"
    )
    assert _airflow_seam_lines(clean) == [], (
        "the scan false-positived on a `signalforge.airflow.*` package-path "
        "import or on docstring prose mentioning airflow"
    )
