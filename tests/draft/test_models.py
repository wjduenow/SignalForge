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
    CandidateTestRowCountAnomalyByPeriod,
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


# --- Custom __repr__ redaction (US-002, DEC-013) --------------------------
# Three text-bearing candidate variants — ``where`` / ``sql`` /
# ``rationale`` — get a redacted ``__repr__`` that omits the LLM-emitted
# text. Mirrors ``DiffEntry``/``DiffReport``/``GradingResult`` redaction
# established by prune DEC-022 / grade DEC-022 / diff DEC-020. Only
# ``__repr__`` is overridden; Pydantic ``__str__`` / serialisation paths
# (``model_dump_json``) still carry the text fields.


def test_candidate_test_custom_sql_repr_omits_sql_and_rationale() -> None:
    """:meth:`CandidateTestCustomSQL.__repr__` does NOT leak ``sql`` or
    ``rationale`` (DEC-013). An accidental ``_LOGGER.warning("test: %s", t)``
    would otherwise dump the full LLM-authored SELECT body into log sinks."""
    test = CandidateTestCustomSQL(
        sql="SELECT SECRET_SQL_BODY FROM t",
        column=None,
        rationale="SECRET_RATIONALE_TEXT",
    )
    rendered = repr(test)
    assert "SECRET_SQL_BODY" not in rendered, (
        "CandidateTestCustomSQL.__repr__ must omit the LLM-emitted sql body (DEC-013)"
    )
    assert "SECRET_RATIONALE_TEXT" not in rendered, (
        "CandidateTestCustomSQL.__repr__ must omit the LLM-emitted rationale (DEC-013)"
    )
    # The identifying surface is still visible.
    assert "custom_sql" in rendered
    assert "model-level" in rendered or "model" in rendered


def test_candidate_test_custom_sql_repr_with_column_scope() -> None:
    """Column-scoped ``custom_sql`` exposes the column NAME (operationally
    useful, not value-bearing) while still hiding the sql body."""
    test = CandidateTestCustomSQL(
        sql="SELECT SECRET FROM t",
        column="user_id",
        rationale="SECRET_RAT",
    )
    rendered = repr(test)
    assert "SECRET" not in rendered
    assert "user_id" in rendered


def test_candidate_test_custom_sql_model_dump_json_still_carries_text() -> None:
    """:meth:`model_dump_json` (serialisation path) is UNCHANGED — only
    ``__repr__`` redacts. Pydantic ``__str__`` is reserved for serialisation
    (DEC-013); overriding it would corrupt the audit-log JSON round-trip."""
    test = CandidateTestCustomSQL(
        sql="SELECT SECRET_SQL_BODY FROM t",
        column=None,
        rationale="SECRET_RATIONALE_TEXT",
    )
    dumped = test.model_dump_json()
    assert "SECRET_SQL_BODY" in dumped
    assert "SECRET_RATIONALE_TEXT" in dumped


def test_candidate_test_row_count_between_repr_omits_where_and_rationale() -> None:
    """:meth:`CandidateTestRowCountBetween.__repr__` does NOT leak ``where``
    or ``rationale`` (DEC-013, retroactive). The numeric bounds stay visible
    (not value-bearing, operationally useful)."""
    test = CandidateTestRowCountBetween(
        minimum=1,
        maximum=100,
        where="SECRET_WHERE_FRAGMENT = 'foo'",
        rationale="SECRET_RATIONALE_TEXT",
    )
    rendered = repr(test)
    assert "SECRET_WHERE_FRAGMENT" not in rendered, (
        "CandidateTestRowCountBetween.__repr__ must omit the LLM-emitted where (DEC-013)"
    )
    assert "SECRET_RATIONALE_TEXT" not in rendered, (
        "CandidateTestRowCountBetween.__repr__ must omit the LLM-emitted rationale (DEC-013)"
    )
    # The numeric bounds + type stay visible (operationally useful).
    assert "row_count_between" in rendered
    assert "1" in rendered
    assert "100" in rendered


