"""Cursor-handle release + ``column_stats`` tests for :class:`SnowflakeAdapter`
(#258 US-001 / US-002).

The ``column_stats`` section (US-002, DEC-001..DEC-006) drives the real adapter
through the injected :class:`FakeSnowflakeConnection`, queuing BOTH the
``INFORMATION_SCHEMA.COLUMNS`` catalog lookup AND the aggregate query per flush.
No drift detector: :class:`ColumnStats` is frozen, produced in-process, and never
read back from disk (mirrors the ingest-layer rule).

Traces to DEC-007 of ``plans/super/258-snowflake-column-stats.md``. Before this
fix only :meth:`SnowflakeAdapter._execute_scalar` closed its cursor in a
``try/finally``; :meth:`_execute`, :meth:`_execute_to_dicts`, and
:meth:`run_test_sql` opened a cursor on the long-lived connection and never
released it — leaking server-side cursor handles across repeated queries. This
mirrors the Databricks PR #257 fix (``tests/warehouse/test_databricks_adapter.py``
§ "Cursor-handle release").

Each test drives one of the three methods and asserts the handed-out cursor's
``closed`` is ``True`` afterwards — on the success path AND the failure path
(``expect_execute(returns=<Exception>)``), proving the ``finally`` arm fires.

Uses :class:`FakeSnowflakeConnection` (``tests/warehouse/_fake_snowflake.py``),
which tracks every cursor it vends via :attr:`cursors` and exposes ``closed``
per cursor — never a ``MagicMock`` (``testing-signal.md``).
"""

from __future__ import annotations

import logging
from datetime import time
from decimal import Decimal
from typing import Any

import pytest

from signalforge.warehouse import SnowflakeAdapter
from signalforge.warehouse.adapters.snowflake import _is_complex_snowflake_type
from signalforge.warehouse.errors import (
    ColumnNotFoundError,
    InvalidIdentifierError,
    QuerySyntaxError,
)
from signalforge.warehouse.models import ColumnStats, TableRef
from tests.warehouse._fake_snowflake import FakeSnowflakeConnection

# ``project`` follows :class:`TableRef`'s GCP-style project-id grammar
# (lowercase start, >= 6 chars); Snowflake quotes it verbatim per-component.
_TABLE = TableRef(project="mydatabase", dataset="SCH", name="ORDERS")

_SIZE_QUERY = r"INFORMATION_SCHEMA\.TABLES"
_SAMPLE_QUERY = r"_sf_sample_hash"
_COUNT_QUERY = r"SELECT COUNT\(\*\) AS failures"


def _make_adapter(conn: FakeSnowflakeConnection) -> SnowflakeAdapter:
    return SnowflakeAdapter(connection=conn)


# ---------------------------------------------------------------------------
# _execute — via get_row_count / _get_num_rows (the INFORMATION_SCHEMA lookup)
# ---------------------------------------------------------------------------


def test_execute_closes_cursor_on_success() -> None:
    """``_execute`` releases the cursor after a successful query so repeated
    queries on the long-lived connection don't leak server-side handles."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(10,)])
    adapter = _make_adapter(conn)

    adapter.get_row_count(_TABLE)

    assert conn.cursors, "expected the adapter to open at least one cursor"
    assert all(c.closed for c in conn.cursors)


def test_execute_closes_cursor_on_failure() -> None:
    """The cursor is released even when the query raises (the ``finally`` arm)."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=RuntimeError("boom"))
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError):
        adapter.get_row_count(_TABLE)

    assert conn.cursors and all(c.closed for c in conn.cursors)


# ---------------------------------------------------------------------------
# _execute_to_dicts — via sample_rows (the projection-subquery sample query)
# ---------------------------------------------------------------------------


def test_execute_to_dicts_closes_cursor_on_success() -> None:
    """``sample_rows`` (via ``_execute_to_dicts``) closes the cursor only AFTER
    ``cursor.description`` has been read to shape the rows (outer ``try/finally``
    wrapping the inner exception-mapping ``try/except``)."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(
        matching=_SAMPLE_QUERY,
        returns=[(1, 10)],
        description=[("ID",), ("AMOUNT",)],
    )
    adapter = _make_adapter(conn)

    rows = adapter.sample_rows(_TABLE, 100)

    assert rows == [{"ID": 1, "AMOUNT": 10}]
    assert conn.cursors and all(c.closed for c in conn.cursors)


def test_execute_to_dicts_closes_cursor_on_failure() -> None:
    """The cursor is released even when the sample query raises."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=RuntimeError("boom"))
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError):
        adapter.sample_rows(_TABLE, 100)

    assert conn.cursors and all(c.closed for c in conn.cursors)


