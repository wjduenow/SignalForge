"""Gated live materialised-sample prune e2e against a real Snowflake (#139 US-004).

This is the **live certification for the projection-subquery sample shape** —
the #139 fix for Snowflake's ``HASH(*)``-in-predicate rejection. The offline
``fakesnow`` / ``sqlglot`` suite (``tests/prune/test_compiler_fakesnow.py``,
``tests/warehouse/test_snowflake_adapter_fakesnow.py``) pins the compiled
Snowflake SQL's *shape* and ``sqlglot`` parse-validity, but neither certifies
that a **real** Snowflake accepts the new sample SQL: ``fakesnow``'s DuckDB
backend cannot execute the variadic ``HASH(*)``, and ``sqlglot`` parses the
*old* (invalid) inline form without complaint. Only a live run certifies the
projection-subquery shape — create an engineered writable table, run a
hand-crafted candidate test against it under
``prune.scope: sample`` + ``prune.sample_strategy: materialised`` (which
exercises ``materialise_sample``'s ``CREATE TEMPORARY TABLE … AS SELECT * EXCLUDE
(_sf_sample_hash) FROM (SELECT t.*, ABS(HASH(*)) AS _sf_sample_hash …)`` CTAS),
and watch the engine drop a ``not_null`` over a guaranteed-non-null column as
``always-passes``.

Before #139, ``materialised`` sample-mode emitted ``MOD(ABS(HASH(*)), n)`` in
WHERE / ORDER BY, which Snowflake rejects (``002079``: ``HASH(*)`` is valid only
in the SELECT projection); that bug (bead ``bd_1-scaffolding-cdp``) is fixed by
the projection-subquery shape + ``render_sample_select``. (A *separate*,
still-open bug — ``oneshot`` routes the sample row-count through a BigQuery-only
``_get_client`` / the not-yet-built ``WarehouseAdapter.get_table_metadata`` seam,
bead ``bd_1-scaffolding-tft`` — is NOT in scope here; this test exercises the
``materialised`` path, which colocates its temp table via the connection-bound
session and does not hit that seam.) This module is the belt-and-suspenders half
— a ``@pytest.mark.snowflake``-gated test that drives a **real**
:class:`SnowflakeAdapter` through :func:`signalforge.prune.prune_tests` end to
end against a live warehouse and asserts the v0.1 differentiator (Architectural
Commitment #1: an always-pass test is dropped, not shipped).

**DEC-004 primary/fallback note.** The emitted SQL uses the PRIMARY form:
``ORDER BY _sf_sample_hash`` at the outer level where ``_sf_sample_hash`` is
``SELECT * EXCLUDE``-d from the output projection. There is a genuine open
question — resolvable only by this live run — whether Snowflake accepts
``ORDER BY <col>`` when that column is ``SELECT * EXCLUDE``-d. If live Snowflake
rejects ``ORDER BY`` of an EXCLUDE-d column, the fallback (DEC-004) is to drop
the outer ``ORDER BY`` in ``render_sample_select`` + the fixtures; the
deterministic ``MOD`` filter alone defines sample membership (matches the
prune compiler CTE, which already emits no ``ORDER BY``). The fallback is NOT
implemented here — the code ships the primary form; this docstring records the
decision point the maintainer's live run resolves.

NO LLM, NO ``generate`` CLI: this test builds the :class:`Model`,
:class:`Manifest`, the :class:`CandidateSchema` (one
:class:`CandidateTestNotNull`), and the :class:`PruneConfig` in-process and calls
:func:`prune_tests` directly. ``prune_tests`` owns the ``with adapter:`` block
itself (it prunes and closes the session), so the prune adapter
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
* ``SNOWFLAKE_WAREHOUSE`` — compute context for the engineered ``CREATE TABLE``,
  the materialised-sample ``CREATE TEMPORARY TABLE … AS SELECT`` CTAS, and the
  per-test ``COUNT(*)``.
* ``SNOWFLAKE_DATABASE`` + ``SNOWFLAKE_SCHEMA`` — the **WRITABLE** target where
  the engineered table is created. The read-only ``SNOWFLAKE_SAMPLE_DATA`` share
  cannot accept a ``CREATE TABLE``, so a writable namespace is required; the
  table is dropped in teardown.

**Cost guidance — set a Snowflake resource monitor FIRST.** Before running,
create a resource monitor with a hard credit cap so a runaway query cannot bill
unbounded credits. Use an **XS (extra-small) warehouse** with **aggressive
auto-suspend** (e.g. 60 seconds) so the compute idles down immediately after the
run. The engineered table is a handful of rows, so the per-test ``COUNT(*)``
is tiny; the dominant cost is warehouse
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

Traces to: #139 US-004 (live certification of the projection-subquery sample
shape via materialised sample-mode prune); originally #124 US-004 (warehouse +
prune-only gated live e2e, then pinned to ``scope=full``).
"""

