"""Ungated ``sqlglot`` ``databricks``-dialect parse-guard over every committed
Databricks compiler fixture (#223 US-004).

Unlike the Snowflake parse-guard (``tests/prune/test_compiler_fakesnow.py``,
gated behind ``@pytest.mark.snowflake`` alongside its ``fakesnow`` execution
tests), this guard runs **UNGATED** — it is collected and executed by the
default ``uv run pytest`` with NO pytest marker (DEC-002 of
``plans/super/223-databricks-prune-compiler.md``, deliberately deviating from
the issue text's "gated under the ``databricks`` marker").

Why ungated:

* ``sqlglot`` is a **base runtime dependency** (``sqlglot>=30,<31`` in
  ``[project].dependencies``), so it is always importable in CI — no
  maintainer-only install is required to run this guard.
* Databricks has **no offline execution fake** (the Snowflake guard can lean on
  ``fakesnow``/DuckDB; there is no equivalent for Spark SQL), so this
  parse-guard is the *sole* automated validity gate for the Databricks compiler
  SQL until the live cert lands in #226. Gating it behind a marker CI never runs
  would mean validity is only checked when a maintainer remembers ``-m
  databricks`` — exactly the gap the #121 lesson warns against
  (``.claude/rules/prune-engine.md`` § "Compiler is dialect-driven":
  *"a new dialect's SQL needs a parser/executor in the loop, not just snapshot
  equality — snapshot equality certifies shape, not validity"*).

What it certifies (and what it does NOT):

* The byte-exact snapshot fixtures (``tests/prune/test_compiler.py``) certify
  the **shape** of the emitted SQL — that the compiler renders the exact bytes
  we expect for a given Databricks dialect.
* This guard certifies **syntactic validity** — that those exact bytes parse as
  legal Spark/Databricks SQL under ``sqlglot``'s ``databricks`` dialect. A
  snapshot can pin invalid SQL byte-for-byte; only a parser in the loop catches
  a mis-shaped ``Dialect`` template (a reserved-word collision, a malformed
  date-arithmetic fragment, a bad quote placement).
* Real-**Spark execution semantics** (``xxhash64`` value behaviour, identifier
  case-folding against a live Unity Catalog table, ``TABLESAMPLE`` /
  projection-placement acceptance) are deferred to the gated live harness in
  #226 — ``sqlglot`` parses SQL, it does not run it.

The two fixture directories are globbed at collection time so a future
Databricks fixture is auto-covered without editing this test (mirrors the
Snowflake guard's discovery style). A pinned floor (``>= 32``: 16 top-level +
16 anomaly) guards against an empty/typo'd glob making the guard vacuously
pass.

Traces to: plans/super/223-databricks-prune-compiler.md US-004 / DEC-002.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot
from sqlglot.errors import ParseError

from signalforge.draft.models import CandidateTestCustomSQL
from signalforge.manifest.models import Column, Manifest, Model
from signalforge.prune.compiler import _compile_test, _InvalidIdentifier
from signalforge.warehouse.models import BIGQUERY_DIALECT, DATABRICKS_DIALECT, TableRef

# ``sqlglot`` is a base runtime dependency — import it directly at module scope.
# Do NOT ``pytest.importorskip`` it: that pattern is only for the gated
# marker/fakesnow deps that may be absent from the default environment.

_FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures" / "prune" / "compiled_sql"
_DATABRICKS_FIXTURES_DIR = _FIXTURES_ROOT / "databricks"
_ANOMALY_DATABRICKS_FIXTURES_DIR = _FIXTURES_ROOT / "anomaly" / "databricks"

# Discover every committed Databricks fixture (top-level + anomaly) at
# collection time. Sorted for deterministic parametrize ordering.
_ALL_DATABRICKS_FIXTURES: list[Path] = sorted(_DATABRICKS_FIXTURES_DIR.glob("*.sql")) + sorted(
    _ANOMALY_DATABRICKS_FIXTURES_DIR.glob("*.sql")
)

# Pinned floor: 16 top-level + 16 anomaly (4 methods × 2 seasonality × 2 query
# types) = 32. ``>=`` so a future fixture grows the set without editing this.
_EXPECTED_MIN_FIXTURES = 32


def _fixture_id(path: Path) -> str:
    """Readable parametrize id relative to the fixtures root, so anomaly
    (``anomaly/databricks/…``) and top-level (``databricks/…``) fixtures stay
    distinguishable in the test report — ``path.parent.name`` is ``databricks``
    for BOTH dirs, so it can't tell them apart."""
    return str(path.relative_to(_FIXTURES_ROOT))


