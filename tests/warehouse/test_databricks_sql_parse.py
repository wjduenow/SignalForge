"""Ungated ``sqlglot`` ``databricks``-dialect parse-guard over the SQL the
:class:`DatabricksAdapter` emits (#224 US-003 + US-004).

Covers ``sample_rows`` (COUNT sizing + sample SELECT), ``materialise_sample``
(COUNT sizing + ``CREATE TEMPORARY TABLE ... AS ...`` CTAS), and ``run_test_sql``
(the ``COUNT(*)`` wrap + the per-row ``to_json(struct(*))`` LIMIT capture query).

This is the adapter-side analogue of the prune-compiler parse-guard
(``tests/prune/test_compiler_databricks.py``, #223): it captures the SQL the
adapter actually executes (via a recording :class:`FakeDatabricksConnection`)
and asserts every statement parses as legal Spark/Databricks SQL under
``sqlglot``'s ``databricks`` dialect.

Why ungated (no ``@pytest.mark.databricks``):

* ``sqlglot`` is a **base runtime dependency** (``sqlglot>=30,<31`` in
  ``[project].dependencies``), so it is always importable in CI — no
  maintainer-only install is required.
* Databricks has **no offline execution fake** (the Snowflake adapter can lean
  on ``fakesnow``/DuckDB; there is no equivalent for Spark SQL), so this
  parse-guard is the *sole* automated validity gate for the adapter's emitted
  sample / count SQL until the gated live cert lands in #226. Gating it behind a
  marker CI never runs would defeat the #121 "keep a parser/executor in the
  loop" lesson (``.claude/rules/prune-engine.md`` § "Compiler is dialect-driven":
  *snapshot equality certifies shape, not validity*).

What it certifies (and what it does NOT):

* The behaviour/byte assertions in ``test_databricks_adapter.py`` certify the
  **shape** of the emitted SQL.
* This guard certifies **syntactic validity** — that those exact bytes parse as
  legal Databricks SQL (a snapshot can pin invalid SQL byte-for-byte; only a
  parser catches a mis-shaped ``Dialect`` template — a backtick slip, a malformed
  ``xxhash64(...)`` / masked ``MOD(...)`` fragment, a bad literal template).
* Real-**Spark execution semantics** (``xxhash64`` value behaviour, identifier
  case-folding against a live Unity Catalog table, ``COUNT(*)`` cost) are
  deferred to the gated live harness in #226 — ``sqlglot`` parses SQL, it does
  not run it.

Traces to: plans/super/224-databricks-sampling.md US-003 / DEC-002 / DEC-010.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
import sqlglot
from sqlglot.errors import ParseError

from signalforge.warehouse.adapters.databricks import DatabricksAdapter
from signalforge.warehouse.models import PartitionFilter, TableRef
from tests.warehouse._fake_databricks import FakeDatabricksConnection

# ``sqlglot`` is a base runtime dependency — import it directly at module scope.
# Do NOT ``pytest.importorskip`` it: that pattern is only for the gated
# marker/fakesnow deps that may be absent from the default environment.

_COUNT_QUERY = r"SELECT COUNT\(\*\)"
_SAMPLE_QUERY = r"xxhash64"


class _RecordingDatabricksConnection(FakeDatabricksConnection):
    """A :class:`FakeDatabricksConnection` that records every executed SQL."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.executed: list[str] = []

    def _consume_execute(self, sql: str):  # type: ignore[override]
        self.executed.append(sql)
        return super()._consume_execute(sql)


_CTAS_QUERY = r"CREATE TEMPORARY TABLE"
_FAILURES_QUERY = r"COUNT\(\*\) AS failures"
_CAPTURE_QUERY = r"to_json\(struct"


def _emitted_sample_sqls(
    table: TableRef,
    *,
    partition_filter: PartitionFilter | None = None,
) -> list[str]:
    """Drive ``sample_rows`` once and return the exact SQL it executed
    (the COUNT sizing query + the sample SELECT)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=[(1,)], description=[("id",)])
    adapter = DatabricksAdapter(connection=conn)
    adapter.sample_rows(table, 100, partition_filter=partition_filter)
    return conn.executed


def _emitted_materialise_sqls(
    table: TableRef,
    *,
    partition_filter: PartitionFilter | None = None,
) -> list[str]:
    """Drive ``materialise_sample`` once and return the exact SQL it executed
    (the COUNT sizing query + the ``CREATE TEMPORARY TABLE ... AS ...`` CTAS)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = DatabricksAdapter(connection=conn)
    adapter.materialise_sample(table, 100, partition_filter=partition_filter)
    return conn.executed