def test_candidate_test_row_count_between_model_dump_json_still_carries_text() -> None:
    """:meth:`model_dump_json` is unchanged — serialisation still carries
    ``where`` / ``rationale``."""
    test = CandidateTestRowCountBetween(
        minimum=1,
        maximum=100,
        where="SECRET_WHERE_FRAGMENT = 'foo'",
        rationale="SECRET_RATIONALE_TEXT",
    )
    dumped = test.model_dump_json()
    assert "SECRET_WHERE_FRAGMENT" in dumped
    assert "SECRET_RATIONALE_TEXT" in dumped


def test_candidate_test_unique_combination_repr_omits_where_and_rationale() -> None:
    """:meth:`CandidateTestUniqueCombination.__repr__` does NOT leak ``where``
    or ``rationale`` (DEC-013). The ``columns`` tuple stays visible — column
    NAMES are not value-bearing."""
    test = CandidateTestUniqueCombination(
        columns=("order_id", "customer_id"),
        where="SECRET_WHERE_FRAGMENT = 'bar'",
        rationale="SECRET_RATIONALE_TEXT",
    )
    rendered = repr(test)
    assert "SECRET_WHERE_FRAGMENT" not in rendered, (
        "CandidateTestUniqueCombination.__repr__ must omit the LLM-emitted where (DEC-013)"
    )
    assert "SECRET_RATIONALE_TEXT" not in rendered, (
        "CandidateTestUniqueCombination.__repr__ must omit the LLM-emitted rationale (DEC-013)"
    )
    # The columns tuple + type stay visible.
    assert "unique_combination" in rendered
    assert "order_id" in rendered
    assert "customer_id" in rendered


def test_candidate_test_unique_combination_model_dump_json_still_carries_text() -> None:
    """:meth:`model_dump_json` is unchanged — serialisation still carries
    ``where`` / ``rationale``."""
    test = CandidateTestUniqueCombination(
        columns=("order_id", "customer_id"),
        where="SECRET_WHERE_FRAGMENT = 'bar'",
        rationale="SECRET_RATIONALE_TEXT",
    )
    dumped = test.model_dump_json()
    assert "SECRET_WHERE_FRAGMENT" in dumped
    assert "SECRET_RATIONALE_TEXT" in dumped


def test_candidate_test_repr_redaction_holds_under_ansi_injection() -> None:
    """The redacted repr does not interpret ANSI escapes / control chars
    embedded in the LLM-emitted text fields — defence-in-depth so a hostile
    payload like ``where="\\x1b[31mEVIL\\x1b[0m"`` cannot leak into a log
    viewer via ``repr()``. (The strip happens implicitly: ``repr()`` does
    not include the field, so the bytes never appear regardless of content.)"""
    test = CandidateTestUniqueCombination(
        columns=("a", "b"),
        where="\x1b[31mEVIL_ANSI\x1b[0m",
        rationale="\x1b[33mEVIL_RAT\x1b[0m",
    )
    rendered = repr(test)
    assert "EVIL_ANSI" not in rendered
    assert "EVIL_RAT" not in rendered
    assert "\x1b" not in rendered


