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
    CandidateTestCustomSQL,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestRowCountBetween,
    CandidateTestUnique,
    CandidateTestUniqueCombination,
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


def test_candidate_test_custom_sql_rejects_empty_sql() -> None:
    """``CandidateTestCustomSQL.sql`` must be non-empty — the ``_sql_non_empty``
    field validator raises on an empty string (an empty failing-rows SELECT is
    meaningless)."""
    with pytest.raises(ValidationError):
        CandidateTestCustomSQL(sql="")


def test_candidate_test_custom_sql_accepts_non_empty_sql() -> None:
    """A non-empty ``sql`` passes the validator and is carried verbatim."""
    test = CandidateTestCustomSQL(sql="select 1 from t where x < 0")
    assert test.sql == "select 1 from t where x < 0"
    assert test.type == "custom_sql"


# ---------------------------------------------------------------------------
# CandidateTestRowCountBetween (#169, DEC-001 / DEC-008 / DEC-013)
# ---------------------------------------------------------------------------


def test_candidate_test_row_count_between_accepts_both_bounds() -> None:
    """Happy path: both bounds, no ``where``."""
    test = CandidateTestRowCountBetween(minimum=100, maximum=10_000)
    assert test.type == "row_count_between"
    assert test.column is None
    assert test.minimum == 100
    assert test.maximum == 10_000
    assert test.where is None
    assert test.rationale is None


def test_candidate_test_row_count_between_accepts_minimum_only() -> None:
    test = CandidateTestRowCountBetween(minimum=1)
    assert test.minimum == 1
    assert test.maximum is None


def test_candidate_test_row_count_between_accepts_maximum_only() -> None:
    test = CandidateTestRowCountBetween(maximum=1_000)
    assert test.minimum is None
    assert test.maximum == 1_000


def test_candidate_test_row_count_between_accepts_where_clause() -> None:
    test = CandidateTestRowCountBetween(
        minimum=10,
        where="event_date >= '2024-01-01'",
    )
    assert test.where == "event_date >= '2024-01-01'"


def test_candidate_test_row_count_between_rejects_negative_minimum() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountBetween(minimum=-1, maximum=10)


def test_candidate_test_row_count_between_rejects_negative_maximum() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountBetween(minimum=0, maximum=-1)


def test_candidate_test_row_count_between_accepts_zero_minimum() -> None:
    """``minimum=0`` is structurally valid (the grader will score it as
    vacuously broad — see #169 DEC-009 — but the model accepts it)."""
    test = CandidateTestRowCountBetween(minimum=0, maximum=100)
    assert test.minimum == 0


def test_candidate_test_row_count_between_rejects_both_bounds_none() -> None:
    """At-least-one-of-(minimum, maximum) — an unbounded row-count test
    carries no signal."""
    with pytest.raises(ValidationError):
        CandidateTestRowCountBetween()


def test_candidate_test_row_count_between_rejects_minimum_greater_than_maximum() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountBetween(minimum=100, maximum=10)


def test_candidate_test_row_count_between_minimum_equal_to_maximum_is_allowed() -> None:
    """``minimum == maximum`` expresses an exact row-count assertion."""
    test = CandidateTestRowCountBetween(minimum=42, maximum=42)
    assert test.minimum == test.maximum == 42


def test_candidate_test_row_count_between_rejects_empty_where() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountBetween(minimum=1, where="")


def test_candidate_test_row_count_between_rejects_whitespace_only_where() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountBetween(minimum=1, where="   \t\n  ")


def test_candidate_test_row_count_between_is_frozen() -> None:
    test = CandidateTestRowCountBetween(minimum=1)
    with pytest.raises(ValidationError):
        test.minimum = 2  # type: ignore[misc]


def test_candidate_test_row_count_between_round_trip_byte_stable() -> None:
    """``model_validate_json`` ∘ ``model_dump_json`` is a no-op on a
    populated variant — required for fixture-driven drift detection."""
    test = CandidateTestRowCountBetween(
        minimum=100,
        maximum=10_000,
        where="event_date >= '2024-01-01'",
        rationale="bounded volume guardrail",
    )
    raw = test.model_dump_json()
    reparsed = CandidateTestRowCountBetween.model_validate_json(raw)
    assert reparsed == test


def test_candidate_test_row_count_between_in_discriminated_union() -> None:
    """The variant resolves correctly through the discriminated union."""
    from pydantic import TypeAdapter

    adapter: TypeAdapter[CandidateTest] = TypeAdapter(CandidateTest)
    parsed = adapter.validate_python(
        {
            "type": "row_count_between",
            "column": None,
            "minimum": 100,
            "maximum": 10_000,
            "where": None,
        }
    )
    assert isinstance(parsed, CandidateTestRowCountBetween)
    assert parsed.minimum == 100


def test_candidate_test_row_count_between_column_must_be_none() -> None:
    """``column`` is hard-coded to ``None`` (model-level only); a
    non-``None`` value fails type-validation."""
    with pytest.raises(ValidationError):
        CandidateTestRowCountBetween(column="any_column", minimum=1)  # type: ignore[arg-type]


def test_candidate_test_row_count_between_extra_ignored() -> None:
    """``extra="ignore"`` is inherited from ``_BASE_CONFIG`` — an unknown
    field is silently dropped (forward-compat with future LLM emissions)."""
    test = CandidateTestRowCountBetween.model_validate(
        {"type": "row_count_between", "minimum": 1, "phantom_field": "x"}
    )
    assert not hasattr(test, "phantom_field")


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


