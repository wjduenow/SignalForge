# 169 — Add `row_count_between` as a 6th first-class test primitive

## Meta

- **Ticket:** [#169](https://github.com/wjduenow/SignalForge/issues/169) — Add row_count_between as a 6th first-class test primitive (drafter + prune + grade)
- **Branch / worktree:** `feature/169-row-count-between` at `../worktrees/SignalForge/169-row-count-between`
- **Phase:** devolved
- **Sessions:** 1 (2026-05-30)
- **Sibling tickets:**
  - **#154** — grade-existing-dbt-expectations-tests (complementary; meeting point at ingest)
  - **#146** — Airflow integration (context-only — the `intuit_airflow` survey is the analysis substrate)
- **Closest precedent (single biggest reference):** #116 `custom_sql` — the 5th-variant addition, exhaustively documented in `plans/super/116-business-rule-tests.md` (15 DECs) and `.claude/rules/business-rule-tests.md`. #163 (drafter business-rules fidelity — parser cardinality gate, numbered envelope, parameterised breach error) and #159 (drafter column types — parser sqlglot type-coherence) are the two follow-ups that hardened the 5th variant; their patterns are directly reusable.

## Discovery

### What

Add `row_count_between` (min/max row-count bounds, optional `where` filter) as a **6th first-class `CandidateTest` variant** that the drafter can propose, the prune engine can compile and evaluate, the grader can score, and the diff layer can render alongside the existing 5 types (`not_null`, `unique`, `accepted_values`, `relationships`, `custom_sql`).

**Distinction from #154:** #154 is a prune+grade-only adapter that **consumes existing** `dbt-expectations` tests via the manifest's `compiled_code` (drafting deliberately out of scope). #169 makes SignalForge **draft** the row-count check itself as a structured artifact so a clean dbt project gets the same coverage without operator hand-authoring. The two meet at the ingest layer — #169's AC-5 promotes ingested `expect_table_row_count_to_be_between` to the new variant instead of today's `SkipReason="custom-or-generic-test"`.

### Why

Survey of `intuit_airflow/plugins/dbt/` (Snowflake, 104 models, ~243 declared tests):

- **40 % of all declared tests (98 / 243) are `dbt_expectations.expect_table_row_count_to_be_between`.** The single most-used test type in the project, ahead of every column-scoped check combined.
- Live `signalforge generate` against `reporting/weekly_query_cost`: produced 8 `not_null` + 11 `custom_sql` tests covering domain semantics — but **proposed no row-count check**, despite the team having declared `row_count_between(min=100)` on this model in their Python annotation layer.

The drafter has no slot in its catalogue to propose this shape, so it never appears. `custom_sql` *could* express it (`SELECT * FROM (SELECT COUNT(*) v FROM {{ this }}) WHERE v < min OR v > max`) but as freeform LLM emission, not as a structured artifact the operator reviews + the grader scores against a row-count rubric. **We're leaking the most common dbt-expectations shape entirely.** This is a direct hit on Architectural Commitment #1 ("signal over volume") in a category the drafter cannot reach today.

### Who

SignalForge users running `signalforge generate` against a dbt-core project where they would otherwise hand-author `dbt_expectations.expect_table_row_count_to_be_between` (or a 12-line `tests/*.sql` singular test) — most concretely, the `intuit_airflow` analysis substrate, but generally any team that uses `dbt-expectations` (industry-standard).

### Acceptance criteria (verbatim from issue)

1. `signalforge generate` against a model with a bounded SELECT proposes `row_count_between` as a structured candidate, not a freeform `custom_sql`.
2. Prune evaluates the test against the warehouse (one extra `COUNT(*)`) and routes to kept / kept-uncertain / dropped per the existing decision matrix.
3. Grade rubric scores the bound's calibration; vacuous bounds (`min=0`) route through the conservative-degrade path.
4. Diff renderer emits the schema.yml block in `dbt_expectations`-compatible form, with `tests/*.sql` fallback for projects without the package.
5. `prune-existing` recognises existing `expect_table_row_count_to_be_between` declarations from external schema.yml (closes the ingest-layer skip on this specific macro).
6. Drift detectors + AST scans + cache-stability snapshot all move in lockstep; the existing exhaustive `isinstance`/`match` audit in `cli-layer.md` § "7th AST scan" extends cleanly.
7. Operational docs + a worked example reproducing the intuit_airflow `weekly_query_cost` case.

### Codebase scout — surfaces a 6th variant must touch

The Codebase Scout subagent enumerated 24 surfaces; the load-bearing ones (a missing arm at any of these is a latent runtime crash, not a type error — the union is open at runtime per `business-rule-tests.md`):

**Production code (must grow an arm)**
1. `src/signalforge/draft/models.py` lines 36–240 — concrete subclass `CandidateTestRowCountBetween` + union + `__all__`.
2. `src/signalforge/draft/config.py` lines 58–206 — `VALID_TEST_TYPES` frozenset (auto-validates `exclude_tests`).
3. `src/signalforge/draft/prompts.py` lines 59–101 — `_TEST_CATALOGUE_LINES` dict (rotates `_PROMPT_VERSION`).
4. `src/signalforge/draft/parser.py` lines 231–281 — `_validate_anchor_contract` arm + any cardinality gate (#163-style).
5. `src/signalforge/prune/compiler.py` lines 354–911 — new `_compile_row_count_between` + `_compile_test` dispatcher arm.
6. `src/signalforge/_common/artifact_id.py` lines 62–110 — `model_test_args_hash` arm (canonical-JSON over `min` / `max` / `where`).
7. `src/signalforge/diff/_emitter.py` lines 131–156 — `_render_test` arm (decides YAML-block vs. `proposed_test_files`).
8. `src/signalforge/diff/_test_file_writer.py` — IF emission falls back to `tests/*.sql` (depends on the emission-form DEC).
9. `src/signalforge/ingest/parser.py` lines 136–222 — promotion of `dbt_expectations.expect_table_row_count_to_be_between` from `SkipReason="custom-or-generic-test"` to the new variant (AC-5).

**Test fixtures + gates (must move in lockstep)**
10. `tests/fixtures/draft/candidate_schema_v1.json` — bump v1 → v2 with new variant row + paired drift detector.
11. `tests/draft/test_drift_detector.py` lines 31–107 — `StrictCandidateTestRowCountBetween` + union.
12. `tests/llm/test_prompt_cache_stability.py` line 71 — new `_EXPECTED_PROMPT_VERSION` hex (only if catalogue lands in CACHED system prompt — see DECs).
13. `tests/test_audit_completeness.py` — verify scans still pass (generic; no enumerative changes expected).

**Docs (5-surface parity — `cli-layer.md`)**
14. `docs/draft-ops.md`, `docs/prune-ops.md`, `docs/grade-ops.md`, `docs/diff-ops.md`, `docs/ingest-ops.md`, `docs/cli-ops.md`, `CHANGELOG.md`.
15. `src/signalforge/skills/signalforge/SKILL.md` — the 6th parity surface (`skill-parity.md`).

### Convention rules — load-bearing constraints discovered

Grouped by rule file, hard-gates marked **[GATE]** and soft-conventions marked *[soft]*:

- **`business-rule-tests.md`** (the `custom_sql` precedent)
  - **[GATE]** Discriminated-union extension: new class + union member with `Field(discriminator="type")` + paired `Strict*` drift mirror + `candidate_schema_v1.json` row.
  - **[GATE]** Exhaustive arms in 5+ dispatch sites (parser, compiler, `_common.artifact_id`, diff emitter, prompt builder); a missing arm is a latent runtime crash.
  - **[GATE]** Compiler: resolve → `_sql_safety.validate_identifier` / `validate_test_sql` → wrap. Dialect-driven, NO `from google.cloud import bigquery` under `signalforge/prune/` (AST import guard).
  - **[GATE]** Materialised-sample substitution if the test builds its own `FROM` (e.g. `COUNT(*) FROM {{ this }}`): rewrite `model.resolve_this().qualified_name` → `table_ref.qualified_name`. Pinned by test asserting compiled SQL references the temp table.
  - **[GATE]** Closed-set `DropReason` (5 values) — new failures route via existing `kept-without-evidence`, never grow the enum.
- **`llm-drafter.md`**
  - **[GATE]** `_PROMPT_VERSION` rotation if the **cached** system prompt changes — `tests/llm/test_prompt_cache_stability.py` snapshot moves in lockstep.
  - **[GATE]** `exclude_tests` dual-defence — prompt filter AND parser anchor-contract rejection.
  - **[GATE]** Collect-all anchor-contract violations; tolerant JSON extraction at `parse_draft_response`.
- **`prune-engine.md`**
  - **[GATE]** Conservative drop-reason taxonomy locked at 5 values.
  - **[GATE]** Compiler is dialect-driven (no `if dialect.name ==`); AST import guard.
  - **[GATE]** Drift detectors for `PruneResult` / `PruneDecision` / `PruneEvent`.
  - **[GATE]** Lazy-format JSON logger (no `_LOGGER.…(f"")`).
- **`grade-layer.md`**
  - *[soft]* Rubric is generic across variants; calibration of `min`/`max` either rides existing 4 criteria or adds a 5th (DEC needed).
  - **[GATE]** `_artifact_id_for` shared-module identity parity (`signalforge._common.artifact_id`).
  - **[GATE]** Grade prompt rotation if rubric criterion text changes.
- **`diff-renderer.md`**
  - **[GATE]** Tier classification (`kept` / `kept-uncertain` / `dropped` / `flagged`) is generic across variants.
  - **[GATE]** Fail-closed sidecar writer; ANSI strip on all user content.
  - **[GATE]** If shipping `tests/*.sql` fallback: extend the **6th fail-closed writer** at `_test_file_writer.py` (already exists from #116) for the new variant, OR keep it `custom_sql`-only and emit `row_count_between` only as YAML.
- **`ingest-layer.md`**
  - **[GATE]** `SkipReason` literal stays closed at 3 values; promotion of `expect_table_row_count_to_be_between` from skip to the new variant is a parser-arm change, not a literal change.
  - **[GATE]** Anchor-contract violations collected; never short-circuits.
- **`cli-layer.md`**
  - **[GATE]** 4-tier exit code taxonomy; 7th AST scan: every typed error mapped.
  - **[GATE]** 5-surface parity (help/docstring/ops-doc/test/DEC) + **6th surface = bundled SKILL.md**.
- **`skill-parity.md`**
  - **[GATE]** Parity gate `tests/cli/test_skill_cli_parity.py` — if any new CLI subcommand/flag/demo-command tokens land for #169, SKILL.md updates in the same commit.
- **`testing-signal.md`**
  - **[GATE]** Drift detectors mandatory for new read-back models; AST scans need planted-violation self-checks.
  - **[GATE]** Engineered determinism for any new live e2e (a tautology over real data is always-pass → dropped; engineered violation is guaranteed kept).

### Reusable design moves from #116 / #163 / #159

The Domain Expert subagent extracted ten moves directly applicable to #169:

1. Discriminated-union extension pattern (DEC-002 of #116).
2. Exhaustive arms audit (DEC-002/DEC-012 of #116).
3. Conservative-bias routing via existing `DropReason` literals (DEC-006/DEC-007 of #116).
4. Special-case `column=None` ahead of column-existence checks (DEC-002 of #116).
5. Dynamic-block prompt rendering with numbered envelope + parameterised breach error (DEC-001/DEC-009 of #163).
6. Parser cardinality gate — "at-least-one-test-per-declared-rule" (DEC-002/DEC-006 of #163).
7. Materialised-sample substitution if the test builds its own FROM (#116 QG bug).
8. `proposed_test_files` for non-YAML artifacts via the 6th fail-closed writer (DEC-010/DEC-011 of #116).
9. Ingest recognition without growing the closed `SkipReason` literal (DEC-013 of #116).
10. Type-coherence parser arm via sqlglot (DEC-003/DEC-006 of #159) — applicable if `where` clauses are allowed.

### Open questions (issue-flagged + discovery-flagged)

From the issue body:
- **Q-A: Variant naming.** `row_count_between` (matches dbt-expectations macro, grep-able) vs. `row_count` (broader, bounds as fields). Issue leans toward the former.
- **Q-B: Emission default form.** `dbt_expectations` YAML block vs. `custom_sql`-style `tests/*.sql` singular file vs. detect-and-choose from `packages.yml` / `manifest.json`.
- **Q-C: Sample-mode behaviour.** Always run against full table (single cheap `COUNT(*)`, independent of `prune.scope`) vs. route to `kept-without-evidence` when `scope="sample"`.
- **Q-D: `where`-filter support.** Allow LLM to propose `where`-filtered row counts (date-window guardrails) vs. keep the primitive simple (model-level total only). Issue says "probably out of scope."
- **Q-E: Grade-rubric calibration.** New criterion in `GradeConfig.rubric` vs. extend existing 4 default criteria with row-count language vs. defer to a follow-up.

Discovery-surfaced (planner needs to resolve):
- **Q-F: Promotion of `dbt_expectations.expect_table_row_count_to_be_between` in ingest.** Ships with #169 (AC-5 explicit) vs. deferred to #154's scope.
- **Q-G: `min=0` / `max=None` semantics.** AC-3 says vacuous bounds route through conservative-degrade; planner picks the recognition rule (e.g. `min <= 0` AND no `where` → vacuous → grade degrades to `score=None`).
- **Q-H: `where`-clause sqlglot type-coherence.** If `where` is supported (Q-D = yes), apply the #159 parser arm verbatim or skip.
- **Q-I: Degenerate-table carve-out.** An empty table with `min=100` always-fails the test on warehouse evaluation — routes to `kept` (real signal), but is that the operator's signal or a sentinel of a broken upstream? Likely `kept` (the test caught a real failure); document explicitly.

### Phase 1 scoping answers (provisional — promoted to DECs in Phase 3)

| ID | Question | Answer | Implication |
|--|--|--|--|
| Q-A | Variant name | **`row_count_between`** | Matches `dbt_expectations.expect_table_row_count_to_be_between` for grep-ability + ingest-recognition parity. Bounds are `minimum: int \| None` / `maximum: int \| None` fields on the model. |
| Q-B | Emission default form | **`dbt_expectations` YAML block always** | `tests: [{dbt_expectations.expect_table_row_count_to_be_between: {min_value: N, max_value: M}}]`. No `packages.yml` detection in v1. Operators without dbt-expectations see the YAML, get a clear error from `dbt parse`, and either install the package or remove the test. No `.sql` fallback (the 6th fail-closed test-file writer stays `custom_sql`-only). |
| Q-C | Sample-mode behaviour | **Always full-table COUNT(*); ignore `prune.scope`** | Single cheap `COUNT(*)` over `<table_ref>` directly. Bypass the sample-CTE wrap. Document the scope divergence prominently in `docs/prune-ops.md` + the variant docstring. Materialised-sample substitution still applies if the engine resolves `table_ref` to a temp table. |
| Q-D | `where`-filter support | **Yes, with sqlglot type-coherence (#159 pattern)** | `where: str \| None = None` on the variant. Parser arm validates via sqlglot the same way `_check_custom_sql_type_coherence` does today. Compiler renders `SELECT COUNT(*) FROM <table_ref> [WHERE <validated-where>]`. **Doubles the parser + compiler scope of this ticket** relative to the model-level-only path; budgeted accordingly. |

## Phase 2 — Architecture Review

### Phase 2 ratings

| Area | Rating | Headline |
|--|--|--|
| Security | concern | `where`-fragment validation seam (DEC-A); subquery-in-WHERE posture (DEC-B); ingest as fresh untrusted-input surface (DEC-C). |
| Performance | concern | AC-5 ingest of N row-count tests = N full-table COUNT(*) calls. Existing `prune.total_budget_seconds` + `maximum_bytes_billed` are the safety net; excess routes to `kept-without-evidence`. Document the cost honestly in ops doc (DEC-F). |
| Data Model | "blocker" = work scope, not defect | 5 exhaustive isinstance dispatch arms (compiler, artifact_id, emitter, ingest parser, draft parser anchor-contract). Routine for a 6th variant; lands in Phase 4 stories. No `DropReason` / `SkipReason` / `audit_schema_version` bump. |
| API Design | concern (naming only) | Pydantic field name decision (DEC-D); diff emitter's YAML uses dbt-expectations naming regardless. |
| Observability | pass | No new audit fields. No `config_hash` rotation. No `audit_schema_version` bump. AC-7 satisfied by docs walkthrough. |
| Testing | pass | ~35–50 new tests across existing modules; precedent #116 ≈ 40. No new AST scans. SKILL.md parity gate stays green. |
| Prompt-cache + Conservative-bias + Skill parity | pass | `_PROMPT_VERSION` rotates (expected; pinned by snapshot). DropReason locked at 5. Vacuous bounds route through rubric score → `flagged` (DEC-E). |

### Phase 2 concerns → DEC candidates (proposed; user confirms in Phase 3)

- **DEC-A — `where`-fragment validation seam.** Compose `SELECT COUNT(*) FROM <table_ref> WHERE <where>` then call existing `validate_test_sql` on the full statement. No new `validate_where_fragment` helper. Reuses `custom_sql` validation verbatim; pre-flight rejects `;`, `--`, `/* */`, unbalanced parens on the full SQL.
- **DEC-B — Subquery-in-WHERE posture.** Keep `_check_custom_sql_type_coherence`'s existing skip-when-uncertain rule. A `WHERE (SELECT 1 FROM information_schema...) > 0` parses cleanly but is not `Column <op> Column`, so the type-coherence arm skips silently. The warehouse executes the query subject to `maximum_bytes_billed` + `prune.total_budget_seconds`; excess → `kept-without-evidence`. No new parser surface.
- **DEC-C — Ingest AC-5 scope.** Promote `dbt_expectations.expect_table_row_count_to_be_between` with full args (`min_value`, `max_value`, AND the `where` config key). Operator-supplied `where` routes through the same compose-then-validate path as LLM-supplied. Closes AC-5 in this ticket; nothing deferred to #154.
- **DEC-D — Field naming on the Pydantic model.** `minimum: int \| None` / `maximum: int \| None` on `CandidateTestRowCountBetween` (matches existing variant precedent — `values`, `to`, `field` are prefix-free). Ingest parser maps dbt-expectations' `min_value` / `max_value` → `minimum` / `maximum`. Diff emitter renders `{min_value: N, max_value: M}` in the YAML block (dbt-expectations naming). Mapping seam: ingest parser inbound, diff emitter outbound.
- **DEC-E — Vacuous-bound handling at the grader.** A drafted `minimum=0, maximum=None` test is technically valid (Pydantic at-least-one-bound satisfied) but uselessly broad. The grader's existing 3-value degrade taxonomy stays locked. Instead, the calibration rubric criterion scores it low → existing `passed: bool` threshold → ships as `flagged`. Calibration prose lands in the rubric criterion text. No new degrade trigger; no new diff tier.
- **DEC-F — Cost transparency for full-table COUNT(*).** Document in `docs/prune-ops.md` § "row_count_between" that the test always scans the full table (or the temp table under `materialised+sample`). For `where`-filtered tests the scan is partition-aligned at best, full at worst. Existing `maximum_bytes_billed` cap + `prune.total_budget_seconds` are the safety net; excess routes to `kept-without-evidence`. No new code path; doc-only.



**Carry-over open questions for Refinement** (won't change the plan shape; resolved during DEC-logging):
- Q-E (grade-rubric calibration): new criterion vs. extend existing 4 — lean toward extending the existing "Correctness" / "Specificity" criterion with row-count guidance to keep the rubric stable; revisit if pilot shows operators don't see calibration scores.
- Q-F (ingest promotion timing): ships with #169 — AC-5 is explicit; treat as in-scope.
- Q-G (vacuous-bounds rule): `minimum is None and maximum is None` rejected by Pydantic validator; `(minimum or 0) <= 0 and maximum is None and where is None` → grader degrades to `score=None` ("vacuous bound on a non-empty table").
- Q-H: subsumed by Q-D = yes.
- Q-I (degenerate-table carve-out): empty table failing `min=N` routes to `kept` (real signal — that IS what the test is meant to catch). Documented, not special-cased.

## Architecture Review

## Refinement Log (Phase 3)

### Session 1 — 2026-05-30

Phase-1 scoping (Q-A through Q-D) and Phase-2 architecture concerns (DEC-A through DEC-F) confirmed. The DEC log below is the binding contract for Phase 4 story generation.

### Decisions

- **DEC-001 — Variant identity (resolves Q-A).** New variant is `CandidateTestRowCountBetween` with `type: Literal["row_count_between"]`, `column: None = None` (model-level only), `minimum: int | None`, `maximum: int | None`, `where: str | None`, `rationale: str | None`. Frozen, `extra="ignore"`. Pydantic validators: `minimum ≥ 0`, `maximum ≥ 0`, `minimum ≤ maximum` (when both set), at-least-one-of-(min, max) (model-after validator), `where` non-empty after strip. Rationale: name matches the `dbt_expectations` macro for ingest-recognition + grep parity (resolves Q-A); the four invariants are the existing variant precedent (`CandidateTestCustomSQL`).

- **DEC-002 — Emission default form (resolves Q-B).** Diff emitter renders kept `row_count_between` artifacts as a YAML block under the `dbt_expectations` namespace: `{dbt_expectations.expect_table_row_count_to_be_between: {min_value: N, max_value: M, where: "..."}}` (omitting null fields). No `.sql` fallback in v1; no `packages.yml` detection. Operators without dbt-expectations installed see the YAML, get a clear `dbt parse` error, and either install or remove. Rationale: the 6th fail-closed writer at `signalforge/diff/_test_file_writer.py` stays `custom_sql`-only; v1 keeps the surface narrow; `dbt-expectations` is industry-standard.

- **DEC-003 — Sample-mode behaviour (resolves Q-C).** `_compile_row_count_between` always emits `SELECT COUNT(*) FROM <table_ref> [WHERE <where>]` regardless of `prune.scope`. Under `prune.scope="sample"` + `sample_strategy="materialised"` the `table_ref` is the temp table (correctly), so the count remains cheap. Under `prune.scope="sample"` + `sample_strategy="oneshot"` the bypass is deliberate — a sampled COUNT(*) is semantically wrong. Documented in `docs/prune-ops.md` § "row_count_between" + the variant docstring.

- **DEC-004 — `where` clause support (resolves Q-D, Q-H).** Variant carries optional `where: str | None`. Parser anchor-contract arm threads the existing `model_columns_by_type` keyword-only through `_validate_anchor_contract` and reuses `_check_custom_sql_type_coherence` (or a tightly scoped sibling) — skip-when-uncertain posture preserved. sqlglot imports stay confined to `signalforge.draft.parser`.

- **DEC-005 — `where`-fragment validation (resolves DEC-A).** Compose `SELECT COUNT(*) FROM <table_ref> WHERE <where>` THEN call existing `validate_test_sql` on the full statement. No new `validate_where_fragment` helper. Reuses the `custom_sql` validation surface verbatim; pre-flight rejects `;`, `--`, `/* */`, unbalanced parens on the composed SQL. A `where` rejected by safety routes via `_InvalidIdentifier` → `kept-without-evidence` (`why="identifier rejected by SQL safety check"`).

- **DEC-006 — Subquery-in-WHERE posture (resolves DEC-B).** Keep `_check_custom_sql_type_coherence`'s existing skip-when-uncertain rule. A `WHERE (SELECT ...) > 0` parses cleanly but is not `Column <op> Column`; the type-coherence arm skips. Warehouse executes the query subject to `maximum_bytes_billed` + `prune.total_budget_seconds`; excess routes to `kept-without-evidence` via the existing `WarehouseError` path. No new parser code.

- **DEC-007 — Ingest AC-5 scope (resolves Q-F, DEC-C).** `signalforge.ingest.parser.parse_test_entry` gains a recognition arm for `dbt_expectations.expect_table_row_count_to_be_between` with full args: `min_value`, `max_value`, AND the `where` config key. Maps to `CandidateTestRowCountBetween(minimum=..., maximum=..., where=...)`. Operator-supplied `where` routes through the same compose-then-validate path as LLM-supplied (DEC-005). Malformed args (e.g. missing both bounds, non-int values, `min_value > max_value`) → `SkippedTest(reason="malformed-supported-test")`. **SkipReason stays the closed 3-value literal** (no new disposition).

- **DEC-008 — Pydantic field naming (resolves DEC-D).** `minimum: int | None` / `maximum: int | None` on the model (matches `values` / `to` / `field` prefix-free precedent across existing variants). Ingest parser maps inbound (`min_value` → `minimum`); diff emitter maps outbound (`minimum` → `min_value` in the YAML block to match dbt-expectations naming). The mapping seams are exactly two functions: `_parse_row_count_between` in ingest, `_render_test` arm in diff emitter.

- **DEC-009 — Vacuous-bound handling (resolves Q-G, DEC-E).** A drafted `minimum=0, maximum=None` test passes Pydantic validation (at-least-one-bound satisfied). The grader's existing 3-trigger degrade taxonomy (`LLMError` retry exhausted, `GradeOutputError`, total budget exceeded) **stays locked**. Instead: one of the existing 4 default rubric criteria (most likely `Specificity` or `Correctness`, refined during Phase 4) gains language scoring "is the bound a meaningful guardrail vs. trivially satisfiable?" — a vacuous bound scores low → existing `passed: bool` threshold → ships as `flagged`. **Caveat:** this rotates the grade-side `_PROMPT_VERSION` and updates the grade-prompt cache-stability snapshot (separate from drafter's `_PROMPT_VERSION`).

- **DEC-010 — Empty-table / degenerate-table carve-out (resolves Q-I).** An empty table with `minimum: 100` always-fails the COUNT(*) test → routes to `kept` (real signal — that IS what the test exists to catch). No special-case in the engine; document explicitly in `docs/prune-ops.md` so operators reading a `kept` decision against an empty warehouse table know the test fired correctly.

- **DEC-011 — DropReason / SkipReason / `audit_schema_version` lockdown.** All three taxonomies stay locked. Every `row_count_between` failure mode maps to existing literals: parser-rejected → `LLMOutputAnchorContractError` (CLI tier 2, never reaches prune); compiler-rejected → `_InvalidIdentifier` → `kept-without-evidence`; warehouse-rejected → `WarehouseError` → `kept-without-evidence`; budget-exhausted → `kept-without-evidence`; bytes-cap exceeded → `kept-without-evidence`; always-pass on warehouse → `always-passes`; failing rows → `kept` (or `failed-on-known-clean-data` on trusted models). Ingest unsupported shapes → `malformed-supported-test` or `custom-or-generic-test`. No `audit_schema_version` bump on any sidecar (variant is "more data of the same shape").

- **DEC-012 — Drafter `_PROMPT_VERSION` rotates; cached-block golden unchanged.** Adding `row_count_between` to `_TEST_CATALOGUE_LINES` changes the cached system prompt → `_PROMPT_VERSION` rotates → `_EXPECTED_PROMPT_VERSION` constant in `tests/llm/test_prompt_cache_stability.py` updates in the same commit. `_CACHED_BLOCK_GOLDEN` (manifest summary) stays untouched. Worked example for the dynamic prompt section: "When the SQL shows a bounded aggregation (`GROUP BY` + date-window `WHERE`), propose `row_count_between` with calibrated `minimum` ≥ 1 to catch upstream pipeline gaps."

- **DEC-013 — Discriminated-union exhaustive arms.** 5 production isinstance dispatch sites grow a `CandidateTestRowCountBetween` arm in the same commit: (a) `signalforge.prune.compiler._compile_test`, (b) `signalforge._common.artifact_id.model_test_args_hash`, (c) `signalforge.diff._emitter._render_test`, (d) `signalforge.ingest.parser._parse_named_test`, (e) `signalforge.draft.parser._validate_anchor_contract`. The 6th (grade-side `_resolve_artifact_from_candidate`) uses string-discriminator matching, not isinstance — no arm change. Drift detector at `tests/draft/test_drift_detector.py` gains `StrictCandidateTestRowCountBetween` mirror; `tests/fixtures/draft/candidate_schema_v1.json` gains a fixture row (no v1 → v2 rename — schema-version is forward-compat).

- **DEC-014 — Cost-transparency documentation (resolves DEC-F).** `docs/prune-ops.md` gains a § "Row-count-between cost model": full-table COUNT(*) is not metadata-free on BigQuery (it scans) nor on Snowflake (it scans unless empty). Under `where`-clause, the scan is partition-aligned at best. `maximum_bytes_billed` + `prune.total_budget_seconds` are the safety nets; excess routes to `kept-without-evidence`. Operators ingesting many existing `expect_table_row_count_to_be_between` declarations should plan budget accordingly. No code change; doc-only.

- **DEC-015 — 5-surface parity for #169.** Changes to the variant's user-visible surface (the new `exclude_tests` token, the dbt-expectations YAML shape, the cost guidance) ripple across: (1) argparse help — N/A, no CLI flag; (2) handler/lib docstrings; (3) `docs/draft-ops.md` + `docs/prune-ops.md` + `docs/grade-ops.md` + `docs/diff-ops.md` + `docs/ingest-ops.md`; (4) tests; (5) this plan-doc's DEC list. SKILL.md (6th surface) gains a paragraph in the operator-facing section describing the new variant + the `exclude_tests` token, but the parity gate at `tests/cli/test_skill_cli_parity.py` stays green automatically (no new subcommand/flag/demo-command tokens).

## Detailed Breakdown (Phase 4)

Story order follows the natural data-flow direction so each story can be tested independently: models → config → ingest → drafter (prompts + parser) → prune (compiler) → grade (rubric) → diff (emitter) → docs + skill → e2e → Quality Gate → Patterns & Memory.

**Validation command** for every story's "done when": `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.

---

### US-001 — Variant model + drift detector + fixture row

**Description.** Land `CandidateTestRowCountBetween` in `signalforge.draft.models` with the four Pydantic invariants (DEC-001). Add the paired strict mirror in `tests/draft/test_drift_detector.py`. Extend `tests/fixtures/draft/candidate_schema_v1.json` with one model-level `row_count_between` row exercising all four fields (no v1→v2 rename).

**Traces to:** DEC-001, DEC-008, DEC-013.

**Files:**
- `src/signalforge/draft/models.py` — new `CandidateTestRowCountBetween` class; extend `CandidateTest` union; extend `__all__`.
- `tests/draft/test_drift_detector.py` — `StrictCandidateTestRowCountBetween(extra="forbid")` + union.
- `tests/fixtures/draft/candidate_schema_v1.json` — one new row.
- `tests/draft/test_models.py` — validators.

**TDD.**
- Field validator: `minimum < 0` raises.
- Field validator: `maximum < 0` raises.
- Field validator: `where` empty or whitespace-only raises.
- Model validator: both bounds None raises.
- Model validator: `minimum > maximum` raises.
- Round-trip: `model_validate` / `model_dump_json` byte-stable.
- `frozen=True` enforced.
- Drift detector loads fixture under `extra="forbid"` (every variant row including the new one passes).

**Acceptance criteria.** All TDD tests pass. Drift detector test green. Canonical validation command green.

**Done when.** `uv run pytest tests/draft/test_models.py tests/draft/test_drift_detector.py` passes with the new variant exercised.

**Depends on:** none.

---

### US-002 — `exclude_tests` token + draft config

**Description.** Add `"row_count_between"` to `VALID_TEST_TYPES` in `signalforge.draft.config`. Update the docstring of `_TEST_CATALOGUE_LINES` and `_coerce_exclude_tests` to reflect 5 + 1 variants.

**Traces to:** DEC-001.

**Files:**
- `src/signalforge/draft/config.py` — `VALID_TEST_TYPES` frozenset + docstring.
- `tests/draft/test_config.py` — pin the new token round-trips through config load + exclude.

**TDD.**
- `exclude_tests=["row_count_between"]` validates.
- `exclude_tests=["row_count_betwen"]` (typo) fails with a "valid types: …" error listing the new token.

**Acceptance criteria.** Token recognized by config; validator rejects typos clearly.

**Done when.** `uv run pytest tests/draft/test_config.py` passes.

**Depends on:** US-001.

---

### US-003 — Drafter prompt catalogue + `_PROMPT_VERSION` rotation

**Description.** Add `row_count_between` to `_TEST_CATALOGUE_LINES` in `signalforge.draft.prompts`. Include a worked-example JSON shape illustrating both the no-`where` and with-`where` forms. Compute the new `_EXPECTED_PROMPT_VERSION` hex; update `tests/llm/test_prompt_cache_stability.py` constant + docstring rotation-history entry. Confirm `_CACHED_BLOCK_GOLDEN` (manifest summary) is untouched. Add coverage for `exclude_tests=["row_count_between"]` filtering the catalogue line out.

**Traces to:** DEC-012.

**Files:**
- `src/signalforge/draft/prompts.py` — `_TEST_CATALOGUE_LINES` entry; `_CUSTOM_SQL_CATALOGUE_LINE` docstring tweak.
- `tests/draft/test_prompts.py` — catalogue includes the new line; `exclude_tests` removes it.
- `tests/llm/test_prompt_cache_stability.py` — new `_EXPECTED_PROMPT_VERSION`; rotation-history bullet.

**TDD.**
- `_render_system_prompt(())` byte-stable across two calls and includes `row_count_between` in catalogue.
- `_render_system_prompt(("row_count_between",))` excludes it.
- `_PROMPT_VERSION` recomputed → matches the new pinned hash.
- `_CACHED_BLOCK_GOLDEN` snapshot still passes (unchanged).

**Acceptance criteria.** Cache-stability test green with new hash. Exclude-tests filter behaves correctly.

**Done when.** `uv run pytest tests/draft/test_prompts.py tests/llm/test_prompt_cache_stability.py` green.

**Depends on:** US-001, US-002.

---

### US-004 — Anchor-contract arm + `where`-clause type-coherence

**Description.** Extend `_validate_anchor_contract` in `signalforge.draft.parser` to handle `CandidateTestRowCountBetween`: model-level only (no parent-column check), validate `where` (if present) against `model_columns_by_type` via the existing `_check_custom_sql_type_coherence` helper (or a tightly scoped sibling that reuses the same sqlglot machinery). Reject candidates whose `where` references unknown columns with a violation appended to the collect-all `violations` tuple. `exclude_tests=["row_count_between"]` rejects any drafted row_count_between with a violation. Confirm sqlglot imports stay confined to `signalforge.draft.parser`.

**Traces to:** DEC-004, DEC-006, DEC-013.

**Files:**
- `src/signalforge/draft/parser.py` — anchor-contract arm + optional helper extension.
- `tests/draft/test_parser.py` — tests below.

**TDD.**
- Valid `row_count_between` with both bounds + no where → no violations.
- `where: "user_id > 100"` referencing a real column → no violations.
- `where: "phantom_col > 1"` (unknown column) → violation in the collected tuple.
- `where: "(SELECT 1 FROM foo) > 0"` (subquery, skip-when-uncertain) → no violations (DEC-006).
- `exclude_tests=("row_count_between",)` + a drafted `row_count_between` → violation listed.
- Collect-all preserved: a candidate with both an unknown-column `where` AND another type's violation produces BOTH.

**Acceptance criteria.** All TDD tests pass. sqlglot import scan confirms no new module imports it.

**Done when.** `uv run pytest tests/draft/test_parser.py` passes.

**Depends on:** US-001.

---

### US-005 — Ingest parser arm (AC-5)

**Description.** `signalforge.ingest.parser` recognises `dbt_expectations.expect_table_row_count_to_be_between` and routes it to the new variant. Extracts `min_value`, `max_value`, and the `where` config key. Malformed shapes (both bounds missing, non-int bounds, `min_value > max_value`, non-string `where`) → `SkippedTest(reason="malformed-supported-test")`. Column-scoped usage (which is not representable on this variant) → `SkippedTest(reason="malformed-supported-test")` with a descriptive `detail` string.

**Traces to:** DEC-007, DEC-008, DEC-011.

**Files:**
- `src/signalforge/ingest/parser.py` — new `_parse_row_count_between` + dispatch arm in `_parse_named_test`.
- `tests/ingest/test_parser.py` — tests below.
- `tests/fixtures/ingest/row_count_between_schema.yml` — new fixture with kept + skipped cases.

**TDD.**
- Valid `dbt_expectations.expect_table_row_count_to_be_between` with `min_value: 100, max_value: 10000` → `CandidateTestRowCountBetween(minimum=100, maximum=10000)`.
- Same with `where: "event_date >= '2024-01-01'"` → variant carries `where` field.
- Missing both bounds → SkippedTest with `"malformed-supported-test"` and descriptive detail.
- `min_value > max_value` → SkippedTest with `"malformed-supported-test"`.
- Column-scoped usage → SkippedTest with `"malformed-supported-test"`.
- Different macro (`expect_table_row_count_to_equal`) → SkippedTest with `"custom-or-generic-test"` (no change to existing behaviour).
- `SkipReason` literal still exactly 3 values.

**Acceptance criteria.** AC-5 fixture round-trips. SkipReason stays closed at 3.

**Done when.** `uv run pytest tests/ingest/` passes.

**Depends on:** US-001.

---

### US-006 — `_common.artifact_id` arm + cross-stage parity

**Description.** Extend `model_test_args_hash` in `signalforge._common.artifact_id` with a `CandidateTestRowCountBetween` arm. Hash domain: `type`, `column` (always `None`), `minimum`, `maximum`, `where` (or its omission) — canonical JSON. Confirm cross-stage `is` identity parity (`signalforge.diff._artifact_id` and `signalforge.grade.engine` re-export the helper).

**Traces to:** DEC-013.

**Files:**
- `src/signalforge/_common/artifact_id.py` — new arm.
- `tests/diff/test_artifact_id.py` — tests below.

**TDD.**
- Two `row_count_between` tests with identical (min, max, where) → identical hash.
- Differing `minimum` → different hash.
- Differing `where` (None vs "x > 1") → different hash.
- `is`-identity parity across `signalforge._common.artifact_id`, `signalforge.diff._artifact_id`, `signalforge.grade.engine` preserved.

**Acceptance criteria.** Collision detection works; cross-stage parity test green.

**Done when.** `uv run pytest tests/diff/test_artifact_id.py` passes.

**Depends on:** US-001.

---

### US-007 — Prune compiler arm + dialect snapshots

**Description.** Implement `_compile_row_count_between` in `signalforge.prune.compiler`. Always emits `SELECT COUNT(*) FROM <quoted-table> [WHERE <where>]` regardless of `prune.scope` (DEC-003). Composes the full statement THEN validates via existing `validate_test_sql` (DEC-005); on rejection, returns `_InvalidIdentifier` → engine routes to `kept-without-evidence` (DEC-011). Identifier quoting / case-folding via the existing `Dialect` value-object fields (no new fields, no `if dialect.name ==`, no `from google.cloud import bigquery`). Extend `_compile_test` dispatcher with the isinstance arm. Add BigQuery + Snowflake snapshot fixtures.

**Traces to:** DEC-003, DEC-005, DEC-006, DEC-013, DEC-014.

**Files:**
- `src/signalforge/prune/compiler.py` — new helper + dispatcher arm.
- `tests/prune/test_compiler.py` — TDD below.
- `tests/fixtures/prune/compiled_sql/{row_count_between,row_count_between_where,row_count_between_only_min,row_count_between_only_max}.sql` — BigQuery snapshots.
- `tests/fixtures/prune/compiled_sql/snowflake/{row_count_between,row_count_between_where}.sql` — Snowflake snapshots.

**TDD.**
- Happy path no-`where` → snapshot match.
- With-`where` → snapshot match.
- Hostile `where` (`"1=1; DROP TABLE users"`) → compiler returns `_InvalidIdentifier`; engine wires to `kept-without-evidence` via DEC-005 path.
- BigQuery dialect: backtick-quoted single qualified token.
- Snowflake dialect: per-component double-quoted, UPPER-folded.
- AST import-guard test confirms no new vendor SDK imports under `signalforge/prune/`.
- Under `materialised+sample`, the engine passes `table_ref=<temp>`, so the compiled SQL references the temp table (NOT the source). Pinned by a dedicated assertion that the compiled SQL contains `_SESSION._sf_sample_*` when the engine path is materialised.

**Acceptance criteria.** All snapshots match. Import guard green. Materialised-sample substitution pinned.

**Done when.** `uv run pytest tests/prune/test_compiler.py` passes including the dialect-snapshot suite.

**Depends on:** US-001.

---

### US-008 — Prune engine routing + decision-matrix coverage

**Description.** Verify the existing decision matrix in `signalforge.prune.engine._decide_from_test_result` routes `row_count_between` correctly across every failure mode (DEC-011). No code change to the engine expected — the matrix is test-type-agnostic. Add tests pinning:

**Traces to:** DEC-005, DEC-010, DEC-011.

**Files:**
- `tests/prune/test_engine.py` — tests below.

**TDD.**
- Always-pass on warehouse (COUNT(*) within bounds) → tier `always-passes`, dropped.
- Failing rows (COUNT(*) outside bounds, untrusted model) → tier `kept`.
- Failing rows (COUNT(*) outside bounds, trusted model) → tier `failed-on-known-clean-data`, dropped.
- Compiler returns `_InvalidIdentifier` → tier `kept-without-evidence` with `why="identifier rejected by SQL safety check"`.
- Warehouse raises (e.g. `TableNotFoundError`) → tier `kept-without-evidence`.
- Total-budget exceeded mid-test → tier `kept-without-evidence` with the locked budget `why` text.
- Empty table with `minimum: 100` (failing rows = 0 on COUNT, so engine treats as failing? — verify against decision matrix; per DEC-010 this routes to `kept` because COUNT(*) = 0 ≠ in-bounds when min ≥ 1).
- DropReason literal still exactly 5 values (drift detector test).

**Acceptance criteria.** Every routing path pinned. DropReason stays closed.

**Done when.** `uv run pytest tests/prune/test_engine.py` passes.

**Depends on:** US-001, US-007.

---

### US-009 — Grade rubric calibration + `_PROMPT_VERSION` rotation

**Description.** Extend one of the existing 4 default `GradeConfig.rubric` criteria (most likely `Specificity` — refined when looking at the live criterion text) with language scoring "is the bound a meaningful guardrail vs. trivially satisfiable?" Rotate the grade-side `_PROMPT_VERSION` (separate from drafter's) and update its cache-stability snapshot. Verify the grader's score-and-degrade taxonomy stays at 3 triggers (DEC-009, DEC-011).

**Traces to:** DEC-009, DEC-011, DEC-012.

**Files:**
- `src/signalforge/grade/rubric.py` (or wherever the 4 default criterion texts live) — refined criterion text.
- `tests/grade/test_engine.py` — vacuous-bound test.
- `tests/grade/test_prompt_cache_stability.py` (if it exists; else add it parallel to the drafter's) — new `_EXPECTED_GRADE_PROMPT_VERSION`.

**TDD.**
- Drafted `row_count_between` with `minimum: 0, maximum: None` graded against a healthy model → criterion score is low (~0.1).
- Drafted `row_count_between` with `minimum: 100, maximum: 10000` on a model with ~5K rows → criterion score is high (~0.85).
- Vacuous-bound test ships with tier `flagged`, NOT `kept-uncertain` (DEC-009).
- Three-trigger degrade taxonomy unchanged (drift detector test).

**Acceptance criteria.** Calibration scoring produces meaningful spread. Three-trigger taxonomy stays locked.

**Done when.** `uv run pytest tests/grade/` passes.

**Depends on:** US-001.

---

### US-010 — Diff emitter arm + YAML shape

**Description.** Extend `_render_test` in `signalforge.diff._emitter` with the `CandidateTestRowCountBetween` arm emitting `{dbt_expectations.expect_table_row_count_to_be_between: {min_value: N, max_value: M, where: "..."}}` (omitting null fields). The `proposed_test_files` path stays `custom_sql`-only (DEC-002). Tier classification (`kept` / `kept-uncertain` / `flagged`) is generic and needs no per-variant arm.

**Traces to:** DEC-002, DEC-008, DEC-013.

**Files:**
- `src/signalforge/diff/_emitter.py` — `_render_test` arm.
- `tests/diff/test_emitter.py` — TDD below.
- `tests/diff/test_engine.py` — tier-classification pin.

**TDD.**
- YAML shape: `{dbt_expectations.expect_table_row_count_to_be_between: {min_value: N, max_value: M}}` (no `where`, no null fields).
- With-where YAML shape: includes `where: "..."` quoted appropriately by `yaml.safe_dump`.
- Hostile content in `where` (e.g. multi-line string, embedded quote chars) → YAML-safe via `yaml.safe_dump`.
- ANSI strip applied to user content (the `rationale` field of the variant) per existing diff DEC-007.
- Markdown emission: variant appears in the kept-table; `where` content rendered through the table-cell HTML-entity escape.

**Acceptance criteria.** YAML byte-stable across snapshots. Markdown safe-escape verified.

**Done when.** `uv run pytest tests/diff/test_emitter.py tests/diff/test_engine.py` passes.

**Depends on:** US-001.

---

### US-011 — Docs (`draft-ops`, `prune-ops`, `grade-ops`, `diff-ops`, `ingest-ops`) + CHANGELOG + SKILL.md

**Description.** Update the five `docs/*-ops.md` files + `CHANGELOG.md` + `src/signalforge/skills/signalforge/SKILL.md` per the 5-surface parity rule (DEC-015). The SKILL.md update is the 6th surface; the parity gate at `tests/cli/test_skill_cli_parity.py` stays green automatically since no new subcommand/flag/demo-command tokens land.

**Content per doc:**
- `docs/draft-ops.md` — new § "Row-count tests (`row_count_between`)" describing the catalogue entry, when the drafter proposes it, and how `exclude_tests=["row_count_between"]` suppresses it. Include the worked example (DEC-012 prose).
- `docs/prune-ops.md` — new § "Row-count cost model" (DEC-014): always full-table COUNT(*); `where`-filtered partition-alignment caveat; `maximum_bytes_billed` + `total_budget_seconds` as safety nets; routes excess to `kept-without-evidence`. Also document the empty-table → `kept` rule (DEC-010).
- `docs/grade-ops.md` — note the calibration criterion's intent for row-count bounds (DEC-009); confirm the 3-trigger degrade taxonomy stays locked.
- `docs/diff-ops.md` — sample of the dbt-expectations YAML emission shape (DEC-002); note about operators without `dbt-expectations` installed seeing the YAML and a clear `dbt parse` error.
- `docs/ingest-ops.md` — recognition of `dbt_expectations.expect_table_row_count_to_be_between` from external schema.yml (AC-5); how malformed args route to `SkippedTest(reason="malformed-supported-test")`.
- `CHANGELOG.md` — unreleased section bullet: "Added: `row_count_between` as a 6th first-class test primitive (drafter + prune + grade + diff + ingest). Operators can suppress via `exclude_tests=[\"row_count_between\"]`."
- `SKILL.md` — one paragraph in the operator-facing section describing the new variant + the `exclude_tests` token. AC-7 reproducible-example narrative lands here (or in `docs/draft-ops.md`).

**Traces to:** DEC-002, DEC-009, DEC-010, DEC-012, DEC-014, DEC-015 + AC-7.

**Files:** as enumerated above.

**Acceptance criteria.** All docs updated. CHANGELOG carries the unreleased bullet. SKILL.md parity gate passes.

**Done when.** `uv run pytest tests/cli/test_skill_cli_parity.py` green + manual doc review.

**Depends on:** US-001 through US-010 (descriptive accuracy depends on the implementation being settled).

---

### US-012 — Engineered-determinism live e2e (gated)

**Description.** Extend the gated `@pytest.mark.bigquery` smoke (and `@pytest.mark.snowflake` if cheap) plus `@pytest.mark.e2e` full-pipeline test with one assertion exercising `row_count_between`. Use the two engineered-determinism tricks:
- Always-pass-and-drop: rely on cooperative LLM proposal of a wide bound that the warehouse satisfies trivially → routes to `always-passes`.
- Engineered-failure-kept: drafted (or operator-injected, similar to `meta.signalforge.business_rules`) `row_count_between` with `where: "false"` → COUNT = 0 → if `minimum: 1`, mathematically guaranteed failing rows → `kept`. Engineered-injection follows the #157 isolation primitives (`copy_fixture_to_tmp` + `inject_model_business_rules` analog).

**Traces to:** AC-1, AC-2, AC-7.

**Files:**
- `tests/cli/test_e2e_bigquery_smoke.py` (or parallel module) — new assertion.
- `tests/cli/_e2e_helpers.py` — if a `inject_model_row_count_rules` helper is needed.

**Acceptance criteria.** Live gated tests pass against the bikeshare fixture (or analog). The intuit_airflow `weekly_query_cost`-style narrative lands in the doc walkthrough (AC-7).

**Done when.** Maintainer runs `SF_RUN_BQ=1 ANTHROPIC_API_KEY=… GOOGLE_CLOUD_PROJECT=… uv run pytest -m bigquery --no-cov -k row_count_between` green.

**Depends on:** US-001 through US-011.

---

### US-013 — Quality Gate (code-review × 4 + CodeRabbit)

**Description.** Run code-reviewer 4 times with diverse angles across the full #169 changeset; fix all real bugs found each pass. Run `/code-review --comment` for inline PR comments. Run validation after each pass. Run CodeRabbit review on the PR; resolve all real-bug findings.

**Diverse reviewer angles (per memory `qg-diverse-reviewer-angles-catch-cross-surface-drift`):**
1. Correctness — focus on the 5 dispatch arms (compiler, artifact_id, emitter, ingest parser, draft parser); pin every isinstance branch lands and any `column=None` special-case fires AHEAD of column-existence checks.
2. Conventions — DropReason / SkipReason / `audit_schema_version` lockdowns; sqlglot confinement; dialect-driven compiler (no `if dialect.name ==`).
3. Tests — drift detector covers the new variant; cache-stability snapshots rotated correctly; engineered-determinism tricks deterministic; collect-all anchor-contract preserved.
4. Docs + UX — 5-surface parity holds (8 files updated in lockstep per DEC-015); CHANGELOG accurate; SKILL.md parity gate green; vacuous-bound calibration text reads as operator-actionable.

**Traces to:** all DECs.

**Files:** all touched in US-001…US-012.

**Acceptance criteria.** All 4 code-review passes find no high-priority real bugs. CodeRabbit findings either fixed or formally responded to. Canonical validation command green. No `Traceback` in CLI test stderr.

**Done when.** PR has 4 review comments confirming clean passes + CodeRabbit findings resolved.

**Depends on:** US-001 through US-012.

---

### US-014 — Patterns & Memory

**Description.** Update durable rules + memory with patterns learned. Specifically:
- `.claude/rules/business-rule-tests.md` — append/update with: this is now the 6th variant addition (the 2nd time the pattern has shipped after `custom_sql` #116). Reinforce the dispatch-arm audit list. Note the `where`-clause + sqlglot reuse from #159.
- Consider whether a new top-level rule `.claude/rules/test-variant-addition.md` is warranted (extracting the generic "how to add a variant" pattern from the now-2-instance precedent). Decision: defer unless a 3rd variant is on the roadmap — premature abstraction beats a precedent path for now.
- Memory: a new memory `signalforge-row-count-between-pattern.md` noting which 5 dispatch arms grow and which (grade) stays string-discriminator-based; cross-link to `[[two-name-skills-skill-convention]]`-style memories about the 6th writer / 5-surface parity.

**Traces to:** all DECs (durable knowledge capture).

**Files:**
- `.claude/rules/business-rule-tests.md` — update.
- `/home/wesd/.claude/projects/-home-wesd-Projects-SignalForge/memory/MEMORY.md` + new memory file — add cross-cutting pattern memory.

**Acceptance criteria.** Rule file accurately reflects the 2-instance precedent. Memory captures the dispatch-arm list + sqlglot reuse pattern.

**Done when.** Plan-doc owner reviews the rule update and approves.

**Depends on:** US-013.

---

### Story dependency graph

```
US-001 (models)
   ├─→ US-002 (config token)
   ├─→ US-003 (prompt + cache rotation)        (depends on US-001, US-002)
   ├─→ US-004 (parser anchor-contract)
   ├─→ US-005 (ingest parser)
   ├─→ US-006 (artifact_id)
   ├─→ US-007 (compiler + dialect snapshots)
   ├─→ US-009 (grade rubric)
   └─→ US-010 (diff emitter)
US-007 ─→ US-008 (prune engine routing)
US-001…US-010 ─→ US-011 (docs + SKILL.md)
US-001…US-011 ─→ US-012 (live e2e gated)
US-001…US-012 ─→ US-013 (Quality Gate)
US-013      ─→ US-014 (Patterns & Memory)
```

`US-002…US-010` can largely run in parallel after `US-001` lands, with the exception of `US-003` waiting on `US-002` (the prompt builder depends on `VALID_TEST_TYPES`) and `US-008` waiting on `US-007` (engine tests exercise the compiler). The "serialize shared-registry beads" memory applies — `US-003` rotates `_PROMPT_VERSION` and `US-009` rotates the grade-side `_PROMPT_VERSION`; both touch cache-stability snapshots in different files but are still best run sequentially to avoid two workers fighting the same kind of golden update.

### Test budget estimate

~35–50 new tests across `tests/draft/`, `tests/prune/`, `tests/diff/`, `tests/ingest/`, `tests/grade/`, plus 1 gated e2e assertion. Precedent #116 was ~40 — this ticket is slightly simpler (no `.sql` file emission, no `proposed_test_files` arm, no new fail-closed writer) but slightly more complex on the `where` + ingest side. Net: comparable order of magnitude.

## Beads Manifest (Phase 7)

Devolved 2026-05-31. Worktree: `../worktrees/SignalForge/169-row-count-between` on branch `feature/169-row-count-between`. PR: [#176](https://github.com/wjduenow/SignalForge/pull/176).

**Epic:** `bd_1-scaffolding-tt8` — #169: Add row_count_between as 6th first-class test primitive

| Bead ID | Story | Depends on |
|--|--|--|
| `bd_1-scaffolding-tt8.1` | US-001 — model + drift detector + fixture row | — (entry point) |
| `bd_1-scaffolding-tt8.2` | US-002 — `VALID_TEST_TYPES` token + `exclude_tests` round-trip | `.1` |
| `bd_1-scaffolding-tt8.3` | US-004 — parser anchor-contract arm + sqlglot type-coherence | `.1` |
| `bd_1-scaffolding-tt8.4` | US-005 — ingest parser (AC-5) | `.1` |
| `bd_1-scaffolding-tt8.5` | US-006 — `_common.artifact_id` arm + cross-stage parity | `.1` |
| `bd_1-scaffolding-tt8.6` | US-007 — prune compiler + BigQuery + Snowflake snapshots | `.1` |
| `bd_1-scaffolding-tt8.7` | US-009 — grade rubric calibration + grade `_PROMPT_VERSION` rotation | `.1` |
| `bd_1-scaffolding-tt8.8` | US-010 — diff emitter arm + dbt-expectations YAML | `.1` |
| `bd_1-scaffolding-tt8.9` | US-003 — drafter prompt catalogue + drafter `_PROMPT_VERSION` rotation | `.1`, `.2` |
| `bd_1-scaffolding-tt8.10` | US-008 — prune engine routing matrix coverage | `.6` |
| `bd_1-scaffolding-tt8.11` | US-011 — docs (5 ops + CHANGELOG + SKILL.md) | `.2, .3, .4, .5, .7, .8, .9, .10` |
| `bd_1-scaffolding-tt8.12` | US-012 — engineered-determinism live e2e | `.11` |
| `bd_1-scaffolding-tt8.13` | Quality Gate — code-review × 4 + CodeRabbit | `.12` |
| `bd_1-scaffolding-tt8.14` | Patterns & Memory — update business-rule-tests rule + memory | `.13` |

**Ralph entry point:** `bd ready` → `bd_1-scaffolding-tt8.1` is the only ready implementation story; everything else is blocked until it lands. After `.1` closes, eight stories (`.2`, `.3`, `.4`, `.5`, `.6`, `.7`, `.8`, `.9`) become ready in parallel; budget Ralph workers accordingly. `.9` (US-003) waits on `.2` (`VALID_TEST_TYPES`) because the prompt builder reads it.

**Serialize-shared-registry caution.** Per memory [[ralph-serialize-shared-registry-beads]], avoid running two concurrent workers on stories that touch the same shared surface. For this epic, the main risk surfaces:
- `.9` (US-003) rotates the drafter `_PROMPT_VERSION` and touches `tests/llm/test_prompt_cache_stability.py`.
- `.7` (US-009) rotates the grade-side `_PROMPT_VERSION` and touches a different cache-stability test (parallel module).

Different files; safe to parallelize, but if conflicts surface, serialize `.7` after `.9`.

## Output

```
Plan devolved to beads.

Epic: bd_1-scaffolding-tt8 (#169)
Tasks: 14 (12 implementation + Quality Gate + Patterns & Memory) with dependencies

Next steps:
1. Run Ralph: /ralph-run
2. Monitor: bd list --status=in_progress
3. When done: /closeout
```
