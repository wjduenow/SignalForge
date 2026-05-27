# LLM drafter (single SDK seam + fail-closed response audit)

Established by issue #5 (LLM draft pipeline). Apply to every module under `signalforge.llm` and `signalforge.draft`, and to any new code that issues an Anthropic API call, parses an LLM response into typed objects, or writes a response-audit record.

The drafter sits between the safety layer (#4) and the prune layer (#6). It enforces "explainable diffs" at the LLM input/output boundary: every Anthropic call goes through one seam with a known retry taxonomy; every response gets a durable receipt; bad LLM output never leaves the parser as a partial artifact.

## One SDK seam — `signalforge.llm._anthropic_client` confines every `# pyright: ignore` (DEC-012; renamed by #135)

Every `# pyright: ignore[...]` and `# type: ignore[...]` comment for the Anthropic SDK lives in **one file**: `src/signalforge/llm/_anthropic_client.py` (renamed from `_client.py` by #135 when the seam went provider-neutral — each vendor now gets a `_<vendor>_client.py` sibling). The shim exposes `AnthropicClientProtocol` (`@runtime_checkable`) duck-typed at exactly the surface the orchestrator's `AnthropicProvider` consumes (`messages.create`, `messages.count_tokens`); both `anthropic.Anthropic` and `tests/llm/_fake.py::FakeAnthropicClient` satisfy it. The `import anthropic` is also confined here (lazy, inside `_load_anthropic_exception_classes` and `_make_anthropic_client`) so the rest of the layer doesn't pay the import cost. The generic orchestrator `signalforge.llm.client.call_llm` carries NO vendor SDK type or ignore — it types the resolved client against a neutral `_LLMClientProtocol` (#135 DEC-012).

**`AnthropicClientProtocol` is public (issue #44).** Re-exported from `signalforge.llm.__init__`; the `client` kwarg on `signalforge.draft.draft_schema` and `signalforge.grade.grade_artifacts` stays typed against it (Anthropic is the default injection surface; a non-Anthropic provider builds its own client and ignores the kwarg). Downstream callers wiring a custom Anthropic shim should type-annotate against the public name. The SDK-ignore confinement contract is unchanged — every `# pyright: ignore` for the Anthropic SDK still lives only in `_anthropic_client.py`. `_AnthropicMessagesProtocol`, `_AnthropicExceptionClasses`, `_load_anthropic_exception_classes`, and `_make_anthropic_client` remain private.

## Provider-neutral seam — generic orchestrator + per-provider strategy (#135)

`call_llm(*, system, cached_block, dynamic_block, model, max_tokens, cache_ttl="5m", prompt_version, max_retries_*, provider="anthropic", client=None) -> LLMResult` (renamed from `call_anthropic` by #135, which dropped the old name) is the single shared seam for **both** the drafter and grader. It owns the generic machinery — retry loop + backoff (`2**attempt*_rand_uniform(0.75,1.25)`), per-class budgets, WARNING/INFO logs, the min/cap token validation, and `LLMResult` assembly — and dispatches the vendor-specific bits to a provider strategy resolved from a registry. When `client is None`, `call_llm` builds it via `strategy.make_client()` (DEC-006 — client construction lives at the seam, not the CLI).

- **`LLMProvider` ABC + registry** in `signalforge.llm.providers`: `register_provider(provider)` / `provider_for(name) -> LLMProvider`; unknown name → `UnknownProviderError(LLMError)` listing available keys (CLI tier 2). A provider supplies `make_client`, `build_create_kwargs`, `build_count_tokens_kwargs`, `extract_text_blocks`, `extract_usage` (→ `UsageMetrics`), `classify_exception` (→ `ExceptionCategory`), plus capability flags `supports_prompt_caching` / `supports_token_count`. Wiring a new provider = that class + `register_provider` + a config enum value. `AnthropicProvider` (`name="anthropic"`, both flags `True`) is the only one registered in v0.x.
- **Neutral value objects:** `UsageMetrics` + the `ExceptionCategory` enum (`AUTH`, `RATE_LIMIT`, `SERVER_ERROR`, `CONNECTION`, `NO_RETRY`) keep the orchestrator off vendor-shaped dicts.
- **Capability-gated behaviour (DEC-008):** `supports_prompt_caching=False` ⇒ no `cache_control` marker, no `extended-cache-ttl` beta header, 0 cache tokens, no dual-zero anomaly WARNING. `supports_token_count=False` ⇒ skip the pre-send count gate (no pre-send `LLMCacheTooLargeError`). Anthropic sets both `True`, so its emitted bytes/control flow are unchanged — the byte-identity gate (fixtures + prompt-cache snapshot + drift detectors) is the regression guard.
- **`provider` config field (DEC-007):** `DraftConfig.provider` (`llm:` block) and `GradeConfig.provider` (`grade:` block), both registry-validated `str` defaulting to `"anthropic"` — **deliberately NOT a `Literal`** (a registry is a plugin point that grows; #136/#137 register a provider instead of editing a Literal in two configs). The validator raises `UnknownProviderError` (an `LLMError`, so Pydantic v2 does NOT wrap it into `ValidationError` — it propagates raw with the available-keys remediation).

When a new vendor lands (#136 OpenAI / #137 Gemini), add a `_<vendor>_client.py` shim + a `LLMProvider` subclass + `register_provider`; don't pool SDK ignores into a generic util module, and don't reach into `call_llm` — extend via the strategy.

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

`tests/test_audit_completeness.py` runs four AST scans:

- `LLMRequest` constructed only in `signalforge.safety.request` (existing from #4).
- `AuditEvent` constructed only in `signalforge.safety.request`.
- `anthropic.Anthropic(...)` constructed only in `signalforge.llm._anthropic_client` (DEC-012 — the SDK seam; module renamed from `_client` by #135).
- `LLMResponseEvent` constructed only in `signalforge.draft.audit` — every event flows through `_build_response_event`.

If a new module legitimately needs to construct one of these gated names, update the scan's exclusion list AND document the audit-write seam. Don't suppress the test.

## `signalforge.yml` top-level namespace: `llm:` (DEC-027)

The drafter's config block is `{ llm: { model, cheap_model, max_output_tokens, cache_ttl, max_retries_429, max_retries_5xx, max_retries_conn } }`. Sibling top-level keys are reserved and silently ignored. `DraftConfig` uses `extra="forbid"`; `_DraftConfigFile` uses `extra="ignore"` at the top level. Mirrors the same pattern across all five pipeline-stage configs.

## Reference

`plans/super/5-llm-draft-pipeline.md` — DEC-001 … DEC-027. `src/signalforge/llm/`, `src/signalforge/draft/` — current implementation. `tests/llm/_fake.py::FakeAnthropicClient` — `expect_*` API. `docs/draft-ops.md` — operational reference. `tests/fixtures/draft/llm_response_*.json` — fixture set exercising happy + each error path.
