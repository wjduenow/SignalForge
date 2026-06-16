"""Example Airflow DAG: nightly run-over-run drift / signal-rot monitor (epic #228, issue #235).

SignalForge's prune step drops a test that *always-passes* on warehouse samples
(Architectural Commitment #1 — an always-pass test is noise). **Signal rot** is
the run-over-run version of that: a test that *used to* catch failing rows (a
``kept`` / ``kept-uncertain`` / ``flagged`` tier) but **now always-passes**
(``dropped`` with ``drop_reason == "always-passes"``). That transition is a
schema-drift alarm worth paging on — the data changed underneath a test that
silently stopped doing anything. A run-over-run **grade regression** (the mean
rubric score fell beyond a threshold) is the second alarming signal.

This DAG shows the **two operator surfaces** for detecting it, side by side:

## Form 1 — ergonomic: drift folded into the generate task

``drift_monitor_ergonomic`` is a single :class:`SignalForgeGenerateOperator` that
detects drift *as part of* its run: it parses the current diff off its own
stdout, compares it against the prior run's persisted ``diff.json``
(``detect_drift_against``), persists this run's ``diff.json`` for the next run
(``drift_history_dir``), and folds the drift verdict into its task state via
``on_drift``. One task does run + compare + persist + decide. The drift summary
rides on the task's XCom under the ``"drift"`` key.

## Form 2 — branchable: generate persists, a dedicated operator gates

When you want the run and the alarm to be **separate tasks** (e.g. the generate
should always succeed-and-persist, and a downstream task is the pageable gate you
can branch / alert on independently):

- ``generate`` runs :class:`SignalForgeGenerateOperator` with
  ``drift_history_dir`` (so it persists today's ``diff.json``) and
  ``on_drift="succeed"`` (so it never fails the run on drift — it just records);
- ``drift_check`` runs the dedicated :class:`SignalForgeDriftOperator`
  **downstream**, pointing ``previous_diff_path`` / ``current_diff_path`` at two
  persisted date-stamped ``diff.json`` sidecars (yesterday + today). It runs NO
  ``signalforge`` CLI invocation — it just reads the two sidecars, computes the
  :class:`~signalforge.airflow.drift.DriftReport`, and is the task whose
  ``on_drift="fail"`` pages. Its XCom IS the drift payload (counts + transition
  lists + the two input hashes; no bulk text, no secrets).

## ``on_drift`` (fail / skip / succeed) — most-severe-wins with ``on_flagged``

``on_drift`` is the run-over-run analogue of ``on_flagged``: ``fail`` (default —
an alarming drift is a hard, **no-retry** ``AirflowFailException``; signal rot is
deterministic, so retrying cannot un-rot it), ``skip`` (mark the task skipped —
route to a review branch via ``AirflowSkipException``), or ``succeed`` (pass
through). On the generate operator the flagged-axis and the drift-axis combine
**most-severe-wins** on an exit-0 run; a hard load/parse/input/external error
(exit 1/2/3) short-circuits both.

## Degrade, never fail (DEC-013)

The comparison is fail-soft: a **missing** prior ``diff.json`` (the first run)
makes this run a non-alarming **baseline**; a ``model_unique_id`` mismatch or a
corrupt/unreadable prior sidecar yields a non-alarming **degraded** report
(``degrade_reason`` set) — never an exception. A missing ``grade.json``
(``--no-grade``) simply leaves the grade-regression axis empty. So a drift
monitor never breaks the DAG over its own bookkeeping; only a *genuine* alarm
trips ``on_drift``.

## Reproducibility — ``as_of``

``as_of="{{ ds }}"`` flows the run's logical date into ``--as-of`` so time-bound
primitives (e.g. ``row_count_anomaly_by_period``) are pinned to the scheduled
date and the run is reproducible at ``(model, as_of)`` granularity. The same two
sidecars + same ``as_of`` reproduce a byte-identical ``DriftReport``.

## Templated paths

Both operators declare their path params in ``template_fields``, so the
prior/current sidecar locations are date-stamped from the task context:
``detect_drift_against`` uses yesterday's history dir
(``{{ macros.ds_add(ds, -1) }}``), ``drift_history_dir`` / ``current_diff_path``
use today's (``{{ ds }}``).

## Configuration (Airflow Variable / env override, with fallbacks)

Resolved at DAG-parse time (env-override-first, then Airflow ``Variable``) WITH
safe fallbacks so the DAG always parses cleanly even before an operator wires
real config:

- ``signalforge_project_dir`` / ``SF_PROJECT_DIR`` — dbt project root (must
  contain ``target/manifest.json``). Falls back to ``/opt/airflow/dbt_project``.
- ``signalforge_model`` / ``SF_MODEL`` — the model to monitor (**file-path or
  unique_id**; a bare name fails). Falls back to
  ``models/staging/stg_orders.sql``. Surfaced as the ``model`` DAG param.
- ``signalforge_history_dir`` / ``SF_HISTORY_DIR`` — base directory under which
  each run persists a date-stamped ``diff.json`` (+ ``grade.json``). Falls back
  to ``/opt/airflow/signalforge_history``. Mount this on durable storage so a
  run can compare against the prior run.

Live runs need ``ANTHROPIC_API_KEY`` + the warehouse env (e.g.
``GOOGLE_CLOUD_PROJECT`` + gcloud ADC for BigQuery) available to the Airflow
worker (Form 1 + the ``generate`` half of Form 2 run the full pipeline; the
dedicated ``drift_check`` reads sidecars only and needs neither). It ships
``schedule=None`` so the example never auto-spends on credentials; set
``schedule="@daily"`` to turn it into a real nightly monitor. Full walkthrough:
docs/airflow-ops.md.
"""

