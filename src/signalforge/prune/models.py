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

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, computed_field, field_serializer

from signalforge.draft import CandidateTest
from signalforge.prune.stats import AnomalyTestStats

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
    bypassed_to_source: bool = False
    """Whether this test was routed PAST the sample to the source table
    (issue #268, DEC-011).

    :attr:`scope` is copied verbatim from ``prune.scope``, so a test that
    bypassed the sample is still recorded as ``scope="sample"`` — and its
    :attr:`why` may even read "on 0 sample rows". Without this field a
    genuinely **sampled** test and a **bypassed** one are indistinguishable
    to a reviewer, which cuts against Architectural Commitment #5
    ("explainable diffs").

    ``True`` for the metadata-aggregate variants under a sample scope
    (``row_count_between`` / ``unique_combination`` /
    ``row_count_anomaly_by_period`` — which have silently carried this lie
    since #169: a ``COUNT(*)`` against a sample is semantically meaningless,
    so they always bypass) and for a manifest-ingested ``custom_sql`` whose
    body could not be safely rewritten onto the sample relation. ``False``
    under ``scope="full"`` (there is no sample to bypass), for row-level
    tests that genuinely ran against the sample, and for decisions taken
    before any routing happened (prune disabled, budget exhausted, sample
    materialisation failed).

    Set at the decision site in :mod:`signalforge.prune.engine` from the
    same :func:`~signalforge.prune.engine._test_requires_source_table`
    predicate that computes the per-test table ref, so the audit field and
    the actual routing cannot drift."""
    as_of: date | None = None
    """Evaluation date for time-bound prune decisions (issue #171, DEC-006).

    Threaded through from ``prune_tests(as_of=...)`` for the
    ``row_count_anomaly_by_period`` variant (the first SignalForge primitive
    whose decision is inherently time-bound). ``None`` for every other test
    variant — they are time-invariant by Architectural Commitment #5. When
    set, serialises as ``YYYY-MM-DD`` ISO 8601 string via the
    ``@field_serializer`` below (not via :func:`signalforge._common.timestamp.iso8601_z`
    — that helper is :class:`datetime`-only per safety-layer.md issue #56)."""
    stats: AnomalyTestStats | None = None
    """Per-decision numerical state from the anomaly-stats query (issue
    #171, DEC-006). Populated only for the ``row_count_anomaly_by_period``
    variant; ``None`` for every other test type. The discriminated-union
    serialisation (DEC-005 of #171, the ``method`` discriminator) is
    handled natively by Pydantic v2 — emits the discriminator field as part
    of the dict on ``model_dump`` / ``model_dump_json``."""

    @field_serializer("as_of")
    def _serialize_as_of(self, value: date | None) -> str | None:
        """Render ``as_of`` as ``YYYY-MM-DD`` ISO 8601 string.

        Mirrors :class:`signalforge.prune.audit.PruneEvent`'s same serializer
        exactly. The :mod:`signalforge._common.timestamp` helper
        (``iso8601_z``) is deliberately :class:`datetime`-only per
        safety-layer.md issue #56 — :class:`date` has no time-of-day
        component and the canonical timestamp shape (``...Z`` suffix) does
        not apply. Pydantic v2's native :class:`date` JSON serialisation
        already emits ``YYYY-MM-DD``, but the explicit serializer documents
        the contract and keeps both the audit-event and read-back models
        rendering identically.
        """
        return value.isoformat() if value is not None else None

    def __repr__(self) -> str:
        """Redacted repr — omits ``compiled_sql``, ``sample_failures``,
        ``stats``, and ``as_of`` (DEC-022 / issue #171 DEC-006).

        Pydantic v2's default ``__repr__`` interpolates every field. The
        per-decision compiled SQL (potentially multi-line, may quote
        upstream column data via the model SQL it scans), sampled-failure
        rows (which may contain PII), per-decision numerical stats (the
        anomaly variant's lookback bands are not load-bearing in casual
        logs and may carry per-period DOW breakdowns), and the time-bound
        ``as_of`` (operator-visible via the audit JSONL where it matters)
        are all elided. Mirrors :class:`PruneResult.__repr__` (DEC-022)
        and :class:`signalforge.draft.models.CandidateTestCustomSQL.__repr__`
        (DEC-013 of #170). Full content stays accessible via
        :meth:`pydantic.BaseModel.model_dump` /
        :meth:`pydantic.BaseModel.model_dump_json` — only the casual
        debug-print path is redacted.
        """
        return (
            f"PruneDecision(test_anchor={self.test_anchor!r}, "
            f"decision={self.decision!r}, "
            f"reason={self.reason!r}, "
            f"failures={self.failures}, "
            f"scope={self.scope!r}, "
            f"bypassed_to_source={self.bypassed_to_source}, "
            f"elapsed_ms={self.elapsed_ms})"
        )

    def __repr_args__(self) -> list[tuple[str | None, Any]]:
        """Redact via Pydantic's structured-repr hook.

        Per memory ``pydantic-v2-repr-args-redaction-required`` + DEC-013
        of #170: ``__repr__`` redacts the ``%s``-interpolation path, but
        ``rich.print()`` / ``devtools.pretty()`` / ``pprint`` reach through
        ``__repr_args__`` and would otherwise still see ``compiled_sql``,
        ``sample_failures``, ``stats``, ``as_of``. Filtering here closes
        the leak across all three structured-debug surfaces with one
        override.
        """
        return [
            ("test_anchor", self.test_anchor),
            ("decision", self.decision),
            ("reason", self.reason),
            ("failures", self.failures),
            ("scope", self.scope),
            ("bypassed_to_source", self.bypassed_to_source),
            ("elapsed_ms", self.elapsed_ms),
        ]


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
