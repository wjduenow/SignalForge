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
import json
import os

import pytest

from signalforge.airflow._resolve import HookResolution
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
    # Per-model sidecar paths are nulled — unstable under a shared batch sidecar
    # (the canned results carry paths; the operator strips them).
    for m in xcom["models"]:
        assert m["diff_sidecar_path"] is None
        assert m["grade_sidecar_path"] is None
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


# --------------------------------------------------------------------------- #
# signalforge_conn_id wiring (#234 US-005)                                    #
# --------------------------------------------------------------------------- #


def _patch_run_capturing_env(
    monkeypatch: pytest.MonkeyPatch,
    results: list[SignalForgeRunResult],
    env_var: str,
) -> tuple[list[list[str]], list[str | None]]:
    """Patch ``run_signalforge`` recording (argv, ``os.environ[env_var]``) per call.

    The env value is sampled INSIDE the fake at call time, so a test can assert
    the provider key was injected for the duration of the run (and later that it
    was restored after ``execute`` returns / raises).
    """
    captured_argv: list[list[str]] = []
    captured_env: list[str | None] = []
    queue = list(results)

    def _fake_run(
        argv: list[str],
        *,
        project_dir: object,
        invocation: object = "in_process",
        timeout_seconds: object = None,
    ) -> SignalForgeRunResult:
        captured_argv.append(list(argv))
        captured_env.append(os.environ.get(env_var))
        return queue.pop(0)

    monkeypatch.setattr("signalforge.airflow.operators.run_signalforge", _fake_run)
    return captured_argv, captured_env


def _patch_hook(monkeypatch: pytest.MonkeyPatch, resolution: HookResolution) -> list[str]:
    """Patch ``_resolve_hook`` to return ``resolution`` + ``register_secret`` to record.

    Returns the list of values passed to ``register_secret`` (so a test can
    assert the resolved key was masked before the run).
    """
    masked: list[str] = []
    monkeypatch.setattr(
        "signalforge.airflow.operators._resolve_hook",
        lambda conn_id: resolution,
    )
    monkeypatch.setattr(
        "signalforge.airflow.operators.register_secret",
        lambda value: masked.append(value),
    )
    return masked


def test_single_model_conn_id_injects_env_masks_key_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """conn_id set → env var injected DURING the run, masked, restored after."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    resolution = HookResolution(
        profiles_dir="/conn/profiles", provider="anthropic", api_key="sk-conn-secret"
    )
    masked = _patch_hook(monkeypatch, resolution)
    argv_cap, env_cap = _patch_run_capturing_env(
        monkeypatch, [_result(exit_code=0, flagged=0)], "ANTHROPIC_API_KEY"
    )

    op = _operator_class()(
        task_id="gen", project_dir="/proj", model="m", signalforge_conn_id="sf_default"
    )
    op.execute(context={})

    # Injected for the run, then restored (absent before → absent after).
    assert env_cap == ["sk-conn-secret"]
    assert "ANTHROPIC_API_KEY" not in os.environ
    # Masked before the run (DEC-006).
    assert masked == ["sk-conn-secret"]
    # The conn-resolved profiles_dir reaches the argv (no operator override).
    argv = argv_cap[0]
    assert argv[argv.index("--profiles-dir") + 1] == "/conn/profiles"


def test_single_model_conn_id_none_unchanged_no_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """conn_id=None (#232 default): no hook touched, no conn-derived argv."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    def _boom(_conn_id: str) -> HookResolution:
        raise AssertionError("_resolve_hook must NOT be called when conn_id is None")

    monkeypatch.setattr("signalforge.airflow.operators._resolve_hook", _boom)
    captured = _patch_run(monkeypatch, [_result(exit_code=0, flagged=0)])

    op = _operator_class()(task_id="gen", project_dir="/proj", model="m")
    op.execute(context={})

    # Hook never called (the _boom guard); argv carries no conn-derived profiles_dir.
    argv = captured[0]
    assert argv[:2] == ["generate", "m"]
    assert "--profiles-dir" not in argv


def test_single_model_conn_env_restored_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception inside the run (exit 3 → raise) still restores the env var."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowException

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resolution = HookResolution(profiles_dir=None, provider="anthropic", api_key="sk-conn-secret")
    _patch_hook(monkeypatch, resolution)
    _patch_run_capturing_env(monkeypatch, [_result(exit_code=3, flagged=0)], "ANTHROPIC_API_KEY")

    op = _operator_class()(task_id="gen", project_dir="/proj", model="m", signalforge_conn_id="sf")
    with pytest.raises(AirflowException):
        op.execute(context={})
    # raise_for_outcome raises INSIDE the _provider_key_env block → finally restores.
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_single_model_conn_prior_env_value_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing ambient key is overlaid for the run, then restored."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-prior")
    resolution = HookResolution(profiles_dir=None, provider="anthropic", api_key="sk-conn-secret")
    _patch_hook(monkeypatch, resolution)
    _, env_cap = _patch_run_capturing_env(
        monkeypatch, [_result(exit_code=0, flagged=0)], "ANTHROPIC_API_KEY"
    )

    op = _operator_class()(task_id="gen", project_dir="/proj", model="m", signalforge_conn_id="sf")
    op.execute(context={})
    assert env_cap == ["sk-conn-secret"]
    assert os.environ["ANTHROPIC_API_KEY"] == "ambient-prior"


