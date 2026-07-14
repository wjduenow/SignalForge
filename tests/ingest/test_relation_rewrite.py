"""Tests for the #268 US-002 relation-locate / rewrite-verify analysis helpers.

``plan_relation_rewrite`` and ``verify_relation_rewrite`` are the sqlglot-AST seam
that lets an ingested (dbt-compiled, FOREIGN-rendered) test body be sampled: the
model's own relation is located by span so the prune compiler can splice it to a
``_SESSION._sf_sample_<hash>`` temp table — and NOTHING else is touched.

The adversarial bodies below ARE the acceptance criteria (#268 §5 US-002). Two of
them encode blockers from the architecture review that a naive implementation
passes clean:

* **AR-B1** — ``select proj.ds.tbl.c from `proj.ds.tbl` `` yields exactly ONE
  matching ``exp.Table`` *and* one dotted token run — but a span-finder that
  matches short runs would splice the **column qualifier**, leaving the ``FROM``
  on **production** while an ``ast_count == span_count`` check still passes.
* **AR-B2** — ``WITH `proj.ds.tbl` AS (…) SELECT * FROM `proj.ds.tbl` `` parses the
  CTE alias as a *single dotted ``Identifier``*, so a naive
  ``{c.alias_or_name for c in find_all(exp.CTE)}`` exclusion set misses it while
  the reference normalises to a ``Table`` tuple that exactly matches the model
  relation. Rewriting a CTE reference to the temp table plausibly yields zero rows
  → ``always-passes`` → **a real test is deleted.**

Where a test asserts a splice *result*, the splice is done HERE, back-to-front on
the identical ``str`` object that was tokenized (DEC-015) — production splicing is
the compiler's job (DEC-001: stage-0 ingest emits no SQL).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlglot

from signalforge.ingest._compiled_sql import (
    RELATION_REWRITE_REASONS,
    RewritePlan,
    plan_relation_rewrite,
    verify_relation_rewrite,
)

_ORDERS: tuple[str, str, str] = ("dev", "main", "orders")
_BQ_REL: tuple[str, str, str] = ("proj", "ds", "tbl")
_TEMP: tuple[str, str] = ("_SESSION", "_sf_sample_deadbeefdeadbeef")

_FIXTURE_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "dbt_project_expectations"
    / "target"
    / "manifest.json"
)


def _splice(sql: str, plan: RewritePlan, replacement: str) -> str:
    """Apply ``plan``'s spans back-to-front (DEC-015: ``.end`` is INCLUSIVE)."""
    out = sql
    for start, end in sorted(plan.spans, reverse=True):
        out = out[:start] + replacement + out[end + 1 :]
    return out


def _bq_temp() -> str:
    return "`{}`.`{}`".format(*_TEMP)


def _duckdb_temp() -> str:
    return '"{}"."{}"'.format(*_TEMP)


# ---------------------------------------------------------------------------
# Happy paths: every dialect's quoting normalises to the same relation tuple.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "relation", "dialect"),
    [
        pytest.param(
            "select * from `proj`.`ds`.`tbl` where x is null",
            _BQ_REL,
            "bigquery",
            id="bigquery-backtick-per-component",
        ),
        pytest.param(
            'select * from "dev"."main"."orders" where x is null',
            _ORDERS,
            "duckdb",
            id="duckdb-double-quote",
        ),
        pytest.param(
            'select * from "DB"."SCH"."ORDERS" where x is null',
            ("DB", "SCH", "ORDERS"),
            "snowflake",
            id="snowflake-double-quote",
        ),
        pytest.param(
            "select * from DB.SCH.ORDERS where x is null",
            ("db", "sch", "orders"),
            "snowflake",
            id="snowflake-unquoted-upper-folds",
        ),
        pytest.param(
            "select * from `proj`.`ds`.`tbl` where x is null",
            _BQ_REL,
            "databricks",
            id="databricks-backtick",
        ),
    ],
)
def test_plan_matches_the_relation_across_dialect_quoting(
    sql: str, relation: tuple[str, ...], dialect: str
) -> None:
    plan = plan_relation_rewrite(sql, relation=relation, dialect=dialect)

    assert plan.samplable
    assert plan.reason is None
    assert len(plan.spans) == 1
    start, end = plan.spans[0]
    # The span covers the WHOLE quoted dotted run, delimiters included.
    assert sql[start : end + 1] == sql[sql.index("from ") + 5 : sql.index(" where")]


