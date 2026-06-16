"""Example Airflow DAG: Airflow-native credentials via ``signalforge_conn_id`` (#234).

This is the **Connection-configured** integration shape (epic #228, US-007) — the
counterpart to the two single-operator examples
(``signalforge_generate_operator_dag.py`` / ``signalforge_prune_existing_operator_dag.py``),
which rely on ambient worker env. Here BOTH operators read their credentials from a
single Airflow **Connection** (plus an optional **Variable** for the LLM API key)
via the ``signalforge_conn_id`` param — so there is **NO inline ``ANTHROPIC_API_KEY``
/ per-task env** on either task. This is the Airflow-native way to configure
SignalForge (Admin → Connections / Variables, or a secrets backend), keeping the
credential out of the DAG source and out of every task definition.

## Connection + Variable setup this DAG assumes

Create ONE Airflow Connection (Admin → Connections, or ``airflow connections add``)
with this id — both tasks point at it via ``signalforge_conn_id``:

- **Conn Id:** ``signalforge_default`` (the ``_CONN_ID`` constant below).
- **Conn Type:** ``generic`` (any type works — the hook reads only ``password`` +
  ``extra``).
- **Password:** the **LLM API key** (e.g. your Anthropic key). The Connection
  ``password`` is auto-masked in Airflow logs, and the hook never logs / XCom's /
  repr's it. The ``generate`` task needs this; the ``prune-existing`` task does NOT
  (it makes no LLM call — see "Generate vs PruneExisting" below).
- **Extra (JSON):** the validated ``extra="forbid"`` schema — only these keys::

      {
        "profiles_dir": "/opt/airflow/dbt_project",
        "provider": "anthropic",
        "cache_scope": "project"
      }

  ``profiles_dir`` (dir holding your dbt ``profiles.yml`` → ``--profiles-dir``),
  ``provider`` (LLM SKU family — ``anthropic`` / ``openai`` / ``gemini``, picks the
  env var the key is injected into), ``cache_scope`` (``per-model`` / ``project``,
  Generate only). Any UNKNOWN key fails loud at resolution. Cost ceilings are
  deliberately NOT in the ``extra`` (no CLI landing strip in v0.7) — set them in the
  committed ``signalforge.yml grade:`` block instead.

Optional Variable fallback for the API key (Admin → Variables, or
``airflow variables set``): key name **``signalforge_api_key``** (the
``API_KEY_VARIABLE_KEY`` constant). When the Connection ``password`` is empty the
hook falls back to this Variable. Use the Connection ``password`` as the primary
home for the key (it auto-masks); the Variable is the fallback.

## Generate vs PruneExisting credentials

Both tasks use the SAME ``signalforge_conn_id``, but they consume it differently
(DEC-016):

- **``generate``** calls the LLM, so it REQUIRES ``provider`` + an API key
  (``password`` or the Variable) AND uses ``profiles_dir`` for warehouse auth.
- **``prune-existing``** is read-only and makes **no LLM call**, so it uses **only**
  ``profiles_dir`` from the Connection ``extra`` — ``provider`` / the key are
  ignored on that path. A prune-existing-only deployment could use a Connection
  with just ``{"profiles_dir": "..."}`` and no password/provider at all.

## Secrets hygiene

The resolved API key never appears in task logs (Airflow secrets-masker +
``password`` auto-masking), in XCom (counts + sidecar paths only), in rendered
templates (``signalforge_conn_id`` is NOT a ``template_fields`` entry), or in any
``__repr__``. For ``invocation="in_process"`` (the default) the key is injected
into ``os.environ`` only for the duration of the run and restored afterward; prefer
``invocation="subprocess"`` for concurrent multi-task workers (in-process shares
``os.environ`` + process-global stdout capture). Full walkthrough: docs/airflow-ops.md.
"""

from __future__ import annotations

from datetime import datetime

from airflow import DAG

from signalforge.airflow import (
    SignalForgeGenerateOperator,
    SignalForgePruneExistingOperator,
)

#: The Airflow Connection id BOTH operators resolve credentials from. Create a
#: Connection with this id (password = LLM API key; extra JSON = profiles_dir /
#: provider / cache_scope) — see the module docstring.
_CONN_ID = "signalforge_default"

_VALID_ON_FLAGGED = ("fail", "skip", "succeed")


def _config(env_name: str, var_name: str, *, default: str) -> str:
    """Resolve a config value: env override first, then Airflow Variable, then default.

    Env is read first so the value is available without a metadata-DB round-trip;
    the Airflow ``Variable`` lookup is best-effort (skipped cleanly when no backend
    is configured). A non-empty ``default`` guarantees both operators construct
    validly at DAG-parse time even before an operator wires real config. NOTE: this
    resolves NON-secret config only — the LLM API key is NOT read here; it comes
    from the Connection (``password``) / the ``signalforge_api_key`` Variable via
    the hook, never inline on a task.
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
_model = _config("SF_MODEL", "signalforge_model", default="models/staging/stg_orders.sql")
_schema = _config("SF_SCHEMA", "signalforge_schema", default="models/staging/schema.yml")
_on_flagged = _config("SF_ON_FLAGGED", "signalforge_on_flagged", default="fail")
if _on_flagged not in _VALID_ON_FLAGGED:
    # Fail loud at parse rather than silently defaulting — a typo'd policy is a
    # config error the DAG author must see.
    raise ValueError(
        "signalforge_on_flagged / SF_ON_FLAGGED must be one of "
        f"{'|'.join(_VALID_ON_FLAGGED)} (got {_on_flagged!r})"
    )


with DAG(
    dag_id="signalforge_hook",
    # Manual trigger by default so the example never auto-spends on credentials.
    # For scheduled monitoring, set e.g. schedule="@daily".
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    params={"select": _select, "model": _model, "schema": _schema},
    tags=["signalforge", "dbt", "data-quality"],
    doc_md=__doc__,
) as dag:
    # `generate` — drift monitor. Credentials (LLM provider + key + profiles_dir)
    # come entirely from the `signalforge_default` Connection (+ optional Variable)
    # via signalforge_conn_id — NO inline ANTHROPIC_API_KEY / env on this task.
    drift_monitor = SignalForgeGenerateOperator(
        task_id="drift_monitor",
        project_dir=_project_dir,
        signalforge_conn_id=_CONN_ID,
        # Templated: params-driven selector + the run's logical date as --as-of.
        select="{{ params.select }}",
        as_of="{{ ds }}",
        # Read-only scheduled drift default: nothing is written; task state is the
        # drift signal, governed by on_flagged.
        write=False,
        on_flagged=_on_flagged,  # type: ignore[arg-type]
    )

    # `prune-existing` — no-LLM signal-rot monitor. Uses ONLY the profiles_dir from
    # the SAME Connection (it makes no LLM call, so provider/key are ignored on this
    # path) — likewise NO inline env on the task.
    signal_rot_monitor = SignalForgePruneExistingOperator(
        task_id="signal_rot_monitor",
        project_dir=_project_dir,
        signalforge_conn_id=_CONN_ID,
        # Templated: params-driven model + schema path + the run's logical date.
        model="{{ params.model }}",
        schema="{{ params.schema }}",
        as_of="{{ ds }}",
        # Accepted for symmetry but inert: prune-existing does no grading, so a
        # clean run is always SUCCESS regardless of on_flagged (#233 DEC-004).
        on_flagged=_on_flagged,  # type: ignore[arg-type]
    )
