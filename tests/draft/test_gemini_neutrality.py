"""Provider-neutrality end-to-end proof for the Gemini provider — drafter
side (#137 US-005, DEC-014 — two-stage scope).

Mirrors :mod:`tests.grade.test_gemini_neutrality` for the drafter half of
the pipeline. Where the grader issues one LLM call per ``(artifact,
criterion)`` pair, the drafter issues exactly ONE call per model, so this
file is correspondingly thinner. Both stages must work through the Gemini
provider to satisfy the DEC-014 contract.

Pins:

* ``DraftConfig(provider="gemini")`` validates (DEC-007 of #135).
* ``draft_schema`` drives the Gemini path end-to-end with
  :class:`tests.llm._fake_gemini.FakeGeminiClient` injected, parses the
  candidate cleanly, and writes an :class:`LLMResponseEvent` with
  ``cache_*_input_tokens == 0`` (DEC-003 of #137 — capability flags
  ``False``/``False``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from signalforge.draft.config import DraftConfig
from signalforge.draft.models import CandidateSchema
from signalforge.draft.schema import DraftOutcome, draft_from_request

if TYPE_CHECKING:
    from signalforge.llm import AnthropicClientProtocol
from signalforge.manifest.loader import load
from signalforge.manifest.models import Manifest, Model
from signalforge.safety.models import LLMRequest, SamplingMode
from tests.llm._fake_gemini import (
    FakeGeminiCandidate,
    FakeGeminiClient,
    FakeGeminiContent,
    FakeGeminiPart,
    FakeGeminiResponse,
    FakeGeminiUsageMetadata,
)

pytestmark = pytest.mark.draft


_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "draft"
_MANIFEST_FIXTURE = _FIXTURE_DIR / "manifest_one_model_with_neighbours.json"
_VALID_RESPONSE_FIXTURE = _FIXTURE_DIR / "llm_response_valid.json"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manifest() -> Manifest:
    return load(_MANIFEST_FIXTURE.parent, manifest_path=_MANIFEST_FIXTURE)


@pytest.fixture
def model(manifest: Manifest) -> Model:
    return manifest.get_model("model.sf_demo.fct_orders")


@pytest.fixture
def gemini_config() -> DraftConfig:
    """A drafter config selecting the Gemini provider.

    ``max_retries_*=0`` keeps a hypothetical retry path from masking a
    mis-classified exception in tests; ``cache_ttl="5m"`` is the default
    (gated off by ``supports_prompt_caching=False`` so it's a no-op).
    """
    return DraftConfig(
        provider="gemini",
        model="gemini-2.5-flash",
        cache_ttl="5m",
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
    )


@pytest.fixture
def valid_response_text() -> str:
    """The raw LLM-output JSON for fct_orders (matches the model's columns)."""
    return _VALID_RESPONSE_FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_draftconfig_validates_provider_gemini() -> None:
    """``DraftConfig(provider="gemini")`` validates against the registry
    (DEC-007 of #135 — registry-validated ``str``, not a ``Literal``)."""
    config = DraftConfig(provider="gemini", model="gemini-2.5-flash")

    assert config.provider == "gemini"
    assert config.model == "gemini-2.5-flash"


def test_draft_schema_drives_gemini_provider_end_to_end(
    model: Model,
    manifest: Manifest,
    gemini_config: DraftConfig,
    valid_response_text: str,
    tmp_path: Path,
) -> None:
    """A full :func:`draft_from_request` run through the Gemini provider
    parses the candidate cleanly, writes an :class:`LLMResponseEvent` to
    the response-audit JSONL, and reports
    ``cache_*_input_tokens == 0`` (DEC-003 of #137 — capability flags
    ``False``/``False`` ⇒ no cache markers, no cache-token accounting).

    Drafter is one LLM call per model, so a single canned response
    suffices.
    """
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

    fake_client = FakeGeminiClient()
    # Drafter response: a CandidateSchema-shaped JSON payload carried as
    # the only ``text`` part of a single candidate. Gemini's
    # ``response_mime_type="application/json"`` (DEC-018) returns JSON in
    # the same ``parts[*].text`` slot the provider walks.
    fake_client.expect_messages_create(
        matching=lambda kw: True,
        returns=FakeGeminiResponse(
            candidates=[
                FakeGeminiCandidate(
                    content=FakeGeminiContent(parts=[FakeGeminiPart(text=valid_response_text)]),
                    finish_reason="STOP",
                )
            ],
            usage_metadata=FakeGeminiUsageMetadata(
                prompt_token_count=1700,
                candidates_token_count=800,
            ),
        ),
    )

    audit_path = tmp_path / "safety_audit.jsonl"
    outcome = draft_from_request(
        request,
        model,
        manifest,
        config=gemini_config,
        audit_path=audit_path,
        # The kwarg is typed against the Anthropic injection surface
        # (DEC-012 of #135); a non-Anthropic provider builds its own
        # client and ignores the protocol header — the cast is the
        # documented seam.
        _client=cast("AnthropicClientProtocol", fake_client),
    )

    assert isinstance(outcome, DraftOutcome)
    assert isinstance(outcome.candidate, CandidateSchema)
    assert outcome.result.input_tokens == 1700
    assert outcome.result.output_tokens == 800

    # Cache-token accounting is 0 because supports_prompt_caching=False —
    # the orchestrator skips cache fields regardless of what the provider
    # returns (DEC-008 of #135).
    assert outcome.result.cache_creation_input_tokens == 0
    assert outcome.result.cache_read_input_tokens == 0

    # Response-audit JSONL: exactly one line, with cache fields at 0.
    response_audit = audit_path.with_name("llm_responses.jsonl")
    assert response_audit.exists()
    lines = response_audit.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["model_unique_id"] == model.unique_id
    assert record["model"] == gemini_config.model
    assert record["cache_creation_input_tokens"] == 0
    assert record["cache_read_input_tokens"] == 0

    fake_client.assert_all_expectations_met()
