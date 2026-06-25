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
    TableNotFoundError,
    WarehouseAuthError,
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
