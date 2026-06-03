# Issue #179 — runtime benchmark after efficiency improvements (#186 + #187 + #188)

**Status:** SKELETON — harness drafted, measurement not yet run. The per-stage
timing scaffold, degraded-grade counting, and table output are complete and
runnable against an inlined substrate model. The real measurement requires (a)
swapping the substrate to the intuit_airflow slice and (b) the two-checkout A/B
below, run by a maintainer with a live `ANTHROPIC_API_KEY`.

Companion to the "Runtime benchmark retest" story on epic
[#179](https://github.com/wjduenow/SignalForge/issues/179). The harness lives at
`tests/research/179-runtime-benchmark/`.

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

## Harness

`tests/research/179-runtime-benchmark/`:

- `test_runtime_benchmark.py` — gated by the `anthropic` marker (deselected from
  default CI). Times draft → prune → grade → diff per stage, counts grade
  degradations (total + budget-exceeded subset), prints a table.
- `_substrate.py` — deterministic inputs: a representative inlined model, a
  schema-only `SafetyPolicy`, shipped-default configs, a do-nothing adapter, an
  empty `PruneResult` (prune disabled).

### Run command

```bash
# -s is required: the per-stage table is PRINTED, not asserted.
pytest -m anthropic --no-cov -s \
    tests/research/179-runtime-benchmark/test_runtime_benchmark.py
```

Levers (environment variables):

- `SF_BENCH_GRADE_MODEL=claude-haiku-4-5` — time the **#187** opt-in fast-grade
  path. Unset → shipped anthropic default (Sonnet), which captures **#186** only.

## Protocol

### The before/after A/B is a TWO-checkout protocol

Wall-clock is machine- and network-dependent, so the only honest A/B re-runs the
OLD revision on the **same machine** rather than comparing against the preserved
2026-05-30 sidecars.

1. **Before:** `git checkout 90af28b` (the `#179` empirical-retest-writeup
   commit — last commit before #186). Copy `tests/research/179-runtime-benchmark/`
   onto that checkout (it uses only long-stable public APIs), run the harness,
   record the per-stage table.
2. **After:** `git checkout dev` (currently `d280fc6`), run again, record.
3. Diff the tables. The grade-stage delta is the #186 (and, with
   `SF_BENCH_GRADE_MODEL`, #187) win.

### Substrate: skeleton vs. real measurement

The skeleton ships ONE inlined model so the harness runs without intuit_airflow.
The real measurement swaps `_substrate.build_models()` to the
`weekly_query_cost.sql` baseline target + the 10-model retest slice from
`~/Projects/intuit_airflow/plugins/dbt` (synthesise each model's columns per
[`179-test-primitive-expansion-retest.md` § Substrate](179-test-primitive-expansion-retest.md),
then `manifest.load` and select the slice). The timing scaffold iterates whatever
`build_models()` returns — no other change needed.

### Prune stays disabled

The baseline ran `prune.enabled: false` (no Snowflake auth). The harness mirrors
that — the prune stage does no warehouse work, its timing reads ~0. Keep it
disabled for the apples-to-apples grade-stage A/B; lifting it (real Snowflake)
changes what is being measured.

### #188 `--select` batch — measured separately

#188 amortises the shared cached prefix across models in ONE
`signalforge generate --select` process. That is a CLI / multi-process concern,
not an in-process orchestrator call, so it is NOT timed by the pytest harness.
Measure it on the real slice on both revisions:

```bash
time signalforge generate --select 'path:models/reporting/*' \
    --project-dir <intuit>/plugins/dbt --profiles-dir /tmp/sf-demo-profiles
```

against the equivalent shell-loop (one process per model), and record the total
batch wall-clock + per-model `[i/N]` timings below.

## Result (maintainer-filled)

> Fill these after running both checkouts. Delete this blockquote when done.

**Substrate used:** _(inlined skeleton model / intuit_airflow N-model slice)_
**Machine:** _(host, network)_ — **Date:** _(YYYY-MM-DD)_

### Per-stage wall-clock — Sonnet default (captures #186)

| Stage | Before (`90af28b`) | After (`dev`) | Δ | Δ % |
|---|---:|---:|---:|---:|
| draft | _s | _s | _s | _% |
| prune (disabled) | ~0s | ~0s | — | — |
| grade | _s | _s | _s | _% |
| diff | _s | _s | _s | _% |
| **TOTAL** | **_s** | **_s** | **_s** | **_%** |

### Per-stage wall-clock — Haiku opt-in (`SF_BENCH_GRADE_MODEL=claude-haiku-4-5`, adds #187)

| Stage | Before (`90af28b`) | After (`dev`) | Δ | Δ % |
|---|---:|---:|---:|---:|
| grade | _s | _s | _s | _% |
| **TOTAL** | **_s** | **_s** | **_s** | **_%** |

### Grade degradation (the headline correctness signal)

| | Baseline 2026-05-30 | After (`dev`, Sonnet) | After (`dev`, Haiku) |
|---|---:|---:|---:|
| comparable (scored) | 17 of 34 | _ | _ |
| degraded — budget exceeded | **17** | _ | _ |

### #188 batch amortisation (`--select` vs. shell-loop)

| | Before (`90af28b`) | After (`dev`) |
|---|---:|---:|
| `--select` total wall-clock | _s | _s |
| shell-loop total wall-clock | _s | _s |

### Findings

- _(grade-stage reduction attributable to #186 + #187; whether budget-exceeded
  reached 0; whether #188 produced a measurable per-model amortisation; any
  follow-on tickets for remaining degradations.)_
