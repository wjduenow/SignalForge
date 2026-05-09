"""AR-B1 / Q4=C cost-model probe: measure ``total_bytes_billed`` from a
deterministic-sample run against
``bigquery-public-data.iowa_liquor_sales.sales``.

Issue #22 (US-008 / DEC-007 / DEC-013) restructures the original
single-test probe into **three** ``@pytest.mark.bigquery`` tests so the
maintainer can see — in one run — the regression baseline AND the
post-Q4=C win AND positive proof that the session cleanup actually
works:

1. ``test_sample_rows_cost_baseline_oneshot`` — preserves AR-B1's
   regression guard. Issues the deterministic sample directly through
   the SDK seam against the source table (the v0.1 ``oneshot`` path)
   and asserts the cost cliff DOES exist (``bytes_billed >=
   _BYTES_WARN_AT``). The 9.92 GB AR-B1 measurement is the figure this
   test pins.
2. ``test_sample_rows_cost_materialised`` — the issue's primary
   acceptance criterion. Calls
   :meth:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter.materialise_sample`
   to produce a per-run ``_SESSION._sf_sample_<run_id>`` temp table,
   then runs a representative ``COUNT(*)`` test via
   :meth:`run_test_sql`, and asserts the **per-test** ``bytes_billed``
   drops below 100 MB (``_BYTES_PER_TEST_TARGET``).
3. ``test_materialised_session_cleaned_up_after_exit`` — positive
   proof of DEC-013: after the adapter's ``__exit__`` fires, querying
   the ``_SESSION._sf_sample_<run_id>`` temp table by name fails
   (``NotFound`` / "session not found" / "table not found"). Without
   this test the cleanup work in US-003 is unobservable from the
   outside — a buggy implementation that no-op'd ``CALL
   BQ.ABORT_SESSION()`` would still let the test "pass" because nothing
   would catch a leaking session.

Gating (mirrors ``tests/warehouse/test_bigquery_integration.py``):

* ``@pytest.mark.bigquery`` — the default ``addopts = "-m 'not bigquery
  and not anthropic and not cli_subprocess'"`` filter excludes these
  tests from the bare ``pytest`` run, so default CI never touches
  BigQuery.
* ``@pytest.mark.skipif(not _bq_runs_enabled(), ...)`` — even
  ``pytest -m bigquery`` skips at runtime unless the maintainer has
  opted in by exporting ``SF_RUN_BQ=1``.

To run locally::

    gcloud auth application-default login
    SF_RUN_BQ=1 pytest -m bigquery tests/warehouse/test_sample_cost_probe.py --no-cov

The ``--no-cov`` is required because ``--cov-fail-under`` in
``addopts`` would otherwise fail this marker-specific run that
exercises only a fraction of the codebase (see
``.claude/rules/testing-signal.md`` § Coverage / "Known gap: excluded
markers").

Each cost test captures ``total_bytes_billed`` directly off the
``QueryJob`` instance returned by ``client.query(...)`` — no
``INFORMATION_SCHEMA.JOBS_BY_USER`` round-trip, no race against
concurrent prune-stage jobs labelled with the same
``signalforge_stage`` tag. The deliberate departure from the adapter's
public ``sample_rows`` path is documented inline in
``_issue_sample_capture_bytes_billed``.

The baseline test's assertion is documentation-grade: a sanity ceiling
(15 GB) plus a soft WARNING at 500 MB so the figure shows up in the
test log even on a successful run. The materialised test, by
contrast, is a hard regression gate — under 100 MB or the build is
broken.
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
so the figure the probes record is directly comparable to what prune
will spend per test in v0.1."""

