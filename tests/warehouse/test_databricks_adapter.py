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

from signalforge.warehouse._sample_id import _compute_run_id, _hash_session_id
from signalforge.warehouse.adapters._databricks_client import (
    _DatabricksClientProtocol,
    _DatabricksCursorProtocol,
    _extract_unresolved_column,
    map_databricks_exception,
)
from signalforge.warehouse.adapters.databricks import DatabricksAdapter
from signalforge.warehouse.errors import (
    ColumnNotFoundError,
    InvalidIdentifierError,
    MaterialisationFailedError,
    QuerySyntaxError,
    SamplingRequiresPartitionFilterError,
    TableNotFoundError,
    UnknownTableSizeError,
    WarehouseAuthError,
    WarehouseError,
)
from signalforge.warehouse.models import (
    DATABRICKS_DIALECT,
    ColumnStats,
    PartitionFilter,
    TableRef,
)
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
    # The close-failure WARNING concerns the SESSION (reaped server-side); it
    # quotes no client-side auto-expire countdown and embeds no DROP statement
    # of its own (materialised tables are handled by the per-table DROP loop
    # above — there are none here since this test never materialised).
    assert "auto-expire" not in msg
    assert "DROP TABLE IF EXISTS" not in msg.upper()
    assert "reaped server-side" in msg
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
    sample SQL. Pins the deterministic hash-mod contract and the
    projection-subquery shape (#226)."""
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
    # num_rows=1000, n=100 → bucket = max(1000//100, 1) = 10. Projection-subquery
    # shape (sample_hash_in_projection=True, #226): the masked xxhash64 hash is
    # computed once in the inner projection alias; WHERE/ORDER BY reference it
    # (Spark rejects struct(*) in a Sort node, so it cannot be inline).
    assert "(xxhash64(to_json(struct(*))) & 9223372036854775807) AS _sf_sample_hash" in sql
    assert "MOD(_sf_sample_hash, 10) < 1" in sql
    assert "ORDER BY _sf_sample_hash" in sql
    assert "LIMIT 100" in sql
    # Databricks strips the helper column with ``EXCEPT`` (Spark), not ``EXCLUDE``.
    assert "SELECT * EXCEPT (_sf_sample_hash)" in sql
    assert "EXCLUDE" not in sql
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


# ---- Cursor-handle release (PR #257 review — no server-side cursor leak) ----


def test_execute_closes_cursor_on_success() -> None:
    """``_execute`` releases the cursor after a successful query so repeated
    queries on the long-lived connection don't leak server-side handles."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(10,)])
    adapter = _make_adapter(conn)

    adapter.get_row_count(_TABLE)

    assert conn.cursors, "expected the adapter to open at least one cursor"
    assert all(c.closed for c in conn.cursors)


def test_execute_closes_cursor_on_failure() -> None:
    """The cursor is released even when the query raises (the ``finally`` arm)."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=RuntimeError("boom"))
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError):
        adapter.get_row_count(_TABLE)

    assert conn.cursors and all(c.closed for c in conn.cursors)


def test_execute_to_dicts_closes_cursor_after_shaping_rows() -> None:
    """``sample_rows`` (via ``_execute_to_dicts``) closes the cursor only after
    ``cursor.description`` has been read to shape the rows."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
    conn.expect_execute(
        matching=_SAMPLE_QUERY, returns=[(1, 10)], description=[("id",), ("amount",)]
    )
    adapter = _make_adapter(conn)

    rows = adapter.sample_rows(_TABLE, 100)

    assert rows == [{"id": 1, "amount": 10}]
    assert conn.cursors and all(c.closed for c in conn.cursors)


def test_materialise_closes_cursor() -> None:
    """``materialise_sample`` releases the CTAS cursor; the temp table lives on
    the pinned connection, not the cursor."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    adapter.materialise_sample(_TABLE, 100)

    assert conn.cursors and all(c.closed for c in conn.cursors)


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

    assert "MOD(_sf_sample_hash, 1000) < 1" in conn.executed[1]


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
    assert "MOD(_sf_sample_hash, 2000000) < 1" in conn.executed[1]


