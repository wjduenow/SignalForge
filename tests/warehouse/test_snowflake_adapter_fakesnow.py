"""Gated fakesnow validation of the SnowflakeAdapter's OWN emitted SQL (US-002).

This harness drives a **real** :class:`SnowflakeAdapter` against an in-memory
``fakesnow`` connection so the adapter's own emitted SQL is validated offline
against a Snowflake-flavoured engine. It is the adapter-level sibling of
``tests/prune/test_compiler_fakesnow.py`` (which validates the *compiler's*
emitted SQL); the two share the "parser/executor in the loop, not just snapshot
equality" lesson from #121 (``prune-engine.md`` § "Compiler is dialect-driven"):
a snapshot can pin invalid SQL byte-for-byte, so a new dialect's SQL needs a real
parser (``sqlglot``) or executor (``fakesnow``) in the loop.

What fakesnow CAN execute (so we drive the real adapter end-to-end):

* ``run_test_sql`` — the plain ``SELECT COUNT(*) AS failures FROM (<sql>) AS t``
  wrap executes against engineered rows. We assert the rule-semantic failing-row
  SHAPE (``failures >= 1`` for an engineered violation; ``== 0`` for clean data),
  NEVER ``HASH()`` value-equality.
* ``_get_num_rows`` — the ``INFORMATION_SCHEMA.TABLES.ROW_COUNT`` lookup executes
  and returns the engineered row count; the unknown-table pathway returns ``None``.

What fakesnow CANNOT execute (so we DEGRADE those sub-cases to sqlglot-parse):

* The hash-mod **sample-mode** SQL (``sample_rows`` + the ``materialise_sample``
  CTAS) uses Snowflake's variadic ``HASH(*)`` row-hash predicate. fakesnow shims
  onto DuckDB, whose ``HASH`` needs explicit args and differs from real Snowflake
  (DEC-005 of #121), so sample-mode EXECUTION is out of scope. Real ``HASH(*)``
  execution is the live harness (US-004/005). We instead capture the adapter's
  exact emitted SQL via the hand-fake and assert it PARSES under ``sqlglot``'s
  Snowflake dialect — the syntax gate that caught the #121 ``"sample"``
  reserved-word bug.
* ``CREATE TEMPORARY TABLE "<db>"."<schema>"."<name>"`` — fakesnow's DuckDB
  backend rejects a qualified temp-table name ("TEMPORARY table names can *only*
  use the 'temp' catalog"), so the CTAS is parse-only too.
* The ``capture_failures`` wrap uses ``ARRAY_AGG(OBJECT_CONSTRUCT(*))``;
  fakesnow's DuckDB has no ``OBJECT_CONSTRUCT(*)`` analogue, so it is parse-only.

Each parse-only sub-case carries an inline comment naming the fakesnow gap.

Determinism is engineered by **rule semantics, not value-equality with real
Snowflake** (``testing-signal.md`` § "Engineered determinism for LLM-driven
assertions"): a ``not_null`` over a column with one NULL row returns
``failures >= 1``; over a column with no NULLs returns ``0`` — and so on for
``unique`` / ``accepted_values`` / ``relationships``.

Gated behind ``@pytest.mark.snowflake`` (excluded from the default ``addopts``
deselection); run with ``uv run pytest -m snowflake --no-cov``.

Traces to: plans/super/124-snowflake-test-harness-docs.md US-002.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from signalforge.warehouse import SnowflakeAdapter
from signalforge.warehouse.models import TableRef
from tests.warehouse._fake_snowflake import FakeSnowflakeConnection

pytestmark = pytest.mark.snowflake

# fakesnow is a maintainer-only dev/test dependency installed for the gated
# ``snowflake`` marker run; importing it at module scope is fine because the
# marker is deselected from the default suite. ``sqlglot`` ships with fakesnow.
fakesnow = pytest.importorskip("fakesnow")

# A GCP-style project-id (6-30 chars, hyphen-permissive grammar — see
# ``validate_project_id``) is the database component; the schema / name use the
# strict identifier regex. ``fake_project`` mirrors the prune fixtures: the
# ``TableRef`` carries the lower-cased, dbt-style identifier, exactly what a real
# manifest yields and what the adapter receives.
_DB = "fake_project"
_SCHEMA = "SCH"
_TABLE = TableRef(project=_DB, dataset=_SCHEMA, name="ORDERS")

# ``SnowflakeAdapter._quote`` folds every component to UPPER then quotes, so the
# adapter's emitted SQL references ``"FAKE_PROJECT"."SCH"."ORDERS"``. Create and
# reference the fixture objects in that *folded* namespace so the execution path
# is representative of real (case-sensitive) Snowflake — NOT accidentally
# resolving via DuckDB's case-insensitive quoted-identifier handling. ``_SCHEMA``
# and the table names are already upper, so only the database component changes.
_DB_FOLDED = _DB.upper()
_SCHEMA_FOLDED = _SCHEMA.upper()

# fakesnow's DuckDB backend requires the catalog (database) + schema to exist
# before a qualified CREATE TABLE; the per-component-quoted three-part name the
# adapter emits then resolves.
_QUOTED_ORDERS = f'"{_DB_FOLDED}"."{_SCHEMA_FOLDED}"."ORDERS"'
_QUOTED_CUSTOMERS = f'"{_DB_FOLDED}"."{_SCHEMA_FOLDED}"."CUSTOMERS"'


@contextmanager
def _fakesnow_connection() -> Iterator[Any]:
    """Yield a live fakesnow connection with the database / schema created.

    The adapter is wired with ``SnowflakeAdapter(connection=<this conn>)`` so the
    real adapter executes its emitted SQL through fakesnow's DuckDB backend.
    """
    with fakesnow.patch():
        import snowflake.connector

        conn = snowflake.connector.connect()
        try:
            cur = conn.cursor()
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {_DB_FOLDED}")
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_DB_FOLDED}.{_SCHEMA_FOLDED}")
            cur.close()
            yield conn
        finally:
            conn.close()


class _RecordingConnection(FakeSnowflakeConnection):
    """A :class:`FakeSnowflakeConnection` that records every executed SQL string.

    Used for the parse-only sub-cases: the adapter runs against canned
    expectations (no real execution) and we recover the exact SQL it emitted to
    feed ``sqlglot.parse_one`` — proving the syntax is valid Snowflake without
    needing fakesnow to be able to RUN it.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.executed: list[str] = []

    def _consume_execute(self, sql: str):  # type: ignore[override]
        self.executed.append(sql)
        return super()._consume_execute(sql)


