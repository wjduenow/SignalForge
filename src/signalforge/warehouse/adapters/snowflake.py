"""Snowflake adapter — v0.2 skeleton (issue #119; epic #118).

The skeleton exists to validate the warehouse-agnostic seam — Architectural
Commitment #3 of ``CLAUDE.md`` — through a *third* concrete adapter code path
(after :class:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter` and the
:class:`signalforge.warehouse.adapters.postgres.PostgresAdapter` stub). Wiring
the ABC + factory seam through Snowflake right now surfaces any leak here
rather than during the real Snowflake implementation (issue #118).

Scope (deliberately minimal):

* :meth:`__init__` captures connection params (``account`` / ``user`` /
  ``password`` / ``role`` / ``warehouse`` / ``database`` / ``schema``) plus the
  key-pair / SSO auth params (``private_key_path`` /
  ``private_key_passphrase`` / ``authenticator``) for forward-compat (DEC-002 /
  DEC-008). No connection is opened; #122 consumes these when opening one.
* :meth:`__repr__` renders ONLY non-credential identifying fields — ``account``
  and ``warehouse`` — so a debug-print or log line never leaks ``user`` /
  ``password`` / ``role`` / ``database`` / ``schema`` (DEC-003).
* :meth:`__init__` accepts an injectable ``connection`` (DEC-001 of #122),
  lazily built via :func:`_snowflake_client.make_real_client` on first
  :meth:`_get_connection`; the connection embodies the session that scopes
  temp tables (DEC-002).
* :meth:`__enter__` returns ``self``; :meth:`__exit__` runs a fail-soft
  :meth:`_cleanup_active_session` that closes the live connection (reaping
  session-scoped temp tables) and swallows-and-warns on failure (DEC-003 of
  #122). With no opened connection (``_active_session is None``), the
  ``with adapter:`` block is a clean no-op.
* :meth:`dialect` returns the :data:`SNOWFLAKE_DIALECT` constant from
  :mod:`signalforge.warehouse.models` (``quote_char='"'``,
  ``identifier_case='upper'``, ``supports_qualify=True``).
* :meth:`sample_rows` is implemented (#122 US-003) — deterministic hash-mod
  sampling (``MOD(ABS(HASH(*)), bucket)``) sized from
  ``INFORMATION_SCHEMA.TABLES.ROW_COUNT``, mirroring BigQuery's fail-loud
  sizing (:class:`UnknownTableSizeError` /
  :class:`SamplingRequiresPartitionFilterError`).
* :meth:`column_stats` and :meth:`run_test_sql` still raise
  :class:`NotImplementedError` naming the epic (#118) so the remaining v0.2
  implementation work has a single grep target (DEC-008).
* :meth:`materialise_sample` / :meth:`estimate_query_bytes` are NOT overridden
  — the ABC defaults (raising :class:`MaterialisationNotSupportedError` /
  :class:`EstimateNotSupportedError`) are the correct v0.2 behaviour for a
  warehouse that hasn't grown those primitives yet (DEC-008).
* :meth:`WarehouseAdapter.from_profile` dispatches ``profile.type ==
  "snowflake"`` here so an operator with a Snowflake profile sees a
  ``NotImplementedError`` rather than the v0.1
  :class:`UnsupportedProfileTypeError`.

What this skeleton does NOT do:

* Real Snowflake connectivity. No ``snowflake.connector`` import — that is
  confined to :mod:`signalforge.warehouse.adapters._snowflake_client`, the
  one-shim-per-vendor SDK seam, when the full implementation lands.
* Extend :class:`DbtProfileTarget` to carry Snowflake-specific fields
  (``account`` / ``user`` / ``role`` / ``warehouse``). The current profile
  model is BigQuery-shaped; growing it to wire those fields into the factory
  is issue #120's work.

When the v0.2 implementation lands (issue #118), replace every
``NotImplementedError`` with the real adapter call.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from signalforge.warehouse._sample_id import _hash_session_id
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import (
    SamplingRequiresPartitionFilterError,
    UnknownTableSizeError,
)
from signalforge.warehouse.models import SNOWFLAKE_DIALECT, ColumnStats, Dialect, TestResult

if TYPE_CHECKING:
    from signalforge.warehouse.adapters._snowflake_client import (
        _SnowflakeClientProtocol,
        _SnowflakeCursorProtocol,
    )
    from signalforge.warehouse.models import PartitionFilter, TableRef


_LOGGER = logging.getLogger("signalforge.warehouse")

# Mirror of :data:`signalforge.warehouse.adapters.bigquery._LARGE_TABLE_THRESHOLD`
# (100M). Re-declared (not imported) so this module never pulls in the BigQuery
# adapter — keeping one source of truth where reasonable, but the value is the
# load-bearing contract: identical sizing behaviour across vendors (DEC-005).
_LARGE_TABLE_THRESHOLD: int = 100_000_000

# Module-level alias so tests can reassign to a deterministic stand-in
# (mirrors prune-engine.md DEC-019 / llm-drafter.md DEC-004 — never
# monkey-patch ``time.monotonic`` globally). Set at the first successful
# ``materialise_sample`` (#122 US-003/US-004) to drive the cleanup-WARNING
# ``auto-expire`` text.
_monotonic = time.monotonic

_V02_REMEDIATION = "SnowflakeAdapter is a v0.2 skeleton (issue #118) — full implementation pending."


class SnowflakeAdapter(WarehouseAdapter):
    """:class:`WarehouseAdapter` for Snowflake profiles (v0.2, in progress).

    #122 US-002 wired the connection seam (:meth:`_get_connection`) and the
    fail-soft ``__exit__`` cleanup; #122 US-003 implements :meth:`sample_rows`
    (deterministic hash-mod sampling). :meth:`column_stats` /
    :meth:`run_test_sql` still raise :class:`NotImplementedError`; the
    materialise / run-test work lands in later #122 stories.
    """

    def __init__(
        self,
        *,
        connection: _SnowflakeClientProtocol | None = None,
        account: str | None = None,
        user: str | None = None,
        password: str | None = None,
        role: str | None = None,
        warehouse: str | None = None,
        database: str | None = None,
        schema: str | None = None,
        private_key_path: str | None = None,
        private_key_passphrase: str | None = None,
        authenticator: str | None = None,
    ) -> None:
        # DEC-001 of #122 — injectable connection seam (mirrors BigQuery's
        # ``client=``). ``None`` triggers a lazy ``make_real_client(...)`` build
        # on first :meth:`_get_connection`; tests inject a fake.
        self._connection = connection
        self._account = account
        self._user = user
        self._password = password
        self._role = role
        self._warehouse = warehouse
        self._database = database
        self._schema = schema
        self._private_key_path = private_key_path
        self._private_key_passphrase = private_key_passphrase
        self._authenticator = authenticator

        # DEC-002 of #122 — the Snowflake *connection* embodies the session
        # that scopes temp tables, so we store the connection object itself
        # (BigQuery stored a ``session_id`` string threaded via
        # ``connection_properties``; Snowflake needs no such routing — every op
        # runs on the one connection). Set on the first :meth:`_get_connection`;
        # reset to ``None`` in :meth:`_cleanup_active_session` so a second
        # ``__exit__`` is a no-op. ``_session_started_at`` (monotonic) is set at
        # the first successful ``materialise_sample`` (#122 US-003/US-004) and
        # drives the cleanup-WARNING ``auto-expire`` text.
        self._active_session: _SnowflakeClientProtocol | None = None
        self._session_started_at: float | None = None

    def __repr__(self) -> str:
        # DEC-003: render ONLY non-credential identifying fields. NEVER user,
        # password, role, database, schema, private_key_path,
        # private_key_passphrase, or authenticator.
        return f"<SnowflakeAdapter account={self._account!r} warehouse={self._warehouse!r}>"

    def _get_connection(self) -> _SnowflakeClientProtocol:
        """Return the live Snowflake connection (DEC-001 of #122).

        Lazily builds the connection via
        :func:`signalforge.warehouse.adapters._snowflake_client.make_real_client`
        from the stored auth params on first use, caching it on
        ``self._connection``. Also records the connection as
        ``self._active_session`` on first open (DEC-002) so the ``__exit__``
        cleanup boundary has something to tear down. The SDK shim import is
        lazy (inside this body) so importing the adapter never requires
        ``snowflake-connector-python`` (it ships only under the ``[snowflake]``
        extra).
        """
        if self._connection is None:
            from signalforge.warehouse.adapters._snowflake_client import make_real_client

            self._connection = make_real_client(
                account=self._account or "",
                user=self._user or "",
                password=self._password or "",
                role=self._role,
                warehouse=self._warehouse,
                database=self._database,
                schema=self._schema,
            )
        if self._active_session is None:
            self._active_session = self._connection
        return self._connection

    def __enter__(self) -> WarehouseAdapter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # DEC-003 of #122 — best-effort, fail-soft session cleanup. Closing the
        # connection ends the Snowflake session and reaps its session-scoped
        # temp tables. Failure is swallowed-and-warned; state always resets so
        # a subsequent ``__exit__`` is a no-op.
        self._cleanup_active_session()

    def _cleanup_active_session(self) -> None:
        """DEC-003 of #122 — best-effort, fail-soft session cleanup.

        Splits out from :meth:`__exit__` so the test surface can exercise the
        cleanup path without entering an actual ``with`` block. Idempotent:
        returns immediately when ``self._active_session`` is ``None`` (mirrors
        the BigQuery cleanup-boundary fail-soft pattern, #22 DEC-013/DEC-014).
        """
        conn = self._active_session
        if conn is None:
            return
        # ``session_id`` is read defensively — a real connection exposes it; a
        # minimal fake might not. The hashed form is used on the happy path
        # (DEC-003 redaction); the raw form is the deliberate DEC-014 narrow
        # exception in the cleanup-failure WARNING only.
        raw_session_id = getattr(conn, "session_id", None)
        try:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001 - cleanup-boundary swallows all
                # Cleanup-boundary fail-soft (#22 DEC-014, adapted to
                # Snowflake): swallow the failure and emit ONE operator-actionable
                # WARNING. Unlike BigQuery, there is NO manual cleanup command —
                # a session-scoped temp table is unreachable outside its owning
                # session, so the honest durable fallback is Snowflake's
                # server-side idle-session reap (which drops the temp table).
                # The raw ``session_id`` is the deliberate DEC-014 narrow
                # exception to DEC-003 redaction so the operator can correlate
                # the orphaned session in Snowflake's query history. ``--quiet``
                # does NOT suppress this WARNING (it floors at WARNING).
                _LOGGER.warning(
                    "Snowflake session cleanup failed; the connection's "
                    "session-scoped temp table will be dropped when Snowflake "
                    "reaps the idle session server-side. No manual drop command "
                    "is possible — a temp table is unreachable outside its "
                    "owning session.\n"
                    "  Session ID: %s\n"
                    "  Reason: %s",
                    raw_session_id,
                    type(exc).__name__,
                )
            else:
                # Happy path — DEC-003 redacted INFO log. The raw ``session_id``
                # never leaves the adapter; only the hash correlates records.
                payload: dict[str, str] = {}
                if raw_session_id is not None:
                    payload["session_id_hash"] = _hash_session_id(str(raw_session_id))
                _LOGGER.info("session closed: %s", json.dumps(payload))
        finally:
            self._active_session = None
            self._session_started_at = None
            self._connection = None

    def dialect(self) -> Dialect:
        return SNOWFLAKE_DIALECT

    # ------------------------------------------------------------------
    # sample_rows — DEC-005 / DEC-006 / DEC-009 / DEC-010 of issue #122.
    # ------------------------------------------------------------------

    def _quote(self, ref: TableRef) -> str:
        """Render a fully-qualified Snowflake table identifier (DEC-006).

        Snowflake quotes each component separately — ``"DB"."SCHEMA"."NAME"``
        — because a single quoted string spanning dots reads as ONE literal
        identifier named ``db.schema.name`` (unlike BigQuery's single
        backtick wrapping the whole path). Two-part ``"SCHEMA"."NAME"`` when
        ``project`` is ``None``. Mirrors the prune compiler's
        ``_qualified_table_name`` under ``quote_qualified_per_component=True``
        so the adapter's sample SQL is consistent with the compiler's CTE.

        The dataset / name are already DEC-013-validated by :class:`TableRef`'s
        ``__post_init__``, so no re-validation is needed before quoting.
        """
        qc = SNOWFLAKE_DIALECT.quote_char
        components = (
            [ref.dataset, ref.name] if ref.project is None else [ref.project, ref.dataset, ref.name]
        )
        return ".".join(f"{qc}{component}{qc}" for component in components)

    def _render_partition_filter(self, pf: PartitionFilter) -> str:
        """Render a :class:`PartitionFilter` to a Snowflake SQL fragment (DEC-006).

        ``datetime`` → ``'…'::TIMESTAMP``; ``date`` → ``'…'::DATE`` (via the
        dialect literal templates); ``str`` is escaped via
        :func:`escape_bq_string_literal` for safe inclusion inside a
        single-quoted literal (Snowflake uses backslash escaping inside
        single quotes, so the BigQuery helper is correct here). The column
        name is per-component quoted and already DEC-013-validated by
        :class:`PartitionFilter`'s ``__post_init__``.

        Mirrors the prune compiler's ``_render_partition_filter(pf, dialect)``.
        """
        qc = SNOWFLAKE_DIALECT.quote_char
        # ``datetime`` is a subclass of ``date``, so check it first.
        if isinstance(pf.value, datetime):
            rendered = SNOWFLAKE_DIALECT.timestamp_literal_template.format(
                value=pf.value.isoformat()
            )
        elif isinstance(pf.value, date):
            rendered = SNOWFLAKE_DIALECT.date_literal_template.format(value=pf.value.isoformat())
        else:
            from signalforge.warehouse._sql_safety import escape_bq_string_literal

            rendered = f"'{escape_bq_string_literal(str(pf.value))}'"
        return f"{qc}{pf.column}{qc} {pf.op} {rendered}"

    def _get_num_rows(self, table: TableRef) -> int | None:
        """Look up ``ROW_COUNT`` from ``INFORMATION_SCHEMA.TABLES`` (DEC-005).

        Case-insensitive on ``TABLE_SCHEMA`` / ``TABLE_NAME`` (Snowflake folds
        unquoted identifiers to upper-case). The schema / name are embedded as
        single-quoted STRING LITERALS — escaped via
        :func:`escape_bq_string_literal` (backslash escaping inside single
        quotes is correct for Snowflake) even though they are already
        identifier-validated on the :class:`TableRef`. The ``<database>``
        prefix is quoted per the dialect; when ``table.project`` is ``None``
        (direct callers — the prune path always qualifies via
        ``TableRef.from_model``) it falls back to ``CURRENT_DATABASE()``.

        Returns ``None`` when no row matches or ``ROW_COUNT`` is ``NULL``
        (views / materialised views do not carry a ``ROW_COUNT``).
        """
        from signalforge.warehouse._sql_safety import escape_bq_string_literal

        qc = SNOWFLAKE_DIALECT.quote_char
        db_prefix = "CURRENT_DATABASE()." if table.project is None else f"{qc}{table.project}{qc}."
        schema_lit = escape_bq_string_literal(table.dataset)
        name_lit = escape_bq_string_literal(table.name)
        sql = (
            f"SELECT ROW_COUNT FROM {db_prefix}INFORMATION_SCHEMA.TABLES "
            f"WHERE UPPER(TABLE_SCHEMA) = UPPER('{schema_lit}') "
            f"AND UPPER(TABLE_NAME) = UPPER('{name_lit}')"
        )
        rows = self._execute(sql, table=table)
        if not rows:
            return None
        first = rows[0]
        value = first[0] if isinstance(first, (list, tuple)) else first
        if value is None:
            return None
        return int(value)

    def _execute(self, sql: str, *, table: TableRef) -> list[Any]:
        """Run ``sql`` on the connection's cursor, returning ``fetchall()``.

        Any SDK exception is routed through
        :func:`signalforge.warehouse.adapters._snowflake_client.map_snowflake_exception`
        (DEC-009): a mapped typed error is re-raised ``from`` the original; an
        unchanged passthrough re-raises the original.
        """
        from signalforge.warehouse.adapters._snowflake_client import map_snowflake_exception

        cursor = self._get_connection().cursor()
        try:
            cursor.execute(sql)
            return list(cursor.fetchall())
        except Exception as exc:
            mapped = map_snowflake_exception(exc, context={"table": table.qualified_name})
            if mapped is exc:
                raise
            raise mapped from exc

    def _execute_to_dicts(self, sql: str, *, table: TableRef) -> list[dict[str, Any]]:
        """Run ``sql`` and shape tuple ``fetchall()`` rows into dicts (DEC-010).

        Reads ``cursor.description`` (DB-API: each descriptor's ``[0]`` is the
        column name) so the adapter builds ``dict`` rows from tuple results
        without depending on a ``DictCursor``.
        """
        from signalforge.warehouse.adapters._snowflake_client import map_snowflake_exception

        cursor = self._get_connection().cursor()
        try:
            cursor.execute(sql)
            rows = list(cursor.fetchall())
        except Exception as exc:
            mapped = map_snowflake_exception(exc, context={"table": table.qualified_name})
            if mapped is exc:
                raise
            raise mapped from exc
        return self._rows_to_dicts(cursor, rows)

    @staticmethod
    def _rows_to_dicts(cursor: _SnowflakeCursorProtocol, rows: list[Any]) -> list[dict[str, Any]]:
        """Build dict rows from tuple ``fetchall()`` results via ``description``.

        Each DB-API descriptor's element ``[0]`` is the column name. A row that
        is already a mapping passes through unchanged (defensive against a
        ``DictCursor``-style connection).
        """
        description = cursor.description
        column_names = [desc[0] for desc in description] if description else []
        result: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                result.append(dict(row))
            else:
                result.append(dict(zip(column_names, row, strict=False)))
        return result

    def sample_rows(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
    ) -> list[dict[str, Any]]:
        """Sample up to ``n`` rows deterministically (DEC-005 / DEC-006).

        Algorithm (mirrors BigQuery's ``sample_rows`` exactly, Snowflake-flavoured):

        1. Look up ``ROW_COUNT`` via :meth:`_get_num_rows`.
        2. Decision:

           * ``num_rows`` unknown + no ``partition_filter`` →
             :class:`UnknownTableSizeError`.
           * ``num_rows`` unknown + ``partition_filter`` present →
             ``bucket = 1000``; debug-log the fallback.
           * ``num_rows >= _LARGE_TABLE_THRESHOLD`` + no ``partition_filter`` →
             :class:`SamplingRequiresPartitionFilterError`.
           * else → ``bucket = max(num_rows // n, 1)``.
        3. Issue ``SELECT * FROM <quoted> AS t WHERE
           MOD(<dialect.sample_row_hash_expr>, <bucket>) < 1
           [AND <partition_filter>] ORDER BY <dialect.sample_row_hash_expr>
           LIMIT n``.

        The hash-mod approach (``MOD(ABS(HASH(*)), bucket)``) is deterministic
        across runs (same input → same prune decision) and works on views /
        MVs / CTEs where ``TABLESAMPLE`` does not. The hash expression is read
        from :data:`SNOWFLAKE_DIALECT` (NOT hard-coded) so the adapter's sample
        SQL is byte-consistent with the prune compiler's sample CTE. The
        ``ORDER BY`` makes ``LIMIT`` truncation deterministic when the WHERE
        filter retains more than ``n`` rows.
        """
        if n <= 0:
            raise ValueError(f"sample_rows requires n > 0; got n={n}")

        num_rows = self._get_num_rows(table)

        if num_rows is None or num_rows == 0:
            if partition_filter is None:
                raise UnknownTableSizeError(table=table.qualified_name)
            bucket = 1000
            _LOGGER.debug(
                "Sampling table with unknown num_rows; using bucket=1000 (table=%s)",
                table.qualified_name,
            )
        elif num_rows >= _LARGE_TABLE_THRESHOLD and partition_filter is None:
            raise SamplingRequiresPartitionFilterError(
                table=table.qualified_name, num_rows=num_rows
            )
        else:
            bucket = max(num_rows // n, 1)

        hash_expr = SNOWFLAKE_DIALECT.sample_row_hash_expr
        quoted = self._quote(table)
        where_clauses = [f"MOD({hash_expr}, {bucket}) < 1"]
        if partition_filter is not None:
            where_clauses.append(self._render_partition_filter(partition_filter))
        where_sql = " AND ".join(where_clauses)
        sql = f"SELECT * FROM {quoted} AS t WHERE {where_sql} ORDER BY {hash_expr} LIMIT {n}"

        return self._execute_to_dicts(sql, table=table)

    def column_stats(self, table: TableRef, column: str) -> ColumnStats:
        raise NotImplementedError(f"column_stats: {_V02_REMEDIATION}")

    def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> TestResult:
        raise NotImplementedError(f"run_test_sql: {_V02_REMEDIATION}")


__all__ = ["SNOWFLAKE_DIALECT", "SnowflakeAdapter"]
