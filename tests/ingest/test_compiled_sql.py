"""Tests for :mod:`signalforge.ingest._compiled_sql` (#154 US-002).

Three pure sqlglot-AST analysis helpers over a compiled-SQL string. Each test
is capable of failing on a real regression (testing-signal.md § "No assert
True"); each helper carries a planted-violation-style NEGATIVE test proving it
does NOT false-positive on the tokens-as-column-names / tokens-inside-literals
cases the AST approach exists to defend (#154 AR rows 9/10/11).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.ingest._compiled_sql import (
    is_deterministic_sql,
    is_prunable_count_scalar,
    is_row_returning,
    parses_under_dialect,
    strip_sql_comments,
    validate_ingested_sql,
)
from signalforge.warehouse.errors import QuerySyntaxError

# --------------------------------------------------------------------------- #
# is_deterministic_sql
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t TABLESAMPLE SYSTEM (10 PERCENT)",
        "SELECT * FROM t WHERE r < RAND()",
        "SELECT * FROM t WHERE r < RANDOM()",
        "SELECT * FROM t WHERE r < RND()",
        "SELECT * FROM t WHERE ts > CURRENT_TIMESTAMP",
        "SELECT * FROM t WHERE ts > CURRENT_TIMESTAMP()",
        "SELECT * FROM t WHERE d > CURRENT_DATE",
        "SELECT * FROM t WHERE ts > NOW()",
        "SELECT * FROM t WHERE ts > GETDATE()",
        "SELECT UUID() AS id FROM t",
        "SELECT GENERATE_UUID() AS id FROM t",
        "SELECT * FROM t ORDER BY RAND() LIMIT 100",
    ],
)
def test_is_deterministic_sql_flags_nondeterministic(sql: str) -> None:
    """A non-deterministic construct anywhere in the tree flags the body."""
    assert is_deterministic_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a, b FROM t WHERE a <> b",
        "SELECT COUNT(*) AS c FROM t",
        "SELECT DATE(order_ts) AS d FROM t WHERE amount > 0",
    ],
)
def test_is_deterministic_sql_accepts_deterministic(sql: str) -> None:
    assert is_deterministic_sql(sql) is True


def test_is_deterministic_sql_no_false_positive_on_column_name() -> None:
    """A column literally named after a non-deterministic function is an
    ``exp.Column``, not a function node — it must NOT flag (the whole point of
    AST inspection over regex/substring)."""
    assert is_deterministic_sql("SELECT random_id FROM t WHERE a <> b") is True
    assert is_deterministic_sql("SELECT current_timestamp_col FROM t") is True
    assert is_deterministic_sql("SELECT uuid_column, rand_score FROM t") is True


def test_is_deterministic_sql_no_false_positive_on_string_literal() -> None:
    """Non-deterministic tokens inside a string literal (an ``exp.Literal``) must
    NOT flag."""
    assert is_deterministic_sql("SELECT 'RAND' AS label FROM t") is True
    assert is_deterministic_sql("SELECT a FROM t WHERE note = 'run NOW() later'") is True


def test_is_deterministic_sql_no_false_positive_in_comment() -> None:
    """A non-deterministic token inside a comment is not a function call (sqlglot
    attaches comments as metadata, never as nodes)."""
    assert is_deterministic_sql("SELECT a FROM t -- uses RAND() someday\nWHERE a <> b") is True


def test_is_deterministic_sql_unparseable_returns_true() -> None:
    """Skip-when-uncertain: an unparseable body affords no positive
    non-determinism claim, so it is treated as deterministic."""
    assert is_deterministic_sql("SELECT FROM WHERE ((( random garbage") is True


# --------------------------------------------------------------------------- #
# is_row_returning
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a, b FROM t WHERE a <> b",
        "SELECT * FROM (SELECT COUNT(*) AS c FROM t) x WHERE c > 5",
        "SELECT COUNT(*) OVER () AS c FROM t",
        "SELECT (SELECT COUNT(*) FROM t2) AS c FROM t1",
        "SELECT COUNT(*) AS c FROM t GROUP BY y",
        "SELECT 1 AS one FROM t",
    ],
)
def test_is_row_returning_true_for_failing_rows(sql: str) -> None:
    """A genuine failing-rows / non-scalar body is row-returning."""
    assert is_row_returning(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM t",
        "SELECT COUNT(*), MAX(x) FROM t",
        "SELECT AVG(amount) AS a FROM t",
        "SELECT COUNT(*) + 1 AS c FROM t",
    ],
)
def test_is_row_returning_false_for_scalar_aggregate(sql: str) -> None:
    """An all-aggregate projection with no GROUP BY collapses to one row →
    scalar → NOT row-returning."""
    assert is_row_returning(sql) is False


def test_is_row_returning_windowed_aggregate_is_not_scalar() -> None:
    """PLANTED NEGATIVE: a windowed aggregate does NOT collapse the query — a
    flat ``find_all(AggFunc)`` would wrongly call this scalar."""
    assert is_row_returning("SELECT id, COUNT(*) OVER () AS n FROM t") is True


def test_is_row_returning_subquery_aggregate_is_not_top_level_scalar() -> None:
    """PLANTED NEGATIVE: an aggregate confined to a subquery whose outer
    projection is a plain column is row-returning, not scalar."""
    assert is_row_returning("SELECT x, (SELECT MAX(y) FROM t2) AS m FROM t1") is True


def test_is_row_returning_unparseable_returns_true() -> None:
    assert is_row_returning("SELECT FROM WHERE ((( garbage") is True


# --------------------------------------------------------------------------- #
# is_row_returning — CTE / derived-table one-row classifier (#270 US-002 / DEC-002)
# --------------------------------------------------------------------------- #

# A scalar count/aggregate re-projected through a chain of single-source
# pass-through scopes is cardinality-1 → scalar → NOT row-returning (must FIRE:
# ``is_row_returning`` returns ``False``, so the ingest bridge skip-records it).
_CTE_ONE_ROW_FIRE: list[tuple[str, str]] = [
    ("WITH c AS (SELECT COUNT(*) AS n FROM t) SELECT n FROM c", "bigquery"),
    ("SELECT n FROM (SELECT COUNT(*) AS n FROM t) x", "bigquery"),
    # SELECT * from an aggregate CTE with no outer filter is still one row.
    ("WITH c AS (SELECT COUNT(*) AS n FROM t) SELECT * FROM c", "bigquery"),
    ("WITH a AS (SELECT COUNT(*) n FROM t), b AS (SELECT n FROM a) SELECT n FROM b", "bigquery"),
    ("SELECT n FROM (SELECT n FROM (SELECT COUNT(*) n FROM t) y) x", "bigquery"),
    # SUM is a collapsing aggregate too — any all-collapse base qualifies.
    ("WITH c AS (SELECT SUM(x) AS s FROM t) SELECT s FROM c", "bigquery"),
    ("SELECT x.n AS m FROM (SELECT COUNT(*) AS n FROM t) x", "bigquery"),
    # WHERE on the AGGREGATE (base) node is fine — COUNT over zero rows is one row.
    ("WITH c AS (SELECT COUNT(*) n FROM t WHERE amount>1000) SELECT n FROM c", "bigquery"),
    # A constant projection over a one-row source is still one row.
    ("WITH c AS (SELECT COUNT(*) n FROM t) SELECT 1 AS x FROM c", "bigquery"),
    # Dialect-quoted variants must resolve identically (scope-based, not textual).
    ('WITH c AS (SELECT COUNT(*) AS "N" FROM "T") SELECT "N" FROM c', "snowflake"),
    ("WITH c AS (SELECT COUNT(*) AS `n` FROM `t`) SELECT `n` FROM c", "databricks"),
]


@pytest.mark.parametrize(("sql", "dialect"), _CTE_ONE_ROW_FIRE)
def test_is_row_returning_fires_on_cte_reprojected_one_row(sql: str, dialect: str) -> None:
    """A count re-projected through a CTE / derived table is scalar (one row)."""
    assert is_row_returning(sql, dialect=dialect) is False


# The tripwires: bodies that LOOK CTE-shaped but are NOT provably one-row. A
# false fire here skip-records a legitimately row-returning body — and for the
# ``validation_errors`` family, that would destroy the ENTIRE dbt-expectations
# macro feature (its 0-or-1-row shell IS legitimately row-returning: 0 = pass).
_CTE_ONE_ROW_NO_FIRE: list[tuple[str, str]] = [
    # Outer WHERE filters the re-projected aggregate → 0-or-1 rows → row-returning.
    ("WITH c AS (SELECT COUNT(*) n FROM t) SELECT n FROM c WHERE n>5", "bigquery"),
    # JOIN / comma-join can multiply the single row.
    ("WITH c AS (SELECT COUNT(*) n FROM t) SELECT c.n FROM c CROSS JOIN big", "bigquery"),
    ("WITH c AS (SELECT COUNT(*) n FROM t) SELECT a.n FROM c a JOIN c b ON a.n=b.n", "bigquery"),
    ("WITH c AS (SELECT COUNT(*) n FROM t) SELECT c.n FROM c, big", "bigquery"),
    # GROUP BY in the CTE → many rows.
    ("WITH c AS (SELECT k, COUNT(*) n FROM t GROUP BY k) SELECT n FROM c", "bigquery"),
    # Pass-through of a PHYSICAL table (no aggregate anywhere) → many rows.
    ("WITH c AS (SELECT id FROM t) SELECT id FROM c", "bigquery"),
    # A set-op derived table is not a single one-row SELECT.
    (
        "SELECT n FROM ((SELECT COUNT(*) n FROM t) UNION ALL (SELECT COUNT(*) n FROM t2)) x",
        "bigquery",
    ),
    # Pass-through reducers all bail (LIMIT / OFFSET / DISTINCT / QUALIFY).
    ("WITH c AS (SELECT COUNT(*) n FROM t) SELECT n FROM c LIMIT 1", "bigquery"),
    ("WITH c AS (SELECT COUNT(*) n FROM t) SELECT n FROM c OFFSET 1", "bigquery"),
    ("WITH c AS (SELECT COUNT(*) n FROM t) SELECT DISTINCT n FROM c", "bigquery"),
    (
        "WITH c AS (SELECT COUNT(*) n FROM t) SELECT n FROM c QUALIFY row_number() over()=1",
        "bigquery",
    ),
    # HAVING on the base aggregate disqualifies the base case.
    ("WITH c AS (SELECT COUNT(*) n FROM t HAVING COUNT(*)>0) SELECT n FROM c", "bigquery"),
    # A pass-through hop with NO FROM is not a provable single-relation one-row shape.
    ("SELECT 1", "bigquery"),
    # A FROM source that is not a Table/Subquery (UNNEST / TVF / VALUES) → bail.
    ("SELECT x FROM UNNEST([1,2,3]) AS x", "bigquery"),
    # A pass-through source carrying TABLESAMPLE / PIVOT re-cardinalises → bail.
    (
        "SELECT n FROM (SELECT COUNT(*) n FROM t) x TABLESAMPLE SYSTEM (10 PERCENT)",
        "bigquery",
    ),
    # A row-GENERATING projection (explode) over a scalar-aggregate source fans
    # the single row into many → bail (Spark/Databricks).
    ("SELECT explode(arr) AS e FROM (SELECT COUNT(*) n FROM t) x", "spark"),
    # A LATERAL VIEW multiplies the source's single row → bail.
    (
        "SELECT n FROM (SELECT COUNT(*) n FROM t) x LATERAL VIEW explode(arr) v AS col",
        "spark",
    ),
    # Existing non-regressions — the new branch must not disturb them.
    ("SELECT id, COUNT(*) OVER () AS n FROM t", "bigquery"),
    ("SELECT x, (SELECT MAX(y) FROM t2) AS m FROM t1", "bigquery"),
]


@pytest.mark.parametrize(("sql", "dialect"), _CTE_ONE_ROW_NO_FIRE)
def test_is_row_returning_does_not_fire_on_filtered_or_multi_source(sql: str, dialect: str) -> None:
    """The classifier bails to row-returning on any filter / multiply / set-op."""
    assert is_row_returning(sql, dialect=dialect) is True


# The dbt-expectations ``validation_errors`` shell is the #1 false-positive
# risk: firing on it skip-records the whole macro family. It is 0-or-1 rows and
# LEGITIMATELY row-returning; the distinguishing feature is the outer WHERE
# ``not(expression = true)`` filter (#270 AR-A). Pull the REAL committed bodies.
_INGESTED_FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "prune" / "compiled_sql" / "ingested"
)


@pytest.mark.parametrize(
    "fixture",
    [
        "bigquery_dbt_expectations_not_null.in.sql",
        "bigquery_dbt_expectations_between.in.sql",
        "bigquery_dbt_expectations_row_count_between.in.sql",
    ],
)
def test_is_row_returning_leaves_validation_errors_shell_row_returning(fixture: str) -> None:
    """CRITICAL: the real dbt-expectations ``validation_errors`` shells (whose
    outer ``SELECT * … WHERE not(expression=true)`` makes them 0-or-1 rows) MUST
    stay row-returning — a false fire destroys the entire macro family."""
    body = (_INGESTED_FIXTURE_DIR / fixture).read_text()
    assert is_row_returning(body) is True


def test_is_row_returning_leaves_max_recency_shell_row_returning() -> None:
    """The dbt-expectations ``max_recency`` shape has an OUTER WHERE filtering the
    re-projected aggregate to 0-or-1 rows → legitimately row-returning."""
    body = (
        "with recency as (select max(loaded_at) as most_recent from t) "
        'select * from recency where most_recent < timestamp("2020-01-01")'
    )
    assert is_row_returning(body) is True


def test_is_row_returning_cte_count_uses_from_underscore_arg_key() -> None:
    """Regression: the pinned sqlglot stores the FROM clause under ``"from_"`` (not
    ``"from"``). A CTE-count firing proves the ``from_`` lookup is live — the whole
    classifier silently dies if it hard-codes ``"from"`` (which returns ``None``)."""
    import sqlglot

    tree = sqlglot.parse_one("WITH c AS (SELECT COUNT(*) n FROM t) SELECT n FROM c")
    # The pinned sqlglot key: hard-coding "from" would disable the rule.
    assert tree.args.get("from") is None
    assert tree.args.get("from_") is not None
    # ...and the classifier still fires because it falls through to "from_".
    assert is_row_returning("WITH c AS (SELECT COUNT(*) n FROM t) SELECT n FROM c") is False


def test_is_row_returning_depth_cap_reads_as_row_returning() -> None:
    """A pass-through chain deeper than the depth cap (32) is unproven → the
    conservative row-returning verdict, never an unbounded recursion."""
    body = "SELECT COUNT(*) n FROM t"
    for i in range(34):
        body = f"SELECT n FROM ({body}) a{i}"
    assert is_row_returning(body) is True
    # A shallow (5-deep) chain of the same shape still fires — the cap, not the
    # nesting per se, is what flips it.
    shallow = "SELECT COUNT(*) n FROM t"
    for i in range(5):
        shallow = f"SELECT n FROM ({shallow}) a{i}"
    assert is_row_returning(shallow) is False


# --------------------------------------------------------------------------- #
# is_prunable_count_scalar
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM t",
        "SELECT count(*) FROM t WHERE x < 0",
        "SELECT count(id) AS n FROM t WHERE x < 0",
        "SELECT count(DISTINCT u) FROM t",
    ],
)
def test_is_prunable_count_scalar_true_for_single_count(sql: str) -> None:
    """A no-GROUP-BY SELECT whose sole projection is a bare COUNT (incl.
    COUNT(DISTINCT), #267 DEC-011) graduates to a prunable count scalar."""
    assert is_prunable_count_scalar(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT avg(x) FROM t",
        "SELECT sum(x) FROM t",
        "SELECT count(*) + 1 AS c FROM t",
        "SELECT count(*), max(x) FROM t",
        "SELECT * FROM t WHERE x < 0",
        "SELECT k, count(*) FROM t GROUP BY k",
        # HAVING (even with no GROUP BY) filters the aggregate result → returns
        # zero-or-one rows on a condition unrelated to the failing-row count, so
        # `0 = pass` no longer holds. Must reject (CodeRabbit #269).
        "SELECT count(*) FROM t HAVING count(*) > 3",
    ],
)
def test_is_prunable_count_scalar_false_for_non_count_scalar(sql: str) -> None:
    """Non-count aggregates, arithmetic-on-count, multi-projection, a
    row-returning body, a GROUP-BY body, and a HAVING body are all rejected
    (#267 DEC-001)."""
    assert is_prunable_count_scalar(sql) is False


