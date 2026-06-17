"""Example Airflow DAG: prune-existing running DOWNSTREAM of a dbt build (epic #228, issue #236).

This is the **post-dbt-build** integration shape: a dbt ``run`` / ``build`` task
materialises (or refreshes) the warehouse tables, and *then* SignalForge's no-LLM
:class:`~signalforge.airflow.SignalForgePruneExistingOperator` prunes the dbt tests
the team *already has* against the freshly-built data — flagging the tests that
*used to* catch failing rows but now always-pass (signal rot). It is the natural
"add SignalForge to my existing dbt pipeline" wiring: ``dbt_build >> prune``.

## No Cosmos dependency

The dbt step here is a plain :class:`~airflow.operators.bash.BashOperator` running
``dbt build`` as an illustrative stand-in — **SignalForge does not depend on
``astronomer-cosmos``**. In a real deployment this upstream task is your existing
dbt run/build, however you already run dbt: a ``BashOperator``/``DbtRunOperator``,
a Cosmos ``DbtTaskGroup``, a ``KubernetesPodOperator``, etc. SignalForge only cares
that the manifest + warehouse tables exist by the time ``prune`` runs; it reads
``target/manifest.json`` from ``project_dir`` and queries the warehouse. Swap the
``dbt_build`` task for your own and keep the ``dbt_build >> prune`` edge.

## Read-only, no LLM, zero Anthropic credential

:class:`SignalForgePruneExistingOperator` runs ``signalforge prune-existing``
(ingest -> prune -> diff, **no LLM call**). The Airflow worker needs **no
``ANTHROPIC_API_KEY``** — only the warehouse credentials (e.g.
``GOOGLE_CLOUD_PROJECT`` + gcloud ADC for BigQuery) the dbt step already requires.
The run is always read-only: ``prune-existing`` has no ``--write`` and the operator
always passes ``--dry-run``; the diff is rendered to stdout (JSON) only and nothing
is written to the dbt project.

## ``on_flagged`` is inert on this path

``prune-existing`` does **no grading**, so there is never a ``flagged`` tier —
outcomes are only kept / kept-uncertain / dropped. A clean (exit-0) run therefore
always yields ``SUCCESS`` regardless of ``on_flagged`` (#233 DEC-004). The param is
read here for symmetry with the sibling operators; it has no effect today.

## Templated fields (``template_fields``)

The operator declares ``template_fields = (project_dir, model, schema,
profiles_dir, as_of, tests_dir)``. This example demonstrates three:

- ``model="{{ params.model }}"`` — the monitored model is params-driven, so a
  manual trigger can override which model is pruned without editing the DAG.
- ``schema="{{ params.schema }}"`` — likewise the path to the hand-authored
  ``schema.yml`` whose tests are pruned.
- ``as_of="{{ ds }}"`` — the run's logical date flows into ``--as-of``, pinning
  time-bound primitives (e.g. ``row_count_anomaly_by_period``) to the scheduled
  date so a backfill is reproducible at ``(model, as_of)`` granularity.

## Configuration (Airflow Variable / env override, with fallbacks)

Resolved at DAG-parse time (env-override-first, then Airflow ``Variable``) WITH
safe fallbacks so the DAG always parses cleanly even before an operator wires real
config:

- ``signalforge_project_dir`` / ``SF_PROJECT_DIR`` — dbt project root (must contain
  ``target/manifest.json`` after the dbt build). Falls back to
  ``/opt/airflow/dbt_project``.
- ``signalforge_model`` / ``SF_MODEL`` — the model to prune (**file-path or
  unique_id**; a bare name fails). Falls back to ``models/staging/stg_orders.sql``.
  Surfaced as the ``model`` DAG param so ``{{ params.model }}`` renders it.
- ``signalforge_schema`` / ``SF_SCHEMA`` — path to the hand-authored ``schema.yml``
  whose tests are pruned. Falls back to ``models/staging/schema.yml``. Surfaced as
  the ``schema`` DAG param.
- ``signalforge_on_flagged`` / ``SF_ON_FLAGGED`` — accepted for symmetry but inert
  here (no grading ⇒ no flagged tier); falls back to ``fail``.

Live runs need the warehouse env (e.g. ``GOOGLE_CLOUD_PROJECT`` + gcloud ADC for
BigQuery) available to the Airflow worker — but, unlike the ``generate`` example,
**no ``ANTHROPIC_API_KEY``**. It ships ``schedule=None`` so the example never
auto-spends warehouse budget; set ``schedule="@daily"`` to turn it into a real
post-build monitor. Full walkthrough: docs/airflow-ops.md.
"""

from __future__ import annotations

import shlex
from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

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
    dag_id="signalforge_after_dbt_build",
    # Manual trigger by default so the example never auto-spends warehouse budget.
    # For a real post-build monitor, set e.g. schedule="@daily".
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    params={"model": _model, "schema": _schema},
    tags=["signalforge", "dbt", "data-quality"],
    doc_md=__doc__,
) as dag:
    # ----------------------------------------------------------------------- #
    # Upstream: build the dbt tables. This BashOperator running `dbt build` is
    # an illustrative stand-in — in a real deployment THIS is your existing dbt
    # run/build, however you already run it (a BashOperator/DbtRunOperator, a
    # Cosmos DbtTaskGroup, a KubernetesPodOperator, ...). SignalForge does NOT
    # depend on Cosmos; just keep the `dbt_build >> prune` edge so prune runs
    # after the manifest + warehouse tables are fresh.
    # ----------------------------------------------------------------------- #
    dbt_build = BashOperator(
        task_id="dbt_build",
        bash_command=f"dbt build --project-dir {shlex.quote(_project_dir)}",
    )

    # ----------------------------------------------------------------------- #
    # Downstream: prune the team's existing dbt tests against the freshly-built
    # data (no LLM, read-only). Templated model/schema (params-driven) and the
    # as-of date (the run's logical date) are rendered from the task context
    # before execute() (template_fields).
    # ----------------------------------------------------------------------- #
    prune = SignalForgePruneExistingOperator(
        task_id="prune",
        project_dir=_project_dir,
        model="{{ params.model }}",
        schema="{{ params.schema }}",
        as_of="{{ ds }}",
        # Accepted for symmetry but inert: prune-existing does no grading, so a
        # clean run is always SUCCESS regardless of on_flagged (#233 DEC-004).
        on_flagged=_on_flagged,  # type: ignore[arg-type]
    )

    dbt_build >> prune
