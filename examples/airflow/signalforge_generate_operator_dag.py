"""Example Airflow DAG: SignalForge drift monitor via ``SignalForgeGenerateOperator``.

This is the **operator-first** integration shape (epic #228, US-003/US-004) — the
counterpart to the two-``PythonOperator`` example in
``signalforge_generate_dag.py``. The dedicated
:class:`~signalforge.airflow.SignalForgeGenerateOperator` collapses the
run → decide → raise contract into a SINGLE task: it runs ``signalforge generate``,
maps the graded result to a neutral ``TaskOutcome`` via the pure
``decide_task_outcome``, and translates that into the matching Airflow signal
(``AirflowFailException`` no-retry / ``AirflowException`` retryable /
``AirflowSkipException`` skip / no-raise on success). Airflow's own
``retries`` / ``retry_delay`` therefore re-run the WHOLE pipeline on a retryable
(exit-3) external-dependency failure — the win over the two-task example, whose
gate-only retry would re-read the same stale XCom.

## Drift-monitor pattern (read-only)

The task runs with ``write=False`` (the safe scheduled default → ``--dry-run``):
nothing is written to disk (both sidecars suppressed), the diff is rendered to
stdout (JSON) only, and the **task state reflects ``on_flagged``**. So a scheduled
run is a pure drift signal — it never mutates the dbt project; it just succeeds,
fails, or skips depending on whether SignalForge flagged below-threshold
artifacts. Set ``schedule="@daily"`` (commented below) to turn this into a
scheduled monitor; it ships ``schedule=None`` so the example never auto-spends on
credentials.

## Templated fields (``template_fields``)

The operator declares ``template_fields = (project_dir, select, model, profiles_dir,
as_of)``, so a DAG author can wire Jinja into them and Airflow renders them from
the task context before ``execute``. This example demonstrates two:

- ``select="{{ params.select }}"`` — the monitored selector is params-driven, so a
  manual trigger can override which models are swept without editing the DAG.
- ``as_of="{{ ds }}"`` — the run's logical date flows into ``--as-of``, pinning
  time-bound primitives (e.g. ``row_count_anomaly_by_period``) to the scheduled
  date so a backfill is reproducible at ``(model, as_of)`` granularity.

## Configuration (Airflow Variable / env override, with fallbacks)

Because the operator is constructed at DAG-parse time, its config is resolved at
parse (env-override-first, then Airflow ``Variable``) WITH safe fallbacks so the
DAG always parses cleanly even before an operator configures it:

- ``signalforge_project_dir`` / ``SF_PROJECT_DIR`` — dbt project root (must contain
  ``target/manifest.json``). Falls back to ``/opt/airflow/dbt_project``.
- ``signalforge_select`` / ``SF_SELECT`` — the ``--select`` selector expression to
  monitor. Falls back to ``tag:staging``. Surfaced as the ``select`` DAG param so
  ``{{ params.select }}`` renders it (and a trigger-conf can override it).
- ``signalforge_on_flagged`` / ``SF_ON_FLAGGED`` — how to treat an exit-0 run that
  flagged below-threshold artifacts: ``fail`` (default — hard task failure so a
  reviewer sees it), ``skip`` (mark the task skipped — route to a review branch),
  or ``succeed`` (pass through).

Live runs need ``ANTHROPIC_API_KEY`` + the warehouse env (e.g.
``GOOGLE_CLOUD_PROJECT`` + gcloud ADC for BigQuery) available to the Airflow
worker. Full walkthrough: docs/airflow-ops.md.
"""

from __future__ import annotations

from datetime import datetime

from airflow import DAG

from signalforge.airflow import SignalForgeGenerateOperator

_VALID_ON_FLAGGED = ("fail", "skip", "succeed")


def _config(env_name: str, var_name: str, *, default: str) -> str:
    """Resolve a config value: env override first, then Airflow Variable, then default.

    Env is read first so the value is available without a metadata-DB round-trip;
    the Airflow ``Variable`` lookup is best-effort (skipped cleanly when no backend
    is configured). A non-empty ``default`` guarantees the operator constructs
    validly at DAG-parse time even before an operator wires real config.
    """
    import os

    value = os.environ.get(env_name)
    if not value:
        try:
            from airflow.models import Variable

            value = Variable.get(var_name, default_var=None)
        except Exception:  # noqa: BLE001 — no backend / not found → fall through
            value = None
    return value or default


_project_dir = _config(
    "SF_PROJECT_DIR", "signalforge_project_dir", default="/opt/airflow/dbt_project"
)
_select = _config("SF_SELECT", "signalforge_select", default="tag:staging")
_on_flagged = _config("SF_ON_FLAGGED", "signalforge_on_flagged", default="fail")
if _on_flagged not in _VALID_ON_FLAGGED:
    # Fail loud at parse rather than silently defaulting — a typo'd policy is a
    # config error the DAG author must see.
    raise ValueError(
        "signalforge_on_flagged / SF_ON_FLAGGED must be one of "
        f"{'|'.join(_VALID_ON_FLAGGED)} (got {_on_flagged!r})"
    )


with DAG(
    dag_id="signalforge_generate_operator",
    # Manual trigger by default so the example never auto-spends on credentials.
    # For scheduled drift detection, set e.g. schedule="@daily".
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    params={"select": _select},
    tags=["signalforge", "dbt", "data-quality"],
    doc_md=__doc__,
) as dag:
    drift_monitor = SignalForgeGenerateOperator(
        task_id="drift_monitor",
        project_dir=_project_dir,
        # Templated: the monitored selector is params-driven ({{ params.select }})
        # and the as-of date is the run's logical date ({{ ds }}) — both rendered
        # from the task context before execute() (template_fields).
        select="{{ params.select }}",
        as_of="{{ ds }}",
        # Read-only scheduled drift default: nothing is written; task state is the
        # drift signal, governed by on_flagged.
        write=False,
        on_flagged=_on_flagged,  # type: ignore[arg-type]
    )
