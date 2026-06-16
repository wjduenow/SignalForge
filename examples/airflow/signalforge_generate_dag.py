"""Example Airflow DAG: run SignalForge's draft → prune → grade → diff pipeline
on a schedule, and act on the graded result via the result → task-state contract.

This is the **OSS-first** integration pattern, refactored onto the shipped
helpers (#231, epic #228):

* **`run_signalforge(argv, *, project_dir, ...)`** runs the pipeline (in-process
  by default — no subprocess overhead, panic-path + four-tier exit-code mapping
  for free) and parses its output into a frozen, Airflow-free
  `SignalForgeRunResult` (exit code, prune/grade tier counts, sidecar paths).
* **`decide_task_outcome(result, *, on_flagged=...)`** is the PURE, Airflow-free
  decision table mapping the run result + the operator's `on_flagged` policy to a
  neutral `TaskOutcome` (a separate axis layered on the four-tier exit codes, NOT
  a fifth tier).
* **`raise_for_outcome(outcome, *, message=...)`** is the thin Airflow-side
  translator that turns the neutral `TaskOutcome` into the matching Airflow
  exception (`AirflowFailException` no-retry / `AirflowException` retryable /
  `AirflowSkipException` skip / no-raise on success).

The dedicated `SignalForgeGenerateOperator` (roadmap, epic #228) will collapse
run + decide + raise into one `execute()`; this example keeps them in two tasks
so the contract is visible, and so the run's tier counts always land in XCom even
when the gate then fails/skips the run.

See docs/airflow-ops.md for the full walkthrough, the result → task-state + XCom
contract, configuration, and the constraints-pinned install.

## Configuration (Airflow Variable / env override)

- `signalforge_project_dir` / `SF_PROJECT_DIR` — dbt project root (must contain
  `target/manifest.json`). Required.
- `signalforge_model` / `SF_MODEL` — model file-path or unique_id (NOT a bare
  name). Required.
- `signalforge_on_flagged` / `SF_ON_FLAGGED` — how to treat an exit-0 run that
  flagged below-threshold artifacts: `fail` (default — hard task failure so a
  reviewer sees it), `skip` (mark the task skipped), or `succeed` (pass through).

Live runs need `ANTHROPIC_API_KEY` + the warehouse env (e.g. `GOOGLE_CLOUD_PROJECT`
+ gcloud ADC for BigQuery) available to the Airflow worker. Full walkthrough:
docs/airflow-ops.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from airflow import DAG
from airflow.exceptions import AirflowFailException
from airflow.operators.python import PythonOperator

from signalforge.airflow import (
    SignalForgeRunResult,
    decide_task_outcome,
    run_signalforge,
)

# `raise_for_outcome` is the Airflow-side translator confined to the one shim
# (`_airflow_compat`); the top-level package re-exports the Airflow-free names
# only, so the translator is imported from the shim directly.
from signalforge.airflow._airflow_compat import raise_for_outcome

_VALID_ON_FLAGGED = ("fail", "skip", "succeed")


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


def _resolve_on_flagged() -> str:
    """Resolve the `on_flagged` policy (Variable / env), defaulting to ``"fail"``.

    An invalid value fails the task loudly (a config error) rather than silently
    defaulting — mirroring the original example's int-validation of the gate
    threshold.
    """
    value = _config("SF_ON_FLAGGED", "signalforge_on_flagged")
    if value is None:
        return "fail"
    if value not in _VALID_ON_FLAGGED:
        raise AirflowFailException(
            "SignalForge config invalid: signalforge_on_flagged / SF_ON_FLAGGED "
            f"must be one of {'|'.join(_VALID_ON_FLAGGED)}"
        )
    return value


def _run_generate(**context: object) -> dict[str, object]:
    """Run `signalforge generate` and push the result summary to XCom.

    Uses `run_signalforge` (in-process by default) and returns
    `SignalForgeRunResult.to_xcom()` — counts + sidecar paths only, no secrets.
    Deliberately does NOT raise on the pipeline outcome: the downstream `gate`
    task is the single decision point (`decide_task_outcome` + `raise_for_outcome`),
    so the tier counts always land in XCom even when the run is then failed /
    skipped. `--dry-run` keeps the scheduled drift use case read-only (no file
    writes; both sidecars suppressed); `--format json` makes the stdout diff
    parseable (the helper injects it when absent — it is passed explicitly here
    for clarity).
    """
    project_dir = _config("SF_PROJECT_DIR", "signalforge_project_dir", required=True)
    model = _config("SF_MODEL", "signalforge_model", required=True)

    result = run_signalforge(
        ["generate", str(model), "--dry-run", "--format", "json"],
        project_dir=str(project_dir),
        invocation="in_process",
    )

    # Surface the pipeline's human-readable progress / footer (stderr) into the
    # Airflow task log; the JSON diff render lives on stdout and is already
    # summarised in the XCom payload below.
    if result.stderr:
        tail = "\n".join(result.stderr.splitlines()[-20:])
        print("[signalforge] stderr tail:\n" + tail)

    xcom = result.to_xcom()
    print(f"[signalforge] exit={result.exit_code} → XCom: {xcom}")
    return xcom


def _gate(**context: object) -> None:
    """Act on the graded result — `decide_task_outcome` + `raise_for_outcome`.

    Pulls the upstream summary from XCom, rebuilds the (Airflow-free)
    `SignalForgeRunResult`, maps it to a neutral `TaskOutcome` via the pure
    decision table, and translates that into the matching Airflow signal:

    * exit 0, no flagged                  → success
    * exit 0, flagged (`on_flagged=fail`) → AirflowFailException (no retry)
    * exit 0, flagged (`on_flagged=skip`) → AirflowSkipException
    * exit 0, flagged (`on_flagged=succeed`) → success
    * exit 1 / 2 (load / input failure)   → AirflowFailException (no retry)
    * exit 3 (external dependency)        → AirflowException (retryable)

    Retry note: in this two-task example a retryable (exit-3) failure surfaces on
    `gate`, and a gate-only retry would re-read the same stale XCom. The dedicated
    operator collapses run + decide into one `execute()` so Airflow's `retries`
    re-run the whole pipeline.
    """
    ti = context["ti"]  # type: ignore[index]
    counts: dict[str, Any] = ti.xcom_pull(task_ids="generate") or {}
    on_flagged = _resolve_on_flagged()

    result = SignalForgeRunResult(
        exit_code=int(counts.get("exit_code", 1)),
        model_unique_ids=tuple(counts.get("model_unique_ids", []) or ()),
        kept=int(counts.get("kept", 0)),
        kept_uncertain=int(counts.get("kept_uncertain", 0)),
        dropped=int(counts.get("dropped", 0)),
        flagged=int(counts.get("flagged", 0)),
        mean_grade=counts.get("mean_grade"),
        diff_sidecar_path=counts.get("diff_sidecar_path"),
        grade_sidecar_path=counts.get("grade_sidecar_path"),
        duration_seconds=counts.get("duration_seconds"),
        stdout="",
        stderr="",
    )

    outcome = decide_task_outcome(result, on_flagged=on_flagged)  # type: ignore[arg-type]
    # Message is built from ints + the validated on_flagged literal only (no raw
    # model strings) — safe to render into the exception / Airflow log.
    message = (
        f"signalforge generate exit={result.exit_code} flagged={result.flagged} "
        f"on_flagged={on_flagged} → {outcome.value}"
    )
    print(f"[signalforge] {message}")
    raise_for_outcome(outcome, message=message)


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