def test_is_prunable_count_scalar_arithmetic_on_count_is_not_bare_count() -> None:
    """PLANTED NEGATIVE: ``COUNT(*) + 1`` is scalar (``is_row_returning`` False)
    yet the projection is an ``exp.Add`` wrapping the Count, not a bare Count —
    the narrower gate must reject it where ``is_row_returning`` does not
    distinguish."""
    assert is_row_returning("SELECT count(*) + 1 AS c FROM t") is False
    assert is_prunable_count_scalar("SELECT count(*) + 1 AS c FROM t") is False


def test_is_prunable_count_scalar_unparseable_returns_false() -> None:
    """Skip-when-uncertain inverts vs the sibling helpers: the POSITIVE claim
    ``is a prunable count scalar`` cannot be made of an unparseable body."""
    assert is_prunable_count_scalar("SELECT FROM WHERE ((( garbage") is False


def test_is_prunable_count_scalar_union_is_not_single_select() -> None:
    """A ``UNION`` root is not a single SELECT → cannot claim count-scalar.

    Both UNION arms are themselves bare count-scalars, so the ONLY thing that
    makes this ``False`` is the non-``exp.Select`` (``exp.Union``) root check —
    isolating that branch. A bare-literal ``SELECT 1 UNION SELECT 2`` would pass
    this test even if the union-root guard regressed (its projections aren't
    counts), so it wouldn't pin the branch.
    """
    assert is_prunable_count_scalar("SELECT count(*) FROM a UNION SELECT count(*) FROM b") is False


