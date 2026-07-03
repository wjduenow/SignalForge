"""Thin wrapper around ``databricks-sql-connector`` to contain pyright noise.

Issue #221 (epic #219) confines every ``# pyright: ignore[...]`` / ``# type:
ignore[...]`` comment that the ``databricks-sql-connector`` SDK provokes to this
module — the one-shim-per-vendor SDK seam, mirroring
:mod:`signalforge.warehouse.adapters._client` (BigQuery) and
:mod:`signalforge.warehouse.adapters._snowflake_client` (Snowflake). EVERY
databricks-sql-connector type-ignore in the whole warehouse subpackage must live
ONLY in this file; the confinement scan
(``tests/warehouse/test_databricks_client_confinement.py``) enforces it.

The protocol split mirrors the real DB-API 2.0 shape ``databricks.sql`` exposes —
query execution lives on the *cursor*, not the connection (a
``databricks.sql.client.Connection`` has ``cursor()`` / ``close()`` but no
``execute()`` / ``fetchall()``): :class:`_DatabricksCursorProtocol` carries
``execute(...)`` / ``fetchall()`` / ``description`` / ``close()``, and
:class:`_DatabricksClientProtocol` (the connection) carries ``cursor()`` /
``close()``. Keeping these honest now means the #224 sampling implementation's
``conn.cursor().execute(...)`` path type-checks against a protocol that actually
describes what ``databricks.sql.connect()`` returns.

:func:`map_databricks_exception` (#224 US-002) translates a connector exception
into a typed :class:`signalforge.warehouse.errors.WarehouseError` subclass —
auth → :class:`WarehouseAuthError`; table-not-found → :class:`TableNotFoundError`;
unresolved-column → :class:`ColumnNotFoundError`; residual Spark/SQL error →
:class:`QuerySyntaxError`; everything else unchanged — mirroring
``map_snowflake_exception``. The ``databricks.sql.exc`` import is lazy inside the
function body so the one-shim-per-vendor rule holds; an auth-flavoured message
still maps without the connector installed.

The ``from databricks import sql`` import is lazy — confined to the body of
:func:`make_real_client` — so importing this shim does not require the connector
to be installed, and the rest of the warehouse subpackage doesn't pay the import
cost (``databricks-sql-connector`` ships only under the ``[databricks]`` optional
extra, not the base install).

Observability discipline: no logger calls in this shim. Logging lives in the
adapter where the stage label is known (mirrors ``_client.py`` / ``_snowflake_client.py``).
"""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class _DatabricksCursorProtocol(Protocol):
    """Duck-typed surface of a Databricks SQL cursor.

    Query execution lives here, not on the connection — the adapter drives a
    query via ``cursor.execute(...)`` then reads rows via ``cursor.fetchall()``
    and releases the cursor via ``close()``. Mirrors the DB-API 2.0 cursor
    shape ``databricks.sql``'s cursor exposes.
    """

    # DB-API 2.0 ``Cursor.description``: a sequence of 7-tuple column
    # descriptors (or ``None`` before any query). Element ``[0]`` of each
    # descriptor is the column name — the #224 sampling path reads it to build
    # ``dict`` rows from tuple ``fetchall()`` results. Typed loosely as ``Any``
    # (matching how the shim types ``execute`` / ``fetchall``) because the real
    # descriptor tuple shape is connector-specific and the adapter only ever
    # indexes ``[0]``.
    @property
    def description(self) -> Any: ...

    def execute(self, operation: str, *args: Any, **kwargs: Any) -> Any: ...

    def fetchall(self) -> Any: ...

    def close(self) -> None: ...


