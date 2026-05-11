"""SignalForge quality grader — score LLM-drafted artefacts via LLM-as-judge.

This subpackage implements the README's "evaluation in the loop" architectural
commitment at the post-prune boundary: every kept candidate (column doc, test,
or model description) goes through a configurable rubric and is scored by a
second LLM call. Rubric design and the judge prompt template land in later
US-00x stories; this scaffold only defines the typed exception hierarchy.

**Safety boundary, by design (DEC-013).** The PII safety redaction boundary
established by issue #4 closed at *draft time* — `signalforge.safety` redacts
column names and values before the drafting LLM call, and the drafter's
`<MODEL_SQL>` envelope (DEC-007 of #5) is the prompt-injection defence on the
input side. Post-draft, :class:`signalforge.draft.CandidateSchema` carries
*real* column names; the grader sends those real names to the LLM-judge by
design, and writes them into the sidecar JSON the operator reviews. The
`<ARTIFACT>` envelope is the only LLM-prompt defence applied inside the
grader. Re-redaction here would defeat both the rubric (judges need real
names to score documentation quality) and the explainable-diffs commitment
(reviewers need to see what was scored).

See ``plans/super/7-quality-grader.md`` for the full design and the DEC log.
"""

from __future__ import annotations

from signalforge.grade.config import GradeConfig, load_grade_config
from signalforge.grade.engine import grade_artifacts
from signalforge.grade.errors import (
    GradeAuditRecordTooLargeError,
    GradeAuditWriteError,
    GradeBelowThresholdError,
    GradeBudgetExceededError,
    GradeConfigError,
    GradeError,
    GradeLLMError,
    GradeOutputError,
    GradeOutputViolationType,
    GradePromptEnvelopeBreachError,
    GradeRubricError,
)
from signalforge.grade.models import GradeEvent, GradingReport, GradingResult
from signalforge.grade.rubric import (
    DEFAULT_RUBRIC,
    Criterion,
    GradeThresholds,
    Rubric,
)

__all__ = (
    "Criterion",
    "DEFAULT_RUBRIC",
    "GradeAuditRecordTooLargeError",
    "GradeAuditWriteError",
    "GradeBelowThresholdError",
    "GradeBudgetExceededError",
    "GradeConfig",
    "GradeConfigError",
    "GradeError",
    "GradeEvent",
    "GradeLLMError",
    "GradeOutputError",
    "GradeOutputViolationType",
    "GradePromptEnvelopeBreachError",
    "GradeRubricError",
    "GradeThresholds",
    "GradingReport",
    "GradingResult",
    "Rubric",
    "grade_artifacts",
    "load_grade_config",
)