# ---------------------------------------------------------------------------
# run_test_sql — the COUNT(*) failing-rows wrap
# ---------------------------------------------------------------------------


def test_run_test_sql_closes_cursor_on_success() -> None:
    """``run_test_sql`` releases its cursor after reading ``description`` and
    the result rows (the ``finally`` arm added by #258 US-001)."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(0,)], description=[("FAILURES",)])
    adapter = _make_adapter(conn)

    adapter.run_test_sql('SELECT "ID" FROM "DB"."SCH"."T" WHERE "ID" IS NULL')

    assert conn.cursors and all(c.closed for c in conn.cursors)


def test_run_test_sql_closes_cursor_on_failure() -> None:
    """The cursor is released even when the wrapped query raises."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=RuntimeError("boom"))
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError):
        adapter.run_test_sql('SELECT "ID" FROM "DB"."SCH"."T" WHERE "ID" IS NULL')

    assert conn.cursors and all(c.closed for c in conn.cursors)


# ===========================================================================
# column_stats (#258 US-002, DEC-001..DEC-006) — full-batch profiling.
#
# BigQuery-style context-manager batching with a Snowflake catalog pre-filter:
# ONE ``INFORMATION_SCHEMA.COLUMNS`` lookup (data_type + MIN/MAX skip decision)
# plus ONE aggregate over the fold-then-quoted table per flush.
# ===========================================================================

_COLUMNS_QUERY = r"INFORMATION_SCHEMA\.COLUMNS"
_AGG_QUERY = r"COUNT\(DISTINCT"
_CATALOG_DESCRIPTION = [("COLUMN_NAME",), ("DATA_TYPE",)]


class _RecordingSnowflakeConnection(FakeSnowflakeConnection):
    """A :class:`FakeSnowflakeConnection` that records every executed SQL string
    (for query-shape / one-aggregate-per-flush assertions)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.executed: list[str] = []

    def _consume_execute(self, sql: str) -> tuple[list[Any], list[Any] | None]:
        self.executed.append(sql)
        return super()._consume_execute(sql)


def _scalar_agg_description(n_columns: int) -> list[tuple[str]]:
    """Aggregate DB-API descriptor for an all-scalar batch of ``n_columns``.

    Mirrors ``_flush_column_stats_batch``'s SELECT order: a leading
    ``ROW_COUNT`` then five index-suffixed aliases per column (count / distinct /
    nulls / min / max). Snowflake folds aliases to UPPER, so the descriptor uses
    the upper-cased forms (the adapter lowercases result keys before reading)."""
    desc: list[tuple[str]] = [("ROW_COUNT",)]
    for i in range(n_columns):
        desc.extend(
            [(f"COUNT_{i}",), (f"DISTINCT_{i}",), (f"NULLS_{i}",), (f"MIN_{i}",), (f"MAX_{i}",)]
        )
    return desc


# ---------------------------------------------------------------------------
# _is_complex_snowflake_type — DEC-003 skip-set.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_str", "expected"),
    [
        ("ARRAY", True),
        ("OBJECT", True),
        ("VARIANT", True),
        ("GEOGRAPHY", True),
        ("GEOMETRY", True),
        # Case / whitespace insensitivity.
        ("array", True),
        ("  Variant  ", True),
        # Parametric tail stripped then re-checked (defensive; Snowflake today
        # returns bare names).
        ("ARRAY<NUMBER>", True),
        # BINARY is orderable — MIN/MAX runs.
        ("BINARY", False),
        ("NUMBER", False),
        ("TEXT", False),
        ("TIMESTAMP_NTZ", False),
        ("DATE", False),
        ("BOOLEAN", False),
    ],
)
def test_is_complex_snowflake_type(type_str: str, expected: bool) -> None:
    """DEC-003 — the complex-type set drives the MIN/MAX skip; BINARY is
    orderable and NOT in the set."""
    assert _is_complex_snowflake_type(type_str) is expected


# ---------------------------------------------------------------------------
# Happy path — populated ColumnStats.
# ---------------------------------------------------------------------------


def test_column_stats_returns_populated_columnstats() -> None:
    """One catalog lookup + one aggregate shape into a fully-populated
    :class:`ColumnStats`, mapped field-for-field."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("AMOUNT", "NUMBER")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=[(1000, 900, 750, 100, 1, 9999)],
        description=_scalar_agg_description(1),
    )

    with _make_adapter(conn) as adapter:
        stats = adapter.column_stats(_TABLE, "amount")

    assert isinstance(stats, ColumnStats)
    assert stats.count == 900
    assert stats.distinct == 750
    assert stats.nulls == 100
    assert stats.min == 1
    assert stats.max == 9999
    assert stats.data_type == "NUMBER"
    conn.assert_all_expectations_met()


