# Super Plan — #135: provider-neutral LLM seam (abstract `signalforge.llm` beyond Anthropic)

## Meta

- **Ticket:** https://github.com/wjduenow/SignalForge/issues/135
- **Parent epic:** #134 (pluggable LLM provider for grading — OpenAI/Gemini). Milestone v0.3.
- **Blocks:** #136 (OpenAI grading), #137 (Gemini grading).
- **Phase:** detailing
- **Branch:** (to create) `feature/135-provider-neutral-llm-seam`
- **Sessions:** 1 (2026-05-27)

## What / Why

Abstract `signalforge.llm` so an LLM vendor plugs in behind a thin, provider-neutral
interface — the prerequisite for OpenAI/Gemini grading. Mirrors the warehouse-adapter
seam (ABC/strategy + factory + per-vendor shim). **Anthropic stays byte-identical as the
default** — existing draft/grade fixtures and snapshots must not move.

The Anthropic coupling is shared by **both** the drafter (`draft_schema`) and the grader
(`grade_artifacts`) — both call the free function `call_anthropic(...)` and type their
`client` kwarg against the public `AnthropicClientProtocol`. So provider support cannot
live in `grade/` alone; the shared seam must be abstracted first. This ticket is that
refactor; #136/#137 then wire concrete providers.

## Discovery findings

### The seam today (all in `src/signalforge/llm/`)

- **`client.py:161` `call_anthropic(*, system, cached_block, dynamic_block, model, max_tokens, cache_ttl="5m", prompt_version, max_retries_429=3, max_retries_5xx=1, max_retries_conn=1, client=None) -> LLMResult`** — the single seam. A free function, not a method. Bakes in:
  - **Retry taxonomy** (`client.py:314-412`): 429×N / 5xx×N / conn×N with `delay = 2**attempt * _rand_uniform(0.75,1.25)`; 4xx-non-auth no-retry → `LLMHelperError`; auth no-retry → `LLMAuthError`. Categorisation keyed to Anthropic exception tuples from `_load_anthropic_exception_classes()`.
  - **count_tokens pre-send gate** (`client.py:238-305`): counts `system + cached_block`; drops the cache marker below the model min (1024/2048), raises `LLMCacheTooLargeError` above the 8000 cap.
  - **Prompt caching** (`client.py:214-228`): two content blocks; block-1 carries `{"cache_control":{"type":"ephemeral","ttl":cache_ttl}}`; `anthropic-beta: extended-cache-ttl-2025-04-11` header only when `cache_ttl=="1h"`.
  - **Cache economics + dual-zero WARNING** (`client.py:429-454`): reads `cache_creation_input_tokens`/`cache_read_input_tokens`; warns only when both are 0 while the marker was active.
  - Module aliases `_sleep`/`_rand_uniform` (`client.py:57-59`) for deterministic test backoff.
- **`_client.py`** — confines every Anthropic `# pyright: ignore` + `import anthropic` (lazy). Public `AnthropicClientProtocol` (`:61`, `.messages.create/.count_tokens`); private `_AnthropicMessagesProtocol`, `_AnthropicExceptionClasses`, `_make_anthropic_client(:81)`, `_load_anthropic_exception_classes(:115)` (returns rate_limit/api_status/auth/connection tuples).
- **`__init__.py` `__all__`** — exports `AnthropicClientProtocol`, `call_anthropic`, `LLMResult`, the `LLM*Error` hierarchy, pricing (`PRICES`, `lookup`, …).

### Call sites (thread `client` for test injection)

- `draft/schema.py:186` `draft_from_request(..., _client: AnthropicClientProtocol | None = None)` → `call_anthropic(... client=_client)`.
- `grade/engine.py:306` `_grade_one(..., client: AnthropicClientProtocol | None)` → `call_anthropic(... client=client)`; surfaced via `grade_artifacts(..., client=...)`.

### Anthropic-specific config / audit

- `draft/config.py`: `model`, `cheap_model`, `cache_ttl: Literal["5m","1h"]="5m"`, `max_retries_*`. `grade/config.py`: `model`, `cache_ttl="1h"`, `max_retries_*`.
- Audit cache fields: `draft/audit.py` `LLMResponseEvent.cache_creation_input_tokens/cache_read_input_tokens` (populated `:169`); `grade/models.py` `GradeEvent` same fields (`:310`), `_build_grade_event` (`grade/audit.py:82`), degraded path hardcodes 0.

### Already-neutral — do NOT touch

Judge system prompt, the `<ARTIFACT>` envelope guard, reproducibility blake2b hashes (`grade/prompts.py`).