def test_conn_explicit_param_beats_extra_profiles_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit operator ``profiles_dir`` wins over the Connection extra (DEC-012)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resolution = HookResolution(profiles_dir="/conn/profiles", provider="anthropic", api_key="sk")
    _patch_hook(monkeypatch, resolution)
    argv_cap, _ = _patch_run_capturing_env(
        monkeypatch, [_result(exit_code=0, flagged=0)], "ANTHROPIC_API_KEY"
    )

    op = _operator_class()(
        task_id="gen",
        project_dir="/proj",
        model="m",
        profiles_dir="/op/profiles",
        signalforge_conn_id="sf",
    )
    op.execute(context={})
    argv = argv_cap[0]
    assert argv[argv.index("--profiles-dir") + 1] == "/op/profiles"


def test_conn_cache_scope_from_extra_reaches_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Connection-extra ``cache_scope`` is precedence-merged into the argv (DEC-012)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    resolution = HookResolution(
        profiles_dir=None, provider="gemini", api_key="sk", cache_scope="project"
    )
    _patch_hook(monkeypatch, resolution)
    argv_cap, env_cap = _patch_run_capturing_env(
        monkeypatch, [_result(exit_code=0, flagged=0)], "GOOGLE_API_KEY"
    )

    op = _operator_class()(task_id="gen", project_dir="/proj", model="m", signalforge_conn_id="sf")
    op.execute(context={})
    argv = argv_cap[0]
    assert argv[argv.index("--cache-scope") + 1] == "project"
    # The provider→env-var mapping picks GOOGLE_API_KEY for gemini (#234 US-001).
    assert env_cap == ["sk"]
    assert "GOOGLE_API_KEY" not in os.environ


def test_conn_api_key_absent_from_returned_xcom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolved key never enters the returned XCom payload (DEC-007)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resolution = HookResolution(profiles_dir=None, provider="anthropic", api_key="sk-conn-secret")
    _patch_hook(monkeypatch, resolution)
    _patch_run_capturing_env(monkeypatch, [_result(exit_code=0, flagged=0)], "ANTHROPIC_API_KEY")

    op = _operator_class()(task_id="gen", project_dir="/proj", model="m", signalforge_conn_id="sf")
    xcom = op.execute(context={})
    assert "sk-conn-secret" not in json.dumps(xcom)


