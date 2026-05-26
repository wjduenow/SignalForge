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

The ``import snowflake.connector`` is lazy — confined to the body of
:func:`make_real_client` (NOT at module top) — so importing this shim does not
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


__all__ = [
    "_SnowflakeClientProtocol",
    "_SnowflakeCursorProtocol",
    "make_real_client",
]
