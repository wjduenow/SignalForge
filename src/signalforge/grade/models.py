"""Typed read-back shapes for the grader (US-002).

Defines the read-back-stable Pydantic v2 models the grader emits to its
callers and to its on-disk audit log:

* :class:`GradingResult` — one verdict per ``(artifact, criterion)``
  pair (DEC-002, DEC-018).
* :class:`GradingReport` — the public sidecar shape, with aggregate
  computed fields (DEC-002, DEC-015, DEC-020).
* :class:`GradeEvent` — one JSONL audit record per LLM-judge call
  (DEC-006, DEC-010, DEC-019, DEC-020).

Every read-back model uses ``extra="ignore"`` per
``docs/rules/manifest-readers.md`` (forward-compat), pairs with a
``Strict<X>(extra="forbid")`` mirror in
``tests/grade/test_drift_detector.py`` validated against committed
fixtures, and exposes a minimal ``__repr__`` per DEC-022 of issue #6 so
an accidental ``_LOGGER.warning("report: %s", report)`` cannot dump
multi-megabyte LLM evidence / reasoning fields into log sinks.

DEC-009 (``artifact_id`` canonical dotted-path format) is consumed here
as a plain ``str`` field — the ``_artifact_id_for(...)`` formatter
helper itself ships with the orchestrator (US-008). Drift on that
format is caught downstream when the helper produces a string that
doesn't match ``^(column|test|model)\\.``.

DEC-015 (retry-exhaustion graceful degrade) is operationalised by
``score: float | None`` plus the field-validator that allows ``None``
but rejects out-of-range / non-finite floats. Aggregate computed fields
on :class:`GradingReport` skip null scores so a single criterion's
exhaustion does not poison the report.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, computed_field, field_serializer, field_validator

from signalforge._common.timestamp import iso8601_z

_BASE_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

_ONE_LINE_WHY_CAP: int = 120
"""Maximum characters surfaced by :attr:`GradingResult.one_line_why`.

