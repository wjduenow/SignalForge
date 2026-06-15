# Airflow integration

Run SignalForge's **draft → prune → grade → diff** pipeline as a scheduled Airflow
DAG — turning the CLI from an ad-hoc tool into a **scheduled schema-drift / signal-rot
monitor**. Consistent with Architectural Commitment #4 (OSS-first, Core-friendly): an
Airflow DAG runs against any dbt-core project, no dbt Cloud dependency.

> **Status (v0.7).** The shipped surface today is an **example DAG** that wraps the
> `signalforge` CLI with a `PythonOperator`. Dedicated `SignalForgeGenerateOperator` /
> `SignalForgePruneExistingOperator` are on the roadmap (epic #228); they are a drop-in
> swap for the `PythonOperator` — the result→task-state + XCom contract below is
> identical. The CLI-wrapping pattern keeps working regardless.

## Install Airflow alongside SignalForge

Airflow is **not** a normal dependency — it is heavy and version-pinned via a
per-`(airflow, python)` constraints file, so it installs into its own environment, not
the core `pip install signalforge-dbt`. Certified floor: **`apache-airflow 2.10.4` on
Python 3.11** (`>=2.8,<3`). Full rationale + the CI shape: `docs/research/airflow-test-environment.md`.

```bash
AIRFLOW_VERSION=2.10.4
PYTHON_VERSION=3.11
CONSTRAINTS="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

uv venv .venv-airflow --python ${PYTHON_VERSION}
uv pip install --python .venv-airflow "apache-airflow==${AIRFLOW_VERSION}" --constraint "${CONSTRAINTS}"
# SignalForge editable/installed. The --constraint is load-bearing: without it,
# google-cloud-bigquery drags protobuf 4->5 over Airflow's pins and risks breaking it.
uv pip install --python .venv-airflow signalforge-dbt --constraint "${CONSTRAINTS}"
```

The `signalforge` CLI must be importable/on PATH in the **same environment** as the
Airflow worker — the example DAG resolves it next to the worker's Python interpreter.

## The example DAG

`examples/airflow/signalforge_generate_dag.py` (`dag_id: signalforge_generate`) has two
tasks:

1. **`generate`** — shells out to `signalforge generate <model> --project-dir <dir>
   --dry-run --format json`, maps the CLI's four-tier exit code to task state, and
   returns the graded tier counts (auto-pushed to XCom).
2. **`gate`** — pulls the tier counts from XCom and fails the run when the flagged count
   exceeds a configurable threshold (the "act on the graded diff" value). Unset
   threshold = report-only, so the DAG runs green out of the box.

Copy it into your `AIRFLOW__CORE__DAGS_FOLDER`, or point the DAGs folder at
`examples/airflow/`.

## Configuration

Set via **Airflow Variables** (Admin → Variables, or `airflow variables set`), with
environment-variable overrides for CI / local runs:

| Airflow Variable          | Env override     | Meaning                                                        |
|---------------------------|------------------|----------------------------------------------------------------|
| `signalforge_project_dir` | `SF_PROJECT_DIR` | dbt project root (must contain `target/manifest.json`) — required |
| `signalforge_model`       | `SF_MODEL`       | model **file-path or unique_id** (a bare name fails) — required |
| `signalforge_max_flagged` | `SF_MAX_FLAGGED` | gate threshold; fail the run if `flagged >` this (unset = no gate) |

The worker also needs the pipeline's own credentials: `ANTHROPIC_API_KEY` for the
drafter/grader and the warehouse env (for BigQuery: `GOOGLE_CLOUD_PROJECT` + Application
Default Credentials). Provide these via the Airflow worker's environment or a secrets
backend — **never** log them.

## Result → task state + XCom (the contract)

The four-tier CLI exit code maps to Airflow task state:

| Exit | Meaning                     | Task outcome                          |
|------|-----------------------------|---------------------------------------|
| 0    | success                     | task succeeds; tier counts → XCom     |
| 1    | load / parse failure        | `AirflowFailException`                |
| 2    | input / invariant failure   | `AirflowFailException`                |
| 3    | external (warehouse / LLM)  | `AirflowFailException`                |

The `generate` task's XCom payload:

```json
{
  "model_unique_id": "model.my_project.stg_orders",
  "kept": 4, "kept_uncertain": 1, "dropped": 11, "flagged": 14,
  "proposed_test_files": 2,
  "run_id": "…"
}
```

Downstream tasks (notification, gating, branching) consume it via
`ti.xcom_pull(task_ids="generate")`.

## Scheduling for drift detection

The example ships with `schedule=None` (manual trigger) so it never auto-spends on
credentials. For nightly schema-drift / signal-rot monitoring, set
`schedule="@daily"` (or run it as a downstream task after your dbt `run` / `build`). A
test that *used* to catch failing rows but now always-passes is exactly the signal-rot
this surfaces — gate or alert on the run-over-run tier-count delta.

## Running it

Headless (no scheduler) — fastest for a one-shot:

```bash
set -a; export SF_PROJECT_DIR=/path/to/dbt SF_MODEL=models/staging/stg_orders.sql; set +a
.venv-airflow/bin/python -c "from airflow.models.dagbag import DagBag; \
  DagBag('examples/airflow', include_examples=False).dags['signalforge_generate'].test()"
```

In the UI:

```bash
export AIRFLOW__CORE__DAGS_FOLDER="$(pwd)/examples/airflow"
.venv-airflow/bin/airflow standalone   # admin password printed; UI on :8080 → trigger signalforge_generate
```

## Testing

`tests/airflow/test_dag_parse.py` ships two gated tests (`@pytest.mark.airflow`):

- **parse** — `DagBag` loads the example with zero import errors and both tasks present
  (no credentials; runs in the gated CI `airflow` job).
- **live** — runs the `generate` task against an `init-demo` project; self-skips without
  `SF_RUN_AIRFLOW=1` + `ANTHROPIC_API_KEY` + `GOOGLE_CLOUD_PROJECT` + `SF_RUN_BQ`.

```bash
uv run --no-sync pytest -m airflow --no-cov   # inside the constraints-pinned airflow venv
```

## Caveats

- **Dedicated operators are roadmap (epic #228).** Today's example wraps the CLI; the
  operator swap is mechanical when it lands.
- **Time-bound tests + reproducibility.** If your draft includes the time-bound
  `row_count_anomaly_by_period` variant, pass `--as-of YYYY-MM-DD` (the DAG's logical
  date is a natural source) so a re-run is reproducible — see `docs/prune-ops.md`.
- **Cost.** Each `generate` run spends real Anthropic + warehouse budget; the prune
  engine's `maximum_bytes_billed` cap and `--dry-run` (no file writes) bound it.
