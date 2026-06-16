"""Gated tests for the shipped Airflow example DAG (`examples/airflow/`).

Belt-and-suspenders gating per `testing-signal.md`:

1. ``pytestmark = pytest.mark.airflow`` — deselected by the default ``addopts``
   ``-m 'not ... and not airflow'`` so a plain ``uv run pytest`` never imports
   Airflow.
2. A runtime ``importorskip`` — clear skip-with-reason when a maintainer runs
   ``-m airflow`` in an env where Airflow isn't installed.

These parse + ``render_template_fields`` tests run unconditionally inside the
gated `airflow` job (no credentials). The full-stack LIVE e2e (the real
operator driven through ``dag.test()`` against the Austin fixture) lives in
``tests/airflow/test_e2e_generate_operator.py`` and self-skips without the live
env.

Run: ``uv run --no-sync pytest -m airflow --no-cov`` inside the constraints-pinned
Airflow venv (see docs/airflow-ops.md / docs/research/airflow-test-environment.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.airflow

_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "airflow"

_AIRFLOW_SKIP = "Apache Airflow not installed (run inside the constraints-pinned airflow venv)"


def _load_example_dag(dag_id: str = "signalforge_generate"):
    from airflow.models.dagbag import DagBag

    # The examples folder ships several DAGs (the two-PythonOperator pipeline
    # example, the single-task generate operator, the no-LLM prune-existing
    # signal-rot monitor, the Connection-configured hook example, the
    # run-over-run drift monitor, and the post-dbt-build prune example); ALL
    # must parse with no import errors regardless of which one the caller asked for.
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


def test_after_dbt_build_example_dag_parses_without_import_errors() -> None:
    """The post-dbt-build prune example DAG parses cleanly via DagBag (#236 DEC-002).

    Distinct ``dag_id`` from the other examples; TWO tasks demonstrating the
    "add SignalForge downstream of an existing dbt build" wiring: a plain
    ``BashOperator`` dbt stand-in (NO Cosmos dependency) feeding the no-LLM
    ``SignalForgePruneExistingOperator``. Parses with NO SignalForge config in
    the env (the DAG's ``_config`` fallbacks keep the operator's
    construction-time validation green at parse).
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    dag = _load_example_dag("signalforge_after_dbt_build")
    assert set(dag.task_ids) == {"dbt_build", "prune"}
    # SignalForge prunes DOWNSTREAM of the dbt build — the edge the example teaches.
    assert dag.get_task("prune").upstream_task_ids == {"dbt_build"}


def test_after_dbt_build_prune_renders_templated_fields() -> None:
    """The post-dbt-build prune operator's ``template_fields`` render from the task context.

    Constructs the operator inside a DAG context and calls
    ``render_template_fields`` with a synthetic context carrying ``ds`` +
    ``params``; asserts the templated ``model`` (``{{ params.model }}``),
    ``schema`` (``{{ params.schema }}``), and ``as_of`` (``{{ ds }}``) render to
    the injected values — the feature the example DAG demonstrates (#236 DEC-002).
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    import importlib
    from datetime import datetime

    from airflow import DAG

    operators = importlib.import_module("signalforge.airflow.operators")
    operator_cls = operators.SignalForgePruneExistingOperator

    with DAG(dag_id="render_test_after_dbt_build", start_date=datetime(2026, 1, 1), schedule=None):
        op = operator_cls(
            task_id="prune",
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


def test_nightly_drift_operator_example_dag_parses_without_import_errors() -> None:
    """The run-over-run drift-monitor example DAG parses cleanly via DagBag (#235 DEC-019).

    Distinct ``dag_id`` from the other examples; THREE tasks demonstrating the
    two drift surfaces side by side: the ergonomic ``SignalForgeGenerateOperator``
    with ``detect_drift_against`` (Form 1), and the branchable
    ``generate`` → dedicated ``SignalForgeDriftOperator`` pair (Form 2). Parses
    with NO SignalForge config in the env (the DAG's ``_config`` fallbacks keep
    every operator's construction-time validation green at parse).
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    dag = _load_example_dag("signalforge_nightly_drift")
    assert set(dag.task_ids) == {"drift_monitor_ergonomic", "generate", "drift_check"}
    # The branchable form wires the dedicated drift check downstream of generate.
    assert dag.get_task("drift_check").upstream_task_ids == {"generate"}


def test_drift_operator_renders_templated_fields() -> None:
    """The dedicated drift operator's ``template_fields`` render from the task context.

    Constructs the operator inside a DAG context and calls
    ``render_template_fields`` with a synthetic context carrying ``ds``; asserts
    the templated ``current_diff_path`` (``{{ ds }}/diff.json``) and ``as_of``
    (``{{ ds }}``) render to the injected value — the date-stamped sidecar-path
    pattern the example DAG demonstrates (#235 DEC-010/DEC-019).
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    import importlib
    from datetime import datetime

    from airflow import DAG

    operators = importlib.import_module("signalforge.airflow.operators")
    operator_cls = operators.SignalForgeDriftOperator

    with DAG(dag_id="render_test_drift", start_date=datetime(2026, 1, 1), schedule=None):
        op = operator_cls(
            task_id="drift_check",
            previous_diff_path="/history/2026-06-14/diff.json",
            current_diff_path="/history/{{ ds }}/diff.json",
            as_of="{{ ds }}",
        )

    # BaseOperator.render_template_fields(context, jinja_env=None) renders every
    # template_fields attr IN PLACE from the context (jinja_env built from the DAG).
    op.render_template_fields({"ds": "2026-06-15"})

    assert op.current_diff_path == "/history/2026-06-15/diff.json"
    assert op.as_of == "2026-06-15"
    # Non-templated literal field is untouched by the render pass.
    assert op.previous_diff_path == "/history/2026-06-14/diff.json"


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
