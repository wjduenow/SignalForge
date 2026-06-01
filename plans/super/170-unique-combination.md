# #170 — `unique_combination` as the 7th first-class test primitive

## Meta

| Field | Value |
|---|---|
| Ticket | https://github.com/wjduenow/SignalForge/issues/170 |
| Parent epic | #179 — Test Generation Expansion |
| Sequencing dependency | #169 — `row_count_between` (shipped 2026-05-31, commit `2fdb91e`) |
| Sibling | #154 — prune+grade adapter for existing dbt-expectations tests (ingest seam — coordinate, don't merge) |
| Branch / worktree | `feature/170-unique-combination` at `/home/wesd/Projects/worktrees/SignalForge/170-unique-combination` |
| Phase | **published** (Phases 1–4 complete; awaiting PR review) |
| Sessions | 1 (2026-05-31) |

## Phase 1 — Discovery

### Ticket summary

Add `unique_combination` (uniqueness over a tuple of ≥2 columns; optional `where` filter) as the **7th `CandidateTest` variant** the drafter proposes, the prune engine compiles, the grader scores, and the diff layer renders. Follows #169's `row_count_between` precedent — variant extension is now a **2-instance precedent** in `business-rule-tests.md` and #170 makes it a 3rd.

The motivating evidence is the `intuit_airflow` survey (143 declared tests across 104 Snowflake models): `dbt_utils.unique_combination_of_columns` + two in-house composite-uniqueness macros account for **15 of 143 tests (10.5%)**. After #169 ships, SignalForge auto-generates ~76% of `intuit_airflow`'s test shapes; adding `unique_combination` lifts that to ~86%. The demo gap is concrete: `signalforge generate` against `weekly_query_cost.sql` today emits a freeform `custom_sql GROUP BY query_signature, query_type HAVING COUNT(*) > 1` instead of a structured `unique_combination(columns=[query_signature, query_type])` — the grader has to reason about it ad-hoc instead of against a per-primitive rubric.

The ticket also folds in a **piggyback documentation story** — author a "What tests SignalForge generates" surface in the README + docs site so prospective evaluators can decide at a glance whether SignalForge's catalogue covers their patterns. Currently a reader has to grep `docs/draft-ops.md` and rule files to discover the primitive list.

### Discovery findings (parallel subagent research)

#### A. The pattern is well-trodden — variant extension is mechanical

The 6 production dispatch sites established by `business-rule-tests.md` § "The 6 production dispatch sites" are all located with file:line precision; the `row_count_between` arm is the template:

| # | Module | Function | Entry | `row_count_between` arm |
|---|---|---|---|---|
| 1 | `prune.compiler` | `_compile_test` | `src/signalforge/prune/compiler.py:905` | `:1017–1025` (helper at `:809–903`) |
| 2 | `_common.artifact_id` | `model_test_args_hash` | `src/signalforge/_common/artifact_id.py:63` | `:104–117` |
| 3 | `diff._emitter` | `_render_test` | `src/signalforge/diff/_emitter.py:132` | `:165–173` |
| 4 | `ingest.parser` | `_parse_named_test` | `src/signalforge/ingest/parser.py:196` | `:216–223` (helper at `:258–372`) |
| 5 | `draft.parser` | `_validate_anchor_contract` | `src/signalforge/draft/parser.py:362` | `:499–524` |
| 6 | `ingest.anchor` | `validate_anchor_contract` | `src/signalforge/ingest/anchor.py:36` | `:82–89` (early-out) |

Supporting infrastructure also located: `CandidateTest` union at `draft/models.py:246–254`; `VALID_TEST_TYPES` at `draft/config.py:58–73`; `_TEST_CATALOGUE_LINES` at `draft/prompts.py:59–82`; `_PROMPT_VERSION = "77e9ee8a6ae7d875"` at `draft/prompts.py:311–321`; pinned at `tests/llm/test_prompt_cache_stability.py:77`; drift mirror at `tests/draft/test_drift_detector.py:70–77`; candidate-schema row at `tests/fixtures/draft/candidate_schema_v1.json:43`.

#### B. The genuinely-new questions (deltas vs. #169)

##### B.1 — `where: str | None` scope (P1 scoping question)

Issue body line 43 explicitly raises: ship `where: None` only in v1 + defer `where`-conditional to a sub-issue? Material tradeoff:

- **With `where` in v1:** epic projection holds (+10 pp / 15 of 143 — covers `dbt_utils.unique_combination_of_columns` × 13 + `bucket_subbucket_uniqueness` × 1 + `unique_if_not_null` × 1). Inherits #169's sqlglot type-coherence reuse pattern verbatim.
- **Without `where` in v1:** coverage drops to +9 pp (13 of 143, just the bare `dbt_utils.unique_combination_of_columns`). Smaller surface, narrower safety/grading scope.

##### B.2 — Sample-mode semantics is a genuinely new shape

