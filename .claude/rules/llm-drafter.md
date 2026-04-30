# LLM drafter (single SDK seam + fail-closed response audit)

Established by issue #5 (LLM draft pipeline). Apply to every module under `signalforge.llm` and `signalforge.draft`, and to any new code that issues an Anthropic API call, parses an LLM response into typed objects, or writes a response-audit record.

The drafter sits between the safety layer (#4) and the prune layer (#6). It enforces SignalForge's "explainable diffs" commitment for the LLM input/output boundary: every Anthropic call goes through one seam with a known retry taxonomy; every response gets a durable receipt; bad LLM output never leaves the parser as a partial artifact.

## One SDK seam — `signalforge.llm._client` confines every `# pyright: ignore` (DEC-012)

`google-cloud-bigquery` had loose stubs; `anthropic` does too. Every `# pyright: ignore[...]` and `# type: ignore[...]` comment for the Anthropic SDK lives in **one file**: `src/signalforge/llm/_client.py`. The shim exposes a `_AnthropicClientProtocol` (Protocol, `@runtime_checkable`) duck-typed at exactly the surface `signalforge.llm.client.call_anthropic` consumes (`messages.create`, `messages.count_tokens`); both `anthropic.Anthropic` and `tests/llm/_fake.py::FakeAnthropicClient` satisfy it. The rest of `llm/` and all of `draft/` stay pyright-clean. The `import anthropic` itself is also confined here (lazy, inside `_load_anthropic_exception_classes` and `_make_anthropic_client`) so the rest of the layer doesn't pay the import cost.

When v0.2 swaps in OpenAI/Bedrock for a "BYOM" mode, the new vendor gets its own `_<vendor>_client.py` shim under `llm/` for the same reason. Don't pool SDK ignores into a generic util module.

## Module-level `_sleep` / `_rand_uniform` aliases (DEC-004)

`signalforge.llm.client` declares:

```python
_sleep = time.sleep
_rand_uniform = random.uniform
```

at module scope so tests can reassign them to deterministic stand-ins (`_sleep` to a no-op recorder; `_rand_uniform` to a fixed value) without monkey-patching `time.sleep` globally — which would break pytest's own timeouts and any other test running concurrently. Mirrors the clauditor `_anthropic.py` pattern.

The retry taxonomy is the full clauditor surface: 429×3, 5xx×1, 4xx no-retry, 401/403 hint-but-no-retry, conn×1. Exponential backoff `(2 ** attempt) * _rand_uniform(0.75, 1.25)`. Each retry emits one `WARNING` with `attempt`, `delay`, `error_class`, `model` (lazy-format JSON; never f-strings — see DEC-011). Future stages (#6 prune-rationale, #7 grader) reuse this seam, so "dial down per call" is exposed via `DraftConfig.max_retries_429` / `_5xx` / `_conn`.

## Fail-closed response audit (DEC-006, DEC-008, DEC-013)

Mirrors safety's fail-closed audit (DEC-011 of `safety-layer.md`) at the LLM-output boundary. Three load-bearing rules:

1. **Propagation IS the defence.** `signalforge.draft.audit.write_response_event` opens with `O_APPEND | O_CREAT | 0o600`, writes one JSONL line, calls `os.fsync`, closes. Catches **no** exceptions internally — `OSError`, `PermissionError`, encoding failures all propagate. The caller (`draft_from_request`) wraps as `LLMResponseAuditWriteError(cause=...)` so downstream pattern-matching can branch on the typed error. **Don't** add try/except inside `write_response_event`; the propagation is the contract.

2. **Size cap before any file open.** `_RESPONSE_AUDIT_RECORD_LIMIT_BYTES = 4000` is checked before the `os.open`, so an oversize record leaves no on-disk artefact. Raises `LLMResponseAuditRecordTooLargeError(size, limit)` which `draft_from_request` propagates **as-is** (it's a typed `DraftError` subclass) rather than re-wrapping into `LLMResponseAuditWriteError`. The exception ladder in `draft_from_request` re-raises `(LLMResponseAuditRecordTooLargeError, KeyboardInterrupt, SystemExit)` before catching `BaseException`.

3. **Bad-JSON dropped does NOT write an audit.** `parse_draft_response` runs **before** the audit write. A response that fails JSON parse, Pydantic validation, or the anchor contract raises an `LLMOutputError` subclass and the audit JSONL stays empty for that call. The LLM provider's logs record the malformed output; the SignalForge response audit only captures successful round-trips. (Tested by `test_draft_from_request_bad_json_does_not_write_response_audit`.)

`LLMResponseEvent` carries `sent_sql_hash` (blake2b-8 of `Model.raw_code`), `parsed_schema_hash` (blake2b-8 of `candidate.model_dump_json` with sorted keys), `response_text_hash` (blake2b-8 of raw LLM text), plus `prompt_version`, cache-token economics, model id, and `signalforge_version`. A reviewer querying "what SQL went out for `customers_v2` on 2026-04-15" gets the answer from the JSONL alone.

## `<MODEL_SQL>` prompt-injection envelope (DEC-007)

`Model.raw_code` is user-authored SQL. A comment like `-- IGNORE PREVIOUS INSTRUCTIONS` could flip the LLM's output without the envelope. `_render_dynamic_block` wraps `raw_code` in `<MODEL_SQL>...</MODEL_SQL>` tags; the system message's anchor contract instructs the LLM to treat anything between the tags as data, not instructions. The wrapper preserves SQL comments (which often hold business context useful for column descriptions) — no content filtering.

**Envelope-breach guard.** A `raw_code` containing the literal `</MODEL_SQL>` would terminate the fence early and let downstream content escape. `_render_dynamic_block` raises `PromptEnvelopeBreachError(model_unique_id)` on detection — refuses to render the prompt, never reaches the LLM. Don't downgrade this to a warning; the envelope is the only defence between malicious manifest content and the LLM. (Tested by `test_render_dynamic_block_rejects_closing_tag_in_raw_code`.)

## Cached-block scope (DEC-009)

The cached block contains **only** the model under draft + its direct `refs` and `depends_on` neighbours from `Manifest`. NOT the full project manifest. Hard cap at 8000 input tokens via the pre-send `messages.count_tokens` check (DEC-024); below the model's minimum (1024 Sonnet/Opus, 2048 Haiku) raises `LLMCacheTooSmallError`, above 8000 raises `LLMCacheTooLargeError`. Both fire **before** any `messages.create` so a malformed prompt never touches the wire.

Don't cache the full manifest "to amortise cost across drafts" — cache invalidation on any unrelated model change would defeat the savings, and the 8000-token cap exists precisely to keep the cache stable across runs. The `tests/llm/test_prompt_cache_stability.py` snapshot pins the cached-block bytes for the canonical fixture; a drift in `_render_manifest_summary` output breaks the test loudly.

## Cache-anomaly WARNING fires only on dual-zero (DEC-014, post-QG fix)

`signalforge.llm.client` emits a `WARNING: cache marker no-op` only when **both** `cache_creation_input_tokens == 0` and `cache_read_input_tokens == 0` despite the cached block carrying a marker and passing the pre-send size check. `cache_creation == 0 alone` is the **normal healthy cache-hit case** (the cache was created on a prior call). The Quality Gate caught the original `if cache_creation == 0:` condition flooding the log on every successful cache hit. Any future "cache health" signals must apply the same dual-zero pattern.

## Whole-draft fail-loud anchor contract (DEC-003, DEC-022)

`signalforge.draft.parser._validate_anchor_contract` collects **every** violation — never short-circuits on the first. Returns a tuple; non-empty raises `LLMOutputAnchorContractError(violations=...)` with the full list. Three independent checks fire on each column:

- `CandidateColumn.name in model_columns` — guards against hallucinated column names. Without this check the LLM could invent `CandidateColumn(name="phantom", tests=[NotNull(column="phantom")])` and pass validation. (QG fix; tested by `test_parse_draft_response_hallucinated_candidate_column_name_raises`.)
- `test.column == column.name` — a column-scoped test must reference its parent column, not a sibling.
- `test.column in model_columns` — independent of the parent-column-mismatch check (NOT under an `elif`); a hallucinated reference surfaces both violations.

For model-level `candidate.tests`, only the `test.column in model_columns` check applies. Duplicate parameterless tests (`not_null`, `unique`) within a column count as violations; multiple `accepted_values` or `relationships` are allowed (they may carry distinct args).

The collected-violations contract is exercised by `test_parse_draft_response_anchor_violation_collects_all_violations`. Don't change the validator to short-circuit on the first violation — the goal is "tell the operator everything wrong about the response in one error so they can fix the prompt or model in one round".

## ANSI-safe lazy-format logger (DEC-011)

Every `_LOGGER.{info,warning,debug,error}` call in `signalforge.llm.*` and `signalforge.draft.*` uses lazy-format with `json.dumps()` for any user-controlled string:

```python
_LOGGER.warning("retry attempt: %s", json.dumps({"attempt": n, "delay": delay, "error_class": exc.__class__.__name__, "model": model}))
```

**Never** f-string-interpolate user-controlled values into a logger call. A column name or model id containing ANSI escapes (`\x1b[31m...`) would inject into log viewers. JSON encoding handles this; f-string interpolation does not. The grep gate at `tests/llm/test_logger_grep_gate.py` runs `_LOGGER\.\w+\(f"` against `src/signalforge/{llm,draft}/` and rejects any hits.

## AST audit-completeness scans (DEC-013)

`tests/test_audit_completeness.py` runs four AST scans:

- `LLMRequest` constructed only in `signalforge.safety.request` (existing scan from #4, kept in `tests/safety/test_public_api.py`).
- `AuditEvent` constructed only in `signalforge.safety.request`.
- `anthropic.Anthropic(...)` constructed only in `signalforge.llm._client` (DEC-012 — the SDK seam).
- `LLMResponseEvent` constructed only in `signalforge.draft.audit` — every event flows through `_build_response_event` which is the single audit-write seam.

If you add a new module that genuinely needs to construct one of these gated names (e.g., a deserialiser for resumption), update the scan's exclusion list AND document the audit-write seam. **Don't suppress the test.**

## `signalforge.yml` top-level namespace: `llm:` (DEC-027)

The drafter's user-facing config block is `{ llm: { model, cheap_model, max_output_tokens, cache_ttl, max_retries_429, max_retries_5xx, max_retries_conn } }`. Sibling top-level keys (`safety:`, future `prune:`/`grade:`) are reserved for other stages and silently ignored by the drafter. `DraftConfig` uses `extra="forbid"` (config-shaped — typos like `mdoel:` fail loud); the wrapping `_DraftConfigFile` uses `extra="ignore"` at the top level so unknown sibling stages don't break the loader.

When introducing a new pipeline-stage config, claim its own top-level key. Don't pile under `llm:` — the LLM seam is shared across stages but each stage's behaviour-knob block stays separate.

## Reference

`plans/super/5-llm-draft-pipeline.md` — DEC-001 … DEC-027. `src/signalforge/llm/`, `src/signalforge/draft/` — current implementation. `tests/llm/_fake.py::FakeAnthropicClient` — `expect_*` API. `docs/draft-ops.md` — operational reference. `tests/fixtures/draft/llm_response_*.json` — fixture set exercising happy + each error path.
