"""Tests for the pure ``_parse_explain_json_bytes`` EXPLAIN-cell parser
(issue #130 US-002, DEC-001 / DEC-002 / DEC-006).

The fixtures under ``tests/fixtures/warehouse/snowflake/`` are hand-crafted —
workers/CI can't reach a live Snowflake. Engineered determinism: the parsed
``int`` is asserted EQUAL to the fixture's known ``bytesAssigned`` (a round
100 MiB), so the test fails on any real regression rather than rubber-stamping
whatever the parser returns. See that directory's README.md for the
maintainer regeneration command against a live ``EXPLAIN USING JSON``.

These tests need no connection — the parser is a module-level pure function.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from signalforge.warehouse.adapters.snowflake import _parse_explain_json_bytes
from signalforge.warehouse.errors import EstimateUnavailableError

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "warehouse" / "snowflake"

# The byte count the sample fixture encodes (100 MiB). Pinned so the parse
# assertion is mathematically guaranteed, not whatever the parser returns.
_EXPECTED_BYTES = 104_857_600


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_parses_fixture_str_to_expected_int() -> None:
    """str (JSON) input → exactly the fixture's bytesAssigned."""
    result = _parse_explain_json_bytes(_load("explain_using_json_sample.json"))
    assert result == _EXPECTED_BYTES


def test_parses_fixture_dict_to_expected_int() -> None:
    """A pre-parsed dict (the connector's other return shape) → same int."""
    document = json.loads(_load("explain_using_json_sample.json"))
    assert isinstance(document, dict)
    assert _parse_explain_json_bytes(document) == _EXPECTED_BYTES


def test_accepts_minimal_dict_input() -> None:
    """The navigation only needs GlobalStats.bytesAssigned, nothing else."""
    assert _parse_explain_json_bytes({"GlobalStats": {"bytesAssigned": 42}}) == 42


def test_zero_bytes_is_a_valid_estimate() -> None:
    """A genuine ``0`` (e.g. a fully-pruned plan) is a real estimate, not a
    degrade — distinct from a *missing* field (which raises)."""
    assert _parse_explain_json_bytes({"GlobalStats": {"bytesAssigned": 0}}) == 0


def test_missing_global_stats_raises() -> None:
    """No-stat fixture (GlobalStats absent) → EstimateUnavailableError."""
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_json_bytes(_load("explain_using_json_no_stats.json"))
    assert "GlobalStats" in excinfo.value.detail


def test_global_stats_not_a_mapping_raises() -> None:
    with pytest.raises(EstimateUnavailableError):
        _parse_explain_json_bytes({"GlobalStats": ["not", "a", "dict"]})


def test_missing_bytes_assigned_raises() -> None:
    """GlobalStats present but no bytesAssigned → raises (never returns 0)."""
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_json_bytes({"GlobalStats": {"partitionsTotal": 10, "partitionsAssigned": 2}})
    assert "bytesAssigned" in excinfo.value.detail


def test_malformed_json_str_raises() -> None:
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_json_bytes("{not valid json")
    assert "JSON" in excinfo.value.detail


def test_non_object_json_document_raises() -> None:
    """A JSON array / scalar at the top level is not a plan document."""
    with pytest.raises(EstimateUnavailableError):
        _parse_explain_json_bytes("[1, 2, 3]")


def test_non_int_bytes_assigned_raises() -> None:
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_json_bytes({"GlobalStats": {"bytesAssigned": "104857600"}})
    assert "integer" in excinfo.value.detail


def test_float_bytes_assigned_raises() -> None:
    """A float is not coerced — it must be an int (no silent truncation)."""
    with pytest.raises(EstimateUnavailableError):
        _parse_explain_json_bytes({"GlobalStats": {"bytesAssigned": 1.5}})


def test_bool_bytes_assigned_raises() -> None:
    """bool is an int subclass in Python, but ``True`` is not a byte count."""
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_json_bytes({"GlobalStats": {"bytesAssigned": True}})
    assert "integer" in excinfo.value.detail


def test_negative_bytes_assigned_raises() -> None:
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_json_bytes({"GlobalStats": {"bytesAssigned": -1}})
    assert "negative" in excinfo.value.detail


# ---------------------------------------------------------------------------
# SnowflakeAdapter.estimate_query_bytes override (US-003, DEC-001/004/005/008).
# ---------------------------------------------------------------------------

from signalforge.warehouse.adapters.snowflake import SnowflakeAdapter  # noqa: E402
from signalforge.warehouse.errors import QuerySyntaxError, WarehouseError  # noqa: E402
from tests.warehouse._fake_snowflake import FakeSnowflakeConnection  # noqa: E402


