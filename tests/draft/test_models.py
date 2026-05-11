"""Tests for ``signalforge.draft.models`` (US-008).

Covers fixture round-trip, the discriminated test-type union, validator
rejection cases, and the ``extra="ignore"`` forward-compat behaviour.
The drift detector (one-off ``extra="forbid"`` model) lands in US-014.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTest,
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestUnique,
)

_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "draft" / "llm_response_valid.json"
)


def test_candidate_schema_round_trip_via_fixture() -> None:
    raw = _FIXTURE_PATH.read_text()
    parsed = CandidateSchema.model_validate_json(raw)
    dumped = parsed.model_dump_json()
    reparsed = CandidateSchema.model_validate_json(dumped)
    assert reparsed == parsed


def test_candidate_schema_version_default_is_1() -> None:
    schema = CandidateSchema(name="x", description="x", columns=())
    assert schema.schema_version == 1


def test_candidate_test_discriminator_rejects_unknown_type() -> None:
    adapter: TypeAdapter[CandidateTest] = TypeAdapter(CandidateTest)
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "phantom", "column": "c"})


def test_candidate_test_accepted_values_rejects_empty_values() -> None:
    with pytest.raises(ValidationError):
        CandidateTestAcceptedValues(column="x", values=())


@pytest.mark.parametrize(
    ("cls", "kwargs"),
    [
        (CandidateTestNotNull, {"column": "c"}),
        (CandidateTestUnique, {"column": "c"}),
        (CandidateTestAcceptedValues, {"column": "c", "values": ("a",)}),
        (
            CandidateTestRelationships,
            {"column": "c", "to": "ref('t')", "field": "id"},
        ),
    ],
)
def test_candidate_test_each_variant_carries_optional_rationale(
    cls: type[BaseModel], kwargs: dict[str, object]
) -> None:
    # rationale=None
    instance_no_rationale = cls(**kwargs)
    assert instance_no_rationale.rationale is None  # type: ignore[attr-defined]
    # rationale="..."
    instance_with_rationale = cls(**kwargs, rationale="because")
    assert instance_with_rationale.rationale == "because"  # type: ignore[attr-defined]


def test_candidate_column_columns_immutable_tuple() -> None:
    schema = CandidateSchema(
        name="m",
        description="d",
        columns=(CandidateColumn(name="c", description="d"),),
    )
    assert schema.columns.__class__ is tuple
    with pytest.raises(ValidationError):
        # frozen=True — assignment to fields raises a ValidationError.
        schema.columns = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("cls", "kwargs"),
    [
        (CandidateTestNotNull, {}),
        (CandidateTestUnique, {}),
        (CandidateTestAcceptedValues, {"values": ("a",)}),
        (CandidateTestRelationships, {"to": "ref('t')", "field": "id"}),
    ],
)
def test_candidate_test_column_field_rejects_empty_string(
    cls: type[BaseModel], kwargs: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        cls(column="", **kwargs)


def test_candidate_test_relationships_requires_to_and_field() -> None:
    # Empty `to`
    with pytest.raises(ValidationError):
        CandidateTestRelationships(column="x", to="", field="id")
    # Empty `field`
    with pytest.raises(ValidationError):
        CandidateTestRelationships(column="x", to="ref('t')", field="")
    # Missing `to` entirely
    with pytest.raises(ValidationError):
        CandidateTestRelationships(column="x", field="id")  # type: ignore[call-arg]
    # Missing `field` entirely
    with pytest.raises(ValidationError):
        CandidateTestRelationships(column="x", to="ref('t')")  # type: ignore[call-arg]


def test_candidate_schema_extra_ignore_drops_unknown_field() -> None:
    payload = {
        "name": "m",
        "description": "d",
        "columns": [],
        "unknown_field": 42,
    }
    schema = CandidateSchema.model_validate(payload)
    assert not hasattr(schema, "unknown_field")


def test_candidate_test_round_trip_via_fixture_includes_all_four_types() -> None:
    raw = json.loads(_FIXTURE_PATH.read_text())

    seen_types: set[str] = set()
    for col in raw.get("columns", []):
        for t in col.get("tests", []):
            seen_types.add(t["type"])
    for t in raw.get("tests", []):
        seen_types.add(t["type"])

    assert seen_types == {"not_null", "unique", "accepted_values", "relationships"}
