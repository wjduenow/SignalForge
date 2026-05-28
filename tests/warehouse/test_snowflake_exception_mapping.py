"""US-001 (#124) — full ``map_snowflake_exception`` taxonomy.

Offline unit tests: ``snowflake-connector-python`` is a dev dependency, so the
real ``snowflake.connector.errors`` classes construct without a live
warehouse. We build genuine ``sfe.*`` instances and assert each mapping arm.

Mirrors ``map_bq_exception``'s coverage:

* object-not-exist (errno 002003)  → ``TableNotFoundError``
* invalid-identifier (errno 000904) → ``ColumnNotFoundError``
* residual ``ProgrammingError``     → ``QuerySyntaxError``
* ``ForbiddenError``                → ``WarehouseAuthError``
* auth-flavoured ``DatabaseError`` / ``OperationalError`` → ``WarehouseAuthError``
* transient / unmapped              → passthrough (returned unchanged)
"""

from __future__ import annotations

from typing import Any

from signalforge.warehouse.adapters._snowflake_client import (
    _extract_invalid_identifier,
    map_snowflake_exception,
)
from signalforge.warehouse.errors import (
    ColumnNotFoundError,
    QuerySyntaxError,
    TableNotFoundError,
    WarehouseAuthError,
)

# NOTE: ``snowflake.connector.errors`` is imported INSIDE each test, never at
# module top level. A sibling test
# (``test_snowflake_client.test_importing_shim_does_not_import_snowflake_connector``)
# deletes ``snowflake.connector`` + submodules from ``sys.modules`` and does
# not restore them, so a module-level ``errors`` object captured at collection
# time would go stale — its class objects would differ from the ones
# ``map_snowflake_exception`` re-imports lazily, breaking ``isinstance``. The
# in-function import guarantees the test's ``sfe`` matches the mapper's. (The
# adjacent ``test_sample_id.py`` mapping tests follow the same pattern.)


def _sfe() -> Any:
    """Lazily import ``snowflake.connector.errors`` so the class objects match
    whatever ``map_snowflake_exception`` re-imports at call time."""
    from snowflake.connector import errors as sfe

    return sfe


def test_object_does_not_exist_maps_to_table_not_found() -> None:
    """errno 002003 ('object does not exist') → ``TableNotFoundError`` with the
    context table on ``.table``."""
    sfe = _sfe()
    exc = sfe.ProgrammingError(msg="Object does not exist", errno=2003)
    mapped = map_snowflake_exception(exc, context={"table": "DB.SCH.CUSTOMERS"})
    assert isinstance(mapped, TableNotFoundError)
    assert mapped.table == "DB.SCH.CUSTOMERS"


def test_object_does_not_exist_without_context_uses_unknown_placeholder() -> None:
    """No ``context`` → ``.table`` falls back to ``"<unknown>"`` (mirrors BQ)."""
    sfe = _sfe()
    exc = sfe.ProgrammingError(msg="Object does not exist", errno=2003)
    mapped = map_snowflake_exception(exc)
    assert isinstance(mapped, TableNotFoundError)
    assert mapped.table == "<unknown>"


def test_object_does_not_exist_keyed_on_message_marker() -> None:
    """Even with a missing/odd errno, the ``"does not exist"`` marker routes to
    ``TableNotFoundError`` (resilience to errno-attribute shifts)."""
    sfe = _sfe()
    exc = sfe.ProgrammingError(
        msg="SQL compilation error: Object 'FOO' does not exist or not authorized"
    )
    mapped = map_snowflake_exception(exc, context={"table": "T"})
    # The marker check still fires regardless of errno.
    assert isinstance(mapped, (TableNotFoundError, WarehouseAuthError))
    # Specifically: "does not exist" takes precedence inside the ProgrammingError arm.
    assert isinstance(mapped, TableNotFoundError)
    assert mapped.table == "T"


def test_invalid_identifier_maps_to_column_not_found() -> None:
    """errno 000904 ('invalid identifier') → ``ColumnNotFoundError`` with the
    extracted column on ``.column`` and the context table on ``.table``."""
    sfe = _sfe()
    exc = sfe.ProgrammingError(msg="invalid identifier 'BAD_COL'", errno=904)
    mapped = map_snowflake_exception(exc, context={"table": "DB.SCH.ORDERS"})
    assert isinstance(mapped, ColumnNotFoundError)
    assert mapped.table == "DB.SCH.ORDERS"
    assert mapped.column == "BAD_COL"