_BYTES_CEILING = 15_000_000_000
"""15 GB sanity ceiling. Not a budget — a runaway-cost circuit-breaker.
AR-B1 measured 9.92 GB on the 30M-row Iowa liquor sales table on
2026-05-01; the original 5 GB ceiling was inconsistent with the
recorded figure (caught during the maintainer probe-run on 2026-05-08).
The ceiling is now 15 GB: ~50% headroom over AR-B1 to absorb table
growth or full-row-scan amplification drift, while still failing loud
if BigQuery's optimiser starts scanning materially more.

Pinned by ``test_probe_constants_unchanged`` so an accidental edit to
this value (or its sibling ``_BYTES_WARN_AT``) breaks the
default-collected scaffolding test loudly rather than silently
relaxing the regression guard.
"""

_BOOTSTRAP_BYTES_BILLED_CAP = 20_000_000_000
"""20 GB cap on the BigQuery adapter when running the probe.

The default ``BigQueryAdapter(max_bytes_billed=100_000_000)`` (DEC-005
of #3 — 100 MB safety net) is intentionally too low to allow
``sample_rows``'s deterministic full-row scan OR ``materialise_sample``'s
one-time CTAS bootstrap to complete on the AR-B1 probe target.

For the maintainer probe specifically — where the goal is to MEASURE
the cost figures for ``docs/prune-ops.md`` rather than ship the
production safety net — we bump the cap to 20 GB. This permits the
bootstrap (~10 GB AR-B1 figure) to execute so the probe can record
the per-test bytes_billed AFTER materialisation (which is the issue's
acceptance gate). 20 GB is twice the AR-B1 measurement so the cap is
not the binding constraint — ``_BYTES_CEILING`` (15 GB) is.

Production users who hit this same wall on wide tables under
``sample_strategy=materialised`` can either raise their adapter's
``max_bytes_billed`` constructor arg OR set
``prune.sample_strategy: oneshot`` in ``signalforge.yml``. Adapter-side
per-stage cap overrides for the materialisation bootstrap are a v0.3
follow-up — see issue #22 PR description's "Maintainer follow-ups"
section.
"""

_BYTES_WARN_AT = 500_000_000
"""500 MB soft threshold. Crossing this is consistent with the AR-B1
worry that ``TO_JSON_STRING(t)`` triggers a full-row scan rather than
a column-pruned read. Emitting a WARNING (rather than failing) keeps
the baseline test documentation-grade — the figure surfaces in the log
so the reviewer can decide whether the post-Q4=C ``materialised`` path
solves the cliff (it does) or whether further work is needed.

The baseline test ALSO uses this constant as the regression-guard
floor: ``bytes_billed >= _BYTES_WARN_AT`` asserts the cost cliff
genuinely exists on the legacy ``oneshot`` path. If a future BigQuery
optimiser fix or a switch in dataset characteristics drops the
baseline below 500 MB, the test fails loud — at which point the
materialised path's win narrative needs revisiting and the issue
acceptance criterion may need to be re-derived.
"""

_BYTES_PER_TEST_TARGET = 100_000_000
"""100 MB hard cap on per-test ``bytes_billed`` for the materialised
strategy. The issue's primary acceptance criterion (#22 acceptance
checkbox: "per-test bytes_billed < 100 MB after Q4=C ships"). Per-test
queries hit the materialised ``_SESSION._sf_sample_<run_id>`` rows
column-pruned, so the ~10 GB cliff measured by the AR-B1 baseline
collapses to ~1 MB in practice — the 100 MB cap is two orders of
magnitude of headroom over the expected figure.
"""

_LOGGER = logging.getLogger("signalforge.warehouse.test.cost_probe")

