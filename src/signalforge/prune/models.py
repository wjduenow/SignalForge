"""Typed read-back shapes for the prune layer (US-004).

Defines the read-back-stable Pydantic models the prune engine returns
to its callers: :class:`PruneResult` and :class:`PruneDecision`, plus
the :data:`DropReason` and :data:`Scope` discriminator literals.
Downstream stages — grader (#7), diff renderer (#8), CLI (#9) —
consume these shapes; v0.2 readers MAY also load the persisted JSON
representation back into these models, so forward-compat matters.

Design commitments operationalised here:

* **DEC-003** — No standalone ``KeptTest`` / ``DroppedTest`` types.
  Filter views (``kept_decisions`` / ``dropped_decisions``) and the
  count aggregates are :func:`pydantic.computed_field` properties
  derived from the canonical ``decisions`` tuple, so a renderer that
  builds a :class:`PruneResult` from a JSONL log gets the same view
  as a freshly produced one.
* **DEC-004** — :attr:`PruneDecision.test` is the typed
  :data:`signalforge.draft.CandidateTest` discriminated union, not a
  loose ``dict[str, Any]``. The grader (#7) and diff renderer (#8)
  reuse the drafter's per-variant display logic; v0.1 readers fail
  loud on a v0.2 test type they don't recognise.
* **DEC-005** — ``compiled_sql_hash`` is the hex digest of
  ``blake2b(sql.encode(), digest_size=8)`` (16 hex characters),
  matching the precedent established by
  :class:`signalforge.draft.LLMResponseEvent`.
* **DEC-014 / DEC-015** — :class:`PruneResult` carries
  :attr:`prune_schema_version` (``Literal[1]``) so on-disk JSON
  consumers can branch on shape changes; field *additions* are
  handled by ``extra="ignore"`` plus a one-off ``extra="forbid"``
  drift detector (US-010). Read-back semantics — no ``extra="forbid"``
  on these models, that's reserved for config-shaped models per
  ``.claude/rules/safety-layer.md``.
* **Transitive immutability** — sequences are :class:`tuple` rather
  than :class:`list` so a caller cannot mutate ``decisions`` after
  construction; ``frozen=True`` blocks attribute reassignment.

This module declares only data shapes. The compilation of a
:class:`signalforge.draft.CandidateTest` to SQL, the warehouse
execution that produces ``failures``, and the always-passes / clean-data
verdict logic all live in sibling modules under
:mod:`signalforge.prune` and are not part of this US.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, computed_field

from signalforge.draft import CandidateTest

DropReason = Literal[
    "always-passes",
    "requires-future-data",
    "failed-on-known-clean-data",
    "kept",
    "kept-without-evidence",
]
"""Closed set of verdict reasons emitted by the prune engine.

The four ``dropped`` reasons cover the noise-direction splits called
out in :file:`CLAUDE.md` (always-pass tests AND tests that fail on
known-clean data are both dropped). The two ``kept`` reasons cover
the with-evidence and without-evidence cases — the latter happens
when the warehouse sample is too small to support a verdict but the
test references real columns and is well-formed.
"""

Scope = Literal["sample", "full"]
"""Whether ``failures`` was measured against a sampled or full scan.

When ``scope == "full"``, :attr:`PruneDecision.sampled_rows` is
``None`` because every row in the model was inspected.
"""

_BASE_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class PruneDecision(BaseModel):
    """One verdict per candidate test.

    Carries the original :data:`signalforge.draft.CandidateTest` (the
    typed discriminated union, not a loose dict) so #7/#8 can reuse the
    drafter's per-variant display logic and v0.1 readers fail loud on a
    v0.2 test type. ``test_anchor`` is ``"column.<col_name>"`` for
    column-scoped tests and the literal string ``"model"`` for
    model-level tests.

    Hash conventions (DEC-005): ``compiled_sql_hash`` is
    ``blake2b(sql.encode(), digest_size=8).hexdigest()`` — 16 hex
    characters, matching the precedent set by
    :class:`signalforge.draft.LLMResponseEvent`.
    """

    model_config = _BASE_CONFIG

    test_anchor: str
    test: CandidateTest
    decision: Literal["kept", "dropped"]
    reason: DropReason
    failures: int
    sampled_rows: int | None
    scope: Scope
    elapsed_ms: int
    compiled_sql_hash: str
    compiled_sql: str
    why: str
    sample_failures: tuple[dict[str, Any], ...] | None = None


class PruneResult(BaseModel):
    """Aggregate result of pruning all candidates for one model.

    ``prune_schema_version`` is bumped only when the persisted JSON /
    JSONL shape changes; field *additions* are handled by
    ``extra="ignore"`` (DEC-015) plus the one-off ``extra="forbid"``
    drift detector that lands in US-010. The kept / dropped views and
    count aggregates are :func:`pydantic.computed_field` properties
    derived from ``decisions`` (DEC-003), so a result reconstructed from
    a JSONL log carries identical views to a freshly produced one.
    """

    model_config = _BASE_CONFIG

    prune_schema_version: Literal[1] = 1
    model_unique_id: str
    decisions: tuple[PruneDecision, ...]
    elapsed_ms: int
    signalforge_version: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def kept_decisions(self) -> tuple[PruneDecision, ...]:
        return tuple(d for d in self.decisions if d.decision == "kept")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dropped_decisions(self) -> tuple[PruneDecision, ...]:
        return tuple(d for d in self.decisions if d.decision == "dropped")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def kept_count(self) -> int:
        return len(self.kept_decisions)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dropped_count(self) -> int:
        return len(self.dropped_decisions)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tests(self) -> int:
        return len(self.decisions)

    def __repr__(self) -> str:
        """Redacted repr — omits per-decision SQL and sample-failure rows.

        DEC-022: Pydantic's default ``__repr__`` would interpolate every
        field including ``decisions[i].compiled_sql``, ``decisions[i].why``,
        and ``decisions[i].sample_failures`` into a single line. An
        accidental ``_LOGGER.warning("result: %s", result)`` would dump
        compiled SQL plus sampled rows (which may contain PII) into log
        sinks. The custom repr collapses to the top-level identity and
        the two count aggregates so log lines stay safe by default;
        callers that genuinely need the full body call
        :meth:`pydantic.BaseModel.model_dump` explicitly.
        """
        return (
            f"PruneResult(model_unique_id={self.model_unique_id!r}, "
            f"kept_count={self.kept_count}, "
            f"dropped_count={self.dropped_count}, "
            f"elapsed_ms={self.elapsed_ms})"
        )


__all__ = (
    "DropReason",
    "PruneDecision",
    "PruneResult",
    "Scope",
)