# --------------------------------------------------------------------------- #
# validate_ingested_sql
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a, b FROM t WHERE a <> b",
        "-- a leading line comment\nSELECT a FROM t WHERE a > 0",
        "SELECT a FROM t -- trailing comment with ; and (unbalanced\nWHERE a > 0",
        "SELECT a /* block ; comment ( */ FROM t WHERE a > 0",
        "SELECT a FROM t WHERE note = 'has ; and -- inside literal'",
        "SELECT a FROM t WHERE label = 'a(b' AND x > 0",
    ],
)
def test_validate_ingested_sql_accepts_comment_bearing_and_literal_safe(sql: str) -> None:
    """Comment-tolerant: ``--`` / ``/* */`` comments (and ``;`` / parens hidden
    inside them or inside string literals) do NOT trip the scan."""
    assert validate_ingested_sql(sql) is None


def test_validate_ingested_sql_rejects_multiple_statements() -> None:
    """A real top-level ``;`` (statement injection) still fails loud, even with
    comments present."""
    with pytest.raises(QuerySyntaxError):
        validate_ingested_sql("SELECT a FROM t; DROP TABLE t -- oops")


def test_validate_ingested_sql_rejects_unbalanced_parens() -> None:
    with pytest.raises(QuerySyntaxError):
        validate_ingested_sql("SELECT a FROM t WHERE (a > 0 AND b < 1")


