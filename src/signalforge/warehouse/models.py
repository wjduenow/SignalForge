"""Typed return types for the warehouse adapter layer (US-004).

Mirrors :mod:`signalforge.manifest.models`'s Pydantic v2 conventions
(``frozen=True``, no ``extra="forbid"`` in production), with two notable
exceptions:

* :class:`Dialect`, :class:`TableRef`, and :class:`PartitionFilter` are
  frozen :func:`dataclasses.dataclass` instances rather than Pydantic models.
  These types are constructed by SignalForge code (not deserialised from
  external JSON), so the dataclass surface keeps the call-site overhead
  minimal while still enforcing immutability.
* :class:`ColumnStats` and :class:`TestResult` are Pydantic v2 models so
  they can round-trip through the (future) JSON cache without hand-written
  serialisation code.

Design commitments operationalised here:

* **DEC-003** — :data:`BIGQUERY_DIALECT` is the single warehouse-flavour
  constant for v0.1; future Snowflake/Postgres ports will add siblings here.
* **DEC-004** — :class:`TableRef` carries the fully-qualified BigQuery
  ``project.dataset.table`` identity used by every adapter call.
* **DEC-013** — every public-API string that ends up in a SQL fragment goes
  through :func:`signalforge.warehouse._sql_safety.validate_identifier` at
  construction time.
* **DEC-014** — :class:`TableRef.from_model` is the single gateway from a
  manifest :class:`signalforge.manifest.Model` to a warehouse identifier;
  the missing-database / missing-schema cases raise typed errors so callers
  can pattern-match without sniffing message text.
* **DEC-016** — :class:`ColumnStats` documents that complex BigQuery types
  (``GEOGRAPHY``, ``JSON``, ``ARRAY<...>``, ``STRUCT<...>``, ``RANGE<...>``,
  ``BYTES``) get ``min=max=None``; there is no useful ordering on those.
* **DEC-018** — :class:`PartitionFilter` carries an explicit operator from a
  fixed ``Literal`` set; arbitrary SQL fragments are not accepted.
* **DEC-020** — :meth:`TestResult.explanation` produces the deterministic
  "why" string that ships with every kept/dropped artifact.
* **DEC-027** — :class:`TableRef` allows ``project=None`` so callers can
  defer project resolution to the BigQuery client's default project.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from signalforge.manifest import Model


# ---------------------------------------------------------------------------
# Dialect
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dialect:
    """Warehouse-flavour capability flags and SQL-fragment templates.

    The adapter consults these flags rather than hard-coding warehouse names
    so the v0.2 Snowflake/Postgres ports can add a sibling constant without
    branching the adapter logic on ``isinstance``.

    The five template/flag fields below are read by the **prune compiler**
    (``signalforge.prune.compiler``, issue #121) so it emits warehouse-correct
    SQL without branching on the dialect *name*:

    * ``sample_row_hash_expr`` — the inner whole-row hash expression the prune
      compiler drops into ``MOD(<expr>, <bucket>) < 1`` for deterministic
      hash-mod sampling. BigQuery uses ``ABS(FARM_FINGERPRINT(TO_JSON_STRING(t)))``
      (referencing the ``t`` alias); Snowflake uses ``ABS(HASH(*))``.
    * ``timestamp_literal_template`` — a ``str.format(value=...)`` template that
      renders a TIMESTAMP literal in a partition filter. BigQuery uses the
      ``TIMESTAMP('{value}')`` function form; Snowflake uses the
      ``'{value}'::TIMESTAMP`` cast form. ``{value}`` is an already-escaped
      ISO value, never raw user input.
    * ``date_literal_template`` — the DATE analogue of the above.
    * ``quote_qualified_per_component`` — when ``True`` the compiler quotes
      each component of a qualified table name separately
      (``"DB"."SCH"."T"`` — Snowflake); when ``False`` it wraps the whole
      dotted path in one backtick-quoted pair (BigQuery's ``p.d.t`` form).
    * ``sample_cte_alias`` — the identifier the deterministic-sample CTE is
      bound to (``WITH <alias> AS (...) ... FROM <alias>``). BigQuery uses
      the bare ``sample``; Snowflake uses the **quoted** ``"sample"`` because
      ``SAMPLE`` is a Snowflake reserved keyword (``TABLESAMPLE``) and an
      unquoted CTE named ``sample`` is a syntax error there. Quoting bypasses
      the keyword interpretation while keeping the recognisable name.

    Two further fields (issue #139) describe **where** the row-hash expression
    is legal so the deterministic-sample SELECT builder
    (``signalforge.warehouse._sample_sql.render_sample_select``) can switch
    shape without name-branching:

    * ``sample_hash_in_projection`` — when ``False`` (BigQuery default) the
      hash expression is placed inline in ``WHERE``/``ORDER BY``; when ``True``
      (Snowflake) the hash is computed once in an inner ``SELECT`` projection
      and referenced by alias in the outer ``WHERE``/``ORDER BY``. Snowflake's
      ``HASH(*)`` is rejected as a predicate (``002079: Use of * as a function
      argument``) and is legal **only** in the SELECT projection.
    * ``sample_hash_alias`` — the column alias the projected hash binds to in
      the projection-subquery shape (emitted unquoted; Snowflake folds it
      consistently in both the projection and the ``EXCLUDE`` clause).

    Five further fields (issue #171, DEC-011) describe **date arithmetic** and
    **percentile** SQL forms for the v0.3 row-count-anomaly variant. Reserved
    here at the dialect surface; the compiler arm that reads them lands in
    US-008. None of the seven pre-#171 variants consume these fields, so
    BigQuery snapshots remain byte-identical.

    * ``date_trunc_expr_template`` — ``str.format(date=..., unit=...)`` template
      for ``DATE_TRUNC``. **Argument order differs between dialects:** BigQuery
      is ``DATE_TRUNC(date, unit)``; Snowflake is ``DATE_TRUNC('unit', date)``
      (unit first, single-quoted literal). Format-named substitution sidesteps
      the positional difference.
    * ``interval_expr_template`` — ``str.format(n=..., unit=...)`` template for
      an interval literal. BigQuery: ``INTERVAL n unit`` (bare). Snowflake:
      ``INTERVAL 'n unit'`` (the whole interval is single-quoted).
    * ``extract_dow_expr_template`` — ``str.format(date=...)`` template for the
      day-of-week extraction. BigQuery uses the ``DAYOFWEEK`` part name;
      Snowflake uses ``DOW``. Function shape is otherwise the same.
    * ``dow_sunday_index`` — the integer the day-of-week extraction returns
      for Sunday. BigQuery's ``DAYOFWEEK`` returns ``1`` for Sunday (1..7);
      Snowflake's ``DOW`` returns ``0`` for Sunday (0..6) by default (the
      session ``WEEK_START`` parameter can change Snowflake's basis; ``0`` is
      the conservative default assumption).
    * ``percentile_cont_expr_template`` — ``str.format(p=..., expr=...,
      offset=...)`` template for a GROUP-BY percentile. **The dialects do NOT
      share a form.** BigQuery has no ordered-set-aggregate ``PERCENTILE_CONT``
      (it is window-only and cannot reduce rows in a ``GROUP BY``), so BigQuery
      uses ``APPROX_QUANTILES(expr, 100)[OFFSET(round(p*100))]``; Snowflake and
      Postgres use the standard-SQL ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY
      expr)`` ordered-set form. ``{offset}`` is ``round(p*100)`` (supplied by
      the compiler); the ``WITHIN GROUP`` form ignores it and the
      ``APPROX_QUANTILES`` form ignores ``{p}``. The ``APPROX_QUANTILES`` median
      is *approximate* (~1% error) — acceptable for row-count anomaly bands and
      consistent with the ``HASH()`` reproducibility caveat (issue #121).
    * ``datetime_literal_template`` — the DATETIME analogue of
      ``timestamp_literal_template`` / ``date_literal_template``. Used by the
      anomaly compiler to render a partition-pruning bound whose type matches a
      ``DATETIME`` date column (BigQuery ``DATETIME('{value}')``).

    The defaults reproduce BigQuery's SQL byte-for-byte so every existing
    construction site stays valid unedited (DEC-001 of issue #121; DEC-001 of
    issue #139; DEC-011 of issue #171).
    """

    name: str
    supports_tablesample: bool
    supports_qualify: bool
    quote_char: str
    identifier_case: Literal["upper", "lower", "preserve"]
    sample_row_hash_expr: str = "ABS(FARM_FINGERPRINT(TO_JSON_STRING(t)))"
    timestamp_literal_template: str = "TIMESTAMP('{value}')"
    date_literal_template: str = "DATE('{value}')"
    datetime_literal_template: str = "DATETIME('{value}')"
    quote_qualified_per_component: bool = False
    sample_cte_alias: str = "sample"
    sample_hash_in_projection: bool = False
    sample_hash_alias: str = "_sf_sample_hash"
    # Issue #171 DEC-011 — date arithmetic + percentile SQL forms for the
    # row-count-anomaly variant. Reserved at the dialect surface in US-002;
    # the compiler arm that reads them lands in US-008.
    date_trunc_expr_template: str = "DATE_TRUNC({date}, {unit})"
    interval_expr_template: str = "INTERVAL {n} {unit}"
    extract_dow_expr_template: str = "EXTRACT(DAYOFWEEK FROM {date})"
    dow_sunday_index: int = 1
    # BigQuery has no GROUP-BY-compatible ordered-set ``PERCENTILE_CONT`` (it is
    # window-only), so the default is the ``APPROX_QUANTILES`` idiom. The
    # ``{offset}`` placeholder is ``round(p * 100)`` (computed by the compiler's
    # ``_percentile_expr``). Snowflake / Postgres override to the standard-SQL
    # ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY expr)`` ordered-set form, which
    # ignores ``{offset}``.
    percentile_cont_expr_template: str = "APPROX_QUANTILES({expr}, 100)[OFFSET({offset})]"


BIGQUERY_DIALECT = Dialect(
    name="bigquery",
    supports_tablesample=True,
    supports_qualify=True,
    quote_char="`",
    identifier_case="preserve",
)


POSTGRES_DIALECT = Dialect(
    name="postgres",
    supports_tablesample=True,
    supports_qualify=False,
    quote_char='"',
    identifier_case="lower",
    # The BigQuery default ``percentile_cont_expr_template`` is now the
    # BigQuery-only ``APPROX_QUANTILES`` idiom, which is invalid Postgres, so
    # Postgres must declare its own form explicitly rather than inherit it.
    # Postgres supports the standard-SQL ordered-set aggregate directly.
    percentile_cont_expr_template="PERCENTILE_CONT({p}) WITHIN GROUP (ORDER BY {expr})",
)
"""Postgres-flavoured :class:`Dialect` for the v0.2 stub adapter (issue #53).

* ``quote_char='"'`` — Postgres uses double-quote for identifier quoting.
* ``identifier_case='lower'`` — unquoted identifiers are folded to
  lowercase (matches Postgres's own SQL parser behaviour).
* ``supports_qualify=False`` — Postgres has no ``QUALIFY`` clause.
* ``supports_tablesample=True`` — ``TABLESAMPLE`` is supported (BERNOULLI
  / SYSTEM), though the prune layer prefers deterministic hash-mod
  sampling anyway (DEC-006 of issue #3).

The five issue-#121 SQL-fragment fields (``sample_row_hash_expr``,
``timestamp_literal_template``, ``date_literal_template``,
``quote_qualified_per_component``, ``sample_cte_alias``) AND the issue-#171
date-arithmetic fields (``date_trunc_expr_template``, ``interval_expr_template``,
``extract_dow_expr_template``, ``dow_sunday_index``) keep their **BigQuery
defaults** here because the Postgres adapter's warehouse ops are not
implemented yet (the #53 stub raises ``NotImplementedError`` from every op
method), so the prune compiler is never invoked for a Postgres profile.
``percentile_cont_expr_template`` is the exception: its BigQuery default is now
the BigQuery-only ``APPROX_QUANTILES`` idiom (invalid Postgres), so Postgres
declares the standard-SQL ``PERCENTILE_CONT … WITHIN GROUP`` form explicitly.
Most of these defaults are
wrong for Postgres and will be corrected when the Postgres adapter's
warehouse ops land (DEC-007 of issue #121; corrected in the Postgres-ops PR
in the issue #53/118 family): Postgres needs
``quote_qualified_per_component=True`` (it quotes ``"schema"."table"`` per
component), the SQL-standard ``TIMESTAMP '...'`` / ``DATE '...'`` literal
forms rather than BigQuery's ``TIMESTAMP(...)`` / ``DATE(...)`` function
form, and Postgres-specific date arithmetic (``EXTRACT(DOW FROM ...)``
returning 0..6 with Sunday=0, ``INTERVAL '1 day'`` string-literal form).
(``sample_cte_alias="sample"`` happens to be Postgres-correct already —
``SAMPLE`` is not reserved in Postgres, only ``TABLESAMPLE`` is.) Shipping
knowingly-wrong-but-untested fragments now would be misleading.

Lives alongside :data:`BIGQUERY_DIALECT` per DEC-003 so the prune
compiler (and any other dialect-aware consumer) imports every flavour
from one place rather than reaching into adapter modules.
"""


SNOWFLAKE_DIALECT = Dialect(
    name="snowflake",
    supports_tablesample=True,
    supports_qualify=True,
    quote_char='"',
    identifier_case="upper",
    sample_row_hash_expr="ABS(HASH(*))",
    sample_cte_alias='"sample"',
    timestamp_literal_template="'{value}'::TIMESTAMP",
    date_literal_template="'{value}'::DATE",
    datetime_literal_template="'{value}'::DATETIME",
    quote_qualified_per_component=True,
    sample_hash_in_projection=True,
    # Issue #171 DEC-011 — Snowflake overrides for the row-count-anomaly variant.
    date_trunc_expr_template="DATE_TRUNC('{unit}', {date})",
    interval_expr_template="INTERVAL '{n} {unit}'",
    extract_dow_expr_template="EXTRACT(DOW FROM {date})",
    # Snowflake's DOW returns 0..6 (0=Sun) by default; the session WEEK_START
    # parameter can shift the basis, so 0 is the conservative assumption.
    dow_sunday_index=0,
    # PERCENTILE_CONT shape matches BigQuery — kept for parity / future Postgres.
    percentile_cont_expr_template="PERCENTILE_CONT({p}) WITHIN GROUP (ORDER BY {expr})",
)
"""Snowflake-flavoured :class:`Dialect` for the v0.2 adapter (issue #119, DEC-004).

* ``quote_char='"'`` — Snowflake uses double-quote for identifier quoting.
* ``identifier_case='upper'`` — unquoted identifiers fold to UPPERCASE.
  This is the **opposite** of Postgres (``identifier_case='lower'``) and is
  **load-bearing** for the Snowflake compiler (issue #121): identifier-case
  folding drives how quoted vs. unquoted names resolve, so the two dialects
  must not share a casing rule.
* ``supports_qualify=True`` — Snowflake supports the ``QUALIFY`` clause.
* ``supports_tablesample=True`` — ``TABLESAMPLE`` is supported, though the
  prune layer prefers deterministic hash-mod sampling anyway (DEC-006 of
  issue #3).
* ``sample_row_hash_expr="ABS(HASH(*))"`` — Snowflake's variadic whole-row
  hash; ``ABS`` before ``MOD`` mirrors the BigQuery structure (issue #121,
  DEC-002).
* ``timestamp_literal_template="'{value}'::TIMESTAMP"`` /
  ``date_literal_template="'{value}'::DATE"`` — the idiomatic Snowflake cast
  form (vs. BigQuery's ``TIMESTAMP('...')`` function form).
* ``quote_qualified_per_component=True`` — Snowflake reads a single quoted
  string spanning dots as one literal identifier named ``db.schema.table``,
  so each component is quoted separately (``"DB"."SCH"."T"``).
* ``sample_hash_in_projection=True`` — Snowflake's ``HASH(*)`` is rejected as
  a ``WHERE``/``ORDER BY`` predicate (``002079``) and is legal only in the
  SELECT projection, so the deterministic-sample SELECT computes the hash in
  an inner projection and references the ``sample_hash_alias`` column in the
  outer clauses (issue #139, DEC-001/DEC-004).
* ``date_trunc_expr_template="DATE_TRUNC('{unit}', {date})"`` — Snowflake's
  argument order is ``(unit, date)`` with the unit as a single-quoted literal,
  the **opposite** of BigQuery's ``(date, unit)``. Named substitution
  (``str.format(date=..., unit=...)``) sidesteps the positional difference.
* ``interval_expr_template="INTERVAL '{n} {unit}'"`` — the whole
  ``n unit`` payload is single-quoted on Snowflake (vs. BigQuery's bare form).
* ``extract_dow_expr_template="EXTRACT(DOW FROM {date})"`` — Snowflake's
  day-of-week part name is ``DOW`` (BigQuery uses ``DAYOFWEEK``).
* ``dow_sunday_index=0`` — Snowflake's ``DOW`` returns ``0`` for Sunday by
  default (the session ``WEEK_START`` parameter can change the basis; ``0`` is
  the conservative assumption). BigQuery's ``DAYOFWEEK`` returns ``1``.
* ``percentile_cont_expr_template`` — same shape as BigQuery
  (``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY expr)``); the field exists
  for parity and to anchor future Postgres variation.

Lives alongside :data:`BIGQUERY_DIALECT` / :data:`POSTGRES_DIALECT` per
DEC-003 so every dialect-aware consumer imports each flavour from one place
rather than reaching into adapter modules.
"""


DATABRICKS_DIALECT = Dialect(
    name="databricks",
    supports_tablesample=True,
    supports_qualify=True,
    quote_char="`",
    identifier_case="lower",
    # Sign-bit MASK, not ABS: xxhash64 returns a SIGNED 64-bit long, and Spark's
    # ABS(Long.MIN_VALUE) stays negative in non-ANSI mode (no positive equivalent
    # fits a signed long), so MOD(ABS(...), bucket) would admit a stray negative
    # residue class and skew the deterministic sample. `& 9223372036854775807`
    # (Long.MAX_VALUE) clears the sign bit — always non-negative, no overflow,
    # uniform — and the renderer's MOD(<expr>, bucket) wrapper stays correct.
    sample_row_hash_expr="(xxhash64(to_json(struct(*))) & 9223372036854775807)",
    timestamp_literal_template="TIMESTAMP '{value}'",
    date_literal_template="DATE '{value}'",
    # Spark/Databricks has no distinct DATETIME type — TIMESTAMP is the
    # wall-clock type — so the DATETIME literal reuses the TIMESTAMP form.
    datetime_literal_template="TIMESTAMP '{value}'",
    quote_qualified_per_component=True,
    sample_hash_in_projection=False,
    # Issue #171 DEC-011 — Databricks overrides for the row-count-anomaly variant.
    date_trunc_expr_template="DATE_TRUNC('{unit}', {date})",
    interval_expr_template="INTERVAL {n} {unit}",
    extract_dow_expr_template="DAYOFWEEK({date})",
    dow_sunday_index=1,
    percentile_cont_expr_template="PERCENTILE_CONT({p}) WITHIN GROUP (ORDER BY {expr})",
)
"""Databricks/Spark-SQL :class:`Dialect` for the v0.x adapter (issue #221, epic #219).

Decided at the skeleton stage; the values the prune compiler keys on are
**certified offline by the #223 ``sqlglot`` ``databricks``-dialect parse-guard**
(``tests/prune/test_compiler_databricks.py`` — ungated, runs in the default
suite) and will be certified **live by #226**. At the skeleton stage the prune
compiler is never invoked for a Databricks profile (every op raises
``NotImplementedError`` / inherits the ABC degrade), so these are
grounded-and-parse-validated but not yet executed against real Spark.

* ``quote_char='`'`` — Databricks quotes identifiers with backticks (Spark SQL),
  unlike Snowflake/Postgres double-quote.
* ``identifier_case='lower'`` — Unity Catalog folds unquoted metadata
  identifiers to **lowercase** (the *opposite* of Snowflake's ``'upper'``, like
  Postgres). ⚠️ Load-bearing for #223 identifier matching; verify against a real
  ``CREATE TABLE`` round-trip before the compiler locks on it.
* ``supports_qualify=True`` — Databricks SQL supports ``QUALIFY`` (Spark 3.5+),
  but ``unique`` stays on the portable ``GROUP BY … HAVING`` form per #121 (a
  ``QUALIFY`` rewrite is a separate semantics decision, not a dialect flag).
* ``sample_row_hash_expr='(xxhash64(to_json(struct(*))) & 9223372036854775807)'``
  — the **64-bit** whole-row hash, sign-bit masked. Spark's bare ``hash(*)`` is
  Murmur3-**32** (collision-prone at scale), so the 64-bit ``xxhash64`` over the
  JSON-serialised row is chosen for sampling stability. The mask (``& Long.MAX``)
  replaces ``ABS``: ``xxhash64`` is signed and Spark's ``ABS(Long.MIN_VALUE)``
  stays negative in non-ANSI mode, which would skew ``MOD(<expr>, bucket) < 1``;
  clearing the sign bit is non-negative + overflow-free + uniform. Same "``HASH``
  is engine/release-stable, not cross-time" caveat Snowflake documented applies.
* ``timestamp_literal_template="TIMESTAMP '{value}'"`` /
  ``date_literal_template="DATE '{value}'"`` — Spark typed-literal form. Spark
  has no separate ``DATETIME`` type, so ``datetime_literal_template`` reuses the
  ``TIMESTAMP`` form.
* ``quote_qualified_per_component=True`` — Unity Catalog three-part names are
  quoted per component (`` `catalog`.`schema`.`table` ``), not as one
  dotted literal.
* ``sample_hash_in_projection=False`` — default inline ``WHERE``/``ORDER BY``
  placement. #224 flips this to ``True`` (the Snowflake #139 projection-subquery
  shape) only if Spark rejects the hash expression as a predicate.
* date-arithmetic / percentile fields (issue #171): Spark's ``date_trunc`` takes
  ``(unit, date)`` with a quoted unit (like Snowflake); ``DAYOFWEEK(date)``
  returns ``1`` for Sunday (like BigQuery, hence ``dow_sunday_index=1``);
  Databricks supports the standard-SQL ``PERCENTILE_CONT(p) WITHIN GROUP``
  ordered-set aggregate.

Lives alongside :data:`BIGQUERY_DIALECT` / :data:`POSTGRES_DIALECT` /
:data:`SNOWFLAKE_DIALECT` per DEC-003 so every dialect-aware consumer imports
each flavour from one place rather than reaching into adapter modules.
"""


# ---------------------------------------------------------------------------
# TableRef
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableRef:
    """Fully-qualified BigQuery table identity (DEC-004).

    ``project`` is allowed to be ``None`` (DEC-027) so callers can defer
    project resolution to the BigQuery client's default project; ``dataset``
    and ``name`` are required and validated at construction time.
    """

    project: str | None
    dataset: str
    name: str

    def __post_init__(self) -> None:
        # Validate non-None fields (project is allowed to be None — DEC-027).
        # ``project`` is dialect-neutral (a BigQuery project ID, a Snowflake
        # database, or a Unity Catalog catalog), so it accepts EITHER a strict
        # SQL identifier (admits short catalogs like ``main`` — DEC-005 of #224)
        # OR GCP's hyphen-permissive project grammar; ``dataset`` and ``name``
        # use the strict identifier regex (warehouses reject hyphens in
        # unquoted dataset / table names anyway).
        from signalforge.warehouse._sql_safety import (
            validate_catalog_or_project,
            validate_identifier,
        )

        if self.project is not None:
            validate_catalog_or_project("project", self.project)
        validate_identifier("dataset", self.dataset)
        validate_identifier("name", self.name)

    @property
    def qualified_name(self) -> str:
        """Stable ``[project.]dataset.name`` identifier for error messages.

        Dialect-neutral (no backticks); ``project`` is omitted when ``None``
        so callers see the same shape they'd type into a console.
        """
        if self.project is None:
            return f"{self.dataset}.{self.name}"
        return f"{self.project}.{self.dataset}.{self.name}"

    @classmethod
    def from_model(cls, model: Model) -> TableRef:
        """Construct a ``TableRef`` from a manifest :class:`Model` (DEC-014).

        Raises :class:`ManifestProjectNotFoundError` if ``model.database`` is
        ``None`` and :class:`ManifestSchemaNotFoundError` if ``model.schema_``
        is ``None``. The runtime imports are kept inside the method so this
        module can be imported before :mod:`signalforge.manifest` is fully
        wired up.
        """
        from signalforge.warehouse.errors import (
            ManifestProjectNotFoundError,
            ManifestSchemaNotFoundError,
        )

        if model.database is None:
            raise ManifestProjectNotFoundError(model_unique_id=model.unique_id)
        if model.schema_ is None:
            raise ManifestSchemaNotFoundError(model_unique_id=model.unique_id)
        return cls(
            project=model.database,
            dataset=model.schema_,
            name=model.alias or model.name,
        )


# ---------------------------------------------------------------------------
# PartitionFilter
# ---------------------------------------------------------------------------


PartitionOp = Literal["=", ">", ">=", "<", "<=", "!="]


@dataclass(frozen=True)
class PartitionFilter:
    """An operator + value pair scoping a sample to a partition (DEC-018).

    The operator is drawn from a fixed :data:`PartitionOp` ``Literal`` so
    callers cannot smuggle arbitrary SQL through the ``op`` field; the
    column name is validated against the DEC-013 identifier regex at
    construction time.
    """

    column: str
    op: PartitionOp
    value: date | datetime | str

    def __post_init__(self) -> None:
        from signalforge.warehouse._sql_safety import validate_identifier

        validate_identifier("partition_filter.column", self.column)


# ---------------------------------------------------------------------------
# ColumnStats
# ---------------------------------------------------------------------------


ColumnMinMax = int | float | str | bool | datetime | date | None


class ColumnStats(BaseModel):
    """Per-column profile returned by ``BigQueryAdapter.column_stats``.

    For BigQuery types where ordering is not meaningful — ``GEOGRAPHY``,
    ``JSON``, ``ARRAY<...>``, ``STRUCT<...>``, ``RANGE<...>``, ``BYTES`` —
    the adapter sets ``min=max=None`` (DEC-016). ``count``, ``distinct``,
    and ``nulls`` are populated for every type.

    ``data_type`` is the raw BigQuery type string (e.g. ``"INT64"``,
    ``"STRING"``, ``"ARRAY<STRUCT<...>>"``); the prune layer keys decisions
    on it without re-reading the catalog.
    """

    model_config = ConfigDict(frozen=True)

    count: int
    distinct: int
    nulls: int
    min: ColumnMinMax = None
    max: ColumnMinMax = None
    data_type: str


# ---------------------------------------------------------------------------
# TestResult
# ---------------------------------------------------------------------------


class TestResult(BaseModel):
    """Outcome of running one candidate test SQL against the warehouse.

    ``passed`` is the binary signal the prune layer keys on; ``failure_count``
    and ``sample_failures`` carry the supporting evidence. ``row_schema``
    records the BigQuery types of each column in ``sample_failures`` so
    :meth:`explanation` can render TIMESTAMP/DATETIME values in a paste-able
    SQL form.

    :meth:`explanation` (DEC-020) produces the deterministic "why" string
    that ships with every kept/dropped artifact; it is intentionally
    side-effect-free so the prune diff is reproducible.
    """

    model_config = ConfigDict(frozen=True)

    # Tell pytest not to collect this class — its name starts with ``Test``
    # but it is a Pydantic data class, not a test class.
    __test__ = False

    passed: bool
    failure_count: int
    sample_failures: list[dict] | None = None
    row_schema: list[tuple[str, str]] | None = None

    def explanation(self) -> str:
        """Render the deterministic "why" string for this test result."""
        if self.passed:
            return "passed"
        base = f"{self.failure_count} rows failed"
        if self.sample_failures:
            from signalforge.warehouse._test_result_repr import compact_repr

            example = compact_repr(self.sample_failures[0], self.row_schema)
            return f"{base} (example: {example})"
        return base


# Sorted alphabetically (verified by tests/warehouse/test_models.py).
__all__ = [
    "BIGQUERY_DIALECT",
    "ColumnStats",
    "Dialect",
    "POSTGRES_DIALECT",
    "SNOWFLAKE_DIALECT",
    "PartitionFilter",
    "TableRef",
    "TestResult",
]
