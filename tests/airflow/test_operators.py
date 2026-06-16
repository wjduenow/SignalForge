"""Gated tests for ``SignalForgeGenerateOperator.execute`` (#232 US-003).

Belt-and-suspenders gating per ``testing-signal.md`` and the
``tests/airflow/test_outcome_translation.py`` precedent:

1. ``pytestmark = pytest.mark.airflow`` — every test is deselected by the default
   ``addopts`` ``-m '... and not airflow'`` so a plain ``uv run pytest`` never
   imports Apache Airflow.
2. A runtime ``pytest.importorskip("airflow")`` as the FIRST line of each test
   (NOT a module-scope import — that would run at collection time even when
   deselected, and ``tests/airflow/test_operators.py`` IS collected by the
   default run before deselection). The skip carries a clear reason when a
   maintainer runs ``-m airflow`` without Airflow installed.

Run inside the constraints-pinned Airflow venv (see
docs/research/airflow-test-environment.md):
``uv run --no-sync pytest -m airflow --no-cov``.

These pin the operator's ``execute`` contract: the argv it builds, the
single-model vs. ``--select`` batch dispatch, the result → task-state translation
(``decide_task_outcome`` → ``raise_for_outcome`` → the matching Airflow signal),
and the XCom payload shape. ``run_signalforge`` (and, for batch,
``_resolve_select_models``) are monkeypatched to canned values so no real
``signalforge`` run happens.
"""

from __future__ import annotations

import importlib

import pytest

from signalforge.airflow.result import SignalForgeRunResult

pytestmark = pytest.mark.airflow

_AIRFLOW_SKIP = "Apache Airflow not installed (run inside the constraints-pinned airflow venv)"


def _result(
    *,
    exit_code: int = 0,
    flagged: int = 0,
    model_unique_ids: tuple[str, ...] = ("model.demo.stg_trips",),
    kept: int = 4,
    kept_uncertain: int = 2,
    dropped: int = 7,
    mean_grade: float | None = 0.9,
    duration_seconds: float | None = 12.5,
) -> SignalForgeRunResult:
    """Build a canned :class:`SignalForgeRunResult` (mirrors test_runner shapes)."""
    return SignalForgeRunResult(
        exit_code=exit_code,
        model_unique_ids=model_unique_ids,
        kept=kept,
        kept_uncertain=kept_uncertain,
        dropped=dropped,
        flagged=flagged,
        mean_grade=mean_grade,
        diff_sidecar_path="/proj/.signalforge/diff.json",
        grade_sidecar_path="/proj/.signalforge/grade.json",
        duration_seconds=duration_seconds,
        stdout="",
        stderr="",
    )


def _operator_class() -> type:
    """Resolve the real operator class (airflow present — built by the factory)."""
    operators = importlib.import_module("signalforge.airflow.operators")
    return operators.SignalForgeGenerateOperator


def _patch_run(
    monkeypatch: pytest.MonkeyPatch, results: list[SignalForgeRunResult]
) -> list[list[str]]:
    """Monkeypatch ``operators.run_signalforge`` to return ``results`` in order.

    Returns a list that captures the argv passed to each call (one entry per
    invocation, in call order), so tests can assert the built argv.
    """
    captured: list[list[str]] = []
    queue = list(results)

    def _fake_run(
        argv: list[str],
        *,
        project_dir: object,
        invocation: object = "in_process",
        timeout_seconds: object = None,
    ) -> SignalForgeRunResult:
        captured.append(list(argv))
        return queue.pop(0)

    monkeypatch.setattr("signalforge.airflow.operators.run_signalforge", _fake_run)
    return captured


# --------------------------------------------------------------------------- #
# Single-model dispatch
# --------------------------------------------------------------------------- #


def test_single_model_success_returns_xcom_and_builds_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit 0 / flagged 0 → no raise, returns ``to_xcom()``; argv is correct."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    result = _result(exit_code=0, flagged=0)
    captured = _patch_run(monkeypatch, [result])

    op = _operator_class()(
        task_id="gen",
        project_dir="/proj",
        model="model.demo.stg_trips",
    )
    xcom = op.execute(context={})

    assert xcom == result.to_xcom()
    # One run, argv = the single-model generate form (write False → --dry-run,
    # --format json always present, no --select / --cache-scope / --as-of).
    assert len(captured) == 1
    argv = captured[0]
    assert argv == [
        "generate",
        "model.demo.stg_trips",
        "--project-dir",
        "/proj",
        "--format",
        "json",
        "--dry-run",
    ]