### Reference seam (warehouse)

`warehouse/base.py` `WarehouseAdapter(abc.ABC)` + `@classmethod from_profile(profile)` lazy-dispatching on `profile.type`; per-vendor shim `adapters/_client.py` confining SDK ignores + `make_real_client` + `map_*_exception`. `tests/llm/_fake.py` `FakeAnthropicClient` (FIFO `expect_count_tokens`/`expect_messages_create` queues) is the test-fake precedent.

## Scoping decisions (Phase 1)

- **SD-1 — Provider boundary: BOTH `grade.provider` AND `draft.provider`.** User chose symmetry over the epic's "drafter out of scope" line. Both config blocks get a `provider` field (`Literal["anthropic"]="anthropic"` for now; #136/#137 widen the Literal). The shared seam is provider-capable; both stages select independently.
- **SD-2 — Generic orchestrator + per-provider strategy.** Keep one retry/backoff/count-tokens/cache loop (the current `call_anthropic` body, generalised). A provider supplies only: client shim, exception→category map, and capability flags (`supports_prompt_caching`, `supports_token_count`). Wiring a new provider = shim + map + enum value (matches the AC literally).
- **SD-3 — `_<vendor>_client.py` siblings + `llm/providers.py` registry.** Follow `llm-drafter.md` verbatim: rename `_client.py`→`_anthropic_client.py`; future vendors get `_openai_client.py` etc. New `llm/providers.py` holds the neutral protocol + capability descriptor + factory/registry. No new subpackage.
- **SD-4 — Rename `call_anthropic`→`call_llm`, DROP `call_anthropic`.** Clean breaking rename (pre-1.0). Remove `call_anthropic` from `__all__`; migrate both call sites; update every doc/surface that names it.

## Architecture review (Phase 2)

| Area | Rating | Finding |
|---|---|---|
| Neutral-protocol design | pass | Clean orchestrator/strategy split (DEC-001/002). |
| Byte-identity / backward-compat | concern | Event + `LLMResult` shapes unchanged → fixtures/snapshot byte-identical. Must update `tests/test_audit_completeness.py:329,360` (`_client.py`→`_anthropic_client.py`); migrate ~15 test imports; update 7 CLI monkeypatch sites. |
| Capability-gated caching | concern | `cache_control` marker + beta header + dual-zero WARNING gated on `supports_prompt_caching`; pre-send count cap gated on `supports_token_count`. Anthropic = both True ⇒ no behaviour change (DEC-008). |
| Config / surface parity | concern | `provider` on both `DraftConfig`+`GradeConfig`; 5-surface parity across docs + rule files (DEC-007, US-006). |
| Observability | pass | Logger grep gate uses `rglob` (rename-safe); logs stay generic lazy-format JSON. |
| Testing strategy | pass | `FakeAnthropicClient` stays; no-cache fake provider proves AC #2/#3 (DEC-011). |
| Security / Performance | pass | No new external surface; Anthropic path identical. |

No blockers. Concerns are resolved by the decisions below.

## Refinement log (Phase 3 — decisions)