def test_column_stats_carries_through_string_and_none_minmax() -> None:
    """STRING min/max bounds pass through; a NULL min/max (all-null column)
    surfaces as ``None``."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("REGION", "TEXT")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=[(500, 0, 0, 500, None, None)],
        description=_scalar_agg_description(1),
    )

    with _make_adapter(conn) as adapter:
        stats = adapter.column_stats(_TABLE, "region")

    assert stats.count == 0
    assert stats.distinct == 0
    assert stats.nulls == 500
    assert stats.min is None
    assert stats.max is None
    assert stats.data_type == "TEXT"


def test_column_stats_query_shape_and_folding() -> None:
    """The catalog + aggregate SQL fold-then-quote every identifier, use the
    ``COUNT(*) - COUNT(col)`` null count (DEC-005), and reuse ``_quote`` for the
    three-part FROM."""
    conn = _RecordingSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("AMOUNT", "NUMBER")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=[(1, 1, 1, 0, 5, 5)],
        description=_scalar_agg_description(1),
    )

    with _make_adapter(conn) as adapter:
        adapter.column_stats(_TABLE, "amount")

    catalog_sql = conn.executed[0]
    assert catalog_sql.startswith("SELECT COLUMN_NAME, DATA_TYPE FROM")
    assert '"MYDATABASE".INFORMATION_SCHEMA.COLUMNS' in catalog_sql
    assert "UPPER(TABLE_SCHEMA) = UPPER('SCH')" in catalog_sql
    assert "UPPER(TABLE_NAME) = UPPER('ORDERS')" in catalog_sql

    agg_sql = conn.executed[1]
    # Fold-then-quote: "amount" folds to UPPER before the quote chars.
    assert 'COUNT("AMOUNT") AS count_0' in agg_sql
    assert 'COUNT(DISTINCT "AMOUNT") AS distinct_0' in agg_sql
    assert '(COUNT(*) - COUNT("AMOUNT")) AS nulls_0' in agg_sql
    assert 'MIN("AMOUNT") AS min_0' in agg_sql
    assert 'MAX("AMOUNT") AS max_0' in agg_sql
    assert agg_sql.endswith('FROM "MYDATABASE"."SCH"."ORDERS"')
    assert "amount" not in agg_sql  # never the raw lowercase identifier


def test_column_stats_resolves_aliases_case_insensitively() -> None:
    """A connection that preserved the alias case (UPPER) still maps — the
    adapter lowercases the result keys before reading them."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("QTY", "NUMBER")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=[(10, 7, 4, 3, 0, 10)],
        # Explicit UPPER aliases (Snowflake's real folding).
        description=[
            ("ROW_COUNT",),
            ("COUNT_0",),
            ("DISTINCT_0",),
            ("NULLS_0",),
            ("MIN_0",),
            ("MAX_0",),
        ],
    )

    with _make_adapter(conn) as adapter:
        stats = adapter.column_stats(_TABLE, "qty")

    assert stats.count == 7
    assert stats.distinct == 4
    assert stats.nulls == 3
    assert stats.min == 0
    assert stats.max == 10
    assert stats.data_type == "NUMBER"


