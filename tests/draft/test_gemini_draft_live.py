"""Maintainer-only live smoke for :func:`draft_from_request` + Gemini (#137 US-008).

Drives the drafter end-to-end against the real Gemini API and asserts
the returned :class:`CandidateSchema` validates and an
:class:`LLMResponseEvent` was written to the response-audit JSONL with
``cache_*_input_tokens == 0`` (DEC-003 of #137 —
``supports_prompt_caching=False``).

Gated by ``@pytest.mark.gemini`` + ``SF_RUN_GEMINI=1`` +
``GOOGLE_API_KEY`` (the belt-and-suspenders pattern from
:file:`.claude/rules/testing-signal.md` § "Belt-and-suspenders
gating"). Uses :func:`draft_from_request` directly with a pre-built
:class:`LLMRequest` so the test does NOT require a live warehouse —
the safety-layer's schema-only mode is sufficient. Mirrors the offline
:mod:`tests.draft.test_gemini_neutrality` shape but substitutes a real
Gemini round-trip for the :class:`FakeGeminiClient`.

Cost economy: ``gemini-2.5-flash`` (cheapest SKU); one LLM call per
drafted model. Shape-only assertions (no LLM-output-byte pins).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from signalforge.draft.config import DraftConfig
from signalforge.draft.models import CandidateSchema
from signalforge.draft.schema import DraftOutcome, draft_from_request
from signalforge.manifest.loader import load
from signalforge.safety.models import LLMRequest, SamplingMode

pytestmark = pytest.mark.gemini


_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "draft"
_MANIFEST_FIXTURE = _FIXTURE_DIR / "manifest_one_model_with_neighbours.json"


def _skip_reason() -> str | None:
    """Return a clear skip-reason string when env vars are missing."""
    if os.environ.get("SF_RUN_GEMINI") != "1":
        return "SF_RUN_GEMINI=1 not set"
    if not os.environ.get("GOOGLE_API_KEY", "").strip():
        return "GOOGLE_API_KEY env var not set"
    return None


def test_draft_from_request_gemini_round_trips_against_real_api(tmp_path: Path) -> None:
    """One real Gemini draft round-trip; shape-only.

    Builds a schema-only :class:`LLMRequest` for ``fct_orders`` (the
    same manifest fixture the offline neutrality test uses), drives
    :func:`draft_from_request` through ``provider="gemini"``, and
    asserts the returned :class:`CandidateSchema` validates plus a
    single :class:`LLMResponseEvent` was written with zero cache
    tokens (DEC-003 of #137).
    """
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    manifest = load(_MANIFEST_FIXTURE.parent, manifest_path=_MANIFEST_FIXTURE)
    model = manifest.get_model("model.sf_demo.fct_orders")

    # Schema-only LLMRequest — no warehouse needed. Columns match the
    # fct_orders fixture exactly so the anchor-contract validator
    # accepts a clean Gemini response.
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

    config = DraftConfig(
        provider="gemini",
        model="gemini-2.5-flash",
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
    )

    audit_path = tmp_path / "safety_audit.jsonl"

    outcome = draft_from_request(
        request,
        model,
        manifest,
        config=config,
        audit_path=audit_path,
        # _client=None ⇒ strategy.make_client() builds the real SDK client.
        _client=None,
    )

    # Shape-only assertions — no byte-level CandidateSchema content pins.
    assert isinstance(outcome, DraftOutcome)
    assert isinstance(outcome.candidate, CandidateSchema)
    assert outcome.candidate.name == model.name
    # Drafter is exactly one LLM call per model.
    assert outcome.result.input_tokens > 0
    # Capability flags False/False ⇒ no cache accounting (DEC-003 of #137).
    assert outcome.result.cache_creation_input_tokens == 0
    assert outcome.result.cache_read_input_tokens == 0
    assert outcome.result.model == "gemini-2.5-flash"

    # Response-audit JSONL: exactly one durable record with cache fields at 0.
    response_audit = audit_path.with_name("llm_responses.jsonl")
    assert response_audit.exists()
    lines = response_audit.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["model_unique_id"] == model.unique_id
    assert record["model"] == "gemini-2.5-flash"
    assert record["cache_creation_input_tokens"] == 0
    assert record["cache_read_input_tokens"] == 0
