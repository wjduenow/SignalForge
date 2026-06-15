"""Spike DAG for issue #229 — proves a SignalForge-shaped DAG parses under a
constraints-pinned local Airflow install.

This is **spike scaffolding**, not the shipped integration. It defines a trivial
placeholder operator inline so the DAG-parse certification (`DagBag`) does not
depend on the not-yet-existent ``signalforge.airflow`` subpackage (epic #228's
*skeleton* child owns that). When the real operators land, this example is
replaced by one importing ``SignalForgeGenerateOperator`` et al.

Certify locally (see docs/research/airflow-test-environment.md for the full
constraints-pinned install):

    export AIRFLOW__CORE__LOAD_EXAMPLES=False
    export AIRFLOW__CORE__DAGS_FOLDER="$(pwd)/examples/airflow"
    .venv-airflow/bin/airflow dags list          # signalforge_spike must appear
"""

from __future__ import annotations

from datetime import datetime

from airflow import DAG
from airflow.models.baseoperator import BaseOperator


class _PlaceholderSignalForgeOperator(BaseOperator):
    """No-op stand-in for the future ``SignalForgeGenerateOperator``.

    Exists only to prove the operator-authoring pattern (subclass ``BaseOperator``,
    implement ``execute``) parses and instantiates under the pinned Airflow. The
    real operator wraps the ``signalforge`` CLI/library surface and maps the
    four-tier exit code → task state (epic #228, *result → task-state* child).
    """

    def __init__(self, *, model: str, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.model = model

    def execute(self, context: object) -> dict[str, str]:
        # Spike: no warehouse, no LLM. The shipped operator returns the diff
        # sidecar tier counts here for downstream XCom consumers.
        return {"spike": "ok", "model": self.model}


with DAG(
    dag_id="signalforge_spike",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["signalforge", "spike"],
) as dag:
    _PlaceholderSignalForgeOperator(task_id="placeholder_generate", model="stg_demo")
