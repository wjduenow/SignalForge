"""Typed candidate-schema models for the LLM drafter (US-008).

Defines the read-back-stable shapes the draft pipeline returns to its
callers: :class:`CandidateSchema`, :class:`CandidateColumn`, and the
discriminated :class:`CandidateTest` union. These models describe the
*output* of the LLM-drafting stage; downstream stages (prune #6, grade
#7, diff render #8) consume them.

Design commitments operationalised here:

* **DEC-003 / DEC-026** — :class:`CandidateSchema` carries a
  ``schema_version: int = 1`` field so future on-disk JSON consumers can
  branch on shape changes. Mirrors :attr:`AuditEvent.audit_schema_version`
  from the safety layer (DEC-014).
* **DEC-010** — Read-back models use ``frozen=True`` + ``extra="ignore"``
  for forward-compat with future LLM response shapes. Pair this with the
  one-off ``extra="forbid"`` drift detector that lands in US-014.
* **Transitive immutability** — sequences are :class:`tuple` rather than
  :class:`list` so a caller cannot mutate ``columns`` / ``tests`` after
  construction.

Construction only validates *non-emptiness* of the load-bearing string
fields; semantic prune-time validation (e.g., does the column exist on
the model?) lands in the prune layer (#6), not here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_BASE_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


def _scope_repr(column: str | None) -> str:
    """Render the scope segment for a redacted candidate-test ``__repr__``.

    Returns ``column=<name>`` for column-scoped tests, ``<model-level>`` for
    model-level tests (``column is None``). The column NAME is operationally
    useful (which column does this test apply to?) and is not value-bearing;
    only the LLM-emitted free-text fields (``sql`` / ``where`` /
    ``rationale``) are redacted.

    Centralises the scope-rendering convention across all candidate-test
    ``__repr__`` overrides so a future variant inherits the same shape.
    """
    if column is None:
        return "<model-level>"
    return f"column={column!r}"


class CandidateTestNotNull(BaseModel):
    """A ``not_null`` test on one column."""

    model_config = _BASE_CONFIG

    type: Literal["not_null"] = "not_null"
    column: str
    rationale: str | None = None

    @field_validator("column")
    @classmethod
    def _column_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("CandidateTestNotNull.column must be non-empty")
        return v


class CandidateTestUnique(BaseModel):
    """A ``unique`` test on one column."""

    model_config = _BASE_CONFIG

    type: Literal["unique"] = "unique"
    column: str
    rationale: str | None = None

    @field_validator("column")
    @classmethod
    def _column_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("CandidateTestUnique.column must be non-empty")
        return v


class CandidateTestAcceptedValues(BaseModel):
    """An ``accepted_values`` test on one column.

    ``values`` is a non-empty tuple of strings (DEC-022 transitive
    immutability). An empty ``values`` tuple is rejected at construction —
    a zero-element accepted-values test is always-fail noise.
    """

    model_config = _BASE_CONFIG

    type: Literal["accepted_values"] = "accepted_values"
    column: str
    values: tuple[str, ...]
    rationale: str | None = None

    @field_validator("column")
    @classmethod
    def _column_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("CandidateTestAcceptedValues.column must be non-empty")
        return v

    @field_validator("values")
    @classmethod
    def _values_non_empty(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(v) == 0:
            raise ValueError("CandidateTestAcceptedValues.values must contain at least one value")
        return v


class CandidateTestRelationships(BaseModel):
    """A ``relationships`` test referencing another model's column."""

    model_config = _BASE_CONFIG

    type: Literal["relationships"] = "relationships"
    column: str
    to: str
    field: str
    rationale: str | None = None

    @field_validator("column")
    @classmethod
    def _column_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("CandidateTestRelationships.column must be non-empty")
        return v

    @field_validator("to")
    @classmethod
    def _to_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("CandidateTestRelationships.to must be non-empty")
        return v

    @field_validator("field")
    @classmethod
    def _field_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("CandidateTestRelationships.field must be non-empty")
        return v


