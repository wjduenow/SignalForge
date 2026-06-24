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

:func:`map_databricks_exception` is a **minimal stub** at the skeleton stage
(auth-flavoured failures → :class:`WarehouseAuthError`; everything else returned
unchanged); the full taxonomy (table-not-found / column-not-found / query-syntax
splits, mirroring ``map_snowflake_exception``) is fleshed out in the test-harness
child (#226).

The ``from databricks import sql`` import is lazy — confined to the body of
:func:`make_real_client` — so importing this shim does not require the connector
to be installed, and the rest of the warehouse subpackage doesn't pay the import
cost (``databricks-sql-connector`` ships only under the ``[databricks]`` optional
extra, not the base install).

Observability discipline: no logger calls in this shim. Logging lives in the
adapter where the stage label is known (mirrors ``_client.py`` / ``_snowflake_client.py``).
"""

from __future__ import annotations

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


def map_databricks_exception(exc: Exception, *, context: dict[str, Any] | None = None) -> Exception:
    """Translate a ``databricks.sql`` exception into a typed warehouse error
    (skeleton stub — issue #221).

    Mirrors :func:`signalforge.warehouse.adapters._snowflake_client.map_snowflake_exception`'s
    return convention: returns the *new* exception so the caller can ``raise
    mapped from exc``; returns ``exc`` unchanged when no specific mapping fits —
    the caller should re-raise the original in that case rather than swallow it.

    **Minimal at the skeleton stage:** an auth-flavoured failure (message
    carrying one of :data:`_AUTH_MARKERS`) maps to :class:`WarehouseAuthError`;
    everything else is returned unchanged. The full taxonomy (object-does-not-exist
    → :class:`TableNotFoundError`, invalid-identifier → :class:`ColumnNotFoundError`,
    residual → :class:`QuerySyntaxError`) is fleshed out in #226, keyed on the
    ``databricks.sql.exc`` hierarchy. No SDK import is needed for this stub — the
    auth detection is message-marker based — so the mapper works whether or not
    the connector is installed.

    The optional ``context`` kwarg carries adapter-side state the raw connector
    exception doesn't expose (e.g. ``{"table": ...}``), mirroring the Snowflake
    mapper; it is unused by this stub but kept in the signature so #226 can fill
    in the Table/Column arms without a call-site change.
    """
    msg_lower = str(exc).lower()
    if any(marker in msg_lower for marker in _AUTH_MARKERS):
        from signalforge.warehouse.errors import WarehouseAuthError

        return WarehouseAuthError(message=str(exc))
    return exc


__all__ = [
    "_DatabricksClientProtocol",
    "_DatabricksCursorProtocol",
    "make_real_client",
    "map_databricks_exception",
]
