"""Real-API smoke test for :func:`signalforge.draft.draft_schema` via OpenAI.

Issue #136 / US-006 — DEC-001, DEC-005, DEC-008. Honours DEC-005's
"scope both grade AND draft explicitly" commitment that
``tests/grade/test_smoke_real_api_openai.py`` alone wouldn't cover at the
live level. Gated by the ``openai`` marker — excluded from default CI by
:file:`pyproject.toml`'s ``addopts = "... -m 'not openai'"``. Requires
``SF_RUN_OPENAI=1`` + ``OPENAI_API_KEY``.

What this proves end-to-end:

* The OpenAIProvider's Chat Completions adapter (DEC-001 / DEC-009)
  reaches the real ``gpt-4o`` and returns JSON parseable as
  :class:`CandidateSchema` under the drafter system prompt (whose
  ``response_format={"type": "json_object"}`` requirement, per
  ``llm-drafter.md`` § Open notes for implementation, must include the
  word "json" — verified at the LLM-seam side).
* The tolerant JSON extractor (`extract_json_payload`, llm-drafter.md
  issue #144) handles whatever prose preamble the OpenAI judge model
  emits before the JSON object.
* The response-audit JSONL is written under
  ``policy.audit_path.with_name("llm_responses.jsonl")`` and round-trips
  through :class:`LLMResponseEvent`.
* OpenAI's ``cache_*_input_tokens`` audit fields are ``0`` because
  ``OpenAIProvider.supports_prompt_caching`` is ``False`` (#135 /
  #136 capability gate). The seam does NOT emit the dual-zero
  cache-anomaly WARNING.

What this deliberately does NOT assert:

* Specific column descriptions / rationale wording — LLM output is not
  deterministic enough; the test would be flaky if pinned.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from signalforge.draft import draft_schema
from signalforge.draft.audit import LLMResponseEvent
from signalforge.draft.config import DraftConfig
from signalforge.draft.models import CandidateSchema
from signalforge.draft.schema import DraftOutcome
from signalforge.manifest.models import Column, Manifest, Model
from signalforge.safety.policy import SafetyPolicy
from tests.safety._fake_adapter import FakeAdapter

pytestmark = pytest.mark.openai

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _openai_runs_enabled() -> bool:
    """``SF_RUN_OPENAI`` is set to a truthy value (mirrors ``SF_RUN_BQ`` / ``SF_RUN_SNOWFLAKE``)."""
    return os.environ.get("SF_RUN_OPENAI", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` only when both gates are satisfied — the test then
    proceeds to make a real OpenAI call. Each missing prerequisite
    yields its own distinct reason so a maintainer running
    ``pytest -m openai`` sees exactly what to set. Treat an empty /
    whitespace-only ``OPENAI_API_KEY`` as "unset" (an empty value would
    otherwise reach the API and produce a noisy auth failure).
    """
    if not _openai_runs_enabled():
        return "SF_RUN_OPENAI=1 required (live test calls the real OpenAI API)"
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return "OPENAI_API_KEY required (live test authenticates against the real OpenAI API)"
    return None


def test_draft_schema_real_openai_api_smoke(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One real OpenAI draft round-trip against a tiny in-test manifest.

    Builds a minimal :class:`Model` / :class:`Manifest`, runs the safety
    layer in schema-only mode (no adapter calls — see
    ``safety-layer.md`` DEC-012(c) / safety/request.py) and issues a
    single ``draft_schema`` call against ``gpt-4o``. Asserts the typed
    :class:`CandidateSchema` validates and the per-call
    :class:`LLMResponseEvent` JSONL row carries zero cache tokens (the
    #136 capability-gate contract).
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    # Minimal in-test fixture — one column with an obvious data type so the
    # LLM has something to draft against. Schema-only mode means the
    # adapter is never invoked, so a do-nothing ``FakeAdapter()`` is fine.
    model = Model(
        unique_id="model.sf_smoke.dim_users",
        name="dim_users",
        resource_type="model",
        package_name="sf_smoke",
        original_file_path="models/marts/dim_users.sql",
        path="marts/dim_users.sql",
        database="sf-smoke-proj",
        schema="main",  # type: ignore[call-arg]
        columns={
            "user_id": Column(name="user_id", data_type="INT64"),
            "email": Column(name="email", data_type="STRING"),
        },
        raw_code="select 1 as user_id, 'a@b.com' as email",
    )
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={model.unique_id: model},
    )

    # Schema-only policy with a per-run audit path under tmp_path. The
    # safety layer writes ``.../audit.jsonl``; the draft layer derives
    # ``.../llm_responses.jsonl`` (DEC-006) next to it.
    audit_path = tmp_path / "audit.jsonl"
    policy = SafetyPolicy(audit_path=audit_path)
    adapter = FakeAdapter()

    # OpenAI drafter: gpt-4o (the DEC-004 default for live smokes). The
    # provider field on DraftConfig is the #135 plug-in seam.
    config = DraftConfig(provider="openai", model="gpt-4o")

    with caplog.at_level(logging.WARNING):
        outcome = draft_schema(model, adapter, policy, manifest, config=config)

    # Shape-only assertions on the typed outcome.
    assert isinstance(outcome, DraftOutcome)
    assert isinstance(outcome.candidate, CandidateSchema)
    # The drafter is anchored to the model's columns — every drafted
    # column must be a real one. The parser already enforces this via
    # the anchor contract (``llm-drafter.md`` DEC-003); the assertion
    # here documents the contract for the smoke-test reader.
    drafted_names = {c.name for c in outcome.candidate.columns}
    assert drafted_names, "expected the drafter to emit at least one column"
    assert drafted_names.issubset({"user_id", "email"})

    # Response-audit JSONL exists next to the safety audit (DEC-006).
    response_audit = audit_path.with_name("llm_responses.jsonl")
    assert response_audit.exists()
    audit_lines = response_audit.read_text(encoding="utf-8").strip().splitlines()
    assert len(audit_lines) >= 1
    # Last line is this run's record — round-trip it through the typed
    # model and assert the OpenAI capability-gate contract.
    event = LLMResponseEvent.model_validate_json(audit_lines[-1])
    assert event.model == "gpt-4o"
    assert event.model_unique_id == model.unique_id
    # OpenAI has no equivalent of Anthropic's prompt-cache discount —
    # ``OpenAIProvider.supports_prompt_caching`` is ``False`` so both
    # cache-token fields must land at zero (#136 DEC-008 capability-gate).
    assert event.cache_creation_input_tokens == 0
    assert event.cache_read_input_tokens == 0

    # The dual-zero cache-anomaly WARNING (``llm-drafter.md`` DEC-014)
    # is capability-gated off for providers with
    # ``supports_prompt_caching=False`` and must NEVER fire here.
    cache_warning_messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and "cache marker no-op" in record.getMessage()
    ]
    assert not cache_warning_messages, (
        f"unexpected cache-anomaly WARNING(s) on OpenAI path "
        f"(supports_prompt_caching=False should gate this off): {cache_warning_messages}"
    )