# ---------------------------------------------------------------------------
# CandidateTestUniqueCombination (#170, DEC-001 / DEC-002 / DEC-014 / DEC-016)
# ---------------------------------------------------------------------------


def test_candidate_test_unique_combination_accepts_two_columns() -> None:
    """Happy path: minimum cardinality (2 columns), no ``where``."""
    test = CandidateTestUniqueCombination(columns=("order_id", "customer_id"))
    assert test.type == "unique_combination"
    assert test.column is None
    assert test.columns == ("order_id", "customer_id")
    assert test.where is None
    assert test.rationale is None


def test_candidate_test_unique_combination_accepts_three_columns_with_where() -> None:
    """Happy path: three columns plus a ``where`` filter and rationale."""
    test = CandidateTestUniqueCombination(
        columns=("order_id", "customer_id", "ordered_at"),
        where="ordered_at >= '2024-01-01'",
        rationale="Composite uniqueness per loaded date window.",
    )
    assert test.columns == ("order_id", "customer_id", "ordered_at")
    assert test.where == "ordered_at >= '2024-01-01'"
    assert test.rationale == "Composite uniqueness per loaded date window."


def test_candidate_test_unique_combination_rejects_single_column() -> None:
    """``len(columns) >= 2`` (DEC-016) — a single-column variant is just
    ``unique`` and carries no new signal."""
    with pytest.raises(ValidationError):
        CandidateTestUniqueCombination(columns=("order_id",))


def test_candidate_test_unique_combination_rejects_empty_columns() -> None:
    """``len(columns) >= 2`` (DEC-016) — zero columns is structurally
    meaningless."""
    with pytest.raises(ValidationError):
        CandidateTestUniqueCombination(columns=())


def test_candidate_test_unique_combination_rejects_duplicate_columns() -> None:
    """No-duplicates invariant (DEC-016) — a duplicate column would compile
    to a uniqueness test that always trivially has the same value in two
    positions; the LLM almost certainly meant something else."""
    with pytest.raises(ValidationError):
        CandidateTestUniqueCombination(columns=("order_id", "order_id"))


def test_candidate_test_unique_combination_rejects_duplicate_among_three() -> None:
    """No-duplicates fires even when only two of three columns clash."""
    with pytest.raises(ValidationError):
        CandidateTestUniqueCombination(
            columns=("order_id", "customer_id", "order_id"),
        )


def test_candidate_test_unique_combination_column_must_be_none() -> None:
    """``column`` is hard-coded to ``None`` (model-level only); a non-``None``
    value fails type-validation."""
    with pytest.raises(ValidationError):
        CandidateTestUniqueCombination(
            column="any_column",  # type: ignore[arg-type]
            columns=("a", "b"),
        )


def test_candidate_test_unique_combination_rejects_empty_where() -> None:
    """Empty ``where`` after strip is non-signal — fail loud (mirrors
    ``CandidateTestRowCountBetween._where_non_empty_when_set``)."""
    with pytest.raises(ValidationError):
        CandidateTestUniqueCombination(columns=("a", "b"), where="")


def test_candidate_test_unique_combination_rejects_whitespace_only_where() -> None:
    with pytest.raises(ValidationError):
        CandidateTestUniqueCombination(columns=("a", "b"), where="   \t\n  ")


def test_candidate_test_unique_combination_accepts_raw_identifier_strings() -> None:
    """Per-column identifier shape validation is deferred to the anchor-
    contract arm (DEC-014); Pydantic accepts raw strings here. A "weird"
    identifier like ``"col with space"`` passes Pydantic without complaint —
    US-004 will reject it at parse-time."""
    test = CandidateTestUniqueCombination(columns=("col with space", "another bad name"))
    assert test.columns == ("col with space", "another bad name")


def test_candidate_test_unique_combination_is_frozen() -> None:
    test = CandidateTestUniqueCombination(columns=("a", "b"))
    with pytest.raises(ValidationError):
        test.columns = ("a", "c")  # type: ignore[misc]


def test_candidate_test_unique_combination_round_trip_byte_stable() -> None:
    """``model_validate_json`` ∘ ``model_dump_json`` is a no-op on a
    populated variant — required for fixture-driven drift detection."""
    test = CandidateTestUniqueCombination(
        columns=("order_id", "customer_id"),
        where="ordered_at >= '2024-01-01'",
        rationale="Composite uniqueness per window.",
    )
    raw = test.model_dump_json()
    reparsed = CandidateTestUniqueCombination.model_validate_json(raw)
    assert reparsed == test


def test_candidate_test_unique_combination_in_discriminated_union() -> None:
    """The variant resolves correctly through the discriminated union."""
    adapter: TypeAdapter[CandidateTest] = TypeAdapter(CandidateTest)
    parsed = adapter.validate_python(
        {
            "type": "unique_combination",
            "column": None,
            "columns": ["order_id", "customer_id"],
            "where": None,
        }
    )
    assert isinstance(parsed, CandidateTestUniqueCombination)
    assert parsed.columns == ("order_id", "customer_id")


def test_candidate_test_unique_combination_extra_ignored() -> None:
    """``extra="ignore"`` is inherited from ``_BASE_CONFIG`` — an unknown
    field is silently dropped (forward-compat with future LLM emissions)."""
    test = CandidateTestUniqueCombination.model_validate(
        {
            "type": "unique_combination",
            "columns": ["a", "b"],
            "phantom_field": "x",
        }
    )
    assert not hasattr(test, "phantom_field")