def test_single_model_flagged_fail_raises_airflow_fail_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit 0, flagged>0, on_flagged='fail' (default) → AirflowFailException."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowFailException

    _patch_run(monkeypatch, [_result(exit_code=0, flagged=1)])

    op = _operator_class()(task_id="gen", project_dir="/proj", model="m")
    with pytest.raises(AirflowFailException):
        op.execute(context={})


def test_single_model_flagged_skip_raises_airflow_skip_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit 0, flagged>0, on_flagged='skip' → AirflowSkipException."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowSkipException

    _patch_run(monkeypatch, [_result(exit_code=0, flagged=1)])

    op = _operator_class()(task_id="gen", project_dir="/proj", model="m", on_flagged="skip")
    with pytest.raises(AirflowSkipException):
        op.execute(context={})


def test_single_model_flagged_succeed_returns_xcom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit 0, flagged>0, on_flagged='succeed' → no raise, returns XCom."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    result = _result(exit_code=0, flagged=3)
    _patch_run(monkeypatch, [result])

    op = _operator_class()(task_id="gen", project_dir="/proj", model="m", on_flagged="succeed")
    xcom = op.execute(context={})
    assert xcom == result.to_xcom()
    assert xcom["flagged"] == 3
    assert xcom["below_threshold"] is True


def test_single_model_exit1_raises_airflow_fail_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit 1 (load/parse) → AirflowFailException (no retry)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowFailException

    _patch_run(monkeypatch, [_result(exit_code=1, flagged=0)])
    op = _operator_class()(task_id="gen", project_dir="/proj", model="m")
    with pytest.raises(AirflowFailException):
        op.execute(context={})


def test_single_model_exit2_raises_airflow_fail_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit 2 (input-validation) → AirflowFailException (no retry)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowFailException

    _patch_run(monkeypatch, [_result(exit_code=2, flagged=0)])
    op = _operator_class()(task_id="gen", project_dir="/proj", model="m")
    with pytest.raises(AirflowFailException):
        op.execute(context={})


def test_single_model_exit3_raises_retryable_airflow_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit 3 (external dependency) → base AirflowException (retryable)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowException, AirflowFailException, AirflowSkipException

    _patch_run(monkeypatch, [_result(exit_code=3, flagged=0)])
    op = _operator_class()(task_id="gen", project_dir="/proj", model="m")
    with pytest.raises(AirflowException) as exc_info:
        op.execute(context={})
    # Must be the BASE AirflowException (retryable), not the no-retry / skip kinds.
    assert type(exc_info.value) is AirflowException
    assert not isinstance(exc_info.value, (AirflowFailException, AirflowSkipException))


def test_single_model_passes_flags_through_to_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write / no_grade / cache_scope / as_of / profiles_dir reach the argv."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    captured = _patch_run(monkeypatch, [_result(exit_code=0, flagged=0)])
    op = _operator_class()(
        task_id="gen",
        project_dir="/proj",
        model="m",
        write=True,
        no_grade=True,
        cache_scope="per-model",
        as_of="2026-06-15",
        profiles_dir="/profiles",
    )
    op.execute(context={})
    argv = captured[0]
    assert "--write" in argv and "--dry-run" not in argv
    assert "--no-grade" in argv
    assert argv[argv.index("--as-of") + 1] == "2026-06-15"
    assert argv[argv.index("--cache-scope") + 1] == "per-model"
    assert argv[argv.index("--profiles-dir") + 1] == "/profiles"


# --------------------------------------------------------------------------- #
# --select batch dispatch
# --------------------------------------------------------------------------- #


def test_batch_loops_per_model_forces_project_cache_and_returns_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ≥2-model batch loops once per model, forces ``--cache-scope project``
    (operator cache_scope unset, DEC-007), aggregates the per-model results, and
    returns the ``{"models": [...], "aggregate": ...}`` XCom (DEC-010)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    model_ids = ("model.p.a", "model.p.b")
    monkeypatch.setattr(
        "signalforge.airflow.operators._resolve_select_models",
        lambda project_dir, select: model_ids,
    )
    r_a = _result(exit_code=0, flagged=0, model_unique_ids=("model.p.a",), kept=3)
    r_b = _result(exit_code=0, flagged=0, model_unique_ids=("model.p.b",), kept=5)
    captured = _patch_run(monkeypatch, [r_a, r_b])

    op = _operator_class()(task_id="gen", project_dir="/proj", select="tag:staging")
    xcom = op.execute(context={})

    # Looped twice, once per resolved model id (as the positional model arg).
    assert len(captured) == 2
    assert captured[0][1] == "model.p.a"
    assert captured[1][1] == "model.p.b"
    # cache_scope forced to project for the ≥2-model batch.
    for argv in captured:
        assert argv[argv.index("--cache-scope") + 1] == "project"
        assert "--select" not in argv  # each loop is a single-model run

    # Aggregate XCom shape (DEC-010): per-model list + one rolled-up aggregate.
    assert set(xcom) == {"models", "aggregate"}
    assert [m["model_unique_ids"] for m in xcom["models"]] == [["model.p.a"], ["model.p.b"]]
    agg = xcom["aggregate"]
    assert agg["kept"] == 8  # 3 + 5
    assert agg["model_unique_ids"] == ["model.p.a", "model.p.b"]
    assert agg["exit_code"] == 0