def _emitted_run_test_sqls(*, capture_failures: int) -> list[str]:
    """Drive ``run_test_sql`` once and return the exact SQL it executed (the
    ``COUNT(*)`` wrap, plus the per-row ``to_json(struct(*))`` LIMIT capture
    query when ``capture_failures > 0``)."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(matching=_FAILURES_QUERY, returns=[(0,)], description=[("failures",)])
    if capture_failures > 0:
        conn.expect_execute(
            matching=_CAPTURE_QUERY,
            returns=[],
            description=[("failure_row",)],
        )
    adapter = DatabricksAdapter(connection=conn)
    adapter.run_test_sql(
        "SELECT `id` FROM `main`.`sales`.`orders` WHERE `id` IS NULL",
        capture_failures=capture_failures,
    )
    return conn.executed


_STATS_QUERY = r"COUNT\(DISTINCT"
_STATS_DESCRIPTION = [
    ("non_null_count",),
    ("distinct_count",),
    ("null_count",),
    ("min_value",),
    ("max_value",),
    ("data_type",),
]


def _emitted_column_stats_sql(table: TableRef, column: str) -> str:
    """Drive ``column_stats`` once and return the single aggregate SQL it
    executed."""
    conn = _RecordingDatabricksConnection()
    conn.expect_execute(
        matching=_STATS_QUERY,
        returns=[(1, 1, 0, 5, 5, "int")],
        description=_STATS_DESCRIPTION,
    )
    adapter = DatabricksAdapter(connection=conn)
    adapter.column_stats(table, column)
    return conn.executed[0]


# Cases covering: three-part + two-part quoting; no filter + datetime/date/str
# partition filters (each exercises a distinct literal-template branch). The
# COUNT query is emitted on every case too, so both statement shapes are parsed.
_THREE_PART = TableRef(project="main", dataset="sales", name="orders")
_TWO_PART = TableRef(project=None, dataset="sales", name="orders")

_PARSE_CASES: list[tuple[str, TableRef, PartitionFilter | None]] = [
    ("three_part_no_filter", _THREE_PART, None),
    ("two_part_no_filter", _TWO_PART, None),
    (
        "datetime_filter",
        _THREE_PART,
        PartitionFilter(column="created_at", op=">=", value=datetime(2024, 1, 2, 3, 4, 5)),
    ),
    (
        "date_filter",
        _THREE_PART,
        PartitionFilter(column="dt", op="=", value=date(2024, 6, 15)),
    ),
    (
        "str_filter",
        _THREE_PART,
        PartitionFilter(column="region", op="=", value="us-east"),
    ),
]


@pytest.mark.parametrize(
    ("table", "partition_filter"),
    [(t, pf) for _id, t, pf in _PARSE_CASES],
    ids=[c[0] for c in _PARSE_CASES],
)
def test_every_emitted_statement_parses_under_databricks_dialect(
    table: TableRef,
    partition_filter: PartitionFilter | None,
) -> None:
    """Every statement the adapter emits for a sample_rows call (COUNT sizing +
    sample SELECT) must parse under sqlglot's ``databricks`` dialect.

    A ``sqlglot.errors.ParseError`` here means the adapter emitted invalid
    Spark/Databricks SQL — e.g. a mis-shaped :class:`Dialect` template (a
    backtick-quoting slip, a malformed ``xxhash64(to_json(struct(*)))`` /
    masked ``MOD(...)`` fragment, a bad ``TIMESTAMP '…'`` / ``DATE '…'`` literal
    template). This is the load-bearing validity certification for the adapter's
    emitted SQL until the gated live cert in #226.
    """
    statements = _emitted_sample_sqls(table, partition_filter=partition_filter)
    assert len(statements) == 2  # COUNT sizing query + the sample SELECT
    for sql in statements:
        parsed = sqlglot.parse_one(sql, dialect="databricks")
        assert parsed is not None


@pytest.mark.parametrize(
    ("table", "partition_filter"),
    [(t, pf) for _id, t, pf in _PARSE_CASES],
    ids=[c[0] for c in _PARSE_CASES],
)
def test_every_emitted_materialise_statement_parses_under_databricks_dialect(
    table: TableRef,
    partition_filter: PartitionFilter | None,
) -> None:
    """Every statement ``materialise_sample`` emits (COUNT sizing + the
    ``CREATE TEMPORARY TABLE <qualified temp> AS <sample body>`` CTAS) must parse
    under sqlglot's ``databricks`` dialect.

    Certifies the CTAS SHAPE only — whether Databricks *accepts* a QUALIFIED
    temporary-table name and persists the session is a **#226 live-cert item**
    (sqlglot parses SQL, it does not run it).
    """
    statements = _emitted_materialise_sqls(table, partition_filter=partition_filter)
    assert len(statements) == 2  # COUNT sizing query + the CTAS
    assert statements[1].startswith("CREATE TEMPORARY TABLE")
    for sql in statements:
        parsed = sqlglot.parse_one(sql, dialect="databricks")
        assert parsed is not None


@pytest.mark.parametrize("capture_failures", [0, 5], ids=["no_capture", "capture"])
def test_every_emitted_run_test_sql_statement_parses_under_databricks_dialect(
    capture_failures: int,
) -> None:
    """Every statement ``run_test_sql`` emits (the ``COUNT(*)`` wrap, plus the
    per-row ``to_json(struct(*))`` LIMIT capture query) must parse under
    sqlglot's ``databricks`` dialect.

    Certifies the wrap / capture SHAPE only — the per-row ``to_json`` JSON-string
    marshalling is a **#226 live-cert item**.
    """
    statements = _emitted_run_test_sqls(capture_failures=capture_failures)
    assert len(statements) == (2 if capture_failures > 0 else 1)
    assert statements[0].startswith("SELECT COUNT(*) AS failures")
    if capture_failures > 0:
        assert "to_json(struct(*))" in statements[1]
    for sql in statements:
        parsed = sqlglot.parse_one(sql, dialect="databricks")
        assert parsed is not None


def test_parse_guard_rejects_malformed_databricks_sql() -> None:
    """Planted-violation self-check: the guard CAN fail.

    Per ``.claude/rules/testing-signal.md`` (the planted-violation philosophy),
    a gate is only trustworthy if a deliberately-broken input is rejected. A
    parse-guard's "violation" is syntactically-invalid SQL — assert
    ``sqlglot.parse_one(..., dialect="databricks")`` raises ``ParseError`` on a
    clearly-malformed string, proving the real guard above would catch an adapter
    that started emitting invalid Databricks SQL.
    """
    with pytest.raises(ParseError):
        sqlglot.parse_one("SELECT FROM WHERE )(", dialect="databricks")


@pytest.mark.parametrize(
    ("table", "column"),
    [
        (_THREE_PART, "amount"),
        (_TWO_PART, "amount"),
        (_THREE_PART, "OrderAmount"),  # mixed-case → fold-to-lower backtick
    ],
    ids=["three_part", "two_part", "mixed_case_column"],
)
def test_column_stats_sql_parses_under_databricks_dialect(
    table: TableRef,
    column: str,
) -> None:
    """The single aggregate SQL ``column_stats`` emits (COUNT / COUNT DISTINCT /
    COUNT_IF / MIN / MAX / ``typeof``) must parse under sqlglot's ``databricks``
    dialect (#224 US-005).

    A ``ParseError`` here means the adapter emitted invalid Spark/Databricks
    aggregate SQL — a fold-then-quote slip or a bad ``COUNT_IF`` / ``typeof``
    fragment. Real-Spark ``typeof`` / ``MIN`` / ``MAX`` semantics are a #226
    live-cert item (sqlglot parses SQL, it does not run it).
    """
    sql = _emitted_column_stats_sql(table, column)
    parsed = sqlglot.parse_one(sql, dialect="databricks")
    assert parsed is not None
