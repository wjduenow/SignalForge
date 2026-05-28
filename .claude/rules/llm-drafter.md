# LLM drafter (single SDK seam + fail-closed response audit)

Established by issue #5 (LLM draft pipeline). Apply to every module under `signalforge.llm` and `signalforge.draft`, and to any new code that issues an Anthropic API call, parses an LLM response into typed objects, or writes a response-audit record.

The drafter sits between the safety layer (#4) and the prune layer (#6). It enforces "explainable diffs" at the LLM input/output boundary: every Anthropic call goes through one seam with a known retry taxonomy; every response gets a durable receipt; bad LLM output never leaves the parser as a partial artifact.

## One SDK seam — `signalforge.llm._anthropic_client` confines every `# pyright: ignore` (DEC-012; renamed by #135)

Every `# pyright: ignore[...]` and `# type: ignore[...]` comment for the Anthropic SDK lives in **one file**: `src/signalforge/llm/_anthropic_client.py` (renamed from `_client.py` by #135 when the seam went provider-neutral — each vendor now gets a `_<vendor>_client.py` sibling). The shim exposes `AnthropicClientProtocol` (`@runtime_checkable`) duck-typed at exactly the surface the orchestrator's `AnthropicProvider` consumes (`messages.create`, `messages.count_tokens`); both `anthropic.Anthropic` and `tests/llm/_fake.py::FakeAnthropicClient` satisfy it. The `import anthropic` is also confined here (lazy, inside `_load_anthropic_exception_classes` and `_make_anthropic_client`) so the rest of the layer doesn't pay the import cost. The generic orchestrator `signalforge.llm.client.call_llm` carries NO vendor SDK type or ignore — it types the resolved client against a neutral `_LLMClientProtocol` (#135 DEC-012).

**`AnthropicClientProtocol` is public (issue #44).** Re-exported from `signalforge.llm.__init__`; the `client` kwarg on `signalforge.draft.draft_schema` and `signalforge.grade.grade_artifacts` stays typed against it (Anthropic is the default injection surface; a non-Anthropic provider builds its own client and ignores the kwarg). Downstream callers wiring a custom Anthropic shim should type-annotate against the public name. The SDK-ignore confinement contract is unchanged — every `# pyright: ignore` for the Anthropic SDK still lives only in `_anthropic_client.py`. `_AnthropicMessagesProtocol`, `_AnthropicExceptionClasses`, `_load_anthropic_exception_classes`, and `_make_anthropic_client` remain private.

## Provider-neutral seam — generic orchestrator + per-provider strategy (#135)

`call_llm(*, system, cached_block, dynamic_block, model, max_tokens, cache_ttl="5m", prompt_version, max_retries_*, provider="anthropic", client=None) -> LLMResult` (renamed from `call_anthropic` by #135, which dropped the old name) is the single shared seam for **both** the drafter and grader. It owns the generic machinery — retry loop + backoff (`2**attempt*_rand_uniform(0.75,1.25)`), per-class budgets, WARNING/INFO logs, the min/cap token validation, and `LLMResult` assembly — and dispatches the vendor-specific bits to a provider strategy resolved from a registry. When `client is None`, `call_llm` builds it via `strategy.make_client()` (DEC-006 — client construction lives at the seam, not the CLI).

- **`LLMProvider` ABC + registry** in `signalforge.llm.providers`: `register_provider(provider)` / `provider_for(name) -> LLMProvider`; unknown name → `UnknownProviderError(LLMError)` listing available keys (CLI tier 2). A provider supplies `make_client`, `build_create_kwargs`, `build_count_tokens_kwargs`, `extract_text_blocks`, `extract_usage` (→ `UsageMetrics`), `classify_exception` (→ `ExceptionCategory`), `estimate_input_tokens(model, text, *, system="", client=None) -> int` (#136 US-005; powers `--estimate`), plus capability flags `supports_prompt_caching` / `supports_token_count`. Wiring a new provider = that class + `register_provider` + a config enum value. Registered in v0.3: `AnthropicProvider` (`name="anthropic"`, both flags `True`); `OpenAIProvider` (`name="openai"`, both flags `False`, #136).
- **Neutral value objects:** `UsageMetrics` + the `ExceptionCategory` enum (`AUTH`, `RATE_LIMIT`, `SERVER_ERROR`, `CONNECTION`, `NO_RETRY`) keep the orchestrator off vendor-shaped dicts.
- **Capability-gated behaviour (DEC-008):** `supports_prompt_caching=False` ⇒ no `cache_control` marker, no `extended-cache-ttl` beta header, 0 cache tokens, no dual-zero anomaly WARNING. `supports_token_count=False` ⇒ skip the pre-send count gate (no pre-send `LLMCacheTooLargeError`). Anthropic sets both `True`, so its emitted bytes/control flow are unchanged — the byte-identity gate (fixtures + prompt-cache snapshot + drift detectors) is the regression guard.
- **`provider` config field (DEC-007):** `DraftConfig.provider` (`llm:` block) and `GradeConfig.provider` (`grade:` block), both registry-validated `str` defaulting to `"anthropic"` — **deliberately NOT a `Literal`** (a registry is a plugin point that grows; #136/#137 register a provider instead of editing a Literal in two configs). The validator raises `UnknownProviderError` (an `LLMError`, so Pydantic v2 does NOT wrap it into `ValidationError` — it propagates raw with the available-keys remediation).

**Gate the cache marker on BOTH capability flags, not just `supports_prompt_caching` (#135 QG lesson).** `call_llm` sets `cache_marker_active = supports_prompt_caching AND supports_token_count`. The pre-send count gate is what enforces the sub-minimum drop + the 8000-token oversize cap; attaching a `cache_control` marker without that gate having run would send an *unvalidated* marker (a sub-minimum block silently no-ops the marker — paying the input premium with no discount; an oversize block bypasses `LLMCacheTooLargeError`). Anthropic is `True/True` so the default path is unaffected, but a future provider that caches yet has no token-count API (`True/False`) must degrade to no-caching rather than send an unguarded marker. A new provider's capability flags are load-bearing — set them honestly, and don't assume "supports caching" alone is sufficient to attach a marker.

When a new vendor lands (#137 Gemini next), add a `_<vendor>_client.py` shim + a `LLMProvider` subclass + `register_provider`; don't pool SDK ignores into a generic util module, and don't reach into `call_llm` — extend via the strategy.

### OpenAI provider shape (#136 — the second concrete provider, the no-cache precedent)

`OpenAIProvider` ships under `provider="openai"` for both stages; the shim at `src/signalforge/llm/_openai_client.py` confines every `# pyright: ignore` / `# type: ignore` for the `openai` SDK (DEC-012 of #5 generalised). Three load-bearing patterns established by #136 that the next no-cache vendor should mirror:

1. **`.messages.create` façade adapter.** The orchestrator hard-calls `llm_client.messages.create(**kwargs)`, but OpenAI's SDK exposes `client.chat.completions.create(...)`. `_OpenAIClientAdapter.messages` is a `SimpleNamespace` instance whose `.create` callable delegates to `chat.completions.create(**kwargs)` and whose `.count_tokens` raises `NotImplementedError` defensively (orchestrator never calls it when `supports_token_count=False`, but the protocol surface is uniform). Any vendor whose SDK uses a different call shape gets the same shim adaptation rather than a special-case branch in `call_llm`.
2. **`response_format={"type":"json_object"}` belt-and-braces with the tolerant JSON parser (DEC-006 of #136).** `OpenAIProvider.build_create_kwargs` attaches the JSON-mode flag at every call. The grade + draft system prompts already contain "json" (case-insensitive), satisfying OpenAI's prompt-requirement check. The tolerant `extract_json_payload` parser (issue #144) remains the fallback if a future model strips the flag; server-side enforcement eliminates the prose-preamble drift class entirely for the providers that support it. **A future vendor with an equivalent JSON-mode (e.g. Gemini's `response_mime_type="application/json"`) MUST set it for the same reason — don't lean on the parser alone when the API exposes a server-side gate.**
3. **`tiktoken` model-id fallback for `--estimate` (DEC-012 of #136).** `_count_openai_tokens(model, text)` calls `tiktoken.encoding_for_model(model)` inside a try/except, falling back to `tiktoken.get_encoding("cl100k_base")` on `KeyError` for unknown ids (newer model SKUs released after the installed `tiktoken` ship out of the registry). **Don't raise on unknown id** — `--estimate` is a calibration signal, not a billing guarantee (mirrors the `EXPLAIN`-based planner-estimate caveat in `warehouse-adapters.md`).

### `estimate_input_tokens(*, system=...)` — separate the system envelope to preserve real-API byte-identity (#136 US-005 / DEC-013, refined by US-008 QG)

The `--estimate` path generalised in US-005 via `LLMProvider.estimate_input_tokens(model, text, *, system="", client=None)`. The `system` parameter is keyword-only with an empty default, but **threading it as its own kwarg is load-bearing for Anthropic byte-identity:**

- **AnthropicProvider** passes `system=system` to `messages.count_tokens(...)` when non-empty. The pre-refactor inline call did the same — and Anthropic's server-side tokenizer applies its own system-envelope tokens to that block. Dropping the kwarg (concatenating system into the user-content text) silently under-reports real-API counts by the system-envelope size, while a fake-driven byte-identity snapshot still passes because the fake returns canned `input_tokens` regardless of kwargs. **Lesson — fake-driven byte-identity tests pin rendered-output identity, NOT real-API call shape.** If a refactor changes the SDK call shape, the snapshot won't catch it; a real-API `@pytest.mark.anthropic` test against `--estimate` (or an explicit kwargs-shape assertion on the fake) is the only way. This is the same "snapshot/sqlglot tier vs live tier" gap documented in `warehouse-adapters.md` for the Snowflake compiler.
- **OpenAIProvider** concatenates `system + text` before `tiktoken` — there's no system-envelope distinction at the BPE level, so every token contributes to the same total. Matches what OpenAI's chat-completion endpoint bills.
- **Grade-side estimate (`_count_grade_criterion_tokens`) deliberately double-counts the rubric.** The pre-refactor shape passed `system=system_and_rubric` AND embedded `system_and_rubric` inside the cached user-content block, so the rubric was counted twice. This was already the behaviour; preserving byte-identity required reproducing it. **Don't "fix" this double-count in a future tidy-pass — it's the pre-refactor shape, and a quiet repair would silently shift estimate figures by ~the rubric size.** A real future cleanup would need a tied snapshot regeneration + operator-visible "estimate calibration changed" CHANGELOG entry.

A new provider added under #137+ MUST implement `estimate_input_tokens` (it's `@abstractmethod`; missing impls fail at instantiation time). The implementation can ignore `system` (FakeNoCacheProvider stub concatenates for word-count; `_DummyProvider` returns `0`); but if the vendor's API distinguishes system from user tokens (like Anthropic), thread it separately or the same drift surfaces.

## Module-level `_sleep` / `_rand_uniform` aliases (DEC-004)

`signalforge.llm.client` declares `_sleep = time.sleep` and `_rand_uniform = random.uniform` at module scope so tests can reassign to deterministic stand-ins without monkey-patching `time.sleep` globally (which would break pytest's own timeouts and any other concurrent test).

The retry taxonomy is the full clauditor surface: 429×3, 5xx×1, 4xx no-retry, 401/403 hint-but-no-retry, conn×1. Exponential backoff `(2 ** attempt) * _rand_uniform(0.75, 1.25)`. Each retry emits one `WARNING` with `attempt`, `delay`, `error_class`, `model` (lazy-format JSON). "Dial down per call" is exposed via `DraftConfig.max_retries_429` / `_5xx` / `_conn`.

## Fail-closed response audit (DEC-006, DEC-008, DEC-013)

Mirrors safety's fail-closed audit at the LLM-output boundary. Three load-bearing rules:

1. **Propagation IS the defence.** `signalforge.draft.audit.write_response_event` opens with `O_APPEND | O_CREAT | 0o600`, writes one JSONL line, `os.fsync`, closes. Catches **no** exceptions internally — `OSError` / `PermissionError` / encoding failures all propagate. Caller (`draft_from_request`) wraps as `LLMResponseAuditWriteError(cause=...)`.
2. **Size cap before any file open.** `_RESPONSE_AUDIT_RECORD_LIMIT_BYTES = 4000` is checked before `os.open`. Raises `LLMResponseAuditRecordTooLargeError(size, limit)` which `draft_from_request` propagates **as-is** (it's a typed `DraftError` subclass), not re-wrapped.
3. **Bad-JSON dropped does NOT write an audit.** `parse_draft_response` runs **before** the audit write. A response that fails JSON parse, Pydantic validation, or the anchor contract raises an `LLMOutputError` subclass and the audit JSONL stays empty for that call. The LLM provider's logs record the malformed output; the SignalForge audit only captures successful round-trips.

`LLMResponseEvent` carries `sent_sql_hash` (blake2b-8 of `Model.raw_code`), `parsed_schema_hash` (blake2b-8 of `candidate.model_dump_json` with sorted keys), `response_text_hash` (blake2b-8 of raw LLM text), plus `prompt_version`, cache-token economics, model id, and `signalforge_version`.

## `<MODEL_SQL>` prompt-injection envelope (DEC-007)

`Model.raw_code` is user-authored SQL. A comment like `-- IGNORE PREVIOUS INSTRUCTIONS` could flip the LLM's output without the envelope. `_render_dynamic_block` wraps `raw_code` in `<MODEL_SQL>...</MODEL_SQL>` tags; the system message's anchor contract instructs the LLM to treat anything between as data.

**Envelope-breach guard.** `_render_dynamic_block` raises `PromptEnvelopeBreachError(model_unique_id)` if `raw_code` contains the literal `</MODEL_SQL>` — refuses to render the prompt, never reaches the LLM. Don't downgrade to a warning; the envelope is the only defence between malicious manifest content and the LLM.

## Cached-block scope (DEC-009)

The cached block contains **only** the model under draft + its direct `refs` and `depends_on` neighbours from `Manifest`. NOT the full project manifest. Hard cap at 8000 input tokens via pre-send `messages.count_tokens` (DEC-024); above 8000 raises `LLMCacheTooLargeError` **before** any `messages.create`. Below the model's minimum cacheable size (1024 Sonnet/Opus, 2048 Haiku), the `cache_control` marker is **dropped** and an INFO line is logged — Anthropic silently no-ops a sub-minimum marker; dropping explicitly avoids paying the count-tokens round-trip twice and silences the dual-zero cache-anomaly WARNING. (Behaviour changed under issue #10 — previously raised `LLMCacheTooSmallError`, removed from the public surface.)

The `tests/llm/test_prompt_cache_stability.py` snapshot pins the cached-block bytes for the canonical fixture; drift in `_render_manifest_summary` output breaks the test loudly.

## Cache-anomaly WARNING fires only on dual-zero (DEC-014, post-QG fix)

`signalforge.llm.client` emits `WARNING: cache marker no-op` only when **both** `cache_creation_input_tokens == 0` and `cache_read_input_tokens == 0` despite the cached block carrying a marker and passing pre-send size check. `cache_creation == 0` alone is the **normal healthy cache-hit case**. Any future "cache health" signal must apply the same dual-zero pattern.

## Tolerant JSON extraction — prose-preamble guardrail (issue #144)

`claude-sonnet-4-6` reproducibly narrates a reasoning preamble ("I need to analyze the business rules carefully...") **before** the JSON object on the business-rules drafting path, so `parse_draft_response`'s strict `CandidateSchema.model_validate_json` failed at line 1. **Assistant-turn prefill (the usual "JSON only" guardrail) is NOT available on this model** — the API rejects it with HTTP 400 `"This model does not support assistant message prefill. The conversation must end with a user message."` So the **parser is the only place a JSON-only guarantee can live**; the prompt is advisory.

`parse_draft_response` routes `raw_text` through `signalforge._common.json_payload.extract_json_payload` before `model_validate_json`. The helper strips a leading prose preamble (and trailing content) around a cleanly-decodable JSON value. Two load-bearing rules:

1. **Decode at the FIRST `{`/`[` only — never scan deeper.** A truncated outer object (whose first brace fails to decode) would otherwise match a complete *inner* fragment, silently turning a "not valid JSON" failure into a wrong-shape parse. On first-candidate failure the helper returns the input unchanged so the strict parser raises the normal `LLMOutputJSONError` with the correct excerpt/position — the truncated/garbage paths are unchanged.
2. **Error envelopes keep the ORIGINAL `raw_text`** (preamble included) so incident reports show exactly what the model emitted; only the `model_validate_json` call sees the extracted payload. The response-audit `response_text_hash` is likewise unchanged (hashes the full API response).

The grade parser shares the same helper (`grade-layer.md`). Prompt-level "JSON only" hardening (the issue's option 3) was deliberately NOT added: it would rotate the cached system-prompt golden for no load-bearing gain once the parser is the guarantee.

## Whole-draft fail-loud anchor contract (DEC-003, DEC-022)

`signalforge.draft.parser._validate_anchor_contract` collects **every** violation — never short-circuits. Returns a tuple; non-empty raises `LLMOutputAnchorContractError(violations=...)` with the full list. Three independent checks per column:

- `CandidateColumn.name in model_columns` — guards against hallucinated column names. Without this, the LLM could invent `CandidateColumn(name="phantom", tests=[NotNull(column="phantom")])` and pass validation.
- `test.column == column.name` — a column-scoped test must reference its parent column.
- `test.column in model_columns` — independent of the parent-column-mismatch check (NOT under `elif`); a hallucinated reference surfaces both violations.

For model-level `candidate.tests`, only the `test.column in model_columns` check applies. Duplicate parameterless tests (`not_null`, `unique`) within a column count as violations; multiple `accepted_values` or `relationships` are allowed (may carry distinct args).

Don't change the validator to short-circuit on the first violation — the goal is "tell the operator everything wrong in one error so they can fix in one round."

## `exclude_tests` dual-defence: prompt + parser (issue #54)

`DraftConfig.exclude_tests: tuple[str, ...] = ()` lets the operator suppress one or more dbt test types from drafting entirely. Four valid entries pinned by `VALID_TEST_TYPES`; config-load validates each entry so a typo like `"not_nul"` fails loud at YAML-load.

Enforced at **two independent layers**:

1. **Prompt-builder server-side filtering.** `_render_system_prompt(exclude_tests)` drops matching entries from the `_TEST_CATALOGUE_LINES` JSON-shape illustration AND from the `### SCOPE` line. A cooperative LLM never proposes excluded types in the first place.
2. **Parser anchor-contract rejection.** `_validate_anchor_contract` gains an `exclude_tests: frozenset[str]` kwarg; any candidate test whose `type` is in the set adds a violation. An LLM that ignores prompt instructions hits `LLMOutputAnchorContractError` here.

Defence in depth. Prompt filter is **cheap and primary**; parser check is **load-bearing for correctness** (prompts are advisory; parsers are contractual). Removing either layer is a regression.

**Prompt-version cache invalidation.** `_prompt_version_for(exclude_tests)` returns `_PROMPT_VERSION` verbatim for the empty case (snapshot-stable), or a fresh `blake2b-8` over `(_PROMPT_VERSION + "|exclude=" + canonical_json)` when any exclusion is present. Canonical form sorts + dedupes the input so `("unique", "not_null")` and `("not_null", "unique", "unique")` hash identically. Load-bearing for Anthropic prompt-cache correctness — two runs with different exclusion sets cache separate system-prompt prefixes.

**Excluding all four types raises at render time.** `_render_system_prompt` raises `ValueError("at least one type must remain")` so the drafter has something to propose. Enforced at render time, not config-load, so a future caller adding a fifth test type can ship a config that excludes the v0.1 four.

## ANSI-safe lazy-format logger (DEC-011)

Same rule as the other layers (`safety-layer.md` DEC-022 / `prune-engine.md` DEC-017 / `grade-layer.md` DEC-029 / `diff-renderer.md` DEC-019). Grep gate at `tests/llm/test_logger_grep_gate.py` scans `src/signalforge/{llm,draft,prune,grade,diff,cli}` (6 dirs as of #9) and rejects any `_LOGGER\.\w+\(f"` hit.

## AST audit-completeness scans (DEC-013)

`tests/test_audit_completeness.py` runs five AST scans relevant to the LLM seam:

- `LLMRequest` constructed only in `signalforge.safety.request` (existing from #4).
- `AuditEvent` constructed only in `signalforge.safety.request`.
- `anthropic.Anthropic(...)` constructed only in `signalforge.llm._anthropic_client` (DEC-012 — the SDK seam; module renamed from `_client` by #135).
- `LLMResponseEvent` constructed only in `signalforge.draft.audit` — every event flows through `_build_response_event`.
- **`openai.OpenAI(...)` constructed only in `signalforge.llm._openai_client`** (the 9th project AST scan, #136 US-001/DEC-010). Mirrors Scan 3 shape exactly — reuses `_AttributeCallFinder`, catches all three bypass patterns (bare via `from openai import OpenAI`, import-alias via `from openai import OpenAI as O`, module-attribute via `import openai; openai.OpenAI(...)`).

When a new vendor lands (#137 Gemini bumps the tally to 10 for `genai.Client(...)`), add a **new** scan rather than extending an existing one — Scan 3 is Anthropic-specific (it hunts `anthropic.Anthropic`), so the OpenAI / Gemini scans are siblings, not extensions. The companion per-vendor `tests/llm/test_<vendor>_client_confinement.py` mirrors the Snowflake-shaped line-based scan for `# type: ignore` / `# pyright: ignore` confinement.

If a new module legitimately needs to construct one of these gated names, update the scan's exclusion list AND document the audit-write seam. Don't suppress the test.

## `signalforge.yml` top-level namespace: `llm:` (DEC-027)

The drafter's config block is `{ llm: { provider, model, cheap_model, max_output_tokens, cache_ttl, max_retries_429, max_retries_5xx, max_retries_conn } }` (`provider` added by #135 — registry-validated `str`, default `"anthropic"`). Sibling top-level keys are reserved and silently ignored. `DraftConfig` uses `extra="forbid"`; `_DraftConfigFile` uses `extra="ignore"` at the top level. Mirrors the same pattern across all five pipeline-stage configs.

## Reference

`plans/super/5-llm-draft-pipeline.md` — DEC-001 … DEC-027. `plans/super/135-provider-neutral-llm-seam.md` — DEC-001 … DEC-012 (the provider-neutral seam: `call_llm`, `LLMProvider` ABC + registry, capability flags, `provider` config field). `plans/super/136-openai-grading-provider.md` — DEC-001 … DEC-014 (OpenAI as the second concrete provider: shim + `OpenAIProvider` + four pricing SKUs + `--estimate` strategy refactor + JSON-mode enforcement; the no-cache real-vendor precedent for #137 Gemini). `src/signalforge/llm/` (incl. `providers.py` + `_anthropic_client.py` + `_openai_client.py`), `src/signalforge/draft/` — current implementation. `tests/llm/_fake.py::FakeAnthropicClient` + `tests/llm/_fake_openai.py::FakeOpenAIClient` — `expect_*` API; `tests/llm/_fake_provider.py::FakeNoCacheProvider` + `tests/grade/test_provider_neutrality.py` + `tests/grade/test_provider_neutrality_openai.py` — the no-cache provider-neutrality proofs (synthetic + real OpenAI). `docs/draft-ops.md` / `docs/grade-ops.md` / `docs/cost-estimate-ops.md` — operational references. `tests/fixtures/draft/llm_response_*.json` / `tests/fixtures/estimate/anthropic_byte_identity_golden.txt` — fixture sets exercising happy + each error path + the DEC-013 Anthropic byte-identity floor.