def test_validate_ingested_sql_injection_hidden_by_comment_still_caught() -> None:
    """PLANTED NEGATIVE: stripping comments must NOT swallow a real trailing
    statement that sits OUTSIDE the comment — the ``;`` before the comment is a
    genuine injection and must still trip the scan."""
    with pytest.raises(QuerySyntaxError):
        validate_ingested_sql("SELECT a FROM t ; SELECT b FROM u /* trailing */")


def test_validate_ingested_sql_comment_only_semicolon_is_tolerated() -> None:
    """A ``;`` that exists ONLY inside a comment is not statement injection — the
    comment-aware strip removes it, so the body validates."""
    assert validate_ingested_sql("SELECT a FROM t WHERE a > 0 -- b; c") is None


def test_validate_ingested_sql_backslash_escaped_quote_keeps_comment_inside_string() -> None:
    """A backslash-escaped quote must not exit the string span early (QG hardening).

    ``'it\\'s -- y'`` is a single BigQuery/standard string literal whose body
    contains ``--``. The comment-stripper must honour the ``\\'`` escape and keep
    the ``-- y`` inside the string, so validation neither false-rejects (spurious
    QuerySyntaxError) nor mis-parses the trailing quote as an unterminated span.
    """
    # `--` lives inside the escaped-quote string → NOT a comment → body is safe.
    assert validate_ingested_sql("SELECT * FROM t WHERE label = 'it\\'s -- fine' AND x > 0") is None
    # A real statement separator outside any string is still rejected.
    with pytest.raises(QuerySyntaxError):
        validate_ingested_sql("SELECT * FROM t WHERE label = 'it\\'s ok'; DROP TABLE t")