_SF_RUN_BQ_REASON = "set SF_RUN_BQ=1 to run BigQuery integration probes"


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
# Helpers — direct SDK seam for cost capture.
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
    what production prune-stage runs will spend on the legacy
    ``oneshot`` strategy. Reading ``total_bytes_billed`` straight off
    the ``QueryJob`` instance eliminates the previous race where the
    ``INFORMATION_SCHEMA.JOBS_BY_USER`` lookup could have attributed
    bytes to the wrong job (a leftover prune-stage run, or a
    concurrent process emitting the same ``signalforge_stage`` label).
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
# Default-collected scaffolding tests (no markers; run by every ``pytest``).
# ---------------------------------------------------------------------------
#
# These two tests cost nothing (no SDK calls, no env vars) and exist
# purely to keep the marker-gated triad below honest:
#
# * ``test_probe_module_imports_and_exposes_three_test_functions``
#   pins the module shape so a refactor that accidentally drops one of
#   the three live-BQ tests (or renames them) breaks the default
#   ``pytest`` run — not just the maintainer-only ``-m bigquery`` run
#   that may be skipped for weeks at a time.
#
# * ``test_probe_constants_unchanged`` pins the regression-guard
#   thresholds so a "let's relax this to 1 GB" edit to ``_BYTES_WARN_AT``
#   gets caught at the same commit. The constants are part of the
#   probe's public regression contract — bumping them without an issue
#   reference is a silent narrative shift.


def test_probe_module_imports_and_exposes_three_test_functions() -> None:
    """Sanity-check the module shape: the three live-BQ tests exist
    and are decorated with ``@pytest.mark.bigquery``.

    Without this test, accidentally dropping or renaming one of the
    three real-BQ probe functions would only surface during the
    maintainer's pre-PR ``-m bigquery`` run — which may be days or
    weeks after the breakage landed. This scaffolding test runs on
    every ``pytest`` invocation and catches the regression at the same
    commit that introduces it.

    The marker check uses ``pytest.mark.bigquery`` introspection
    (``pytestmark`` / ``pytest.Mark`` collection on the function
    object) rather than re-decorating in this file, so a future
    refactor that replaces ``@pytest.mark.bigquery`` with a different
    gate (e.g., a parametrised marker) breaks this test loudly.
    """
    import tests.warehouse.test_sample_cost_probe as probe

    expected = {
        "test_sample_rows_cost_baseline_oneshot",
        "test_sample_rows_cost_materialised",
        "test_materialised_session_cleaned_up_after_exit",
    }
    actual = {
        name for name in dir(probe) if name.startswith("test_") and callable(getattr(probe, name))
    }
    missing = expected - actual
    assert not missing, f"probe module missing expected test functions: {sorted(missing)}"

    # Every live-BQ probe must carry the bigquery marker so default
    # ``pytest`` excludes it via the addopts marker filter.
    for fn_name in expected:
        fn = getattr(probe, fn_name)
        marks = getattr(fn, "pytestmark", [])
        marker_names = {m.name for m in marks}
        assert "bigquery" in marker_names, (
            f"{fn_name} missing @pytest.mark.bigquery; default pytest run "
            f"would execute it and hit the BigQuery network"
        )


def test_probe_constants_unchanged() -> None:
    """Pin the cost-probe thresholds so a silent edit can't soften the
    regression guard.

    ``_BYTES_CEILING`` (15 GB) is the runaway-cost circuit-breaker on
    the baseline ``oneshot`` test; ``_BYTES_WARN_AT`` (500 MB) is the
    soft floor that asserts the AR-B1 cost cliff genuinely exists on
    the legacy path. Together they are the contract that the
    materialised path's <100 MB win is measuring against.

    If a future ticket genuinely needs to move these values (e.g., a
    BigQuery optimiser change reduces baseline bytes), update both
    this test AND the constants in lockstep — the test name plus the
    DEC reference in the docstring is the audit trail.
    """
    assert _BYTES_CEILING == 15_000_000_000, (
        f"_BYTES_CEILING moved silently: got {_BYTES_CEILING}; "
        f"the 15 GB sanity ceiling is the regression-guard contract for "
        f"test_sample_rows_cost_baseline_oneshot (raised from 5 GB to "
        f"accommodate AR-B1's 9.92 GB measurement; see the constant's "
        f"docstring for the 2026-05-08 maintainer-run audit trail)"
    )
    assert _BYTES_WARN_AT == 500_000_000, (
        f"_BYTES_WARN_AT moved silently: got {_BYTES_WARN_AT}; "
        f"the 500 MB soft threshold is what asserts the AR-B1 cost cliff "
        f"exists on the legacy oneshot path"
    )
    assert _BYTES_PER_TEST_TARGET == 100_000_000, (
        f"_BYTES_PER_TEST_TARGET moved silently: got {_BYTES_PER_TEST_TARGET}; "
        f"the 100 MB cap is issue #22's primary acceptance criterion for "
        f"the materialised-strategy per-test cost"
    )