def test_span_slice_end_is_inclusive_and_reproduces_the_token_run() -> None:
    sql = "select * from `proj`.`ds`.`tbl`"
    plan = plan_relation_rewrite(sql, relation=_BQ_REL, dialect="bigquery")

    assert plan.samplable
    start, end = plan.spans[0]
    assert sql[start : end + 1] == "`proj`.`ds`.`tbl`"
    # Off-by-one guard: an EXCLUSIVE read would drop the trailing backtick.
    assert sql[start:end] != "`proj`.`ds`.`tbl`"


# ---------------------------------------------------------------------------
# AR-B1 — the column qualifier must NOT be spliced; the FROM must be.
# ---------------------------------------------------------------------------


def test_ar_b1_column_qualifier_is_not_spliced_but_the_from_is() -> None:
    sql = "select proj.ds.tbl.c from `proj`.`ds`.`tbl`"

    plan = plan_relation_rewrite(sql, relation=_BQ_REL, dialect="bigquery")

    assert plan.samplable, plan.reason
    assert len(plan.spans) == 1
    start, _end = plan.spans[0]
    # The single span is the FROM clause's relation, NOT the leading column path.
    assert start == sql.index("`proj`")

    rewritten = _splice(sql, plan, _bq_temp())
    assert rewritten == ("select proj.ds.tbl.c from `_SESSION`.`_sf_sample_deadbeefdeadbeef`")
    # The column qualifier survived byte-intact.
    assert "select proj.ds.tbl.c from" in rewritten


# ---------------------------------------------------------------------------
# AR-B2 / DEC-005 — CTE alias collisions are refused, never rewritten.
# ---------------------------------------------------------------------------


def test_ar_b2_dotted_cte_alias_colliding_with_the_relation_is_refused() -> None:
    sql = "WITH `proj.ds.tbl` AS (select 1 as x) SELECT * FROM `proj.ds.tbl`"

    plan = plan_relation_rewrite(sql, relation=_BQ_REL, dialect="bigquery")

    assert not plan.samplable
    assert plan.reason == "cte-alias-collision"
    assert plan.spans == ()


def test_cte_alias_shadowing_the_model_name_is_refused() -> None:
    sql = "WITH orders AS (select 1 as x) SELECT * FROM orders"

    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")

    assert not plan.samplable
    assert plan.reason == "cte-alias-collision"


def test_cte_alias_collision_is_case_insensitive() -> None:
    sql = "WITH ORDERS AS (select 1 as x) SELECT * FROM ORDERS"

    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="bigquery")

    assert not plan.samplable
    assert plan.reason == "cte-alias-collision"


def test_non_colliding_cte_reference_does_not_consume_a_span() -> None:
    """A dbt-expectations-shaped body: the CTE ref is not a physical relation."""
    sql = (
        'with grouped_expression as (select amount from "dev"."main"."orders")\n'
        "select * from grouped_expression where amount is null"
    )

    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")

    assert plan.samplable, plan.reason
    assert len(plan.spans) == 1

    rewritten = _splice(sql, plan, _duckdb_temp())
    assert "grouped_expression" in rewritten
    assert '"dev"."main"."orders"' not in rewritten
    assert verify_relation_rewrite(
        rewritten, source=_ORDERS, temp=_TEMP, expected_n=1, dialect="duckdb"
    )