def test_estimate_query_bytes_happy_path_returns_fixture_int() -> None:
    """The EXPLAIN cell (fixture JSON) parses to the fixture's bytesAssigned."""
    fake = FakeSnowflakeConnection()
    fake.expect_execute(
        matching=r"^EXPLAIN USING JSON ",
        returns=[(_load("explain_using_json_sample.json"),)],
    )
    adapter = SnowflakeAdapter(connection=fake)
    assert adapter.estimate_query_bytes("SELECT * FROM analytics.public.orders") == _EXPECTED_BYTES
    fake.assert_all_expectations_met()


def test_estimate_query_bytes_rejects_semicolon_before_any_cursor_call() -> None:
    """A ``;``-containing SQL is rejected by ``validate_test_sql`` BEFORE the
    cursor is ever touched — no execute expectation is consumed."""
    fake = FakeSnowflakeConnection()
    # No expectations queued: any execute would raise AssertionError("unexpected
    # query: ..."), so the assertion below proves validation runs first.
    adapter = SnowflakeAdapter(connection=fake)
    with pytest.raises(Exception) as excinfo:
        adapter.estimate_query_bytes("SELECT 1; DROP TABLE x")
    # The failure is the SQL-safety reject, NOT a fake "unexpected query".
    assert "unexpected query" not in str(excinfo.value)
    fake.assert_all_expectations_met()  # nothing consumed


def test_estimate_query_bytes_embeds_validated_sql_after_explain_prefix() -> None:
    """The executed SQL starts with ``EXPLAIN USING JSON `` and embeds the
    validated user SQL verbatim (DEC-004)."""
    seen: list[str] = []
    fake = FakeSnowflakeConnection()

    # Wrap the connection's execute consumer to record what was executed.
    original = fake._consume_execute

    def _record(sql: str):  # type: ignore[no-untyped-def]
        seen.append(sql)
        return original(sql)

    fake._consume_execute = _record  # type: ignore[method-assign]
    fake.expect_execute(
        matching=r"^EXPLAIN USING JSON ",
        returns=[(_load("explain_using_json_sample.json"),)],
    )
    adapter = SnowflakeAdapter(connection=fake)
    user_sql = "SELECT customer_id FROM analytics.public.orders WHERE order_total > 0"
    adapter.estimate_query_bytes(user_sql)
    assert len(seen) == 1
    assert seen[0] == f"EXPLAIN USING JSON {user_sql}"


def test_estimate_query_bytes_maps_connector_exception() -> None:
    """A connector ``ProgrammingError`` from the EXPLAIN maps to a typed
    :class:`WarehouseError` raised ``from`` the original (DEC-005)."""
    pytest.importorskip("snowflake.connector")
    from snowflake.connector import errors as sfe

    fake = FakeSnowflakeConnection()
    original_exc = sfe.ProgrammingError("SQL compilation error: bad EXPLAIN")
    fake.expect_execute(matching=r"^EXPLAIN USING JSON ", returns=original_exc)
    adapter = SnowflakeAdapter(connection=fake)
    with pytest.raises(QuerySyntaxError) as excinfo:
        adapter.estimate_query_bytes("SELECT * FROM analytics.public.orders")
    assert isinstance(excinfo.value, WarehouseError)
    assert excinfo.value.__cause__ is original_exc


def test_estimate_query_bytes_empty_result_raises_unavailable() -> None:
    """No rows from the EXPLAIN → EstimateUnavailableError (never a fabricated
    number / 0)."""
    fake = FakeSnowflakeConnection()
    fake.expect_execute(matching=r"^EXPLAIN USING JSON ", returns=[])
    adapter = SnowflakeAdapter(connection=fake)
    with pytest.raises(EstimateUnavailableError):
        adapter.estimate_query_bytes("SELECT * FROM analytics.public.orders")


def test_estimate_query_bytes_reraises_unmapped_connector_exception() -> None:
    """An exception ``map_snowflake_exception`` does NOT recognise (i.e. not an
    auth/SQL connector error) passes through unchanged and is re-raised as-is —
    the ``mapped is exc`` passthrough arm of ``_execute_scalar`` (DEC-005). The
    original exception propagates rather than being swallowed or re-wrapped."""
    fake = FakeSnowflakeConnection()
    original_exc = RuntimeError("transient cursor failure")
    fake.expect_execute(matching=r"^EXPLAIN USING JSON ", returns=original_exc)
    adapter = SnowflakeAdapter(connection=fake)
    with pytest.raises(RuntimeError) as excinfo:
        adapter.estimate_query_bytes("SELECT * FROM analytics.public.orders")
    # Same object re-raised (not wrapped in a WarehouseError).
    assert excinfo.value is original_exc
