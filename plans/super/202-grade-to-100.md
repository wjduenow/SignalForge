# Super Plan — #202: Grade to 100% (rate-limit-aware throttle + degraded-pair sweep + require-complete)

## Meta
- **Ticket:** [#202](https://github.com/wjduenow/SignalForge/issues/202)
- **Phase:** discovery
- **Branch / worktree:** `feature/202-grade-to-100` @ `../worktrees/SignalForge/202-grade-to-100`
- **Base branch:** `origin/dev` (NOT `main` — all prior seams #186/#198/#189 live on `dev`; 35 commits ahead of `main`)
- **Sessions:** 1 (2026-06-04)

---

## Discovery

### What / Why / Who
- **What:** Drive the grade stage to **100% scored** — or **fail loud** with the exact ungraded pairs named — for every run that did not opt into an explicit operator ceiling.
- **Why:** A partial grade run (`aggregate_complete=False`) is a defect unless the operator deliberately traded completeness away (a cost/time ceiling). The #179 wide-model retest proved the remaining gap is **self-inflicted**: 10-way concurrency bursts past the per-minute rate cap → thundering herd of 429s → each call exhausts its 3×429 retries on blind backoff → degrade. 40-col dev arm left **70/408** pairs as `GradeLLMError`; 16-col left **34**.
- **Who:** Operators running `signalforge generate` (grade stage) and CI gates that must never green-light a half-graded model.

### Acceptance criteria (from ticket)
1. **Stage 1** — LLM retry seam honors `retry-after` / `anthropic-ratelimit-*` headers + a **shared** rate limiter across concurrent grade calls (adaptive concurrency). Unit-tested with a fake 429+`retry-after` burst that previously exhausted retries.
2. **Stage 2** — bounded degraded-pair sweep re-grades `score=None` **non-ceiling** pairs after the main pass; unit test drives N transient failures that succeed on sweep → report reaches 100%.
3. **Stage 3** — `grade.require_complete` / `--require-complete` fails loud (non-zero exit, named pairs) when a non-ceiling pair remains ungraded after recovery; ceiling-degrades do NOT trip it. 5-surface parity for the flag.
4. **Stage 4** — `grade-ops.md` + `grade-layer.md` distinguish transient-recoverable vs operator-ceiling vs unrecoverable; "partial is acceptable" paths scoped to explicit ceilings only. Raise `grade.max_retries_429` default.
5. Conventions honored: `extra="forbid"`, validators, drift detectors + fixtures, AST scans, logger grep gate, exit-code taxonomy.
6. **Full retest** — re-run `benchmark_runtime.py` on the 40-col + 16-col models, cold grade cache, fixed rate tier; beat both prod and last-dev baselines (see table below). PASS only if dev arm reaches `aggregate_complete=True` OR exits non-zero under `--require-complete` with named pairs.

### Baseline metrics to beat (`docs/research/179-runtime-benchmark.md`)
**40-col model (2026-06-04):**

| Metric | Prod 0.5.0 | Dev (#198) | Target #202 |
|---|---:|---:|---:|
| pairs | 408 | 408 | 408 |
| scored | 83 | 338 | **408 (100%)** |
| budget-exceeded degradations | 325 | 0 | **0** |
| `GradeLLMError` degradations | 0 | 70 | **0** |
| `aggregate_complete` | False | False | **True** |
| grade throughput | 0.27/s | 0.76/s | raise further |

**16-col model (2026-06-03):** dev had 34 non-budget `score=None` → target **0** (or fail-loud under `--require-complete`).

### Codebase map (grounded in `origin/dev`)

**Stage-1 seam — LLM client retry path**
- `src/signalforge/llm/client.py`
  - `_backoff_warn()` (line 243) — blind `(2**total_attempts) * _rand_uniform(0.75, 1.25)`; emits the per-attempt WARNING. Used by BOTH sync + async loops.
  - `call_llm` (line 274) — sync drafter path; 429 retry branch ~line 462.
  - `call_llm_async` (line 673) — **the grade path**; 429 retry branch lines 807–824. Identical blind backoff; **ignores `retry-after` / `anthropic-ratelimit-*` headers; no shared limiter** — each coroutine retries independently. This is the thundering-herd source.
  - Module aliases `_sleep` / `_rand_uniform` / `_async_sleep` (lines 71–73) — test override seam.
- `src/signalforge/llm/providers.py` — `LLMProvider.classify_exception() → ExceptionCategory{AUTH,RATE_LIMIT,SERVER_ERROR,CONNECTION,NO_RETRY}`. `AnthropicProvider` (~line 302). Headers reachable via `RateLimitError.response.headers` (429) and the SDK response object (success); OpenAI/Gemini expose different/none → limiter must degrade gracefully.
- `src/signalforge/llm/_anthropic_client.py` — SDK shim (DEC-012 confinement).

**Stage-2/3 seam — grade engine**
- `src/signalforge/grade/engine.py`
  - `_grade_artifacts_async_core` (line 595) — `asyncio.TaskGroup` + `asyncio.Semaphore(max_concurrent_calls)` (line 680) wrapped in `asyncio.timeout(effective_budget)`.
  - `_one(...)` (line 760) — per-pair coroutine. Ceiling checks (lines 785–838, locked reason strings), happy path (847+), per-pair degrade on `GradeLLMError`/`GradeOutputError`/`GradePromptEnvelopeBreachError` (line 862) via `_build_degraded`.
  - `_build_degraded` (line 505) — `GradingResult(score=None, passed=False, ...)` (DEC-015).
  - `_format_degrade_reasoning` (line ~448) — transient reason = `"call failed: GradeLLMError"` (issue #158 collapse).
  - **Degrade-reason discriminators** (how the sweep tells transient from ceiling):
    - transient (sweep target): `"call failed: ..."`
    - budget: `"grade budget exceeded ({N}s) before evaluation"`
    - ceilings: `"grade call ceiling exceeded (...)"` / `"grade cost ceiling exceeded (...)"` / `"grade token ceiling exceeded (...)"`
  - `grade_artifacts` (public sync entry, line 1178) — wraps the async core in `asyncio.run` (line 1515), builds `GradingReport` (line 1536), `write_grading_report` (line 1548), then the **`fail_on_below_threshold` raise-after-sidecar block (line 1585)** — the exact precedent for a `require_complete` check.
- `src/signalforge/grade/config.py` — `GradeConfig` (`extra="forbid"`). Knobs: `max_retries_429=3` (155), `max_concurrent_calls=10` (258, bounded [1,100]), `total_budget_seconds=None`+`budget_base_seconds=60`/`budget_per_pair_seconds=20.0` (scaled-budget #198), soft ceilings `max_grade_calls`/`max_grade_cost_usd`/`max_grade_tokens` (227/237/248), `fail_on_below_threshold=False` (305), `cache_enabled=True` (327). New `require_complete` lands here.
- `src/signalforge/grade/cache.py` — content-addressed `.signalforge/grade-cache/<16hex>.json`; 5-part key; degraded results **never** cached → a sweep reuses every prior success and only re-touches failures (#189; cheap-sweep property).

**Stage-3 seam — CLI + errors**
- `src/signalforge/cli/generate.py` — `add_parser` / `cmd_generate`; `--min-score` override pattern via `model_validate()`. `--require-complete` flag lands here.
- `src/signalforge/grade/errors.py` — 10-class taxonomy. `GradeBelowThresholdError` (tier-2, raise-after-sidecar precedent). `GradeBudgetExceededError` reserved/never-raised. Exit-code table `_EXCEPTION_TO_EXIT_CODE` + AST scan 7 (every `*Error` must register).
- `src/signalforge/skills/signalforge/SKILL.md` — 6th parity surface for CLI flags; `tests/cli/test_skill_cli_parity.py` gate.

**Retest harness**
- `tests/research/179-runtime-benchmark/benchmark_runtime.py` + `docs/research/179-runtime-benchmark.md` (both on `dev`).

### Conventions that gate every story (from `.claude/rules/`)
- **Validation command:** `uv run pytest` (default markers; `--cov-fail-under=80`). Gated markers: `bigquery`, `anthropic`, `e2e`, `cli_subprocess`, `wheel_smoke`, `openai`, `gemini`.
- **5-surface parity (CLI flag):** argparse help · handler docstring · `docs/cli-ops.md` · parity test · DEC in `plans/super/9-cli-entrypoint.md`. **+6th:** `SKILL.md` + `test_skill_cli_parity.py`.
- **Exit-code taxonomy (4-tier, immutable wording):** 0 ok · 1 load/parse · 2 input-validation/post-call invariant · 3 external/API/audit-durability. New `*Error` must register in `_EXCEPTION_TO_EXIT_CODE` (AST scan 7) and walk `__mro__`.
- **Degrade 5-surface parity (grade-layer.md):** exit-code · remediation text · JSONL `reasoning=` · sidecar field · CLI test assertion.
- **Drift detectors:** read-back models (`GradingResult`/`GradingReport`/`GradeEvent`, `extra="ignore"`) paired with `Strict<X>(extra="forbid")` mirrors validated against committed fixtures (`tests/fixtures/grade/*`).
- **AST scans (3 bypass patterns: bare / alias / module-attr):** `GradeEvent` only built in `audit._build_grade_event`; new errors registered; planted-violation self-check on every new gate.
- **Logger grep gate:** all `_LOGGER.{info,warning,...}` use lazy `json.dumps()`, never f-strings; AST-based visitor over `src/signalforge/{llm,draft,prune,grade,diff,cli}`.
- **Config models:** `extra="forbid"`; file wrapper `_GradeConfigFile` `extra="ignore"`; validators raise typed errors (not bare `ValidationError`).
- No `workflow-project.md` exists.

---

## Phase 1 scoping decisions (answered)

| # | Decision | Resolution |
|---|----------|------------|
| Q1 | `require_complete` default | **`true`** — fail-loud by default. Ceiling-degrades never trip it. Raises the bar on Stage-1+2 reliability (a surviving transient now exits non-zero by default). |
| Q2 | Incomplete error model | **New `GradeIncompleteError` (tier-2)** — names the exact ungraded pairs; registers in `_EXCEPTION_TO_EXIT_CODE`; raise-after-sidecar like `GradeBelowThresholdError`. |
| Q3 | Stage-1 limiter scope | **Both sync + async paths** — header-aware backoff + shared limiter serve `call_llm` and `call_llm_async`. Needs a limiter abstraction usable from a thread (sync) and an event loop (async). |
| Q4 | Sweep activation | **Always-on backstop** — bounded by round cap + cool-down; no-op when no transient failures; #189 cache makes re-touch cheap. |

**Implications carried into architecture review:**
- Q1=`true`: the retest's normal-operation pass *requires* `aggregate_complete=True` (or an explicit ceiling); Stage-1+2 must be reliable enough that the default doesn't false-trip on ordinary transients.
- Q3=both: a single limiter must expose a sync `acquire`/`release` (threading primitives) AND an async `acquire`/`release` (asyncio primitives), OR two cooperating limiters sharing the header-derived budget state. Flagged as a design concern below.

---

## Architecture Review (Phase 2)

| Area | Rating | Key finding |
|------|--------|-------------|
| **Transient/ceiling classification** | **blocker** | Sweep + require_complete must distinguish transient (`"call failed:"`) from budget/ceiling degrades. Today the only signal is the **reason string** — a prefix-match is a drift hazard (a future reason-string tweak silently breaks the sweep). Fix: a structured `degrade_reason_type` discriminator field (→ DEC-203, Q5). |
| **Budget-trip vs explicit ceiling semantics** | **blocker** | The issue names "a tight `total_budget_seconds`" as a *legitimate* operator ceiling, but the **default scaled** budget (when `total_budget_seconds is None`) is a runaway guard, not an operator choice. Which trips require_complete? (→ DEC-204, Q6). |
| **CLI config-clobber w/ default-true** | **blocker→resolved** | `require_complete` defaults `true` in both config and CLI. A naive `store_true` default would clobber an explicit `grade.require_complete: false` in `signalforge.yml`. Fix: `BooleanOptionalAction` + `default=None` sentinel, override only when flag explicitly passed (mirrors `--min-score`/`--scope`). → DEC-208. |
| **Sync+async limiter dual surface** | concern→resolved | A single object straddling `threading` + `asyncio` is a foot-gun. Two cooperating limiters (`SyncRateLimiter` / `AsyncRateLimiter`) sharing a header-derived `RateLimitBudget` snapshot; async limiter threaded via `ContextVar`; provider seam `extract_rate_limit_info()` returns a neutral budget; OpenAI/Gemini → empty (graceful). → DEC-205. |
| **Header access vs SDK confinement (DEC-012)** | pass | `RateLimitError.response.headers` reachable; vendor parsing stays inside `AnthropicProvider`, returns neutral `RateLimitBudget`. No vendor type leaks the provider boundary. |
| **Limiter × Semaphore × scaled budget** | pass | Limiter paces *when to send*; `Semaphore(max_concurrent_calls)` caps *how many*; `asyncio.timeout(effective_budget)` is the backstop. Limiter waits count against the budget — correct. AIMD dials an effective concurrency ≤ the semaphore. |
| **aggregate_complete post-sweep** | concern→resolved | The report is built at `engine.py:1536` from the async-core results. The sweep must run **before** the report is assembled so `aggregate_complete`/`pass_rate`/`mean_score` reflect post-recovery state. Fold the sweep into the async entry (one `asyncio.run`). → DEC-206. |
| **GradeIncompleteError wiring** | concern→resolved | New tier-2 error; register in `_EXCEPTION_TO_EXIT_CODE` (AST scan 7); raise **after** sidecar write, **before** `fail_on_below_threshold` (line 1585). Carries named pairs truncated to ~20 + "N more" (stderr stays bounded; full list in JSONL). → DEC-207. |
| **Drift detectors / fixtures / schema** | concern | The new `degrade_reason_type` field bumps `audit_schema_version` 2→3; update `tests/fixtures/grade/*`, the `Strict*` mirrors, and keep a v2 replay anchor. → DEC-203. |
| **Sweep audit forensics** | pass | Append a NEW audit record per swept pair (immutable-log posture); optional `sweep_round: int|None` field for forensic queries; sidecar reflects final post-sweep result. |
| **Observability** | pass | New log lines (limiter dial events, sweep round start/end with counts, incomplete failure) use lazy `json.dumps` per the grep gate; no f-strings; no sensitive data. |
| **Testing / parity / coverage** | pass | All gates have established precedents (drift detector, AST scan 7 + planted-violation, skill-parity, logger grep gate). `uv run pytest --cov-fail-under=80`; limiter/sweep/require_complete are unit-testable with the fake-client + `_async_sleep`/clock-injection seams; live retest is a gated/manual step. |

**No remaining blockers once DEC-203/204 are decided** (Q5/Q6 below). All other items have concrete resolutions captured as DEC-205…208.

## Refinement Log (Phase 3)

### Decisions

**DEC-203 — Structured degrade discriminator (Q5=A).**
Add `degrade_reason_type: Literal["transient","budget","ceiling"] | None` to `GradingResult` and `GradeEvent` (`None` for scored pairs). Set once in `_build_degraded` by mapping the reason it's handed: `"call failed: …"`→`transient`, `"grade budget exceeded …"`→`budget`, `"grade … ceiling exceeded …"`→`ceiling`. The sweep (DEC-206) and require_complete (DEC-204) branch on the enum, never the string. Bump `audit_schema_version` 2→3; refresh `tests/fixtures/grade/*`, add `StrictGradeEventV3`, keep the v2 fixture as a replay anchor.
*Rationale:* the rules favor structured discriminators over prefix-matching; this is the load-bearing classification for both new stages and must not be a drift hazard.

**DEC-204 — require_complete trip semantics (Q6=A).**
`require_complete` trips (raises `GradeIncompleteError`) on any `score=None` pair where:
- `degrade_reason_type == "transient"` — **always trips**; OR
- `degrade_reason_type == "budget"` **AND** `config.total_budget_seconds is None` — a default-scaled-budget trip is a Stage-1-failure canary → **trips**.
Exempt (never trip): `degrade_reason_type == "ceiling"` (explicit `max_grade_*` knobs) and `"budget"` when `total_budget_seconds` was **explicitly set** (a deliberate operator time-ceiling).
*Rationale:* the ticket names "a tight `total_budget_seconds`" as a legitimate opt-in ceiling, but the default scaled guard is not an operator choice.

**DEC-205 — Stage-1 shared rate limiter (Q3=both paths).**
New module `src/signalforge/llm/_rate_limiter.py`:
- `RateLimitBudget` — frozen, neutral value object (requests/tokens remaining + reset, `retry_after`).
- Two cooperating limiters sharing one budget snapshot: `SyncRateLimiter` (threading, for `call_llm`) and `AsyncRateLimiter` (asyncio, for `call_llm_async`, shared across the grade `TaskGroup` via a `ContextVar`).
- AIMD adaptive concurrency: multiplicative-decrease on 429, additive-increase on headroom; effective concurrency bounded `[1, max_concurrent_calls]`.
- Provider seam `extract_rate_limit_info(exc, *, response=None) -> RateLimitBudget` honoring `retry-after` + `anthropic-ratelimit-*`; `AnthropicProvider` parses (SDK confined per DEC-012); OpenAI/Gemini return an **empty** budget → graceful fallback to blind backoff.
- Backoff honors `retry_after`/reset headers when present; falls back to `(2**attempt)*_rand_uniform(...)` otherwise. The `_backoff_warn` WARNING shape is preserved.
- Test seams: `FakeRateLimitError(headers=...)`, injectable clock, existing `_sleep`/`_async_sleep` overrides.

**DEC-206 — Stage-2 bounded sweep.**
Folded into the async entry so the `GradingReport` is built **post-sweep**. After the main pass + a short cool-down, collect `score=None` pairs with `degrade_reason_type=="transient"`, re-grade them **sequentially** (concurrency 1 — no second herd) via `_grade_one_async`; loop until zero remain or `grade.sweep_max_rounds` (default 3). Reuses the #189 cache: prior successes are cached/never re-called, failures are re-touched and written to cache on success. Each swept attempt appends a NEW audit record carrying `sweep_round: int|None`. `aggregate_complete`/`pass_rate`/`mean_score` recompute from post-sweep results. New config: `sweep_max_rounds: int = 3`, `sweep_cooldown_seconds: float = 2.0` (`0` disables the wait; sweep itself is always-on per Q4).

**DEC-207 — `GradeIncompleteError` (tier-2).**
New error in `grade/errors.py` (parent `GradeError`), fields: `incomplete_pairs: tuple[tuple[str,str],...]` (artifact_id, criterion_id), `require_complete: bool`, `aggregate_complete: bool`. Message truncates to the first 20 pairs + "… and N more" (full list lives in the JSONL audit). Registered in `_EXCEPTION_TO_EXIT_CODE` at **tier 2** (AST scan 7 + planted-violation). Raised **after** the sidecar write + INFO log, and **before** the existing `fail_on_below_threshold` check (incomplete is structural; below-threshold is verdictual).

**DEC-208 — CLI flag + config-override.**
`grade.require_complete: bool = True` on `GradeConfig` (`extra="forbid"`). CLI flag `--require-complete / --no-require-complete` via `argparse.BooleanOptionalAction` with `default=None` sentinel; override applied as `GradeConfig.model_validate({**dump, "require_complete": override})` **only when the sentinel is not None**, so an explicit `grade.require_complete: false` in `signalforge.yml` is never clobbered by the CLI default. 6-surface parity (argparse help · handler docstring · `docs/cli-ops.md` · parity test · DEC in `plans/super/9-cli-entrypoint.md` · `SKILL.md` + skill-parity test).

**DEC-210 — Library biases toward completion; budget is an intentional opt-in (user feedback, 2026-06-04).**
The default grade posture drives **every pair to a score**. A `<100%` grade result is legitimate **only when the operator intentionally limited cost/time** — an explicit `max_grade_*` ceiling or an explicitly-set `total_budget_seconds`. Incompleteness must never be a *passive by-product* of a default guard.
- The scaled wall-clock budget survives **only as a generous runaway guard** (catches genuine hangs / pathological artifacts), never as a throughput cap that passively degrades pairs. With Stage-1 (limiter paces at the rate limit) + Stage-2 (sweep recovers transients), a normal run completes well inside the guard (the #179 retest: ~448s grade vs an ~880s scaled guard — already non-binding).
- A default-scaled-budget trip (`total_budget_seconds is None`) is therefore **not** a legitimate partial — it fails loud (DEC-204), surfacing a Stage-1 regression rather than silently shipping a partial.
- **Re-tune for headroom:** confirm `budget_base_seconds` / `budget_per_pair_seconds` defaults leave ample margin over the limiter-paced wall-clock so the guard cannot passively bind at expected scales; widen if the retest (US-009) shows otherwise. Do **not** lower the default budget.
- Docs (`grade-ops.md`) state the bias-to-completion posture plainly and show exactly how to opt into a limit (the three `max_grade_*` knobs + an explicit `total_budget_seconds`), so limiting is a deliberate, documented act.
*Rationale:* completion is the product's job; a partial must be a choice the operator made on purpose, not something the defaults did to them.

**DEC-209 — Stage-4 defaults + docs.**
Raise `grade.max_retries_429` default `3 → 6` (belt-and-braces; the limiter is the primary fix). Docs (`docs/grade-ops.md`, `docs/cli-ops.md`) + rules (`.claude/rules/grade-layer.md`, `.claude/rules/llm-drafter.md`) distinguish **transient-recoverable** vs **operator-ceiling** vs **unrecoverable**, scope every "partial is acceptable" path to explicit ceilings only, and document the concurrency↔rate-limit relationship that caused the 70.

### Session notes
- Worktree re-based onto `origin/dev` (Phase 1) — non-negotiable; the seams don't exist on `main`.
- All four scoping answers + both refinement answers took the recommended-or-stricter option; the two non-default scoping picks (require_complete default-true; limiter on both paths) raise reliability/surface and are reflected in US-003/US-006 scope.

---

## Detailed Breakdown (Phase 4)

Architecture order: models/schema → llm-layer (limiter) → grade engine (sweep) → errors/config/engine (require_complete) → CLI → docs/defaults → retest → quality gate → patterns. Validation command for every story: **`uv run pytest`** (`--cov-fail-under=80`).

### US-001 — Degrade discriminator field + schema bump
**Description:** Add the structured `degrade_reason_type` discriminator to `GradingResult` + `GradeEvent`, set in `_build_degraded`, and bump the audit schema. This is the foundation both new stages classify on.
**Traces to:** DEC-203.
**Files:** `src/signalforge/grade/models.py` (+field on both models, `None` for scored), `src/signalforge/grade/engine.py` (`_build_degraded` maps reason→type), `src/signalforge/grade/audit.py` (`audit_schema_version` 2→3), `tests/fixtures/grade/*` (refresh + v2 replay anchor), `tests/grade/test_drift_detector.py` (`StrictGradeEventV3`).
**TDD:** `_build_degraded` sets `transient`/`budget`/`ceiling` for each reason string; scored result → `None`; drift detector passes against refreshed fixtures; v2 replay still validates.
**Done When:**
- [ ] `degrade_reason_type` present on `GradingResult` + `GradeEvent`, set for all three degrade causes + `None` when scored
- [ ] `audit_schema_version` bumped to 3; v2 replay anchor retained
- [ ] Fixtures + Strict mirrors refreshed; drift detector green
- [ ] `uv run pytest` passes
**Depends on:** none.

### US-002 — `RateLimitBudget` + provider `extract_rate_limit_info` seam
**Description:** Create the neutral `RateLimitBudget` value object and the provider header-extraction seam, honoring `retry-after` + `anthropic-ratelimit-*` inside the Anthropic provider; OpenAI/Gemini degrade to empty.
**Traces to:** DEC-205.
**Files:** `src/signalforge/llm/_rate_limiter.py` (new — `RateLimitBudget` + parse helper), `src/signalforge/llm/providers.py` (abstract `extract_rate_limit_info`; Anthropic impl; OpenAI/Gemini empty), `tests/llm/_fake.py` (`FakeRateLimitError(headers=...)`), `tests/llm/test_rate_limiter.py` (new).
**TDD:** Anthropic parses each header (incl. malformed → field `None`); 429 exception headers read via `.response.headers`; OpenAI/Gemini return all-`None`; no vendor type leaks past the provider boundary (DEC-012).
**Done When:**
- [ ] `RateLimitBudget` + `extract_rate_limit_info` implemented; Anthropic populated, others empty
- [ ] Header parsing handles present/absent/malformed values without raising
- [ ] `uv run pytest` passes
**Depends on:** none.

### US-003 — Sync + async limiters + client wiring (Stage-1 acceptance)
**Description:** Implement `SyncRateLimiter`/`AsyncRateLimiter` sharing a budget snapshot with AIMD concurrency, and wire them into the `call_llm` and `call_llm_async` 429 branches so retries pace at the rate limit (honor `retry-after`/reset) instead of bursting. Thread the async limiter via a `ContextVar`.
**Traces to:** DEC-205.
**Files:** `src/signalforge/llm/_rate_limiter.py` (limiter classes + clock seam), `src/signalforge/llm/client.py` (429 branches sync ~462 + async 807-824; ContextVar; fallback to blind backoff when budget empty), `tests/llm/test_client_retries.py`, `tests/llm/test_rate_limiter.py`.
**TDD (Stage-1 acceptance criterion):** a fake-client **burst returning 429+`retry-after`** that previously exhausted the 3×429 budget now does NOT exhaust (limiter waits per `retry-after`); AIMD decreases concurrency on 429 and increases on headroom; sync happy-path timing unchanged (limiter idle when no 429).
**Done When:**
- [ ] Burst-that-previously-exhausted test passes (no retry-exhaustion)
- [ ] AIMD decrease/increase unit-tested; effective concurrency bounded `[1, max_concurrent_calls]`
- [ ] Existing drafter/grade retry tests still pass (no happy-path regression)
- [ ] New log lines lazy `json.dumps`; logger grep gate green
- [ ] `uv run pytest` passes
**Depends on:** US-002.

### US-004 — Share the limiter across the grade async core
**Description:** Instantiate the shared limiter in `grade_artifacts`, set the `ContextVar`, and seed initial concurrency from `max_concurrent_calls` so every `TaskGroup` coroutine paces against one budget. Confirm interaction with the `asyncio.timeout(effective_budget)` backstop.
**Traces to:** DEC-205.
**Files:** `src/signalforge/grade/engine.py` (`grade_artifacts` setup + `_grade_artifacts_async_core`), `tests/grade/test_engine.py`.
**TDD:** a concurrent grade burst (fake client → 429+`retry-after` under load) that previously left N `GradeLLMError` degradations now reaches **0**; budget-timeout path still degrades correctly when genuinely over budget.
**Done When:**
- [ ] Concurrent-burst grade test reaches 0 transient degradations
- [ ] Budget-timeout behavior unchanged for genuine over-budget runs
- [ ] `uv run pytest` passes
**Depends on:** US-003.

### US-005 — Bounded degraded-pair sweep (Stage 2)
**Description:** Fold an always-on, bounded sweep into the async entry: after the main pass + cool-down, re-grade `transient` `score=None` pairs sequentially until zero remain or `sweep_max_rounds`, then build the report from post-sweep results.
**Traces to:** DEC-206 (uses DEC-203 discriminator).
**Files:** `src/signalforge/grade/engine.py` (sweep loop before report build; recompute aggregates), `src/signalforge/grade/config.py` (`sweep_max_rounds=3`, `sweep_cooldown_seconds=2.0` + validators), `src/signalforge/grade/models.py` (`sweep_round: int|None` on `GradeEvent`), `tests/grade/test_engine.py`, `tests/grade/test_config.py`, fixtures.
**TDD:** N transient failures that succeed on sweep → `aggregate_complete=True`, 100% scored; `sweep_max_rounds` cap honored (still-failing pairs remain `score=None`); ceiling/budget pairs are NOT swept; swept pairs append a `sweep_round` audit record; cache reused (no re-call of already-scored pairs).
**Done When:**
- [ ] Transient-recovery test reaches 100% scored; round cap honored
- [ ] Ceiling/budget pairs excluded from the sweep
- [ ] Report aggregates reflect post-sweep state; sidecar carries final results
- [ ] `sweep_round` field + fixtures/drift updated; logger grep gate green
- [ ] `uv run pytest` passes
**Depends on:** US-001, US-004.

### US-006 — `GradeIncompleteError` + `require_complete` config + engine check (Stage 3 core)
**Description:** Add the tier-2 `GradeIncompleteError`, the `grade.require_complete` config field (default `true`), and the post-sidecar engine check that raises it for unrecovered non-exempt pairs per DEC-204.
**Traces to:** DEC-204, DEC-207.
**Files:** `src/signalforge/grade/errors.py` (new error + remediation), `src/signalforge/cli/_helpers.py` (register tier-2 in `_EXCEPTION_TO_EXIT_CODE`), `src/signalforge/grade/config.py` (`require_complete: bool = True` + validator), `src/signalforge/grade/engine.py` (check after sidecar, before `fail_on_below_threshold`), `tests/grade/test_engine.py`, `tests/grade/test_errors.py`, `tests/cli/test_exit_codes.py`, the AST-scan/planted-violation test.
**TDD:** trips on transient; trips on default-scaled-budget (`total_budget_seconds is None`); does NOT trip on `ceiling`; does NOT trip on explicit `total_budget_seconds` budget; raises **after** sidecar write; names pairs (truncated >20); maps to exit code 2; AST scan 7 catches a deliberately-unregistered planted error.
**Done When:**
- [ ] `GradeIncompleteError` registered tier-2; AST scan 7 + planted-violation green
- [ ] All four trip/exempt cases covered by tests
- [ ] Raise ordering: after sidecar + INFO, before `fail_on_below_threshold`
- [ ] `uv run pytest` passes
**Depends on:** US-001, US-005.

### US-007 — CLI `--require-complete` flag + 6-surface parity (Stage 3 CLI)
**Description:** Wire the `--require-complete/--no-require-complete` flag with the no-clobber override pattern and bring every parity surface into agreement.
**Traces to:** DEC-208.
**Files:** `src/signalforge/cli/generate.py` (argparse `BooleanOptionalAction` default=None; override via `model_validate` only when not None; handler docstring), `docs/cli-ops.md` (flag + exit-code + stderr), `src/signalforge/skills/signalforge/SKILL.md`, `plans/super/9-cli-entrypoint.md` (DEC), `tests/cli/test_generate.py`, `tests/cli/test_skill_cli_parity.py`.
**TDD:** `--require-complete` → override `True`; `--no-require-complete` → `False`; flag absent → file `grade.require_complete: false` preserved (no clobber); end-to-end transient-incomplete run exits 2 with named pairs in stderr; skill-parity passes.
**Done When:**
- [ ] Flag present on live CLI; all 6 parity surfaces agree; parity + skill-parity tests green
- [ ] No-clobber override verified (file config preserved when flag absent)
- [ ] End-to-end exit-2 + named-pairs stderr test passes
- [ ] `uv run pytest` passes
**Depends on:** US-006.

### US-008 — Stage-4 defaults + docs/rules (incl. bias-to-completion posture)
**Description:** Raise the `max_retries_429` default, assert/re-tune the default budget so it can't passively bind, and bring docs/rules into line with the bias-to-completion posture and the transient/ceiling/unrecoverable taxonomy.
**Traces to:** DEC-209, DEC-210.
**Files:** `src/signalforge/grade/config.py` (`max_retries_429: int = 6`; budget defaults left non-binding, widened only if US-009 shows otherwise), `docs/grade-ops.md`, `docs/cli-ops.md`, `.claude/rules/grade-layer.md`, `.claude/rules/llm-drafter.md`, `tests/grade/test_config.py`.
**Done When:**
- [ ] `max_retries_429` default raised; config test updated
- [ ] `grade-ops.md` states the **bias-to-completion** posture plainly: the library drives to 100% by default; a `<100%` result is legitimate **only** via an intentional `max_grade_*` ceiling or an explicitly-set `total_budget_seconds`
- [ ] `grade-ops.md` shows exactly how to opt into a limit (the knobs), and documents that a default-scaled-budget trip fails loud (never a silent partial)
- [ ] Test asserts the default scaled budget leaves headroom over expected limiter-paced wall-clock at representative scales (non-binding by default)
- [ ] `grade-layer.md` distinguishes transient-recoverable / operator-ceiling / unrecoverable; "partial is acceptable" scoped to explicit, operator-chosen limits only
- [ ] `llm-drafter.md` § retry taxonomy documents the limiter seam
- [ ] `uv run pytest` passes
**Depends on:** US-007.

### US-009 — Full retest + benchmark writeup (gated/manual)
**Description:** Re-run `benchmark_runtime.py` on the 40-col + 16-col models, cold grade cache, fixed rate tier, and record results that beat both baselines (or fail-loud with named pairs). Prepare any harness wiring the new flags require; the live run is operator-executed (needs `ANTHROPIC_API_KEY` + target project + host).
**Traces to:** acceptance criterion #6.
**Files:** `tests/research/179-runtime-benchmark/benchmark_runtime.py` (wire `--require-complete` / new knobs if needed), `docs/research/179-runtime-benchmark.md` (record the #202 column).
**Done When:**
- [ ] Harness supports the #202 run (dry-run/fake validated under `uv run pytest`)
- [ ] Live retest recorded: 40-col → 408/408 scored, 0 `GradeLLMError`, `aggregate_complete=True` (or non-zero under `--require-complete` with named pairs); 16-col → 0 non-budget `score=None`
- [ ] Grade wall-clock + effective throughput reported vs prod and last-dev baselines
**Depends on:** US-008. *(Note: live run is a manual gated step — Ralph prepares + dry-runs; operator executes the metered run.)*

### US-010 — Quality Gate
**Description:** Run the code reviewer 4× across the full changeset, fixing all real bugs each pass; run CodeRabbit if available; project validation must pass after fixes.
**Traces to:** all DECs.
**Done When:**
- [ ] 4 reviewer passes complete; all real findings fixed
- [ ] CodeRabbit review addressed (if available)
- [ ] `uv run pytest` (incl. drift/AST/grep/skill-parity gates) passes
**Depends on:** US-001…US-009.

### US-011 — Patterns & Memory (priority 99)
**Description:** Capture new patterns (rate-limiter seam, structured degrade discriminator, sweep/require_complete taxonomy) into `.claude/rules/` + `docs/` + memory.
**Done When:**
- [ ] `.claude/rules/` + docs updated with the limiter + discriminator + recovery taxonomy patterns
- [ ] `uv run pytest` passes
**Depends on:** US-010.

### Rules-compliance gate (applied to every story)
`extra="forbid"` on config models · typed validators · drift detectors + refreshed fixtures (US-001/005) · AST scans incl. scan-7 registration + planted-violation (US-006) · logger grep gate / lazy `json.dumps` (US-003/005/006) · exit-code taxonomy tier-2 (US-006) · 6-surface CLI parity (US-007) · `uv run pytest --cov-fail-under=80` every story.

## Beads Manifest
_(Phase 7 — pending)_
