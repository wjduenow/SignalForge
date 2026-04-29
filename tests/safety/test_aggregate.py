"""Tests for :func:`signalforge.safety.aggregate.aggregate_columns` (US-009).

Exercises the redacted/non-redacted split, the DEC-008 single-context-open
batching, the hashed-name keying for redacted columns (DEC-010), and the
``ColumnNotInModelError`` raise for unknown column names. The end-of-file
fixture round trip uses ``tests/fixtures/safety/manifest_with_pii_meta.json``
loaded via the real loader.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.manifest.loader import load
from signalforge.manifest.models import Column, Config, Model
from signalforge.safety.aggregate import aggregate_columns
from signalforge.safety.errors import ColumnNotInModelError
from signalforge.safety.policy import SafetyPolicy
from signalforge.safety.redact import hash_column_name
from signalforge.warehouse.models import ColumnStats, TableRef
from tests.safety._fake_adapter import FakeAdapter

pytestmark = pytest.mark.safety


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_column(
    name: str,
    *,
    tags: tuple[str, ...] = (),
    meta: dict | None = None,
) -> Column:
    return Column(name=name, tags=list(tags), meta=meta or {})


def _make_model(
    *,
    unique_id: str = "model.test.x",
    database: str | None = "my-project",
    schema: str | None = "ds",
    name: str = "tbl",
    alias: str | None = None,
    tags: tuple[str, ...] = (),
    meta: dict | None = None,
    columns: dict[str, Column] | None = None,
) -> Model:
    return Model(
        unique_id=unique_id,
        name=name,
        resource_type="model",
        package_name="test",
        original_file_path="models/x.sql",
        path="x.sql",
        database=database,
        schema=schema,
        alias=alias,
        tags=list(tags),
        config=Config(materialized="table", tags=list(tags), meta=meta or {}),
        columns=columns or {},
        raw_code="select 1",
    )


def _stats(count: int = 100, distinct: int = 80, nulls: int = 5) -> ColumnStats:
    return ColumnStats(
        count=count,
        distinct=distinct,
        nulls=nulls,
        min=0,
        max=999,
        data_type="INT64",
    )


_POLICY = SafetyPolicy()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_aggregate_columns_redacted_returns_none_keyed_by_hashed_name() -> None:
    column = _make_column("customer_email")
    model = _make_model(name="t", columns={"customer_email": column})
    fake = FakeAdapter()

    stats, redactions = aggregate_columns(fake, model, ["customer_email"], _POLICY)

    hashed = hash_column_name("customer_email")
    assert stats == {hashed: None}
    assert len(redactions) == 1
    assert redactions[0].column_name == "customer_email"
    assert redactions[0].hashed_name == hashed
    assert redactions[0].reason == "pattern_match"
    fake.assert_all_expectations_met()


def test_aggregate_columns_calls_adapter_for_non_redacted() -> None:
    column = _make_column("id")
    model = _make_model(name="t", columns={"id": column})
    table = TableRef.from_model(model)

    fake = FakeAdapter()
    fake.expect_column_stats(table=table, column="id", returns=_stats())

    stats, redactions = aggregate_columns(fake, model, ["id"], _POLICY)

    assert set(stats.keys()) == {"id"}
    assert isinstance(stats["id"], ColumnStats)
    assert stats["id"].count == 100
    assert redactions == ()
    fake.assert_all_expectations_met()


def test_aggregate_columns_does_not_call_adapter_for_redacted() -> None:
    column = _make_column("user_email")
    model = _make_model(name="t", columns={"user_email": column})

    fake = FakeAdapter()  # no expectations queued
    stats, redactions = aggregate_columns(fake, model, ["user_email"], _POLICY)

    assert stats == {hash_column_name("user_email"): None}
    assert len(redactions) == 1
    fake.assert_all_expectations_met()


def test_aggregate_columns_uses_with_adapter_context() -> None:
    column = _make_column("id")
    model = _make_model(name="t", columns={"id": column})
    table = TableRef.from_model(model)

    fake = FakeAdapter()
    fake.expect_column_stats(table=table, column="id", returns=_stats())

    assert fake.enter_count == 0
    assert fake.exit_count == 0

    aggregate_columns(fake, model, ["id"], _POLICY)

    # Context opened exactly once around the (single) column_stats call,
    # then closed.
    assert fake.enter_count == 1
    assert fake.exit_count == 1


def test_aggregate_columns_returns_redaction_records_for_redacted() -> None:
    columns = {
        "user_email": _make_column("user_email"),
        "phone": _make_column("phone"),
    }
    model = _make_model(name="t", columns=columns)
    fake = FakeAdapter()

    _, redactions = aggregate_columns(fake, model, ["user_email", "phone"], _POLICY)

    assert len(redactions) == 2
    reasons = {r.reason for r in redactions}
    assert reasons == {"pattern_match"}


def test_aggregate_columns_handles_mixed_redacted_and_non_redacted() -> None:
    columns = {
        "id": _make_column("id"),
        "email": _make_column("email"),
    }
    model = _make_model(name="t", columns=columns)
    table = TableRef.from_model(model)

    fake = FakeAdapter()
    # Only one call expected (for the non-redacted column).
    fake.expect_column_stats(table=table, column="id", returns=_stats())

    stats, redactions = aggregate_columns(fake, model, ["id", "email"], _POLICY)

    hashed_email = hash_column_name("email")
    assert set(stats.keys()) == {"id", hashed_email}
    assert isinstance(stats["id"], ColumnStats)
    assert stats[hashed_email] is None
    assert len(redactions) == 1
    assert redactions[0].column_name == "email"
    fake.assert_all_expectations_met()


def test_aggregate_columns_unknown_column_raises_column_not_in_model() -> None:
    model = _make_model(
        unique_id="model.test.t",
        name="t",
        columns={"id": _make_column("id")},
    )
    fake = FakeAdapter()

    with pytest.raises(ColumnNotInModelError) as exc_info:
        aggregate_columns(fake, model, ["does_not_exist"], _POLICY)

    assert exc_info.value.column_name == "does_not_exist"
    assert exc_info.value.model_unique_id == "model.test.t"


def test_aggregate_columns_with_fixture_manifest() -> None:
    """End-to-end: load the safety fixture and run the customer model.

    The fixture's ``model.sf_demo.customers`` has five columns; four are
    redacted via DEC-003 signals (email/pattern, customer_ssn_optout/meta,
    taxpayer_id/tag, birth_date/meta_contains_pii) and ``id`` is the
    non-redacted control.
    """
    repo_root = Path(__file__).resolve().parents[2]
    fixture = repo_root / "tests" / "fixtures" / "safety" / "manifest_with_pii_meta.json"

    manifest = load(fixture.parent, manifest_path=fixture)
    customers = manifest.get_model("model.sf_demo.customers")
    table = TableRef.from_model(customers)

    fake = FakeAdapter()
    fake.expect_column_stats(table=table, column="id", returns=_stats())

    requested = list(customers.columns.keys())
    stats, redactions = aggregate_columns(fake, customers, requested, _POLICY)

    # Four redacted columns; one (id) keyed by real name with stats.
    assert len(redactions) == 4
    assert "id" in stats
    assert isinstance(stats["id"], ColumnStats)

    # The four redacted columns are keyed by hashed name and map to None.
    redacted_names = {"email", "customer_ssn_optout", "taxpayer_id", "birth_date"}
    for name in redacted_names:
        assert stats[hash_column_name(name)] is None

    fake.assert_all_expectations_met()


def test_aggregate_columns_returns_tuple_for_redactions_field() -> None:
    column = _make_column("user_email")
    model = _make_model(name="t", columns={"user_email": column})
    fake = FakeAdapter()

    result = aggregate_columns(fake, model, ["user_email"], _POLICY)

    assert result[1].__class__ is tuple


def test_aggregate_columns_empty_columns_list_returns_empty_dict() -> None:
    model = _make_model(name="t", columns={"id": _make_column("id")})
    fake = FakeAdapter()

    stats, redactions = aggregate_columns(fake, model, [], _POLICY)

    assert stats == {}
    assert redactions == ()
    # Empty request must never open the adapter context.
    assert fake.enter_count == 0
    fake.assert_all_expectations_met()