from __future__ import annotations

import os
import uuid

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

# Engineered-table name PREFIX. The full name gets a per-run random suffix
# (see ``_unique_table_name``) so two concurrent maintainer runs against the
# same writable schema cannot race on the same `DROP TABLE` / clobber an
# unrelated leftover object. The prefix + suffix are a valid bare identifier
# (strict DEC-013 regex used by ``TableRef``). The ``region`` column is a
# literal constant on every row so ``not_null`` over it always passes.
_ENGINEERED_TABLE_PREFIX = "sf_prune_live_engineered"


def _unique_table_name() -> str:
    """A per-run engineered-table name: prefix + 12 random hex chars."""
    return f"{_ENGINEERED_TABLE_PREFIX}_{uuid.uuid4().hex[:12]}"


def _snowflake_runs_enabled() -> bool:
    """``SF_RUN_SNOWFLAKE`` is set to a truthy value (the Snowflake analogue of
    the ``SF_RUN_BQ`` opt-in; accepts ``1``/``true``/``yes``/``on``)."""
    return os.environ.get("SF_RUN_SNOWFLAKE", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required prerequisite is missing.

    Returns ``None`` only when the opt-in flag AND every connection env var is
    present — the test then proceeds to make real Snowflake calls (CREATE TABLE,
    a materialised-sample CTAS, a per-test ``COUNT(*)``, DROP TABLE). Each
    missing prerequisite yields its own distinct reason so a maintainer running
    ``pytest -m snowflake`` sees exactly what to set.
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
    """Per-component quoted, UPPER-folded Snowflake identifier (#124).

    Must fold to UPPER then quote — byte-identical to the prune compiler's
    ``_quote`` / ``SnowflakeAdapter._quote`` — so the table this test
    CREATEs/DROPs directly is the same case-sensitive object the compiled
    full-scope ``not_null`` (run via ``run_test_sql``) REFERENCEs. A
    case-preserved helper would create ``"…<lowercase>"`` while the compiler
    references the upper-folded ``"…<UPPERCASE>"`` → "Table not found"
    (Snowflake quoted identifiers are case-sensitive).
    """
    return f'"{database.upper()}"."{schema.upper()}"."{name.upper()}"'


@pytest.mark.snowflake
def test_prune_drops_always_passes_not_null_live_materialised_sample() -> None:
    """Prune a hand-crafted ``not_null`` against a live engineered table in
    materialised sample-mode (the #139 projection-subquery sample shape).

    Skips cleanly under ``pytest -m snowflake`` when any prerequisite is
    missing. With credentials present:

    1. Creates a tiny engineered table in the writable
       ``SNOWFLAKE_DATABASE.SNOWFLAKE_SCHEMA`` — two columns where ``region`` is
       the literal ``'austin'`` on every row (guaranteed non-null). The table
       MUST be in a writable schema: ``materialise_sample`` colocates its
       ``CREATE TEMPORARY TABLE`` in the source db/schema, so read-only shared
       data (``SNOWFLAKE_SAMPLE_DATA``) cannot be the source.
    2. Builds an in-process :class:`Model` / :class:`Manifest` /
       :class:`CandidateSchema` carrying ONE :class:`CandidateTestNotNull` over
       the guaranteed-non-null ``region`` column.
    3. Calls :func:`prune_tests` with ``scope="sample"`` +
       ``sample_strategy="materialised"`` — the engine materialises a temp-table
       sample via the projection-subquery CTAS (``CREATE TEMPORARY TABLE … AS
       SELECT * EXCLUDE (_sf_sample_hash) FROM (SELECT t.*, ABS(HASH(*)) AS
       _sf_sample_hash …)``) and runs the compiled ``not_null`` against it. This
       is the #139 fix: pre-fix, the ``MOD(ABS(HASH(*)), n)``-in-WHERE/ORDER-BY
       form was rejected by Snowflake (``002079``).
    4. Asserts at least one :class:`PruneDecision` is
       ``decision == "dropped"`` with ``reason == "always-passes"`` — the v0.1
       differentiator (Architectural Commitment #1).
    5. Tears the engineered table down with ``DROP TABLE IF EXISTS`` in a
       ``finally`` (idempotent; tolerates a partial-setup failure). The
       materialised temp table is session-scoped and reaped when ``prune_tests``
       closes the prune adapter's connection.
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    database = os.environ["SNOWFLAKE_DATABASE"]
    schema = os.environ["SNOWFLAKE_SCHEMA"]
    # Per-run unique name so concurrent runs don't race on DROP / clobber.
    table_name = _unique_table_name()
    quoted = _quoted_table(database, schema, table_name)

    # --- Setup: create the engineered table (own short-lived adapter). --------
    # ``prune_tests`` (scope=sample, materialised) materialises a temp-table
    # sample FROM this source table on its OWN adapter/connection, so the source
    # must persist beyond the setup session — a regular (non-temp) table created
    # here, dropped in teardown. (The materialised sample temp table is
    # session-scoped and reaped when the prune adapter closes its connection.)
    setup_adapter = _make_adapter()
    with setup_adapter:
        cursor = setup_adapter._get_connection().cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {quoted}")
            cursor.execute(f"CREATE TABLE {quoted} (id INTEGER, region VARCHAR)")
            # ``region`` is the literal 'austin' on every row → never NULL, so the
            # ``not_null`` candidate is mathematically always-pass.
            cursor.execute(
                f"INSERT INTO {quoted} (id, region) VALUES "
                f"(1, 'austin'), (2, 'austin'), (3, 'austin'), (4, 'austin')"
            )
        finally:
            cursor.close()

    try:
        # --- Build the in-process pipeline inputs. ----------------------------
        # ``Model.alias or model.name`` becomes the ``TableRef.name`` via
        # ``TableRef.from_model``; ``database`` / ``schema_`` resolve the
        # qualified source table. The model's ``name`` must equal the
        # ``CandidateSchema.name`` (the diff/anchor convention across stages).
        model = Model.model_validate(
            {
                "unique_id": f"model.signalforge_live.{table_name}",
                "name": table_name,
                "resource_type": "model",
                "package_name": "signalforge_live",
                "original_file_path": f"models/{table_name}.sql",
                "path": f"{table_name}.sql",
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
            name=table_name,
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

        # ``scope="sample"`` + ``sample_strategy="materialised"`` — the engine
        # materialises a temp-table sample via the #139 projection-subquery CTAS,
        # then runs the compiled ``not_null`` against it. This exercises the
        # exact path the #139 fix repairs (``HASH(*)`` moved into an inner
        # SELECT projection + ``SELECT * EXCLUDE``). The engineered table is a
        # handful of rows, so the CTAS + COUNT(*) are cheap.
        config = PruneConfig(scope="sample", sample_strategy="materialised")

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
            teardown_cursor = teardown_adapter._get_connection().cursor()
            try:
                teardown_cursor.execute(f"DROP TABLE IF EXISTS {quoted}")
            finally:
                teardown_cursor.close()
