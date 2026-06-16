"""Gated tests for the shipped Airflow example DAG (`examples/airflow/`).

Belt-and-suspenders gating per `testing-signal.md`:

1. ``pytestmark = pytest.mark.airflow`` — deselected by the default ``addopts``
   ``-m 'not ... and not airflow'`` so a plain ``uv run pytest`` never imports
   Airflow.
2. A runtime ``importorskip`` — clear skip-with-reason when a maintainer runs
   ``-m airflow`` in an env where Airflow isn't installed.

The parse test runs unconditionally inside the gated `airflow` job (no
credentials). The live test additionally self-skips without the live env
(``SF_RUN_AIRFLOW=1`` + the Anthropic/BigQuery gates) — it spends real
Anthropic + warehouse budget.

Run: ``uv run --no-sync pytest -m airflow --no-cov`` inside the constraints-pinned
Airflow venv (see docs/airflow-ops.md / docs/research/airflow-test-environment.md).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.airflow

_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "airflow"

_AIRFLOW_SKIP = "Apache Airflow not installed (run inside the constraints-pinned airflow venv)"


def _load_example_dag(dag_id: str = "signalforge_generate"):
    from airflow.models.dagbag import DagBag

    # The examples folder ships FOUR DAGs (the two-PythonOperator pipeline
    # example, the single-task generate drift monitor, the no-LLM prune-existing
    # signal-rot monitor, and the Connection-configured both-operators hook
    # example); ALL must parse with no import errors regardless of which one the
    # caller asked for.
    bag = DagBag(dag_folder=str(_EXAMPLES_DIR), include_examples=False)
    assert bag.import_errors == {}, f"DAG import errors: {bag.import_errors}"
    # Read the in-memory parsed-DAG dict, NOT bag.get_dag(): get_dag() consults the
    # metadata DB (DagModel.get_current), which requires `airflow db init`. Parse
    # certification must not need a DB — bag.dags is populated purely from parsing.
    assert dag_id in bag.dags, f"parsed dags: {list(bag.dags)}"
    return bag.dags[dag_id]


def test_example_dag_parses_without_import_errors() -> None:
    """The shipped example DAG folder parses cleanly via DagBag — no import errors.

    Acceptance signal for the Airflow example: the DAG authored against the
    chosen Airflow floor parses in-process with both tasks present.
    """
    # Runtime gate (inside the test, not at module scope) so a default
    # `uv run pytest` collection never imports Airflow even when deselected.
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    dag = _load_example_dag()
    assert set(dag.task_ids) == {"generate", "gate"}
    # `generate` feeds `gate` — the result→task-state + XCom contract the example teaches.
    assert dag.get_task("gate").upstream_task_ids == {"generate"}


def test_operator_example_dag_parses_without_import_errors() -> None:
    """The operator-based drift-monitor example DAG parses cleanly via DagBag.

    Distinct ``dag_id`` from the two-PythonOperator example; a SINGLE task built
    with the dedicated ``SignalForgeGenerateOperator``. Parses with NO SignalForge
    config in the env (the DAG's ``_config`` fallbacks keep the operator's
    construction-time validation green at parse).
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    dag = _load_example_dag("signalforge_generate_operator")
    assert set(dag.task_ids) == {"drift_monitor"}


