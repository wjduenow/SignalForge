# Airflow integration

Run SignalForge's **draft → prune → grade → diff** pipeline as a scheduled Airflow
DAG — turning the CLI from an ad-hoc tool into a **scheduled schema-drift / signal-rot
monitor**. Consistent with Architectural Commitment #4 (OSS-first, Core-friendly): an
Airflow DAG runs against any dbt-core project, no dbt Cloud dependency.

> **Status (v0.7).** The shipped surface today is an **example DAG** that drives the
> pipeline through the `signalforge.airflow` helpers (`run_signalforge` +
> `decide_task_outcome` + `raise_for_outcome`) from a `PythonOperator`. Dedicated
> `SignalForgeGenerateOperator` / `SignalForgePruneExistingOperator` are on the roadmap
> (epic #228); they are a drop-in swap for the `PythonOperator` — they reuse the exact
> same result→task-state + XCom contract documented below. The helper-wrapping pattern
> keeps working regardless.

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

1. **`generate`** — calls `run_signalforge(["generate", <model>, "--dry-run",
   "--format", "json"], project_dir=<dir>)` and pushes `result.to_xcom()` (tier counts
   + sidecar paths) to XCom. It deliberately does **not** raise on the pipeline outcome
   so the counts always land in XCom even when the run is later failed / skipped.
2. **`gate`** — pulls the summary from XCom, rebuilds the `SignalForgeRunResult`, and
   acts on it via `decide_task_outcome(result, on_flagged=...)` →
   `raise_for_outcome(outcome, message=...)`. This is the single "act on the graded
   diff" decision point; the `on_flagged` policy (`fail` default) decides what a flagged
   run does.

Splitting run from decide keeps the contract visible; the roadmap
`SignalForgeGenerateOperator` collapses both halves into one `execute()`.

Copy it into your `AIRFLOW__CORE__DAGS_FOLDER`, or point the DAGs folder at
`examples/airflow/`.

## Configuration

Set via **Airflow Variables** (Admin → Variables, or `airflow variables set`), with
environment-variable overrides for CI / local runs:

| Airflow Variable          | Env override     | Meaning                                                        |
|---------------------------|------------------|----------------------------------------------------------------|
| `signalforge_project_dir` | `SF_PROJECT_DIR` | dbt project root (must contain `target/manifest.json`) — required |
| `signalforge_model`       | `SF_MODEL`       | model **file-path or unique_id** (a bare name fails) — required |
| `signalforge_on_flagged`  | `SF_ON_FLAGGED`  | how to treat a flagged (exit-0) run: `fail` (default) / `skip` / `succeed` |

The worker also needs the pipeline's own credentials: `ANTHROPIC_API_KEY` for the
drafter/grader and the warehouse env (for BigQuery: `GOOGLE_CLOUD_PROJECT` + Application
Default Credentials). Provide these via the Airflow worker's environment or a secrets
backend — **never** log them.

## Result → task-state + XCom contract

The integration formalises one contract — reused verbatim by the example DAG today
and the dedicated operators on the roadmap — turning a SignalForge run into (1) an
Airflow task state and (2) an XCom payload. Two Airflow-free pieces in
`signalforge.airflow` carry it (eagerly importable — no Airflow install needed), plus
a thin Airflow-side translator:

- `run_signalforge(argv, *, project_dir, ...) -> SignalForgeRunResult` — runs the
  pipeline and parses its output into a frozen result. Airflow-free.
- `decide_task_outcome(result, *, on_flagged="fail") -> TaskOutcome` — the **pure**
  decision table (the result core). Airflow-free.
- `raise_for_outcome(outcome, *, message)` — the thin Airflow-side translator that
  turns a `TaskOutcome` into the matching Airflow exception. It lives in
  `signalforge.airflow._airflow_compat` (not the package top, and not eagerly
  re-exported); the `from airflow.exceptions import ...` is lazy in its body, so the
  symbol is importable without Airflow, but *calling* it needs the `[airflow]` extra.

### Exit → TaskOutcome → Airflow

`TaskOutcome` is a **separate axis layered on the four-tier CLI exit codes — NOT a
fifth exit tier** (don't collapse exit tiers 2 & 3, and don't invent a fifth). The
exit code is the *input*; the `TaskOutcome` is the *output*:

| Exit | + condition                  | `TaskOutcome`    | Airflow signal                          |
|------|------------------------------|------------------|-----------------------------------------|
| 0    | `flagged == 0`               | `SUCCESS`        | task succeeds                           |
| 0    | `flagged > 0`, `on_flagged=fail`    | `FAIL_NO_RETRY`  | `AirflowFailException` (no retry) |
| 0    | `flagged > 0`, `on_flagged=skip`    | `SKIP`           | `AirflowSkipException`           |
| 0    | `flagged > 0`, `on_flagged=succeed` | `SUCCESS`        | task succeeds                    |
| 1    | load / parse failure         | `FAIL_NO_RETRY`  | `AirflowFailException` (no retry)        |
| 2    | input / invariant failure    | `FAIL_NO_RETRY`  | `AirflowFailException` (no retry)        |
| 3    | external (warehouse / LLM)   | `FAIL_RETRYABLE` | `AirflowException` (Airflow retry policy applies) |

The `AirflowFailException` vs `AirflowException` split is load-bearing: tiers 1/2 are
deterministic (retrying can't help, so retries are bypassed), while tier 3
(auth / rate-limit / warehouse / API blips) is worth retrying under the task's
`retries` / `retry_delay`.

### `on_flagged` keys on the diff's flagged count, not the exit code

A flagged run **exits 0 by default** — SignalForge runs with
`grade.fail_on_below_threshold=false`, so a below-threshold artifact is surfaced via
the diff JSON's `flagged_count > 0` (`SignalForgeRunResult.below_threshold`), **not** via
exit 2. Under the documented default `--dry-run` path there is no sidecar file (it is
suppressed); the count is read off the rendered diff on stdout (see *Transport +
`--dry-run`* below). Tier 2 stays purely *hard* input errors (`ModelNotFoundError`,
anchor-contract failures); conflating it with "reviewable flagged" would mis-route hard
errors to the review branch. So `on_flagged` only applies to a *successful* (exit-0) run:

- `fail` (default — signal over volume): a flagged run is a hard task failure so a
  reviewer sees it.
- `skip`: raises `AirflowSkipException`. Route a downstream review task off it (a
  trigger-rule / `BranchPythonOperator` path).
- `succeed`: pass through — report-only.

### Invocation modes

`run_signalforge(..., invocation=...)`:

- `in_process` (default) — calls `signalforge.cli.main(argv)` directly: no subprocess
  overhead, and the CLI's panic-path + four-tier exit-code mapping come for free. It
  snapshots and restores `sys.excepthook` and the env keys the CLI mutates
  (`NO_COLOR` / `FORCE_COLOR` / `DBT_PROFILES_DIR`) so a long-lived worker stays clean
  across tasks.
- `subprocess` — runs `[sys.executable, "-m", "signalforge", *argv]` (list form, never
  `shell=True`) in a fresh interpreter for full isolation (worker memory hygiene /
  conflicting deps). `timeout_seconds` bounds it.

> **Concurrency caveat.** In-process capture uses `contextlib.redirect_stdout`, which
> is **process-global** — two concurrent in-process invocations in the *same* worker
> would clobber each other's captured output. Use `invocation="subprocess"` when tasks
> may run concurrently in one worker process.

### XCom payload

`SignalForgeRunResult.to_xcom()` returns **counts + sidecar paths only — no secrets**
(audits carry only `blake2b-8` hashes; the model SQL stays in the sidecar *file*,
surfaced by path):

```json
{
  "exit_code": 0,
  "model_unique_ids": ["model.my_project.stg_orders"],
  "kept": 4,
  "kept_uncertain": 1,
  "dropped": 11,
  "flagged": 0,
  "mean_grade": 0.91,
  "below_threshold": false,
  "diff_sidecar_path": "/path/to/dbt/.signalforge/diff.json",
  "grade_sidecar_path": "/path/to/dbt/.signalforge/grade.json",
  "duration_seconds": 312.0
}
```

Downstream tasks consume it via `ti.xcom_pull(task_ids="generate")`. Pushing the *full*
sidecar contents is an opt-in the operator child may add (size-warned — XCom backends
cap payload size; the default keeps the payload small).

### Transport + `--dry-run`

The diff counts are read off **stdout** (`--format json`, injected when absent), not
the sidecar file — because `--dry-run` (the read-only scheduled drift mode) suppresses
both sidecar files. The JSON *shape* (`DiffReport.model_dump_json`) is the contract,
byte-identical whether read from the file or stdout. `mean_grade` is read from
`grade.json` when present; it is `None` under `--dry-run` / `--no-grade` (no grade
sidecar), and `diff_sidecar_path` / `grade_sidecar_path` are `None` when the files
don't exist.

### `--select` batch limitation

Sidecars are last-writer-wins, so a single `run_signalforge` over a `--select` batch
reflects the **last** model's diff JSON; `model_unique_ids` lists the full match set.
Accurate per-model batch XCom is the `SignalForgeGenerateOperator`'s job (it loops per
model). For now, drive one `run_signalforge` per model when you need per-model counts.

### Usage

```python
from signalforge.airflow import decide_task_outcome, run_signalforge
from signalforge.airflow._airflow_compat import raise_for_outcome

result = run_signalforge(
    ["generate", "models/staging/stg_orders.sql", "--dry-run"],
    project_dir="/path/to/dbt",
)
outcome = decide_task_outcome(result, on_flagged="fail")
raise_for_outcome(outcome, message=f"signalforge exit={result.exit_code}")
```

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
