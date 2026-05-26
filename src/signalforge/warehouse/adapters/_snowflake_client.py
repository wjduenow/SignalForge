"""Thin wrapper around ``snowflake-connector-python`` to contain pyright noise.

US-002 (#119) confines every ``# pyright: ignore[...]`` / ``# type:
ignore[...]`` comment that the ``snowflake-connector-python`` SDK provokes to
this module — the one-shim-per-vendor SDK seam, mirroring
:mod:`signalforge.warehouse.adapters._client` (the BigQuery shim). EVERY
snowflake-connector-python type-ignore in the whole warehouse subpackage must
live ONLY in this file (DEC-005). The shim exposes duck-typed protocols matching
the narrow surface the (future) :class:`SnowflakeAdapter` will consume, so the
adapter calls the same method signatures regardless of how its connection was
constructed.

The protocol split mirrors the real DB-API 2.0 shape ``snowflake.connector``
exposes — query execution lives on the *cursor*, not the connection
(``snowflake.connector.SnowflakeConnection`` has ``cursor()`` / ``close()`` but
no ``execute()`` / ``fetchall()``): :class:`_SnowflakeCursorProtocol` carries
``execute(...)`` / ``fetchall()``, and :class:`_SnowflakeClientProtocol` (the
connection) carries ``cursor()`` / ``close()``. Keeping these honest now means
the #118 implementation's ``conn.cursor().execute(...)`` path type-checks
against a protocol that actually describes what ``connect()`` returns.

:func:`map_snowflake_exception` (DEC-009 of #122) translates a connector
exception into a typed :class:`signalforge.warehouse.errors.WarehouseError`
subclass; it lazy-imports ``snowflake.connector.errors`` inside its body so the
one-shim-per-vendor rule holds, mirroring ``_client.py``'s ``map_bq_exception``.

The ``import snowflake.connector`` is lazy — confined to the bodies of
:func:`make_real_client` and :func:`map_snowflake_exception` (NOT at module
top) — so importing this shim does not
require the connector to be installed, and the rest of the warehouse subpackage
doesn't pay the import cost (DEC-006 — ``snowflake-connector-python`` ships only
under the ``[snowflake]`` optional-dependency extra, not the base install).

Observability discipline: no logger calls in this shim. Logging lives in the
adapter where the stage label is known (mirrors ``_client.py`` DEC-027).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class _SnowflakeCursorProtocol(Protocol):
    """Duck-typed surface of a Snowflake cursor.

    Query execution lives here, not on the connection — the adapter drives a
    query via ``cursor.execute(...)`` then reads rows via ``cursor.fetchall()``
    and releases the cursor via ``close()``. Mirrors the DB-API 2.0 cursor
    shape ``snowflake.connector``'s cursor exposes.
    """

    # DB-API 2.0 ``Cursor.description``: a sequence of 7-tuple column
    # descriptors (or ``None`` before any query). Element ``[0]`` of each
    # descriptor is the column name — :meth:`SnowflakeAdapter.sample_rows`
    # (#122) reads it to build ``dict`` rows from tuple ``fetchall()``
    # results without depending on a ``DictCursor``. Typed loosely as
    # ``Any`` (matching how the shim types ``execute`` / ``fetchall``)
    # because the real descriptor tuple shape is connector-specific and
    # the adapter only ever indexes ``[0]``.
    @property
    def description(self) -> Any: """
Cursor description following the DB-API 7-tuple format or None when unavailable.

Returns:
    A sequence of 7-tuples describing result columns (typically where the first element is the column name), or `None` if no description is available (e.g., before a query has been executed).
"""
...

    def execute(self, command: str, *args: Any, **kwargs: Any) -> Any: """
Execute an SQL statement on the cursor.

Parameters:
    command (str): The SQL statement to execute.
    *args (Any): Positional parameters for parameter substitution in the statement.
    **kwargs (Any): Driver-specific execution options (e.g., parameter styles or execution flags).

Returns:
    Any: Driver-specific execution result (commonly the cursor itself or a result proxy).
"""
...

    def fetchall(self) -> Any: """
Fetch all remaining rows produced by the executed query.

Returns:
    rows: The result set as a sequence of rows (commonly a list/tuple of row tuples or row-like objects); an empty sequence if no rows remain.
