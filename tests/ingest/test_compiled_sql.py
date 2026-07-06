"""Tests for :mod:`signalforge.ingest._compiled_sql` (#154 US-002).

Three pure sqlglot-AST analysis helpers over a compiled-SQL string. Each test
is capable of failing on a real regression (testing-signal.md § "No assert
True"); each helper carries a planted-violation-style NEGATIVE test proving it
does NOT false-positive on the tokens-as-column-names / tokens-inside-literals
cases the AST approach exists to defend (#154 AR rows 9/10/11).
"""

from __future__ import annotations

import pytest

from signalforge.ingest._compiled_sql import (
    is_deterministic_sql,
    is_prunable_count_scalar,
    is_row_returning,
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
    ],
)
def test_is_prunable_count_scalar_false_for_non_count_scalar(sql: str) -> None:
    """Non-count aggregates, arithmetic-on-count, multi-projection, a
    row-returning body, and a GROUP-BY body are all rejected (#267 DEC-001)."""
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
