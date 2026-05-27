"""Gated live warehouse + prune-only e2e against a real Snowflake (#124 US-004).

The offline `fakesnow` / `sqlglot` suite (`tests/prune/test_compiler_fakesnow.py`)
pins the compiled Snowflake SQL's *shape* and parse-validity, and the sampling
adapter is exercised via injected fakes in the default suite. Neither can
certify the full prune-against-a-real-table path: create an engineered table,
deterministically sample it into a session-scoped `TEMPORARY TABLE`
(`sample_strategy="materialised"`), run a hand-crafted candidate test, and watch
the engine drop a `not_null` over a guaranteed-non-null column as
``always-passes``. This module is the belt-and-suspenders half — a
``@pytest.mark.snowflake``-gated test that drives a **real**
:class:`SnowflakeAdapter` through :func:`signalforge.prune.prune_tests` end to
end against a live warehouse and asserts the v0.1 differentiator (Architectural
Commitment #1: an always-pass test is dropped, not shipped).

NO LLM, NO ``generate`` CLI: this test builds the :class:`Model`,
:class:`Manifest`, the :class:`CandidateSchema` (one
:class:`CandidateTestNotNull`), and the :class:`PruneConfig` in-process and calls
:func:`prune_tests` directly. ``prune_tests`` owns the ``with adapter:`` block
itself (it materialises, prunes, and closes the session), so the prune adapter
is NOT pre-entered here; a *separate* short-lived adapter does the engineered
table setup and the ``DROP TABLE`` teardown.

Belt-and-suspenders gating (``.claude/rules/testing-signal.md`` § "End-to-end
gated tests"):

1. ``@pytest.mark.snowflake`` — registered in ``pyproject.toml``
   ``[tool.pytest.ini_options].markers`` and deselected by the default
   ``addopts`` (``-m '... and not snowflake'``), so the default ``pytest`` run
   never collects this test.
2. A runtime :func:`_skip_reason` — when a maintainer runs ``pytest -m
   snowflake`` but lacks credentials, each missing prerequisite surfaces as a
   distinct skip-with-reason rather than a confusing connection error.

Required env vars (each missing one yields its own distinct skip reason):

* ``SF_RUN_SNOWFLAKE=1`` — the project-wide opt-in for "this test talks to a
  real warehouse" (mirrors ``SF_RUN_BQ=1`` for the BigQuery e2e; accepts
  ``1``/``true``/``yes``/``on``).
* ``SNOWFLAKE_ACCOUNT`` / ``SNOWFLAKE_USER`` / ``SNOWFLAKE_PASSWORD`` — the
  minimal password-auth connection triple.
* ``SNOWFLAKE_WAREHOUSE`` — compute context for the CTAS + per-test queries.
* ``SNOWFLAKE_DATABASE`` + ``SNOWFLAKE_SCHEMA`` — the **WRITABLE** target where
  the engineered table is created. The read-only ``SNOWFLAKE_SAMPLE_DATA`` share
  cannot accept a ``CREATE TABLE`` / CTAS, so a writable namespace is required;
  the table is dropped in teardown.

**Cost guidance — set a Snowflake resource monitor FIRST.** Before running,
create a resource monitor with a hard credit cap so a runaway query cannot bill
unbounded credits. Use an **XS (extra-small) warehouse** with **aggressive
auto-suspend** (e.g. 60 seconds) so the compute idles down immediately after the
run. The engineered table is a handful of rows, so the materialised-sample CTAS
and the per-test ``COUNT(*)`` are tiny; the dominant cost is warehouse
spin-up — an XS warehouse with fast auto-suspend keeps a single run well under a
cent.

Run via the maintainer-only invocation (``--no-cov`` because
``--cov-fail-under`` in ``addopts`` would fail a marker-specific run that
exercises only a fraction of the codebase)::

    export SF_RUN_SNOWFLAKE=1
    export SNOWFLAKE_ACCOUNT=<org-account>
    export SNOWFLAKE_USER=<user>
    export SNOWFLAKE_PASSWORD=<password>
    export SNOWFLAKE_WAREHOUSE=<xs-warehouse>
    export SNOWFLAKE_DATABASE=<writable-database>
    export SNOWFLAKE_SCHEMA=<writable-schema>
    uv run pytest -m snowflake --no-cov

Engineered determinism (``.claude/rules/testing-signal.md`` § "Engineered
determinism"): the assertion does NOT depend on any LLM output — the candidate
test is hand-crafted. The engineered table's ``region`` column is the literal
``'austin'`` on every row, so a ``not_null`` test over it returns zero failing
rows on any sample → the prune engine routes it to ``always-passes`` (drop)
mathematically, not probabilistically.

Traces to: #124 US-004 (warehouse + prune-only gated live e2e).
"""

