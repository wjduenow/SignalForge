"""Databricks adapter — v0.x skeleton (issue #221; epic #219).

The skeleton exists to validate the warehouse-agnostic seam — Architectural
Commitment #3 of ``CLAUDE.md`` — through a *fourth* concrete adapter code path
(after BigQuery, the Postgres stub, and the Snowflake adapter). Wiring the ABC +
factory seam through Databricks right now surfaces any remaining BigQuery-ism
here rather than during the real implementation (#223 compiler, #224 sampling).
It mirrors the Snowflake skeleton (#119) closely.

Scope (deliberately minimal):

* :meth:`__init__` captures connection params (``host`` / ``http_path`` /
  ``token`` / ``catalog`` / ``schema``) plus the forward-compat OAuth-M2M auth
  params (``auth_type`` / ``client_id`` / ``client_secret``). No connection is
  opened (mirror Snowflake DEC-001 of #122); #224 consumes these when opening
  one.
* :meth:`__repr__` renders ONLY non-credential identifying fields — ``host`` /
  ``http_path`` / ``catalog`` — so a debug-print or log line never leaks
  ``token`` / ``client_secret`` (the repr-redaction rule).
* :meth:`__init__` accepts an injectable ``connection`` (mirror Snowflake
  DEC-001 of #122), lazily built via :func:`_databricks_client.make_real_client`
  on first :meth:`_get_connection`; the Databricks connection embodies the
  session that scopes temp objects.
* :meth:`__enter__` returns ``self``; :meth:`__exit__` runs a fail-soft
  :meth:`_cleanup_active_session` that closes the live connection and
  swallows-and-warns on failure. With no opened connection
  (``_active_session is None``) the ``with adapter:`` block is a clean no-op.
* :meth:`dialect` returns the :data:`DATABRICKS_DIALECT` constant.
* :meth:`sample_rows` / :meth:`column_stats` / :meth:`run_test_sql` raise
  :class:`NotImplementedError` naming the epic (#219) — implemented in #224 /
  the follow-ups bucket.
* :meth:`materialise_sample` / :meth:`estimate_query_bytes` /
  :meth:`get_row_count` / :meth:`run_stats_query` inherit the ABC typed degrade
  (``MaterialisationNotSupportedError`` / ``EstimateNotSupportedError`` /
  ``RowCountNotSupportedError`` / ``StatsQueryNotSupportedError``) — a clean,
  operator-actionable signal until #224 / #225 land.
* :meth:`WarehouseAdapter.from_profile` dispatches ``profile.type ==
  "databricks"`` here so an operator with a Databricks profile sees a typed
  "v0.x pending" ``NotImplementedError`` rather than the v0.1
  :class:`UnsupportedProfileTypeError`.

The ``databricks-sql-connector`` import stays confined to
:mod:`signalforge.warehouse.adapters._databricks_client` (the one-shim-per-vendor
SDK seam); this module opens connections only through that shim's
``make_real_client`` and never imports the connector directly.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from signalforge.warehouse._sample_id import _hash_session_id
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.models import (
    DATABRICKS_DIALECT,
    ColumnStats,
    Dialect,
    TableRef,
    TestResult,
)

if TYPE_CHECKING:
    from signalforge.warehouse.adapters._databricks_client import _DatabricksClientProtocol
    from signalforge.warehouse.models import PartitionFilter


_LOGGER = logging.getLogger("signalforge.warehouse")

_SKELETON_REMEDIATION = (
    "DatabricksAdapter is a v0.x skeleton (issue #219) — full implementation pending."
)


class DatabricksAdapter(WarehouseAdapter):
    """:class:`WarehouseAdapter` for Databricks SQL profiles (v0.x skeleton).

    Issue #221 stands up the seam end-to-end with every warehouse operation
    degrading gracefully: :meth:`sample_rows` / :meth:`column_stats` /
    :meth:`run_test_sql` raise :class:`NotImplementedError`; the degrade-default
    ABC methods (:meth:`materialise_sample` / :meth:`estimate_query_bytes` /
    :meth:`get_row_count` / :meth:`run_stats_query`) inherit their typed
    ``*NotSupportedError``. The sampling / SQL surface lands in #224.
    """

    def __init__(
        self,
        *,
        connection: _DatabricksClientProtocol | None = None,
        host: str | None = None,
        http_path: str | None = None,
        token: str | None = None,
        catalog: str | None = None,
        schema: str | None = None,
        auth_type: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        # Injectable connection seam (mirrors Snowflake DEC-001 of #122 /
        # BigQuery's ``client=``). ``None`` triggers a lazy
        # ``make_real_client(...)`` build on first :meth:`_get_connection`; tests
        # inject a fake. No connection is opened here.
        self._connection = connection
        self._host = host
        self._http_path = http_path
        self._token = token
        self._catalog = catalog
        self._schema = schema
        # Forward-compat OAuth-M2M auth params (epic open-decision #3 — PAT only
        # for v0.x). Captured so a later child can open an M2M connection without
        # a constructor-signature change; the skeleton's ``make_real_client`` uses
        # PAT (``token``) only.
        self._auth_type = auth_type
        self._client_id = client_id
        self._client_secret = client_secret

        # The Databricks connection embodies the session that scopes temp
        # objects (mirrors Snowflake DEC-002 of #122). Set on the first
        # :meth:`_get_connection`; reset to ``None`` in
        # :meth:`_cleanup_active_session` so a second ``__exit__`` is a no-op.
        self._active_session: _DatabricksClientProtocol | None = None

    def __repr__(self) -> str:
        # Render ONLY non-credential identifying fields. NEVER token, schema, or
        # the OAuth-M2M ``client_id`` / ``client_secret``.
        return (
            f"<DatabricksAdapter host={self._host!r} "
            f"http_path={self._http_path!r} catalog={self._catalog!r}>"
        )

    def _get_connection(self) -> _DatabricksClientProtocol:
        """Return the live Databricks connection (lazy build; mirrors Snowflake).

        Lazily builds the connection via
        :func:`signalforge.warehouse.adapters._databricks_client.make_real_client`
        from the stored PAT params on first use, caching it on
        ``self._connection``. Records the connection as ``self._active_session``
        on first open so the ``__exit__`` cleanup boundary has something to tear
        down. The SDK shim import is lazy (inside this body) so importing the
        adapter never requires ``databricks-sql-connector`` (it ships only under
        the ``[databricks]`` extra). Not exercised at the skeleton stage (every
        op raises / degrades before reaching here) but present so #224 builds the
        sampling surface on top without restructuring.
        """
        if self._connection is None:
            from signalforge.warehouse.adapters._databricks_client import make_real_client

            self._connection = make_real_client(
                host=self._host or "",
                http_path=self._http_path or "",
                token=self._token or "",
            )
        if self._active_session is None:
            self._active_session = self._connection
        return self._connection

    def __enter__(self) -> WarehouseAdapter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # Best-effort, fail-soft session cleanup (mirrors Snowflake #122
        # DEC-003). Closing the connection ends the Databricks session. Failure
        # is swallowed-and-warned; state always resets so a subsequent
        # ``__exit__`` is a no-op.
        self._cleanup_active_session()

    @staticmethod
    def _read_session_id(conn: _DatabricksClientProtocol) -> str | None:
        """Best-effort read of the connection's opaque session id.

        Read defensively — a minimal fake exposes a plain ``session_id``
        attribute (mirroring how the Snowflake adapter reads ``conn.session_id``),
        while the real ``databricks.sql`` ``Connection`` exposes
        ``get_session_id_hex()`` instead. Try the attribute first, fall back to
        the getter (wrapped so a broken connection can't raise out of the
        cleanup boundary). Returns ``None`` when neither surfaces a value.
        """
        raw = getattr(conn, "session_id", None)
        if raw is not None:
            return str(raw)
        getter = getattr(conn, "get_session_id_hex", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:  # noqa: BLE001 - cleanup boundary must never raise
                return None
            return None if value is None else str(value)
        return None

    def _cleanup_active_session(self) -> None:
        """Best-effort, fail-soft session cleanup (cleanup-boundary fail-soft).

        Splits out from :meth:`__exit__` so the test surface can exercise the
        cleanup path without entering an actual ``with`` block. Idempotent:
        returns immediately when ``self._active_session`` is ``None`` (mirrors
        the Snowflake / BigQuery cleanup-boundary fail-soft pattern).
        """
        conn = self._active_session
        if conn is None:
            return
        # The hashed form is used on the happy path (redaction); the raw form is
        # the deliberate narrow exception in the cleanup-failure WARNING only
        # (mirrors Snowflake #122 DEC-014).
        raw_session_id = self._read_session_id(conn)
        try:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001 - cleanup-boundary swallows all
                # Cleanup-boundary fail-soft (mirrors Snowflake #122 DEC-014):
                # swallow the failure and emit ONE operator-actionable WARNING.
                # Like Snowflake there is NO manual cleanup command — a Databricks
                # session-local temp object is unreachable outside its owning
                # session, so the honest durable fallback is Databricks' server-side
                # reap of the session when the SQL warehouse drops the connection.
                # The raw ``session_id`` is the deliberate narrow exception to the
                # redaction rule so the operator can correlate the orphaned session
                # in Databricks' query history; the WARNING quotes NO client-side
                # ``auto-expire in <N>s`` countdown (the reap is server-side and not
                # locally computable). ``--quiet`` does NOT suppress this WARNING (it
                # floors at WARNING). Lazy-format ``%s`` for ANSI safety
                # (warehouse-layer convention).
                _LOGGER.warning(
                    "Databricks session cleanup failed; the connection's "
                    "session-local temp objects will be dropped when Databricks "
                    "reaps the session server-side (when the SQL warehouse drops "
                    "the connection). No manual cleanup command is possible — a "
                    "session-local temp object is unreachable outside its owning "
                    "session.\n"
                    "  Session ID: %s\n"
                    "  Reason: %s",
                    raw_session_id,
                    type(exc).__name__,
                )
            else:
                # Happy path — redacted INFO log. The raw ``session_id`` never
                # leaves the adapter; only the hash correlates records. Lazy-format
                # JSON for ANSI safety (warehouse-layer convention).
                payload: dict[str, str] = {}
                if raw_session_id is not None:
                    payload["session_id_hash"] = _hash_session_id(raw_session_id)
                _LOGGER.info("session closed: %s", json.dumps(payload))
        finally:
            # Reset only the session-tracking state — NOT ``self._connection``.
            # Idempotency comes from the ``_active_session is None`` early-return;
            # nulling ``self._connection`` would route a re-entry back through
            # the lazy-build branch and silently discard a test-injected fake
            # (mirrors Snowflake / BigQuery cleanup).
            self._active_session = None

    def dialect(self) -> Dialect:
        return DATABRICKS_DIALECT

    def sample_rows(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(f"sample_rows: {_SKELETON_REMEDIATION}")

    def column_stats(self, table: TableRef, column: str) -> ColumnStats:
        raise NotImplementedError(f"column_stats: {_SKELETON_REMEDIATION}")

    def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> TestResult:
        raise NotImplementedError(f"run_test_sql: {_SKELETON_REMEDIATION}")


__all__ = ["DATABRICKS_DIALECT", "DatabricksAdapter"]