def test_candidate_test_repr_args_redacted_for_rich_pretty_hooks() -> None:
    """QG Pass 1 finding C1: Pydantic v2's ``__rich_repr__()`` /
    ``__pretty__()`` reach through ``__repr_args__()`` rather than ``repr()``,
    so a custom ``__repr__`` alone leaves the redacted fields visible to
    ``rich.print()`` / ``devtools.pretty()`` / structured-debug tooling.
    DEC-013's "closes the latent log-hygiene gap" intent requires the
    structured hook to be filtered too. Pinned across all three redacting
    variants (CustomSQL / RowCountBetween / UniqueCombination)."""

    # CandidateTestCustomSQL — redacts sql + rationale via __repr_args__
    sql_test = CandidateTestCustomSQL(
        sql="SELECT SECRET_SQL FROM t",
        column="x",
        rationale="SECRET_CUSTOMSQL_RAT",
    )
    sql_args = list(sql_test.__repr_args__())
    sql_args_str = repr(sql_args)
    assert "SECRET_SQL" not in sql_args_str
    assert "SECRET_CUSTOMSQL_RAT" not in sql_args_str
    safe_fields = {name for name, _ in sql_args}
    assert "sql" not in safe_fields
    assert "rationale" not in safe_fields
    assert "type" in safe_fields
    assert "column" in safe_fields

    # CandidateTestRowCountBetween — redacts where + rationale
    rcb_test = CandidateTestRowCountBetween(
        minimum=1,
        maximum=10,
        where="SECRET_RCB_WHERE",
        rationale="SECRET_RCB_RAT",
    )
    rcb_args = list(rcb_test.__repr_args__())
    rcb_args_str = repr(rcb_args)
    assert "SECRET_RCB_WHERE" not in rcb_args_str
    assert "SECRET_RCB_RAT" not in rcb_args_str
    rcb_safe = {name for name, _ in rcb_args}
    assert "where" not in rcb_safe
    assert "rationale" not in rcb_safe
    assert {"type", "column", "minimum", "maximum"} <= rcb_safe

    # CandidateTestUniqueCombination — redacts where + rationale; columns visible
    uc_test = CandidateTestUniqueCombination(
        columns=("a", "b"),
        where="SECRET_UC_WHERE",
        rationale="SECRET_UC_RAT",
    )
    uc_args = list(uc_test.__repr_args__())
    uc_args_str = repr(uc_args)
    assert "SECRET_UC_WHERE" not in uc_args_str
    assert "SECRET_UC_RAT" not in uc_args_str
    uc_safe = {name for name, _ in uc_args}
    assert "where" not in uc_safe
    assert "rationale" not in uc_safe
    assert {"type", "column", "columns"} <= uc_safe

    # And model_dump_json() still carries the secrets (serialisation unchanged)
    assert "SECRET_SQL" in sql_test.model_dump_json()
    assert "SECRET_RCB_WHERE" in rcb_test.model_dump_json()
    assert "SECRET_UC_WHERE" in uc_test.model_dump_json()


# ---------------------------------------------------------------------------
# CandidateTestRowCountAnomalyByPeriod (#171, DEC-007)
# ---------------------------------------------------------------------------


def test_candidate_test_row_count_anomaly_by_period_happy_path_defaults() -> None:
    """Minimal happy path: only the required ``date_column``; every other
    field takes its default."""
    test = CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at")
    assert test.type == "row_count_anomaly_by_period"
    assert test.column is None
    assert test.date_column == "ordered_at"
    assert test.period == "day"
    assert test.lookback_periods == 28
    assert test.method == "mad"
    assert test.seasonality == "none"
    assert test.threshold == 3.0
    assert test.min_samples_per_bucket == 3
    assert test.where is None
    assert test.rationale is None


def test_candidate_test_row_count_anomaly_by_period_accepts_full_fields() -> None:
    test = CandidateTestRowCountAnomalyByPeriod(
        date_column="created_at",
        period="hour",
        lookback_periods=72,
        method="zscore",
        seasonality="dow",
        threshold=2.5,
        min_samples_per_bucket=5,
        where="status = 'active'",
        rationale="Hourly anomaly band with weekly seasonality.",
    )
    assert test.date_column == "created_at"
    assert test.period == "hour"
    assert test.lookback_periods == 72
    assert test.method == "zscore"
    assert test.seasonality == "dow"
    assert test.threshold == 2.5
    assert test.min_samples_per_bucket == 5
    assert test.where == "status = 'active'"
    assert test.rationale == "Hourly anomaly band with weekly seasonality."


def test_candidate_test_row_count_anomaly_by_period_rejects_empty_date_column() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(date_column="")


def test_candidate_test_row_count_anomaly_by_period_rejects_whitespace_date_column() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(date_column="   \t\n  ")


