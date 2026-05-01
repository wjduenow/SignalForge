"""AR-B1 cost-model probe: measure ``total_bytes_billed`` from a
deterministic-sample run against
``bigquery-public-data.iowa_liquor_sales.sales``.

The Phase-1 cost model (issue #6, plan ``plans/super/6-prune-engine.md``)
assumed sample-mode is cheap because the failing-rows SQL only reads one
column. But the deterministic sampler uses

    WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), bucket) < 1

which serialises the **whole row** into the predicate — BigQuery cannot
column-prune through a function argument. AR-B1 records the worry that
sample-mode could be 50–500x more expensive than the original "1 column
x 100k rows" estimate. This probe records the live figure so the
reviewer can confirm or refute that worry against a real public table.

Gating (mirrors ``tests/warehouse/test_bigquery_integration.py``):

* ``@pytest.mark.bigquery`` — the default ``addopts = "-m 'not bigquery
  and not anthropic'"`` filter excludes this test from the bare
  ``pytest`` run, so default CI never touches BigQuery.
* ``@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), ...)`` — even
  ``pytest -m bigquery`` skips at runtime unless the maintainer has
  opted in by exporting ``SF_RUN_BQ=1``.

To run locally::

    gcloud auth application-default login
    SF_RUN_BQ=1 pytest -m bigquery tests/warehouse/test_sample_cost_probe.py

The test is **documentation-grade**, not regression-grade: the assertion
is a sanity ceiling (5 GB) rather than a tight budget, and a soft
WARNING fires at 500 MB so the figure shows up in the test log even on
a successful run. US-014 (``docs/prune-ops.md``) folds the recorded
figure into the cost-model section.

If the post-call ``INFORMATION_SCHEMA.JOBS_BY_USER`` lookup fails (IAM,
region availability, eventual-consistency lag, etc.), the test marks
itself ``xfail`` with the documented reason rather than failing the
suite — exposing ``total_bytes_billed`` cleanly via the adapter is a
v0.2 concern (DEC-027 of issue #6, ``plans/super/6-prune-engine.md``)
and out of scope for this story.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import pytest

from signalforge.warehouse.adapters.bigquery import BigQueryAdapter
from signalforge.warehouse.models import TableRef

# ---------------------------------------------------------------------------
# Probe target + thresholds.
# ---------------------------------------------------------------------------

_TARGET = TableRef(
    project="bigquery-public-data",
    dataset="iowa_liquor_sales",
    name="sales",
)
"""Iowa liquor sales: ~30M rows, ~24 columns. Public; covered by the
BigQuery 1 TB/month free tier for typical maintainer use."""

_SAMPLE_SIZE = 100_000
"""Matches the ``PruneConfig.sample_size`` default in the Phase-1 plan
so the figure the probe records is directly comparable to what prune
will spend per test in v0.1."""

_BYTES_CEILING = 5_000_000_000
"""5 GB sanity ceiling. Not a budget — a runaway-cost circuit-breaker.
The Iowa liquor sales table is roughly 4 GB on disk; the deterministic
sampler reading every column should not exceed this even with full-row
scan amplification."""

_BYTES_WARN_AT = 500_000_000
"""500 MB soft threshold. Crossing this is consistent with the AR-B1
worry that ``TO_JSON_STRING(t)`` triggers a full-row scan rather than
a column-pruned read. Emitting a WARNING (rather than failing) keeps
the test documentation-grade — the figure surfaces in the log so the
reviewer can decide whether v0.2 should escalate to Q4=C
(temp-table-materialised sample)."""

_LOGGER = logging.getLogger("signalforge.warehouse.test.cost_probe")


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _adapter_billing_project(adapter: BigQueryAdapter) -> str:
    """Return the billing project the adapter resolves to.

    ``BigQueryAdapter`` does not expose its underlying client publicly;
    we reach through the private ``_get_client`` to read ``project``
    because the probe needs a region-qualified
    ``INFORMATION_SCHEMA.JOBS_BY_USER`` lookup. This is test-only access
    — production code routes through the adapter API.
    """
    # Accessing a private member is intentional: the probe is a
    # diagnostic that lives outside the adapter's public contract.
    client = adapter._get_client()  # noqa: SLF001
    return str(client.project)


def _lookup_total_bytes_billed(
    adapter: BigQueryAdapter,
    *,
    started_after_epoch_s: float,
) -> int | None:
    """Query ``INFORMATION_SCHEMA.JOBS_BY_USER`` for the most recent
    ``warehouse_sample`` job and return its ``total_bytes_billed``.

    Returns ``None`` if no matching job is found (eventual-consistency
    lag) or if the lookup itself fails for any reason — the caller then
    ``xfail``s with the documented reason. We deliberately do NOT
    propagate the exception: a probe that hard-fails on environmental
    quirks (regional dataset availability, missing
    ``bigquery.jobs.list`` permission, etc.) is worse than no probe.

    The query filters on the ``signalforge_stage`` label that
    ``BigQueryAdapter._default_job_config`` stamps on every job
    (DEC-015 of issue #3). Filtering by label scopes the result to the
    sample we just issued without needing the job-id round-trip.
    """
    project = _adapter_billing_project(adapter)
    # ``INFORMATION_SCHEMA.JOBS_BY_USER`` is region-qualified. We
    # default to US (the multi-region the public ``bigquery-public-data``
    # tables live in) when the adapter has no explicit location.
    region = adapter._location or "US"  # noqa: SLF001
    region_qualifier = f"region-{region.lower()}"

    # ``creation_time`` is a TIMESTAMP; the probe records monotonic
    # epoch seconds and we compare against TIMESTAMP_SECONDS at lookup.
    sql = (
        f"SELECT total_bytes_billed "
        f"FROM `{project}.{region_qualifier}.INFORMATION_SCHEMA.JOBS_BY_USER` "
        f"WHERE creation_time >= TIMESTAMP_SECONDS(@since) "
        f"AND EXISTS (SELECT 1 FROM UNNEST(labels) AS l "
        f"WHERE l.key = 'signalforge_stage' AND l.value = 'warehouse_sample') "
        f"ORDER BY creation_time DESC LIMIT 1"
    )

    try:
        # Build an ad-hoc query-parameterised job. We bypass the
        # adapter's ``run_test_sql`` because the probe SQL is not a
        # failing-rows test — it's a metadata read.
        from google.cloud import bigquery  # type: ignore[import-not-found]

        client = adapter._get_client()  # noqa: SLF001
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("since", "INT64", int(started_after_epoch_s)),
            ],
            use_query_cache=False,
        )
        rows = list(client.query(sql, job_config=job_config).result())
    except Exception as exc:  # pragma: no cover - environment-dependent
        _LOGGER.info(
            "INFORMATION_SCHEMA lookup failed; the probe will xfail. error_class=%s",
            type(exc).__name__,
        )
        return None

    if not rows:
        return None
    bytes_billed = getattr(rows[0], "total_bytes_billed", None)
    if bytes_billed is None:
        # The row exists but the column is null — for a non-dry-run
        # query this should not happen, but treat as "unknown" rather
        # than zero so the xfail path engages cleanly.
        return None
    return int(bytes_billed)


# ---------------------------------------------------------------------------
# The probe.
# ---------------------------------------------------------------------------


@pytest.mark.bigquery
@pytest.mark.skipif(
    not os.environ.get("SF_RUN_BQ"),
    reason="set SF_RUN_BQ=1 to run BigQuery integration probes",
)
def test_sample_rows_bytes_billed_within_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Measure ``total_bytes_billed`` for a deterministic 100k-row
    sample of ``bigquery-public-data.iowa_liquor_sales.sales``.

    Asserts:

    * ``bytes_billed < 5 GB`` (sanity ceiling, not a budget).
    * If ``bytes_billed >= 500 MB``, a WARNING fires carrying the figure
      so the run log records it for ``docs/prune-ops.md``.

    If the post-call ``INFORMATION_SCHEMA`` lookup yields no row
    (eventual-consistency lag) or fails for any other reason, the test
    marks itself ``xfail`` with the documented reason. The probe is
    documentation-grade: a missing figure is acceptable; a wrong figure
    is not.
    """
    adapter = BigQueryAdapter()

    started_at_epoch_s = time.time()
    started_at_monotonic = time.monotonic()
    with adapter:
        rows: list[dict[str, Any]] = adapter.sample_rows(_TARGET, _SAMPLE_SIZE)
    elapsed_s = time.monotonic() - started_at_monotonic

    # Belt-and-suspenders: the sampler must actually return rows. If
    # this fails the cost figure is meaningless.
    assert rows, "expected at least one sampled row from a 30M-row table"
    assert len(rows) <= _SAMPLE_SIZE

    bytes_billed = _lookup_total_bytes_billed(adapter, started_after_epoch_s=started_at_epoch_s)
    if bytes_billed is None:
        pytest.xfail(
            "bytes-billed retrieval via INFORMATION_SCHEMA.JOBS_BY_USER did "
            "not return a matching row (IAM, regional availability, or "
            "eventual-consistency lag). Adapter-side bytes_billed exposure "
            "is a v0.2 concern (DEC-027 of issue #6)."
        )

    _LOGGER.info(
        "sample_rows cost probe: total_bytes_billed=%d sample_size=%d elapsed_s=%.2f table=%s",
        bytes_billed,
        _SAMPLE_SIZE,
        elapsed_s,
        _TARGET.qualified_name,
    )

    if bytes_billed >= _BYTES_WARN_AT:
        with caplog.at_level(logging.WARNING, logger=_LOGGER.name):
            _LOGGER.warning(
                "sample_rows bytes_billed exceeds soft threshold: "
                "bytes_billed=%d threshold=%d table=%s. "
                "Consistent with AR-B1 (TO_JSON_STRING(t) triggers full-row "
                "scan); v0.2 should evaluate Q4=C (temp-table-materialised "
                "sample).",
                bytes_billed,
                _BYTES_WARN_AT,
                _TARGET.qualified_name,
            )
        assert any(
            record.levelno == logging.WARNING
            and "bytes_billed exceeds soft threshold" in record.getMessage()
            for record in caplog.records
        ), "expected the WARNING to be captured by caplog"

    assert bytes_billed < _BYTES_CEILING, (
        f"sample_rows bytes_billed={bytes_billed} exceeds sanity ceiling "
        f"{_BYTES_CEILING}; investigate before relying on the v0.1 "
        f"one-query-per-test prune cost model."
    )