def test_column_stats_empty_table() -> None:
    """An empty table still carries the column in the catalog, so the aggregate
    yields count=0 / min=max=NULL with a real ``data_type``."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("AMOUNT", "NUMBER")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=[(0, 0, 0, 0, None, None)],
        description=_scalar_agg_description(1),
    )

    with _make_adapter(conn) as adapter:
        stats = adapter.column_stats(_TABLE, "amount")

    assert stats.count == 0
    assert stats.distinct == 0
    assert stats.nulls == 0
    assert stats.min is None
    assert stats.max is None
    assert stats.data_type == "NUMBER"


# ---------------------------------------------------------------------------
# Identifier validation + guard.
# ---------------------------------------------------------------------------


def test_column_stats_validates_column_identifier() -> None:
    """A malformed column name is rejected by ``validate_identifier`` BEFORE any
    query is issued (DEC-013) — no expectation is consumed."""
    conn = FakeSnowflakeConnection()

    with _make_adapter(conn) as adapter, pytest.raises(InvalidIdentifierError):
        adapter.column_stats(_TABLE, "amount; DROP TABLE x")

    assert not conn.cursors  # nothing executed


def test_column_stats_raises_runtime_error_outside_with_block() -> None:
    """DEC-025 guard — outside a ``with adapter:`` block the batching caches are
    ``None`` and ``column_stats`` raises ``RuntimeError`` before any query."""
    conn = FakeSnowflakeConnection()
    adapter = _make_adapter(conn)  # NOT entered as a context manager

    with pytest.raises(RuntimeError, match="with adapter"):
        adapter.column_stats(_TABLE, "amount")

    assert not conn.cursors


# ---------------------------------------------------------------------------
# Table quoting.
# ---------------------------------------------------------------------------


def test_column_stats_two_part_table_quoting() -> None:
    """``project=None`` yields an unqualified ``INFORMATION_SCHEMA.COLUMNS``
    catalog query and a two-part ``"SCH"."ORDERS"`` aggregate FROM clause."""
    two_part = TableRef(project=None, dataset="SCH", name="ORDERS")
    conn = _RecordingSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("AMOUNT", "NUMBER")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=[(1, 1, 1, 0, 5, 5)],
        description=_scalar_agg_description(1),
    )

    with _make_adapter(conn) as adapter:
        adapter.column_stats(two_part, "amount")

    catalog_sql = conn.executed[0]
    # Unqualified INFORMATION_SCHEMA (no leading "<db>." prefix).
    assert "FROM INFORMATION_SCHEMA.COLUMNS" in catalog_sql
    assert '"."INFORMATION_SCHEMA' not in catalog_sql
    assert conn.executed[1].endswith('FROM "SCH"."ORDERS"')


# ---------------------------------------------------------------------------
# Error mapping.
# ---------------------------------------------------------------------------


def test_column_stats_programming_error_maps_to_query_syntax_error() -> None:
    """A connector ``ProgrammingError`` from the aggregate maps to
    :class:`QuerySyntaxError` via ``map_snowflake_exception``."""
    pytest.importorskip("snowflake.connector")
    from snowflake.connector import errors as sfe

    conn = FakeSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("AMOUNT", "NUMBER")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=sfe.ProgrammingError("SQL compilation error: bad aggregate"),
    )

    with _make_adapter(conn) as adapter, pytest.raises(QuerySyntaxError):
        adapter.column_stats(_TABLE, "amount")


def test_column_stats_column_not_found_maps_with_context() -> None:
    """A column absent from the catalog map raises :class:`ColumnNotFoundError`
    (carrying the table + column context) BEFORE the aggregate is issued."""
    conn = FakeSnowflakeConnection()
    # Catalog returns a DIFFERENT column, so the requested one is absent.
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("AMOUNT", "NUMBER")], description=_CATALOG_DESCRIPTION
    )

    with _make_adapter(conn) as adapter, pytest.raises(ColumnNotFoundError) as exc_info:
        adapter.column_stats(_TABLE, "ghost")

    assert exc_info.value.column == "ghost"
    assert exc_info.value.table == _TABLE.qualified_name
    # Only the catalog query ran; the aggregate was never issued.
    assert len(conn.cursors) == 1


# ---------------------------------------------------------------------------
# Complex-type MIN/MAX skip (DEC-003) + Decimal coercion (DEC-004).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("data_type", ["ARRAY", "OBJECT", "VARIANT", "GEOGRAPHY", "GEOMETRY"])
def test_column_stats_complex_type_nulls_min_max(data_type: str) -> None:
    """A complex Snowflake type omits MIN/MAX from the emitted aggregate and
    returns ``min=max=None`` (DEC-003)."""
    conn = _RecordingSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("PAYLOAD", data_type)], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        # row_count, count_0, distinct_0, nulls_0 — no MIN/MAX for a complex col.
        returns=[(42, 40, 2, 0)],
        description=[("ROW_COUNT",), ("COUNT_0",), ("DISTINCT_0",), ("NULLS_0",)],
    )

    with _make_adapter(conn) as adapter:
        stats = adapter.column_stats(_TABLE, "payload")

    assert stats.count == 40
    assert stats.distinct == 2
    assert stats.min is None
    assert stats.max is None
    assert stats.data_type == data_type
    # MIN/MAX absent from the emitted SQL.
    agg_sql = conn.executed[1]
    assert "MIN(" not in agg_sql
    assert "MAX(" not in agg_sql


def test_column_stats_scalar_type_preserves_min_max() -> None:
    """A scalar (orderable) type keeps MIN/MAX in the aggregate and the result."""
    conn = _RecordingSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("TS", "TIMESTAMP_NTZ")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=[(5, 5, 4, 1, "2020-01-01", "2020-12-31")],
        description=_scalar_agg_description(1),
    )

    with _make_adapter(conn) as adapter:
        stats = adapter.column_stats(_TABLE, "ts")

    assert stats.min == "2020-01-01"
    assert stats.max == "2020-12-31"
    assert stats.data_type == "TIMESTAMP_NTZ"
    assert 'MIN("TS") AS min_0' in conn.executed[1]


def test_column_stats_decimal_min_max_coerced_to_float() -> None:
    """A NUMBER column whose aggregate returns ``Decimal`` min/max is coerced to
    ``float`` (DEC-004 — ``Decimal`` is not in the ``ColumnStats`` union)."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("PRICE", "NUMBER")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        # row_count, count_0, distinct_0, nulls_0, min_0, max_0.
        returns=[(3, 3, 3, 0, Decimal("1.50"), Decimal("99.99"))],
        description=_scalar_agg_description(1),
    )

    with _make_adapter(conn) as adapter:
        stats = adapter.column_stats(_TABLE, "price")

    assert isinstance(stats.min, float)
    assert isinstance(stats.max, float)
    assert stats.min == 1.50
    assert stats.max == 99.99


