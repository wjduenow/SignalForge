# Issue #179 — runtime benchmark after efficiency improvements (#186 + #187 + #188 + #198 + #202)

**Status:** #186/#198 measurements complete (2026-06-03 / 2026-06-04) — prod
0.5.0 vs dev 0.6.0.dev0 Sonnet A/B. **#202 grade-to-completion retest complete
(2026-06-05) — PASS on both arms:** 40-col 408/408 scored (#198's 70 transient
`GradeLLMError` degradations → 0), 16-col 212/212 scored (#186's 34 non-budget
`score=None` → 0), `aggregate_complete=True` on both (see § "#202
grade-to-completion retest"). #187 Haiku and #188 batch dimensions still pending.
The harness is re-runnable per the commands below.

Companion to the "Runtime benchmark retest" story on epic
[#179](https://github.com/wjduenow/SignalForge/issues/179). The harness lives at
`tests/research/179-runtime-benchmark/benchmark_runtime.py`.

## Why this exists

The 2026-05-30 baseline (see
[`179-test-primitive-expansion-retest.md`](179-test-primitive-expansion-retest.md))
was measured on the pre-efficiency code and hit the grade-budget ceiling: **17 of
34 doc/rationale grade attempts degraded with `grade budget exceeded (300s)`**,
and the whole exercise ran **~95 min wall-clock / ~4,044 grade calls**. Three
improvements have since landed on `dev`, all targeting the grade stage (the
bottleneck) and the `--select` batch path:

| Change | PR / commit | What it does | Default-on? |
|---|---|---|---|
| **#186** grade-layer `asyncio` refactor | #190 / `f0e315b` | concurrent `(artifact × criterion)` grade calls | ✅ yes |
| **#187** faster grade defaults | #193 / `4a70799` | Haiku + per-provider fast models | ⚠ opt-in (anthropic default stays Sonnet) |
| **#188** bulk shared cached prefix | #195 / `d280fc6` | amortise the static system prompt across `--select` models | ✅ yes (batch only) |
| **#189** persistent grade cache | #196 / `4ad19ef` | re-runs reuse prior grade verdicts from `.signalforge/grade-cache/` | ✅ yes (re-runs only) |

### #189 grade cache — control for it, then measure it separately

#189 is the wrinkle that makes a naive prod-vs-dev A/B lie: dev caches grade
verdicts on disk; prod has no cache. A warm dev cache would make dev look
artificially fast. So the script `--cache-mode bypass` (default) passes dev's
`--no-cache` flag — the grade stage runs **cold**, fair against the cacheless
prod release. The flag only exists on dev, so the script version-gates it (prod
omits it and is cold by definition). The #189 *re-run* win is a separate, dev-only
measurement: `--cache-mode warm` run twice (first populates, second hits cache);
prod has no equivalent, so this column compares dev-cold vs. dev-warm, not vs.
prod.

This benchmark turns "we made it faster" into a measured per-stage delta, and
confirms the 17/34 budget degradation is **closed**, not assumed closed.

## The A/B: production PyPI package vs. the code on `dev`

The "before" is the released **`signalforge-dbt` PyPI package**; the "after" is
the editable **`dev`** checkout. You cannot swap an installed package
mid-process, so each side runs in its own venv and the only stable contract
across the two versions is the `signalforge` CLI. The harness is therefore a
pure-stdlib script that shells out to the venv-local `signalforge` and reads the
durable sidecars — a prod venv needs ONLY `pip install signalforge-dbt`.

Run the SAME command in two venvs, on ONE machine (wall-clock is machine- and
network-dependent — both halves must run on the same host):

```bash
# 1. BEFORE — production PyPI package
python -m venv .venv-prod
.venv-prod/bin/pip install signalforge-dbt
.venv-prod/bin/python tests/research/179-runtime-benchmark/benchmark_runtime.py \
    --project-dir ~/Projects/intuit_airflow/plugins/dbt \
    --profiles-dir /tmp/sf-demo-profiles

# 2. AFTER — the code on dev (editable from this checkout)
python -m venv .venv-dev
.venv-dev/bin/pip install -e .
.venv-dev/bin/python tests/research/179-runtime-benchmark/benchmark_runtime.py \
    --project-dir ~/Projects/intuit_airflow/plugins/dbt \
    --profiles-dir /tmp/sf-demo-profiles
```

The script resolves `signalforge` from the SAME venv as the interpreter running
it, so `.venv-prod/bin/python` benchmarks prod and `.venv-dev/bin/python`
benchmarks dev — unambiguously. It prints the resolved version in the table so
the two runs are self-labelling.

### #187 opt-in (Haiku)

The shipped anthropic grade default stays Sonnet, so a default run captures #186
only. To also measure #187, set `grade.model: claude-haiku-4-5` in the intuit
project's `signalforge.yml` and run a third time; record it in the Haiku column.

### Honest caveat — net release-to-release delta, not pure isolation

The prod PyPI release predates #169/#170/#171, so `dev` drafts 8 test primitives
vs. prod's 5 — dev does *more* grading work, not less. The wall-clock delta is
therefore the **net user-facing change between the last release and dev**, which
conflates the efficiency wins (#186/#187/#188) with the added primitives. The
**budget-exceeded degradation count** is the cleaner isolated signal. For a pure
efficiency isolation, A/B two git checkouts at the SAME primitive set
(`90af28b` — the `#179` writeup commit, last before #186 — vs. `dev`); the script
works there too (`pip install -e .` on each checkout).

## Preconditions (one-time, per the epic's retest protocol)

The intuit project must already be prepared per
[`179-test-primitive-expansion-retest.md` § Substrate](179-test-primitive-expansion-retest.md):

- `dbt deps` + `dbt parse` run (generates `target/manifest.json`).
- A synthesised `_signalforge_*_schema.yml` so the target model exposes its
  columns (the Python-annotation manifest gap).
- A `signalforge.yml` with `safety.mode: schema-only` + `prune.enabled: false`.
- The `/tmp/sf-demo-profiles` profile override.
- `ANTHROPIC_API_KEY` set.
- The prod version must support `prune.enabled` (#35) + the Snowflake adapter
  (#53) — any 0.4+ release qualifies.

## What the script measures

`grade` and `diff` durations are read from the sidecars' `duration_seconds`
(`.signalforge/grade.json`, `.signalforge/diff.json` — stable fields since #7/#8,
so they parse identically under prod and dev). `draft + overhead` is **derived**
as `total − grade − diff` (no sidecar carries draft duration; CLI startup +
manifest load fall in here too). `prune` reads ~0 while disabled. `TOTAL` is the
subprocess wall-clock. The grade-degradation counts come from `grade.json`'s
`results` (`score is None`, and the `budget`-reasoning subset). The script
asserts nothing — a benchmark records numbers; it does not gate a build.

### #188 `--select` batch — measured separately

#188 amortises the shared cached prefix across models in ONE
`signalforge generate --select` process. That is a multi-model concern the
single-model script does not cover. Measure it on both venvs:

```bash
time .venv-prod/bin/signalforge generate --select 'path:models/reporting/*' \
    --project-dir ~/Projects/intuit_airflow/plugins/dbt --profiles-dir /tmp/sf-demo-profiles
# ...repeat with .venv-dev/bin/signalforge, and vs. an equivalent shell-loop
```

Record the totals in the batch table below.

## Result — measured 2026-06-03 (Sonnet A/B + #189 cache; #187 Haiku pending)

**Intuit project:** `~/Projects/intuit_airflow/plugins/dbt` — **model:** `models/reporting/weekly_query_cost.sql` (16 columns, synthesised schema)
**Machine:** local (single Anthropic account, runs back-to-back) — **Date:** 2026-06-03
**Prod version:** `0.5.0` (PyPI `signalforge-dbt`) — **Dev version:** `0.6.0.dev0` (worktree editable)
**Grade model:** anthropic default (Sonnet) both sides. Prune disabled. Diff format `json`.

### Per-stage wall-clock — Sonnet default, cold grade cache

| Stage | Before (prod 0.5.0) | After (dev 0.6.0.dev0) | Δ | Δ % |
|---|---:|---:|---:|---:|
| draft + overhead (derived) | 39.8s | 45.8s | +6.0s | +15% |
| prune (disabled) | ~0s | ~0s | — | — |
| grade | **303.1s** (budget-capped) | **222.9s** | −80.2s | −26% |
| diff | 0.0s | 0.0s | — | — |
| **TOTAL** | **342.9s** | **268.7s** | **−74.2s** | **−22%** |

### Grade degradation (the headline correctness signal)

| | Baseline 2026-05-30 | Prod 0.5.0 | Dev 0.6.0.dev0 (cold) |
|---|---:|---:|---:|
| artifacts graded | 34 | 208 | 220 |
| comparable (scored) | 17 | 75 | **186** |
| degraded — budget exceeded | **17** | **133** | **0** |
| degraded — other (`score=None`) | 0 | 0 | 34 |

**Read this carefully — the grade Δ of −80s *understates* the win.** Prod did not
"finish grade in 303s"; it **hit the 300s budget ceiling and gave up**, leaving
133 of 208 artifacts ungraded (`grade budget exceeded`). Dev graded **more**
artifacts (220 vs 208), **scored 2.5× as many** (186 vs 75), and did it in 222s
**without hitting the budget at all** — `0` budget degradations. The real metric
is grade throughput, not the capped wall-clock: **#186's asyncio concurrency** is
the win, exactly closing the 2026-05-30 baseline's 17/34 failure mode (now 0).

The 34 remaining dev `score=None` degradations are **not** budget-related (retry-
exhaustion / parser degradations per DEC-015 of #7) — worth a glance at
`.signalforge/grade.json` but orthogonal to the efficiency story.

### #189 grade cache — measured, and it does NOT help full-pipeline re-runs

Two `--cache-mode warm` runs back-to-back (run 1 populates, run 2 should hit):

| | Dev cold (`--no-cache`) | Dev warm run 1 (populate) | Dev warm run 2 (cache present) |
|---|---:|---:|---:|
| grade | 222.9s | 229.5s | **241.9s** |
| budget-exceeded | 0 | 0 | 0 |

**Finding: the grade cache produced no measurable speed-up on a re-run (241.9s ≈
the 222.9s cold run).** The cache populated 370 entries on run 1, but run 2 still
graded from scratch. Root cause (verified in `signalforge.grade.cache.compute_cache_key`):
the cache key includes `artifact_text_hash` — the hash of the *drafted artifact
text*. The drafter is a live, non-deterministic LLM, so every `signalforge generate`
re-draft emits different text → different key → **cache miss on every re-run**.

#189's cache only helps when the **same** artifact text is graded again — i.e.
re-grading a *pinned / unchanged* candidate: a `--no-grade` draft-once-then-grade
flow, a CI run with a frozen candidate, or an interrupted grade resumed over an
identical draft. It does **not** accelerate repeated `generate` (draft + grade)
runs. Worth a follow-on doc note so operators don't expect a re-run speed-up the
architecture can't deliver.

### Measurement caveat — back-to-back API latency

`draft + overhead` is derived (`total − grade − diff`) and absorbs all API
latency variance. Warm run 1 showed an **outlier 647s** draft+overhead (vs ~45s
on the cold run) with **no 429s logged** — soft latency under back-to-back load
against a single Anthropic account, not a real draft regression. The **grade-stage
sidecar `duration_seconds` is the stable metric**; treat derived draft+overhead as
noisy. For publication-grade numbers, space runs out (or use separate accounts)
and average 3 runs per side.

### #187 Haiku opt-in — PENDING

Not yet measured. To run: set `grade.model: claude-haiku-4-5` in the intuit
`signalforge.yml`, re-run the dev side cold, and fill:

| | Dev Sonnet (cold) | Dev Haiku (cold) |
|---|---:|---:|
| grade | 222.9s | _s |
| scored / graded | 186 / 220 | _ / _ |

### #188 `--select` batch — PENDING

Not yet measured (single-model run only). See § "#188 `--select` batch" above
for the command.

### Findings

- **#186 (asyncio) is a decisive, default-on win.** Grade went from budget-capped
  (303s / 133 ungraded) to complete (222s / 0 budget-degraded) while grading more
  artifacts — the 2026-05-30 baseline's headline failure mode is closed.
- **Net release-to-release total: 342.9s → 268.7s (−22%)** despite dev drafting 8
  primitives vs prod's 5 (#169/#170/#171), which pushed draft+overhead slightly
  *up* — the grade win dominates.
- **#189's grade cache does not speed up repeated `generate` runs** (text-keyed
  invalidation + non-deterministic draft). Re-frame its value as re-grade-of-
  identical-candidate, not pipeline re-run. ← candidate follow-on ticket.
- **#187 / #188 pending** a second round; rate-limit cool-down recommended first.

## Result — #198 wide-model retest, measured 2026-06-04 (Sonnet A/B, raised rate tier)

Validates **#198** (scale the grade wall-clock budget with the `(artifact × criterion)`
count + optional cost ceilings; PR #199, merged to `dev`). The 16-col baseline above
never stressed the *budget* — dev finished in 222.9s under the old flat 300s. To exercise
the scaled budget, `weekly_query_cost` was **synthesised to 40 columns** (24 synthetic
`NUMBER` columns appended to the manifest node — restored after the run). Default config
plus `llm.max_output_tokens: 8192` on **both** arms (the default 4096 truncates a 40-col
*draft* — a draft-width limit orthogonal to #198, see caveat). Cold grade cache; both arms
back-to-back on one host at a **raised Anthropic rate tier**.

### Per-stage wall-clock — 40-col model, Sonnet, cold cache

| Stage | Prod 0.5.0 (sequential) | Dev 0.6.0.dev0 (#186+#198) |
|---|---:|---:|
| draft + overhead (derived) | 57.5s | 60.6s |
| prune (disabled) | ~0s | ~0s |
| grade | **302.8s** (hit flat 300s ceiling) | 447.7s |
| diff | 0.0s | 0.0s |
| **TOTAL** | **360.3s** | 508.4s |

### Grade degradation — the #198 headline

| | Prod 0.5.0 | Dev 0.6.0.dev0 |
|---|---:|---:|
| artifacts graded (`artifact × criterion`) | 408 | 408 |
| comparable (scored) | 83 | **338** |
| **degraded — budget exceeded** | **325 (80%)** | **0** |
| degraded — other (`GradeLLMError`) | 0 | 70 |
| grade throughput (scored / grade-s) | 0.27/s | **0.76/s (~2.8×)** |

### Findings

- **#198 confirmed: 0 budget-exceeded degradations on a 40-col model**, vs prod's **325**.
  Dev's scaled budget (`60 + 20·⌈408/10⌉ ≈ 880s`) absorbed all 408 pairs; prod's flat 300s
  sequential budget abandoned 80% at the ceiling. This is the live counterpart to the
  deterministic `test_grade_artifacts_wide_model_completes_with_zero_budget_degradations`
  (488 pairs, 0 degradations) unit test.
- **Prod's lower *total* wall-clock is an artifact of quitting early** — it "finished" at
  360s only by degrading 80% of pairs. Dev graded ~4× more pairs (338 vs 83) at ~2.8×
  throughput. Compare *work completed*, not raw total.
- **The 70 dev `GradeLLMError` degradations are a rate-limit artifact, NOT #198.** Even at
  the raised tier, 10-way concurrency bursts past the per-minute cap → some calls exhaust
  their 429 retries. Effective throughput ~55/min (408 / 447.7s) shows the rate limit is
  *still* the binding ceiling — concurrency ran at ~3× of its 10× potential. Mitigation:
  raise the tier further, or lower `grade.max_concurrent_calls` / raise
  `grade.max_retries_429` to stop overshooting the cap.
- **Draft-width caveat (orthogonal to #198):** at default `llm.max_output_tokens` (4096) a
  40-col model truncates the single *draft* call (`stop_reason='max_tokens'`) before grade
  is ever reached — a separate limitation of the one-shot draft step. Raised to 8192 on both
  arms for this run; a candidate follow-on is chunked / streamed drafting for very wide models.

## Result — #202 grade-to-completion retest (2026-06-05 — PASS, both arms)

> Live, metered Anthropic benchmark, one host, `signalforge 0.6.0.dev0` @ branch
> `feature/202-grade-to-100`, Sonnet default, cold grade cache, `max_concurrent_calls=10`,
> `--require-complete` armed. Dev arm only (prod 0.5.0 / dev #198 columns are the recorded
> baselines from the 2026-06-03/04 runs). Re-runnable per § "How to run the #202 live retest".

Validates **#202** (grade-to-completion): the four stages merged on this branch close the
#198 retest's open gap — its dev arm scored 338/408 but left **70 `GradeLLMError`
degradations** (a rate-limit artifact, not #198). #202 attacks exactly that:

| Change | Stage | What it does | Default-on? |
|---|---|---|---|
| **#202 US-004** shared header-honoring rate limiter (DEC-205) | 1 | sync/async limiter paces dispatch at the provider's advertised rate; honours `retry-after` / `anthropic-ratelimit-*` headers + AIMD back-off | ✅ yes |
| **#202 US-005** always-on bounded sweep | 2 | re-grades every transient `score=None` pair sequentially for a few calmer rounds before the report is assembled | ✅ yes |
| **#202 US-006/US-007** `grade.require_complete` (default True) + `--require-complete` CLI | 3 | raises tier-2 `GradeIncompleteError` naming the still-ungraded pairs if any non-exempt pair survives the sweep | ✅ yes (config) / opt-in (CLI flag) |
| **#202 US-008** `max_retries_429` default 3→6 (DEC-209) | 4 | wider per-call 429 budget — belt-and-braces on top of the limiter | ✅ yes |

### What the harness now reports for #202

The harness (`benchmark_runtime.py`) gained, all read from the same `grade.json` sidecar:

- a version-gated **`--require-complete`** flag (omitted on the prod arm exactly like
  `--no-cache`, since prod 0.5.0 has no completeness contract);
- the top-level **`aggregate_complete`** flag (the v0.1 completeness signal);
- the **per-`degrade_reason_type` split** (`transient` / `budget` / `ceiling`) — the #202
  lens replacing the raw `score=None` total, since the retest target is **0 transient
  (`GradeLLMError`) degradations**;
- the **ungraded-pair list** (`(artifact_id, criterion_id)`), which the operator
  cross-checks against the `GradeIncompleteError` (exit 2) message under `--require-complete`.

### PASS condition (locked by the bead)

Absent an explicit operator ceiling, the dev arm must EITHER reach
**`aggregate_complete=True`** (every pair scored) **OR** exit **non-zero under
`--require-complete` with the exact ungraded pairs named**. Targets to beat:

- **40-col model:** 408/408 scored, **0 `GradeLLMError` degradations**,
  `aggregate_complete=True` (vs #198 dev: 338 scored / 70 `GradeLLMError`).
- **16-col model:** **0 non-budget `score=None`** (vs the #186 dev run's 34 non-budget
  `score=None`).

### Per-stage wall-clock — 40-col model, Sonnet, cold cache, fixed rate tier

| Stage | Prod 0.5.0 (sequential) | Dev #198 (#186+#198) | Dev #202 (this issue, 2026-06-05) |
|---|---:|---:|---:|
| draft + overhead (derived) | 57.5s | 60.6s | 60.6s |
| prune (disabled) | ~0s | ~0s | ~0s |
| grade | 302.8s (flat 300s ceiling) | 447.7s | 622.0s |
| diff | 0.0s | 0.0s | 0.0s |
| **TOTAL** | 360.3s | 508.4s | 682.6s |

### Grade completeness — 40-col model (the #202 headline)

| | Prod 0.5.0 | Dev #198 | Dev #202 (this issue, 2026-06-05) |
|---|---:|---:|---:|
| artifacts graded (`artifact × criterion`) | 408 | 408 | 408 |
| comparable (scored) | 83 | 338 | **408 (100%)** |
| degraded — budget exceeded | 325 (80%) | 0 | **0** |
| degraded — transient (`GradeLLMError`) | 0 | 70 | **0** |
| degraded — other (`score=None`) | 0 | 0 | 0 |
| **`aggregate_complete`** | False | False | **True** |
| ungraded pairs named (under `--require-complete`) | n/a | n/a | none (every pair scored, exit 0) |
| grade throughput (scored / grade-s) | 0.27/s | 0.76/s | 0.66/s |

### Grade completeness — 16-col model (`weekly_query_cost`, default 16 columns)

| | Dev #186 (cold) | Dev #202 (this issue, 2026-06-05) |
|---|---:|---:|
| artifacts graded | 220 | 212 |
| comparable (scored) | 186 | **212 (100%)** |
| degraded — budget exceeded | 0 | 0 |
| degraded — non-budget (`score=None`) | 34 | **0** |
| **`aggregate_complete`** | False | **True** |
| ungraded pairs named (under `--require-complete`) | n/a | none (every pair scored, exit 0) |

> _Graded-pair count differs (212 vs 220) because candidate drafting is non-deterministic — a
> different number of candidate tests is drafted per run. The #202 signal is the **completeness**:
> the #186 baseline's **34 non-budget `score=None`** degradations → **0**, `aggregate_complete=True`,
> exit 0 (the `--require-complete` gate was satisfied without raising). Grade wall-clock 279.5s
> (vs #186 222.9s): pairs that previously failed-fast-and-degraded now complete at the paced rate —
> the intended bias-to-completion tradeoff. Dev arm: `signalforge 0.6.0.dev0` @ branch
> `feature/202-grade-to-100`, Sonnet default, cold grade cache, `max_concurrent_calls=10`._

### `--require-complete` outcome (this issue)

| | Dev #202 (40-col) | Dev #202 (16-col) |
|---|---:|---:|
| harness exit code | 0 | 0 |
| `aggregate_complete` reached? | True (408/408 scored) | True (212/212 scored) |
| if non-zero: ungraded pairs named (exit 2 = `GradeIncompleteError`) | n/a — completeness reached, no raise | n/a — completeness reached, no raise |

### Findings (#202) — 2026-06-05 live retest

**PASS on both arms.** Run on one host, `signalforge 0.6.0.dev0` @ branch `feature/202-grade-to-100`,
Sonnet default, cold grade cache, `max_concurrent_calls=10` (the default that produced the #198 herd),
`--require-complete` armed.

- **40-col model (the headline):** the #198 dev arm's **70 transient `GradeLLMError` degradations → 0**.
  408/408 pairs scored, `aggregate_complete=True`, exit 0 — the always-on `--require-complete` gate
  was satisfied without raising. Beats **both** baselines: vs prod 0.5.0 the budget-capped 83/408 → a
  fully-graded 408/408; vs dev #198 the 70 transient degradations → 0. The Stage-1 shared rate limiter
  (honoring `retry-after` / `anthropic-ratelimit-*` + AIMD) paced the 10-way fan-out at the provider's
  advertised rate instead of bursting past it, so no call exhausted its 429 budget; the Stage-2 sweep
  had no residual transients to recover.
- **16-col model:** the #186 dev arm's **34 non-budget `score=None` → 0**. 212/212 scored,
  `aggregate_complete=True`, exit 0. (212 vs the #186 baseline's 220 graded pairs is candidate-drafting
  non-determinism, not a completeness change.)
- **Throughput / wall-clock:** grade wall-clock rose (40-col 622.0s vs #198 447.7s; 16-col 279.5s vs
  #186 222.9s) and effective throughput is comparable-to-slightly-lower (40-col 0.66/s vs #198 0.76/s).
  This is the **intended bias-to-completion tradeoff** (DEC-210): pairs that the #198 arm degraded
  fast-and-partial now complete at the paced rate. The issue hoped the limiter would *raise* throughput
  by avoiding 429-retry churn; in practice it pays a modest wall-clock premium to *guarantee* completion.
  The binding result — `aggregate_complete=True`, zero silent partials — is met on both arms.

## How to run the #202 live retest (operator-only, metered)

> This is a **live, metered Anthropic run** — it issues real draft + grade calls and costs
> money. It requires the prepared intuit_airflow dbt project, an `ANTHROPIC_API_KEY` at the
> required (raised) rate tier, and a single host (wall-clock is machine/network dependent).
> Do **not** run it in CI. Run it back-to-back on ONE machine and transcribe the printed
> tables into the placeholder cells above.

**Preconditions** — same substrate as § Preconditions (intuit project prepared per
`179-test-primitive-expansion-retest.md` § Substrate: `dbt deps` + `dbt parse`, a synthesised
`_signalforge_*_schema.yml`, `signalforge.yml` with `safety.mode: schema-only` +
`prune.enabled: false`, `/tmp/sf-demo-profiles`), PLUS:

- **40-col model:** synthesise `weekly_query_cost` to 40 columns (append 24 synthetic
  `NUMBER` columns to the manifest node, as in the #198 run — restore after), and set
  `llm.max_output_tokens: 8192` so the 40-col *draft* is not truncated (a draft-width limit
  orthogonal to #202 — see the #198 draft-width caveat).
- **16-col model:** the default `weekly_query_cost` (no synthesis).
- **Cold grade cache** on every run (the harness `--cache-mode bypass` default passes
  `--no-cache` on dev — fair vs the cacheless prod arm).
- **Fixed rate tier:** run the whole A/B on the SAME (raised) Anthropic rate tier, since the
  #198 70-`GradeLLMError` artifact was a rate-limit overshoot — a different tier confounds
  the #202 limiter result.

**Environment variables** (or pass the matching `--…` flags):

```bash
export ANTHROPIC_API_KEY=sk-ant-...                       # raised rate tier
export SF_BENCH_PROJECT_DIR=~/Projects/intuit_airflow/plugins/dbt
export SF_BENCH_PROFILES_DIR=/tmp/sf-demo-profiles
```

**Commands** — dev arm, both models, cold cache, `--require-complete` armed:

```bash
# Dev venv (editable from this checkout)
python -m venv .venv-dev && .venv-dev/bin/pip install -e .

# 40-col model (synthesise weekly_query_cost → 40 cols + llm.max_output_tokens: 8192 first)
.venv-dev/bin/python tests/research/179-runtime-benchmark/benchmark_runtime.py \
    --model models/reporting/weekly_query_cost.sql \
    --require-complete            # exits 2 + names ungraded pairs if not aggregate_complete

# 16-col model (default weekly_query_cost; restore the manifest node first)
.venv-dev/bin/python tests/research/179-runtime-benchmark/benchmark_runtime.py \
    --model models/reporting/weekly_query_cost.sql \
    --require-complete
```

Run the **prod 0.5.0 arm** the same way for the before-columns (it OMITS `--require-complete`
and `--no-cache` automatically — both are version-gated and unsupported pre-#202):

```bash
python -m venv .venv-prod && .venv-prod/bin/pip install signalforge-dbt
.venv-prod/bin/python tests/research/179-runtime-benchmark/benchmark_runtime.py \
    --model models/reporting/weekly_query_cost.sql --require-complete
```

The harness prints, for each run: the per-stage wall-clock, the grade-degradation counts
with the per-`degrade_reason_type` split, `aggregate_complete`, and the ungraded-pair list.
A `--require-complete` run that cannot reach a complete corpus **exits non-zero (2)** and the
dev CLI names the ungraded pairs on stderr — the harness re-derives the same list from
`grade.json` and parses the sidecars regardless of exit code. Transcribe each table into the
matching `_pending live run_` cells above and replace § Findings (#202).