- **DEC-001 — Generic orchestrator + provider strategy (SD-2).** `call_llm` owns the retry loop, backoff math (`2**attempt*_rand_uniform(0.75,1.25)`), WARNING/INFO logging, the min/cap token validation, and `LLMResult` assembly. The provider strategy owns: build create-kwargs, build count-tokens-kwargs, extract text blocks, extract usage, classify exception→category, and capability flags. The orchestrator never touches an Anthropic-shaped dict.
- **DEC-002 — Neutral value objects.** Introduce `UsageMetrics(input_tokens, output_tokens, cache_creation_input_tokens=0, cache_read_input_tokens=0)` and an `ExceptionCategory` enum (`AUTH`, `RATE_LIMIT`, `SERVER_ERROR`, `CONNECTION`, `NO_RETRY`). `extract_usage`→`UsageMetrics`; `classify_exception`→`ExceptionCategory`. Orchestrator dispatches on the enum, not on SDK exception classes.
- **DEC-003 — `LLMProvider` ABC + registry.** New `llm/providers.py`: `LLMProvider(abc.ABC)` (abstract `make_client`, `build_create_kwargs`, `build_count_tokens_kwargs`, `extract_text_blocks`, `extract_usage`, `classify_exception`; class-attr/property `name`, `supports_prompt_caching`, `supports_token_count`) + a process-level registry (`register_provider(provider)` / `provider_for(name) -> LLMProvider`; unknown name → typed `LLMError` subclass listing available keys). Mirrors `WarehouseAdapter.from_profile` dispatch, adapted to a name registry so new providers register rather than editing a factory `if`-ladder.
- **DEC-004 — Module layout (SD-3).** Rename `_client.py`→`_anthropic_client.py` (keeps `AnthropicClientProtocol` public per #44, plus `_AnthropicExceptionClasses`/`_make_anthropic_client`/`_load_anthropic_exception_classes`). `llm/providers.py` holds the ABC + value objects + registry. Future vendors add `_openai_client.py` siblings + a provider class. No new subpackage.
- **DEC-005 — `call_anthropic`→`call_llm`, drop old name (SD-4).** `call_llm` added to `__all__`; `call_anthropic` removed. Migrate both call sites + every test import + the `test_public_api.py`/`test_schema.py` documented-surface lists.
- **DEC-006 — Real-client construction pushed into `call_llm` (RF-1).** When `client is None`, `call_llm` resolves the strategy via the registry and calls `strategy.make_client()`. The CLI generate path stops calling `_make_anthropic_client`; it passes `provider=config.provider`. The 7 CLI monkeypatch tests switch to patching `AnthropicProvider.make_client` (or the registry) / injecting a fake client.
- **DEC-007 — `provider` config field on BOTH configs, registry-validated `str` (SD-1).** `DraftConfig.provider: str = "anthropic"` and `GradeConfig.provider: str = "anthropic"`, each with a validator asserting registry membership (fail-loud on unknown, listing available providers — mirrors the `trusted_models` validate-at-entry fail-loud). **Deliberate deviation from the `Literal`+`extra="forbid"` convention** (`safety-layer.md` DEC-015): a provider registry is a plugin point designed to grow, so #136/#137 register a provider instead of editing a `Literal` in two places, and a test can register a fake provider for AC #3. `call_llm` gains `provider: str = "anthropic"`. Other `extra="forbid"` config fields are unchanged.
- **DEC-008 — Capability degrade semantics.** `supports_prompt_caching=False` ⇒ no `cache_control` marker, no `extended-cache-ttl` beta header, report 0 cache tokens, skip the dual-zero anomaly WARNING. `supports_token_count=False` ⇒ skip the pre-send count gate entirely (no `LLMCacheTooLargeError` raised pre-send; documented deferral — a provider without token-counting can't enforce the 8000 cap up front). Anthropic sets both `True`, so its control flow + emitted bytes are unchanged.
- **DEC-009 — Keep `cache_ttl` config + `cache_*_input_tokens` audit fields as-is.** `cache_ttl: Literal["5m","1h"]` stays on both configs (Anthropic-specific; ignored when `supports_prompt_caching=False`). `LLMResponseEvent`/`GradeEvent` keep `cache_creation_input_tokens`/`cache_read_input_tokens` (default 0). Drift detectors + fixtures unchanged → byte-identity holds.
- **DEC-010 — AST confinement scan renamed, not extended.** `tests/test_audit_completeness.py` `anthropic.Anthropic(...)` confinement updates `_client.py`→`_anthropic_client.py` (lines 329, 360). No new scan in #135; a future provider's SDK-construction confinement (e.g. `openai.OpenAI()`) is that provider ticket's job.
- **DEC-011 — No-cache fake provider proves AC #2 + #3.** A test-only provider (`supports_prompt_caching=False`, `supports_token_count=False`) registered in the registry, selected via `grade.provider`, driven through `grade_artifacts` → assert: audit JSONL + sidecar round-trip, `cache_*_input_tokens==0`, drift detector + reproducibility blake2b hashes intact, no dual-zero WARNING. The fake IS the "shim + exception map + enum value" wiring, so it doubles as the AC #2 proof.
- **DEC-012 — Public client-protocol typing.** `AnthropicClientProtocol` stays public + Anthropic-specific (back-compat, #44). `call_llm`'s `client` param is typed `object | None` and handed to the strategy; `draft_schema`/`grade_artifacts` keep `client: AnthropicClientProtocol | None` (Anthropic is the default injection surface) — documented that non-Anthropic providers build their own client and ignore the kwarg.

## Story breakdown (Phase 4)

Ordering: foundation types → Anthropic strategy + rename → generic orchestrator → config/CLI wiring → neutrality proof → docs. Every story's AC includes the canonical `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.

### US-001 — Provider foundation: value objects + `LLMProvider` ABC + registry
- **Description:** Create `src/signalforge/llm/providers.py` with the `ExceptionCategory` enum, `UsageMetrics` value object, the `LLMProvider` ABC, and a process-level registry (`register_provider`/`provider_for`). No Anthropic behaviour wired yet.
- **Traces to:** DEC-001, DEC-002, DEC-003.
- **AC:** `provider_for("anthropic")` returns a provider (after US-002 registers it; here it raises the typed unknown-provider error listing available keys); unknown name raises an `LLMError` subclass with remediation. `UsageMetrics`/`ExceptionCategory` exported as needed. Validation passes.
- **Done when:** `providers.py` exists with the ABC + registry + value objects; unit tests cover registry hit/miss.
- **Files:** `src/signalforge/llm/providers.py` (new); `src/signalforge/llm/errors.py` (add `UnknownProviderError(LLMError)` or reuse `LLMHelperError` — pick fail-loud typed); `src/signalforge/llm/__init__.py` (export new public names); `tests/llm/test_providers.py` (new).
- **Depends on:** none.
- **TDD:** registry returns registered provider; unknown key raises typed error with available-keys remediation; `UsageMetrics` defaults cache fields to 0; `ExceptionCategory` has the five members.

### US-002 — `AnthropicProvider` strategy + shim rename + AST scan update
- **Description:** Rename `_client.py`→`_anthropic_client.py`; implement `AnthropicProvider(LLMProvider)` moving the Anthropic-specific request-build, text/usage extraction, and exception classification (`_extract_text_blocks`, `_extract_usage_field`, `_is_5xx`, `_is_4xx_non_auth`, `_load_anthropic_exception_classes`) behind the ABC methods; register it. Update the AST confinement scan.
- **Traces to:** DEC-002, DEC-003, DEC-004, DEC-010.
- **AC:** `AnthropicProvider` reproduces current extraction byte-for-byte against fake responses; `classify_exception` maps RateLimit/APIStatus(5xx)/APIStatus(4xx)/Auth/Connection to the right `ExceptionCategory`; `supports_prompt_caching`/`supports_token_count` both `True`; `provider_for("anthropic")` returns it; `test_audit_completeness.py` confinement points at `_anthropic_client.py`; validation passes.
- **Done when:** Anthropic logic lives on the provider; `_anthropic_client.py` is the only SDK-ignore home; registry wired.
- **Files:** `src/signalforge/llm/_client.py`→`_anthropic_client.py` (git mv); `src/signalforge/llm/providers.py` (+`AnthropicProvider`, register); `tests/test_audit_completeness.py:329,360`; `tests/llm/_fake.py` + `tests/llm/test_client_shim.py` imports; `tests/llm/test_providers.py`.
- **Depends on:** US-001.
- **TDD:** per-exception classification; usage extraction → `UsageMetrics`; text-block extraction parity; create/count-tokens kwargs match the current Anthropic shape.

### US-003 — Generic `call_llm` orchestrator
- **Description:** Refactor the `call_anthropic` body into `call_llm(*, system, cached_block, dynamic_block, model, max_tokens, cache_ttl="5m", prompt_version, max_retries_*, provider="anthropic", client=None) -> LLMResult`: resolve strategy from registry; build client via `strategy.make_client()` when `None`; pre-send count gate via strategy gated on `supports_token_count`; retry loop dispatching on `classify_exception`; cache marker/beta + dual-zero WARNING gated on `supports_prompt_caching`; assemble `LLMResult` from strategy extraction. Drop `call_anthropic`; add `call_llm` to `__all__`.
- **Traces to:** DEC-001, DEC-005, DEC-006, DEC-008.
- **AC:** All existing retry/cache tests (migrated to `call_llm`) pass; for `provider="anthropic"` the emitted logs + `LLMResult` are byte-identical to before; `call_anthropic` no longer importable; `test_public_api`/`test_schema` documented lists updated. Validation passes.
- **Done when:** `call_llm` is the single seam; Anthropic path proven byte-identical.
- **Files:** `src/signalforge/llm/client.py`; `src/signalforge/llm/__init__.py`; `tests/llm/test_client.py`, `tests/llm/test_client_retries.py`, `tests/llm/test_public_api.py`, `tests/draft/test_schema.py` (import + documented-list churn).
- **Depends on:** US-002.
- **TDD:** migrate the retry-budget, backoff-determinism (`_sleep`/`_rand_uniform`), cache-marker-drop, cache-too-large, and dual-zero-WARNING tests onto `call_llm`; assert `client is None` builds via the strategy.

### US-004 — `provider` config field + stage threading + CLI client-construction migration
- **Description:** Add registry-validated `provider: str = "anthropic"` to `DraftConfig` + `GradeConfig`. Thread `provider=config.provider` from `draft_from_request` and `grade._grade_one` into `call_llm`. Remove the `_make_anthropic_client` call from the CLI generate path (client now built inside `call_llm`); migrate the 7 CLI monkeypatch tests.
- **Traces to:** DEC-006, DEC-007.
- **AC:** Both configs round-trip `provider`; an unknown provider value fails loud at config load with available-keys remediation; `generate`/grade still work end-to-end with an injected fake client; CLI no longer references `_make_anthropic_client`. Validation passes.
- **Done when:** Provider selection flows config→`call_llm` for both stages; CLI migrated.
- **Files:** `src/signalforge/draft/config.py`, `src/signalforge/grade/config.py`, `src/signalforge/draft/schema.py`, `src/signalforge/grade/engine.py`, `src/signalforge/cli/generate.py`; the 7 `tests/cli/test_*` monkeypatch sites; `tests/draft/test_config.py`, `tests/grade/test_config.py`.
- **Depends on:** US-003.
- **TDD:** config accepts `anthropic`, rejects `bogus` with typed error; draft/grade pass the configured provider to `call_llm`; CLI generate path patches the registry/provider rather than `_make_anthropic_client`.

### US-005 — No-cache fake provider: provider-neutrality proof (AC #2 + #3)
- **Description:** Add a test-only provider (`supports_prompt_caching=False`, `supports_token_count=False`), registered in the registry, selected via `grade.provider`, and driven through `grade_artifacts`. Prove the audit/sidecar round-trip with zero cache metrics and intact reproducibility hashes.
- **Traces to:** DEC-008, DEC-011.
- **AC:** With the fake provider: `grade_artifacts` writes a valid audit JSONL + sidecar; `cache_*_input_tokens==0`; the grade drift detector validates the produced event; reproducibility blake2b hashes match the Anthropic-path recipe; no dual-zero WARNING emitted; no `cache_control`/beta header built. The fake provider is wired purely as shim + exception map + registry registration (AC #2). Validation passes.
- **Done when:** AC #2 + #3 are pinned by tests.
- **Files:** `tests/llm/_fake_provider.py` (new, test-only); `tests/grade/test_provider_neutrality.py` (new); possibly `tests/llm/_fake.py` (a no-cache response fake).
- **Depends on:** US-004.
- **TDD:** the whole story is the test (drives the no-cache provider and asserts the round-trip invariants).

### US-006 — Docs + 5-surface parity
- **Description:** Update operator-facing docs and rule files for the provider seam: the `provider` config knob, the `call_llm` rename, the `_<vendor>_client.py` convention, capability-gated caching.
- **Traces to:** DEC-004, DEC-005, DEC-007, DEC-008.
- **AC:** `docs/draft-ops.md` + `docs/grade-ops.md` document `provider`; `.claude/rules/llm-drafter.md` (+ `grade-layer.md` where it names `call_anthropic`/`_client`) updated to the neutral seam + registry; no stale `call_anthropic`/`_client.py` references in user-facing docs. `mkdocs build` (non-strict) clean. Validation passes.
- **Done when:** All 5 surfaces name `call_llm` + `provider` consistently.
- **Files:** `docs/draft-ops.md`, `docs/grade-ops.md`, `.claude/rules/llm-drafter.md`, `.claude/rules/grade-layer.md` (only where it names the seam). *(Note: `.claude/` edits are orchestrator-only — see Patterns & Memory note.)*
- **Depends on:** US-005.

### Quality Gate
- Run the code reviewer 4× across the full changeset, fixing real bugs each pass; run CodeRabbit if available; full validation green after fixes. Depends on US-001…US-006.

### Patterns & Memory
- Update `.claude/rules/llm-drafter.md` with the durable provider-seam convention (registry + capability flags + `_<vendor>_client.py`); note the registry-validated-`str` deviation from the `Literal` config convention and why. Depends on Quality Gate. **Worker-writability caveat:** `.claude/rules/` edits are orchestrator-only in Ralph worktrees (see memory `ralph-worker-claude-dir-perms`) — US-006 + P&M `.claude/` edits land via the orchestrator, not a worker.

## Open notes for implementation

- `AnthropicClientProtocol` stays public + Anthropic-named (DEC-012); don't rename it to a "neutral" name — it genuinely describes the Anthropic `.messages` surface.
- Watch the CLI monkeypatch migration (DEC-006): tests patching `gen_mod._make_anthropic_client` must move to the registry/provider seam; a missed one silently makes a "real client" call in a test.
- The `git mv _client.py _anthropic_client.py` must keep `# pyright: ignore` confinement intact — pyright is in the validate gate.