def test_normal_count_buckets_num_rows_over_n() -> None:
    """``bucket = max(num_rows // n, 1)`` for a normal-sized table."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(5000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
    adapter = _make_adapter(conn)

    adapter.sample_rows(_TABLE, 100)

    # bucket = max(5000 // 100, 1) = 50.
    assert "MOD(_sf_sample_hash, 50) < 1" in conn.executed[1]


def test_tiny_table_buckets_floor_at_one() -> None:
    """``num_rows < n`` → ``bucket = max(num_rows // n, 1) = 1`` (floor)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(5,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
    adapter = _make_adapter(conn)

    adapter.sample_rows(_TABLE, 100)

    assert "MOD(_sf_sample_hash, 1) < 1" in conn.executed[1]


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


# ---------------------------------------------------------------------------
# materialise_sample + run_test_sql (#224 US-004)
#
# #226 CERTIFIED LIVE: the live run proved Databricks REJECTS a qualified
# ``CREATE TEMPORARY TABLE <cat>.<sch>.<temp>`` name, so the adapter materialises
# into a real ``CREATE OR REPLACE TABLE`` colocated with the source (dropped at
# session cleanup); the ``databricks-sql-connector`` DOES persist the session
# across queries (the materialised table is reachable from a follow-up
# ``run_test_sql``). The per-row ``to_json(struct(*))`` capture marshalling was
# also certified live (the remaining #226 ledger items are tracked in the plan).
# ---------------------------------------------------------------------------

# Distinct regexes so the sizing COUNT, the CTAS, the run_test_sql COUNT, and the
# capture query can never cross-match in the fake's expectation queue.
_SIZE_QUERY = r"AS row_count"
_CTAS_QUERY = r"CREATE OR REPLACE TABLE"
_FAILURES_QUERY = r"COUNT\(\*\) AS failures"
_CAPTURE_QUERY = r"to_json\(struct"


def _expected_run_id() -> str:
    return _compute_run_id(table=_TABLE, n=100, partition_filter=None)


# ---- materialise_sample — CTAS shape + deterministic temp name (DEC-004/008) ---


def test_materialise_ctas_sql_shape_and_temp_name() -> None:
    """The CTAS contains ``CREATE OR REPLACE TABLE``, the deterministic
    ``_sf_sample_<run_id>`` name (run_id byte-identical to the shared recipe),
    and the inline-predicate sample body (DEC-002) — masked ``xxhash64`` in
    ``WHERE``/``ORDER BY``, ``LIMIT n``, source + temp per-component
    backtick-quoted and folded to lower.

    #226: a real qualified table (NOT ``TEMPORARY TABLE``) — Databricks rejects
    a qualified temp name; the table is dropped at session cleanup.
    """
    conn = _RecordingDatabricksConnection()
    # num_rows=1000, n=100 → bucket = max(1000//100, 1) = 10.
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    adapter.materialise_sample(_TABLE, 100)

    ctas = conn.executed[1]
    run_id = _expected_run_id()
    temp_name = f"_sf_sample_{run_id}"

    assert ctas.startswith("CREATE OR REPLACE TABLE")
    # Projection-subquery shape (sample_hash_in_projection=True, #226): the hash
    # is computed in the inner projection alias; Spark rejects struct(*) in a
    # Sort node, so the ORDER BY references the alias. Databricks strips the
    # helper column with EXCEPT, not Snowflake's EXCLUDE. The temp name appears.
    assert temp_name in ctas
    assert "SELECT * EXCEPT (_sf_sample_hash)" in ctas
    assert "EXCLUDE" not in ctas
    assert "(xxhash64(to_json(struct(*))) & 9223372036854775807) AS _sf_sample_hash" in ctas
    assert "MOD(_sf_sample_hash, 10) < 1" in ctas
    assert "ORDER BY _sf_sample_hash" in ctas
    assert ctas.rstrip().endswith("LIMIT 100")
    # Source per-component quoted + fold-to-lower.
    assert "`main`.`sales`.`orders`" in ctas
    # Temp table colocated with the source catalog / schema, per-component
    # quoted, lower-folded — byte-identical to how the compiler REFERENCEs it.
    assert f"`main`.`sales`.`{temp_name}`" in ctas


def test_materialise_then_exit_drops_table() -> None:
    """A materialised table is a real ``CREATE OR REPLACE TABLE`` (NOT a
    session-temp), so ``__exit__`` explicitly ``DROP TABLE IF EXISTS``-es it
    before closing the connection and clears the tracking list (issue #226)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    conn.expect_execute(matching=r"DROP TABLE IF EXISTS", returns=[])
    adapter = _make_adapter(conn)

    temp_ref = adapter.materialise_sample(_TABLE, 100)
    assert adapter._materialised_tables == [temp_ref]

    adapter.__exit__(None, None, None)

    drops = [q for q in conn.executed if q.startswith("DROP TABLE IF EXISTS")]
    assert len(drops) == 1
    assert temp_ref.name in drops[0]
    # Tracking list cleared so a second ``__exit__`` is a no-op.
    assert adapter._materialised_tables == []
    conn.assert_all_expectations_met()


def test_materialise_then_exit_swallows_drop_failure_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``DROP`` failure at the cleanup boundary is swallowed (``__exit__`` must
    NOT raise) and emits ONE WARNING naming the manual ``DROP`` command — the
    cleanup-boundary fail-soft contract (issue #226)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    conn.expect_execute(matching=r"DROP TABLE IF EXISTS", returns=RuntimeError("boom"))
    adapter = _make_adapter(conn)

    adapter.materialise_sample(_TABLE, 100)
    with caplog.at_level(logging.WARNING):
        adapter.__exit__(None, None, None)  # must NOT raise

    # Exactly ONE drop-failure WARNING for the single materialised table — the
    # documented one-WARNING-per-table contract (not "at least one").
    drop_warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "DROP TABLE IF EXISTS" in rec.getMessage()
    ]
    assert len(drop_warnings) == 1
    # The drop failure must NOT mask the connection close — close still fires
    # after the per-table swallow (the DROP loop precedes conn.close()).
    assert conn.close_call_count == 1
    # State still reset despite the drop failure (idempotent second exit).
    assert adapter._materialised_tables == []
    conn.assert_all_expectations_met()


def test_materialise_returns_fully_qualified_temp_ref() -> None:
    """The returned :class:`TableRef` is fully-qualified via the source catalog /
    schema with the deterministic temp name (DEC-004)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    result = adapter.materialise_sample(_TABLE, 100)

    run_id = _expected_run_id()
    assert result == TableRef(
        project=_TABLE.project, dataset=_TABLE.dataset, name=f"_sf_sample_{run_id}"
    )


def test_materialise_pins_active_session() -> None:
    """``materialise_sample`` pins ``_active_session`` to the connection so a
    follow-up ``run_test_sql`` reaches the materialised table (DEC-006)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    adapter.materialise_sample(_TABLE, 100)

    assert adapter._active_session is conn


def test_materialise_run_id_byte_identical_across_calls() -> None:
    """Identical ``(table, n, partition_filter)`` → byte-identical temp-table
    name across two fresh adapters (DEC-008)."""
    names: list[str] = []
    for _ in range(2):
        conn = _RecordingDatabricksConnection()
        conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
        conn.expect_execute(matching=_CTAS_QUERY, returns=[])
        adapter = _make_adapter(conn)
        names.append(adapter.materialise_sample(_TABLE, 100).name)
    assert names[0] == names[1]
    assert names[0] == f"_sf_sample_{_expected_run_id()}"


def test_materialise_run_id_varies_with_partition_filter() -> None:
    """The ``partition_filter`` is threaded into the deterministic ``run_id``:
    no-filter and two distinct filters yield three distinct temp-table names —
    guarding against a Databricks-specific call dropping the filter arg."""
    names: set[str] = set()
    filters: list[PartitionFilter | None] = [
        None,
        PartitionFilter(column="dt", op=">=", value=date(2026, 1, 1)),
        PartitionFilter(column="dt", op=">=", value=date(2026, 2, 1)),
    ]
    for pf in filters:
        conn = _RecordingDatabricksConnection()
        conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
        conn.expect_execute(matching=_CTAS_QUERY, returns=[])
        adapter = _make_adapter(conn)
        names.add(adapter.materialise_sample(_TABLE, 100, partition_filter=pf).name)
    assert len(names) == 3


def test_materialise_rejects_non_positive_n() -> None:
    """``n <= 0`` → ``ValueError`` before any warehouse contact."""
    conn = FakeDatabricksConnection()  # no expectations → any query raises loudly
    adapter = _make_adapter(conn)

    with pytest.raises(ValueError, match="n > 0"):
        adapter.materialise_sample(_TABLE, 0)

    conn.assert_all_expectations_met()


def test_materialise_ctas_sdk_failure_wraps_in_materialisation_failed() -> None:
    """A CTAS SDK failure → :class:`MaterialisationFailedError` with the
    underlying exception preserved on ``cause`` AND in the raise-from chain."""
    boom = RuntimeError("network blip during CTAS")
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=boom)
    adapter = _make_adapter(conn)

    with pytest.raises(MaterialisationFailedError) as exc_info:
        adapter.materialise_sample(_TABLE, 100)

    assert exc_info.value.cause is boom
    assert exc_info.value.__cause__ is boom
    assert "main.sales.orders" in str(exc_info.value)


def test_materialise_ctas_mapped_error_wraps_in_materialisation_failed() -> None:
    """A mapped connector error (table not found) is routed through
    ``map_databricks_exception`` first, then wrapped — the mapped typed error is
    the ``cause`` (and the raise-from chain still preserves the original SDK
    exception)."""
    dbe = _dbe()
    err = dbe.ServerOperationError(  # type: ignore[attr-defined]
        "[TABLE_OR_VIEW_NOT_FOUND] Table or view not found: main.sales.orders"
    )
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=err)
    adapter = _make_adapter(conn)

    with pytest.raises(MaterialisationFailedError) as exc_info:
        adapter.materialise_sample(_TABLE, 100)

    assert isinstance(exc_info.value.cause, TableNotFoundError)
    assert exc_info.value.__cause__ is err


def test_materialise_logs_hashed_session_id_never_raw(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The success INFO log carries ``session_id_hash`` (blake2b-4), the source
    table, sample_rows, and run_id — never the raw connection ``session_id``."""
    conn = _RecordingDatabricksConnection(session_id="super-secret-session-xyz")
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        adapter.materialise_sample(_TABLE, 100)

    records = [r for r in caplog.records if "materialised sample" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "session_id_hash" in message
    assert "super-secret-session-xyz" not in message
    assert _hash_session_id("super-secret-session-xyz") in message
    assert "main.sales.orders" in message
    assert _expected_run_id() in message


def test_materialise_applies_partition_filter_in_ctas() -> None:
    """A ``PartitionFilter`` lands ONCE in the CTAS ``WHERE`` (rendered via the
    Databricks dialect literal template) alongside the hash-mod predicate."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    pf = PartitionFilter(column="dt", op=">=", value=date(2026, 1, 1))
    adapter.materialise_sample(_TABLE, 100, partition_filter=pf)

    ctas = conn.executed[1]
    # bucket=10; the partition predicate is ANDed after the hash-mod predicate.
    assert "MOD(_sf_sample_hash, 10) < 1 AND " in ctas
    assert "DATE '2026-01-01'" in ctas
    assert ctas.count("DATE '2026-01-01'") == 1


# ---- Reachability — follow-up run_test_sql on the SAME connection (DEC-006) ---


def test_materialised_temp_table_is_reachable_via_same_connection() -> None:
    """After ``materialise_sample``, a follow-up ``run_test_sql`` executes on the
    SAME connection object — so the materialised table is reachable by its
    qualified name (DEC-006). The fake records all executes on one connection.

    #226 (certified live): the ``databricks-sql-connector`` persists the session
    across queries, so the follow-up reaches the materialised table.
    """
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    conn.expect_execute(matching=_FAILURES_QUERY, returns=[(0,)], description=[("failures",)])
    adapter = _make_adapter(conn)

    temp_ref = adapter.materialise_sample(_TABLE, 100)
    test_sql = f"SELECT `id` FROM `main`.`sales`.`{temp_ref.name}` WHERE `id` IS NULL"
    adapter.run_test_sql(test_sql)

    assert len(conn.executed) == 3
    assert conn.executed[1].startswith("CREATE OR REPLACE TABLE")
    assert conn.executed[2].startswith("SELECT COUNT(*) AS failures")
    # The COUNT wrapper references the temp table, not the source.
    assert temp_ref.name in conn.executed[2]
    assert "orders" not in conn.executed[2]
    assert adapter._active_session is conn


# ---- #116 substitution — compiler references the TEMP table, NOT the source ---


def test_compiler_substitutes_temp_table_not_source() -> None:
    """The #116 materialised-sample-substitution gotcha, exercised on the test
    type that can ACTUALLY bypass it: a self-FROM ``custom_sql`` singular test
    (``SELECT ... FROM {{ this }} ...``). Fed the materialised temp
    :class:`TableRef` with :data:`DATABRICKS_DIALECT` at ``scope="full"`` (the
    shape the engine uses after materialising), the compiler must rewrite the
    resolved ``{{ this }}`` source name to the ``_sf_sample_<run_id>`` temp
    table. A bypass here would silently full-scan production under the
    materialised strategy.

    (The four built-in variants — ``not_null`` etc. — always ``FROM`` the passed
    ``table_ref`` and so can never bypass substitution; only the self-FROM
    ``custom_sql`` path can, which is why the gotcha is pinned here.)
    """
    from signalforge.draft.models import CandidateTestCustomSQL
    from signalforge.manifest.models import Column, Manifest, Model
    from signalforge.prune.compiler import _compile_test

    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    temp_ref = adapter.materialise_sample(_TABLE, 100)

    # A model whose ``resolve_this()`` == the SOURCE table (main.sales.orders),
    # so the custom_sql ``{{ this }}`` resolves to the source and the compiler
    # must rewrite it to the temp ``table_ref`` (because temp != source).
    model = Model(
        unique_id="model.shop.orders",
        name="orders",
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="main",
        schema="sales",  # type: ignore[call-arg]
        columns={"amount": Column(name="amount")},
        raw_code="select 1",
    )
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={"model.shop.orders": model},
    )

    compiled = _compile_test(
        CandidateTestCustomSQL(sql="SELECT * FROM {{ this }} WHERE amount < 0"),
        temp_ref,
        DATABRICKS_DIALECT,
        manifest,
        model=model,
        scope="full",
    )

    assert isinstance(compiled, str)
    # The self-FROM ``{{ this }}`` was rewritten to the materialised temp table ...
    assert temp_ref.name in compiled
    # ... and the source table's bare name never leaks (a bypass would leave the
    # resolved source ``orders`` here, full-scanning production).
    assert "orders" not in compiled


# ---- run_test_sql — COUNT(*) wrap + per-row to_json capture (DEC-007) ---------


def test_run_test_sql_validates_sql_first() -> None:
    """``validate_test_sql`` rejects a SQL with a ``;`` before any execute."""
    conn = FakeDatabricksConnection()
    adapter = _make_adapter(conn)

    with pytest.raises(QuerySyntaxError, match="single statement"):
        adapter.run_test_sql("SELECT 1; DROP TABLE t")

    conn.assert_all_expectations_met()


def test_run_test_sql_zero_failures_passes() -> None:
    """Zero failing rows → ``passed=True``, ``failure_count=0``,
    ``sample_failures=None``."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_FAILURES_QUERY, returns=[(0,)], description=[("failures",)])
    adapter = _make_adapter(conn)

    result = adapter.run_test_sql("SELECT `id` FROM `main`.`sales`.`orders` WHERE `id` IS NULL")

    assert result.passed is True
    assert result.failure_count == 0
    assert result.sample_failures is None
    assert result.row_schema is None


def test_run_test_sql_nonzero_failures_fails() -> None:
    """Non-zero failing rows → ``passed=False``, ``failure_count=N``."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_FAILURES_QUERY, returns=[(7,)], description=[("failures",)])
    adapter = _make_adapter(conn)

    result = adapter.run_test_sql("SELECT `id` FROM `main`.`sales`.`orders` WHERE `id` IS NULL")

    assert result.passed is False
    assert result.failure_count == 7


def test_run_test_sql_count_alias_resolves_case_insensitively() -> None:
    """A folded / preserved-case ``FAILURES`` alias still resolves — the count
    is read case-insensitively (Databricks folds unquoted aliases to lower, but
    a dict-cursor that preserved case must work too)."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_FAILURES_QUERY, returns=[(3,)], description=[("FAILURES",)])
    adapter = _make_adapter(conn)

    result = adapter.run_test_sql("SELECT `id` FROM `main`.`sales`.`orders` WHERE `id` IS NULL")

    assert result.failure_count == 3


def test_run_test_sql_capture_json_loads_each_row() -> None:
    """``capture_failures > 0`` issues a SECOND ``to_json(struct(*))`` LIMIT
    capture query; each row's JSON STRING is ``json.loads``-ed individually into
    a dict (NOT a single outer decode like Snowflake's VARIANT — DEC-007).

    #226 live-cert: the per-row ``to_json`` marshalling shape is shape-only here.
    """
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_FAILURES_QUERY, returns=[(2,)], description=[("failures",)])
    conn.expect_execute(
        matching=_CAPTURE_QUERY,
        returns=[('{"id": 1, "name": "alice"}',), ('{"id": 2, "name": "bob"}',)],
        description=[("failure_row",)],
    )
    adapter = _make_adapter(conn)

    result = adapter.run_test_sql(
        "SELECT `id` FROM `main`.`sales`.`orders` WHERE `id` IS NULL", capture_failures=5
    )

    assert result.passed is False
    assert result.failure_count == 2
    assert result.sample_failures == [
        {"id": 1, "name": "alice"},
        {"id": 2, "name": "bob"},
    ]
    # The capture query is a SECOND query (count first) carrying per-row to_json
    # + a LIMIT bounded at capture_failures.
    assert conn.executed[0].startswith("SELECT COUNT(*) AS failures")
    assert "to_json(struct(*))" in conn.executed[1]
    assert "LIMIT 5" in conn.executed[1]


def test_run_test_sql_capture_empty_yields_empty_list() -> None:
    """Zero captured rows → ``sample_failures == []`` (capture still ran)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_FAILURES_QUERY, returns=[(0,)], description=[("failures",)])
    conn.expect_execute(matching=_CAPTURE_QUERY, returns=[], description=[("failure_row",)])
    adapter = _make_adapter(conn)

    result = adapter.run_test_sql(
        "SELECT `id` FROM `main`.`sales`.`orders` WHERE `id` IS NULL", capture_failures=5
    )

    assert result.failure_count == 0
    assert result.sample_failures == []


def test_run_test_sql_capture_passes_through_dict_rows() -> None:
    """A connection that already vends parsed mapping rows (a dict-cursor) is
    handled by the ``dict`` passthrough in ``_parse_failure_row`` — no
    ``json.loads`` needed."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_FAILURES_QUERY, returns=[(1,)], description=[("failures",)])
    conn.expect_execute(
        matching=_CAPTURE_QUERY,
        returns=[{"failure_row": {"id": 9, "amount": -3}}],
    )
    adapter = _make_adapter(conn)

    result = adapter.run_test_sql(
        "SELECT `id` FROM `main`.`sales`.`orders` WHERE `id` IS NULL", capture_failures=5
    )

    assert result.sample_failures == [{"id": 9, "amount": -3}]


def test_run_test_sql_programming_error_maps_to_query_syntax_error() -> None:
    """A connector error from the COUNT(*) wrap maps to :class:`QuerySyntaxError`
    via ``map_databricks_exception`` (the ``_execute_to_dicts`` mapped branch)."""
    dbe = _dbe()
    err = dbe.ServerOperationError("[PARSE_SYNTAX_ERROR] bad syntax")  # type: ignore[attr-defined]
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_FAILURES_QUERY, returns=err)
    adapter = _make_adapter(conn)

    with pytest.raises(QuerySyntaxError):
        adapter.run_test_sql("SELECT `id` FROM `main`.`sales`.`orders` WHERE `id` IS NULL")


