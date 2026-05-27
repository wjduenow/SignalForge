"""Real-API smoke test for :func:`signalforge.grade.grade_artifacts` (US-010).

Gated by the ``anthropic`` marker — excluded from default CI by
:file:`pyproject.toml`'s ``addopts = "... -m 'not anthropic'"``. Requires
``ANTHROPIC_API_KEY``. Mirrors :file:`tests/draft/test_smoke_real_api.py`
in shape: ``pytestmark = pytest.mark.anthropic``, env-var skip-gate,
shape-only assertions (no specific scores or ``passed`` outcomes — LLM
output is not deterministic enough for that contract).

Path chosen: **C** (full round-trip). Unlike the drafter's smoke test
(which short-circuits on the cached-block size check), the grader's
cached block is the rubric — for the locked four-criterion
:data:`signalforge.grade.DEFAULT_RUBRIC` it sits comfortably above the
1024-token Sonnet minimum. With a tiny one-column / one-criterion
fixture and a locally-supplied single-criterion rubric, a full
``grade_artifacts`` run issues exactly 5 LLM calls (one per artifact:
``column.user_id.description``, ``column.user_id.rationale``,
``model.description``, ``model.rationale``,
``test.column.user_id.not_null``) × 1 criterion = 5 calls — a
reasonable smoke-test budget at ~$0.005 per call on Sonnet 4.6.

What this proves end-to-end:

* ``ANTHROPIC_API_KEY`` is present and valid.
* :func:`signalforge.llm.client.call_llm` reaches Anthropic and
  returns a parseable response under the grader prompt template.
* :func:`signalforge.grade.parser.parse_grade_response` validates the
  LLM-judge output through :class:`GradingResult`.
* :func:`signalforge.grade.engine.grade_artifacts` writes both the
  fail-closed JSONL audit and the sidecar JSON, and the sidecar
  parses cleanly through :class:`GradingReport.model_validate_json`.

What this deliberately does NOT assert:

* Specific scores or ``passed`` outcomes — LLM output is not
  deterministic enough; the rubric drives the LLM's verdict and the
  test would be flaky if pinned.
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
from signalforge.manifest.models import Column, Model
from signalforge.prune.models import PruneResult

pytestmark = pytest.mark.anthropic


def test_grade_artifacts_real_api_smoke(tmp_path: Path) -> None:
    """One real Anthropic round-trip against a tiny fixture; shape-only.

    Builds a minimal :class:`CandidateSchema` (1 column with
    description + rationale + 1 ``not_null`` test) and a
    single-criterion rubric (clarity). Issues 5 real LLM calls through
    :func:`signalforge.grade.grade_artifacts` and asserts only that
    the typed sidecar parses cleanly and the JSONL audit landed.
    """
    # Treat empty / whitespace-only env values as "unset" — an empty
    # ANTHROPIC_API_KEY would still pass ``"in os.environ"`` but produce
    # a noisy auth failure on the live call. Skip cleanly in that case.
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        pytest.skip("ANTHROPIC_API_KEY not set")

    # Tiny manifest model — only the fields the grader's pipeline reads.
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
    # only consumer of dropped tests, and we're running with a custom
    # single-criterion (clarity) rubric so the empty tuple is fine.
    prune_result = PruneResult(
        model_unique_id=model.unique_id,
        decisions=(),
        elapsed_ms=0,
        signalforge_version=_sf.__version__,
    )

    audit_path = tmp_path / "grade.jsonl"
    sidecar_path = tmp_path / "grade.json"

    report = grade_artifacts(
        model,
        candidate,
        prune_result,
        rubric=rubric,
        audit_path=audit_path,
        sidecar_path=sidecar_path,
        project_dir=tmp_path,
    )

    # Shape-only assertions — no specific score / ``passed`` contract.
    assert isinstance(report, GradingReport)
    assert report.model_unique_id == model.unique_id
    # 5 artifacts × 1 criterion = 5 results.
    assert len(report.results) == 5
    for r in report.results:
        assert r.criterion_id == "clarity"
        # Score is either ``None`` (degraded path — DEC-015) or a
        # finite float in [0.0, 1.0]. The model's own validator already
        # enforces this at construction; the assertion documents the
        # contract for the smoke test reader.
        assert r.score is None or 0.0 <= r.score <= 1.0

    # Sidecar JSON exists and round-trips through the typed model.
    assert sidecar_path.exists()
    sidecar_text = sidecar_path.read_text(encoding="utf-8")
    GradingReport.model_validate_json(sidecar_text)

    # Audit JSONL exists and has at least one durable record per call.
    assert audit_path.exists()
    audit_lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(audit_lines) >= 1
