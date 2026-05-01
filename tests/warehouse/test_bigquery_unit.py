"""Comprehensive unit tests for BigQueryAdapter (US-009).

Smoke-level coverage for ``_quote``, ``_render_partition_filter``, the
outside-context ``RuntimeError`` and ``__repr__`` redaction lives in
``test_bigquery_smoke.py`` (US-008). This module covers the behavioural
surface those checks don't reach: cost defaults, job-config plumbing,
``sample_rows`` decision branches, ``column_stats`` batching, exception
mapping, and the context-manager lifecycle.

Every test injects a :class:`FakeBigQueryClient` (DEC-002) — never reaches
out to real BigQuery. Each test is capable of failing on a real regression
(``testing-signal.md``).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

import pytest
from google.api_core.exceptions import BadRequest, NotFound

from signalforge.warehouse import (
    BIGQUERY_DIALECT,
    BigQueryAdapter,
    BytesBilledExceededError,
    ColumnNotFoundError,
    PartitionFilter,
    QuerySyntaxError,
    SamplingRequiresPartitionFilterError,
    TableNotFoundError,
    TableRef,
    UnknownTableSizeError,
    WarehouseAuthError,
)
from tests.warehouse._fake import FakeBigQueryClient, FakeTable

# ---------------------------------------------------------------------------
# __init__ + cost defaults
# ---------------------------------------------------------------------------


def test_default_max_bytes_billed_is_100mb() -> None:
    """DEC-019: bare ``BigQueryAdapter()`` defaults to 100 MB cap."""
    adapter = BigQueryAdapter()
    assert adapter._max_bytes_billed == 100_000_000


def test_explicit_max_bytes_billed_overrides_default() -> None:
    """An explicit ``max_bytes_billed=`` should win over the default."""
    adapter = BigQueryAdapter(max_bytes_billed=42_000_000)
    assert adapter._max_bytes_billed == 42_000_000


def test_init_stores_project_and_location() -> None:
    """The constructor should retain ``project`` / ``location`` for quoting +
    repr (the smoke test pins repr; this pins the underlying state)."""
    adapter = BigQueryAdapter(project="proj", location="EU")
    assert adapter._project == "proj"
    assert adapter._location == "EU"


def test_init_accepts_injected_client(fake_client: FakeBigQueryClient) -> None:
    """An injected client should be used directly (no lazy-build)."""
    adapter = BigQueryAdapter(client=fake_client)
    assert adapter._client is fake_client


# ---------------------------------------------------------------------------
# _default_job_config — DEC-015 (use_query_cache=False, labels, max_bytes)
# ---------------------------------------------------------------------------


def test_query_job_config_use_query_cache_false(adapter: BigQueryAdapter) -> None:
    """DEC-015: every QueryJobConfig must carry ``use_query_cache=False``
    for reproducibility (Architectural Commitment #5: explainable diffs)."""
    cfg = adapter._default_job_config(stage="warehouse_sample")
    assert cfg.use_query_cache is False


def test_query_job_config_labels_set(adapter: BigQueryAdapter) -> None:
    """DEC-015: labels must include ``signalforge_stage`` and
    ``signalforge_version`` for v0.2 cost attribution."""
    cfg = adapter._default_job_config(stage="warehouse_sample")
    labels = dict(cfg.labels)
    assert "signalforge_stage" in labels
    assert labels["signalforge_stage"] == "warehouse_sample"
    assert "signalforge_version" in labels
    # version label cannot contain '.' per BigQuery label rules.
    assert "." not in labels["signalforge_version"]


def test_query_job_config_max_bytes_billed_set(adapter: BigQueryAdapter) -> None:
    """DEC-015: ``maximum_bytes_billed`` on the job config must reflect the
    adapter's configured cap so BigQuery rejects oversize queries."""
    cfg = adapter._default_job_config(stage="warehouse_sample")
    assert cfg.maximum_bytes_billed == adapter._max_bytes_billed


def test_query_job_config_stage_label_threads_through(adapter: BigQueryAdapter) -> None:
    """A different ``stage`` argument should yield a different label."""
    cfg = adapter._default_job_config(stage="warehouse_test")
    assert dict(cfg.labels)["signalforge_stage"] == "warehouse_test"


def test_make_query_job_config_sets_use_query_cache_false() -> None:
    """DEC-015 (non-negotiable): the shared job-config helper must always
    produce ``use_query_cache=False``. Until US-013's QG this branch was
    masked from coverage by ``# pragma: no cover``; it's the only place
    the cache is disabled, so it gets a unit test of its own."""
    from google.cloud import bigquery

    from signalforge import __version__
    from signalforge.warehouse.adapters._client import _make_query_job_config

    cfg = _make_query_job_config(stage="warehouse_sample", max_bytes_billed=12345)

    assert isinstance(cfg, bigquery.QueryJobConfig)
    assert cfg.use_query_cache is False
    assert cfg.maximum_bytes_billed == 12345
    assert cfg.labels == {
        "signalforge_stage": "warehouse_sample",
        "signalforge_version": __version__.replace(".", "_"),
    }


# ---------------------------------------------------------------------------
# dialect
# ---------------------------------------------------------------------------


def test_dialect_returns_bigquery_constant(adapter: BigQueryAdapter) -> None:
    """DEC-003: the adapter's ``dialect()`` returns the canonical constant
    instance (not a copy) so callers can identity-compare."""
    assert adapter.dialect() is BIGQUERY_DIALECT


# ---------------------------------------------------------------------------
# sample_rows — DEC-006 / DEC-024
# ---------------------------------------------------------------------------


def test_sample_rows_uses_farm_fingerprint(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """DEC-006: the deterministic sampler must emit FARM_FINGERPRINT in the
    SQL — the prune layer relies on same-input → same-output reproducibility."""
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    fake_client.expect_query(
        matching=r"FARM_FINGERPRINT",
        returns=[{"a": 1}],
    )

    rows = adapter.sample_rows(table_ref, n=10)

    assert rows == [{"a": 1}]
    fake_client.assert_all_expectations_met()


def test_sample_rows_includes_partition_filter(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """DEC-014: a supplied PartitionFilter should be rendered into the WHERE clause."""
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    fake_client.expect_query(
        matching=r"`event_date`\s*>=\s*DATE\('2024-01-01'\)",
        returns=[{"x": 1}],
    )

    pf = PartitionFilter(column="event_date", op=">=", value=date(2024, 1, 1))
    rows = adapter.sample_rows(table_ref, n=10, partition_filter=pf)

    assert rows == [{"x": 1}]
    fake_client.assert_all_expectations_met()


def test_sample_rows_unknown_size_raises_unknown_table_size(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
) -> None:
    """DEC-024: ``Table.num_rows is None`` + no PartitionFilter → fail loud."""
    fake_client.expect_get_table(
        ref=table_ref,
        returns=FakeTable(num_rows=None, schema=[]),
    )

    with pytest.raises(UnknownTableSizeError) as exc_info:
        adapter.sample_rows(table_ref, n=10)
    # The typed ``.table`` field must be a stable qualified identifier,
    # not the dataclass repr ``TableRef(project=..., dataset=..., name=...)``
    # (Copilot review feedback).
    assert exc_info.value.table == table_ref.qualified_name
    assert "TableRef(" not in exc_info.value.table


def test_sample_rows_rejects_non_positive_n(
    adapter: BigQueryAdapter,
    table_ref: TableRef,
) -> None:
    """``n <= 0`` would yield ``ZeroDivisionError`` or nonsensical SQL;
    fail loud at the public boundary instead (Copilot review feedback)."""
    with pytest.raises(ValueError, match="n > 0"):
        adapter.sample_rows(table_ref, n=0)
    with pytest.raises(ValueError, match="n > 0"):
        adapter.sample_rows(table_ref, n=-5)


def test_sample_rows_large_unfiltered_raises_sampling_requires_partition(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
) -> None:
    """DEC-024: ``num_rows >= _LARGE_TABLE_THRESHOLD`` + no PartitionFilter
    refuses to scan terabytes."""
    fake_client.expect_get_table(
        ref=table_ref,
        returns=FakeTable(num_rows=200_000_000, schema=[]),
    )

    with pytest.raises(SamplingRequiresPartitionFilterError) as exc_info:
        adapter.sample_rows(table_ref, n=10)

    assert exc_info.value.num_rows == 200_000_000
    assert exc_info.value.table == table_ref.qualified_name
    assert "TableRef(" not in exc_info.value.table


def test_sample_rows_uses_bucket_n_for_unknown_size_with_filter(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
) -> None:
    """DEC-024: ``num_rows`` unknown + PartitionFilter present → fall back
    to a bucket=1000; the call should succeed."""
    fake_client.expect_get_table(
        ref=table_ref,
        returns=FakeTable(num_rows=None, schema=[]),
    )
    fake_client.expect_query(
        matching=r"FARM_FINGERPRINT",
        returns=[{"x": 1}],
    )
    pf = PartitionFilter(column="event_date", op=">=", value=date(2024, 1, 1))

    rows = adapter.sample_rows(table_ref, n=10, partition_filter=pf)

    assert rows == [{"x": 1}]


def test_sample_rows_caches_get_table_within_context(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """DEC-025: inside ``with adapter:``, repeated ``sample_rows`` calls for
    the same table reuse cached metadata (one ``get_table``, many queries)."""
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    fake_client.expect_query(matching=r"FARM_FINGERPRINT", returns=[{"x": 1}])
    fake_client.expect_query(matching=r"FARM_FINGERPRINT", returns=[{"x": 2}])

    with adapter:
        adapter.sample_rows(table_ref, n=10)
        adapter.sample_rows(table_ref, n=10)

    fake_client.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# column_stats — DEC-008 / DEC-013 / DEC-016 / DEC-023 / DEC-025
# ---------------------------------------------------------------------------


def _column_stats_row_for(columns: list[tuple[str, str]]) -> dict[str, Any]:
    """Build a fake aggregate-query result row for the given columns.

    Mirrors ``_flush_column_stats_batch``'s SELECT shape: count_<col>,
    distinct_<col>, nulls_<col>, plus min_<col> / max_<col> when the type
    isn't complex (DEC-016).
    """
    complex_types = {"GEOGRAPHY", "JSON", "BYTES"}
    parametric = {"ARRAY", "STRUCT", "RANGE"}
    row: dict[str, Any] = {"row_count": 100}
    for name, bq_type in columns:
        upper = bq_type.upper()
        head = upper.split("<", 1)[0]
        is_complex = upper in complex_types or head in parametric
        row[f"count_{name}"] = 100
        row[f"distinct_{name}"] = 50
        row[f"nulls_{name}"] = 0
        if not is_complex:
            row[f"min_{name}"] = 1
            row[f"max_{name}"] = 99
    return row


def _wrap_query_capture(fake_client: FakeBigQueryClient) -> dict[str, Any]:
    """Wrap ``fake_client.query`` to capture the SQL text of the last call.

    Returns a dict the caller can introspect after the adapter call. The
    underlying expectation matching still runs — we're only intercepting
    the SQL string for substring assertions.
    """
    captured: dict[str, Any] = {"sqls": []}
    original = fake_client.query

    def wrapped(sql: str, job_config: Any = None) -> Any:
        captured["sqls"].append(sql)
        return original(sql, job_config=job_config)

    fake_client.query = wrapped  # type: ignore[method-assign]
    return captured


def test_column_stats_batches_pre_queued_columns(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
) -> None:
    """DEC-008 (Option A simplified): when multiple columns are queued in
    ``_column_stats_pending`` before the first stat read, they flush in a
    single batched aggregate query."""
    schema = [("a", "INT64"), ("b", "STRING")]
    fake_client.expect_get_table(ref=table_ref, returns=FakeTable(num_rows=100, schema=schema))
    fake_client.expect_query(
        matching=r"count_a",
        returns=[_column_stats_row_for(schema)],
    )
    captured = _wrap_query_capture(fake_client)

    with adapter:
        # Pre-seed pending so the first column_stats call flushes the batch.
        assert adapter._column_stats_pending is not None
        adapter._column_stats_pending[table_ref] = ["a", "b"]
        stats_a = adapter.column_stats(table_ref, "a")
        # The "b" result is now cached — no second query.
        stats_b = adapter.column_stats(table_ref, "b")
        assert stats_a.count == 100
        assert stats_b.count == 100

    # One batched query; both column counts in the SQL.
    assert len(captured["sqls"]) == 1
    assert "count_a" in captured["sqls"][0]
    assert "count_b" in captured["sqls"][0]
    fake_client.assert_all_expectations_met()


def test_column_stats_eager_flush_drains_full_pending_batch(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
) -> None:
    """DEC-008 (Option A): a stat-access flushes EVERY column queued for that
    table in a single round-trip. Pre-queue 3, call once, expect 1 query."""
    schema = [("a", "INT64"), ("b", "STRING"), ("c", "INT64")]
    fake_client.expect_get_table(ref=table_ref, returns=FakeTable(num_rows=100, schema=schema))
    fake_client.expect_query(
        matching=r"count_a",
        returns=[_column_stats_row_for(schema)],
    )
    captured = _wrap_query_capture(fake_client)

    with adapter:
        assert adapter._column_stats_pending is not None
        adapter._column_stats_pending[table_ref] = ["a", "b", "c"]
        adapter.column_stats(table_ref, "a")
        # b and c already resolved in the same flush.
        adapter.column_stats(table_ref, "b")
        adapter.column_stats(table_ref, "c")

    assert len(captured["sqls"]) == 1
    sql = captured["sqls"][0]
    assert "count_a" in sql
    assert "count_b" in sql
    assert "count_c" in sql


def test_column_stats_skips_min_max_for_geography(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
) -> None:
    """DEC-016: GEOGRAPHY columns must NOT get MIN/MAX in the SQL and the
    returned ColumnStats must carry ``min=max=None``."""
    schema = [("geo_col", "GEOGRAPHY")]
    fake_client.expect_get_table(ref=table_ref, returns=FakeTable(num_rows=10, schema=schema))
    fake_client.expect_query(
        matching=r"count_geo_col",
        returns=[_column_stats_row_for(schema)],
    )
    captured = _wrap_query_capture(fake_client)

    with adapter:
        stats = adapter.column_stats(table_ref, "geo_col")

    sql = captured["sqls"][0]
    assert "MIN(`geo_col`)" not in sql
    assert "MAX(`geo_col`)" not in sql
    assert stats.min is None
    assert stats.max is None
    assert stats.data_type == "GEOGRAPHY"


@pytest.mark.parametrize(
    "complex_type",
    ["JSON", "BYTES", "ARRAY<INT64>", "STRUCT<a INT64>", "RANGE<DATE>"],
)
def test_column_stats_skips_min_max_for_complex_types(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    complex_type: str,
) -> None:
    """DEC-016: every parametric / scalar complex BQ type skips MIN/MAX."""
    schema = [("col", complex_type)]
    fake_client.expect_get_table(ref=table_ref, returns=FakeTable(num_rows=10, schema=schema))
    fake_client.expect_query(
        matching=r"count_col",
        returns=[_column_stats_row_for(schema)],
    )
    captured = _wrap_query_capture(fake_client)

    with adapter:
        stats = adapter.column_stats(table_ref, "col")

    sql = captured["sqls"][0]
    assert "MIN(`col`)" not in sql
    assert "MAX(`col`)" not in sql
    assert stats.min is None
    assert stats.max is None


def test_column_stats_warns_at_threshold(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEC-023: a queued batch larger than the threshold logs a WARNING.

    Because the simplified Option A flushes on every ``column_stats`` call,
    we pre-populate ``_column_stats_pending`` with enough already-queued
    columns to trip the threshold on the first call.
    """
    monkeypatch.setattr("signalforge.warehouse.adapters.bigquery._COLUMN_BATCH_WARN_AT", 2)
    schema = [("a", "INT64"), ("b", "INT64"), ("c", "INT64")]
    fake_client.expect_get_table(ref=table_ref, returns=FakeTable(num_rows=10, schema=schema))
    fake_client.expect_query(
        matching=r"count_a",
        returns=[_column_stats_row_for(schema)],
    )

    with caplog.at_level(logging.WARNING, logger="signalforge.warehouse"), adapter:
        # Pre-seed the pending list so the next column_stats call sees
        # len(pending) > threshold.
        assert adapter._column_stats_pending is not None
        adapter._column_stats_pending[table_ref] = ["a", "b"]
        adapter.column_stats(table_ref, "c")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Large column_stats batch" in r.getMessage() for r in warnings)


def test_column_stats_validates_column_identifier(
    adapter: BigQueryAdapter,
    table_ref: TableRef,
) -> None:
    """DEC-013: ``column`` argument is validated against the identifier regex
    BEFORE the context-manager check, so an invalid name raises
    InvalidIdentifierError even outside a ``with``."""
    from signalforge.warehouse.errors import InvalidIdentifierError

    with pytest.raises(InvalidIdentifierError):
        adapter.column_stats(table_ref, "bad name; DROP TABLE x")


# ---------------------------------------------------------------------------
# run_test_sql — DEC-007 / DEC-013 / DEC-015
# ---------------------------------------------------------------------------


def test_run_test_sql_wraps_with_count(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-007: the candidate SQL is wrapped in COUNT(*) AS failures."""
    fake_client.expect_query(
        matching=r"COUNT\(\*\) AS failures",
        returns=[{"failures": 0}],
    )

    result = adapter.run_test_sql("SELECT 1 AS x WHERE 1=0", capture_failures=0)

    assert result.passed is True


def test_run_test_sql_capture_uses_array_agg(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-007: ``capture_failures > 0`` adds ARRAY_AGG with the LIMIT."""
    fake_client.expect_query(
        matching=r"ARRAY_AGG.*LIMIT 5",
        returns=[{"failures": 0, "samples": []}],
    )

    result = adapter.run_test_sql("SELECT 1 AS x WHERE 1=0", capture_failures=5)

    assert result.passed is True


def test_run_test_sql_passed_when_zero_failures(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-007: ``failure_count == 0`` → ``passed=True``."""
    fake_client.expect_query(
        matching=r"COUNT\(\*\)",
        returns=[{"failures": 0}],
    )

    result = adapter.run_test_sql("SELECT 1")

    assert result.passed is True
    assert result.failure_count == 0


def test_run_test_sql_failed_with_count(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-007: non-zero failures → ``passed=False`` and the count carries through."""
    fake_client.expect_query(
        matching=r"COUNT\(\*\)",
        returns=[{"failures": 42}],
    )

    result = adapter.run_test_sql("SELECT 1")

    assert result.passed is False
    assert result.failure_count == 42


def test_run_test_sql_capture_returns_sample_failures(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-007: captured rows surface in ``sample_failures``."""
    fake_client.expect_query(
        matching=r"ARRAY_AGG",
        returns=[{"failures": 2, "samples": [{"a": 1}, {"a": 2}]}],
    )

    result = adapter.run_test_sql("SELECT 1", capture_failures=5)

    assert result.sample_failures is not None
    assert len(result.sample_failures) == 2
    assert result.sample_failures[0] == {"a": 1}


def test_run_test_sql_row_schema_is_none(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """v0.1 contract: ``row_schema`` is None (populating it requires a
    separate dry_run; deferred to v0.2)."""
    fake_client.expect_query(
        matching=r"COUNT\(\*\)",
        returns=[{"failures": 0}],
    )

    result = adapter.run_test_sql("SELECT 1")

    assert result.row_schema is None


def test_run_test_sql_rejects_semicolons(adapter: BigQueryAdapter) -> None:
    """DEC-013: candidate SQL with ``;`` is rejected pre-flight."""
    with pytest.raises(QuerySyntaxError):
        adapter.run_test_sql("SELECT 1; DROP TABLE x")


def test_run_test_sql_rejects_unbalanced_parens(adapter: BigQueryAdapter) -> None:
    """DEC-013: unbalanced parens are rejected pre-flight."""
    with pytest.raises(QuerySyntaxError):
        adapter.run_test_sql("SELECT (1")


def test_run_test_sql_rejects_double_dash(adapter: BigQueryAdapter) -> None:
    """DEC-013: ``--`` line comments are rejected pre-flight."""
    with pytest.raises(QuerySyntaxError):
        adapter.run_test_sql("SELECT 1 -- comment")


def test_run_test_sql_no_capture_returns_no_samples(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """``capture_failures=0`` → ``sample_failures is None`` (not an empty list)."""
    fake_client.expect_query(
        matching=r"COUNT\(\*\)",
        returns=[{"failures": 0}],
    )

    result = adapter.run_test_sql("SELECT 1")

    assert result.sample_failures is None


# ---------------------------------------------------------------------------
# Exception mapping — DEC-026 + adapters/_client.py
# ---------------------------------------------------------------------------


def test_bytes_billed_exceeded_wraps_bq_bad_request(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """A BadRequest mentioning 'maximum bytes billed' maps to
    BytesBilledExceededError (typed; prune layer pattern-matches)."""
    fake_client.expect_query(
        matching=r"COUNT\(\*\)",
        returns=BadRequest("Query exceeded limit for maximum bytes billed"),
    )

    with pytest.raises(BytesBilledExceededError):
        adapter.run_test_sql("SELECT 1")


def test_bytes_billed_exceeded_carries_configured_limit(
    fake_client: FakeBigQueryClient,
) -> None:
    """The mapper must thread the adapter's configured ``max_bytes_billed``
    through ``context=`` so the rendered error carries the real cap (not
    the historical ``limit=0`` placeholder)."""
    fake_client.expect_query(
        matching=r"COUNT\(\*\)",
        returns=BadRequest("Query exceeded limit for maximum bytes billed"),
    )
    adapter = BigQueryAdapter(
        project="fake_project",
        location="US",
        max_bytes_billed=42_000_000,
        client=fake_client,
    )

    with pytest.raises(BytesBilledExceededError) as exc_info:
        adapter.run_test_sql("SELECT 1")

    assert exc_info.value.limit == 42_000_000
    assert "limit=42000000" in str(exc_info.value)


def test_column_not_found_wraps_bq_bad_request(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """Real BigQuery surfaces missing columns as ``BadRequest`` with the
    text ``Unrecognized name``. The mapper must route those to
    ColumnNotFoundError, not the generic QuerySyntaxError bucket."""
    fake_client.expect_query(
        matching=r"COUNT\(\*\)",
        returns=BadRequest("Unrecognized name: foo at [3:8]"),
    )

    with pytest.raises(ColumnNotFoundError) as exc_info:
        adapter.run_test_sql("SELECT foo FROM `p.d.t`")
    # The typed ``.column`` field must hold the bare identifier, not
    # the full ``BadRequest`` message text (Copilot review feedback).
    assert exc_info.value.column == "foo"


def test_query_syntax_error_wraps_bq_bad_request(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """A generic BadRequest maps to QuerySyntaxError."""
    fake_client.expect_query(
        matching=r"COUNT\(\*\)",
        returns=BadRequest("Syntax error: unexpected token at line 1"),
    )

    with pytest.raises(QuerySyntaxError):
        adapter.run_test_sql("SELECT 1")


def test_table_not_found_wraps_bq_not_found(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
) -> None:
    """A NotFound during get_table maps to TableNotFoundError."""
    fake_client.expect_get_table(
        ref=table_ref,
        returns=NotFound("Table 'fake_project.analytics.dim_users' not found"),
    )

    with pytest.raises(TableNotFoundError) as exc_info:
        adapter.sample_rows(table_ref, n=10)
    # ``.table`` must be a stable qualified identifier, not the truncated
    # google.api_core ``NotFound`` message (Copilot review feedback).
    assert exc_info.value.table == table_ref.qualified_name


def test_warehouse_auth_error_wraps_default_credentials_error(
    monkeypatch: pytest.MonkeyPatch,
    table_ref: TableRef,
) -> None:
    """ADC failures during lazy client construction map to WarehouseAuthError."""

    def _raise(*_args: object, **_kwargs: object) -> Any:
        raise WarehouseAuthError("nope")

    monkeypatch.setattr("signalforge.warehouse.adapters.bigquery.make_real_client", _raise)

    adapter = BigQueryAdapter(project="proj")  # no client= → lazy build path

    with pytest.raises(WarehouseAuthError):
        adapter.sample_rows(table_ref, n=10)


# ---------------------------------------------------------------------------
# Context manager — DEC-008 / DEC-025
# ---------------------------------------------------------------------------


def test_context_manager_enter_returns_self(adapter: BigQueryAdapter) -> None:
    """Entering the context returns the adapter itself (so callers can use
    ``with BigQueryAdapter(...) as a:``)."""
    with adapter as a:
        assert a is adapter


def test_refresh_table_metadata_drops_cache_entry(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """``refresh_table_metadata`` invalidates one entry; the next call
    re-fetches via ``get_table``. Backs the user-facing remediation in
    ``UnknownTableSizeError.default_remediation``.
    """
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    fake_client.expect_query(matching=r"FARM_FINGERPRINT", returns=[{"x": 1}])
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    fake_client.expect_query(matching=r"FARM_FINGERPRINT", returns=[{"x": 2}])

    with adapter:
        adapter.sample_rows(table_ref, n=10)
        # First sample populated the cache; refresh should drop it,
        # forcing the second sample to consume the second get_table.
        assert adapter._table_metadata_cache is not None
        assert table_ref in adapter._table_metadata_cache

        adapter.refresh_table_metadata(table_ref)

        assert table_ref not in adapter._table_metadata_cache
        adapter.sample_rows(table_ref, n=10)

    # All four expectations consumed — second get_table was needed.
    fake_client.assert_all_expectations_met()


def test_refresh_table_metadata_outside_context_is_noop(
    adapter: BigQueryAdapter,
    table_ref: TableRef,
) -> None:
    """Calling outside ``with adapter:`` is a safe no-op (no cache to invalidate)."""
    # Cache is None outside context.
    assert adapter._table_metadata_cache is None

    adapter.refresh_table_metadata(table_ref)  # should not raise

    assert adapter._table_metadata_cache is None


def test_context_manager_exit_clears_caches(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """Exiting the context invalidates the table cache (DEC-025: a fresh
    block re-fetches metadata)."""
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    fake_client.expect_query(matching=r"FARM_FINGERPRINT", returns=[{"x": 1}])

    with adapter:
        adapter.sample_rows(table_ref, n=10)
        # Inside the block, the cache is populated.
        assert adapter._table_metadata_cache is not None
        assert table_ref in adapter._table_metadata_cache

    # After exit, the cache is reset to None.
    assert adapter._table_metadata_cache is None
    assert adapter._column_stats_pending is None
    assert adapter._column_stats_results is None


def test_context_manager_exit_clears_caches_even_if_flush_raises(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """A flush failure during clean exit must NOT leave the adapter in a
    half-cleaned state — the next ``with`` block must start from empty
    caches (DEC-025).
    """
    # Seed a pending column_stats batch that will fail to flush at exit
    # because the get_table expectation needed by the flush is missing.
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    fake_client.expect_query(matching=r"FARM_FINGERPRINT", returns=[{"x": 1}])

    with pytest.raises(AssertionError), adapter:  # noqa: PT012
        adapter.sample_rows(table_ref, n=10)
        # Queue a column_stats request whose flush will fail because
        # no get_table / query expectations are registered for it.
        assert adapter._column_stats_pending is not None
        adapter._column_stats_pending[table_ref] = ["nonexistent_col"]
        # __exit__ tries to flush; FakeBigQueryClient raises
        # AssertionError on unexpected calls, which propagates because
        # exc_type is None on clean exit.

    # Despite the flush raising, the cleanup ran in the finally block.
    assert adapter._table_metadata_cache is None
    assert adapter._column_stats_pending is None
    assert adapter._column_stats_results is None


def test_context_manager_exit_swallows_flush_errors_during_user_exception(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
) -> None:
    """If the user raises inside ``with``, a flush failure on exit must NOT
    mask the original exception."""
    schema = [("a", "INT64")]
    fake_client.expect_get_table(ref=table_ref, returns=FakeTable(num_rows=10, schema=schema))
    # No expect_query — the flush will raise AssertionError (unexpected query).

    class _UserError(Exception):
        pass

    with pytest.raises(_UserError), adapter:
        # Queue a column without flushing it (so __exit__ tries to flush).
        adapter._column_stats_pending = {table_ref: ["a"]}
        adapter._column_stats_results = {}
        adapter._table_metadata_cache = {}
        raise _UserError("user code blew up")


def test_column_stats_drains_pending_after_flush(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
) -> None:
    """After a flush, the pending list for the table is drained so a follow-up
    column_stats call enqueues a fresh batch (rather than re-flushing)."""
    schema = [("a", "INT64"), ("b", "INT64")]
    fake_client.expect_get_table(ref=table_ref, returns=FakeTable(num_rows=10, schema=schema))
    fake_client.expect_query(
        matching=r"count_a",
        returns=[_column_stats_row_for(schema)],
    )

    with adapter:
        adapter.column_stats(table_ref, "a")
        # After first flush, pending should be empty for this table.
        assert adapter._column_stats_pending is not None
        assert adapter._column_stats_pending.get(table_ref) == []


# ---------------------------------------------------------------------------
# _quote integration with TableRef.project=None (DEC-027) — exercises
# adapter._get_client().project lookup beyond the smoke test.
# ---------------------------------------------------------------------------


def test_quote_uses_fake_client_project_when_table_ref_project_none(
    adapter: BigQueryAdapter,
) -> None:
    """DEC-027: with ``TableRef.project=None`` the ``_quote`` helper falls
    back to the underlying client's billing project."""
    ref = TableRef(project=None, dataset="ds", name="tbl")
    quoted = adapter._quote(ref)
    assert quoted == "`fake_project.ds.tbl`"


# ---------------------------------------------------------------------------
# Sanity: smoke-test SQL fragments are still valid under realistic flows.
# ---------------------------------------------------------------------------


def test_sample_rows_emits_limit_clause(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """The SQL must end with ``LIMIT n`` so BigQuery doesn't materialise more
    rows than requested (defence-in-depth on the bucket-mod sampler)."""
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    fake_client.expect_query(
        matching=r"FARM_FINGERPRINT",
        returns=[{"x": 1}],
    )
    captured = _wrap_query_capture(fake_client)

    adapter.sample_rows(table_ref, n=7)

    assert re.search(r"LIMIT 7\s*$", captured["sqls"][0]) is not None


def test_sample_rows_includes_order_by_for_deterministic_limit(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """DEC-006: when the bucket-mod WHERE retains more than n rows, the
    ``LIMIT`` truncation must be deterministic. The adapter inserts
    ``ORDER BY FARM_FINGERPRINT(TO_JSON_STRING(t))`` before ``LIMIT`` so
    the same input always yields the same sample."""
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    fake_client.expect_query(
        matching=r"ORDER BY FARM_FINGERPRINT",
        returns=[{"x": 1}],
    )
    captured = _wrap_query_capture(fake_client)

    adapter.sample_rows(table_ref, n=10)

    sql = captured["sqls"][0]
    assert "ORDER BY" in sql
    # ORDER BY must precede LIMIT in the rendered SQL.
    assert sql.index("ORDER BY") < sql.index("LIMIT")
