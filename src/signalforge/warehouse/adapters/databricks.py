"""Databricks adapter — deterministic sampling surface (issue #224; epic #219).

Implements the warehouse-agnostic seam — Architectural Commitment #3 of
``CLAUDE.md`` — through a *fourth* concrete adapter code path (after BigQuery,
the Postgres stub, and the Snowflake adapter), mirroring the Snowflake adapter
(#119 skeleton, #122 sampling) closely. The #221 skeleton wired the ABC +
factory seam and #223 landed the prune-compiler dialect; #224 (this surface)
lands the first real warehouse I/O. Live validity against a Databricks SQL
warehouse is certified in #226.

Surface:

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
* :meth:`sample_rows` is implemented (#224 US-003) — deterministic hash-mod
  sampling (the inline-predicate shape: ``MOD((xxhash64(to_json(struct(*))) &
  9223372036854775807), bucket) < 1``) sized from :meth:`get_row_count`
  (``SELECT COUNT(*)``), with the fail-loud sizing the Snowflake / BigQuery
  adapters share (:class:`UnknownTableSizeError` /
  :class:`SamplingRequiresPartitionFilterError`).
* :meth:`get_row_count` is implemented (#224 US-003) — overrides the ABC degrade
  with a ``SELECT COUNT(*)`` (``DESCRIBE DETAIL`` has no reliable ``numRows``;
  COUNT is metadata-cheap on Delta), returning ``None`` on a
  :class:`WarehouseError` so the shared sizing pathway decides.
* :meth:`materialise_sample` is implemented (#224 US-004) — overrides the ABC
  degrade with a session-scoped, qualified ``CREATE TEMPORARY TABLE
  <cat>.<sch>._sf_sample_<run_id> AS <deterministic sample body>`` (``run_id``
  from the shared :mod:`signalforge.warehouse._sample_id` recipe), pinning the
  connection so a follow-up :meth:`run_test_sql` reaches the temp table.
* :meth:`run_test_sql` is implemented (#224 US-004) — overrides the
  :class:`NotImplementedError` stub: wraps a candidate failing-rows SELECT in a
  ``COUNT(*)`` aggregate (plus a per-row ``to_json(struct(*))`` LIMIT capture
  query when ``capture_failures > 0``) and returns a typed :class:`TestResult`.
* :meth:`column_stats` is implemented (#224 US-005, DEC-011) — overrides the
  :class:`NotImplementedError` stub with a single aggregate query (count /
  distinct / nulls / min / max / data_type). Databricks ships this AHEAD of
  Snowflake (which stubs it); the Snowflake-parity decision is GitHub issue #258.
* :meth:`estimate_query_bytes` / :meth:`run_stats_query` inherit the ABC typed
  degrade (``EstimateNotSupportedError`` / ``StatsQueryNotSupportedError``) — a
  clean, operator-actionable signal until #225 lands.
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
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from signalforge.warehouse._sample_id import _compute_run_id, _hash_session_id
from signalforge.warehouse._sample_sql import render_sample_select
from signalforge.warehouse._sql_safety import validate_identifier, validate_test_sql
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import (
    MaterialisationFailedError,
    SamplingRequiresPartitionFilterError,
    UnknownTableSizeError,
    WarehouseError,
)
from signalforge.warehouse.models import (
    DATABRICKS_DIALECT,
    ColumnStats,
    Dialect,
    TableRef,
    TestResult,
)

if TYPE_CHECKING:
    from signalforge.warehouse.adapters._databricks_client import (
        _DatabricksClientProtocol,
        _DatabricksCursorProtocol,
    )
    from signalforge.warehouse.models import PartitionFilter


_LOGGER = logging.getLogger("signalforge.warehouse")

# Mirror of :data:`signalforge.warehouse.adapters.bigquery._LARGE_TABLE_THRESHOLD`
# (100M). Re-declared (not imported) so this module never pulls in the BigQuery
# adapter — the value is the load-bearing contract: identical sizing behaviour
# across vendors (mirrors the Snowflake adapter's same re-declaration).
_LARGE_TABLE_THRESHOLD: int = 100_000_000


class DatabricksAdapter(WarehouseAdapter):
    """:class:`WarehouseAdapter` for Databricks SQL profiles.

    Issue #224 (US-003) lands the first real warehouse I/O: :meth:`sample_rows`
    (deterministic inline-predicate hash-mod), :meth:`get_row_count`
    (``SELECT COUNT(*)``), and the shared fail-loud :meth:`_resolve_sample_bucket`
    sizing, all on a connection wired via :meth:`_get_connection` with a fail-soft
    ``__exit__`` cleanup. US-004 adds :meth:`materialise_sample` (session-scoped
    qualified ``CREATE TEMPORARY TABLE``) and :meth:`run_test_sql` (``COUNT(*)``
    failing-rows wrap + per-row ``to_json`` capture). US-005 adds
    :meth:`column_stats` (single aggregate-only profiling query) — shipped AHEAD
    of Snowflake, whose parity is tracked as issue #258. :meth:`estimate_query_bytes` /
    :meth:`run_stats_query` inherit their typed ``*NotSupportedError`` degrade
    until #225 lands.
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

    # ------------------------------------------------------------------
    # get_row_count + sizing + sample_rows — DEC-002 / DEC-003 / DEC-008
    # of issue #224.
    # ------------------------------------------------------------------

    @staticmethod
    def _fold(identifier: str) -> str:
        """Case-fold one identifier per :attr:`Dialect.identifier_case` (DEC-008).

        Mirrors the prune compiler's ``_fold_identifier`` (and the Snowflake
        adapter's ``_fold``) so the adapter and the compiler quote identifiers
        byte-identically — load-bearing for the ``materialised`` strategy (the
        temp table the adapter CREATEs and the compiler REFERENCEs must fold to
        the same case, since quoted identifiers are case-sensitive). Databricks
        folds unquoted identifiers to **lowercase** (Unity Catalog), the
        opposite of Snowflake's ``"upper"``. Folds an already-validated ASCII
        identifier (validated on :class:`TableRef` / :class:`PartitionFilter`
        construction), so it cannot introduce a quote-breaking character.
        """
        case = DATABRICKS_DIALECT.identifier_case
        if case == "upper":
            return identifier.upper()
        if case == "lower":
            return identifier.lower()
        return identifier

    def _quote_identifier(self, identifier: str) -> str:
        """Fold-then-quote ONE identifier with the dialect's backtick (DEC-008)."""
        qc = DATABRICKS_DIALECT.quote_char
        return f"{qc}{self._fold(identifier)}{qc}"

    def _quote(self, ref: TableRef) -> str:
        """Render a fully-qualified Databricks table identifier (DEC-008).

        Unity Catalog three-part names are quoted **per component**
        (`` `catalog`.`schema`.`table` ``) because
        :attr:`Dialect.quote_qualified_per_component` is ``True`` — a single
        backtick-quoted string spanning dots would read as ONE literal
        identifier named ``catalog.schema.table``. Two-part `` `schema`.`table` ``
        when ``project`` is ``None``.

        **Fold-then-quote, identical to the prune compiler's ``_quote`` /
        ``_fold_identifier``.** Each component is case-folded
        (``identifier_case='lower'`` for Databricks) BEFORE the backticks are
        added, so a conventionally-cased manifest identifier resolves against the
        real Unity Catalog object and the temp table the adapter CREATEs in
        :meth:`materialise_sample` matches the name the compiler REFERENCEs (the
        #124 lesson — CREATE-vs-REFERENCE must never diverge).
        """
        components = (
            [ref.dataset, ref.name] if ref.project is None else [ref.project, ref.dataset, ref.name]
        )
        return ".".join(self._quote_identifier(c) for c in components)

    def _render_partition_filter(self, pf: PartitionFilter) -> str:
        """Render a :class:`PartitionFilter` to a Databricks SQL fragment (DEC-008).

        ``datetime`` → ``TIMESTAMP '…'``; ``date`` → ``DATE '…'`` (via the
        dialect literal templates — Spark typed-literal form); ``str`` is escaped
        via :func:`escape_bq_string_literal` for safe inclusion inside a
        single-quoted literal. The column name is fold-then-quoted (per-component
        backtick) and already validated on :class:`PartitionFilter` construction.

        Mirrors the Snowflake adapter's ``_render_partition_filter`` and the
        prune compiler's ``_render_partition_filter(pf, dialect)`` — the
        partition predicate this method renders matches what the compiler emits
        for the deterministic sample CTE.
        """
        # ``datetime`` is a subclass of ``date``, so check it first.
        if isinstance(pf.value, datetime):
            rendered = DATABRICKS_DIALECT.timestamp_literal_template.format(
                value=pf.value.isoformat()
            )
        elif isinstance(pf.value, date):
            rendered = DATABRICKS_DIALECT.date_literal_template.format(value=pf.value.isoformat())
        else:
            from signalforge.warehouse._sql_safety import escape_bq_string_literal

            rendered = f"'{escape_bq_string_literal(str(pf.value))}'"
        return f"{self._quote_identifier(pf.column)} {pf.op} {rendered}"

    def _execute(self, sql: str, *, table: TableRef | None = None) -> list[Any]:
        """Run ``sql`` on the connection's cursor, returning ``fetchall()``.

        Any SDK exception is routed through
        :func:`signalforge.warehouse.adapters._databricks_client.map_databricks_exception`
        (DEC-009): a mapped typed error is re-raised ``from`` the original; an
        unchanged passthrough re-raises the original. ``table`` (when supplied)
        gives the mapper the ``table`` identifier for the Table/Column arms.
        """
        from signalforge.warehouse.adapters._databricks_client import map_databricks_exception

        context = {"table": table.qualified_name} if table is not None else None
        cursor = self._get_connection().cursor()
        try:
            cursor.execute(sql)
            return list(cursor.fetchall())
        except Exception as exc:
            mapped = map_databricks_exception(exc, context=context)
            if mapped is exc:
                raise
            raise mapped from exc
        finally:
            # Release the server-side cursor handle on both the success and
            # failure paths so repeated queries on the long-lived connection
            # don't leak cursors.
            cursor.close()

    def _execute_to_dicts(self, sql: str, *, table: TableRef | None = None) -> list[dict[str, Any]]:
        """Run ``sql`` and shape tuple ``fetchall()`` rows into dicts (DEC-002).

        Reads ``cursor.description`` (DB-API: each descriptor's ``[0]`` is the
        column name) so the adapter builds ``dict`` rows from tuple results
        without depending on a dict-cursor. SDK errors route through
        :func:`map_databricks_exception` (DEC-009).
        """
        from signalforge.warehouse.adapters._databricks_client import map_databricks_exception

        context = {"table": table.qualified_name} if table is not None else None
        cursor = self._get_connection().cursor()
        try:
            try:
                cursor.execute(sql)
                rows = list(cursor.fetchall())
            except Exception as exc:
                mapped = map_databricks_exception(exc, context=context)
                if mapped is exc:
                    raise
                raise mapped from exc
            # _rows_to_dicts reads cursor.description, so shape the rows BEFORE
            # the finally closes the cursor.
            return self._rows_to_dicts(cursor, rows)
        finally:
            cursor.close()

    @staticmethod
    def _rows_to_dicts(cursor: _DatabricksCursorProtocol, rows: list[Any]) -> list[dict[str, Any]]:
        """Build dict rows from tuple ``fetchall()`` results via ``description``.

        Each DB-API descriptor's element ``[0]`` is the column name. A row that
        is already a mapping passes through unchanged (defensive against a
        dict-cursor-style connection). Mirrors the Snowflake adapter's
        ``_rows_to_dicts``.
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

    def get_row_count(self, table: TableRef) -> int | None:
        """Return the row count for ``table`` via ``SELECT COUNT(*)``, or ``None``
        when it cannot be determined (DEC-003).

        Overrides the ABC default (which raises
        :class:`RowCountNotSupportedError`). This is the seam
        :func:`signalforge.prune.engine._resolve_sample_bucket` calls to size the
        deterministic-sample bucket under ``prune.scope: sample``.

        **Why ``COUNT(*)`` and not metadata** (DEC-003): ``DESCRIBE DETAIL`` has
        no reliable top-level ``numRows`` (it lives in the Delta ``statistics``
        map, populated only after ``ANALYZE TABLE COMPUTE STATISTICS`` →
        commonly NULL/stale), and ``information_schema.tables`` carries no
        ``row_count``. ``SELECT COUNT(*)`` is the reliable source — metadata-only
        / cheap on Delta tables.

        Any :class:`WarehouseError` (a non-countable target, a mapped SDK error)
        returns ``None`` so the shared :meth:`_resolve_sample_bucket` fail-loud
        sizing decides what to do (unknown + no filter → fail loud; unknown +
        filter → ``bucket=1000``). A non-:class:`WarehouseError` (an unmapped
        transient blip) propagates unchanged.
        """
        sql = f"SELECT COUNT(*) AS row_count FROM {self._quote(table)}"
        try:
            rows = self._execute(sql, table=table)
        except WarehouseError:
            return None
        if not rows:
            return None
        first = rows[0]
        if isinstance(first, dict):
            value = next(iter(first.values()), None)
        elif isinstance(first, (list, tuple)):
            value = first[0]
        else:
            value = first
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
        """Size the deterministic-sample bucket via the fail-loud sizing pathway
        (DEC-003), mirroring the Snowflake / BigQuery adapters exactly:

        * row count unknown + no ``partition_filter`` →
          :class:`UnknownTableSizeError`.
        * row count unknown + ``partition_filter`` present → ``bucket = 1000``
          (DEBUG-logged fallback).
        * row count ``>= _LARGE_TABLE_THRESHOLD`` + no ``partition_filter`` →
          :class:`SamplingRequiresPartitionFilterError`.
        * else → ``bucket = max(num_rows // n, 1)``.

        Row count comes from :meth:`get_row_count` (``SELECT COUNT(*)``), which
        returns ``None`` on a :class:`WarehouseError`; ``None`` and ``0`` route
        through the same unknown-size pathway (mirrors BigQuery's
        ``num_rows == 0`` branch).
        """
        num_rows = self.get_row_count(table)
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

    def sample_rows(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
    ) -> list[dict[str, Any]]:
        """Sample up to ``n`` rows deterministically (DEC-002 / DEC-003 / DEC-008).

        Algorithm (mirrors the Snowflake / BigQuery ``sample_rows``,
        Databricks-flavoured):

        1. Reject ``n <= 0`` with :class:`ValueError` before any warehouse
           contact.
        2. Size the bucket via :meth:`_resolve_sample_bucket` (``SELECT
           COUNT(*)`` + the shared fail-loud sizing).
        3. Emit the deterministic sample SELECT via the shared
           :func:`signalforge.warehouse._sample_sql.render_sample_select` helper
           (``order_by_hash=True``). For Databricks
           (``sample_hash_in_projection=False``) this is the **inline-predicate**
           shape — Databricks has NO Snowflake-style ``HASH(*)`` predicate
           restriction, so ``xxhash64(...)`` and the masked ``MOD(...)`` are
           legal directly in ``WHERE``/``ORDER BY``::

               SELECT * FROM `cat`.`sch`.`tbl` AS t
               WHERE MOD((xxhash64(to_json(struct(*))) & 9223372036854775807), <bucket>) < 1
                 [AND <partition_filter>]
               ORDER BY (xxhash64(to_json(struct(*))) & 9223372036854775807)
               LIMIT n

        The hash-mod approach is deterministic across runs (same input → same
        prune decision) and works on views / CTEs where ``TABLESAMPLE`` does not.
        The hash expression and sample SHAPE are read from
        :data:`DATABRICKS_DIALECT` (NEVER hard-coded) so the adapter's sample SQL
        is byte-consistent with the prune compiler's sample CTE (Architectural
        Commitment #5). The ``ORDER BY`` makes ``LIMIT`` truncation deterministic.
        The partition filter is rendered by the adapter's own
        :meth:`_render_partition_filter` and passed to the helper as
        ``extra_where``.
        """
        if n <= 0:
            raise ValueError(f"sample_rows requires n > 0; got n={n}")

        bucket = self._resolve_sample_bucket(table, n, partition_filter=partition_filter)

        quoted = self._quote(table)
        extra_where = (
            self._render_partition_filter(partition_filter)
            if partition_filter is not None
            else None
        )
        sql = render_sample_select(
            quoted,
            dialect=DATABRICKS_DIALECT,
            sample_bucket=bucket,
            sample_size=n,
            extra_where=extra_where,
            order_by_hash=True,
        )

        return self._execute_to_dicts(sql, table=table)

    # ------------------------------------------------------------------
    # materialise_sample — DEC-004 / DEC-006 / DEC-008 of issue #224.
    # ------------------------------------------------------------------

    def materialise_sample(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
        ttl_seconds: int = 3600,
    ) -> TableRef:
        """Materialise a deterministic sample into a session-scoped Databricks
        ``TEMPORARY TABLE``; return a :class:`TableRef` pointing at it (DEC-004).

        Overrides the ABC default (which raises
        :class:`MaterialisationNotSupportedError`). Mirrors the Snowflake
        adapter's :meth:`materialise_sample` (issue #122) verbatim, Databricks-
        flavoured:

        DEC-008 — the ``run_id`` reuses the shared
        :func:`signalforge.warehouse._sample_id._compute_run_id` recipe so the
        temp-table name ``_sf_sample_<run_id>`` is byte-identical to the
        BigQuery / Snowflake adapters' for the same ``(table, n,
        partition_filter)`` tuple under the same ``signalforge.__version__``.

        DEC-006 — the temp table is created on the live connection's session
        (the connection embodies the session that scopes the temp table). The
        connection is pinned as ``self._active_session`` (via
        :meth:`_get_connection`) so a subsequent :meth:`run_test_sql` on the
        same connection reaches the temp table.

        DEC-004 — the deterministic sample SELECT body is built by the shared
        :func:`signalforge.warehouse._sample_sql.render_sample_select` helper
        (``order_by_hash=True``), which for Databricks
        (``sample_hash_in_projection=False``) emits the inline-predicate shape
        (Databricks has NO Snowflake-style ``HASH(*)`` predicate restriction).
        The hash expression and shape are read from :data:`DATABRICKS_DIALECT`
        (NOT hard-coded) so the CTAS bytes stay consistent with
        :meth:`sample_rows` and the prune compiler's sample CTE (Architectural
        Commitment #5). ``partition_filter`` lands ONCE here, in the CTAS
        ``WHERE`` (rendered by :meth:`_render_partition_filter`, passed to the
        helper as ``extra_where``).

        The ``TEMP TABLE`` is colocated with the SOURCE (created as
        ``<source catalog>.<source schema>._sf_sample_<run_id>``, per-component
        backtick-quoted + fold-to-lower, identical to how the compiler will
        REFERENCE it) and the returned :class:`TableRef` is fully-qualified via
        the source catalog / schema.

        .. note::

            Whether Databricks accepts a **qualified** temporary-table name in
            ``CREATE TEMPORARY TABLE <cat>.<sch>.<temp> AS ...`` AND whether the
            ``databricks-sql-connector`` persists the session across queries (so
            the temp table is reachable from a follow-up
            :meth:`run_test_sql`) are **#226 live-cert items** — this story
            certifies SHAPE only (fakes + the ungated sqlglot parse-guard), NOT
            live validity. The documented fallback if rejected live is a
            bare-name temp table or ``CREATE OR REPLACE TABLE`` in the
            configured schema + an explicit ``DROP`` (plan DEC-004).

        Args:
            table: Source production table to sample from.
            n: Target sample size; bucket sizing mirrors :meth:`sample_rows`
                (deterministic hash-mod, fail-loud size guards).
            partition_filter: Optional :class:`PartitionFilter` applied ONCE
                inside the CTAS ``WHERE`` clause.
            ttl_seconds: accepted for ABC parity but IGNORED by Databricks —
                there is no client-side TTL knob; the connection's session-local
                temp objects are reaped server-side when the warehouse drops the
                connection (mirrors Snowflake; the cleanup WARNING quotes no
                countdown).

        Returns:
            :class:`TableRef` with ``project=table.project``,
            ``dataset=table.dataset``, ``name="_sf_sample_<run_id>"`` — the
            session-scoped temp table, fully-qualified via the source catalog /
            schema.

        Raises:
            ValueError: ``n <= 0``.
            MaterialisationFailedError: any SDK / network / quota failure during
                the CTAS (wraps the original via ``cause=``).
            UnknownTableSizeError, SamplingRequiresPartitionFilterError:
                propagated from :meth:`_resolve_sample_bucket` (the shared
                fail-loud sizing contract).
        """
        if n <= 0:
            raise ValueError(f"materialise_sample requires n > 0; got n={n}")

        # DEC-008 — byte-identical recipe to BigQuery / Snowflake (shared helper).
        run_id = _compute_run_id(table=table, n=n, partition_filter=partition_filter)
        temp_name = f"_sf_sample_{run_id}"
        # The temp-table identifier MUST pass validate_identifier before
        # quoting. blake2b-8 lowercase hex is alphanumeric so the regex always
        # passes; the explicit call documents the contract and catches any
        # future drift in _compute_run_id.
        validate_identifier("temp_table_name", temp_name)

        # Shared fail-loud sizing pathway (same as sample_rows; DEC-003).
        bucket = self._resolve_sample_bucket(table, n, partition_filter=partition_filter)

        quoted_source = self._quote(table)
        # The TEMP TABLE is colocated with the source (DEC-004): same catalog /
        # schema, per-component fold-then-quote, with the deterministic temp name.
        temp_ref = TableRef(project=table.project, dataset=table.dataset, name=temp_name)
        quoted_temp = self._quote(temp_ref)

        extra_where = (
            self._render_partition_filter(partition_filter)
            if partition_filter is not None
            else None
        )
        # Inline-predicate sample body (DEC-002): the masked xxhash64 expression
        # sits directly in WHERE / ORDER BY (Databricks has no HASH(*)
        # predicate restriction). Read from the dialect, not hard-coded.
        select_body = render_sample_select(
            quoted_source,
            dialect=DATABRICKS_DIALECT,
            sample_bucket=bucket,
            sample_size=n,
            extra_where=extra_where,
            order_by_hash=True,
        )
        sql = f"CREATE TEMPORARY TABLE {quoted_temp} AS {select_body}"

        # Open / reuse the connection (also sets self._active_session) so the
        # follow-up run_test_sql reaches the temp table (DEC-006).
        conn = self._get_connection()
        from signalforge.warehouse.adapters._databricks_client import map_databricks_exception

        cursor = conn.cursor()
        try:
            cursor.execute(sql)
        except Exception as exc:
            # DEC-009 / DEC-004 — route the SDK failure through the Databricks
            # exception mapper first, then wrap in the typed
            # MaterialisationFailedError (mirrors Snowflake / BigQuery). The
            # raise-from chain preserves the original SDK exception via
            # __cause__.
            mapped = map_databricks_exception(exc, context={"table": table.qualified_name})
            cause: BaseException = mapped if mapped is not exc else exc
            raise MaterialisationFailedError(
                message=f"sample materialisation failed for {table.qualified_name}: {cause}",
                cause=cause,
            ) from exc
        finally:
            # Release the cursor handle (the temp table lives on the pinned
            # connection/session, not the cursor, so it stays reachable).
            cursor.close()

        # INFO log uses the HASHED session id, never the raw value. Lazy-format
        # JSON for ANSI safety (warehouse-layer convention).
        raw_session_id = self._read_session_id(conn)
        payload: dict[str, Any] = {
            "table": table.qualified_name,
            "sample_rows": n,
            "run_id": run_id,
        }
        if raw_session_id is not None:
            payload["session_id_hash"] = _hash_session_id(raw_session_id)
        _LOGGER.info("materialised sample: %s", json.dumps(payload))

        return temp_ref

    def column_stats(self, table: TableRef, column: str) -> ColumnStats:
        """Return an aggregate profile for one column (DEC-011 of issue #224).

        Overrides the v0.x ``NotImplementedError`` stub. Databricks implements
        ``column_stats`` AHEAD of Snowflake (which stubs it); the Snowflake-parity
        decision is tracked as GitHub issue #258.

        A SINGLE aggregate query over the fold-then-quoted table computes the
        :class:`ColumnStats` contract for the fold-then-quoted column — mirroring
        :meth:`BigQueryAdapter.column_stats`'s semantics in Spark SQL:

        * ``count`` — ``COUNT(<col>)`` (NON-null count, matching BigQuery).
        * ``distinct`` — ``COUNT(DISTINCT <col>)``.
        * ``nulls`` — ``COUNT_IF(<col> IS NULL)`` (Spark's ``COUNTIF`` analogue).
        * ``min`` / ``max`` — ``MIN(<col>)`` / ``MAX(<col>)``. **Known
          divergence from BigQuery (DEC-011 follow-up):** BigQuery skips MIN/MAX
          and sets ``min = max = None`` for complex types (ARRAY / STRUCT / MAP /
          JSON / BINARY / GEOGRAPHY), per the :class:`ColumnStats` contract. This
          adapter emits MIN/MAX unconditionally because ``data_type`` is derived
          inline (``typeof``) in the same single aggregate, so the column's type
          is not known before the query is built. On a complex column Spark
          either raises (mapped → :class:`QuerySyntaxError`) or returns a
          non-scalar; honouring the skip-for-complex contract needs a type
          pre-fetch and is a **#226 live-cert item** (see the note below).
        * ``data_type`` — ``MAX(typeof(<col>))`` (Spark's DDL type string; an
          empty table yields ``NULL`` → coerced to ``""``, matching BigQuery's
          "type unknown → empty string" precedent).

        Unlike BigQuery (DEC-008/DEC-025), there is NO context-manager batching:
        the call runs its own single round-trip on
        :meth:`_get_connection`'s connection, mirroring this adapter's
        :meth:`sample_rows` / :meth:`run_test_sql` / :meth:`get_row_count`. The
        column is validated by
        :func:`signalforge.warehouse._sql_safety.validate_identifier` before it
        reaches the SQL string (DEC-013); SDK errors route through
        :func:`map_databricks_exception` (via :meth:`_execute_to_dicts`).

        .. note::

            Real-Spark ``typeof`` / ``MIN`` / ``MAX`` semantics against a live
            Unity Catalog table are a **#226 live-cert item** — certified here
            against the fake + the ``sqlglot`` parse-guard only. The
            complex-type MIN/MAX divergence noted above (BigQuery skips MIN/MAX
            for ARRAY / STRUCT / MAP / JSON / BINARY / GEOGRAPHY; this adapter
            emits them unconditionally) is part of that #226 live cert — the
            scalar-column path is the supported surface for v0.x.
        """
        validate_identifier("column", column)

        quoted_col = self._quote_identifier(column)
        sql = (
            f"SELECT COUNT({quoted_col}) AS non_null_count, "
            f"COUNT(DISTINCT {quoted_col}) AS distinct_count, "
            f"COUNT_IF({quoted_col} IS NULL) AS null_count, "
            # MIN/MAX are emitted unconditionally; complex-typed columns
            # (ARRAY/STRUCT/MAP/JSON/BINARY/GEOGRAPHY) diverge from BigQuery's
            # skip-and-None contract — a #226 live-cert item (see docstring).
            f"MIN({quoted_col}) AS min_value, "
            f"MAX({quoted_col}) AS max_value, "
            f"MAX(typeof({quoted_col})) AS data_type "
            f"FROM {self._quote(table)}"
        )

        rows = self._execute_to_dicts(sql, table=table)
        if not rows:  # pragma: no cover - aggregate always returns one row
            raise RuntimeError(f"column_stats aggregate returned no rows for table {table}")

        # Databricks folds the unquoted aliases to lower, but resolve
        # case-insensitively so a fake / dict-cursor that preserved case works too.
        lowered = {str(k).lower(): v for k, v in rows[0].items()}
        raw_type = lowered.get("data_type")
        return ColumnStats(
            count=int(lowered["non_null_count"]),
            distinct=int(lowered["distinct_count"]),
            nulls=int(lowered["null_count"]),
            min=lowered.get("min_value"),
            max=lowered.get("max_value"),
            data_type=str(raw_type) if raw_type is not None else "",
        )

    # ------------------------------------------------------------------
    # run_test_sql — DEC-007 of issue #224.
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_failure_row(row: dict[str, Any]) -> dict[str, Any]:
        """Parse one captured failing row (DEC-007).

        Each captured row carries a single ``to_json(struct(*))`` column whose
        value is a JSON **string** (Spark's ``to_json`` returns a string per
        row — NOT a VARIANT / array like Snowflake's ``OBJECT_CONSTRUCT``, so
        each value is ``json.loads``-ed individually, never a single outer
        decode). A connection that already vended a parsed mapping (a fake / a
        dict-cursor) passes through via :func:`dict`.
        """
        value: Any = next(iter(row.values()), None)
        parsed: Any = json.loads(value) if isinstance(value, str) else value
        return dict(parsed)

    def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> TestResult:
        """Run a candidate failing-rows SELECT and return a typed
        :class:`TestResult` (DEC-007).

        Overrides the :class:`NotImplementedError` stub. The candidate is
        sanity-checked by
        :func:`signalforge.warehouse._sql_safety.validate_test_sql` (no ``;``,
        no ``--`` comments, balanced parens) before wrapping.

        The failing-row ``COUNT(*)`` is always computed via
        ``SELECT COUNT(*) AS failures FROM (<sql>) AS t``. When
        ``capture_failures > 0`` a SECOND query captures up to
        ``capture_failures`` example failing rows via Spark's
        ``SELECT to_json(struct(*)) AS failure_row FROM (<sql>) AS s LIMIT
        <capture_failures>`` — per-row ``to_json`` (NOT ``collect_list`` →
        ``ARRAY<STRING>``), so each row's JSON string is ``json.loads``-ed
        individually (DEC-007). Columns resolve case-insensitively via
        ``cursor.description`` (Databricks folds unquoted aliases to lower).

        Both queries execute on ``self._active_session`` (the connection
        :meth:`_get_connection` returns / a prior :meth:`materialise_sample`
        pinned), so a materialised temp table is reachable. SDK errors route
        through :func:`map_databricks_exception` (via :meth:`_execute_to_dicts`);
        ``row_schema`` is ``None`` in v0.x (mirrors BigQuery / Snowflake).

        .. note::

            The ``to_json(struct(*))`` per-row capture shape (and the JSON-
            string marshalling assumption) is a **#226 live-cert item** —
            certified here against the fake + sqlglot parse only, not live.
        """
        validate_test_sql(sql)

        count_rows = self._execute_to_dicts(f"SELECT COUNT(*) AS failures FROM ({sql}) AS t")
        if not count_rows:  # pragma: no cover - aggregate always returns one row
            raise RuntimeError("run_test_sql COUNT wrapper returned no rows")
        # Databricks folds the unquoted ``failures`` alias to lower, but resolve
        # case-insensitively so a fake / dict-cursor that preserved case works too.
        lowered = {str(k).lower(): v for k, v in count_rows[0].items()}
        failure_count = int(lowered["failures"])

        sample_failures: list[dict[str, Any]] | None
        if capture_failures > 0:
            capture_rows = self._execute_to_dicts(
                f"SELECT to_json(struct(*)) AS failure_row "
                f"FROM ({sql}) AS s LIMIT {capture_failures}"
            )
            sample_failures = [self._parse_failure_row(r) for r in capture_rows]
        else:
            sample_failures = None

        return TestResult(
            passed=(failure_count == 0),
            failure_count=failure_count,
            sample_failures=sample_failures,
            row_schema=None,
        )


__all__ = ["DATABRICKS_DIALECT", "DatabricksAdapter"]
