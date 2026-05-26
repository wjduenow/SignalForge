"""Tests for ``SnowflakeAdapter.sample_rows`` (issue #122, US-003).

Deterministic hash-mod sampling, the Snowflake analog of BigQuery's
``MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), bucket)`` →
``MOD(ABS(HASH(*)), bucket)`` (DEC-006). Same fail-loud sizing guards as
BigQuery (:class:`UnknownTableSizeError`,
:class:`SamplingRequiresPartitionFilterError`; DEC-005). Tuple ``fetchall()``
results are shaped into dicts via ``cursor.description`` (DEC-010); SDK
exceptions route through ``map_snowflake_exception`` (DEC-009).

Uses :class:`FakeSnowflakeConnection` (``tests/warehouse/_fake_snowflake.py``)
with explicit ``expect_execute`` round-trips — never a ``MagicMock`` (which
would auto-pass and violate ``testing-signal.md``). Determinism is engineered
by asserting the *exact* SQL bytes, not just behaviour.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from signalforge.warehouse.adapters.snowflake import SnowflakeAdapter
from signalforge.warehouse.errors import (
    QuerySyntaxError,
    SamplingRequiresPartitionFilterError,
    UnknownTableSizeError,
)
from signalforge.warehouse.models import PartitionFilter, TableRef
from tests.warehouse._fake_snowflake import FakeSnowflakeConnection

# A regex that matches the INFORMATION_SCHEMA size lookup but NOT the sample
# query, so the two expectations can't accidentally cross-match.
_SIZE_QUERY = r"INFORMATION_SCHEMA\.TABLES"
# The sample query starts with SELECT * and never touches INFORMATION_SCHEMA.
_SAMPLE_QUERY = r"SELECT \* FROM"

# ``project`` is the database; it follows :class:`TableRef`'s GCP-style
# project-id grammar (lowercase start, >= 6 chars), but Snowflake quotes it
# verbatim per-component. ``dataset`` (schema) / ``name`` use the strict
# identifier regex, so uppercase is fine.
_TABLE = TableRef(project="mydatabase", dataset="SCH", name="ORDERS")


def _make_adapter(conn: FakeSnowflakeConnection) -> SnowflakeAdapter:
    """
    Create a SnowflakeAdapter bound to the provided FakeSnowflakeConnection.
    
    Parameters:
        conn (FakeSnowflakeConnection): Fake or recording connection to use for the adapter.
    
    Returns:
        SnowflakeAdapter: Adapter instance that uses the supplied connection.
    """
    return SnowflakeAdapter(connection=conn)


# ---------------------------------------------------------------------------
# A recording connection so we can capture the exact executed SQL.
# ---------------------------------------------------------------------------


class _RecordingConnection(FakeSnowflakeConnection):
    """A :class:`FakeSnowflakeConnection` that records every executed SQL."""

    def __init__(self, **kwargs: object) -> None:
        """
        Initialize the recording connection and prepare the executed-SQL log.
        
        This constructor forwards all keyword arguments to the base connection initializer and creates
        an `executed` list that will capture every SQL string executed by the connection in order.
        
        Attributes:
            executed (list[str]): Recorded SQL statements appended in execution order.
        """
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.executed: list[str] = []

    def _consume_execute(self, sql: str):  # type: ignore[override]
        """
        Record the executed SQL string and forward execution to the underlying implementation.
        
        This appends the provided SQL to self.executed as a record of executed statements, then returns whatever value the underlying execution method returns.
        
        Parameters:
            sql (str): The SQL statement that was executed.
        
        Returns:
            The result returned by the underlying execution implementation.
        """
        self.executed.append(sql)
        return super()._consume_execute(sql)


# ---------------------------------------------------------------------------
# Determinism (DEC-006)
# ---------------------------------------------------------------------------


def test_sample_sql_is_byte_identical_across_two_calls() -> None:
    """
    Verify that calling sample_rows with the same table, n, and partition_filter produces byte-identical sample SQL across separate runs.
    
    Asserts the generated sample query is identical across two independent connections and contains the expected deterministic sampling constructs: the hash-mod bucket expression, an ORDER BY on ABS(HASH(*)), a LIMIT matching the requested sample size, and fully qualified, per-component double-quoted table identifiers.
    """
    sample_sqls: list[str] = []
    for _ in range(2):
        conn = _RecordingConnection()
        conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
        conn.expect_execute(
            matching=_SAMPLE_QUERY,
            returns=[(1,)],
            description=[("ID",)],
        )
        adapter = _make_adapter(conn)
        adapter.sample_rows(_TABLE, 100)
        # The sample query is the second execute.
        sample_sqls.append(conn.executed[1])

    assert sample_sqls[0] == sample_sqls[1]
    sql = sample_sqls[0]
    # num_rows=1000, n=100 → bucket = max(1000//100, 1) = 10.
    assert "MOD(ABS(HASH(*)), 10) < 1" in sql
    assert "ORDER BY ABS(HASH(*))" in sql
    assert "LIMIT 100" in sql
    # Per-component double-quoting.
    assert '"mydatabase"."SCH"."ORDERS"' in sql


# ---------------------------------------------------------------------------
# Sizing branches (DEC-005)
# ---------------------------------------------------------------------------


def test_null_row_count_no_filter_raises_unknown_table_size() -> None:
    """``ROW_COUNT`` NULL (view/MV) + no partition_filter → fail loud."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(None,)])
    adapter = _make_adapter(conn)

    with pytest.raises(UnknownTableSizeError):
        adapter.sample_rows(_TABLE, 100)


