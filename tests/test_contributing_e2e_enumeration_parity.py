"""Parity gate: ``CONTRIBUTING.md`` enumerates every paid e2e test file.

Issue #157 US-004 (DEC-007) — gate-over-prompt, mirrors the 5-surface parity
precedent in ``tests/cli/test_5_surface_parity_*.py``
(``.claude/rules/cli-layer.md`` § "Multi-surface parity for behaviour
changes"). The historic gap this fixes: ``tests/cli/test_e2e_business_rules.py``
is ``@pytest.mark.e2e``-marked but was silently absent from CONTRIBUTING's
enumeration of the paid e2e suite — exactly the "doc drift" failure mode
the gate-over-prompt convention exists to prevent.

The test reads ``CONTRIBUTING.md`` once and asserts:

1. Every paid e2e test-file basename appears verbatim in the doc. A new
   ``test_e2e_*.py`` that the maintainer forgets to enumerate fails loud.
2. The recommended parallel-execution invocation ``pytest -m e2e -n 3
   --no-cov`` is documented (issue #157 US-004 DEC-001 / DEC-003).
3. The Anthropic 50-RPM rate-limit caveat is anchored in the prose (so
   a future edit can't silently strip it).
4. The pointer to ``scripts/measure_e2e_cost.py`` (US-003) survives.

Runs in the default suite — no marker. The grep is cheap.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRIBUTING = _REPO_ROOT / "CONTRIBUTING.md"

# Every paid e2e test file. Snowflake is included even though it carries
# ``@pytest.mark.snowflake`` (not ``e2e``) — it is the other-warehouse paid
# pipeline test and belongs in the enumeration alongside the four
# ``@pytest.mark.e2e``-marked siblings.
_PAID_E2E_FILES = (
    "test_e2e_bigquery_smoke.py",
    "test_e2e_openai_smoke.py",
    "test_e2e_gemini_smoke.py",
    "test_e2e_snowflake_smoke.py",
    "test_e2e_business_rules.py",
)

# The recommended parallel invocation. Documented verbatim in CONTRIBUTING's
# "Parallel execution (recommended)" subsection (issue #157 US-004).
_PARALLEL_INVOCATION = "pytest -m e2e -n 3 --no-cov"

# Anchor phrases for the Anthropic rate-limit caveat. Both must co-occur in
# CONTRIBUTING — the caveat is load-bearing for operators running with -n 3.
_RATE_LIMIT_ANCHORS = ("Anthropic", "rate limit")

# Pointer to the US-003 cost-rollup helper.
_COST_HELPER_POINTER = "scripts/measure_e2e_cost.py"


def _read_contributing() -> str:
    return _CONTRIBUTING.read_text(encoding="utf-8")


def test_contributing_enumerates_every_paid_e2e_test_file() -> None:
    """Every paid e2e test file basename appears in ``CONTRIBUTING.md``.

    Prevents the doc gap that motivated US-004: a new
    ``tests/cli/test_e2e_*.py`` (or one that already exists but was missed)
    must be enumerated in CONTRIBUTING § "Tests in the live e2e suite" so
    the pre-release maintainer sees it.
    """
    content = _read_contributing()
    missing = [name for name in _PAID_E2E_FILES if name not in content]
    assert not missing, (
        f"CONTRIBUTING.md is missing enumeration of paid e2e test file(s): {missing}. "
        f"Add an entry under '## Live e2e suite (pre-release only)' → "
        f"'### Tests in the live e2e suite'."
    )


def test_contributing_documents_parallel_invocation() -> None:
    """``pytest -m e2e -n 3 --no-cov`` appears verbatim in CONTRIBUTING.

    Pins the recommended invocation surface from issue #157 US-004
    (DEC-001 + DEC-003). A future doc edit that drops or rewords the
    `-n 3` recommendation fails loud here.
    """
    content = _read_contributing()
    assert _PARALLEL_INVOCATION in content, (
        f"CONTRIBUTING.md is missing the recommended parallel invocation "
        f"'{_PARALLEL_INVOCATION}'. Document it under '### Parallel execution "
        f"(recommended)' per #157 US-004."
    )


def test_contributing_documents_anthropic_rate_limit_caveat() -> None:
    """Anthropic rate-limit caveat is anchored in the parallel-execution prose.

    With ``-n 3`` concurrent paid e2e tests can collectively breach
    Anthropic's per-minute quota and trigger the WARNING retry path. The
    caveat must survive doc edits.
    """
    content = _read_contributing()
    missing_anchors = [phrase for phrase in _RATE_LIMIT_ANCHORS if phrase not in content]
    assert not missing_anchors, (
        f"CONTRIBUTING.md is missing rate-limit caveat anchor(s) {missing_anchors}. "
        f"The Anthropic-rate-limit warning is load-bearing for operators "
        f"running with `-n 3` (#157 US-004)."
    )


def test_contributing_points_at_cost_rollup_helper() -> None:
    """Pointer to ``scripts/measure_e2e_cost.py`` (US-003) survives doc edits."""
    content = _read_contributing()
    assert _COST_HELPER_POINTER in content, (
        f"CONTRIBUTING.md is missing the pointer to '{_COST_HELPER_POINTER}'. "
        f"The cost-rollup helper from #157 US-003 should be cross-referenced "
        f"from the parallel-execution subsection."
    )