def test_batch_aggregated_exit_code_drives_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch outcome is driven by the AGGREGATE (max) exit code: one exit-3
    model makes the whole task a retryable AirflowException."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowException

    model_ids = ("model.p.a", "model.p.b")
    monkeypatch.setattr(
        "signalforge.airflow.operators._resolve_select_models",
        lambda project_dir, select: model_ids,
    )
    _patch_run(
        monkeypatch,
        [
            _result(exit_code=0, flagged=0, model_unique_ids=("model.p.a",)),
            _result(exit_code=3, flagged=0, model_unique_ids=("model.p.b",)),
        ],
    )

    op = _operator_class()(task_id="gen", project_dir="/proj", select="tag:staging")
    with pytest.raises(AirflowException) as exc_info:
        op.execute(context={})
    # exit 3 → retryable base AirflowException (max over [0, 3] = 3).
    from airflow.exceptions import AirflowFailException, AirflowSkipException

    assert not isinstance(exc_info.value, (AirflowFailException, AirflowSkipException))


def test_batch_honours_explicit_cache_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit operator ``cache_scope`` is NOT overridden by the ≥2-model
    auto-promote (DEC-007 only forces project when the operator left it unset)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    model_ids = ("model.p.a", "model.p.b")
    monkeypatch.setattr(
        "signalforge.airflow.operators._resolve_select_models",
        lambda project_dir, select: model_ids,
    )
    captured = _patch_run(
        monkeypatch,
        [
            _result(exit_code=0, flagged=0, model_unique_ids=("model.p.a",)),
            _result(exit_code=0, flagged=0, model_unique_ids=("model.p.b",)),
        ],
    )

    op = _operator_class()(
        task_id="gen", project_dir="/proj", select="tag:staging", cache_scope="per-model"
    )
    op.execute(context={})
    for argv in captured:
        assert argv[argv.index("--cache-scope") + 1] == "per-model"


def test_batch_single_match_does_not_force_cache_and_keeps_batch_xcom_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``--select`` that resolves to exactly ONE model is still the batch path:
    cache_scope is NOT force-promoted (DEC-007 needs ≥2 models) yet the XCom keeps
    the ``{"models": [...], "aggregate": ...}`` batch shape (DEC-010 keys on the
    ``select`` param, not the match count)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    monkeypatch.setattr(
        "signalforge.airflow.operators._resolve_select_models",
        lambda project_dir, select: ("model.p.only",),
    )
    captured = _patch_run(
        monkeypatch,
        [_result(exit_code=0, flagged=0, model_unique_ids=("model.p.only",), kept=4)],
    )

    op = _operator_class()(task_id="gen", project_dir="/proj", select="tag:rare")
    xcom = op.execute(context={})

    # One loop iteration; cache_scope left unset (NOT forced — only one model).
    assert len(captured) == 1
    assert "--cache-scope" not in captured[0]
    # Still the batch XCom shape even with a single match.
    assert set(xcom) == {"models", "aggregate"}
    assert len(xcom["models"]) == 1
    assert xcom["aggregate"]["model_unique_ids"] == ["model.p.only"]


# --------------------------------------------------------------------------- #
# Construction-time validation (fail fast at DAG-parse)
# --------------------------------------------------------------------------- #


def test_construction_rejects_model_and_select_both_set() -> None:
    """The mutex is enforced at __init__ (DAG-parse fail-fast)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from signalforge.airflow.errors import AirflowConfigError

    with pytest.raises(AirflowConfigError):
        _operator_class()(task_id="gen", project_dir="/proj", model="m", select="tag:x")


def test_construction_rejects_bad_on_flagged() -> None:
    """An invalid ``on_flagged`` fails fast at __init__."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from signalforge.airflow.errors import AirflowConfigError

    with pytest.raises(AirflowConfigError):
        _operator_class()(task_id="gen", project_dir="/proj", model="m", on_flagged="bogus")
