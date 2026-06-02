# #186 — Parallelise grade layer via asyncio

> **Meta**
> - **Ticket:** [#186](https://github.com/wjduenow/SignalForge/issues/186) — `grade: parallelise per-(artifact × criterion) calls via asyncio.gather (deferred-to-v0.2 graduation)`
> - **Branch:** `feature/186-grade-asyncio` (off `origin/dev`)
> - **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/186-grade-asyncio`
> - **Phase:** published (PR [#190](https://github.com/wjduenow/SignalForge/pull/190) draft — awaiting approval)
> - **Plan started:** 2026-06-01

## Background

[#186](https://github.com/wjduenow/SignalForge/issues/186) executes the v0.2 graduation explicitly anticipated by `.claude/rules/grade-layer.md` § "One LLM call per (artifact × criterion); sequential (DEC-004, DEC-027)":

> Sequential, not parallel (mirrors prune DEC-028). `asyncio.gather` deferred to v0.2.

The #179 empirical retest (`docs/research/179-test-primitive-expansion-retest.md`, PR #182) measured **83 % of `signalforge generate` wall time landing in the grade layer** — ~280 s of every ~340 s per-model run was sequential `(criterion × artifact)` LLM calls. With ~270–290 grade calls per ~70-artifact model (and thousands on a 170-col wide model), realistic concurrency target is **10–20 in-flight grades**. Estimated grade-wall after refactor: ~20–30 s / model, putting per-model wall under a minute when combined with the separate Haiku follow-up.

## Scope

**In scope:**
- Convert the sequential `(artifact × criterion)` loop in `signalforge.grade.engine.grade_artifacts` to an `asyncio.TaskGroup`-orchestrated concurrent dispatch with a configurable cap (default 10 in-flight).
- Extend the LLM seam (`signalforge.llm.client.call_llm`) with an async sibling that preserves the existing retry taxonomy, ANSI-safe logging, cache-anomaly WARNING, and provider strategy contract.
- Add `GradeConfig.max_concurrent_calls: int = 10` (range-validated, `extra="forbid"`); update the strict drift-detector mirror.
- Preserve every load-bearing grade-layer invariant (see § "Invariants to preserve").
- Add tests for: per-criterion retry isolation under concurrency; total-budget cancellation mid-flight; conservative degrade routing; `aggregate_complete` semantics; fail-closed audit-write atomicity under concurrent appends.
- Update `docs/grade-ops.md` and `grade-layer.md` to reflect the asyncio orchestrator.

**Out of scope:**
- Parallelising the **drafter** (one LLM call per model — nothing to parallelise).
- Parallelising the **prune** layer (mirrors-but-separate graduation per `prune-engine.md` DEC-028; tracked separately).
- Cross-model batch parallelism (the existing per-model loop in `cmd_generate --select` stays sequential — `cli-layer.md` § "Multi-model batch driver").
- Switching the default grader model (Sonnet → Haiku is the separate #160-class follow-up).
- Removing the sync `grade_artifacts` public signature — callers (CLI, library users) keep one entry point.

## Architectural commitments to preserve

(From `CLAUDE.md` and `.claude/rules/grade-layer.md`.)

1. **Signal over volume.** The refactor changes execution shape only — every always-pass / always-fail decision still ships with positive evidence.
2. **Evaluation in the loop.** Every artifact still gets one judge call per criterion; cap is a concurrency knob, not a sampling knob.
3. **Explainable diffs.** Per-pair `why` survives; the audit JSONL still carries one record per evaluated pair with all five reproducibility hashes.
4. **OSS-first.** No new vendor dependency; `anthropic` and the other provider SDKs already ship async clients.

## Invariants to preserve (load-bearing)

Each is a `grade-layer.md` DEC. The refactor MUST preserve all of them:

| # | DEC | Invariant | Impact under asyncio |
|---|---|---|---|
| 1 | DEC-002, DEC-015 | Conservative two-state `score: float | None`; three degrade routes (LLMError, GradeOutputError, budget) | **Adds 4th source:** `asyncio.CancelledError` mid-flight when budget trips. Folds into existing "budget exceeded" reasoning text — no new degrade reason. |
| 2 | DEC-004 (per-criterion retry isolation) | One failed pair degrades; siblings keep running | `asyncio.gather(..., return_exceptions=True)` style OR `TaskGroup` with per-coroutine `try/except`. Each pair's exception caught inside its own coroutine. |
| 3 | DEC-006, DEC-012 | Fail-closed audit JSONL; size cap before open; atomic per-line write | **Preserved unchanged** — each coroutine opens its own fd; PIPE_BUF (4096) ≥ record cap (4000), so concurrent `O_APPEND` writes are atomic. Ordering across pairs becomes arrival-order, NOT (criterion × artifact) order — explicitly accepted per ticket and grade-layer audit semantics. |
| 4 | DEC-008 (envelope breach) | Whole-run pre-flight scan over every `(artifact_id, artifact_text)` before first LLM call | Runs once before async dispatch. Unchanged. |
| 5 | DEC-010, DEC-019 (reproducibility hashes) | `rubric_hash`, `prompt_version_template`, `criterion_prompt_hash`, `response_text_hash` per event | Computed per-call inside each coroutine. Unchanged. |
| 6 | DEC-011 (total-budget cancellation) | Pending pairs cancelled, every un-evaluated pair routes to `kept-without-evidence + reason="grade budget exceeded …"`, one stderr WARNING | Implemented via `asyncio.timeout(total_budget_seconds)` wrapping the `TaskGroup`; un-completed coroutines catch `CancelledError` → existing degrade text. |
| 7 | DEC-029 (single `GradeEvent` construction seam, 6th AST scan) | Only `signalforge.grade.audit._build_grade_event` constructs a `GradeEvent` | Static AST check; concurrent calls all route through the seam. Unchanged. |
| 8 | DEC-029 (`signalforge.yml` `grade:` namespace, `extra="forbid"`) | `GradeConfig` extends only via known fields | Adds `max_concurrent_calls: int = 10` to `GradeConfig`; updates `StrictGradeConfig` drift mirror in lockstep. |
| 9 | LLM seam (`llm-drafter.md` DEC-012) | Single SDK seam confines `# pyright: ignore` for Anthropic | Async path also confined to `signalforge.llm._anthropic_client` (and per-vendor sibling shims). `AnthropicClientProtocol` extends with optional `async_messages` surface or a sibling `AsyncAnthropicClientProtocol`. |
| 10 | `_sleep` / `_rand_uniform` aliases (`llm-drafter.md` DEC-004) | Module-level aliases for deterministic test injection | Mirrored on the async path: `_async_sleep = asyncio.sleep`; tests override at module scope. |

## Discovery findings (Phase 1)

### Current sequential loop

`src/signalforge/grade/engine.py:660-746` — `criterion`-outer / `artifact`-inner iteration via `_iterate_artifacts()` (lines 207-227). Per-pair body in `_grade_one()` (lines 306-319) issues one `call_llm` per pair; degraded path in `_build_degraded()` (lines 416 …) constructs the `GradingResult(score=None, …)` and matching `GradeEvent`. Audit write at line 744 (`_write_event_or_abort`).

**Budget enforcement:** wall-clock check at line 671 (`if (time.monotonic() - start_monotonic) >= total_budget_seconds`). Once tripped, every remaining pair degrades without an LLM call.

**`aggregate_complete`:** computed field on `GradingReport` (`models.py:218`), `True` iff every result has non-null `score`.

### LLM seam status

- `signalforge.llm.client.call_llm` is **sync-only** today; `signalforge.llm._anthropic_client._make_anthropic_client` builds `anthropic.Anthropic` (sync); `AnthropicClientProtocol` describes `messages.create` and `count_tokens` as sync methods.
- `anthropic.AsyncAnthropic` exists in the SDK; OpenAI ships `openai.AsyncOpenAI`; Google's `genai.Client` is sync (its async variant is `genai.aio` namespace, library-version-dependent).
- The codebase has **zero existing async usage** (`grep -r "async def\|asyncio\|await" src/signalforge/` empty). Grade is the first.

### Tests pinning behaviour

- `tests/grade/test_engine.py:506-599` — per-criterion retry isolation (one failed pair, rest scored, `aggregate_complete is False`).
- `tests/grade/test_engine.py:471-499` — total-budget cancellation via `monkeypatch.setattr(engine_module.time, "monotonic", ...)`.
- `tests/grade/test_engine.py:606-637` — `_format_degrade_reasoning` surfaces inner `LLMResponseFormatError` message.
- `tests/grade/test_models.py` — `aggregate_complete` computed-field semantics.
- `tests/grade/test_drift_detector.py` — `StrictGradeConfig` (extra="forbid") needs the new `max_concurrent_calls` field.

### Audit-writer atomicity under concurrency

`signalforge.grade.audit.write_grade_event` (lines 141-267) opens a fresh fd per call (`os.open(..., O_WRONLY | O_APPEND | O_CREAT, 0o600)`), does a short-write loop over `os.write`, fsyncs, closes. POSIX guarantees `O_APPEND` atomicity per-call up to `PIPE_BUF` (4096 B on Linux); `_GRADE_AUDIT_RECORD_LIMIT_BYTES = 4000` is exactly chosen to fit under that bound. **Concurrent appends from coroutines are safe by construction.** Ordering of lines becomes arrival order rather than `(criterion × artifact)` order — snapshot tests will need to sort by `(artifact_id, criterion_id)` before compare.

### Dev-dep additions required

- `pytest-asyncio` — NOT currently in `[dependency-groups].dev`. The plan must add it.
- The Anthropic / OpenAI SDKs already ship async clients; no new top-level dep.

### Python floor

`requires-python = ">=3.11"` and CI matrix is 3.11 / 3.12 / 3.13 (`python-build.md`). `asyncio.TaskGroup` lands in 3.11; floor is sufficient with no version bump.

## Scoping decisions (Phase 1 close)

| # | Decision | Rationale |
|---|---|---|
| Q1 | **All three providers (Anthropic + OpenAI + Gemini) async at launch.** | Avoids a "provider feature parity" debt overhang; each provider strategy gains an `async_messages_create` cousin in lockstep. Extends gated live-e2e matrix (`@pytest.mark.anthropic` / `openai` / `gemini`) by a tested-once async path per vendor. |
| Q2 | **Sibling async surface (`call_llm_async`).** | Smallest blast radius — existing sync callers (drafter, future stages, all today's tests) are unchanged. `signalforge.llm.client.call_llm_async` mirrors `call_llm`'s signature + retry taxonomy + `_async_sleep`/`_rand_uniform` aliases (DEC-004 of `llm-drafter.md` mirrored). `AnthropicClientProtocol` keeps the sync surface; a sibling `AsyncAnthropicClientProtocol` carries the async one. |
| Q3 | **Default `max_concurrent_calls = 10`.** | Matches ticket. Operators can raise via `signalforge.yml` `grade.max_concurrent_calls` for ~170-col wide models without code changes. Cap range-validated `[1, 100]`. |

Deferred to refinement (Phase 3):
- Concurrency primitive shape (`asyncio.TaskGroup` + `asyncio.Semaphore` vs. plain `gather`).
- Snapshot-test JSONL re-sorting strategy (test-side sort vs. reader helper).
- Whether OpenAI / Gemini providers need a per-vendor `AsyncClientProtocol` or one shared `_LLMAsyncClientProtocol`.

## Architecture Review (Phase 2)

Six parallel review subagents reported (security, performance, API/seam design, observability+testing, data-model+AST scans, per-provider async deep-dive). Findings triaged below.

### Blockers (must resolve in refinement before stories generate)

| # | Area | Finding | Fix shape |
|---|---|---|---|
| B1 | **`ExceptionGroup` traceback leak** | `asyncio.TaskGroup` propagates `ExceptionGroup` on context exit. Current `signalforge.cli._helpers.format_error_to_stderr` has no handler → `str(group)` would print a multi-line traceback (violates `cli-layer.md` DEC-016 "no traceback ever leaks"). | Two-layer defence: (a) every coroutine catches `(GradeLLMError, GradeOutputError, GradePromptEnvelopeBreachError)` inside its body → `TaskGroup` only sees `asyncio.TimeoutError` from the budget wrapper, never `ExceptionGroup`; (b) belt-and-braces `ExceptionGroup` handler in `format_error_to_stderr` rendering the multi-bullet shape. Test: hostile coroutine that raises a non-grade exception triggers the renderer; assert no `"Traceback"` in stderr. |
| B2 | **Provider `supports_async` capability flag + ABC method** | `LLMProvider` ABC has no `supports_async` flag or `make_async_client()` method. Without it, a v0.4 provider lacking async would `TypeError` at `await client.messages.create(...)`. | Add `supports_async: ClassVar[bool] = True` (Anthropic + OpenAI + Gemini all set True for v0.3). Add `@abc.abstractmethod def make_async_client() -> _LLMAsyncClientProtocol`. `call_llm_async` raises a typed `LLMProviderAsyncUnsupportedError` (CLI tier 3 — runtime resource) if `not provider.supports_async`; the grade engine catches at orchestrator entry and clamps `max_concurrent_calls = 1` with one WARNING, OR raises and the operator fixes config. **Refinement decides which.** |
| B3 | **AST scan #3 + #9 extensions for async constructors** | Scan 3 pins `anthropic.Anthropic(...)` to `_anthropic_client.py`; scan 9 pins `openai.OpenAI(...)` to `_openai_client.py`. The async constructors (`AsyncAnthropic`, `AsyncOpenAI`) bypass the existing scans. Scan 10 (Gemini) is untouched — Gemini's async surface is `.aio` on the same `genai.Client`, not a separate class. | Add **Scan 3b** (`anthropic.AsyncAnthropic` only in `_anthropic_client.py`) and **Scan 9b** (`openai.AsyncOpenAI` only in `_openai_client.py`). Total project AST scans goes **10 → 12** in lockstep with the lib code. Each gets the three-pattern bypass coverage from `testing-signal.md` § "AST single-construction-seam scans" + a planted-violation self-check. |
| B4 | **Fake clients gain async surface** | `FakeAnthropicClient` / `FakeOpenAIClient` / `FakeGeminiClient` only expose sync `create` / `count_tokens`. `await fake.messages.create(...)` fails — sync method isn't awaitable. | Add async sibling surfaces to each fake (`async def create`, `async def count_tokens`) that drain the same `expect_*` queues. Two implementation shapes considered: (a) **dual sync+async methods on one fake** — simpler, one expect queue per kind, both surfaces drain the same queue (chosen in refinement); (b) **separate `FakeAsync<Vendor>Client` classes** — duplicates expect plumbing. |
| B5 | **`pytest-asyncio` dev-dep + strict mode** | Not currently in `[dependency-groups].dev` or `[project.optional-dependencies].dev`. `@pytest.mark.asyncio` would silently skip tests in modern pytest-asyncio modes. | Add `pytest-asyncio>=0.23,<1` to both groups (matches the project's dual-listing convention from `python-build.md`). Set `asyncio_mode = "strict"` under `[tool.pytest.ini_options]` — every async test must explicitly use `@pytest.mark.asyncio`. Register the marker in `[tool.pytest.ini_options].markers`. |

### Concerns (resolve in refinement)

| # | Area | Finding | Recommended path |
|---|---|---|---|
| C1 | **Anthropic prompt-cache cost penalty** | Calls 1–10 dispatch concurrently before cache warms → all 10 pay the cache-write premium (≈ 1.25× input cost) instead of the read discount (≈ 0.10×). The rubric block is ~430 tokens × ~10 concurrent = **~4 450 extra input-token-equivalents per model run** (~$0.003–$0.005 / model). Quantified but cumulative. | Accept the cost; document explicitly in `docs/grade-ops.md` § "Cost expectations under concurrency"; surface in CHANGELOG `[Unreleased]` § Changed. Operator can set `grade.max_concurrent_calls: 1` for cost-sensitive runs. **No code change.** OpenAI + Gemini have `supports_prompt_caching=False` so no penalty applies. |
| C2 | **`asyncio.CancelledError` budget-vs-other attribution** | When `asyncio.timeout(total_budget_seconds)` fires, every in-flight coroutine receives `CancelledError`. Cannot mechanically distinguish budget-trip from KeyboardInterrupt / SystemExit. | Pattern: orchestrator wraps `TaskGroup` in `try: async with asyncio.timeout(N): … except TimeoutError: _budget_exceeded = True`. Each coroutine catches `CancelledError` and checks the orchestrator-scope flag. If set → degrade to `reasoning="grade budget exceeded (Ns) before evaluation"`. If unset → re-raise (parent task is dying for an unrelated reason; let `KeyboardInterrupt` propagate to the CLI boundary). |
| C3 | **`asyncio.run` event-loop nesting** | If a future caller (e.g. v0.4 cross-model batch parallelism) wraps `grade_artifacts` in an outer event loop, the inner `asyncio.run(_grade_artifacts_async_core(...))` raises `RuntimeError: asyncio.run() cannot be called from a running event loop`. | v0.3 ships single-loop only. Detect with `try: asyncio.get_running_loop(); raise NestedEventLoopError(remediation=...) except RuntimeError: asyncio.run(...)`. Document the v0.4 follow-up explicitly in `docs/grade-ops.md` and add a typed `GradeNestedEventLoopError(GradeError)` to the exit-code table (CLI tier 1 — operator configuration / environment problem; same tier as `ManifestNotFoundError`). Or accept the bare RuntimeError will leak and let the v0.4 follow-up fix it cleanly. **Refinement decides.** |
| C4 | **`total_budget_seconds = 300` default** | Sequential nominal grade wall was ~30 s; budget tripped only on catastrophic stalls. Under concurrent dispatch the nominal wall is ~5–6 s, so 300 s is overly permissive and the soft warning never fires when ops would want it to. | Two options: (a) **lower default to 60 s** in same commit, document under CHANGELOG `[Unreleased]` § Changed as a behaviour change; (b) **keep 300 s** for zero behaviour drift, document the new headroom and recommend in ops docs. Recommend (a) only if also adding an INFO line when budget exhausted at a multiple of nominal wall (i.e. soft signal); else (b). |
| C5 | **`os.fsync` blocks the event loop** | `audit.write_grade_event` calls `os.fsync(fd)` synchronously. Under async, every audit-write blocks the event loop for ~1–5 ms (NVMe) up to ~50 ms (slow SSD). At concurrency 10, the loop stalls ~30 ms per batch end. | Wrap the writer call in `await loop.run_in_executor(None, write_grade_event, event, audit_path=…)`. The fsync still serialises at the OS level (no wall-time gain), but the event loop frees to schedule the next coroutine's prompt-building. Hygiene fix; low-risk. |
| C6 | **Budget-trip WARNING wording** | Existing sequential WARNING fields: `run_id`, `model_unique_id`, `evaluated`, `remaining_pairs`, `total_budget_seconds`. Under async, "evaluated" is ambiguous because tasks may be in-flight. | Extend the JSON to: `run_id`, `model_unique_id`, `completed_count`, `cancelled_count` (in-flight at trip time), `degraded_count` (un-started), `total_budget_seconds`. Lock the field set; pin via test. |
| C7 | **Audit JSONL snapshot ordering** | Current snapshot fixtures land in `(criterion, artifact)` order from the iterator. Under concurrent dispatch, lines land in arrival order — non-deterministic. | In every snapshot-style test, post-load sort by `(artifact_id, criterion_id)` before compare. Add a `_sort_grade_events(jsonl_lines)` helper in `tests/grade/_helpers.py`. The orchestrator does NOT sort before writing (that would buffer and break per-decision fail-closed durability). |
| C8 | **PIPE_BUF assumption on non-Linux** | `_GRADE_AUDIT_RECORD_LIMIT_BYTES = 4000` < Linux `PIPE_BUF = 4096` → atomic. macOS `PIPE_BUF = 512` → torn writes possible under concurrent appends. | The project's CI matrix is Linux (`ci.yml`) and the user runs WSL2; the existing rule already encodes Linux-POSIX semantics (`safety-layer.md` § Scan 8). Document the Linux-only atomicity assumption in `audit.py` module docstring + `grade-layer.md`. No code change for v0.3; macOS users wanting strong atomicity set `max_concurrent_calls = 1`. |
| C9 | **Per-provider live async smoke design** | Each provider has a gated live smoke. Async needs at least one per vendor exercising concurrent dispatch. | Add three new test files: `test_e2e_anthropic_async_smoke.py`, `test_e2e_openai_async_smoke.py`, `test_e2e_gemini_async_smoke.py`. Each: same env-var gating as the sync sibling, set `grade.max_concurrent_calls = 3` (small for cost), invoke `signalforge generate`, assert audit JSONL has expected pair count and no traceback in stderr. Three new tests rather than a parametrize keeps failures actionable per-vendor. |

### Passes (no further action)

- Whole-run envelope-breach pre-flight (DEC-008) runs in sync prefix before `asyncio.run` — unchanged.
- Per-coroutine exception isolation (DEC-004) — each coroutine catches its own grade-typed exceptions; no `ExceptionGroup` from grade-layer code.
- SDK clients are coroutine-safe (`httpx.AsyncClient`-backed for all three vendors).
- Single `GradeEvent` construction seam (DEC-029) — concurrent calls still all route through `_build_grade_event`; static AST scan untouched.
- `GradingResult` / `GradingReport` / `GradeEvent` data shapes unchanged; `audit_schema_version` stays `Literal[1]` (record shape identical, only ordering changes).
- Realistic speedup ~5–6× wall reduction (Amdahl-limited by tail latencies) — meets ticket's stated target.
- `Semaphore(max_concurrent_calls)` is the correct throttle primitive; pure-`gather` + retry-backoff would also work but loses operator-readable concurrency limits.
- Cancellation overhead ~100 ms for 10 tasks; memory footprint <2 MB; CPU prompt-build sub-millisecond.
- `build_create_kwargs` / `extract_text_blocks` / `extract_usage` / `is_clean_completion` / `classify_exception` are pure helpers reused across sync + async paths unchanged.
- `call_llm_async` signature is 1:1 with `call_llm`; `_async_sleep = asyncio.sleep` is the only new module-level alias (`_rand_uniform` stays sync — it's deterministic).
- `max_concurrent_calls = 1` is bit-for-bit equivalent to v0.1 sequential output (semaphore serialises in dispatch order).
- No CLI flag for `max_concurrent_calls` — mirrors `min_pass_rate` / `min_mean_score` config-file-only convention.
- Token-count gate adds 20 in-flight requests at peak (10 count + 10 create), well under Anthropic 4 000 RPM and OpenAI/Gemini equivalents.
- Cache-anomaly WARNING dedup: keep per-call WARNING (preserves per-criterion diagnostic signal; noise is acceptable).
- No new error classes from this work → AST scan #7 (exit-code mapping) untouched; the 13-`errors.py` count gate stays at 13.
- Fail-closed writer shape (AST scan #8) unchanged; `write_grade_event` body is identical.
- No drift-detector mirror needed for `GradeConfig` (config-shaped, not read-back per `safety-layer.md` § "extra= placement convention").
- Bundled skill (`SKILL.md`) doesn't currently document grade-layer config knobs — skill parity unaffected (per `skill-parity.md` the skill is parity for the CLI surface, not config-file fields).
- Logger grep gate covers `signalforge.{cli,demo,diff,draft,grade,llm,manifest,prune,safety,warehouse}` (10 dirs); the new async log sites in `signalforge.grade.engine` are already covered.

## Refinement Log (Phase 3)

### Decisions

- **DEC-001 (scope, Q1).** All three providers (Anthropic + OpenAI + Gemini) ship async at launch. Avoids feature-parity debt overhang; the per-vendor shim pattern from #135 / #136 / #137 absorbs the extension cleanly.
- **DEC-002 (seam shape, Q2).** Sibling async surface: `signalforge.llm.client.call_llm_async` parallel to existing sync `call_llm`. Smallest blast radius — no existing caller migrates. `AnthropicClientProtocol` keeps the sync surface; a sibling `AsyncAnthropicClientProtocol` carries the async surface. Same shape per-vendor.
- **DEC-003 (cap default, Q3).** `GradeConfig.max_concurrent_calls: int = 10`, range `[1, 100]`, `extra="forbid"`. Operators tune via `signalforge.yml`. Setting `1` yields v0.1 sequential behaviour bit-for-bit (semaphore serialises in dispatch order).
- **DEC-004 (public API).** Public `grade_artifacts(...)` keeps its sync signature unchanged. Internally: sync prefix → `asyncio.run(_grade_artifacts_async_core(...))` → sync suffix. No async sibling on the grade surface (the LLM seam is the only place the dual sync+async surface lives).
- **DEC-005 (LLMProvider ABC additions).** Add `supports_async: ClassVar[bool] = True` (default) + `@abc.abstractmethod def make_async_client() -> _LLMAsyncClientProtocol` to `LLMProvider`. All three concrete providers set `supports_async = True` for v0.3.
- **DEC-006 (sync-provider clash).** When `not provider.supports_async and config.max_concurrent_calls > 1`, raise typed `LLMProviderAsyncUnsupportedError(LLMError)` at `grade_artifacts` orchestrator entry **before** `asyncio.run` — CLI tier 3. Remediation: `"Set 'grade.max_concurrent_calls: 1' in signalforge.yml or pick an async-capable provider."` No silent clamp. Mirrors the project's `extra="forbid"` fail-loud posture.
- **DEC-007 (ExceptionGroup defence).** Two-layer: (a) every coroutine catches `(GradeLLMError, GradeOutputError, GradePromptEnvelopeBreachError)` inside its body — `TaskGroup` only ever sees `asyncio.TimeoutError` from the budget wrapper, never a grade-typed exception. (b) Belt-and-braces `isinstance(exc, BaseExceptionGroup)` branch in `signalforge.cli._helpers.format_error_to_stderr` renders the group as the existing multi-bullet stderr shape. Test: hostile coroutine raises non-grade exception → assert no `"Traceback"` in stderr.
- **DEC-008 (CancelledError budget attribution).** Orchestrator wraps `TaskGroup` in `try: async with asyncio.timeout(total_budget_seconds): … except TimeoutError: _budget_exceeded = True`. Each coroutine catches `CancelledError` and checks the orchestrator-scope flag — if set → degrade to `reasoning=f"grade budget exceeded ({N}s) before evaluation"` (existing locked text). If unset → re-raise (parent task dying for an unrelated reason; let `KeyboardInterrupt` propagate to the CLI boundary).
- **DEC-009 (nested event-loop guard).** `grade_artifacts` sync entry detects via `try: asyncio.get_running_loop()` — if a loop is already running, raise typed `GradeNestedEventLoopError(GradeError)` (CLI tier 1) with remediation `"v0.3 grade_artifacts is single-event-loop only. Call before entering an event loop, or wait for v0.4 async sibling."`. AST scan 7 picks it up automatically (depth-1 errors.py glob).
- **DEC-010 (budget default).** `GradeConfig.total_budget_seconds` stays at 300 s. Zero behaviour drift; operators tune down via `signalforge.yml` if they want the new tighter floor. `docs/grade-ops.md` documents the new headroom.
- **DEC-011 (async sleep alias).** Add module-level `_async_sleep = asyncio.sleep` in `signalforge.llm.client` (mirrors the existing `_sleep` / `_rand_uniform` aliases per `llm-drafter.md` DEC-004). `_rand_uniform` stays unchanged — it's deterministic and synchronous; both sync and async retry-backoff math uses it. The grade engine also exposes a module-level `_async_sleep` alias for the same test-injection purpose (used by the budget-cancellation test).
- **DEC-012 (fake client async surface).** Dual sync+async methods on the existing `FakeAnthropicClient` / `FakeOpenAIClient` / `FakeGeminiClient`. Both surfaces drain the same `expect_*` queues — one queue per kind (create / count_tokens), not two. Simpler than separate `FakeAsync<Vendor>Client` classes; tests can mix sync + async drains against one fake instance.
- **DEC-013 (pytest-asyncio).** Add `"pytest-asyncio>=0.23,<1"` to BOTH `[dependency-groups].dev` AND `[project.optional-dependencies].dev` (dual-listing convention, `python-build.md`). `[tool.pytest.ini_options].asyncio_mode = "strict"`. Register `asyncio` marker explicitly. Default-exclude pattern: no — async tests run on every default pytest invocation.
- **DEC-014 (AST scans extension).** Add Scan 3b (`anthropic.AsyncAnthropic(...)` only in `_anthropic_client.py`) and Scan 9b (`openai.AsyncOpenAI(...)` only in `_openai_client.py`). Both reuse `_AttributeCallFinder` from `testing-signal.md` § "AST single-construction-seam scans" with the three-pattern bypass coverage. Each gets a planted-violation self-check. Scan 10 (Gemini) untouched — `.aio` is a namespace on the existing `genai.Client`, not a separate class. **Total project AST scans: 10 → 12.**
- **DEC-015 (JSONL ordering).** Audit JSONL lands in arrival order on disk; the orchestrator does NOT sort before writing (would defeat the per-decision fail-closed durability). Tests sort by `(artifact_id, criterion_id)` via shared `tests/grade/_helpers.py::_sort_grade_events(lines)` helper before snapshot compare. The five reproducibility hashes (`rubric_hash`, `prompt_version_template`, `criterion_prompt_hash`, `response_text_hash`, `args_hash`) are computed per-call independently — no ordering dependency.
- **DEC-016 (audit_schema_version).** Stays `Literal[1]` — record shape unchanged, only on-disk ordering changes. External consumers don't gate on ordering; same record fields, same JSON shape.
- **DEC-017 (fsync executor wrap).** Inside `_grade_one_async`, after building the `GradeEvent`, call `await loop.run_in_executor(None, write_grade_event, event, audit_path=resolved_audit_path)` instead of a bare sync call. `os.fsync` still serialises at the OS level (no wall-time win), but the event loop frees to schedule the next coroutine's prompt-building during the fsync. Hygiene fix; low-risk.
- **DEC-018 (budget-trip WARNING shape).** Existing WARNING fields extend: `run_id`, `model_unique_id`, `completed_count`, `cancelled_count`, `degraded_count`, `total_budget_seconds`. (`completed_count` = pairs scored before the trip; `cancelled_count` = in-flight at trip time, routed to degrade via the `_budget_exceeded` flag; `degraded_count` = un-started, routed to degrade.) JSON field set locked; pinned by `tests/grade/test_engine.py::test_grade_artifacts_budget_warning_shape_locked`.
- **DEC-019 (cache-anomaly WARNING).** Keep per-call WARNING (don't dedup). Preserves per-criterion diagnostic signal — if pair 3 hits the anomaly but pair 5 doesn't, the operator wants to know which. Cache-anomaly WARNING is rare in healthy runs; noise is acceptable.
- **DEC-020 (live async smokes).** Three new test files: `tests/cli/test_e2e_anthropic_async_smoke.py`, `test_e2e_openai_async_smoke.py`, `test_e2e_gemini_async_smoke.py`. Each gated identically to the sync sibling (`@pytest.mark.e2e + @pytest.mark.<vendor>` + same env-var pattern via `_skip_reason_*`). Set `grade.max_concurrent_calls = 3` (small but >1; cost-conscious). Assert audit JSONL pair count matches expected + no `"Traceback"` in stderr. Same `apply_provider_override` + `copy_fixture_to_tmp` primitives as the existing smokes (`testing-signal.md` § "Per-test provider overlay").
- **DEC-021 (cache cost penalty).** Anthropic prompt-cache write multiplier under concurrency (calls 1..N pay write premium instead of read discount; ~3.5× the cached-rubric-block cost portion; absolute ~$0.003–$0.005 per typical model run). Accept; document in `docs/grade-ops.md` § "Cost expectations under concurrency"; surface in `CHANGELOG.md [Unreleased]` § Changed. Operators tune via `grade.max_concurrent_calls: 1` for cost-sensitive runs. OpenAI + Gemini have `supports_prompt_caching=False`; no penalty there.
- **DEC-022 (Linux PIPE_BUF assumption).** `_GRADE_AUDIT_RECORD_LIMIT_BYTES = 4000 < PIPE_BUF = 4096` ⇒ atomic concurrent appends on Linux. macOS `PIPE_BUF = 512` would allow torn writes under concurrency. Project CI matrix is Linux; users on macOS wanting strong atomicity set `max_concurrent_calls = 1`. Document in `audit.py` module docstring + `grade-layer.md`. No code change.
- **DEC-023 (no CLI flag).** `max_concurrent_calls` ships as `signalforge.yml grade:` config-file knob only. No `--max-concurrent-calls` CLI argparse argument. Mirrors `min_pass_rate` / `min_mean_score` / `cache_ttl` precedent (`cli-layer.md` § "Per-stage config knobs stay in YAML").
- **DEC-024 (no `GradeConfig` drift detector).** Config-shaped models aren't read-back from disk in the typed-result path (`safety-layer.md` § "extra= placement convention"); the drift-detector pattern applies only to `extra="ignore"` read-back models. `GradeConfig` (`extra="forbid"`) gets a validator-only test for the new field's range.

### Session notes

- Six parallel review subagents (security / performance / API-seam / observability+testing / data-model+AST / per-provider deep-dive) ran in Phase 2.
- The 5 blockers are all of the form "the plan must specify this" — none of them invalidate the refactor; each maps to a story below.
- The reviewers initially flagged the Anthropic prompt-cache cost penalty as a blocker; downgraded to a documented concern per DEC-021 (small absolute impact, operator-tunable).
- One unsurprising finding: the codebase has zero existing async usage — grade is the first stage to graduate, so this work doubles as a template the eventual prune asyncio graduation (`prune-engine.md` DEC-028 deferred) will copy.

## Detailed Breakdown (Phase 4)

**Story ordering** mirrors the project's natural shape: dev-deps + plumbing → LLM seam → per-vendor shims → orchestrator → grade engine → tests → docs → quality gate → patterns. Every story's AC includes the canonical validation `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` (CLAUDE.md § "Validation").

### US-001 — pytest-asyncio dev-dep + asyncio marker + module-level `_async_sleep` alias

**Description.** Add `pytest-asyncio` to dev-deps, configure strict-mode + asyncio marker, and add the `_async_sleep = asyncio.sleep` alias in `signalforge.llm.client` (mirrors `_sleep` / `_rand_uniform` per `llm-drafter.md` DEC-004). Ships an in-test smoke proving the dev-dep wires up correctly.

**Traces to:** DEC-013, DEC-011.

**Acceptance criteria.**
- `pyproject.toml` `[dependency-groups].dev` contains `"pytest-asyncio>=0.23,<1"`.
- `pyproject.toml` `[project.optional-dependencies].dev` contains the same pin (dual-listing convention).
- `[tool.pytest.ini_options].asyncio_mode = "strict"` set.
- `[tool.pytest.ini_options].markers` includes `"asyncio: async tests requiring pytest-asyncio"`.
- `signalforge.llm.client` declares `_async_sleep = asyncio.sleep` at module scope.
- Smoke test `tests/llm/test_async_sleep_alias.py::test_async_sleep_is_asyncio_sleep` asserts identity.
- Smoke test `tests/llm/test_async_marker_works.py` decorated `@pytest.mark.asyncio async def test_smoke()` runs and `assert True` (proves the harness wires up).
- Validation passes.

**Done when:** validation green; both smoke tests pass; `uv sync --dev` resolves pytest-asyncio.

**Files:**
- `pyproject.toml` — dependency-groups + optional-dependencies + pytest config.
- `src/signalforge/llm/client.py` — new module-level alias.
- `tests/llm/test_async_sleep_alias.py` — new file.
- `tests/llm/test_async_marker_works.py` — new file.

**Depends on:** none.

---

### US-002 — `LLMProvider` ABC additions + `LLMProviderAsyncUnsupportedError`

**Description.** Extend the provider-neutral ABC with `supports_async: ClassVar[bool] = True` (default) and `@abc.abstractmethod def make_async_client() -> _LLMAsyncClientProtocol`. Add `_LLMAsyncMessagesProtocol` + `_LLMAsyncClientProtocol` to `signalforge.llm.client` (alongside the sync siblings — same module, mirrors the existing `_LLMClientProtocol` pattern). Add `LLMProviderAsyncUnsupportedError(LLMError)` to `signalforge.llm.errors`; register in `_EXCEPTION_TO_EXIT_CODE` at tier 3.

**Traces to:** DEC-005, DEC-006.

**Acceptance criteria.**
- `signalforge.llm.providers.LLMProvider` declares `supports_async: ClassVar[bool] = True` and abstract `make_async_client(self) -> "_LLMAsyncClientProtocol"`.
- `signalforge.llm.client` declares `_LLMAsyncMessagesProtocol` (async `create`, async `count_tokens`) and `_LLMAsyncClientProtocol` (`messages: _LLMAsyncMessagesProtocol`).
- `signalforge.llm.errors.LLMProviderAsyncUnsupportedError(LLMError)` has `default_remediation = "Set 'grade.max_concurrent_calls: 1' in signalforge.yml or pick an async-capable provider."`.
- `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE[LLMProviderAsyncUnsupportedError]` = 3.
- AST scan 7 (`tests/test_audit_completeness.py::test_every_typed_error_is_in_exit_code_mapping_table`) passes verbatim.
- `tests/llm/test_providers.py::test_llmprovider_abc_declares_async_methods` pins the abstract surface.
- Validation passes.

**Done when:** validation green; abstract method on ABC; new error class registered; scan 7 green.

**Files:**
- `src/signalforge/llm/providers.py` — ABC extension.
- `src/signalforge/llm/client.py` — `_LLMAsyncMessagesProtocol` + `_LLMAsyncClientProtocol`.
- `src/signalforge/llm/errors.py` — `LLMProviderAsyncUnsupportedError`.
- `src/signalforge/cli/_helpers.py` — exit-code map.
- `tests/llm/test_providers.py` — new test.
- `tests/llm/test_errors.py` — pin remediation text.

**Depends on:** none (parallel with US-001).

---

### US-003 — Anthropic async shim + `AnthropicProvider.make_async_client` + AST Scan 3b

**Description.** Extend `signalforge.llm._anthropic_client` with the async client shape — protocol, lazy `AsyncAnthropic` import, factory. Override `AnthropicProvider.make_async_client` to return the adapter. Add **Scan 3b** to `tests/test_audit_completeness.py`: `anthropic.AsyncAnthropic(...)` only in `_anthropic_client.py`. Three-pattern bypass coverage + planted-violation self-check.

**Traces to:** DEC-001, DEC-002, DEC-014.

**Acceptance criteria.**
- `_anthropic_client.py` declares `_AnthropicAsyncMessagesProtocol`, `AsyncAnthropicClientProtocol` (`@runtime_checkable`), `_make_anthropic_async_client(api_key)` (lazy import).
- `AnthropicProvider.make_async_client(self)` returns `_make_anthropic_async_client(...)`.
- `AnthropicProvider.supports_async = True`.
- `tests/test_audit_completeness.py::test_async_anthropic_constructed_only_in_shim` (Scan 3b) walks every `.py` under `src/signalforge/` and rejects `anthropic.AsyncAnthropic(...)` outside `_anthropic_client.py`. Covers bare / import-alias / module-attribute patterns.
- Companion `test_attribute_call_finder_catches_all_anthropic_async_bypass_patterns` planted-violation self-check passes.
- `tests/llm/test_anthropic_client_confinement.py` already-grep-based test scales to catch the new ignore lines.
- Validation passes.

**Done when:** validation green; Anthropic async surface lives only in the shim; AST scan 3b green; planted-violation test green.

**Files:**
- `src/signalforge/llm/_anthropic_client.py` — async shim.
- `src/signalforge/llm/providers.py` — `AnthropicProvider.make_async_client` override.
- `tests/test_audit_completeness.py` — Scan 3b + helper count update.
- `tests/llm/test_anthropic_client.py` — pin the async factory shape.

**Depends on:** US-002.

---

### US-004 — OpenAI async shim + `OpenAIProvider.make_async_client` + AST Scan 9b

**Description.** Mirrors US-003 for OpenAI. The shim has the extra façade complexity (`messages.create` → `await chat.completions.create(...)`) — implement `_OpenAIAsyncClientAdapter` paralleling the existing `_OpenAIClientAdapter`. Add Scan 9b for `openai.AsyncOpenAI`.

**Traces to:** DEC-001, DEC-002, DEC-014.

**Acceptance criteria.**
- `_openai_client.py` declares `_OpenAIAsyncMessagesAdapter` whose `async create(**kw)` forwards to `await self._raw.chat.completions.create(**kw)` and whose `async count_tokens(**kw)` raises `NotImplementedError` (mirrors sync; orchestrator never calls when `supports_token_count=False`).
- `_make_openai_async_client(api_key)` lazy-imports `from openai import AsyncOpenAI` and wraps in the adapter.
- `OpenAIProvider.make_async_client` override.
- `OpenAIProvider.supports_async = True`.
- Scan 9b: `tests/test_audit_completeness.py::test_async_openai_constructed_only_in_shim`. Three-pattern bypass coverage + planted-violation self-check.
- `tests/llm/test_openai_client_confinement.py` confinement test still green.
- Validation passes.

**Done when:** validation green; OpenAI async surface lives only in the shim; Scan 9b + planted-violation green.

**Files:**
- `src/signalforge/llm/_openai_client.py` — async adapter.
- `src/signalforge/llm/providers.py` — `OpenAIProvider.make_async_client` override.
- `tests/test_audit_completeness.py` — Scan 9b + count update.
- `tests/llm/test_openai_client.py` — pin async factory shape + JSON-mode kwarg parity (DEC-006 of #136).

**Depends on:** US-002.

---

### US-005 — Gemini async adapter (`.aio` namespace passthrough)

**Description.** Gemini's async surface is `client.aio.models.generate_content(...)` / `client.aio.models.count_tokens(...)` on the same `genai.Client` — no separate class. Build `_GeminiAsyncMessagesAdapter` that forwards via `.aio.models`. Build `_GeminiAsyncClientAdapter` whose `.messages` returns the async adapter. **Scan 10 stays unchanged** — `genai.Client(...)` construction still only in `_gemini_client.py`.

**Traces to:** DEC-001, DEC-002.

**Acceptance criteria.**
- `_gemini_client.py` declares `_GeminiAsyncMessagesAdapter.create(**kw) -> await client.aio.models.generate_content(**kw)` and `count_tokens(**kw) -> await client.aio.models.count_tokens(**kw)`.
- `_GeminiAsyncClientAdapter.messages` property returns `_GeminiAsyncMessagesAdapter(self._client)`.
- `GeminiProvider.make_async_client()` reuses the existing `_make_gemini_client(api_key)` (the same `genai.Client`) and wraps in the async adapter.
- `GeminiProvider.supports_async = True`.
- Scan 10 (existing) continues to pass — no `genai.Client(...)` construction added outside the shim.
- `tests/llm/test_gemini_client_confinement.py` confinement test still green.
- Validation passes.

**Done when:** validation green; Gemini async surface lives only in the shim; Scan 10 unchanged.

**Files:**
- `src/signalforge/llm/_gemini_client.py` — async adapter (no new `genai.Client` site).
- `src/signalforge/llm/providers.py` — `GeminiProvider.make_async_client` override.
- `tests/llm/test_gemini_client.py` — pin async-adapter `.aio` forwarding shape.

**Depends on:** US-002.

---

### US-006 — `signalforge.llm.client.call_llm_async` — the async orchestrator function

**Description.** Sibling of `call_llm`. Identical signature (keyword-only); same provider strategy dispatch; same retry taxonomy + per-class budgets; same lazy-format JSON WARNING / INFO emission. Backoff uses `await _async_sleep(...)` instead of `_sleep(...)`. Returns `LLMResult`.

**Traces to:** DEC-001, DEC-002, DEC-011.

**Acceptance criteria.**
- `signalforge.llm.client.call_llm_async(*, system, cached_block, dynamic_block, model, max_tokens, cache_ttl="5m", prompt_version, max_retries_429=3, max_retries_5xx=1, max_retries_conn=1, provider="anthropic", client=None) -> LLMResult` — exact signature parity with `call_llm`.
- Internal retry loop uses `await _async_sleep(...)` for backoff; `_rand_uniform(0.75, 1.25)` for jitter (unchanged — sync deterministic).
- Provider strategy resolved via `provider_for(name)`; calls `await strategy.make_async_client()` if `client is None`; calls `await client.messages.create(**strategy.build_create_kwargs(...))`.
- WARNING / INFO emission identical to sync (per-retry WARNING; final INFO if applicable). All logs via lazy-format JSON (logger grep gate covers `llm/`).
- Cache-anomaly WARNING (DEC-019: keep per-call) fires identically.
- `LLMProviderAsyncUnsupportedError` raised if `strategy.supports_async` is `False`.
- `tests/llm/test_call_llm_async.py` covers: happy-path, success-after-2-retries-429, exhausted-429, exhausted-5xx, conn-error-retry, sync-only-provider-raises (FakeNoCacheProvider).
- Validation passes.

**Done when:** validation green; `call_llm_async` parallels `call_llm`'s contract; full test coverage.

**Files:**
- `src/signalforge/llm/client.py` — `call_llm_async` + helper coroutines.
- `tests/llm/test_call_llm_async.py` — new test file.
- `tests/llm/_fake_provider.py` — extend `FakeNoCacheProvider` to optionally set `supports_async=False` for the sync-only-provider test.

**Depends on:** US-001, US-002, US-003, US-004, US-005.

---

### US-007 — Fake clients gain dual sync+async surface (Anthropic + OpenAI + Gemini)

**Description.** Add `async def create(**kw)` and `async def count_tokens(**kw)` methods to each existing `Fake<Vendor>Client`. Both sync and async surfaces drain the same `expect_*` queues. Tests can mix sync drains and async drains on the same fake instance.

**Traces to:** DEC-012.

**Acceptance criteria.**
- `FakeAnthropicClient` (in `tests/llm/_fake.py`): existing `messages.create(**kw)` sync method preserved; new `messages.async_create(**kw)` or duck-typed parallel attribute (decide shape — see DEC-012, dual methods on one class). Both consume from `_messages_create_expectations` queue.
- `FakeOpenAIClient` (`tests/llm/_fake_openai.py`): same.
- `FakeGeminiClient` (`tests/llm/_fake_gemini.py`): same.
- `tests/llm/test_fake_dual_surface.py::test_async_drain_consumes_same_queue` — queue an `expect_messages_create(matching=…, returns=…)`, drain via `await fake.messages.create(...)` once, then `await fake.assert_all_expectations_met()` passes.
- `tests/llm/test_fake_dual_surface.py::test_mixed_sync_async_drain` — queue two expectations, drain one sync + one async, assert all met.
- Validation passes.

**Done when:** validation green; all three fakes expose async surfaces; existing sync tests untouched.

**Files:**
- `tests/llm/_fake.py` — Anthropic fake async extension.
- `tests/llm/_fake_openai.py` — OpenAI fake async extension.
- `tests/llm/_fake_gemini.py` — Gemini fake async extension.
- `tests/llm/test_fake_dual_surface.py` — new test file.

**Depends on:** US-001.

---

### US-008 — `GradeConfig.max_concurrent_calls` field + `GradeNestedEventLoopError` + grade orchestrator entry guards

**Description.** Add the config field with range `[1, 100]` validator. Add `GradeNestedEventLoopError(GradeError)` typed error + exit-code mapping (tier 1). Wire both checks at `grade_artifacts` orchestrator entry — before any `asyncio.run`.

**Traces to:** DEC-003, DEC-006, DEC-009.

**Acceptance criteria.**
- `GradeConfig.max_concurrent_calls: int = 10` placed immediately after `total_budget_seconds` (logical grouping: budget control).
- `@field_validator("max_concurrent_calls")` rejects `< 1` or `> 100` with message `"must be in the closed interval [1, 100]"`.
- `GradeNestedEventLoopError(GradeError)` declared in `signalforge.grade.errors` with `default_remediation = "v0.3 grade_artifacts is single-event-loop only. Call before entering an event loop, or wait for v0.4 async sibling."`.
- `_EXCEPTION_TO_EXIT_CODE[GradeNestedEventLoopError] = 1`.
- `grade_artifacts(...)` sync entry: detects nested event loop via `try: asyncio.get_running_loop(); raise GradeNestedEventLoopError(...) except RuntimeError: pass` — runs **after** prune-result / project-dir / audit-path checks but **before** `asyncio.run`.
- Same entry block: if `provider_for(config.provider).supports_async is False and config.max_concurrent_calls > 1` → `raise LLMProviderAsyncUnsupportedError(...)`.
- `tests/grade/test_config.py::test_max_concurrent_calls_field_range` exercises bounds.
- `tests/grade/test_engine.py::test_grade_artifacts_raises_on_nested_event_loop` exercises the nested guard.
- `tests/grade/test_engine.py::test_grade_artifacts_raises_on_sync_only_provider` exercises the clash detection (uses `FakeNoCacheProvider` with `supports_async=False`).
- AST scan 7 (errors-in-exit-code-table) passes verbatim.
- Validation passes.

**Done when:** validation green; field validates; both entry guards raise typed errors before `asyncio.run`.

**Files:**
- `src/signalforge/grade/config.py` — field + validator.
- `src/signalforge/grade/errors.py` — `GradeNestedEventLoopError`.
- `src/signalforge/grade/engine.py` — entry guards (no asyncio yet — that's US-009).
- `src/signalforge/cli/_helpers.py` — exit-code map.
- `tests/grade/test_config.py` — range-validation tests.
- `tests/grade/test_engine.py` — guard tests.

**Depends on:** US-002.

---

### US-009 — Grade engine asyncio refactor (`_grade_one_async`, `_grade_artifacts_async_core`, semaphore + TaskGroup + budget timeout)

**Description.** **The meat of the ticket.** Replace the sequential `while iter_index < len(iterator)` loop in `grade_artifacts` with an async core orchestrated by `asyncio.TaskGroup` + `asyncio.Semaphore(max_concurrent_calls)` + `asyncio.timeout(total_budget_seconds)`. Each pair runs as a coroutine `_grade_one_async`. Audit-write happens via `await loop.run_in_executor(None, write_grade_event, ...)`. Per-coroutine try/except mirrors the sync path's degrade logic. Budget-trip cancellation routes via the `_budget_exceeded` flag → existing `"grade budget exceeded ({N}s) before evaluation"` reasoning. Public `grade_artifacts(...)` becomes a sync wrapper calling `asyncio.run(_grade_artifacts_async_core(...))`. Module declares `_async_sleep = asyncio.sleep` for test injection (separate alias from the LLM client's; deterministic injection in budget-cancellation tests). WARNING shape extension per DEC-018: JSON fields `completed_count` / `cancelled_count` / `degraded_count` / `total_budget_seconds`.

**Traces to:** DEC-002, DEC-004, DEC-007, DEC-008, DEC-010, DEC-011, DEC-015, DEC-016, DEC-017, DEC-018.

**TDD.**
- `test_grade_artifacts_concurrent_dispatches_in_parallel` — fake instrumented to record concurrent-in-flight count; assert max in-flight ≤ `max_concurrent_calls=3`.
- `test_grade_artifacts_concurrency_1_byte_equivalent_to_v0_1` — config `max_concurrent_calls=1`; audit JSONL byte-equal to the existing v0.1 fixture (after the test's `_sort_grade_events` helper which is a no-op here since order matches).
- `test_grade_artifacts_concurrent_budget_warning_shape_locked` — pins the exact JSON field set in the WARNING.

**Acceptance criteria.**
- New private `_grade_one_async(...)` coroutine in `engine.py` mirrors `_grade_one(...)` body but `await`s `call_llm_async(...)`. Returns `(grading_result, event)` pair.
- New `_grade_artifacts_async_core(...)` coroutine: builds the iterator (preserving `(criterion, artifact)` order via `_iterate_artifacts(...)` unchanged), creates `semaphore = asyncio.Semaphore(config.max_concurrent_calls)`, wraps `TaskGroup` in `try: async with asyncio.timeout(config.total_budget_seconds): … except TimeoutError: budget_exceeded = True`. Each task: `async with semaphore: try: await _grade_one_async(...) except (GradeLLMError, GradeOutputError, GradePromptEnvelopeBreachError) → degrade; except CancelledError: if budget_exceeded → degrade with budget reasoning; else: raise`.
- Audit write inside the coroutine: `await asyncio.get_running_loop().run_in_executor(None, _write_event_or_abort, event, resolved_audit_path)`.
- Public `grade_artifacts(...)` (sync) — sync prefix (validation, canonicalisation, envelope-breach pre-flight, nested-loop guard, provider-async-clash guard from US-008) → `results = asyncio.run(_grade_artifacts_async_core(...))` → sync suffix (sidecar write, report assembly, `fail_on_below_threshold` raise). Function signature, return type, and side effects unchanged from caller's perspective.
- Module-level `_async_sleep = asyncio.sleep` declared (separate alias from `signalforge.llm.client._async_sleep` — test injection at engine level for the budget-cancellation test).
- Budget-trip WARNING JSON field set: `{"run_id", "model_unique_id", "completed_count", "cancelled_count", "degraded_count", "total_budget_seconds"}` — pinned by test.
- All three TDD tests pass.
- The 6th AST scan (single `GradeEvent` construction seam) continues to pass verbatim — every `_build_grade_event` call still inside `signalforge.grade.audit`.
- Validation passes.

**Done when:** validation green; concurrent dispatch works; `max_concurrent_calls=1` is byte-equivalent to v0.1; budget-cancellation routes correctly.

**Files:**
- `src/signalforge/grade/engine.py` — async core + sync wrapper + WARNING shape.
- `tests/grade/test_engine.py` — concurrency + budget + byte-equivalence tests.

**Depends on:** US-006, US-007, US-008.

---

### US-010 — `ExceptionGroup` renderer in `format_error_to_stderr` (defence-in-depth)

**Description.** Add an `isinstance(exc, BaseExceptionGroup)` branch to `signalforge.cli._helpers.format_error_to_stderr` that renders the group as the existing multi-bullet stderr shape (header + `  - <ExcClass>: <msg>` bullets). Test with a hostile coroutine that raises a non-grade exception (e.g. `KeyError`) to confirm no `"Traceback"` leaks.

**Traces to:** DEC-007.

**Acceptance criteria.**
- `format_error_to_stderr` handles `BaseExceptionGroup` (Python 3.11+): header `"ERROR: Grade orchestrator encountered N concurrent failures:"`, bullets `"  - <ExcClass>: <repr-safe-str>"`, cap 10 bullets + overflow `"  ... and K more"`.
- Each exception's text routed via the existing `_format_value` (repr-safe) so ANSI / control chars don't leak.
- `tests/cli/test_format_error_to_stderr.py::test_exception_group_renders_as_multi_bullet` exercises a 3-exception group.
- `tests/grade/test_engine.py::test_grade_artifacts_hostile_coroutine_no_traceback` — inject a coroutine that raises `KeyError`; assert `"Traceback" not in capsys.readouterr().err`.
- Validation passes.

**Done when:** validation green; ExceptionGroup never leaks a traceback to stderr.

**Files:**
- `src/signalforge/cli/_helpers.py` — renderer extension.
- `tests/cli/test_format_error_to_stderr.py` — new test or extend existing.
- `tests/grade/test_engine.py` — hostile-coroutine test.

**Depends on:** US-009 (need the asyncio path to exercise the leak).

---

### US-011 — Test helper `_sort_grade_events` + retry-isolation test + budget-cancellation test (async)

**Description.** Add `tests/grade/_helpers.py::_sort_grade_events(lines: list[dict]) -> list[dict]` keyed by `(artifact_id, criterion_id)`. Rewrite the existing per-criterion retry-isolation test (currently sequential at `tests/grade/test_engine.py:506-599`) to use pair-identity predicates (regardless of dispatch order). Rewrite the budget-cancellation test (`tests/grade/test_engine.py:471-499`) to use `monkeypatch.setattr(engine_module, "_async_sleep", fake_sleep)` for deterministic fast-forward; assert `aggregate_complete=False` + the new WARNING JSON shape.

**Traces to:** DEC-015, DEC-018.

**Acceptance criteria.**
- `tests/grade/_helpers.py::_sort_grade_events(lines)` returns lines sorted by `(artifact_id, criterion_id)`. Pinned: idempotent + stable for ties.
- `tests/grade/test_engine.py::test_grade_artifacts_one_pair_retry_exhausted_under_concurrency` uses `expect_messages_create(matching=lambda kw: contains_pair_identity(kw, "column.X.description", "clarity"), returns=LLMRateLimitError(...))`. Assert one degraded + N-1 scored regardless of dispatch order.
- `tests/grade/test_engine.py::test_grade_artifacts_budget_exceeded_under_async` uses `_async_sleep` monkey-patch. Assert `aggregate_complete=False`, `completed_count + cancelled_count + degraded_count == total_pairs`, and the WARNING JSON contains the locked field set.
- Validation passes.

**Done when:** validation green; both rewritten tests + the helper land.

**Files:**
- `tests/grade/_helpers.py` — new file.
- `tests/grade/test_engine.py` — rewrite two tests + import the helper.

**Depends on:** US-007 (fake async surface), US-009 (engine async core).

---

### US-012 — Three per-provider live async smoke tests

**Description.** One new gated test file per vendor exercising the concurrent dispatch path against the real provider. Same env-var gating as the sync siblings. `grade.max_concurrent_calls = 3` (small but >1; cost-conscious — DEC-020).

**Traces to:** DEC-020.

**Acceptance criteria.**
- `tests/cli/test_e2e_anthropic_async_smoke.py` — `@pytest.mark.e2e @pytest.mark.anthropic`; env-var gating mirrors sync sibling; uses `copy_fixture_to_tmp` + `apply_provider_override(grade_max_concurrent_calls=3)`; asserts (a) `signalforge generate` exits 0, (b) audit JSONL has expected pair count, (c) no `"Traceback"` in stderr.
- `tests/cli/test_e2e_openai_async_smoke.py` — same shape, OpenAI provider.
- `tests/cli/test_e2e_gemini_async_smoke.py` — same shape, Gemini provider.
- `tests/cli/_e2e_helpers.py::apply_provider_override` gains a `grade_max_concurrent_calls: int | None = None` kwarg (additive, defaults preserve byte-equality).
- `CONTRIBUTING.md` § "Live e2e suite (pre-release only)" enumerates the three new tests; `tests/test_contributing_e2e_enumeration_parity.py` parity gate stays green.
- Validation passes (the live tests are deselected by default markers).

**Done when:** validation green; three new smokes land; parity gate green.

**Files:**
- `tests/cli/test_e2e_anthropic_async_smoke.py` — new.
- `tests/cli/test_e2e_openai_async_smoke.py` — new.
- `tests/cli/test_e2e_gemini_async_smoke.py` — new.
- `tests/cli/_e2e_helpers.py` — `apply_provider_override` extension.
- `CONTRIBUTING.md` — enumeration update.

**Depends on:** US-009 (engine async core), US-007 (fake async — not strictly needed for live but consistent).

---

### US-013 — Documentation + rule files + CHANGELOG

**Description.** Update operator-facing docs (`grade-ops.md`), rule files (`grade-layer.md`, `llm-drafter.md`), and `CHANGELOG.md`. Lock the new DEC numbering and link references.

**Traces to:** all DECs (this is the durable record).

**Acceptance criteria.**
- `docs/grade-ops.md` § "Concurrency" — new section documenting `max_concurrent_calls`, the typed errors (`LLMProviderAsyncUnsupportedError`, `GradeNestedEventLoopError`), cost expectations under concurrency (Anthropic cache penalty per DEC-021), Linux PIPE_BUF assumption (DEC-022), v0.4 nesting limitation (DEC-009).
- `.claude/rules/grade-layer.md` § "One LLM call per (artifact × criterion); sequential" graduates to "parallel via asyncio (DEC-004 of #186)" — preserves historical DEC pointer, names the new orchestration shape, links DECs.
- `.claude/rules/llm-drafter.md` § "Module-level `_sleep` / `_rand_uniform` aliases (DEC-004)" extended with the `_async_sleep` sibling + the sibling `call_llm_async` surface.
- `CHANGELOG.md [Unreleased]` § Added: `max_concurrent_calls` config knob + `call_llm_async` + per-provider async surfaces + two new typed errors.
- `CHANGELOG.md [Unreleased]` § Changed: grade-layer concurrent dispatch behaviour; Anthropic prompt-cache cost penalty note; audit JSONL ordering is now arrival-order (semantically unchanged, only on-disk sequence differs).
- This plan doc's Phase 6 / 7 marker set to `approved` (will move to `devolved` after `/super-plan 186` is told to devolve).
- Validation passes.

**Done when:** validation green; all four doc surfaces updated; CHANGELOG carries the entries.

**Files:**
- `docs/grade-ops.md` — new section.
- `.claude/rules/grade-layer.md` — graduation language.
- `.claude/rules/llm-drafter.md` — `_async_sleep` extension.
- `CHANGELOG.md` — entries.
- `plans/super/186-grade-asyncio-parallel.md` — phase marker.

**Depends on:** all implementation stories US-001 through US-012.

---

### US-014 — Quality Gate

**Description.** Run `code-review` four times across the full changeset, fixing real bugs found each pass. Run CodeRabbit review. Project validation must pass after all fixes. **This story depends on every implementation story.**

**Traces to:** the project's quality-gate convention (every super-plan ships one).

**Acceptance criteria.**
- 4 distinct `code-review` invocations with the four reviewer angles per memory `qg-diverse-reviewer-angles-catch-cross-surface-drift` (correctness / conventions / tests / docs+UX). Cross-surface findings (named by ≥2 angles) get high priority.
- All real bugs found are fixed. No deferred Pass-3 "nice-to-have" defensive tests if they cover patch-diff lines (memory `qg-pass-3-defer-defensive-tests-fails-codecov`).
- CodeRabbit review opened on the PR (when the PR exists); findings triaged.
- Final `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` exits clean.
- Validate the matrix floor: `uv run --python 3.11 pytest` + `uv run --python 3.12 pytest` (memory `ralph-validate-matrix-floor-not-just-ceiling`).
- Codecov patch coverage passes the project's standard.

**Done when:** all 4 code-review passes clear of real bugs; CodeRabbit findings addressed; validation green on 3.11 / 3.12 / 3.13.

**Files:** any file touched by US-001 through US-013 may receive QG fixes.

**Depends on:** US-001, US-002, US-003, US-004, US-005, US-006, US-007, US-008, US-009, US-010, US-011, US-012, US-013.

---

### US-015 — Patterns & Memory (priority 99)

**Description.** Update `.claude/rules/` with the durable patterns learned during this work; capture project-specific memories for the future async expansions (prune's eventual graduation in particular).

**Acceptance criteria.**
- `.claude/rules/grade-layer.md` — explicit § "Asyncio orchestrator pattern" with: TaskGroup + Semaphore + asyncio.timeout shape; `_budget_exceeded` flag attribution; per-coroutine catch isolation; fsync-in-executor; `_async_sleep` test injection. References #186 DEC-007, DEC-008, DEC-009, DEC-017.
- `.claude/rules/llm-drafter.md` — § "Dual sync+async LLM seam (DEC-002 of #186)" with: sibling `call_llm_async`; per-vendor async shims confined to `_<vendor>_client.py`; `_LLMAsyncClientProtocol` location; `LLMProvider.supports_async` capability flag + `make_async_client` ABC method.
- `.claude/rules/testing-signal.md` — § "AST single-construction-seam scans" updates the scan count from 10 → 12; references Scan 3b + Scan 9b. § "Engineered determinism" adds the `_sort_grade_events` helper pattern + the pair-identity predicate fake-injection pattern.
- New project memory `signalforge-asyncio-orchestrator-pattern.md` — captures the TaskGroup + Semaphore + asyncio.timeout shape as a precedent for prune's eventual graduation.
- New project memory `signalforge-dual-sync-async-fake-pattern.md` — captures the "dual sync+async methods on one fake class, shared expect queue" pattern.
- New project memory `signalforge-async-seam-confinement.md` — captures: every vendor SDK's async constructor stays in its `_<vendor>_client.py` shim, paired with an AST scan; total scan count must increase by 1 per new constructor name.
- Validation passes.

**Done when:** validation green; rules updates land; three new memory entries land; MEMORY.md index updated.

**Files:**
- `.claude/rules/grade-layer.md`
- `.claude/rules/llm-drafter.md`
- `.claude/rules/testing-signal.md`
- `/home/wesd/.claude/projects/-home-wesd-Projects-SignalForge/memory/signalforge-asyncio-orchestrator-pattern.md`
- `/home/wesd/.claude/projects/-home-wesd-Projects-SignalForge/memory/signalforge-dual-sync-async-fake-pattern.md`
- `/home/wesd/.claude/projects/-home-wesd-Projects-SignalForge/memory/signalforge-async-seam-confinement.md`
- `/home/wesd/.claude/projects/-home-wesd-Projects-SignalForge/memory/MEMORY.md`

**Depends on:** US-014.

---

### Dependency graph summary

```
US-001 (dev-deps + asyncio marker + _async_sleep)
US-002 (LLMProvider ABC + LLMProviderAsyncUnsupportedError)
  ├─> US-003 (Anthropic async shim + Scan 3b)
  ├─> US-004 (OpenAI async shim + Scan 9b)
  └─> US-005 (Gemini async adapter)
            └─> US-006 (call_llm_async)        depends on US-001 + US-003 + US-004 + US-005
US-001  └──> US-007 (fakes dual surface)
US-002  └──> US-008 (GradeConfig field + GradeNestedEventLoopError + entry guards)

US-006 + US-007 + US-008  ──> US-009 (grade engine asyncio refactor — the meat)
US-009 ──> US-010 (ExceptionGroup renderer)
US-009 + US-007 ──> US-011 (test helper + isolation + budget tests)
US-009 + US-007 ──> US-012 (per-provider live async smokes)

US-001..US-012 ──> US-013 (docs + CHANGELOG + rules)
US-001..US-013 ──> US-014 (Quality Gate)
US-014 ──> US-015 (Patterns & Memory)
```

**Parallelism opportunities (Ralph):**
- US-001 ‖ US-002 (both bedrock; no shared files)
- US-003 ‖ US-004 ‖ US-005 (three independent per-vendor stories, each touches only its own shim)
- US-006 + US-007 + US-008 (after the three shim stories, the LLM orchestrator can land in parallel with the fake surface and grade config) — caution per memory `ralph-serialize-shared-registry-beads`: US-002 + US-008 both touch `_EXCEPTION_TO_EXIT_CODE`; serialize at least the registration edits.
- US-010 + US-011 + US-012 (after US-009, three independent stories on the testing/CLI surface)

## Beads Manifest (Phase 7 — pending)

(Epic + task IDs.)

## References

- [#186](https://github.com/wjduenow/SignalForge/issues/186) — this ticket.
- [#179](https://github.com/wjduenow/SignalForge/issues/179) — the empirical retest establishing the 83 % grade-wall share.
- PR #182 — `docs/research/179-test-primitive-expansion-retest.md`.
- `.claude/rules/grade-layer.md` — every load-bearing grade-layer DEC.
- `.claude/rules/llm-drafter.md` § "One SDK seam", § "Module-level `_sleep` / `_rand_uniform` aliases".
- `.claude/rules/safety-layer.md` § "Fail-closed writer shape — Scan 8 covers all five writers".
- `.claude/rules/prune-engine.md` § "Total-budget semantics (DEC-011)" — the precedent pattern.
- `.claude/rules/testing-signal.md` — engineered determinism, no-traceback floor, strict-markers.
- `.claude/rules/cli-layer.md` § "Four-tier exit-code taxonomy" + § "7th AST scan".
- `.claude/rules/python-build.md` — `requires-python = ">=3.11"`.
