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

**Cost-baseline parity (added by issue #157 US-006).** The measured
2026-05-29 baseline ($1.38/full-suite run at pricing-table version
``2026-05-28``) is quoted across three doc surfaces — ``CONTRIBUTING.md``,
``plans/super/155-gemini-truncation-e2e-gap.md``, and ``docs/grade-ops.md``.
This gate prevents future doc drift in the cost figures across those three
surfaces: every surface must carry the ``2026-05-28`` pricing-table version
stamp (the byte-stable cross-surface anchor — the per-provider/per-model
USD figures differ in shape between surfaces), and the ``1.38`` full-suite
headline must appear verbatim in CONTRIBUTING + the ``155-…md`` plan
(``docs/grade-ops.md`` quotes per-model figures and links to the full-suite
total instead, so the parity assertion is scoped accordingly). Planted-violation
self-check: temporarily change ``2026-05-28`` to ``2026-99-99`` in any one
surface; the gate fires with the missing-marker name.

Runs in the default suite — no marker. The grep is cheap.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRIBUTING = _REPO_ROOT / "CONTRIBUTING.md"

# Cost-baseline parity surfaces (issue #157 US-006). All three quote the
# measured 2026-05-29 baseline; the ``2026-05-28`` pricing-table version
# is the byte-stable cross-surface anchor.
_PLAN_155 = _REPO_ROOT / "plans" / "super" / "155-gemini-truncation-e2e-gap.md"
_GRADE_OPS = _REPO_ROOT / "docs" / "grade-ops.md"
_COST_BASELINE_SURFACES = (_CONTRIBUTING, _PLAN_155, _GRADE_OPS)

# Pricing-table version stamp the measurement was taken at. Byte-stable
# across all three surfaces — the right cross-surface anchor.
_PRICING_TABLE_VERSION_STAMP = "2026-05-28"

# Full-suite headline figure. Quoted as ``$1.38`` or bare ``1.38`` in
# CONTRIBUTING + the 155-...md plan; docs/grade-ops.md quotes per-model
# figures instead and links to the full-suite rollup.
_FULL_SUITE_HEADLINE = "1.38"
_FULL_SUITE_HEADLINE_SURFACES = (_CONTRIBUTING, _PLAN_155)

# "Calibration signal, not a billing guarantee" framing (mirrors the
# warehouse-adapters.md precedent). All three cost-baseline surfaces should
# carry this verbatim.
_CALIBRATION_FRAMING = "calibration signal, not a billing guarantee"

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


def test_cost_baseline_pricing_table_version_stamp_appears_across_all_three_surfaces() -> None:
    """The ``2026-05-28`` pricing-table version stamp appears across all three surfaces.

    Issue #157 US-006 lifted the measured 2026-05-29 baseline into
    ``CONTRIBUTING.md``, ``plans/super/155-gemini-truncation-e2e-gap.md``,
    and ``docs/grade-ops.md``. The pricing-table version stamp is the
    byte-stable cross-surface anchor (per-provider USD figures differ in
    shape between surfaces — full-suite total in CONTRIBUTING + 155-...md;
    per-provider per-model in grade-ops). A future doc edit that rotates
    the stamp on one surface without the other two fails loud here.
    """
    missing = []
    for surface in _COST_BASELINE_SURFACES:
        content = surface.read_text(encoding="utf-8")
        if _PRICING_TABLE_VERSION_STAMP not in content:
            missing.append(str(surface.relative_to(_REPO_ROOT)))
    assert not missing, (
        f"Cost-baseline surface(s) missing pricing-table version stamp "
        f"'{_PRICING_TABLE_VERSION_STAMP}': {missing}. All three surfaces "
        f"(CONTRIBUTING.md, plans/super/155-gemini-truncation-e2e-gap.md, "
        f"docs/grade-ops.md) must quote the same pricing-table version per "
        f"issue #157 US-006."
    )


def test_cost_baseline_full_suite_headline_appears_in_contributing_and_plan_155() -> None:
    """The ``1.38`` full-suite headline appears in CONTRIBUTING + the 155-…md plan.

    Scoped to two surfaces (not all three) because ``docs/grade-ops.md``
    deliberately quotes **per-model** figures ($0.38 / $0.21 / $0.045) and
    links to the full-suite rollup rather than restating it. CONTRIBUTING
    and the 155-…md DEC-010 / architecture-review row both reference the
    full-suite total directly.
    """
    missing = []
    for surface in _FULL_SUITE_HEADLINE_SURFACES:
        content = surface.read_text(encoding="utf-8")
        if _FULL_SUITE_HEADLINE not in content:
            missing.append(str(surface.relative_to(_REPO_ROOT)))
    assert not missing, (
        f"Cost-baseline surface(s) missing full-suite headline "
        f"'{_FULL_SUITE_HEADLINE}': {missing}. CONTRIBUTING.md + "
        f"plans/super/155-gemini-truncation-e2e-gap.md must both quote the "
        f"~$1.38 full-suite total per issue #157 US-006."
    )


def test_cost_baseline_calibration_framing_appears_across_all_three_surfaces() -> None:
    """The calibration-framing phrase appears verbatim across all three surfaces.

    Mirrors the ``warehouse-adapters.md`` § "Cleanup-boundary fail-soft"
    framing precedent — vendor figures are calibration, not contractual.
    The phrase MUST appear verbatim (copy-pasteable) on all three
    cost-baseline surfaces so a future doc edit that softens the caveat
    on one surface without the others fails loud here.
    """
    missing = []
    for surface in _COST_BASELINE_SURFACES:
        content = surface.read_text(encoding="utf-8")
        if _CALIBRATION_FRAMING not in content:
            missing.append(str(surface.relative_to(_REPO_ROOT)))
    assert not missing, (
        f"Cost-baseline surface(s) missing calibration framing "
        f"'{_CALIBRATION_FRAMING}': {missing}. All three surfaces must "
        f"carry the verbatim phrase per issue #157 US-006 (mirrors the "
        f"warehouse-adapters.md fail-soft framing precedent)."
    )
