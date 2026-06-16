# Airflow integration

Run SignalForge's **draft → prune → grade → diff** pipeline as a scheduled Airflow
DAG — turning the CLI from an ad-hoc tool into a **scheduled schema-drift / signal-rot
monitor**. Consistent with Architectural Commitment #4 (OSS-first, Core-friendly): an
Airflow DAG runs against any dbt-core project, no dbt Cloud dependency.

> **Status (v0.7).** The dedicated **`SignalForgeGenerateOperator`** has landed (epic
> #228, issue #232) — see [SignalForgeGenerateOperator](#signalforgegenerateoperator)
> below. It is the recommended surface: one task wraps `signalforge generate` (single
> model or a `--select` batch) and reuses the exact same result→task-state + XCom
> contract documented here. The **example DAG** that drives the pipeline through the
> `signalforge.airflow` helpers (`run_signalforge` + `decide_task_outcome` +
> `raise_for_outcome`) from a `PythonOperator` still ships as a from-scratch reference;
> the operator collapses both halves of that pattern into one `execute()`. A
> `SignalForgePruneExistingOperator` remains on the roadmap.

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

### `--select` batch behaviour: the raw seam vs the operator

This is a property of the **raw `run_signalforge` seam**, not the operator. A single
`run_signalforge` call over a `--select` batch lets the CLI's own batch driver render
every matched model, but the sidecars are last-writer-wins — so the parsed result
reflects only the **last** model's diff JSON, and `model_unique_ids` contains at most
that last model's id (NOT the full match set). If you call the raw seam directly and
need per-model counts, drive one `run_signalforge` per model yourself.

The **`SignalForgeGenerateOperator` overcomes this**: it resolves the `--select`
expression itself and loops `run_signalforge` once per matched model, so its XCom
carries accurate per-model counts plus a rollup aggregate
(see [`--select` batch: per-model XCom + aggregate task-state](#-select-batch-per-model-xcom-aggregate-task-state)).
Prefer the operator over the raw seam for any batch.

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

## SignalForgeGenerateOperator

`SignalForgeGenerateOperator` runs `signalforge generate` (a single model or a
`--select` batch) as **one Airflow task**. It wraps the Airflow-free
`run_signalforge` seam: maps its params to a `generate` argv, runs the pipeline, maps
the result to a `TaskOutcome` via the pure `decide_task_outcome`, and raises the
matching Airflow signal — returning the run's XCom payload (counts + sidecar paths).
It is the recommended surface over the hand-wired `PythonOperator` example DAG.

The operator ships behind the `[airflow]` extra. Importing it without Airflow
installed is fine (attribute access stays Airflow-free); **constructing** it without
Airflow raises `ModuleNotFoundError` pointing at `pip install 'signalforge-dbt[airflow]'`.

### Minimal usage

```python
from signalforge.airflow import SignalForgeGenerateOperator

monitor = SignalForgeGenerateOperator(
    task_id="signalforge_nightly",
    project_dir="/opt/dbt/my_project",
    select="tag:nightly",   # or model="models/staging/stg_orders.sql"
    write=False,            # dry-run: the safe scheduled default (no file writes)
    on_flagged="fail",      # a below-threshold (flagged) run fails the task
)
```

### Params → CLI flags

Every `__init__` kwarg and the `signalforge generate` flag it maps to. `--format json`
and `--project-dir` are always injected by the runner.

| Operator param | Maps to | Notes |
|----------------|---------|-------|
| `task_id` | — | standard Airflow; required |
| `project_dir` | `--project-dir <dir>` | dbt project root (must contain `target/manifest.json`); required. **`template_fields`** |
| `model` | positional `<model>` | model **file-path or unique_id** (a bare name fails); mutex with `select`. **`template_fields`** |
| `select` | `--select <expr>` | dbt-style selector (`tag:…`, `path:…`, comma-union); mutex with `model`. **`template_fields`** |
| `profiles_dir` | `--profiles-dir <dir>` | overrides `DBT_PROFILES_DIR`; omitted when unset. **`template_fields`** |
| `write` | `--write` (True) / `--dry-run` (False) | default `False`. See [`write=False`](#writefalse-is-dry-run-the-safe-scheduled-default) |
| `no_grade` | `--no-grade` | default `False`; skips the grade stage (`mean_grade` → `None`) |
| `cache_scope` | `--cache-scope <scope>` | `per-model` / `project`; omitted when unset (auto-promoted on batches — see below) |
| `as_of` | `--as-of <YYYY-MM-DD>` | reproducibility anchor for time-bound tests; `{{ ds }}` is a natural source. **`template_fields`** |
| `on_flagged` | — (decision layer) | `fail` (default) / `skip` / `succeed`; see [`on_flagged` branches](#on_flagged-keys-on-the-diffs-flagged-count-not-the-exit-code) |
| `invocation` | — (run mode) | `in_process` (default) / `subprocess`; see [Invocation modes](#invocation-modes) |
| `**kwargs` | — | passed to `BaseOperator` (`retries`, `retry_delay`, `depends_on_past`, …) |

Exactly one of `model` or `select` must be set (a validation error fires at DAG-parse
time otherwise). The five `template_fields` are Jinja-rendered from the task context
before `execute`, so `as_of="{{ ds }}"`, `model="{{ params.model }}"`, etc. work.
`execute` re-validates the rendered values before building any argv (DEC-005).

The operator does **not** take a `config_overrides` param in v0.7 (DEC-002) — see
[Cost / time guardrails](#cost-time-guardrails-via-signalforgeyml) below.

### `write=False` is `--dry-run` (the safe scheduled default)

`write=False` (the default) passes `--dry-run`: the run writes **nothing** — no
`schema.yml`, no proposed `.sql`, no `.signalforge/diff.json` or `grade.json` sidecars
(DEC-003). XCom counts are built from the diff JSON on **stdout**, and the
`diff_sidecar_path` / `grade_sidecar_path` XCom fields are `None`. This is exactly what
a scheduled drift monitor wants: observe the tier-count delta run-over-run without
mutating the repo.

`write=True` passes `--write`: SignalForge writes the proposed `schema.yml` (kept tests)
plus any `custom_sql` `.sql` files, and the sidecar files are written so the path fields
populate.

### `on_flagged` keys on the diff's flagged count, not the exit code

A flagged run **exits 0** (SignalForge runs `grade.fail_on_below_threshold=false`), so a
below-threshold artifact surfaces via the diff's `flagged_count > 0`, **not** via exit 2
(DEC-008). `on_flagged` only applies to a *successful* (exit-0) run:

- `fail` (default — signal over volume): a flagged run is a hard task failure
  (`AirflowFailException`, no retry) so a reviewer sees it.
- `skip`: raises `AirflowSkipException` — route a downstream review task off it (a
  trigger-rule / `BranchPythonOperator` path).
- `succeed`: pass through — report-only.

Hard input errors (tier 2: `ModelNotFoundError`, anchor-contract failures) stay hard
task failures regardless of `on_flagged` — they never route to the review branch.

### Invocation modes

`invocation` selects how the operator runs the CLI:

- `in_process` (default) — calls `signalforge.cli.main(argv)` directly: no subprocess
  overhead, and the CLI's panic-path + four-tier exit-code mapping come for free. It
  snapshots and restores `sys.excepthook`, the env keys the CLI mutates
  (`NO_COLOR` / `FORCE_COLOR` / `DBT_PROFILES_DIR`), and the root logger so a long-lived
  worker stays clean across tasks.
- `subprocess` — runs `[sys.executable, "-m", "signalforge", *argv]` (list form, never
  `shell=True`) in a fresh interpreter.

> **Concurrency caveat (DEC-004).** In-process capture uses
> `contextlib.redirect_stdout`, which is **process-global** — two concurrent
> `in_process` tasks in the *same* worker would clobber each other's captured output.
> Use `invocation="subprocess"` for parallel / high-concurrency workers; it is the clean
> per-task isolation choice.

### Cost / time guardrails via `signalforge.yml`

The operator takes **no** cost/time-ceiling params in v0.7 (DEC-002). Bound scheduled
spend through the project's committed `signalforge.yml grade:` block instead — every run
the operator launches reads it:

```yaml
grade:
  max_grade_cost_usd: 5.00     # degrade-not-fail once the run's grade spend hits the cap
  max_grade_calls: 200         # cap on judge calls
  max_grade_tokens: 500000     # cap on judge tokens
  total_budget_seconds: 600    # absolute wall-clock cap on the grade stage
```

These are the documented way to cap a scheduled DAG's Anthropic spend. (Warehouse spend
is bounded separately by the prune engine's `maximum_bytes_billed`, and `write=False`
keeps the run read-only.)

### `--select` batch: per-model XCom + aggregate task-state

For a `--select` batch the operator resolves the selector to its model unique_ids itself
and loops `run_signalforge` once per model (DEC-001), overcoming the raw seam's
last-writer-wins sidecar limitation (see
[`--select` batch behaviour](#-select-batch-behaviour-the-raw-seam-vs-the-operator)).
When `cache_scope` is unset and ≥2 models match, the operator forces
`--cache-scope project` on each looped call (DEC-007) so Anthropic's server-side prompt
cache amortises the byte-identical project prefix across the siblings instead of paying
cache-creation per model.

The XCom payload for a batch is `{"models": [<per-model to_xcom() dict>…],
"aggregate": <rollup to_xcom() dict>}` (DEC-010). The **single** Airflow task state is
driven by the aggregate (DEC-008): `exit_code = max(per-model exit codes)` over the
four-tier severity ordering (mirrors the CLI's `_run_batch.total_exit_code`); the tier
counts (`kept` / `kept_uncertain` / `dropped` / `flagged`) are element-wise sums;
`mean_grade` is the mean of the non-`None` per-model means; the aggregate's sidecar paths
are `None` (a batch has no single sidecar). A batch mixing a tier-2 and a tier-3 model
maps to the retryable tier-3 outcome — the tier-2 model re-fails until the task's retry
budget exhausts (acceptable at single-task granularity).

A single-model run returns the bare `result.to_xcom()` dict (no `models`/`aggregate`
wrapper).

### Concurrent `project_dir` sidecar caveat

Two runs against the **same** `project_dir` collide on the `.signalforge/*.json`
sidecars (`O_TRUNC`, last-writer-wins). Under the default `write=False` (dry-run) this is
a non-issue — no sidecars are written, so there is nothing to collide. With `write=True`,
give each concurrent run its own `project_dir` (or accept last-writer-wins). This is
distinct from the in-process stdout-capture concurrency caveat above (that one bites even
under dry-run; use `subprocess` for it).

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

- **`SignalForgeGenerateOperator` has landed (epic #228, #232);** see
  [SignalForgeGenerateOperator](#signalforgegenerateoperator). The `PythonOperator`
  example DAG remains a from-scratch reference. A `SignalForgePruneExistingOperator`
  is still roadmap.
- **Time-bound tests + reproducibility.** If your draft includes the time-bound
  `row_count_anomaly_by_period` variant, pass `--as-of YYYY-MM-DD` (the DAG's logical
  date is a natural source) so a re-run is reproducible — see `docs/prune-ops.md`.
- **Cost.** Each `generate` run spends real Anthropic + warehouse budget; the prune
  engine's `maximum_bytes_billed` cap and `--dry-run` (no file writes) bound it.
