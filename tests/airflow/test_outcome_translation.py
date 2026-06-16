"""Gated tests for the Airflow-side ``raise_for_outcome`` translator (#231 US-003).

Belt-and-suspenders gating per ``testing-signal.md`` and the
``tests/airflow/test_dag_parse.py`` precedent:

1. ``@pytest.mark.airflow`` on each test — deselected by the default ``addopts``
   ``-m '... and not airflow'`` so a plain ``uv run pytest`` never imports Airflow.
2. A runtime ``pytest.importorskip("airflow")`` INSIDE each test (NOT at module
   scope — module-scope import would run at collection time even when deselected)
   — a clear skip-with-reason when a maintainer runs ``-m airflow`` in an env
   where Airflow isn't installed.

Run: ``uv run --no-sync pytest -m airflow --no-cov`` inside the constraints-pinned
Airflow venv (see docs/research/airflow-test-environment.md).

These pin the result → task-state contract's Airflow half (#231, DEC-006): the
neutral :class:`signalforge.airflow.result.TaskOutcome` produced by the pure
``decide_task_outcome`` is translated here into the airflow exception the task
runner understands — ``AirflowFailException`` (no retry) vs ``AirflowException``
(retryable) vs ``AirflowSkipException`` (skip) vs no-raise (success).
"""

from __future__ import annotations

import pytest

from signalforge.airflow._airflow_compat import raise_for_outcome
from signalforge.airflow.result import TaskOutcome

_AIRFLOW_SKIP = "Apache Airflow not installed (run inside the constraints-pinned airflow venv)"


@pytest.mark.airflow
def test_fail_no_retry_raises_airflow_fail_exception() -> None:
    """``FAIL_NO_RETRY`` → ``AirflowFailException`` (bypasses the retry policy)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowFailException

    with pytest.raises(AirflowFailException) as exc_info:
        raise_for_outcome(TaskOutcome.FAIL_NO_RETRY, message="hard failure")
    # Exact type — not merely an AirflowException subclass — is the no-retry signal.
    assert type(exc_info.value) is AirflowFailException
    assert str(exc_info.value) == "hard failure"


@pytest.mark.airflow
def test_fail_retryable_raises_base_airflow_exception() -> None:
    """``FAIL_RETRYABLE`` → base ``AirflowException`` (honours ``retries``)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowException, AirflowFailException, AirflowSkipException

    with pytest.raises(AirflowException) as exc_info:
        raise_for_outcome(TaskOutcome.FAIL_RETRYABLE, message="transient failure")
    # Must be the BASE AirflowException, never the no-retry/skip subclasses —
    # otherwise the retry policy would be bypassed (FailException) or the task
    # would be skipped (SkipException) instead of retried.
    assert type(exc_info.value) is AirflowException
    assert not isinstance(exc_info.value, (AirflowFailException, AirflowSkipException))
    assert str(exc_info.value) == "transient failure"


@pytest.mark.airflow
def test_skip_raises_airflow_skip_exception() -> None:
    """``SKIP`` → ``AirflowSkipException`` (marks the task skipped)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowSkipException

    with pytest.raises(AirflowSkipException) as exc_info:
        raise_for_outcome(TaskOutcome.SKIP, message="nothing to do")
    assert type(exc_info.value) is AirflowSkipException
    assert str(exc_info.value) == "nothing to do"


@pytest.mark.airflow
def test_success_returns_none_and_raises_nothing() -> None:
    """``SUCCESS`` → return ``None``, raise nothing (the task succeeds)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    assert raise_for_outcome(TaskOutcome.SUCCESS, message="all good") is None


@pytest.mark.airflow
def test_exception_hierarchy_is_as_assumed() -> None:
    """The translator's mapping relies on airflow's exception hierarchy:

    ``AirflowFailException`` and ``AirflowSkipException`` are distinct subclasses
    of ``AirflowException``. Pin that so a future airflow rev that flattened the
    hierarchy (making the no-retry / retryable distinction collapse) fails loud.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowException, AirflowFailException, AirflowSkipException

    assert AirflowFailException is not AirflowSkipException
    assert issubclass(AirflowFailException, AirflowException)
    assert issubclass(AirflowSkipException, AirflowException)
    # The no-retry signal is genuinely a subclass, not the base itself.
    assert AirflowFailException is not AirflowException
