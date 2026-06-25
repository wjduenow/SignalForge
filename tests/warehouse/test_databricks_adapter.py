"""Adapter unit tests for the Databricks connection seam, fail-soft cleanup,
fake, and exception mapping (#224 US-002).

The Databricks analogue of the Snowflake #122 / #124 connection / cleanup /
error-mapping work. Drives the :class:`DatabricksAdapter` through its injected
:class:`FakeDatabricksConnection`, pins the cleanup-boundary fail-soft contract
(swallow-and-warn, raw session id ONLY in the failure WARNING, hashed id in the
success INFO, idempotent second exit), the ``__repr__`` redaction rule, and the
full :func:`map_databricks_exception` taxonomy (constructed from the connector's
own ``databricks.sql.exc`` classes — a dev dependency, so they build offline).

These are OFFLINE unit tests (no ``@pytest.mark.databricks`` marker) — they run
in the default suite, like the Snowflake exception-mapping tests.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import pytest

from signalforge.warehouse._sample_id import _hash_session_id
from signalforge.warehouse.adapters._databricks_client import (
    _DatabricksClientProtocol,
    _DatabricksCursorProtocol,
    _extract_unresolved_column,
    map_databricks_exception,
)
from signalforge.warehouse.adapters.databricks import DatabricksAdapter
from signalforge.warehouse.errors import (
    ColumnNotFoundError,
    QuerySyntaxError,
    SamplingRequiresPartitionFilterError,
    TableNotFoundError,
    UnknownTableSizeError,
    WarehouseAuthError,
    WarehouseError,
)
from signalforge.warehouse.models import PartitionFilter, TableRef
from tests.warehouse._fake_databricks import FakeDatabricksConnection

_LOGGER_NAME = "signalforge.warehouse"


# ---------------------------------------------------------------------------
# FakeDatabricksConnection — drives adapter ops (later beads depend on it)
# ---------------------------------------------------------------------------


def test_fake_satisfies_connection_and_cursor_protocols() -> None:
    """The fake structurally satisfies the duck-typed protocols the adapter
    consumes — so later beads can inject it wherever a real connection goes."""
    fake = FakeDatabricksConnection()
    assert isinstance(fake, _DatabricksClientProtocol)
    assert isinstance(fake.cursor(), _DatabricksCursorProtocol)


def test_fake_cursor_execute_fetchall_description_roundtrip() -> None:
    """``expect_execute`` queues a round-trip; ``execute`` consumes it and
    stashes rows + description for ``fetchall`` / ``description``."""
    fake = FakeDatabricksConnection()
    fake.expect_execute(
        matching=r"SELECT COUNT",
        returns=[(0,)],
        description=[("failures",)],
    )
    cur = fake.cursor()
    cur.execute("SELECT COUNT(*) AS failures FROM (SELECT 1) AS t")
    assert cur.fetchall() == [(0,)]
    assert cur.description == [("failures",)]
    fake.assert_all_expectations_met()


def test_fake_execute_can_raise_a_registered_exception() -> None:
    """``returns=<Exception>`` raises on consumption — drives the adapter's
    error-mapping path in later beads."""
    boom = RuntimeError("boom")
    fake = FakeDatabricksConnection()
    fake.expect_execute(matching=r"SELECT", returns=boom)
    cur = fake.cursor()
    with pytest.raises(RuntimeError, match="boom"):
        cur.execute("SELECT 1")


def test_fake_unexpected_query_raises_loudly() -> None:
    """A query with no matching expectation raises ``AssertionError`` (the
    NO-MagicMock posture — nothing auto-passes)."""
    fake = FakeDatabricksConnection()
    cur = fake.cursor()
    with pytest.raises(AssertionError, match="unexpected query"):
        cur.execute("SELECT 1")


def test_fake_assert_all_expectations_met_flags_unconsumed() -> None:
    fake = FakeDatabricksConnection()
    fake.expect_execute(matching=r"SELECT", returns=[(1,)])
    with pytest.raises(AssertionError, match="Unconsumed expectations"):
        fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# Connection seam — _get_connection pins the injected fake as the session
# ---------------------------------------------------------------------------


def test_get_connection_returns_injected_fake_and_pins_active_session() -> None:
    """The injectable ``connection=`` seam: ``_get_connection`` returns the
    injected fake (no lazy build) and records it as the active session so the
    ``__exit__`` cleanup boundary has something to tear down."""
    fake = FakeDatabricksConnection()
    adapter = DatabricksAdapter(connection=fake)

    assert adapter._active_session is None  # not pinned until first use
    conn = adapter._get_connection()

    assert conn is fake
    assert adapter._connection is fake
    assert adapter._active_session is fake


# ---------------------------------------------------------------------------
# Fail-soft cleanup — success INFO uses the hashed id
# ---------------------------------------------------------------------------


def test_exit_closes_connection_and_logs_hashed_session_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A clean ``__exit__`` closes the connection once, resets the session
    state, and logs ONE INFO carrying the HASHED session id (never the raw
    value)."""
    fake = FakeDatabricksConnection(session_id="sess-xyz")
    adapter = DatabricksAdapter(connection=fake)
    adapter._get_connection()  # pin the active session

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        adapter.__exit__(None, None, None)

    assert fake.close_call_count == 1
    assert adapter._active_session is None
    # _connection is deliberately NOT nulled (would discard an injected fake).
    assert adapter._connection is fake

    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    msg = infos[0].getMessage()
    assert "sess-xyz" not in msg  # raw id never leaks on the happy path
    assert _hash_session_id("sess-xyz") in msg