# ---------------------------------------------------------------------------
# (1) Baseline regression guard — the v0.1 ``oneshot`` path.
# ---------------------------------------------------------------------------


@pytest.mark.bigquery
@pytest.mark.skipif(not _bq_runs_enabled(), reason=_SF_RUN_BQ_REASON)
def test_sample_rows_cost_baseline_oneshot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard for the AR-B1 9.92 GB measurement on the v0.1
    ``oneshot`` sample path (DEC-007 of issue #22).

    Asserts:

    * ``bytes_billed >= _BYTES_WARN_AT`` (500 MB) — positive proof the
      cost cliff exists on the legacy path; without this the
      materialised-path win narrative is unfalsifiable.
    * ``bytes_billed < _BYTES_CEILING`` (15 GB) — sanity ceiling, not a
      budget. A runaway above this means the assumption that the Iowa
      liquor sales table costs roughly its on-disk size is broken and
      the probe's other assertions can't be trusted.
    * If ``bytes_billed >= _BYTES_WARN_AT``, a WARNING fires carrying
      the figure so the run log records it for ``docs/prune-ops.md``
      Cost-model section.

    Uses the SDK seam directly (rather than the adapter's public
    ``sample_rows``) so the ``QueryJob.total_bytes_billed`` figure can
    be read off the just-issued job — no ``INFORMATION_SCHEMA``
    round-trip, no race against concurrent prune-stage jobs labelled
    with the same ``signalforge_stage`` tag. Adapter-side
    bytes_billed exposure on the public surface remains a v0.2
    concern (DEC-027 of issue #6).
    """
    adapter = BigQueryAdapter(max_bytes_billed=_BOOTSTRAP_BYTES_BILLED_CAP)

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
        "sample_rows oneshot baseline: total_bytes_billed=%d sample_size=%d "
        "elapsed_s=%.2f table=%s",
        bytes_billed,
        _SAMPLE_SIZE,
        elapsed_s,
        _TARGET.qualified_name,
    )

    if bytes_billed >= _BYTES_WARN_AT:
        with caplog.at_level(logging.WARNING, logger=_LOGGER.name):
            _LOGGER.warning(
                "sample_rows bytes_billed exceeds soft threshold (expected on "
                "the legacy oneshot path): bytes_billed=%d threshold=%d "
                "table=%s. Consistent with AR-B1 (TO_JSON_STRING(t) triggers "
                "full-row scan); the materialised-strategy probe should drop "
                "per-test bytes well below 100 MB.",
                bytes_billed,
                _BYTES_WARN_AT,
                _TARGET.qualified_name,
            )
        assert any(
            record.levelno == logging.WARNING
            and "bytes_billed exceeds soft threshold" in record.getMessage()
            for record in caplog.records
        ), "expected the WARNING to be captured by caplog"

    # Regression guard (DEC-007 of issue #22): the cost cliff MUST exist
    # on the legacy oneshot path. If a future change drops the baseline
    # below the soft floor, the materialised-path win narrative needs
    # revisiting — the assertion fails loud rather than silently letting
    # the materialised target slide.
    assert bytes_billed >= _BYTES_WARN_AT, (
        f"sample_rows oneshot bytes_billed={bytes_billed} fell BELOW the "
        f"AR-B1 regression floor {_BYTES_WARN_AT}; the cost cliff on the "
        f"legacy path appears to have lifted. Re-evaluate the issue #22 "
        f"acceptance criterion before relaxing the materialised target."
    )

    assert bytes_billed < _BYTES_CEILING, (
        f"sample_rows oneshot bytes_billed={bytes_billed} exceeds sanity "
        f"ceiling {_BYTES_CEILING}; investigate before relying on the "
        f"v0.1 one-query-per-test prune cost model."
    )


# ---------------------------------------------------------------------------
# (2) Acceptance criterion — Q4=C materialised strategy.
# ---------------------------------------------------------------------------


@pytest.mark.bigquery
@pytest.mark.skipif(not _bq_runs_enabled(), reason=_SF_RUN_BQ_REASON)
def test_sample_rows_cost_materialised() -> None:
    """Issue #22 primary acceptance criterion: per-test
    ``bytes_billed`` < 100 MB on the Q4=C materialised strategy
    (DEC-007 of issue #22).

    Drives the adapter's public materialise/run-test seam end-to-end:

    1. ``materialise_sample`` opens a BigQuery session and issues the
       ``CREATE TEMP TABLE _sf_sample_<run_id> AS SELECT ...`` CTAS.
       The materialisation cost is captured separately and logged
       (NOT asserted against the per-test target — the materialisation
       is a one-time-per-run cost amortised across N tests).
    2. ``run_test_sql`` issues a representative ``COUNT(*)``-style
       failing-rows test against the materialised temp table. The
       per-test ``bytes_billed`` is captured via the same SDK seam as
       the baseline test.
    3. Per-test ``bytes_billed`` MUST be below
       ``_BYTES_PER_TEST_TARGET`` (100 MB) — the issue's hard
       acceptance gate. Per-test queries hit the materialised rows
       column-pruned, so the ~10 GB AR-B1 cliff collapses to ~1 MB in
       practice; the 100 MB cap is two orders of magnitude of
       headroom.

    The test runs INSIDE the adapter context manager so
    ``__exit__``'s DEC-013 cleanup fires after the assertions pass —
    leaving an orphan session would impact the maintainer's quota
    budget across runs.

    Uses the same direct-SDK ``client.query`` seam as the baseline
    test for the per-test cost capture so the two figures are
    apples-to-apples (both read ``QueryJob.total_bytes_billed`` off
    the just-issued job, no ``INFORMATION_SCHEMA`` round-trip).
    """
    adapter = BigQueryAdapter(max_bytes_billed=_BOOTSTRAP_BYTES_BILLED_CAP)

    per_test_bytes = -1
    with adapter:
        # (1) Materialise the sample. The session_id is captured on
        # ``adapter._active_session_id`` and routed through every
        # subsequent ``run_test_sql`` call via connection_properties
        # (DEC-002 of #22). The returned ``temp_ref.qualified_name``
        # is the two-part ``_SESSION._sf_sample_<run_id>`` —
        # ``project=None`` on the returned TableRef is load-bearing
        # because BigQuery rejects the three-part
        # ``<project>._SESSION.<name>`` form even inside the owning
        # session.
        started_at_materialise = time.monotonic()
        temp_ref = adapter.materialise_sample(
            _TARGET,
            _SAMPLE_SIZE,
        )
        elapsed_materialise_s = time.monotonic() - started_at_materialise

        # We don't have a direct seam to capture ``bytes_billed`` for
        # the CTAS itself (the adapter doesn't return the QueryJob),
        # but it's not the figure under test — the issue's acceptance
        # criterion is the **per-test** bytes, not the
        # materialisation cost. Log the materialisation duration so
        # the maintainer's ops record captures both figures.
        _LOGGER.info(
            "materialised sample for cost probe: temp_ref=%s elapsed_materialise_s=%.2f",
            temp_ref.qualified_name,
            elapsed_materialise_s,
        )

        # (2) Issue a representative per-test query directly through
        # the SDK seam so we can read total_bytes_billed off the
        # QueryJob. Mirrors the SQL shape that
        # ``BigQueryAdapter.run_test_sql`` would wrap (a SELECT-* over
        # the sample rows, COUNT-aggregated). The session_id flows
        # through ``_default_job_config(stage=..., session_id=...)``
        # so the per-test query routes into the same session and can
        # resolve ``_SESSION._sf_sample_<run_id>``.
        client = adapter._get_client()  # noqa: SLF001 - documented seam
        # Mirror what prune's `not_null` compiler emits — a single-column
        # IS NULL scan over the materialised rows. ``invoice_and_item_number``
        # is the iowa_liquor_sales primary-key column, so a real NOT NULL
        # test against it is the canonical shape. The original probe
        # query (``WHERE FALSE``) short-circuited in BQ's planner before
        # any column scan, billing zero bytes — meaningless as a
        # post-Q4=C cost figure. Caught during the maintainer probe-run
        # on 2026-05-08.
        per_test_sql = (
            f"SELECT COUNT(*) AS failures FROM `{temp_ref.qualified_name}` "
            f"WHERE invoice_and_item_number IS NULL"
        )
        started_at_test = time.monotonic()
        job = client.query(
            per_test_sql,
            job_config=adapter._default_job_config(  # noqa: SLF001
                stage="warehouse_test",
                session_id=adapter._active_session_id,  # noqa: SLF001
            ),
        )
        list(job.result())
        elapsed_test_s = time.monotonic() - started_at_test
        # ``or -1`` would corrupt a legitimate 0-byte measurement
        # (``0 or -1 == -1``). Treat None / missing as unavailable; treat
        # 0 as a valid measurement (BQ may bill 0 bytes for queries that
        # the planner can satisfy from metadata alone).
        raw_bytes_billed = getattr(job, "total_bytes_billed", None)
        per_test_bytes = -1 if raw_bytes_billed is None else int(raw_bytes_billed)

        _LOGGER.info(
            "per-test cost probe (materialised): bytes_billed=%d elapsed_test_s=%.2f temp_ref=%s",
            per_test_bytes,
            elapsed_test_s,
            temp_ref.qualified_name,
        )

    if per_test_bytes < 0:  # pragma: no cover - environment-dependent
        pytest.xfail(
            "QueryJob.total_bytes_billed was unavailable on the just-issued "
            "per-test job (rare; non-dry-run jobs populate this field)."
        )

    # (3) The hard regression gate. If this fails, the Q4=C
    # materialised strategy is not delivering the per-test cost win
    # the issue committed to — the build is broken until it is.
    assert per_test_bytes < _BYTES_PER_TEST_TARGET, (
        f"materialised per-test bytes_billed={per_test_bytes} exceeds the "
        f"100 MB acceptance target ({_BYTES_PER_TEST_TARGET}); the Q4=C "
        f"materialisation strategy is not delivering the cost win "
        f"committed to in issue #22's acceptance criterion."
    )


# ---------------------------------------------------------------------------
# (3) Cleanup verification — DEC-013 positive proof.
# ---------------------------------------------------------------------------


@pytest.mark.bigquery
@pytest.mark.skipif(not _bq_runs_enabled(), reason=_SF_RUN_BQ_REASON)
def test_materialised_session_cleaned_up_after_exit() -> None:
    """Positive proof of DEC-013 cleanup: after ``__exit__`` fires,
    querying the ``_SESSION._sf_sample_<run_id>`` temp table by name
    fails (DEC-013 / DEC-007 of issue #22).

    Without this test, a buggy ``__exit__`` that no-op'd ``CALL
    BQ.ABORT_SESSION()`` would not be observable from the outside —
    every other test in the suite is happy-path-inside-the-context;
    only this test asserts that the cleanup work actually tore the
    session down.

    Test shape:

    1. Open the adapter as a context manager.
    2. Call ``materialise_sample`` to create
       ``_SESSION._sf_sample_<run_id>``; capture the returned
       :class:`TableRef`.
    3. Exit the context (``__exit__`` fires; DEC-013 issues ``CALL
       BQ.ABORT_SESSION()``; the session and every ``_SESSION.*``
       table inside it are torn down server-side).
    4. AFTER ``__exit__``, attempt to query the temp table by its
       qualified name through a fresh SDK call (no session id
       routing, because the session no longer exists). The query
       MUST fail.

    BigQuery surfaces the failure through one of several shapes:
    ``google.api_core.exceptions.NotFound``, ``BadRequest`` carrying
    "session not found" / "table not found" / "Unrecognized name", or
    a generic ``GoogleAPIError``. The test asserts on the broad
    ``GoogleAPIError`` superclass so a future BigQuery error-message
    refactor doesn't make the test brittle, while still catching the
    real-world "the cleanup didn't work" regression.

    A buggy implementation that left the session alive would make the
    follow-up query SUCCEED (the temp table would still exist), and
    the test would fail because the expected exception didn't fire —
    making this a true regression guard rather than an assertion that
    can't fail (testing-signal.md DEC-010).
    """
    # Lazy import so the default-collected scaffolding tests don't
    # require ``google-cloud-bigquery`` on the import path. The
    # ``@pytest.mark.bigquery`` gate already excludes this test from
    # the default run, but lazy import keeps the import cost off the
    # collection phase.
    from google.api_core import exceptions as gae

    adapter = BigQueryAdapter(max_bytes_billed=_BOOTSTRAP_BYTES_BILLED_CAP)

    captured_session_id: str | None = None
    with adapter:
        temp_ref = adapter.materialise_sample(
            _TARGET,
            _SAMPLE_SIZE,
        )
        # Capture the active session_id BEFORE leaving the context so
        # the post-exit query below can route through the *same*
        # session. Without this capture the post-exit query would have
        # no session attached, and the assertion would pass for the
        # wrong reason — a fresh-SDK query against the two-part
        # ``_SESSION._sf_sample_<...>`` name fails regardless of
        # whether ``__exit__`` aborted the session.
        captured_session_id = adapter._active_session_id  # noqa: SLF001
        assert captured_session_id is not None, (
            "materialise_sample did not record an active session_id; "
            "DEC-013 cleanup verification cannot proceed"
        )

    # __exit__ has fired by now — DEC-013 cleanup ran. The adapter's
    # state must be reset (``_cleanup_active_session`` resets in its
    # inner ``finally``).
    assert adapter._active_session_id is None, (  # noqa: SLF001
        "adapter._active_session_id is still set after __exit__; "
        "DEC-013 cleanup did not run its finally-block reset"
    )

    # Issue the post-exit query routed through the *captured* session
    # id. This is the load-bearing assertion: if DEC-013 cleanup
    # actually aborted the session, BigQuery surfaces "session not
    # found" (or similar) when the query references that session id;
    # if cleanup leaked, the temp table would still resolve inside the
    # captured session and the query would succeed — the
    # ``pytest.raises`` would fail with "DID NOT RAISE", catching the
    # regression. Without the captured ``session_id`` on the wire the
    # query would fail for an unrelated reason ("two-part _SESSION
    # name has no namespace") and the test would pass even when
    # cleanup was broken.
    client = adapter._get_client()  # noqa: SLF001 - documented seam

    with pytest.raises(gae.GoogleAPIError) as excinfo:
        job = client.query(
            f"SELECT COUNT(*) FROM `{temp_ref.qualified_name}`",
            job_config=adapter._default_job_config(  # noqa: SLF001
                stage="warehouse_test",
                session_id=captured_session_id,
            ),
        )
        list(job.result())

    # The error message text varies (``Not found``, ``Unrecognized
    # name``, ``session not found``), so we don't assert on the
    # message string — only that the broad ``GoogleAPIError`` class
    # fires. The error type alone is sufficient signal that the temp
    # table is gone; a buggy cleanup that left the session alive
    # would have made ``client.query(...)`` succeed and the
    # ``pytest.raises`` block would fail with "DID NOT RAISE".
    _LOGGER.info(
        "DEC-013 cleanup verified: temp_ref=%s post-__exit__ query failed with %s",
        temp_ref.qualified_name,
        type(excinfo.value).__name__,
    )
