# 163 — Drafter business-rules fidelity

## Meta

- **Ticket:** [#163](https://github.com/wjduenow/SignalForge/issues/163) — `test_e2e_business_rules: drafter ignores meta.signalforge.business_rules and hallucinates an unrelated rule`
- **Branch:** `feature/163-drafter-business-rules`
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/163-drafter-business-rules`
- **Phase:** `complete` (PR [#164](https://github.com/wjduenow/SignalForge/pull/164); beads epic `bd_1-scaffolding-74b` — all stories closed)
- **Sessions:** 1 (2026-05-29)

## Symptom

Live e2e run (2026-05-29, drafter=`claude-sonnet-4-6`, Austin bikeshare fixture). The `tests/cli/test_e2e_business_rules.py::test_e2e_business_rules_drafts_prunes_custom_sql` injects **two** rules into `meta.signalforge.business_rules`:

1. **Tautology** (always-passes): `duration_minutes must always be greater than or equal to itself …`
2. **Engineered failing rows** (kept): `every trip must start and end at the same station (a row violates this rule when start_station_id <> end_station_id)`

The drafter emitted **one** `custom_sql` test matching **neither** rule:
```sql
SELECT trip_id, duration_minutes FROM {{ this }} WHERE duration_minutes <= 0
```

Pipeline completed cleanly (exit 0, audits intact, cost rollup unaffected). The failure is the drafter's instruction-following on `business_rules` — not anything SignalForge's own code paths control directly.

## Discovery findings

### Current rendering shape (verified in code)

- `src/signalforge/draft/prompts.py:557-579` — `_render_business_rules_section(model)` renders rules as a plain bulleted list under `## BUSINESS RULES` in the **dynamic block**:
  ```text
  ## BUSINESS RULES

  Operator-supplied business rules for this model. Draft one custom_sql test per rule, translating each into a failing-rows SELECT (a non-empty result means the rule was violated):

  - (model) duration_minutes must always be greater than or equal to itself …
  - (model) every trip must start and end at the same station …
  ```
- `src/signalforge/draft/prompts.py:104-115` — the **only** business-rules instruction in the cached system prompt (`_CUSTOM_SQL_SCOPE_INSTRUCTION`):
  > "If a BUSINESS RULES section appears in the data block below, draft one `custom_sql` test per stated rule, translating the natural-language rule into a failing-rows SELECT. When no business rules are supplied, you MAY still infer `custom_sql` tests …"
- `_PROMPT_VERSION` (`src/signalforge/draft/prompts.py:298-308`) is a `blake2b-8` hash of `_SYSTEM_PROMPT + _MANIFEST_SUMMARY_TEMPLATE + JSON(_DATA_SECTION_TEMPLATES)`. System-prompt changes rotate it; dynamic-block changes do not.
- Parser anchor contract (`src/signalforge/draft/parser.py`) currently has **no rule-to-test cardinality check**. A run with 2 rules and 0 custom_sql tests passes validation silently.
- Test injection helper: `tests/cli/_e2e_helpers.py:137-188` `inject_model_business_rules` writes both `config.meta.signalforge.business_rules` AND `meta.signalforge.business_rules` (belt-and-braces).
- Existing unit tests for rendering: `tests/draft/test_prompts.py:410-426` only check that the literal rule strings + `"## BUSINESS RULES"` appear — no cardinality / numbering / envelope shape pinned.

### Convention-checker constraints (the load-bearing watch-outs)

1. **`business_rules` stay in the dynamic block, not the cached system prompt** (`business-rule-tests.md` DEC-001). Moving per-rule text into the cached prompt would invalidate Anthropic prompt-cache for every model that has rules. — **load-bearing**.
2. **`_PROMPT_VERSION` rotates if and only if the cached system prompt template changes** (`llm-drafter.md` § "Cached-block scope"). If we tighten the SCOPE instruction text (Lever A), bump the version AND regenerate `tests/llm/test_prompt_cache_stability.py` golden in lockstep.
3. **Prompt-injection envelope guard** (`llm-drafter.md` DEC-007). If we wrap rules in `<BUSINESS_RULE>` tags, `_render_dynamic_block` must raise `PromptEnvelopeBreachError` when any rule contains the closing tag. Mirrors the `<MODEL_SQL>` precedent.
4. **Conservative-bias / no new `DropReason`** (`prune-engine.md`). A parser-side rejection of "too few `custom_sql` tests" should surface as `LLMOutputAnchorContractError` (collect-all violations), NOT a new prune drop-reason — the prune stage never sees the rejected candidate.
5. **Gate-over-prompt** (`testing-signal.md`). The fix must be verifiable at unit-test time without an LLM in the loop — a unit test that injects a 2-rule payload and a hand-rolled bad candidate must reject; a 2-rule payload + matching candidate must accept.
6. **Fail-closed audit invariant**: bad-JSON / parse-fail responses still write no audit row. A new parser rejection path must run BEFORE the audit write (current ordering already satisfies this).
7. **Tolerant JSON extraction is the only JSON-only guarantee on `claude-sonnet-4-6`** (`llm-drafter.md` § "Tolerant JSON extraction" / issue #144). Assistant-turn prefill is API 400. Don't propose prefill-based hardening.
8. **Provider-neutral seam** (`llm-drafter.md` DEC-006 of #135). System-prompt / dynamic-block / parser changes apply uniformly across Anthropic, OpenAI, Gemini — no branching on provider.
9. **Exit-code lockstep** (`cli-layer.md` 7th AST scan). If we introduce a new error class, register it in `_EXCEPTION_TO_EXIT_CODE`. The simplest path — re-use `LLMOutputAnchorContractError` — needs no new entry.
10. **Skill-parity + 5-surface graduation** (`skill-parity.md` / `cli-layer.md`). No CLI flag is being added; no SKILL.md / `docs/cli-ops.md` parity work expected. `docs/draft-ops.md` + `business-rule-tests.md` itself ARE in scope.

### Domain-expert lever analysis (5 levers evaluated)

| Lever | Cost | False-pos risk | Gate? | LOC |
|---|---|---|---|---|
| **A. System-prompt restructure** (strengthen "MUST emit one per rule"; remove "MAY infer" when rules present) | rotates `_PROMPT_VERSION` + snapshot | moderate (model may over-emit) | prompt-only | ~20 |
| **B. Dynamic-block hardening** (number rules, wrap each in `<BUSINESS_RULE id="N">…</BUSINESS_RULE>` + envelope-breach guard) | none (dynamic block) | low | prompt-only | ~10 |
| **C1. Parser count-only gate** (reject when `custom_sql_count < business_rule_count`) | none (parser-side) | very low | **full gate** | ~30 |
| **C2. Parser rule-ID gate** (require each test attribute itself to a rule via new `rule_id` field) | schema bump + prompt rotation | very low | full gate | ~80 |
| **D. Coverage warning only** | none | none | no enforcement | ~15 |
| **E. Two-step drafting** (one call for built-ins, one for `custom_sql` per rule) | **2× LLM cost** | moderate | prompt-focused | ~100 |

**Domain expert recommendation:** Combine **B + C1** as the minimal high-confidence fix; consider escalating to A only if real-world compliance stays low after B+C1 lands.

## Scoping decisions (Phase 1)

- **Q1 → Lever B + C1.** Dynamic-block hardening (numbered `<BUSINESS_RULE id="N">…</BUSINESS_RULE>` envelopes + breach guard) AND parser cardinality gate (`LLMOutputAnchorContractError` violation when count < N). No system-prompt restructure, no `_PROMPT_VERSION` rotation.
- **Q2 → at-least-one-per-rule.** Gate rejects only when `custom_sql_count < len(business_rules)`. Excess is allowed (legitimate multi-test decomposition of a complex rule).
- **Q3 → unit-level only.** Hand-rolled candidates in `tests/draft/test_parser.py` for the gate; rely on the existing `tests/cli/test_e2e_business_rules.py` as the live-pipeline cert.
- **Q4 → thread through `parse_draft_response`.** Pass `business_rules: tuple[str, ...]` from `draft_from_request` → `parse_draft_response` → `_validate_anchor_contract`. Mirrors `model_columns_by_type` threading from #159 — single source of truth, no re-read drift risk.

## Architecture review

| Area | Rating | Note |
|---|---|---|
| Security | pass (with envelope guard) | `<BUSINESS_RULE>` envelope mirrors `<MODEL_SQL>`; closing-tag substring scan rejects rules that would break the fence. Defence-in-depth — operator content already passes the safety layer's ANSI strip earlier. |
| Performance | pass | ≤1KB added to dynamic block per typical N=2–5 rules; zero cached-block impact; O(N_tests) parser gate. |
| Data model | pass | No `CandidateSchema` / `CandidateTestCustomSQL` field changes. No `audit_schema_version` bump. |
| API design | pass | `parse_draft_response` gains keyword-only `business_rules: tuple[str, ...] = ()` (non-breaking; all 36 existing parser-test call sites work unchanged). |
| Observability | pass | Re-uses existing multi-violation `LLMOutputAnchorContractError` stderr shape (`cli-layer.md` DEC-008). No new `_LOGGER` calls. |
| Testing | pass | Unit-level cardinality gate verifiable with hand-rolled candidates (no LLM); existing e2e is the live cert. |
| Cache stability | pass | No `_PROMPT_VERSION` rotation; cached-block + golden snapshot untouched. |
| Provider neutrality | pass | Parser gate is provider-independent; dynamic block stays provider-neutral. |
| Exit-code lockstep | pass | No new error class. Re-uses tier-2 `LLMOutputAnchorContractError` and tier-2 `PromptEnvelopeBreachError` (parameterised). |
| 5-surface parity | pass | No CLI flag / no SKILL.md change. Docs touches: `business-rule-tests.md` cardinality + envelope; `llm-drafter.md` parameterised breach pattern. |

**Blockers: 0. Concerns: 0 (after refinement Q5–Q7).**

## Refinement log

### Decisions

- **DEC-001 — Fix shape: Lever B + C1.** Dynamic-block hardening (numbered `<BUSINESS_RULE id="N">…</BUSINESS_RULE>` envelopes) + parser cardinality gate (`LLMOutputAnchorContractError` violation when count < N). System-prompt restructure (Lever A) is held in reserve if Sonnet 4.6 compliance remains low after this fix lands. **Rationale:** gate-over-prompt per `testing-signal.md`; no `_PROMPT_VERSION` rotation; minimal LOC; verifiable without LLM.
- **DEC-002 — Cardinality is at-least-one-per-rule.** `count >= len(business_rules)` accepts. Excess allowed (the LLM may legitimately split a complex rule into two SELECTs). Exact equality conflates over- and under-coverage; per-scope (model vs. column) requires a `rule_id` field we explicitly didn't add.
- **DEC-003 — Unit-level parser tests + reuse existing e2e.** Hand-rolled candidates exercise the new gate without LLM cost. `tests/cli/test_e2e_business_rules.py` is already the live reproduction and certifies the round-trip after the fix.
- **DEC-004 — Thread `business_rules` through `parse_draft_response` (mirror #159).** Keyword-only `business_rules: tuple[str, ...] = ()` on `parse_draft_response` and `_validate_anchor_contract`. Built in `draft_from_request` (`schema.py`) from `_read_business_rules(model)`. Single source of truth; mirrors `model_columns_by_type` threading from issue #159 verbatim.
- **DEC-005 — Reuse `PromptEnvelopeBreachError` with parameterised envelope.** Extend `__init__` with `envelope: str = "MODEL_SQL"` and `rule_index: int | None = None` kwargs (both default-safe; existing call site untouched). Message renders `</MODEL_SQL>` or `</BUSINESS_RULE>` per envelope. No taxonomy growth, no new exit-code entry.
- **DEC-006 — Violation message names rules verbatim.** When the parser gate fires, the violation lists every declared business rule (prefixed `(model)` or `(column X)` per the renderer's existing prefix). Since cardinality is at-least-one-per-rule (DEC-002), we can't identify a specific missing rule — the operator gets the full declared set + the actual `custom_sql` count. Message shape pinned by test.
- **DEC-007 — No INFO breadcrumb on rule injection.** The `LLMResponseEvent` audit row already carries `response_text_hash` + the prompt that triggered it. Adding an INFO line per render is noise in default-quiet runs.
- **DEC-008 — `exclude_tests=("custom_sql",)` short-circuits both surfaces.** When the operator forbids `custom_sql`, `_render_business_rules_section` returns `""` (don't tell the LLM to draft rules it can't emit) AND `_validate_anchor_contract` skips the cardinality gate (no rules in scope). Mirrors how `_render_system_prompt(exclude_tests)` already drops `custom_sql` from the catalogue.
- **DEC-009 — Envelope format.** Per rule: opening tag `<BUSINESS_RULE id="N">` on its own line; rule text indented 2 spaces on the next line(s); closing tag `</BUSINESS_RULE>` on its own line. IDs start at 1. Section header `## BUSINESS RULES` + lead-in prose unchanged. Pinned by test.

## Stories

Each story is right-sized for one Ralph context window. Acceptance criteria trace to DECs; the canonical validation command (`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`) is the floor for every story.

### US-001 — Dynamic-block envelope hardening + parameterised breach guard

**Traces to:** DEC-001, DEC-005, DEC-008, DEC-009.

**Description:** Replace the bulleted `## BUSINESS RULES` list in `_render_business_rules_section` with numbered `<BUSINESS_RULE id="N">…</BUSINESS_RULE>` envelopes. Extend `PromptEnvelopeBreachError` to be envelope-parameterised and add a pre-render breach guard for the new envelope.

**Acceptance criteria:**

1. `_render_business_rules_section(model)` emits `<BUSINESS_RULE id="N">\n  <rule text>\n</BUSINESS_RULE>` per rule (N starts at 1, body indented 2 spaces). Section header `## BUSINESS RULES` and lead-in prose unchanged.
2. The rule body still carries the existing scope prefix (`(model)` / `(column X)`) verbatim.
3. `_render_business_rules_section` short-circuits to `""` when `custom_sql` is in `DraftConfig.exclude_tests` (passed in from the renderer via the existing thread).
4. `PromptEnvelopeBreachError.__init__` accepts new keyword-only args: `envelope: str = "MODEL_SQL"`, `rule_index: int | None = None`. Existing single call site in `prompts.py` keeps working unchanged.
5. Rendered message: when `envelope="MODEL_SQL"`, byte-equal to the current `</MODEL_SQL>` message; when `envelope="BUSINESS_RULE"`, mentions the rule index (`"… in rule #2 of model.x.y …"`).
6. `_render_business_rules_section` scans each rule for the literal `</BUSINESS_RULE>` substring (boring substring match, no whitespace normalisation per `llm-drafter.md` DEC-007/grade-layer.md envelope-breach precedent) and raises `PromptEnvelopeBreachError(model.unique_id, envelope="BUSINESS_RULE", rule_index=i)`.
7. `uv run pytest tests/draft/test_prompts.py` passes; `uv run pytest tests/llm/test_prompt_cache_stability.py` passes **with no `_PROMPT_VERSION` change** (this is the load-bearing check that we didn't accidentally touch the cached system prompt).
8. Canonical validation command passes.

**Done when:** new envelope shape renders, breach guard fires loudly on poisoned rules, and `_PROMPT_VERSION` is byte-identical to its pre-fix value.

**Files:**

- `src/signalforge/draft/prompts.py` — rewrite `_render_business_rules_section`; thread `exclude_tests: tuple[str, ...]` (or read from the existing config seam); add breach scan.
- `src/signalforge/draft/errors.py` — parameterise `PromptEnvelopeBreachError.__init__`.
- `tests/draft/test_prompts.py` — update `test_business_rules_render_into_dynamic_block` for the new shape; add `test_business_rules_section_short_circuits_when_custom_sql_excluded`; add `test_business_rules_envelope_breach_guard_fires_on_closing_tag`; add `test_business_rules_envelope_breach_message_includes_rule_index`.
- `tests/draft/test_errors.py` *(if exists; otherwise add to nearest)* — pin parameterised `PromptEnvelopeBreachError` message for both envelopes; assert existing `MODEL_SQL` byte-equal.

**Depends on:** none.

**TDD:** yes — write the envelope-shape test, breach-guard test, and parameterised-error tests FIRST. Implement until all pass.

### US-002 — Parser cardinality gate + business_rules threading

**Traces to:** DEC-001, DEC-002, DEC-003, DEC-004, DEC-006, DEC-008.

**Description:** Add a keyword-only `business_rules: tuple[str, ...] = ()` to `parse_draft_response` and `_validate_anchor_contract`. Thread it from `draft_from_request` (the orchestrator already builds the rules tuple via `_read_business_rules(model)`). In `_validate_anchor_contract`, when `business_rules` is non-empty AND `custom_sql` is NOT in `exclude_tests`, append one violation if `custom_sql_count < len(business_rules)`.

**Acceptance criteria:**

1. `parse_draft_response` signature gains keyword-only `business_rules: tuple[str, ...] = ()` (default keeps every existing call site working). Public-API surface confirmed via `tests/draft/test_public_api.py` (or equivalent).
2. `_validate_anchor_contract` signature gains the same kwarg; existing `model_columns_by_type` threading is the precedent — match its placement and pyright-narrowing style.
3. `draft_from_request` (in `signalforge.draft.schema`) builds `business_rules = tuple(_read_business_rules(model))` AFTER the existing `model_columns_by_type` build and threads it through `parse_draft_response(...)`. (`_read_business_rules` already exists in `prompts.py`; expose / re-import as needed.)
4. Gate logic: when `business_rules` non-empty AND `"custom_sql" not in exclude_tests`, count `custom_sql` tests across `candidate.tests` + every `column.tests` and append one violation if `count < len(business_rules)`.
5. Violation message: `Expected ≥{N} custom_sql test(s) (one per declared business rule), got {actual}. Declared rules: {comma-separated quoted rule strings with their (model)/(column X) prefixes}.` Pinned by test.
6. Gate is a no-op when `business_rules = ()` (preserves all 36 existing parser-test call sites; backward compat).
7. Gate is a no-op when `"custom_sql"` is in `exclude_tests` (DEC-008).
8. Unit tests cover: under-coverage rejection (2 rules + 1 custom_sql → violation present); coverage match (2 rules + 2 → accept); over-coverage allowed (2 rules + 3 → accept); empty rules + zero custom_sql → accept; empty rules + custom_sql present → accept (inferred-fallback path preserved); custom_sql in exclude_tests + non-empty rules → no gate violation; column-level custom_sql tests counted; model-level custom_sql tests counted; mixed counted.
9. Multi-violation collect-all preserved: a candidate with a hallucinated column AND a cardinality miss produces BOTH violations in one `LLMOutputAnchorContractError`.
10. Canonical validation command passes.

**Done when:** the parser gate rejects an under-coverage response loudly with a verbose message, the inferred-fallback path stays open, and `tests/cli/test_e2e_business_rules.py` (the existing e2e) continues to be the live cert.

**Files:**

- `src/signalforge/draft/parser.py` — add kwarg + gate logic to `_validate_anchor_contract`; add kwarg to `parse_draft_response`.
- `src/signalforge/draft/schema.py` — build `business_rules` tuple from `_read_business_rules(model)`; thread through `parse_draft_response(...)`.
- `src/signalforge/draft/prompts.py` — confirm `_read_business_rules` is importable from `schema.py` (or re-export); no behavioural change.
- `tests/draft/test_parser.py` — 8+ new tests per AC #8.

**Depends on:** US-001 (the envelope-shape change is the operator-facing half; landing the parser gate without it would surface the rejection without giving the LLM the clearer input format — together they're the complete fix).

**TDD:** yes — write the 8 gate-behaviour tests FIRST against the unchanged parser (they should fail); implement until all pass.

### US-003 — Quality Gate (code review x4 + CodeRabbit)

**Traces to:** the project's standard Quality-Gate convention.

**Description:** Run the `/code-review` skill four times across the full diff, fix every real bug found each pass. Run CodeRabbit if available. The canonical validation command must pass after all fixes.

**Acceptance criteria:**

1. Four `/code-review` passes complete; all real findings landed as fixes (not deferred).
2. CodeRabbit review requested on the draft PR (when bot is configured); maintainer-deemed real findings landed.
3. `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` passes locally.
4. `uv run pytest tests/llm/test_prompt_cache_stability.py` passes with `_PROMPT_VERSION` unchanged from pre-fix.
5. No `Traceback` in any stderr from CLI subprocess smoke tests.

**Done when:** all four review passes are green, validation is green, the diff is the size we said it would be.

**Files:** any touched by US-001/US-002 (no scope expansion in this story).

**Depends on:** US-001 + US-002.

### US-004 — Patterns & Memory (docs + rules update)

**Traces to:** DEC-005, DEC-006, DEC-008, DEC-009; `business-rule-tests.md` conventions.

**Description:** Roll the durable conventions from this fix into the rule files + ops docs.

**Acceptance criteria:**

1. `.claude/rules/business-rule-tests.md` § "Two input paths, both in the dynamic prompt block" gains a sub-bullet documenting:
   - The numbered `<BUSINESS_RULE id="N">…</BUSINESS_RULE>` envelope (DEC-009).
   - The at-least-one-per-rule cardinality contract enforced at the parser (DEC-002, DEC-006).
   - The `exclude_tests=("custom_sql",)` short-circuit on both surfaces (DEC-008).
2. `.claude/rules/llm-drafter.md` § "`<MODEL_SQL>` prompt-injection envelope" gains a note that `PromptEnvelopeBreachError` is now envelope-parameterised (the second envelope `<BUSINESS_RULE>` shipped in #163) — and that future envelopes follow the same pattern (extend with a new `envelope=` arg, never a new error class).
3. `docs/draft-ops.md` (or wherever the operator-facing business-rules story lives) carries a short "Cardinality contract" subsection.
4. The plan doc's `Beads manifest` section is populated post-devolve (Phase 7).
5. Canonical validation command passes.

**Done when:** the rule files + ops doc reflect the new conventions, future contributors can find the pattern without re-reading the plan.

**Files:**

- `.claude/rules/business-rule-tests.md`
- `.claude/rules/llm-drafter.md`
- `docs/draft-ops.md` *(if it documents the business-rules path; check during the story)*
- `plans/super/163-drafter-business-rules-fidelity.md` — Beads manifest section.

**Depends on:** US-003.

## Beads manifest

- **Epic:** `bd_1-scaffolding-74b` — #163: drafter business-rules fidelity
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/163-drafter-business-rules`
- **Branch:** `feature/163-drafter-business-rules`
- **External ref:** `gh-163`

| Story | Bead ID | Depends on | Status | Commit |
|---|---|---|---|---|
| US-001 — Dynamic-block envelope hardening + parameterised breach guard | `bd_1-scaffolding-74b.1` | — | ✅ closed | `cfd3510` (merged via `0d32311`) |
| US-002 — Parser cardinality gate + business_rules threading | `bd_1-scaffolding-74b.2` | US-001 | ✅ closed | `2bbe092` (merged via `9731a1f`) |
| US-003 — Quality Gate (code-review x4 + 5 invariant tests landed) | `bd_1-scaffolding-74b.3` | US-001, US-002 | ✅ closed | `213e777` (inline) |
| US-004 — Patterns & Memory (rule files + ops docs) | `bd_1-scaffolding-74b.4` | US-003 | ✅ closed | this commit |

**Run summary:** Ralph autonomous run on 2026-05-30. 2 worker beads + 2 inline beads. Final validation: 2662 tests passed, 97.72% coverage. `_PROMPT_VERSION` unchanged at `c9e7ee1f6f465933` (load-bearing cache-stability gate held throughout).