def test_with_block_triggers_cleanup_after_connection_opened(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Full ``with adapter:`` flow — opening the connection inside the block
    pins the session, and leaving the block closes it exactly once."""
    fake = FakeDatabricksConnection(session_id="sess-with")
    with (
        caplog.at_level(logging.INFO, logger=_LOGGER_NAME),
        DatabricksAdapter(connection=fake) as adapter,
    ):
        assert isinstance(adapter, DatabricksAdapter)
        adapter._get_connection()
    assert fake.close_call_count == 1
    assert adapter._active_session is None


# ---------------------------------------------------------------------------
# Fail-soft cleanup — failure swallows + warns with the raw session id
# ---------------------------------------------------------------------------


def test_exit_swallows_close_failure_and_warns_with_raw_session_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``close()`` failure is swallowed (``__exit__`` must NOT raise) and
    produces EXACTLY ONE WARNING carrying the RAW session id (the deliberate
    narrow exception to redaction) plus the failure class name, with NO
    traceback attached."""
    fake = FakeDatabricksConnection(
        session_id="sess-abc",
        close_raises=RuntimeError("close blew up"),
    )
    adapter = DatabricksAdapter(connection=fake)
    adapter._get_connection()

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        # Must not raise — fail-soft.
        adapter.__exit__(None, None, None)

    warnings_ = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings_) == 1
    rec = warnings_[0]
    assert rec.exc_info is None  # no traceback attached
    msg = rec.getMessage()
    assert "sess-abc" in msg  # raw id present in the failure WARNING
    assert "RuntimeError" in msg
    # No manual cleanup command (no DROP statement) and no auto-expire countdown.
    assert "auto-expire" not in msg
    assert "DROP TABLE" not in msg.upper()
    assert "No manual cleanup command is possible" in msg
    # State reset even on the failure path.
    assert adapter._active_session is None


