"""Gated tests for the two drift surfaces' ``execute`` (#235 US-006).

Belt-and-suspenders gating per ``testing-signal.md`` and the
``tests/airflow/test_operators.py`` precedent:

1. ``pytestmark = pytest.mark.airflow`` — every test is deselected by the default
   ``addopts`` ``-m '... and not airflow'`` so a plain ``uv run pytest`` never
   imports Apache Airflow.
2. A runtime ``pytest.importorskip("airflow")`` as the FIRST line of each test
   (NOT a module-scope import — that would run at collection time even when
   deselected). The skip carries a clear reason when a maintainer runs
   ``-m airflow`` without Airflow installed.

Run inside the constraints-pinned Airflow venv (see
docs/research/airflow-test-environment.md):
``uv run --no-sync pytest -m airflow --no-cov``.

These pin BOTH drift surfaces end-to-end (exercising DEC-001/010/013/015):

* the ``SignalForgeGenerateOperator`` drift path (``detect_drift_against`` +
  ``on_drift`` + ``drift_history_dir`` persistence): the ``"drift"`` key on the
  XCom; the signal-rot fixture pair driving ``on_drift`` fail/skip/succeed →
  the matching Airflow signal; the baseline (prior missing) + degrade (model
  mismatch) + unparseable-current degrade paths all succeeding;
* the dedicated ``SignalForgeDriftOperator``: prev+curr fixture pair → drift on
  XCom; ``on_drift`` mapping; baseline (prior None) → success; a missing CURRENT
  diff → hard ``AirflowConfigError``.

``run_signalforge`` is monkeypatched to canned :class:`SignalForgeRunResult`s
(carrying the committed US-001 fixture diff JSON on stdout) so no real
``signalforge`` run happens; the dedicated operator reads the fixtures off disk
(it runs no CLI), so it is exercised against tmp-copied fixtures directly.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from signalforge.airflow.result import SignalForgeRunResult

pytestmark = pytest.mark.airflow

_AIRFLOW_SKIP = "Apache Airflow not installed (run inside the constraints-pinned airflow venv)"

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "airflow" / "drift_pairs"

_MODEL = "model.shop.fct_orders"


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #


def _fixture_text(name: str) -> str:
    """Read a committed drift_pairs fixture's JSON text."""
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


def _layout_prior(
    tmp_path: Path,
    *,
    prev_diff: str | None = "signal_rot_prev_diff.json",
    prev_grade: str | None = "signal_rot_prev_grade.json",
    curr_grade: str | None = "signal_rot_curr_grade.json",
) -> tuple[str, str | None]:
    """Lay out a prior ``<dir>/diff.json`` (+ ``grade.json`` sibling) for the generate path.

    Mirrors the on-disk convention the generate operator's drift wiring reads:
    the prior diff sits at ``detect_drift_against`` and its grade sibling at
    ``<that dir>/grade.json``. The current run's grade sidecar is written
    separately (the generate operator reads it off ``result.grade_sidecar_path``).

    Returns ``(detect_drift_against_path, current_grade_sidecar_path | None)``.
    ``prev_diff=None`` lays out NO prior diff (the path still points into the
    empty dir → a missing-prior baseline).
    """
    prev = tmp_path / "prev"
    prev.mkdir(exist_ok=True)
    if prev_diff is not None:
        (prev / "diff.json").write_text(_fixture_text(prev_diff), encoding="utf-8")
    if prev_grade is not None:
        (prev / "grade.json").write_text(_fixture_text(prev_grade), encoding="utf-8")

    current_grade_path: str | None = None
    if curr_grade is not None:
        curr = tmp_path / "curr"
        curr.mkdir(exist_ok=True)
        (curr / "grade.json").write_text(_fixture_text(curr_grade), encoding="utf-8")
        current_grade_path = str(curr / "grade.json")

    return str(prev / "diff.json"), current_grade_path


def _layout_pair(tmp_path: Path, *, prior: bool = True) -> tuple[str, str]:
    """Lay out a prev + curr diff/grade quartet for the dedicated drift operator.

    The dedicated operator reads BOTH diffs off disk and auto-siblings the grades
    (``<dir>/diff.json`` ↔ ``<dir>/grade.json``). Returns
    ``(previous_diff_path, current_diff_path)``. ``prior=False`` omits the prior
    diff so the operator establishes a baseline.
    """
    prev = tmp_path / "prev"
    prev.mkdir(exist_ok=True)
    if prior:
        (prev / "diff.json").write_text(
            _fixture_text("signal_rot_prev_diff.json"), encoding="utf-8"
        )
    (prev / "grade.json").write_text(_fixture_text("signal_rot_prev_grade.json"), encoding="utf-8")

    curr = tmp_path / "curr"
    curr.mkdir(exist_ok=True)
    (curr / "diff.json").write_text(_fixture_text("signal_rot_curr_diff.json"), encoding="utf-8")
    (curr / "grade.json").write_text(_fixture_text("signal_rot_curr_grade.json"), encoding="utf-8")

    return str(prev / "diff.json"), str(curr / "diff.json")