# ---------------------------------------------------------------------------
# DEC-006 — single physical relation enforced on the AST, never a \bjoin\b regex.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        pytest.param(
            'select * from "dev"."main"."orders" o join "dev"."main"."customers" c on o.cid = c.id',
            "duckdb",
            id="explicit-join",
        ),
        pytest.param(
            'select * from "dev"."main"."orders" o, "dev"."main"."customers" c where o.cid = c.id',
            "duckdb",
            id="comma-join-missed-by-a-join-regex",
        ),
        pytest.param(
            'select * from "dev"."main"."orders" o where o.cid in '
            '(select c.id from "dev"."main"."customers" c where c.x = o.x)',
            "duckdb",
            id="correlated-subquery-missed-by-a-join-regex",
        ),
        pytest.param(
            'select * from "dev"."main"."orders" o where not exists '
            '(select 1 from "dev"."main"."customers" c where c.id = o.cid)',
            "duckdb",
            id="not-exists-missed-by-a-join-regex",
        ),
    ],
)
def test_multi_relation_bodies_are_refused(sql: str, dialect: str) -> None:
    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect=dialect)

    assert not plan.samplable
    assert plan.reason == "multi-relation"
    assert plan.spans == ()


def test_a_join_regex_would_have_missed_these_bodies() -> None:
    """Planted self-check: the DEC-006 bodies defeat prune's `\\bjoin\\b` heuristic."""
    import re

    join_re = re.compile(r"\bjoin\b", re.IGNORECASE)
    comma_join = (
        'select * from "dev"."main"."orders" o, "dev"."main"."customers" c where o.cid = c.id'
    )
    assert join_re.search(comma_join) is None
    # ...yet the AST gate refuses it.
    assert not plan_relation_rewrite(comma_join, relation=_ORDERS, dialect="duckdb").samplable


# ---------------------------------------------------------------------------
# Self-join and multi-occurrence bodies.
# ---------------------------------------------------------------------------


def test_self_join_yields_two_spans_both_rewritten_to_the_same_temp() -> None:
    sql = (
        'select a.id from "dev"."main"."orders" a '
        'join "dev"."main"."orders" b on a.id = b.parent_id where a.x is null'
    )

    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")

    assert plan.samplable, plan.reason
    assert len(plan.spans) == 2

    rewritten = _splice(sql, plan, _duckdb_temp())
    assert rewritten.count(_duckdb_temp()) == 2
    assert '"dev"."main"."orders"' not in rewritten
    assert verify_relation_rewrite(
        rewritten, source=_ORDERS, temp=_TEMP, expected_n=2, dialect="duckdb"
    )


def test_partial_rewrite_of_a_self_join_fails_verification() -> None:
    """DEC-004: a leftover PRODUCTION reference must be caught on the AST."""
    sql = (
        'select a.id from "dev"."main"."orders" a '
        'join "dev"."main"."orders" b on a.id = b.parent_id'
    )
    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")
    assert len(plan.spans) == 2

    # Hand-corrupt: splice only the LAST span (back-to-front, one step).
    start, end = sorted(plan.spans)[-1]
    partial = sql[:start] + _duckdb_temp() + sql[end + 1 :]
    assert '"dev"."main"."orders"' in partial  # the residual prod reference

    assert not verify_relation_rewrite(
        partial, source=_ORDERS, temp=_TEMP, expected_n=2, dialect="duckdb"
    )


def test_function_call_and_dot_suffixed_result_never_yield_a_span() -> None:
    """A ``f(x).y`` struct access exercises BOTH run guards.

    ``f`` is a name token followed by ``(`` (a function / TVF call, never a
    relation) and ``y`` is a name token *preceded* by a ``.`` that no run head
    consumed. Neither may be mistaken for a relation component.
    """
    sql = "select f(x).y, * from `proj`.`ds`.`tbl`"

    plan = plan_relation_rewrite(sql, relation=_BQ_REL, dialect="bigquery")

    assert plan.samplable, plan.reason
    assert len(plan.spans) == 1
    start, _end = plan.spans[0]
    assert start == sql.index("`proj`")


