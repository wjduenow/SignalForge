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

import pytest

from signalforge.warehouse.adapters.databricks import (
    _SPARK_DEFAULT_SIZE_SENTINEL_BYTES,
    _parse_explain_cost_bytes,
)
from signalforge.warehouse.errors import EstimateUnavailableError


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