def test_invalid_identifier_keyed_on_message_marker() -> None:
    """Message marker ``"invalid identifier"`` routes to ``ColumnNotFoundError``
    even without an errno."""
    sfe = _sfe()
    exc = sfe.ProgrammingError(msg="SQL compilation error: invalid identifier 'WIDGET'")
    mapped = map_snowflake_exception(exc)
    assert isinstance(mapped, ColumnNotFoundError)
    assert mapped.table == "<unknown>"
    assert mapped.column == "WIDGET"


def test_residual_programming_error_maps_to_query_syntax() -> None:
    """A ``ProgrammingError`` that is neither object-not-exist nor
    invalid-identifier → ``QuerySyntaxError``."""
    sfe = _sfe()
    exc = sfe.ProgrammingError(msg="SQL compilation error: syntax error line 1", errno=1003)
    mapped = map_snowflake_exception(exc, context={"table": "T"})
    assert isinstance(mapped, QuerySyntaxError)
    assert "syntax error" in mapped.detail


def test_table_column_split_runs_before_query_syntax_fallthrough() -> None:
    """The Table/Column arms MUST precede the broad ``QuerySyntaxError``
    fallthrough — an object-not-exist error never lands on QuerySyntaxError."""
    sfe = _sfe()
    exc = sfe.ProgrammingError(msg="Object does not exist", errno=2003)
    mapped = map_snowflake_exception(exc)
    assert not isinstance(mapped, QuerySyntaxError)
    assert isinstance(mapped, TableNotFoundError)


def test_forbidden_error_maps_to_auth() -> None:
    """``ForbiddenError`` (HTTP 403) → ``WarehouseAuthError``."""
    sfe = _sfe()
    exc = sfe.ForbiddenError(msg="403 Forbidden")
    mapped = map_snowflake_exception(exc)
    assert isinstance(mapped, WarehouseAuthError)


def test_auth_flavoured_database_error_maps_to_auth() -> None:
    """A ``DatabaseError`` carrying an auth marker → ``WarehouseAuthError``."""
    sfe = _sfe()
    exc = sfe.DatabaseError(msg="Incorrect username or password was specified.", errno=250001)
    mapped = map_snowflake_exception(exc)
    assert isinstance(mapped, WarehouseAuthError)


def test_auth_flavoured_operational_error_maps_to_auth() -> None:
    """An ``OperationalError`` carrying an auth marker → ``WarehouseAuthError``."""
    sfe = _sfe()
    exc = sfe.OperationalError(msg="authentication token expired")
    mapped = map_snowflake_exception(exc)
    assert isinstance(mapped, WarehouseAuthError)


def test_transient_operational_error_passes_through_unchanged() -> None:
    """A non-auth ``OperationalError`` (network blip, timeout) is returned
    unchanged so the caller re-raises the original."""
    sfe = _sfe()
    exc = sfe.OperationalError(msg="Connection timed out while reading from socket")
    mapped = map_snowflake_exception(exc)
    assert mapped is exc


def test_unmapped_exception_passes_through_unchanged() -> None:
    """A non-connector exception is returned unchanged (identity)."""
    exc = ValueError("not a snowflake error")
    mapped = map_snowflake_exception(exc)
    assert mapped is exc


def test_plain_database_error_without_auth_marker_passes_through() -> None:
    """A bare ``DatabaseError`` without an auth marker (and not a
    ``ProgrammingError``) falls through to passthrough."""
    sfe = _sfe()
    exc = sfe.DatabaseError(msg="some generic database failure", errno=999999)
    mapped = map_snowflake_exception(exc)
    assert mapped is exc


def test_extract_invalid_identifier_falls_back_to_full_message() -> None:
    """When no identifier pattern matches, the helper returns the full message
    so ``ColumnNotFoundError.column`` stays non-empty."""
    msg = "completely unparseable wording with no quoted identifier"
    assert _extract_invalid_identifier(msg) == msg


def test_extract_invalid_identifier_pulls_bare_token() -> None:
    """The helper extracts the bare identifier from Snowflake's quoted form."""
    assert _extract_invalid_identifier("000904: invalid identifier 'MY_COL'") == "MY_COL"
