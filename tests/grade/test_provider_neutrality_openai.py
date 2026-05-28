"""OpenAI provider-neutrality proof — #136 US-003 (mirrors US-005 of #135).

The OpenAI analogue of :mod:`tests.grade.test_provider_neutrality`. Drives
:func:`signalforge.grade.grade_artifacts` end-to-end with the real
:class:`signalforge.llm.providers.OpenAIProvider` (selected via
``GradeConfig(provider="openai")``) plus the hand-rolled
:class:`tests.llm._fake_openai.FakeOpenAIClient`, and pins the
capability-degrade invariants from DEC-008 of #135 / DEC-005, DEC-006,
DEC-009, DEC-011 of #136:

* ``provider_for("openai")`` resolves an :class:`OpenAIProvider` with both
  capability flags ``False``.
* ``GradeConfig(provider="openai", model="gpt-4o")`` validates without
  error (the registry-validated ``provider`` field accepts it because the
  provider was registered at module-import time).
* ``grade_artifacts(..., client=FakeOpenAIClient())`` writes a valid audit
  JSONL + sidecar JSON; every produced ``GradeEvent`` records
  ``cache_*_input_tokens == 0`` and the four reproducibility blake2b-8
  hashes are well-formed; the JSONL round-trips through the strict
  ``extra="forbid"`` drift mirror; the sidecar JSON round-trips through
  :class:`GradingReport`; NO dual-zero cache-anomaly WARNING fires; and
  :meth:`FakeOpenAIClient.assert_all_expectations_met` reports zero
  unconsumed expectations.

Registry isolation mirrors :mod:`tests.grade.test_provider_neutrality` —
snapshot + restore ``signalforge.llm.providers._REGISTRY`` so any in-test
registry mutation does not leak.
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
from signalforge.llm.providers import OpenAIProvider, provider_for

if TYPE_CHECKING:
    from signalforge.llm import AnthropicClientProtocol
from signalforge.manifest.models import Column, Model
from signalforge.prune.models import PruneResult
from tests.grade.test_drift_detector import StrictGradeEvent
from tests.llm._fake_openai import (
    FakeOpenAIChoice,
    FakeOpenAIClient,
    FakeOpenAICompletion,
    FakeOpenAIMessage,
    FakeOpenAIUsage,
)

_FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "grade"

# A 16-hex blake2b-8 fingerprint (the reproducibility-hash recipe across the
# audit corpus). Used to assert the produced events carry well-formed hashes.
_BLAKE2B8_HEX = re.compile(r"^[0-9a-f]{16}$")


@pytest.fixture
def _isolate_registry() -> Any:
    """Snapshot + restore the process-level provider registry.

    Mirrors :mod:`tests.grade.test_provider_neutrality`'s fixture — keeps
    any in-test ``register_provider`` mutation from leaking across tests.
    The default :class:`OpenAIProvider` is registered at import time, so
    after restore ``provider_for("openai")`` still resolves cleanly.
    """
    from signalforge.llm import providers as providers_module

    saved = dict(providers_module._REGISTRY)
    try:
        yield
    finally:
        providers_module._REGISTRY.clear()
        providers_module._REGISTRY.update(saved)


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


def _canned_judge_response_text() -> str:
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


def _build_canned_completion() -> FakeOpenAICompletion:
    """Build a fresh canned :class:`FakeOpenAICompletion` for one judge call.

    A new instance per call keeps the JSONL ``response_text_hash`` stable
    (hash is over the text payload only) while avoiding accidental shared
    mutable state between expectations.
    """
    return FakeOpenAICompletion(
        choices=[
            FakeOpenAIChoice(
                message=FakeOpenAIMessage(content=_canned_judge_response_text()),
            )
        ],
        usage=FakeOpenAIUsage(prompt_tokens=120, completion_tokens=60),
    )


def _project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".signalforge").mkdir(parents=True, exist_ok=True)
    return project_dir


def _fast_config() -> GradeConfig:
    return GradeConfig(
        provider="openai",
        model="gpt-4o",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=60,
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_provider_for_openai_resolves_with_correct_capability_flags(
    _isolate_registry: None,
) -> None:
    """``provider_for("openai")`` returns an :class:`OpenAIProvider` with both
    capability flags ``False`` (DEC-008 of #135 / DEC-001 of #136)."""
    provider = provider_for("openai")
    assert isinstance(provider, OpenAIProvider)
    assert provider.supports_prompt_caching is False
    assert provider.supports_token_count is False


def test_grade_config_provider_openai_validates_after_registration(
    _isolate_registry: None,
) -> None:
    """``GradeConfig(provider="openai", model="gpt-4o")`` validates without
    error (DEC-007 of #135 — the registry-validated ``provider`` field
    accepts a registered key)."""
    config = GradeConfig(provider="openai", model="gpt-4o")
    assert config.provider == "openai"
    assert config.model == "gpt-4o"


def test_grade_artifacts_drives_openai_provider_end_to_end(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    _isolate_registry: None,
) -> None:
    """End-to-end OpenAI grade run: zero cache tokens in the JSONL, well-formed
    reproducibility hashes, strict drift-mirror round-trip on the JSONL,
    :class:`GradingReport` round-trip on the sidecar, no dual-zero
    cache-anomaly WARNING, and all expectations on the fake consumed
    (DEC-005, DEC-006, DEC-009, DEC-011 of #136)."""
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _single_criterion()

    # The sample candidate has 2 columns + 1 column test + 0 model tests = 7
    # artifacts × 1 criterion = 7 judge calls (matches the upstream
    # FakeNoCacheProvider neutrality test). Queue one expectation per call.
    client = FakeOpenAIClient()

    def _is_openai_create_kwargs(kwargs: dict[str, Any]) -> bool:
        # Callable matcher: assert the orchestrator handed the provider
        # OpenAI-shaped kwargs and never an Anthropic ``cache_control`` /
        # ``extra_headers`` cache header. Returning False here would surface
        # as "unexpected messages.create call: did not match expectation".
        if kwargs.get("model") != "gpt-4o":
            return False
        if "response_format" not in kwargs:
            return False
        # No Anthropic-shaped fields ever attached.
        if "system" in kwargs:
            return False
        if kwargs.get("extra_headers"):
            return False
        # No cache_control marker on any content block.
        messages = kwargs.get("messages") or []
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "cache_control" in block:
                        return False
        return True

    for _ in range(7):
        client.expect_messages_create(
            matching=_is_openai_create_kwargs,
            returns=_build_canned_completion(),
        )

    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    sidecar_path = project_dir / ".signalforge" / "grade.json"

    with caplog.at_level("WARNING", logger="signalforge.llm.client"):
        # ``grade_artifacts`` types its ``client`` kwarg against the Anthropic
        # injection surface (DEC-012 of #135). A non-Anthropic provider builds
        # its own client and the orchestrator hands it straight to the
        # strategy, so the cast is the documented seam.
        report = grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_fast_config(),
            client=cast("AnthropicClientProtocol", client),
            project_dir=project_dir,
            audit_path=audit_path,
            sidecar_path=sidecar_path,
        )

    assert isinstance(report, GradingReport)
    assert len(report.results) == 7
    assert report.aggregate_complete is True
    assert all(r.score == 0.8 and r.passed for r in report.results)

    # --- Audit JSONL: one durable record per call, all cache tokens 0. ---
    rows = _read_jsonl(audit_path)
    assert len(rows) == len(report.results) == 7
    events = [GradeEvent.model_validate(r) for r in rows]
    for event in events:
        assert event.cache_creation_input_tokens == 0
        assert event.cache_read_input_tokens == 0
        assert event.run_id == report.run_id
        # Reproducibility blake2b-8 fingerprints are present + well-formed.
        assert _BLAKE2B8_HEX.match(event.rubric_hash)
        assert _BLAKE2B8_HEX.match(event.prompt_version_template)
        assert _BLAKE2B8_HEX.match(event.criterion_prompt_hash)
        assert _BLAKE2B8_HEX.match(event.response_text_hash)

    # --- Drift detector: each JSONL line round-trips through the strict
    #     extra="forbid" mirror (zero-cache events validate cleanly). ---
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            StrictGradeEvent.model_validate_json(line)

    # --- Sidecar JSON: present + round-trips through GradingReport. ---
    assert sidecar_path.exists()
    round_tripped = GradingReport.model_validate_json(sidecar_path.read_text(encoding="utf-8"))
    assert round_tripped.run_id == report.run_id
    assert len(round_tripped.results) == len(report.results)

    # --- No dual-zero cache-anomaly WARNING: OpenAI's supports_prompt_caching=False
    #     gates this WARNING off (it would otherwise false-alarm on every call). ---
    assert not any("cache marker no-op" in rec.getMessage() for rec in caplog.records), (
        "OpenAI provider (supports_prompt_caching=False) must suppress the "
        "dual-zero cache-anomaly WARNING"
    )

    # --- The fake's expectation queue is exhausted (no leftover, no extras). ---
    client.assert_all_expectations_met()