def test_same_arity_dotted_path_that_is_not_the_relation_consumes_no_span() -> None:
    """A 3-part struct path (``o.addr.zip``) has the relation's arity but not its
    identity — it must normalise-mismatch and be skipped, not spliced."""
    sql = 'select o.addr.zip from "dev"."main"."orders" o where o.x is null'

    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")

    assert plan.samplable, plan.reason
    assert len(plan.spans) == 1

    rewritten = _splice(sql, plan, _duckdb_temp())
    assert rewritten.startswith("select o.addr.zip from ")
    assert verify_relation_rewrite(
        rewritten, source=_ORDERS, temp=_TEMP, expected_n=1, dialect="duckdb"
    )


def test_span_ast_disagreement_is_refused_as_span_mismatch() -> None:
    """A column path of the SAME arity as the relation → refuse, never guess.

    With a 2-part relation, ``select main.orders from "main"."orders"`` yields
    TWO maximal 2-part runs (the column path and the FROM) but only ONE
    ``exp.Table``. The tokenizer and the parser disagree about how many times the
    relation appears — fail closed rather than splice the wrong one.
    """
    sql = 'select main.orders from "main"."orders"'

    plan = plan_relation_rewrite(sql, relation=("main", "orders"), dialect="duckdb")

    assert not plan.samplable
    assert plan.reason == "span-mismatch"
    assert plan.spans == ()


@pytest.mark.parametrize(
    ("source", "temp", "expected_n"),
    [
        pytest.param((), _TEMP, 1, id="empty-source"),
        pytest.param(_ORDERS, (), 1, id="empty-temp"),
        pytest.param(("a", "b", "c", "d"), _TEMP, 1, id="over-long-source"),
        pytest.param(_ORDERS, _TEMP, 0, id="non-positive-expected-n"),
    ],
)
def test_verify_rejects_a_malformed_request(
    source: tuple[str, ...], temp: tuple[str, ...], expected_n: int
) -> None:
    assert not verify_relation_rewrite(
        f"select * from {_duckdb_temp()}",
        source=source,
        temp=temp,
        expected_n=expected_n,
        dialect="duckdb",
    )


def test_three_occurrences_cte_subquery_and_main_all_rewrite() -> None:
    sql = (
        'with c as (select id from "dev"."main"."orders")\n'
        'select o.id from "dev"."main"."orders" o\n'
        'where o.id in (select s.id from "dev"."main"."orders" s where s.x is null)\n'
        "  and o.id in (select id from c)"
    )

    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")

    assert plan.samplable, plan.reason
    assert len(plan.spans) == 3

    rewritten = _splice(sql, plan, _duckdb_temp())
    assert rewritten.count(_duckdb_temp()) == 3
    assert verify_relation_rewrite(
        rewritten, source=_ORDERS, temp=_TEMP, expected_n=3, dialect="duckdb"
    )


# ---------------------------------------------------------------------------
# Refusals: zero-match, homoglyph/case drift, unparseable.
# ---------------------------------------------------------------------------


def test_zero_match_when_the_body_only_references_a_source() -> None:
    sql = 'select * from "dev"."raw"."stripe_payments" where amount is null'

    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")

    assert not plan.samplable
    # The only physical relation is not the model's → nothing to rewrite.
    assert plan.reason == "zero-match"
    assert plan.spans == ()


def test_single_backtick_dotted_relation_is_refused_not_mis_spliced() -> None:
    """A single-backtick DOTTED relation (`` `proj.ds.tbl` ``) parses as a
    3-part table but tokenizes as ONE arity-1 identifier, so no arity-3 run is
    found → ``zero-match``. dbt-bigquery emits the per-component form, so this
    is uncommon, but it must fail closed (→ source bypass) rather than guess an
    offset. (Pins the reachability of the ``if not spans`` arm.)
    """
    plan = plan_relation_rewrite(
        "select c from `proj.ds.tbl`", relation=_BQ_REL, dialect="bigquery"
    )

    assert not plan.samplable
    assert plan.reason == "zero-match"
    assert plan.spans == ()


