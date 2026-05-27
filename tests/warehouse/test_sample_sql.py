"""Unit tests for ``signalforge.warehouse._sample_sql.render_sample_select``.

The shared deterministic-sample SELECT builder (issue #139, US-001) emits
two shapes switched ONLY on the boolean ``Dialect.sample_hash_in_projection``
flag — never on ``dialect.name`` (DEC-002/DEC-004). The inline branch must
reproduce the prune compiler's current BigQuery sample-CTE body byte-for-byte
(DEC-003); the projection-subquery branch is the Snowflake fix where
``HASH(*)`` is computed once in an inner projection and the WHERE/ORDER BY
reference the resulting alias.

Every test is capable of failing (``testing-signal.md`` — no
``assert True``-shaped placeholders).
"""

from __future__ import annotations

import pytest

from signalforge.warehouse._sample_sql import render_sample_select
from signalforge.warehouse.models import (
    BIGQUERY_DIALECT,
    SNOWFLAKE_DIALECT,
    Dialect,
)

_TBL = "`p.d.t`"


@pytest.mark.unit
def test_inline_no_order_no_extra_where_matches_compiler_cte_body() -> None:
    """Inline branch (BigQuery default) with ``order_by_hash=False`` and no
    ``extra_where`` reproduces the prune compiler's current sample-CTE body
    byte-for-byte (DEC-003)."""
    sql = render_sample_select(
        _TBL,
        dialect=BIGQUERY_DIALECT,
        sample_bucket=10,
        sample_size=100000,
        order_by_hash=False,
    )
    assert sql == (
        "SELECT * FROM `p.d.t` AS t "
        "WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), 10) < 1 "
        "LIMIT 100000"
    )


@pytest.mark.unit
def test_inline_with_order_and_extra_where() -> None:
    """Inline branch with ``order_by_hash=True`` and an ``extra_where``
    predicate appends ``AND <pf>`` to the WHERE and ``ORDER BY <expr>``."""
    sql = render_sample_select(
        _TBL,
        dialect=BIGQUERY_DIALECT,
        sample_bucket=10,
        sample_size=500,
        extra_where="ts >= TIMESTAMP('2020-01-01')",
        order_by_hash=True,
    )
    expr = "ABS(FARM_FINGERPRINT(TO_JSON_STRING(t)))"
    assert sql == (
        f"SELECT * FROM `p.d.t` AS t "
        f"WHERE MOD({expr}, 10) < 1 AND ts >= TIMESTAMP('2020-01-01') "
        f"ORDER BY {expr} LIMIT 500"
    )


@pytest.mark.unit
def test_projection_with_order_no_extra_where() -> None:
    """Projection-subquery branch (Snowflake) computes the hash in an inner
    projection and references the alias in WHERE/ORDER BY; the outer
    projection ``EXCLUDE``s the alias so returned rows carry only original
    columns (DEC-004)."""
    sql = render_sample_select(
        '"D"."T"',
        dialect=SNOWFLAKE_DIALECT,
        sample_bucket=10,
        sample_size=500,
        order_by_hash=True,
    )
    assert sql == (
        "SELECT * EXCLUDE (_sf_sample_hash) FROM "
        '(SELECT t.*, ABS(HASH(*)) AS _sf_sample_hash FROM "D"."T" AS t) '
        "WHERE MOD(_sf_sample_hash, 10) < 1 "
        "ORDER BY _sf_sample_hash LIMIT 500"
    )


@pytest.mark.unit
def test_projection_extra_where_lands_at_outer_level() -> None:
    """Projection branch places ``AND <pf>`` at the OUTER WHERE level (after
    the ``MOD(_sf_sample_hash, ...) < 1`` predicate), not inside the inner
    subquery."""
    sql = render_sample_select(
        '"D"."T"',
        dialect=SNOWFLAKE_DIALECT,
        sample_bucket=7,
        sample_size=42,
        extra_where="ts >= '2020-01-01'::TIMESTAMP",
        order_by_hash=False,
    )
    assert sql == (
        "SELECT * EXCLUDE (_sf_sample_hash) FROM "
        '(SELECT t.*, ABS(HASH(*)) AS _sf_sample_hash FROM "D"."T" AS t) '
        "WHERE MOD(_sf_sample_hash, 7) < 1 AND ts >= '2020-01-01'::TIMESTAMP "
        "LIMIT 42"
    )


@pytest.mark.unit
def test_branch_switches_on_flag_not_dialect_name() -> None:
    """A synthetic dialect with an unrecognised ``name`` but
    ``sample_hash_in_projection=True`` STILL renders the projection form —
    proving the helper never branches on ``dialect.name`` (DEC-002)."""
    madeup = Dialect(
        name="madeup",
        supports_tablesample=True,
        supports_qualify=False,
        quote_char='"',
        identifier_case="preserve",
        sample_row_hash_expr="ABS(HASH(*))",
        sample_hash_in_projection=True,
    )
    sql = render_sample_select(
        '"X"',
        dialect=madeup,
        sample_bucket=3,
        sample_size=9,
        order_by_hash=True,
    )
    assert sql.startswith("SELECT * EXCLUDE (_sf_sample_hash) FROM (SELECT t.*, ")
    assert "MOD(_sf_sample_hash, 3) < 1" in sql
    assert "ORDER BY _sf_sample_hash" in sql
