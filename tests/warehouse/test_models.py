"""Unit tests for the warehouse models module (US-004).

Covers the five public types (Dialect, TableRef, PartitionFilter,
ColumnStats, TestResult) plus the two private helper modules
(_sql_safety, _test_result_repr). Every test is capable of failing
(``testing-signal.md`` — no ``assert True``-shaped placeholders).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from signalforge.manifest.models import Model
from signalforge.warehouse._sql_safety import validate_test_sql
from signalforge.warehouse._test_result_repr import compact_repr
from signalforge.warehouse.errors import (
    InvalidIdentifierError,
    ManifestProjectNotFoundError,
    QuerySyntaxError,
)
from signalforge.warehouse.models import (
    BIGQUERY_DIALECT,
    ColumnStats,
    PartitionFilter,
    TableRef,
    TestResult,
)


def _minimal_model_dict(**overrides: Any) -> dict[str, Any]:
    """Build a minimal-but-valid Model payload for from_model tests."""
    base: dict[str, Any] = {
        "unique_id": "model.my_pkg.my_model",
        "name": "my_model",
        "resource_type": "model",
        "package_name": "my_pkg",
        "original_file_path": "models/my_model.sql",
        "path": "my_model.sql",
        "database": "my_project",
        "schema": "analytics",
        "raw_code": "select 1 as id",
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_bigquery_dialect_constant_is_frozen() -> None:
    """Mutating BIGQUERY_DIALECT raises FrozenInstanceError (DEC-003)."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        BIGQUERY_DIALECT.name = "snowflake"  # type: ignore[misc]


@pytest.mark.unit
@pytest.mark.error
def test_tableref_rejects_invalid_dataset() -> None:
    """Hyphenated dataset name fails the DEC-013 identifier regex."""
    with pytest.raises(InvalidIdentifierError):
        TableRef(project="p", dataset="bad-name", name="t")


@pytest.mark.unit
@pytest.mark.error
def test_tableref_rejects_invalid_name() -> None:
    """Adversarial table name with `;` is rejected at construction time."""
    with pytest.raises(InvalidIdentifierError):
        TableRef(project="p", dataset="d", name="x;DROP")


@pytest.mark.unit
@pytest.mark.error
def test_tableref_rejects_invalid_project() -> None:
    """Adversarial project string (whitespace + ``!``) fails the project regex.

    Hyphens are intentionally allowed (real GCP project IDs use them); the
    rejection set is the SQL-injection-shaped inputs.
    """
    with pytest.raises(InvalidIdentifierError):
        TableRef(project="bad project!", dataset="d", name="t")


@pytest.mark.unit
def test_tableref_accepts_hyphenated_gcp_project() -> None:
    """Real GCP project IDs use hyphens (``my-co-prod-12345``); the project
    regex must accept them. Closes the QG-of-US-013 finding-1 gap."""
    ref = TableRef(project="my-co-prod-12345", dataset="d", name="t")
    assert ref.project == "my-co-prod-12345"


@pytest.mark.unit
def test_tableref_accepts_none_project() -> None:
    """``project=None`` is allowed (DEC-027): defer to BQ client default."""
    ref = TableRef(project=None, dataset="d", name="t")
    assert ref.project is None


@pytest.mark.unit
def test_tableref_from_model_happy_path() -> None:
    """``from_model`` returns project/dataset/name from a populated Model."""
    model = Model.model_validate(_minimal_model_dict())
    ref = TableRef.from_model(model)
    assert (ref.project, ref.dataset, ref.name) == ("my_project", "analytics", "my_model")


@pytest.mark.unit
def test_tableref_from_model_uses_alias_over_name() -> None:
    """When ``alias`` is set, ``TableRef.name`` follows it (DEC-014)."""
    model = Model.model_validate(_minimal_model_dict(alias="x", name="y"))
    ref = TableRef.from_model(model)
    assert ref.name == "x"


@pytest.mark.unit
@pytest.mark.error
def test_tableref_from_model_raises_when_database_none() -> None:
    """Missing ``database`` field surfaces ManifestProjectNotFoundError."""
    model = Model.model_validate(_minimal_model_dict(database=None))
    with pytest.raises(ManifestProjectNotFoundError):
        TableRef.from_model(model)


@pytest.mark.unit
@pytest.mark.error
def test_partition_filter_rejects_invalid_column() -> None:
    """Adversarial column name on PartitionFilter is rejected (DEC-013)."""
    with pytest.raises(InvalidIdentifierError):
        PartitionFilter(column="bad-col", op="=", value="2024-01-01")


@pytest.mark.unit
def test_partition_filter_accepts_each_op() -> None:
    """All six operators in the PartitionOp Literal construct successfully."""
    ops = ("=", ">", ">=", "<", "<=", "!=")
    constructed = [PartitionFilter(column="dt", op=op, value="2024-01-01") for op in ops]
    assert [pf.op for pf in constructed] == list(ops)


@pytest.mark.unit
def test_column_stats_complex_type_min_max_none() -> None:
    """For complex BQ types, min/max default to None (DEC-016)."""
    cs = ColumnStats(count=10, distinct=10, nulls=0, data_type="GEOGRAPHY")
    assert cs.min is None and cs.max is None


@pytest.mark.unit
def test_test_result_explanation_passed() -> None:
    """A passing TestResult renders ``"passed"``."""
    tr = TestResult(passed=True, failure_count=0)
    assert tr.explanation() == "passed"


@pytest.mark.unit
def test_test_result_explanation_failed_no_samples() -> None:
    """Failing TestResult without samples renders the count-only string."""
    tr = TestResult(passed=False, failure_count=42)
    assert tr.explanation() == "42 rows failed"


@pytest.mark.unit
def test_test_result_explanation_failed_with_sample() -> None:
    """Failing TestResult with sample + schema renders a TIMESTAMP fragment."""
    tr = TestResult(
        passed=False,
        failure_count=3,
        sample_failures=[{"id": 7, "ts": "2024-01-01T00:00:00"}],
        row_schema=[("id", "INT64"), ("ts", "TIMESTAMP")],
    )
    rendered = tr.explanation()
    assert "example:" in rendered
    assert "TIMESTAMP('2024-01-01T00:00:00')" in rendered


@pytest.mark.unit
def test_compact_repr_truncates_long_strings() -> None:
    """String values longer than 40 chars are truncated with `...` (DEC-020)."""
    long_value = "x" * 100
    rendered = compact_repr({"col": long_value})
    # The rendered form is `col='xxx...'`; the inner value must be 40 chars
    # max (37 x's + '...'), wrapped in single quotes.
    assert "..." in rendered
    assert "x" * 41 not in rendered


@pytest.mark.unit
@pytest.mark.error
def test_validate_test_sql_rejects_semicolon() -> None:
    """A trailing ``;`` in candidate test SQL is rejected (DEC-013)."""
    with pytest.raises(QuerySyntaxError):
        validate_test_sql("select 1 from t;")
