"""Drift detector for ``CandidateSchema`` and ``LLMResponseEvent`` (US-014).

Production reads ``CandidateSchema`` and ``LLMResponseEvent`` back with
``extra="ignore"`` for forward-compat (DEC-010 / DEC-013). Pair every such
reader-shaped model with a one-off ``extra="forbid"`` strict variant and
validate it against a committed fixture: adding a field to production
without updating the fixture OR the strict model breaks the test loudly.

Mirrors the precedent set by
:mod:`tests.safety.test_drift_detector.StrictAuditEvent`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

_STRICT_BASE = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# StrictCandidateSchema family — mirrors src/signalforge/draft/models.py
# ---------------------------------------------------------------------------


class StrictCandidateTestNotNull(BaseModel):
    model_config = _STRICT_BASE
    type: Literal["not_null"] = "not_null"
    column: str
    rationale: str | None = None


class StrictCandidateTestUnique(BaseModel):
    model_config = _STRICT_BASE
    type: Literal["unique"] = "unique"
    column: str
    rationale: str | None = None


class StrictCandidateTestAcceptedValues(BaseModel):
    model_config = _STRICT_BASE
    type: Literal["accepted_values"] = "accepted_values"
    column: str
    values: tuple[str, ...]
    rationale: str | None = None


class StrictCandidateTestRelationships(BaseModel):
    model_config = _STRICT_BASE
    type: Literal["relationships"] = "relationships"
    column: str
    to: str
    field: str
    rationale: str | None = None


class StrictCandidateTestCustomSQL(BaseModel):
    model_config = _STRICT_BASE
    type: Literal["custom_sql"] = "custom_sql"
    sql: str
    column: str | None = None
    rationale: str | None = None


_StrictCandidateTest = Annotated[
    StrictCandidateTestNotNull
    | StrictCandidateTestUnique
    | StrictCandidateTestAcceptedValues
    | StrictCandidateTestRelationships
    | StrictCandidateTestCustomSQL,
    Field(discriminator="type"),
]


class StrictCandidateColumn(BaseModel):
    model_config = _STRICT_BASE
    name: str
    description: str
    rationale: str | None = None
    tests: tuple[_StrictCandidateTest, ...] = ()
    meta: dict[str, Any] | None = None


class StrictCandidateSchema(BaseModel):
    """Mirror of production :class:`signalforge.draft.models.CandidateSchema`
    with ``extra="forbid"``.

    If you add a field to ``CandidateSchema`` (or any of the
    ``CandidateTest*`` variants / ``CandidateColumn``), you MUST:

    1. Add it here, AND
    2. Update ``tests/fixtures/draft/candidate_schema_v1.json`` (regenerated
       via ``tests/fixtures/draft/regenerate.sh`` once US-015 lands).
    """

    model_config = _STRICT_BASE
    schema_version: int = 1
    name: str
    description: str
    rationale: str | None = None
    columns: tuple[StrictCandidateColumn, ...]
    tests: tuple[_StrictCandidateTest, ...] = ()


# ---------------------------------------------------------------------------
# StrictLLMResponseEvent — mirrors src/signalforge/draft/audit.py
# ---------------------------------------------------------------------------


class StrictLLMResponseEvent(BaseModel):
    """Mirror of production :class:`signalforge.draft.audit.LLMResponseEvent`
    with ``extra="forbid"``.

    If you add a field to ``LLMResponseEvent`` (signalforge.draft.audit),
    you MUST:

    1. Add it here, AND
    2. Update ``tests/fixtures/draft/llm_response_audit_sample.jsonl``
       (regenerated via ``tests/fixtures/draft/regenerate.sh`` once US-015
       lands).
    """

    model_config = _STRICT_BASE
    timestamp: datetime
    model_unique_id: str
    prompt_version: str
    response_text_hash: str
    parsed_schema_hash: str
    sent_sql_hash: str
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    input_tokens: int
    output_tokens: int
    model: str
    signalforge_version: str
    audit_schema_version: int = 1


# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------


_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "draft"
_CANDIDATE_SCHEMA_FIXTURE = _FIXTURE_DIR / "candidate_schema_v1.json"
_LLM_RESPONSE_FIXTURE = _FIXTURE_DIR / "llm_response_audit_sample.jsonl"


# ---------------------------------------------------------------------------
# Tests — fixture validates against strict model
# ---------------------------------------------------------------------------


def test_candidate_schema_extra_forbid_against_fixture() -> None:
    """Validate the committed candidate-schema fixture against the strict
    model. Failure means production grew a field without updating either
    the fixture or :class:`StrictCandidateSchema` above.
    """
    payload = json.loads(_CANDIDATE_SCHEMA_FIXTURE.read_text(encoding="utf-8"))
    StrictCandidateSchema.model_validate(payload)


def test_candidate_schema_drift_detector_rejects_unknown_field() -> None:
    payload = json.loads(_CANDIDATE_SCHEMA_FIXTURE.read_text(encoding="utf-8"))
    payload["phantom_field"] = "x"
    with pytest.raises(ValidationError):
        StrictCandidateSchema.model_validate(payload)


def test_candidate_schema_strict_model_field_set_matches_production() -> None:
    """Production :class:`CandidateSchema` and :class:`StrictCandidateSchema`
    must declare the same field set."""
    from signalforge.draft.models import CandidateSchema

    prod_fields = set(CandidateSchema.model_fields.keys())
    strict_fields = set(StrictCandidateSchema.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictCandidateSchema is missing fields present in CandidateSchema: "
        f"{missing_in_strict}. Update StrictCandidateSchema to match."
    )
    assert not extra_in_strict, (
        f"StrictCandidateSchema has fields absent from CandidateSchema: "
        f"{extra_in_strict}. Remove from StrictCandidateSchema or add to "
        "CandidateSchema."
    )


def test_candidate_column_strict_model_field_set_matches_production() -> None:
    """Production :class:`CandidateColumn` and
    :class:`StrictCandidateColumn` must declare the same field set."""
    from signalforge.draft.models import CandidateColumn

    prod_fields = set(CandidateColumn.model_fields.keys())
    strict_fields = set(StrictCandidateColumn.model_fields.keys())
    missing = prod_fields - strict_fields
    extra = strict_fields - prod_fields
    assert not missing, f"StrictCandidateColumn missing: {missing}"
    assert not extra, f"StrictCandidateColumn has extra: {extra}"


def test_llm_response_event_extra_forbid_against_fixture() -> None:
    """Validate the committed audit-record fixture against the strict
    model. Failure means production grew a field without updating either
    the fixture or :class:`StrictLLMResponseEvent` above.
    """
    line = _LLM_RESPONSE_FIXTURE.read_text(encoding="utf-8").strip()
    assert line, f"expected one JSON line in {_LLM_RESPONSE_FIXTURE}"
    payload = json.loads(line)
    StrictLLMResponseEvent.model_validate(payload)


def test_llm_response_event_drift_detector_rejects_unknown_field() -> None:
    line = _LLM_RESPONSE_FIXTURE.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    payload["phantom_field"] = "x"
    with pytest.raises(ValidationError):
        StrictLLMResponseEvent.model_validate(payload)


def test_llm_response_event_strict_model_field_set_matches_production() -> None:
    """Production :class:`LLMResponseEvent` and
    :class:`StrictLLMResponseEvent` must declare the same field set."""
    from signalforge.draft.audit import LLMResponseEvent

    prod_fields = set(LLMResponseEvent.model_fields.keys())
    strict_fields = set(StrictLLMResponseEvent.model_fields.keys())
    missing = prod_fields - strict_fields
    extra = strict_fields - prod_fields
    assert not missing, (
        f"StrictLLMResponseEvent missing fields present in LLMResponseEvent: "
        f"{missing}. Update StrictLLMResponseEvent to match."
    )
    assert not extra, (
        f"StrictLLMResponseEvent has fields absent from LLMResponseEvent: "
        f"{extra}. Remove or add to LLMResponseEvent."
    )
