# Super Plan — #198: Grade budget: scale wall-clock with work + optional cost ceiling

## Meta
- **Ticket:** https://github.com/wjduenow/SignalForge/issues/198
- **Phase:** complete
- **Branch:** feature/198-grade-budget-scaling (worktree: .claude/worktrees/198-grade-budget-scaling)
- **Base:** origin/dev @ 4ad19ef (TBD — see Q3)
- **Sessions:** 1 (2026-06-03)

## Problem (from ticket)
`GradeConfig.total_budget_seconds` defaults to a flat **300s** wall-clock ceiling on the whole grade
stage. It is mis-calibrated three ways: (1) docstring sizes it for "60 calls × 1s p50" but a real
16-col model produces 208–228 `(artifact × criterion)` pairs; (2) sized for the pre-#186 sequential
era — post-#186 concurrent dispatch (`asyncio.TaskGroup` + `Semaphore(max_concurrent_calls)`) outruns
it but the default was never recalibrated; (3) a fixed wall-clock doesn't scale with model width.

### Part A — scale wall-clock with work
`effective_budget = budget_base_seconds + budget_per_pair_seconds × ceil(num_pairs / max_concurrent_calls)`.
New `GradeConfig` fields: `budget_base_seconds: int = 60`, `budget_per_pair_seconds: float`,
`total_budget_seconds: int | None = None` (reinterpreted as optional absolute hard ceiling;
effective = `min(scaled, total_budget_seconds)` when both apply). Degrade contract (DEC-015) unchanged.

### Part B — optional opt-in cost ceilings (all default None = off)
`max_grade_calls: int | None`, `max_grade_cost_usd: float | None`, `max_grade_tokens: int | None`.
Whichever ceiling trips first stops dispatch; remaining pairs degrade with a reason naming the ceiling.
Cost via `signalforge.llm.pricing.lookup(model)`.

## Discovery findings (Session 1)