def _parse_snowflake(sql: str) -> None:
    """Assert ``sql`` parses under sqlglot's Snowflake dialect.

    Raises ``sqlglot.errors.ParseError`` on invalid Snowflake syntax — the gate
    that catches reserved-word / quoting regressions a byte-exact snapshot would
    silently pin.
    """
    import sqlglot

    parsed = sqlglot.parse_one(sql, dialect="snowflake")
    assert parsed is not None


# ===========================================================================
# EXECUTE: run_test_sql over engineered rows (plain COUNT(*) wrap — no HASH).
# Rule-semantic failing-row SHAPE only; never HASH() value-equality.
# ===========================================================================


def _create_orders(conn: Any, *, column_sql: str, values_sql: str) -> None:
    cur = conn.cursor()
    cur.execute(f"CREATE TABLE {_QUOTED_ORDERS} ({column_sql})")
    cur.execute(f"INSERT INTO {_QUOTED_ORDERS} VALUES {values_sql}")
    cur.close()


# --- not_null ---------------------------------------------------------------


def test_run_test_sql_not_null_violation_executes_to_failures() -> None:
    """A column with one NULL row → ``run_test_sql`` reports failures >= 1."""
    failing_rows = f'SELECT "CUSTOMER_ID" FROM {_QUOTED_ORDERS} WHERE "CUSTOMER_ID" IS NULL'
    with _fakesnow_connection() as conn:
        _create_orders(conn, column_sql="CUSTOMER_ID INT", values_sql="(1), (2), (NULL)")
        adapter = SnowflakeAdapter(connection=conn)
        result = adapter.run_test_sql(failing_rows)
    assert result.failure_count >= 1
    assert result.passed is False


