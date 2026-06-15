"""Gated DAG-parse certification for the Airflow test-environment spike (#229).

Belt-and-suspenders gating per `testing-signal.md`:

1. ``pytestmark = pytest.mark.airflow`` — deselected by the default ``addopts``
   ``-m 'not ... and not airflow'`` so a plain ``uv run pytest`` never imports
   Airflow.
2. A runtime ``importorskip`` — surfaces a clear skip-with-reason when a
   maintainer runs ``-m airflow`` in an env where Airflow isn't installed under
   its constraints file.

Run: ``uv run --no-sync pytest -m airflow --no-cov`` inside the constraints-pinned
Airflow venv (see docs/research/airflow-test-environment.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.airflow

# Gate 2: skip (don't error) when Airflow isn't importable in this interpreter.
pytest.importorskip(
    "airflow",
    reason="Apache Airflow not installed (run inside the constraints-pinned airflow venv)",
)

_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "airflow"


def test_spike_dag_parses_without_import_errors() -> None:
    """The example DAG folder parses cleanly via DagBag — no import/parse errors.

    This is the acceptance signal for #229: a SignalForge-shaped DAG authored
    against the chosen Airflow floor parses in-process, proving the constraints-
    pinned install + our authoring pattern are compatible.
    """
    from airflow.models.dagbag import DagBag

    bag = DagBag(dag_folder=str(_EXAMPLES_DIR), include_examples=False)

    assert bag.import_errors == {}, f"DAG import errors: {bag.import_errors}"
    dag = bag.get_dag("signalforge_spike")
    assert dag is not None
    assert "placeholder_generate" in dag.task_ids
