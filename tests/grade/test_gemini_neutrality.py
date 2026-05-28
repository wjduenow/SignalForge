"""Provider-neutrality end-to-end proof for the Gemini provider (#137 US-005).

Mirrors :mod:`tests.grade.test_provider_neutrality` (the
:class:`FakeNoCacheProvider` precedent) but substitutes the real
:class:`signalforge.llm.providers.GeminiProvider` registered at module import
time + :class:`tests.llm._fake_gemini.FakeGeminiClient` as the injected client.
Drives :func:`signalforge.grade.grade_artifacts` end-to-end through a
non-Anthropic, non-caching provider and pins the capability-degrade invariants
from DEC-003 / DEC-005 / DEC-008 of issue #137:

* ``provider_for("gemini")`` resolves the provider and
  ``GradeConfig(provider="gemini")`` validates cleanly (DEC-007 of #135).
* A full :func:`grade_artifacts` run on the Gemini provider produces a valid
  audit JSONL + sidecar JSON, ``cache_*_input_tokens == 0`` on every event,
  intact 16-hex blake2b-8 reproducibility hashes, NO dual-zero cache-anomaly
  WARNING (DEC-003 — capability flags ``False``/``False``), and the strict
  ``extra="forbid"`` drift mirror accepts every line.
* A Gemini ``finish_reason="SAFETY"`` response (no text parts) routes the
  affected pair through :class:`GradeLLMError` → degraded
  ``GradingResult(score=None, passed=False, reasoning="call failed:
  GradeLLMError")`` (DEC-005); the other pairs remain scored and
  ``aggregate_complete is False``.

The Gemini provider is registered at :mod:`signalforge.llm.providers` import
time, so no registry-isolation fixture is required (registration survives the
test). Tests construct their own :class:`FakeGeminiClient` per call to avoid
cross-test state leak.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from signalforge.draft.models import CandidateSchema
from signalforge.grade.config import GradeConfig
from signalforge.grade.engine import grade_artifacts
from signalforge.grade.models import GradeEvent, GradingReport
from signalforge.grade.rubric import Criterion, Rubric
from signalforge.llm.providers import GeminiProvider, provider_for

if TYPE_CHECKING:
    from signalforge.llm import AnthropicClientProtocol
from signalforge.manifest.models import Column, Model
from signalforge.prune.models import PruneResult
from tests.grade.test_drift_detector import StrictGradeEvent
from tests.llm._fake_gemini import (
    FakeGeminiCandidate,
    FakeGeminiClient,
    FakeGeminiContent,
    FakeGeminiPart,
    FakeGeminiResponse,
    FakeGeminiUsageMetadata,
)

_FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "grade"

# A 16-hex blake2b-8 fingerprint (the reproducibility-hash recipe across the
# audit corpus). Used to assert the produced events carry well-formed hashes.
_BLAKE2B8_HEX = re.compile(r"^[0-9a-f]{16}$")


# ---------------------------------------------------------------------------
# Fixture builders (mirror tests.grade.test_provider_neutrality verbatim so
# this neutrality file stays self-contained, matching the precedent).
# ---------------------------------------------------------------------------


def _make_model() -> Model:
    return Model(
        unique_id="model.shop.orders",
        name="orders",
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={
            "order_id": Column(name="order_id"),
            "customer_id": Column(name="customer_id"),
        },
        raw_code="select 1",
    )


def _empty_prune_result(model: Model) -> PruneResult:
    return PruneResult(
        model_unique_id=model.unique_id,
        decisions=(),
        elapsed_ms=0,
        signalforge_version="0.0.0-test",
    )


def _load_sample_candidate() -> CandidateSchema:
    raw = (_FIXTURE_PATH / "sample_candidate.json").read_text(encoding="utf-8")
    return CandidateSchema.model_validate_json(raw)


def _single_criterion() -> Rubric:
    """A one-criterion rubric so every judge call shares one ``criterion_id``.

    The grade parser anchors on ``returned.criterion_id == sent.criterion_id``;
    a single canned response carrying this id satisfies every call.
    """
    return (Criterion(id="clarity", criterion="Is it clear?"),)


def _canned_judge_payload() -> str:
    """A grade-judge JSON payload the parser accepts for the ``clarity`` call."""
    return json.dumps(
        {
            "criterion_id": "clarity",
            "score": 0.8,
            "passed": True,
            "evidence": "concise and unambiguous",
            "reasoning": "reads clearly",
        }
    )


def _gemini_response_with_json(payload: str) -> FakeGeminiResponse:
    """Build a successful Gemini response carrying ``payload`` as its only
    text part.

    Gemini's ``response_mime_type="application/json"`` (DEC-018) returns JSON
    as the ``text`` of a single content part; the provider extracts it through
    :meth:`GeminiProvider.extract_text_blocks`.
    """
    return FakeGeminiResponse(
        candidates=[
            FakeGeminiCandidate(
                content=FakeGeminiContent(parts=[FakeGeminiPart(text=payload)]),
                finish_reason="STOP",
            )
        ],
        usage_metadata=FakeGeminiUsageMetadata(
            prompt_token_count=120,
            candidates_token_count=40,
        ),
    )


def _gemini_safety_blocked_response() -> FakeGeminiResponse:
    """Build a Gemini response that mimics the safety-blocked path: a
    candidate with no ``content`` and ``finish_reason="SAFETY"`` — no text
    parts available (DEC-005).

    :meth:`GeminiProvider.extract_text_blocks` raises
    :class:`LLMResponseFormatError` (an :class:`LLMError` subclass) on this
    shape; the grade engine wraps as :class:`GradeLLMError` and degrades the
    pair.
    """
    return FakeGeminiResponse(
        candidates=[
            FakeGeminiCandidate(content=None, finish_reason="SAFETY"),
        ],
        usage_metadata=FakeGeminiUsageMetadata(
            prompt_token_count=120,
            candidates_token_count=0,
        ),
    )


def _project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".signalforge").mkdir(parents=True, exist_ok=True)
    return project_dir


def _fast_config() -> GradeConfig:
    return GradeConfig(
        provider="gemini",
        model="gemini-2.5-flash",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=60,
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_provider_for_gemini_resolves() -> None:
    """``provider_for("gemini")`` resolves the :class:`GeminiProvider`
    registered at import time, with both capability flags ``False``
    (DEC-003 of #137).

    Probably redundant with the US-002 provider-registration tests but keeps
    the neutrality file self-contained, mirroring the
    ``FakeNoCacheProvider`` precedent.
    """
    provider = provider_for("gemini")

    assert isinstance(provider, GeminiProvider)
    assert provider.name == "gemini"
    assert provider.supports_prompt_caching is False
    assert provider.supports_token_count is False


def test_gradeconfig_validates_provider_gemini() -> None:
    """``GradeConfig(provider="gemini")`` validates against the registry
    (DEC-007 of #135 — registry-validated ``str``, not a ``Literal``).
    """
    config = GradeConfig(provider="gemini", model="gemini-2.5-flash")

    assert config.provider == "gemini"
    assert config.model == "gemini-2.5-flash"


def test_grade_artifacts_drives_gemini_provider_end_to_end(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full ``grade_artifacts`` run on the Gemini provider produces a
    valid audit JSONL + sidecar, zero cache tokens, intact reproducibility
    hashes, and no dual-zero cache-anomaly WARNING (DEC-003 of #137 — both
    capability flags ``False``).

    The sample fixture has 2 columns + 1 column test + 0 model tests = 7
    artifacts × 1 criterion = 7 judge calls (the same count the
    :class:`FakeNoCacheProvider` precedent asserts), all scored.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _single_criterion()

    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    sidecar_path = project_dir / ".signalforge" / "grade.json"

    fake_client = FakeGeminiClient()
    payload = _canned_judge_payload()
    # 7 expected calls — one per (artifact, criterion) pair. The orchestrator
    # is sequential, so FIFO matching is sufficient.
    expected_calls = 7
    for _ in range(expected_calls):
        fake_client.expect_messages_create(
            matching=lambda kw: True,
            returns=_gemini_response_with_json(payload),
        )

    with caplog.at_level("WARNING", logger="signalforge.llm.client"):
        report = grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_fast_config(),
            # The kwarg is typed against the Anthropic injection surface
            # (DEC-012 of #135); a non-Anthropic provider builds its own
            # client and ignores the protocol header — the cast is the
            # documented seam.
            client=cast("AnthropicClientProtocol", fake_client),
            project_dir=project_dir,
            audit_path=audit_path,
            sidecar_path=sidecar_path,
        )

    assert isinstance(report, GradingReport)
    assert len(report.results) == expected_calls
    assert report.aggregate_complete is True
    assert all(r.score == 0.8 and r.passed for r in report.results)

    # --- Audit JSONL: one durable record per call; cache tokens always 0. ---
    rows = _read_jsonl(audit_path)
    assert len(rows) == expected_calls
    events = [GradeEvent.model_validate(r) for r in rows]
    for event in events:
        assert event.cache_creation_input_tokens == 0
        assert event.cache_read_input_tokens == 0
        assert event.run_id == report.run_id
        assert _BLAKE2B8_HEX.match(event.rubric_hash)
        assert _BLAKE2B8_HEX.match(event.prompt_version_template)
        assert _BLAKE2B8_HEX.match(event.criterion_prompt_hash)
        assert _BLAKE2B8_HEX.match(event.response_text_hash)

    # --- Drift detector: every JSONL line round-trips through the strict
    #     extra="forbid" mirror. Catches a silent schema addition. ---
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            StrictGradeEvent.model_validate_json(line)

    # --- Sidecar JSON: present + round-trips through GradingReport. ---
    assert sidecar_path.exists()
    round_tripped = GradingReport.model_validate_json(sidecar_path.read_text(encoding="utf-8"))
    assert round_tripped.run_id == report.run_id
    assert len(round_tripped.results) == len(report.results)

    # --- No dual-zero cache-anomaly WARNING: a non-caching provider must
    #     have suppressed it (it would otherwise false-alarm on every call). ---
    assert not any("cache marker no-op" in rec.getMessage() for rec in caplog.records), (
        "Gemini provider (supports_prompt_caching=False) must suppress the "
        "dual-zero cache-anomaly WARNING"
    )

    # --- All queued expectations consumed; no leftovers. ---
    fake_client.assert_all_expectations_met()


def test_grade_artifacts_safety_blocked_response_degrades_pair(
    tmp_path: Path,
) -> None:
    """A Gemini ``finish_reason="SAFETY"`` response (no text parts) routes
    the affected pair through :class:`GradeLLMError` → degraded
    ``GradingResult(score=None, passed=False)`` (DEC-005 of #137).

    The provider's :meth:`extract_text_blocks` raises
    :class:`LLMResponseFormatError` (an :class:`LLMError` subclass) on
    safety-blocked content; the engine wraps as :class:`GradeLLMError` and
    routes through ``_build_degraded`` with
    ``reasoning="call failed: GradeLLMError"``. Other pairs in the run
    remain scored; ``aggregate_complete is False`` because at least one
    pair was not positively evaluated.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _single_criterion()

    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    sidecar_path = project_dir / ".signalforge" / "grade.json"

    fake_client = FakeGeminiClient()
    payload = _canned_judge_payload()
    expected_calls = 7
    # First call: safety-blocked → degrade. Remaining six: scored.
    fake_client.expect_messages_create(
        matching=lambda kw: True,
        returns=_gemini_safety_blocked_response(),
    )
    for _ in range(expected_calls - 1):
        fake_client.expect_messages_create(
            matching=lambda kw: True,
            returns=_gemini_response_with_json(payload),
        )

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_fast_config(),
        client=cast("AnthropicClientProtocol", fake_client),
        project_dir=project_dir,
        audit_path=audit_path,
        sidecar_path=sidecar_path,
    )

    assert isinstance(report, GradingReport)
    assert len(report.results) == expected_calls

    # At least one degraded pair → aggregate_complete is False.
    assert report.aggregate_complete is False

    degraded = [r for r in report.results if r.score is None]
    scored = [r for r in report.results if r.score is not None]

    assert len(degraded) == 1
    assert len(scored) == expected_calls - 1

    # The degraded pair carries the GradeLLMError shape (DEC-005 of #137,
    # generalised provider-neutral by DEC-001/DEC-005 of #155 — the
    # is_clean_completion ABC now routes BOTH the safety-blocked path
    # exercised here AND the MAX_TOKENS-with-partial-text path through
    # the same orchestrator gate, so this assertion is the shared
    # contract pin for both regressions).
    bad = degraded[0]
    assert bad.score is None
    assert bad.passed is False
    assert bad.evidence == ""
    assert bad.reasoning == "call failed: GradeLLMError"

    # The other pairs were scored normally.
    for good in scored:
        assert good.score == 0.8
        assert good.passed is True

    fake_client.assert_all_expectations_met()