def test_candidate_test_row_count_anomaly_by_period_rejects_zero_lookback() -> None:
    """``lookback_periods >= 1`` — zero history yields no anomaly band."""
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(date_column="d", lookback_periods=0)


def test_candidate_test_row_count_anomaly_by_period_rejects_negative_lookback() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(date_column="d", lookback_periods=-1)


def test_candidate_test_row_count_anomaly_by_period_accepts_lookback_one() -> None:
    """Boundary: ``lookback_periods=1`` is the minimum allowed (a one-period
    "compare to yesterday" anomaly recipe)."""
    test = CandidateTestRowCountAnomalyByPeriod(date_column="d", lookback_periods=1)
    assert test.lookback_periods == 1


def test_candidate_test_row_count_anomaly_by_period_rejects_zero_min_samples() -> None:
    """``min_samples_per_bucket >= 1`` — zero samples cannot drive a band."""
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(date_column="d", min_samples_per_bucket=0)


def test_candidate_test_row_count_anomaly_by_period_rejects_negative_min_samples() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(date_column="d", min_samples_per_bucket=-1)


@pytest.mark.parametrize("method", ["mad", "zscore", "percentile"])
def test_candidate_test_row_count_anomaly_by_period_rejects_zero_threshold_for_band_methods(
    method: str,
) -> None:
    """``threshold > 0`` for ``method in {mad, zscore, percentile}`` (DEC-007)
    — a zero or negative band multiplier collapses the anomaly window."""
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(
            date_column="d",
            method=method,  # type: ignore[arg-type]
            threshold=0.0,
        )


@pytest.mark.parametrize("method", ["mad", "zscore", "percentile"])
def test_candidate_test_row_count_anomaly_by_period_rejects_negative_threshold_for_band_methods(
    method: str,
) -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(
            date_column="d",
            method=method,  # type: ignore[arg-type]
            threshold=-1.0,
        )


def test_candidate_test_row_count_anomaly_by_period_min_max_accepts_zero_threshold() -> None:
    """``method=min_max`` IGNORES ``threshold`` (DEC-007) — any value
    accepted, including ``0`` and negatives."""
    test = CandidateTestRowCountAnomalyByPeriod(
        date_column="d",
        method="min_max",
        threshold=0.0,
    )
    assert test.method == "min_max"
    assert test.threshold == 0.0


def test_candidate_test_row_count_anomaly_by_period_min_max_accepts_negative_threshold() -> None:
    """``method=min_max`` IGNORES ``threshold`` — a negative value is
    accepted (it's unused by the compile arm)."""
    test = CandidateTestRowCountAnomalyByPeriod(
        date_column="d",
        method="min_max",
        threshold=-99.0,
    )
    assert test.threshold == -99.0


def test_candidate_test_row_count_anomaly_by_period_rejects_empty_where() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(date_column="d", where="")


def test_candidate_test_row_count_anomaly_by_period_rejects_whitespace_only_where() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(date_column="d", where="   \t\n  ")


def test_candidate_test_row_count_anomaly_by_period_accepts_where_clause() -> None:
    test = CandidateTestRowCountAnomalyByPeriod(
        date_column="d",
        where="status = 'active'",
    )
    assert test.where == "status = 'active'"


def test_candidate_test_row_count_anomaly_by_period_column_must_be_none() -> None:
    """``column`` is hard-coded to ``None`` (model-level only); a
    non-``None`` value fails type-validation."""
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(
            column="any_column",  # type: ignore[arg-type]
            date_column="d",
        )


def test_candidate_test_row_count_anomaly_by_period_rejects_unknown_period() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(
            date_column="d",
            period="month",  # type: ignore[arg-type]
        )


def test_candidate_test_row_count_anomaly_by_period_rejects_unknown_method() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(
            date_column="d",
            method="bayesian",  # type: ignore[arg-type]
        )


