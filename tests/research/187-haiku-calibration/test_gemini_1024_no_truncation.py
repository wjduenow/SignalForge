"""Gemini @ 1024-token cap does NOT truncate-degrade (#187 US-005 / DEC-004).

DEC-004 of the #187 plan defends ``max_output_tokens=1024`` as the new
grade-config default partly on the claim that **1024 is enough headroom
to prevent Gemini truncation** on a verbose artifact (Gemini's judge
responses run longer than Anthropic's for the same rubric). This gated
check verifies that claim empirically: grade a deliberately verbose
artifact with ``provider="gemini", model="gemini-2.5-flash"`` under the
new ``max_output_tokens=1024`` default and assert the result is **not
degraded** — i.e. the judge produced a clean, parseable verdict
(``score is not None``) rather than a ``None`` from a ``max_tokens``
truncation / parse failure.

Gating — belt-and-suspenders, mirroring
:file:`tests/grade/test_gemini_grade_live.py`:

* ``pytestmark = pytest.mark.gemini`` — the existing ``gemini`` marker,
  excluded from the default ``pytest`` run via :file:`pyproject.toml`'s
  ``addopts``. Default CI **deselects** this test (it is imported during
  collection but the test body never runs).
* A runtime ``pytest.skip(...)`` when ``SF_RUN_GEMINI != "1"`` OR
  ``GOOGLE_API_KEY`` is unset/blank.

Run::

    SF_RUN_GEMINI=1 GOOGLE_API_KEY=... pytest -m gemini --no-cov \\
        tests/research/187-haiku-calibration/test_gemini_1024_no_truncation.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import signalforge as _sf
from signalforge.draft.models import CandidateColumn, CandidateSchema
from signalforge.grade import Criterion, GradingReport, grade_artifacts
from signalforge.grade.config import GradeConfig
from signalforge.manifest.models import Column, Model
from signalforge.prune.models import PruneResult

pytestmark = pytest.mark.gemini


def _skip_reason() -> str | None:
    """Return a clear skip-reason string when env vars are missing."""
    if os.environ.get("SF_RUN_GEMINI") != "1":
        return "SF_RUN_GEMINI=1 not set"
    if not os.environ.get("GOOGLE_API_KEY", "").strip():
        return "GOOGLE_API_KEY env var not set"
    return None


def test_gemini_grade_at_1024_tokens_is_not_truncation_degraded(tmp_path: Path) -> None:
    """Grade a verbose artifact on Gemini @ 1024 tokens; assert no degrade.

    Uses a deliberately long, dense column description + rationale (the
    shape most likely to elicit a long judge response) and a
    single-criterion rubric. Asserts every returned
    :class:`GradingResult` has a non-``None`` score and
    ``aggregate_complete is True`` — proving 1024 output tokens left the
    Gemini judge enough headroom to finish cleanly. A truncation at the
    cap would surface as ``score=None`` (the DEC-015 degraded path) and
    fail this assertion loud.
    """
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    model = Model(
        unique_id="model.sf_calib.dim_customers",
        name="dim_customers",
        resource_type="model",
        package_name="sf_calib",
        original_file_path="models/marts/dim_customers.sql",
        path="marts/dim_customers.sql",
        database="sf-calib-proj",
        schema="main",  # type: ignore[call-arg]
        columns={"customer_id": Column(name="customer_id")},
        raw_code="select 1 as customer_id",
    )

    # A verbose artifact: long description + long rationale. The judge's
    # reasoning + evidence for a dense artifact is the worst case for the
    # output-token budget.
    verbose_description = (
        "Surrogate primary key uniquely identifying each customer record "
        "in the conformed customer dimension. Generated deterministically "
        "from the upstream source system's composite natural key "
        "(source_system_code, source_customer_id) via "
        "dbt_utils.generate_surrogate_key so that the same logical "
        "customer always hashes to the same surrogate value across full "
        "refreshes and incremental loads. Downstream fact tables "
        "(fct_orders, fct_subscriptions, fct_support_tickets) join on this "
        "column exclusively; the natural key is intentionally not exposed "
        "to BI to prevent leakage of source-system implementation detail "
        "into the semantic layer. Stability of this surrogate across loads "
        "is a hard contract — a change in the hashing inputs would silently "
        "fan out as duplicate-or-orphaned rows across every downstream mart."
    )
    verbose_rationale = (
        "Documented at this length because the surrogate-key contract is "
        "the single most load-bearing invariant in the customer dimension: "
        "every downstream join, every slowly-changing-dimension lineage "
        "trace, and every data-quality reconciliation depends on it. A "
        "future maintainer tempted to swap the hashing inputs, change the "
        "salt, or fall back to the raw natural key needs the full rationale "
        "in one place so the blast radius is obvious before the change "
        "ships rather than discovered in a downstream incident review."
    )

    candidate = CandidateSchema(
        name="dim_customers",
        description="Curated one-row-per-customer dimension table for analytics.",
        rationale="Conformed customer dimension consumed by every downstream fact table.",
        columns=(
            CandidateColumn(
                name="customer_id",
                description=verbose_description,
                rationale=verbose_rationale,
                tests=(),
            ),
        ),
        tests=(),
    )

    rubric = (
        Criterion(
            id="clarity",
            criterion=(
                "Is the column description clear, specific, and actionable? "
                "Explain in detail what is strong or weak about it, citing "
                "specific phrases, before giving your verdict."
            ),
        ),
    )

    prune_result = PruneResult(
        model_unique_id=model.unique_id,
        decisions=(),
        elapsed_ms=0,
        signalforge_version=_sf.__version__,
    )

    # The contract under test: provider=gemini, the 1024-token DEFAULT.
    config = GradeConfig(
        provider="gemini",
        model="gemini-2.5-flash",
        # max_output_tokens left at the new 1024 default deliberately —
        # this is the value DEC-004 claims prevents Gemini truncation.
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=120,
    )
    assert config.max_output_tokens == 1024

    audit_path = tmp_path / "grade.jsonl"
    sidecar_path = tmp_path / "grade.json"

    report = grade_artifacts(
        model,
        candidate,
        prune_result,
        rubric=rubric,
        config=config,
        client=None,
        audit_path=audit_path,
        sidecar_path=sidecar_path,
        project_dir=tmp_path,
    )

    assert isinstance(report, GradingReport)
    # 5 artifacts × 1 criterion = 5 results (column desc/rationale, model
    # desc/rationale — empty model rationale still grades — and no tests).
    # Actually: 1 column desc + 1 column rationale + model desc + model
    # rationale = 4 artifacts (no tests). Assert the no-degrade contract
    # over whatever the engine emits.
    assert report.results, "expected at least one grading result"

    # The load-bearing assertion: NO result degraded to score=None. A
    # 1024-token truncation on the verbose artifact would flip one or
    # more to None and trip this.
    degraded = [r for r in report.results if r.score is None]
    assert not degraded, (
        "Gemini grade at max_output_tokens=1024 produced degraded "
        f"(score=None) results on a verbose artifact: "
        f"{[(r.artifact_id, r.criterion_id) for r in degraded]}. "
        "DEC-004's claim that 1024 prevents Gemini truncation does NOT "
        "hold for this artifact shape."
    )
    assert report.aggregate_complete is True

    # Sidecar round-trips through the typed model.
    assert sidecar_path.exists()
    GradingReport.model_validate_json(sidecar_path.read_text(encoding="utf-8"))