# --------------------------------------------------------------------------- #
# Hostile-input totality (#268 US-001 / DEC-012(1))
# --------------------------------------------------------------------------- #


def _deeply_nested_body(depth: int = 2000) -> str:
    """A body whose paren nesting exceeds sqlglot's recursive-descent depth.

    ``sqlglot.parse_one`` blows the Python recursion limit on this input and
    raises ``RecursionError`` — which is a ``RuntimeError``, NOT a
    ``sqlglot.errors.SqlglotError``, so pre-#268 it escaped every helper's
    ``except`` and aborted the whole prune run with no audit rows written.
    """
    return "select * from t where " + "(" * depth + "1=1" + ")" * depth


def test_deeply_nested_body_triggers_recursion_error_in_sqlglot() -> None:
    """Pin the premise: the crafted body really does blow sqlglot's recursion.

    Without this, the three conservative-verdict tests below could pass
    vacuously if a future sqlglot grew an iterative parser — this test would
    fail loudly first, telling the maintainer the hostile input needs re-crafting
    rather than silently degrading the totality guarantee to an assertion about
    a body that parses fine.
    """
    import sqlglot

    with pytest.raises(RecursionError):
        sqlglot.parse_one(_deeply_nested_body(), dialect="bigquery")


def test_is_deterministic_sql_recursion_error_returns_conservative_true() -> None:
    """A RecursionError must NOT escape — the helper degrades to ``True``."""
    assert is_deterministic_sql(_deeply_nested_body()) is True