def test_second_exit_is_a_clean_no_op(caplog: pytest.LogCaptureFixture) -> None:
    """After a first ``__exit__`` resets the session state, a second ``__exit__``
    is a no-op: no further ``close()`` call and no second WARNING."""
    fake = FakeDatabricksConnection(close_raises=RuntimeError("x"))
    adapter = DatabricksAdapter(connection=fake)
    adapter._get_connection()

    adapter.__exit__(None, None, None)
    assert fake.close_call_count == 1

    caplog.clear()  # drop the first exit's WARNING so we measure ONLY the second
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        adapter.__exit__(None, None, None)  # second exit
    assert fake.close_call_count == 1  # not called again
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_cleanup_with_no_opened_connection_is_a_no_op(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_cleanup_active_session`` returns immediately when no connection was
    opened — no close, no log."""
    fake = FakeDatabricksConnection()
    adapter = DatabricksAdapter(connection=fake)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        adapter._cleanup_active_session()
    assert fake.close_call_count == 0
    assert caplog.records == []


def test_read_session_id_falls_back_to_get_session_id_hex() -> None:
    """A real ``databricks.sql`` ``Connection`` exposes ``get_session_id_hex()``
    rather than a ``session_id`` attribute — the defensive reader falls back to
    the getter."""

    class _ConnWithHexGetter:
        def get_session_id_hex(self) -> str:
            return "deadbeef"

        def cursor(self) -> object:  # pragma: no cover - protocol filler
            return object()

        def close(self) -> None:  # pragma: no cover - not exercised here
            return None

    assert DatabricksAdapter._read_session_id(_ConnWithHexGetter()) == "deadbeef"  # type: ignore[arg-type]


def test_read_session_id_swallows_getter_failure() -> None:
    """A broken ``get_session_id_hex()`` must NOT raise out of the cleanup
    boundary — the reader returns ``None`` instead."""

    class _ConnWithBrokenGetter:
        def get_session_id_hex(self) -> str:
            raise RuntimeError("connection is dead")

        def cursor(self) -> object:  # pragma: no cover - protocol filler
            return object()

        def close(self) -> None:  # pragma: no cover - not exercised here
            return None

    assert DatabricksAdapter._read_session_id(_ConnWithBrokenGetter()) is None  # type: ignore[arg-type]


def test_read_session_id_returns_none_when_no_id_surface() -> None:
    """A connection exposing neither ``session_id`` nor ``get_session_id_hex``
    yields ``None`` — the cleanup log simply omits the id."""

    class _ConnWithNoIdSurface:
        def cursor(self) -> object:  # pragma: no cover - protocol filler
            return object()

        def close(self) -> None:  # pragma: no cover - not exercised here
            return None

    assert DatabricksAdapter._read_session_id(_ConnWithNoIdSurface()) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# __repr__ credential redaction (AC-3)
# ---------------------------------------------------------------------------


def test_repr_shows_only_safe_fields_never_credentials() -> None:
    """``__repr__`` renders ONLY ``host`` + ``http_path`` + ``catalog`` — never
    ``token`` / ``schema`` / ``client_secret`` (nor their values)."""
    adapter = DatabricksAdapter(
        host="dbc-abc123.cloud.databricks.com",
        http_path="/sql/1.0/warehouses/abc123",
        token="dapideadbeefcafe",
        catalog="main",
        schema="analytics",
        auth_type="databricks-oauth",
        client_id="svc-client",
        client_secret="s3cret-oauth",
    )
    rendered = repr(adapter)

    assert "dbc-abc123.cloud.databricks.com" in rendered
    assert "/sql/1.0/warehouses/abc123" in rendered
    assert "main" in rendered

    assert "dapideadbeefcafe" not in rendered
    assert "token" not in rendered
    assert "analytics" not in rendered
    assert "schema" not in rendered
    assert "s3cret-oauth" not in rendered
    assert "client_secret" not in rendered
    assert "client_id" not in rendered
    assert "svc-client" not in rendered


# ---------------------------------------------------------------------------
# map_databricks_exception taxonomy (constructed from the connector's own
# databricks.sql.exc classes — a dev dependency, so they build offline)
# ---------------------------------------------------------------------------


def _dbe() -> object:
    """Import ``databricks.sql.exc`` lazily so the class objects match whatever
    ``map_databricks_exception`` re-imports at call time (mirrors the Snowflake
    mapping test's ``_sfe`` helper)."""
    from databricks.sql import exc as dbe

    return dbe


def test_table_not_found_maps_to_table_not_found_with_context() -> None:
    dbe = _dbe()
    exc = dbe.ServerOperationError(  # type: ignore[attr-defined]
        "[TABLE_OR_VIEW_NOT_FOUND] Table or view not found: main.sch.foo"
    )
    mapped = map_databricks_exception(exc, context={"table": "main.sch.foo"})
    assert isinstance(mapped, TableNotFoundError)
    assert mapped.table == "main.sch.foo"


def test_table_not_found_without_context_uses_unknown_placeholder() -> None:
    dbe = _dbe()
    exc = dbe.ServerOperationError(  # type: ignore[attr-defined]
        "[TABLE_OR_VIEW_NOT_FOUND] Table or view not found: foo"
    )
    mapped = map_databricks_exception(exc)
    assert isinstance(mapped, TableNotFoundError)
    assert mapped.table == "<unknown>"


def test_unresolved_column_maps_to_column_not_found() -> None:
    dbe = _dbe()
    exc = dbe.ServerOperationError(  # type: ignore[attr-defined]
        "[UNRESOLVED_COLUMN.WITH_SUGGESTION] A column, variable, or function "
        "parameter with name `bad_col` cannot be resolved."
    )
    mapped = map_databricks_exception(exc, context={"table": "main.sch.orders"})
    assert isinstance(mapped, ColumnNotFoundError)
    assert mapped.table == "main.sch.orders"
    assert mapped.column == "bad_col"


def test_residual_programming_error_maps_to_query_syntax() -> None:
    dbe = _dbe()
    exc = dbe.ServerOperationError(  # type: ignore[attr-defined]
        "[PARSE_SYNTAX_ERROR] Syntax error at or near 'SELEKT'"
    )
    mapped = map_databricks_exception(exc, context={"table": "T"})
    assert isinstance(mapped, QuerySyntaxError)
    assert "syntax error" in mapped.detail.lower()


def test_table_column_split_runs_before_query_syntax_fallthrough() -> None:
    """The Table/Column arms precede the broad ``QuerySyntaxError`` fallthrough —
    a table-not-found error never lands on ``QuerySyntaxError``."""
    dbe = _dbe()
    exc = dbe.ServerOperationError(  # type: ignore[attr-defined]
        "[TABLE_OR_VIEW_NOT_FOUND] Table or view not found"
    )
    mapped = map_databricks_exception(exc)
    assert not isinstance(mapped, QuerySyntaxError)
    assert isinstance(mapped, TableNotFoundError)


def test_auth_flavoured_server_error_maps_to_auth() -> None:
    """A SQL-error type carrying an auth marker maps to ``WarehouseAuthError``
    (before the residual ``QuerySyntaxError`` fallthrough)."""
    dbe = _dbe()
    exc = dbe.ServerOperationError("PERMISSION_DENIED: permission denied on table foo")  # type: ignore[attr-defined]
    mapped = map_databricks_exception(exc)
    assert isinstance(mapped, WarehouseAuthError)


def test_connect_time_auth_operational_error_maps_to_auth() -> None:
    """A connect-time / transient type (``OperationalError``) carrying an auth
    marker still maps to auth via the non-SQL-error message-marker arm."""
    dbe = _dbe()
    exc = dbe.OperationalError("Invalid access token")  # type: ignore[attr-defined]
    mapped = map_databricks_exception(exc)
    assert isinstance(mapped, WarehouseAuthError)


def test_transient_operational_error_passes_through_unchanged() -> None:
    """A non-auth transient ``OperationalError`` (network blip) is returned
    unchanged — NOT mis-mapped to ``QuerySyntaxError`` (the reason the
    Table/Column/Syntax split is scoped to the SQL-error types)."""
    dbe = _dbe()
    exc = dbe.OperationalError("Connection reset by peer")  # type: ignore[attr-defined]
    mapped = map_databricks_exception(exc)
    assert mapped is exc


def test_non_connector_exception_passes_through_unchanged() -> None:
    exc = ValueError("not a databricks error")
    mapped = map_databricks_exception(exc)
    assert mapped is exc


def test_extract_unresolved_column_falls_back_to_full_message() -> None:
    msg = "completely unparseable wording with no identifier token"
    assert _extract_unresolved_column(msg) == msg


def test_extract_unresolved_column_pulls_bare_token() -> None:
    assert _extract_unresolved_column("with name `my_col` cannot be resolved") == "my_col"


# ---------------------------------------------------------------------------
# get_row_count + sizing + sample_rows (#224 US-003)
# ---------------------------------------------------------------------------

# The size query is ``SELECT COUNT(*)`` (no metadata table); the sample query
# carries the dialect hash expression and never touches COUNT, so the two
# expectations can't cross-match.
_COUNT_QUERY = r"SELECT COUNT\(\*\)"
_SAMPLE_QUERY = r"xxhash64"

# ``project`` is the Unity Catalog catalog (short names like ``main`` are
# allowed since US-001 relaxed TableRef.project). ``identifier_case='lower'`` so
# the conventionally-named warehouse object resolves under fold-to-lower.
_TABLE = TableRef(project="main", dataset="sales", name="orders")


class _RecordingDatabricksConnection(FakeDatabricksConnection):
    """A :class:`FakeDatabricksConnection` that records every executed SQL."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.executed: list[str] = []

    def _consume_execute(self, sql: str):  # type: ignore[override]
        self.executed.append(sql)
        return super()._consume_execute(sql)


def _make_adapter(conn: FakeDatabricksConnection) -> DatabricksAdapter:
    return DatabricksAdapter(connection=conn)


# ---- Determinism (DEC-002) ------------------------------------------------


def test_sample_sql_is_byte_identical_across_two_calls() -> None:
    """Identical ``(table, n, partition_filter)`` → byte-identical executed
    sample SQL. Pins the deterministic hash-mod contract and the inline shape."""
    sample_sqls: list[str] = []
    for _ in range(2):
        conn = _RecordingDatabricksConnection()
        conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
        conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
        adapter = _make_adapter(conn)
        adapter.sample_rows(_TABLE, 100)
        sample_sqls.append(conn.executed[1])

    assert sample_sqls[0] == sample_sqls[1]
    sql = sample_sqls[0]
    # num_rows=1000, n=100 → bucket = max(1000//100, 1) = 10. Inline-predicate
    # shape (sample_hash_in_projection=False): the masked xxhash64 expression
    # sits directly in WHERE / ORDER BY (no Snowflake-style projection subquery).
    assert "(xxhash64(to_json(struct(*))) & 9223372036854775807)" in sql
    assert "MOD((xxhash64(to_json(struct(*))) & 9223372036854775807), 10) < 1" in sql
    assert "ORDER BY (xxhash64(to_json(struct(*))) & 9223372036854775807)" in sql
    assert "LIMIT 100" in sql
    assert "EXCLUDE" not in sql  # not the projection-subquery shape
    # Per-component backtick quoting, folded to lower (#124): catalog "main",
    # schema "sales", table "orders".
    assert "`main`.`sales`.`orders`" in sql


def test_sample_sql_folds_mixed_case_identifiers_to_lower() -> None:
    """Identifier_case='lower' folds a mixed-case manifest identifier so the
    quoted (case-sensitive) name resolves against the real Unity Catalog object —
    the same fold the prune compiler applies (CREATE-vs-REFERENCE parity)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
    adapter = _make_adapter(conn)

    table = TableRef(project="Main", dataset="Sales", name="Orders")
    adapter.sample_rows(table, 100)

    assert "`main`.`sales`.`orders`" in conn.executed[1]


# ---- Sizing branches (DEC-003) --------------------------------------------


def test_get_row_count_returns_count() -> None:
    """The Databricks override returns ``SELECT COUNT(*)`` — the seam the prune
    engine calls under ``prune.scope: sample``."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(2_500_000,)])
    adapter = _make_adapter(conn)

    assert adapter.get_row_count(_TABLE) == 2_500_000


def test_get_row_count_query_is_quoted_count_star() -> None:
    """The COUNT query targets the fold-then-quoted table ref."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(10,)])
    adapter = _make_adapter(conn)

    adapter.get_row_count(_TABLE)

    assert conn.executed[0] == "SELECT COUNT(*) AS row_count FROM `main`.`sales`.`orders`"


def test_get_row_count_shapes_dict_row() -> None:
    """A dict-cursor-style mapping row is handled — the first value is the count."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[{"row_count": 42}])
    adapter = _make_adapter(conn)

    assert adapter.get_row_count(_TABLE) == 42


def test_get_row_count_returns_none_on_warehouse_error() -> None:
    """A mapped :class:`WarehouseError` (e.g. table not found) → ``None``
    (DEC-003) so the shared sizing pathway decides what to do."""
    dbe = _dbe()
    err = dbe.ServerOperationError(  # type: ignore[attr-defined]
        "[TABLE_OR_VIEW_NOT_FOUND] Table or view not found: main.sales.orders"
    )
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=err)
    adapter = _make_adapter(conn)

    assert adapter.get_row_count(_TABLE) is None


def test_get_row_count_returns_none_on_empty_result() -> None:
    """An empty fetchall (no row) → ``None``."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[])
    adapter = _make_adapter(conn)

    assert adapter.get_row_count(_TABLE) is None


def test_get_row_count_propagates_non_warehouse_error() -> None:
    """An UNMAPPED transient (non-:class:`WarehouseError`) propagates unchanged —
    get_row_count only swallows WarehouseError."""
    sentinel = RuntimeError("transient network blip")
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=sentinel)
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError) as exc_info:
        adapter.get_row_count(_TABLE)
    assert exc_info.value is sentinel
    # And the sentinel is NOT a WarehouseError (proves the catch is scoped).
    assert not isinstance(sentinel, WarehouseError)