def test_column_stats_binary_min_max_coerced_to_none() -> None:
    """BINARY is SQL-orderable (MIN/MAX runs, NOT in the skip set), but the
    connector returns ``bytearray`` — outside the ``ColumnStats`` union. The
    coercion net nulls it rather than raising a ValidationError that would fail
    the whole batch (#258 QG). count/distinct/nulls still populate."""
    conn = _RecordingSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("BLOB", "BINARY")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        # min_0 / max_0 come back as bytearray (non-utf8 blob) from the connector.
        returns=[(4, 4, 3, 1, bytearray(b"\x00\x01"), bytearray(b"\xff\xfe"))],
        description=_scalar_agg_description(1),
    )

    with _make_adapter(conn) as adapter:
        stats = adapter.column_stats(_TABLE, "blob")

    # MIN/MAX ARE emitted for BINARY (not a skip-set type) ...
    assert 'MIN("BLOB") AS min_0' in conn.executed[1]
    # ... but the bytearray result is nulled by _coerce_min_max.
    assert stats.min is None
    assert stats.max is None
    assert stats.count == 4
    assert stats.distinct == 3
    assert stats.nulls == 1
    assert stats.data_type == "BINARY"


def test_column_stats_time_min_max_coerced_to_none() -> None:
    """TIME is SQL-orderable but the connector returns ``datetime.time``, which
    is not in the ``ColumnStats`` union — the coercion net nulls it (#258 QG)."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("T", "TIME")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=[(6, 6, 5, 1, time(8, 30), time(17, 45))],
        description=_scalar_agg_description(1),
    )

    with _make_adapter(conn) as adapter:
        stats = adapter.column_stats(_TABLE, "t")

    assert stats.min is None
    assert stats.max is None
    assert stats.count == 6
    assert stats.data_type == "TIME"


def test_column_stats_failed_column_does_not_poison_rest_of_table() -> None:
    """A column that fails to resolve must not stay stuck in the pending queue:
    a later ``column_stats`` for a DIFFERENT valid column of the same table must
    still succeed (#258 QG — Critical). The pending batch is drained up front, so
    the failed call's column is gone before the next call queues a fresh batch."""
    conn = FakeSnowflakeConnection()
    # Call 1 ("bogus"): catalog lookup returns only AMOUNT — bogus is absent, so
    # column_stats raises ColumnNotFoundError before any aggregate.
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("AMOUNT", "NUMBER")], description=_CATALOG_DESCRIPTION
    )
    # Call 2 ("amount"): a FRESH catalog lookup + the aggregate. This only runs
    # if the pending queue was drained after call 1's failure — otherwise the
    # stuck "bogus" column would re-poison this flush and raise again.
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("AMOUNT", "NUMBER")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=[(3, 3, 3, 0, 1, 9)],
        description=_scalar_agg_description(1),
    )

    with _make_adapter(conn) as adapter:
        with pytest.raises(ColumnNotFoundError):
            adapter.column_stats(_TABLE, "bogus")
        # Pre-fix, this would ALSO raise ColumnNotFoundError (bogus re-included).
        stats = adapter.column_stats(_TABLE, "amount")

    assert stats.count == 3
    assert stats.data_type == "NUMBER"


