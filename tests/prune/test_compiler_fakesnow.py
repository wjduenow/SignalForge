"""Gated fakesnow parse/run validation of the compiler's Snowflake SQL (US-004).

These tests feed the compiler's *real* emitted Snowflake SQL (compiled via
``_compile_test`` with ``SNOWFLAKE_DIALECT``) into an in-memory `fakesnow`
connection against tiny engineered tables, then wrap each failing-rows SELECT
exactly as the warehouse adapter does
(``SELECT COUNT(*) AS failures FROM (<sql>) AS t`` — see
``BigQueryAdapter.run_test_sql``) and assert it parses, executes, and returns
the expected failing-row *shape*.

Determinism is engineered by **rule semantics, not value-equality with real
Snowflake** (``docs/rules/testing-signal.md`` § "End-to-end gated tests"):

* a ``not_null`` over a column with one NULL row returns ``failures >= 1``;
  over a column with no NULLs returns ``0``;
* a ``unique`` over a column with a duplicate returns ``failures >= 1``;
  over distinct values returns ``0``;
* an ``accepted_values`` with one out-of-set value returns ``failures >= 1``;
  with only in-set values returns ``0``;
* a ``relationships`` with one orphan child returns ``failures >= 1``;
  with every child matched returns ``0``.

We never assert specific ``HASH()`` values — fakesnow shims onto DuckDB and
its hash algorithm differs from real Snowflake (DEC-005). For the same reason
the *execution* tests run in **``scope="full"`` only**: the sample-mode SQL
uses Snowflake's variadic ``HASH(*)`` row-hash predicate, which fakesnow's
DuckDB backend cannot execute (its ``HASH`` needs explicit args). Real-Snowflake
sample-mode *semantics* are deferred to #124's live harness.

The sample-mode SQL *syntax* IS guarded here, though:
``test_every_snowflake_fixture_parses_under_snowflake_dialect`` parses every
fixture (full AND sample) through ``sqlglot``'s Snowflake dialect. That guard
is what caught the reserved-word bug where the sample CTE was the unquoted
``sample`` (``SAMPLE`` is reserved in Snowflake, so ``WITH sample AS`` is a
syntax error — the alias is now the quoted ``"sample"``). The byte-exact
Snowflake snapshot fixtures (US-003) remain the authoritative gate for
sample-mode SQL *shape*.

Gated behind ``@pytest.mark.snowflake`` (excluded from the default
``addopts`` deselection); run with ``uv run pytest -m snowflake --no-cov``.

Traces to: plans/super/121-prune-snowflake-dialect.md US-004 / DEC-005.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from signalforge.draft.models import (
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestUnique,
)
from signalforge.manifest.models import Column, Manifest, Model
from signalforge.prune.compiler import _compile_test
from signalforge.warehouse.models import SNOWFLAKE_DIALECT, TableRef

if TYPE_CHECKING:  # pragma: no cover - typing only
    from signalforge.draft.models import CandidateTest

pytestmark = pytest.mark.snowflake

# fakesnow is a maintainer-only dev/test dependency installed for the gated
# ``snowflake`` marker run; importing it at module scope is fine because the
# marker is deselected from the default suite.
fakesnow = pytest.importorskip("fakesnow")


# The fixtures + compiler use a three-part ``fake_project.dataset.orders``
# table ref, which Snowflake renders per-component-UPPER-quoted as
# ``"FAKE_PROJECT"."DATASET"."ORDERS"``. fakesnow's DuckDB backend requires
# the database (catalog) and schema to exist before a qualified CREATE TABLE.
_DB = "FAKE_PROJECT"
_SCHEMA = "DATASET"


def _make_orders_table_ref() -> TableRef:
    return TableRef(project="fake_project", dataset="dataset", name="orders")


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


def _make_customers_model() -> Model:
    return Model(
        unique_id="model.shop.customers",
        name="customers",
        resource_type="model",
        package_name="shop",
        original_file_path="models/customers.sql",
        path="customers.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"id": Column(name="id")},
        raw_code="select 1",
    )


def _make_manifest() -> Manifest:
    return Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={
            "model.shop.orders": _make_orders_model(),
            "model.shop.customers": _make_customers_model(),
        },
    )


def _compile_snowflake(test: CandidateTest) -> str:
    """Compile ``test`` to a Snowflake failing-rows SELECT via the real compiler.

    ``scope="full"`` (no sampling) — see the module docstring for why
    sample-mode is out of fakesnow's reach.
    """
    compiled = _compile_test(
        test,
        _make_orders_table_ref(),
        SNOWFLAKE_DIALECT,
        _make_manifest(),
        scope="full",
    )
    # The four built-ins always compile to a plain ``str`` here (the
    # ``_RequiresFutureData`` / ``_InvalidIdentifier`` sentinels only fire for
    # manifest-absent relationships parents or malformed identifiers, neither
    # of which these engineered tests exercise).
    assert isinstance(compiled, str), f"expected compiled SQL, got {compiled!r}"
    return compiled


def _wrap_count(sql: str) -> str:
    """Wrap a failing-rows SELECT exactly as ``run_test_sql`` does."""
    return f"SELECT COUNT(*) AS failures FROM ({sql}) AS t"


@contextmanager
def _snowflake_conn() -> Iterator[Any]:
    """Yield a fakesnow cursor with the ``FAKE_PROJECT.DATASET`` namespace ready."""
    with fakesnow.patch():
        import snowflake.connector

        conn = snowflake.connector.connect()
        try:
            cur = conn.cursor()
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {_DB}")
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_DB}.{_SCHEMA}")
            yield cur
            cur.close()
        finally:
            conn.close()


def _run_failures(cur: Any, failing_rows_sql: str) -> int:
    """Execute the COUNT-wrapped failing-rows SQL and return the failure count."""
    cur.execute(_wrap_count(failing_rows_sql))
    row = cur.fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# not_null
# ---------------------------------------------------------------------------


def test_not_null_violation_returns_failures() -> None:
    """A column with one NULL row yields failures >= 1 (engineered violation)."""
    sql = _compile_snowflake(CandidateTestNotNull(column="customer_id"))
    with _snowflake_conn() as cur:
        cur.execute(f"CREATE TABLE {_DB}.{_SCHEMA}.ORDERS (CUSTOMER_ID INT)")
        cur.execute(f"INSERT INTO {_DB}.{_SCHEMA}.ORDERS VALUES (1), (2), (NULL)")
        assert _run_failures(cur, sql) >= 1


def test_not_null_clean_returns_zero() -> None:
    """A column with no NULLs yields zero failures (engineered always-pass)."""
    sql = _compile_snowflake(CandidateTestNotNull(column="customer_id"))
    with _snowflake_conn() as cur:
        cur.execute(f"CREATE TABLE {_DB}.{_SCHEMA}.ORDERS (CUSTOMER_ID INT)")
        cur.execute(f"INSERT INTO {_DB}.{_SCHEMA}.ORDERS VALUES (1), (2), (3)")
        assert _run_failures(cur, sql) == 0


# ---------------------------------------------------------------------------
# unique
# ---------------------------------------------------------------------------


def test_unique_violation_returns_failures() -> None:
    """A column with a duplicate value yields failures >= 1."""
    sql = _compile_snowflake(CandidateTestUnique(column="customer_id"))
    with _snowflake_conn() as cur:
        cur.execute(f"CREATE TABLE {_DB}.{_SCHEMA}.ORDERS (CUSTOMER_ID INT)")
        cur.execute(f"INSERT INTO {_DB}.{_SCHEMA}.ORDERS VALUES (1), (1), (2)")
        assert _run_failures(cur, sql) >= 1


def test_unique_clean_returns_zero() -> None:
    """Distinct values yield zero failures.

    Also pins DEC-023 NULL-exclusion: multiple NULLs do NOT violate
    uniqueness in dbt's convention, so a NULL alongside distinct non-NULLs
    must still report zero.
    """
    sql = _compile_snowflake(CandidateTestUnique(column="customer_id"))
    with _snowflake_conn() as cur:
        cur.execute(f"CREATE TABLE {_DB}.{_SCHEMA}.ORDERS (CUSTOMER_ID INT)")
        cur.execute(f"INSERT INTO {_DB}.{_SCHEMA}.ORDERS VALUES (1), (2), (NULL), (NULL)")
        assert _run_failures(cur, sql) == 0


# ---------------------------------------------------------------------------
# accepted_values
# ---------------------------------------------------------------------------


def test_accepted_values_violation_returns_failures() -> None:
    """An out-of-set value yields failures >= 1."""
    sql = _compile_snowflake(
        CandidateTestAcceptedValues(column="customer_id", values=("placed", "shipped"))
    )
    with _snowflake_conn() as cur:
        cur.execute(f"CREATE TABLE {_DB}.{_SCHEMA}.ORDERS (CUSTOMER_ID VARCHAR)")
        cur.execute(
            f"INSERT INTO {_DB}.{_SCHEMA}.ORDERS VALUES ('placed'), ('shipped'), ('cancelled')"
        )
        assert _run_failures(cur, sql) >= 1


def test_accepted_values_clean_returns_zero() -> None:
    """Only in-set values (plus a NULL, excluded by DEC-023) yield zero failures."""
    sql = _compile_snowflake(
        CandidateTestAcceptedValues(column="customer_id", values=("placed", "shipped"))
    )
    with _snowflake_conn() as cur:
        cur.execute(f"CREATE TABLE {_DB}.{_SCHEMA}.ORDERS (CUSTOMER_ID VARCHAR)")
        cur.execute(f"INSERT INTO {_DB}.{_SCHEMA}.ORDERS VALUES ('placed'), ('shipped'), (NULL)")
        assert _run_failures(cur, sql) == 0


# ---------------------------------------------------------------------------
# relationships (multi-table LEFT JOIN orphan detection)
# ---------------------------------------------------------------------------


def test_relationships_orphan_returns_failures() -> None:
    """A child FK with no matching parent yields failures >= 1."""
    sql = _compile_snowflake(
        CandidateTestRelationships(column="customer_id", to="customers", field="id")
    )
    with _snowflake_conn() as cur:
        cur.execute(f"CREATE TABLE {_DB}.{_SCHEMA}.ORDERS (CUSTOMER_ID INT)")
        cur.execute(f"CREATE TABLE {_DB}.{_SCHEMA}.CUSTOMERS (ID INT)")
        cur.execute(f"INSERT INTO {_DB}.{_SCHEMA}.ORDERS VALUES (1), (2)")
        cur.execute(f"INSERT INTO {_DB}.{_SCHEMA}.CUSTOMERS VALUES (1)")  # no parent for 2
        assert _run_failures(cur, sql) >= 1


def test_relationships_all_matched_returns_zero() -> None:
    """Every non-NULL child FK matched (and a NULL FK, excluded) yields zero failures."""
    sql = _compile_snowflake(
        CandidateTestRelationships(column="customer_id", to="customers", field="id")
    )
    with _snowflake_conn() as cur:
        cur.execute(f"CREATE TABLE {_DB}.{_SCHEMA}.ORDERS (CUSTOMER_ID INT)")
        cur.execute(f"CREATE TABLE {_DB}.{_SCHEMA}.CUSTOMERS (ID INT)")
        cur.execute(f"INSERT INTO {_DB}.{_SCHEMA}.ORDERS VALUES (1), (2), (NULL)")
        cur.execute(f"INSERT INTO {_DB}.{_SCHEMA}.CUSTOMERS VALUES (1), (2)")
        assert _run_failures(cur, sql) == 0


# ---------------------------------------------------------------------------
# Syntax guard: every Snowflake fixture (full AND sample) must PARSE under
# sqlglot's Snowflake dialect. This is cheaper than execution and reaches the
# sample-mode fixtures that fakesnow cannot run (HASH(*)). It is the guard that
# catches reserved-word regressions like an unquoted ``WITH sample AS`` CTE.
# ---------------------------------------------------------------------------

_SNOWFLAKE_FIXTURES_DIR = (
    Path(__file__).parent.parent / "fixtures" / "prune" / "compiled_sql" / "snowflake"
)

_ALL_SNOWFLAKE_FIXTURES = [
    "not_null.sql",
    "unique.sql",
    "accepted_values.sql",
    "relationships.sql",
    "not_null_sample.sql",
    "unique_sample.sql",
    "accepted_values_sample.sql",
    "relationships_sample.sql",
    "custom_sql.sql",
    "custom_sql_sample.sql",
    "custom_sql_fullscan.sql",
]


@pytest.mark.parametrize("fixture_name", _ALL_SNOWFLAKE_FIXTURES)
def test_every_snowflake_fixture_parses_under_snowflake_dialect(fixture_name: str) -> None:
    """Every emitted Snowflake fixture must parse under sqlglot's snowflake dialect.

    sqlglot ships with fakesnow. A parse error here means the compiler emitted
    invalid Snowflake SQL — e.g. an unquoted CTE named ``sample`` (``SAMPLE`` is
    a Snowflake reserved keyword). Unlike the fakesnow execution tests above,
    this reaches the sample-mode fixtures too (no ``HASH(*)`` execution needed).
    """
    import sqlglot

    sql = (_SNOWFLAKE_FIXTURES_DIR / fixture_name).read_text(encoding="utf-8")
    # Raises sqlglot.errors.ParseError on invalid Snowflake syntax.
    parsed = sqlglot.parse_one(sql, dialect="snowflake")
    assert parsed is not None
