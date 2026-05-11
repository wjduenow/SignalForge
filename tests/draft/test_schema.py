"""Tests for :mod:`signalforge.draft.schema` (US-013).

Covers the integration layer that wires the prompt builder, the LLM seam,
the parser, and the response-audit writer into the public draft entry
points (`draft_from_request` + `draft_schema`). Plus the public-API
re-exports per DEC-020.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from signalforge.draft import schema as draft_schema_mod
from signalforge.draft.config import DraftConfig
from signalforge.draft.errors import (
    LLMOutputJSONError,
    LLMResponseAuditWriteError,
)
from signalforge.draft.models import CandidateSchema
from signalforge.draft.schema import DraftOutcome, draft_from_request, draft_schema
from signalforge.manifest.loader import load
from signalforge.manifest.models import Manifest, Model
from signalforge.safety.errors import AuditWriteError
from signalforge.safety.models import LLMRequest, SamplingMode
from signalforge.safety.policy import SafetyPolicy
from tests.llm._fake import (
    FakeAnthropicClient,
    FakeCountTokensResponse,
    FakeMessage,
    FakeTextBlock,
    FakeUsage,
)
from tests.safety._fake_adapter import FakeAdapter

pytestmark = pytest.mark.draft


_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "draft"
_MANIFEST_FIXTURE = _FIXTURE_DIR / "manifest_one_model_with_neighbours.json"
_VALID_RESPONSE_FIXTURE = _FIXTURE_DIR / "llm_response_valid.json"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def manifest() -> Manifest:
    return load(_MANIFEST_FIXTURE.parent, manifest_path=_MANIFEST_FIXTURE)


@pytest.fixture
def model(manifest: Manifest) -> Model:
    return manifest.get_model("model.sf_demo.fct_orders")


@pytest.fixture
def policy(tmp_path: Path) -> SafetyPolicy:
    return SafetyPolicy(
        mode=SamplingMode.SCHEMA_ONLY,
        audit_path=tmp_path / "audit.jsonl",
    )


@pytest.fixture
def config() -> DraftConfig:
    return DraftConfig(model="claude-sonnet-4-6", cache_ttl="5m")


@pytest.fixture
def valid_response_text() -> str:
    """The raw LLM-output JSON for fct_orders (matches the model's columns)."""
    return _VALID_RESPONSE_FIXTURE.read_text(encoding="utf-8")


def _set_up_fake_anthropic(
    fake: FakeAnthropicClient,
    *,
    response_text: str,
    cached_tokens: int = 1500,
    input_tokens: int = 1700,
    output_tokens: int = 800,
) -> None:
    """Queue the two SDK calls `call_anthropic` makes: count_tokens then create."""
    fake.expect_count_tokens(
        matching=lambda kw: True,
        returns=FakeCountTokensResponse(input_tokens=cached_tokens),
    )
    fake.expect_messages_create(
        matching=lambda kw: True,
        returns=FakeMessage(
            content=[FakeTextBlock(text=response_text)],
            usage=FakeUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cached_tokens,
                cache_read_input_tokens=0,
            ),
            model="claude-sonnet-4-6",
        ),
    )


# ---------------------------------------------------------------------------
# DraftOutcome
# ---------------------------------------------------------------------------


def test_draft_outcome_carries_candidate_request_result(
    model: Model,
    valid_response_text: str,
) -> None:
    candidate = CandidateSchema.model_validate_json(valid_response_text)
    request = LLMRequest(
        model_unique_id=model.unique_id,
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=("order_id", "customer_id", "amount", "ordered_at"),
        redactions=(),
        sampled_rows=None,
        aggregates=None,
        schema=(("order_id", "INT64"),),
    )
    from signalforge.llm.models import LLMResult

    result = LLMResult(
        text_blocks=(valid_response_text,),
        response_text=valid_response_text,
        input_tokens=1700,
        output_tokens=800,
        model="claude-sonnet-4-6",
        prompt_version="abc123",
        raw_message=object(),
    )

    outcome = DraftOutcome(candidate=candidate, request=request, result=result)

    assert outcome.candidate is candidate
    assert outcome.request is request
    assert outcome.result is result


def test_draft_outcome_frozen(
    model: Model,
    valid_response_text: str,
) -> None:
    candidate = CandidateSchema.model_validate_json(valid_response_text)
    request = LLMRequest(
        model_unique_id=model.unique_id,
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=(),
        redactions=(),
        sampled_rows=None,
        aggregates=None,
        schema=(),
    )
    from signalforge.llm.models import LLMResult

    result = LLMResult(
        text_blocks=(),
        response_text="",
        input_tokens=0,
        output_tokens=0,
        model="x",
        prompt_version="x",
        raw_message=None,
    )
    outcome = DraftOutcome(candidate=candidate, request=request, result=result)

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        outcome.candidate = candidate  # type: ignore[misc]


