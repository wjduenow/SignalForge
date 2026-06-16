"""Tests for ``signalforge.airflow.result`` (issue #231 / US-001).

These tests import ONLY the Airflow-free result core — never the real
``apache-airflow`` package — so they run in the **default** pytest suite
(NO ``airflow`` marker). They pin:

1. The full :func:`decide_task_outcome` decision table (all 7 rows, including
   all three ``on_flagged`` branches at exit 0).
2. ``below_threshold == (flagged > 0)``.
3. ``to_xcom()`` keys + JSON round-trip + no ``stdout`` / ``stderr`` leakage.
4. ``TaskOutcome`` has exactly four members (guards against silent 5th-tier
   creep).
5. An unexpected ``exit_code`` maps conservatively to ``FAIL_NO_RETRY``.
6. Importing ``signalforge.airflow`` (with the new eager re-exports) still does
   not pull ``airflow`` into ``sys.modules``.
"""

from __future__ import annotations

import json
import sys

import pytest

from signalforge.airflow import (
    DriftArtifact,
    DriftReport,
    OnFlagged,
    SignalForgeRunResult,
    TaskOutcome,
    decide_task_outcome,
)
from signalforge.airflow.result import OnDrift


def _make_result(*, exit_code: int = 0, flagged: int = 0) -> SignalForgeRunResult:
    """Build a ``SignalForgeRunResult`` with sane defaults for the fields a
    given test does not care about."""
    return SignalForgeRunResult(
        exit_code=exit_code,
        model_unique_ids=("model.pkg.a", "model.pkg.b"),
        kept=5,
        kept_uncertain=1,
        dropped=2,
        flagged=flagged,
        mean_grade=0.87,
        diff_sidecar_path=".signalforge/diff.json",
        grade_sidecar_path=".signalforge/grade.json",
        duration_seconds=12.5,
        stdout="rendered diff here",
        stderr="some progress line",
    )


@pytest.mark.parametrize(
    ("exit_code", "flagged", "on_flagged", "expected"),
    [
        # exit 0, no flagged -> SUCCESS (on_flagged irrelevant)
        (0, 0, "fail", TaskOutcome.SUCCESS),
        # exit 0, flagged, each on_flagged branch
        (0, 3, "fail", TaskOutcome.FAIL_NO_RETRY),
        (0, 3, "skip", TaskOutcome.SKIP),
        (0, 3, "succeed", TaskOutcome.SUCCESS),
        # exit 1 / 2 -> hard fail, no retry
        (1, 0, "fail", TaskOutcome.FAIL_NO_RETRY),
        (2, 0, "fail", TaskOutcome.FAIL_NO_RETRY),
        # exit 3 -> retryable external-dependency failure
        (3, 0, "fail", TaskOutcome.FAIL_RETRYABLE),
    ],
)
def test_decide_task_outcome_table(
    exit_code: int, flagged: int, on_flagged: OnFlagged, expected: TaskOutcome
) -> None:
    """Every documented row of the decision table maps as specified."""
    result = _make_result(exit_code=exit_code, flagged=flagged)
    assert decide_task_outcome(result, on_flagged=on_flagged) == expected


def test_decide_task_outcome_default_on_flagged_is_fail() -> None:
    """The default ``on_flagged`` ("fail") turns an exit-0 flagged run into a
    hard task failure."""
    result = _make_result(exit_code=0, flagged=2)
    assert decide_task_outcome(result) == TaskOutcome.FAIL_NO_RETRY


@pytest.mark.parametrize("exit_code", [99, -1, 4, 255])
def test_decide_task_outcome_unexpected_exit_code_fails_no_retry(exit_code: int) -> None:
    """Any unexpected exit code (negative or > 3) defaults conservatively to
    FAIL_NO_RETRY — an unknown failure should fail, not retry forever."""
    result = _make_result(exit_code=exit_code, flagged=0)
    assert decide_task_outcome(result) == TaskOutcome.FAIL_NO_RETRY


@pytest.mark.parametrize(("flagged", "expected"), [(0, False), (1, True), (5, True)])
def test_below_threshold_derives_from_flagged(flagged: int, expected: bool) -> None:
    """``below_threshold`` is exactly ``flagged > 0``."""
    result = _make_result(flagged=flagged)
    assert result.below_threshold is expected
    assert result.below_threshold == (flagged > 0)


