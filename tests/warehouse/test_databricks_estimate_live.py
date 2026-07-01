"""Gated live ``EXPLAIN COST`` estimate against a real Databricks (#226 US-003).

The offline parser is already pinned against hand-crafted fixtures
(``tests/warehouse/test_databricks_estimate.py`` +
``tests/fixtures/warehouse/databricks/``). Ralph workers and CI cannot reach a
live Databricks SQL warehouse, so that fixture pins *shape* only. This module is
the belt-and-suspenders other half: a ``@pytest.mark.databricks``-gated test
that drives a **real** :class:`DatabricksAdapter` through
:meth:`estimate_query_bytes` against a live Free-Edition warehouse to certify
that the committed fixture's parse (the MAX Spark CBO
``Statistics(sizeInBytes=...)`` across plan nodes) still matches what Databricks
actually returns from ``EXPLAIN COST``.

Gating + the live-adapter builder live in the SHARED
:mod:`tests.warehouse._databricks_live` helper (this is the first of the #226
live tests — US-004 prune + US-005 CLI e2e reuse the same helper rather than
re-declaring the gate, the way the two Snowflake live files each duplicate it).

Engineered-determinism caveat: ``EXPLAIN COST`` figures are Spark *CBO
estimates* whose accuracy depends on ``ANALYZE TABLE`` / Delta-log stats
freshness, and they vary across runtimes (mirrors the Snowflake ``EXPLAIN``
planner-estimate caveat and the ``HASH()`` reproducibility caveat from #121).
This test asserts SHAPE + positivity (a parseable ``int > 0`` from a real table
scan), NEVER an exact byte value. ``samples.nyctaxi.trips`` is a Delta table in
the always-present ``samples`` catalog, so it carries a real transaction-log
size and never trips the ``8.0 EiB`` (``Long.MaxValue``) no-stats sentinel that
:func:`_parse_explain_cost_bytes` degrades on.

Traces to: plans/super/226-* DEC-* / US-003 ; epic #219.
"""

from __future__ import annotations

import pytest

from tests.warehouse._databricks_live import build_live_adapter, skip_reason


@pytest.mark.databricks
def test_estimate_query_bytes_live_explain_cost_returns_positive_int() -> None:
    """A real ``DatabricksAdapter.estimate_query_bytes`` over a live EXPLAIN COST.

    Skips cleanly under ``pytest -m databricks`` when any prerequisite is
    missing. With credentials present, constructs a real adapter (NOT a fake),
    runs ``estimate_query_bytes("SELECT * FROM samples.nyctaxi.trips")``, and
    asserts the result is a parseable ``int > 0``. The query scans a real Delta
    table in the always-present ``samples`` catalog, so the CBO carries a
    genuine ``sizeInBytes`` and the estimate is strictly positive — the contract
    being certified is "the parser extracts a real
    ``Statistics(sizeInBytes=...)`` from a live ``EXPLAIN COST`` plan," not a
    specific magnitude.
    """
    if reason := skip_reason():
        pytest.skip(reason)

    adapter = build_live_adapter()
    with adapter:
        bytes_est = adapter.estimate_query_bytes("SELECT * FROM samples.nyctaxi.trips")

    assert isinstance(bytes_est, int)
    assert bytes_est > 0
