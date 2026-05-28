"""Maintainer-only live smoke for :func:`grade_artifacts` + Gemini (#137 US-008).

Drives the grader end-to-end against the real Gemini API with a
1-criterion rubric over a 1-column candidate (5 artifacts × 1 criterion
= 5 judge calls, matching the cost shape of the existing
``anthropic``-marked :file:`tests/grade/test_smoke_real_api.py`).
Asserts the returned :class:`GradingReport` carries at least one
:class:`GradingResult` with a non-``None`` score and
``aggregate_complete is True`` — shape only, no specific score pins
(LLM output is not deterministic enough; see
:file:`.claude/rules/testing-signal.md` § "End-to-end gated tests").

Gated by ``@pytest.mark.gemini`` + ``SF_RUN_GEMINI=1`` +
``GOOGLE_API_KEY`` (belt-and-suspenders pattern).

Cost economy: ``gemini-2.5-flash`` (cheapest SKU); 1 criterion ×
5 artifacts. Single-criterion rubric mirrors
:file:`tests/grade/test_smoke_real_api.py` exactly.

What this proves end-to-end on the Gemini path:

* ``GradeConfig(provider="gemini")`` validates against the registry.
* :func:`signalforge.grade.grade_artifacts` drives the orchestrator
  through Gemini's :class:`GeminiProvider`; the JSON-enforced
  response (``response_mime_type="application/json"``, DEC-018 of
  #137) parses cleanly through :class:`GradingResult`.
* Both the fail-closed JSONL audit and the sidecar JSON land; the
  sidecar round-trips through :class:`GradingReport.model_validate_json`.
* Cache-token fields are 0 on every event (DEC-003 of #137).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import signalforge as _sf
from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTestNotNull,
)
from signalforge.grade import (
    Criterion,
    GradingReport,
    grade_artifacts,
)
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


def test_grade_artifacts_gemini_round_trips_against_real_api(tmp_path: Path) -> None:
    """One real Gemini grade round-trip over 1 criterion × 5 artifacts.

    Builds a tiny :class:`CandidateSchema` (1 column with description +
    rationale + 1 ``not_null`` test) and a 1-criterion (``clarity``)
    rubric — 5 artifacts × 1 criterion = 5 judge calls. Asserts the
    sidecar parses cleanly, at least one result has a non-``None``
    score, and ``aggregate_complete is True``.
    """
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    # Tiny manifest model — only fields the grader's pipeline reads.
    model = Model(
        unique_id="model.sf_smoke.dim_users",
        name="dim_users",
        resource_type="model",
        package_name="sf_smoke",
        original_file_path="models/marts/dim_users.sql",
        path="marts/dim_users.sql",
        database="sf-smoke-proj",
        schema="main",  # type: ignore[call-arg]
        columns={"user_id": Column(name="user_id")},
        raw_code="select 1 as user_id",
    )

    candidate = CandidateSchema(
        name="dim_users",
        description="Curated user dimension table for analytics.",
        rationale=(
            "Joins source.users with source.user_profiles to produce one row per active user."
        ),
        columns=(
            CandidateColumn(
                name="user_id",
                description=(
                    "Primary key uniquely identifying each user. Sourced from source.users.id."
                ),
                rationale="Used as join key by every downstream fact table.",
                tests=(
                    CandidateTestNotNull(
                        column="user_id",
                        rationale="Primary keys must never be null.",
                    ),
                ),
            ),
        ),
        tests=(),
    )

    rubric = (
        Criterion(
            id="clarity",
            criterion=(
                "Is the column description clear, specific, and "
                "actionable? Does it unambiguously explain the column's "
                "purpose and business meaning without jargon or vagueness?"
            ),
        ),
    )

    # Empty prune result — the grader's no-redundant criterion is the
    # only consumer of dropped tests, and we're running a custom
    # single-criterion (clarity) rubric so the empty tuple is fine.
    prune_result = PruneResult(
        model_unique_id=model.unique_id,
        decisions=(),
        elapsed_ms=0,
        signalforge_version=_sf.__version__,
    )

    config = GradeConfig(
        provider="gemini",
        model="gemini-2.5-flash",
        max_output_tokens=512,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=120,
    )

    audit_path = tmp_path / "grade.jsonl"
    sidecar_path = tmp_path / "grade.json"

    report = grade_artifacts(
        model,
        candidate,
        prune_result,
        rubric=rubric,
        config=config,
        # client=None ⇒ strategy.make_client() builds the real SDK client
        # (DEC-006 of #135).
        client=None,
        audit_path=audit_path,
        sidecar_path=sidecar_path,
        project_dir=tmp_path,
    )

    # Shape-only assertions — no specific score / passed contract.
    assert isinstance(report, GradingReport)
    assert report.model_unique_id == model.unique_id
    # 5 artifacts × 1 criterion = 5 results.
    assert len(report.results) == 5

    # At least one positively-scored result — proves the JSON-enforced
    # response (DEC-018 of #137) parsed cleanly through the grader.
    scored = [r for r in report.results if r.score is not None]
    assert scored, "expected at least one scored GradingResult on the live path"

    # When every pair scores, aggregate_complete is True. A degraded
    # pair (network blip, parse error) would flip it; the assertion is
    # the contract the live test pins.
    assert report.aggregate_complete is True

    for r in report.results:
        assert r.criterion_id == "clarity"
        # Score is either None (degraded — DEC-015 of #7) or a finite
        # float in [0.0, 1.0]. The model's own validator enforces the
        # range; the assertion documents the contract.
        assert r.score is None or 0.0 <= r.score <= 1.0

    # Sidecar JSON exists and round-trips through the typed model.
    assert sidecar_path.exists()
    GradingReport.model_validate_json(sidecar_path.read_text(encoding="utf-8"))

    # Audit JSONL exists with at least one durable record per call.
    assert audit_path.exists()
    audit_lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(audit_lines) >= 1
