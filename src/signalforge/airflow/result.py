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
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    # Type-checker-only import: keeps ``result.py`` airflow-free AND avoids any
    # runtime import edge (``drift.py`` does not import ``result.py``, so there
    # is no cycle today — but the annotation is a string under
    # ``from __future__ import annotations`` and ``decide_task_outcome`` only
    # duck-types ``drift.alarming`` at runtime, so no runtime import is needed).
    from signalforge.airflow.drift import DriftReport

# How an operator should treat an exit-0 run that flagged below-threshold
# artifacts. Operator-supplied policy; the default ("fail") makes a flagged run a
# hard task failure so a reviewer sees it.
OnFlagged = Literal["fail", "skip", "succeed"]

# How an operator should treat an exit-0 run whose run-over-run drift is
# *alarming* (signal rot or a grade regression — :attr:`DriftReport.alarming`).
# The run-over-run analogue of :data:`OnFlagged` (issue #235, DEC-006); same
# three policies, same default ("fail").
OnDrift = Literal["fail", "skip", "succeed"]


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


# Severity ordering for combining the flagged-outcome and the drift-outcome on
# an exit-0 run (DEC-006 — "most-severe wins"). Only the three exit-0-reachable
# outcomes appear: the 1/2/3 exit tiers short-circuit BEFORE either policy, so
# ``FAIL_RETRYABLE`` never enters the combine.
_OUTCOME_SEVERITY: dict[TaskOutcome, int] = {
    TaskOutcome.SUCCESS: 1,
    TaskOutcome.SKIP: 2,
    TaskOutcome.FAIL_NO_RETRY: 3,
}


def _policy_outcome(triggered: bool, policy: OnFlagged | OnDrift) -> TaskOutcome:
    """Map a (triggered?, policy) pair to an exit-0 :class:`TaskOutcome`.

    Shared by both the ``on_flagged`` and ``on_drift`` axes (their literals are
    identical). ``triggered=False`` is always ``SUCCESS``; a triggered condition
    maps ``"skip"`` → ``SKIP``, ``"succeed"`` → ``SUCCESS``, ``"fail"`` (the
    default) → ``FAIL_NO_RETRY``. Always returns one of the three exit-0-reachable
    outcomes, so its result is safe to look up in :data:`_OUTCOME_SEVERITY`.
    """
    if not triggered:
        return TaskOutcome.SUCCESS
    if policy == "skip":
        return TaskOutcome.SKIP
    if policy == "succeed":
        return TaskOutcome.SUCCESS
    # policy == "fail" (the default)
    return TaskOutcome.FAIL_NO_RETRY


def _most_severe(left: TaskOutcome, right: TaskOutcome) -> TaskOutcome:
    """Return the more-severe of two exit-0 outcomes (DEC-006 most-severe wins).

    Severity rank ``FAIL_NO_RETRY (3) > SKIP (2) > SUCCESS (1)``. Both arguments
    must be exit-0-reachable outcomes (the only ones :func:`_policy_outcome`
    produces).
    """
    return left if _OUTCOME_SEVERITY[left] >= _OUTCOME_SEVERITY[right] else right


def decide_task_outcome(
    result: SignalForgeRunResult,
    *,
    on_flagged: OnFlagged = "fail",
    on_drift: OnDrift = "fail",
    drift: DriftReport | None = None,
) -> TaskOutcome:
    """Map a run result + ``on_flagged`` / ``on_drift`` policies to a :class:`TaskOutcome`.

    Pure — no I/O, no airflow. Decision table (exit-code tiers short-circuit
    BEFORE either policy, so ``drift`` is consulted only on an exit-0 run):

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

    **Drift (issue #235, DEC-006).** When ``drift is None`` the behaviour is
    byte-identical to the pre-#235 table above (every #231/#232/#233 caller is
    unchanged). When ``drift is not None and drift.alarming`` (signal rot or a
    grade regression) on an exit-0 run, the ``on_drift`` policy is folded in and
    the function returns the MOST-SEVERE of the flagged-outcome and the
    drift-outcome (rank ``FAIL_NO_RETRY > SKIP > SUCCESS``). A non-alarming or
    degraded ``DriftReport`` (``drift.alarming`` ``False``) never changes the
    outcome. ``drift`` is ignored entirely on a non-exit-0 run.
    """
    if result.exit_code != 0:
        if result.exit_code == 3:
            return TaskOutcome.FAIL_RETRYABLE
        # exit 1, exit 2, and any unexpected code (negative / > 3): fail, no retry.
        return TaskOutcome.FAIL_NO_RETRY

    flagged_outcome = _policy_outcome(result.below_threshold, on_flagged)
    if drift is None or not drift.alarming:
        # No drift supplied, or drift is non-alarming/degraded → flagged axis
        # alone governs. Byte-identical to the pre-#235 exit-0 decision.
        return flagged_outcome
    drift_outcome = _policy_outcome(True, on_drift)
    return _most_severe(flagged_outcome, drift_outcome)


__all__ = [
    "OnDrift",
    "OnFlagged",
    "SignalForgeRunResult",
    "TaskOutcome",
    "decide_task_outcome",
]
