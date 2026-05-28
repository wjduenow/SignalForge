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

import re
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
    def description(self) -> Any: ...

    def execute(self, command: str, *args: Any, **kwargs: Any) -> Any: ...

    def fetchall(self) -> Any: ...

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
    """Construct a real ``snowflake.connector`` connection.

    The ``snowflake.connector`` import is lazy (inside the body) so this
    module imports cleanly without the connector installed —
    ``snowflake-connector-python`` ships only under the ``[snowflake]``
    optional-dependency extra. The single ``# type: ignore[import-not-found]``
    for the SDK import is confined here per DEC-005.
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
    """Translate a ``snowflake.connector`` exception into a typed warehouse error.

    Mirrors :func:`signalforge.warehouse.adapters._client.map_bq_exception`'s
    shape and return convention: returns the *new* exception so the caller can
    ``raise mapped from exc``; returns ``exc`` unchanged when no specific
    mapping fits — the caller should re-raise the original in that case rather
    than swallow it.

    Full taxonomy (issue #124, mirroring ``map_bq_exception``):

    * a connector ``ForbiddenError`` / auth-flavoured ``DatabaseError`` /
      ``OperationalError`` (bad credentials, incorrect username/password)
      → :class:`WarehouseAuthError`;
    * a ``ProgrammingError`` whose ``errno`` is ``2003`` ("object does not
      exist", message marker ``"does not exist"``) → :class:`TableNotFoundError`
      (``table`` from ``context`` or ``"<unknown>"``);
    * a ``ProgrammingError`` whose ``errno`` is ``904`` ("invalid identifier",
      message marker ``"invalid identifier"``) → :class:`ColumnNotFoundError`
      (``column`` extracted from the message);
    * any residual ``ProgrammingError`` (SQL compilation / syntax)
      → :class:`QuerySyntaxError`;
    * anything else → returned unchanged.

    The Table/Column split runs BEFORE the broad ``ProgrammingError`` →
    :class:`QuerySyntaxError` fallthrough. No new ``WarehouseError`` subclass is
    introduced (that would force exit-code-table + AST-scan changes);
    ``BytesBilledExceededError`` is deliberately omitted because Snowflake has
    no bytes-billed cap.

    The ``snowflake.connector.errors`` import is **lazy** — confined to this
    function body — so the one-shim-per-vendor rule holds (every
    snowflake-connector type/pyright-ignore lives only in this file, DEC-005)
    and importing this shim never requires the connector to be installed. If
    the connector is absent, the exception is returned unchanged.

    The optional ``context`` kwarg carries adapter-side state the raw connector
    exception doesn't expose (e.g. ``{"table": ...}``), mirroring
    ``map_bq_exception``; it supplies the ``table`` identifier for the
    Table/Column arms so the typed ``.table`` field stays a stable identifier
    rather than a truncated connector message.
    """
    try:
        from snowflake.connector import errors as sfe  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - snowflake-connector ships under [snowflake]
        return exc

    from signalforge.warehouse.errors import (
        ColumnNotFoundError,
        QuerySyntaxError,
        TableNotFoundError,
        WarehouseAuthError,
    )

    table_id = str(context["table"]) if context is not None and "table" in context else "<unknown>"

    # ``ProgrammingError`` is a ``DatabaseError`` subclass; check it BEFORE the
    # broader auth buckets so a SQL compilation error never mis-maps to auth.
    if isinstance(exc, sfe.ProgrammingError):
        errno = getattr(exc, "errno", None)
        msg_lower = str(exc).lower()
        # Object does not exist — Snowflake errno 002003 (key on errno where
        # reliable; fall back to the message marker for resilience to wording
        # / errno-attribute shifts).
        if errno == 2003 or "does not exist" in msg_lower:
            return TableNotFoundError(table=table_id)
        # Invalid identifier (unknown column) — Snowflake errno 000904.
        if errno == 904 or "invalid identifier" in msg_lower:
            column = _extract_invalid_identifier(str(exc))
            return ColumnNotFoundError(table=table_id, column=column)
        # Residual ``ProgrammingError`` — "your SQL is malformed".
        return QuerySyntaxError(detail=str(exc))

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


_INVALID_IDENTIFIER_RE = re.compile(
    r"invalid identifier\s+'?([A-Za-z_][A-Za-z0-9_.$]*)'?",
    re.IGNORECASE,
)


def _extract_invalid_identifier(message: str) -> str:
    """Best-effort pull of a column identifier out of Snowflake's
    ``000904: ... invalid identifier 'FOO'`` message.

    Returns the bare identifier if found; otherwise the full message.
    Falling back to the message keeps :class:`ColumnNotFoundError`'s
    ``column`` field non-empty even when Snowflake's wording shifts (mirrors
    ``_client._extract_unrecognized_column``).
    """
    m = _INVALID_IDENTIFIER_RE.search(message)
    if m:
        return m.group(1)
    return message


__all__ = [
    "_SnowflakeClientProtocol",
    "_SnowflakeCursorProtocol",
    "make_real_client",
    "map_snowflake_exception",
]
