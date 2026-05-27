"""Gated live ``EXPLAIN USING JSON`` estimate against a real Snowflake (#130 US-005).

The offline parser is already pinned against the hand-crafted fixtures
(``tests/warehouse/test_snowflake_estimate.py`` +
``tests/fixtures/warehouse/snowflake/``). Ralph workers and CI cannot reach a
live Snowflake account, so that fixture pins *shape* only. This module is the
belt-and-suspenders other half (DEC-006 of
``plans/super/130-snowflake-estimate-explain.md``): a ``@pytest.mark.snowflake``-
gated test that drives a **real** :class:`SnowflakeAdapter` through
:meth:`estimate_query_bytes` against a live warehouse to certify that the
committed fixture's shape (``GlobalStats.bytesAssigned``) still matches what
Snowflake actually returns.

Belt-and-suspenders gating (``.claude/rules/testing-signal.md`` § "End-to-end
gated tests"):

1. ``@pytest.mark.snowflake`` — registered in ``pyproject.toml``
   ``[tool.pytest.ini_options].markers`` and deselected by the default
   ``addopts`` (``-m '... and not snowflake'``), so the default ``pytest`` run
   never collects this test.
2. A runtime :func:`_skip_reason` — when a maintainer runs
   ``pytest -m snowflake`` but lacks credentials, each missing prerequisite
   surfaces as a distinct skip-with-reason rather than a confusing connection
   error.

The required env vars mirror what :meth:`SnowflakeAdapter._get_connection`
(via ``make_real_client``) consumes when no connection is injected:

* ``SF_RUN_SNOWFLAKE=1`` — the project-wide opt-in for "this test talks to a
  real warehouse" (mirrors ``SF_RUN_BQ=1`` for the BigQuery e2e).
* ``SNOWFLAKE_ACCOUNT`` / ``SNOWFLAKE_USER`` / ``SNOWFLAKE_PASSWORD`` — the
  minimal password-auth connection triple.
* ``SNOWFLAKE_WAREHOUSE`` — required so ``EXPLAIN`` has a warehouse context.

Run via the maintainer-only invocation (``--no-cov`` because
``--cov-fail-under`` in ``addopts`` would fail a marker-specific run that
exercises only a fraction of the codebase)::

    export SF_RUN_SNOWFLAKE=1
    export SNOWFLAKE_ACCOUNT=<org-account>
    export SNOWFLAKE_USER=<user>
    export SNOWFLAKE_PASSWORD=<password>
    export SNOWFLAKE_WAREHOUSE=<warehouse>
    uv run pytest -m snowflake --no-cov

Engineered-determinism caveat: ``EXPLAIN`` figures are *planner estimates* and
vary across Snowflake releases and table layouts. This test asserts SHAPE +
non-negativity (a parseable ``int >= 0`` and a ``GlobalStats``-bearing plan),
NEVER an exact byte value (that exact-value assertion lives in the offline
fixture test, against the round number the committed fixture encodes).

Maintainer fixture-regen note: see
``tests/fixtures/warehouse/snowflake/README.md`` § "Maintainer regeneration
(live Snowflake)" for how to recapture a fresh ``EXPLAIN USING JSON`` plan and
refresh ``explain_using_json_sample.json``.

Traces to: plans/super/130-snowflake-estimate-explain.md DEC-006 / US-005.
"""

from __future__ import annotations

import os

import pytest

from signalforge.warehouse.adapters.snowflake import (
    SnowflakeAdapter,
    _parse_explain_json_bytes,
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# The connection env vars ``make_real_client`` needs for password auth, plus
# the warehouse so ``EXPLAIN`` has compute context.
_REQUIRED_CONN_VARS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_WAREHOUSE",
)


def _snowflake_runs_enabled() -> bool:
    """``SF_RUN_SNOWFLAKE`` is set to a truthy value (the Snowflake analogue of
    the ``SF_RUN_BQ`` opt-in; accepts ``1``/``true``/``yes``/``on``)."""
    return os.environ.get("SF_RUN_SNOWFLAKE", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required prerequisite is missing.

    Returns ``None`` only when the opt-in flag AND every connection env var is
    present — the test then proceeds to make a real Snowflake ``EXPLAIN`` call.
    Each missing prerequisite yields its own distinct reason so a maintainer
    running ``pytest -m snowflake`` sees exactly what to set.
    """
    if not _snowflake_runs_enabled():
        return "SF_RUN_SNOWFLAKE=1 required (live test talks to a real Snowflake warehouse)"
    for var in _REQUIRED_CONN_VARS:
        if not os.environ.get(var):
            return f"{var} required (Snowflake connection parameter for the live EXPLAIN call)"
    return None


@pytest.mark.snowflake
def test_estimate_query_bytes_live_explain_returns_nonnegative_int() -> None:
    """A real ``SnowflakeAdapter.estimate_query_bytes`` over a live EXPLAIN.

    Skips cleanly under ``pytest -m snowflake`` when any prerequisite is
    missing. With credentials present, constructs a real adapter (NOT a fake),
    runs ``estimate_query_bytes("SELECT 1")``, and asserts the result is a
    parseable non-negative ``int``. ``SELECT 1`` is a metadata-only query that
    may legitimately estimate ``0`` bytes (no table scan), so we assert
    ``>= 0`` rather than ``> 0`` — the contract being certified is "the parser
    extracts a real ``GlobalStats.bytesAssigned`` from a live plan," not a
    specific magnitude.
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    adapter = SnowflakeAdapter(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        # Optional context — passed through when set so a maintainer can point
        # the EXPLAIN at a specific role / database / schema.
        role=os.environ.get("SNOWFLAKE_ROLE"),
        database=os.environ.get("SNOWFLAKE_DATABASE"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA"),
    )
    with adapter:
        estimated_bytes = adapter.estimate_query_bytes("SELECT 1")

    assert isinstance(estimated_bytes, int)
    assert estimated_bytes >= 0


@pytest.mark.snowflake
def test_live_explain_plan_matches_committed_fixture_shape() -> None:
    """The live ``EXPLAIN USING JSON`` plan shares the fixture's top-level shape.

    Certifies that a real Snowflake ``EXPLAIN USING JSON`` cell still carries a
    parseable ``GlobalStats.bytesAssigned`` — the exact shape the committed
    ``explain_using_json_sample.json`` fixture pins offline. We assert the live
    cell parses through the SAME pure parser the adapter uses
    (:func:`_parse_explain_json_bytes`) and yields a non-negative ``int``;
    drift in Snowflake's plan shape (a renamed ``GlobalStats`` / dropped
    ``bytesAssigned``) breaks this loud and signals the maintainer to refresh
    the fixture per the README regen note.

    NOT a byte-value assertion — planner estimates vary across releases
    (mirrors the ``HASH()`` reproducibility caveat from #121); the offline
    fixture test owns the exact-value pin.
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    adapter = SnowflakeAdapter(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        role=os.environ.get("SNOWFLAKE_ROLE"),
        database=os.environ.get("SNOWFLAKE_DATABASE"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA"),
    )
    # Reach through the adapter's own scalar-exec seam so this exercises the
    # real cursor / EXPLAIN path; the result cell goes through the same pure
    # parser the production estimate uses.
    with adapter:
        cell = adapter._execute_scalar("EXPLAIN USING JSON SELECT 1")

    assert cell is not None, "live EXPLAIN USING JSON returned no rows"
    parsed = _parse_explain_json_bytes(cell)
    assert isinstance(parsed, int)
    assert parsed >= 0