def test_run_test_sql_unmapped_error_passes_through_unchanged() -> None:
    """An exception ``map_databricks_exception`` does not map is re-raised
    unchanged from ``run_test_sql`` — the passthrough branch."""
    sentinel = RuntimeError("transient network blip")
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_FAILURES_QUERY, returns=sentinel)
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError) as exc_info:
        adapter.run_test_sql("SELECT `id` FROM `main`.`sales`.`orders` WHERE `id` IS NULL")
    assert exc_info.value is sentinel


# ---------------------------------------------------------------------------
# column_stats (#224 US-005, DEC-011) — aggregate-only profiling.
#
# Databricks implements column_stats AHEAD of Snowflake (which stubs it); the
# Snowflake-parity decision is tracked as GitHub issue #258.
# ---------------------------------------------------------------------------

_STATS_QUERY = r"COUNT\(DISTINCT"

# Full DB-API descriptor for the single aggregate row, in projected order.
_STATS_DESCRIPTION = [
    ("non_null_count",),
    ("distinct_count",),
    ("null_count",),
    ("min_value",),
    ("max_value",),
    ("data_type",),
]


def test_column_stats_returns_populated_columnstats() -> None:
    """A single aggregate round-trip shapes into a fully-populated
    :class:`ColumnStats`, mapped field-for-field from the result row
    (count=non-null, distinct, nulls, min, max, data_type)."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(
        matching=_STATS_QUERY,
        returns=[(900, 750, 100, 1, 9999, "int")],
        description=_STATS_DESCRIPTION,
    )
    adapter = _make_adapter(conn)

    stats = adapter.column_stats(_TABLE, "amount")

    assert isinstance(stats, ColumnStats)
    assert stats.count == 900
    assert stats.distinct == 750
    assert stats.nulls == 100
    assert stats.min == 1
    assert stats.max == 9999
    assert stats.data_type == "int"
    conn.assert_all_expectations_met()


def test_column_stats_carries_through_string_and_none_minmax() -> None:
    """``min``/``max`` pass through whatever the connector returns (here STRING
    bounds); a NULL min/max from an all-null column surfaces as ``None``."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(
        matching=_STATS_QUERY,
        returns=[(0, 0, 500, None, None, "string")],
        description=_STATS_DESCRIPTION,
    )
    adapter = _make_adapter(conn)

    stats = adapter.column_stats(_TABLE, "region")

    assert stats.count == 0
    assert stats.distinct == 0
    assert stats.nulls == 500
    assert stats.min is None
    assert stats.max is None
    assert stats.data_type == "string"


