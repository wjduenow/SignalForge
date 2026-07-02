"""Gated live certification for Snowflake ``column_stats`` (#258 US-004).

This is the **live certification for the ``column_stats`` implementation** — the
#258 backfill that gives Snowflake parity with Databricks and enables
``safety: aggregate-only`` on Snowflake (DEC-001). The offline hand-fake /
``fakesnow`` suite (``tests/warehouse/test_snowflake_adapter.py``,
``tests/warehouse/test_snowflake_adapter_fakesnow.py``) pins the compiled SQL's
*shape* + ``fakesnow`` execution of the scalar aggregate, but neither certifies
that a **real** Snowflake ACCEPTS the shape — chiefly the **DEC-003 complex-type
MIN/MAX skip-set**. ``fakesnow``'s DuckDB backend does not model Snowflake's
``ARRAY`` / ``OBJECT`` / ``VARIANT`` ``MIN`` / ``MAX`` rejection semantics, so
only a live run settles whether ``_COMPLEX_SNOWFLAKE_TYPES`` (``{ARRAY, OBJECT,
VARIANT, GEOGRAPHY, GEOMETRY}``) is complete — the #227 lesson restated for
Snowflake: **fakes/parse certify SHAPE, live certifies ACCEPTANCE.** An
UNDER-skipped unorderable column would raise and fail the whole batch (DEC-003),
so this test's complex-column assertions are the load-bearing skip-set
validation the plan calls out.

NO LLM, NO ``generate`` CLI: this test builds a :class:`TableRef` in-process and
calls :meth:`SnowflakeAdapter.column_stats` directly inside a ``with adapter:``
block (the ABC batching contract — DEC-025 requires the block; DEC-006 of #258
opens the per-table batch caches on ``__enter__``). A **separate** short-lived
adapter does the engineered-table setup and the ``DROP TABLE`` teardown.

The engineered table MUST live in a WRITABLE schema: the read-only
``SNOWFLAKE_SAMPLE_DATA`` share carries no ``ARRAY`` / ``OBJECT`` / ``VARIANT``
column and cannot accept a ``CREATE TABLE`` — so, exactly like
``test_snowflake_prune_live.py``, the table is created in the maintainer's
``SNOWFLAKE_DATABASE.SNOWFLAKE_SCHEMA`` and dropped in ``finally``.

**No drift detector** for :class:`ColumnStats`: it is frozen, produced
in-process, and never read back from disk (mirrors the ingest-layer rule +
DEC-008 of #258).

Belt-and-suspenders gating (``.claude/rules/testing-signal.md`` § "End-to-end
gated tests") — identical to ``test_snowflake_prune_live.py``:

1. ``@pytest.mark.snowflake`` — registered in ``pyproject.toml``
   ``[tool.pytest.ini_options].markers`` and deselected by the default
   ``addopts`` (``-m '... and not snowflake'``), so the default ``pytest`` run
   never collects this test.
2. A runtime :func:`_skip_reason` — when a maintainer runs ``pytest -m
   snowflake`` but lacks credentials, each missing prerequisite surfaces as a
   distinct skip-with-reason rather than a confusing connection error.

Required env vars (each missing one yields its own distinct skip reason):

* ``SF_RUN_SNOWFLAKE=1`` — the project-wide opt-in for "this test talks to a
  real warehouse" (accepts ``1``/``true``/``yes``/``on``).
* ``SNOWFLAKE_ACCOUNT`` / ``SNOWFLAKE_USER`` / ``SNOWFLAKE_PASSWORD`` — the
  minimal password-auth connection triple.
* ``SNOWFLAKE_WAREHOUSE`` — compute context for the engineered ``CREATE TABLE``,
  the ``INFORMATION_SCHEMA.COLUMNS`` catalog lookup, and the per-column
  aggregate.
* ``SNOWFLAKE_DATABASE`` + ``SNOWFLAKE_SCHEMA`` — the **WRITABLE** target where
  the engineered table is created (and dropped in teardown).

**Cost guidance — set a Snowflake resource monitor FIRST.** Before running,
create a resource monitor with a hard credit cap. Use an **XS (extra-small)
warehouse** with **aggressive auto-suspend** (e.g. 60 seconds). The engineered
table is a handful of rows, so the catalog lookup + per-column aggregate are
tiny; the dominant cost is warehouse spin-up.

Run via the maintainer-only invocation (``--no-cov`` because ``--cov-fail-under``
in ``addopts`` would fail a marker-specific run that exercises only a fraction of
the codebase)::

    export SF_RUN_SNOWFLAKE=1
    export SNOWFLAKE_ACCOUNT=<org-account>
    export SNOWFLAKE_USER=<user>
    export SNOWFLAKE_PASSWORD=<password>
    export SNOWFLAKE_WAREHOUSE=<xs-warehouse>
    export SNOWFLAKE_DATABASE=<writable-database>
    export SNOWFLAKE_SCHEMA=<writable-schema>
    uv run pytest -m snowflake --no-cov

Engineered determinism (``.claude/rules/testing-signal.md`` § "Engineered
determinism"): the assertions do NOT depend on any LLM output — the engineered
rows are hand-crafted, so every count / null / min / max assertion is
mathematically guaranteed. The scalar ``id`` / ``name`` columns are populated on
both rows (count=2, nulls=0); the complex ``tags`` / ``meta`` / ``payload``
columns hold a value on row 1 and NULL on row 2 (count=1, nulls=1) so the
null-count and skip assertions are meaningful.

Traces to: #258 US-004 (gated live cert of Snowflake ``column_stats`` —
scalar populate + complex-type MIN/MAX skip-set validation, DEC-003/DEC-008).
"""

