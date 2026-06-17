"""Subprocess driver for the gated live Airflow generate e2e (#236 / DEC-001).

Run as a CHILD process by ``tests/airflow/test_e2e_generate_operator.py``.

Why a subprocess: Airflow binds its ORM engine to ``$AIRFLOW_HOME/airflow.db``
at **import time** (``airflow/__init__.py`` → ``settings.configure_orm()`` reads
``SQL_ALCHEMY_CONN`` once, derived from ``AIRFLOW_HOME``). Setting ``AIRFLOW_HOME``
*after* ``import airflow`` in the pytest worker does NOT repoint that engine, so
an in-process ``dag.test()`` would silently run against the maintainer's
``~/airflow`` (polluting it, or erroring on a clean machine). Running the whole
``dag.test()`` in a fresh child — whose environment already carries
``AIRFLOW_HOME`` BEFORE this module imports Airflow — guarantees true per-run
isolation of the metadata DB without mutating any Airflow global state in the
pytest worker.

The parent migrates ``$AIRFLOW_HOME/airflow.db`` (``airflow db migrate``) before
launching this driver, so no migration happens here — this process just imports
Airflow (binding to the already-migrated per-run DB), builds the inline
single-``--model`` DAG with the real ``SignalForgeGenerateOperator``, runs
``dag.test()``, and prints one machine-readable result line.

Usage::

    python tests/airflow/_e2e_generate_driver.py <project_dir>

Prints exactly one line ``__SF_E2E_RESULT__ <json>`` to stdout carrying the task
state and the operator's XCom dict; the parent test parses that line and asserts.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

_RESULT_MARKER = "__SF_E2E_RESULT__"


def main() -> int:
    project_dir = sys.argv[1]

    # ``AIRFLOW_HOME`` is already in the environment (the parent set it before
    # launching this child), so importing Airflow here binds the ORM engine to
    # the per-run tmp DB the parent just migrated.
    from airflow import DAG

    from signalforge.airflow.operators import SignalForgeGenerateOperator

    # Inline DAG with a SINGLE ``--model`` task (the example DAGs use
    # ``--select``; the always-passes assertion needs the one staging model).
    # ``write=False`` keeps the run read-only; ``on_flagged="succeed"`` so the
    # fixture's tight grade thresholds don't fail the Airflow task — the parent
    # pins pipeline completion + the warehouse-side always-passes drop, not the
    # grade verdict.
    with DAG(
        dag_id="sf_e2e_generate",
        schedule=None,
        start_date=datetime(2026, 1, 1),
        catchup=False,
    ) as dag:
        SignalForgeGenerateOperator(
            task_id="generate",
            project_dir=project_dir,
            model="models/staging/stg_bikeshare_trips.sql",
            write=False,
            on_flagged="succeed",
        )

    # Drive the DAG end-to-end through Airflow's task runner (airflow 2.10.4
    # returns the created ``DagRun``).
    dag_run = dag.test()
    ti = dag_run.get_task_instance("generate")
    state = None if ti is None else str(ti.state)
    xcom = None if ti is None else ti.xcom_pull(task_ids="generate")

    # One machine-readable line for the parent. Airflow's own task logs may also
    # hit stdout/stderr; the parent scans for this marker prefix.
    print(f"{_RESULT_MARKER} {json.dumps({'state': state, 'xcom': xcom})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
