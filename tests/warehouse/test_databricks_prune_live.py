"""Gated live materialised-sample prune e2e against a real Databricks (#226 US-004).

This is the **live certification for the Databricks materialised sample path** —
the ``CREATE OR REPLACE TABLE <cat>.<sch>._sf_sample_<run_id> AS <projection-
subquery sample body>`` CTAS that :meth:`DatabricksAdapter.materialise_sample`
emits and the prune compiler then REFERENCEs. The offline surface (hand-rolled
fakes in ``tests/warehouse/_fake_databricks.py`` + the ungated ``sqlglot``
parse-guard in ``test_databricks_sql_parse.py`` / ``test_databricks_adapter.py``)
pins the emitted Databricks SQL's *shape* and ``sqlglot`` parse-validity, but
neither certifies that a **real** Databricks SQL warehouse accepts the SQL — a
snapshot pins invalid SQL byte-for-byte (the #121/#124/#171 lesson). Indeed this
live run is what FOUND the shape was wrong: it proved Databricks rejects a
qualified ``CREATE TEMPORARY TABLE`` name (→ a real ``CREATE OR REPLACE TABLE`` +
explicit DROP) and rejects ``struct(*)`` in a Sort node (→ the projection-subquery
sample shape). Only a live run certifies, end to end:

1. ``materialise_sample`` — that Databricks accepts the qualified
   ``CREATE OR REPLACE TABLE <cat>.<sch>.<temp> AS ...`` (a **real**, not session-
   local, table — explicitly DROPped at cleanup) AND that the
   ``databricks-sql-connector`` persists the session across queries (so the table
   is reachable from the follow-up ``run_test_sql``). Both are now CERTIFIED by
   this test (#226); the originally-assumed ``CREATE TEMPORARY TABLE`` / inline
   sample shape was reversed here — see the adapter's ``materialise_sample``
   docstring.
2. ``get_row_count`` (``SELECT COUNT(*)``) — the sizing seam the materialised
   path shares (``_resolve_sample_bucket``).
3. ``run_test_sql`` — the ``SELECT COUNT(*) AS failures FROM (<sql>) AS t`` wrap
   plus the per-row ``to_json(struct(*))`` capture (default ``capture_failure_rows=3``).
4. ``column_stats`` — a thin live assert on the single aggregate-only profiling
   query (count / distinct / nulls / min / max / ``typeof`` data_type), shipped
   AHEAD of Snowflake (parity tracked as issue #258).

NO LLM, NO ``generate`` CLI: this test builds the :class:`Model`,
:class:`Manifest`, the :class:`CandidateSchema` (one
:class:`CandidateTestNotNull`), and the :class:`PruneConfig` in-process and calls
:func:`prune_tests` directly. ``prune_tests`` owns the ``with adapter:`` block
itself (it prunes and closes the session), so the prune adapter is NOT pre-entered
here; *separate* short-lived adapters do the engineered-table setup, the warm-up /
``column_stats`` reads, and the ``DROP TABLE`` teardown.

Gating + the live-adapter builder live in the SHARED
:mod:`tests.warehouse._databricks_live` helper (US-003), so the gate is declared
in exactly one place rather than duplicated across each #226 live test (the way
the two Snowflake live files each re-declare their gate). Belt-and-suspenders
gating (``.claude/rules/testing-signal.md`` § "End-to-end gated tests"):

1. ``@pytest.mark.databricks`` — registered in ``pyproject.toml``
   ``[tool.pytest.ini_options].markers`` and deselected by the default ``addopts``
   (``-m '... and not databricks'``), so the default ``pytest`` run never collects
   this test.
2. A runtime :func:`skip_reason` (from the shared helper) — when a maintainer runs
   ``pytest -m databricks`` but lacks credentials, each missing prerequisite
   surfaces as its own distinct skip-with-reason rather than a confusing
   connection error.

Required env vars (each missing one yields its own distinct skip reason via the
shared :func:`skip_reason`):

* ``SF_RUN_DATABRICKS=1`` — the project-wide opt-in for "this test talks to a real
  Databricks SQL warehouse" (mirrors ``SF_RUN_BQ`` / ``SF_RUN_SNOWFLAKE``).
* ``DATABRICKS_SERVER_HOSTNAME`` / ``DATABRICKS_HTTP_PATH`` / ``DATABRICKS_TOKEN``
  — the minimal PAT-auth connection triple.

The engineered table is created in the **WRITABLE** ``workspace.default`` namespace
(the Databricks Free-Edition default catalog is writable; overridable via
``DATABRICKS_CATALOG`` / ``DATABRICKS_SCHEMA``). ``materialise_sample`` colocates
its ``CREATE OR REPLACE TABLE`` in the source catalog / schema, so the source MUST
live in a writable namespace; the engineered table is dropped in a ``finally``.

**Cost guidance — use a small Databricks SQL warehouse with aggressive
auto-stop.** The engineered table is a handful of rows, so the per-test
``COUNT(*)`` / CTAS / ``EXPLAIN``-free path is tiny; the dominant cost is warehouse
spin-up. The Free-Edition 2X-Small serverless warehouse with auto-stop keeps a
single run negligible.

Run via the maintainer-only invocation (``--no-cov`` because ``--cov-fail-under``
in ``addopts`` would fail a marker-specific run that exercises only a fraction of
the codebase)::

    export SF_RUN_DATABRICKS=1
    export DATABRICKS_SERVER_HOSTNAME=<workspace-host>
    export DATABRICKS_HTTP_PATH=<sql-warehouse-http-path>
    export DATABRICKS_TOKEN=<personal-access-token>
    uv run pytest -m databricks --no-cov tests/warehouse/test_databricks_prune_live.py

Engineered determinism (``.claude/rules/testing-signal.md`` § "Engineered
determinism"): the assertion does NOT depend on any LLM output — the candidate
test is hand-crafted. The engineered table's ``region`` column is the literal
``'austin'`` on every row, so a ``not_null`` test over it returns zero failing
rows on any sample → the prune engine routes it to ``always-passes`` (drop)
mathematically, not probabilistically. A warm-up guard
(``COUNT_IF(region IS NULL) == 0``) asserts the non-null invariant before the
prune drop-reason is relied upon.

Traces to: #226 US-004 (live certification of the Databricks materialised-sample
prune path + a thin live ``column_stats`` assert); epic #219.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest

from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTestNotNull,
    CandidateTestRowCountAnomalyByPeriod,
)
from signalforge.manifest.models import Column, Config, Manifest, Model
from signalforge.prune import PruneConfig, prune_tests
from signalforge.warehouse.models import DATABRICKS_DIALECT, TableRef
from tests.warehouse._databricks_live import build_live_adapter, skip_reason

# The WRITABLE namespace the engineered table is created in. The Databricks
# Free-Edition default catalog ``workspace`` (schema ``default``) is writable;
# a maintainer can point the engineered table at a different writable namespace
# via ``DATABRICKS_CATALOG`` / ``DATABRICKS_SCHEMA`` (the same optional context
# vars ``build_live_adapter`` reads).
_WRITABLE_CATALOG = os.environ.get("DATABRICKS_CATALOG") or "workspace"
_WRITABLE_SCHEMA = os.environ.get("DATABRICKS_SCHEMA") or "default"

# Engineered-table name PREFIX. The full name gets a per-run random suffix
# (see ``_unique_table_name``) so two concurrent maintainer runs against the same
# writable schema cannot race on the same ``DROP TABLE`` / clobber an unrelated
# leftover object. The prefix + suffix are a valid bare identifier (the strict
# DEC-013 regex used by ``TableRef``). The ``region`` column is a literal
# constant on every row so ``not_null`` over it always passes.
_ENGINEERED_TABLE_PREFIX = "sf_prune_live_engineered"


def _unique_table_name() -> str:
    """A per-run engineered-table name: prefix + 12 random hex chars."""
    return f"{_ENGINEERED_TABLE_PREFIX}_{uuid.uuid4().hex[:12]}"


def _fold(identifier: str) -> str:
    """Case-fold one identifier per :attr:`Dialect.identifier_case`.

    Mirrors :meth:`DatabricksAdapter._fold` / the prune compiler's
    ``_fold_identifier``. Databricks (Unity Catalog) folds unquoted identifiers
    to **lowercase**, so the table this test CREATEs/DROPs directly is the same
    case-sensitive object the compiled ``not_null`` (run via ``run_test_sql``
    against the materialised temp) REFERENCEs. All names used here are already
    lowercase, so the fold is the identity — applied anyway to document the
    contract and survive any future name change.
    """
    case = DATABRICKS_DIALECT.identifier_case
    if case == "upper":
        return identifier.upper()
    if case == "lower":
        return identifier.lower()
    return identifier


def _quote_identifier(identifier: str) -> str:
    """Fold-then-backtick-quote ONE identifier (mirrors the adapter)."""
    qc = DATABRICKS_DIALECT.quote_char
    return f"{qc}{_fold(identifier)}{qc}"


def _quoted_table(catalog: str, schema: str, name: str) -> str:
    """Per-component backtick-quoted, lower-folded Databricks identifier.

    Byte-identical to :meth:`DatabricksAdapter._quote` (per-component quoting
    because :attr:`Dialect.quote_qualified_per_component` is ``True`` for
    Databricks) — so the table this test CREATEs/DROPs directly is the same
    case-sensitive Unity Catalog object the compiler REFERENCEs when the prune
    engine runs the compiled ``not_null`` against the materialised sample.
    """
    return ".".join(_quote_identifier(c) for c in (catalog, schema, name))


@pytest.mark.databricks
def test_prune_drops_always_passes_not_null_live_materialised_sample() -> None:
    """Prune a hand-crafted ``not_null`` against a live engineered table in
    materialised sample-mode, plus a thin live ``column_stats`` assert.

    Skips cleanly under ``pytest -m databricks`` when any prerequisite is
    missing. With credentials present:

    1. Creates a tiny engineered table in the writable
       ``workspace.default`` namespace — two columns where ``region`` is the
       literal ``'austin'`` on every row (guaranteed non-null). The table MUST be
       in a writable namespace: ``materialise_sample`` colocates its
       ``CREATE OR REPLACE TABLE`` in the source catalog / schema.
    2. **Warm-up guard** — asserts ``COUNT_IF(region IS NULL) == 0`` before the
       engineered-determinism contract is relied upon.
    3. **Thin ``column_stats`` live assert** — calls
       :meth:`DatabricksAdapter.column_stats` and asserts ``count`` / ``distinct``
       / ``nulls`` are ints and ``data_type`` is a non-empty ``str`` (the single
       aggregate-only profiling query, shipped ahead of Snowflake per #258).
    4. Builds an in-process :class:`Model` / :class:`Manifest` /
       :class:`CandidateSchema` carrying ONE :class:`CandidateTestNotNull` over
       the guaranteed-non-null ``region`` column.
    5. Calls :func:`prune_tests` with ``scope="sample"`` +
       ``sample_strategy="materialised"`` — the engine materialises a sample via
       the projection-subquery CTAS (``CREATE OR REPLACE TABLE
       <cat>.<sch>._sf_sample_<run_id> AS SELECT * EXCEPT (_sf_sample_hash) FROM
       (SELECT t.*, (xxhash64(...) & 9223372036854775807) AS _sf_sample_hash FROM
       <src> AS t) WHERE MOD(_sf_sample_hash, <bucket>) < 1 ...``) and runs the
       compiled ``not_null`` against it. This certifies the #226 items: Databricks
       accepts the qualified ``CREATE OR REPLACE TABLE`` AND the connector persists
       the session so the follow-up ``run_test_sql`` reaches the materialised
       table.
    6. Asserts at least one :class:`PruneDecision` is ``decision == "dropped"``
       with ``reason == "always-passes"`` — the v0.1 differentiator
       (Architectural Commitment #1).
    7. Tears the engineered table down with ``DROP TABLE IF EXISTS`` in a
       ``finally`` (idempotent). The materialised sample table is a real
       ``CREATE OR REPLACE TABLE`` explicitly ``DROP TABLE IF EXISTS``-ed at the
       adapter's cleanup boundary when ``prune_tests`` closes the prune adapter's
       connection (it does NOT auto-reap — it is not session-local).
    """
    if reason := skip_reason():
        pytest.skip(reason)

    catalog = _WRITABLE_CATALOG
    schema = _WRITABLE_SCHEMA
    # Per-run unique name so concurrent runs don't race on DROP / clobber.
    table_name = _unique_table_name()
    quoted = _quoted_table(catalog, schema, table_name)
    quoted_region = _quote_identifier("region")
    table_ref = TableRef(project=catalog, dataset=schema, name=table_name)

    # --- Setup: create the engineered table (own short-lived adapter). --------
    # ``prune_tests`` (scope=sample, materialised) materialises a sample FROM this
    # source table on its OWN adapter/connection, so the source must persist
    # beyond the setup session — a regular table created here, dropped in
    # teardown. (The materialised sample table is a real ``CREATE OR REPLACE
    # TABLE`` explicitly DROPped at the prune adapter's cleanup boundary, not
    # session-reaped.)
    setup_adapter = build_live_adapter()
    with setup_adapter:
        cursor = setup_adapter._get_connection().cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {quoted}")
            cursor.execute(f"CREATE TABLE {quoted} (id INT, region STRING)")
            # ``region`` is the literal 'austin' on every row → never NULL, so the
            # ``not_null`` candidate is mathematically always-pass.
            cursor.execute(
                f"INSERT INTO {quoted} (id, region) VALUES "
                f"(1, 'austin'), (2, 'austin'), (3, 'austin'), (4, 'austin')"
            )
        finally:
            cursor.close()

    try:
        # --- Warm-up guard + thin column_stats live assert (own adapter). -----
        # Assert the engineered non-null invariant BEFORE relying on the prune
        # drop-reason, and exercise the live ``column_stats`` aggregate. Bind the
        # concrete adapter to a variable (not the ``with``-block ``as`` target,
        # whose static type is the ``WarehouseAdapter`` ABC) so the Databricks
        # ``column_stats`` / ``_get_connection`` surface resolves.
        read_adapter = build_live_adapter()
        with read_adapter:
            guard_cursor = read_adapter._get_connection().cursor()
            try:
                guard_cursor.execute(
                    f"SELECT COUNT_IF({quoted_region} IS NULL) AS nulls FROM {quoted}"
                )
                guard_rows = list(guard_cursor.fetchall())
            finally:
                guard_cursor.close()
            assert guard_rows, "warm-up COUNT_IF query returned no rows"
            guard_first = guard_rows[0]
            nulls = guard_first["nulls"] if isinstance(guard_first, dict) else guard_first[0]
            assert int(nulls) == 0, (
                "engineered determinism violated: 'region' must be non-null on every "
                f"row before relying on the always-passes drop. Got nulls={nulls!r}"
            )

            stats = read_adapter.column_stats(table_ref, "region")
            assert isinstance(stats.count, int)
            assert isinstance(stats.distinct, int)
            assert isinstance(stats.nulls, int)
            assert stats.nulls == 0, (
                "column_stats should report zero nulls for the literal-constant "
                f"'region' column; got nulls={stats.nulls!r}"
            )
            assert isinstance(stats.data_type, str) and stats.data_type, (
                f"column_stats must return a non-empty data_type string; got {stats.data_type!r}"
            )

        # --- Build the in-process pipeline inputs. ----------------------------
        # ``Model.alias or model.name`` becomes the ``TableRef.name`` via
        # ``TableRef.from_model``; ``database`` (the Unity Catalog catalog) /
        # ``schema_`` resolve the qualified source table. The model's ``name``
        # must equal the ``CandidateSchema.name`` (the diff/anchor convention
        # across stages).
        model = Model.model_validate(
            {
                "unique_id": f"model.signalforge_live.{table_name}",
                "name": table_name,
                "resource_type": "model",
                "package_name": "signalforge_live",
                "original_file_path": f"models/{table_name}.sql",
                "path": f"{table_name}.sql",
                "database": catalog,
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
        # materialises a sample via the projection-subquery CTAS, then runs the
        # compiled ``not_null`` against it. This exercises the exact #226
        # live-cert path: a qualified ``CREATE OR REPLACE TABLE`` + the connector
        # persisting the session across queries. The engineered table is a
        # handful of rows, so the CTAS + COUNT(*) are cheap.
        config = PruneConfig(scope="sample", sample_strategy="materialised")

        # ``prune_tests`` owns the ``with adapter:`` block — pass a NOT-entered
        # adapter and do not wrap this call in our own ``with``.
        result = prune_tests(model, build_live_adapter(), candidates, manifest, config=config)

        always_passes_drops = [
            d for d in result.decisions if d.decision == "dropped" and d.reason == "always-passes"
        ]
        assert always_passes_drops, (
            "expected at least one PruneDecision with decision='dropped' and "
            "reason='always-passes' (the v0.1 differentiator). The engineered "
            "'region' column is the literal 'austin' on every row, so the "
            "hand-crafted not_null candidate must drop as always-passes. Got "
            f"decisions={result.decisions!r}"
        )
    finally:
        # --- Teardown: drop the engineered table (idempotent). ----------------
        # A fresh adapter — the prune adapter's session has been closed by its
        # own ``__exit__``. ``IF EXISTS`` tolerates a partial setup where the
        # table was never created.
        teardown_adapter = build_live_adapter()
        with teardown_adapter:
            teardown_cursor = teardown_adapter._get_connection().cursor()
            try:
                teardown_cursor.execute(f"DROP TABLE IF EXISTS {quoted}")
            finally:
                teardown_cursor.close()


# ---------------------------------------------------------------------------
# #227 US-004 — two gated live-certification tests for the Databricks adapter.
#
# These certify two code paths that the #226 offline shape tier (hand-rolled
# fakes + the ungated ``sqlglot`` parse-guard) pins for SHAPE but that a REAL
# Databricks SQL warehouse has never EXECUTED — exactly the class of "the parser
# certifies syntax, not acceptance" gap the #226 live pass surfaced three times
# (a qualified ``CREATE TEMPORARY TABLE``, ``struct(*)`` in a Sort node, a
# BigQuery-branded error string — all ``sqlglot``-parseable, all rejected live).
#
#   * Test A (R4/DEC-003) — the ``row_count_anomaly_by_period`` two-query
#     (stats + violation) Spark SQL, which has only ever been ``sqlglot``-parsed
#     (#223), never RUN. The prior #226 prune live cert exercised only the
#     ``not_null`` single-query path.
#   * Test B (R2/DEC-004) — the ``to_json(struct(*))`` per-row failing-row
#     capture branch of ``run_test_sql``, which never fired live because #226's
#     engineered candidates were all always-pass (0 failing rows → capture
#     branch skipped; see the adapter's ``run_test_sql`` docstring note).
#
# Both carry ``@pytest.mark.databricks`` (deselected by default) AND the shared
# runtime :func:`skip_reason` gate, so they SELF-SKIP cleanly with no rig. The
# live run itself is a MAINTAINER step (a later bead) — here we only ship
# correct, gracefully-skipping test code.
# ---------------------------------------------------------------------------

# ``--as-of`` for the anomaly cert. The engineered table carries a stable band
# of history days ending BEFORE this date plus an "as-of" bucket ON it, so the
# lookback window ``[as_of - 28d, as_of)`` holds far more than the
# ``min_samples_per_bucket`` floor (3) — the decision is a GENUINE evaluated
# outcome, not a cold-start degrade. Pinned so the two-query stats/violation
# windows are reproducible at ``(model, as_of)`` (the #171 DEC-001 carve-out).
_ANOMALY_AS_OF = date(2024, 3, 1)


@pytest.mark.databricks
def test_row_count_anomaly_by_period_evaluates_live_two_query_stats(tmp_path) -> None:
    """Prune a hand-crafted ``row_count_anomaly_by_period`` against a live
    engineered table and certify the two-query (stats + violation) path RAN.

    Skips cleanly under ``pytest -m databricks`` when any prerequisite is
    missing. With credentials present:

    1. Creates a tiny engineered table in the writable ``workspace.default``
       namespace with an ``event_date`` DATE column carrying a stable band of
       history days (2024-02-05 .. 2024-02-28) plus rows ON the ``--as-of``
       day (2024-03-01) — enough per-day buckets in the 28-day lookback to
       clear the cold-start floor (``min_samples_per_bucket=3``), so the
       decision is a genuine evaluated outcome.
    2. Builds an in-process :class:`Model` / :class:`Manifest` /
       :class:`CandidateSchema` carrying ONE model-level
       :class:`CandidateTestRowCountAnomalyByPeriod` over ``event_date``.
    3. Calls :func:`prune_tests` with ``scope="full"`` and
       ``as_of=_ANOMALY_AS_OF``. Under full scope the anomaly variant queries
       the SOURCE table directly (no sampling / materialise), so the engine
       issues the compiled **stats** query via
       :meth:`DatabricksAdapter.run_stats_query` and then the **violation**
       query via :meth:`run_test_sql` — the exact two-query split that has only
       ever been ``sqlglot``-parsed (#223), never executed on a real warehouse.
    4. Asserts a real :class:`PruneDecision` for the anomaly variant is
       produced with an *evaluated* ``reason`` (``kept`` / ``dropped`` /
       ``kept-without-evidence``), and — the load-bearing pin — that the
       decision carries a populated :class:`AnomalyTestStats`. Populated
       ``stats`` proves the stats query EXECUTED on the live warehouse and its
       rows parsed into the discriminated union; a degrade to
       ``StatsQueryNotSupportedError`` (or any live SQL rejection of the stats
       query) would route to ``kept-without-evidence`` with ``stats=None``, so
       ``stats is not None`` is precisely the "``run_stats_query`` did NOT
       degrade" certification.
    5. Tears the engineered table down with ``DROP TABLE IF EXISTS`` in a
       ``finally`` (idempotent).

    Traces to: #227 US-004 (R4/DEC-003 — live cert of the anomaly two-query
    stats path); #171 (the variant); epic #219.
    """
    if reason := skip_reason():
        pytest.skip(reason)

    catalog = _WRITABLE_CATALOG
    schema = _WRITABLE_SCHEMA
    table_name = _unique_table_name()
    quoted = _quoted_table(catalog, schema, table_name)

    # --- Build the engineered VALUES: a stable band of history days plus the
    # as-of bucket. Each day carries the same row count so the anomaly band is
    # tight and the stats window has ample samples (>> min_samples_per_bucket).
    # The as-of day itself gets rows so the violation query has a "current"
    # bucket to evaluate. The exact kept-vs-dropped outcome is deliberately NOT
    # asserted (any evaluated outcome certifies the path); the load-bearing
    # signal is that the two-query stats path RAN (stats populated).
    row_id = 0
    values_parts: list[str] = []
    history_start = date(2024, 2, 5)
    for offset in range(24):  # 24 distinct history days, all inside the lookback
        day = history_start + timedelta(days=offset)
        for _ in range(3):
            row_id += 1
            values_parts.append(f"({row_id}, DATE '{day.isoformat()}')")
    for _ in range(3):  # rows ON the as-of day → a real "current" bucket
        row_id += 1
        values_parts.append(f"({row_id}, DATE '{_ANOMALY_AS_OF.isoformat()}')")
    values_clause = ", ".join(values_parts)

    try:
        # Setup lives INSIDE the teardown try/finally so a partial CREATE (e.g.
        # the INSERT failing after the table exists) still hits the DROP below,
        # never leaking a live table.
        setup_adapter = build_live_adapter()
        with setup_adapter:
            cursor = setup_adapter._get_connection().cursor()
            try:
                cursor.execute(f"DROP TABLE IF EXISTS {quoted}")
                cursor.execute(f"CREATE TABLE {quoted} (id INT, event_date DATE)")
                cursor.execute(f"INSERT INTO {quoted} (id, event_date) VALUES {values_clause}")
            finally:
                cursor.close()

        # ``data_type="DATE"`` on the date column so the ``--as-of`` bound
        # literal is type-matched (``_date_value_literal`` — the DEC-012
        # partition-pruning predicate compares the bare column against a
        # type-matched literal). The model's ``name`` == the
        # ``CandidateSchema.name`` (the diff/anchor cross-stage convention).
        model = Model.model_validate(
            {
                "unique_id": f"model.signalforge_live.{table_name}",
                "name": table_name,
                "resource_type": "model",
                "package_name": "signalforge_live",
                "original_file_path": f"models/{table_name}.sql",
                "path": f"{table_name}.sql",
                "database": catalog,
                "schema": schema,
                "columns": {
                    "id": Column(name="id"),
                    "event_date": Column(name="event_date", data_type="DATE"),
                },
                "config": Config(materialized="table"),
            }
        )
        manifest = Manifest(metadata={}, nodes={model.unique_id: model})

        candidates = CandidateSchema(
            name=table_name,
            description="engineered anomaly live-e2e table",
            columns=(),
            tests=(
                CandidateTestRowCountAnomalyByPeriod(
                    date_column="event_date",
                    rationale="per-period row-count anomaly over the engineered daily band",
                ),
            ),
        )

        # ``scope="full"`` — the anomaly variant queries the SOURCE table
        # directly (no sampling / materialise), so the engine runs the compiled
        # stats query via ``run_stats_query`` then the violation query via
        # ``run_test_sql``. The engineered table is a handful of rows, so both
        # queries are cheap.
        config = PruneConfig(scope="full")

        # ``prune_tests`` owns the ``with adapter:`` block — pass a NOT-entered
        # adapter and thread the reproducibility ``as_of``.
        result = prune_tests(
            model,
            build_live_adapter(),
            candidates,
            manifest,
            config=config,
            as_of=_ANOMALY_AS_OF,
        )

        anomaly_decisions = [
            d for d in result.decisions if d.test.type == "row_count_anomaly_by_period"
        ]
        assert anomaly_decisions, (
            "expected exactly one row_count_anomaly_by_period PruneDecision — the "
            "hand-crafted candidate must reach the engine, compile to the "
            "(stats_sql, violation_sql) tuple, and produce a decision. Got "
            f"decision test types: {sorted({d.test.type for d in result.decisions})}"
        )
        decision = anomaly_decisions[0]

        # An *evaluated* reason — NOT a crash. All three are genuine engine
        # verdicts (kept/dropped are with-evidence; kept-without-evidence is the
        # documented degrade arm). The load-bearing distinction from a broken
        # path is that a decision exists at all AND that ``stats`` populated.
        assert decision.reason in {"kept", "dropped", "kept-without-evidence"}, (
            f"anomaly decision must carry an evaluated reason; got {decision.reason!r}"
        )
        assert decision.decision in {"kept", "dropped"}, (
            f"anomaly decision must be kept or dropped; got {decision.decision!r}"
        )

        # THE load-bearing pin: populated ``AnomalyTestStats`` proves the stats
        # query EXECUTED on the live warehouse and parsed into the discriminated
        # union. A degrade to StatsQueryNotSupportedError (or any live rejection
        # of the stats SQL) leaves ``stats=None`` and routes to
        # kept-without-evidence — so this assertion is exactly the "run_stats_query
        # did NOT degrade + the two-query stats path ran" certification.
        assert decision.stats is not None, (
            "expected AnomalyTestStats to populate on the decision — this proves "
            "run_stats_query executed the compiled Spark stats SQL live and its "
            "rows parsed (it did NOT degrade to StatsQueryNotSupportedError, which "
            "would leave stats=None and route to kept-without-evidence). This is "
            "the R4/DEC-003 certification the offline sqlglot parse-guard cannot "
            f"give. Got decision={decision!r}, reason={decision.reason!r}."
        )
        assert decision.stats.method in {"mad", "zscore", "percentile", "min_max"}, (
            f"AnomalyTestStats.method must be one of the four supported methods; "
            f"got {decision.stats.method!r}"
        )
        assert decision.stats.n_periods >= 1, (
            "AnomalyTestStats.n_periods must be >= 1 on a decision whose stats "
            f"query ran; got n_periods={decision.stats.n_periods!r}"
        )
    finally:
        teardown_adapter = build_live_adapter()
        with teardown_adapter:
            teardown_cursor = teardown_adapter._get_connection().cursor()
            try:
                teardown_cursor.execute(f"DROP TABLE IF EXISTS {quoted}")
            finally:
                teardown_cursor.close()


@pytest.mark.databricks
def test_run_test_sql_captures_failing_rows_live_to_json_struct() -> None:
    """Certify the ``to_json(struct(*))`` failing-row CAPTURE branch of
    :meth:`DatabricksAdapter.run_test_sql` against a live warehouse.

    Skips cleanly under ``pytest -m databricks`` when any prerequisite is
    missing. With credentials present, drives a real
    :meth:`DatabricksAdapter.run_test_sql` with a ``custom_sql``-shaped
    failing-rows SELECT that is GUARANTEED to return >= 1 row
    (``SELECT 1 AS failing_id, 'engineered-failure' AS reason`` — a constant
    single row, no table needed) and ``capture_failures=3`` (> 0), so the
    SECOND query (``SELECT to_json(struct(*)) AS failure_row FROM (<sql>) AS s
    LIMIT 3``) FIRES. This branch never ran during the #226 live pass because
    every engineered candidate there was always-pass (0 failing rows → capture
    skipped; see the adapter's ``run_test_sql`` docstring note), so the per-row
    ``to_json`` → ``json.loads`` marshalling (:meth:`_parse_failure_row`) has
    only ever been fake-driven + ``sqlglot``-parsed.

    Asserts:

    * ``passed is False`` and ``failure_count >= 1`` — the COUNT(*) wrap saw
      the failing row.
    * ``sample_failures`` is a non-empty list — the capture branch produced
      structured rows (NOT ``None``, which is the ``capture_failures == 0``
      shape).
    * Each captured row is a ``dict`` decoded from the ``to_json`` payload, and
      the first row carries the engineered columns/values (case-folded lookup,
      since Databricks lower-folds unquoted ``struct(*)`` field names) — proving
      the ``json.loads`` marshalling produced real structured content, not an
      empty / stringified blob.

    Traces to: #227 US-004 (R2/DEC-004 — live cert of the to_json(struct(*))
    failing-row capture); #224 DEC-007 (the capture branch); epic #219.
    """
    if reason := skip_reason():
        pytest.skip(reason)

    # A constant single-row failing-rows SELECT — no table needed, guaranteed
    # to return exactly one row so the COUNT(*) wrap sees failure_count == 1 and
    # the capture branch has a row to marshal. Two columns (an int + a string)
    # so the decoded ``struct(*)`` dict is genuinely structured, not trivial.
    failing_sql = "SELECT 1 AS failing_id, 'engineered-failure' AS reason"

    adapter = build_live_adapter()
    with adapter:
        result = adapter.run_test_sql(failing_sql, capture_failures=3)

    # The COUNT(*) wrap saw the engineered failing row.
    assert result.passed is False, "the engineered failing-rows SELECT must NOT pass"
    assert result.failure_count >= 1, (
        f"expected failure_count >= 1 from the constant failing row; got {result.failure_count!r}"
    )

    # The capture branch fired (capture_failures > 0) and produced structured
    # rows — NOT ``None`` (the capture_failures == 0 shape).
    assert result.sample_failures is not None, (
        "sample_failures must be a list (capture_failures=3 > 0 → the "
        "to_json(struct(*)) capture branch must fire), not None"
    )
    assert len(result.sample_failures) >= 1, (
        "expected at least one captured failing row from the to_json(struct(*)) "
        f"branch; got {result.sample_failures!r}"
    )

    first = result.sample_failures[0]
    assert isinstance(first, dict), (
        f"each captured row must be a dict decoded from to_json(struct(*)); got {type(first)!r}"
    )
    assert first, "the captured row must be non-empty (decoded from the to_json payload)"

    # Databricks lower-folds unquoted struct(*) field names; look up
    # case-insensitively so the assertion survives the fold. This pins that the
    # json.loads marshalling produced the REAL engineered content — the honest
    # certification that the capture branch decodes structured rows, not blobs.
    lowered = {str(k).lower(): v for k, v in first.items()}
    assert lowered.get("failing_id") == 1, (
        "the captured row must carry the engineered ``failing_id`` == 1 "
        f"(decoded from to_json(struct(*))); got row {first!r}"
    )
    assert "engineered-failure" in str(lowered.get("reason")), (
        "the captured row must carry the engineered ``reason`` string "
        f"(decoded from to_json(struct(*))); got row {first!r}"
    )
