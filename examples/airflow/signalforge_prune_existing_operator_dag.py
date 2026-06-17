"""Example Airflow DAG: no-LLM signal-rot monitor via ``SignalForgePruneExistingOperator``.

This is the **cheapest, zero-credential** SignalForge integration shape (epic
#228, issue #233) — the sibling of the ``signalforge generate`` operator example
(``signalforge_generate_operator_dag.py``). The dedicated
:class:`~signalforge.airflow.SignalForgePruneExistingOperator` collapses the
run → decide → raise contract into a SINGLE task: it runs
``signalforge prune-existing`` (ingest -> prune -> diff, **no LLM call**),
maps the result to a neutral ``TaskOutcome`` via the pure ``decide_task_outcome``,
and translates that into the matching Airflow signal (``AirflowFailException``
no-retry / ``AirflowException`` retryable / no-raise on success).

## Signal-rot monitor pattern (read-only, no LLM)

The task prunes the dbt tests a team *already has* (a hand-authored / generated
``schema.yml``) against live warehouse data on a schedule — flagging the tests
that *used* to catch failing rows but now always-pass (signal rot). It makes
**no Anthropic call**, so the Airflow worker needs **no ``ANTHROPIC_API_KEY``** —
only the warehouse credentials (e.g. ``GOOGLE_CLOUD_PROJECT`` + gcloud ADC for
BigQuery). The run is always read-only: ``prune-existing`` has no ``--write`` and
the operator always passes ``--dry-run``; the diff is rendered to stdout (JSON)
only and nothing is written to the dbt project. Set ``schedule="@daily"``
(commented below) to turn this into a scheduled monitor; it ships
``schedule=None`` so the example never auto-spends warehouse budget.

## ``on_flagged`` is inert on this path

``prune-existing`` does **no grading**, so there is never a ``flagged`` tier —
outcomes are only kept / kept-uncertain / dropped. A clean (exit-0) run therefore
always yields ``SUCCESS`` regardless of ``on_flagged`` (#233 DEC-004). The param
is read here for symmetry with the sibling operators (and so a future grade pass
layered on top would Just Work), but it has no effect today. The example still
surfaces it as a DAG param so the operator-config shape stays consistent across
the family.

## Templated fields (``template_fields``)

The operator declares ``template_fields = (project_dir, model, schema,
profiles_dir, as_of, tests_dir)``, so a DAG author can wire Jinja into them and
Airflow renders them from the task context before ``execute``. This example
demonstrates three:

- ``model="{{ params.model }}"`` — the monitored model is params-driven, so a
  manual trigger can override which model is pruned without editing the DAG.
- ``schema="{{ params.schema }}"`` — likewise the path to the hand-authored
  ``schema.yml`` whose tests are pruned.
- ``as_of="{{ ds }}"`` — the run's logical date flows into ``--as-of``, pinning
  time-bound primitives (e.g. ``row_count_anomaly_by_period``) to the scheduled
  date so a backfill is reproducible at ``(model, as_of)`` granularity.

## Configuration (Airflow Variable / env override, with fallbacks)

Because the operator is constructed at DAG-parse time, its config is resolved at
parse (env-override-first, then Airflow ``Variable``) WITH safe fallbacks so the
DAG always parses cleanly even before an operator configures it:

- ``signalforge_project_dir`` / ``SF_PROJECT_DIR`` — dbt project root (must
  contain ``target/manifest.json``). Falls back to ``/opt/airflow/dbt_project``.
- ``signalforge_model`` / ``SF_MODEL`` — the model to prune (**file-path or
  unique_id**; a bare name fails). Falls back to
  ``models/staging/stg_orders.sql``. Surfaced as the ``model`` DAG param so
  ``{{ params.model }}`` renders it.
- ``signalforge_schema`` / ``SF_SCHEMA`` — path to the hand-authored
  ``schema.yml`` whose tests are pruned. Falls back to
  ``models/staging/schema.yml``. Surfaced as the ``schema`` DAG param.
- ``signalforge_on_flagged`` / ``SF_ON_FLAGGED`` — accepted for symmetry but
  inert here (no grading ⇒ no flagged tier); falls back to ``fail``.

Live runs need the warehouse env (e.g. ``GOOGLE_CLOUD_PROJECT`` + gcloud ADC for
BigQuery) available to the Airflow worker — but, unlike the ``generate`` example,
**no ``ANTHROPIC_API_KEY``**. Full walkthrough: docs/airflow-ops.md.
"""

from __future__ import annotations

from datetime import datetime

from airflow import DAG

from signalforge.airflow import SignalForgePruneExistingOperator

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
_model = _config("SF_MODEL", "signalforge_model", default="models/staging/stg_orders.sql")
_schema = _config("SF_SCHEMA", "signalforge_schema", default="models/staging/schema.yml")
_on_flagged = _config("SF_ON_FLAGGED", "signalforge_on_flagged", default="fail")
if _on_flagged not in _VALID_ON_FLAGGED:
    # Fail loud at parse rather than silently defaulting — a typo'd policy is a
    # config error the DAG author must see (even though on_flagged is inert here).
    raise ValueError(
        "signalforge_on_flagged / SF_ON_FLAGGED must be one of "
        f"{'|'.join(_VALID_ON_FLAGGED)} (got {_on_flagged!r})"
    )


with DAG(
    dag_id="signalforge_prune_existing_operator",
    # Manual trigger by default so the example never auto-spends warehouse budget.
    # For scheduled signal-rot detection, set e.g. schedule="@daily".
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    params={"model": _model, "schema": _schema},
    tags=["signalforge", "dbt", "data-quality"],
    doc_md=__doc__,
) as dag:
    signal_rot_monitor = SignalForgePruneExistingOperator(
        task_id="signal_rot_monitor",
        project_dir=_project_dir,
        # Templated: the monitored model + schema path are params-driven
        # ({{ params.model }} / {{ params.schema }}) and the as-of date is the
        # run's logical date ({{ ds }}) — all rendered from the task context
        # before execute() (template_fields).
        model="{{ params.model }}",
        schema="{{ params.schema }}",
        as_of="{{ ds }}",
        # Accepted for symmetry but inert: prune-existing does no grading, so a
        # clean run is always SUCCESS regardless of on_flagged (#233 DEC-004).
        on_flagged=_on_flagged,  # type: ignore[arg-type]
    )