### Code map (verified on the worktree)
- **`src/signalforge/grade/config.py`** — `GradeConfig` (frozen, `extra="forbid"`).
  - `total_budget_seconds: int = 300` (L176) + docstring (the stale "60 calls × 1s" sizing).
  - `max_concurrent_calls: int = 10` (L183, validated `[1,100]`).
  - `@field_validator("max_output_tokens","total_budget_seconds") _positive` (L327) — rejects ≤0;
    must learn to skip `None` once `total_budget_seconds` becomes `int | None`.
  - `@model_validator(mode="before") _resolve_model_default` (#187 sentinel pattern, L278).
  - `_GradeConfigFile` wrapper `extra="ignore"` (L475).
  - **No CLI flag** exists for `max_concurrent_calls` or `total_budget_seconds` (config-file-only).
- **`src/signalforge/grade/engine.py`** — `_grade_artifacts_async_core`:
  - `pairs = list(_iterate_artifacts(...))`, `total_pairs = len(pairs)` (L600–601) — the count is
    known BEFORE dispatch. This is where `effective_budget` will be computed.
  - `semaphore = asyncio.Semaphore(resolved_config.max_concurrent_calls)` (L626).
  - `total_budget_seconds = resolved_config.total_budget_seconds` (L651) — single read site.
  - `async with asyncio.timeout(total_budget_seconds): async with asyncio.TaskGroup() as tg:` (L831) —
    ALL pairs created as tasks at once; semaphore bounds concurrency.
  - `_one(index, artifact_id, artifact_text, criterion)` (L654) — per-pair coroutine; LLM call inside;
    `result.input_tokens / output_tokens / cache_*` available after the call (L420–423).
  - Synthesis pass (L870–893) fills un-completed slots with
    `reasoning=f"grade budget exceeded ({total_budget_seconds}s) before evaluation"`.
  - Budget WARNING (L902) — locked field set `{run_id, model_unique_id, completed_count,
    degraded_count, total_budget_seconds}` (pinned by `test_grade_artifacts_concurrent_budget_warning_shape_locked`).
  - `_build_degraded(... reasoning=...)` (L452) — constructs `(GradingResult score=None, GradeEvent)`,
    zero tokens. `_format_degrade_reasoning(exc)` (L428) is for LLM-layer failures (not budget).
- **`src/signalforge/llm/pricing.py`** — `ModelPricing(input_per_mtok, output_per_mtok,
  cache_write_5m_per_mtok, cache_read_per_mtok)`; `PRICES` (read-only); `lookup(model) -> ModelPricing`
  (raises `EstimateUnknownModelError`). `PRICE_TABLE_VERSION="2026-05-28"`.
- **`src/signalforge/llm/cost/_rollup.py::_compute_record_usd`** (L258) — the exact USD formula
  `(in·in_mtok + out·out_mtok + cc·cw_mtok + cr·cr_mtok)/1e6`. Private; a public cost helper can be
  hoisted/shared for the ceiling.
- **`src/signalforge/grade/models.py`** — `GradingResult.score: float|None`, `reasoning`;
  `GradingReport.aggregate_complete` (computed: True iff every score non-None); `pass_rate`/`mean_score`
  over scored subset only. `GradeEvent.audit_schema_version: int = 2` (token fields present).
  **No full-config-hash on GradeEvent** → adding GradeConfig fields perturbs NO audit hash.
- **`src/signalforge/grade/errors.py::GradeBudgetExceededError`** — defined but RESERVED (never raised;
  v0.1 degrades). The ticket keeps degrade semantics → it stays reserved (no graduation, no new errors).

### Convention constraints (from .claude/rules/)
- `grade-layer.md`: DEC-015 conservative degrade (never silent drop; `aggregate_complete` signals
  partial). `extra="forbid"` on `GradeConfig` + `field_validator`s. New fields under `grade:` namespace.
  Read-back drift detectors (GradingResult/Report/Event) only matter if GradeEvent shape changes (it
  won't). `GradeBudgetExceededError` reserved — keep reserved. #187 frozen-config default-from-sibling
  resolves in `mode="before"`.
- `cli-layer.md`: new typed errors → tier-3 + exit-code table + 7th AST scan. **But ceilings degrade,
  not raise → no new typed errors → no exit-code/AST churn.** New numeric config fields are
  signalforge.yml-only (precedent: `max_concurrent_calls`, `total_budget_seconds`) → no CLI flag, no
  5-surface parity (unless we choose to add a flag — see Q2).
- `testing-signal.md`: no `assert True`; pin the computed formula with a unit test; fail-loud on absent
  pricing key (degrade with named reason rather than crash).
- `prune-engine.md` §5-surface parity: only if `GradeBudgetExceededError` graduates (it won't here).

### Critical calibration finding
The ticket's proposed `budget_per_pair_seconds = 2.0` **contradicts the ticket's own baseline**: 220
pairs at concurrency 10 took **222.9s**, i.e. ~10s effective per concurrency-wave (Sonnet judge p50
~10s/call). `60 + 2.0·ceil(220/10) = 104s` would trip at 104s and degrade ~half the pairs — recreating
the failure. The default must be grounded in the ~10s/call reality (~15–20s/pair for ~1.5–2× headroom).
See Q1.

### Benchmark-harness coordination finding
`tests/research/179-runtime-benchmark/benchmark_runtime.py` + `docs/research/179-runtime-benchmark.md`
live ONLY on `feature/179-runtime-benchmark` (5 commits, no open PR). The harness drives the CLI against
an **external local repo** `~/Projects/intuit_airflow/plugins/dbt` with a live Anthropic key → the
benchmark-rerun AC is a **maintainer-only manual step**, not a Ralph bead. But the harness is not on
`dev` (current base). See Q3.

## Scoping questions — see session log below

## Session 1 decisions (2026-06-03)

- **DEC-001 — Scaled wall-clock formula (Part A).** Add `budget_base_seconds: int = 60` and
  `budget_per_pair_seconds: float = 20.0`; reinterpret `total_budget_seconds: int | None = None`.
  `effective_budget = budget_base_seconds + budget_per_pair_seconds × ceil(num_pairs / max_concurrent_calls)`;
  when `total_budget_seconds` is set, `effective = min(scaled, total_budget_seconds)`.
  *Rationale (Q1):* baseline shows ~10s/call (220 pairs @ c=10 → 222.9s); `per_pair=20.0` gives a
  500s backstop on the baseline (~2.25× headroom) — a true runaway guard that tolerates 429 retry
  storms, not a completion constraint. The ticket-literal `2.0` would compute 104s and degrade ~half
  the pairs. `total_budget_seconds: <int>` preserves exact v0.1 absolute-cap semantics for pinned configs.

- **DEC-002 — Ceilings degrade, never raise.** `max_grade_calls / max_grade_cost_usd / max_grade_tokens`
  (all `| None = None`, opt-in). A tripped ceiling routes un-started pairs through the existing
  `_build_degraded` path with a `reasoning` string naming the ceiling. **No new typed errors** →
  no exit-code-table / 7th-AST-scan churn; `GradeBudgetExceededError` stays reserved. Mirrors the
  DEC-015 conservative-degrade contract and prune-engine's conservative-bias routing template.

- **DEC-003 — Soft/best-effort ceiling semantics (Q4).** All pairs are dispatched into the one
  `TaskGroup`; cost/tokens are known only post-call, so the cost/token ceiling stops *un-started*
  pairs (bounded overshoot ≤ `max_concurrent_calls − 1` in-flight calls). `max_grade_calls` is made
  near-hard by reserving a slot (increment a shared counter) BEFORE the LLM call inside `_one`. Single
  event loop → check-and-increment is race-free as long as no `await` separates read and decision.
  Cache-hit pairs (#189, prefilled before the async core) make no LLM call → never count against any
  ceiling. Documented overshoot in `docs/grade-ops.md`.

- **DEC-004 — signalforge.yml-only, no CLI flags (Q2).** New fields live under the `grade:` namespace,
  `extra="forbid"` + `field_validator`s, matching `max_concurrent_calls`/`total_budget_seconds`.
  No CLI flags, no 5-surface parity. Runtime-override flags deferred to a follow-up if requested.

- **DEC-005 — Base-branch sequencing (Q3).** Merge `feature/179-runtime-benchmark` to `dev` first
  (open its PR, land it), then rebase the #198 worktree onto updated `dev`, so the benchmark harness
  + `docs/research/179-runtime-benchmark.md` are present for the maintainer rerun AC. Implementation +
  unit tests do not depend on the harness; only the final benchmark step does.

- **DEC-006 — No `audit_schema_version` bump.** GradeEvent shape is unchanged; degrade reasons ride the
  existing free-text `reasoning` field; there is no full-config-hash on GradeEvent, so new GradeConfig
  fields perturb no reproducibility hash. Read-back drift detectors (GradingResult/Report/Event) stay
  valid unchanged. (If refinement decides to add a structured "tripped ceiling" enum to GradeEvent, the
  bump + drift-fixture refresh come back in scope — currently out of scope.)

## Phase 2 — Architecture review (Session 1)

| Area | Rating | Finding |
|---|---|---|
| Concurrency / race-freedom | **pass** | Single event loop. `max_grade_calls` slot-reservation is race-free if the increment sits immediately after `async with semaphore:` and BEFORE the first `await` (the LLM call). Cost/tokens soft-accumulator (update post-call, check pre-call) is coherent under the single-threaded loop. |
| Double-audit risk | **pass** | A ceiling-degrade inside `_one` sets `results_by_index[index]` (non-None) → the post-TaskGroup synthesis pass (L874 `if … is not None: continue`) skips it. No double GradeEvent. Use an **unshielded** audit-write for the pre-call ceiling degrade (no in-flight LLM await to race); keep the shielded write only for post-LLM degrades. |
| Counters / locked WARNING shape | **concern → Q5** | Ceiling-degrades that resolve inside `_one` should count as `completed` (preserves `completed + degraded == total_pairs`; keeps the synthesis-pass `degraded` semantics for wall-clock-only). The locked budget WARNING field set is pinned by `test_grade_artifacts_concurrent_budget_warning_shape_locked` — its `total_budget_seconds` field now carries the *computed effective* value (rename vs keep — Q5). Ceiling trips need their own operator signal (Q5). |
| Config migration | **pass** | `_positive` validator (config.py L327) currently covers `max_output_tokens` + `total_budget_seconds`; split so `total_budget_seconds` allows `None`. `test_grade_config_defaults_match_dec_023_to_027` (test_config.py L230) asserts `total_budget_seconds == 300` → change to `is None` + add 5 new default asserts. Fixtures `example_config.yml` (300) / austin `signalforge.yml` (600) keep working (explicit ints). No StrictGradeConfig (config-shaped). |
| `asyncio.timeout(None)` edge | **pass** | `_compute_effective_budget` always returns a finite int (`int(scaled)` when `total_budget_seconds is None`, else `int(min(scaled, total))`), so the timeout site never receives `None`. |
| Testing strategy | **pass** | Pure `_compute_effective_budget(base, per_pair, total, num_pairs, concurrency) -> int` unit-tested directly (no asyncio). Ceiling tests mirror `_config_tiny_budget` + a fake returning known `input_tokens`/`output_tokens`; cost tests MUST use a real SKU (`claude-sonnet-4-6`/`claude-haiku-4-5`) since `claude-fake` is absent from `PRICES`. `_make_candidate_with_n_columns(40)` drives the ≥40-col "0 degradations" AC deterministically (fake client, no live API). |

### Exact touch-points (verified)
- `grade/config.py`: field L176 (`total_budget_seconds`), `_positive` validator L327, defaults test surface.
- `grade/engine.py`: L600-601 (`total_pairs`), L651 (single read → compute effective), L831 (`asyncio.timeout`), L882 (degrade reason), L902-914 (WARNING), `_one` L654 (slot-reservation site, just inside the semaphore).
- `grade/cost`: reuse the `_compute_record_usd` formula (`_rollup.py` L258) for the cost ceiling.
- Tests: `tests/grade/test_config.py` L230 + L660, `tests/grade/test_engine.py` (`_config_tiny_budget` L433, warning-shape L1745), `tests/grade/_fake.py::expect_grade_responses` (token params).
- Docs/rules: `docs/grade-ops.md` (L126/145/168 primary), `.claude/rules/grade-layer.md` L131-133 (DEC-029 grade: key enumeration — add 5 keys).

## Phase 3 — Refinement decisions (Session 1)

- **DEC-007 — New `grade ceiling exceeded` WARNING (Q5=A).** When any opt-in ceiling
  (`max_grade_calls`/`max_grade_cost_usd`/`max_grade_tokens`) trips, the engine emits ONE end-of-run
  stderr WARNING, distinct from the wall-clock budget WARNING. Locked field set:
  `{run_id, model_unique_id, ceiling, limit, completed_count, degraded_count}` (`ceiling` ∈
  `{"calls","cost_usd","tokens"}`). Lazy-format JSON (`_LOGGER.warning("grade ceiling exceeded: %s",
  json.dumps({...}))`) per the ANSI-safe logger grep gate. New pinned-shape test.

- **DEC-008 — Rename the budget WARNING field `total_budget_seconds` → `effective_budget_seconds`
  (Q6=A).** It now carries the computed effective budget actually passed to `asyncio.timeout`. Update
  `test_grade_artifacts_concurrent_budget_warning_shape_locked` to the new field name in lockstep.

- **DEC-009 — Degrade-reason strings (locked verbatim).** Wall-clock (synthesis pass, value now
  effective): `f"grade budget exceeded ({effective_budget_seconds}s) before evaluation"` (unchanged
  wording). Ceilings: `f"grade call ceiling exceeded ({max_grade_calls} calls)"`,
  `f"grade cost ceiling exceeded (${max_grade_cost_usd})"`,
  `f"grade token ceiling exceeded ({max_grade_tokens} tokens)"`. Pinned by tests.

- **DEC-010 — Accounting.** Cost = full USD incl. cache (reuse the `_compute_record_usd` formula:
  `(in·in_mtok + out·out_mtok + cc·cw_mtok + cr·cr_mtok)/1e6`) via `lookup(config.model)`. Tokens =
  `input + output + cache_creation + cache_read` (all token movement). Calls = LLM calls only (cache
  hits make no call → never counted). `_compute_effective_budget` is computed on `total_pairs`
  (includes any prefilled cache-hit slots — conservative over-budget, harmless for a backstop).

- **DEC-011 — Ceiling-degrades count as `completed`.** A ceiling-degrade resolves INSIDE `_one`
  (sets the slot, unshielded audit-write) so it counts in `counters["completed"]` — preserving the
  `completed + degraded == total_pairs` invariant and reserving `degraded` for the wall-clock
  synthesis pass. Cache-hit pairs (prefilled, #189) bypass `_one` entirely.

## Phase 4 — Detailed breakdown (stories)

> Validation command (every story's final AC):
> `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

### US-001 — GradeConfig: budget + ceiling fields + validator split
**Description.** Add the Part-A/Part-B fields to `GradeConfig` and split the positivity validator so
`total_budget_seconds` accepts `None`.
**Traces to:** DEC-001, DEC-002, DEC-004, DEC-006.
**Files:** `src/signalforge/grade/config.py`; `tests/grade/test_config.py`.
**Changes:**
- `total_budget_seconds: int = 300` → `int | None = None` (docstring rewritten: now an *optional absolute
  hard ceiling*; `None` → use scaled formula; effective = `min(scaled, total_budget_seconds)`).
- Add `budget_base_seconds: int = 60`, `budget_per_pair_seconds: float = 20.0` (documented: per-wave wall
  allowance; default grounded in ~10s/call baseline × ~2.25× headroom).
- Add `max_grade_calls: int | None = None`, `max_grade_cost_usd: float | None = None`,
  `max_grade_tokens: int | None = None` (opt-in; documented as soft ceilings, cost via `signalforge.llm.pricing`).
- Split `_positive`: keep `max_output_tokens`; add positivity for `budget_base_seconds` /
  `budget_per_pair_seconds`; add allow-None-or-positive validators for `total_budget_seconds` and the three
  `max_grade_*`.
**TDD:**
- `test_grade_config_defaults_*`: change `total_budget_seconds == 300` → `is None`; add asserts for the 5 new
  defaults (`budget_base_seconds == 60`, `budget_per_pair_seconds == 20.0`, three `max_grade_* is None`).
- positivity: `budget_base_seconds=0` / `budget_per_pair_seconds=0.0` / `total_budget_seconds=0` raise; the three
  `max_grade_*=0` raise; all `*=None` (where optional) pass.
- `extra="forbid"` still rejects a typo (e.g. `max_grade_cal:`).
**Done When:** new fields present with documented defaults; validators behave per TDD; `extra="forbid"`
intact; existing fixtures (`example_config.yml`=300, austin=600) still load; validation command passes.
**Depends on:** none.

### US-002 — `_compute_effective_budget` pure helper + unit tests
**Description.** Add a pure module-level helper in `grade/engine.py` computing the scaled budget; unit-test in
isolation (no asyncio).
**Traces to:** DEC-001, DEC-010.
**Files:** `src/signalforge/grade/engine.py`; `tests/grade/test_engine.py`.
**Changes:** `def _compute_effective_budget(*, budget_base_seconds, budget_per_pair_seconds, total_budget_seconds,
num_pairs, max_concurrent_calls) -> int:` → `scaled = base + per_pair*math.ceil(num_pairs/concurrency)`;
return `int(scaled)` when `total_budget_seconds is None`, else `int(min(scaled, total_budget_seconds))`.
`num_pairs == 0` → returns `base` (guard the ceil divide-by-… is fine; concurrency ≥ 1 by validator).
**TDD (pin the formula):**
- 220 pairs, c=10, base=60, per_pair=20.0, total=None → **500**.
- same with total=300 → **300** (cap wins).
- same with total=900 → **500** (scaled wins).
- num_pairs=1, c=10 → 60 + 20·1 = 80.
- num_pairs=0 → 60.
- non-multiple: 221 pairs, c=10 → ceil(22.1)=23 → 60+460=520.
**Done When:** helper returns a finite int for all inputs incl. `total=None`; formula tests pass; validation passes.
**Depends on:** US-001.

### US-003 — Engine: wire scaled budget + rename WARNING field + ≥40-col AC test
**Description.** Use `_compute_effective_budget` at the timeout site; thread the effective value into the degrade
reason; rename the budget WARNING field; add the width AC test.
**Traces to:** DEC-001, DEC-008, DEC-009.
**Files:** `src/signalforge/grade/engine.py`; `tests/grade/test_engine.py`.
**Changes:**
- Replace L651 read: compute `effective_budget = _compute_effective_budget(... num_pairs=total_pairs,
  max_concurrent_calls=resolved_config.max_concurrent_calls)`.
- `asyncio.timeout(effective_budget)` (L831); synthesis-pass reason uses `effective_budget` (L882, DEC-009 wording).
- WARNING field rename `total_budget_seconds` → `effective_budget_seconds` carrying `effective_budget` (L911).
**TDD:**
- Update `test_grade_artifacts_concurrent_budget_warning_shape_locked` to the renamed field; assert the value
  equals the computed effective budget for the tiny-budget config.
- **AC (≥40-col, 0 width-induced degradations under default config):** `_make_candidate_with_n_columns(40)` +
  default `GradeConfig` + fast fake client → `report.aggregate_complete is True`, zero `score is None` results,
  no budget WARNING emitted.
**Done When:** the timeout uses the scaled budget; WARNING field renamed + test green; ≥40-col AC test green;
validation passes.
**Depends on:** US-002.

### US-004 — Engine: cost/calls/tokens ceilings (degrade path) + ceiling WARNING
**Description.** Add the three opt-in ceilings to the `_one` dispatch with soft/best-effort semantics, degrading
un-started pairs and emitting one end-of-run ceiling WARNING.
**Traces to:** DEC-002, DEC-003, DEC-007, DEC-009, DEC-010, DEC-011.
**Files:** `src/signalforge/grade/engine.py`; `tests/grade/test_engine.py`; (maybe) `tests/grade/_fake.py`.
**Changes:**
- Closure accumulators: `calls_made`, `cost_usd`, `tokens` (+ a `tripped: {"ceiling": str|None, "limit": ...}` cell).
- In `_one`, immediately after `async with semaphore:` and BEFORE the LLM `await`: if a ceiling is configured and
  already met/exceeded → build degraded (DEC-009 reason), **unshielded** audit-write, set slot, `completed += 1`,
  record `tripped`, return. `max_grade_calls`: reserve a slot (increment) pre-call, no `await` between check and
  increment (DEC-003). After a successful call: add usage to `cost_usd` (via `lookup(config.model)` USD formula) and
  `tokens`, increment `calls_made` if not pre-reserved.
- End-of-run: if `tripped["ceiling"]` set, emit the DEC-007 `grade ceiling exceeded` WARNING (locked shape).
- Cache-hit (prefilled) pairs never enter `_one` → excluded from all ceilings (DEC-010).
**TDD (each ceiling degrades; never silent drop):**
- `max_grade_calls=K`: exactly ~K pairs scored, rest degraded with `"grade call ceiling exceeded (K calls)"`,
  `aggregate_complete False`, one ceiling WARNING `ceiling="calls"`.
- `max_grade_cost_usd`: **real SKU** (`claude-sonnet-4-6`), fake returns known `input/output_tokens`; cap chosen so
  ~N pairs fit; degraded reason `"grade cost ceiling exceeded ($…)"`, WARNING `ceiling="cost_usd"`.
- `max_grade_tokens`: known per-call tokens, cap → degrade `"grade token ceiling exceeded (… tokens)"`,
  WARNING `ceiling="tokens"`.
- ceiling WARNING shape pinned (`{run_id, model_unique_id, ceiling, limit, completed_count, degraded_count}`).
- cache-hit pairs don't count against a tiny `max_grade_calls` (prefilled slots bypass the ceiling).
**Done When:** all three ceilings degrade un-started pairs with the locked reasons; one ceiling WARNING per tripped
run; `aggregate_complete` reflects partial; no double-audit (slot set inside `_one`); validation passes.
**Depends on:** US-003.

### US-005 — Docs + example fixture parity (worker-writable surfaces)
**Description.** Update `docs/grade-ops.md` and the example config fixture for the new budget model + ceilings.
**Traces to:** DEC-001, DEC-002, DEC-004, DEC-007, DEC-008, DEC-009.
**Files:** `docs/grade-ops.md`; `tests/fixtures/grade/example_config.yml` (+ its round-trip test if fields added).
**Changes:**
- `docs/grade-ops.md`: rewrite the `total_budget_seconds` field doc (L168 area) → optional absolute cap; document
  `budget_base_seconds`/`budget_per_pair_seconds` + the formula + the ~2.25× headroom rationale; document the three
  ceilings + soft/best-effort overshoot (≤ `max_concurrent_calls − 1`); update the budget-WARNING field name; add the
  new ceiling WARNING; refresh the per-provider cost-guidance mentions.
- `example_config.yml`: add the new keys as explicit/commented examples; keep round-trip test green (update it if keys
  are added to the asserted set).
**Done When:** docs describe the scaled budget + ceilings accurately; example fixture round-trips; validation passes.
**Depends on:** US-004.
**Note:** `.claude/rules/grade-layer.md` (DEC-029 key enumeration + taxonomy note) is **orchestrator-writable only**
(Ralph workers can't write `.claude/`), so that edit lives in the Patterns & Memory story.

### US-098 — Quality Gate (code review ×4 + CodeRabbit)
**Description.** Run the code reviewer 4× across the full changeset, fixing all real bugs each pass; run CodeRabbit if
available. Validation must pass after fixes.
**Depends on:** US-005 (all implementation complete).

### US-099 — Patterns & Memory (priority 99, orchestrator-run)
**Description.** Update `.claude/rules/grade-layer.md` (DEC-029 grade: key enumeration — add `budget_base_seconds`,
`budget_per_pair_seconds`, `max_grade_calls`, `max_grade_cost_usd`, `max_grade_tokens`; note the scaled-budget +
ceiling degrade as part of the DEC-015 taxonomy; note the renamed `effective_budget_seconds` WARNING field + the new
`grade ceiling exceeded` WARNING). Record any reusable pattern (e.g. "scale a wall-clock backstop with work × not
flat"; "opt-in ceilings degrade, never raise → no exit-code/AST churn") in memory.
**Depends on:** US-098.

## Maintainer-only steps (NOT Ralph beads — live API / external repo / `.claude` writes)
- **M-1 (prerequisite, DEC-005).** Open the PR for `feature/179-runtime-benchmark` → merge to `dev`, then
  rebase the #198 worktree onto updated `dev` so `tests/research/179-runtime-benchmark/` +
  `docs/research/179-runtime-benchmark.md` are present.
- **M-2 (closing AC).** Re-run `tests/research/179-runtime-benchmark/benchmark_runtime.py` against
  `~/Projects/intuit_airflow/plugins/dbt` for `weekly_query_cost` AND a ≥40-col model (live Anthropic key,
  cold grade cache, `prune.enabled: false`). Record the new per-stage numbers in
  `docs/research/179-runtime-benchmark.md` and confirm **0 width-induced budget degradations** on the wide model.

## Beads manifest (devolved 2026-06-04)
- **Epic:** `SignalForge-xfg`
- **Tasks (dependency chain US-001 → … → Patterns & Memory):**
  - `SignalForge-xfg.1` — US-001 GradeConfig fields + validator split (ready)
  - `SignalForge-xfg.2` — US-002 `_compute_effective_budget` helper + formula tests (← xfg.1)
  - `SignalForge-xfg.3` — US-003 engine wire scaled budget + WARNING rename + ≥40-col AC (← xfg.2)
  - `SignalForge-xfg.4` — US-004 cost/calls/tokens ceilings + ceiling WARNING (← xfg.3)
  - `SignalForge-xfg.5` — US-005 docs/grade-ops.md + example fixture parity (← xfg.4)
  - `SignalForge-xfg.6` — Quality Gate ×4 + CodeRabbit (← xfg.5)
  - `SignalForge-xfg.7` — Patterns & Memory incl. grade-layer.md DEC-029 (← xfg.6)
- **Worktree:** `.claude/worktrees/198-grade-budget-scaling` (branch `feature/198-grade-budget-scaling`, base `dev`).
- **PR:** #199 (draft → ready on devolve). Maintainer-only M-2 benchmark rerun is the closing AC.

## Status: Complete (2026-06-04)

All 7 beads (epic `SignalForge-xfg`) landed via `/ralph-run`; epic auto-closed. Full suite green
(3592 passed). Implementation commits `581b19a..ce14143` on `feature/198-grade-budget-scaling`.

- **PR:** #199
- **Quality Gate finding (real bug, fixed):** the cost ceiling looked up `pricing.lookup` per-pair
  inside the `TaskGroup`; a prefix-valid-but-unpriced SKU + `max_grade_cost_usd` raised
  `EstimateUnknownModelError` mid-run (uncaught by the per-pair `except`), aborting after billable
  calls. Fixed by resolving pricing once up front (fail-fast at entry) + regression test
  (`ce14143`/`ddc3ca6`). Lesson recorded in `grade-layer.md` + bd memory.
- **Compounding update:** `.claude/rules/grade-layer.md` (scaled-budget + ceilings contract, DEC-029
  enumeration), `docs/grade-ops.md`, bd memory `grade-runtime-budgets-198-scale-a-wall-clock`.
- **Maintainer-only remaining (M-2 closing AC):** re-run `tests/research/179-runtime-benchmark/benchmark_runtime.py`
  on `weekly_query_cost` + a ≥40-col model (live key), record numbers in `docs/research/179-runtime-benchmark.md`.
  The deterministic ≥40-col "0 width-induced degradations" AC already passes
  (`test_grade_artifacts_wide_model_completes_with_zero_budget_degradations`).