def test_is_row_returning_recursion_error_returns_conservative_true() -> None:
    """A RecursionError must NOT escape — the helper degrades to ``True``."""
    assert is_row_returning(_deeply_nested_body()) is True


def test_is_prunable_count_scalar_recursion_error_returns_conservative_false() -> None:
    """A RecursionError must NOT escape — the helper degrades to ``False``."""
    assert is_prunable_count_scalar(_deeply_nested_body()) is False


def test_unknown_dialect_raises_value_error_from_sqlglot() -> None:
    """Pin the premise: an unknown dialect name raises ``ValueError`` (not a
    ``SqlglotError``), so it escaped the pre-#268 ``except`` too."""
    import sqlglot

    with pytest.raises(ValueError):
        sqlglot.parse_one("SELECT 1", dialect="nope")


def test_is_deterministic_sql_unknown_dialect_returns_conservative_true() -> None:
    """An unknown ``dialect=`` must degrade, never escape as a ``ValueError``."""
    assert is_deterministic_sql("SELECT a FROM t WHERE a > 0", dialect="nope") is True


def test_is_row_returning_unknown_dialect_returns_conservative_true() -> None:
    """An unknown ``dialect=`` must degrade, never escape as a ``ValueError``."""
    assert is_row_returning("SELECT COUNT(*) FROM t", dialect="nope") is True


def test_is_prunable_count_scalar_unknown_dialect_returns_conservative_false() -> None:
    """An unknown ``dialect=`` must degrade, never escape as a ``ValueError``."""
    assert is_prunable_count_scalar("SELECT COUNT(*) FROM t", dialect="nope") is False


# --------------------------------------------------------------------------- #
# parses_under_dialect (#270 US-001 / DEC-003)
# --------------------------------------------------------------------------- #