def test_operator_renders_templated_fields() -> None:
    """The operator's ``template_fields`` render from the task context.

    Constructs the operator inside a DAG context and calls
    ``render_template_fields`` with a synthetic context carrying ``ds`` + ``params``;
    asserts the templated ``select`` (``{{ params.select }}``) and ``as_of``
    (``{{ ds }}``) render to the injected values — the feature the example DAG
    demonstrates.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    import importlib
    from datetime import datetime

    from airflow import DAG

    operators = importlib.import_module("signalforge.airflow.operators")
    operator_cls = operators.SignalForgeGenerateOperator

    with DAG(dag_id="render_test", start_date=datetime(2026, 1, 1), schedule=None):
        op = operator_cls(
            task_id="drift_monitor",
            project_dir="/proj",
            select="{{ params.select }}",
            as_of="{{ ds }}",
        )

    # BaseOperator.render_template_fields(context, jinja_env=None) renders every
    # template_fields attr IN PLACE from the context (jinja_env built from the DAG).
    op.render_template_fields({"ds": "2026-06-15", "params": {"select": "tag:staging"}})

    assert op.select == "tag:staging"
    assert op.as_of == "2026-06-15"
    # Non-templated / no-Jinja fields are untouched by the render pass.
    assert op.project_dir == "/proj"
    assert op.model is None


def test_prune_existing_operator_example_dag_parses_without_import_errors() -> None:
    """The no-LLM prune-existing signal-rot-monitor example DAG parses cleanly via DagBag.

    Distinct ``dag_id`` from the generate examples; a SINGLE task built with the
    dedicated ``SignalForgePruneExistingOperator`` (#233). Parses with NO
    SignalForge config in the env (the DAG's ``_config`` fallbacks keep the
    operator's construction-time validation green at parse — ``project_dir`` /
    ``model`` / ``schema`` all resolve to non-empty defaults).
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    dag = _load_example_dag("signalforge_prune_existing_operator")
    assert set(dag.task_ids) == {"signal_rot_monitor"}


def test_prune_existing_operator_renders_templated_fields() -> None:
    """The prune-existing operator's ``template_fields`` render from the task context.

    Constructs the operator inside a DAG context and calls
    ``render_template_fields`` with a synthetic context carrying ``ds`` +
    ``params``; asserts the templated ``model`` (``{{ params.model }}``),
    ``schema`` (``{{ params.schema }}``), and ``as_of`` (``{{ ds }}``) render to
    the injected values — the feature the example DAG demonstrates (#233 DEC-007).
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    import importlib
    from datetime import datetime

    from airflow import DAG

    operators = importlib.import_module("signalforge.airflow.operators")
    operator_cls = operators.SignalForgePruneExistingOperator

    with DAG(dag_id="render_test_prune", start_date=datetime(2026, 1, 1), schedule=None):
        op = operator_cls(
            task_id="signal_rot_monitor",
            project_dir="/proj",
            model="{{ params.model }}",
            schema="{{ params.schema }}",
            as_of="{{ ds }}",
        )

    # BaseOperator.render_template_fields(context, jinja_env=None) renders every
    # template_fields attr IN PLACE from the context (jinja_env built from the DAG).
    op.render_template_fields(
        {
            "ds": "2026-06-15",
            "params": {
                "model": "models/staging/stg_orders.sql",
                "schema": "models/staging/schema.yml",
            },
        }
    )

    assert op.model == "models/staging/stg_orders.sql"
    assert op.schema == "models/staging/schema.yml"
    assert op.as_of == "2026-06-15"
    # Non-templated / no-Jinja fields are untouched by the render pass.
    assert op.project_dir == "/proj"
    assert op.tests_dir is None


def test_hook_operator_example_dag_parses_without_import_errors() -> None:
    """The Connection-configured both-operators example DAG parses cleanly via DagBag.

    Distinct ``dag_id`` from the single-operator examples; TWO tasks (the
    ``SignalForgeGenerateOperator`` drift monitor + the
    ``SignalForgePruneExistingOperator`` signal-rot monitor) both wired to ONE
    ``signalforge_conn_id`` (+ the ``signalforge_api_key`` Variable) with no inline
    per-task env — the #234 acceptance shape (A8). Parses with NO SignalForge config
    in the env (the DAG's ``_config`` fallbacks keep both operators' construction-time
    validation green at parse; the conn id is a literal constant, resolved only at
    ``execute`` time, so parse needs no Connection backend).
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    dag = _load_example_dag("signalforge_hook")
    assert set(dag.task_ids) == {"drift_monitor", "signal_rot_monitor"}


def _live_skip_reason() -> str | None:
    missing = [
        v
        for v in ("SF_RUN_AIRFLOW", "ANTHROPIC_API_KEY", "GOOGLE_CLOUD_PROJECT", "SF_RUN_BQ")
        if not os.environ.get(v)
    ]
    return f"live Airflow e2e needs: {', '.join(missing)}" if missing else None


# External-service + subprocess markers (per testing-signal.md). Stacks with the
# module-level `airflow` marker; the `-m airflow` CI job still selects this test
# (it self-skips without the live env). Applied per-function so the parse test,
# which needs no credentials, stays `airflow`-only.
@pytest.mark.e2e
@pytest.mark.anthropic
@pytest.mark.bigquery
@pytest.mark.cli_subprocess
def test_generate_task_runs_live_against_demo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LIVE: the `generate` task runs the real pipeline against an init-demo project.

    Gated by SF_RUN_AIRFLOW + ANTHROPIC_API_KEY + GOOGLE_CLOUD_PROJECT + SF_RUN_BQ
    (belt-and-suspenders with the marker). Spends real Anthropic + BigQuery budget.
    Exercises the example's exit-code handling + XCom shape end-to-end.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    reason = _live_skip_reason()
    if reason:
        pytest.skip(reason)

    from signalforge.demo import copy_demo

    project_dir = copy_demo(tmp_path / "demo")
    monkeypatch.setenv("SF_PROJECT_DIR", str(project_dir))
    monkeypatch.setenv("SF_MODEL", "models/staging/stg_bikeshare_trips.sql")

    dag = _load_example_dag()
    result = dag.get_task("generate").python_callable()  # type: ignore[attr-defined]

    assert result["model_unique_id"].endswith("stg_bikeshare_trips")
    # Every tier-count key the example pushes to XCom must be present + non-negative.
    for key in ("kept", "kept_uncertain", "dropped", "flagged", "proposed_test_files"):
        assert isinstance(result[key], int) and result[key] >= 0