def test_to_xcom_keys_and_round_trip() -> None:
    """``to_xcom()`` returns exactly the documented keys, round-trips through
    JSON, and excludes ``stdout`` / ``stderr``."""
    result = _make_result(exit_code=0, flagged=2)
    payload = result.to_xcom()

    expected_keys = {
        "exit_code",
        "model_unique_ids",
        "kept",
        "kept_uncertain",
        "dropped",
        "flagged",
        "mean_grade",
        "below_threshold",
        "diff_sidecar_path",
        "grade_sidecar_path",
        "duration_seconds",
    }
    assert set(payload.keys()) == expected_keys

    # Bulk text must never ride along in XCom.
    assert "stdout" not in payload
    assert "stderr" not in payload

    # model_unique_ids is rendered as a JSON-friendly list, not a tuple.
    assert payload["model_unique_ids"] == ["model.pkg.a", "model.pkg.b"]
    assert isinstance(payload["model_unique_ids"], list)

    # below_threshold reflects flagged > 0.
    assert payload["below_threshold"] is True

    # Round-trips cleanly through json.
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped == payload


def test_to_xcom_round_trips_with_none_fields() -> None:
    """Optional ``None`` fields (mean_grade, sidecar paths, duration) survive the
    JSON round-trip."""
    result = SignalForgeRunResult(
        exit_code=0,
        model_unique_ids=(),
        kept=0,
        kept_uncertain=0,
        dropped=0,
        flagged=0,
        mean_grade=None,
        diff_sidecar_path=None,
        grade_sidecar_path=None,
        duration_seconds=None,
        stdout="",
        stderr="",
    )
    payload = result.to_xcom()
    assert payload["mean_grade"] is None
    assert payload["below_threshold"] is False
    assert json.loads(json.dumps(payload)) == payload


def _drift(*, alarming: bool, degraded: bool = False) -> DriftReport:
    """Build a :class:`DriftReport` whose :attr:`alarming` property is as asked.

    * ``alarming=True`` → one ``newly_always_passes`` artifact (signal rot).
    * ``alarming=False`` → all transition lists empty.
    * ``degraded=True`` → a ``degrade_reason`` is set; the alarm lists stay empty
      so ``alarming`` is ``False`` by construction (DEC-013).
    """
    rot = (
        (
            DriftArtifact(
                artifact_id="test.column.amount.not_null",
                previous_tier="kept",
                current_tier="dropped",
                previous_drop_reason=None,
                current_drop_reason="always-passes",
                why="always passes on the sample",
            ),
        )
        if alarming
        else ()
    )
    return DriftReport(
        signalforge_version="0.7.0.dev0",
        model_unique_id="model.shop.fct_orders",
        as_of=None,
        grade_regression_threshold=0.05,
        previous_diff_hash="0" * 16,
        current_diff_hash="1" * 16,
        newly_always_passes=rot,
        degrade_reason="model mismatch: prior=a current=b" if degraded else None,
    )


def test_drift_helper_alarming_property_truth_table() -> None:
    """Sanity-pin the helper: it produces the alarming state each case asks for."""
    assert _drift(alarming=True).alarming is True
    assert _drift(alarming=False).alarming is False
    assert _drift(alarming=False, degraded=True).alarming is False


def test_decide_task_outcome_drift_none_is_byte_identical_to_pre_235() -> None:
    """Passing ``drift=None`` reproduces EVERY pre-#235 row exactly.

    The ``on_drift`` value is irrelevant when ``drift is None`` — pinned by
    varying it across all three policies and asserting the result still equals
    the drift-free decision.
    """
    cases: list[tuple[int, int, OnFlagged, TaskOutcome]] = [
        (0, 0, "fail", TaskOutcome.SUCCESS),
        (0, 3, "fail", TaskOutcome.FAIL_NO_RETRY),
        (0, 3, "skip", TaskOutcome.SKIP),
        (0, 3, "succeed", TaskOutcome.SUCCESS),
        (1, 0, "fail", TaskOutcome.FAIL_NO_RETRY),
        (2, 0, "fail", TaskOutcome.FAIL_NO_RETRY),
        (3, 0, "fail", TaskOutcome.FAIL_RETRYABLE),
    ]
    for exit_code, flagged, on_flagged, expected in cases:
        result = _make_result(exit_code=exit_code, flagged=flagged)
        for on_drift in ("fail", "skip", "succeed"):
            got = decide_task_outcome(result, on_flagged=on_flagged, on_drift=on_drift, drift=None)
            assert got == expected, (exit_code, flagged, on_flagged, on_drift)
        # And explicitly equal to the drift-free single-arg form.
        assert decide_task_outcome(result, on_flagged=on_flagged) == expected


