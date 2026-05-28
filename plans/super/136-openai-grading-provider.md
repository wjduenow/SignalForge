# Super Plan — #136: OpenAI model support for grading

## Meta

- **Ticket:** https://github.com/wjduenow/SignalForge/issues/136
- **Parent epic:** #134 (pluggable LLM provider for grading — OpenAI/Gemini). Milestone v0.3.
- **Depends on:** #135 (provider-neutral LLM seam) — **merged**, PR #148.
- **Sibling:** #137 (Gemini grading; mirrors the same shape).
- **Phase:** published (awaiting approval — PR #152)
- **Branch:** `feature/136-openai-grading`
- **PR:** https://github.com/wjduenow/SignalForge/pull/152

## What / Why

Register **OpenAI** as the second LLM provider behind the #135 provider-neutral seam, selectable via `grade.provider: openai` in `signalforge.yml`. Validates that the seam is genuinely vendor-pluggable — Anthropic stays byte-identical (the regression floor), and a real-world non-caching vendor wires in cleanly through the same `LLMProvider` ABC + registry that #135 established. Closes the v0.3 epic's "OpenAI grading" deliverable; #137 (Gemini) ships next as the third provider with the same pattern.

The seam already validates the abstract case: `tests/grade/test_provider_neutrality.py` registers `FakeNoCacheProvider` (both capability flags `False`) and proves `grade_artifacts` end-to-end. **OpenAI is the same shape with a real SDK** — `supports_prompt_caching=False`, `supports_token_count=False` (no Anthropic-equivalent count_tokens API; OpenAI offers a one-shot completions call). The work is mechanical SDK plumbing + a confinement shim + tests, with one genuine design choice — how `make_client()` reconciles OpenAI's `client.chat.completions.create(...)` with the orchestrator's hard-coded `client.messages.create(...)` protocol.

## Discovery findings

### The seam after #135 (all in `src/signalforge/llm/`)

- **`providers.py`** — `LLMProvider` ABC + `register_provider(provider)` / `provider_for(name)` registry + `AnthropicProvider` (registered at import time). Capability flags: `name`, `supports_prompt_caching`, `supports_token_count`. Six abstract methods: `make_client`, `build_create_kwargs`, `build_count_tokens_kwargs`, `extract_text_blocks`, `extract_usage`, `classify_exception`.
- **`client.py` `call_llm(*, system, cached_block, dynamic_block, model, max_tokens, cache_ttl="5m", prompt_version, max_retries_*, provider="anthropic", client=None) -> LLMResult`** — generic orchestrator. Retry loop dispatches on `ExceptionCategory` (AUTH / RATE_LIMIT / SERVER_ERROR / CONNECTION / NO_RETRY). Pre-send token-count gate is **gated on `supports_token_count`** — skipped entirely when `False`. Cache marker + dual-zero WARNING gated on `supports_prompt_caching`.
- **`_anthropic_client.py`** — sole home of every Anthropic `# pyright: ignore`. Lazy SDK import inside `_make_anthropic_client` and `_load_anthropic_exception_classes`. Confinement enforced by `tests/test_audit_completeness.py` Scan 3 (`anthropic.Anthropic(...)` constructions allowed only here).
- **`pricing.py`** — `_PRICES_MUTABLE` dict of `ModelPricing(input_per_mtok, output_per_mtok, cache_write_5m_per_mtok, cache_read_per_mtok)`. Three Anthropic SKUs (sonnet-4-6, opus-4-7, haiku-4-5). `PRICE_TABLE_VERSION = "2026-05-11"`. Consumed by `cli/_estimate.py:457,473`.

### Grade / draft consumption sites

- `src/signalforge/grade/engine.py:306-310` — `call_llm(system=_SYSTEM_PROMPT, cached_block=rubric_block, dynamic_block=dynamic_block, ..., provider=config.provider, client=client)`.
- `src/signalforge/grade/config.py:127` — `provider: str = "anthropic"` with the `_provider_registered` validator (lines 209–230) calling `provider_for(v)`.
- `src/signalforge/draft/config.py:113` — same shape, parity-mirrored.
- `src/signalforge/draft/schema.py:197` — `call_llm(..., provider=config.provider, ...)`.

### `--estimate` cost-preview path (`src/signalforge/cli/_estimate.py`)

- **Anthropic-coupled today.** Threads a single `anthropic_client: AnthropicClientProtocol` through `_count_draft_tokens` (calls `anthropic_client.messages.count_tokens(...)`) and the grader-side equivalent.
- **`pricing_grade = _pricing.lookup(grade_config.model)`** (line 473) — raises `EstimateUnknownModelError` on unknown model.
- **Scope answer:** SD-3 says **fully wire OpenAI to `--estimate`** — generalise the estimate path's token-counting to be provider-aware (Anthropic = `messages.count_tokens`; OpenAI = `tiktoken` local), add OpenAI pricing entries, keep Anthropic byte-identical.

### Provider-neutrality test infrastructure (already in place)

- `tests/llm/_fake.py` — `FakeAnthropicClient` with `expect_count_tokens` / `expect_messages_create` FIFO queues + `assert_all_expectations_met()`. This is the shape to mirror as `FakeOpenAIClient`.
- `tests/llm/_fake_provider.py` — `FakeNoCacheProvider` (synthetic no-cache provider) + `FakeNoCacheClient` with `create_calls` inspector + a `count_tokens` that raises if invoked. Already proves the no-cache path works end-to-end.
- `tests/grade/test_provider_neutrality.py` — three tests: AC #1 registry validation, AC #2 GradeConfig validator accepts a registered provider, AC #3 `grade_artifacts` drives the no-cache provider end-to-end and verifies cache_*=0 in audit JSONL + 16-hex blake2b-8 reproducibility hashes + sidecar round-trip + **no dual-zero WARNING**.
- AST confinement precedents: `tests/test_audit_completeness.py` Scan 3 for `anthropic.Anthropic(...)`; `tests/warehouse/test_snowflake_client_confinement.py` for the line-based `# type: ignore` confinement (Snowflake-shaped).

### `openai` SDK availability

- **Not a declared dependency.** `pyproject.toml:19` pins `anthropic>=0.50,<2.0`; no `openai` entry. (`openai 1.3.7` happens to be in this env but isn't required by SignalForge.) Need a new optional extra `[openai]` mirroring `[snowflake]`, with a lazy SDK import inside `_openai_client.py` (the same pattern as Snowflake — `tests/warehouse/test_snowflake_client_confinement.py` enforces it).

## Scoping decisions (Phase 1)

- **SD-1 — OpenAI API surface: Chat Completions.** `client.chat.completions.create(...)`. Stable, universal, simplest. Read text from `response.choices[0].message.content`. Pin `openai>=1.40` (covers stable Chat Completions + structured outputs). Responses API deferred.
- **SD-2 — No cross-validation of provider/model.** Keep `GradeConfig.model` a free string. Model-naming evolves fast (gpt-4o → gpt-4.1 → …); a hard allowlist rots. Registry validates the provider name only; an obviously-wrong model errors at API call time with a typed `LLMError`.
- **SD-3 — Fully wire OpenAI to `--estimate`.** Generalise the estimate path's token-counting to be provider-aware: Anthropic keeps `messages.count_tokens`; OpenAI uses `tiktoken` (local BPE counter, no API call). Add OpenAI pricing SKUs. Anthropic estimate bytes stay identical.
- **SD-4 — Default judge model: `gpt-4o`.** Used in docs examples + the gated live test. `GradeConfig.model` default stays `claude-sonnet-4-6` — operators choosing OpenAI explicitly set `grade.model: gpt-4o`.

## Architecture review (Phase 2)

| Area | Rating | Finding |
|---|---|---|
| **SDK confinement** | concern | Every `# pyright: ignore` / `import openai` must live in `_openai_client.py`. Extend Scan 3 in `tests/test_audit_completeness.py` to also exclude `_openai_client.py` for `openai.OpenAI(...)` constructions. Mirrors Anthropic precedent exactly. **Resolution:** add the new AST scan + add `openai` to the per-SDK exclusion lists. |
| **`.messages` adapter shape** | concern | OpenAI SDK exposes `client.chat.completions.create(...)`, NOT `client.messages.create(...)`. The orchestrator hard-calls `llm_client.messages.create(**kwargs)`. **Resolution:** `OpenAIProvider.make_client()` returns a thin adapter object whose `.messages.create(**kwargs)` delegates to the underlying `openai.OpenAI().chat.completions.create(**kwargs)`. `messages.count_tokens` is never called (`supports_token_count=False`); the adapter raises `NotImplementedError` on that path defensively. |
| **`build_count_tokens_kwargs` ABC contract** | pass | A provider with `supports_token_count=False` never sees the method called by the orchestrator. Precedent: `FakeNoCacheProvider.build_count_tokens_kwargs` raises `NotImplementedError`. Match that. |
| **Anthropic byte-identity** | concern | The estimate refactor (SD-3) threads a provider strategy through `_count_draft_tokens` + the grader-side equivalent. **Anthropic estimate output must stay byte-identical.** Resolution: extract the SDK-call into a per-strategy `count_input_tokens(client, ...) -> int` method; Anthropic impl is the existing `client.messages.count_tokens(...)` call verbatim. Pin a snapshot test on Anthropic estimate stdout before refactor + verify after. |
| **`tiktoken` dependency** | concern | Adding a local-tokeniser dependency for OpenAI estimate. `tiktoken` is OpenAI-published, MIT, no native build (wheels for cpython 3.11–3.13). **Resolution:** add to the `[openai]` optional extra, lazy-import inside `_openai_client.py`. `OpenAIProvider.count_input_tokens` uses tiktoken's `encoding_for_model` with a graceful fallback to `cl100k_base` for unknown model ids. |
| **Pricing table churn** | pass | `_PRICES_MUTABLE` gains OpenAI SKUs (at minimum `gpt-4o`); `PRICE_TABLE_VERSION` bumps. Additive change. Cache fields set to `0.0` (OpenAI has no equivalent cache discount). |
| **Drafter provider symmetry** | concern | #135 gave BOTH `grade.provider` and `draft.provider` a `str` field. Once `OpenAIProvider` is registered, both stages naturally accept `provider: openai`. **Decision needed (refinement):** scope #136 to "grade only" with `draft.provider: openai` documented as untested-but-permitted, OR explicitly cover both stages. The work is identical; the doc message differs. |
| **GradeEvent / drift detector** | pass | `GradeEvent.cache_creation_input_tokens` / `cache_read_input_tokens` default to 0 — already proven by `FakeNoCacheProvider` round-trip in `tests/grade/test_drift_detector.py`. No schema bump. |
| **Reproducibility hashes** | pass | `rubric_hash`, `prompt_version_template`, `criterion_prompt_hash`, `response_text_hash` are LLM-content-agnostic. OpenAI responses produce a different `response_text_hash` (different judge model output) but the same 16-hex blake2b-8 shape. |
| **Exception taxonomy** | pass | OpenAI SDK exceptions map cleanly: `AuthenticationError`/`PermissionDeniedError` → AUTH; `RateLimitError` → RATE_LIMIT; `APIConnectionError` → CONNECTION; `APIStatusError` with 5xx → SERVER_ERROR; 4xx-non-auth + anything else → NO_RETRY. Mirrors `AnthropicProvider.classify_exception`. |
| **JSON parser tolerance** | pass | `parse_grade_response` already routes through `extract_json_payload` (issue #144) which strips prose preambles. No prefill needed (OpenAI Chat Completions doesn't support assistant-turn prefill either — same constraint as `claude-sonnet-4-6`). Optional refinement: set OpenAI's `response_format={"type":"json_object"}` to enforce JSON server-side. |
| **Live gated test** | pass | Add `@pytest.mark.openai` marker; gate on `SF_RUN_OPENAI=1` + `OPENAI_API_KEY`; register in `pyproject.toml` `[tool.pytest.ini_options].markers` + add to `addopts -m 'not ...'` exclusion. Mirrors the `anthropic` marker precedent. |
| **`--estimate` parity test** | concern | Need a unit-level estimate test with `grade.provider: openai` driving a `FakeOpenAIClient` + faked-or-real tiktoken count, asserting the report renders correctly. |
| **Observability / logger gate** | pass | Lazy-format JSON logger gate (`tests/llm/test_logger_grep_gate.py`) already scans `src/signalforge/llm`. New `_openai_client.py` falls under the gate automatically. No new logger calls planned in the shim — logging stays in `client.py`. |
| **Documentation surfaces** | concern | `docs/grade-ops.md` needs an OpenAI section (config snippet, no-cache caveat, model id guidance, env var). `docs/cost-estimate-ops.md` (or wherever `--estimate` ops live) needs the tiktoken note. `CLAUDE.md` "Related projects" doesn't need a change. `.claude/rules/llm-drafter.md` adds a sub-section on the OpenAI shim + Chat Completions adapter pattern as the precedent for #137. **Resolution:** dedicated docs story. |
| **CHANGELOG** | pass | Add a `0.3.0.dev` entry under "Added" for OpenAI grading. |

**Blockers:** none. **Concerns:** 6 listed — all routed to refinement or absorbed into specific stories. No architectural blockers.

## Refinement log

### Phase 1 scoping decisions (operator-facing)

- **DEC-001 — OpenAI API surface: Chat Completions.** `client.chat.completions.create(...)` is the stable, universal surface; read text from `response.choices[0].message.content`. Pin `openai>=1.40`. Responses API deferred — no operator-visible feature in v0.3 needs it. From SD-1.
- **DEC-002 — No cross-validation of provider/model.** `GradeConfig.model` / `DraftConfig.model` stay free strings. The provider registry validates the provider name; the model id is checked at API call time. Model naming evolves too fast (gpt-4o → gpt-4.1 → next) for a hard allowlist to be worth maintaining. From SD-2.
- **DEC-003 — `--estimate` fully wired for OpenAI.** Generalise the estimate-path token-counting through a new `LLMProvider.estimate_input_tokens(model, text) -> int` ABC method. Anthropic impl calls `client.messages.count_tokens(...)` (preserves byte-identity); OpenAI impl uses `tiktoken` (local BPE, no API call). Add OpenAI pricing SKUs. From SD-3.
- **DEC-004 — Default judge model: `gpt-4o`.** Used in docs examples + gated live tests. `GradeConfig.model` / `DraftConfig.model` keep their `claude-sonnet-4-6` default — operators selecting OpenAI explicitly set the model. From SD-4.

### Phase 3 refinement decisions

- **DEC-005 — Scope both grade AND draft explicitly.** `OpenAIProvider` is global once registered (provider field on both configs is symmetric since #135). Ship both stages with tests, docs sections, and live smokes. The work is mechanical; asymmetric documentation would imply a non-existent guard. The two stages share one provider class, one shim, one set of pricing SKUs — the *test/docs surface* doubles, not the implementation.
- **DEC-006 — Server-enforce JSON via `response_format={"type":"json_object"}`.** `OpenAIProvider.build_create_kwargs` attaches the JSON-mode flag. Belt-and-braces with the existing tolerant `extract_json_payload` parser: server-side enforcement eliminates the prose-preamble drift class (mirrors issue #144's fix for `claude-sonnet-4-6`), and the parser remains the fallback if a future model strips the flag. The grade system prompt already names "JSON" so OpenAI's prompt-requirement check passes.
- **DEC-007 — Ship four OpenAI SKUs in `pricing.py`.** `gpt-4o` (default judge per DEC-004), `gpt-4o-mini` (budget tier), `gpt-4.1` (newer flagship), `gpt-4-turbo` (back-compat). Each carries `input_per_mtok` + `output_per_mtok`; cache fields are `0.0` (OpenAI has no equivalent cache discount). Bump `PRICE_TABLE_VERSION` to the ship date.
- **DEC-008 — Live gated smoke covers `grade_artifacts`, `draft_schema`, AND `--estimate`.** Three `@pytest.mark.openai` tests gated on `SF_RUN_OPENAI=1` + `OPENAI_API_KEY`: one drives end-to-end grading against the real API; one drives end-to-end drafting (honors DEC-005's both-stages scope at live level too); one runs `signalforge generate --estimate` with `grade.provider: openai` and asserts the report renders. Mirrors the maintainer-only `anthropic` marker precedent and #137's three-live-test breadth.

### Phase 2 architecture-concern resolutions

- **DEC-009 — `OpenAIProvider.make_client()` returns a thin `.messages`-shaped adapter.** OpenAI SDK exposes `client.chat.completions.create(...)`; the orchestrator hard-calls `client.messages.create(**kwargs)`. The adapter pattern: `_OpenAIClientAdapter` has a `.messages` namespace whose `.create(**kwargs)` delegates to the underlying `openai.OpenAI().chat.completions.create(**kwargs)`. `.messages.count_tokens` raises `NotImplementedError` defensively (orchestrator never calls it for a `supports_token_count=False` provider).
- **DEC-010 — `_openai_client.py` is the sole home of every OpenAI SDK ignore; add a new 9th AST scan.** Scan 3 in `tests/test_audit_completeness.py` is Anthropic-specific (`anthropic.Anthropic(...)`); adding the OpenAI confinement requires a **new** AST scan, not an extension — bumping the project tally from 8 → 9 (and to 10 once #137's Gemini scan lands; whichever vendor merges first owns the 8 → 9 bump and the second owns 9 → 10). The new scan reuses the existing `_QualifiedNameCallFinder` helper per `testing-signal.md` § "AST single-construction-seam scans must catch all three bypass patterns" (bare / import-alias / module-attribute). Excludes `_openai_client.py`; sanity check asserts ≥1 legitimate `openai.OpenAI(...)` construction lives in the shim. Companion per-file confinement test `tests/llm/test_openai_client_confinement.py` mirrors the Snowflake-shaped `# type: ignore` line scan.
- **DEC-011 — `OpenAIProvider.build_count_tokens_kwargs` raises `NotImplementedError`.** Matches `FakeNoCacheProvider.build_count_tokens_kwargs` precedent. The orchestrator never invokes it (`supports_token_count=False`), but the ABC requires the method present; raising is the honest behaviour.
- **DEC-012 — `tiktoken` lives in the `[openai]` extra, lazy-imported in the shim; dual-listed across all three dev slots.** Mirrors Snowflake's `[snowflake]` precedent verbatim per `python-build.md` § "uv-managed dev environment": `openai>=1.40,<3.0` AND `tiktoken>=0.7,<1.0` appear in **three** places in lockstep — `[project.optional-dependencies].openai` (operator install: `pip install signalforge-dbt[openai]`), `[project.optional-dependencies].dev` (pip back-compat for `pip install -e ".[dev]"`), and `[dependency-groups].dev` (uv-native, what CI uses). Missing any one slot drifts the install surfaces. `uv.lock` refreshes in the same commit. `_count_openai_tokens(model, text)` uses `tiktoken.encoding_for_model(model)` with a `cl100k_base` fallback for unknown ids.
- **DEC-013 — Anthropic estimate byte-identity is the floor.** Before the estimate refactor, capture a golden snapshot of `signalforge generate --estimate` stdout for an Anthropic-config fixture. After the refactor (DEC-003 — strategy-driven token counting), the snapshot must reproduce byte-for-byte. Pin via `tests/cli/test_estimate.py`.
- **DEC-014 — `_load_openai_exception_classes()` returns empty tuples on `ImportError`.** Mirrors `_load_anthropic_exception_classes`'s `pragma: no cover` branch exactly. If a base install ships without the `[openai]` extra, `import openai` raises and the loader returns a frozen `_OpenAIExceptionClasses` with empty tuples in every category. `OpenAIProvider.classify_exception` then routes every exception to `NO_RETRY` cleanly — the operator never gets `provider: openai` to resolve a real call (the registry validator at config load would fail first, since the registration also runs lazily), but import-time behaviour is graceful. **Refusal / content-filter symmetry note:** OpenAI returns refusals as model-generated text (e.g. "I cannot help with that") rather than via a typed exception. The grade parser's tolerant JSON extraction (issue #144) treats unparseable refusal text as `GradeOutputError(violation_type="json_parse")` → standard degrade. No Gemini-style `safety_filter → typed degrade` DEC is needed; the existing pipeline handles it.

## Detailed breakdown

Stories follow the natural ordering: dependency wiring → shim → provider strategy → fakes/tests → pricing → estimate refactor → live smokes → docs → QG → P&M. The canonical validation command (`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`) is implicit in every AC.

### US-001 — `_openai_client.py` shim + dependency + AST confinement

**Description:** Create the single shim where every `openai` SDK type ignore lives, add the optional `[openai]` extra (openai + tiktoken) in lockstep across the three pyproject slots, and add a **new 9th AST scan** confining `openai.OpenAI(...)` constructions to the shim.

**Traces to:** DEC-001, DEC-009, DEC-010, DEC-012, DEC-014.

**Files:**
- `src/signalforge/llm/_openai_client.py` (new) — `OpenAIClientProtocol`, `_OpenAIMessagesAdapter`, `_OpenAIClientAdapter`, `_make_openai_client(api_key=None)`, `_load_openai_exception_classes()` (DEC-014 empty-tuple fallback on `ImportError`), `_OpenAIExceptionClasses` frozen dataclass, `_count_openai_tokens(model, text)`. All `# pyright: ignore` / `# type: ignore` confined here.
- `pyproject.toml` — **three-slot dual listing** per DEC-012: add `openai = ["openai>=1.40,<3.0", "tiktoken>=0.7,<1.0"]` under `[project.optional-dependencies]`; append both packages to `[project.optional-dependencies].dev` AND `[dependency-groups].dev`. Regenerate `uv.lock`.
- `tests/test_audit_completeness.py` — **add a new 9th AST scan** (NOT an extension of Scan 3 — Scan 3 is Anthropic-specific) reusing `_QualifiedNameCallFinder` to detect `openai.OpenAI(...)` constructions, excluding `_openai_client.py`. Add the three-pattern planted-violation regression test (bare / import-alias / module-attribute) per `testing-signal.md` § "AST single-construction-seam scans must catch all three bypass patterns." Sanity test asserts ≥1 legitimate `openai.OpenAI(...)` in the shim.
- `tests/llm/test_openai_client_confinement.py` (new) — line-based scan rejecting `openai`-mentioning `# type: ignore` / `# pyright: ignore` outside the shim (mirrors `tests/warehouse/test_snowflake_client_confinement.py`).

**TDD:** Write the 9th scan + planted-violation test + per-file confinement test first; all should fail (no shim) → red. Add the shim with one legitimate `openai.OpenAI(...)` construction → green. Then plant each of the three bypass patterns (bare, import-alias, module-attribute) and re-run to confirm the scan catches each; revert.

**Acceptance criteria:**
- `uv sync --dev` installs both `openai` and `tiktoken` (dev group includes the extra).
- `_make_openai_client(api_key=None)` lazy-imports the SDK and returns a `_OpenAIClientAdapter`.
- `_OpenAIClientAdapter.messages.create(**kwargs)` delegates to `chat.completions.create(**kwargs)`; `.messages.count_tokens(...)` raises `NotImplementedError`.
- `_count_openai_tokens("gpt-4o", "hello world")` returns a positive int; an unknown model id falls back to `cl100k_base` without raising.
- AST Scan 3 still passes; a planted `openai.OpenAI(...)` outside the shim fails the scan.
- Line-based confinement test asserts every `openai`-tagged `# type: ignore` lives only in `_openai_client.py`.

**Done when:** Above ACs all pass; canonical validation command is green.

**Depends on:** none.

### US-002 — `OpenAIProvider` + registration + config-validator coverage

**Description:** Add `OpenAIProvider(LLMProvider)` to `providers.py`, register at import time, and pin that both `GradeConfig` and `DraftConfig` accept `provider="openai"` after registration.

**Traces to:** DEC-001, DEC-005, DEC-006, DEC-009, DEC-011.

**Files:**
- `src/signalforge/llm/providers.py` — add `OpenAIProvider` class (mirrors `AnthropicProvider` shape) with `name="openai"`, `supports_prompt_caching=False`, `supports_token_count=False`. Six ABC method impls: `make_client()` → `_make_openai_client()`; `build_create_kwargs()` → returns `{"model", "max_tokens", "messages": [{"role":"system",...},{"role":"user","content": cached_block+dynamic_block}], "response_format":{"type":"json_object"}}` (cache_marker_active / cache_ttl ignored); `build_count_tokens_kwargs()` raises `NotImplementedError`; `extract_text_blocks()` reads `response.choices[0].message.content`; `extract_usage()` reads `response.usage.{prompt_tokens, completion_tokens}` mapped to `UsageMetrics(input_tokens, output_tokens, cache_creation_input_tokens=0, cache_read_input_tokens=0)`; `classify_exception()` maps SDK exceptions via `_load_openai_exception_classes()` to `ExceptionCategory`. `register_provider(OpenAIProvider())` at module end.
- `src/signalforge/llm/__init__.py` — export `OpenAIProvider` in `__all__`.
- `tests/llm/test_providers.py` — extend (or add) tests: `provider_for("openai")` returns an `OpenAIProvider`; `UnknownProviderError("xyz")` message lists both "anthropic" and "openai"; each ABC method has a focused unit test against synthetic inputs/exceptions.
- `tests/grade/test_config.py` + `tests/draft/test_config.py` — pin that `GradeConfig(provider="openai", model="gpt-4o")` validates; `DraftConfig(provider="openai", model="gpt-4o")` validates.

**TDD:** For each ABC method, write the unit test first (e.g. `classify_exception(openai.RateLimitError(...))` returns `ExceptionCategory.RATE_LIMIT`); fill in impl until green. Cover all five `ExceptionCategory` branches (AUTH / RATE_LIMIT / SERVER_ERROR / CONNECTION / NO_RETRY) — each maps from a real `openai.*` exception class.

**Acceptance criteria:**
- `provider_for("openai")` returns an `OpenAIProvider` instance with `supports_prompt_caching=False`, `supports_token_count=False`.
- `OpenAIProvider().build_create_kwargs(...)` returns a dict containing `model`, `max_tokens`, `messages` (a list with a system role + a user role), and `response_format={"type":"json_object"}`. No `cache_control` marker anywhere.
- `OpenAIProvider().build_count_tokens_kwargs(...)` raises `NotImplementedError`.
- `OpenAIProvider().classify_exception(...)` returns the correct `ExceptionCategory` for at least one concrete SDK exception per category.
- `GradeConfig(provider="openai", model="gpt-4o")` and `DraftConfig(provider="openai", model="gpt-4o")` validate without error.
- `provider_for("xyz")` raises `UnknownProviderError` listing `("anthropic", "openai")` (order-insensitive).

**Done when:** Above ACs pass; validation green.

**Depends on:** US-001.

### US-003 — `FakeOpenAIClient` + grade end-to-end provider-neutrality test

**Description:** Build the test fake mirroring `FakeAnthropicClient`'s `expect_*` API and add an end-to-end `grade_artifacts(provider="openai")` integration test that proves cache_*=0, reproducibility hashes, and no dual-zero WARNING — the OpenAI analogue of the existing `FakeNoCacheProvider` proof.

**Traces to:** DEC-001, DEC-005, DEC-006, DEC-009, DEC-011.

**Files:**
- `tests/llm/_fake_openai.py` (new) — `FakeOpenAIUsage(prompt_tokens, completion_tokens)`, `FakeOpenAIMessage(content, role="assistant")`, `FakeOpenAIChoice(message, index=0, finish_reason="stop")`, `FakeOpenAICompletion(choices, usage, model, id, object="chat.completion")`. `_MessagesAdapter` with FIFO `_create_queue: list[_CreateExpectation]` + `create_calls: list[dict]` inspector. `FakeOpenAIClient` exposes `.messages` (delegating to `chat.completions` for parity with real SDK adapter); `expect_messages_create(matching, returns)` + `assert_all_expectations_met()`.
- `tests/grade/test_provider_neutrality_openai.py` (new) — three tests mirroring the no-cache provider neutrality suite: (1) `provider_for("openai")` resolves and capability flags are False/False; (2) `GradeConfig(provider="openai", model="gpt-4o")` validates; (3) `grade_artifacts(..., provider="openai", client=FakeOpenAIClient())` drives the engine end-to-end against canned JSON judge responses and asserts: JSONL `cache_creation_input_tokens == 0` and `cache_read_input_tokens == 0`, 16-hex blake2b-8 reproducibility hashes, sidecar round-trips, no dual-zero cache-anomaly WARNING in caplog.

**TDD:** Write the end-to-end test first; it fails because no `FakeOpenAIClient` exists. Build the fake until the test passes. Then plant edge cases (Exception in `returns`, mismatched `matching`) and confirm the fake's `assert_all_expectations_met()` catches under-consumption.

**Acceptance criteria:**
- `FakeOpenAIClient` exposes `.messages.create(**kwargs)` consuming one matching expectation from the FIFO queue; raises `AssertionError` on no-match.
- The end-to-end test passes: `grade_artifacts(provider="openai", client=FakeOpenAIClient())` produces a valid `GradingReport`, JSONL audit, and sidecar.
- `caplog` contains no `"cache marker no-op"` WARNING in the OpenAI path.
- `assert_all_expectations_met()` after the run reports zero un-consumed expectations.

**Done when:** Above ACs pass; validation green.

**Depends on:** US-002.

### US-004 — Pricing entries (`gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4-turbo`)

**Description:** Add four OpenAI SKUs to `_PRICES_MUTABLE` in `pricing.py`; bump `PRICE_TABLE_VERSION`.

**Traces to:** DEC-003, DEC-004, DEC-007.

**Files:**
- `src/signalforge/llm/pricing.py` — extend `_PRICES_MUTABLE` with the four SKUs (input/output per-Mtok USD from OpenAI's public price page at PR-prep time; cache fields = 0.0). Bump `PRICE_TABLE_VERSION` to today (e.g. `"2026-05-27"`).
- `tests/llm/test_pricing.py` — assert `lookup("gpt-4o")`, `lookup("gpt-4o-mini")`, `lookup("gpt-4.1")`, `lookup("gpt-4-turbo")` each return a non-zero `input_per_mtok` and `output_per_mtok` and zero cache fields. Assert `lookup("gpt-9-unicorn")` still raises `EstimateUnknownModelError`.

**TDD:** Pricing-lookup tests first (red); add SKU entries (green); pin the version bump.

**Acceptance criteria:**
- All four OpenAI SKUs resolve via `lookup()` with non-zero input/output rates and zero cache rates.
- `PRICE_TABLE_VERSION` bumped.
- Unknown model still raises.

**Done when:** Above ACs pass; validation green.

**Depends on:** none (parallel-safe).

### US-005 — `--estimate` provider-aware token counting

**Description:** Generalise the estimate path's token-counting through a new `LLMProvider.estimate_input_tokens(model, text) -> int` abstract method. Anthropic impl preserves byte-identity (calls existing SDK `count_tokens`); OpenAI impl uses tiktoken; `FakeNoCacheProvider` impl returns a constant. Refactor `cli/_estimate.py` to thread the strategy.

**Traces to:** DEC-003, DEC-007, DEC-012, DEC-013.

**Files:**
- `src/signalforge/llm/providers.py` — add `LLMProvider.estimate_input_tokens(model, text) -> int` abstract method. Implement on `AnthropicProvider` (delegates to its SDK; reuses the client construction path used by `_estimate`); implement on `OpenAIProvider` (delegates to `_count_openai_tokens`).
- `src/signalforge/cli/_estimate.py` — refactor `_count_draft_tokens` and the grader-side equivalent to dispatch through `provider_for(config.provider).estimate_input_tokens(model, text)`. Remove the hard-coded `anthropic_client.messages.count_tokens(...)` callsite; thread the resolved client (or `None` for clients the strategy builds itself) through the strategy.
- `tests/llm/_fake_provider.py` — add `FakeNoCacheProvider.estimate_input_tokens(model, text) -> int` returning a constant (e.g. `len(text.split())`) so existing neutrality tests still pass.
- `tests/cli/test_estimate.py` — (a) pin Anthropic byte-identity: capture a golden snapshot of estimate stdout for an Anthropic-config fixture BEFORE the refactor in the same commit (via a new fixture) and assert it after; (b) add a test driving `--estimate` with `grade.provider: openai` + `grade.model: gpt-4o` (and `draft.provider: openai` + `draft.model: gpt-4o`) against `FakeOpenAIClient`, asserting the report renders with non-zero token counts and a non-zero USD estimate.

**TDD:** Capture Anthropic golden first; refactor; verify identity. Then write the OpenAI estimate test; implement until green.

**Acceptance criteria:**
- `LLMProvider.estimate_input_tokens(model, text) -> int` is an abstract method on the ABC.
- `AnthropicProvider.estimate_input_tokens` reproduces the pre-refactor token count for the same input.
- `OpenAIProvider.estimate_input_tokens` returns a positive int for `gpt-4o`.
- Anthropic estimate stdout snapshot is byte-identical before and after the refactor (pinned by `tests/cli/test_estimate.py`).
- `signalforge generate --estimate` with `grade.provider: openai` produces an `EstimateReport` with non-zero grader token counts and non-zero USD figures.

**Done when:** Above ACs pass; validation green; no `--cov-fail-under` regression.

**Depends on:** US-002, US-004.

### US-006 — Live gated smoke tests (`grade_artifacts` + `draft_schema` + `--estimate`)

**Description:** Add the `openai` pytest marker, register three gated tests against the real OpenAI API (grader + drafter + estimate, per DEC-005 + DEC-008), document the env-var gate. Mirrors the `anthropic` marker precedent.

**Traces to:** DEC-001, DEC-004, DEC-005, DEC-008.

**Files:**
- `pyproject.toml` — register `"openai: real-API smoke test (requires OPENAI_API_KEY; excluded from default CI)"` under `[tool.pytest.ini_options].markers`; extend `addopts -m 'not ...'` exclusion to include `not openai`.
- `tests/grade/test_smoke_real_api_openai.py` (new) — `pytestmark = pytest.mark.openai`; env-gate `SF_RUN_OPENAI=1` + `OPENAI_API_KEY`. Drives `grade_artifacts(..., provider="openai", config=GradeConfig(model="gpt-4o", ...), client=None)` and asserts shape-only (positive scores, valid JSONL, no dual-zero WARNING).
- `tests/draft/test_smoke_real_api_openai.py` (new) — `pytestmark = pytest.mark.openai`; same env gates. Drives `draft_schema(..., provider="openai", config=DraftConfig(model="gpt-4o", ...), client=None)` against a small in-test manifest fixture; asserts `CandidateSchema` validates + `LLMResponseEvent` JSONL is written with `cache_*_input_tokens == 0`. Honours DEC-005's "scope both stages" commitment that US-003's grade-only neutrality test alone doesn't cover live-side.
- `tests/cli/test_e2e_estimate_openai.py` (new) — `pytestmark = pytest.mark.openai`; same env gates. Runs `signalforge generate --estimate ...` with `grade.provider: openai` + `grade.model: gpt-4o`; asserts the rendered report includes a non-zero grader USD estimate, exit code 0, no traceback.
- `CONTRIBUTING.md` (or `docs/cost-estimate-ops.md`) — document the `SF_RUN_OPENAI=1` + `OPENAI_API_KEY` gating env vars next to the existing Anthropic equivalents.

**TDD:** Stub the three test files with the env-skip plumbing first; ensure the default suite still passes (marker is excluded). Run `uv run pytest -m openai --no-cov` manually with credentials to validate against the live API once.

**Acceptance criteria:**
- `uv run pytest` excludes the new tests by default (marker not in default set).
- `uv run pytest -m openai --no-cov` with `SF_RUN_OPENAI=1` + `OPENAI_API_KEY` runs all three tests; without those env vars, each skips with a clear reason naming the missing var.
- Grade live smoke against `gpt-4o` produces a valid `GradingReport` (shape assertions only — no value pinning).
- Draft live smoke against `gpt-4o` produces a `CandidateSchema` that validates and an `LLMResponseEvent` JSONL row with zero cache tokens.
- Live `--estimate` produces non-zero token counts and a non-zero USD estimate; exit 0.

**Done when:** Above ACs pass; default validation still green (gated tests excluded); maintainer has run the live smokes once.

**Depends on:** US-002, US-005.

### US-007 — Documentation surfaces

**Description:** Update every documentation surface that names the available providers / `--estimate` flow / shim convention.

**Traces to:** DEC-001 through DEC-014 (collectively).

**Files:**
- `docs/grade-ops.md` — add an "OpenAI provider" section: config snippet (`grade.provider: openai`, `grade.model: gpt-4o`), `OPENAI_API_KEY` env var, no-prompt-cache caveat, link to live smoke gating.
- `docs/draft-ops.md` — add equivalent section for `draft.provider: openai`.
- `docs/cost-estimate-ops.md` (or wherever `--estimate` ops live; create if absent) — add tiktoken note + the `[openai]` extra requirement.
- `.claude/rules/llm-drafter.md` — extend the "Provider-neutral seam" section with the OpenAIProvider shim notes (Chat Completions adapter pattern, `response_format=json_object`, capability flags False/False). Becomes the canonical precedent for #137 (Gemini).
- `CHANGELOG.md` — under `0.3.0.dev` "Added": `OpenAI as a grading + drafting provider (#136). Set grade.provider: openai or draft.provider: openai in signalforge.yml; requires the [openai] install extra and OPENAI_API_KEY.`
- `README.md` — if the README enumerates supported providers, extend the list.

**TDD:** N/A (docs only).

**Acceptance criteria:**
- `docs/grade-ops.md` carries an OpenAI section with a copy-pasteable config example.
- `docs/draft-ops.md` carries the equivalent.
- The estimate ops doc names tiktoken + the `[openai]` extra.
- `.claude/rules/llm-drafter.md` carries the OpenAIProvider shim sub-section.
- `CHANGELOG.md` has the new entry.
- `uv run --only-group docs mkdocs build` succeeds (the `docs-build` CI job mirrors this).

**Done when:** Above ACs pass; the `docs-build` job is green locally.

**Depends on:** US-001, US-002, US-005 (so the docs describe a working surface).

### US-008 — Quality Gate

**Description:** Multi-pass code review across the full changeset, CodeRabbit if available, full validation including gated markers.

**Files:** wherever the prior stories' bugs land.

**Acceptance criteria:**
- `/code-review` run 4 times; every real bug surfaced is fixed (false positives recorded with rationale).
- CodeRabbit review run if accessible.
- `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` passes.
- `uv run pytest -m anthropic --no-cov` passes (proves Anthropic byte-identity in the estimate refactor end-to-end).
- `uv run pytest -m openai --no-cov` passes locally with credentials.
- `uv run pytest -m wheel_smoke --no-cov` passes (new `[openai]` extra doesn't break wheel build).
- Coverage stays at or above the current threshold.

**Done when:** All gates green.

**Depends on:** US-001 through US-007.

### US-009 — Patterns & Memory

**Description:** Capture durable lessons in `.claude/rules/llm-drafter.md` (the canonical precedent for #137) and add memory entries for any non-obvious traps surfaced during implementation.

**Files:**
- `.claude/rules/llm-drafter.md` — refine the OpenAI shim sub-section if implementation surfaced anything unexpected (likely candidates: the `.messages` adapter wrap pattern, tiktoken model-id fallback strategy, `response_format=json_object` interaction with the tolerant JSON parser).
- `~/.claude/projects/-home-wesd-Projects-SignalForge/memory/` — one memory file per non-obvious trap, indexed in `MEMORY.md`.

**Acceptance criteria:**
- `.claude/rules/llm-drafter.md` has a concrete OpenAI sub-section a future contributor can mirror for #137.
- Memory entries (if any) follow the user/feedback/project/reference taxonomy and link related entries via `[[name]]`.

**Done when:** Above ACs pass.

**Depends on:** US-008.

## Worker-writability routing

Per `~/.claude/projects/-home-wesd-Projects-SignalForge/memory/ralph-worker-claude-dir-perms.md`: **Ralph workers cannot Write under `.claude/` in worktrees** — only the orchestrator can. The story split honours this:

- US-001 through US-007 + US-008 (Quality Gate) touch only worker-writable paths (`src/`, `tests/`, `pyproject.toml`, `docs/`, `CHANGELOG.md`, `README.md`, `uv.lock`).
- **US-009 (Patterns & Memory) is orchestrator-only** because it edits `.claude/rules/llm-drafter.md` (and potentially `.claude/rules/grade-layer.md`). If a worker is dispatched against US-009 the bead fails with a write-denied error; route it to the orchestrator.

This is the same routing convention `#137`'s plan codifies; mirroring it here so the rule lands durably for both #136 and #137.

## Open notes for implementation

Pragmatic verification items that depend on the installed SDK version at implementation time — flag during US-001 / US-002 / US-005, not codified as DECs because the SDK surface evolves faster than the plan:

- **Verify `openai` SDK exception class names + status-code attrs against the installed version.** DEC-009's exception → `ExceptionCategory` mapping (`openai.AuthenticationError`/`PermissionDeniedError` → AUTH; `RateLimitError` → RATE_LIMIT; `APIConnectionError` → CONNECTION; `APIStatusError` 5xx → SERVER_ERROR; 4xx-non-auth + everything else → NO_RETRY) is the shape; the precise class names (`InternalServerError` vs `APIStatusError`-with-status-code; `Timeout` vs `APITimeoutError`) may need a tiny adjustment for `openai>=1.40`. The unit tests in US-002 drive this — write the tests against the installed SDK, then implement to pass.
- **Confirm the `.messages.create` façade adapts cleanly to `chat.completions.create`.** US-001 / US-002: the orchestrator hard-calls `llm_client.messages.create(**kwargs)`. `_OpenAIClientAdapter.messages.create(**kwargs)` delegates to `self._raw.chat.completions.create(**kwargs)`. The kwargs dict shape is OpenAI-native (`model`, `max_tokens`, `messages` list of `{role, content}` dicts, `response_format`). Verify there's no per-call mutation needed; if `extra_headers` is passed (it shouldn't be — capability-gated off), drop it at the adapter rather than at the provider.
- **`tiktoken` model-id fallback table.** `tiktoken.encoding_for_model("gpt-4o")` works for the four planned SKUs at SDK version pin time; if a model id isn't recognised, fall through to `tiktoken.get_encoding("cl100k_base")`. Don't raise on unknown — `--estimate` is a calibration signal, not a billing guarantee (mirrors the planner-estimate caveats in `warehouse-adapters.md` § "estimate_query_bytes graduation"). Log one INFO line per unknown-model fallback so the operator knows the count is approximate.
- **`response_format={"type":"json_object"}` requires "json" in the prompt.** The grade system prompt already names JSON; verify by reading `signalforge.grade.prompts._SYSTEM_PROMPT` during US-002. The drafter system prompt likewise. If either ever drops the word "json", the OpenAI request will fail server-side with a `BadRequestError` — pin a unit test asserting both prompts contain `"json"` (case-insensitive) to catch future drift.
- **`pricing.lookup` returns zero cache fields for the four OpenAI SKUs.** US-004: assert this in `tests/llm/test_pricing.py`. `cli/_estimate.py:489` lines (cache cost math) should produce 0.0 contributions without raising — verify the multiplication doesn't break on a zero `cache_write_5m_per_mtok`.
- **Anthropic byte-identity snapshot — capture in the SAME COMMIT as the refactor.** US-005: the `tests/cli/test_estimate.py` golden file must be added in the same PR commit that introduces the strategy method, or git history can't prove byte-identity. Capture stdout pre-refactor on the feature branch's first commit; refactor on the second; the test compares against the captured golden. If the snapshot changes during the refactor, the refactor is wrong.

## Beads manifest

- **Epic:** `bd_1-scaffolding-4tw` — `#136 epic: OpenAI grading provider` (P2, external-ref `gh-136`)
- **Tasks** (dep edges per plan's "Depends on:" lines; all P2):
  - `.1` US-001 — `_openai_client.py` shim + `[openai]` extra + 9th AST scan — **READY** (no deps)
  - `.2` US-002 — `OpenAIProvider` + registration + config-validator coverage — blocked by `.1`
  - `.3` US-003 — `FakeOpenAIClient` + grade end-to-end provider-neutrality test — blocked by `.2`
  - `.4` US-004 — Pricing entries (gpt-4o, gpt-4o-mini, gpt-4.1, gpt-4-turbo) — **READY** (parallel-safe; no deps)
  - `.5` US-005 — `--estimate` provider-aware token counting — blocked by `.2`, `.4`
  - `.6` US-006 — Live gated smoke tests (grade + draft + `--estimate`) — blocked by `.2`, `.5`
  - `.7` US-007 — Documentation surfaces — blocked by `.1`, `.2`, `.5`
  - `.8` Quality Gate — code-review ×4 + CodeRabbit + full validation + `wheel_smoke` — blocked by `.1`–`.7`
  - `.9` Patterns & Memory (orchestrator-only — edits `.claude/rules/`) — blocked by `.8`
- **Cross-epic gate (downstream):** `bd_1-scaffolding-41a` (sentinel for #137 US-007) **`DEPENDS ON .9`**. When this epic completes, US-009 closes → sentinel becomes close-eligible → operator closes sentinel after PR #152 merges to `dev` → #137 US-007 unblocks. See #137 plan DEC-019.
- **Parallel-safe entry points at devolve time:** `.1` (shim/extra/AST) and `.4` (pricing). They touch disjoint files — `.1` edits `_openai_client.py` / `pyproject.toml` / `test_audit_completeness.py`; `.4` edits `pricing.py` / `test_pricing.py` — so `ralph-serialize-shared-registry-beads` does NOT apply. Run them concurrently.
- **Sessions:** 2 (initial plan; devolve + #137 cross-review revisions)