Diff renderer (#8) consumes the property directly; the cap keeps a
single-line summary readable inside a column-width-bounded diff.
"""


class GradingResult(BaseModel):
    """One verdict per ``(artifact, criterion)`` pair.

    The ``artifact_id`` field carries the canonical dotted-path string
    documented in DEC-009 (e.g. ``"column.email.description"``,
    ``"test.column.user_id.not_null"``). This model treats it as an
    opaque ``str`` — the formatter helper ships with the orchestrator
    in US-008.

    DEC-015 — the degraded path: when the LLM judge exhausts retries or
    the total budget trips before a pair runs, the orchestrator records
    ``score=None, passed=False, evidence="", reasoning="..."`` rather
    than failing the whole report. Aggregate computed fields on
    :class:`GradingReport` skip null scores so partial-run economics
    stay correct.

    DEC-022 of #6 — the custom ``__repr__`` collapses to identity +
    score + pass-flag so accidental ``_LOGGER`` interpolations cannot
    dump multi-paragraph ``evidence`` / ``reasoning`` content to log
    sinks. Callers that genuinely need the full body call
    :meth:`pydantic.BaseModel.model_dump`.
    """

    model_config = _BASE_CONFIG

    artifact_id: str
    criterion_id: str
    score: float | None
    passed: bool
    evidence: str = ""
    reasoning: str = ""

    @field_validator("score")
    @classmethod
    def _score_in_range_or_none(cls, value: float | None) -> float | None:
        """Reject NaN/inf and out-of-range non-None values; allow ``None``.

        ``None`` is the documented degraded-path sentinel (DEC-015) — a
        criterion that exhausted retries or hit the total budget before
        evaluation lands here. A non-None score must be a finite real
        number in ``[0.0, 1.0]``.
        """
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError(
                f"score must be a finite real number in [0.0, 1.0] or None; got {value!r}"
            )
        if value < 0.0 or value > 1.0:
            raise ValueError(f"score must be in [0.0, 1.0] or None; got {value!r}")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def one_line_why(self) -> str:
        """First sentence of :attr:`reasoning`, capped at 120 chars (DEC-018).

        Splits on the literal ``". "`` (period + space) and returns the
        first segment, capped at :data:`_ONE_LINE_WHY_CAP`. If no such
        sentence boundary exists, falls back to the leading
        ``_ONE_LINE_WHY_CAP`` characters of :attr:`reasoning`. Empty
        ``reasoning`` returns the empty string.

        The diff renderer (#8) consumes this property directly so
        display logic stays in the data layer (Architectural Commitment
        #5 — explainable diffs).
        """
        if not self.reasoning:
            return ""
        sentinel = ". "
        idx = self.reasoning.find(sentinel)
        if idx == -1:
            return self.reasoning[:_ONE_LINE_WHY_CAP]
        first = self.reasoning[: idx + 1]  # keep the period itself
        return first[:_ONE_LINE_WHY_CAP]

    def __repr__(self) -> str:
        """Minimal repr — omits ``evidence`` and ``reasoning``.

        DEC-022 of issue #6: an accidental
        ``_LOGGER.warning("result: %s", result)`` would otherwise emit
        the full LLM-judge reasoning paragraph, which can include
        verbatim sample-row content quoted by the judge. The custom
        repr collapses to identity + score + pass-flag; full bodies
        remain accessible via ``result.model_dump()``.
        """
        return (
            f"GradingResult(artifact_id={self.artifact_id!r}, "
            f"criterion_id={self.criterion_id!r}, "
            f"score={self.score!r}, passed={self.passed!r})"
        )


class GradingReport(BaseModel):
    """Sidecar shape — the grader's public output for one model.

    Carried verbatim into the on-disk JSON sidecar (DEC-012) and into
    the diff-renderer (#8) input. ``rubric_hash`` is the 16-hex blake2b-8
    digest of the canonical rubric JSON (DEC-010); ``run_id`` is the
    uuid4 hex generated once at orchestrator entry (DEC-020) and is
    repeated on every :class:`GradeEvent` so reviewers can correlate
    sidecar to JSONL without timestamp ranges.

    Aggregate computed fields:

    * :attr:`pass_rate` — mean of ``passed`` over results with a
      non-null score (DEC-015 — null scores are degraded path and are
      skipped by aggregation).
    * :attr:`mean_score` — mean of ``score`` over results with a
      non-null score.
    * :attr:`aggregate_complete` — ``True`` iff every result has a
      non-null score; ``False`` signals to the diff renderer that the
      aggregate is partial.
    * :attr:`passed` — ``pass_rate >= thresholds[0]`` AND
      ``mean_score >= thresholds[1]`` (DEC-002 — clauditor's aggregate
      hard cutoff).

    DEC-022 of #6 — minimal ``__repr__`` collapses to identity +
    aggregate counts; the per-result payload stays out of accidental
    log lines.
    """

    model_config = _BASE_CONFIG

    grade_schema_version: Literal[1] = 1
    signalforge_version: str
    run_id: str
    timestamp: datetime
    duration_seconds: float
    model_unique_id: str
    rubric_hash: str
    thresholds: tuple[float, float]
    results: tuple[GradingResult, ...]

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return iso8601_z(value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pass_rate(self) -> float:
        """Mean of ``passed`` over scored results; ``0.0`` if none scored.

        Skips null-score results (DEC-015 — degraded path) so a single
        criterion's retry-exhaustion does not silently lower the rate
        of the criteria that did run successfully. Returns ``0.0`` when
        every result is degraded or the report carries no results.
        """
        scored = tuple(r for r in self.results if r.score is not None)
        if not scored:
            return 0.0
        return sum(1 for r in scored if r.passed) / len(scored)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mean_score(self) -> float:
        """Mean of ``score`` over scored results; ``0.0`` if none scored."""
        scored = tuple(r.score for r in self.results if r.score is not None)
        if not scored:
            return 0.0
        return sum(scored) / len(scored)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def aggregate_complete(self) -> bool:
        """``True`` iff every result has a non-null score (DEC-015).

        The diff renderer (#8) flags partial aggregates explicitly so
        operators don't mistake a degraded-path mean for a real one.
        Returns ``True`` for an empty results tuple (vacuous truth) —
        no degraded results means complete.
        """
        return all(r.score is not None for r in self.results)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        """Aggregate hard cutoff (DEC-002).

        ``True`` iff both :attr:`pass_rate` and :attr:`mean_score` meet
        or exceed their respective threshold in :attr:`thresholds`.
        Mirrors clauditor's threshold-AND semantics; the diff renderer
        rescales to 0–5 stars at display time but the data layer stays
        in clauditor shape.
        """
        min_pass_rate, min_mean_score = self.thresholds
        return self.pass_rate >= min_pass_rate and self.mean_score >= min_mean_score

    def __repr__(self) -> str:
        """Minimal repr — omits the per-result payload (DEC-022 of #6).

        The full ``results`` tuple remains accessible via
        ``report.results`` / ``report.model_dump()`` — the custom repr
        only protects accidental ``_LOGGER`` interpolations.
        """
        return (
            f"GradingReport(model_unique_id={self.model_unique_id!r}, "
            f"results_count={len(self.results)}, "
            f"pass_rate={self.pass_rate!r}, "
            f"mean_score={self.mean_score!r}, "
            f"passed={self.passed!r}, "
            f"aggregate_complete={self.aggregate_complete!r}, "
            f"duration_seconds={self.duration_seconds!r})"
        )


class GradeEvent(BaseModel):
    """One JSONL audit record per LLM-judge call.

    Mirrors the fail-closed JSONL convention established by
    :class:`signalforge.safety.request.AuditEvent` and
    :class:`signalforge.draft.audit.LLMResponseEvent`. Read-back-stable
    (``extra="ignore"``) — older readers tolerate forward-compat field
    additions, while the one-off ``Strict<GradeEvent>(extra="forbid")``
    drift detector catches silent schema expansion before a live audit
    log does.

    Reproducibility fields (DEC-010, DEC-019, DEC-020):

    * :attr:`rubric_hash` — 16-hex ``blake2b-8`` of the canonical
      rubric JSON. Carried on every event AND on the sidecar
      :class:`GradingReport`.
    * :attr:`prompt_version_template` — 16-hex ``blake2b-8`` of the
      system prompt + cached rubric block + envelope tag. Constant
      across all criteria of one run.
    * :attr:`criterion_prompt_hash` — 16-hex ``blake2b-8`` of the
      per-criterion prompt fragment. Stable across artifacts of one
      run.
    * :attr:`run_id` — uuid4 hex generated once at orchestrator entry
      (DEC-020); ties JSONL records to their sidecar report.

    DEC-015 sentinel: when retries exhaust before a response is
    captured, :attr:`response_text_hash` is the empty string
    (no response text to hash) and :attr:`score` is ``None``.
    """

    model_config = _BASE_CONFIG

    audit_schema_version: Literal[1] = 1
    signalforge_version: str
    run_id: str
    timestamp: datetime
    model_unique_id: str
    artifact_id: str
    criterion_id: str
    score: float | None
    passed: bool
    evidence: str = ""
    reasoning: str = ""
    rubric_hash: str
    prompt_version_template: str
    criterion_prompt_hash: str
    response_text_hash: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return iso8601_z(value)

    @field_validator("score")
    @classmethod
    def _score_in_range_or_none(cls, value: float | None) -> float | None:
        """Reject NaN/inf and out-of-range non-None values; allow ``None``.

        Mirrors :meth:`GradingResult._score_in_range_or_none` — the
        audit log carries the same score-shape contract as the
        in-memory result so a JSONL replay round-trips cleanly.
        """
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError(
                f"score must be a finite real number in [0.0, 1.0] or None; got {value!r}"
            )
        if value < 0.0 or value > 1.0:
            raise ValueError(f"score must be in [0.0, 1.0] or None; got {value!r}")
        return value


__all__ = (
    "GradeEvent",
    "GradingReport",
    "GradingResult",
)