def test_case_variant_relation_fails_the_exact_match_on_a_case_sensitive_dialect() -> None:
    sql = "select * from `PROJ`.`DS`.`TBL`"

    plan = plan_relation_rewrite(sql, relation=_BQ_REL, dialect="bigquery")

    assert not plan.samplable
    assert plan.reason == "zero-match"


def test_homoglyph_relation_fails_the_exact_match() -> None:
    # Cyrillic 'о' (U+043E) in place of ASCII 'o' in "orders".
    sql = 'select * from "dev"."main"."оrders"'

    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")

    assert not plan.samplable
    assert plan.reason == "zero-match"


def test_suffix_match_is_never_accepted() -> None:
    """A bare `orders` must NOT match the 3-part model relation."""
    sql = "select * from orders where x is null"

    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")

    assert not plan.samplable
    assert plan.reason == "zero-match"


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("this is not sql at all ((((", id="garbage"),
        pytest.param("select * from", id="truncated"),
    ],
)
def test_unparseable_body_is_refused_without_raising(sql: str) -> None:
    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")

    assert not plan.samplable
    assert plan.reason in RELATION_REWRITE_REASONS


def test_deeply_nested_body_does_not_leak_a_recursion_error() -> None:
    sql = "select * from t where " + "(" * 2000 + "1=1" + ")" * 2000

    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")

    assert not plan.samplable
    assert plan.reason in RELATION_REWRITE_REASONS


def test_unknown_dialect_is_refused_without_raising() -> None:
    plan = plan_relation_rewrite(
        'select * from "dev"."main"."orders"', relation=_ORDERS, dialect="nope"
    )

    assert not plan.samplable
    assert plan.reason == "unparseable"


def test_empty_relation_is_refused() -> None:
    plan = plan_relation_rewrite(
        'select * from "dev"."main"."orders"', relation=(), dialect="duckdb"
    )

    assert not plan.samplable
    assert plan.reason == "zero-match"


# ---------------------------------------------------------------------------
# Comments, string literals, multibyte — the tokenizer never hands us a span
# inside a comment or a literal, and multibyte chars do not shift the offsets.
# ---------------------------------------------------------------------------


def test_comment_and_string_literal_text_that_looks_like_the_relation_is_untouched() -> None:
    sql = (
        '-- "dev"."main"."orders"\n'
        '/* "dev"."main"."orders" */\n'
        'select * from "dev"."main"."orders"\n'
        'where label = \'dev.main.orders\' and note = \'"dev"."main"."orders"\''
    )

    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")

    assert plan.samplable, plan.reason
    assert len(plan.spans) == 1

    rewritten = _splice(sql, plan, _duckdb_temp())
    # Comment + literal text survived byte-intact; only the FROM moved.
    assert rewritten.startswith('-- "dev"."main"."orders"\n/* "dev"."main"."orders" */')
    assert "'dev.main.orders'" in rewritten
    assert '\'"dev"."main"."orders"\'' in rewritten
    assert rewritten.count(_duckdb_temp()) == 1
    assert verify_relation_rewrite(
        rewritten, source=_ORDERS, temp=_TEMP, expected_n=1, dialect="duckdb"
    )


def test_multibyte_literal_before_the_relation_does_not_shift_spans() -> None:
    sql = "select * from `proj`.`ds`.`tbl` where s = 'héllo—wörld — ✓'"
    # A byte-offset (rather than character-offset) implementation would slice wrong.
    prefixed = "select 'héllo—x—✓' as k, * from `proj`.`ds`.`tbl`"

    for body in (sql, prefixed):
        plan = plan_relation_rewrite(body, relation=_BQ_REL, dialect="bigquery")
        assert plan.samplable, plan.reason
        start, end = plan.spans[0]
        assert body[start : end + 1] == "`proj`.`ds`.`tbl`"


# ---------------------------------------------------------------------------
# verify_relation_rewrite — the DEC-004 post-condition.
# ---------------------------------------------------------------------------


def test_verify_accepts_a_clean_full_rewrite() -> None:
    rewritten = f"select * from {_duckdb_temp()} where x is null"

    assert verify_relation_rewrite(
        rewritten, source=_ORDERS, temp=_TEMP, expected_n=1, dialect="duckdb"
    )


