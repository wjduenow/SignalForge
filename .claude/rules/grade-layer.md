# Grade layer (LLM-as-judge rubric scoring + fail-closed audit + sidecar)

Established by issue #7 (quality grader). Apply to every module under `signalforge.grade` and to any new code that calls the LLM-judge seam, writes a grade audit record, or produces a per-run sidecar JSON.

The grade layer sits between the prune engine (#6) and the diff renderer (#8). It encodes Architectural Commitment #2 ("evaluation in the loop") — every kept artifact gets a per-criterion score with a one-line "why", and the operator gets a sidecar JSON for diff/review.

## Conservative score-and-degrade taxonomy (DEC-002, DEC-015)

`GradingResult.score` is `float | None`. The two-state semantics:

- **Scored:** `score: float ∈ [0.0, 1.0]` + `passed: bool`. The judge ran, the response parsed, the anchor contract held.
- **Degraded:** `score: None, passed: False, evidence: "", reasoning: "<failure reason>"`. The pair could not be positively evaluated. Three reasons land here:
  1. `LLMError` retries exhausted → `reasoning="call failed: GradeLLMError"`.
  2. `GradeOutputError` (parser failure / anchor-contract failure) → `reasoning="call failed: GradeOutputError"`.
  3. `total_budget_seconds` exceeded → `reasoning="grade budget exceeded ..."`.

The aggregate `pass_rate` and `mean_score` are computed over the **scored** subset only. `aggregate_complete: bool` is `True` iff every result was scored. **Load-bearing invariant: graceful degrade, never silent drop.** Operators check `aggregate_complete` to know if the report is partial.

The whole run only aborts when the **audit itself** fails (`GradeAuditWriteError`/`GradeAuditRecordTooLargeError`). Mirrors prune DEC-006: a partial audit is worse than no audit.

## Fail-closed JSONL + sidecar JSON, both end-of-write durable (DEC-006, DEC-012)

Two writers in `signalforge.grade.audit`. Both follow the project's third-time-shipped fail-closed pattern (after safety / draft / prune / now grade):

1. **Propagation IS the defence.** Both writers open with strict mode flags (`O_APPEND | O_CREAT | 0o600` for JSONL; `O_WRONLY | O_CREAT | O_TRUNC | 0o600` for sidecar), single `os.write` (looped on short returns), `os.fsync`, close. **No try/except** around write/fsync; only a `try/finally` for `os.close(fd)` (which doesn't suppress exceptions). Path-canonicalisation failures wrap as `GradeAuditWriteError(cause=...)`; nothing else is wrapped.

2. **Size cap before any file open.** `_GRADE_AUDIT_RECORD_LIMIT_BYTES = 4000` (per-line JSONL) and `_GRADE_SIDECAR_RECORD_LIMIT_BYTES = 1_000_000` (whole-document sidecar). Oversize raises `GradeAuditRecordTooLargeError` BEFORE any `os.open` — no on-disk artefact.

3. **Per-decision JSONL write per `(artifact, criterion)` call.** A run that crashes mid-iteration leaves the JSONL with one durable record per evaluated pair up to the failure point. The sidecar is written end-of-run only; absence of the sidecar with a populated JSONL signals a partial run.

The sidecar's `O_TRUNC` overwrite is acceptable because it's single-doc; concurrent runs against the same `sidecar_path` produce different `run_id`s and last-writer-wins. Operators are expected to use a per-run path or accept overwrite semantics (mirrors clauditor).

## Symlink-hardened path canonicalisation at the orchestrator, not the writer (post-QG fix)

`grade_artifacts` is the place that knows the true `project_dir`. The writers' own `canonicalise_path` derivation (`audit_path.parent.parent`) is **not safe** for caller-supplied paths — a caller passing `audit_path=/tmp/grade.jsonl` would derive `project_dir=/` and any symlink would pass containment.

The fix: the orchestrator calls `canonicalise_path(raw_audit_path, resolved_project_dir)` and `canonicalise_path(raw_sidecar_path, resolved_project_dir)` BEFORE handing off to the writers. Failures wrap as `GradeAuditWriteError`. The writers' own canonicalise stays as defence-in-depth on the default path, but the load-bearing gate is the engine's. **Mirrors `signalforge.prune.engine` precedent verbatim.**

When introducing a fourth audit-write seam (diff renderer #8 will need one for its diff-history if any), apply the same engine-level canonicalisation. Don't trust the writer to derive its own `project_dir`.

## `<ARTIFACT>` envelope + whole-run pre-flight breach guard (DEC-008)

The grader sends LLM-drafted artifact text (column descriptions, test rationales, etc.) into the judge prompt. That text is itself LLM-generated, so a drafted column description containing `</ARTIFACT>` would terminate the fence early and inject judge-prompt instructions. Defence: literal-substring envelope-breach guard.

`signalforge.grade.prompts._render_dynamic_block` raises `GradePromptEnvelopeBreachError` if `</ARTIFACT>` appears in any payload field. The orchestrator runs a **whole-run pre-flight** scan over every `(artifact_id, artifact_text)` pair BEFORE the first LLM call (DEC-013 of the plan; mirrors drafter DEC-007 of #5). Loud fail at this gate is the only LLM-prompt defence between malicious artifact content and the judge.

Don't downgrade to a warning. Don't add whitespace/case normalisation (creates false-positive risk; the defence is "boring substring match"). The open tag alone (`<ARTIFACT>`) is allowed inside payloads — only the closing tag breaks the fence.

## One LLM call per (artifact × criterion); sequential (DEC-004, DEC-027)

For 4 default criteria × ~12 artifacts per typical model = ~48 calls per `grade_artifacts(...)`. The trade-off (~3.4× cost vs. batched per-artifact) buys:

- **Per-criterion retry isolation.** One bad criterion exhausting retries doesn't fail-loud the whole report; only that pair degrades.
- **Per-criterion prompt tuning headroom in v0.2.** Each criterion has its own prompt seam already.
- **Trivial single-criterion anchor contract.** Single-criterion call → no positional alignment problem; just `returned.criterion_id == sent.criterion_id`.

Sequential, not parallel. Mirrors prune DEC-028. `asyncio.gather` deferred to v0.2. Single-threaded iteration over `(criterion, artifact)` pairs makes total-budget cancellation enforceable and JSONL writes ordered.

The cached prompt block is the rubric criterion list (constant per run); the dynamic block is the per-pair `<ARTIFACT>...</ARTIFACT>` envelope. Anthropic prompt-cache TTL defaults to `"1h"` for the grader (vs. drafter's `"5m"`) — 60 sequential calls fit easily, and the longer TTL gives margin for stalls without extra cost.

## Reproducibility hash fields on every GradeEvent (DEC-010, DEC-019)

Every `GradeEvent` carries five 16-hex blake2b-8 (digest_size=8) fingerprints:

- `rubric_hash` — canonical-sorted JSON of the rubric (sorted by id, JSON dumped with `sort_keys=True, separators=(",",":")`). Same `rubric_hash` across all `GradeEvent` records in a run = same rubric. Mirrors safety's `policy_hash` (DEC-014 of #4).
- `prompt_version_template` — blake2b-8 of `_SYSTEM_PROMPT + render_rubric_block(rubric) + envelope_tags`. Constant per run for a given rubric. Changes when system prompt or rubric block template changes.
- `criterion_prompt_hash` — blake2b-8 of `criterion.id + "\x00" + criterion.criterion + "\x00" + envelope_tags`. Per-criterion, stable across artifacts. NUL-byte separator prevents id/text concatenation collisions.
- `response_text_hash` — blake2b-8 of the raw LLM response text. Empty string sentinel on the degraded path (no response captured).
- (Plus `args_hash` on collision-disambiguated `artifact_id`s — see below.)

The four default criterion texts (DEC-016) are locked verbatim and tested for stability via a pinned golden hash. Changing the text is a reproducibility break — bump `audit_schema_version` if it happens.

## `_artifact_id_for` canonical dotted-path format (DEC-009 + post-QG fix)

Six artifact_id shapes the formatter emits:

- `column.<col>.description` / `column.<col>.rationale`
- `model.description` / `model.rationale`
- `test.column.<col>.<type>` (or `.<args_hash>` when collision)
- `test.model.<type>` (or `.<args_hash>` when collision)

Collision rule (post-QG fix): two tests in the SAME scope (model-level OR same-column) sharing a `test.type` get an 8-hex `_model_test_args_hash` suffix to disambiguate. Without this, two `accepted_values` tests on the same column with different `values` lists would produce identical `artifact_id`s and JSONL records would collide on the `(run_id, artifact_id, criterion_id)` triple — the diff renderer (#8) couldn't distinguish them.

The `extract_artifact_text` resolver in `signalforge.grade.prompts` accepts both 4-part and 5-part dotted forms. When v0.2 adds new artifact shapes (model-level docs, snapshots, etc.), extend both the formatter and the resolver in lockstep — they're a paired contract.

## Single GradeEvent construction seam (DEC-029, sixth AST scan)

`signalforge.grade.audit._build_grade_event` is the only place in the package that constructs a `GradeEvent`. Stamps `signalforge_version` from `signalforge.__version__`. The 6th AST scan in `tests/test_audit_completeness.py` rejects `Call(func=Name(id="GradeEvent"))` outside `signalforge.grade.audit`. Sanity test asserts ≥1 construction site exists in `audit.py` — guards against accidental rename-without-update.

If a new module legitimately needs to construct a `GradeEvent` (e.g., a deserialiser for resumption), update the scan's exclusion list AND document the new audit-write seam. Don't suppress the test.

Pattern for future stages: any new fail-closed audit-event type gets a 7th/8th scan in the same file.

## ANSI-safe lazy-format JSON logger + grep gate (DEC-029, fourth dir)

Same rule as `safety-layer.md` DEC-022 / `llm-drafter.md` DEC-011 / `prune-engine.md` DEC-017. Never f-string-interpolate user-controlled strings into a `_LOGGER` call:

```python
_LOGGER.warning("grade budget exceeded: %s", json.dumps({...}))
```

The grep gate at `tests/llm/test_logger_grep_gate.py` now scans `src/signalforge/{llm,draft,prune,grade}` (4 dirs) and rejects any `_LOGGER\.\w+\(f"` hit. Extend the scan when the diff renderer (#5th dir) ships.

## `prune_result.model_unique_id == model.unique_id` boundary check (post-QG fix)

`grade_artifacts` requires the prune result to belong to the same model under grade. Mismatch raises `GradeError` at orchestrator entry, BEFORE any LLM call. This is "convention as boundary" — without it, a stale prune result could silently drive the no-redundant criterion (v0.2) or feed misleading dropped-test context to the judge.

`prune_result` is reserved for the v0.2 no-redundant criterion expansion, but the model-unique-id linkage is the v0.1 contract. Apply the same check at any future orchestrator entry that takes a typed result from a sibling stage.

## Custom `__repr__` on result-shaped models (DEC-022, mirrors prune)

Pydantic v2's default `__repr__` emits every field. `GradingResult` carries `evidence` and `reasoning` (potentially PII-bearing quoted artifact text). `GradingReport` carries the full `results` tuple plus computed fields.

`GradingResult.__repr__` shows only `artifact_id`, `criterion_id`, `score`, `passed`. `GradingReport.__repr__` shows only `model_unique_id`, `len(results)`, `pass_rate`, `mean_score`, `passed`, `aggregate_complete`, `duration_seconds`. Apply to any future result-shaped model whose fields include user-content payloads.

## Drift detectors are mandatory for read-back models (DEC-010 of #6 generalised)

Every `extra="ignore"` production model — `GradingResult`, `GradingReport`, `GradeEvent` — pairs with a `Strict<X>(extra="forbid")` mirror in `tests/grade/test_drift_detector.py`, validated against committed fixtures (`tests/fixtures/grade/{grade_event_v1.jsonl,grade_report_v1.json}`). Adding a field to production without updating the strict mirror OR the fixture breaks the test loudly.

The `extra=` placement convention from `safety-layer.md` DEC-015 applies verbatim:

- `GradeConfig`, `_GradeConfigFile` inner content, `Criterion`, `GradeThresholds` → `extra="forbid"` (config-shaped; typos like `mdoel:` must fail loud).
- `_GradeConfigFile` top-level → `extra="ignore"` (sibling stages reserved).
- `GradingResult`, `GradingReport`, `GradeEvent` → `extra="ignore"` (read-back; forward-compat).

## API alignment with adjacent stages

`grade_artifacts(model, candidate, prune_result, *, rubric=None, config=None, audit_path=None, sidecar_path=None, client=None, project_dir=None) -> GradingReport`. Matches the precedent from `prune_tests` / `draft_schema`:

- Model + data front-paired positionally.
- Keyword-only optionals separated by `*`.
- `client` kwarg for test injection.
- `project_dir` kwarg for orchestrator-level path resolution.

`load_grade_config(project_dir, path=None) -> GradeConfig` matches `load_prune_config` / `load_draft_config` / `load_safety_config`. Resolution order: explicit `path` > `<project_dir>/signalforge.yml grade:` > defaults.

When introducing a new stage entry in v0.2 (CLI, diff renderer), match the precedent. Diverging is more code than the alignment.

## `signalforge.yml` top-level namespace: `grade:` (DEC-029)

The grade-stage block is `{ grade: { model, cache_ttl, max_output_tokens, max_retries_*, total_budget_seconds, min_pass_rate, min_mean_score, fail_on_below_threshold, rubric? } }`. Sibling top-level keys (`safety:`, `llm:`, `prune:`, future `diff:`) are reserved for other stages and silently ignored by the grade loader.

`GradeConfig` uses `extra="forbid"`; the wrapping `_GradeConfigFile` uses `extra="ignore"` at the top level. Mirrors `prune-engine.md` DEC-020 / `llm-drafter.md` DEC-027 / `safety-layer.md` DEC-025 verbatim.

When introducing a new pipeline-stage config, claim its own top-level key. Don't pile under `grade:` — each stage's behaviour-knob block stays separate.

## v0.2 reservations (forward-compat surface, currently no-op)

Three exported names ship in v0.1 but are not consumed:

- `GradeBudgetExceededError` — never raised in v0.1; v0.2 will raise it on a hard "the run did nothing" failure (e.g., budget trips before the first pair).
- `GradeThresholds` — exported but `GradeConfig` carries the flat `min_pass_rate`/`min_mean_score` fields and `GradingReport.thresholds` is a bare `tuple[float, float]`. v0.2 will wire `GradeThresholds` as the canonical container.
- `GradeConfig.fail_on_below_threshold` — currently a no-op. v0.2 will wire it into the CLI exit-code path.

Document these explicitly in their docstrings. The pattern: ship the surface in v0.1 so v0.2 is a behaviour change, not an API break.

## Reference

`plans/super/7-quality-grader.md` — DEC-001 … DEC-029. `src/signalforge/grade/` — current implementation. `docs/grade-ops.md` — operational reference. `tests/grade/test_drift_detector.py` — schema-drift gate. `tests/test_audit_completeness.py` — AST-scan suite (6 scans as of #7). `tests/llm/test_logger_grep_gate.py` — lazy-format logger gate (4 dirs as of #7). `tests/fixtures/grade/{grade_event_v1.jsonl,grade_report_v1.json}` — committed audit/sidecar fixtures.
