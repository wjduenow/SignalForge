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


def _load_example_dag():
    from airflow.models.dagbag import DagBag

    bag = DagBag(dag_folder=str(_EXAMPLES_DIR), include_examples=False)
    assert bag.import_errors == {}, f"DAG import errors: {bag.import_errors}"
    # Read the in-memory parsed-DAG dict, NOT bag.get_dag(): get_dag() consults the
    # metadata DB (DagModel.get_current), which requires `airflow db init`. Parse
    # certification must not need a DB — bag.dags is populated purely from parsing.
    assert "signalforge_generate" in bag.dags, f"parsed dags: {list(bag.dags)}"
    return bag.dags["signalforge_generate"]


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
