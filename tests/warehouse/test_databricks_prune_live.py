"""Gated live materialised-sample prune e2e against a real Databricks (#226 US-004).

This is the **live certification for the Databricks materialised sample path** —
the ``CREATE TEMPORARY TABLE <cat>.<sch>._sf_sample_<run_id> AS <inline-predicate
sample body>`` CTAS that :meth:`DatabricksAdapter.materialise_sample` emits and the
prune compiler then REFERENCEs. The offline surface (hand-rolled fakes in
``tests/warehouse/_fake_databricks.py`` + the ungated ``sqlglot`` parse-guard in
``test_databricks_sql_parse.py`` / ``test_databricks_adapter.py``) pins the emitted
Databricks SQL's *shape* and ``sqlglot`` parse-validity, but neither certifies that
a **real** Databricks SQL warehouse accepts the SQL — a snapshot pins invalid SQL
byte-for-byte (the #121/#124/#171 lesson). Only a live run certifies, end to end:

1. ``materialise_sample`` — whether Databricks accepts a **qualified** temporary
   table name in ``CREATE TEMPORARY TABLE <cat>.<sch>.<temp> AS ...`` AND whether
   the ``databricks-sql-connector`` persists the session across queries (so the
   temp table is reachable from the follow-up ``run_test_sql``). Both are flagged
   as #226 live-cert items in the adapter's ``materialise_sample`` docstring.
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
its ``CREATE TEMPORARY TABLE`` in the source catalog / schema, so the source MUST
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

import pytest

from signalforge.draft.models import CandidateColumn, CandidateSchema, CandidateTestNotNull
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
       ``CREATE TEMPORARY TABLE`` in the source catalog / schema.
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
       ``sample_strategy="materialised"`` — the engine materialises a temp-table
       sample via the inline-predicate CTAS (``CREATE TEMPORARY TABLE
       <cat>.<sch>._sf_sample_<run_id> AS SELECT * FROM <src> AS t WHERE
       MOD((xxhash64(...) & 9223372036854775807), <bucket>) < 1 ...``) and runs
       the compiled ``not_null`` against it. This certifies the #226 live items:
       a qualified temp-table name in ``CREATE TEMPORARY TABLE`` AND the
       connector persisting the session so the follow-up ``run_test_sql`` reaches
       the temp table.
    6. Asserts at least one :class:`PruneDecision` is ``decision == "dropped"``
       with ``reason == "always-passes"`` — the v0.1 differentiator
       (Architectural Commitment #1).
    7. Tears the engineered table down with ``DROP TABLE IF EXISTS`` in a
       ``finally`` (idempotent). The materialised temp table is session-scoped
       and reaped when ``prune_tests`` closes the prune adapter's connection.
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
    # ``prune_tests`` (scope=sample, materialised) materialises a temp-table
    # sample FROM this source table on its OWN adapter/connection, so the source
    # must persist beyond the setup session — a regular (non-temp) table created
    # here, dropped in teardown. (The materialised sample temp table is
    # session-scoped and reaped when the prune adapter closes its connection.)
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
        # materialises a temp-table sample via the inline-predicate CTAS, then
        # runs the compiled ``not_null`` against it. This exercises the exact
        # #226 live-cert path: a qualified ``CREATE TEMPORARY TABLE`` + the
        # connector persisting the session across queries. The engineered table
        # is a handful of rows, so the CTAS + COUNT(*) are cheap.
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