def test_zero_row_count_no_filter_raises_unknown_table_size() -> None:
    """``ROW_COUNT`` of 0 + no partition_filter routes through the same
    unknown-size pathway as NULL (mirrors BigQuery's ``num_rows == 0`` branch)
    → fail loud. Guards against a regression that split 0 from None."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(0,)])
    adapter = _make_adapter(conn)

    with pytest.raises(UnknownTableSizeError):
        adapter.sample_rows(_TABLE, 100)


def test_no_matching_row_count_row_raises_unknown_table_size() -> None:
    """No INFORMATION_SCHEMA row at all (empty fetchall) → unknown size."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[])
    adapter = _make_adapter(conn)

    with pytest.raises(UnknownTableSizeError):
        adapter.sample_rows(_TABLE, 100)


def test_null_row_count_with_filter_uses_bucket_1000() -> None:
    """``ROW_COUNT`` NULL + partition_filter present → bucket=1000 fallback."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(None,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("ID",)])
    adapter = _make_adapter(conn)

    pf = PartitionFilter(column="DT", op=">=", value=date(2024, 1, 1))
    adapter.sample_rows(_TABLE, 100, partition_filter=pf)

    sample_sql = conn.executed[1]
    assert "MOD(ABS(HASH(*)), 1000) < 1" in sample_sql


def test_huge_row_count_no_filter_raises_requires_partition_filter() -> None:
    """
    Verifies that sampling a table with ROW_COUNT >= 100,000,000 and no partition filter raises SamplingRequiresPartitionFilterError.
    
    This test fakes the INFORMATION_SCHEMA row count to be 100,000,000 and asserts that calling sample_rows without a partition_filter fails with the specific guard error.
    """
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(100_000_000,)])
    adapter = _make_adapter(conn)

    with pytest.raises(SamplingRequiresPartitionFilterError):
        adapter.sample_rows(_TABLE, 100)


def test_huge_row_count_with_filter_proceeds() -> None:
    """``ROW_COUNT >= 100M`` + a partition_filter → proceeds (bucket sized)."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(200_000_000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("ID",)])
    adapter = _make_adapter(conn)

    pf = PartitionFilter(column="DT", op=">=", value=date(2024, 1, 1))
    adapter.sample_rows(_TABLE, 100, partition_filter=pf)

    # bucket = max(200_000_000 // 100, 1) = 2_000_000.
    assert "MOD(ABS(HASH(*)), 2000000) < 1" in conn.executed[1]


