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

from pydantic import BaseModel, ConfigDict, Field, field_validator

_BASE_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


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


CandidateTest = Annotated[
    CandidateTestNotNull
    | CandidateTestUnique
    | CandidateTestAcceptedValues
    | CandidateTestRelationships,
    Field(discriminator="type"),
]
"""Discriminated union over the four test variants (DEC-003).

The discriminator field is ``type``; its value space is the closed
:class:`Literal` union of the four variant strings. Unknown ``type``
values raise :class:`pydantic.ValidationError` at construction — adding
a fifth test variant requires extending this union and the
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

    schema_version: int = 1
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
    "CandidateTestNotNull",
    "CandidateTestRelationships",
    "CandidateTestUnique",
)
