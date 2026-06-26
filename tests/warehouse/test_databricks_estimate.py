"""Tests for the pure ``_parse_explain_cost_bytes`` plan-text parser
(issue #225 US-001, DEC-002 / DEC-003 / DEC-004 / DEC-005 / DEC-006 / DEC-007).

Spark/Databricks has no BigQuery-style ``dry_run`` byte count; the closest
primitive is ``EXPLAIN COST <sql>``, which annotates each optimized-logical-plan
node with cost-based-optimizer ``Statistics(sizeInBytes=<num> <unit>)``. This
parser is the Databricks analogue of Snowflake's ``_parse_explain_json_bytes``,
but it parses plan *text* (binary units, scientific notation, the no-stats
sentinel) rather than JSON — materially more fragile, so the table-driven
coverage below is load-bearing.

These are synthetic inline plan-text snippets — workers/CI can't reach a live
Databricks SQL warehouse. A maintainer-captured real fixture is a SEPARATE bead
(US-005). Engineered determinism: every parse assertion is mathematically
guaranteed against a known ``sizeInBytes``, never whatever the parser returns.
The parser needs no connection — it is a module-level pure function.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from signalforge.warehouse.adapters.databricks import (
    _SPARK_DEFAULT_SIZE_SENTINEL_BYTES,
    DatabricksAdapter,
    _parse_explain_cost_bytes,
)
from signalforge.warehouse.errors import (
    EstimateUnavailableError,
    QuerySyntaxError,
    TableNotFoundError,
)
from tests.warehouse._fake_databricks import FakeDatabricksConnection


def _stats(size: str) -> str:
    """A single-node plan fragment carrying one ``Statistics(sizeInBytes=...)``."""
    return f"Relation foo[id], Statistics(sizeInBytes={size}, rowCount=1)"


# --- Unit conversion (B / KiB / MiB / GiB) ---------------------------------


def test_single_bytes_value() -> None:
    """A bare ``B`` value converts 1:1."""
    assert _parse_explain_cost_bytes(_stats("512 B")) == 512


def test_kib_conversion() -> None:
    assert _parse_explain_cost_bytes(_stats("1.0 KiB")) == 1024


def test_mib_conversion() -> None:
    assert _parse_explain_cost_bytes(_stats("1.0 MiB")) == 1024**2


def test_gib_conversion() -> None:
    assert _parse_explain_cost_bytes(_stats("2.0 GiB")) == 2 * 1024**3


def test_tib_conversion() -> None:
    # TiB (power 4) — a distinct regex alternation arm + _SIZE_UNIT_POWERS key
    # that no other test exercises (a dropped `TiB` arm would slip line coverage).
    assert _parse_explain_cost_bytes(_stats("3.0 TiB")) == 3 * 1024**4


def test_pib_conversion() -> None:
    # PiB (power 5) — likewise a distinct arm/key; EiB is only reachable via the
    # no-stats sentinel, so PiB is the largest unit pinned as a real estimate.
    assert _parse_explain_cost_bytes(_stats("2.0 PiB")) == 2 * 1024**5


def test_decimal_value() -> None:
    """A decimal mantissa (``12.3 MiB``) truncates to int after scaling."""
    assert _parse_explain_cost_bytes(_stats("12.3 MiB")) == int(12.3 * 1024**2)


def test_scientific_notation_value() -> None:
    """Scientific notation (``5.0E+2 KiB`` == 500 KiB) parses."""
    assert _parse_explain_cost_bytes(_stats("5.0E+2 KiB")) == int(500 * 1024)


# --- Max-across-nodes selection (DEC-003) ----------------------------------


def test_returns_max_node_not_root() -> None:
    """A multi-node plan returns the MAX sizeInBytes (the leaf scan), NOT the
    tiny root output size — the cost proxy is the bytes scanned (DEC-003)."""
    plan = (
        "Aggregate [count(1)], Statistics(sizeInBytes=8.0 B, rowCount=1)\n"
        "+- Project [id], Statistics(sizeInBytes=12.0 MiB)\n"
        "   +- Relation foo[id] parquet, Statistics(sizeInBytes=12.0 MiB, rowCount=5.00E+5)"
    )
    assert _parse_explain_cost_bytes(plan) == 12 * 1024**2


def test_realistic_multiline_explain_cost_plan() -> None:
    """A faithful synthetic ``EXPLAIN COST`` optimized-logical-plan: an Aggregate
    over a Project over a Relation, each carrying Statistics — parses to the
    leaf-scan bytes (the largest node)."""
    plan = (
        "== Optimized Logical Plan ==\n"
        "Aggregate [region#3], [region#3, count(1) AS cnt#10L], "
        "Statistics(sizeInBytes=48.0 B, rowCount=2)\n"
        "+- Project [region#3], Statistics(sizeInBytes=3.0 MiB, rowCount=5.00E+5)\n"
        "   +- Filter (isnotnull(id#1) AND (id#1 > 0)), "
        "Statistics(sizeInBytes=3.0 MiB, rowCount=5.00E+5)\n"
        "      +- Relation spark_catalog.default.trips[id#1,region#3] parquet, "
        "Statistics(sizeInBytes=12.0 MiB, rowCount=1.00E+6)\n"
    )
    assert _parse_explain_cost_bytes(plan) == 12 * 1024**2


# --- The 8.0 EiB no-stats sentinel (DEC-004) -------------------------------


def test_eib_sentinel_raises_with_analyze_table_hint() -> None:
    """A ``8.0 EiB`` node is Spark's no-CBO-statistics default size → raises,
    and the detail names ``ANALYZE TABLE`` so the operator can act (DEC-004)."""
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_cost_bytes(_stats("8.0 EiB"))
    assert "ANALYZE TABLE" in excinfo.value.detail


def test_eib_sentinel_value_is_two_to_the_63() -> None:
    """Guard the sentinel constant itself: ``8 * 1024**6 == 2**63``."""
    assert _SPARK_DEFAULT_SIZE_SENTINEL_BYTES == 2**63


def test_sentinel_node_dominates_real_nodes() -> None:
    """When the leaf is stats-less (sentinel) it propagates up as the max →
    the whole estimate is unavailable, never a 9-exabyte report."""
    plan = (
        "Project [id], Statistics(sizeInBytes=4.0 MiB)\n"
        "+- Relation foo[id] parquet, Statistics(sizeInBytes=8.0 EiB)"
    )
    with pytest.raises(EstimateUnavailableError):
        _parse_explain_cost_bytes(plan)


# --- No sizeInBytes at all (DEC-005) ---------------------------------------


def test_no_size_in_bytes_raises() -> None:
    """A plan with zero ``sizeInBytes`` matches → raises, never fabricates 0."""
    plan = (
        "== Optimized Logical Plan ==\n"
        "Aggregate [count(1)]\n"
        "+- Relation spark_catalog.default.foo[id] parquet"
    )
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_cost_bytes(plan)
    assert "sizeInBytes" in excinfo.value.detail


# --- Non-finite / negative parsed value (DEC-007) --------------------------


def test_non_finite_scientific_overflow_raises() -> None:
    """A scientific mantissa that overflows ``float`` to ``inf`` (``1E+400``)
    is non-finite → raises rather than scaling an infinity into bytes."""
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_cost_bytes(_stats("1E+400 B"))
    assert "non-finite" in excinfo.value.detail


# --- Defensive non-str guards (DEC-007) ------------------------------------


def test_none_cell_raises() -> None:
    """A ``None`` cell (malformed connector return) → raises, never crashes."""
    with pytest.raises(EstimateUnavailableError):
        _parse_explain_cost_bytes(None)


def test_list_cell_raises() -> None:
    """A ``list`` cell (malformed connector return) → raises."""
    with pytest.raises(EstimateUnavailableError):
        _parse_explain_cost_bytes(["not", "plan", "text"])


# ===========================================================================
# DatabricksAdapter.estimate_query_bytes override (US-002, DEC-002 / DEC-008 /
# DEC-009 / DEC-012). These drive the real adapter through the injected
# FakeDatabricksConnection (no live warehouse). The EXPLAIN COST plan-text
# returned by the fake is a faithful synthetic optimized-logical-plan; the
# byte assertion is engineered-deterministic against a known leaf sizeInBytes.
# ===========================================================================

_EXPLAIN_COST_QUERY = r"^EXPLAIN COST"

# A faithful synthetic ``EXPLAIN COST`` optimized-logical-plan whose leaf scan
# carries Statistics(sizeInBytes=12.0 MiB) — the MAX node, so the parser
# returns exactly that.
_REALISTIC_PLAN = (
    "== Optimized Logical Plan ==\n"
    "Aggregate [region#3], [region#3, count(1) AS cnt#10L], "
    "Statistics(sizeInBytes=48.0 B, rowCount=2)\n"
    "+- Project [region#3], Statistics(sizeInBytes=3.0 MiB, rowCount=5.00E+5)\n"
    "   +- Relation spark_catalog.default.trips[id#1,region#3] parquet, "
    "Statistics(sizeInBytes=12.0 MiB, rowCount=1.00E+6)\n"
)


class _RecordingDatabricksConnection(FakeDatabricksConnection):
    """A :class:`FakeDatabricksConnection` that records every executed SQL so a
    test can assert the exact statement the adapter dispatched."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.executed: list[str] = []

    def _consume_execute(self, sql: str) -> tuple[list[Any], list[Any] | None]:  # type: ignore[override]
        self.executed.append(sql)
        return super()._consume_execute(sql)