def test_candidate_test_row_count_anomaly_by_period_rejects_unknown_seasonality() -> None:
    with pytest.raises(ValidationError):
        CandidateTestRowCountAnomalyByPeriod(
            date_column="d",
            seasonality="quarterly",  # type: ignore[arg-type]
        )


def test_candidate_test_row_count_anomaly_by_period_is_frozen() -> None:
    test = CandidateTestRowCountAnomalyByPeriod(date_column="d")
    with pytest.raises(ValidationError):
        test.threshold = 5.0  # type: ignore[misc]


def test_candidate_test_row_count_anomaly_by_period_round_trip_byte_stable() -> None:
    """``model_validate_json`` ∘ ``model_dump_json`` is a no-op on a
    populated variant — required for fixture-driven drift detection."""
    test = CandidateTestRowCountAnomalyByPeriod(
        date_column="created_at",
        period="week",
        lookback_periods=12,
        method="percentile",
        seasonality="dow",
        threshold=2.0,
        min_samples_per_bucket=4,
        where="status = 'active'",
        rationale="weekly anomaly with dow seasonality",
    )
    raw = test.model_dump_json()
    reparsed = CandidateTestRowCountAnomalyByPeriod.model_validate_json(raw)
    assert reparsed == test


def test_candidate_test_row_count_anomaly_by_period_in_discriminated_union() -> None:
    """The variant resolves correctly through the discriminated union."""
    adapter: TypeAdapter[CandidateTest] = TypeAdapter(CandidateTest)
    parsed = adapter.validate_python(
        {
            "type": "row_count_anomaly_by_period",
            "column": None,
            "date_column": "ordered_at",
            "period": "day",
            "lookback_periods": 28,
            "method": "mad",
            "seasonality": "none",
            "threshold": 3.0,
            "min_samples_per_bucket": 3,
            "where": None,
        }
    )
    assert isinstance(parsed, CandidateTestRowCountAnomalyByPeriod)
    assert parsed.date_column == "ordered_at"


def test_candidate_test_row_count_anomaly_by_period_extra_ignored() -> None:
    """``extra="ignore"`` is inherited from ``_BASE_CONFIG`` — an unknown
    field is silently dropped (forward-compat with future LLM emissions)."""
    test = CandidateTestRowCountAnomalyByPeriod.model_validate(
        {
            "type": "row_count_anomaly_by_period",
            "date_column": "d",
            "phantom_field": "x",
        }
    )
    assert not hasattr(test, "phantom_field")


def test_candidate_test_row_count_anomaly_by_period_repr_omits_where_and_rationale() -> None:
    """:meth:`__repr__` shows only ``(type, column, method, seasonality)``;
    omits the LLM-emitted ``where`` and ``rationale`` (DEC-013 of #170)."""
    test = CandidateTestRowCountAnomalyByPeriod(
        date_column="created_at",
        method="zscore",
        seasonality="dow",
        where="SECRET_WHERE_FRAGMENT = 'foo'",
        rationale="SECRET_RATIONALE_TEXT",
    )
    rendered = repr(test)
    assert "SECRET_WHERE_FRAGMENT" not in rendered, (
        "CandidateTestRowCountAnomalyByPeriod.__repr__ must omit the LLM-emitted where"
    )
    assert "SECRET_RATIONALE_TEXT" not in rendered, (
        "CandidateTestRowCountAnomalyByPeriod.__repr__ must omit the LLM-emitted rationale"
    )
    # The identifying surface stays visible.
    assert "row_count_anomaly_by_period" in rendered
    assert "zscore" in rendered
    assert "dow" in rendered