def test_conn_missing_key_raises_config_error_and_skips_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider present but api_key absent → AirflowConfigError before any run."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from signalforge.airflow.errors import AirflowConfigError

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_hook(monkeypatch, HookResolution(profiles_dir=None, provider="anthropic", api_key=None))
    argv_cap, _ = _patch_run_capturing_env(monkeypatch, [_result()], "ANTHROPIC_API_KEY")

    op = _operator_class()(task_id="gen", project_dir="/proj", model="m", signalforge_conn_id="sf")
    with pytest.raises(AirflowConfigError):
        op.execute(context={})
    # Fails before run_signalforge is reached, and leaks no env var.
    assert argv_cap == []
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_conn_missing_provider_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """api_key present but provider absent → AirflowConfigError (generate needs both)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from signalforge.airflow.errors import AirflowConfigError

    _patch_hook(monkeypatch, HookResolution(profiles_dir=None, provider=None, api_key="sk"))
    argv_cap, _ = _patch_run_capturing_env(monkeypatch, [_result()], "ANTHROPIC_API_KEY")

    op = _operator_class()(task_id="gen", project_dir="/proj", model="m", signalforge_conn_id="sf")
    with pytest.raises(AirflowConfigError):
        op.execute(context={})
    assert argv_cap == []


def test_conn_id_not_in_template_fields() -> None:
    """``signalforge_conn_id`` is NOT a templated field (DEC-007)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    assert "signalforge_conn_id" not in _operator_class().template_fields


def test_batch_with_conn_id_injects_env_for_every_model_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A --select batch injects the env var around EACH per-model run + restores it,
    and the conn-resolved profiles_dir (+ forced project cache) reach each argv."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "signalforge.airflow.operators._resolve_select_models",
        lambda project_dir, select: ("model.p.a", "model.p.b"),
    )
    resolution = HookResolution(
        profiles_dir="/conn/profiles", provider="anthropic", api_key="sk-conn-secret"
    )
    _patch_hook(monkeypatch, resolution)
    argv_cap, env_cap = _patch_run_capturing_env(
        monkeypatch,
        [
            _result(exit_code=0, flagged=0, model_unique_ids=("model.p.a",)),
            _result(exit_code=0, flagged=0, model_unique_ids=("model.p.b",)),
        ],
        "ANTHROPIC_API_KEY",
    )

    op = _operator_class()(
        task_id="gen", project_dir="/proj", select="tag:staging", signalforge_conn_id="sf"
    )
    op.execute(context={})

    # Env var present for BOTH per-model runs, restored afterward.
    assert env_cap == ["sk-conn-secret", "sk-conn-secret"]
    assert "ANTHROPIC_API_KEY" not in os.environ
    # conn profiles_dir + the ≥2-model forced project cache reach each argv.
    for argv in argv_cap:
        assert argv[argv.index("--profiles-dir") + 1] == "/conn/profiles"
        assert argv[argv.index("--cache-scope") + 1] == "project"


# --------------------------------------------------------------------------- #
# SignalForgePruneExistingOperator (#233) — single-model, no-LLM, read-only
# --------------------------------------------------------------------------- #


def _prune_existing_operator_class() -> type:
    """Resolve the real prune-existing operator class (airflow present)."""
    operators = importlib.import_module("signalforge.airflow.operators")
    return operators.SignalForgePruneExistingOperator


def test_prune_existing_success_returns_xcom_and_builds_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit 0 / flagged 0 → no raise, returns ``to_xcom()`` (single dict); argv correct."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    result = _result(exit_code=0, flagged=0)
    captured = _patch_run(monkeypatch, [result])

    op = _prune_existing_operator_class()(
        task_id="prune",
        project_dir="/proj",
        model="model.demo.stg_trips",
        schema="models/staging/schema.yml",
    )
    xcom = op.execute(context={})

    # to_xcom() is a single dict (NOT the {"models", "aggregate"} batch shape).
    assert xcom == result.to_xcom()
    # One run — argv is the prune-existing form: positional model, required
    # --schema, --format json + --dry-run always present, no --select/--write.
    assert len(captured) == 1
    assert captured[0] == [
        "prune-existing",
        "model.demo.stg_trips",
        "--schema",
        "models/staging/schema.yml",
        "--project-dir",
        "/proj",
        "--format",
        "json",
        "--dry-run",
    ]


