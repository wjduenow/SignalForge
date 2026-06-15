"""Example Airflow DAG: run SignalForge's draft → prune → grade → diff pipeline
on a schedule, and gate the DAG on the graded result.

This is the **OSS-first, CLI-wrapping** integration pattern: a `PythonOperator`
shells out to the `signalforge` CLI, maps its four-tier exit code to Airflow task
state, and pushes the graded tier counts to XCom for downstream notification /
gating tasks. (Dedicated `SignalForgeGenerateOperator` / `…PruneExistingOperator`
are on the v0.7 roadmap — epic #228 — and will be a drop-in swap for the
`PythonOperator` here; the result→task-state + XCom contract is identical.)

See docs/airflow-ops.md for the full walkthrough, configuration, and the
constraints-pinned install.

## Configuration (Airflow Variable / env override)

- `signalforge_project_dir` / `SF_PROJECT_DIR` — dbt project root (must contain
  `target/manifest.json`). Required.
- `signalforge_model` / `SF_MODEL` — model file-path or unique_id (NOT a bare
  name). Required.
- `signalforge_max_flagged` / `SF_MAX_FLAGGED` — gate threshold; fail the run if
  `flagged >` this (unset = no gate).

Live runs need `ANTHROPIC_API_KEY` + the warehouse env (e.g. `GOOGLE_CLOUD_PROJECT`
+ gcloud ADC for BigQuery) available to the Airflow worker. Full walkthrough:
docs/airflow-ops.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from airflow import DAG
from airflow.exceptions import AirflowFailException
from airflow.operators.python import PythonOperator

# The `signalforge` console script lives in the same environment as the worker's
# Python interpreter (the constraints-pinned venv that has both Airflow and
# signalforge installed).
_SF_BIN = str(Path(sys.executable).parent / "signalforge")


def _config(env_name: str, var_name: str, *, required: bool = False) -> str | None:
    """Resolve a config value: env override first, then Airflow Variable.

    Env is read first so the value is available without a metadata-DB round-trip
    (and makes the DAG's task callables unit-testable without a running Airflow).
    The Airflow Variable lookup is best-effort — it's skipped cleanly when no
    backend is configured.
    """
    import os

    value = os.environ.get(env_name)
    if not value:
        try:
            from airflow.models import Variable

            value = Variable.get(var_name, default_var=None)
        except Exception:  # noqa: BLE001 — no backend / not found → fall through
            value = None
    if not value and required:
        raise AirflowFailException(
            f"SignalForge config missing: set env {env_name} or Airflow Variable {var_name}"
        )
    return value


def _run_generate(**context: object) -> dict[str, object]:
    """Run `signalforge generate` and return the graded tier counts (→ XCom).

    Four-tier exit-code → task-state mapping (the CLI taxonomy):
    0 success → return the result dict; 1 load / 2 input / 3 external → raise
    `AirflowFailException` so the task reflects the pipeline failure. (The
    dedicated operator will map tier 2 below-threshold to a branch/skip; this
    example keeps it to success-or-fail.)
    """
    project_dir = _config("SF_PROJECT_DIR", "signalforge_project_dir", required=True)
    model = _config("SF_MODEL", "signalforge_model", required=True)

    cmd = [
        _SF_BIN,
        "generate",
        str(model),
        "--project-dir",
        str(project_dir),
        "--dry-run",
        "--format",
        "json",
    ]
    print(f"[signalforge] running: {' '.join(cmd)}")
    # Bound the external call so a hung CLI / warehouse / LLM can't tie up a worker
    # slot indefinitely. 1h is generous for a single model; tune for your fleet.
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired as exc:
        raise AirflowFailException("signalforge generate timed out after 3600s") from exc
    print(f"[signalforge] exit={proc.returncode}")
    if proc.stderr:
        print("[signalforge] stderr tail:\n" + "\n".join(proc.stderr.splitlines()[-10:]))

    if proc.returncode != 0:
        raise AirflowFailException(
            f"signalforge generate exited {proc.returncode} (four-tier exit-code taxonomy)"
        )

    report = json.loads(proc.stdout)
    result = {
        "model_unique_id": report["model_unique_id"],
        "kept": report["kept_count"],
        "kept_uncertain": report["kept_uncertain_count"],
        "dropped": report["dropped_count"],
        "flagged": report["flagged_count"],
        "proposed_test_files": len(report.get("proposed_test_files", [])),
        "run_id": report["run_id"],
    }
    print(f"[signalforge] tier counts → XCom: {result}")
    return result


def _gate(**context: object) -> None:
    """Gate the DAG on the graded result — the 'act on the graded diff' value.

    Pulls the upstream tier counts from XCom and fails the run when the flagged
    count exceeds `signalforge_max_flagged` (Variable / `SF_MAX_FLAGGED`). When
    the threshold is unset, the gate only reports — so the DAG runs green
    out-of-the-box.
    """
    ti = context["ti"]  # type: ignore[index]
    counts = ti.xcom_pull(task_ids="generate") or {}
    flagged = int(counts.get("flagged", 0))
    print(f"[signalforge] graded result: {counts}")

    threshold = _config("SF_MAX_FLAGGED", "signalforge_max_flagged")
    if threshold is None:
        print("[signalforge] no flagged-count gate set (signalforge_max_flagged) — report only.")
        return
    try:
        max_flagged = int(threshold)
    except ValueError as exc:
        raise AirflowFailException(
            "SignalForge config invalid: signalforge_max_flagged / SF_MAX_FLAGGED must be an int"
        ) from exc
    if flagged > max_flagged:
        raise AirflowFailException(
            f"SignalForge gate: {flagged} flagged artifact(s) > threshold {max_flagged}"
        )
    print(f"[signalforge] gate passed: {flagged} flagged ≤ threshold {max_flagged}")


with DAG(
    dag_id="signalforge_generate",
    # Manual trigger by default so the example never auto-spends on credentials.
    # For scheduled drift detection, set e.g. schedule="@daily".
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["signalforge", "dbt", "data-quality"],
    doc_md=__doc__,
) as dag:
    generate = PythonOperator(task_id="generate", python_callable=_run_generate)
    gate = PythonOperator(task_id="gate", python_callable=_gate)
    generate >> gate