def test_run_test_sql_not_null_clean_executes_to_zero() -> None:
    """A column with no NULLs → ``run_test_sql`` reports zero failures."""
    failing_rows = f'SELECT "CUSTOMER_ID" FROM {_QUOTED_ORDERS} WHERE "CUSTOMER_ID" IS NULL'
    with _fakesnow_connection() as conn:
        _create_orders(conn, column_sql="CUSTOMER_ID INT", values_sql="(1), (2), (3)")
        adapter = SnowflakeAdapter(connection=conn)
        result = adapter.run_test_sql(failing_rows)
    assert result.failure_count == 0
    assert result.passed is True


# --- unique -----------------------------------------------------------------


def test_run_test_sql_unique_violation_executes_to_failures() -> None:
    """A duplicate value → the dbt-shaped GROUP BY ... HAVING failing-rows SELECT
    reports failures >= 1 through the adapter's COUNT wrap."""
    failing_rows = (
        f'SELECT "CUSTOMER_ID" FROM {_QUOTED_ORDERS} '
        f'WHERE "CUSTOMER_ID" IS NOT NULL '
        f'GROUP BY "CUSTOMER_ID" HAVING COUNT(*) > 1'
    )
    with _fakesnow_connection() as conn:
        _create_orders(conn, column_sql="CUSTOMER_ID INT", values_sql="(1), (1), (2)")
        adapter = SnowflakeAdapter(connection=conn)
        result = adapter.run_test_sql(failing_rows)
    assert result.failure_count >= 1
    assert result.passed is False


def test_run_test_sql_unique_clean_with_nulls_executes_to_zero() -> None:
    """Distinct values plus multiple NULLs → zero failures (DEC-023 NULL
    exclusion: multiple NULLs do NOT violate uniqueness)."""
    failing_rows = (
        f'SELECT "CUSTOMER_ID" FROM {_QUOTED_ORDERS} '
        f'WHERE "CUSTOMER_ID" IS NOT NULL '
        f'GROUP BY "CUSTOMER_ID" HAVING COUNT(*) > 1'
    )
    with _fakesnow_connection() as conn:
        _create_orders(conn, column_sql="CUSTOMER_ID INT", values_sql="(1), (2), (NULL), (NULL)")
        adapter = SnowflakeAdapter(connection=conn)
        result = adapter.run_test_sql(failing_rows)
    assert result.failure_count == 0
    assert result.passed is True


# --- accepted_values --------------------------------------------------------


def test_run_test_sql_accepted_values_violation_executes_to_failures() -> None:
    """An out-of-set value → failures >= 1."""
    failing_rows = (
        f'SELECT "STATUS" FROM {_QUOTED_ORDERS} '
        f"WHERE \"STATUS\" IS NOT NULL AND \"STATUS\" NOT IN ('placed', 'shipped')"
    )
    with _fakesnow_connection() as conn:
        _create_orders(
            conn,
            column_sql="STATUS VARCHAR",
            values_sql="('placed'), ('shipped'), ('cancelled')",
        )
        adapter = SnowflakeAdapter(connection=conn)
        result = adapter.run_test_sql(failing_rows)
    assert result.failure_count >= 1
    assert result.passed is False


def test_run_test_sql_accepted_values_clean_executes_to_zero() -> None:
    """Only in-set values (plus a NULL, excluded by DEC-023) → zero failures."""
    failing_rows = (
        f'SELECT "STATUS" FROM {_QUOTED_ORDERS} '
        f"WHERE \"STATUS\" IS NOT NULL AND \"STATUS\" NOT IN ('placed', 'shipped')"
    )
    with _fakesnow_connection() as conn:
        _create_orders(
            conn,
            column_sql="STATUS VARCHAR",
            values_sql="('placed'), ('shipped'), (NULL)",
        )
        adapter = SnowflakeAdapter(connection=conn)
        result = adapter.run_test_sql(failing_rows)
    assert result.failure_count == 0
    assert result.passed is True