def test_verify_rejects_a_residual_source_reference() -> None:
    rewritten = f'select * from {_duckdb_temp()} a join "dev"."main"."orders" b on a.id = b.id'

    assert not verify_relation_rewrite(
        rewritten, source=_ORDERS, temp=_TEMP, expected_n=1, dialect="duckdb"
    )


def test_verify_rejects_the_wrong_temp_count() -> None:
    rewritten = f"select * from {_duckdb_temp()} where x is null"

    assert not verify_relation_rewrite(
        rewritten, source=_ORDERS, temp=_TEMP, expected_n=2, dialect="duckdb"
    )


def test_verify_rejects_unparseable_rewritten_sql() -> None:
    assert not verify_relation_rewrite(
        "select * from ((((", source=_ORDERS, temp=_TEMP, expected_n=1, dialect="duckdb"
    )


def test_verify_rejects_an_ar_b1_shaped_column_only_rewrite() -> None:
    """The exact AR-B1 defeat: the column path moved, the FROM stayed on prod."""
    corrupted = "select `_SESSION`.`_sf_sample_deadbeefdeadbeef`.c from `proj`.`ds`.`tbl`"

    assert not verify_relation_rewrite(
        corrupted, source=_BQ_REL, temp=_TEMP, expected_n=1, dialect="bigquery"
    )


def test_verify_rejects_an_unknown_dialect() -> None:
    assert not verify_relation_rewrite(
        'select * from "a"."b"', source=_ORDERS, temp=_TEMP, expected_n=1, dialect="nope"
    )


# ---------------------------------------------------------------------------
# The REAL fixture bodies (DuckDB-quoted, dbt-expectations-rendered).
# ---------------------------------------------------------------------------


def _fixture_compiled_bodies() -> list[tuple[str, str]]:
    manifest = json.loads(_FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for unique_id, node in manifest["nodes"].items():
        if node.get("resource_type") != "test":
            continue
        code = node.get("compiled_code")
        if isinstance(code, str) and code.strip():
            out.append((unique_id, code))
    return out


def test_fixture_manifest_carries_the_expected_compiled_test_bodies() -> None:
    """Guard the parametrize below against a vacuous empty-glob pass."""
    assert len(_fixture_compiled_bodies()) >= 5


@pytest.mark.parametrize(
    ("unique_id", "sql"),
    [pytest.param(uid, sql, id=uid.split(".")[-1][:40]) for uid, sql in _fixture_compiled_bodies()],
)
def test_real_dbt_expectations_bodies_rewrite_and_verify(unique_id: str, sql: str) -> None:
    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")

    assert plan.samplable, f"{unique_id}: {plan.reason}"
    assert len(plan.spans) >= 1

    rewritten = _splice(sql, plan, _duckdb_temp())

    # dbt's comments, indentation and blank lines survive outside the spans.
    assert '"dev"."main"."orders"' not in rewritten
    assert rewritten.count(_duckdb_temp()) == len(plan.spans)
    assert verify_relation_rewrite(
        rewritten,
        source=_ORDERS,
        temp=_TEMP,
        expected_n=len(plan.spans),
        dialect="duckdb",
    )
    # The rewritten body is still valid SQL.
    assert sqlglot.parse_one(rewritten, dialect="duckdb") is not None


def test_real_body_preserves_dbt_bytes_outside_the_spans() -> None:
    bodies = dict(_fixture_compiled_bodies())
    sql = next(v for k, v in bodies.items() if "expect_column_values_to_not_be_null" in k)
    plan = plan_relation_rewrite(sql, relation=_ORDERS, dialect="duckdb")
    assert plan.samplable

    rewritten = _splice(sql, plan, _duckdb_temp())
    start, end = plan.spans[0]
    assert rewritten[:start] == sql[:start]
    assert rewritten[start + len(_duckdb_temp()) :] == sql[end + 1 :]