def test_estimate_query_bytes_happy_path_returns_leaf_scan_bytes() -> None:
    """``EXPLAIN COST`` plan text → the MAX (leaf-scan) ``sizeInBytes`` =
    12.0 MiB in bytes."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_EXPLAIN_COST_QUERY, returns=[(_REALISTIC_PLAN,)])
    adapter = DatabricksAdapter(connection=conn)

    assert adapter.estimate_query_bytes("SELECT 1") == int(12.0 * 1024**2)
    conn.assert_all_expectations_met()


def test_estimate_query_bytes_injection_guard_rejects_before_any_cursor() -> None:
    """``validate_test_sql`` rejects a ``;``-bearing statement BEFORE any
    ``EXPLAIN COST`` is dispatched — the fake records no execute."""
    conn = _RecordingDatabricksConnection()
    # No expectation queued: if the adapter reached the cursor it would raise
    # AssertionError("unexpected query"), not the validation error.
    adapter = DatabricksAdapter(connection=conn)

    with pytest.raises(QuerySyntaxError):
        adapter.estimate_query_bytes("SELECT 1; DROP TABLE x")

    assert conn.executed == []


def test_estimate_query_bytes_maps_connector_exception() -> None:
    """An SDK error from ``EXPLAIN COST`` routes through
    ``map_databricks_exception`` and surfaces as the mapped
    :class:`WarehouseError` (chained ``from`` the original)."""
    from databricks.sql import exc as dbe

    err = dbe.ServerOperationError(  # type: ignore[attr-defined]
        "[TABLE_OR_VIEW_NOT_FOUND] Table or view not found: main.sch.foo"
    )
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_EXPLAIN_COST_QUERY, returns=err)
    adapter = DatabricksAdapter(connection=conn)

    with pytest.raises(TableNotFoundError) as excinfo:
        adapter.estimate_query_bytes("SELECT 1")
    assert excinfo.value.__cause__ is err


def test_estimate_query_bytes_dispatches_explain_cost_prefix_with_verbatim_sql() -> None:
    """The dispatched statement starts with ``EXPLAIN COST `` and embeds the
    validated user SQL verbatim (DEC-008)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_EXPLAIN_COST_QUERY, returns=[(_REALISTIC_PLAN,)])
    adapter = DatabricksAdapter(connection=conn)
    user_sql = "SELECT `id` FROM `main`.`sales`.`orders` WHERE `id` IS NULL"

    adapter.estimate_query_bytes(user_sql)

    assert conn.executed == [f"EXPLAIN COST {user_sql}"]


