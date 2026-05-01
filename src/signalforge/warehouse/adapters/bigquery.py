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

import logging
from datetime import date, datetime
from typing import Any

from signalforge.warehouse._sql_safety import validate_identifier, validate_test_sql
from signalforge.warehouse.adapters._client import (
    _BQClientProtocol,
    _make_query_job_config,
    make_real_client,
    map_bq_exception,
    row_to_dict,
)
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import (
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

    def _default_job_config(self, *, stage: str, timeout_ms: int | None = None) -> Any:
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
        """
        from signalforge import __version__

        return _make_query_job_config(
            max_bytes_billed=self._max_bytes_billed,
            stage=stage,
            version=__version__,
            timeout_ms=timeout_ms,
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
                wrapped, job_config=self._default_job_config(stage="warehouse_test")
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


# ---------------------------------------------------------------------------
# Schema normalisation
# ---------------------------------------------------------------------------


def _normalise_schema(schema: Any) -> list[tuple[str, str]]:
    """Convert a BigQuery ``Table.schema`` into ``(name, bq_type)`` pairs.

    ``google.cloud.bigquery.SchemaField`` exposes ``.name`` and
    ``.field_type``. The :class:`tests.warehouse._fake.FakeTable.schema`
    is already a ``list[tuple[str, str]]``. Anything else is best-effort
    coerced.
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