# A body that is *itself* well-formed SQL but that ``sqlglot`` cannot parse — the
# premise for the conservative-``False`` verdict below. Pinned by
# :func:`test_malformed_body_raises_sqlglot_error` so the ``False`` test can never
# pass vacuously (mirrors the recursion / unknown-dialect premise pins above).
_MALFORMED_BODY = "SELECT FROM WHERE (("


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a, b FROM t WHERE a <> b",
        "SELECT COUNT(*) AS c FROM t",
        "WITH c AS (SELECT n FROM t) SELECT n FROM c",
        "SELECT * FROM t",
    ],
)
def test_parses_under_dialect_clean_parse_returns_true(sql: str) -> None:
    """A body that parses cleanly under the dialect returns ``True``."""
    assert parses_under_dialect(sql) is True


def test_malformed_body_raises_sqlglot_error() -> None:
    """PLANTED PREMISE: the malformed body really does raise a ``SqlglotError``.

    Mirrors :func:`test_unknown_dialect_raises_value_error_from_sqlglot` — without
    it, the conservative-``False`` test below could pass vacuously if a future
    ``sqlglot`` learned to parse this body, silently degrading the totality
    guarantee to an assertion about a body that parses fine.
    """
    import sqlglot
    import sqlglot.errors

    with pytest.raises(sqlglot.errors.SqlglotError):
        sqlglot.parse_one(_MALFORMED_BODY, dialect="bigquery")


def test_parses_under_dialect_sqlglot_error_returns_false() -> None:
    """A ``SqlglotError``-raising body degrades to ``False`` (skip-when-uncertain)."""
    assert parses_under_dialect(_MALFORMED_BODY) is False


def test_parses_under_dialect_recursion_error_returns_false() -> None:
    """A ``RecursionError`` from a deeply-nested body must NOT escape → ``False``."""
    assert parses_under_dialect(_deeply_nested_body()) is False


def test_parses_under_dialect_unknown_dialect_returns_false() -> None:
    """An unknown ``dialect=`` (``ValueError``) must degrade, never escape → ``False``."""
    assert parses_under_dialect("SELECT 1", dialect="nope") is False


def test_parses_under_dialect_tree_is_none_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``None`` from ``sqlglot.parse_one`` degrades to ``False``.

    ``sqlglot`` 30.2.1 raises rather than returning ``None`` for empty / comment-only
    bodies, so the ``tree is None`` branch is pinned by forcing ``parse_one`` to
    return ``None`` — the branch must still fail closed to ``False``.
    """
    monkeypatch.setattr("sqlglot.parse_one", lambda *args, **kwargs: None)
    assert parses_under_dialect("SELECT COUNT(*) FROM t") is False


# --------------------------------------------------------------------------- #
# strip_sql_comments (#270 US-001 / DEC-001 — promoted from the private helper)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # -- line comment dropped
        ("SELECT a FROM t -- trailing\nWHERE a > 0", "SELECT a FROM t \nWHERE a > 0"),
        # /* */ block comment collapses to one space
        ("SELECT a /* block */ FROM t", "SELECT a   FROM t"),
        # a `--` inside a single-quoted literal is NOT a comment
        ("SELECT '-- not a comment' AS x FROM t", "SELECT '-- not a comment' AS x FROM t"),
        # a backslash-escaped quote keeps the span open, so its `--` stays literal
        ("SELECT 'it\\'s -- ok' FROM t", "SELECT 'it\\'s -- ok' FROM t"),
        # no comments → byte-identical passthrough
        ("SELECT COUNT(*) FROM t WHERE a <> b", "SELECT COUNT(*) FROM t WHERE a <> b"),
    ],
)
def test_strip_sql_comments_behaviour(sql: str, expected: str) -> None:
    """``strip_sql_comments`` (the public promotion of ``_strip_sql_comments``)
    drops comments while leaving string-literal spans untouched."""
    assert strip_sql_comments(sql) == expected


def test_strip_sql_comments_is_the_validate_ingested_sql_input() -> None:
    """Regression pin: the comment stripper still feeds ``validate_ingested_sql``.

    The rename from the private ``_strip_sql_comments`` must not have changed the
    behaviour ``validate_ingested_sql`` relies on — a ``;`` hidden ONLY inside a
    comment is stripped and the body validates."""
    # `;` lives inside the block comment → stripped → single-statement body.
    validate_ingested_sql("SELECT a /* ; not injection */ FROM t WHERE a > 0")
