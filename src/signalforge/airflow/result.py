"""Airflow-free result type + pure task-state decision table.

US-001 of issue #231 (epic #228, v0.7 Airflow operator roadmap). This module is
the *pure core* of the SignalForge run-result → Airflow task-state contract:

* :class:`SignalForgeRunResult` — a frozen, Airflow-free dataclass capturing the
  outcome of a ``signalforge generate`` run (exit code, prune/grade counts,
  sidecar paths, captured stdout/stderr).
* :class:`TaskOutcome` — a NEUTRAL four-member discriminator describing what an
  Airflow task should do (succeed / skip / fail-no-retry / fail-retryable).
* :func:`decide_task_outcome` — a PURE function mapping a run result + the
  operator's ``on_flagged`` policy to a :class:`TaskOutcome`.

**This module imports NO airflow.** That is load-bearing: it lets
``signalforge.airflow.__init__`` re-export these names **eagerly** (alongside the
error classes — only the operator/hook names stay lazy, DEC-006), and it keeps the
decision logic unit-testable in the default pytest suite without the heavy,
version-pinned Apache Airflow dependency installed.

**:class:`TaskOutcome` is a SEPARATE axis from the CLI's four-tier exit-code
taxonomy** (``.claude/rules/cli-layer.md``). The exit code (0/1/2/3) is the
*input* to :func:`decide_task_outcome`; the :class:`TaskOutcome` is the *output*.
``TaskOutcome`` is NOT a fifth exit tier — never collapse exit tiers 2 & 3, and
never invent a fifth. The mapping from exit code to task outcome is the whole
point of this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

# How an operator should treat an exit-0 run that flagged below-threshold
# artifacts. Operator-supplied policy; the default ("fail") makes a flagged run a
# hard task failure so a reviewer sees it.
OnFlagged = Literal["fail", "skip", "succeed"]


class TaskOutcome(StrEnum):
    """Neutral discriminator for what an Airflow task should do.

    Exactly FOUR members — a separate axis from the CLI's four-tier exit-code
    taxonomy (it is NOT a fifth exit tier; see ``.claude/rules/cli-layer.md``).
    A :class:`enum.StrEnum` (the str-subclass form, per safety ``SamplingMode``
    DEC-024) so the value is JSON-serialisable and reads cleanly in logs / XCom.
    """

    SUCCESS = "success"
    SKIP = "skip"
    FAIL_NO_RETRY = "fail_no_retry"
    FAIL_RETRYABLE = "fail_retryable"


@dataclass(frozen=True)
class SignalForgeRunResult:
    """The outcome of one ``signalforge generate`` run.

    Frozen and Airflow-free: this is the value the runner produces and the
    operator consumes. ``below_threshold`` is a derived read-only property
    (``flagged > 0``, DEC-001) rather than a stored field, so it stays
    consistent with ``flagged`` by construction.
    """

    exit_code: int
    model_unique_ids: tuple[str, ...]
    kept: int
    kept_uncertain: int
    dropped: int
    flagged: int
    mean_grade: float | None
    diff_sidecar_path: str | None
    grade_sidecar_path: str | None
    duration_seconds: float | None
    stdout: str
    stderr: str

    @property
    def below_threshold(self) -> bool:
        """Whether any artifact was flagged below the grading threshold.

        Derived from ``flagged > 0`` (DEC-001) — not stored, so it can never
        drift out of sync with ``flagged``.
        """
        return self.flagged > 0

    def to_xcom(self) -> dict[str, object]:
        """Return a JSON-serialisable dict of COUNTS + PATHS only.

        Deliberately omits ``stdout`` / ``stderr`` (bulk text) and any
        secret-shaped field — XCom is small, durable, and visible in the Airflow
        UI, so only the summary scalars and sidecar paths belong here. The result
        round-trips through ``json.dumps`` / ``json.loads``.
        """
        return {
            "exit_code": self.exit_code,
            "model_unique_ids": list(self.model_unique_ids),
            "kept": self.kept,
            "kept_uncertain": self.kept_uncertain,
            "dropped": self.dropped,
            "flagged": self.flagged,
            "mean_grade": self.mean_grade,
            "below_threshold": self.below_threshold,
            "diff_sidecar_path": self.diff_sidecar_path,
            "grade_sidecar_path": self.grade_sidecar_path,
            "duration_seconds": self.duration_seconds,
        }


def decide_task_outcome(
    result: SignalForgeRunResult, *, on_flagged: OnFlagged = "fail"
) -> TaskOutcome:
    """Map a run result + ``on_flagged`` policy to a :class:`TaskOutcome`.

    Pure — no I/O, no airflow. Decision table:

    * exit 0, no flagged                       → ``SUCCESS``
    * exit 0, flagged, ``on_flagged="fail"``   → ``FAIL_NO_RETRY``
    * exit 0, flagged, ``on_flagged="skip"``   → ``SKIP``
    * exit 0, flagged, ``on_flagged="succeed"``→ ``SUCCESS``
    * exit 1 (load/parse failure)              → ``FAIL_NO_RETRY``
    * exit 2 (input-validation failure)        → ``FAIL_NO_RETRY``
    * exit 3 (external dependency failure)     → ``FAIL_RETRYABLE``

    Tier 2 stays a hard input error → ``FAIL_NO_RETRY`` (flagged artifacts are
    detected on an exit-0 run via ``result.below_threshold``, NOT via tier 2).
    Any unexpected ``exit_code`` (negative, or > 3) defaults conservatively to
    ``FAIL_NO_RETRY`` — an unknown failure should fail the task, not retry
    forever.
    """
    if result.exit_code == 0:
        if not result.below_threshold:
            return TaskOutcome.SUCCESS
        if on_flagged == "skip":
            return TaskOutcome.SKIP
        if on_flagged == "succeed":
            return TaskOutcome.SUCCESS
        # on_flagged == "fail" (the default)
        return TaskOutcome.FAIL_NO_RETRY
    if result.exit_code == 3:
        return TaskOutcome.FAIL_RETRYABLE
    # exit 1, exit 2, and any unexpected code (negative / > 3): fail, no retry.
    return TaskOutcome.FAIL_NO_RETRY


__all__ = [
    "OnFlagged",
    "SignalForgeRunResult",
    "TaskOutcome",
    "decide_task_outcome",
]