`row_count_between` (#169 US-007a) and the `business-rule-tests.md` § "Materialised-sample substitution" rule give a decision rule for the next variant: **row-level vs. metadata/aggregate?** `unique_combination` doesn't fit cleanly:
- It IS row-level (GROUP BY ... HAVING returns duplicate-key rows).
- BUT it's **approximate-on-sample** — a uniqueness violation in the full table may NOT surface in the sample (the sample looks unique, but the full table has duplicates → false negative → drafted test ships uncertain).

Three routings, none obvious-default:
- **(i)** Consume `table_ref` as-is per the row-level rule → accept false-negative risk; document.
- **(ii)** Route to `kept-without-evidence` when `scope="sample"` with `why="composite uniqueness on sample is approximate"` → conservative, but operators in sample mode get no signal at all.
- **(iii)** Engine override to source (mirror #169) → always full-scan via GROUP BY; bytes-billed cap remains the cost guardrail.

Recommend Phase 2 architecture subagent review.

##### B.3 — Grain-meaningfulness in the grade rubric

A `unique_combination(columns=[primary_key, anything])` is vacuously unique (the prune step catches it as `always-passes`, but the grader is the *conceptual-error gate before warehouse cost*). The 4 default criteria today are `clarity` / `consistency` / `rationale` / `no-redundant`. #169 DEC-009 already extended `no-redundant` with parenthetical calibration prose for `row_count_between` vacuous bounds — same shape applies.

**Finding (load-bearing):** `business-rule-tests.md` § "Lockstep `_PROMPT_VERSION` rotation when extending the catalogue (#169 DEC-012)" claims a grade-side `_PROMPT_VERSION` constant rotates in lockstep — **but no such constant exists today** (verified — `grep` only finds `rubric_hash` / `criterion_prompt_hash` per-event surfaces; no module-level snapshot constant; no `tests/grade/test_prompt_cache_stability.py`). The rubric refinement bumps `rubric_hash` *dynamically* (it's computed from canonical-JSON of the rubric), but there is no snapshot-pinned regression gate. Either the rule-file claim is overstated, or #170 must *establish* the grade-side cache-stability surface as a pre-requisite. Phase 2 will adjudicate.

##### B.4 — Catalogue source-of-truth for the piggyback story

The ticket suggests either extending `business-rule-tests.md` or creating `.claude/rules/drafter-catalogue.md`. Convention Checker recommends a third option: a new **`docs/drafter-catalogue.md`** ops doc — keeps the rule file pattern-focused (architectural), keeps catalogue-as-examples in the operator-facing tier, mirrors the established `.claude/rules/` vs. `docs/*-ops.md` split (`CLAUDE.md` line 39).

#### C. Other findings worth surfacing

- **dbt_utils package detection — decision rule already exists.** Per `business-rule-tests.md` § "On-disk artifact" (verbatim from rule file): "No `.sql` fallback in v0.3; no `packages.yml` detection. Operators without `dbt-expectations` installed see the YAML, get a clear `dbt parse` error, and either install or remove." This decision generalises to `dbt_utils` — same posture; no detection seam needed. (Confirms the ticket's open question.)
- **Variant name `unique_combination`** wins on grep parity with the dbt-utils macro it most directly replaces; `composite_unique` was the alternative. Likely confirms in P3.
- **Existing fixture gap.** The bikeshare fixture has no multi-column GROUP BY model to e2e-test the drafter's freeform→structured translation. Either engineer one or pin the e2e against an injected test scenario (mirroring #169's `inject_model_business_rules` pattern). Phase 4 detailing.
- **AST scan-7 count NOT bumped** — #170 introduces no new `errors.py` module. Existing scans 1–10 cover construction-location bypasses (LLMRequest, AuditEvent, SDK clients, response events); none guard the `CandidateTest` union itself. Convention Checker flagged that a missing arm on the 7th variant would be a latent runtime crash (open at runtime). Phase 2 to decide: is the 6-arm pattern test-coverage-enforced today, or does #170 need an exhaustive-arm AST scan?
- **`_artifact_id` canonical form for `columns` tuple.** Sort-and-dedupe in canonical-JSON vs. order-preserving. Order-preserving is the cleaner semantic (LLM proposed `(a, b)` not `(b, a)`) but might trigger meaningless cache misses. Phase 2 architecture decision.
- **Issue #154 sibling.** Stay siblings, not coordinated — #170 ships the `dbt_utils.unique_combination_of_columns` ingest recognition in lockstep with the drafter (same shape as #169). #154 picks up the rest of the dbt-expectations / dbt-utils recognition surface independently.

### Proposed scope

- **Code:** 7th `CandidateTest` variant + 6-arm dispatch growth + drafter prompt catalogue entry + drafter `_PROMPT_VERSION` rotation + drift mirror + candidate-schema fixture row.
- **Grade:** extend `no-redundant` criterion text with calibration prose for grain-meaningfulness; possibly *establish* grade-side cache-stability surface (Phase 2 adjudicates).
- **Tests:** snapshot fixtures (compiler, prompt-cache, anchor-contract collect-all, drift detector); compiler dialect-portability (BigQuery + Snowflake snapshot byte-equality where applicable); ingest parser + ingest anchor exemption; an engineered-fixture e2e demonstrating drafter freeform→structured translation.
- **Docs:** README "What tests SignalForge generates" + `docs/draft-ops.md` paraphrase + new `docs/drafter-catalogue.md` (or extension to `business-rule-tests.md` — Phase 1 scoping question) + CHANGELOG `[Unreleased]` entry.
- **Quality gate + Patterns & Memory** (standard tail).

Out of scope (deferrals): epic-level retest verification against `intuit_airflow` (lives in #179); `bucket_subbucket_uniqueness` / `unique_if_not_null` ingest recognition (custom in-house macros, no public signature); `expect_column_to_exist` (per ticket — schema-declaration, not behavioural).

### Scoping answers (Phase 1 decisions)

- **S1 — `where` scope.** Ship `where: str | None` in v1. Reuses #169's sqlglot type-coherence verbatim; holds the epic #179 +10 pp / 15-of-143 coverage projection. The two custom-macro shapes (`bucket_subbucket_uniqueness`, `unique_if_not_null`) are covered semantically.
- **S2 — Catalogue SSOT.** New `docs/drafter-catalogue.md` ops doc. Keeps rule files context-light; matches the established `.claude/rules/` (architecture) vs. `docs/*-ops.md` (operator examples) split. Linked from README + `business-rule-tests.md` + `draft-ops.md`.
- **S3 — Docs story scope.** Include in #170 — code + docs ship in one PR. Two tail stories: SSOT authoring + README/CHANGELOG/site-verify.
- **S4 — E2E fixture.** Engineer a new committed fixture model (`stg_bikeshare_station_pairs.sql` or similar) with natural composite-key uniqueness. Pinnable AC: the drafter emits structured `unique_combination` against the new fixture, not freeform `custom_sql`.

---

## Phase 2 — Architecture Review

Five parallel reviews ran: Security, Performance, Data Model + API Design, Testing Strategy, Observability + Ops Docs. Detailed findings live in session transcripts; the action-relevant summary:

### Ratings table

| Area | Rating | Headline |
|---|---|---|
| Security — `where` safety contract | pass | Reuses #169 DEC-005 compose-then-`validate_test_sql` pattern verbatim. |
| Security — prompt-injection envelope | pass | `where` arrives back from LLM in the JSON response, not the prompt input; existing envelope contract holds. |
| Security — ingest trust (`dbt_utils.*`) | pass | Operator-authored YAML, not LLM text. `yaml.safe_load` + 5 MB cap already in place; collect-all anchor pattern unchanged. |
| Security — `columns[i]` validation + case-folding | **concern** | Phase 4 must confirm the compiler arm iterates `validate_identifier` + `_fold_identifier(_quote)` per column, matching `_compile_unique` shape (`prune/compiler.py:407,414`). |
| Security — custom `__repr__` for PII-bearing fields | **concern** | Default Pydantic repr exposes `where`/`sql`/`rationale`. Neither `CandidateTestRowCountBetween` nor `CandidateTestCustomSQL` redacts today; the rule-file convention (`prune-engine.md` DEC-022) explicitly applies to **result** shapes, not candidate shapes. Phase 3 decides whether to establish for #170 or defer. |
| Performance — sample-mode semantics for composite uniqueness | **blocker** | Three routings; Performance + Testing reviews converge on **Option (iii)** — engine override to source via `per_test_table_ref` (mirror #169 US-007a). Same shape as `row_count_between`'s metadata-bypass; just extend the existing conditional at `prune/engine.py` to include `unique_combination`. Pin with a behavioural test. |
| Performance — `args_hash` canonical form for `columns` tuple | **concern** | Reviews diverge: Performance says **SORT** (precedent: `accepted_values.values` sorts at `_common/artifact_id.py:79–84`); Data Model says **PRESERVE** (LLM intent). Phase 3 question. |
| Performance — GROUP BY cost shape | pass | Same shuffle cost as single-column `unique`; bounded by `maximum_bytes_billed`. Documented in catalogue; no new guardrail. |
| Performance — `where` pushdown cost | pass | Plain SQL pushdown; same multi-table classifier as `custom_sql`. |
| Data Model — Pydantic variant shape (`columns: tuple[str, ...]`) | pass | Mirrors `CandidateTestAcceptedValues.values: tuple[str, ...]` at `draft/models.py:82`. JSON arrays deserialise cleanly. |
| Data Model — `len(columns) >= 2` + no-duplicates validators | pass | Pattern is `@field_validator` for len + `@model_validator(mode="after")` for duplicates (mirrors `CandidateTestRowCountBetween._bounds_consistent` at `draft/models.py:230–243`). |
| Data Model — Per-column identifier check site | pass | At the anchor-contract arm (`draft/parser.py:362`), not Pydantic. Matches `accepted_values.values` / `relationships.to`/`.field` precedent (validation happens at parse-time anchor check, not on the candidate shape). |
| Data Model — Discriminated-union extension | pass | Append to `CandidateTest` union at `draft/models.py:246–254`; extend `__all__`. Pydantic v2 enforces discriminator at parse time. |
| Data Model — Drafter prompt catalogue entry shape | pass | Two forms (no-`where` + with-`where`); cautionary prose steering away from vacuously-unique tuples. Rotates `_PROMPT_VERSION`. |
| Data Model — Grade rubric refinement | pass (decision: **extend `no-redundant`**) | Cross-review consensus on Option A. Add calibration prose naming `unique_combination` + the `(pk, anything)` vacuous shape inside the existing `no-redundant` criterion text. |
| Data Model / Convention — Grade-side `_PROMPT_VERSION` analog | **concern** | `business-rule-tests.md` claims a grade-side rotation contract that does NOT exist (verified — no `signalforge.grade._PROMPT_VERSION`, no `tests/grade/test_prompt_cache_stability.py`; only dynamic `rubric_hash` on each event). Two paths: (a) establish the snapshot surface as a #170 story; (b) document the asymmetry and amend the rule file. Phase 3 picks. |
| Data Model — `_artifact_id` formatter | pass | Pattern `test.model.unique_combination.<args_hash>` on collision; `_model_test_args_hash` recipe extended to include `columns` + `where`. Sort/preserve canonical form is the open Q above. |
| Data Model — Ingest field-naming seam (`columns` ↔ `combination_of_columns`) | pass | Two functions, one each side of ingest+diff (mirrors #169's `minimum`/`min_value`). |
| Data Model — Ingest strictness for malformed dbt_utils args | pass (decision: **`SkippedTest(reason="malformed-supported-test")`**) | Cross-review consensus mirrors `_parse_row_count_between` strictness. Duplicate columns also → `SkippedTest` (not silently dedupe). |
| Data Model — AST/mechanic exhaustiveness gate | **concern** | Data Model + Testing reviews recommend a targeted test (NOT a full AST scan) at `tests/test_audit_completeness.py` that constructs a `unique_combination` candidate and asserts it passes through all 6 dispatch sites without raising. Cheap; load-bearing for the next variant after #170. Phase 4 story. |
| Testing — drift detector + candidate-schema fixture | pass | Mirror `StrictCandidateTestRowCountBetween` + add one fixture row. |
| Testing — drafter prompt-cache stability rotation | pass | Mechanical rotation; inline golden moves in lockstep. |
| Testing — anchor-contract collect-all coverage | pass | Add ~7 new parser tests mirroring #169's matrix. |
| Testing — compiler snapshot fixtures (BigQuery + Snowflake) | pass | ~6 new `.sql` snapshots (3 BQ + 3 Snowflake); covered by parametrised harness; `sqlglot` parse-guard auto-runs. |
| Testing — sample-mode behavioural pin | **blocker** | Pin Option (iii) routing with a `test_prune_tests_unique_combination_under_materialised_references_source_not_temp_table`-shaped behavioural test. Without it, a snapshot certifies shape, not routing — exactly the #169 US-007a QG lesson. |
| Testing — ingest parser variant matrix | pass | ~12–15 new tests mirroring #169 shape. |
| Testing — ingest anchor-exemption test | pass | Mirror `test_model_level_row_count_between_with_none_column_does_not_raise`. |
| Testing — e2e fixture engineering | **concern** | Phase 1 S4 decided to engineer `stg_bikeshare_station_pairs.sql` (or similar). Manifest-regen contract: workers can't reach dbt cloud / live DBs, so use the **hand-crafted manifest seed** pattern per `testing-signal.md` § "Hand-crafted manifest seed when workers can't run live tooling". Add a loads-only test to verify the seed parses. |
| Testing — e2e gating + provider matrix | pass | `@pytest.mark.e2e` + `SF_RUN_BQ=1` + `GOOGLE_CLOUD_PROJECT` + `ANTHROPIC_API_KEY` standard 4-env gate. |
| Observability — new logging seams | pass | Zero. All 6 dispatch sites extend existing functions; stage-0 ingest stays log-free. |
| Observability — audit-corpus recipe stability | pass | Discriminated-union extension flows through `model_dump_json` walks. `parsed_schema_hash` / `compiled_sql` / `rubric_hash` recipes unchanged. |
| Ops docs — `docs/draft-ops.md` | pass | Insert new section after #169's `row_count_between` block (~line 517), mirroring its shape. |
| Ops docs — `docs/prune-ops.md` | pass | Existing single-column `unique` documentation transitively covers composite uniqueness (GROUP BY+HAVING shape identical). No new section beyond a one-line callout in the variant index. |
| Ops docs — `docs/grade-ops.md` | pass | One-paragraph extension to `no-redundant` criterion narrative (matches the rubric prose change). |
| Ops docs — `docs/ingest-ops.md` | pass | New subsection: "Recognition of `dbt_utils.unique_combination_of_columns`" (mirrors `expect_table_row_count_to_be_between` section from #169). |
| Ops docs — new `docs/drafter-catalogue.md` | pass | SSOT per Phase 1 S2 decision. Outline drafted in Observability review. Add to `mkdocs.yml` nav between draft-ops and prune-ops. |
| Ops docs — README "What tests SignalForge generates" | pass | Compact 7-row table; positioned after "What it does", before "How it works". Auto-propagates to site home via include-markdown stub. |
| Ops docs — CHANGELOG `[Unreleased]` | pass | Added + Documentation + Changed bullets. Template drafted in Observability review. |
| Ops docs — SKILL.md parity | pass | No new subcommand / flag / demo command. Gate auto-passes. |

### Auto-decided architecture findings (cross-review consensus, no Phase 3 question needed)

- **Sample-mode routing → Option (iii):** engine override to source via `per_test_table_ref` — extend the existing `isinstance(test, CandidateTestRowCountBetween)` conditional at the prune engine's per-test loop to include `unique_combination`. Pin with behavioural test. Documented in `business-rule-tests.md` as the metadata-bypass pattern (the same source-vs-temp routing #169 ships).
- **Grade rubric refinement → Option A (extend `no-redundant`):** add 2-3 sentences naming `unique_combination` and the `(pk, anything)` vacuous shape inside the existing criterion text. Stays at 4 criteria.
- **Ingest strictness → `SkippedTest(reason="malformed-supported-test")`** for: missing `combination_of_columns`, empty list, len < 2, duplicates, non-string items, column-scoped usage. Same shape as `_parse_row_count_between`.
- **Mechanic exhaustiveness check → targeted test, NOT a full AST scan:** add one test at `tests/test_audit_completeness.py` (or co-located) that constructs a `unique_combination` candidate and asserts it routes correctly through all 6 dispatch sites. Cheap; load-bearing for the next variant.
- **E2E fixture seed → hand-crafted manifest seed** per `testing-signal.md`. Workers can't run live `dbt parse`; commit the parsed manifest alongside the new model SQL, with a loads-only test verifying the seed.

### Open Phase 3 decisions (architecture concerns surfaced to user)

The plan has **3 genuinely contested or load-bearing decisions** that need explicit user input:

1. **`columns` tuple canonical form for `args_hash`** — SORT (cache efficiency, mirrors `accepted_values.values` precedent) vs PRESERVE (LLM structural intent).
2. **Grade-side cache-stability surface** — establish `signalforge.grade._PROMPT_VERSION` + `tests/grade/test_prompt_cache_stability.py` snapshot as a #170 story, OR document the asymmetry + amend `business-rule-tests.md`.
3. **Custom `__repr__` for candidate variants carrying LLM-emitted text fields** — establish redaction for `where` / `sql` / `rationale` on candidate variants (covers existing `CandidateTestRowCountBetween` and `CandidateTestCustomSQL` retroactively), OR defer to a follow-up issue (the rule-file convention currently applies only to result shapes).

## Phase 3 — Refinement (DEC log)

Decisions consolidated from Phase 1 scoping (S1–S4), Phase 2 architecture review (cross-review consensus items), and Phase 3 user input (3 contested decisions).

| DEC | Decision | Rationale |
|---|---|---|
| **DEC-001** | **Variant name = `unique_combination`** (vs. `composite_unique`). | Grep parity with the dbt-utils macro it most directly replaces. Phase 1 P3 implicit confirmation. |
| **DEC-002** | **`where: str | None` shipped in v1** (Phase 1 S1). | Holds the epic #179 +10 pp / 15-of-143 coverage projection. Inherits #169's sqlglot type-coherence reuse pattern verbatim. |
| **DEC-003** | **Catalogue SSOT lives in new `docs/drafter-catalogue.md`** (Phase 1 S2). | Matches the `.claude/rules/` (architecture) vs. `docs/*-ops.md` (operator examples) split. Linked from README + `business-rule-tests.md` + `draft-ops.md`. |
| **DEC-004** | **Docs story bundled into #170** (Phase 1 S3). | A 7th primitive without its evaluation surface ships product but not the story. One PR closes the loop. |
| **DEC-005** | **Engineer new fixture `stg_bikeshare_station_pairs.sql`** with natural composite-key uniqueness (Phase 1 S4). | Pinnable e2e AC: drafter emits structured `unique_combination` against the new fixture instead of freeform `custom_sql`. |
| **DEC-006** | **Sample-mode routing: Option (iii) — engine override to source** via `per_test_table_ref`, mirror #169 US-007a. | Cross-review consensus (Performance + Testing). Composite uniqueness on a sample is semantically approximate (false-negative risk); always-full-scan is honest. Bounded by `maximum_bytes_billed`. Extends existing `isinstance(test, CandidateTestRowCountBetween)` conditional in `prune/engine.py`. Pin with a behavioural test. |
| **DEC-007** | **Grade rubric refinement → extend `no-redundant`** (Option A; cross-review consensus). | Add 2-3 sentences naming `unique_combination` and the `(pk, anything)` vacuous shape inside the existing criterion text. Stays at 4 criteria. |
| **DEC-008** | **Ingest strictness → `SkippedTest(reason="malformed-supported-test")`** on missing/empty/len<2/duplicate/non-string `combination_of_columns`; column-scoped usage. | Mirrors `_parse_row_count_between` strictness verbatim. |
| **DEC-009** | **Mechanic exhaustiveness gate → targeted dispatch-site routing test**, NOT a full AST scan. | Constructs a `unique_combination` candidate and asserts it routes through all 6 dispatch sites without raising. Cheap; load-bearing for the next variant. |
| **DEC-010** | **E2E fixture seed → hand-crafted manifest seed** per `testing-signal.md` § "Hand-crafted manifest seed when workers can't run live tooling". | Ralph workers in worktrees can't reach dbt cloud / live DBs. Commit the parsed manifest alongside the new model SQL; add a loads-only test verifying the seed parses. |
| **DEC-011** | **`columns` tuple canonical form for `args_hash` → SORT before hashing** (Phase 3 answer). | Mirrors `accepted_values.values` precedent at `_common/artifact_id.py:79-84` which sorts before canonicalising. `unique_combination(columns=[a,b])` ≡ `unique_combination(columns=[b,a])` (GROUP BY result-row identity); single artifact_id → single warehouse call → stable cache reuse. |
| **DEC-012** | **Establish grade-side `_PROMPT_VERSION` snapshot surface in #170** (Phase 3 answer). | Add `signalforge.grade.prompts._PROMPT_VERSION` + `tests/grade/test_prompt_cache_stability.py` pinning the rubric hash. Mirrors drafter side exactly. #170 rotates the new constant in the same commit that extends `no-redundant`. Closes the overstated rule-file claim with reality. |
| **DEC-013** | **Custom `__repr__` redaction established for #170 + retroactively on existing variants** (Phase 3 answer). | Add a shared `__repr__` override (mixin or `@field_serializer` analog) that redacts `where` / `sql` / `rationale` on `CandidateTestUniqueCombination` AND retroactively on `CandidateTestRowCountBetween` + `CandidateTestCustomSQL`. Closes the latent log-hygiene gap surfaced by Security review. |
| **DEC-014** | **Per-column identifier check at the anchor-contract arm**, NOT Pydantic field validator. | Matches `accepted_values.values` / `relationships.to`/`.field` precedent — validation happens at parse-time anchor check; Pydantic carries raw strings. Compiler arm separately routes each `columns[i]` through `validate_identifier` + `_fold_identifier(_quote)` before quoting (defence-in-depth). |
| **DEC-015** | **`where`-fragment safety: compose full statement, route through `validate_test_sql`**, reuse `_check_custom_sql_type_coherence` for sqlglot type-coherence. | Reuses #169 DEC-005 verbatim. Composed shape: `SELECT col1, col2 FROM <table_ref> [WHERE <where>] GROUP BY col1, col2 HAVING COUNT(*) > 1`. Safety-rejected → `_InvalidIdentifier` → `kept-without-evidence`. |
| **DEC-016** | **Pydantic validators**: `@field_validator` on `columns` enforces `len >= 2`; `@model_validator(mode="after")` enforces no-duplicates (mirrors `CandidateTestRowCountBetween._bounds_consistent`). | Field-level `len` check fails fast at deserialise; model-level no-dupes ensures the violation message is "composite key has duplicate column" rather than per-element. |
| **DEC-017** | **`business-rule-tests.md` becomes a 3-instance precedent** for variant extension (`custom_sql` from #116, `row_count_between` from #169, `unique_combination` from #170). | The Patterns & Memory story updates the rule file's "n-instance precedent" framing + adds #170 as the third worked example. |

## Phase 4 — Detailed story breakdown

15 implementation stories + Quality Gate + Patterns & Memory. Ordering: variant plumbing → drafter → compiler+prune → artifact_id + diff → ingest → grade → cross-cutting + fixtures → e2e → docs → QG → P&M. Every story validates with `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.

### US-001 — `CandidateTestUniqueCombination` variant model + plumbing

**Traces to:** DEC-001, DEC-002, DEC-014, DEC-016.

**Description:** Add the 7th `CandidateTest` Pydantic variant + every supporting registration. Foundation for every downstream story.

**Acceptance criteria:**
- `CandidateTestUniqueCombination` class in `src/signalforge/draft/models.py`: `type: Literal["unique_combination"] = "unique_combination"`, `column: None = None`, `columns: tuple[str, ...]`, `where: str | None = None`, `rationale: str | None = None`.
- `@field_validator("columns")` enforces `len(v) >= 2` (DEC-016); `@model_validator(mode="after")` enforces no-duplicates (DEC-016).
- Per-column identifier shape check is NOT in Pydantic (DEC-014); raw strings carried through.
- Added to `CandidateTest` discriminated union at `draft/models.py:246–254` and to `__all__` export.
- `VALID_TEST_TYPES` frozenset in `src/signalforge/draft/config.py:58` gains `"unique_combination"`.
- Drift mirror `StrictCandidateTestUniqueCombination` in `tests/draft/test_drift_detector.py:70+`; added to `_StrictCandidateTest` union.
- New row in `tests/fixtures/draft/candidate_schema_v1.json` (after the `row_count_between` row).
- `uv run pytest tests/draft/test_drift_detector.py tests/draft/test_models.py` green.

**Done when:** drift detector + candidate-schema fixture validation pass; the union accepts `{"type": "unique_combination", ...}` and rejects unknown discriminators / malformed `columns`.

**Files:** `src/signalforge/draft/models.py`, `src/signalforge/draft/config.py`, `tests/draft/test_drift_detector.py`, `tests/draft/test_models.py` (new tests), `tests/fixtures/draft/candidate_schema_v1.json`.

**Depends on:** none.

**TDD:** Yes — write parser tests for `len<2 → ValidationError`, duplicate columns → ValidationError, valid 2-column happy path, valid 3-column + `where`, then implement the class to make them pass.

---

### US-002 — Custom `__repr__` redaction (mixin + retroactive adoption)

**Traces to:** DEC-013.

**Description:** Establish redacted `__repr__` on candidate variants carrying LLM-emitted text (`where` / `sql` / `rationale`). Apply to `CandidateTestUniqueCombination`, `CandidateTestRowCountBetween`, `CandidateTestCustomSQL`. Closes the log-hygiene gap surfaced by Security review.

**Acceptance criteria:**
- New shared helper (mixin or per-class `__repr__` overrides — implementer's call so long as the redaction is uniform). Surface shows `type`, scope (column or model-level), and the constraint shape WITHOUT the text-bearing fields.
- Pydantic `__str__` (used for serialisation) unchanged — only `__repr__` is overridden.
- Existing variants `CandidateTestRowCountBetween` (`where`) + `CandidateTestCustomSQL` (`sql`) adopt the redaction. New variant `CandidateTestUniqueCombination` (`where`) too.
- Tests assert `repr(instance)` does NOT contain the actual `where` / `sql` / `rationale` text for a sample instance.
- Existing debug-repr tests (if any) updated to expect the redacted form.

**Done when:** `repr(CandidateTest<X>(where="SECRET", ...))` does not contain "SECRET" for X ∈ {RowCountBetween, CustomSQL, UniqueCombination}.

**Files:** `src/signalforge/draft/models.py`, `tests/draft/test_models.py`.

**Depends on:** US-001.

**TDD:** Yes — write the three redaction-assertion tests first; implement to make them pass.

---

### US-003 — Drafter prompt catalogue entry + `_PROMPT_VERSION` rotation

**Traces to:** DEC-002.

**Description:** Add `unique_combination` to `_TEST_CATALOGUE_LINES` with two illustrated forms (no-`where`, with-`where`) and cautionary prose steering away from vacuously-unique tuples like `(pk, anything)`. Rotate `_PROMPT_VERSION` + cache-stability snapshot in lockstep.

**Acceptance criteria:**
- Catalogue entry under `_TEST_CATALOGUE_LINES["unique_combination"]` in `src/signalforge/draft/prompts.py:59–82`. Includes both JSON shapes; one sentence of when-to-propose guidance; one sentence cautioning against vacuously-unique tuples (pk-bearing).
- Surrounding docstring (`prompts.py:83–102`) updated to reference "seven variants" + "issue #170".
- `_PROMPT_VERSION` constant at `prompts.py:311–321` recomputed and updated in source.
- `tests/llm/test_prompt_cache_stability.py:77` `_EXPECTED_PROMPT_VERSION` updated to the new hash.
- Rotation history comment at `tests/llm/test_prompt_cache_stability.py:13–48` extends with a #170 entry.
- The inline cached-block golden (if it changed under the new manifest summary) moves in lockstep.
- `uv run pytest tests/llm/test_prompt_cache_stability.py` green.

**Done when:** the cache-stability snapshot test passes against the new constant value with the new catalogue entry rendered.

**Files:** `src/signalforge/draft/prompts.py`, `tests/llm/test_prompt_cache_stability.py`.

**Depends on:** US-001.

**TDD:** Compute the new hash via the existing helper, pin the new constant, then update source. (Inverse-TDD because the hash is the target.)

---

### US-004 — Draft parser anchor-contract arm + collect-all matrix

**Traces to:** DEC-014, DEC-015, DEC-016.

**Description:** Extend `_validate_anchor_contract` at `src/signalforge/draft/parser.py:362` with a `unique_combination` arm: per-column existence check, duplicate-column rejection (Pydantic should already block but defence-in-depth at parse), `where` type-coherence via `_check_custom_sql_type_coherence` reuse. Collect-all, never short-circuit.

**Acceptance criteria:**
- Arm at `draft/parser.py:362+` mirroring the `row_count_between` shape (`:499–524`).
- Each `columns[i]` checked against `model_columns`; missing columns → one violation per missing.
- `where` (when non-None) routed through `_check_custom_sql_type_coherence(test.where, model_columns, types_map, dialect_name)` — same helper #169 ships.
- Collect-all preserved: multiple violations surface in one `LLMOutputAnchorContractError`.
- ~7 new parser tests (see Testing review §3): valid pair (no `where`), valid 3-column + `where`, hallucinated column in tuple, hallucinated column in `where`, type-incoherent `where`, multi-violation collect, exclude-tests gate.
- `uv run pytest tests/draft/test_parser.py -k unique_combination` green.

**Done when:** all anchor-contract paths surface every violation, never short-circuit; the dispatch arm routes `unique_combination` through the same collect-all spine as other variants.

**Files:** `src/signalforge/draft/parser.py`, `tests/draft/test_parser.py`.

**Depends on:** US-001.

**TDD:** Yes — 7 failing parser tests first; implement the arm to make them pass.

---

### US-005a — Prune compiler arm `_compile_unique_combination` + BQ/Snowflake snapshots

**Traces to:** DEC-014, DEC-015.

**Description:** Implement the compiler arm in `src/signalforge/prune/compiler.py`. Dialect-driven (no `dialect.name` branching). 6 new snapshot fixtures + `sqlglot` parse-guard on Snowflake.

**Acceptance criteria:**
- `_compile_unique_combination(test, table_ref, dialect)` colocated with `_compile_row_count_between` at `prune/compiler.py:809+`. Emits `SELECT <cols> FROM <table_ref> [WHERE <where>] GROUP BY <cols> HAVING COUNT(*) > 1`. Each `columns[i]` routes through `validate_identifier` + `_fold_identifier`/`_quote`; identifier rejection returns `_InvalidIdentifier`. `where` (when non-None) composed into the full statement and routed through `validate_test_sql`.
- Dispatcher arm added at `_compile_test:905+`.
- 6 new snapshot fixtures: `tests/fixtures/prune/compiled_sql/bigquery/unique_combination_{pair,with_where,three_columns}.sql` + Snowflake mirrors. `sqlglot` parse-guard auto-runs on Snowflake snapshots.
- Snapshot-equality tests pass on every fixture (BQ + Snowflake).
- `uv run pytest tests/prune/test_compiler.py -k unique_combination` green.

**Done when:** snapshots are byte-equal across BQ + Snowflake; `sqlglot` parses the Snowflake SQL clean.

**Files:** `src/signalforge/prune/compiler.py`, `tests/prune/test_compiler.py`, 6 new fixture `.sql` files.

**Depends on:** US-001, US-004.

**TDD:** Yes — write the snapshot tests with empty fixtures first; implement to make them pass.

---

### US-005b — Engine sample-mode source override + behavioural routing pin

**Traces to:** DEC-006.

**Description:** Extend the existing `row_count_between` source-vs-temp conditional in `prune/engine.py` to include `unique_combination`. Composite uniqueness on a sample is semantically approximate (false-negative risk); always-route-to-source mirrors #169 US-007a. Pin with a behavioural test — snapshot equality (US-005a) certifies shape but NOT engine routing.

**Acceptance criteria:**
- Engine extends the existing `isinstance(test, CandidateTestRowCountBetween)` conditional in `prune/engine.py` to include `CandidateTestUniqueCombination`. `per_test_table_ref` resolves to the **source** table under both `sample_strategy="materialised"` and `sample_strategy="oneshot"` when `scope="sample"`. Under `scope="full"` no change.
- **Behavioural routing test pin** (load-bearing): `test_prune_tests_unique_combination_under_materialised_references_source_not_temp_table` mirrors #169's `row_count_between` precedent. Asserts the compiled SQL references the source table, NEVER `_SESSION._sf_sample_*`. Parametrise across `materialised` AND `oneshot` strategies.
- Companion test asserts `scope="full"` is unchanged (source table is also used trivially — no regression).
- `uv run pytest tests/prune/test_engine.py -k unique_combination` green.

**Done when:** the engine routes `unique_combination` to source under sample mode, pinned by behavioural test (not just snapshot equality).

**Files:** `src/signalforge/prune/engine.py`, `tests/prune/test_engine.py`.

**Depends on:** US-005a.

**TDD:** Yes — write the failing behavioural routing test first; implement the engine arm to make it pass.

---

### US-006 — `_common.artifact_id` arm (SORTED `columns`) + diff emitter arm

**Traces to:** DEC-011 (sort), DEC-001 (variant name).

**Description:** Two small, related arms in the cross-stage shared seam (`_common/artifact_id.py`) and the diff YAML emitter (`diff/_emitter.py`).

**Acceptance criteria:**
- `_common/artifact_id.py:104+` gains arm: `elif isinstance(test, CandidateTestUniqueCombination): payload = {"type": test.type, "column": test.column, "columns": sorted(test.columns), "where": test.where}`. **`sorted(test.columns)` is load-bearing per DEC-011.**
- Cross-stage parity holds — re-exports from `signalforge.diff._artifact_id` and `signalforge.grade.engine` pick up the new arm via `is`-identity (no per-module change needed there).
- `diff/_emitter.py:132+` gains arm rendering `{"dbt_utils.unique_combination_of_columns": {"combination_of_columns": [...]}}` (omitting `where` when None; including it when non-None). **Field-name mapping seam**: Pydantic `columns` → YAML `combination_of_columns`.
- Tests: artifact_id hash equality for `(a,b)` vs `(b,a)` (load-bearing DEC-011 pin); collision test for two `unique_combination` tests on same model with different `columns` (uses `_model_test_args_hash` suffix); diff emitter byte-equal YAML for known input.
- `uv run pytest tests/_common/test_artifact_id.py tests/diff/test_emitter.py -k unique_combination` green.

**Done when:** sorted-canonical hash invariant pinned; YAML emission matches dbt_utils macro shape.

**Files:** `src/signalforge/_common/artifact_id.py`, `src/signalforge/diff/_emitter.py`, `tests/_common/test_artifact_id.py`, `tests/diff/test_emitter.py`.

**Depends on:** US-001.

**TDD:** Yes — hash-identity test + YAML byte-equality test first.

---

### US-007 — Ingest parser arm + ingest anchor-exemption

**Traces to:** DEC-008.

**Description:** Add `_parse_unique_combination` helper + recognition arm in `signalforge.ingest.parser._parse_named_test`; add model-level loop exemption in `signalforge.ingest.anchor.validate_anchor_contract`. Variant matrix tests for both surfaces.

**Acceptance criteria:**
- `_parse_unique_combination(*, body, column)` helper in `ingest/parser.py` (colocated with `_parse_row_count_between`). Reads `combination_of_columns: list[str]` from dict body, optional `where: str | None`. Maps to `CandidateTestUniqueCombination(columns=tuple(...), where=...)`. Per DEC-008, routes to `SkippedTest(reason="malformed-supported-test")` on: missing key, empty list, len < 2, duplicate items, non-string items, column-scoped usage, non-empty non-string `where`.
- Recognition arm added at `_parse_named_test:196+`: `if name == "dbt_utils.unique_combination_of_columns": return _parse_unique_combination(body=body, column=column)`.
- Ingest anchor early-out arm at `ingest/anchor.py:36+`: `if test.type == "unique_combination": continue` in the model-level test loop (mirrors `row_count_between` at `:82–89`).
- ~12 new ingest parser tests (see Testing review §6); 1 new ingest anchor test (mirrors `test_model_level_row_count_between_with_none_column_does_not_raise`).
- Config-keys stripped per `ingest-layer.md` § "dbt syntax tolerance" — verify via parametrised test (severity, tags, name, error_if, warn_if, description ignored).
- `uv run pytest tests/ingest/ -k unique_combination` green.

**Done when:** valid `dbt_utils.unique_combination_of_columns` schema.yml entries parse to typed variants; malformed inputs route to typed skip; ingest anchor doesn't fire spurious "column None" violations.

**Files:** `src/signalforge/ingest/parser.py`, `src/signalforge/ingest/anchor.py`, `tests/ingest/test_parser.py`, `tests/ingest/test_anchor.py`.

**Depends on:** US-001.

**TDD:** Yes — write the 12-shape parser matrix + 1 anchor test first.

---

### US-008 — Grade rubric `no-redundant` extension for grain-meaningfulness

**Traces to:** DEC-007.

**Description:** Extend the `no-redundant` criterion text in `src/signalforge/grade/rubric.py:189–198` with 2–3 sentences naming `unique_combination` and the vacuously-unique-tuple shape `(pk, anything)`. Keep at 4 criteria (no 5th).

**Acceptance criteria:**
- Criterion text at `rubric.py:189–198` extended. Sample addition (final wording to be authored): "For `unique_combination` tests, is the composite column tuple a meaningful grain (e.g. `(order_id, line_item_id)`) — not vacuously unique because one column is already a primary key? A tuple like `(primary_key, anything)` is always unique by construction and adds no signal."
- `DEFAULT_RUBRIC` constant rebuilds at module import; the dynamic `rubric_hash` flows correctly through `GradeEvent` rows.
- Existing grade tests that pin the rubric hash (if any) — verify they're rebuilt or update them in lockstep.
- No 5th criterion added (DEC-007).
- `uv run pytest tests/grade/` green.

**Done when:** the criterion text mentions `unique_combination` + the vacuous shape; the rubric rebuilds without test churn beyond hash-pin updates.

**Files:** `src/signalforge/grade/rubric.py`, possibly `tests/grade/test_rubric.py`.

**Depends on:** US-001 (the variant exists so the prose has a real referent).

**TDD:** Author the prose, run the tests, update any rubric-hash-pinning tests.

---

### US-009 — Establish grade-side `_PROMPT_VERSION` snapshot surface

**Traces to:** DEC-012.

**Description:** Build the grade-side cache-stability surface that the rule file claims exists but actually doesn't. Add `signalforge.grade.prompts._PROMPT_VERSION` (blake2b-8 over the grade system prompt + canonical rubric criterion list) + `tests/grade/test_prompt_cache_stability.py` pinning it. Rotates in this PR (the rubric just changed in US-008).

**Acceptance criteria:**
- New module-level constant `_PROMPT_VERSION: str = "..."` (16-hex blake2b-8) in `src/signalforge/grade/prompts.py` (or wherever the system prompt is constructed). Recipe documented in module docstring: `blake2b(_SYSTEM_PROMPT + canonical_rubric_json, digest_size=8).hexdigest()` (mirror the drafter recipe shape).
- New `tests/grade/test_prompt_cache_stability.py` pinning the constant + the canonical rubric JSON bytes. Documents rotation policy: bump when any of the 4 criterion texts change OR the system prompt changes. Includes a #170 rotation history entry.
- The new constant IS rotated in this commit (US-008 extended `no-redundant`).
- `business-rule-tests.md` § "Lockstep `_PROMPT_VERSION` rotation when extending the catalogue (#169 DEC-012)" stays accurate as written.
- `uv run pytest tests/grade/test_prompt_cache_stability.py` green.

**Done when:** the snapshot constant pins the current rubric+prompt bytes; the rotation policy is documented in source; the rule file's claim is now reality.

**Files:** `src/signalforge/grade/prompts.py`, `tests/grade/test_prompt_cache_stability.py` (new).

**Depends on:** US-008.

**TDD:** Inverse — compute the hash, pin it, then write the assertion.

---

### US-010 — Mechanic exhaustiveness gate (6-site dispatch routing test)

**Traces to:** DEC-009.

**Description:** Add one test that constructs a `unique_combination` candidate and routes it through all 6 dispatch sites without raising. Catches the latent "missing arm = runtime crash" risk for the next variant after #170.

**Acceptance criteria:**
- New test `tests/test_audit_completeness.py::test_candidate_test_variants_route_through_all_six_dispatch_sites` (or co-located). Constructs a minimal `CandidateTestUniqueCombination(columns=("a", "b"))` candidate + a minimal model + a minimal manifest.
- Asserts each of the 6 sites does NOT raise on the variant:
  1. `prune.compiler._compile_test` returns a compiled SQL string.
  2. `_common.artifact_id.model_test_args_hash` returns a 16-hex hash.
  3. `diff._emitter._render_test` returns a dict body.
  4. `ingest.parser._parse_named_test` (recognises a synthetic dbt_utils dict).
  5. `draft.parser._validate_anchor_contract` (no violations on a valid candidate).
  6. `ingest.anchor.validate_anchor_contract` (no violations on a model-level candidate).
- Test parametrises over EVERY variant in `CandidateTest` union so the next variant addition forces an arm at each site or fails this test.
- `uv run pytest tests/test_audit_completeness.py` green.

**Done when:** every variant in the union is mechanically exercised against every dispatch site.

**Files:** `tests/test_audit_completeness.py` (new test).

**Depends on:** US-001, US-004, US-005a, US-005b, US-006, US-007.

**TDD:** Yes — write the parametrised test first; verify it catches a synthetic missing arm.

---

### US-011 — Engineered fixture model `stg_bikeshare_station_pairs.sql` + hand-crafted manifest seed

**Traces to:** DEC-005, DEC-010.

**Description:** Add a new fixture model with natural composite-key uniqueness so the drafter's prompt example reliably steers `claude-sonnet-4-6` toward `unique_combination`. Hand-craft the manifest seed (Ralph workers can't run live `dbt parse`).

**Acceptance criteria:**
- New model `tests/fixtures/dbt_project_austin/models/staging/stg_bikeshare_station_pairs.sql`. SELECT body includes a multi-column `GROUP BY` natural pattern (e.g. `(start_station_id, end_station_id, trip_date)` aggregating trip counts). Real columns from the bikeshare source table; no engineered literal columns (source-as-model alias trick per `testing-signal.md`).
- Hand-crafted addition to `tests/fixtures/dbt_project_austin/target/manifest.json` for the new model (per `testing-signal.md` § "Hand-crafted manifest seed when workers can't run live tooling"). Strip non-deterministic fields.
- Update `tests/fixtures/regenerate.sh` documenting the maintainer-only full regen command if/when run with live dbt.
- New loads-only test verifying `signalforge.manifest.load(fixture_dir)` succeeds with the new model + the seed survives Pydantic parsing.
- `uv run pytest tests/fixtures/ tests/manifest/ -k station_pairs` green.

**Done when:** the new fixture model parses cleanly through the manifest loader without env vars; the SELECT body has the multi-column GROUP BY pattern the drafter needs.

**Files:** `tests/fixtures/dbt_project_austin/models/staging/stg_bikeshare_station_pairs.sql` (new), `tests/fixtures/dbt_project_austin/target/manifest.json` (hand-edited), `tests/fixtures/regenerate.sh`, `tests/fixtures/test_loads.py` (or wherever loads-only tests live).

**Depends on:** none (independent — can land before or after the code stories).

**TDD:** Yes — write the loads-only test first.

---

### US-012 — Gated e2e test: drafter emits structured `unique_combination` against engineered fixture

**Traces to:** DEC-005, plus the issue's AC-1 / AC-8.

**Description:** New `@pytest.mark.e2e`-gated test that runs `signalforge generate` against the engineered fixture model and asserts the drafter emits a structured `unique_combination` candidate (NOT freeform `custom_sql`). Verifies the load-bearing behavioural claim of #170.

**Acceptance criteria:**
- New test `tests/cli/test_e2e_unique_combination.py` (or extend the bikeshare e2e). `@pytest.mark.e2e` + standard 4-env gate (`SF_RUN_BQ=1`, `GOOGLE_CLOUD_PROJECT`, `ANTHROPIC_API_KEY`, plus any provider overlay if multi-provider runs).
- Uses `copy_fixture_to_tmp` per `testing-signal.md` § "Parallel-safe e2e".
- Runs `signalforge generate models/staging/stg_bikeshare_station_pairs.sql --project-dir <tmp>` (or unique_id form).
- Asserts the parsed candidate schema includes ≥1 `CandidateTestUniqueCombination` entry on the model (or a column-targeted unique_combination via the typed shape).
- Asserts the prune+grade pipeline runs to completion (exit 0); diff sidecar + grade sidecar present.
- Optional: pin a calibration scenario — engineer the fixture's natural uniqueness pattern so the drafted test is mathematically guaranteed kept (`always-passes` would be a regression).
- `uv run pytest -m e2e -k unique_combination --no-cov` green when env vars set.

**Done when:** the maintainer can reproduce the freeform→structured translation against the engineered fixture in one e2e invocation.

**Files:** `tests/cli/test_e2e_unique_combination.py` (or extension to existing bikeshare e2e).

**Depends on:** US-001 … US-011 (the entire code path).

**TDD:** No — engineered live behaviour against a specific LLM; pin after observation, not before.

---

### US-013 — Author SSOT `docs/drafter-catalogue.md` + README section + ops doc paraphrases + CHANGELOG + mkdocs nav

**Traces to:** DEC-003, DEC-004.

**Description:** The piggyback documentation story. SSOT catalogue + README "What tests SignalForge generates" + ops doc updates + CHANGELOG entry + mkdocs nav.

**Acceptance criteria:**
- New file `docs/drafter-catalogue.md` (outline drafted in Phase 2 Observability review): header paragraph; 7-row catalogue table (name + dbt equivalent + structural slots + ingest signature + sample-mode behaviour + scope); `custom_sql` catch-all sub-section; "What we do NOT generate today" sub-section; References section.
- README "What tests SignalForge generates" section after "What it does", before "How it works". Compact 7-row table; links to the SSOT.
- `docs/draft-ops.md` — new `unique_combination` section after the `row_count_between` block (~line 517).
- `docs/ingest-ops.md` — new "Recognition of `dbt_utils.unique_combination_of_columns`" subsection mirroring the `expect_table_row_count_to_be_between` precedent.
- `docs/grade-ops.md` — paragraph extension naming the `no-redundant` criterion change.
- `docs/prune-ops.md` — verify no new section needed (composite GROUP BY mechanics covered by existing `unique` documentation); add a one-line callout to the variant index if one exists.
- `mkdocs.yml` — add `- Test Catalogue: drafter-catalogue.md` between draft-ops and prune-ops (or wherever the existing nav flows).
- `CHANGELOG.md` `[Unreleased]` — Added + Documentation + Changed entries per template in Phase 2 Observability review.
- Local `uv run mkdocs build` succeeds (no broken-link errors).
- Maintainer notes the post-merge rendered-site spot-check at https://wjduenow.github.io/SignalForge/ for the README change + new catalogue page (manual; not a gate).

**Done when:** all 7 surface updates land in one commit; mkdocs builds clean; SSOT is the single source for catalogue prose.

**Files:** `docs/drafter-catalogue.md` (new), `README.md`, `docs/draft-ops.md`, `docs/ingest-ops.md`, `docs/grade-ops.md`, `docs/prune-ops.md`, `mkdocs.yml`, `CHANGELOG.md`.

**Depends on:** US-001 … US-009 (so the variant's surface details are real before being documented).

**TDD:** No — prose authoring.

---

### US-014 — Quality Gate (4 reviewer angles + CodeRabbit + validation)

**Description:** Run the code-review skill 4 times across the full changeset from distinct angles, fix all real bugs each pass, then run CodeRabbit. Validation green after every fix pass.

**Acceptance criteria:**
- Run `/code-review` at high effort, with 4 distinct reviewer angles per memory `qg-diverse-reviewer-angles-catch-cross-surface-drift`: (a) correctness — does the variant flow through all 6 dispatch sites; behavioural routing pin holds; (b) conventions — DECs match what's implemented; rule-file parity (`business-rule-tests.md`); (c) tests — collect-all coverage; sample-mode behavioural pin; mechanic exhaustiveness gate; (d) docs+UX — SSOT-doc consistency with code, mkdocs builds, CHANGELOG bullets accurate.
- Same finding from 2+ angles → high-priority by triangulation; fix before merge.
- Run CodeRabbit on the PR; address actionable findings.
- After every fix pass: `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` green.
- Run gated suites at least once: `uv run pytest -m cli_subprocess --no-cov`, `uv run pytest -m wheel_smoke --no-cov`, `uv run pytest -m snowflake --no-cov`.

**Done when:** 4 reviewer angles + CodeRabbit clean; full validation green; gated markers green.

**Depends on:** US-001 … US-013.

---

### US-015 — Patterns & Memory (update conventions, rule files, memories)

**Description:** Update `.claude/rules/business-rule-tests.md` to reflect the 3-instance precedent. Update memories where new patterns emerged. Update `cli-layer.md` / `grade-layer.md` if the grade-side `_PROMPT_VERSION` surface changes the rule wording.

**Acceptance criteria:**
- `.claude/rules/business-rule-tests.md` updated: the "2-instance precedent" framing becomes 3-instance; #170 added as the third worked example; the metadata-bypass routing arm at the engine now references both `row_count_between` AND `unique_combination`; the field-naming mapping seam section adds the `columns` ↔ `combination_of_columns` pair to the precedent table.
- `.claude/rules/llm-drafter.md` § "Cached-block scope" — verify still accurate (no change needed beyond updating "six test variants" → "seven test variants" if such count appears).
- `.claude/rules/grade-layer.md` — if US-009 established the grade-side `_PROMPT_VERSION`, document the rotation policy (parallel to the drafter side).
- New memory if a pattern emerged that should outlive this PR (e.g. "dbt_utils macro recognition follows the dbt_expectations pattern but with field-name remapping" if not already captured).
- Update the `MEMORY.md` index for any new memory file.
- Validation green.

**Done when:** the rule-file framing matches the 3-instance reality; #170's durable conventions are captured.

**Files:** `.claude/rules/business-rule-tests.md`, possibly `.claude/rules/llm-drafter.md`, `.claude/rules/grade-layer.md`, possibly new entries under `/home/wesd/.claude/projects/-home-wesd-Projects-SignalForge/memory/`.

**Depends on:** US-014.

---

### Story dependency graph

```
US-001 ──┬── US-002
         ├── US-003
         ├── US-004 ── US-005a ── US-005b
         ├── US-006
         ├── US-007
         └── US-008 ── US-009

US-001 + US-004 + US-005a + US-005b + US-006 + US-007 ── US-010 (mechanic gate)

US-011 ─── (independent fixture seed)

US-001..US-011 ── US-012 (e2e)

US-001..US-009 ── US-013 (docs SSOT)

US-001..US-013 ── US-014 (QG) ── US-015 (P&M)
```

**Story count:** 16 implementation + QG + P&M = 18 beads total.

### Rules compliance check (pre-Phase-5 gate)

Cross-referenced every story against the loaded `.claude/rules/*.md` constraints. The 6 dispatch sites match `business-rule-tests.md` § "The 6 production dispatch sites" (validates US-004 through US-007). DropReason taxonomy stays closed (validates US-005 sample-mode routing — uses `kept-without-evidence` only via existing `_InvalidIdentifier` path). Audit-event recipes unchanged (validates US-001 candidate-schema row + US-005 `compiled_sql` flow). Stage-0 ingest stays log-free (validates US-007). Fail-closed writer count unchanged (no new singular-test file emission). Skill parity gate auto-passes (no new subcommand). 5-surface parity holds for the variant (docstring + ops doc + tests + DEC + rule file all updated in US-013 + US-015). No rule violations.

## Phase 5 — Publish PR

Plan committed on `feature/170-unique-combination`, pushed to origin, draft PR opened against `dev` (SignalForge convention per the user memory `feedback_pr_target_dev`).

## Phase 6 — Approved

*(pending)*

## Phase 7 — Devolve (Beads Manifest)

*(pending)*