def test_unknown_size_no_filter_raises_unknown_table_size() -> None:
    """COUNT unresolvable (error → None) + no partition_filter → fail loud."""
    dbe = _dbe()
    err = dbe.ServerOperationError("[TABLE_OR_VIEW_NOT_FOUND] not found")  # type: ignore[attr-defined]
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=err)
    adapter = _make_adapter(conn)

    with pytest.raises(UnknownTableSizeError):
        adapter.sample_rows(_TABLE, 100)


def test_zero_count_no_filter_raises_unknown_table_size() -> None:
    """A COUNT of 0 + no partition_filter routes through the unknown-size
    pathway (mirrors BigQuery's ``num_rows == 0`` branch) → fail loud."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(0,)])
    adapter = _make_adapter(conn)

    with pytest.raises(UnknownTableSizeError):
        adapter.sample_rows(_TABLE, 100)


def test_unknown_size_with_filter_uses_bucket_1000() -> None:
    """COUNT unresolvable + partition_filter present → bucket=1000 fallback."""
    dbe = _dbe()
    err = dbe.ServerOperationError("[TABLE_OR_VIEW_NOT_FOUND] not found")  # type: ignore[attr-defined]
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=err)
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
    adapter = _make_adapter(conn)

    pf = PartitionFilter(column="dt", op=">=", value=date(2024, 1, 1))
    adapter.sample_rows(_TABLE, 100, partition_filter=pf)

    assert "MOD((xxhash64(to_json(struct(*))) & 9223372036854775807), 1000) < 1" in conn.executed[1]


def test_huge_count_no_filter_raises_requires_partition_filter() -> None:
    """COUNT ``>= 100M`` + no partition_filter → fail loud."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(100_000_000,)])
    adapter = _make_adapter(conn)

    with pytest.raises(SamplingRequiresPartitionFilterError):
        adapter.sample_rows(_TABLE, 100)


