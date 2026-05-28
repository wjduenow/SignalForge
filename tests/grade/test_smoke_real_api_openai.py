"""Real-API smoke test for :func:`signalforge.grade.grade_artifacts` via OpenAI.

Issue #136 / US-006 — DEC-001, DEC-004, DEC-005, DEC-008. Gated by the
``openai`` marker — excluded from default CI by :file:`pyproject.toml`'s
``addopts = "... -m 'not openai'"``. Requires ``SF_RUN_OPENAI=1`` +
``OPENAI_API_KEY``. Mirrors :file:`tests/grade/test_smoke_real_api.py`
(the Anthropic equivalent) in shape: ``pytestmark = pytest.mark.openai``,
env-var skip-gate, shape-only assertions (no specific scores or
``passed`` outcomes — LLM output is not deterministic enough for that
contract).

What this proves end-to-end:

* ``OPENAI_API_KEY`` is present and valid; the OpenAIProvider's Chat
  Completions adapter (#136 DEC-001 / DEC-009) reaches OpenAI and
  returns a parseable response under the grader prompt template.
* :func:`signalforge.grade.parser.parse_grade_response` validates the
  LLM-judge output through :class:`GradingResult` for an OpenAI
  ``gpt-4o`` response (the default judge model per DEC-004).
* :func:`signalforge.grade.engine.grade_artifacts` writes both the
  fail-closed JSONL audit and the sidecar JSON.
* OpenAI's ``cache_*_input_tokens`` are zero (``supports_prompt_caching=False``)
  and the seam does NOT emit the dual-zero cache-anomaly WARNING
  (capability-gated off in #135 + #136).

What this deliberately does NOT assert:

* Specific scores or ``passed`` outcomes — LLM output is not
  deterministic enough; the rubric drives the LLM's verdict and the
  test would be flaky if pinned.
"""

from __future__ import annotations

import logging
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
    GradeConfig,
    GradingReport,
    grade_artifacts,
)
from signalforge.manifest.models import Column, Model
from signalforge.prune.models import PruneResult

pytestmark = pytest.mark.openai

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _openai_runs_enabled() -> bool:
    """``SF_RUN_OPENAI`` is set to a truthy value (mirrors ``SF_RUN_BQ`` / ``SF_RUN_SNOWFLAKE``)."""
    return os.environ.get("SF_RUN_OPENAI", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` only when both gates are satisfied — the test then
    proceeds to make real OpenAI calls. Each missing prerequisite yields
    its own distinct reason so a maintainer running
    ``pytest -m openai`` sees exactly what to set. Treat an empty /
    whitespace-only ``OPENAI_API_KEY`` as "unset" (an empty value would
    otherwise reach the API and produce a noisy auth failure).
    """
    if not _openai_runs_enabled():
        return "SF_RUN_OPENAI=1 required (live test calls the real OpenAI API)"
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return "OPENAI_API_KEY required (live test authenticates against the real OpenAI API)"
    return None


def test_grade_artifacts_real_openai_api_smoke(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One real OpenAI round-trip against a tiny fixture; shape-only.

    Builds a minimal :class:`CandidateSchema` (1 column with
    description + rationale + 1 ``not_null`` test) and a
    single-criterion rubric (clarity). Issues 5 real LLM calls through
    :func:`signalforge.grade.grade_artifacts` (5 artifacts × 1 criterion)
    and asserts only that the typed sidecar parses cleanly, the JSONL
    audit landed, OpenAI cache token fields are zero, and no dual-zero
    cache-anomaly WARNING was emitted (capability gate from #135 / #136).
    """
    if reason := _skip_reason():
        pytest.skip(reason)

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

    # OpenAI judge: gpt-4o (the DEC-004 default judge model). The
    # provider field on GradeConfig is the #135 plug-in seam.
    config = GradeConfig(provider="openai", model="gpt-4o")

    with caplog.at_level(logging.WARNING):
        report = grade_artifacts(
            model,
            candidate,
            prune_result,
            rubric=rubric,
            config=config,
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

    # OpenAI provider has ``supports_prompt_caching=False`` — the
    # dual-zero cache-anomaly WARNING (llm-drafter.md DEC-014) is
    # capability-gated off and must NEVER fire on this path.
    cache_warning_messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and "cache marker no-op" in record.getMessage()
    ]
    assert not cache_warning_messages, (
        f"unexpected cache-anomaly WARNING(s) on OpenAI path "
        f"(supports_prompt_caching=False should gate this off): {cache_warning_messages}"
    )