@pytest.mark.parametrize(
    ("flagged", "on_flagged", "alarming", "on_drift", "expected"),
    [
        # --- drift drives (not flagged): on_drift maps straight through ---
        (0, "fail", True, "fail", TaskOutcome.FAIL_NO_RETRY),
        (0, "fail", True, "skip", TaskOutcome.SKIP),
        (0, "fail", True, "succeed", TaskOutcome.SUCCESS),
        # --- non-alarming drift never changes the flagged-only outcome ---
        (0, "fail", False, "fail", TaskOutcome.SUCCESS),
        (2, "fail", False, "fail", TaskOutcome.FAIL_NO_RETRY),
        (2, "succeed", False, "fail", TaskOutcome.SUCCESS),
        # --- both trip: MOST-SEVERE wins (FAIL_NO_RETRY > SKIP > SUCCESS) ---
        (2, "fail", True, "succeed", TaskOutcome.FAIL_NO_RETRY),  # flagged worse
        (2, "succeed", True, "fail", TaskOutcome.FAIL_NO_RETRY),  # drift worse
        (2, "skip", True, "fail", TaskOutcome.FAIL_NO_RETRY),  # drift worse
        (2, "fail", True, "skip", TaskOutcome.FAIL_NO_RETRY),  # flagged worse
        (2, "skip", True, "succeed", TaskOutcome.SKIP),  # skip > success
        (2, "succeed", True, "skip", TaskOutcome.SKIP),  # skip > success
        (2, "succeed", True, "succeed", TaskOutcome.SUCCESS),  # both benign
        (2, "skip", True, "skip", TaskOutcome.SKIP),  # tie
    ],
)
def test_decide_task_outcome_flagged_x_drift_most_severe(
    flagged: int,
    on_flagged: OnFlagged,
    alarming: bool,
    on_drift: OnDrift,
    expected: TaskOutcome,
) -> None:
    """On an exit-0 run, the flagged-outcome and drift-outcome combine to the
    MOST-SEVERE verdict (DEC-006)."""
    result = _make_result(exit_code=0, flagged=flagged)
    got = decide_task_outcome(
        result, on_flagged=on_flagged, on_drift=on_drift, drift=_drift(alarming=alarming)
    )
    assert got == expected


def test_decide_task_outcome_degraded_drift_never_trips() -> None:
    """A degraded DriftReport (``alarming=False``) never changes the outcome,
    even with ``on_drift="fail"`` — degrade, don't page (DEC-013)."""
    result = _make_result(exit_code=0, flagged=0)
    degraded = _drift(alarming=False, degraded=True)
    assert decide_task_outcome(result, on_drift="fail", drift=degraded) == TaskOutcome.SUCCESS


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        (1, TaskOutcome.FAIL_NO_RETRY),
        (2, TaskOutcome.FAIL_NO_RETRY),
        (3, TaskOutcome.FAIL_RETRYABLE),
        (99, TaskOutcome.FAIL_NO_RETRY),
    ],
)
def test_decide_task_outcome_exit_tiers_ignore_alarming_drift(
    exit_code: int, expected: TaskOutcome
) -> None:
    """The 1/2/3 exit tiers short-circuit BEFORE the drift policy.

    The load-bearing case is exit 3 + alarming drift + ``on_drift="fail"``: it
    stays ``FAIL_RETRYABLE`` (the retryable external-dependency verdict), NOT
    downgraded to ``FAIL_NO_RETRY`` by the drift combine — drift is consulted
    only on an exit-0 run (DEC-006)."""
    result = _make_result(exit_code=exit_code, flagged=0)
    got = decide_task_outcome(result, on_drift="fail", drift=_drift(alarming=True))
    assert got == expected


def test_task_outcome_has_exactly_four_members() -> None:
    """Guard against silent fifth-tier creep — ``TaskOutcome`` is a SEPARATE
    axis from the CLI exit-code taxonomy and must stay four-valued."""
    assert len(TaskOutcome) == 4
    assert {member.name for member in TaskOutcome} == {
        "SUCCESS",
        "SKIP",
        "FAIL_NO_RETRY",
        "FAIL_RETRYABLE",
    }


def test_importing_signalforge_airflow_with_result_reexports_does_not_import_airflow() -> None:
    """The new eager result re-exports keep the no-eager-airflow-import contract:
    ``signalforge.airflow.result`` carries no ``from airflow ...`` import.

    Order-independent: scrub any ``airflow`` / cached ``signalforge.airflow``
    entries a PRIOR test may have left in ``sys.modules`` FIRST, then re-import
    ``signalforge.airflow`` cleanly and assert nothing airflow-prefixed appears.
    Mirrors the scrub idiom in ``tests/airflow/test_airflow_no_eager_import.py``.
    """
    import importlib

    for name in list(sys.modules):
        if name == "airflow" or name.startswith("airflow."):
            del sys.modules[name]
        if name == "signalforge.airflow" or name.startswith("signalforge.airflow."):
            del sys.modules[name]

    importlib.import_module("signalforge.airflow")

    leaked = sorted(
        name for name in sys.modules if name == "airflow" or name.startswith("airflow.")
    )
    assert not leaked, f"airflow leaked into sys.modules: {leaked}"
