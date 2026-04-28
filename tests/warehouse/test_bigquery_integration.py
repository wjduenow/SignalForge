"""Gated integration tests for :class:`BigQueryAdapter` (US-010).

These tests exercise the adapter against the *real*
``bigquery-public-data.samples.shakespeare`` dataset (164K rows, free
under BigQuery's 1 TB/month query tier) plus a monkeypatched ADC path
for the auth-failure surface. They are maintainer-only for v0.1; no CI
job runs them.

Belt-and-suspenders gating (DEC-011): every test wears BOTH

* ``@pytest.mark.bigquery`` — filtered out by the default
  ``addopts = "-m 'not bigquery'"`` so a bare ``pytest`` never collects
  them, and
* ``@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), ...)`` — so
  ``pytest -m bigquery`` without the env var (or without configured
  Application Default Credentials) skips at runtime instead of erroring
  out on a missing credentials chain.

To run locally:

.. code-block:: bash

    gcloud auth application-default login
    SF_RUN_BQ=1 pytest -m bigquery

The two non-Shakespeare tests (``max_bytes_billed`` and ``adc_unconfigured``
— DEC-028) cover the cost-cap and auth-failure paths that unit tests
can only mock; the cost cap reuses the Shakespeare table with a 1-byte
``max_bytes_billed`` so the dry-run rejection is free.
"""

from __future__ import annotations

import os

import pytest

from signalforge.warehouse.adapters.bigquery import BigQueryAdapter
from signalforge.warehouse.errors import (
    BytesBilledExceededError,
    WarehouseAuthError,
)
from signalforge.warehouse.models import TableRef

_SF_RUN_BQ_REASON = "requires SF_RUN_BQ=1 and ADC"


def _shakespeare_ref() -> TableRef:
    """Build the Shakespeare ``TableRef`` for the integration tests.

    Real GCP project IDs use hyphens (``bigquery-public-data``); since the
    QG of US-013, the project regex accepts the hyphen-permissive GCP
    grammar so the regular ``TableRef(...)`` constructor is sufficient.
    """
    return TableRef(project="bigquery-public-data", dataset="samples", name="shakespeare")


@pytest.mark.bigquery
@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), reason=_SF_RUN_BQ_REASON)
def test_int_sample_rows_returns_n_rows_from_shakespeare() -> None:
    """Sampling Shakespeare with n=10 must return at most 10 rows whose
    schema matches the public dataset (``word``, ``word_count``, ``corpus``,
    ``corpus_date``)."""
    adapter = BigQueryAdapter()
    with adapter:
        rows = adapter.sample_rows(_shakespeare_ref(), n=10)

    assert len(rows) <= 10
    assert rows, "expected at least one sampled row from a 164K-row table"
    expected_columns = {"word", "word_count", "corpus", "corpus_date"}
    for row in rows:
        assert expected_columns.issubset(row.keys()), f"row missing expected columns: {row.keys()}"


@pytest.mark.bigquery
@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), reason=_SF_RUN_BQ_REASON)
def test_int_column_stats_returns_correct_count_for_corpus() -> None:
    """``column_stats`` on Shakespeare's ``word`` column must report a
    positive ``count`` and ``distinct``, and a ``data_type`` that BigQuery
    recognises as a STRING-flavoured type."""
    adapter = BigQueryAdapter()
    with adapter:
        stats = adapter.column_stats(_shakespeare_ref(), "word")

    assert stats.count > 0
    assert stats.distinct > 0
    # BigQuery reports STRING columns as either "STRING" or (legacy)
    # "VARCHAR"; accept either to keep the test resilient against schema
    # surface drift in the public dataset.
    assert stats.data_type.upper() in {"STRING", "VARCHAR"}


@pytest.mark.bigquery
@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), reason=_SF_RUN_BQ_REASON)
def test_int_run_test_sql_passes_for_known_clean_query() -> None:
    """``WHERE FALSE`` always returns zero rows — the adapter must report
    ``passed=True`` and ``failure_count=0``."""
    adapter = BigQueryAdapter()
    result = adapter.run_test_sql(
        "SELECT * FROM `bigquery-public-data.samples.shakespeare` WHERE FALSE"
    )

    assert result.passed is True
    assert result.failure_count == 0


@pytest.mark.bigquery
@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), reason=_SF_RUN_BQ_REASON)
def test_int_run_test_sql_fails_for_known_dirty_query() -> None:
    """A test-SQL that intentionally returns rows must report
    ``passed=False``, a positive ``failure_count``, and a non-empty
    ``sample_failures`` list bounded by ``capture_failures=3``."""
    adapter = BigQueryAdapter()
    result = adapter.run_test_sql(
        "SELECT word FROM `bigquery-public-data.samples.shakespeare` LIMIT 5",
        capture_failures=3,
    )

    assert result.passed is False
    assert result.failure_count > 0
    assert result.sample_failures is not None
    assert len(result.sample_failures) > 0
    assert len(result.sample_failures) <= 3


@pytest.mark.bigquery
@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), reason=_SF_RUN_BQ_REASON)
def test_int_max_bytes_billed_blocks_oversize_query() -> None:
    """A 1-byte ``max_bytes_billed`` cap must trip BigQuery's pre-flight
    estimator and surface as :class:`BytesBilledExceededError` (DEC-015,
    DEC-028). Using Shakespeare keeps the test free — BigQuery rejects
    the query before any bytes are billed."""
    adapter = BigQueryAdapter(max_bytes_billed=1)

    with pytest.raises(BytesBilledExceededError):
        adapter.run_test_sql("SELECT * FROM `bigquery-public-data.samples.shakespeare` LIMIT 1")


@pytest.mark.bigquery
@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), reason=_SF_RUN_BQ_REASON)
def test_int_adc_unconfigured_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ADC is unconfigured the adapter must surface a typed
    :class:`WarehouseAuthError` (not a Google-internal exception). The
    client is constructed lazily, so the failure happens on first use —
    here, ``run_test_sql``."""
    import google.auth  # type: ignore[import-not-found]
    from google.auth.exceptions import (  # type: ignore[import-not-found]
        DefaultCredentialsError,
    )

    def fake_default(*args: object, **kwargs: object) -> object:
        raise DefaultCredentialsError("no credentials")

    monkeypatch.setattr(google.auth, "default", fake_default)

    adapter = BigQueryAdapter()
    with pytest.raises(WarehouseAuthError):
        adapter.run_test_sql("SELECT 1")