class CandidateTestCustomSQL(BaseModel):
    """A custom singular SQL test (DEC-002).

    Carries the raw singular-test SQL the LLM authored. Per dbt's
    singular-test convention, the SQL returns the *failing* rows: a test
    passes when the query returns zero rows. ``column`` is optional —
    ``None`` marks a model-level business-rule assertion; a non-empty
    string scopes the test to one column. Distinct from the four standard
    schema-test variants, which compile to known dbt generic tests; this
    variant is a free-form escape hatch for business rules that the
    generic catalogue cannot express.
    """

    model_config = _BASE_CONFIG

    type: Literal["custom_sql"] = "custom_sql"
    sql: str
    column: str | None = None
    rationale: str | None = None

    @field_validator("sql")
    @classmethod
    def _sql_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("CandidateTestCustomSQL.sql must be non-empty")
        return v

    def __repr__(self) -> str:
        """Redacted repr — omits the LLM-emitted ``sql`` and ``rationale``
        (DEC-013 of #170).

        An accidental ``_LOGGER.warning("test: %s", t)`` would otherwise
        dump the full LLM-authored singular-test SELECT into log sinks; the
        body can be arbitrarily long, multi-line, and may quote upstream
        column data via the model SQL it scans. Mirrors the redaction
        precedent established on ``PruneDecision`` (prune DEC-022),
        ``GradingResult`` (grade DEC-022), and ``DiffEntry`` (diff DEC-020).
        Full content remains accessible via :meth:`model_dump` /
        :meth:`model_dump_json` — only the casual debug-print path
        (``repr()`` / ``%s``-interpolation) is redacted.
        """
        return f"CandidateTestCustomSQL(type='custom_sql', {_scope_repr(self.column)})"

    def __repr_args__(self) -> list[tuple[str | None, Any]]:
        """Redact via Pydantic's structured-repr hook.

        DEC-013 of #170 + QG Pass 1 finding C1: ``__repr__`` redacts the
        ``%s``-interpolation path; ``__rich_repr__`` / ``__pretty__``
        (rich.print() / devtools / pprint debug tooling) reach through
        ``__repr_args__`` and would otherwise still see the redacted
        fields. Filtering here closes the leak across all three surfaces
        with one override.
        """
        return [("type", self.type), ("column", self.column)]


