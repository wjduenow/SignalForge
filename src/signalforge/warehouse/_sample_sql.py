"""Shared deterministic-sample SELECT builder (issue #139, DEC-002/DEC-004).

A single stateless string-builder owns the two warehouse sample SHAPES so the
prune compiler's sample CTE and the Snowflake adapter's ``sample_rows`` /
``materialise_sample`` stay byte-consistent (Architectural Commitment #5).

The two shapes are switched ONLY on the boolean
:attr:`signalforge.warehouse.models.Dialect.sample_hash_in_projection` — never
on ``dialect.name`` (``prune-engine.md`` DEC-025; the prune compiler imports
this helper and must stay name-agnostic):

* **Inline** (``sample_hash_in_projection=False``, BigQuery) — the row-hash
  expression sits directly in ``WHERE``/``ORDER BY``::

      SELECT * FROM <table_sql> AS t
      WHERE MOD(<hash_expr>, <bucket>) < 1[ AND <extra_where>]
      [ORDER BY <hash_expr>] LIMIT <n>

  This reproduces the prune compiler's current sample-CTE body byte-for-byte
  (DEC-003), the load-bearing BigQuery regression gate.

* **Projection-subquery** (``sample_hash_in_projection=True``, Snowflake) —
  Snowflake's ``HASH(*)`` is rejected as a predicate (``002079``) and is legal
  only in the SELECT projection, so the hash is computed once in an inner
  projection bound to ``<alias>`` (``dialect.sample_hash_alias``) and the outer
  ``WHERE``/``ORDER BY`` reference that alias; ``SELECT * EXCLUDE (<alias>)``
  strips the helper column so returned rows carry only original columns::

      SELECT * EXCLUDE (<alias>) FROM
      (SELECT t.*, <hash_expr> AS <alias> FROM <table_sql> AS t)
      WHERE MOD(<alias>, <bucket>) < 1[ AND <extra_where>]
      [ORDER BY <alias>] LIMIT <n>

``extra_where`` is a **pre-rendered** partition predicate fragment supplied by
the caller (each consumer owns its dialect-correct ``_render_partition_filter``);
this helper never renders partition filters. No logging.
"""

from __future__ import annotations

from signalforge.warehouse.models import Dialect

__all__ = ["render_sample_select"]


def render_sample_select(
    table_sql: str,
    *,
    dialect: Dialect,
    sample_bucket: int,
    sample_size: int,
    extra_where: str | None = None,
    order_by_hash: bool,
) -> str:
    """Render a deterministic hash-mod sample ``SELECT`` for ``table_sql``.

    Args:
        table_sql: An already-quoted ``FROM`` target (e.g. ``` `p.d.t` ``` or
            ``"DB"."SCH"."T"``). Aliased to ``t`` in the emitted SQL.
        dialect: Drives the hash expression and the inline-vs-projection shape.
        sample_bucket: The ``MOD(<hash>, <bucket>) < 1`` bucket size.
        sample_size: The trailing ``LIMIT`` value.
        extra_where: A pre-rendered partition predicate; appended as
            ``AND <extra_where>`` to the (outer) ``WHERE`` when provided.
        order_by_hash: When ``True`` an ``ORDER BY`` on the hash is emitted
            (the adapters want it; the compiler CTE passes ``False``).

    Returns:
        A single ``SELECT`` statement (no trailing semicolon, no CTE wrapper).
    """
    expr = dialect.sample_row_hash_expr

    if dialect.sample_hash_in_projection:
        alias = dialect.sample_hash_alias
        where_sql = f"MOD({alias}, {sample_bucket}) < 1"
        if extra_where is not None:
            where_sql += f" AND {extra_where}"
        order_sql = f" ORDER BY {alias}" if order_by_hash else ""
        return (
            f"SELECT * EXCLUDE ({alias}) FROM "
            f"(SELECT t.*, {expr} AS {alias} FROM {table_sql} AS t) "
            f"WHERE {where_sql}{order_sql} LIMIT {sample_size}"
        )

    where_sql = f"MOD({expr}, {sample_bucket}) < 1"
    if extra_where is not None:
        where_sql += f" AND {extra_where}"
    order_sql = f" ORDER BY {expr}" if order_by_hash else ""
    return f"SELECT * FROM {table_sql} AS t WHERE {where_sql}{order_sql} LIMIT {sample_size}"