def test_huge_count_with_filter_proceeds() -> None:
    """COUNT ``>= 100M`` + a partition_filter → proceeds (bucket sized)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(200_000_000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
    adapter = _make_adapter(conn)

    pf = PartitionFilter(column="dt", op=">=", value=date(2024, 1, 1))
    adapter.sample_rows(_TABLE, 100, partition_filter=pf)

    # bucket = max(200_000_000 // 100, 1) = 2_000_000.
    assert (
        "MOD((xxhash64(to_json(struct(*))) & 9223372036854775807), 2000000) < 1" in conn.executed[1]
    )


def test_normal_count_buckets_num_rows_over_n() -> None:
    """``bucket = max(num_rows // n, 1)`` for a normal-sized table."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(5000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
    adapter = _make_adapter(conn)

    adapter.sample_rows(_TABLE, 100)

    # bucket = max(5000 // 100, 1) = 50.
    assert "MOD((xxhash64(to_json(struct(*))) & 9223372036854775807), 50) < 1" in conn.executed[1]


def test_tiny_table_buckets_floor_at_one() -> None:
    """``num_rows < n`` → ``bucket = max(num_rows // n, 1) = 1`` (floor)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(5,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
    adapter = _make_adapter(conn)

    adapter.sample_rows(_TABLE, 100)

    assert "MOD((xxhash64(to_json(struct(*))) & 9223372036854775807), 1) < 1" in conn.executed[1]


# ---- n <= 0 guard ---------------------------------------------------------


@pytest.mark.parametrize("bad_n", [0, -1, -100])
def test_non_positive_n_raises_value_error(bad_n: int) -> None:
    """``n <= 0`` → ``ValueError`` BEFORE any warehouse contact."""
    conn = FakeDatabricksConnection()  # no expectations → any query raises loudly
    adapter = _make_adapter(conn)

    with pytest.raises(ValueError, match="requires n > 0"):
        adapter.sample_rows(_TABLE, bad_n)


# ---- Dict shaping via cursor.description (DEC-002) -------------------------


def test_tuple_rows_shaped_into_dicts_via_description() -> None:
    """Tuple ``fetchall()`` results + a ``description`` → list of dicts keyed by
    column name (no dict-cursor dependency)."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
    conn.expect_execute(
        matching=_SAMPLE_QUERY,
        returns=[(1, "alice"), (2, "bob")],
        description=[("id", "INT"), ("name", "STRING")],
    )
    adapter = _make_adapter(conn)

    rows = adapter.sample_rows(_TABLE, 100)

    assert rows == [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]


def test_sample_rows_passes_through_dict_rows_unchanged() -> None:
    """A connection that vends mapping rows is handled by the dict passthrough
    branch — no description needed."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
    conn.expect_execute(
        matching=_SAMPLE_QUERY,
        returns=[{"id": 1, "amount": 10}, {"id": 2, "amount": 20}],
    )
    adapter = _make_adapter(conn)

    rows = adapter.sample_rows(_TABLE, 100)

    assert rows == [{"id": 1, "amount": 10}, {"id": 2, "amount": 20}]


# ---- Partition filter rendering (DEC-008) ---------------------------------


def test_datetime_partition_filter_renders_timestamp_literal() -> None:
    """A ``datetime`` value renders via the ``TIMESTAMP '…'`` template and is
    ANDed into the WHERE; the column is fold-then-quoted."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
    adapter = _make_adapter(conn)

    pf = PartitionFilter(column="created_at", op=">=", value=datetime(2024, 1, 2, 3, 4, 5))
    adapter.sample_rows(_TABLE, 100, partition_filter=pf)

    sql = conn.executed[1]
    assert "TIMESTAMP '2024-01-02T03:04:05'" in sql
    assert "`created_at` >= " in sql
    assert " AND " in sql


def test_date_partition_filter_renders_date_literal() -> None:
    """A ``date`` value renders via the ``DATE '…'`` template."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
    adapter = _make_adapter(conn)

    pf = PartitionFilter(column="dt", op="=", value=date(2024, 6, 15))
    adapter.sample_rows(_TABLE, 100, partition_filter=pf)

    assert "DATE '2024-06-15'" in conn.executed[1]


def test_str_partition_filter_value_is_escaped_inside_single_quotes() -> None:
    """A ``str`` value is escaped (single-quote → backslash-quote) inside the
    single-quoted literal — defends against breaking out of the literal."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
    adapter = _make_adapter(conn)

    pf = PartitionFilter(column="region", op="=", value="o'hare")
    adapter.sample_rows(_TABLE, 100, partition_filter=pf)

    sql = conn.executed[1]
    assert "'o\\'hare'" in sql
    assert "`region` = " in sql


# ---- project=None (two-part quoting) --------------------------------------


def test_project_none_uses_two_part_quoting() -> None:
    """When ``table.project`` is ``None`` the COUNT + sample queries use two-part
    `` `schema`.`table` `` quoting."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
    adapter = _make_adapter(conn)

    table = TableRef(project=None, dataset="sales", name="orders")
    adapter.sample_rows(table, 100)

    assert "`sales`.`orders`" in conn.executed[0]
    assert "`sales`.`orders`" in conn.executed[1]


# ---- SDK exception mapping (DEC-009) --------------------------------------


def test_sample_query_programming_error_maps_to_query_syntax_error() -> None:
    """A connector error from the sample query maps to :class:`QuerySyntaxError`
    via ``map_databricks_exception`` (the ``_execute_to_dicts`` mapped branch)."""
    dbe = _dbe()
    err = dbe.ServerOperationError("[PARSE_SYNTAX_ERROR] bad syntax")  # type: ignore[attr-defined]
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=err)
    adapter = _make_adapter(conn)

    with pytest.raises(QuerySyntaxError):
        adapter.sample_rows(_TABLE, 100)


def test_sample_query_unmapped_error_passes_through_unchanged() -> None:
    """An unmapped exception from the sample query is re-raised unchanged from
    ``_execute_to_dicts`` — the passthrough branch."""
    sentinel = RuntimeError("transient network blip")
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=sentinel)
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError) as exc_info:
        adapter.sample_rows(_TABLE, 100)
    assert exc_info.value is sentinel