def test_candidate_test_row_count_anomaly_by_period_repr_args_redaction() -> None:
    """QG Pass 1 finding C1 — ``__repr_args__`` redacts ``where`` +
    ``rationale`` so ``rich.print()`` / ``devtools.pretty()`` /
    ``pprint`` all inherit the filter. Pinned per memory
    `pydantic-v2-repr-args-redaction-required`."""
    test = CandidateTestRowCountAnomalyByPeriod(
        date_column="created_at",
        method="mad",
        seasonality="none",
        where="SECRET_ANOM_WHERE",
        rationale="SECRET_ANOM_RAT",
    )
    args = list(test.__repr_args__())
    args_str = repr(args)
    assert "SECRET_ANOM_WHERE" not in args_str
    assert "SECRET_ANOM_RAT" not in args_str
    safe_fields = {name for name, _ in args}
    assert "where" not in safe_fields
    assert "rationale" not in safe_fields
    # These should appear in __repr_args__ per the DEC-007 redacted shape.
    assert {"type", "column", "method", "seasonality"} <= safe_fields


def test_candidate_test_row_count_anomaly_by_period_pprint_redaction() -> None:
    """``pprint.pformat`` reaches through ``__repr_args__`` (Pydantic v2);
    a pure ``__repr__`` override alone would leak via this path. Pinned
    per memory `pydantic-v2-repr-args-redaction-required`."""
    import pprint

    test = CandidateTestRowCountAnomalyByPeriod(
        date_column="created_at",
        method="mad",
        where="SECRET_PPRINT_WHERE",
        rationale="SECRET_PPRINT_RAT",
    )
    rendered = pprint.pformat(test)
    assert "SECRET_PPRINT_WHERE" not in rendered
    assert "SECRET_PPRINT_RAT" not in rendered


def test_candidate_test_row_count_anomaly_by_period_model_dump_json_carries_text() -> None:
    """:meth:`model_dump_json` is unchanged — serialisation still carries
    ``where`` / ``rationale`` (only the casual debug-print path is
    redacted)."""
    test = CandidateTestRowCountAnomalyByPeriod(
        date_column="created_at",
        where="SECRET_DUMP_WHERE",
        rationale="SECRET_DUMP_RAT",
    )
    dumped = test.model_dump_json()
    assert "SECRET_DUMP_WHERE" in dumped
    assert "SECRET_DUMP_RAT" in dumped


def test_candidate_test_row_count_anomaly_by_period_repr_under_ansi_injection() -> None:
    """Defence-in-depth — hostile ANSI escapes in ``where`` / ``rationale``
    cannot reach a log viewer via ``repr()`` because the fields are
    redacted entirely."""
    test = CandidateTestRowCountAnomalyByPeriod(
        date_column="d",
        where="\x1b[31mEVIL_ANSI\x1b[0m",
        rationale="\x1b[33mEVIL_RAT\x1b[0m",
    )
    rendered = repr(test)
    assert "EVIL_ANSI" not in rendered
    assert "EVIL_RAT" not in rendered
    assert "\x1b" not in rendered


def test_custom_sql_from_manifest_is_excluded_from_serialization() -> None:
    """DEC-016 byte-identity: ``from_manifest`` MUST NOT alter the serialized shape.

    ``from_manifest`` is ``Field(default=False, exclude=True)`` precisely so the
    diff ``candidate_hash``, the proposed YAML, and every committed ``custom_sql``
    fixture stay byte-identical whether a test is manifest-ingested or drafted.
    This pins that invariant directly: a plausible "why is this excluded?" cleanup
    that drops ``exclude=True`` would silently change ``candidate_hash`` for every
    ``custom_sql`` and break diff reproducibility — with NO other test failing
    (verified via mutation testing at QG time), so this guard is load-bearing.
    """
    ingested = CandidateTestCustomSQL(sql="select 1", column=None, from_manifest=True)
    drafted = CandidateTestCustomSQL(sql="select 1", column=None, from_manifest=False)

    # The flag is readable at runtime (drives prune routing / diff scoping) ...
    assert ingested.from_manifest is True
    assert drafted.from_manifest is False

    # ... but never serialized: absent from model_dump, and the two serializations
    # are byte-identical (so the blake2b candidate_hash is invariant to the flag).
    assert "from_manifest" not in ingested.model_dump()
    assert "from_manifest" not in json.loads(ingested.model_dump_json())
    assert ingested.model_dump_json() == drafted.model_dump_json()