def _generate_result(
    *,
    stdout_fixture: str | None = "signal_rot_curr_diff.json",
    stdout: str | None = None,
    grade_sidecar_path: str | None = None,
    exit_code: int = 0,
    flagged: int = 0,
) -> SignalForgeRunResult:
    """Build a canned generate result carrying the CURRENT diff JSON on stdout.

    The generate operator's drift wiring parses the current :class:`DiffReport`
    off ``result.stdout`` (#231 DEC-005), so the canned result carries the
    committed current-run fixture there. ``stdout`` overrides ``stdout_fixture``
    (use it for an unparseable-current degrade test).
    """
    body = stdout if stdout is not None else _fixture_text(stdout_fixture) if stdout_fixture else ""
    return SignalForgeRunResult(
        exit_code=exit_code,
        model_unique_ids=(_MODEL,),
        kept=1,
        kept_uncertain=0,
        dropped=1,
        flagged=flagged,
        mean_grade=0.8,
        diff_sidecar_path=None,
        grade_sidecar_path=grade_sidecar_path,
        duration_seconds=1.0,
        stdout=body,
        stderr="",
    )


def _generate_operator_class() -> type:
    operators = importlib.import_module("signalforge.airflow.operators")
    return operators.SignalForgeGenerateOperator


def _drift_operator_class() -> type:
    operators = importlib.import_module("signalforge.airflow.operators")
    return operators.SignalForgeDriftOperator


def _patch_run(monkeypatch: pytest.MonkeyPatch, result: SignalForgeRunResult) -> None:
    """Monkeypatch ``operators.run_signalforge`` to return ``result``."""

    def _fake_run(
        argv: list[str],
        *,
        project_dir: object,
        invocation: object = "in_process",
        timeout_seconds: object = None,
    ) -> SignalForgeRunResult:
        return result

    monkeypatch.setattr("signalforge.airflow.operators.run_signalforge", _fake_run)


# --------------------------------------------------------------------------- #
# SignalForgeGenerateOperator — drift path (detect_drift_against)
# --------------------------------------------------------------------------- #


def test_generate_drift_signal_rot_on_fail_raises_airflow_fail_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Signal rot (kept→always-passes) with ``on_drift='fail'`` → AirflowFailException.

    The exit-0 generate run flagged nothing, but the prior→current diff shows the
    ``not_null`` test rotted from ``kept`` to ``dropped(always-passes)`` → the
    drift report is alarming → most-severe(SUCCESS, FAIL_NO_RETRY) = FAIL_NO_RETRY.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowFailException

    detect_against, curr_grade = _layout_prior(tmp_path)
    _patch_run(monkeypatch, _generate_result(grade_sidecar_path=curr_grade))

    op = _generate_operator_class()(
        task_id="gen",
        project_dir="/proj",
        model=_MODEL,
        detect_drift_against=detect_against,
        on_drift="fail",
    )
    with pytest.raises(AirflowFailException):
        op.execute(context={})


def test_generate_drift_signal_rot_on_skip_raises_airflow_skip_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Alarming drift with ``on_drift='skip'`` → AirflowSkipException (route to review)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowSkipException

    detect_against, curr_grade = _layout_prior(tmp_path)
    _patch_run(monkeypatch, _generate_result(grade_sidecar_path=curr_grade))

    op = _generate_operator_class()(
        task_id="gen",
        project_dir="/proj",
        model=_MODEL,
        detect_drift_against=detect_against,
        on_drift="skip",
    )
    with pytest.raises(AirflowSkipException):
        op.execute(context={})


