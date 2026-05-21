"""Anchor-contract validator for the ingest layer (US-004).

Verifies that every column and test in a built
:class:`signalforge.draft.models.CandidateSchema` references a column that
actually exists on the target ``Model`` (the caller supplies that column
set as a ``frozenset[str]`` — typically ``frozenset(model.columns.keys())``).

DEC-002: a test referencing a column absent from the ``Model`` means the
ingested YAML is stale or wrong vs. the manifest. This is a correctness
error the operator must fix — fail loud. Distinct from *unsupported test
types*, which are skip+record (DEC-003).

DEC-007: the check is re-implemented in-layer rather than importing
``signalforge.draft.parser._validate_anchor_contract``. That validator
raises the LLM-typed :class:`LLMOutputAnchorContractError`, which would
surface an LLM-taxonomy error from a non-LLM path and create a cross-layer
``ingest → draft`` import. The body is small (set-membership + per-test
checks); duplication is cheaper than the taxonomy smell. The phrasing
mirrors the drafter's validator so the two read consistently.

Whole-file fail-loud: the validator collects **every** violation rather
than short-circuiting on the first, so the operator can fix the schema in
a single pass. A clean candidate raises nothing.

This module emits **no logging** (manifest-readers.md rule #4 — reader /
parser modules are deterministic and stay silent; observability lives in
the consuming prune / grade stages).
"""

from __future__ import annotations

from signalforge.draft.models import CandidateSchema
from signalforge.ingest.errors import IngestAnchorContractError


def validate_anchor_contract(
    candidate: CandidateSchema,
    model_columns: frozenset[str],
) -> None:
    """Verify ``candidate`` references only columns present on the model.

    Walks the candidate schema collecting every anchor-contract violation
    (never short-circuits on the first). Raises one
    :class:`IngestAnchorContractError` whose ``violations`` tuple lists all
    of them when any exist; returns ``None`` when the candidate is clean.

    Checks performed:

    * Each :class:`signalforge.draft.models.CandidateColumn` name must
      reference a real column in ``model_columns``.
    * Each per-column test must carry ``test.column == column.name`` (a
      column-scoped test citing a different column would land under the
      wrong YAML key).
    * Each per-column test's ``column`` must reference a real column in
      ``model_columns``.
    * Each model-level test's ``column`` must reference a real column in
      ``model_columns``.
    """
    violations: list[str] = []

    for column in candidate.columns:
        if column.name not in model_columns:
            violations.append(
                f"CandidateColumn references nonexistent column {column.name!r} "
                f"(available: {sorted(model_columns)})"
            )
        for test in column.tests:
            if test.column != column.name:
                violations.append(
                    f"column test on column={column.name!r} references {test.column!r}"
                )
            if test.column not in model_columns:
                violations.append(
                    f"test references nonexistent column {test.column!r} "
                    f"(available: {sorted(model_columns)})"
                )

    for test in candidate.tests:
        if test.column not in model_columns:
            violations.append(f"model-level test references nonexistent column {test.column!r}")

    if violations:
        raise IngestAnchorContractError(tuple(violations))


__all__ = ("validate_anchor_contract",)