def test_normal_row_count_buckets_num_rows_over_n() -> None:
    """``bucket = max(num_rows // n, 1)`` for a normal-sized table."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(5000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("ID",)])
    adapter = _make_adapter(conn)

    adapter.sample_rows(_TABLE, 100)

    # bucket = max(5000 // 100, 1) = 50.
    assert "MOD(ABS(HASH(*)), 50) < 1" in conn.executed[1]


def test_tiny_table_buckets_floor_at_one() -> None:
    """``num_rows < n`` → ``bucket = max(num_rows // n, 1) = 1`` (floor)."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(5,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("ID",)])
    adapter = _make_adapter(conn)

    adapter.sample_rows(_TABLE, 100)

    assert "MOD(ABS(HASH(*)), 1) < 1" in conn.executed[1]


# ---------------------------------------------------------------------------
# n <= 0 guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_n", [0, -1, -100])
def test_non_positive_n_raises_value_error(bad_n: int) -> None:
    """``n <= 0`` → ``ValueError`` BEFORE any warehouse contact."""
    conn = FakeSnowflakeConnection()  # no expectations queued → any query raises
    adapter = _make_adapter(conn)

    with pytest.raises(ValueError, match="requires n > 0"):
        adapter.sample_rows(_TABLE, bad_n)


# ---------------------------------------------------------------------------
# Dict shaping via cursor.description (DEC-010)
# ---------------------------------------------------------------------------


def test_tuple_rows_shaped_into_dicts_via_description() -> None:
    """Tuple ``fetchall()`` results + a ``description`` → list of dicts keyed
    by column name (DEC-010 — no ``DictCursor`` dependency)."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(
        matching=_SAMPLE_QUERY,
        returns=[(1, "alice"), (2, "bob")],
        # DB-API descriptor: element [0] is the column name.
        description=[("ID", "NUMBER"), ("NAME", "TEXT")],
    )
    adapter = _make_adapter(conn)

    rows = adapter.sample_rows(_TABLE, 100)

    assert rows == [
        {"ID": 1, "NAME": "alice"},
        {"ID": 2, "NAME": "bob"},
    ]


# ---------------------------------------------------------------------------
# Partition filter rendering (DEC-006)
# ---------------------------------------------------------------------------


def test_datetime_partition_filter_renders_timestamp_cast() -> None:
    """A ``datetime`` value renders via the ``'…'::TIMESTAMP`` template and is
    ANDed into the WHERE."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("ID",)])
    adapter = _make_adapter(conn)

    pf = PartitionFilter(column="CREATED_AT", op=">=", value=datetime(2024, 1, 2, 3, 4, 5))
    adapter.sample_rows(_TABLE, 100, partition_filter=pf)

    sql = conn.executed[1]
    assert "'2024-01-02T03:04:05'::TIMESTAMP" in sql
    assert '"CREATED_AT" >= ' in sql
    assert " AND " in sql


def test_date_partition_filter_renders_date_cast() -> None:
    """A ``date`` value renders via the ``'…'::DATE`` template."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("ID",)])
    adapter = _make_adapter(conn)

    pf = PartitionFilter(column="DT", op="=", value=date(2024, 6, 15))
    adapter.sample_rows(_TABLE, 100, partition_filter=pf)

    assert "'2024-06-15'::DATE" in conn.executed[1]


def test_str_partition_filter_value_is_escaped_inside_single_quotes() -> None:
    """A ``str`` value is escaped (single-quote → backslash-quote) inside the
    single-quoted literal — defends against breaking out of the literal."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("ID",)])
    adapter = _make_adapter(conn)

    pf = PartitionFilter(column="REGION", op="=", value="o'hare")
    adapter.sample_rows(_TABLE, 100, partition_filter=pf)

    sql = conn.executed[1]
    assert "'o\\'hare'" in sql
    assert '"REGION" = ' in sql


# ---------------------------------------------------------------------------
# CURRENT_DATABASE() fallback when project is None (DEC-005 edge)
# ---------------------------------------------------------------------------


def test_project_none_size_query_uses_current_database() -> None:
    """When ``table.project`` is ``None`` the INFORMATION_SCHEMA lookup falls
    back to ``CURRENT_DATABASE().`` (documented edge for direct callers)."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("ID",)])
    adapter = _make_adapter(conn)

    table = TableRef(project=None, dataset="SCH", name="ORDERS")
    adapter.sample_rows(table, 100)

    size_sql = conn.executed[0]
    assert "CURRENT_DATABASE().INFORMATION_SCHEMA.TABLES" in size_sql
    # Two-part quoting on the sample query when project is None.
    assert '"SCH"."ORDERS"' in conn.executed[1]


def test_size_query_embeds_escaped_string_literals_case_insensitively() -> None:
    """The size lookup embeds schema/name as escaped STRING LITERALS and
    matches case-insensitively (Snowflake folds unquoted identifiers)."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("ID",)])
    adapter = _make_adapter(conn)

    adapter.sample_rows(_TABLE, 100)

    size_sql = conn.executed[0]
    assert "UPPER(TABLE_SCHEMA) = UPPER('SCH')" in size_sql
    assert "UPPER(TABLE_NAME) = UPPER('ORDERS')" in size_sql
    assert '"mydatabase".INFORMATION_SCHEMA.TABLES' in size_sql


# ---------------------------------------------------------------------------
# SDK exception mapping (DEC-009)
# ---------------------------------------------------------------------------


def test_sdk_programming_error_maps_to_query_syntax_error() -> None:
    """
    Verifies that a Snowflake `ProgrammingError` raised during the sample query is converted to `QuerySyntaxError`.
    
    The test injects a fake connection that returns a `snowflake.connector.errors.ProgrammingError` for the sampling SQL and asserts that `sample_rows` raises `QuerySyntaxError`.
    """
    pytest.importorskip("snowflake.connector")
    from snowflake.connector import errors as sfe

    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(
        matching=_SAMPLE_QUERY,
        returns=sfe.ProgrammingError("SQL compilation error: bad syntax"),
    )
    adapter = _make_adapter(conn)

    with pytest.raises(QuerySyntaxError):
        adapter.sample_rows(_TABLE, 100)


def test_size_query_programming_error_maps_to_query_syntax_error() -> None:
    """A connector ``ProgrammingError`` from the INFORMATION_SCHEMA size query
    maps to :class:`QuerySyntaxError` (the ``_execute`` mapped-error branch)."""
    pytest.importorskip("snowflake.connector")
    from snowflake.connector import errors as sfe

    conn = FakeSnowflakeConnection()
    conn.expect_execute(
        matching=_SIZE_QUERY,
        returns=sfe.ProgrammingError("SQL compilation error: bad table"),
    )
    adapter = _make_adapter(conn)

    with pytest.raises(QuerySyntaxError):
        adapter.sample_rows(_TABLE, 100)


def test_size_query_unmapped_error_passes_through_unchanged() -> None:
    """An exception ``map_snowflake_exception`` does not map (``mapped is exc``)
    is re-raised unchanged from ``_execute`` — the passthrough branch."""
    sentinel = RuntimeError("transient network blip")
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=sentinel)
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError) as exc_info:
        adapter.sample_rows(_TABLE, 100)
    assert exc_info.value is sentinel


