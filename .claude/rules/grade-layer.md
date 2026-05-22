# Grade layer (LLM-as-judge rubric scoring + fail-closed audit + sidecar)

Established by issue #7 (quality grader). Apply to every module under `signalforge.grade` and to any new code that calls the LLM-judge seam, writes a grade audit record, or produces a per-run sidecar JSON.

The grade layer sits between the prune engine (#6) and the diff renderer (#8). It encodes Architectural Commitment #2 ("evaluation in the loop") — every kept artifact gets a per-criterion score with a one-line "why", and the operator gets a sidecar JSON for diff/review.

## Conservative score-and-degrade taxonomy (DEC-002, DEC-015)

`GradingResult.score` is `float | None`. Two-state semantics:

- **Scored:** `score: float ∈ [0.0, 1.0]` + `passed: bool`. The judge ran, the response parsed, the anchor contract held.
- **Degraded:** `score: None, passed: False, evidence: "", reasoning: "<failure reason>"`. Three causes route here:
  1. `LLMError` retries exhausted → `reasoning="call failed: GradeLLMError"`.
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

## One LLM call per (artifact × criterion); sequential (DEC-004, DEC-027)

For 4 default criteria × ~12 artifacts per typical model = ~48 calls per `grade_artifacts(...)`. The ~3.4× cost vs. batched per-artifact buys:

- **Per-criterion retry isolation.** One bad criterion exhausting retries doesn't fail-loud the whole report; only that pair degrades.
- **Per-criterion prompt tuning headroom in v0.2.** Each criterion has its own prompt seam already.
- **Trivial single-criterion anchor contract.** Single-criterion call → no positional alignment problem; just `returned.criterion_id == sent.criterion_id`.

Sequential, not parallel (mirrors prune DEC-028). `asyncio.gather` deferred to v0.2. Single-threaded iteration over `(criterion, artifact)` pairs makes total-budget cancellation enforceable and JSONL writes ordered.

The cached prompt block is the rubric criterion list (constant per run); the dynamic block is the per-pair `<ARTIFACT>...</ARTIFACT>` envelope. Anthropic prompt-cache TTL defaults to `"1h"` for the grader (vs. drafter's `"5m"`) — 60 sequential calls fit easily with margin for stalls.

## Reproducibility hash fields on every GradeEvent (DEC-010, DEC-019)

Every `GradeEvent` carries five 16-hex blake2b-8 fingerprints:

- `rubric_hash` — canonical-sorted JSON of the rubric (sorted by id, JSON dumped with `sort_keys=True, separators=(",",":")`). Same `rubric_hash` across all records in a run = same rubric. Mirrors safety's `policy_hash`.
- `prompt_version_template` — blake2b-8 of `_SYSTEM_PROMPT + render_rubric_block(rubric) + envelope_tags`. Constant per run for a given rubric.
- `criterion_prompt_hash` — blake2b-8 of `criterion.id + "\x00" + criterion.criterion + "\x00" + envelope_tags`. Per-criterion, stable across artifacts. NUL-byte separator prevents id/text concatenation collisions.
- `response_text_hash` — blake2b-8 of the raw LLM response text. Empty string sentinel on the degraded path.
- (Plus `args_hash` on collision-disambiguated `artifact_id`s — see below.)

The four default criterion texts (DEC-016) are locked verbatim and tested for stability via a pinned golden hash. Changing the text is a reproducibility break — bump `audit_schema_version` if it happens.

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

## Schema-version surfaces

Two exported names ship but are not consumed. **Both re-verified still-reserved on 2026-05-22 (issue #62)** — the v0.1 designs each anticipated remain intact, so neither was promoted:

- `GradeBudgetExceededError` — **still reserved.** Never raised; the engine unconditionally degrades un-evaluated pairs to `GradingResult(score=None)` and surfaces a budget-curtailed run via `aggregate_complete=False` (DEC-015). The reservation still matches the design: v0.2 will raise this on a hard "the run did nothing" failure (budget trips before the first pair is graded) — a category genuinely distinct from the partial-degrade case, so routing through `aggregate_complete` alone would lose signal. Keep reserved until a grade-layer rework adds that pre-first-pair hard-fail path. Already registered in the CLI exit-code table (tier 3).
- `GradeThresholds` — **still reserved.** `GradeConfig` carries flat `min_pass_rate`/`min_mean_score` and `GradingReport.thresholds` is a bare `tuple[float, float]`. The reservation still matches the design: the eventual canonical container should be the `BaseModel` form (already implemented in `rubric.py` with `[0.0, 1.0]` range validation a bare `tuple`/`NamedTuple` can't carry), and v0.2 will wire it so callers pass one object instead of two flat scalars. No grade-layer rework is in flight, so wiring it now would be churn for no caller — leave the flat fields until that rework lands.

A third item originally tracked under issue #62 — `DiffReport.audit_schema_version` — **graduated in #50** (bumped `1 → 2` for the `kept-uncertain` four-tier taxonomy) and is no longer a pending reservation.

Graduated in #9:

- `GradeConfig.fail_on_below_threshold` — raises `GradeBelowThresholdError` (DEC-021 of #9). Default remains `False` (report-only). The raise lands AFTER `write_grading_report(...)` returns and BEFORE `grade_artifacts(...)` returns the report so the operator has a complete `grade.json` on disk for diagnosis. Pinned by `test_grade_below_threshold_writes_sidecar_before_raising`. CLI maps the raise to its `INPUT` exit-code tier (exit 2).

## Reference

`plans/super/7-quality-grader.md` — DEC-001 … DEC-029. `src/signalforge/grade/` — current implementation. `docs/grade-ops.md` — operational reference. `tests/grade/test_drift_detector.py` — schema-drift gate. `tests/test_audit_completeness.py` — AST-scan suite (6 scans as of #7). `tests/llm/test_logger_grep_gate.py` — lazy-format logger gate (4 dirs as of #7). `tests/fixtures/grade/{grade_event_v1.jsonl,grade_report_v1.json}` — committed audit/sidecar fixtures.