def test_generate_drift_signal_rot_on_succeed_returns_xcom_with_drift_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Alarming drift with ``on_drift='succeed'`` → no raise; XCom carries ``"drift"`` (DEC-015).

    The drift summary rides under the ``"drift"`` key alongside the run's own
    counts; the signal-rot transition (1 newly-always-passes) and the grade
    regression (prev 0.9 → curr 0.8, delta 0.1 > 0.05) both surface.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    detect_against, curr_grade = _layout_prior(tmp_path)
    _patch_run(monkeypatch, _generate_result(grade_sidecar_path=curr_grade))

    op = _generate_operator_class()(
        task_id="gen",
        project_dir="/proj",
        model=_MODEL,
        detect_drift_against=detect_against,
        on_drift="succeed",
    )
    xcom = op.execute(context={})

    # Base run keys still present (the run succeeded, exit 0, flagged 0).
    assert xcom["exit_code"] == 0
    assert xcom["below_threshold"] is False
    # The drift summary is nested under "drift".
    assert "drift" in xcom
    drift = xcom["drift"]
    assert isinstance(drift, dict)
    assert drift["model_unique_id"] == _MODEL
    assert drift["alarming"] is True
    assert drift["baseline"] is False
    assert drift["counts"]["newly_always_passes"] == 1
    assert drift["counts"]["grade_regressions"] == 1
    # JSON-serialisable (XCom hygiene): round-trips with no bulk text / secrets.
    json.dumps(xcom)


def test_generate_drift_baseline_when_prior_missing_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing prior diff (first run) → baseline DriftReport → SUCCESS (DEC-013).

    ``on_drift='fail'`` does NOT trip because a baseline is never alarming.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    # Point detect_drift_against at a path with NO prior diff file present.
    detect_against, curr_grade = _layout_prior(tmp_path, prev_diff=None, prev_grade=None)
    _patch_run(monkeypatch, _generate_result(grade_sidecar_path=curr_grade))

    op = _generate_operator_class()(
        task_id="gen",
        project_dir="/proj",
        model=_MODEL,
        detect_drift_against=detect_against,
        on_drift="fail",
    )
    xcom = op.execute(context={})

    assert "drift" in xcom
    drift = xcom["drift"]
    assert isinstance(drift, dict)
    assert drift["baseline"] is True
    assert drift["alarming"] is False


def test_generate_drift_degrade_on_model_mismatch_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A prior diff for a DIFFERENT model → degraded (non-alarming) report → SUCCESS (DEC-013).

    ``compute_drift`` degrades on a ``model_unique_id`` mismatch: it sets
    ``degrade_reason`` and an empty (never-alarming) report rather than raising,
    so ``on_drift='fail'`` does not trip.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    # Write a prior diff whose model_unique_id differs from the current run's.
    prev = tmp_path / "prev"
    prev.mkdir()
    prior_obj = json.loads(_fixture_text("signal_rot_prev_diff.json"))
    prior_obj["model_unique_id"] = "model.shop.some_other_model"
    (prev / "diff.json").write_text(json.dumps(prior_obj), encoding="utf-8")
    detect_against = str(prev / "diff.json")

    _patch_run(monkeypatch, _generate_result())

    op = _generate_operator_class()(
        task_id="gen",
        project_dir="/proj",
        model=_MODEL,
        detect_drift_against=detect_against,
        on_drift="fail",
    )
    xcom = op.execute(context={})

    assert "drift" in xcom
    drift = xcom["drift"]
    assert isinstance(drift, dict)
    assert drift["degrade_reason"] is not None
    assert drift["alarming"] is False


def test_generate_drift_unparseable_current_skips_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unparseable current diff (no JSON on stdout) → no ``"drift"`` key, run governs (DEC-013).

    Drift degrades to ``None`` (no verdict, cannot alarm); the generate run's own
    exit-0 / flagged-0 verdict still governs the task → SUCCESS, no drift key.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    detect_against, _ = _layout_prior(tmp_path)
    _patch_run(monkeypatch, _generate_result(stdout="not json at all", stdout_fixture=None))

    op = _generate_operator_class()(
        task_id="gen",
        project_dir="/proj",
        model=_MODEL,
        detect_drift_against=detect_against,
        on_drift="fail",
    )
    xcom = op.execute(context={})

    assert "drift" not in xcom
    assert xcom["exit_code"] == 0


def test_generate_drift_persists_current_run_to_history_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``drift_history_dir`` persists this run's diff.json (+ grade.json) for next time (DEC-009).

    Reuses the fail-closed ``write_sidecar`` writer; the grade sidecar is copied
    best-effort. After ``execute`` the history dir holds a valid prior pair the
    next run can point ``detect_drift_against`` at.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    from signalforge.airflow.drift import load_diff_report

    detect_against, curr_grade = _layout_prior(tmp_path)
    history_dir = tmp_path / "history"
    _patch_run(monkeypatch, _generate_result(grade_sidecar_path=curr_grade))

    op = _generate_operator_class()(
        task_id="gen",
        project_dir="/proj",
        model=_MODEL,
        detect_drift_against=detect_against,
        drift_history_dir=str(history_dir),
        on_drift="succeed",
    )
    op.execute(context={})

    persisted_diff = history_dir / "diff.json"
    assert persisted_diff.exists()
    loaded = load_diff_report(persisted_diff)
    assert loaded is not None
    assert loaded.model_unique_id == _MODEL
    # The grade sidecar was copied alongside it.
    assert (history_dir / "grade.json").exists()