@runtime_checkable
class _DatabricksClientProtocol(Protocol):
    """Duck-typed surface common to a real Databricks ``Connection`` and a fake.

    Both production (``databricks.sql.client.Connection``) and test fakes
    satisfy this protocol, so the adapter calls the same methods regardless of
    which one was injected. The protocol is intentionally narrow — only the
    surface the adapter actually consumes.

    ``cursor()`` returns a :class:`_DatabricksCursorProtocol`; the adapter drives
    queries via that cursor's ``execute(...)`` / ``fetchall()`` and tears the
    connection down via ``close()``. This matches the real Databricks
    ``Connection``, which carries ``cursor()`` / ``close()`` but NOT
    ``execute()`` / ``fetchall()`` directly.
    """

    def cursor(self) -> _DatabricksCursorProtocol: ...

    def close(self) -> None: ...


def make_real_client(
    *,
    host: str,
    http_path: str,
    token: str,
) -> _DatabricksClientProtocol:  # pragma: no cover - requires the SDK + live creds
    """Construct a real ``databricks.sql`` connection (PAT auth).

    The ``from databricks import sql`` import is lazy (inside the body) so this
    module imports cleanly without the connector installed —
    ``databricks-sql-connector`` ships only under the ``[databricks]`` optional
    extra. The single ``# type: ignore[import-not-found]`` for the SDK import is
    confined here.

    v0.x scope is **PAT (personal access token) auth only** — OAuth M2M
    (``client_id`` / ``client_secret``) is a tracked epic open-decision and lands
    in a later child; the adapter captures those params for forward-compat but
    this builder consumes only the token.
    """
    from databricks import sql  # type: ignore[import-not-found]

    return sql.connect(  # type: ignore[no-any-return]
        server_hostname=host,
        http_path=http_path,
        access_token=token,
    )


_AUTH_MARKERS: tuple[str, ...] = (
    "authentication",
    "credential",
    "invalid access token",
    "access denied",
    "not authorized",
    "unauthorized",
    "permission denied",
)

# Spark / Databricks SQL surfaces object-not-found via the ``TABLE_OR_VIEW_NOT_FOUND``
# error class (and the legacy "Table or view not found" message). Markers are
# matched case-insensitively against the lower-cased message.
_TABLE_NOT_FOUND_MARKERS: tuple[str, ...] = (
    "table_or_view_not_found",
    "table or view not found",
)

# Unresolved-column surfaces via the ``UNRESOLVED_COLUMN`` error class / the
# "cannot be resolved" message ("A column ... with name `x` cannot be resolved").
_COLUMN_NOT_FOUND_MARKERS: tuple[str, ...] = (
    "unresolved_column",
    "cannot be resolved",
)

_UNRESOLVED_COLUMN_RE = re.compile(
    r"(?:with name|cannot resolve)\s+[`'\"]?([A-Za-z_][A-Za-z0-9_.$]*)[`'\"]?",
    re.IGNORECASE,
)


def _extract_unresolved_column(message: str) -> str:
    """Best-effort pull of a column identifier out of Databricks' unresolved-column
    message (``... with name `bad_col` cannot be resolved`` / ``cannot resolve
    'bad_col'``).

    Returns the bare identifier if found; otherwise the full message. Falling
    back to the message keeps :class:`ColumnNotFoundError`'s ``column`` field
    non-empty even when Spark's wording shifts (mirrors the Snowflake shim's
    ``_extract_invalid_identifier``).
    """
    m = _UNRESOLVED_COLUMN_RE.search(message)
    if m:
        return m.group(1)
    return message


