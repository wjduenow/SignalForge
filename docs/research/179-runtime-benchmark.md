# Issue #179 — runtime benchmark after efficiency improvements (#186 + #187 + #188)

**Status:** SKELETON — harness drafted, measurement not yet run. The script,
sidecar parsing, degraded-grade counting, and table output are complete and
runnable; the measurement requires a maintainer with a live `ANTHROPIC_API_KEY`
and the prepared local intuit_airflow repo (below).

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

## Result (maintainer-filled)

> Fill these after running both venvs. Delete this blockquote when done.

**Intuit project:** `~/Projects/intuit_airflow/plugins/dbt` — **model:** _(target)_
**Machine:** _(host, network)_ — **Date:** _(YYYY-MM-DD)_
**Prod version:** _(e.g. 0.5.0)_ — **Dev version:** _(0.6.0.dev0)_

### Per-stage wall-clock — Sonnet default (captures #186)

| Stage | Before (prod PyPI) | After (`dev`) | Δ | Δ % |
|---|---:|---:|---:|---:|
| draft + overhead (derived) | _s | _s | _s | _% |
| prune (disabled) | ~0s | ~0s | — | — |
| grade | _s | _s | _s | _% |
| diff | _s | _s | _s | _% |
| **TOTAL** | **_s** | **_s** | **_s** | **_%** |

### Grade stage — Haiku opt-in (`grade.model: claude-haiku-4-5`, adds #187)

| | Before (prod PyPI) | After (`dev`, Haiku) |
|---|---:|---:|
| grade | _s | _s |

### Grade degradation (the headline correctness signal)

| | Baseline 2026-05-30 | After (`dev`, Sonnet) | After (`dev`, Haiku) |
|---|---:|---:|---:|
| artifacts graded | 34 | _ | _ |
| comparable (scored) | 17 | _ | _ |
| degraded — budget exceeded | **17** | _ | _ |

### #188 batch amortisation (`--select` vs. shell-loop)

| | Before (prod PyPI) | After (`dev`) |
|---|---:|---:|
| `--select` total wall-clock | _s | _s |
| shell-loop total wall-clock | _s | _s |

### Findings

- _(grade-stage reduction attributable to #186 + #187; whether budget-exceeded
  reached 0; whether #188 produced a measurable per-model amortisation; the
  primitive-count conflation's effect on draft time; any follow-on tickets.)_