def test_column_stats_query_shape_and_folding() -> None:
    """The emitted aggregate SQL reuses ``_quote`` (three-part backtick table)
    and fold-then-quotes the column, computing the full metric set."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(
        matching=_STATS_QUERY,
        returns=[(1, 1, 0, 5, 5, "int")],
        description=_STATS_DESCRIPTION,
    )
    adapter = _make_adapter(conn)

    adapter.column_stats(_TABLE, "amount")

    sql = conn.executed[0]
    assert sql.startswith("SELECT COUNT(`amount`) AS non_null_count")
    assert "COUNT(DISTINCT `amount`) AS distinct_count" in sql
    assert "COUNT_IF(`amount` IS NULL) AS null_count" in sql
    assert "MIN(`amount`) AS min_value" in sql
    assert "MAX(`amount`) AS max_value" in sql
    assert "MAX(typeof(`amount`)) AS data_type" in sql
    assert sql.endswith("FROM `main`.`sales`.`orders`")


def test_column_stats_folds_mixed_case_column_to_lower() -> None:
    """A conventionally-cased manifest column is fold-then-quoted to lowercase
    backtick (``identifier_case='lower'``), reusing ``_quote_identifier``."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(
        matching=_STATS_QUERY,
        returns=[(1, 1, 0, 5, 5, "int")],
        description=_STATS_DESCRIPTION,
    )
    adapter = _make_adapter(conn)

    adapter.column_stats(_TABLE, "OrderAmount")

    sql = conn.executed[0]
    assert "`orderamount`" in sql
    assert "OrderAmount" not in sql