def test_estimate_query_bytes_closes_cursor_on_success() -> None:
    """``_execute_scalar`` releases the cursor after a successful EXPLAIN COST."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_EXPLAIN_COST_QUERY, returns=[(_REALISTIC_PLAN,)])
    adapter = DatabricksAdapter(connection=conn)

    adapter.estimate_query_bytes("SELECT 1")

    assert conn.cursors, "expected the adapter to open at least one cursor"
    assert all(c.closed for c in conn.cursors)


def test_estimate_query_bytes_closes_cursor_on_mapped_exception() -> None:
    """The cursor is released even when EXPLAIN COST raises (the ``finally`` arm)."""
    from databricks.sql import exc as dbe

    err = dbe.ServerOperationError(  # type: ignore[attr-defined]
        "[TABLE_OR_VIEW_NOT_FOUND] Table or view not found: main.sch.foo"
    )
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_EXPLAIN_COST_QUERY, returns=err)
    adapter = DatabricksAdapter(connection=conn)

    with pytest.raises(TableNotFoundError):
        adapter.estimate_query_bytes("SELECT 1")

    assert conn.cursors and all(c.closed for c in conn.cursors)


def test_estimate_query_bytes_empty_result_raises_unavailable() -> None:
    """No rows from EXPLAIN COST → :class:`EstimateUnavailableError`, never a
    fabricated 0."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_EXPLAIN_COST_QUERY, returns=[])
    adapter = DatabricksAdapter(connection=conn)

    with pytest.raises(EstimateUnavailableError):
        adapter.estimate_query_bytes("SELECT 1")


