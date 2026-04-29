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
    """Warehouse-flavour capability flags.

    The adapter consults these flags rather than hard-coding warehouse names
    so the v0.2 Snowflake/Postgres ports can add a sibling constant without
    branching the adapter logic on ``isinstance``.
    """

    name: str
    supports_tablesample: bool
    supports_qualify: bool
    quote_char: str
    identifier_case: Literal["upper", "lower", "preserve"]


BIGQUERY_DIALECT = Dialect(
    name="bigquery",
    supports_tablesample=True,
    supports_qualify=True,
    quote_char="`",
    identifier_case="preserve",
)


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
        # ``project`` follows GCP's hyphen-permissive grammar; ``dataset``
        # and ``name`` use the strict identifier regex (BigQuery rejects
        # hyphens in unquoted dataset / table names anyway).
        from signalforge.warehouse._sql_safety import (
            validate_identifier,
            validate_project_id,
        )

        if self.project is not None:
            validate_project_id("project", self.project)
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
    "PartitionFilter",
    "TableRef",
    "TestResult",
]