def test_column_stats_resolves_aliases_case_insensitively() -> None:
    """A connection that preserved the alias case (UPPER) still maps — the
    adapter lowercases the result keys before reading them."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(
        matching=_STATS_QUERY,
        returns=[(7, 4, 3, 0, 10, "bigint")],
        description=[
            ("NON_NULL_COUNT",),
            ("DISTINCT_COUNT",),
            ("NULL_COUNT",),
            ("MIN_VALUE",),
            ("MAX_VALUE",),
            ("DATA_TYPE",),
        ],
    )
    adapter = _make_adapter(conn)

    stats = adapter.column_stats(_TABLE, "qty")

    assert stats.count == 7
    assert stats.distinct == 4
    assert stats.nulls == 3
    assert stats.data_type == "bigint"


def test_column_stats_none_data_type_coerces_to_empty_string() -> None:
    """An empty table yields ``MAX(typeof(...)) = NULL`` → ``data_type`` is
    coerced to ``""`` (matching BigQuery's "type unknown → empty string")."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(
        matching=_STATS_QUERY,
        returns=[(0, 0, 0, None, None, None)],
        description=_STATS_DESCRIPTION,
    )
    adapter = _make_adapter(conn)

    stats = adapter.column_stats(_TABLE, "amount")

    assert stats.data_type == ""