def test_prune_existing_passes_optional_flags_through_to_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """profiles_dir / manifest / scope / sample_strategy / as_of / tests_dir reach argv."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    captured = _patch_run(monkeypatch, [_result(exit_code=0, flagged=0)])
    op = _prune_existing_operator_class()(
        task_id="prune",
        project_dir="/proj",
        model="m",
        schema="s.yml",
        profiles_dir="/profiles",
        manifest="target/manifest.json",
        scope="sample",
        sample_strategy="oneshot",
        as_of="2026-06-15",
        tests_dir="tests",
    )
    op.execute(context={})
    argv = captured[0]
    assert argv[argv.index("--profiles-dir") + 1] == "/profiles"
    assert argv[argv.index("--manifest") + 1] == "target/manifest.json"
    assert argv[argv.index("--scope") + 1] == "sample"
    assert argv[argv.index("--sample-strategy") + 1] == "oneshot"
    assert argv[argv.index("--as-of") + 1] == "2026-06-15"
    assert argv[argv.index("--tests-dir") + 1] == "tests"
    # Read-only monitor — never --write, never --select (#233 DEC-001/DEC-003).
    assert "--write" not in argv
    assert "--select" not in argv


def test_prune_existing_exit1_raises_airflow_fail_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit 1 (load/parse) → AirflowFailException (no retry)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowFailException

    _patch_run(monkeypatch, [_result(exit_code=1, flagged=0)])
    op = _prune_existing_operator_class()(
        task_id="prune", project_dir="/proj", model="m", schema="s.yml"
    )
    with pytest.raises(AirflowFailException):
        op.execute(context={})


def test_prune_existing_exit2_raises_airflow_fail_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit 2 (input-validation, incl. IngestError/ModelNotFoundError) → AirflowFailException."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowFailException

    _patch_run(monkeypatch, [_result(exit_code=2, flagged=0)])
    op = _prune_existing_operator_class()(
        task_id="prune", project_dir="/proj", model="m", schema="s.yml"
    )
    with pytest.raises(AirflowFailException):
        op.execute(context={})


def test_prune_existing_exit3_raises_retryable_airflow_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit 3 (warehouse / external dependency) → base AirflowException (retryable)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from airflow.exceptions import AirflowException, AirflowFailException, AirflowSkipException

    _patch_run(monkeypatch, [_result(exit_code=3, flagged=0)])
    op = _prune_existing_operator_class()(
        task_id="prune", project_dir="/proj", model="m", schema="s.yml"
    )
    with pytest.raises(AirflowException) as exc_info:
        op.execute(context={})
    assert type(exc_info.value) is AirflowException
    assert not isinstance(exc_info.value, (AirflowFailException, AirflowSkipException))


@pytest.mark.parametrize("on_flagged", ["fail", "skip", "succeed"])
def test_prune_existing_on_flagged_is_inert_clean_run_always_succeeds(
    monkeypatch: pytest.MonkeyPatch, on_flagged: str
) -> None:
    """``on_flagged`` is inert without grading (#233 DEC-004): a clean exit-0 run
    (flagged always 0 on the no-LLM path) yields SUCCESS for ALL three values —
    no raise, returns the XCom dict."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    result = _result(exit_code=0, flagged=0)
    _patch_run(monkeypatch, [result])
    op = _prune_existing_operator_class()(
        task_id="prune",
        project_dir="/proj",
        model="m",
        schema="s.yml",
        on_flagged=on_flagged,
    )
    xcom = op.execute(context={})
    assert xcom == result.to_xcom()
    assert xcom["flagged"] == 0
    assert xcom["below_threshold"] is False


def test_prune_existing_construction_rejects_empty_schema() -> None:
    """``schema`` is required (#233 DEC-002) — empty fails fast at __init__."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from signalforge.airflow.errors import AirflowConfigError

    with pytest.raises(AirflowConfigError):
        _prune_existing_operator_class()(task_id="prune", project_dir="/proj", model="m", schema="")


def test_prune_existing_construction_rejects_bad_on_flagged() -> None:
    """An invalid ``on_flagged`` fails fast at __init__ (validated for symmetry)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    from signalforge.airflow.errors import AirflowConfigError

    with pytest.raises(AirflowConfigError):
        _prune_existing_operator_class()(
            task_id="prune", project_dir="/proj", model="m", schema="s.yml", on_flagged="bogus"
        )


# --------------------------------------------------------------------------- #
# SignalForgePruneExistingOperator + signalforge_conn_id (#234 US-006)         #
# Read-only path: resolves profiles_dir ONLY — NO key injection (DEC-016).     #
# --------------------------------------------------------------------------- #


def test_prune_existing_conn_id_resolves_profiles_dir_and_injects_no_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """conn_id set → resolved profiles_dir reaches argv; NO provider env injected.

    The resolution deliberately carries a ``provider`` + ``api_key`` to prove the
    prune-existing path IGNORES them: no ``register_secret`` call, no env var set
    or restored, the credential never touches ``os.environ`` (DEC-016).
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    resolution = HookResolution(
        profiles_dir="/conn/profiles", provider="anthropic", api_key="sk-conn-secret"
    )
    masked = _patch_hook(monkeypatch, resolution)
    argv_cap, env_cap = _patch_run_capturing_env(
        monkeypatch, [_result(exit_code=0, flagged=0)], "ANTHROPIC_API_KEY"
    )

    op = _prune_existing_operator_class()(
        task_id="prune",
        project_dir="/proj",
        model="m",
        schema="s.yml",
        signalforge_conn_id="sf_default",
    )
    op.execute(context={})

    # Conn-resolved profiles_dir reaches the argv (no operator override).
    argv = argv_cap[0]
    assert argv[argv.index("--profiles-dir") + 1] == "/conn/profiles"
    # NO key injection on the read-only path: register_secret never called, the
    # env var never set during the run, and untouched in os.environ afterward.
    assert masked == []
    assert env_cap == [None]
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_prune_existing_conn_explicit_param_beats_extra_profiles_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit operator ``profiles_dir`` wins over the Connection extra (DEC-012)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    resolution = HookResolution(profiles_dir="/conn/profiles", provider=None, api_key=None)
    _patch_hook(monkeypatch, resolution)
    captured = _patch_run(monkeypatch, [_result(exit_code=0, flagged=0)])

    op = _prune_existing_operator_class()(
        task_id="prune",
        project_dir="/proj",
        model="m",
        schema="s.yml",
        profiles_dir="/op/profiles",
        signalforge_conn_id="sf",
    )
    op.execute(context={})
    argv = captured[0]
    assert argv[argv.index("--profiles-dir") + 1] == "/op/profiles"


def test_prune_existing_conn_id_none_unchanged_no_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """conn_id=None (#233 default): no hook touched, argv byte-identical to #233."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    def _boom(_conn_id: str) -> HookResolution:
        raise AssertionError("_resolve_hook must NOT be called when conn_id is None")

    monkeypatch.setattr("signalforge.airflow.operators._resolve_hook", _boom)
    captured = _patch_run(monkeypatch, [_result(exit_code=0, flagged=0)])

    op = _prune_existing_operator_class()(
        task_id="prune", project_dir="/proj", model="m", schema="s.yml"
    )
    op.execute(context={})
    assert captured[0] == [
        "prune-existing",
        "m",
        "--schema",
        "s.yml",
        "--project-dir",
        "/proj",
        "--format",
        "json",
        "--dry-run",
    ]


def test_prune_existing_conn_id_not_in_template_fields() -> None:
    """``signalforge_conn_id`` is NOT a templated field on prune-existing (DEC-007)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    assert "signalforge_conn_id" not in _prune_existing_operator_class().template_fields
