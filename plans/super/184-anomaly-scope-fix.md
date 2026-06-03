# #184 — Drafter mis-scopes `row_count_anomaly_by_period` to date columns

## Meta

- **Ticket:** [#184](https://github.com/wjduenow/SignalForge/issues/184)
- **Title:** `#171 follow-on: Drafter mis-scopes row_count_anomaly_by_period to date columns on models with audit timestamps`
- **Branch:** `plan/184-anomaly-scope-fix`
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/184-anomaly-scope-fix`
- **Base:** `origin/dev` @ `b19c161` (post-#183 / #185)
- **Phase:** `detailing` (stories ready for review)
- **Sessions:** 1 (2026-06-02)
- **Related:** #171 (origin), #179 (retest that surfaced it), #183 (parallel pattern — shipped today), PR #182 (writeup), PR #191 (#183 lockstep precedent)

---

## Phase 1 — Discovery

### What / Why / Who

**What.** The LLM drafter emits `row_count_anomaly_by_period` candidate tests **inside a column's `tests:` list** when the manifest carries an audit-timestamp column (`creation_ts`, `update_ts`, `loaded_at`, `created_at`, `event_date`, `partition_date`, `LOAD_TIMESTAMP`, …). The variant is **type-level model-only** (`column: None = None`, see `business-rule-tests.md` § "The variants"), so column-scoped emissions produce `test.column = None` and fail the parser's anchor contract with `LLMOutputAnchorContractError` — the entire `signalforge generate` run errors out (CLI exit 2).

**Why.** The current `_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION` (`src/signalforge/draft/prompts.py:199–230`) names example column names (`loaded_at` / `created_at` / `event_date` / `partition_date`) as a "when to propose" heuristic, but **never explicitly says the test itself lives in the model-level `tests:` list, not under any column's `tests:` list**. The drafter pattern-matches `creation_ts ≈ created_at` and over-anchors. The catalogue entry (`_TEST_CATALOGUE_LINES`, lines 89–98) shows three JSON forms but doesn't make the scope position visually obvious.

**Who hits it.** Every dbt project using the Intuit-style `PREDEFINED_AUDIT_COLUMNS` convention — i.e. every model with `creation_ts` + `update_ts`. Reproduced **3/3** on the first three Phase B candidates of #179's retest:
- `raw/taxday_auction_insights.sql` (anchored to `Day` and `LOAD_TIMESTAMP`)
- `analytical/tvp_yelp.sql` (anchored to `DATE`)
- `analytical/core_hourly_performance.sql` (anchored to `creation_ts`)

Phase A missed it because the hand-written `_signalforge_demo_schema.yml` for `weekly_query_cost.sql` omitted audit timestamps; Phase B's AST-based schema synthesiser merged them and surfaced the bug.

**Workaround (in use today).** Operator sets `llm.exclude_tests: [row_count_anomaly_by_period]` in `signalforge.yml` (documented in `llm-drafter.md` § "`exclude_tests` dual-defence"). The whole variant is suppressed.

### Acceptance criteria (from ticket body)

1. **Prompt-side (primary).** Rewrite `_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION` to explicitly say *"this test goes in the model-level `tests:` list, NOT inside any column's `tests:` list — the `date_column` argument names the column but the test itself is model-scoped"*. Add a worked-example catalogue line showing the JSON shape at model scope.
2. **Lockstep `_PROMPT_VERSION` rotation** + drafter-side cache-stability snapshot update per `business-rule-tests.md` § "Lockstep `_PROMPT_VERSION` rotation when extending the catalogue".
3. **Parser-side defence-in-depth (secondary; design decision).** When a model-level-only variant appears inside a column's `tests:` array, parser COULD re-attach to model scope rather than raising `LLMOutputAnchorContractError`. Trade-off explicit in the ticket: silent correction may mask future prompt regressions. **DEC required before implementing.**
4. **Validation.** Re-run `signalforge generate` against the 15 Phase B candidates (#179 § "Reproducing this retest"); assert the drafter (a) proposes the variant at model scope where appropriate, AND (b) does not anchor it to column scope. Reset the `signalforge.yml` workaround in the operator-side substrate (`~/Projects/intuit_airflow/plugins/dbt/`).

### Codebase findings

| Surface | Path | Lines | Role |
|---|---|---|---|
| Target constant | `src/signalforge/draft/prompts.py` | 199–230 | `_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION` — rewrite primary |
| Sibling precedent | `src/signalforge/draft/prompts.py` | 232–254 | `_ROW_COUNT_BETWEEN_SCOPE_INSTRUCTION` (shape from #183 — landed today) |
| Catalogue lines | `src/signalforge/draft/prompts.py` | 59–99 | `_TEST_CATALOGUE_LINES` — anomaly entry at 89–98 (3 forms, no scope marker) |
| System prompt | `src/signalforge/draft/prompts.py` | 257–327 | `_SYSTEM_PROMPT_TEMPLATE` — `{row_count_anomaly_scope}` placeholder |
| Render entry | `src/signalforge/draft/prompts.py` | 330–400 | `_render_system_prompt` — gating at 389–391 |
| Hash | `src/signalforge/draft/prompts.py` | 449–459 | `_PROMPT_VERSION` (auto-rotates) |
| Cache-stability gate | `tests/llm/test_prompt_cache_stability.py` | 107 | `_EXPECTED_PROMPT_VERSION = "e568fb3e4602e465"` (post-#183) |
| Cache golden | `tests/llm/test_prompt_cache_stability.py` | 115–148 | `_CACHED_BLOCK_GOLDEN` (manifest summary; **not** affected by scope-prose edit) |
| Prose tests (existing) | `tests/draft/test_prompts.py` | 560–634 | 6 anomaly prose pins (incremental fact / dow / method defaults / inclusion / exclusion / keeps-other-excluded) |
| Parser arm (existing) | `src/signalforge/draft/parser.py` | 572–615 | Model-level `row_count_anomaly_by_period` arm — currently rejects `column != None` via fallthrough |
| Parser tests | `tests/draft/test_parser.py` | 1820–2049 | 9 anomaly anchor-contract tests |
| Rule (variant pattern) | `.claude/rules/business-rule-tests.md` | (multiple) | Lockstep rotation + 6 dispatch sites + paired-prose-tests contract |
| Rule (drafter) | `.claude/rules/llm-drafter.md` | (multiple) | Anchor contract, exclude_tests dual-defence |
| Ops doc | `docs/draft-ops.md` | (search for `row_count_anomaly_by_period`) | Operator-facing teaching surface |

### Coordination note

PR #191 (issue #183 — `_ROW_COUNT_BETWEEN_SCOPE_INSTRUCTION`) merged at **2026-06-02 15:33Z** (~hours before this plan). It rotated `_PROMPT_VERSION` `c11a73cc95b31614 → e568fb3e4602e465`. Six surfaces touched: `prompts.py`, `test_prompt_cache_stability.py`, `test_prompts.py`, `business-rule-tests.md`, `docs/draft-ops.md`, plan file. **Our #184 fix lands AFTER #183 on `dev` — no parallel-Ralph collision risk.** We rotate from `e568fb3e4602e465` to a new hash. The six-surface change shape from #183 is the template.

### Rules-compliance constraints (from Convention Checker)

The full list is in the discovery agent's output. Top-12 that gate this work (carry into Phase 4 validation):

1. **Paired prose tests** (`business-rule-tests.md`) — if the scope text changes, the existing `test_system_prompt_scope_teaches_incremental_fact_table_heuristic` + `_documents_method_defaults` substring pins must still pass (or be re-pinned in the same commit).
2. **Inclusion + exclude-gating pin pair** must both still pass (per-type, not all-or-nothing gating).
3. **`_PROMPT_VERSION` rotation + cache-stability snapshot** in the same commit (drafter side only; grade side untouched).
4. **Cached-block 8000-token cap** — verify with canonical fixture; the new prose adds bytes.
5. **Grade-side `_PROMPT_VERSION` does NOT rotate** unless the rubric `no-redundant` criterion text changes for anomaly calibration — it doesn't here. Keep independent.
6. **No f-string in `_LOGGER`** anywhere under `src/signalforge/{llm,draft,…}/` (AST grep gate).
7. **Six-surface change shape** — mirror #183: `prompts.py` + `test_prompt_cache_stability.py` + `test_prompts.py` + `business-rule-tests.md` + `docs/draft-ops.md` + plan file.
8. **Branch / PR target dev**, not main (user memory `feedback_pr_target_dev`).
9. **No `Co-Authored-By: Claude` trailers / `🤖 Generated with Claude Code` footers / Ralph attribution** anywhere (user memory `no-claude-coauthor-attribution`).
10. **Anchor-contract collect-all preserved** — if the secondary parser lever ships, it must not short-circuit other violations.
11. **`exclude_tests` dual-defence preserved** — `exclude_tests: [row_count_anomaly_by_period]` (the current operator workaround) must keep working as a kill-switch even after the fix lands.
12. **The bundled Claude Code skill** (`SKILL.md`) is a parity surface for CLI subcommand/flag changes — **not affected by this fix** (no CLI surface change).

### Memories that apply

- `signalforge-row-count-anomaly-pattern` — the #171 variant-extension precedent (this fix lives inside that pattern).
- `signalforge-row-count-between-pattern` — #169 sibling; the catalogue + scope-instruction surface is the same shape.
- `drafter-business-rules-hallucination-163` — Sonnet-4-6's "follows instructions when given crisp, model-vs-column-scoped prose" — directly relevant to phrasing choice.
- `ralph-serialize-shared-registry-beads` — #183 is already merged; serialisation pressure resolved.
- `signalforge-gemini-e2e-marker-selection` + `qg-pass-3-defer-defensive-tests-fails-codecov` — generic Ralph-era lessons; apply if Quality Gate finds gaps.
- `feedback_pr_target_dev`, `no-claude-coauthor-attribution`, `wsl2-pytest-transient-segfault` — workflow hygiene.

---

## Phase 1 — Scoping answers (locked)

1. **Parser-side defence-in-depth: SHIP IT IN THIS TICKET as belt-and-braces.** Add a parser branch that handles column-scoped `row_count_anomaly_by_period` / `row_count_between` / `unique_combination`. Operator-visible WARNING. Same change handles all three model-only variants.
2. **Catalogue shape: PROSE-ONLY change inside `_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION`.** Add the model-vs-column emphasis + a short worked-example YAML snippet with surrounding `models:` / `tests:` context. Catalogue JSON lines stay byte-identical. Single-surface edit.
3. **Validation: ALL 15 Phase B candidates (full re-aggregation).** Update `docs/research/179-test-primitive-expansion-retest.md` with the followup results.
4. **Docs: ops doc + research doc amendment.** Update `docs/draft-ops.md` with the model-vs-column rule + amend the #179 research doc with a "Followup #184 resolution" note.

---

## Phase 2 — Architecture Review

### Ratings table

| Area | Rating | Key finding |
|---|---|---|
| Conventions | **CONCERN** | "Silent correction" violates the gate-over-prompt philosophy and the whole-draft fail-loud anchor contract. Re-attach must be either (a) a non-fatal violation surfaced in the violations list, or (b) a fatal violation with a clearer error message. A truly silent rewrite is precedent-breaking. |
| Testing | **PASS** | All 6 existing prose tests survive a prose-only `_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION` edit (substring pins are scope-text-agnostic). All 9 existing parser tests stay valid. New parser tests (~7) needed only if re-attach ships in its full belt-and-braces form. |
| Observability | **CONCERN** | If the parser rewrites the candidate, `LLMResponseEvent.parsed_schema_hash` reflects the corrected form, not the LLM's actual output — audit trail lies. Need explicit DEC on how to preserve forensic visibility (recommend: add `parser_reshaped: tuple[ReshapeRecord, ...]` field to `LLMResponseEvent`). |

### Cross-cutting findings (all three reviews agreed)

1. **Respect `exclude_tests`.** If operator set `exclude_tests: [row_count_anomaly_by_period]` (current workaround), the parser MUST still reject column-scoped emissions, NOT re-attach them. Kill-switch beats convenience.
2. **Preserve collect-all.** Re-attach branch must NOT short-circuit the violations loop. Other violations (e.g. hallucinated columns) still surface in the same error.
3. **Module-level `_LOGGER` needed in parser.py.** Today the parser has no logger (stage-0 reader posture). A WARNING introduces it — covered by the existing `tests/llm/test_logger_grep_gate.py` since `src/signalforge/draft/` is already scanned.
4. **WARNING shape:** positional `%s` + `json.dumps({…})` (no `extra=`), mirroring `signalforge.llm.client` precedents (`cache marker no-op`, `retry attempt`).
5. **Phase B validation is maintainer-side** (not pytest-gated). Artifact decision needed: commit per-model sidecars OR a one-line summary JSON OR writeup-only.

### Areas not reviewed (out of scope)

- **Performance:** A single parser branch + one WARNING per re-attach is negligible cost.
- **Data model:** No new public types unless we add `parser_reshaped` to `LLMResponseEvent` (depends on observability decision below).
- **API design:** No CLI surface change. No SKILL.md change.
- **Security:** Envelope-breach guard for `<MODEL_SQL>` / `<BUSINESS_RULE>` unchanged. The re-attach mutates an already-parsed Pydantic model (safe ground).

---

## Phase 3 — Refinement Log

### Decisions

**DEC-001 — Prompt-side prose rewrite of `_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION` is the primary lever.**
Add an explicit sentence (verbatim from the ticket): *"This test goes in the model-level `tests:` list, NOT inside any column's `tests:` list — the `date_column` argument names the column but the test itself is model-scoped."* Plus a short worked-example YAML snippet showing the model-level placement, embedded inside the prose block (not as a separate template surface). Catalogue JSON lines (`_TEST_CATALOGUE_LINES`) stay byte-identical — prose-only change. Rationale: minimum byte churn for the largest behavioural lever; mirrors #183 / #169 shape.

**DEC-002 — Parser-side silent re-attach with always-emit WARNING + durable audit record.**
When `_validate_anchor_contract` encounters a `CandidateTest` of a model-only variant (`row_count_anomaly_by_period` / `row_count_between` / `unique_combination`) sitting inside a column's `tests` array with `test.column == None`, the parser MUTATES the candidate to move the test from `column.tests` to `candidate.tests` (model-level), emits ONE `_LOGGER.warning(...)` with lazy-format JSON, and APPENDS one `ReshapeRecord` to a new `parser_reshaped: tuple[ReshapeRecord, ...]` field that lands on `LLMResponseEvent`. The run succeeds. Rationale: belt-and-braces against future drafter regression while preserving forensic visibility via WARNING (operator-visible at runtime) + audit field (durable across runs). Closes the "silent correction masks regressions" tension explicitly.

**DEC-003 — `exclude_tests` kill-switch beats re-attach convenience.**
If `test.type in exclude_tests`, the parser does NOT re-attach; it raises the standard `LLMOutputAnchorContractError` with the existing exclude-tests violation message. The operator's opt-out is always honoured. Pinned by `test_reattach_respects_exclude_tests_gate`.

**DEC-004 — Re-attach applies uniformly to ALL three model-only variants.**
`row_count_anomaly_by_period` (the bug as reported), `row_count_between` (#169), and `unique_combination` (#170). Same shape, same WARNING template, same `ReshapeRecord` event. Closes a latent bug class for the two sibling variants — neither has been observed to mis-scope live, but the variant-extension pattern (`business-rule-tests.md` § "The variants") says they share the type-level `column: None = None` constraint.

**DEC-005 — `LLMResponseEvent` schema bump 1 → 2 + new `parser_reshaped: tuple[ReshapeRecord, ...] = ()` field.**
- New frozen `ReshapeRecord(BaseModel)` model with fields: `original_column: str` (the column the test was nested under), `target_scope: Literal["model"] = "model"` (single value today; forward-compat for v0.x), `test_type: str` (one of the three model-only variants), `reason: str` (human-readable rationale; locked verbatim for the v0.1 cause: `"model-only variant emitted at column scope; re-attached to model-level tests:"`).
- `LLMResponseEvent.parser_reshaped: tuple[ReshapeRecord, ...] = ()` — empty tuple default preserves byte-equality on the no-reshape happy path; existing v1 fixtures stay valid (`extra="ignore"` posture).
- `LLMResponseEvent.audit_schema_version: int = 2` (was `= 1`). Field stays typed `int` (not `Literal`) so older v1 audit JSONLs still round-trip.
- Drift detector mirror: `tests/draft/test_drift_detector.py::StrictLLMResponseEvent` adds `parser_reshaped` + a strict `StrictReshapeRecord` paired model + a NEW fixture row at `tests/fixtures/draft/llm_response_with_reshape_v2.jsonl` exercising the reshape path. Existing `llm_response_audit_sample.jsonl` stays at v1-shape so the drift detector validates both schema versions.

**DEC-006 — Parser-side WARNING emission shape.**
The parser gains a module-level `_LOGGER = logging.getLogger(__name__)` (it has none today; stage-0 reader posture deliberately broken here because the parser now signals an operator-actionable event). WARNING template, locked:

```python
_LOGGER.warning(
    "parser re-attach: %s",
    json.dumps(
        {
            "test_type": test.type,
            "from_scope": f"column={original_column!r}",
            "to_scope": "<model-level>",
            "model_unique_id": model_unique_id,
            "reason": "model-only variant mis-scoped to column",
        }
    ),
)
```

Always emit (no `--quiet` suppress, no config flag — mirrors the `cache marker no-op` precedent from `signalforge.llm.client`). Gated only on the technical condition (model-only variant + `column is not None` + type not in `exclude_tests`). Pinned by `test_reattach_warning_lazy_format_json_shape`.

**DEC-007 — `_PROMPT_VERSION` rotates drafter-side only.**
The prose edit in `_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION` is part of the cached system prompt (`_SYSTEM_PROMPT_TEMPLATE` consumes it), so `signalforge.draft.prompts._PROMPT_VERSION` auto-rotates. We update `tests/llm/test_prompt_cache_stability.py::_EXPECTED_PROMPT_VERSION` from `e568fb3e4602e465` to the new hash. The `_CACHED_BLOCK_GOLDEN` (manifest summary) is untouched. **Grade-side `_PROMPT_VERSION` does NOT rotate** — the rubric `no-redundant` criterion is unchanged. No grade-side surface modified.

**DEC-008 — Validation: full 15-candidate Phase B re-aggregation; result lands in `docs/research/179-test-primitive-expansion-retest.md` as a "Followup #184 resolution" section.**
Append a new top-level section to the existing research doc with: per-model pass/fail tally, scope-correctness count (model-level vs. column-level emissions, pre-fix vs. post-fix), the before/after `_PROMPT_VERSION` pair, count of `parser_reshaped` events triggered (load-bearing — a healthy fix should make the WARNING rarely fire because the prompt rewrite suffices), and one paragraph of narrative. No sidecars committed (matches existing writeup style). The operator-side `~/Projects/intuit_airflow/plugins/dbt/signalforge.yml` workaround `llm.exclude_tests: [row_count_anomaly_by_period]` is reset as part of the validation run (manual; outside this repo).

**DEC-009 — Eight-surface change (extends #183's six-surface template).**
The mechanical surfaces that move in lockstep:
1. `src/signalforge/draft/prompts.py` — rewrite `_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION` (DEC-001).
2. `src/signalforge/draft/parser.py` — re-attach branch + `_LOGGER` + import (DEC-002, DEC-006).
3. `src/signalforge/draft/audit.py` — `ReshapeRecord` + `parser_reshaped` field + schema bump (DEC-005).
4. `tests/draft/test_prompts.py` — verify existing prose pins still pass; add NEW pins for the explicit model-vs-column sentence + the worked-example snippet.
5. `tests/llm/test_prompt_cache_stability.py` — rotate `_EXPECTED_PROMPT_VERSION` (DEC-007).
6. `tests/draft/test_parser.py` — add ~7 new re-attach tests covering all three variants + collect-all + exclude_tests gate + WARNING shape + dedupe behaviour (DEC-002, DEC-003, DEC-004).
7. `tests/draft/test_drift_detector.py` + `tests/fixtures/draft/llm_response_with_reshape_v2.jsonl` — strict mirror + v2 fixture (DEC-005).
8. `docs/draft-ops.md` + `docs/research/179-test-primitive-expansion-retest.md` + `.claude/rules/business-rule-tests.md` + `.claude/rules/llm-drafter.md` — doc surfaces and rule-file updates (the model-vs-column emphasis goes into business-rule-tests.md's variant-pattern documentation; the re-attach + WARNING semantics + audit-field-bump go into llm-drafter.md).

**DEC-010 — Dedupe deferred to a follow-up.**
If the LLM emits the same variant TWICE (once at column scope, once at model scope), the parser re-attaches the column-scoped form, producing two equivalent model-level tests. Diff renderer + prune engine already handle redundant tests gracefully (both would be pruned identically; the diff entry shows them under the same `artifact_id` derivation). Adding dedupe in the parser would require args-canonical comparison logic that's overkill for v0.1. Pinned by `test_reattach_does_not_dedupe_against_existing_model_level_form` (documents the chosen behaviour).

**DEC-011 — No CLI surface change; no SKILL.md update.**
The fix is entirely inside the drafter pipeline (prompts + parser + audit). Operators see the WARNING + the corrected output; no new flag, no new subcommand, no SKILL.md text drift. The bundled Claude Code skill (`SKILL.md`) is not in the lockstep set for this ticket.

### Session notes

- **2026-06-02:** Discovery + Architecture + Refinement in one session. Discovery agents identified that PR #191 (issue #183) merged hours earlier — sets `_PROMPT_VERSION` baseline `e568fb3e4602e465` and the six-surface change template. Architecture review surfaced the silent-correction-vs-fail-loud tension across all three review areas (conventions, testing, observability); user picked the audit-field path (option A across both observability + audit-shape questions) which is the heaviest scope but closes the audit-trail gap explicitly.

---

## Phase 4 — Detailed Breakdown (stories)

Eight implementation stories + Quality Gate + Patterns & Memory = 10 stories total. Ordered by dependency. Every story's "Acceptance" includes the canonical validation command: `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.

### US-001 — Prompt prose rewrite (primary lever)

**Description.** Rewrite `_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION` in `src/signalforge/draft/prompts.py` to teach explicit model-level scope. Add the verbatim sentence from DEC-001 plus a short worked-example YAML snippet showing model-level placement.

**Traces to:** DEC-001, DEC-007.

**Files:**
- `src/signalforge/draft/prompts.py:199–230` — replace the existing instruction block; preserve all current calibration prose (incremental-fact-table heuristic, dow seasonality, method defaults) so existing prose-test pins survive.

**TDD:**
1. Add new test pin: `test_system_prompt_scope_states_model_level_placement` — asserts the substring `"model-level \`tests:\` list"` (or equivalent verbatim phrase from the new prose) appears in the rendered system prompt.
2. Add new test pin: `test_system_prompt_scope_shows_worked_example_at_model_scope` — asserts the worked YAML snippet appears verbatim (look for distinctive substrings like `"# model-level test (NOT under a column's tests:)"`).
3. Run existing 6 prose tests (`test_system_prompt_scope_teaches_incremental_fact_table_heuristic` + 5 siblings); all stay green.
4. Run `test_prompt_version_pinned_to_us_010_value` — observe the failure, copy the new hash from the failure message into `_EXPECTED_PROMPT_VERSION`, re-run, green.

**Done when:**
- New prose substring pins added in `tests/draft/test_prompts.py`; all anomaly-related prose tests pass.
- `_EXPECTED_PROMPT_VERSION` rotated in `tests/llm/test_prompt_cache_stability.py`.
- Cached-block byte count stays under 8000 tokens against the canonical fixture (existing `messages.count_tokens` gate — the test will fail loud if exceeded).
- Validation command green.

**Depends on:** none.

### US-002 — `ReshapeRecord` + `LLMResponseEvent.parser_reshaped` audit-field bump

**Description.** Add the new `ReshapeRecord(BaseModel)` model + `parser_reshaped: tuple[ReshapeRecord, ...] = ()` field on `LLMResponseEvent` + bump `audit_schema_version: int = 1` → `= 2`. Wire `extra="ignore"` posture on `ReshapeRecord` (read-back model, forward-compat). Locked field values per DEC-005.

**Traces to:** DEC-005.

**Files:**
- `src/signalforge/draft/audit.py:62–91` — add `ReshapeRecord`; add `parser_reshaped` field; bump `audit_schema_version` default.

**TDD:**
1. Write fixture: `tests/fixtures/draft/llm_response_with_reshape_v2.jsonl` — one row matching the v1 shape PLUS `parser_reshaped: [{...}]` populated + `audit_schema_version: 2`.
2. Extend `tests/draft/test_drift_detector.py::StrictLLMResponseEvent` — add the `parser_reshaped: tuple[StrictReshapeRecord, ...] = ()` field; add a `StrictReshapeRecord(BaseModel)` with `extra="forbid"`; load and validate BOTH the existing v1 fixture (parser_reshaped absent → empty tuple) AND the new v2 fixture (parser_reshaped populated).
3. Add test `test_reshape_record_extra_forbid_rejects_unknown_field` exercising drift detection.
4. Existing audit-write tests in `tests/draft/test_audit.py` stay green (`parser_reshaped=()` default → byte-equal output for the no-reshape path).

**Done when:**
- `StrictLLMResponseEvent` validates both v1 (legacy) and v2 (new) fixtures.
- `ReshapeRecord` immutable (`frozen=True`) and `extra="ignore"`.
- All existing audit tests pass.
- Validation command green.

**Depends on:** none (independent of US-001).

### US-003 — Parser re-attach branch + WARNING emission

**Description.** Add the re-attach branch to `_validate_anchor_contract` in `src/signalforge/draft/parser.py`. When a column-scoped model-only variant is detected (test.type in {row_count_anomaly_by_period, row_count_between, unique_combination} AND test.column is not None AND test.type NOT in exclude_tests), MUTATE the candidate by removing the test from `column.tests` and appending it to `candidate.tests` (with `column` field cleared per the type-level None constraint). Emit ONE WARNING per re-attach using the locked template from DEC-006. Add module-level `_LOGGER = logging.getLogger(__name__)` + `import json` + `import logging`.

The function signature gains keyword-only param `reshapes_collected: list[ReshapeRecord] | None = None` — when caller passes a list, the parser appends a `ReshapeRecord` per re-attach. When None, parser just emits the WARNING (back-compat for direct unit tests).

**Traces to:** DEC-002, DEC-003, DEC-004, DEC-006.

**Files:**
- `src/signalforge/draft/parser.py` — add re-attach branch in `_validate_anchor_contract`; add module-level `_LOGGER`; modify function signature.
- `src/signalforge/draft/schema.py:224–254` — `draft_from_request` passes a fresh `reshapes_collected=[]` list to `parse_draft_response`; threads the collected list into `LLMResponseEvent.parser_reshaped=tuple(reshapes_collected)` at audit-write time.

**TDD (new tests in `tests/draft/test_parser.py`):**
1. `test_row_count_anomaly_column_scoped_reattaches_to_model_level` — happy path for the bug as reported.
2. `test_row_count_between_column_scoped_reattaches_to_model_level` — sibling variant.
3. `test_unique_combination_column_scoped_reattaches_to_model_level` — sibling variant.
4. `test_reattach_emits_one_warning_per_event` — caplog asserts ONE `_LOGGER.warning` per re-attached test; payload JSON has the five DEC-006 keys.
5. `test_reattach_respects_exclude_tests_gate` — exclude_tests set → REJECT, do NOT re-attach.
6. `test_reattach_plus_hallucinated_column_collects_all` — re-attach happens AND hallucinated column violation still surfaces; both visible in the violations list.
7. `test_reattach_appends_to_reshapes_collected_when_provided` — caller-supplied list receives ReshapeRecord with correct fields.
8. `test_reattach_does_not_dedupe_against_existing_model_level_form` — DEC-010 documentation; two equivalent model-level forms allowed.
9. `test_reattach_warning_lazy_format_json_shape` — DEC-006 WARNING template pin.
10. Existing 9 anomaly anchor-contract tests stay green.

**Done when:**
- All 10 new tests pass; existing 9 stay green.
- `_LOGGER` correctly imported; lazy-format logger grep gate (`tests/llm/test_logger_grep_gate.py`) passes.
- Validation command green.

**Depends on:** US-002 (`ReshapeRecord` type must exist before parser appends to a list of it).

### US-004 — Wire `parser_reshaped` through `draft_from_request` to the audit event

**Description.** `signalforge.draft.schema.draft_from_request` collects the reshape list from `parse_draft_response` and threads it into the `LLMResponseEvent` constructor before `write_response_event` is called.

**Traces to:** DEC-002, DEC-005.

**Files:**
- `src/signalforge/draft/schema.py` — instantiate `reshapes_collected: list[ReshapeRecord] = []`; pass to `parse_draft_response(..., reshapes_collected=reshapes_collected)`; pass `parser_reshaped=tuple(reshapes_collected)` to `LLMResponseEvent(...)` constructor.

**TDD (in `tests/draft/test_schema.py` or `test_draft_schema_response.py`):**
1. `test_draft_from_request_threads_reshapes_to_audit_event` — fake LLM client returns a CandidateSchema with one column-scoped anomaly; `draft_from_request` writes an `LLMResponseEvent` with `parser_reshaped` populated (one entry).
2. `test_draft_from_request_no_reshapes_writes_empty_tuple` — no-reshape happy path produces `parser_reshaped=()` (back-compat).
3. Existing `test_draft_schema_response.py` tests stay green.

**Done when:**
- New tests pass.
- A real audit JSONL line is produced with `parser_reshaped` populated when a reshape occurs.
- Validation command green.

**Depends on:** US-002, US-003.

### US-005 — Parser audit-completeness scan extension

**Description.** Verify that the existing AST scan for `LLMResponseEvent` construction (one site only, in `signalforge.draft.audit._build_response_event`) is unaffected by the new field. Confirm no new gated symbol introduced. Update `tests/test_audit_completeness.py` only if the scan count changes (it shouldn't — adding a field to an existing event class doesn't add a new construction seam).

**Traces to:** DEC-005, DEC-009.

**Files:**
- `tests/test_audit_completeness.py` — likely no change; just confirm the scan still passes.

**TDD:** Run `pytest tests/test_audit_completeness.py` post-US-002 + US-003 + US-004. Confirm green.

**Done when:** All 8+ AST scans pass; planted-violation self-checks pass; validation command green.

**Depends on:** US-002, US-003, US-004.

### US-006 — Operator-facing docs + rule files

**Description.** Update the operator-facing teaching surfaces with the model-vs-column emphasis + the new re-attach + WARNING semantics + audit-field bump.

**Traces to:** DEC-001, DEC-002, DEC-005, DEC-006, DEC-009.

**Files:**
- `docs/draft-ops.md` — add a sentence to the `row_count_anomaly_by_period` operator subsection (mirror #183's posture for `row_count_between`): name the model-vs-column scope rule; cite the parser re-attach + WARNING + `parser_reshaped` audit field as the secondary defence.
- `.claude/rules/business-rule-tests.md` — extend § "The variants" with a note that the model-only constraint is now enforced by the parser via re-attach (not just rejected fail-loud). Add a § subsection "Parser re-attach for mis-scoped model-only variants (#184)" with the DEC pointers + a one-line note that `_LOGGER` is now present in `signalforge.draft.parser`.
- `.claude/rules/llm-drafter.md` — extend § "Whole-draft fail-loud anchor contract" with the re-attach carve-out: "Three model-only variants (`row_count_anomaly_by_period` / `row_count_between` / `unique_combination`) sitting at column scope are silently re-attached to model scope with a WARNING + `parser_reshaped` audit record; this is the ONE place the anchor contract is non-fatal (DEC-002 of #184)." Document `audit_schema_version` bump 1 → 2.
- `CHANGELOG.md` — `[Unreleased]` § Fixed (one line: "Drafter mis-scoped `row_count_anomaly_by_period` to date columns on models with audit timestamps; parser now re-attaches column-scoped emissions of model-only variants to model scope with a WARNING + new `parser_reshaped` audit field. (#184)"); `[Unreleased]` § Changed (one line: "`LLMResponseEvent.audit_schema_version` bumped 1 → 2 to carry the new `parser_reshaped` field.").

**TDD:** Read-time correctness; reviewed in QG Pass 4 (docs).

**Done when:** All four files updated. Validation command green (no test failures from doc edits).

**Depends on:** US-001 (prose rewrite drives the operator-facing teaching), US-003 (re-attach drives the rule-file additions), US-005 (audit field drives the version bump prose).

### US-007 — Phase B re-aggregation against intuit_airflow substrate

**Description.** Maintainer-side validation against the operator's `~/Projects/intuit_airflow/plugins/dbt/` substrate per the #179 "Reproducing this retest" recipe. Reset `signalforge.yml` workaround (`llm.exclude_tests: [row_count_anomaly_by_period]`), re-run `signalforge generate` against all 15 Phase B candidates, tally outcomes.

**Traces to:** DEC-008.

**Files (this repo):**
- `docs/research/179-test-primitive-expansion-retest.md` — append "Followup #184 resolution" section: per-model pass/fail table; scope-correctness count (anomaly variant proposed at model-level / column-level / not proposed); count of `parser_reshaped` events observed (load-bearing — should be RARE if the prompt fix is sufficient); before/after `_PROMPT_VERSION` (`e568fb3e4602e465` → new hash); one paragraph of narrative.

**Files (operator-side, outside repo):**
- `~/Projects/intuit_airflow/plugins/dbt/signalforge.yml` — remove the `llm.exclude_tests: [row_count_anomaly_by_period]` line (manual cleanup as part of validation; not committed).

**TDD:** N/A — this is integration validation, not a pytest gate.

**Done when:**
- All 15 candidates run.
- Tally + narrative landed in `docs/research/179-test-primitive-expansion-retest.md`.
- At least the 3 originally-failing candidates (`taxday_auction_insights` / `tvp_yelp` / `core_hourly_performance`) succeed AND propose `row_count_anomaly_by_period` at model scope.
- Validation command green (the writeup edit is markdown — no test impact).

**Depends on:** US-001, US-003, US-004, US-005, US-006 (must be code-complete before validation).

### US-008 — Plan file + CHANGELOG land in the same commit set

**Description.** Commit this plan document (`plans/super/184-anomaly-scope-fix.md`) alongside the implementation. Mirrors #183's posture (plan file ships in the same PR as the implementation).

**Traces to:** DEC-009.

**Files:** `plans/super/184-anomaly-scope-fix.md` (this document — already committed at Phase 5 publish).

**TDD:** N/A.

**Done when:** Plan committed. Validation command green.

**Depends on:** All other US.

### US-009 — Quality Gate (4x code-review + CodeRabbit)

**Description.** Run `/code-review` four times against the full changeset with four distinct angles (correctness / conventions / tests / docs+UX), fixing all real bugs found each pass. Then run `/ultrareview` for the CodeRabbit pass. Confirm validation command stays green after all fixes.

Specific QG focus areas (from architecture review + memory hits):
- **Conventions pass:** AST scans (logger grep gate, audit-completeness scan, single-construction seams); drift-detector v1+v2 dual coverage; `exclude_tests` gate honoured.
- **Correctness pass:** Re-attach + collect-all interaction (no short-circuit); WARNING fires exactly once per reshape (no double-fire on re-validation); `parser_reshaped` tuple is in the same order as reshapes occurred (deterministic).
- **Tests pass:** Cached-block byte-cap headroom under canonical fixture (the new prose adds bytes); planted-violation self-check on the new audit-completeness scan if any added; per-variant test parity (all three model-only variants have re-attach happy-path coverage).
- **Docs+UX pass:** Operator-facing prose in `docs/draft-ops.md` reads cleanly; rule-file additions don't break existing § anchor links (`mkdocs-atx-in-fenced-block-breaks-anchors` memory — verify any ATX in fenced blocks).

**Per `qg-pass-3-defer-defensive-tests-fails-codecov`:** any nice-to-have defensive test surfaced during QG that covers a line in the patch diff must be promoted to MUST-FIX before merge (codecov holds patch coverage to project standard regardless).

**Per `drive-by-format-reveals-merge-induced-drift`:** orchestrator runs `ruff format --check .` after merging each worker branch + before pushing.

**Traces to:** Quality-gate convention; all DECs reviewed.

**Done when:** 4 review passes complete; all real bugs fixed; `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` green; CodeRabbit clean or all comments addressed.

**Depends on:** All US-001 through US-008.

### US-010 — Patterns & Memory

**Description.** Update `.claude/rules/` + write a new memory if patterns emerged. Specifically:
- `.claude/rules/llm-drafter.md` — verify the re-attach + audit-bump prose is clear and references the right DECs.
- `.claude/rules/business-rule-tests.md` — verify the model-only variant constraint is now documented as "parser-enforced via re-attach" rather than "parser-rejected fail-loud" (the implicit semantic from #169/#170/#171).
- Consider a new memory: `signalforge-parser-reattach-pattern.md` if the re-attach + WARNING + audit-field shape is reusable for a future v0.x situation (e.g. a hypothetical 8th model-only variant). The memory should name the three-layer pattern (mutate + WARNING + audit-record) and the three load-bearing invariants (respect exclude_tests, preserve collect-all, always emit WARNING).

**Traces to:** Cross-cutting convention; final.

**Done when:** Rule files updated where applicable; new memory written (or explicit decision to skip if no pattern emerged); validation command green.

**Depends on:** US-009.

---

## Phase 5 — Publish PR

Pending. After user approval of the breakdown.

---

## Beads Manifest

Pending devolve.