# ---------------------------------------------------------------------------
# Batching — multiple columns in one aggregate flush.
# ---------------------------------------------------------------------------


def test_column_stats_batches_all_queued_columns_in_one_flush() -> None:
    """Two columns queued before the first read flush in ONE aggregate query
    (pre-seed ``_column_stats_pending``, mirroring the BigQuery batch test)."""
    conn = _RecordingSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY,
        returns=[("AMOUNT", "NUMBER"), ("QTY", "NUMBER")],
        description=_CATALOG_DESCRIPTION,
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=[(100, 90, 80, 10, 1, 9999, 95, 50, 5, 2, 500)],
        description=_scalar_agg_description(2),
    )

    adapter = _make_adapter(conn)
    with adapter:
        assert adapter._column_stats_pending is not None
        adapter._column_stats_pending[_TABLE] = ["amount", "qty"]
        stats_a = adapter.column_stats(_TABLE, "amount")
        # Second read hits the cache — no new query.
        stats_b = adapter.column_stats(_TABLE, "qty")

    # amount is index 0, qty is index 1 (pending-list order).
    assert stats_a.count == 90
    assert stats_a.min == 1
    assert stats_b.count == 95
    assert stats_b.max == 500

    # Exactly ONE aggregate query (plus the one catalog lookup) covered both.
    agg_queries = [s for s in conn.executed if "COUNT(DISTINCT" in s]
    assert len(agg_queries) == 1
    assert '"AMOUNT"' in agg_queries[0]
    assert '"QTY"' in agg_queries[0]
    assert len(conn.executed) == 2  # catalog + single aggregate
    conn.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# Edge cases — large-batch WARNING, catalog-row skip, empty-batch no-op.
# ---------------------------------------------------------------------------


def test_column_stats_warns_on_large_batch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Queuing more than ``_COLUMN_BATCH_WARN_AT`` columns emits ONE WARNING per
    flush (DEC-006; threshold patched to 0 so a single column trips it)."""
    monkeypatch.setattr("signalforge.warehouse.adapters.snowflake._COLUMN_BATCH_WARN_AT", 0)
    conn = FakeSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY, returns=[("AMOUNT", "NUMBER")], description=_CATALOG_DESCRIPTION
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=[(1, 1, 1, 0, 5, 5)],
        description=_scalar_agg_description(1),
    )

    with (
        caplog.at_level(logging.WARNING, logger="signalforge.warehouse"),
        _make_adapter(conn) as adapter,
    ):
        adapter.column_stats(_TABLE, "amount")

    assert any("Large column_stats batch" in r.getMessage() for r in caplog.records)


def test_column_stats_skips_catalog_rows_with_null_name_or_type() -> None:
    """Catalog rows with a NULL ``COLUMN_NAME`` or ``DATA_TYPE`` are skipped; a
    valid row for the requested column still resolves."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(
        matching=_COLUMNS_QUERY,
        returns=[(None, "NUMBER"), ("AMOUNT", None), ("AMOUNT", "NUMBER")],
        description=_CATALOG_DESCRIPTION,
    )
    conn.expect_execute(
        matching=_AGG_QUERY,
        returns=[(1, 1, 1, 0, 5, 5)],
        description=_scalar_agg_description(1),
    )

    with _make_adapter(conn) as adapter:
        stats = adapter.column_stats(_TABLE, "amount")

    assert stats.data_type == "NUMBER"


def test_flush_column_stats_batch_no_op_when_no_columns_queued() -> None:
    """``_flush_column_stats_batch`` with nothing queued short-circuits before
    any warehouse call (the empty-batch guard)."""
    conn = FakeSnowflakeConnection()
    adapter = _make_adapter(conn)

    with adapter:
        adapter._flush_column_stats_batch(_TABLE)

    assert not conn.cursors