# ---------------------------------------------------------------------------
# draft_from_request
# ---------------------------------------------------------------------------


def test_draft_from_request_happy_path_returns_outcome(
    model: Model,
    manifest: Manifest,
    config: DraftConfig,
    valid_response_text: str,
    tmp_path: Path,
) -> None:
    request = LLMRequest(
        model_unique_id=model.unique_id,
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=("order_id", "customer_id", "amount", "ordered_at"),
        redactions=(),
        sampled_rows=None,
        aggregates=None,
        schema=(
            ("order_id", "INT64"),
            ("customer_id", "INT64"),
            ("amount", "FLOAT64"),
            ("ordered_at", "TIMESTAMP"),
        ),
    )
    anthropic_fake = FakeAnthropicClient()
    _set_up_fake_anthropic(anthropic_fake, response_text=valid_response_text)

    outcome = draft_from_request(
        request,
        model,
        manifest,
        config=config,
        audit_path=tmp_path / "safety_audit.jsonl",
        _client=anthropic_fake,
    )

    assert isinstance(outcome, DraftOutcome)
    assert isinstance(outcome.candidate, CandidateSchema)
    assert outcome.request is request
    assert outcome.result.input_tokens == 1700
    assert outcome.result.output_tokens == 800
    anthropic_fake.assert_all_expectations_met()


def test_draft_from_request_writes_response_audit_record(
    model: Model,
    manifest: Manifest,
    config: DraftConfig,
    valid_response_text: str,
    tmp_path: Path,
) -> None:
    request = LLMRequest(
        model_unique_id=model.unique_id,
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=("order_id",),
        redactions=(),
        sampled_rows=None,
        aggregates=None,
        schema=(("order_id", "INT64"),),
    )
    anthropic_fake = FakeAnthropicClient()
    _set_up_fake_anthropic(anthropic_fake, response_text=valid_response_text)

    audit_path = tmp_path / "safety_audit.jsonl"
    draft_from_request(
        request,
        model,
        manifest,
        config=config,
        audit_path=audit_path,
        _client=anthropic_fake,
    )

    response_audit = audit_path.with_name("llm_responses.jsonl")
    assert response_audit.exists()
    lines = response_audit.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["model_unique_id"] == model.unique_id
    assert record["model"] == config.model
    assert len(record["prompt_version"]) == 16
    assert len(record["response_text_hash"]) == 16
    assert len(record["parsed_schema_hash"]) == 16
    assert len(record["sent_sql_hash"]) == 16
    assert record["audit_schema_version"] == 1