def test_sample_query_unmapped_error_passes_through_unchanged() -> None:
    """An unmapped exception from the sample query is re-raised unchanged from
    ``_execute_to_dicts`` — the passthrough branch."""
    sentinel = RuntimeError("transient network blip")
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=sentinel)
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError) as exc_info:
        adapter.sample_rows(_TABLE, 100)
    assert exc_info.value is sentinel


def test_sample_rows_passes_through_dict_rows_unchanged() -> None:
    """A connection that vends mapping rows (DictCursor-style) is handled by
    ``_rows_to_dicts``'s dict passthrough branch — no description needed."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(
        matching=_SAMPLE_QUERY,
        returns=[{"ID": 1, "AMOUNT": 10}, {"ID": 2, "AMOUNT": 20}],
    )
    adapter = _make_adapter(conn)

    rows = adapter.sample_rows(_TABLE, 100)

    assert rows == [{"ID": 1, "AMOUNT": 10}, {"ID": 2, "AMOUNT": 20}]


def test_get_connection_lazily_builds_real_client_when_none_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``connection=`` injected, ``_get_connection`` lazily builds via
    the shim's ``make_real_client`` (and pins it as ``_active_session``)."""
    built = FakeSnowflakeConnection()
    built.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    built.expect_execute(matching=_SAMPLE_QUERY, returns=[], description=[("ID",)])
    calls: list[dict[str, object]] = []

    def _fake_make_real_client(**kwargs: object) -> FakeSnowflakeConnection:
        """
        Record any keyword arguments passed to the fake client factory and return a prebuilt FakeSnowflakeConnection.
        
        Parameters:
        	kwargs (object): Arbitrary keyword arguments supplied when constructing a real client; each call's kwargs are appended to the outer `calls` list for inspection.
        
        Returns:
        	built (FakeSnowflakeConnection): The preconstructed fake Snowflake connection instance returned for tests.
        """
        calls.append(kwargs)
        return built

    monkeypatch.setattr(
        "signalforge.warehouse.adapters._snowflake_client.make_real_client",
        _fake_make_real_client,
    )
    adapter = SnowflakeAdapter(account="acme-prod", warehouse="WH")

    assert adapter._get_connection() is built
    assert adapter._active_session is built
    assert len(calls) == 1
    # A second call reuses the built connection (no re-build).
    assert adapter._get_connection() is built
    assert len(calls) == 1