def test_column_stats_validates_column_identifier() -> None:
    """A malformed column name is rejected by ``validate_identifier`` BEFORE any
    query is issued (DEC-013) — no expectation is consumed."""
    conn = FakeDatabricksConnection()
    adapter = _make_adapter(conn)

    with pytest.raises(InvalidIdentifierError):
        adapter.column_stats(_TABLE, "amount; DROP TABLE x")


def test_column_stats_two_part_table_quoting() -> None:
    """``project=None`` yields a two-part ``schema``.``table`` FROM clause."""
    two_part = TableRef(project=None, dataset="sales", name="orders")
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(
        matching=_STATS_QUERY,
        returns=[(1, 1, 0, 5, 5, "int")],
        description=_STATS_DESCRIPTION,
    )
    adapter = _make_adapter(conn)

    adapter.column_stats(two_part, "amount")

    assert conn.executed[0].endswith("FROM `sales`.`orders`")


def test_column_stats_programming_error_maps_to_query_syntax_error() -> None:
    """A connector error from the aggregate maps via ``map_databricks_exception``
    (the ``_execute_to_dicts`` mapped branch) — here to :class:`QuerySyntaxError`."""
    dbe = _dbe()
    err = dbe.ServerOperationError("[PARSE_SYNTAX_ERROR] bad syntax")  # type: ignore[attr-defined]
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_STATS_QUERY, returns=err)
    adapter = _make_adapter(conn)

    with pytest.raises(QuerySyntaxError):
        adapter.column_stats(_TABLE, "amount")


def test_column_stats_column_not_found_maps_with_context() -> None:
    """An unresolved-column connector error maps to :class:`ColumnNotFoundError`
    (the table context flows through ``_execute_to_dicts``)."""
    dbe = _dbe()
    err = dbe.ServerOperationError(  # type: ignore[attr-defined]
        "[UNRESOLVED_COLUMN.WITH_SUGGESTION] A column or function `nope` cannot be resolved"
    )
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_STATS_QUERY, returns=err)
    adapter = _make_adapter(conn)

    with pytest.raises(ColumnNotFoundError):
        adapter.column_stats(_TABLE, "amount")