# --- relationships ----------------------------------------------------------


def _create_orders_and_customers(conn: Any, *, orders_values: str, customers_values: str) -> None:
    cur = conn.cursor()
    cur.execute(f"CREATE TABLE {_QUOTED_ORDERS} (CUSTOMER_ID INT)")
    cur.execute(f"CREATE TABLE {_QUOTED_CUSTOMERS} (ID INT)")
    cur.execute(f"INSERT INTO {_QUOTED_ORDERS} VALUES {orders_values}")
    cur.execute(f"INSERT INTO {_QUOTED_CUSTOMERS} VALUES {customers_values}")
    cur.close()


def test_run_test_sql_relationships_orphan_executes_to_failures() -> None:
    """A child FK with no matching parent → failures >= 1 (LEFT JOIN orphan)."""
    failing_rows = (
        f'SELECT child."CUSTOMER_ID" FROM {_QUOTED_ORDERS} AS child '
        f'LEFT JOIN {_QUOTED_CUSTOMERS} AS parent ON child."CUSTOMER_ID" = parent."ID" '
        f'WHERE child."CUSTOMER_ID" IS NOT NULL AND parent."ID" IS NULL'
    )
    with _fakesnow_connection() as conn:
        _create_orders_and_customers(
            conn,
            orders_values="(1), (2)",
            customers_values="(1)",  # no parent for 2
        )
        adapter = SnowflakeAdapter(connection=conn)
        result = adapter.run_test_sql(failing_rows)
    assert result.failure_count >= 1
    assert result.passed is False


def test_run_test_sql_relationships_all_matched_executes_to_zero() -> None:
    """Every non-NULL child FK matched (NULL FK excluded) → zero failures."""
    failing_rows = (
        f'SELECT child."CUSTOMER_ID" FROM {_QUOTED_ORDERS} AS child '
        f'LEFT JOIN {_QUOTED_CUSTOMERS} AS parent ON child."CUSTOMER_ID" = parent."ID" '
        f'WHERE child."CUSTOMER_ID" IS NOT NULL AND parent."ID" IS NULL'
    )
    with _fakesnow_connection() as conn:
        _create_orders_and_customers(
            conn, orders_values="(1), (2), (NULL)", customers_values="(1), (2)"
        )
        adapter = SnowflakeAdapter(connection=conn)
        result = adapter.run_test_sql(failing_rows)
    assert result.failure_count == 0
    assert result.passed is True


# ===========================================================================
# EXECUTE: _get_num_rows over INFORMATION_SCHEMA.TABLES.ROW_COUNT.
# fakesnow populates ROW_COUNT, so the real lookup executes end-to-end.
# ===========================================================================


def test_get_num_rows_executes_against_information_schema() -> None:
    """``_get_num_rows`` runs the real ``INFORMATION_SCHEMA.TABLES.ROW_COUNT``
    lookup through fakesnow and returns the engineered row count."""
    with _fakesnow_connection() as conn:
        _create_orders(conn, column_sql="CUSTOMER_ID INT", values_sql="(1), (2), (3)")
        adapter = SnowflakeAdapter(connection=conn)
        num_rows = adapter._get_num_rows(_TABLE)
    assert num_rows == 3


def test_get_num_rows_returns_none_for_absent_table() -> None:
    """No matching INFORMATION_SCHEMA row → ``None`` (the unknown-size
    pathway), executed against a real fakesnow catalog."""
    with _fakesnow_connection() as conn:
        adapter = SnowflakeAdapter(connection=conn)
        absent = TableRef(project=_DB, dataset=_SCHEMA, name="NO_SUCH_TABLE")
        num_rows = adapter._get_num_rows(absent)
    assert num_rows is None


# ===========================================================================
# PARSE-ONLY: the sample-mode / temp-table / capture SQL the adapter emits
# cannot EXECUTE under fakesnow's DuckDB backend, but MUST parse as valid
# Snowflake. We capture the exact emitted bytes via the hand-fake and feed
# them to sqlglot's Snowflake dialect — the syntax gate that caught the #121
# ``WITH sample`` reserved-word bug.
# ===========================================================================