from __future__ import annotations

import os
import uuid

import pytest

from signalforge.warehouse import ColumnStats, SnowflakeAdapter, TableRef

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Connection env vars the setup / stats / teardown adapters need for password
# auth, plus the writable namespace the engineered table is created in.
_REQUIRED_CONN_VARS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
)

# Engineered-table name PREFIX. The full name gets a per-run random suffix (see
# ``_unique_table_name``) so two concurrent maintainer runs against the same
# writable schema cannot race on the same ``DROP TABLE`` / clobber an unrelated
# leftover object. The prefix + suffix are a valid bare identifier (the strict
# DEC-013 regex used by ``TableRef``).
_ENGINEERED_TABLE_PREFIX = "sf_colstats_live_engineered"


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
    an ``INFORMATION_SCHEMA.COLUMNS`` lookup, per-column aggregates, DROP TABLE).
    Each missing prerequisite yields its own distinct reason so a maintainer
    running ``pytest -m snowflake`` sees exactly what to set.
    """
    if not _snowflake_runs_enabled():
        return "SF_RUN_SNOWFLAKE=1 required (live test talks to a real Snowflake warehouse)"
    for var in _REQUIRED_CONN_VARS:
        if not os.environ.get(var):
            return (
                f"{var} required (Snowflake connection / writable-target parameter "
                f"for the live column_stats e2e)"
            )
    return None


def _make_adapter() -> SnowflakeAdapter:
    """Construct a real :class:`SnowflakeAdapter` from the env vars.

    A fresh adapter is built per use (setup / stats / teardown) so a ``with``
    block's session close does not strand another phase's cursors — each adapter
    owns its own connection.
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

    Must fold to UPPER then quote — byte-identical to the adapter's ``_quote`` —
    so the table this test CREATEs / DROPs directly is the same case-sensitive
    object the adapter's ``column_stats`` catalog lookup + aggregate REFERENCE
    (the adapter folds the :class:`TableRef` to UPPER before quoting). A
    case-preserved helper would create ``"…<lowercase>"`` while the adapter
    references the upper-folded ``"…<UPPERCASE>"`` → "Table not found".
    """
    return f'"{database.upper()}"."{schema.upper()}"."{name.upper()}"'


@pytest.mark.snowflake
def test_column_stats_live_scalar_populated_and_complex_skipped() -> None:
    """Certify ``column_stats`` against a live engineered table: scalar columns
    populate min/max, complex (ARRAY/OBJECT/VARIANT) columns return min=max=None
    without raising (the DEC-003 skip-set validation).

    Skips cleanly under ``pytest -m snowflake`` when any prerequisite is
    missing. With credentials present:

    1. Creates a tiny engineered table in the writable
       ``SNOWFLAKE_DATABASE.SNOWFLAKE_SCHEMA`` — scalar columns ``id`` (NUMBER)
       and ``name`` (VARCHAR), populated on both rows; complex columns ``tags``
       (ARRAY), ``meta`` (OBJECT), ``payload`` (VARIANT), populated on row 1 and
       NULL on row 2. Complex constructors (``ARRAY_CONSTRUCT`` etc.) are not
       constant expressions, so the rows are inserted via ``INSERT … SELECT …
       UNION ALL SELECT …`` rather than ``INSERT … VALUES``.
    2. Inside ``with adapter:`` (the ABC batching contract — DEC-025 / DEC-006 of
       #258), calls :meth:`SnowflakeAdapter.column_stats` per column against a
       :class:`TableRef` for the engineered table.
    3. Asserts the SCALAR columns return populated
       ``count``/``distinct``/``nulls`` AND non-``None`` ``min``/``max`` AND a
       non-empty ``data_type``.
    4. Asserts the COMPLEX columns return ``min is None`` and ``max is None``
       WITHOUT raising — the load-bearing #227-style skip-set validation
       (DEC-003) — with populated ``count`` (1, one non-null row) / ``nulls``
       (1) and a non-empty ``data_type``.
    5. Tears the engineered table down with ``DROP TABLE IF EXISTS`` in a
       ``finally`` (idempotent; tolerates a partial-setup failure).
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    database = os.environ["SNOWFLAKE_DATABASE"]
    schema = os.environ["SNOWFLAKE_SCHEMA"]
    # Per-run unique name so concurrent runs don't race on DROP / clobber.
    table_name = _unique_table_name()
    quoted = _quoted_table(database, schema, table_name)

    # --- Setup: create + populate the engineered table (own short-lived adapter).
    setup_adapter = _make_adapter()
    with setup_adapter:
        cursor = setup_adapter._get_connection().cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {quoted}")
            cursor.execute(
                f"CREATE TABLE {quoted} "
                f"(id NUMBER, name VARCHAR, tags ARRAY, meta OBJECT, payload VARIANT)"
            )
            # Complex constructors are not constant expressions, so INSERT …
            # VALUES is rejected — use INSERT … SELECT … UNION ALL SELECT ….
            # Row 1 populates every column; row 2 leaves the complex columns
            # NULL (typed casts keep the UNION branch types unified) so the
            # complex columns get count=1, nulls=1.
            cursor.execute(
                f"INSERT INTO {quoted} (id, name, tags, meta, payload) "
                f"SELECT 1, 'alpha', ARRAY_CONSTRUCT(1, 2, 3), "
                f"OBJECT_CONSTRUCT('k', 'v1'), TO_VARIANT(100) "
                f"UNION ALL "
                f"SELECT 2, 'bravo', NULL::ARRAY, NULL::OBJECT, NULL::VARIANT"
            )
        finally:
            cursor.close()

    try:
        # ``project`` = the writable database; ``dataset`` = the schema; ``name``
        # = the engineered table. The adapter folds each to UPPER before quoting,
        # so the raw (lowercased) values here resolve to the same object created
        # above (mirrors ``test_snowflake_prune_live.py``'s ``TableRef`` shape).
        table_ref = TableRef(project=database, dataset=schema, name=table_name)

        # ``column_stats`` MUST run inside a ``with adapter:`` block — the
        # ``__enter__`` opens the per-table batch caches; outside the block the
        # DEC-025 guard raises ``RuntimeError``.
        stats_adapter = _make_adapter()
        with stats_adapter:
            id_stats: ColumnStats = stats_adapter.column_stats(table_ref, "id")
            name_stats: ColumnStats = stats_adapter.column_stats(table_ref, "name")
            tags_stats: ColumnStats = stats_adapter.column_stats(table_ref, "tags")
            meta_stats: ColumnStats = stats_adapter.column_stats(table_ref, "meta")
            payload_stats: ColumnStats = stats_adapter.column_stats(table_ref, "payload")

        # --- Scalar columns: fully populated, min/max present. ----------------
        for label, stats in (("id", id_stats), ("name", name_stats)):
            assert stats.count == 2, (
                f"scalar column {label!r}: both rows are populated, so "
                f"count should be 2; got {stats.count} ({stats!r})"
            )
            assert stats.distinct == 2, (
                f"scalar column {label!r}: two distinct values, so distinct "
                f"should be 2; got {stats.distinct} ({stats!r})"
            )
            assert stats.nulls == 0, (
                f"scalar column {label!r}: no NULLs, so nulls should be 0; "
                f"got {stats.nulls} ({stats!r})"
            )
            assert stats.min is not None, (
                f"scalar column {label!r}: MIN should be populated (orderable "
                f"type, not skipped); got min=None ({stats!r})"
            )
            assert stats.max is not None, (
                f"scalar column {label!r}: MAX should be populated (orderable "
                f"type, not skipped); got max=None ({stats!r})"
            )
            assert stats.data_type, (
                f"scalar column {label!r}: data_type should be a non-empty "
                f"warehouse type string; got {stats.data_type!r} ({stats!r})"
            )

        # --- Complex columns: MIN/MAX skipped (DEC-003), no raise. ------------
        # THIS is the load-bearing #227-style skip-set validation: if any of
        # ARRAY / OBJECT / VARIANT were NOT in ``_COMPLEX_SNOWFLAKE_TYPES``, the
        # aggregate would have emitted MIN/MAX for it and (per the plan's DEC-003
        # analysis) Snowflake would raise, failing the whole batch — so reaching
        # these assertions at all proves the skip-set covers these types, and
        # the ``min is None`` / ``max is None`` checks confirm they were skipped.
        for label, stats in (
            ("tags", tags_stats),
            ("meta", meta_stats),
            ("payload", payload_stats),
        ):
            assert stats.min is None, (
                f"complex column {label!r}: MIN must be skipped (DEC-003 skip-set) "
                f"so min should be None; got {stats.min!r} ({stats!r})"
            )
            assert stats.max is None, (
                f"complex column {label!r}: MAX must be skipped (DEC-003 skip-set) "
                f"so max should be None; got {stats.max!r} ({stats!r})"
            )
            assert stats.count == 1, (
                f"complex column {label!r}: row 1 populated, row 2 NULL, so the "
                f"non-null count should be 1; got {stats.count} ({stats!r})"
            )
            assert stats.nulls == 1, (
                f"complex column {label!r}: exactly one NULL row, so nulls "
                f"should be 1; got {stats.nulls} ({stats!r})"
            )
            assert stats.data_type, (
                f"complex column {label!r}: data_type should be a non-empty "
                f"warehouse type string (e.g. ARRAY/OBJECT/VARIANT); got "
                f"{stats.data_type!r} ({stats!r})"
            )
    finally:
        # --- Teardown: drop the engineered table (idempotent). ----------------
        # A fresh adapter — the stats adapter's session has been closed by its
        # own ``__exit__``. ``IF EXISTS`` tolerates a partial setup where the
        # table was never created.
        teardown_adapter = _make_adapter()
        with teardown_adapter:
            teardown_cursor = teardown_adapter._get_connection().cursor()
            try:
                teardown_cursor.execute(f"DROP TABLE IF EXISTS {quoted}")
            finally:
                teardown_cursor.close()
