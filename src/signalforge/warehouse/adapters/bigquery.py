"""BigQuery adapter — full v0.1 implementation (US-008).

Replaces the US-006 stub. Implements every abstract method on
:class:`signalforge.warehouse.base.WarehouseAdapter` against
``google-cloud-bigquery`` (or any duck-typed compatible client — see
:mod:`signalforge.warehouse.adapters._client`).

Operationalised design decisions
--------------------------------

* **DEC-006 / DEC-024** — :meth:`sample_rows` uses the deterministic
  ``MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), bucket) < 1`` hash-mod
  pattern with the bucket sized from ``Table.num_rows``. Fails loud
  (:class:`UnknownTableSizeError`,
  :class:`SamplingRequiresPartitionFilterError`) rather than scan TBs of
  un-partitioned data when sizing information is missing or the table is
  larger than :data:`_LARGE_TABLE_THRESHOLD`.
* **DEC-007** — :meth:`run_test_sql` wraps the candidate SQL in a
  ``COUNT(*) [+ ARRAY_AGG]`` aggregate and returns a typed
  :class:`signalforge.warehouse.models.TestResult`.
* **DEC-008 / DEC-025** — :meth:`column_stats` requires an active context
  manager; calls accumulate per-table and the first read flushes a single
  batched aggregate query. Outside ``with``, raises :class:`RuntimeError`.
* **DEC-013** — column / SQL inputs are validated by
  :mod:`signalforge.warehouse._sql_safety` before they reach a SQL string.
* **DEC-014** — :meth:`_render_partition_filter` formats the
  :class:`PartitionFilter` value by Python type
  (``date``/``datetime``/``str``).
* **DEC-015** — every ``QueryJobConfig`` originates in
  :meth:`_default_job_config` and carries
  ``use_query_cache=False``,
  ``maximum_bytes_billed=<adapter limit>``, and
  ``labels={"signalforge_stage": …, "signalforge_version": …}``.
* **DEC-016** — complex BigQuery types (``GEOGRAPHY``, ``JSON``, ``BYTES``,
  ``ARRAY<…>``, ``STRUCT<…>``, ``RANGE<…>``) skip ``MIN/MAX`` in the
  emitted SQL and surface ``min=max=None`` on the returned
  :class:`ColumnStats`.
* **DEC-022** — :meth:`__repr__` renders only ``project`` and
  ``location``; never the underlying client or any credential.
* **DEC-023** — :meth:`column_stats` emits a ``WARNING`` when the queued
  batch for one table exceeds :data:`_COLUMN_BATCH_WARN_AT`.
* **DEC-027** — ``project=None`` on a :class:`TableRef` is resolved to
  the underlying client's billing project at quote time.

All ``# pyright: ignore[...]`` noise is confined to
:mod:`signalforge.warehouse.adapters._client`; this module imports the
shim's typed surface and is otherwise pyright-clean.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from typing import Any

from signalforge.warehouse._sample_id import _compute_run_id, _hash_session_id
from signalforge.warehouse._sql_safety import validate_identifier, validate_test_sql
from signalforge.warehouse.adapters._client import (
    _BQClientProtocol,
    _make_dry_run_job_config,
    _make_query_job_config,
    make_real_client,
    map_bq_exception,
    row_to_dict,
)
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import (
    MaterialisationFailedError,
    SamplingRequiresPartitionFilterError,
    UnknownTableSizeError,
)
from signalforge.warehouse.models import (
    BIGQUERY_DIALECT,
    ColumnStats,
    Dialect,
    PartitionFilter,
    TableRef,
    TestResult,
)

_LOGGER = logging.getLogger("signalforge.warehouse")

# Module-level alias so tests can reassign to a deterministic stand-in
# (mirrors prune-engine.md DEC-019 / llm-drafter.md DEC-004 — never
# monkey-patch ``time.monotonic`` globally). Used by materialise_sample +
# __exit__ to drive the DEC-014 ``auto-expire in <N>s`` text.
_monotonic = time.monotonic


# ---------------------------------------------------------------------------
# Module-level tunables (patchable by tests).
# ---------------------------------------------------------------------------

_LARGE_TABLE_THRESHOLD: int = 100_000_000
"""DEC-024 fail-loud threshold. ``sample_rows`` requires a
:class:`PartitionFilter` once ``Table.num_rows`` exceeds this value.
Module-level so tests can patch it down to 100 without a multi-hundred-MB
fixture."""

_COLUMN_BATCH_WARN_AT: int = 500
"""DEC-023 soft-warning threshold. Queued ``column_stats`` columns above
this count produce one ``WARNING`` per flush. Module-level so tests can
patch it down to (e.g.) 5 columns."""

_COMPLEX_BQ_TYPES: frozenset[str] = frozenset({"GEOGRAPHY", "JSON", "BYTES"})
"""DEC-016 scalar complex types where ``MIN``/``MAX`` is omitted. The
parametric complex types (``ARRAY<…>``, ``STRUCT<…>``, ``RANGE<…>``) are
detected by their type-name prefix in :func:`_is_complex_type`."""

_PARAMETRIC_COMPLEX_PREFIXES: frozenset[str] = frozenset({"ARRAY", "STRUCT", "RANGE"})


def _is_complex_type(bq_type: str) -> bool:
    """Return True for BQ types where MIN/MAX is not meaningful (DEC-016).

    Handles both the scalar complex types (``GEOGRAPHY``, ``JSON``,
    ``BYTES``) and the parametric ones (``ARRAY<…>``, ``STRUCT<…>``,
    ``RANGE<…>``); the prefix split keeps the check resilient against
    arbitrary nested-type bodies.
    """
    upper = bq_type.upper()
    if upper in _COMPLEX_BQ_TYPES:
        return True
    head = upper.split("<", 1)[0]
    return head in _PARAMETRIC_COMPLEX_PREFIXES


# ---------------------------------------------------------------------------
# BigQueryAdapter
# ---------------------------------------------------------------------------


class BigQueryAdapter(WarehouseAdapter):
    """Production BigQuery implementation of :class:`WarehouseAdapter`.

    Construct via :meth:`WarehouseAdapter.from_profile` for the CLI / prune
    layers; tests inject a :class:`_FakeBigQueryClient` via the ``client=``
    kwarg to exercise the adapter without a network round-trip.

    The adapter is a context manager (DEC-008, DEC-025): :meth:`column_stats`
    *requires* an active ``with`` block and flushes its batched aggregate
    query at first read. :meth:`sample_rows` works inside or outside a
    block; the cached ``Table`` metadata is reused inside.
    """

    def __init__(
        self,
        *,
        project: str | None = None,
        location: str | None = None,
        max_bytes_billed: int = 100_000_000,
        client: _BQClientProtocol | None = None,
    ) -> None:
        self._project = project
        self._location = location
        self._max_bytes_billed = max_bytes_billed
        # When ``client is None`` the real bigquery.Client is built lazily
        # at first use so importing the adapter never reaches out to ADC.
        self._client: _BQClientProtocol | None = client

        # Context-manager state. ``None`` outside an active ``with`` block;
        # populated to empty dicts on ``__enter__`` and reset on ``__exit__``.
        self._table_metadata_cache: dict[TableRef, Any] | None = None
        self._column_stats_pending: dict[TableRef, list[str]] | None = None
        self._column_stats_results: dict[TableRef, dict[str, ColumnStats]] | None = None

        # Session-state carrying the BigQuery session_id once
        # :meth:`materialise_sample` has run (DEC-002 of issue #22). Lives
        # outside the per-context cache because materialisation does NOT
        # require an active ``with`` block (the prune orchestrator owns
        # the lifecycle), but ``__exit__``'s DEC-013 cleanup wants to see
        # any session that opened during the block. ``None`` until the
        # first successful materialise; reset to ``None`` in ``__exit__``
        # after the abort. ``_session_started_at`` and
        # ``_session_ttl_seconds`` are the inputs the DEC-014 cleanup
        # WARNING uses to render the ``auto-expire in <N>s`` line.
        self._active_session_id: str | None = None
        self._session_started_at: float | None = None
        self._session_ttl_seconds: int | None = None

    # ------------------------------------------------------------------
    # __repr__ — DEC-022 redaction.
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        # DEC-022: project + location only. No client, no credentials.
        return f"<BigQueryAdapter project={self._project!r} location={self._location!r}>"

    # ------------------------------------------------------------------
    # Context manager — DEC-008 / DEC-025 batching state.
    # ------------------------------------------------------------------

    def __enter__(self) -> WarehouseAdapter:
        self._table_metadata_cache = {}
        self._column_stats_pending = {}
        self._column_stats_results = {}
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        tb: object,
    ) -> None:
        # Flush any still-pending column_stats batches so the results dict
        # is populated for any caller still holding a returned ColumnStats
        # reference. If we're already unwinding a user exception, swallow
        # any flush errors so we don't mask the original. Cache cleanup
        # runs in ``finally`` so a failing flush during a clean exit
        # cannot leave the adapter in a half-cleaned state — the next
        # ``with`` block must start from a known empty cache (DEC-025).
        try:
            pending = self._column_stats_pending or {}
            for table in list(pending.keys()):
                if not pending[table]:
                    continue
                try:
                    self._flush_column_stats_batch(table)
                except Exception:
                    if exc_type is None:
                        raise
        finally:
            self._table_metadata_cache = None
            self._column_stats_pending = None
            self._column_stats_results = None

        # DEC-013 / DEC-014 of issue #22 — best-effort BigQuery session
        # cleanup. Issues ``CALL BQ.ABORT_SESSION();`` inside the active
        # session via ``connection_properties``, swallows any failure,
        # and either INFO-logs success (with hashed session_id only —
        # DEC-003) or WARNING-logs the cleanup-failure remediation (with
        # the raw session_id — DEC-014, the deliberate exception). State
        # always resets in the inner ``finally`` so a subsequent
        # ``__exit__`` is a no-op.
        self._cleanup_active_session()

    def _cleanup_active_session(self) -> None:
        """DEC-013 / DEC-014 best-effort session cleanup.

        Splits out so the test surface can exercise the cleanup path
        without entering an actual ``with`` block. Idempotent: returns
        immediately when ``_active_session_id`` is ``None``.
        """
        session_id = self._active_session_id
        if session_id is None:
            return
        try:
            try:
                # Issue the abort inside the same session via
                # connection_properties. The query routes through
                # ``_default_job_config`` so ``use_query_cache=False`` +
                # the bytes cap apply to the abort too — preserves the
                # DEC-015 reproducibility / cost-attribution invariant.
                job = self._get_client().query(
                    "CALL BQ.ABORT_SESSION();",
                    job_config=self._default_job_config(
                        stage="warehouse_session_abort",
                        session_id=session_id,
                    ),
                )
                # ``.result()`` flushes the call so we observe any
                # server-side failure here rather than at process exit.
                list(job.result())
            except Exception as exc:  # noqa: BLE001 - DEC-014 swallows all
                # DEC-014 multi-line WARNING — raw session_id is the
                # deliberate exception to DEC-003 redaction. The bq
                # command is unconstructable without it, so a hash here
                # would defeat the remediation purpose.
                ttl_remaining = self._compute_ttl_remaining_seconds()
                _LOGGER.warning(
                    "BigQuery session cleanup failed; session will auto-expire "
                    "in %ds (BigQuery TTL).\n"
                    "  Session ID: %s\n"
                    "  Reason: %s\n"
                    "  To clean up immediately:\n"
                    "    bq query --connection_property=session_id=%s "
                    '--use_legacy_sql=false "CALL BQ.ABORT_SESSION();"',
                    ttl_remaining,
                    session_id,
                    type(exc).__name__,
                    session_id,
                )
            else:
                # Happy path — DEC-003 redacted INFO log. ``session_id_hash``
                # is the only session-correlating identifier on disk; the
                # raw session_id never leaves the adapter.
                _LOGGER.info(
                    "session closed: %s",
                    json.dumps(
                        {
                            "session_id_hash": _hash_session_id(session_id),
                            "ttl_remaining_seconds": self._compute_ttl_remaining_seconds(),
                        }
                    ),
                )
        finally:
            self._active_session_id = None
            self._session_started_at = None
            self._session_ttl_seconds = None

    def _compute_ttl_remaining_seconds(self) -> int:
        """DEC-014 ``auto-expire in <N>s`` floor.

        ``max(1, int(ttl_seconds - elapsed))``. Floor at 1 avoids
        ``auto-expire in 0s`` confusion: if the session has actually
        expired, the abort would have succeeded with "session not found"
        and the WARNING wouldn't fire.
        """
        ttl = self._session_ttl_seconds
        started_at = self._session_started_at
        if ttl is None or started_at is None:
            return 1
        elapsed = _monotonic() - started_at
        return max(1, int(ttl - elapsed))

    # ------------------------------------------------------------------
    # Public dialect surface.
    # ------------------------------------------------------------------

    def dialect(self) -> Dialect:
        return BIGQUERY_DIALECT

    # ------------------------------------------------------------------
    # Internal helpers — client resolution + SQL fragments.
    # ------------------------------------------------------------------

    def _get_client(self) -> _BQClientProtocol:
        """Return the active client, lazily building a real one on first use."""
        if self._client is None:
            self._client = make_real_client(self._project, self._location)
        return self._client

    def _default_job_config(
        self,
        *,
        stage: str,
        timeout_ms: int | None = None,
        create_session: bool = False,
        session_id: str | None = None,
    ) -> Any:
        """Build the DEC-015 job config for ``stage``.

        Always:

        * ``use_query_cache=False`` (reproducibility / explainable diffs).
        * ``maximum_bytes_billed`` set to the adapter's configured cap.
        * ``labels={"signalforge_stage": stage, "signalforge_version": …}``
          for v0.2 cost attribution.

        ``timeout_ms`` (DEC-013 of issue #6, AR-B2): when not ``None``,
        threaded through to :func:`_make_query_job_config` so the
        underlying ``QueryJobConfig.job_timeout_ms`` is set. Currently
        used only by tests; the prune layer (issue #6) reserves this
        for v0.2 budget enforcement and does NOT thread a non-None
        value through ``run_test_sql`` in v0.1. The warehouse adapter's
        own ``sample_rows`` / ``column_stats`` / ``run_test_sql`` paths
        leave it ``None``.

        ``create_session`` (DEC-002 of issue #22): when ``True``, the
        job opens a fresh BigQuery session. Used exclusively by
        :meth:`materialise_sample` so the temp-table CTAS lives inside
        a server-side session whose ``session_id`` the SDK exposes via
        ``job.session_info.session_id`` after ``.result()``.

        ``session_id`` (DEC-002 of issue #22): when not ``None``, the
        job routes into the named session via
        ``connection_properties=[ConnectionProperty(key="session_id",
        value=...)]`` so per-test failing-rows queries (and the
        ``CALL BQ.ABORT_SESSION()`` cleanup) can read the
        ``_SESSION._sf_sample_<run_id>`` temp table that
        :meth:`materialise_sample` produced.
        """
        from signalforge import __version__

        return _make_query_job_config(
            max_bytes_billed=self._max_bytes_billed,
            stage=stage,
            version=__version__,
            timeout_ms=timeout_ms,
            create_session=create_session,
            session_id=session_id,
        )

    def _quote(self, ref: TableRef) -> str:
        """Render a fully-qualified BigQuery identifier (DEC-013, DEC-027).

        ``project=None`` on the :class:`TableRef` resolves to the
        underlying client's billing project; the dataset and table names
        are already DEC-013-validated by :class:`TableRef`'s
        ``__post_init__``.
        """
        project = ref.project if ref.project is not None else self._get_client().project
        return f"`{project}.{ref.dataset}.{ref.name}`"

    def _render_partition_filter(self, pf: PartitionFilter) -> str:
        """Render a :class:`PartitionFilter` to a SQL fragment (DEC-014).

        ``datetime`` → ``TIMESTAMP('…')``; ``date`` → ``DATE('…')``;
        ``str`` is escaped via :func:`escape_bq_string_literal` for safe
        inclusion inside a single-quoted BigQuery string literal. The
        column name is already DEC-013-validated by
        :class:`PartitionFilter`'s ``__post_init__``.

        ``escape_bq_string_literal`` handles backslash, single-quote,
        newline, carriage return, tab, and NUL — the full set BigQuery
        either escapes inside single-quoted literals or rejects.
        """
        # ``datetime`` is a subclass of ``date``, so check it first.
        if isinstance(pf.value, datetime):
            rendered = f"TIMESTAMP('{pf.value.isoformat()}')"
        elif isinstance(pf.value, date):
            rendered = f"DATE('{pf.value.isoformat()}')"
        else:
            from signalforge.warehouse._sql_safety import escape_bq_string_literal

            rendered = f"'{escape_bq_string_literal(str(pf.value))}'"
        return f"`{pf.column}` {pf.op} {rendered}"

    def refresh_table_metadata(self, table: TableRef) -> None:
        """Drop any cached ``Table`` metadata for ``table``.

        Useful when an :class:`UnknownTableSizeError` was raised because
        the table's ``num_rows`` was stale (e.g. the table was just
        materialised) — call this and retry the failing operation
        without leaving and re-entering the ``with adapter:`` block.

        No-op outside an active context (when there is no cache to
        invalidate). Does not refetch — the next call to ``_get_table``
        will repopulate.
        """
        cache = self._table_metadata_cache
        if cache is not None:
            cache.pop(table, None)

    def _get_table(self, table: TableRef) -> Any:
        """Return ``Table`` metadata, caching inside an active context."""
        cache = self._table_metadata_cache
        if cache is not None and table in cache:
            return cache[table]
        try:
            meta = self._get_client().get_table(table.qualified_name)
        except Exception as exc:
            mapped = map_bq_exception(
                exc,
                context={
                    "max_bytes_billed": self._max_bytes_billed,
                    "table": table.qualified_name,
                },
            )
            if mapped is exc:
                raise
            raise mapped from exc
        if cache is not None:
            cache[table] = meta
        return meta

    # ------------------------------------------------------------------
    # sample_rows — DEC-006 / DEC-024.
    # ------------------------------------------------------------------

    def sample_rows(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
    ) -> list[dict[str, Any]]:
        """Sample up to ``n`` rows deterministically (DEC-006).

        Algorithm (DEC-024):

        1. Look up ``Table.num_rows`` (cached inside an active context).
        2. Decision:

           * ``num_rows`` unknown + no ``partition_filter`` →
             :class:`UnknownTableSizeError`.
           * ``num_rows`` unknown + ``partition_filter`` present →
             ``bucket = 1000``; debug-log the fallback.
           * ``num_rows >= _LARGE_TABLE_THRESHOLD`` + no
             ``partition_filter`` →
             :class:`SamplingRequiresPartitionFilterError`.
           * else → ``bucket = max(num_rows // n, 1)``.
        3. Issue
           ``SELECT * FROM <quoted> AS t WHERE MOD(ABS(FARM_FINGERPRINT(
           TO_JSON_STRING(t))), <bucket>) < 1 [AND <partition_filter>]
           LIMIT n``.

        The hash-mod approach is deterministic across runs (same input →
        same prune decision) and works on views, MVs, wildcard tables,
        and CTEs — places where ``TABLESAMPLE`` does not.
        """
        if n <= 0:
            raise ValueError(f"sample_rows requires n > 0; got n={n}")

        meta = self._get_table(table)
        num_rows: int | None = getattr(meta, "num_rows", None)

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

        quoted = self._quote(table)
        where_clauses = [
            f"MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), {bucket}) < 1",
        ]
        if partition_filter is not None:
            where_clauses.append(self._render_partition_filter(partition_filter))
        where_sql = " AND ".join(where_clauses)
        # ``ORDER BY FARM_FINGERPRINT(TO_JSON_STRING(t))`` makes ``LIMIT n``
        # truncation deterministic when the WHERE filter retains more than
        # ``n`` rows (DEC-006). Without it BigQuery's LIMIT picks an
        # arbitrary subset and breaks the same-input → same-output prune
        # contract.
        sql = (
            f"SELECT * FROM {quoted} AS t WHERE {where_sql} "
            f"ORDER BY FARM_FINGERPRINT(TO_JSON_STRING(t)) "
            f"LIMIT {n}"
        )

        try:
            job = self._get_client().query(
                sql, job_config=self._default_job_config(stage="warehouse_sample")
            )
            rows = list(job.result())
        except Exception as exc:
            mapped = map_bq_exception(
                exc,
                context={
                    "max_bytes_billed": self._max_bytes_billed,
                    "table": table.qualified_name,
                },
            )
            if mapped is exc:
                raise
            raise mapped from exc

        return [row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # materialise_sample — DEC-001 / DEC-002 / DEC-003 of issue #22.
    # ------------------------------------------------------------------

    def materialise_sample(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
        ttl_seconds: int = 3600,
    ) -> TableRef:
        """Materialise a deterministic sample into a BigQuery session
        temp table; return a :class:`TableRef` pointing at it.

        DEC-001 — Compiled-SQL determinism via seeded ``run_id``.
        ``run_id = blake2b-8(table.qualified_name + signalforge_version
        + n + canonical_json(partition_filter))`` (16 hex chars). The
        same ``(table, n, partition_filter)`` tuple under the same
        ``signalforge_version`` produces a byte-equal temp-table name
        — the prune compiler's ``compiled_sql_hash`` invariant holds
        because the per-test SQL references the deterministic temp
        table.

        DEC-002 — BigQuery session lifecycle. The first query
        (``CREATE TEMP TABLE _sf_sample_<run_id> AS SELECT ...``) runs
        with ``QueryJobConfig.create_session=True``. BigQuery assigns
        a server-side ``session_id``; the SDK exposes it on
        ``job.session_info.session_id`` once ``.result()`` completes.
        We capture that, store it on ``self._active_session_id``, and
        use ``connection_properties`` on every subsequent query
        (``run_test_sql``, the ``__exit__`` abort) to route into the
        same session so the ``_SESSION._sf_sample_<run_id>`` temp
        table is reachable.

        DEC-003 — Session-id redaction. The success INFO log emits
        ``session_id_hash = blake2b-4(session_id).hexdigest()`` (8 hex
        chars); the raw session_id never leaves the adapter except in
        the DEC-014 cleanup-failure WARNING.

        DEC-004 — ``ttl_seconds`` is OUR-side only: it never touches
        the BigQuery SDK. BigQuery enforces its own server-side
        session TTL. The kwarg drives the DEC-014 WARNING text's
        ``auto-expire in <N>s`` line if cleanup fails.

        Q5 — ``partition_filter`` lands in the materialisation
        ``WHERE`` clause once. Per-test queries against the temp
        table do NOT re-apply it (the materialised rows already
        survived the filter).

        Args:
            table: Source production table to sample from.
            n: Target sample size; bucket sizing mirrors
                :meth:`sample_rows` (deterministic hash-mod, fail-loud
                size guards).
            partition_filter: Optional :class:`PartitionFilter` applied
                ONCE inside the CTAS WHERE clause.
            ttl_seconds: OUR-side hint to the DEC-014 WARNING text
                only; not passed to BigQuery (DEC-013 / DEC-004).

        Returns:
            :class:`TableRef` with ``project=None``,
            ``dataset="_SESSION"``, ``name="_sf_sample_<run_id>"``.
            ``project=None`` is load-bearing: BigQuery rejects the
            three-part ``<project>._SESSION.<name>`` form even inside
            the owning session, so the rendered ``qualified_name`` is
            the two-part ``_SESSION._sf_sample_<run_id>``.

        Raises:
            MaterialisationFailedError: any SDK / network / quota
                failure during the CTAS query (wraps the original
                exception via ``cause=``).
            ValueError: ``n <= 0``.
            UnknownTableSizeError, SamplingRequiresPartitionFilterError:
                propagated from the underlying size-check pathway
                (mirrors :meth:`sample_rows`'s fail-loud sizing
                contract).
        """
        if n <= 0:
            raise ValueError(f"materialise_sample requires n > 0; got n={n}")

        run_id = _compute_run_id(table=table, n=n, partition_filter=partition_filter)
        temp_name = f"_sf_sample_{run_id}"
        # DEC-013 of #3 / C3 of #22 — the temp-table identifier MUST pass
        # validate_identifier before quoting. blake2b-8 lowercase hex is
        # always alphanumeric so the regex passes; the explicit call
        # documents the contract and catches any future drift in
        # _compute_run_id (e.g., if someone adds a hyphen separator).
        validate_identifier("temp_table_name", temp_name)

        # Mirror the sample_rows sizing pathway — same failure-loud
        # contract, same deterministic bucket. The compile-time bucket
        # for materialisation is sized off the source table's num_rows
        # so the produced sample is ~3-5x n before LIMIT.
        meta = self._get_table(table)
        num_rows: int | None = getattr(meta, "num_rows", None)
        if num_rows is None or num_rows == 0:
            if partition_filter is None:
                raise UnknownTableSizeError(table=table.qualified_name)
            bucket = 1000
        elif num_rows >= _LARGE_TABLE_THRESHOLD and partition_filter is None:
            raise SamplingRequiresPartitionFilterError(
                table=table.qualified_name, num_rows=num_rows
            )
        else:
            bucket = max(num_rows // n, 1)

        quoted_source = self._quote(table)
        where_clauses = [
            f"MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), {bucket}) < 1",
        ]
        if partition_filter is not None:
            where_clauses.append(self._render_partition_filter(partition_filter))
        where_sql = " AND ".join(where_clauses)
        # The deterministic predicate is byte-identical to
        # ``sample_rows`` (Architectural Commitment #5): same hash-mod
        # formula, same ORDER BY, same LIMIT shape. The CREATE TEMP
        # TABLE wraps it; per-test queries hit the materialised rows
        # column-pruned, so the per-test bytes drop to ~1 MB instead
        # of the ~10 GB the cost-probe AR-B1 measured.
        sql = (
            f"CREATE TEMP TABLE {temp_name} AS "
            f"SELECT * FROM {quoted_source} AS t "
            f"WHERE {where_sql} "
            f"ORDER BY FARM_FINGERPRINT(TO_JSON_STRING(t)) "
            f"LIMIT {n}"
        )

        started_at = _monotonic()
        try:
            job = self._get_client().query(
                sql,
                job_config=self._default_job_config(
                    stage="warehouse_sample_materialise",
                    create_session=True,
                ),
            )
            # ``.result()`` blocks until BigQuery completes the CTAS.
            # The SDK populates ``job.session_info`` only after the job
            # finishes successfully; capturing earlier would race the
            # server-side session assignment.
            list(job.result())
        except Exception as exc:
            # DEC-008 of #22 — wrap any SDK / network / quota failure
            # in a typed MaterialisationFailedError. Route the raw
            # exception through ``map_bq_exception`` first (matching
            # ``sample_rows`` / ``column_stats`` / ``run_test_sql``) so
            # the message and ``cause`` carry our stable warehouse
            # error surface instead of SDK/version-specific wording.
            # The raise-from chain still preserves the original SDK
            # exception via ``__cause__`` for post-mortem.
            mapped = map_bq_exception(
                exc,
                context={
                    "max_bytes_billed": self._max_bytes_billed,
                    "table": table.qualified_name,
                },
            )
            cause: BaseException = mapped if mapped is not exc else exc
            raise MaterialisationFailedError(
                message=f"sample materialisation failed for {table.qualified_name}: {cause}",
                cause=cause,
            ) from exc

        session_info = getattr(job, "session_info", None)
        session_id: str | None = (
            getattr(session_info, "session_id", None) if session_info is not None else None
        )
        if session_id is None:
            # The SDK contract guarantees session_info on a
            # create_session=True job that completed successfully. A
            # missing session_id is a real protocol violation — fail
            # loud rather than ship an unreachable temp table.
            raise MaterialisationFailedError(
                message=(
                    f"BigQuery did not return a session_id for the materialise job "
                    f"on {table.qualified_name}; the CTAS completed but the session "
                    "is unreachable for follow-up queries."
                ),
                cause=None,
            )

        # State lands AFTER the SDK confirmed both completion and
        # session minting — partial state on failure is what the
        # ``raise from`` paths above prevent.
        self._active_session_id = session_id
        self._session_started_at = started_at
        self._session_ttl_seconds = ttl_seconds

        duration_ms = int((_monotonic() - started_at) * 1000)
        # DEC-003 of #22 — INFO log uses session_id_hash, never raw.
        # Lazy-format JSON for ANSI safety (mirrors safety-layer DEC-022 /
        # llm-drafter DEC-011; the warehouse layer tracks the same
        # convention).
        _LOGGER.info(
            "materialised sample: %s",
            json.dumps(
                {
                    "table": table.qualified_name,
                    "sample_rows": n,
                    "session_id_hash": _hash_session_id(session_id),
                    "run_id": run_id,
                    "duration_ms": duration_ms,
                }
            ),
        )

        # Return a TableRef pointing at the session-scoped temp table.
        # ``_SESSION`` is BigQuery's pseudo-dataset for session-local
        # objects; per-test queries reference it as the two-part
        # ``_SESSION._sf_sample_<run_id>`` (no project prefix). BigQuery
        # rejects the three-part ``<project>._SESSION.<name>`` form even
        # inside the owning session ("Use of _SESSION is not allowed
        # here") — caught during the maintainer probe-run on 2026-05-08.
        # ``project=None`` makes ``TableRef.qualified_name`` render the
        # two-part form so the compiler's quoted reference is valid SQL.
        return TableRef(
            project=None,
            dataset="_SESSION",
            name=temp_name,
        )

    # ------------------------------------------------------------------
    # column_stats — DEC-008 / DEC-013 / DEC-016 / DEC-023 / DEC-025.
    # ------------------------------------------------------------------

    def column_stats(self, table: TableRef, column: str) -> ColumnStats:
        """Return per-column profile, batched per-table inside a ``with``.

        Public contract per DEC-008: one column at a time. Inside an
        active context, calls accumulate per-table and the first read of
        any returned :class:`ColumnStats` triggers a single batched
        aggregate query for every column queued for that table. Outside
        a context, raises :class:`RuntimeError` (DEC-025); ad-hoc users
        always know they're using the batching path.
        """
        validate_identifier("column", column)

        if self._column_stats_pending is None or self._column_stats_results is None:
            raise RuntimeError("column_stats must be called inside a `with adapter:` block")

        # If we already have the answer cached from a previous flush in
        # this ``with`` block, return it directly.
        cached = self._column_stats_results.get(table, {}).get(column)
        if cached is not None:
            return cached

        pending = self._column_stats_pending.setdefault(table, [])
        if column not in pending:
            pending.append(column)
        if len(pending) > _COLUMN_BATCH_WARN_AT:
            _LOGGER.warning(
                "Large column_stats batch: %d columns for %s; consider splitting",
                len(pending),
                table,
            )

        # The spec's "lazy" semantics here are simplified to "first call to
        # column_stats(table, X) flushes every column queued for `table`."
        # Subsequent calls in the same `with` block hit the cache above; if
        # the caller queues new columns after a flush, the next call will
        # flush them in turn. This is simpler than a ColumnStats proxy and
        # matches the documented "first stat access flushes" intent.
        self._flush_column_stats_batch(table)

        result = self._column_stats_results.get(table, {}).get(column)
        if result is None:  # pragma: no cover - defensive; flush populates this
            raise RuntimeError(f"column_stats internal error: {column!r} missing from flush result")
        return result

    def _flush_column_stats_batch(self, table: TableRef) -> None:
        """Issue the batched aggregate query for every column queued for ``table``.

        One round-trip per table per flush. Result rows are mapped back to
        per-column :class:`ColumnStats`; complex types (DEC-016) get
        ``min=max=None``.
        """
        if self._column_stats_pending is None or self._column_stats_results is None:
            return  # pragma: no cover - guarded by caller
        columns = list(self._column_stats_pending.get(table, []))
        if not columns:
            return

        # Pull the schema for type-aware MIN/MAX skipping.
        meta = self._get_table(table)
        schema_pairs = _normalise_schema(getattr(meta, "schema", []))
        type_by_column: dict[str, str] = {name: bq_type for name, bq_type in schema_pairs}

        select_fragments: list[str] = ["COUNT(*) AS row_count"]
        for col in columns:
            col_type = type_by_column.get(col, "")
            select_fragments.extend(
                [
                    f"COUNT(`{col}`) AS count_{col}",
                    f"COUNT(DISTINCT `{col}`) AS distinct_{col}",
                    f"COUNTIF(`{col}` IS NULL) AS nulls_{col}",
                ]
            )
            if not _is_complex_type(col_type):
                select_fragments.extend(
                    [
                        f"MIN(`{col}`) AS min_{col}",
                        f"MAX(`{col}`) AS max_{col}",
                    ]
                )

        sql = f"SELECT {', '.join(select_fragments)} FROM {self._quote(table)}"

        try:
            job = self._get_client().query(
                sql, job_config=self._default_job_config(stage="warehouse_stats")
            )
            rows = list(job.result())
        except Exception as exc:
            mapped = map_bq_exception(
                exc,
                context={
                    "max_bytes_billed": self._max_bytes_billed,
                    "table": table.qualified_name,
                },
            )
            if mapped is exc:
                raise
            raise mapped from exc

        if not rows:  # pragma: no cover - aggregate query always returns one row
            raise RuntimeError(f"column_stats aggregate returned no rows for table {table}")
        row_dict = row_to_dict(rows[0])

        results = self._column_stats_results.setdefault(table, {})
        for col in columns:
            col_type = type_by_column.get(col, "")
            is_complex = _is_complex_type(col_type)
            results[col] = ColumnStats(
                count=int(row_dict[f"count_{col}"]),
                distinct=int(row_dict[f"distinct_{col}"]),
                nulls=int(row_dict[f"nulls_{col}"]),
                min=None if is_complex else row_dict.get(f"min_{col}"),
                max=None if is_complex else row_dict.get(f"max_{col}"),
                data_type=col_type,
            )
        # Drain the pending list so a follow-up column_stats call can queue
        # a fresh batch without re-flushing the just-resolved columns.
        self._column_stats_pending[table] = []
        _LOGGER.debug("Flushed column_stats batch for %s: %s", table, columns)

    # ------------------------------------------------------------------
    # run_test_sql — DEC-007 / DEC-013 / DEC-015.
    # ------------------------------------------------------------------

    def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> TestResult:
        """Run a candidate test SQL and return a typed :class:`TestResult`.

        Wraps the candidate in a ``COUNT(*)`` aggregate (plus
        ``ARRAY_AGG`` of up to ``capture_failures`` example rows when
        requested). The candidate is sanity-checked by
        :func:`signalforge.warehouse._sql_safety.validate_test_sql` before
        wrapping (no ``;``, no ``--`` comments, balanced parens).

        ``row_schema`` is set to ``None`` in v0.1 — populating it requires
        a separate ``dry_run`` to extract the inner-row schema, which we
        defer to v0.2 once the explainable-diffs prune layer is hooked
        up.
        """
        validate_test_sql(sql)

        if capture_failures > 0:
            wrapped = (
                f"SELECT COUNT(*) AS failures, "
                f"ARRAY_AGG(t LIMIT {capture_failures}) AS samples "
                f"FROM ({sql}) AS t"
            )
        else:
            wrapped = f"SELECT COUNT(*) AS failures FROM ({sql}) AS t"

        try:
            job = self._get_client().query(
                wrapped,
                job_config=self._default_job_config(
                    stage="warehouse_test",
                    # DEC-002 of #22 — when materialise_sample has minted
                    # an active session, every per-test query routes
                    # into it via connection_properties so the
                    # ``_SESSION._sf_sample_<run_id>`` temp table is
                    # reachable. ``None`` (no active session) reverts to
                    # the v0.1 oneshot path unchanged.
                    session_id=self._active_session_id,
                ),
            )
            rows = list(job.result())
        except Exception as exc:
            mapped = map_bq_exception(exc, context={"max_bytes_billed": self._max_bytes_billed})
            if mapped is exc:
                raise
            raise mapped from exc

        if not rows:  # pragma: no cover - aggregate always returns one row
            raise RuntimeError("run_test_sql wrapper returned no rows")
        row = row_to_dict(rows[0])
        failure_count = int(row["failures"])
        sample_failures: list[dict[str, Any]] | None
        if capture_failures > 0:
            raw_samples = row.get("samples") or []
            sample_failures = [
                row_to_dict(r) if not isinstance(r, dict) else r for r in raw_samples
            ]
        else:
            sample_failures = None

        return TestResult(
            passed=(failure_count == 0),
            failure_count=failure_count,
            sample_failures=sample_failures,
            # v0.2: extract sample row schema for explainable diffs via a
            # separate dry_run on the inner SQL.
            row_schema=None,
        )

    # ------------------------------------------------------------------
    # estimate_query_bytes — US-002 of issue #36.
    # ------------------------------------------------------------------

    def estimate_query_bytes(self, sql: str) -> int:
        """Estimate bytes BigQuery would process for ``sql`` via dry_run.

        Issues one ``client.query(sql, job_config=...)`` call with
        ``QueryJobConfig(dry_run=True, use_query_cache=False)`` and
        returns ``int(job.total_bytes_processed)``. BigQuery does not
        bill bytes for a dry_run, so the job config deliberately does
        NOT set ``maximum_bytes_billed`` — a cap on something that
        never bills would be dead config (US-002 of issue #36;
        DEC-004 of the plan).

        ``sql`` is subject to the same cheap rejects as
        :meth:`run_test_sql` (no ``;``, no ``--``, balanced parens) via
        :func:`signalforge.warehouse._sql_safety.validate_test_sql`.

        SDK / network / quota failures route through
        :func:`signalforge.warehouse.adapters._client.map_bq_exception`
        — auth surfaces as :class:`WarehouseAuthError`, BadRequest as
        :class:`QuerySyntaxError` or :class:`BytesBilledExceededError`,
        etc. (mirrors :meth:`run_test_sql` / :meth:`sample_rows`).
        """
        validate_test_sql(sql)

        try:
            from signalforge import __version__ as _pkg_version

            job = self._get_client().query(
                sql,
                job_config=_make_dry_run_job_config(
                    stage="warehouse_estimate_query_bytes",
                    version=_pkg_version,
                ),
            )
        except Exception as exc:
            mapped = map_bq_exception(exc, context={"max_bytes_billed": self._max_bytes_billed})
            if mapped is exc:
                raise
            raise mapped from exc

        total_bytes = getattr(job, "total_bytes_processed", None)
        if total_bytes is None:
            # Dry-run jobs populate ``total_bytes_processed`` on the
            # returned :class:`QueryJob` without calling ``.result()`` —
            # BigQuery validates the SQL server-side and returns the
            # estimate inline. A ``None`` here is a real SDK contract
            # violation; surface it loudly so a regression in the
            # client surface doesn't silently return 0 bytes.
            raise RuntimeError(
                "BigQuery returned no total_bytes_processed for the dry_run query; "
                "the SDK contract was violated."
            )
        return int(total_bytes)


# ---------------------------------------------------------------------------
# Schema normalisation
# ---------------------------------------------------------------------------


def _normalise_schema(schema: Any) -> list[tuple[str, str]]:
    """
    Normalize a BigQuery table schema into a list of (name, bq_type) tuples.
    
    Accepts:
    - An iterable of two-element tuples (name, bq_type).
    - Objects with attributes `name` and either `field_type` or `type` (e.g., google.cloud.bigquery.SchemaField).
    Coerces both components to `str`. If `schema` is falsy or entries cannot be interpreted, returns an empty list or skips invalid entries.
    
    Parameters:
        schema (Any): Schema representation to normalize.
    
    Returns:
        list[tuple[str, str]]: A list of (column_name, bigquery_type) tuples.
    """
    out: list[tuple[str, str]] = []
    for entry in schema or []:
        if isinstance(entry, tuple) and len(entry) == 2:
            name, bq_type = entry
            out.append((str(name), str(bq_type)))
            continue
        name = getattr(entry, "name", None)
        bq_type = getattr(entry, "field_type", None) or getattr(entry, "type", None)
        if name is not None and bq_type is not None:
            out.append((str(name), str(bq_type)))
    return out


__all__ = ["BigQueryAdapter"]