from __future__ import annotations

import os

import pytest

from signalforge.draft.models import CandidateColumn, CandidateSchema, CandidateTestNotNull
from signalforge.manifest.models import Column, Config, Manifest, Model
from signalforge.prune import PruneConfig, prune_tests
from signalforge.warehouse import SnowflakeAdapter

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Connection env vars the prune + setup adapters need for password auth, plus
# the writable namespace the engineered table is created in.
_REQUIRED_CONN_VARS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
)

# Engineered table name (a valid bare identifier — passes the strict
# DEC-013 identifier regex used by ``TableRef``). The ``region`` column is a
# literal constant on every row so ``not_null`` over it always passes.
_ENGINEERED_TABLE = "sf_prune_live_engineered"


def _snowflake_runs_enabled() -> bool:
    """``SF_RUN_SNOWFLAKE`` is set to a truthy value (the Snowflake analogue of
    the ``SF_RUN_BQ`` opt-in; accepts ``1``/``true``/``yes``/``on``)."""
    return os.environ.get("SF_RUN_SNOWFLAKE", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required prerequisite is missing.

    Returns ``None`` only when the opt-in flag AND every connection env var is
    present — the test then proceeds to make real Snowflake calls (CREATE TABLE,
    materialised sample, COUNT(*), DROP TABLE). Each missing prerequisite yields
    its own distinct reason so a maintainer running ``pytest -m snowflake`` sees
    exactly what to set.
    """
    if not _snowflake_runs_enabled():
        return "SF_RUN_SNOWFLAKE=1 required (live test talks to a real Snowflake warehouse)"
    for var in _REQUIRED_CONN_VARS:
        if not os.environ.get(var):
            return (
                f"{var} required (Snowflake connection / writable-target parameter "
                f"for the live prune e2e)"
            )
    return None


def _make_adapter() -> SnowflakeAdapter:
    """Construct a real :class:`SnowflakeAdapter` from the env vars.

    A fresh adapter is built per use (setup / prune / teardown) so the prune
    adapter's ``with``-block session close does not strand the setup/teardown
    cursors — each adapter owns its own connection.
    """
    return SnowflakeAdapter(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        database=os.environ["SNOWFLAKE_DATABASE"],
        schema=os.environ["SNOWFLAKE_SCHEMA"],
        role=os.environ.get("SNOWFLAKE_ROLE"),
    )


def _quoted_table(database: str, schema: str, name: str) -> str:
    """Per-component quoted Snowflake identifier ``"DB"."SCHEMA"."NAME"``."""
    return f'"{database}"."{schema}"."{name}"'


@pytest.mark.snowflake
def test_prune_drops_always_passes_not_null_live_materialised() -> None:
    """Prune a hand-crafted ``not_null`` against a live engineered table.

    Skips cleanly under ``pytest -m snowflake`` when any prerequisite is
    missing. With credentials present:

    1. Creates a tiny engineered table in the writable
       ``SNOWFLAKE_DATABASE.SNOWFLAKE_SCHEMA`` — two columns where ``region`` is
       the literal ``'austin'`` on every row (guaranteed non-null).
    2. Builds an in-process :class:`Model` / :class:`Manifest` /
       :class:`CandidateSchema` carrying ONE :class:`CandidateTestNotNull` over
       the guaranteed-non-null ``region`` column.
    3. Calls :func:`prune_tests` with ``sample_strategy="materialised"`` and
       ``scope="sample"`` — the engine deterministically samples the table into
       a session-scoped ``TEMPORARY TABLE`` and runs the test against it.
    4. Asserts at least one :class:`PruneDecision` is
       ``decision == "dropped"`` with ``reason == "always-passes"`` — the v0.1
       differentiator (Architectural Commitment #1).
    5. Tears the engineered table down with ``DROP TABLE IF EXISTS`` in a
       ``finally`` (idempotent; tolerates a partial-setup failure).
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    database = os.environ["SNOWFLAKE_DATABASE"]
    schema = os.environ["SNOWFLAKE_SCHEMA"]
    quoted = _quoted_table(database, schema, _ENGINEERED_TABLE)

    # --- Setup: create the engineered table (own short-lived adapter). --------
    # A regular (non-temp) table is required: ``prune_tests`` under the
    # ``materialised`` strategy issues ``CREATE TEMPORARY TABLE ... AS SELECT
    # ... FROM <this table>`` on a *different* connection's session, so the
    # source must persist beyond the setup session.
    setup_adapter = _make_adapter()
    with setup_adapter:
        cursor = setup_adapter._get_connection().cursor()
        cursor.execute(f"DROP TABLE IF EXISTS {quoted}")
        cursor.execute(f"CREATE TABLE {quoted} (id INTEGER, region VARCHAR)")
        # ``region`` is the literal 'austin' on every row → never NULL, so the
        # ``not_null`` candidate is mathematically always-pass.
        cursor.execute(
            f"INSERT INTO {quoted} (id, region) VALUES "
            f"(1, 'austin'), (2, 'austin'), (3, 'austin'), (4, 'austin')"
        )

    try:
        # --- Build the in-process pipeline inputs. ----------------------------
        # ``Model.alias or model.name`` becomes the ``TableRef.name`` via
        # ``TableRef.from_model``; ``database`` / ``schema_`` resolve the
        # qualified source table. The model's ``name`` must equal the
        # ``CandidateSchema.name`` (the diff/anchor convention across stages).
        model = Model.model_validate(
            {
                "unique_id": f"model.signalforge_live.{_ENGINEERED_TABLE}",
                "name": _ENGINEERED_TABLE,
                "resource_type": "model",
                "package_name": "signalforge_live",
                "original_file_path": f"models/{_ENGINEERED_TABLE}.sql",
                "path": f"{_ENGINEERED_TABLE}.sql",
                "database": database,
                "schema": schema,
                "columns": {
                    "id": Column(name="id"),
                    "region": Column(name="region"),
                },
                "config": Config(materialized="table"),
            }
        )
        manifest = Manifest(metadata={}, nodes={model.unique_id: model})

        candidates = CandidateSchema(
            name=_ENGINEERED_TABLE,
            description="engineered live-e2e table",
            columns=(
                CandidateColumn(
                    name="region",
                    description="literal region constant",
                    tests=(
                        CandidateTestNotNull(
                            column="region",
                            rationale="region is a literal constant; not_null should always pass",
                        ),
                    ),
                ),
            ),
        )

        # ``sample_strategy="materialised"`` + ``scope="sample"`` is the path
        # under test. ``sample_size`` is well above the engineered row count so
        # the deterministic hash-mod bucket sizing keeps every row.
        config = PruneConfig(scope="sample", sample_strategy="materialised", sample_size=1000)

        # ``prune_tests`` owns the ``with adapter:`` block — pass a NOT-entered
        # adapter and do not wrap this call in our own ``with``.
        result = prune_tests(model, _make_adapter(), candidates, manifest, config=config)

        always_passes_drops = [
            d for d in result.decisions if d.decision == "dropped" and d.reason == "always-passes"
        ]
        assert always_passes_drops, (
            "expected at least one PruneDecision with decision='dropped' and "
            "reason='always-passes' (the v0.1 differentiator). The engineered "
            f"'region' column is the literal 'austin' on every row, so the "
            f"hand-crafted not_null candidate must drop as always-passes. Got "
            f"decisions={result.decisions!r}"
        )
    finally:
        # --- Teardown: drop the engineered table (idempotent). ----------------
        # A fresh adapter — the prune adapter's session has been closed by its
        # own ``__exit__``. ``IF EXISTS`` tolerates a partial setup where the
        # table was never created.
        teardown_adapter = _make_adapter()
        with teardown_adapter:
            teardown_adapter._get_connection().cursor().execute(f"DROP TABLE IF EXISTS {quoted}")
