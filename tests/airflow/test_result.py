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
    OnFlagged,
    SignalForgeRunResult,
    TaskOutcome,
    decide_task_outcome,
)


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
    ``signalforge.airflow.result`` carries no ``from airflow ...`` import."""
    leaked = sorted(
        name for name in sys.modules if name == "airflow" or name.startswith("airflow.")
    )
    assert not leaked, f"airflow leaked into sys.modules: {leaked}"