def test_estimate_query_bytes_reraises_unmapped_connector_exception() -> None:
    """An exception ``map_databricks_exception`` does NOT recognise passes
    through ``_execute_scalar`` unchanged (the ``mapped is exc`` → bare ``raise``
    arm) — the original object surfaces, NOT a wrapped ``WarehouseError``."""
    original = RuntimeError("transient cursor failure")
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_EXPLAIN_COST_QUERY, returns=original)
    adapter = DatabricksAdapter(connection=conn)

    with pytest.raises(RuntimeError) as excinfo:
        adapter.estimate_query_bytes("SELECT 1")
    assert excinfo.value is original


def test_estimate_query_bytes_normalises_dict_row_to_first_cell() -> None:
    """A dict-cursor row (``{"plan": "<text>"}``) has its first VALUE fed to the
    parser, not the whole mapping — otherwise the parser would see a dict and
    trip a false degrade."""
    conn = FakeDatabricksConnection()
    conn.expect_execute(matching=_EXPLAIN_COST_QUERY, returns=[{"plan": _REALISTIC_PLAN}])
    adapter = DatabricksAdapter(connection=conn)

    assert adapter.estimate_query_bytes("SELECT 1") == int(12.0 * 1024**2)
    conn.assert_all_expectations_met()


# ===========================================================================
# Committed-fixture parser pin (US-005, DEC-010 / DEC-007 of issue #225).
#
# The inline snippets above pin the parser against *synthetic* plan text. These
# two tests pin it against committed FILE fixtures shaped like real Databricks
# ``EXPLAIN COST`` output (an Optimized Logical Plan + Physical Plan over a
# parquet ``Relation`` leaf). The fixtures are DOCUMENTED-FORMAT PLACEHOLDERS —
# a maintainer captures the real plan from Databricks Free Edition (the regen
# command lives in ``tests/fixtures/warehouse/databricks/README.md``) and swaps
# them in; live end-to-end validity is certified by issue #226. See that README
# for the capture one-liner + the "TO BE REPLACED" note.
#
# Engineered determinism: ``explain_cost_sample.txt`` bakes its leaf-scan node at
# ``Statistics(sizeInBytes=128.0 MiB)`` — the MAX node — so the parser returns
# exactly ``int(128.0 * 1024**2)``, NEVER whatever the parser happens to compute.
# ===========================================================================

_DATABRICKS_FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "warehouse" / "databricks"
)

# The leaf-scan ``sizeInBytes`` baked into ``explain_cost_sample.txt`` (128.0 MiB).
_SAMPLE_FIXTURE_LEAF_BYTES = int(128.0 * 1024**2)


def test_explain_cost_sample_fixture_parses_to_known_leaf_scan_bytes() -> None:
    """The committed ``explain_cost_sample.txt`` (a multi-node Optimized Logical
    Plan + Physical Plan whose parquet ``Relation`` leaf carries
    ``Statistics(sizeInBytes=128.0 MiB)``) parses to exactly the leaf-scan byte
    count — the MAX across nodes (DEC-003). A leading provenance line / blank
    lines must NOT perturb the extracted max."""
    plan = (_DATABRICKS_FIXTURE_DIR / "explain_cost_sample.txt").read_text()
    assert _parse_explain_cost_bytes(plan) == _SAMPLE_FIXTURE_LEAF_BYTES


def test_explain_cost_no_stats_fixture_raises_unavailable() -> None:
    """The committed ``explain_cost_no_stats.txt`` (every node showing Spark's
    ``8.0 EiB`` no-CBO-statistics sentinel) raises
    :class:`EstimateUnavailableError`, never a fabricated 9-exabyte figure
    (DEC-004). The ``detail`` names ``ANALYZE TABLE`` as the remediation."""
    plan = (_DATABRICKS_FIXTURE_DIR / "explain_cost_no_stats.txt").read_text()
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_cost_bytes(plan)
    assert "ANALYZE TABLE" in excinfo.value.detail