def test_draft_from_request_audit_failure_drops_outcome(
    model: Model,
    manifest: Manifest,
    config: DraftConfig,
    valid_response_text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = LLMRequest(
        model_unique_id=model.unique_id,
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=(),
        redactions=(),
        sampled_rows=None,
        aggregates=None,
        schema=(),
    )
    anthropic_fake = FakeAnthropicClient()
    _set_up_fake_anthropic(anthropic_fake, response_text=valid_response_text)

    def _failing_write(*args: Any, **kwargs: Any) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(draft_schema_mod, "write_response_event", _failing_write)

    with pytest.raises(LLMResponseAuditWriteError) as exc_info:
        draft_from_request(
            request,
            model,
            manifest,
            config=config,
            audit_path=tmp_path / "safety_audit.jsonl",
            _client=anthropic_fake,
        )
    assert isinstance(exc_info.value.cause, OSError)


def test_draft_from_request_emits_prompt_version_debug_log_on_success(
    model: Model,
    manifest: Manifest,
    config: DraftConfig,
    valid_response_text: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = LLMRequest(
        model_unique_id=model.unique_id,
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=(),
        redactions=(),
        sampled_rows=None,
        aggregates=None,
        schema=(),
    )
    anthropic_fake = FakeAnthropicClient()
    _set_up_fake_anthropic(anthropic_fake, response_text=valid_response_text)

    with caplog.at_level(logging.DEBUG, logger="signalforge.draft.schema"):
        draft_from_request(
            request,
            model,
            manifest,
            config=config,
            audit_path=tmp_path / "safety_audit.jsonl",
            _client=anthropic_fake,
        )

    matching = [r for r in caplog.records if "prompt_version" in r.getMessage()]
    assert len(matching) >= 1
    assert matching[0].levelno == logging.DEBUG


def test_draft_from_request_bad_json_does_not_write_response_audit(
    model: Model,
    manifest: Manifest,
    config: DraftConfig,
    tmp_path: Path,
) -> None:
    request = LLMRequest(
        model_unique_id=model.unique_id,
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=(),
        redactions=(),
        sampled_rows=None,
        aggregates=None,
        schema=(),
    )
    anthropic_fake = FakeAnthropicClient()
    _set_up_fake_anthropic(anthropic_fake, response_text="this is not json")

    audit_path = tmp_path / "safety_audit.jsonl"
    with pytest.raises(LLMOutputJSONError):
        draft_from_request(
            request,
            model,
            manifest,
            config=config,
            audit_path=audit_path,
            _client=anthropic_fake,
        )

    response_audit = audit_path.with_name("llm_responses.jsonl")
    assert not response_audit.exists()


# ---------------------------------------------------------------------------
# draft_schema wrapper
# ---------------------------------------------------------------------------


def test_draft_schema_wrapper_calls_build_llm_request(
    model: Model,
    manifest: Manifest,
    policy: SafetyPolicy,
    config: DraftConfig,
    valid_response_text: str,
) -> None:
    fake_adapter = FakeAdapter()
    anthropic_fake = FakeAnthropicClient()
    _set_up_fake_anthropic(anthropic_fake, response_text=valid_response_text)

    outcome = draft_schema(
        model,
        fake_adapter,
        policy,
        manifest,
        config=config,
        _client=anthropic_fake,
    )

    assert isinstance(outcome, DraftOutcome)
    assert outcome.request.model_unique_id == model.unique_id
    assert outcome.request.mode is SamplingMode.SCHEMA_ONLY
    fake_adapter.assert_all_expectations_met()
    anthropic_fake.assert_all_expectations_met()


def test_draft_schema_wrapper_propagates_safety_audit_write_error(
    model: Model,
    manifest: Manifest,
    config: DraftConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = SafetyPolicy(
        mode=SamplingMode.SCHEMA_ONLY,
        audit_path=tmp_path / "safety_audit.jsonl",
    )
    fake_adapter = FakeAdapter()

    # Force the SAFETY-side audit to fail. AuditWriteError must propagate
    # UNCHANGED — it's a safety-layer error, not a draft-layer one.
    from signalforge.safety import audit as safety_audit

    def _failing_write(event: Any, audit_path: Path) -> None:
        raise AuditWriteError(audit_path, OSError("disk full"))

    monkeypatch.setattr(safety_audit, "write", _failing_write)
    # The draft layer doesn't catch AuditWriteError — assert it bubbles up.
    with pytest.raises(AuditWriteError):
        draft_schema(
            model,
            fake_adapter,
            policy,
            manifest,
            config=config,
            _client=FakeAnthropicClient(),
        )


# ---------------------------------------------------------------------------
# Public API surface (DEC-020)
# ---------------------------------------------------------------------------


def test_public_api_imports_match_dec_020() -> None:
    """Both subpackage ``__all__`` tuples match the canonical surface.

    Originally pinned the DEC-020 surface from #5; expanded by US-001
    of #9 (DEC-013) to add the eight tier-mappable :class:`DraftError`
    subclasses to ``signalforge.draft.__all__`` and the one
    :class:`LLMResponseFormatError` re-export to
    ``signalforge.llm.__all__``. Per-subpackage public-API drift is
    pinned by :mod:`tests.draft.test_public_api` and
    :mod:`tests.llm.test_public_api`; this test is the cross-cutting
    smoke check that retains the original DEC-020 framing.
    """
    import signalforge.draft as draft_pkg
    import signalforge.llm as llm_pkg

    assert draft_pkg.__all__ == (
        "CandidateColumn",
        "CandidateSchema",
        "CandidateTest",
        "DraftConfig",
        "DraftConfigInvalidError",
        "DraftConfigNotFoundError",
        "DraftError",
        "DraftOutcome",
        "LLMOutputAnchorContractError",
        "LLMOutputError",
        "LLMOutputJSONError",
        "LLMOutputValidationError",
        "LLMResponseAuditRecordTooLargeError",
        "LLMResponseAuditWriteError",
        "LLMResponseEvent",
        "PromptEnvelopeBreachError",
        "draft_from_request",
        "draft_schema",
        "load_draft_config",
    )
    # Expanded by US-001 of #36: adds the ``signalforge.llm.pricing``
    # surface (PRICE_TABLE_VERSION / PRICES / ModelPricing / lookup) and
    # the ``EstimateUnknownModelError`` typed exception. Expanded by
    # issue #44: adds ``AnthropicClientProtocol`` (promoted from the
    # private ``_AnthropicClientProtocol`` so the ``client`` kwarg on
    # ``draft_schema`` / ``grade_artifacts`` can be type-annotated against
    # a non-underscore public name).
    assert llm_pkg.__all__ == (
        "PRICES",
        "PRICE_TABLE_VERSION",
        "AnthropicClientProtocol",
        "EstimateUnknownModelError",
        "LLMAuthError",
        "LLMCacheTooLargeError",
        "LLMConnectionError",
        "LLMError",
        "LLMHelperError",
        "LLMRateLimitError",
        "LLMResponseFormatError",
        "LLMResult",
        "LLMServerError",
        "ModelPricing",
        "call_anthropic",
        "lookup",
    )