def map_databricks_exception(exc: Exception, *, context: dict[str, Any] | None = None) -> Exception:
    """Translate a ``databricks.sql`` exception into a typed warehouse error
    (#224 US-002, mirroring ``map_snowflake_exception``).

    Mirrors :func:`signalforge.warehouse.adapters._snowflake_client.map_snowflake_exception`'s
    shape and return convention: returns the *new* exception so the caller can
    ``raise mapped from exc``; returns ``exc`` unchanged when no specific mapping
    fits — the caller should re-raise the original in that case rather than
    swallow it.

    Taxonomy:

    * A Spark SQL execution / DB-API programming error
      (``ServerOperationError`` / ``ProgrammingError``) carrying a
      table-not-found marker (:data:`_TABLE_NOT_FOUND_MARKERS`) →
      :class:`TableNotFoundError` (``table`` from ``context`` or ``"<unknown>"``).
    * The same scope carrying an unresolved-column marker
      (:data:`_COLUMN_NOT_FOUND_MARKERS`) → :class:`ColumnNotFoundError`
      (``column`` extracted from the message).
    * The same scope carrying an auth marker → :class:`WarehouseAuthError`.
    * Any residual ``ServerOperationError`` / ``ProgrammingError`` (SQL
      compilation / syntax) → :class:`QuerySyntaxError`.
    * Any OTHER exception carrying an auth marker (connect-time
      ``RequestError`` / ``OperationalError``, or — with the connector absent —
      a plain exception) → :class:`WarehouseAuthError`.
    * Anything else → returned unchanged (so a transient network blip falls
      through to the caller).

    The Table/Column split runs BEFORE the broad ``QuerySyntaxError``
    fallthrough, and is scoped to the SQL-error connector types so a transient
    ``OperationalError`` is NOT mis-mapped to ``QuerySyntaxError``. No new
    ``WarehouseError`` subclass is introduced (that would force exit-code-table
    + AST-scan changes), and ``BytesBilledExceededError`` is deliberately
    omitted because Databricks has no bytes-billed cap.

    The ``databricks.sql.exc`` import is **lazy** — confined to this function
    body — so the one-shim-per-vendor rule holds (every databricks-sql-connector
    type-ignore lives only in this file) and importing this shim never requires
    the connector to be installed. If the connector is absent, only the
    message-marker auth detection runs (everything else passes through).

    The optional ``context`` kwarg carries adapter-side state the raw connector
    exception doesn't expose (e.g. ``{"table": ...}``), mirroring the Snowflake
    mapper; it supplies the ``table`` identifier for the Table/Column arms.
    """
    msg_lower = str(exc).lower()

    try:
        from databricks.sql import exc as dbe
    except ImportError:  # pragma: no cover - connector ships under [databricks]
        # Connector absent: message-marker auth detection only; everything else
        # passes through unchanged.
        if any(marker in msg_lower for marker in _AUTH_MARKERS):
            from signalforge.warehouse.errors import WarehouseAuthError

            return WarehouseAuthError(message=str(exc))
        return exc

    from signalforge.warehouse.errors import (
        ColumnNotFoundError,
        QuerySyntaxError,
        TableNotFoundError,
        WarehouseAuthError,
    )

    table_id = str(context["table"]) if context is not None and "table" in context else "<unknown>"

    # Scope the Table/Column/Syntax split to the SQL-error connector types
    # (Spark execution errors surface as ``ServerOperationError``; DB-API
    # programming errors as ``ProgrammingError``). A transient
    # ``OperationalError`` / ``RequestError`` is deliberately NOT in scope so a
    # network blip falls through to passthrough rather than mis-mapping to
    # ``QuerySyntaxError``.
    if isinstance(exc, (dbe.ServerOperationError, dbe.ProgrammingError)):
        if any(marker in msg_lower for marker in _TABLE_NOT_FOUND_MARKERS):
            return TableNotFoundError(table=table_id)
        if any(marker in msg_lower for marker in _COLUMN_NOT_FOUND_MARKERS):
            return ColumnNotFoundError(table=table_id, column=_extract_unresolved_column(str(exc)))
        if any(marker in msg_lower for marker in _AUTH_MARKERS):
            return WarehouseAuthError(message=str(exc))
        # Residual SQL error — "your SQL is malformed".
        return QuerySyntaxError(detail=str(exc))

    # Connect-time / transient errors: only an auth-flavoured message maps to
    # auth; everything else passes through unchanged.
    if any(marker in msg_lower for marker in _AUTH_MARKERS):
        return WarehouseAuthError(message=str(exc))
    return exc


__all__ = [
    "_DatabricksClientProtocol",
    "_DatabricksCursorProtocol",
    "make_real_client",
    "map_databricks_exception",
]