def test_databricks_fixture_set_is_non_empty() -> None:
    """The glob discovered at least the 32 committed fixtures.

    Without this floor an empty or mistyped glob would make the parse-guard
    below vacuously pass (zero parametrize cases = zero assertions), silently
    disabling the sole automated validity gate for the Databricks compiler SQL.
    """
    assert len(_ALL_DATABRICKS_FIXTURES) >= _EXPECTED_MIN_FIXTURES, (
        f"expected >= {_EXPECTED_MIN_FIXTURES} Databricks fixtures, "
        f"discovered {len(_ALL_DATABRICKS_FIXTURES)}: "
        f"{[_fixture_id(p) for p in _ALL_DATABRICKS_FIXTURES]}"
    )


@pytest.mark.parametrize("fixture_path", _ALL_DATABRICKS_FIXTURES, ids=_fixture_id)
def test_every_databricks_fixture_parses_under_databricks_dialect(
    fixture_path: Path,
) -> None:
    """Every committed Databricks compiler fixture must parse under sqlglot's
    ``databricks`` dialect.

    A ``sqlglot.errors.ParseError`` here means the compiler emitted invalid
    Spark/Databricks SQL — e.g. a mis-shaped :class:`Dialect` template field
    (a backtick-quoting slip, a reserved-word CTE alias, a malformed
    ``DATE_TRUNC('unit', date)`` / ``xxhash64(...)`` / ``PERCENTILE_CONT … WITHIN
    GROUP`` fragment). This reaches both the row-level fixtures and the
    sample-mode + row-count-anomaly fixtures (no execution needed), making it the
    load-bearing validity certification for the Databricks compiler SQL until the
    live cert in #226.
    """
    sql = fixture_path.read_text(encoding="utf-8")
    # Raises sqlglot.errors.ParseError on invalid Databricks syntax.
    parsed = sqlglot.parse_one(sql, dialect="databricks")
    assert parsed is not None


def test_parse_guard_rejects_malformed_databricks_sql() -> None:
    """Planted-violation self-check: the guard CAN fail.

    Per ``.claude/rules/testing-signal.md`` (the planted-violation philosophy),
    a gate is only trustworthy if a deliberately-broken input is rejected. A
    parse-guard's "violation" is syntactically-invalid SQL — assert
    ``sqlglot.parse_one(..., dialect="databricks")`` raises
    ``sqlglot.errors.ParseError`` on a
    clearly-malformed string, proving the real guard above would catch a
    compiler that started emitting invalid Databricks SQL.
    """
    with pytest.raises(ParseError):
        sqlglot.parse_one("SELECT FROM WHERE )(", dialect="databricks")


# ---------------------------------------------------------------------------
# #270 US-003 DEC-003 (G1) — the compiler REFUSES an ingested body the LIVE
# Databricks dialect does not accept, before the always-1 verbatim wrap. A body
# that parses under the ingest-side ``"bigquery"`` default but raises under
# ``databricks`` routes to ``_InvalidIdentifier`` → ``kept-without-evidence``.
# ---------------------------------------------------------------------------


def _make_orders_model() -> Model:
    return Model(
        unique_id="model.shop.orders",
        name="orders",
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"customer_id": Column(name="customer_id")},
        raw_code="select 1",
    )


def _make_orders_table_ref() -> TableRef:
    return TableRef(project="fake_project", dataset="dataset", name="orders")


def _make_manifest() -> Manifest:
    return Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={"model.shop.orders": _make_orders_model()},
    )


def test_compile_ingested_custom_sql_unparseable_under_databricks_returns_sentinel() -> None:
    """#270 DEC-003 (G1) per-dialect pin: a ``FOR SYSTEM_TIME AS OF`` clause
    parses under BigQuery (the ingest default) but raises under sqlglot's
    ``databricks`` dialect, so the compiler refuses it under ``DATABRICKS_DIALECT``
    → ``_InvalidIdentifier`` → kept-without-evidence, never the always-1 wrap."""
    # BigQuery parses ``FOR SYSTEM_TIME AS OF``; Databricks/Spark does not.
    body = (
        "select order_id\n"
        "from `fake_project`.`dataset`.`orders`\n"
        "for system_time as of timestamp '2024-01-01'"
    )
    result = _compile_test(
        CandidateTestCustomSQL(sql=body, from_manifest=True),
        _make_orders_table_ref(),
        DATABRICKS_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
    )
    assert isinstance(result, _InvalidIdentifier)
    assert "does not parse under the live warehouse dialect" in result.reason
    assert "databricks" in result.reason
    # Sanity: the SAME body compiles under BigQuery — it is the DIALECT refusing.
    ok = _compile_test(
        CandidateTestCustomSQL(sql=body, from_manifest=True),
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
    )
    assert ok == body