def test_sample_rows_emitted_sql_parses_under_snowflake_dialect() -> None:
    """``sample_rows`` emits ``MOD(ABS(HASH(*)), <bucket>) < 1`` + ``ORDER BY
    ABS(HASH(*))`` — fakesnow's DuckDB ``HASH`` cannot RUN it (variadic
    ``HASH(*)``; DEC-005 of #121), so this is parse-only. The SQL MUST parse as
    valid Snowflake."""
    conn = _RecordingConnection()
    # num_rows=1000, n=100 → bucket = max(1000 // 100, 1) = 10.
    conn.expect_execute(matching=r"INFORMATION_SCHEMA", returns=[(1000,)])
    conn.expect_execute(matching=r"MOD\(ABS\(HASH", returns=[], description=[])
    adapter = SnowflakeAdapter(connection=conn)

    adapter.sample_rows(_TABLE, 100)

    sample_sql = conn.executed[1]
    assert "MOD(ABS(HASH(*)), 10) < 1" in sample_sql
    assert "ORDER BY ABS(HASH(*))" in sample_sql
    _parse_snowflake(sample_sql)


def test_materialise_ctas_emitted_sql_parses_under_snowflake_dialect() -> None:
    """The ``materialise_sample`` CTAS emits ``CREATE TEMPORARY TABLE
    "<db>"."<schema>"."_sf_sample_<run_id>" AS SELECT ... HASH(*) ...`` —
    fakesnow rejects a qualified temp-table name ("TEMPORARY table names can
    *only* use the 'temp' catalog") AND cannot run ``HASH(*)``, so this is
    parse-only. The SQL MUST parse as valid Snowflake."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=r"INFORMATION_SCHEMA", returns=[(1000,)])
    conn.expect_execute(matching=r"CREATE TEMPORARY TABLE", returns=[])
    adapter = SnowflakeAdapter(connection=conn)

    adapter.materialise_sample(_TABLE, 100)

    ctas = conn.executed[1]
    assert ctas.startswith("CREATE TEMPORARY TABLE")
    assert "MOD(ABS(HASH(*)), 10) < 1" in ctas
    _parse_snowflake(ctas)


def test_capture_failures_wrap_emitted_sql_parses_under_snowflake_dialect() -> None:
    """The ``capture_failures`` wrap emits ``ARRAY_AGG(OBJECT_CONSTRUCT(*))`` —
    fakesnow's DuckDB has no ``OBJECT_CONSTRUCT(*)`` analogue ("Scalar Function
    with name star_map does not exist"), so this is parse-only. The SQL MUST
    parse as valid Snowflake."""
    conn = _RecordingConnection()
    conn.expect_execute(
        matching=r"ARRAY_AGG\(OBJECT_CONSTRUCT\(\*\)\)",
        returns=[(0, [])],
        description=[("FAILURES",), ("SAMPLES",)],
    )
    adapter = SnowflakeAdapter(connection=conn)

    adapter.run_test_sql(
        f'SELECT "CUSTOMER_ID" FROM {_QUOTED_ORDERS} WHERE "CUSTOMER_ID" IS NULL',
        capture_failures=5,
    )

    wrap = conn.executed[0]
    assert "ARRAY_AGG(OBJECT_CONSTRUCT(*))" in wrap
    assert "LIMIT 5" in wrap
    _parse_snowflake(wrap)


def test_get_num_rows_emitted_sql_parses_under_snowflake_dialect() -> None:
    """The ``_get_num_rows`` INFORMATION_SCHEMA lookup (which DOES execute under
    fakesnow above) also parses cleanly under sqlglot's Snowflake dialect —
    belt-and-braces against a quoting/keyword regression in the size query."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=r"INFORMATION_SCHEMA", returns=[(42,)])
    adapter = SnowflakeAdapter(connection=conn)

    adapter._get_num_rows(_TABLE)

    _parse_snowflake(conn.executed[0])