# --------------------------------------------------------------------------- #
# SignalForgeDriftOperator — dedicated, reads two diff.json sidecars
# --------------------------------------------------------------------------- #


def test_drift_operator_signal_rot_on_fail_raises_airflow_fail_exception(tmp_path: Path) -> None:
    """Signal-rot pair with ``on_drift='fail'`` → AirflowFailException (no retry)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowFailException

    prev_diff, curr_diff = _layout_pair(tmp_path)
    op = _drift_operator_class()(
        task_id="drift",
        previous_diff_path=prev_diff,
        current_diff_path=curr_diff,
        on_drift="fail",
    )
    with pytest.raises(AirflowFailException):
        op.execute(context={})


def test_drift_operator_signal_rot_on_skip_raises_airflow_skip_exception(tmp_path: Path) -> None:
    """Signal-rot pair with ``on_drift='skip'`` → AirflowSkipException."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowSkipException

    prev_diff, curr_diff = _layout_pair(tmp_path)
    op = _drift_operator_class()(
        task_id="drift",
        previous_diff_path=prev_diff,
        current_diff_path=curr_diff,
        on_drift="skip",
    )
    with pytest.raises(AirflowSkipException):
        op.execute(context={})


def test_drift_operator_signal_rot_on_succeed_returns_drift_xcom(tmp_path: Path) -> None:
    """Signal-rot pair with ``on_drift='succeed'`` → no raise; returns ``drift.to_xcom()`` directly.

    The dedicated operator's XCom IS the drift payload (NOT nested under a
    ``"drift"`` key — that nesting is the generate operator's shape).
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    prev_diff, curr_diff = _layout_pair(tmp_path)
    op = _drift_operator_class()(
        task_id="drift",
        previous_diff_path=prev_diff,
        current_diff_path=curr_diff,
        on_drift="succeed",
    )
    xcom = op.execute(context={})

    assert xcom["model_unique_id"] == _MODEL
    assert xcom["alarming"] is True
    assert xcom["baseline"] is False
    assert xcom["counts"]["newly_always_passes"] == 1
    assert xcom["counts"]["grade_regressions"] == 1
    json.dumps(xcom)


def test_drift_operator_baseline_when_prior_missing_succeeds(tmp_path: Path) -> None:
    """A missing PRIOR diff → baseline DriftReport → SUCCESS, returns the baseline XCom (DEC-013).

    ``on_drift='fail'`` does not trip because a baseline is never alarming.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    prev_diff, curr_diff = _layout_pair(tmp_path, prior=False)
    op = _drift_operator_class()(
        task_id="drift",
        previous_diff_path=prev_diff,  # configured path, but no file present
        current_diff_path=curr_diff,
        on_drift="fail",
    )
    xcom = op.execute(context={})

    assert xcom["baseline"] is True
    assert xcom["alarming"] is False
    assert xcom["model_unique_id"] == _MODEL


def test_drift_operator_missing_current_diff_raises_config_error(tmp_path: Path) -> None:
    """A missing/unreadable CURRENT diff is a hard config error, NOT a baseline (DEC-010).

    The dedicated operator reads the current diff from a path (it runs no CLI), so
    an absent current file means the operator is misconfigured — it raises
    ``AirflowConfigError`` rather than silently degrading.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from signalforge.airflow.errors import AirflowConfigError

    prev_diff, curr_diff = _layout_pair(tmp_path)
    # Remove the current diff so the load returns None.
    Path(curr_diff).unlink()
    op = _drift_operator_class()(
        task_id="drift",
        previous_diff_path=prev_diff,
        current_diff_path=curr_diff,
        on_drift="fail",
    )
    with pytest.raises(AirflowConfigError):
        op.execute(context={})


def test_drift_operator_construction_rejects_empty_current_path() -> None:
    """``current_diff_path`` is required — empty fails fast at __init__ (DEC-010/DEC-012)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from signalforge.airflow.errors import AirflowConfigError

    with pytest.raises(AirflowConfigError):
        _drift_operator_class()(
            task_id="drift",
            previous_diff_path="/prev/diff.json",
            current_diff_path="",
        )


def test_drift_operator_construction_rejects_bad_on_drift() -> None:
    """An invalid ``on_drift`` fails fast at __init__ (DEC-010/DEC-012)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from signalforge.airflow.errors import AirflowConfigError

    with pytest.raises(AirflowConfigError):
        _drift_operator_class()(
            task_id="drift",
            previous_diff_path="/prev/diff.json",
            current_diff_path="/curr/diff.json",
            on_drift="bogus",
        )