from __future__ import annotations

from datetime import datetime

from airflow import DAG

from signalforge.airflow import SignalForgeDriftOperator, SignalForgeGenerateOperator


def _config(env_name: str, var_name: str, *, default: str) -> str:
    """Resolve a config value: env override first, then Airflow Variable, then default.

    Env is read first so the value is available without a metadata-DB round-trip;
    the Airflow ``Variable`` lookup is best-effort (skipped cleanly when no backend
    is configured). A non-empty ``default`` guarantees the operators construct
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
_model = _config("SF_MODEL", "signalforge_model", default="models/staging/stg_orders.sql")
_history_dir = _config(
    "SF_HISTORY_DIR", "signalforge_history_dir", default="/opt/airflow/signalforge_history"
)

# Date-stamped history paths (rendered from the task context before execute()).
# Yesterday's diff is the comparison baseline; today's dir is where this run
# persists its diff.json for tomorrow's run.
_today_dir = f"{_history_dir}/{{{{ ds }}}}"
_yesterday_dir = f"{_history_dir}/{{{{ macros.ds_add(ds, -1) }}}}"


with DAG(
    dag_id="signalforge_drift_monitor",
    # Manual trigger by default so the example never auto-spends on credentials.
    # For a real nightly drift monitor, set schedule="@daily".
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    params={"model": _model},
    tags=["signalforge", "dbt", "data-quality", "drift"],
    doc_md=__doc__,
) as dag:
    # ----------------------------------------------------------------------- #
    # Form 1 — ergonomic: one task runs + compares + persists + decides.
    # ----------------------------------------------------------------------- #
    drift_monitor_ergonomic = SignalForgeGenerateOperator(
        task_id="drift_monitor_ergonomic",
        project_dir=_project_dir,
        model="{{ params.model }}",
        # Read-only scheduled default: nothing is written to the dbt project.
        write=False,
        as_of="{{ ds }}",
        # Compare this run's diff (parsed off stdout) against yesterday's
        # persisted diff.json, and persist this run's diff.json (+ grade.json)
        # under today's dir for tomorrow. A missing prior → baseline (no alarm).
        detect_drift_against=f"{_yesterday_dir}/diff.json",
        drift_history_dir=_today_dir,
        # An alarming drift (signal rot or grade regression) fails the task,
        # no retry. Use "skip" to route to a review branch, "succeed" to record-only.
        on_drift="fail",
    )

    # ----------------------------------------------------------------------- #
    # Form 2 — branchable: generate persists (and never fails on drift), a
    # dedicated downstream operator is the pageable gate.
    # ----------------------------------------------------------------------- #
    generate = SignalForgeGenerateOperator(
        task_id="generate",
        project_dir=_project_dir,
        model="{{ params.model }}",
        write=False,
        as_of="{{ ds }}",
        # Persist today's diff.json (+ grade.json) for the dedicated check below.
        # detect_drift_against is required to trigger persistence; point it at
        # yesterday so the generate task also records a drift summary on its XCom.
        detect_drift_against=f"{_yesterday_dir}/diff.json",
        drift_history_dir=_today_dir,
        # Record-only here: the generate task always succeeds; the gate is the
        # downstream dedicated operator.
        on_drift="succeed",
    )

    drift_check = SignalForgeDriftOperator(
        task_id="drift_check",
        # Two persisted, date-stamped diff.json sidecars: yesterday vs today.
        # The grade.json siblings are auto-resolved next to each diff.
        previous_diff_path=f"{_yesterday_dir}/diff.json",
        current_diff_path=f"{_today_dir}/diff.json",
        as_of="{{ ds }}",
        # This is the pageable gate: an alarming drift fails the task (no retry).
        on_drift="fail",
    )

    generate >> drift_check