class CandidateTestRowCountBetween(BaseModel):
    """A model-level row-count-bounds test (#169, DEC-001).

    Asserts that the model's ``COUNT(*)`` (optionally filtered by
    ``where``) lies within ``[minimum, maximum]``. Either bound may be
    ``None`` to express a half-open range, but at least one must be set —
    a test with neither bound is vacuously satisfiable and carries no
    signal.

    This is the 6th first-class :class:`CandidateTest` variant, and the
    second variant after :class:`CandidateTestCustomSQL` to be **model-level
    only** (``column`` is hard-coded to ``None``). The diff emitter renders
    kept artifacts in the ``dbt_expectations`` namespace (DEC-002):
    ``{dbt_expectations.expect_table_row_count_to_be_between:
    {min_value: N, max_value: M, where: "..."}}`` — operators without
    ``dbt-expectations`` installed will see a clear ``dbt parse`` error.

    Sample-mode behaviour (DEC-003, corrected post-US-007a + post-QG): the
    prune compiler always emits a CTE-wrapped failing-rows SELECT of the
    form ``SELECT n FROM (SELECT COUNT(*) AS n FROM <table_ref>
    [WHERE <where>]) AS rc WHERE <bound-violation-predicate>`` regardless
    of ``prune.scope`` — a sampled ``COUNT(*)`` is semantically wrong. The
    prune engine's per-test loop **routes ``row_count_between`` past the
    materialised-sample substitution back to the source table** so the
    bounds verdict is correct at the default config (a COUNT(*) against a
    materialised sample returns the sample size, not the model's real row
    count). The COUNT(*) against the source is a single aggregate scan —
    cheap even on petabyte tables.

    Field naming (DEC-008): the Python-side fields are ``minimum`` /
    ``maximum`` (matching the prefix-free precedent set by ``values``,
    ``to``, ``field`` on existing variants). The ingest parser maps
    ``min_value`` / ``max_value`` from dbt-expectations YAML inbound; the
    diff emitter maps ``minimum`` / ``maximum`` → ``min_value`` /
    ``max_value`` outbound.
    """

    model_config = _BASE_CONFIG

    type: Literal["row_count_between"] = "row_count_between"
    column: None = None
    minimum: int | None = None
    maximum: int | None = None
    where: str | None = None
    rationale: str | None = None

    @field_validator("minimum")
    @classmethod
    def _minimum_non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("CandidateTestRowCountBetween.minimum must be >= 0 when set")
        return v

    @field_validator("maximum")
    @classmethod
    def _maximum_non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("CandidateTestRowCountBetween.maximum must be >= 0 when set")
        return v

    @field_validator("where")
    @classmethod
    def _where_non_empty_when_set(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError(
                "CandidateTestRowCountBetween.where must be non-empty after strip when set"
            )
        return v

    @model_validator(mode="after")
    def _bounds_consistent(self) -> CandidateTestRowCountBetween:
        if self.minimum is None and self.maximum is None:
            raise ValueError(
                "CandidateTestRowCountBetween requires at least one of "
                "(minimum, maximum) to be set — an unbounded row-count "
                "test carries no signal"
            )
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(
                "CandidateTestRowCountBetween.minimum "
                f"({self.minimum}) must be <= maximum ({self.maximum})"
            )
        return self

    def __repr__(self) -> str:
        """Redacted repr — omits the LLM-emitted ``where`` and ``rationale``
        (DEC-013 of #170, retroactive).

        The numeric bounds (``minimum`` / ``maximum``) stay visible: they're
        not value-bearing and answering "what does this test assert?" at a
        glance is operationally useful. The free-text ``where`` clause is a
        SQL fragment the LLM authored and is exactly the kind of payload the
        redaction exists to keep out of log sinks. Mirrors the precedent on
        :class:`PruneDecision` / :class:`GradingResult` / :class:`DiffEntry`.
        Full content remains accessible via :meth:`model_dump_json`.
        """
        return (
            "CandidateTestRowCountBetween(type='row_count_between', "
            "<model-level>, "
            f"minimum={self.minimum!r}, maximum={self.maximum!r})"
        )

    def __repr_args__(self) -> list[tuple[str | None, Any]]:
        """Redact via Pydantic's structured-repr hook (QG Pass 1 finding C1).

        See :meth:`CandidateTestCustomSQL.__repr_args__` for rationale.
        """
        return [
            ("type", self.type),
            ("column", self.column),
            ("minimum", self.minimum),
            ("maximum", self.maximum),
        ]


class CandidateTestUniqueCombination(BaseModel):
    """A model-level multi-column-uniqueness test (#170, DEC-001).

    Asserts that the tuple ``(c1, c2, ...)`` is unique across the model
    (optionally filtered by ``where``). The 7th first-class
    :class:`CandidateTest` variant, and the third — after
    :class:`CandidateTestCustomSQL` and :class:`CandidateTestRowCountBetween`
    — to be **model-level only** (``column`` is hard-coded to ``None``).

    The variant fills the gap between the single-column ``unique`` test and
    the free-form ``custom_sql`` escape hatch: composite uniqueness is a
    common business invariant (e.g. one row per ``(order_id, line_no)``,
    one row per ``(user_id, day)``) that the original four built-ins
    cannot express. Diff emission targets the ``dbt_utils.unique_combination_of_columns``
    macro (DEC-002 of #170): ``{dbt_utils.unique_combination_of_columns:
    {combination_of_columns: [c1, c2, ...]}}``.

    Cardinality (DEC-016): ``len(columns) >= 2`` — a single-column variant
    is just ``unique`` and carries no new signal; an empty-tuple variant
    is structurally meaningless. The no-duplicates invariant (DEC-016)
    rejects ``columns=("a", "a")`` and any other tuple with a repeated
    entry: a duplicate column compiles to a uniqueness test that always
    trivially has the same value in two positions; the LLM almost
    certainly meant something else.

    Per-column identifier shape validation is **deferred to the anchor-
    contract arm** (DEC-014; lands in US-004): Pydantic carries raw
    strings here, matching the ``accepted_values.values`` /
    ``relationships.to`` / ``.field`` precedent set on the existing
    variants. The compiler arm (US-005a) separately routes each
    ``columns[i]`` through ``validate_identifier`` + ``_fold_identifier``
    + ``_quote`` before quoting (defence-in-depth).
    """

    model_config = _BASE_CONFIG

    type: Literal["unique_combination"] = "unique_combination"
    column: None = None
    columns: tuple[str, ...]
    where: str | None = None
    rationale: str | None = None

    @field_validator("columns")
    @classmethod
    def _columns_min_two(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(v) < 2:
            raise ValueError(
                "CandidateTestUniqueCombination.columns must contain at least "
                f"two entries (got {len(v)}) — a single-column variant is just "
                "`unique` and carries no new signal"
            )
        return v

    @field_validator("where")
    @classmethod
    def _where_non_empty_when_set(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError(
                "CandidateTestUniqueCombination.where must be non-empty after strip when set"
            )
        return v

    @model_validator(mode="after")
    def _columns_no_duplicates(self) -> CandidateTestUniqueCombination:
        if len(set(self.columns)) != len(self.columns):
            raise ValueError(
                "CandidateTestUniqueCombination.columns must not contain "
                f"duplicates (got {list(self.columns)!r}) — a duplicate column "
                "compiles to a uniqueness test that always trivially has the "
                "same value in two positions"
            )
        return self

    def __repr__(self) -> str:
        """Redacted repr — omits the LLM-emitted ``where`` and ``rationale``
        (DEC-013 of #170).

        The ``columns`` tuple stays visible: column NAMES are not
        value-bearing and answering "which combination is asserted unique?"
        is operationally useful. The free-text ``where`` clause is a SQL
        fragment the LLM authored and is exactly the kind of payload the
        redaction exists to keep out of log sinks. Mirrors the precedent on
        :class:`PruneDecision` / :class:`GradingResult` / :class:`DiffEntry`.
        Full content remains accessible via :meth:`model_dump_json`.
        """
        return (
            "CandidateTestUniqueCombination(type='unique_combination', "
            f"<model-level>, columns={self.columns!r})"
        )

    def __repr_args__(self) -> list[tuple[str | None, Any]]:
        """Redact via Pydantic's structured-repr hook (QG Pass 1 finding C1).

        See :meth:`CandidateTestCustomSQL.__repr_args__` for rationale.
        """
        return [("type", self.type), ("column", self.column), ("columns", self.columns)]


class CandidateTestRowCountAnomalyByPeriod(BaseModel):
    """A model-level per-period row-count-anomaly test (#171, DEC-007).

    Buckets the model's rows by ``date_column`` truncated to ``period``
    (``hour`` / ``day`` / ``week``), then asserts each bucket's row count
    against an anomaly band derived from the previous ``lookback_periods``
    buckets via the selected statistical ``method`` (``mad`` /
    ``zscore`` / ``percentile`` / ``min_max``). The 8th first-class
    :class:`CandidateTest` variant, and the fourth — after
    :class:`CandidateTestCustomSQL`, :class:`CandidateTestRowCountBetween`,
    and :class:`CandidateTestUniqueCombination` — to be **model-level
    only** (``column`` is hard-coded to ``None``).

    The variant catches the volume-anomaly class of pipeline failure that
    a static :class:`CandidateTestRowCountBetween` band cannot: an
    incremental fact table whose daily load suddenly drops to 1% or
    spikes to 10× the rolling baseline. ``seasonality="dow"`` opt-in
    compares each weekday only to other instances of the same weekday in
    the lookback window — the right shape for a business-calendar grain
    where Mondays and Saturdays are systematically different.

    Method semantics (DEC-007):

    * ``mad`` — Median Absolute Deviation. Robust against outliers.
      ``threshold`` is the number of MAD multiples (default ``3.0``) that
      bound the band: ``[median - threshold * MAD, median + threshold * MAD]``.
    * ``zscore`` — Standard z-score. Sensitive to outliers (which is
      sometimes what you want). ``threshold`` is the number of standard
      deviations.
    * ``percentile`` — Tukey-style percentile band. ``threshold`` is the
      IQR multiplier (default ``3.0``).
    * ``min_max`` — Bound by the literal min/max of the lookback window.
      ``threshold`` is **ignored** — any value (including ``0`` and
      negatives) is accepted but unused. Use when you want to catch any
      excursion beyond the historical envelope, no margin.

    ``min_samples_per_bucket`` (default ``3``) is the floor on observed
    samples per seasonality bucket before the test will score that
    bucket; under-sampled buckets degrade silently (``kept-without-evidence``).

    Per-field shape validation (defence in depth) is deferred to the
    anchor-contract arm (US-006) and the compile arm (US-008): Pydantic
    carries raw strings here, matching the precedent set on the existing
    variants. ``where`` is a SQL fragment whose type-coherence is
    validated by ``_check_custom_sql_type_coherence`` (#159) at parse
    time.
    """

    model_config = _BASE_CONFIG

    type: Literal["row_count_anomaly_by_period"] = "row_count_anomaly_by_period"
    column: None = None
    date_column: str
    period: Literal["hour", "day", "week"] = "day"
    lookback_periods: int = 28
    method: Literal["mad", "zscore", "percentile", "min_max"] = "mad"
    seasonality: Literal["none", "dow"] = "none"
    threshold: float = 3.0
    min_samples_per_bucket: int = 3
    where: str | None = None
    rationale: str | None = None

    @model_validator(mode="after")
    def _validate_fields(self) -> CandidateTestRowCountAnomalyByPeriod:
        if not self.date_column.strip():
            raise ValueError("CandidateTestRowCountAnomalyByPeriod.date_column must be non-empty")
        if self.lookback_periods < 1:
            raise ValueError(
                "CandidateTestRowCountAnomalyByPeriod.lookback_periods must be >= 1 "
                f"(got {self.lookback_periods})"
            )
        if self.min_samples_per_bucket < 1:
            raise ValueError(
                "CandidateTestRowCountAnomalyByPeriod.min_samples_per_bucket must be >= 1 "
                f"(got {self.min_samples_per_bucket})"
            )
        # `min_max` ignores threshold (DEC-007) — accept any value
        # including 0 and negatives; the compile arm will not consume it.
        if self.method != "min_max" and self.threshold <= 0:
            raise ValueError(
                "CandidateTestRowCountAnomalyByPeriod.threshold must be > 0 for "
                f"method={self.method!r} (got {self.threshold!r}); `min_max` is the "
                "only method that ignores `threshold`"
            )
        if self.where is not None and not self.where.strip():
            raise ValueError(
                "CandidateTestRowCountAnomalyByPeriod.where must be non-empty after strip when set"
            )
        return self

    def __repr__(self) -> str:
        """Redacted repr — shows only ``(type, column, method, seasonality)``;
        omits the LLM-emitted ``where`` and ``rationale`` (DEC-013 of #170).

        ``method`` and ``seasonality`` are operationally useful (which
        anomaly recipe is this?) and not value-bearing. ``date_column`` /
        ``period`` / ``lookback_periods`` / ``threshold`` /
        ``min_samples_per_bucket`` are also non-secret but omitted from
        the casual debug-print path to keep the repr compact; full
        content remains accessible via :meth:`model_dump_json`.

        Mirrors the precedent on :class:`CandidateTestCustomSQL` /
        :class:`CandidateTestRowCountBetween` /
        :class:`CandidateTestUniqueCombination`.
        """
        return (
            "CandidateTestRowCountAnomalyByPeriod("
            f"type='row_count_anomaly_by_period', <model-level>, "
            f"method={self.method!r}, seasonality={self.seasonality!r})"
        )

    def __repr_args__(self) -> list[tuple[str | None, Any]]:
        """Redact via Pydantic's structured-repr hook (QG Pass 1 finding C1).

        See :meth:`CandidateTestCustomSQL.__repr_args__` for rationale.
        ``rich.print()`` / ``devtools.pretty()`` / ``pprint`` reach through
        ``__repr_args__`` rather than ``repr()``, so the override here
        closes the redaction across all structured-debug surfaces in
        lockstep with ``__repr__``.
        """
        return [
            ("type", self.type),
            ("column", self.column),
            ("method", self.method),
            ("seasonality", self.seasonality),
        ]


CandidateTest = Annotated[
    CandidateTestNotNull
    | CandidateTestUnique
    | CandidateTestAcceptedValues
    | CandidateTestRelationships
    | CandidateTestCustomSQL
    | CandidateTestRowCountBetween
    | CandidateTestUniqueCombination
    | CandidateTestRowCountAnomalyByPeriod,
    Field(discriminator="type"),
]
"""Discriminated union over the eight test variants (DEC-003 / DEC-002 /
#169 DEC-001 / #170 DEC-001 / #171 DEC-007).

The discriminator field is ``type``; its value space is the closed
:class:`Literal` union of the eight variant strings. Unknown ``type``
values raise :class:`pydantic.ValidationError` at construction — adding
a ninth test variant requires extending this union and the
``Literal`` on each variant class. The drift detector (US-014) catches
the case where a fixture grows a new test type without the model.
"""


class CandidateColumn(BaseModel):
    """One column on a candidate schema.

    Carries the per-column ``description`` and ``rationale`` that the
    LLM produced, plus zero or more column-scoped tests. ``meta`` is a
    free-form dict reserved for fields the LLM emits but the prune layer
    does not yet consume; it survives the round-trip but is not validated.
    """

    model_config = _BASE_CONFIG

    name: str
    description: str
    rationale: str | None = None
    tests: tuple[CandidateTest, ...] = ()
    meta: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("CandidateColumn.name must be non-empty")
        return v


class CandidateSchema(BaseModel):
    """The candidate schema returned by the LLM drafter (US-008).

    ``schema_version`` is frozen at ``1`` for v0.1; future on-disk
    consumers branch on this. The ``tests`` tuple at this level holds
    *model-level* tests (e.g., a uniqueness assertion across the row),
    distinct from per-column tests on each :class:`CandidateColumn`.
    """

    model_config = _BASE_CONFIG

    schema_version: Literal[1] = 1
    name: str
    description: str
    rationale: str | None = None
    columns: tuple[CandidateColumn, ...]
    tests: tuple[CandidateTest, ...] = ()

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("CandidateSchema.name must be non-empty")
        return v


__all__ = (
    "CandidateColumn",
    "CandidateSchema",
    "CandidateTest",
    "CandidateTestAcceptedValues",
    "CandidateTestCustomSQL",
    "CandidateTestNotNull",
    "CandidateTestRelationships",
    "CandidateTestRowCountAnomalyByPeriod",
    "CandidateTestRowCountBetween",
    "CandidateTestUnique",
    "CandidateTestUniqueCombination",
)