"""
...

    def close(self) -> None: ...


@runtime_checkable
class _SnowflakeClientProtocol(Protocol):
    """Duck-typed surface common to a real ``SnowflakeConnection`` and a fake.

    Both production (``snowflake.connector.SnowflakeConnection``) and test
    fakes satisfy this protocol, so the adapter calls the same methods
    regardless of which one was injected. The protocol is intentionally
    narrow — only the surface the adapter actually consumes.

    ``cursor()`` returns a :class:`_SnowflakeCursorProtocol`; the adapter drives
    queries via that cursor's ``execute(...)`` / ``fetchall()`` and tears the
    connection down via ``close()``. This matches the real
    ``snowflake.connector.SnowflakeConnection``, which carries ``cursor()`` /
    ``close()`` but NOT ``execute()`` / ``fetchall()`` directly.
    """

    def cursor(self) -> _SnowflakeCursorProtocol: ...

    def close(self) -> None: ...


def make_real_client(
    *,
    account: str,
    user: str,
    password: str,
    role: str | None = None,
    warehouse: str | None = None,
    database: str | None = None,
    schema: str | None = None,
) -> _SnowflakeClientProtocol:  # pragma: no cover - requires the SDK + live creds
    """
    Create a Snowflake connection using the provided credentials.
    
    The Snowflake SDK is imported lazily inside this function so the module can be imported without the optional SDK installed.
    
    Returns:
        A connection object implementing _SnowflakeClientProtocol.
    """
    import snowflake.connector  # type: ignore[import-not-found]

    return snowflake.connector.connect(  # type: ignore[no-any-return]
        account=account,
        user=user,
        password=password,
        role=role,
        warehouse=warehouse,
        database=database,
        schema=schema,
    )


def map_snowflake_exception(exc: Exception, *, context: dict[str, Any] | None = None) -> Exception:
    """
    Map a snowflake.connector exception to a typed warehouse error when a known mapping applies.
    
    Attempts a lazy import of snowflake.connector.errors; if the connector is unavailable or no mapping matches, returns the original exception unchanged. When provided, the optional context may include a "table" key whose string value is appended to QuerySyntaxError details.
    
    Parameters:
        exc (Exception): The original exception raised by the Snowflake connector.
        context (dict[str, Any] | None): Optional adapter context (e.g., {"table": ...}) used to enrich mapped errors.
    
    Returns:
        Exception: A newly constructed warehouse error (e.g., QuerySyntaxError or WarehouseAuthError) when a mapping applies, otherwise the original `exc`.
    """
    try:
        from snowflake.connector import errors as sfe  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - snowflake-connector ships under [snowflake]
        return exc

    from signalforge.warehouse.errors import QuerySyntaxError, WarehouseAuthError

    table_id = str(context["table"]) if context is not None and "table" in context else None

    # ``ProgrammingError`` is a ``DatabaseError`` subclass; check it BEFORE the
    # broader auth buckets so a SQL compilation error never mis-maps to auth.
    if isinstance(exc, sfe.ProgrammingError):
        detail = str(exc)
        if table_id is not None:
            detail = f"{detail} (table={table_id})"
        return QuerySyntaxError(detail=detail)

    # Bad credentials / forbidden access. Snowflake's connector raises
    # ``ForbiddenError`` for HTTP 403 and ``DatabaseError`` /
    # ``OperationalError`` (errno 250001 etc.) for a failed login; key on the
    # type plus an auth-flavoured message so a generic operational blip
    # (network, timeout) falls through to the unchanged-passthrough arm.
    if isinstance(exc, sfe.ForbiddenError):
        return WarehouseAuthError(message=str(exc))
    if isinstance(exc, (sfe.DatabaseError, sfe.OperationalError)):
        msg_lower = str(exc).lower()
        auth_markers = (
            "incorrect username or password",
            "authentication",
            "auth",
            "credential",
            "access denied",
            "not authorized",
        )
        if any(marker in msg_lower for marker in auth_markers):
            return WarehouseAuthError(message=str(exc))

    return exc


__all__ = [
    "_SnowflakeClientProtocol",
    "_SnowflakeCursorProtocol",
    "make_real_client",
    "map_snowflake_exception",
]
