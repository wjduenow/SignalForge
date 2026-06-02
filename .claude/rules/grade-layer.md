# Grade layer (LLM-as-judge rubric scoring + fail-closed audit + sidecar)

Established by issue #7 (quality grader). Apply to every module under `signalforge.grade` and to any new code that calls the LLM-judge seam, writes a grade audit record, or produces a per-run sidecar JSON.

The grade layer sits between the prune engine (#6) and the diff renderer (#8). It encodes Architectural Commitment #2 ("evaluation in the loop") — every kept artifact gets a per-criterion score with a one-line "why", and the operator gets a sidecar JSON for diff/review.

## Conservative score-and-degrade taxonomy (DEC-002, DEC-015)

`GradingResult.score` is `float | None`. Two-state semantics:

- **Scored:** `score: float ∈ [0.0, 1.0]` + `passed: bool`. The judge ran, the response parsed, the anchor contract held.
- **Degraded:** `score: None, passed: False, evidence: "", reasoning: "<failure reason>"`. Three causes route here:
  1. `LLMError` retries exhausted → `reasoning="call failed: GradeLLMError"`. **Also covers a provider-specific safety-filter / no-content response** (Gemini's `finish_reason ∈ {SAFETY, RECITATION, OTHER, ...}` with empty parts is the v0.3 example — `GeminiProvider.extract_text_blocks` raises a typed `LLMResponseFormatError` per DEC-005 of #137, which propagates as an `LLMError` and lands here). The contract is provider-neutral: a future vendor with a content-filter surface MUST route through `LLMResponseFormatError` so the conservative degrade fires uniformly — `grade-artifacts` does NOT switch on provider name.
  2. `GradeOutputError` (parser failure / anchor-contract failure) → `reasoning="call failed: GradeOutputError"`.
  3. `total_budget_seconds` exceeded → `reasoning="grade budget exceeded ..."`.

Aggregate `pass_rate` and `mean_score` are computed over the **scored** subset only. `aggregate_complete: bool` is `True` iff every result was scored. **Load-bearing invariant: graceful degrade, never silent drop.** Operators check `aggregate_complete` to know if the report is partial.

The whole run only aborts when the **audit itself** fails (`GradeAuditWriteError` / `GradeAuditRecordTooLargeError`). A partial audit is worse than no audit.

## Fail-closed JSONL + sidecar JSON, both end-of-write durable (DEC-006, DEC-012)

Two writers in `signalforge.grade.audit`, both following the project's fail-closed pattern (fourth shipped instance — safety / draft / prune / grade):

1. **Propagation IS the defence.** Both writers open with strict mode flags (`O_APPEND | O_CREAT | 0o600` for JSONL; `O_WRONLY | O_CREAT | O_TRUNC | 0o600` for sidecar), single `os.write` (looped on short returns), `os.fsync`, close. **No try/except** around write/fsync; only a `try/finally` for `os.close(fd)`. Path-canonicalisation failures wrap as `GradeAuditWriteError(cause=...)`; nothing else is wrapped.
2. **Size cap before any file open.** `_GRADE_AUDIT_RECORD_LIMIT_BYTES = 4000` (per-line JSONL) and `_GRADE_SIDECAR_RECORD_LIMIT_BYTES = 1_000_000` (whole-document sidecar). Oversize raises `GradeAuditRecordTooLargeError` BEFORE any `os.open` — no on-disk artefact.
3. **JSONL is per-decision write; sidecar is end-of-run.** A run that crashes mid-iteration leaves the JSONL with one durable record per evaluated pair up to the failure point; absence of the sidecar with a populated JSONL signals a partial run.

The sidecar's `O_TRUNC` overwrite is acceptable because it's single-doc; concurrent runs against the same `sidecar_path` produce different `run_id`s and last-writer-wins. Operators are expected to use a per-run path or accept overwrite semantics.

## Symlink-hardened path canonicalisation at the orchestrator, not the writer (post-QG fix)

`grade_artifacts` is the place that knows the true `project_dir`. The orchestrator calls `canonicalise_path(raw_audit_path, resolved_project_dir)` and `canonicalise_path(raw_sidecar_path, resolved_project_dir)` BEFORE handing off to the writers. Failures wrap as `GradeAuditWriteError`. The writers' own canonicalise stays as defence-in-depth, but the load-bearing gate is the engine's. Mirrors `signalforge.prune.engine` precedent verbatim.

When introducing a fourth audit-write seam (diff renderer #8 will need one for diff-history if any), apply the same engine-level canonicalisation.

## `<ARTIFACT>` envelope + whole-run pre-flight breach guard (DEC-008)

The grader sends LLM-drafted artifact text into the judge prompt. That text is itself LLM-generated, so a drafted column description containing `</ARTIFACT>` would terminate the fence early and inject judge-prompt instructions.

`signalforge.grade.prompts._render_dynamic_block` raises `GradePromptEnvelopeBreachError` if `</ARTIFACT>` appears in any payload field. The orchestrator runs a **whole-run pre-flight** scan over every `(artifact_id, artifact_text)` pair BEFORE the first LLM call (mirrors drafter DEC-007 of #5). Loud fail at this gate is the only LLM-prompt defence between malicious artifact content and the judge.

Don't downgrade to a warning. Don't add whitespace/case normalisation (creates false-positive risk; the defence is "boring substring match"). The open tag alone (`<ARTIFACT>`) is allowed inside payloads — only the closing tag breaks the fence.

## One LLM call per (artifact × criterion); parallel via asyncio (DEC-004 of #6, graduated by #186)

For 4 default criteria × ~12 artifacts per typical model = ~48 calls per `grade_artifacts(...)`. The ~3.4× cost vs. batched per-artifact buys:

- **Per-criterion retry isolation.** One bad criterion exhausting retries doesn't fail-loud the whole report; only that pair degrades.
- **Per-criterion prompt tuning headroom.** Each criterion has its own prompt seam already.
- **Trivial single-criterion anchor contract.** Single-criterion call → no positional alignment problem; just `returned.criterion_id == sent.criterion_id`.

**Parallel via `asyncio.TaskGroup` + `asyncio.Semaphore(max_concurrent_calls)` + `asyncio.timeout(total_budget_seconds)` (#186).** Was sequential in v0.1 (`asyncio.gather deferred to v0.2`); #186 closed the deferral. The orchestrator shape:

- Public `grade_artifacts(...)` keeps its sync signature; internally it runs sync prefix → `asyncio.run(_grade_artifacts_async_core(...))` → sync suffix. Same return type, same side effects from a caller's perspective. `GradeNestedEventLoopError(GradeError)` (CLI tier 1) raises at orchestrator entry if a running event loop is already present — v0.3 ships single-event-loop only; cross-model batch parallelism is a v0.4 follow-up.
- Per-coroutine try/except mirrors the v0.1 sync per-pair degrade: `try: await _grade_one_async(...) except (GradeLLMError, GradeOutputError, GradePromptEnvelopeBreachError) → _build_degraded(...)`. **There is no `except asyncio.CancelledError` arm** — the original design tried to attribute "in-flight at trip" vs "un-started at trip" via an orchestrator-scope `_budget_exceeded` flag set in `except TimeoutError`, but children's `except CancelledError` arms fire BEFORE the orchestrator can set the flag, so the branch was dead code (#186 QG Pass 1+3 triangulation). The synthesis pass at the end of `_grade_artifacts_async_core` is the **single source of truth** for un-completed pairs: any `None` slot in `results_by_index` is filled with a budget-degrade record. The WARNING JSON field set is therefore `{run_id, model_unique_id, completed_count, degraded_count, total_budget_seconds}` — no `cancelled_count` field. Multi-exception `BaseExceptionGroup`s that escape bubble to the CLI boundary's `format_error_to_stderr` renderer, which has an `isinstance(exc, ExceptionGroup)` branch (#186 DEC-007 / US-010) rendering the group as the existing multi-bullet stderr shape — no traceback ever leaks (`cli-layer.md` DEC-016). Single-exception groups (rare — only the fail-closed audit-write error path triggers them) are unwrapped inside `_grade_artifacts_async_core` and re-raised as the inner typed exception so callers can still pattern-match.
- The fail-closed audit writer runs sync (DEC-006), but its call site inside the coroutine wraps via `audit_future = loop.run_in_executor(None, _write_event_or_abort, event, audit_path); try: await asyncio.shield(audit_future) except CancelledError: await audit_future; raise` (#186 DEC-017 + QG Pass 1 BLOCKER fix + PR #190 review refinement) so `os.fsync` doesn't block the event loop AND a budget timeout firing during the audit await doesn't produce a duplicate record AND a `GradeAuditWriteError` raised mid-cancellation still propagates to the `TaskGroup`. The `shield` is load-bearing: without it, a timeout-during-audit cancels the coroutine BEFORE the post-await slot assignment runs, the synthesis pass sees `None` in the slot, and writes a SECOND audit record for the same `(artifact_id, criterion_id)` pair. The captured-future + `await audit_future; raise` on `CancelledError` ensures the fail-closed contract: writer exceptions surface even when cancellation arrives mid-flight. The slot assignment happens BEFORE the audit `await` (in-memory state precedes durable state) so the synthesis pass correctly skips the index regardless of when the cancellation propagates. Each call opens its own fd; concurrent `O_APPEND` writes are POSIX-atomic per-syscall but `PIPE_BUF` does NOT apply to regular files (#186 DEC-022 + PR #190 CodeRabbit/Copilot review correction — the `PIPE_BUF` ceiling is a pipe/FIFO concept). In practice on Linux ext4/btrfs/xfs, a ≤4000-byte `os.write(fd, encoded)` completes in one syscall and the short-write loop never iterates, so concurrent appenders interleave cleanly between records; but the kernel may legitimately return a short write and the loop's second `write()` can be interleaved by another appender's record. Users who need stricter byte-level atomicity guarantees (Linux or any POSIX target) should set `max_concurrent_calls=1`.
- **Audit JSONL ordering becomes arrival-order**, not `(criterion, artifact)` iteration order — explicitly accepted per #186 (record shape unchanged, `audit_schema_version` still `Literal[1]`). Tests that snapshot JSONL post-load sort via `tests/grade/_helpers.py::_sort_grade_events(lines)` keyed on `(artifact_id, criterion_id)` (DEC-015). `max_concurrent_calls=1` is the v0.1 byte-equivalent fallback (semaphore-of-1 serialises in dispatch order; preserves `(criterion, artifact)` ordering — the migration safety floor).

**`GradeConfig.max_concurrent_calls: int = 10`** (range `[1, 100]`, `extra="forbid"`; #186 DEC-003). Default 10 matches the typical Anthropic-tier throughput sweet-spot — measured ~9× wall-clock reduction on a typical ~280-pair model (~70 artifacts × 4 criteria; ~280 s sequential → ~30 s concurrent), close to the Amdahl ceiling at concurrency=10. Operators tune via `signalforge.yml`. Setting 1 yields v0.1 sequential behaviour bit-for-bit.

**Provider async capability flag — `LLMProviderAsyncUnsupportedError`.** `LLMProvider.supports_async: ClassVar[bool] = True` (default), with abstract `make_async_client() -> _LLMAsyncClientProtocol`. All three v0.3 concretes (`AnthropicProvider` / `OpenAIProvider` / `GeminiProvider`) set `supports_async = True`. A future provider whose SDK lacks async support sets `False`; against such a provider `grade_artifacts` raises `LLMProviderAsyncUnsupportedError(LLMError)` (CLI tier 3) at orchestrator entry — fail-loud rather than silent clamp (mirrors `extra="forbid"` posture). **`max_concurrent_calls=1` is NOT an escape hatch** (#186 QG Pass 1 Concern #2): the engine consumes `call_llm_async` exclusively post-#186, so cap=1 against a sync-only provider would degrade every pair to `GradeLLMError` silently. The remediation points the operator at picking an async-capable provider.

**Anthropic prompt-cache cost penalty under concurrency (#186 DEC-021).** Calls 1..N dispatch in parallel before any response returns, so each pays the cache-write premium (~1.25× input cost on the cached rubric block) instead of the cache-read discount (~0.10×). For 10 in-flight × ~430-token rubric block ≈ ~4 450 extra input-token-equivalents per model run (absolute ~$0.003–$0.005 / typical model). Operators wanting the v0.1 cost profile set `grade.max_concurrent_calls: 1`. OpenAI + Gemini have `supports_prompt_caching=False`; no penalty applies.

The cached prompt block is the rubric criterion list (constant per run); the dynamic block is the per-pair `<ARTIFACT>...</ARTIFACT>` envelope. Anthropic prompt-cache TTL defaults to `"1h"` for the grader (vs. drafter's `"5m"`).

**Tolerant JSON extraction (issue #144).** `parse_grade_response` routes the response through `signalforge._common.json_payload.extract_json_payload` (after `_strip_code_fence`) so a judge that narrates a prose preamble before the `{` still parses. The Anthropic judge models (the `claude-haiku-4-5` default per #187, or an explicit `claude-sonnet-4-6`) do NOT support an assistant-turn prefill (API 400), so the parser is the only JSON-only guardrail. Same decode rule as the drafter — decode at the first structural char (`{` or `[`) only, return unchanged on failure — see `llm-drafter.md` § "Tolerant JSON extraction"; a no-JSON response still routes to `GradeOutputError(violation_type="json_parse")` and the conservative degrade.

## Reproducibility hash fields on every GradeEvent (DEC-010, DEC-019)

Every `GradeEvent` carries five 16-hex blake2b-8 fingerprints:

- `rubric_hash` — canonical-sorted JSON of the rubric (sorted by id, JSON dumped with `sort_keys=True, separators=(",",":")`). Same `rubric_hash` across all records in a run = same rubric. Mirrors safety's `policy_hash`.
- `prompt_version_template` — blake2b-8 of `_SYSTEM_PROMPT + render_rubric_block(rubric) + envelope_tags`. Constant per run for a given rubric.
- `criterion_prompt_hash` — blake2b-8 of `criterion.id + "\x00" + criterion.criterion + "\x00" + envelope_tags`. Per-criterion, stable across artifacts. NUL-byte separator prevents id/text concatenation collisions.
- `response_text_hash` — blake2b-8 of the raw LLM response text. Empty string sentinel on the degraded path.
- (Plus `args_hash` on collision-disambiguated `artifact_id`s — see below.)

The four default criterion texts (DEC-016) are locked verbatim and tested for stability via a pinned golden hash. Changing the text is a reproducibility break — bump `audit_schema_version` if it happens.

**Grade-side `_PROMPT_VERSION` snapshot surface (#170 DEC-012).** Issue #170 closed an asymmetry that had drifted into `business-rule-tests.md`: the rule file historically claimed "two `_PROMPT_VERSION` constants in the pipeline, each with its own cache-stability snapshot" — but only the drafter side had a snapshot. The grade side carried `rubric_hash` / `prompt_version_template` / `criterion_prompt_hash` dynamically per-event but had no module-level constant and no pinning test. #170 added `signalforge.grade.prompts._PROMPT_VERSION` (typed `Final[str]`, computed at import via `prompt_version_template(DEFAULT_RUBRIC)`) plus `tests/grade/test_prompt_cache_stability.py` mirroring the drafter snapshot shape — pins both the `_PROMPT_VERSION` hex AND a rendered rubric-block golden with `difflib.unified_diff` on mismatch. **Rotation policy: rotate the snapshot when the grade `_SYSTEM_PROMPT` text changes OR any of the four `DEFAULT_RUBRIC` criterion texts change.** Mirrors the drafter-side `_TEST_CATALOGUE_LINES` rotation contract (`llm-drafter.md`). A new variant's grade-rubric extension (e.g. #170's `no-redundant` extension for grain-meaningfulness) rotates the constant value naturally; pre-existing pinned tests fail loud and the new value is computed and pinned in the same commit (`tests/grade/test_rubric.py` + `tests/grade/test_prompts.py` rotate alongside).

## `_artifact_id_for` canonical dotted-path format (DEC-009, issue #42 hoist)

Six shapes the formatter emits:

- `column.<col>.description` / `column.<col>.rationale`
- `model.description` / `model.rationale`
- `test.column.<col>.<type>` (or `.<args_hash>` when collision)
- `test.model.<type>` (or `.<args_hash>` when collision)

Collision rule: two tests in the SAME scope (model-level OR same-column) sharing a `test.type` get an 8-hex `_model_test_args_hash` suffix. Without this, two `accepted_values` tests on the same column with different `values` lists would produce identical `artifact_id`s and JSONL records would collide on the `(run_id, artifact_id, criterion_id)` triple.

The `extract_artifact_text` resolver accepts both 4-part and 5-part dotted forms. When v0.2 adds new artifact shapes, extend both the formatter and the resolver in lockstep — they're a paired contract.

**Implementation lives in the shared seam.** `_artifact_id_for`, `_model_test_args_hash`, and `_test_args_hashes` in `signalforge.grade.engine` are re-exports of `signalforge._common.artifact_id` (`artifact_id_for`, `model_test_args_hash`, `compute_args_hashes`); `signalforge.diff._artifact_id` does the same. Cross-stage parity is enforced by `is` identity rather than byte-equal snapshot — drift is impossible by construction. When extending the formatter, edit only the shared module.

## Single GradeEvent construction seam (DEC-029, sixth AST scan)

`signalforge.grade.audit._build_grade_event` is the only place in the package that constructs a `GradeEvent`. Stamps `signalforge_version` from `signalforge.__version__`. The 6th AST scan in `tests/test_audit_completeness.py` rejects `Call(func=Name(id="GradeEvent"))` outside `signalforge.grade.audit`. Sanity test asserts ≥1 construction site exists in `audit.py` — guards against rename-without-update.

If a new module legitimately needs to construct a `GradeEvent`, update the scan's exclusion list AND document the new audit-write seam.

## ANSI-safe lazy-format JSON logger + grep gate (DEC-029)

Same rule as the other layers (`safety-layer.md` DEC-022 / `llm-drafter.md` DEC-011 / `prune-engine.md` DEC-017). The grep gate at `tests/llm/test_logger_grep_gate.py` now scans `src/signalforge/{llm,draft,prune,grade}` (4 dirs as of #7) and rejects any `_LOGGER\.\w+\(f"` hit.

## `prune_result.model_unique_id == model.unique_id` boundary check (post-QG fix)

`grade_artifacts` requires the prune result to belong to the same model under grade. Mismatch raises `GradeError` at orchestrator entry, BEFORE any LLM call. Without it, a stale prune result could silently drive the no-redundant criterion (v0.2) or feed misleading dropped-test context to the judge. Apply the same `<arg>.<id> == model.<id>` check at any future orchestrator entry that takes a typed result from a sibling stage.

## Custom `__repr__` on result-shaped models (DEC-022, mirrors prune)

Pydantic v2's default `__repr__` emits every field. `GradingResult` carries `evidence` and `reasoning` (potentially PII-bearing quoted artifact text); `GradingReport` carries the full `results` tuple plus computed fields.

`GradingResult.__repr__` shows only `artifact_id`, `criterion_id`, `score`, `passed`. `GradingReport.__repr__` shows only `model_unique_id`, `len(results)`, `pass_rate`, `mean_score`, `passed`, `aggregate_complete`, `duration_seconds`.

## Drift detectors are mandatory for read-back models (DEC-010 of #6 generalised)

Every `extra="ignore"` production model — `GradingResult`, `GradingReport`, `GradeEvent` — pairs with a `Strict<X>(extra="forbid")` mirror in `tests/grade/test_drift_detector.py`, validated against committed fixtures (`tests/fixtures/grade/{grade_event_v1.jsonl,grade_report_v1.json}`). Adding a field to production without updating the strict mirror OR the fixture breaks the test loudly.

`extra=` placement convention from `safety-layer.md` DEC-015 applies verbatim: config-shaped (`GradeConfig`, `_GradeConfigFile` inner, `Criterion`, `GradeThresholds`) → `extra="forbid"`; `_GradeConfigFile` top level → `extra="ignore"`; read-back (`GradingResult`, `GradingReport`, `GradeEvent`) → `extra="ignore"`.

## API alignment with adjacent stages

`grade_artifacts(model, candidate, prune_result, *, rubric=None, config=None, audit_path=None, sidecar_path=None, client=None, project_dir=None) -> GradingReport`. Matches `prune_tests` / `draft_schema`: model + data front-paired positionally; keyword-only optionals after `*`; `client` for test injection; `project_dir` for orchestrator-level path resolution.

`load_grade_config(project_dir, path=None) -> GradeConfig` matches `load_prune_config` / `load_draft_config` / `load_safety_config`. Resolution order: explicit `path` > `<project_dir>/signalforge.yml grade:` > defaults.

## `signalforge.yml` top-level namespace: `grade:` (DEC-029)

The grade-stage block is `{ grade: { model, cache_ttl, max_output_tokens, max_retries_*, total_budget_seconds, min_pass_rate, min_mean_score, fail_on_below_threshold, rubric? } }`. Sibling top-level keys are reserved and silently ignored by the grade loader. `GradeConfig` uses `extra="forbid"`; `_GradeConfigFile` uses `extra="ignore"` at the top level. Mirrors the other layers' top-level-namespace pattern verbatim.

## Locked defaults: per-provider fast model + 1024 output cap (DEC-026, #187)

`GradeConfig`'s locked defaults (DEC-023..DEC-027) carry two #187 changes:

- **`model` default is now a per-provider sentinel.** The field defaults to `None`; a `mode="before"` model-validator (`_resolve_model_default`) resolves the sentinel at config-load to the calling provider's fast model from `signalforge.llm.providers.PROVIDER_FAST_MODELS` — `anthropic` → `claude-haiku-4-5`, `openai` → `gpt-4o-mini`, `gemini` → `gemini-2.5-flash`. (Pre-#187 the default was the bare `claude-sonnet-4-6` literal regardless of provider.) An explicit `model:` is honoured verbatim; after construction the field is always a concrete non-empty string, never `None`. The resolver runs `before` because `GradeConfig` is `frozen=True` and a `mode="after"` mutation would raise. A provider NOT in the fast-model table is left un-injected so the `provider` field-validator surfaces `UnknownProviderError` rather than a masking `KeyError`.
- **`max_output_tokens` default raised 256 → 1024** so a verbose one-line `gemini-2.5-flash` grade JSON is not truncated (a truncation would surface as the wrong typed degrade — `GradeOutputError` instead of `GradeLLMError`). Still a cap, not a target; the expected JSON is ~150 tokens, so the larger ceiling costs nothing on the happy path.

**Model↔provider compat validator (DEC-006 of #187).** A `mode="after"` validator (`_validate_model_provider_compat`) reads `signalforge.llm.providers.PROVIDER_SKU_PREFIXES` (`anthropic` → `claude-`, `openai` → `gpt-`, `gemini` → `gemini-`) and fails loud at config-load when `provider` is a known-prefix provider AND the resolved/explicit `model` carries a *different* known provider's SKU prefix (e.g. `provider: openai` with a `claude-` model). Two cases are deliberately left alone: a model whose prefix matches no known provider (forward-compat for future SKUs) and a registry-valid provider outside the prefix table (custom/plugin providers may use any model name). Both `PROVIDER_FAST_MODELS` and `PROVIDER_SKU_PREFIXES` are the single source of truth — no hardcoded SKUs or prefixes in the grade config module. Every fast-model value is an exact key in `signalforge.llm.pricing.PRICES`, so the `--estimate` path never raises on the resolved default.

## Reusable conventions distilled from #187

Two patterns from the #187 sentinel-default work generalise beyond the grade layer. Reach for them whenever a config field's default depends on *another* field, or whenever a default is looked up in a table keyed by a registry-growable value.

**Frozen-config "default from a sibling field" resolves in `@model_validator(mode="before")`, never `mode="after"`.** When a config field defaults based on another field on the same model (here `model` ← `provider`), the resolution MUST inject the computed value into the raw dict in a `mode="before"` validator — NOT mutate `self.<field>` in a `mode="after"` validator. The pipeline's config models are `frozen=True` (`extra="forbid"`), and a `mode="after"` `self.model = ...` raises (Pydantic forbids attribute assignment on a frozen instance). The before-validator runs ahead of field validation, so the injected value flows through the normal construction path and the field is concrete the moment the frozen instance exists. Copy-on-write the dict (`data = {**data, "model": resolved}`) so a caller-owned mapping is never mutated, and guard the input shape (`if not isinstance(data, dict): return data`) so an already-constructed instance passed to `model_validate` passes through untouched. This is the reusable convention for any future "this knob defaults from that knob" on a frozen `*Config` (e.g. a draft `cheap_model` ← `provider`, a prune `partition_filter` ← `scope`).

**A default looked up in a table keyed by a registry-growable field must FAIL LOUD on a registered-but-absent key — never leak the sentinel.** This is the load-bearing #187 lesson. `model` defaults from `PROVIDER_FAST_MODELS[provider]`, but `provider` is a *registry-validated `str`, not a `Literal`* (the provider registry is a plugin point designed to grow — see `llm-drafter.md`). So three population states exist for the key field, and each needs a distinct fate:

1. **Unregistered provider** — the `provider` field-validator already raises `UnknownProviderError`. The before-validator declines to inject (uses `.get()`, not `[]`) so it never masks that with a `KeyError`.
2. **Registered AND in the fast-model table** — the before-validator injects the fast model. Happy path.
3. **Registered BUT absent from the fast-model table** (a custom/plugin provider — the registry-growth path) — the before-validator has no value to inject, so `model` reaches the `mode="after"` validator still `None`. **This case must fail loud**, requiring an explicit `grade.model`, rather than letting `None` flow downstream.

State 3 is the trap. The #187 Quality-Gate review caught a real bug here: the original compat validator only checked SKU-prefix mismatches and silently returned for a non-prefix provider, so a registered-but-untabled provider left `model=None` — directly contradicting the engine's `assert config.model is not None` invariant (the engine, `GradeEvent.model`, and the cost-rollup's prefix dispatch all assert/depend on a concrete model). The fix makes `_validate_model_provider_compat` raise at config-load when `model is None`, keeping the "model is never `None` post-construction" invariant *genuinely* true rather than merely usually true.

The general rule for any per-X default table whose key field comes from a growable registry: enumerate the three population states explicitly, and make the "registered-but-absent-from-the-table" state a loud config-load failure that names the remediation (set the field explicitly). A `None`/sentinel that survives construction because the table happened not to cover a key is exactly the silent-no-op failure mode that downstream `assert`/exact-match consumers turn into a confusing far-from-the-cause crash. `PROVIDER_FAST_MODELS` / `PROVIDER_SKU_PREFIXES` live in `signalforge.llm.providers` and are the single source for per-provider fast models / SKU-prefix dispatch (shared with `cost/_rollup.py`, reusable by a future draft `--cheap`).

## Schema-version surfaces

Two exported names ship but are not consumed. **Both re-verified still-reserved on 2026-05-22 (issue #62)** — the v0.1 designs each anticipated remain intact, so neither was promoted:

- `GradeBudgetExceededError` — **still reserved.** Never raised; the engine unconditionally degrades un-evaluated pairs to `GradingResult(score=None)` and surfaces a budget-curtailed run via `aggregate_complete=False` (DEC-015). The reservation still matches the design: v0.2 will raise this on a hard "the run did nothing" failure (budget trips before the first pair is graded) — a category genuinely distinct from the partial-degrade case, so routing through `aggregate_complete` alone would lose signal. Keep reserved until a grade-layer rework adds that pre-first-pair hard-fail path. Already registered in the CLI exit-code table (tier 3).
- `GradeThresholds` — **still reserved.** `GradeConfig` carries flat `min_pass_rate`/`min_mean_score` and `GradingReport.thresholds` is a bare `tuple[float, float]`. The reservation still matches the design: the eventual canonical container should be the `BaseModel` form (already implemented in `rubric.py` with `[0.0, 1.0]` range validation a bare `tuple`/`NamedTuple` can't carry), and v0.2 will wire it so callers pass one object instead of two flat scalars. No grade-layer rework is in flight, so wiring it now would be churn for no caller — leave the flat fields until that rework lands.

A third item originally tracked under issue #62 — `DiffReport.audit_schema_version` — **graduated in #50** (bumped `1 → 2` for the `kept-uncertain` four-tier taxonomy) and is no longer a pending reservation.

Graduated in #9:

- `GradeConfig.fail_on_below_threshold` — raises `GradeBelowThresholdError` (DEC-021 of #9). Default remains `False` (report-only). The raise lands AFTER `write_grading_report(...)` returns and BEFORE `grade_artifacts(...)` returns the report so the operator has a complete `grade.json` on disk for diagnosis. Pinned by `test_grade_below_threshold_writes_sidecar_before_raising`. CLI maps the raise to its `INPUT` exit-code tier (exit 2).

## Reference

`plans/super/7-quality-grader.md` — DEC-001 … DEC-029. `src/signalforge/grade/` — current implementation. `docs/grade-ops.md` — operational reference. `tests/grade/test_drift_detector.py` — schema-drift gate. `tests/test_audit_completeness.py` — AST-scan suite (6 scans as of #7). `tests/llm/test_logger_grep_gate.py` — lazy-format logger gate (4 dirs as of #7). `tests/fixtures/grade/{grade_event_v1.jsonl,grade_report_v1.json}` — committed audit/sidecar fixtures.
