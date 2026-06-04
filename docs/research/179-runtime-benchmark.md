# Issue #179 — runtime benchmark after efficiency improvements (#186 + #187 + #188 + #198)

**Status:** First measurement complete (2026-06-03) — prod 0.5.0 vs dev
0.6.0.dev0 Sonnet A/B + #189 cache, single model. #187 Haiku and #188 batch
dimensions still pending. See § Result. The harness is re-runnable per the
commands below.

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
