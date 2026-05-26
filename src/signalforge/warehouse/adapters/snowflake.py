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
* :meth:`materialise_sample` (#122 US-004) lands a deterministic sample into a
  session-scoped ``TEMPORARY TABLE`` (``_sf_sample_<run_id>``, ``run_id`` from
  the shared :mod:`signalforge.warehouse._sample_id` recipe) and pins the
  connection so a follow-up :meth:`run_test_sql` reaches the temp table.
* :meth:`run_test_sql` (#122 US-004) wraps a candidate failing-rows SELECT in a
  ``COUNT(*)`` aggregate (plus ``ARRAY_AGG(OBJECT_CONSTRUCT(*))`` sample-row
  capture when ``capture_failures > 0``) and returns a typed
  :class:`TestResult`.
* :meth:`column_stats` still raises :class:`NotImplementedError` naming the
  epic (#118) so the remaining v0.2 implementation work has a single grep
  target (DEC-008). :meth:`estimate_query_bytes` is NOT overridden — the ABC
  default (raising :class:`EstimateNotSupportedError`) is the correct v0.2
  behaviour pending issue #123.
* :meth:`WarehouseAdapter.from_profile` dispatches ``profile.type ==
  "snowflake"`` here so an operator with a Snowflake profile sees a
  ``NotImplementedError`` rather than the v0.1
  :class:`UnsupportedProfileTypeError`.

Still pending (NOT implemented here):

* :meth:`column_stats` — raises :class:`NotImplementedError` naming the epic
  (#118); the per-column profiling path lands in a later v0.2 issue.
* :meth:`estimate_query_bytes` — NOT overridden; the ABC default
  (:class:`EstimateNotSupportedError`) is the correct degrade pending #123.

The ``snowflake.connector`` import stays confined to
:mod:`signalforge.warehouse.adapters._snowflake_client` (the one-shim-per-vendor
SDK seam); this module opens connections only through that shim's
``make_real_client`` and never imports the connector directly.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from signalforge.warehouse._sample_id import _compute_run_id, _hash_session_id
from signalforge.warehouse._sql_safety import validate_identifier, validate_test_sql
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import (
    MaterialisationFailedError,
    SamplingRequiresPartitionFilterError,
    UnknownTableSizeError,
)
from signalforge.warehouse.models import (
    SNOWFLAKE_DIALECT,
    ColumnStats,
    Dialect,
    TableRef,
    TestResult,
)

if TYPE_CHECKING:
    from signalforge.warehouse.adapters._snowflake_client import (
        _SnowflakeClientProtocol,
        _SnowflakeCursorProtocol,
    )
    from signalforge.warehouse.models import PartitionFilter


_LOGGER = logging.getLogger("signalforge.warehouse")

# Mirror of :data:`signalforge.warehouse.adapters.bigquery._LARGE_TABLE_THRESHOLD`
# (100M). Re-declared (not imported) so this module never pulls in the BigQuery
# adapter — keeping one source of truth where reasonable, but the value is the
# load-bearing contract: identical sizing behaviour across vendors (DEC-005).
_LARGE_TABLE_THRESHOLD: int = 100_000_000

# Module-level alias so tests can reassign to a deterministic stand-in
# (mirrors prune-engine.md DEC-019 / llm-drafter.md DEC-004 — never
# monkey-patch ``time.monotonic`` globally). Used to stamp
# ``_session_started_at`` at the first successful ``materialise_sample`` as
# run provenance. NOTE: unlike BigQuery, the Snowflake cleanup WARNING does
# NOT quote a client-side ``auto-expire in <N>s`` countdown — Snowflake reaps
# idle sessions on a server-side, account-configurable timeout we can't
# compute, so ``_session_started_at`` is recorded for provenance only.
_monotonic = time.monotonic

_V02_REMEDIATION = "SnowflakeAdapter is a v0.2 skeleton (issue #118) — full implementation pending."


class SnowflakeAdapter(WarehouseAdapter):
    """:class:`WarehouseAdapter` for Snowflake profiles (v0.2, in progress).

    Issue #122 implements the sampling surface: :meth:`sample_rows`
    (deterministic hash-mod), :meth:`materialise_sample` (session-scoped
    ``TEMPORARY TABLE``), and :meth:`run_test_sql` (``COUNT(*)`` failing-rows
    wrap), all on a connection wired via :meth:`_get_connection` with a
    fail-soft ``__exit__`` cleanup. :meth:`column_stats` still raises
    :class:`NotImplementedError` (a later v0.2 issue); :meth:`estimate_query_bytes`
    inherits the ABC degrade (pending #123).
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
        # the first successful ``materialise_sample`` as run provenance only —
        # the Snowflake cleanup WARNING deliberately does NOT quote a
        # client-side ``auto-expire in <N>s`` countdown (server-side reap; see
        # the ``_monotonic`` note above).
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
            # Reset only the session-tracking state — NOT ``self._connection``.
            # Idempotency comes from the ``_active_session is None`` early-return
            # above, so a second ``__exit__`` is a no-op regardless. Nulling
            # ``self._connection`` here would be wrong: a later call would route
            # back through ``_get_connection()``'s lazy-build branch and
            # silently construct a *real* connection from (possibly empty)
            # creds, discarding a test-injected fake — mirrors BigQuery's
            # cleanup, which resets ``_active_session_id`` but never the client.
            self._active_session = None
            self._session_started_at = None

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
        ``TableRef.from_model``) the query is left **unqualified**
        (``INFORMATION_SCHEMA.TABLES``), which Snowflake resolves against the
        connection's current database. (``CURRENT_DATABASE().INFORMATION_SCHEMA``
        is invalid — ``CURRENT_DATABASE()`` is a scalar function, not a
        namespace qualifier.)

        Returns ``None`` when no row matches or ``ROW_COUNT`` is ``NULL``
        (views / materialised views do not carry a ``ROW_COUNT``).
        """
        from signalforge.warehouse._sql_safety import escape_bq_string_literal

        qc = SNOWFLAKE_DIALECT.quote_char
        db_prefix = "" if table.project is None else f"{qc}{table.project}{qc}."
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

    def _resolve_sample_bucket(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None,
    ) -> int:
        """Size the deterministic-sample bucket via the fail-loud sizing
        pathway shared by :meth:`sample_rows` and :meth:`materialise_sample`
        (DEC-005, single source of truth — no duplicated sizing logic).

        Mirrors BigQuery's sizing exactly:

        * ``num_rows`` unknown + no ``partition_filter`` →
          :class:`UnknownTableSizeError`.
        * ``num_rows`` unknown + ``partition_filter`` present → ``bucket = 1000``
          (DEBUG-logged fallback).
        * ``num_rows >= _LARGE_TABLE_THRESHOLD`` + no ``partition_filter`` →
          :class:`SamplingRequiresPartitionFilterError`.
        * else → ``bucket = max(num_rows // n, 1)``.
        """
        num_rows = self._get_num_rows(table)
        if num_rows is None or num_rows == 0:
            if partition_filter is None:
                raise UnknownTableSizeError(table=table.qualified_name)
            _LOGGER.debug(
                "Sampling table with unknown num_rows; using bucket=1000 (table=%s)",
                table.qualified_name,
            )
            return 1000
        if num_rows >= _LARGE_TABLE_THRESHOLD and partition_filter is None:
            raise SamplingRequiresPartitionFilterError(
                table=table.qualified_name, num_rows=num_rows
            )
        return max(num_rows // n, 1)

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

        bucket = self._resolve_sample_bucket(table, n, partition_filter=partition_filter)

        hash_expr = SNOWFLAKE_DIALECT.sample_row_hash_expr
        quoted = self._quote(table)
        where_clauses = [f"MOD({hash_expr}, {bucket}) < 1"]
        if partition_filter is not None:
            where_clauses.append(self._render_partition_filter(partition_filter))
        where_sql = " AND ".join(where_clauses)
        sql = f"SELECT * FROM {quoted} AS t WHERE {where_sql} ORDER BY {hash_expr} LIMIT {n}"

        return self._execute_to_dicts(sql, table=table)

    # ------------------------------------------------------------------
    # materialise_sample — DEC-002 / DEC-004 / DEC-006 / DEC-007 / DEC-008
    # / DEC-009 of issue #122.
    # ------------------------------------------------------------------

    def materialise_sample(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
        ttl_seconds: int = 3600,
    ) -> TableRef:
        """Materialise a deterministic sample into a session-scoped Snowflake
        ``TEMPORARY TABLE``; return a :class:`TableRef` pointing at it.

        DEC-008 — the ``run_id`` reuses the shared
        :func:`signalforge.warehouse._sample_id._compute_run_id` recipe so the
        temp-table name ``_sf_sample_<run_id>`` is byte-identical to the
        BigQuery adapter's for the same ``(table, n, partition_filter)`` tuple
        under the same ``signalforge.__version__``.

        DEC-002 — the temp table is created on the live connection's session;
        the connection embodies the session that scopes the temp table. We pin
        the connection as ``self._active_session`` so a subsequent
        :meth:`run_test_sql` on the same connection reaches the temp table (no
        ``connection_properties`` routing needed, unlike BigQuery).

        DEC-006 — the deterministic ``MOD(<dialect.sample_row_hash_expr>,
        <bucket>) < 1`` predicate + ``ORDER BY <dialect.sample_row_hash_expr>``
        are read from :data:`SNOWFLAKE_DIALECT` (NOT hard-coded) so the CTAS
        bytes stay consistent with :meth:`sample_rows` and the prune compiler's
        sample CTE (Architectural Commitment #5). ``partition_filter`` lands
        ONCE here, in the CTAS ``WHERE``; per-test queries against the temp
        table do not re-apply it.

        DEC-007 — the ``TEMP TABLE`` is colocated with the SOURCE (created as
        ``<source db>.<source schema>._sf_sample_<run_id>``) and the returned
        :class:`TableRef` is fully-qualified via the source DB / schema.

        Args:
            table: Source production table to sample from.
            n: Target sample size; bucket sizing mirrors :meth:`sample_rows`
                (deterministic hash-mod, fail-loud size guards).
            partition_filter: Optional :class:`PartitionFilter` applied ONCE
                inside the CTAS ``WHERE`` clause.
            ttl_seconds: accepted for ABC parity with BigQuery but IGNORED by
                Snowflake — there is no client-side TTL knob and the cleanup
                WARNING quotes no countdown; Snowflake reaps idle sessions on a
                server-side, account-configurable timeout.

        Returns:
            :class:`TableRef` with ``project=table.project``,
            ``dataset=table.dataset``, ``name="_sf_sample_<run_id>"`` — the
            session-scoped temp table, fully-qualified via the source DB /
            schema.

        Raises:
            ValueError: ``n <= 0``.
            MaterialisationFailedError: any SDK / network / quota failure
                during the CTAS (wraps the original via ``cause=``).
            UnknownTableSizeError, SamplingRequiresPartitionFilterError:
                propagated from :meth:`_resolve_sample_bucket` (the shared
                fail-loud sizing contract).
        """
        if n <= 0:
            raise ValueError(f"materialise_sample requires n > 0; got n={n}")

        # DEC-008 — byte-identical recipe to BigQuery (shared helper).
        run_id = _compute_run_id(table=table, n=n, partition_filter=partition_filter)
        temp_name = f"_sf_sample_{run_id}"
        # DEC-013 of #3 — the temp-table identifier MUST pass
        # validate_identifier before quoting. blake2b-8 lowercase hex is
        # alphanumeric so the regex always passes; the explicit call documents
        # the contract and catches any future drift in _compute_run_id.
        validate_identifier("temp_table_name", temp_name)

        # Shared fail-loud sizing pathway (same as sample_rows; DEC-005).
        bucket = self._resolve_sample_bucket(table, n, partition_filter=partition_filter)

        hash_expr = SNOWFLAKE_DIALECT.sample_row_hash_expr
        quoted_source = self._quote(table)
        # The TEMP TABLE is colocated with the source (DEC-007): same DB /
        # schema, per-component quoted, with the deterministic temp name.
        temp_ref = TableRef(project=table.project, dataset=table.dataset, name=temp_name)
        quoted_temp = self._quote(temp_ref)

        where_clauses = [f"MOD({hash_expr}, {bucket}) < 1"]
        if partition_filter is not None:
            where_clauses.append(self._render_partition_filter(partition_filter))
        where_sql = " AND ".join(where_clauses)
        sql = (
            f"CREATE TEMPORARY TABLE {quoted_temp} AS "
            f"SELECT * FROM {quoted_source} AS t "
            f"WHERE {where_sql} "
            f"ORDER BY {hash_expr} "
            f"LIMIT {n}"
        )

        # Open / reuse the connection (also sets self._active_session) and stamp
        # the session-open time as run provenance (NOT consumed by the cleanup
        # WARNING — Snowflake quotes no client-side TTL countdown).
        conn = self._get_connection()
        self._session_started_at = _monotonic()
        from signalforge.warehouse.adapters._snowflake_client import map_snowflake_exception

        cursor = conn.cursor()
        try:
            cursor.execute(sql)
        except Exception as exc:
            # DEC-009 / DEC-007 — route the SDK failure through the Snowflake
            # exception mapper first, then wrap in the typed
            # MaterialisationFailedError (mirrors BigQuery). The raise-from
            # chain preserves the original SDK exception via __cause__.
            mapped = map_snowflake_exception(exc, context={"table": table.qualified_name})
            cause: BaseException = mapped if mapped is not exc else exc
            raise MaterialisationFailedError(
                message=f"sample materialisation failed for {table.qualified_name}: {cause}",
                cause=cause,
            ) from exc

        # DEC-003 — INFO log uses the HASHED session id, never the raw value.
        # Lazy-format JSON for ANSI safety (warehouse-layer convention).
        raw_session_id = getattr(conn, "session_id", None)
        payload: dict[str, Any] = {
            "table": table.qualified_name,
            "sample_rows": n,
            "run_id": run_id,
        }
        if raw_session_id is not None:
            payload["session_id_hash"] = _hash_session_id(str(raw_session_id))
        _LOGGER.info("materialised sample: %s", json.dumps(payload))

        return temp_ref

    # ------------------------------------------------------------------
    # run_test_sql — DEC-004 / DEC-009 of issue #122.
    # ------------------------------------------------------------------

    def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> TestResult:
        """Run a candidate failing-rows SELECT and return a typed
        :class:`TestResult` (DEC-004).

        The candidate is sanity-checked by
        :func:`signalforge.warehouse._sql_safety.validate_test_sql` (no ``;``,
        no ``--`` comments, balanced parens) before wrapping.

        ``capture_failures == 0`` wraps the candidate in a plain
        ``SELECT COUNT(*) AS failures FROM (<sql>) AS t``. ``capture_failures >
        0`` additionally captures up to ``capture_failures`` example failing
        rows via Snowflake's ``ARRAY_AGG(OBJECT_CONSTRUCT(*))`` over a
        ``LIMIT``-bounded subquery, with the full failing-row ``COUNT(*)``
        computed over the whole set:

        ``SELECT (SELECT COUNT(*) FROM (<sql>) AS c) AS failures,
        (SELECT ARRAY_AGG(OBJECT_CONSTRUCT(*)) FROM (SELECT * FROM (<sql>) AS s
        LIMIT <capture_failures>) AS l) AS samples``

        Executes on ``self._active_session`` (the connection
        :meth:`_get_connection` returns / a prior :meth:`materialise_sample`
        pinned), so a materialised temp table is reachable. SDK errors route
        through :func:`map_snowflake_exception`; ``row_schema`` is ``None`` in
        v0.2 (mirrors BigQuery).
        """
        validate_test_sql(sql)

        if capture_failures > 0:
            wrapped = (
                f"SELECT (SELECT COUNT(*) FROM ({sql}) AS c) AS failures, "
                f"(SELECT ARRAY_AGG(OBJECT_CONSTRUCT(*)) "
                f"FROM (SELECT * FROM ({sql}) AS s LIMIT {capture_failures}) AS l) AS samples"
            )
        else:
            wrapped = f"SELECT COUNT(*) AS failures FROM ({sql}) AS t"

        from signalforge.warehouse.adapters._snowflake_client import map_snowflake_exception

        cursor = self._get_connection().cursor()
        try:
            cursor.execute(wrapped)
            rows = list(cursor.fetchall())
            description = cursor.description
        except Exception as exc:
            mapped = map_snowflake_exception(exc, context={})
            if mapped is exc:
                raise
            raise mapped from exc

        if not rows:  # pragma: no cover - aggregate always returns one row
            raise RuntimeError("run_test_sql wrapper returned no rows")

        column_names = [desc[0] for desc in description] if description else []
        first = rows[0]
        row: dict[str, Any] = (
            dict(first) if isinstance(first, dict) else dict(zip(column_names, first, strict=False))
        )
        # Snowflake folds unquoted aliases to UPPER-case, so the ``failures`` /
        # ``samples`` aliases come back as ``FAILURES`` / ``SAMPLES``. Resolve
        # case-insensitively so the wrapper works regardless of identifier
        # folding (and a DictCursor-style passthrough that preserved case).
        lowered = {str(k).lower(): v for k, v in row.items()}
        failure_count = int(lowered["failures"])

        sample_failures: list[dict[str, Any]] | None
        if capture_failures > 0:
            raw_samples = lowered.get("samples") or []
            sample_failures = [dict(r) for r in raw_samples]
        else:
            sample_failures = None

        return TestResult(
            passed=(failure_count == 0),
            failure_count=failure_count,
            sample_failures=sample_failures,
            row_schema=None,
        )

    def column_stats(self, table: TableRef, column: str) -> ColumnStats:
        raise NotImplementedError(f"column_stats: {_V02_REMEDIATION}")


__all__ = ["SNOWFLAKE_DIALECT", "SnowflakeAdapter"]
