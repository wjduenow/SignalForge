# Airflow integration

Run SignalForge's **draft → prune → grade → diff** pipeline as a scheduled Airflow
DAG — turning the CLI from an ad-hoc tool into a **scheduled schema-drift / signal-rot
monitor**. Consistent with Architectural Commitment #4 (OSS-first, Core-friendly): an
Airflow DAG runs against any dbt-core project, no dbt Cloud dependency.

> **Status (v0.7).** Two dedicated operators have landed (epic #228): the
> **`SignalForgeGenerateOperator`** (issue #232 —
> [section](#signalforgegenerateoperator)) wraps `signalforge generate` (single model or
> a `--select` batch), and the **`SignalForgePruneExistingOperator`** (issue #233 —
> [section](#signalforgepruneexistingoperator)) wraps the no-LLM, read-only
> `signalforge prune-existing` (ingest → prune → diff) as a zero-credential signal-rot
> monitor. Both reuse the exact same result→task-state + XCom contract documented here.
> The **example DAG** that drives the pipeline through the `signalforge.airflow` helpers
> (`run_signalforge` + `decide_task_outcome` + `raise_for_outcome`) from a
> `PythonOperator` still ships as a from-scratch reference; the operators collapse both
> halves of that pattern into one `execute()`.

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
(see the **`--select` batch: per-model XCom + aggregate task-state** section below).
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
| `write` | `--write` (True) / `--dry-run` (False) | default `False`. See the **`write=False`** section below |
| `no_grade` | `--no-grade` | default `False`; skips the grade stage (`mean_grade` → `None`) |
| `cache_scope` | `--cache-scope <scope>` | `per-model` / `project`; omitted when unset (auto-promoted on batches — see below) |
| `as_of` | `--as-of <YYYY-MM-DD>` | reproducibility anchor for time-bound tests; `{{ ds }}` is a natural source. **`template_fields`** |
| `on_flagged` | — (decision layer) | `fail` (default) / `skip` / `succeed`; see the **`on_flagged`** section below |
| `invocation` | — (run mode) | `in_process` (default) / `subprocess`; see the **Invocation modes** section below |
| `signalforge_conn_id` | — (hook) | optional Airflow Connection id; resolves warehouse auth + LLM credentials via `SignalForgeHook` (see the **Airflow-native credentials** section). `None` (default) = ambient-env mode, byte-identical to prior behaviour. **Not** a `template_field` (secrets hygiene). |
| `**kwargs` | — | passed to `BaseOperator` (`retries`, `retry_delay`, `depends_on_past`, …) |

Exactly one of `model` or `select` must be set (a validation error fires at DAG-parse
time otherwise). The five `template_fields` are Jinja-rendered from the task context
before `execute`, so `as_of="{{ ds }}"`, `model="{{ params.model }}"`, etc. work.
`execute` re-validates the rendered values before building any argv (DEC-005).

The operator does **not** take a `config_overrides` param in v0.7 (DEC-002) — see the
**Cost / time guardrails via `signalforge.yml`** section below.

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

A flagged run **exits 0** *under the default* `grade.fail_on_below_threshold=false`, so a
below-threshold artifact surfaces via the diff's `flagged_count > 0`, **not** via exit 2
(DEC-008). `on_flagged` only applies to such a *successful* (exit-0) run. (If you set
`grade.fail_on_below_threshold=true` in `signalforge.yml`, a below-threshold run instead
exits **2** → `FAIL_NO_RETRY` via the exit-code mapping, bypassing the `on_flagged`
branch entirely — so leave it at the default if you want `skip`/`succeed` to take effect.)

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
last-writer-wins sidecar limitation (see the
**`--select` batch behaviour: the raw seam vs the operator** section above).
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

## SignalForgePruneExistingOperator

`SignalForgePruneExistingOperator` runs `signalforge prune-existing` (ingest → prune →
diff, **no LLM call**) as **one Airflow task**. It is the cheapest, fastest,
zero-credential SignalForge integration: a **signal-rot monitor** that grades the dbt
tests a team *already has* (a hand-authored or generated `schema.yml`) against live
warehouse data on a schedule, flagging the tests that *used* to catch failing rows but
now always-pass. Because it makes no Anthropic call, the worker needs **no
`ANTHROPIC_API_KEY`** — only the warehouse credentials.

Like the generate operator it ships behind the `[airflow]` extra. Importing it without
Airflow installed is fine (attribute access stays Airflow-free); **constructing** it
without Airflow raises `ModuleNotFoundError` pointing at
`pip install 'signalforge-dbt[airflow]'`.

### Minimal usage

```python
from signalforge.airflow import SignalForgePruneExistingOperator

monitor = SignalForgePruneExistingOperator(
    task_id="signalforge_signal_rot",
    project_dir="/opt/dbt/my_project",
    model="models/staging/stg_orders.sql",      # file-path or unique_id (a bare name fails)
    schema="models/staging/schema.yml",         # the hand-authored schema.yml to prune
)
```

### Read-only / no-LLM by design (DEC-001)

The operator deliberately exposes **no `write` param and no `mode` param**:

- `prune-existing` has no `--write` — the `--schema` source is a hand-authored file and
  overwriting it would be surprising. The operator is **always read-only**: it passes
  `--dry-run` on every run, so nothing is written to the dbt project (both sidecars
  suppressed) and the diff is read off **stdout** (`--format json`) — the #231 JSON
  transport, not the suppressed sidecar file.
- `--mode` (a `SafetyPolicy` knob) is **inert** on this path — `prune_tests` never reads
  the safety policy, and the LLM payload `--mode` shapes is never built. The warehouse
  knobs that *do* matter are `scope` (`--scope`) and `sample_strategy`
  (`--sample-strategy`).

No Anthropic credential is required or read — this path never touches an LLM credential
at all (no `conn_id`-style LLM connection param exists on the operator).

### `schema` is a required param (DEC-002)

`prune-existing` cannot run without `--schema <path>` (the hand-authored `schema.yml`
whose tests are pruned). `schema` is therefore a **required** operator param; an
empty / `None` / blank value raises `AirflowConfigError` (CLI tier 2) at DAG-parse /
instantiation time, before any `run_signalforge` call — alongside the empty-`project_dir`,
empty-`model`, and leading-dash argv-injection guards.

### Single-model only — no `--select` batch (DEC-003)

`prune-existing` takes a single positional `<model>` and has **no `--select`**. The
operator therefore drops the entire batch apparatus the generate operator carries: no
selector resolution, no per-model loop, no aggregation, no `--cache-scope`. One
`run_signalforge` call, one `SignalForgeRunResult`, one `to_xcom()` payload.

### `on_flagged` is inert without grading (DEC-004)

`prune-existing` calls `render_diff(grading_report=None)` — there is **no grading**, so
there is never a `flagged` tier. Entries are only kept / kept-uncertain / dropped,
`result.flagged` is always `0`, and `result.below_threshold` is always `False`. A clean
(exit-0) run therefore always yields the `SUCCESS` `TaskOutcome` **regardless of
`on_flagged`**.

`on_flagged` is retained (and validated against `{fail, skip, succeed}`) for symmetry
with the sibling operators — and so a future grade pass layered on top would Just Work —
but **it has no effect today**. The exit→`TaskOutcome`→Airflow table for tiers 1/2/3 (a
hard load/parse/input/external error) still applies exactly as documented above: tier 1
or 2 → `FAIL_NO_RETRY`, tier 3 (warehouse/auth) → `FAIL_RETRYABLE`.

### Params → CLI flags

Every `__init__` kwarg and the `signalforge prune-existing` flag it maps to.
`--format json` and `--dry-run` are **always** injected (DEC-005). There is no `write`,
`mode`, `select`, `no_grade`, or `cache_scope` param.

| Operator param | Maps to | Notes |
|----------------|---------|-------|
| `task_id` | — | standard Airflow; required |
| `project_dir` | `--project-dir <dir>` | dbt project root (must contain `target/manifest.json`); required. **`template_fields`** |
| `model` | positional `<model>` | model **file-path or unique_id** (a bare name fails); required. **`template_fields`** |
| `schema` | `--schema <path>` | the hand-authored `schema.yml` to prune; **required** (DEC-002). **`template_fields`** |
| `profiles_dir` | `--profiles-dir <dir>` | overrides `DBT_PROFILES_DIR`; omitted when unset. **`template_fields`** |
| `manifest` | `--manifest <path>` | explicit `manifest.json` override; omitted when unset |
| `scope` | `--scope <scope>` | prune scope (`sample` / `full`); omitted when unset |
| `sample_strategy` | `--sample-strategy <strategy>` | `materialised` / `oneshot`; omitted when unset |
| `as_of` | `--as-of <YYYY-MM-DD>` | reproducibility anchor for time-bound tests; `{{ ds }}` is a natural source. **`template_fields`** |
| `tests_dir` | `--tests-dir <dir>` | directory of singular-test `tests/*.sql` files to ingest too (DEC-006); omitted when unset. **`template_fields`** |
| `on_flagged` | — (decision layer) | `fail` (default) / `skip` / `succeed`; **inert without grading** (DEC-004) |
| `invocation` | — (run mode) | `in_process` (default) / `subprocess`; see the **Invocation modes** section above |
| `signalforge_conn_id` | — (hook) | optional Airflow Connection id; resolves **`profiles_dir` only** via `SignalForgeHook` (read-only — no LLM key; see the **Airflow-native credentials** section). `None` (default) = ambient-env mode, byte-identical to prior behaviour. **Not** a `template_field` (secrets hygiene). |
| `**kwargs` | — | passed to `BaseOperator` (`retries`, `retry_delay`, `depends_on_past`, …) |

The six `template_fields` (`project_dir`, `model`, `schema`, `profiles_dir`, `as_of`,
`tests_dir`) are Jinja-rendered from the task context before `execute`, so
`model="{{ params.model }}"`, `schema="{{ params.schema }}"`, `as_of="{{ ds }}"`, etc.
work. `execute` re-validates the rendered values before building any argv.

### `tests_dir` — singular `custom_sql` test ingestion (DEC-006)

When set, `tests_dir` passes `--tests-dir <dir>` so `prune-existing` also ingests the
singular-test (`tests/*.sql`) files in that directory — the `custom_sql` business-rule
test surface — alongside the `schema.yml` tests, pruning them in one run. Omitted when
unset (the CLI defaults to `<project>/tests`).

## Drift / signal-rot detection

SignalForge's prune step drops a test that *always-passes* on warehouse samples
(Architectural Commitment #1 — an always-pass test is noise). **Signal rot** is the
run-over-run version of that: a test that *used to* catch failing rows (a `kept` /
`kept-uncertain` / `flagged` tier) but **now always-passes** (`dropped` with
`drop_reason == "always-passes"`). The data changed underneath a test that silently
stopped doing anything — a schema-drift alarm worth paging on. A run-over-run **grade
regression** (the mean rubric score fell beyond a threshold) is the second alarming
signal.

Drift detection is **single-model** in v0.7, opt-in, and comes in **two operator
surfaces** — both backed by the same pure, Airflow-free comparison core
(`signalforge.airflow.compute_drift`).

### Form 1 — ergonomic: drift folded into the generate task

The `SignalForgeGenerateOperator` detects drift *as part of* its run when
`detect_drift_against` is set: it parses the current diff off its own stdout, loads the
prior run's `diff.json` from the templated path, computes a `DriftReport`, optionally
persists this run's `diff.json` (+ `grade.json` sibling) for the next run
(`drift_history_dir`), and folds the drift verdict into the task state via `on_drift`.
One task does run + compare + persist + decide. The drift summary rides on the task's
XCom under the `"drift"` key.

```python
from signalforge.airflow import SignalForgeGenerateOperator

monitor = SignalForgeGenerateOperator(
    task_id="drift_monitor",
    project_dir="/opt/dbt/my_project",
    model="models/staging/stg_orders.sql",
    write=False,                       # read-only scheduled default
    as_of="{{ ds }}",                  # reproducibility anchor
    # Compare against yesterday's persisted diff; persist today's for tomorrow.
    detect_drift_against="/opt/airflow/sf_history/{{ macros.ds_add(ds, -1) }}/diff.json",
    drift_history_dir="/opt/airflow/sf_history/{{ ds }}",
    on_drift="fail",                   # alarming drift → hard, no-retry failure
)
```

When `detect_drift_against` is unset the operator behaves byte-identically to the
pre-drift generate operator — no drift key, no behaviour change.

### Form 2 — branchable: generate persists, a dedicated operator gates

When you want the run and the alarm to be **separate tasks** — the generate should
always succeed-and-persist, and a downstream task is the pageable gate you branch /
alert on independently — use the dedicated `SignalForgeDriftOperator`. It runs **no
`signalforge` CLI invocation**: it reads two persisted `diff.json` sidecars
(yesterday + today, plus auto-resolved `grade.json` siblings) off disk, computes the
`DriftReport`, and drives its single task state off the drift verdict. Because it
touches no LLM and no warehouse, the worker needs **no credentials** for this task.

```python
from signalforge.airflow import SignalForgeDriftOperator, SignalForgeGenerateOperator

generate = SignalForgeGenerateOperator(
    task_id="generate",
    project_dir="/opt/dbt/my_project",
    model="models/staging/stg_orders.sql",
    detect_drift_against="/opt/airflow/sf_history/{{ macros.ds_add(ds, -1) }}/diff.json",
    drift_history_dir="/opt/airflow/sf_history/{{ ds }}",  # persists today's diff.json
    on_drift="succeed",                # record-only: the generate task never fails on drift
)

drift_check = SignalForgeDriftOperator(
    task_id="drift_check",
    previous_diff_path="/opt/airflow/sf_history/{{ macros.ds_add(ds, -1) }}/diff.json",
    current_diff_path="/opt/airflow/sf_history/{{ ds }}/diff.json",
    on_drift="fail",                   # the pageable gate
)

generate >> drift_check
```

`previous_diff_path` and `current_diff_path` are **required** params (validated at
DAG-parse). The dedicated operator's XCom **is** the drift payload (not nested under a
`"drift"` key — that nesting is the generate operator's shape).

### `detect_drift_against` + `drift_history_dir` — the date-stamped history pattern

Each run persists a date-stamped `diff.json` (`drift_history_dir="…/{{ ds }}"`); the
next run compares against the prior date's copy (`detect_drift_against="…/{{ macros.ds_add(ds, -1) }}/diff.json"`).
Both params are in `template_fields`, so the logical date stamps the path. Mount the
history base on durable storage so a run can find the prior run's sidecar. Persistence
reuses the fail-closed `write_sidecar` writer (no new writer); the `grade.json` sibling
is copied best-effort. Persistence requires **both** `detect_drift_against` (which enables
the drift step) **and** `drift_history_dir` (the destination): with `drift_history_dir`
unset the run still computes drift but writes nothing.

### `on_drift` (fail / skip / succeed) — most-severe-wins with `on_flagged`

`on_drift` is the run-over-run analogue of `on_flagged`:

- `fail` (default) — an alarming drift is a hard, **no-retry** `AirflowFailException`.
  Signal rot is deterministic, so retrying cannot un-rot it; it is a reviewer signal,
  not a transient.
- `skip` — `AirflowSkipException` (mark the task skipped — route to a review branch).
- `succeed` — pass through (record-only; the drift still rides on XCom).

On the generate operator the flagged-axis (`on_flagged`) and the drift-axis (`on_drift`)
**combine most-severe-wins** on an exit-0 run (`FAIL_NO_RETRY > SKIP > SUCCESS`). A hard
load/parse/input/external error (exit 1/2/3) short-circuits both axes per the
[Exit → TaskOutcome → Airflow](#exit--taskoutcome--airflow) table.

### Degrade, never fail (DEC-013)

The comparison is fail-soft, so a drift monitor never breaks the DAG over its own
bookkeeping:

- **Missing prior `diff.json`** (the first run), **or a corrupt / unreadable / oversize**
  prior sidecar → a non-alarming **baseline** (`baseline=True`); the task SUCCEEDs and
  persists today's diff for next time. The prior loader (`load_diff_report`) is fail-soft
  and returns `None` for absent *and* malformed input alike, so both collapse to the same
  baseline — a corrupt prior is **not** distinguished from a missing one.
- **`model_unique_id` mismatch** between the two diffs → a non-alarming **degraded** report
  with `degrade_reason` set; the task SUCCEEDs. (This is the one degrade path that sets
  `degrade_reason`; it fires inside `compute_drift`, after both diffs load.)
- **Missing `grade.json`** (`--no-grade`) → tier-transition drift is still computed; the
  grade-regression axis is simply empty.
- **Unparseable current diff** (on the generate path) → drift degrades to *no verdict*
  (no `"drift"` key); the generate run's own exit-code / flagged verdict still governs.

A degraded or baseline report is **never** `alarming`, so `on_drift` cannot trip on it.

On the **dedicated** operator only, a missing / unreadable **current** diff is the one
hard error: it reads the current diff from a path (it runs no CLI), so an absent current
file means the operator is misconfigured → `AirflowConfigError` (CLI tier 2), not a
baseline.

### `--as-of` reproducibility

`as_of="{{ ds }}"` flows the logical date into `--as-of` so time-bound primitives (e.g.
`row_count_anomaly_by_period`) are pinned to the scheduled date — see
`docs/prune-ops.md`. The same two sidecars + same `as_of` reproduce a byte-identical
`DriftReport` (the report carries the two input `blake2b-8` hashes).

### Drift XCom payload (`DriftReport.to_xcom`)

The drift summary is JSON-serialisable, carries **no bulk sidecar text and no secrets**,
and on the generate operator is nested under `"drift"` (the dedicated operator returns it
directly). Shape:

- `schema_version`, `model_unique_id`, `as_of` (iso or `null`), `baseline`, `alarming`,
  `degrade_reason`, `grade_regression_threshold`, `previous_diff_hash`, `current_diff_hash`;
- `counts` — per-category counts: `newly_always_passes` (the signal-rot tally),
  `newly_dropped`, `newly_kept`, `added_artifacts`, `removed_artifacts`,
  `grade_regressions`, `columns_added`, `columns_removed`;
- the transition lists (`newly_always_passes` / `newly_dropped` / `newly_kept` as
  artifact dicts with a truncated one-line `why`), `added_artifacts` /
  `removed_artifacts` (artifact-id lists), `grade_regressions`, `schema_shape_changes`.

`alarming` is `True` iff there is signal rot (`newly_always_passes`) or a grade
regression (`grade_regressions`) — those are the categories that page; `newly_kept` /
`newly_dropped` / added / removed / schema-shape are informational.

### Worked example DAG

`examples/airflow/signalforge_nightly_drift.py` (`dag_id="signalforge_nightly_drift"`)
ships both surfaces side by side: a `drift_monitor_ergonomic` task (Form 1) and a
`generate` → `drift_check` pair (Form 2). It ships `schedule=None`; set
`schedule="@daily"` for a real nightly monitor.
## Airflow-native credentials: `SignalForgeHook` + `signalforge_conn_id`

Instead of managing `ANTHROPIC_API_KEY` / `DBT_PROFILES_DIR` as inline env on every
task, configure SignalForge the **Airflow-native** way: one Airflow **Connection**
(plus an optional **Variable** for the LLM key), referenced by both operators via the
optional `signalforge_conn_id` param. The hook (`SignalForgeHook`) resolves that
Connection into the three things a run needs — warehouse auth (`profiles_dir`), the LLM
`provider`, and the `api_key` — and the operator injects the key into the run's
environment only for the duration of that run.

`signalforge_conn_id` is **opt-in**: when it is `None` (the default) both operators
behave exactly as before (ambient env / inline config), byte-identical to #232/#233.
Set it to switch to the Connection-driven path.

### Setting up the Connection

Create one Connection (Admin → Connections, or `airflow connections add`) and point
both operators at it via `signalforge_conn_id="<conn id>"`:

- **Conn Type** — any (e.g. `generic`); the hook reads only `password` + `extra`.
- **Password** — the **LLM API key** (the *primary* key source). Airflow auto-masks a
  Connection `password` in task logs.
- **Extra (JSON)** — the validated schema below.

```bash
airflow connections add signalforge_default \
  --conn-type generic \
  --conn-password "$ANTHROPIC_API_KEY" \
  --conn-extra '{"profiles_dir": "/opt/airflow/dbt_project", "provider": "anthropic", "cache_scope": "project"}'
```

### Connection `extra` schema (validated `extra="forbid"`)

The `extra` JSON is parsed through a Pydantic model with `extra="forbid"`, so an
**unknown key fails loud** at resolution (a typo like `cache_scop` raises
`AirflowConfigError`, tier 2 — it never silently no-ops). Exactly three keys, all
optional:

| `extra` key    | Meaning                                                                              |
|----------------|--------------------------------------------------------------------------------------|
| `profiles_dir` | Directory holding the dbt `profiles.yml` → mapped to `--profiles-dir` (warehouse auth). Omit to use the worker's ambient `DBT_PROFILES_DIR`. |
| `provider`     | LLM SKU family — `anthropic` / `openai` / `gemini`. Selects the env var the resolved key is injected into (see allowlist below). Omit for a prune-existing-only Connection (no LLM call). |
| `cache_scope`  | `per-model` / `project` — the Anthropic cached-prefix knob. **Generate only** (PruneExisting ignores it). |

**Cost ceilings are deliberately NOT supported in the `extra` for v0.7** (there is no
CLI flag that delivers them — `config_overrides` is deferred, DEC-011). Bound a
scheduled DAG's spend through the committed `signalforge.yml grade:` block instead
(`max_grade_cost_usd` / `max_grade_calls` / `max_grade_tokens` / `total_budget_seconds`
— see the **Cost / time guardrails via `signalforge.yml`** section above).

### API key: `password` primary, Variable fallback

The key's primary source is the Connection `password`. When that is empty, the hook
falls back to an Airflow **Variable** named **`signalforge_api_key`** (the
`signalforge.airflow._resolve.API_KEY_VARIABLE_KEY` constant — a single fixed,
provider-agnostic name):

```bash
airflow variables set signalforge_api_key "$ANTHROPIC_API_KEY"
```

The resolver is **lenient** — it returns `api_key` / `provider` as `None` when absent;
*requiredness is enforced by the consuming operator* (Generate raises when either is
missing; PruneExisting needs neither). Prefer the Connection `password` (it auto-masks);
the Variable is the fallback.

### Provider allowlist (closed)

`provider`, when present, must be one of a **closed allowlist** — the single source of
truth is `signalforge.llm.providers.PROVIDER_ENV_VAR_KEYS`:

| `provider`  | env var the key is injected into |
|-------------|----------------------------------|
| `anthropic` | `ANTHROPIC_API_KEY`              |
| `openai`    | `OPENAI_API_KEY`                 |
| `gemini`    | `GOOGLE_API_KEY` (the `google-genai` SDK's convention, not a `GEMINI_*` name) |

An **unknown provider fails loud** (`AirflowConfigError`) — the check runs whenever
`provider` is set, regardless of operator, so an arbitrary env-var name can never be
derived from a Connection (this closes the arbitrary-env-var injection vector even for a
prune-existing-only Connection that happens to set `provider`).

### Precedence: explicit param > Connection `extra` > default (DEC-012)

For the two knobs that exist on both surfaces — `profiles_dir` (both operators) and
`cache_scope` (Generate only) — an explicit, non-empty **operator param wins** over the
Connection `extra` value, which in turn wins over the downstream tool default. Mirrors
the CLI's `flag > YAML > default` precedence: set `profiles_dir=...` on the operator to
override the Connection's `extra.profiles_dir` for that one task.

### Generate vs PruneExisting credentials (DEC-016)

The two operators consume the **same** Connection differently:

- **`SignalForgeGenerateOperator`** calls the LLM, so it **requires** both a `provider`
  and an `api_key` (else `AirflowConfigError`), and uses `profiles_dir` for warehouse
  auth. It masks the resolved key, precedence-merges `profiles_dir` / `cache_scope`, and
  injects the provider's API-key env var around the run.
- **`SignalForgePruneExistingOperator`** is read-only and makes **no LLM call**, so it
  uses **only** `profiles_dir` — `provider` / `api_key` / `cache_scope` on the resolution
  are ignored. It calls **no** `register_secret`, injects **no** provider env var. A
  prune-existing-only deployment can therefore use a Connection with just
  `{"profiles_dir": "..."}` and no `password` / `provider` at all. (An allowlist-invalid
  `provider`, if one *is* present, still raises in the resolver per the closed allowlist.)

### Secrets hygiene — the four surfaces

The resolved LLM API key never appears in any of these:

1. **Task logs** — Airflow's secrets-masker (the operator calls `register_secret(key)`
   before any logging or run, belt-and-braces over Airflow's auto-masking of a
   Connection `password`).
2. **XCom** — `to_xcom()` carries tier **counts + sidecar paths only** (no secrets); the
   key is held in a local, never stored on the operator/hook instance.
3. **Rendered templates** — `signalforge_conn_id` is **NOT** a `template_fields` entry
   (templating a credential reference is an avoidable leak surface), so it never renders
   into the task-instance rendered fields.
4. **`__repr__`** — `SignalForgeHook.__repr__` shows only the conn id; the resolution's
   `HookResolution.__repr__` shows only `profiles_dir` + `provider` — never the key value
   *or* a field-name label that would reveal a credential is present (mirrors
   `SnowflakeAdapter.__repr__`).

### In-process concurrency caveat (DEC-015)

For `invocation="in_process"` (the default), the operator injects the provider's API-key
env var into `os.environ` only **around** the `run_signalforge` call and restores it in a
`finally` (absent-before → deleted; prior value → restored) — so the secret does not
linger in a long-lived worker across tasks. But because `os.environ` (and the in-process
stdout capture) is **process-global**, two concurrent in-process tasks in the *same*
worker can race on the injected key / captured output. **Prefer `invocation="subprocess"`
for concurrent multi-task workers** — it runs each task in a fresh interpreter with its
own environment.

### Usage

```python
from signalforge.airflow import (
    SignalForgeGenerateOperator,
    SignalForgePruneExistingOperator,
)

# Generate: needs provider + key + profiles_dir, all from the Connection.
generate = SignalForgeGenerateOperator(
    task_id="drift_monitor",
    project_dir="/opt/airflow/dbt_project",
    signalforge_conn_id="signalforge_default",
    select="tag:staging",
    write=False,
)

# PruneExisting: uses ONLY profiles_dir from the same Connection (no LLM key).
prune = SignalForgePruneExistingOperator(
    task_id="signal_rot_monitor",
    project_dir="/opt/airflow/dbt_project",
    signalforge_conn_id="signalforge_default",
    model="models/staging/stg_orders.sql",
    schema="models/staging/schema.yml",
)
```

The shipped example DAG `examples/airflow/signalforge_hook_dag.py`
(`dag_id: signalforge_hook`) configures **both** operators via a single
`signalforge_conn_id` (+ the `signalforge_api_key` Variable) with **no inline per-task
env** — copy it as a starting point.

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

## Managed Airflow runtimes (Astronomer / MWAA / Composer)

SignalForge's Airflow operators are **documented, not certified** against managed
Airflow runtimes — **Astronomer**, **AWS MWAA**, and **Google Cloud Composer**. The
certified target is the constraints-pinned local stack above (`apache-airflow 2.10.4`
on Python 3.11); the notes below are **pointers, not a support claim**. Treat them as a
starting point and validate against your own runtime.

The integration is designed to drop into a managed runtime cleanly — the base
`pip install signalforge-dbt` stays **Airflow-free** (Architectural Commitment #4), so
it can sit alongside whatever Airflow the runtime already pins without dragging a second
Airflow tree into the resolution. Three practical caveats under a managed scheduler:

- **The `[airflow]` extra vs the runtime's pinned Airflow.** A managed runtime ships its
  own Airflow at a fixed version, installed under its own constraints. Do **not** let
  `signalforge-dbt[airflow]` (which declares `apache-airflow>=2.8,<3`) re-resolve or
  upgrade that Airflow. Install SignalForge **compatibly** — typically just
  `signalforge-dbt` (the operators import Airflow from the runtime; the `[airflow]`
  extra exists mainly to pin Airflow in a *standalone* env). When you do need the extra,
  add it under the runtime's own constraints file (the same `--constraint` discipline as
  the local install above) so it can never pull Airflow's pins forward. The certified
  version floor lives in `docs/research/airflow-test-environment.md` — confirm your
  runtime's Airflow is `>=2.8,<3`.
- **Where credentials live — the runtime's secrets backend.** Prefer the Airflow-native
  `SignalForgeHook` + `signalforge_conn_id` path (see the **Airflow-native credentials**
  section) over inline per-task env. On a managed runtime, the Connection / Variable it
  resolves come from that runtime's configured **secrets backend** (Astronomer secrets,
  AWS Secrets Manager for MWAA, Google Secret Manager for Composer) — SignalForge reads
  the resolved Connection / Variable exactly the same way regardless of backend, so the
  LLM key and `profiles_dir` stay out of your DAG source and out of task logs.
- **The base install stays Airflow-free.** Because `signalforge-dbt`'s core has no
  Airflow dependency, adding SignalForge to a managed image is additive — it never forces
  the runtime's Airflow version. The operators resolve Airflow lazily at *construction*
  time, so importing `signalforge.airflow` in a DAG file parses fine even on a worker
  image where the extra wasn't separately installed (construction then fails loud with
  the `pip install 'signalforge-dbt[airflow]'` pointer if Airflow truly is absent).

These are deployment pointers only — for the certified version floor, the `airflow`
pytest marker, and the CI test recipe, see `docs/research/airflow-test-environment.md`.

## Testing

`tests/airflow/test_dag_parse.py` ships gated tests (`@pytest.mark.airflow`):

- **parse** — `DagBag` loads each shipped example with zero import errors and the
  expected tasks present: the two-`PythonOperator` pipeline (`signalforge_generate`), the
  `SignalForgeGenerateOperator` drift monitor (`signalforge_generate_operator`), the
  `SignalForgePruneExistingOperator` signal-rot monitor
  (`signalforge_prune_existing_operator`), the Connection-configured both-operators
  example (`signalforge_hook` — `drift_monitor` + `signal_rot_monitor` wired via
  `signalforge_conn_id`), and the run-over-run drift monitor
  (`signalforge_nightly_drift`). No credentials; runs in the gated CI `airflow` job.
- **render** — each operator's `template_fields` Jinja-render from a synthetic task
  context (e.g. `{{ params.model }}` / `{{ ds }}`).
- **execute** — `tests/airflow/test_drift_operators.py` and
  `tests/airflow/test_operators.py` drive each operator's `execute` against a fake
  `run_signalforge` (and, for the dedicated drift operator, the committed
  `tests/fixtures/airflow/drift_pairs/*.json` sidecars): the result/drift → `TaskOutcome`
  → Airflow-signal translation, the XCom shape, and the baseline / degrade / unparseable
  paths. No credentials; runs in the gated CI `airflow` job.
- **live** — runs the `generate` task against an `init-demo` project; self-skips without
  `SF_RUN_AIRFLOW=1` + `ANTHROPIC_API_KEY` + `GOOGLE_CLOUD_PROJECT` + `SF_RUN_BQ`.

```bash
uv run --no-sync pytest -m airflow --no-cov   # inside the constraints-pinned airflow venv
```

## Caveats

- **Three operators have landed (epic #228):** the `SignalForgeGenerateOperator` (#232,
  [section](#signalforgegenerateoperator)), the no-LLM `SignalForgePruneExistingOperator`
  (#233, [section](#signalforgepruneexistingoperator)), and the run-over-run drift surface
  (#235) — `detect_drift_against` on the generate operator plus the dedicated
  `SignalForgeDriftOperator` ([section](#drift--signal-rot-detection)). The `PythonOperator`
  example DAG remains a from-scratch reference.
- **Time-bound tests + reproducibility.** If your draft includes the time-bound
  `row_count_anomaly_by_period` variant, pass `--as-of YYYY-MM-DD` (the DAG's logical
  date is a natural source) so a re-run is reproducible — see `docs/prune-ops.md`.
- **Cost.** Each `generate` run spends real Anthropic + warehouse budget; the prune
  engine's `maximum_bytes_billed` cap and `--dry-run` (no file writes) bound it. A
  `prune-existing` run spends **warehouse budget only** (no LLM call), bounded by the
  same `maximum_bytes_billed` cap.
- **Managed runtimes are documented, not certified.** Running on Astronomer / MWAA /
  Composer is supported as a set of deployment pointers (the `[airflow]`-extra vs
  pinned-Airflow interplay, the secrets-backend credential path) — not a certification.
  See the **Managed Airflow runtimes** section above.
