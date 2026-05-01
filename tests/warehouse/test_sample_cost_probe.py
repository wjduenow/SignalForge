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

The probe captures ``total_bytes_billed`` directly off the
``QueryJob`` instance returned by ``client.query(...)`` — no
``INFORMATION_SCHEMA.JOBS_BY_USER`` round-trip, no race against
concurrent prune-stage jobs labelled with the same
``signalforge_stage`` tag. The deliberate departure from the adapter's
public ``sample_rows`` path is documented inline.
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


def _bq_runs_enabled() -> bool:
    """Return ``True`` when the user opted into BigQuery integration runs.

    Restricts the set of "truthy" ``SF_RUN_BQ`` values to the explicit
    affirmatives. The naive ``not os.environ.get("SF_RUN_BQ")`` would
    treat ``SF_RUN_BQ=0``, ``SF_RUN_BQ=false``, ``SF_RUN_BQ=no`` as
    truthy (any non-empty string) — surprising behaviour for a user
    trying to turn the runs OFF.
    """
    return os.environ.get("SF_RUN_BQ", "").lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _issue_sample_capture_bytes_billed(
    adapter: BigQueryAdapter,
    *,
    target: TableRef,
    sample_size: int,
) -> tuple[list[dict[str, Any]], int]:
    """Issue the deterministic sample directly through the SDK shim and
    return ``(rows, total_bytes_billed)``.

    The probe deliberately bypasses the adapter's public
    :meth:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter.sample_rows`
    so it can read ``QueryJob.total_bytes_billed`` after the job
    completes. The adapter's public surface drops the ``QueryJob``
    after iterating its rows; widening the public API to expose job
    stats is a v0.2 concern (DEC-027 of issue #6).

    The probe replicates the adapter's deterministic-sample SQL shape
    (DEC-006 of issue #3) byte-for-byte so the figure recorded matches
    what production prune-stage runs will spend. Reading
    ``total_bytes_billed`` straight off the ``QueryJob`` instance
    eliminates the previous race where the
    ``INFORMATION_SCHEMA.JOBS_BY_USER`` lookup could have attributed
    bytes to the wrong job (a leftover prune-stage run, or a concurrent
    process emitting the same ``signalforge_stage`` label).
    """
    client = adapter._get_client()  # noqa: SLF001  # documented seam
    table = client.get_table(target.qualified_name)
    num_rows = getattr(table, "num_rows", None)
    if not num_rows:
        # Public dataset; this is a defensive guard rather than a
        # production-relevant path. If ever it fires the probe
        # short-circuits via xfail in the caller.
        raise RuntimeError(
            f"sample probe: public table {target.qualified_name!r} returned "
            f"unknown num_rows; cannot derive bucket"
        )
    bucket = max(num_rows // sample_size, 1)

    sql = (
        f"SELECT * FROM `{target.qualified_name}` AS t "
        f"WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), {bucket}) < 1 "
        f"LIMIT {sample_size}"
    )

    job = client.query(
        sql,
        job_config=adapter._default_job_config(stage="warehouse_sample"),  # noqa: SLF001
    )
    rows = list(job.result())  # consume to ensure completion
    total_bytes_billed = getattr(job, "total_bytes_billed", None)
    if total_bytes_billed is None:  # pragma: no cover - environment-dependent
        # Non-dry-run jobs always populate this; if absent treat as
        # unknown via -1 so the caller can xfail cleanly.
        total_bytes_billed = -1
    rows_dict: list[dict[str, Any]] = [
        dict(r.items()) if hasattr(r, "items") else dict(r) for r in rows
    ]
    return rows_dict, int(total_bytes_billed)


# ---------------------------------------------------------------------------
# The probe.
# ---------------------------------------------------------------------------


@pytest.mark.bigquery
@pytest.mark.skipif(
    not _bq_runs_enabled(),
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

    Uses the SDK seam directly (rather than the adapter's public
    ``sample_rows``) so the ``QueryJob.total_bytes_billed`` figure can
    be read off the just-issued job — no ``INFORMATION_SCHEMA``
    round-trip, no race against concurrent prune-stage jobs labelled
    with the same ``signalforge_stage`` tag. Adapter-side bytes_billed
    exposure on the public surface remains a v0.2 concern (DEC-027 of
    issue #6).
    """
    adapter = BigQueryAdapter()

    started_at_monotonic = time.monotonic()
    with adapter:
        rows, bytes_billed = _issue_sample_capture_bytes_billed(
            adapter,
            target=_TARGET,
            sample_size=_SAMPLE_SIZE,
        )
    elapsed_s = time.monotonic() - started_at_monotonic

    # Belt-and-suspenders: the sampler must actually return rows. If
    # this fails the cost figure is meaningless.
    assert rows, "expected at least one sampled row from a 30M-row table"
    assert len(rows) <= _SAMPLE_SIZE

    if bytes_billed < 0:  # pragma: no cover - environment-dependent
        pytest.xfail(
            "QueryJob.total_bytes_billed was unavailable on the just-issued "
            "sample job (rare; non-dry-run jobs populate this field). "
            "Adapter-side bytes_billed exposure is a v0.2 concern (DEC-027 of "
            "issue #6)."
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
