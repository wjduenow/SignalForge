# Super Plan — #267: aggregate/scalar dbt-expectations macros — source-table routing (Direction 2)

## Meta

- **Ticket:** https://github.com/wjduenow/SignalForge/issues/267
- **Branch / worktree:** `feature/267-aggregate-macro-routing` @ `../worktrees/SignalForge/267-aggregate-macro-routing`
- **Phase:** detailing
- **Parent:** #154 (dbt-expectations prune+grade adapter via manifest `compiled_code`) — landed, PR #266, `plans/super/154-dbt-expectations-prune.md` DEC-004.
- **Sibling follow-up (keep distinct):** #268 (`scope=sample` AST relation-rewrite for ingested tests).
- **Sessions:** 1 (2026-07-05)

---

## Phase 1 — Discovery

### What the ticket asks

#154's first pass prunes **row-returning** compiled test bodies only. Aggregate/scalar-returning
compiled bodies are **skip-recorded** because the adapter's `SELECT COUNT(*) AS failures FROM (<sql>) AS t`
envelope, wrapped around a scalar body, yields `failures=1` **always** → the engine routes every such
test to `kept` regardless of the true verdict (the exact `row_count_between` bug, since fixed). #267
tracks *supporting* those bodies: detect the aggregate shape, force source-table routing (Direction 2),
and decide the pass/fail verdict shape.

### Two findings that reshape the ticket

**Finding A — source-table routing is ALREADY implemented (by #154).**
`signalforge.prune.engine._test_requires_source_table` (L590–591) already returns `True` for **any**
`from_manifest` custom_sql under `materialised` and `oneshot`; under `scope=full` (`sample_strategy=None`)
`compile_table_ref` already resolves to the source. Both engine sites (the `all_bypass_to_source`
short-circuit L1443–1448 and the per-test `per_test_table_ref` override L1595–1599) consult that one
helper. So the acceptance criterion "routed to the source table under every sample strategy" is **already
structurally satisfied** for ingested aggregate bodies the moment they stop being skip-recorded. #267
adds **no new engine routing arm** — only the behavioural mixed-candidate pin the rules require, plus the
ingest-gate change and the verdict-shape restructure.

**Finding B — dbt-expectations macros are ALREADY row-returning.**
dbt-expectations wraps *every* macro (incl. `expect_table_row_count_to_be_between`) in a row-returning
`… validation_errors as (select * … where not(expression = true)) select * from validation_errors` shell.
So the ticket's headline examples are already handled by #154's row-returning path. The bodies that
*actually* get skip-recorded as scalar/aggregate are hand-written singular tests or non-expectations
generic tests whose compiled body is a bare `SELECT COUNT(*) …` / single numeric. The committed
`tests/fixtures/dbt_project_expectations` fixture contains **no** bare-scalar body today; the aggregate
cases live only as synthetic inline strings in unit tests.

### The crux — verdict semantics for a scalar body

A dbt **singular test's** contract is "returned rows ARE the failures; pass = zero rows." A scalar/aggregate
body (`SELECT COUNT(*) FROM t`) breaks that contract — it returns one *value* row, not failing rows — which
is why `_AGGREGATE_SKIP_DETAIL` calls it "structurally unusable." For `row_count_between` we hold the bounds
in structured fields (`minimum`/`maximum`) and restructure to
`SELECT n FROM (SELECT COUNT(*) AS n …) AS rc WHERE n < min OR n > max`. For an **ingested** aggregate
custom_sql there are **no structured bound fields** — the threshold, if any, is not recoverable from the
compiled SQL. Under dbt's "returned rows = failures" convention, a `COUNT(*) [WHERE cond]` body means
"cond-rows (or all rows) are failures," so a failing-rows form can be recovered for the **count-of-rows
idiom**; a non-count aggregate (`AVG`/`SUM`/`MIN`/`MAX`) carries no such convention and cannot be soundly
interpreted. This is the central design decision (see Refinement Q1).

### Code map (grounded)

| Concern | File · symbol · lines | Note |
|---|---|---|
| Ingest skip gate (the lever) | `ingest/reader.py` · `_classify_manifest_test` L643–649 | `if not is_row_returning(cc): skip malformed-supported-test`. #267 changes this. |
| Aggregate-shape classifier | `ingest/_compiled_sql.py` · `is_row_returning` L170–211, `_projection_collapses` L140–167 | sqlglot-AST; `False` = scalar. Reuse as the positive gate; likely extend to sub-classify count-family. |
| Skip detail constant | `ingest/reader.py` · `_AGGREGATE_SKIP_DETAIL` L507–512 | Names #267. Inverts / narrows with the gate. |
| Compiler `from_manifest` arm | `prune/compiler.py` · `_compile_custom_sql` L684–727 | Currently `return test.sql` verbatim. The verdict-shape restructure lands here (or at ingest). |
| Restructure template | `prune/compiler.py` · `_compile_row_count_between` L857–950 | `SELECT n FROM (SELECT COUNT(*) AS n …) AS rc WHERE <predicate>` — the shape to mirror. |
| Adapter envelope | `warehouse/adapters/bigquery.py` L942 (+ snowflake L967, databricks L1234) | `SELECT COUNT(*) AS failures FROM (<sql>) AS t`. Compiler must NOT pre-wrap. |
| Engine source-routing helper | `prune/engine.py` · `_test_requires_source_table` L510–599 | Already routes `from_manifest` → source under all strategies. No new arm needed. |
| Engine routing sites | `prune/engine.py` L1443–1448 (short-circuit), L1595–1599 (per-test) | Both already consult the helper. |
| Verdict matrix | `prune/engine.py` · `_decide_from_test_result` L670–749 | Type-agnostic on `failure_count`; correct once SQL shape is fixed. |
| DropReason lock | `prune/models.py` L55–70 | 5 values. Never a 6th. |
| SkipReason lock | `ingest/models.py` L36–53 | 3 values. Never a 4th. |
| Tests (ingest) | `tests/ingest/test_manifest_tests.py` L141 (`_is_skipped_not_wrong_kept`), `test_compiled_sql.py` L112 | The skip-expectation inverts under #267. |
| Tests (compiler) | `tests/prune/test_compiler.py` L1008+ (ingested custom_sql), L1751+ (row_count_between snapshots) | |
| Tests (engine) | `tests/prune/test_engine.py` L4244+ (`_test_requires_source_table` units), L5407+ (ingested routing), L2777+ (custom_sql engineered determinism) | Mixed-candidate pin is load-bearing. |
| Fixtures | `tests/fixtures/dbt_project_expectations/target/manifest.json` | No bare-scalar body today — new fixture or synthetic manifest needed. |

### Rule constraints → validation criteria (from `.claude/rules/`)

1. **`DropReason` 5-value LOCK** — non-evaluable → existing `kept-without-evidence`, never a 6th.
2. **Conservative-bias routing** — never silently drop; diagnostic in `why`. (`prune-engine.md`)
3. **Two-conditional engine routing in lockstep + load-bearing mixed-candidate test** — even though no new
   arm is added, the behavioural mixed-candidate pin (1× ingested-aggregate → source, 1× non-`from_manifest`
   → sampled temp) is mandatory. (`prune-engine.md` / #170 QG Pass 3)
4. **`_test_requires_source_table` centralisation; Direction 1 vs 2** — aggregate = Direction 2; already
   handled via the `from_manifest` arm; compose, don't fight it. (`prune-engine.md` / #171)
5. **`from_manifest` gating** — gate ingested-vs-drafted on `from_manifest`, never `type=="custom_sql"`;
   `exclude=True` keeps `candidate_hash` byte-identical; ingested tests stay READ-ONLY (never
   `proposed_test_files`). (`business-rule-tests.md` / #154)
6. **`SkipReason` 3-value LOCK** — residue reuses an existing reason; never a 4th. (`ingest-layer.md`)
7. **sqlglot-AST not regex; sqlglot stays confined** to `ingest/_compiled_sql` (+ draft/parser, prune
   compiler consuming the ingest helper). (`ingest-layer.md` / #154 DEC-006)
8. **No `dialect.name` branching; no SDK import under `prune/`** — `test_compiler_import_guard.py` green.
9. **Stage-0 ingest reader** — no logging, no warehouse/LLM calls, no SQL building.
10. **`_compile_custom_sql` ordering** — resolve → `validate_ingested_sql` (comment-tolerant, NOT #116
    `validate_test_sql`) → return; no pre-wrap `COUNT(*)`.
11. **No `_PRUNE_AUDIT_SCHEMA_VERSION` bump expected** — reuses `custom_sql`/`from_manifest`; no new
    `PruneEvent` field. Drift detectors for `PruneResult`/`PruneDecision`/`PruneEvent` stay green.
12. **No `_PROMPT_VERSION` rotation** — entirely prune-side; both cache-stability goldens stay green
    (their green-ness is itself a guard).
13. **Engineer determinism by rule semantics; pin engine routing not just compiler snapshot** — tautology
    → dropped, engineered-failing → kept, via `FakeBigQueryClient` (no marker); assert dispatched SQL is
    the restructured failing-rows form against the source, not `_SESSION._sf_sample_*`. (`testing-signal.md`)
14. **`workflow-project.md` does not exist** — conventions live entirely in `.claude/rules/`.

### Validation command

```bash
uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
```

Default suite is the primary gate (all #267 work is unit-testable via `FakeBigQueryClient`, no marker).
`test_compiler_import_guard.py` and both `_PROMPT_VERSION` goldens run in-default and must stay green.

### Relevant parent DECs

- **#154 DEC-004** — row-returning-only pass 1; aggregate skip is a **correctness gate**, not an
  optimization; aggregate support filed as **#267**.
- **#154 DEC-006** — sqlglot-AST foundation (regex unsafe).
- **#154 DEC-007** — `from_manifest` → full-scope-against-source; sample-scope rewrite deferred to **#268**.
- **#171 DEC-009** — `_test_requires_source_table` centralisation; both engine sites; mixed-candidate test
  load-bearing.
- **#171 DEC-010** — cross-variant bypass-tightening precedent (CHANGELOG `[Unreleased]` entry + per-variant
  mixed test + routing-table update).

---

## Phase 2 — Architecture Review

Two empirical risks were pressure-tested directly (not by opinion):

- **Restructure SQL validity** — the wrap `SELECT sf_agg_value FROM (SELECT (<body>) AS sf_agg_value) AS sf_agg WHERE sf_agg_value <> 0`, and the adapter's outer `SELECT COUNT(*) AS failures FROM (<that>) AS t`, **parse cleanly under sqlglot `bigquery` / `snowflake` / `databricks`** (the FROM-less scalar-subquery projection is accepted by all three). The body carries its own dialect quoting; the wrap adds only dialect-neutral literals.
- **Fixture association** — `associate_test_model` resolves a **singular test with a single `ref()`** via the `depends_on.nodes` one-model fallback (no `attached_node` / `test_metadata` needed). Vehicle is viable.

| Area | Rating | Finding |
|---|---|---|
| Security | pass | Restructure composes trusted literals around an already-`validate_ingested_sql`'d body; composed form re-validated. No new injection surface, no secrets/logging. |
| Performance | pass | Count against the **source** is one aggregate scan, cheap even on large tables, bounded by `maximum_bytes_billed`. A no-partition-filter bare `COUNT(*)` scans the whole table — inherent to a row-count test; documented, not a regression. |
| Data model | pass | **No** new `DropReason` (5-value lock), **no** new `SkipReason` (3-value lock), **no** `CandidateTestCustomSQL` field (`candidate_hash` byte-identical), **no** `_PRUNE_AUDIT_SCHEMA_VERSION` bump, **no** `PruneEvent` field. |
| API design | pass | One new pure helper `is_prunable_count_scalar(sql, *, dialect) -> bool` (mirrors `is_row_returning`/`is_deterministic_sql`); restructure is a private compiler helper. No public-surface change. |
| Observability | pass | Stage-0 ingest stays silent; no new engine/compiler logs (existing scope=sample INFO already covers `from_manifest`); diff `why` cascade surfaces the macro rationale unchanged. |
| Testing strategy | concern | The bulk of the work. Must pin: classifier matrix, compiler restructure branch + snapshot, engineered determinism (tautology→dropped / engineered-failing→kept), the **load-bearing mixed-candidate routing pin**, belt-and-braces kept-without-evidence, the new committed fixture + loads/ingest test, and the byte-identity / import-guard / `_PROMPT_VERSION` guards staying green. Addressed by US-001…US-005. |
| sqlglot confinement (proj) | pass | Classifier lives in `ingest/_compiled_sql` (existing importer); restructure is **pure-string** in the compiler (no sqlglot). No new importer → no new confinement scan. Guard: the compiler gains no `import sqlglot`. |
| Dialect-neutrality (proj) | pass | Restructure uses the body's own quoting + neutral literals; no `dialect.name` branch; validated on BQ/SF/DB. `test_compiler_import_guard.py` unaffected. |
| Semantic soundness (crux) | pass (documented) | `0 = pass` is sound under dbt's "returned rows = failures": a `COUNT(*) [WHERE cond]` scalar's value IS the failing-row count; the count-should-be-positive inversion is not expressible as a dbt singular test. Bare no-WHERE COUNT = "table should be empty." Documented in DEC-004 + ops docs. |

**Blockers:** none. **Concerns:** testing breadth (owned by the story set) and documenting the `0=pass` convention (DEC-004 + docs). No architecture-blocking issue — this is a bounded, well-understood follow-up.

## Phase 3 — Refinement Log

**Scoping answers (2026-07-05):** Q1 → *Count-family unwrap (sound)*; Q2 → *Keep skip-recording* non-count/ambiguous bodies; Q3 → *New committed manifest fixture*.

- **DEC-001 — Graduate only single top-level COUNT-family scalar bodies.** New classifier `is_prunable_count_scalar(sql, *, dialect="bigquery") -> bool` in `ingest/_compiled_sql`: root is `SELECT`, no `GROUP BY`, exactly one top-level projection that is a bare `exp.Count` (COUNT(*), COUNT(col), COUNT(DISTINCT) all included). Rejects: non-count aggregates (AVG/SUM/MIN/MAX), arithmetic-on-count (`COUNT(*)+1`), multi-projection, GROUP-BY, non-SELECT/unparseable (skip-when-uncertain → `False`). Rationale: only the count-of-rows idiom is soundly interpretable. *(Q1 = count-family unwrap.)*
- **DEC-002 — Restructure via a pure-string scalar-subquery wrap in the compiler.** `SELECT sf_agg_value FROM (SELECT (<body>) AS sf_agg_value) AS sf_agg WHERE sf_agg_value <> 0`. Lives in `prune/compiler.py::_compile_custom_sql` (SQL building is the compiler's job). **No sqlglot** (avoids a 3rd importer + new confinement scan). Adapter's `COUNT(*) AS failures` wrap → 0 rows (pass) / 1 row (fail). Mirrors `_compile_row_count_between`'s failing-rows contract. Validated across BQ/SF/DB.
- **DEC-003 — Classify at ingest (bool), restructure at compiler.** The stage-0 no-SQL-building rule forbids ingest from *building* the wrap; ingest only *classifies* and routes a graduatable count-scalar to a candidate (`from_manifest=True`, `sql=cc` verbatim). The compiler re-derives scalar-vs-row via `is_row_returning` and restructures scalars. **No new candidate field → `candidate_hash` byte-identical** (guard test retained).
- **DEC-004 — `0 = pass` is sound under dbt convention; document it.** Returned rows = failures; a COUNT scalar's value is the failing-row count. The "count should be > 0" inversion is not expressible as a dbt singular test, so we are faithful. Bare no-WHERE COUNT = "this table should be empty" (reject-table pattern). Documented in `docs/ingest-ops.md` + `docs/prune-ops.md`.
- **DEC-005 — Non-count / ambiguous scalars keep skip-recording; `SkipReason` stays 3-value.** Reuse `malformed-supported-test`; narrow `_AGGREGATE_SKIP_DETAIL` to name the non-count residue and state that count-of-rows scalars ARE now pruned (#267). *(Q2 = keep skip-recording.)*
- **DEC-006 — No new engine routing arm.** `_test_requires_source_table` already routes every `from_manifest` custom_sql → source under `materialised`/`oneshot` (and scope=full → source anyway). Aggregate count-scalars ARE `from_manifest` custom_sql, so routing is already correct; #267 adds only the behavioural **mixed-candidate pin**. Routing-table docstring unchanged.
- **DEC-007 — Belt-and-braces: a non-restructurable scalar reaching the compiler → `kept-without-evidence`.** The compiler re-checks `is_prunable_count_scalar`; a scalar that slipped the ingest gate and isn't count-restructurable returns `_InvalidIdentifier` (never the always-1 wrap). Conservative-bias routing; DropReason 5-value lock held.
- **DEC-008 — New committed fixture = a singular test with a single `ref()`.** A `tests/*.sql` body `SELECT count(*) FROM {{ ref('<model>') }} WHERE <cond>` compiles to a bare scalar count and associates via `depends_on.nodes`. Regenerated via the pinned `uvx` dbt-duckdb flow (non-deterministic fields stripped), committed. A loads/ingest test proves it becomes a candidate (not skip-recorded). *(Q3 = new committed fixture.)*
- **DEC-009 — No `_PRUNE_AUDIT_SCHEMA_VERSION` bump, no `_PROMPT_VERSION` rotation.** Prune-side only; drift detectors + both cache-stability goldens stay green (their green-ness is a guard).
- **DEC-010 — CHANGELOG + docs + rule-file narrative in lockstep.** Behavior change (count-of-rows dbt tests now pruned, not skip-recorded) gets a `CHANGELOG.md` `[Unreleased]` entry; `docs/ingest-ops.md` + `docs/prune-ops.md` narratives; and the `.claude/rules/` narratives (`business-rule-tests.md` § "THIRD source", `ingest-layer.md`, `prune-engine.md`) updated in the Patterns & Memory story (orchestrator-only writes for `.claude/`).
- **DEC-011 — `COUNT(DISTINCT)` is graduated** (still a count that is 0 iff no matching rows → `0=pass` holds). Minor; recorded so a reviewer doesn't read it as an oversight.

**Scope boundary:** #267 is source-routing + verdict-shape only. Sample-scope AST relation-rewrite for ingested tests stays **#268** — do not pull it in.

## Phase 4 — Detailed Breakdown

Ordering: ingest classifier → ingest gate → compiler restructure → engine pins → fixture → docs → Quality Gate → Patterns & Memory. Every story's AC includes the canonical validation command passing.

### US-001 — `is_prunable_count_scalar` classifier (ingest/_compiled_sql)
- **Traces to:** DEC-001, DEC-011.
- **Description:** Add the sqlglot-AST classifier that detects a graduatable count-of-rows scalar body. Pure function, stage-0 (returns bool, builds no SQL).
- **Files:** `src/signalforge/ingest/_compiled_sql.py` (new `is_prunable_count_scalar`; export via `__init__` if the sibling helpers are); `tests/ingest/test_compiled_sql.py`.
- **TDD:** graduate = `SELECT count(*) FROM t`, `... WHERE x<0`, `count(id) AS n`, `count(DISTINCT u)`; reject = `avg(x)`, `sum(x)`, `count(*)+1`, `count(*), max(x)` (multi), `SELECT * ... WHERE` (row-returning), `... GROUP BY k`; skip-when-uncertain = unparseable / `UNION` / non-SELECT → `False`.
- **Done when:** classifier + matrix tests pass; validation command green.
- **Depends on:** none.

### US-002 — Ingest gate routes count-scalars to candidates (ingest/reader.py)
- **Traces to:** DEC-003, DEC-005.
- **Description:** In `_classify_manifest_test`, a scalar body (`not is_row_returning`) that `is_prunable_count_scalar` falls through to the common determinism→safety→candidate tail (`from_manifest=True`, `sql=cc` verbatim); a non-count scalar still skip-records. Narrow `_AGGREGATE_SKIP_DETAIL` per DEC-005.
- **Files:** `src/signalforge/ingest/reader.py`; `tests/ingest/test_manifest_tests.py` (invert `test_aggregate_body_is_skipped_not_wrong_kept` into count→candidate / non-count→skip).
- **TDD:** count-scalar node → candidate (from_manifest=True, sql verbatim); AVG node → skip `malformed-supported-test` with narrowed detail; non-deterministic count-scalar → still skip (determinism gate in the common tail).
- **Done when:** gate + tests pass; validation green.
- **Depends on:** US-001.

### US-003 — Compiler restructures the scalar `from_manifest` arm (prune/compiler.py)
- **Traces to:** DEC-002, DEC-007.
- **Description:** In `_compile_custom_sql`'s `from_manifest` branch, after determinism + `validate_ingested_sql`: if `not is_row_returning(test.sql, dialect=dialect.name)`, and `is_prunable_count_scalar` holds, return the pure-string failing-rows wrap; else (non-restructurable scalar) return `_InvalidIdentifier` (kept-without-evidence). Row-returning bodies still return `test.sql` verbatim. Re-validate the composed SQL (`validate_ingested_sql`). **No `import sqlglot` added to the compiler.**
- **Files:** `src/signalforge/prune/compiler.py`; `tests/prune/test_compiler.py` (restructure snapshot + branch tests + belt-and-braces `_InvalidIdentifier`).
- **TDD:** scalar-count body → composed wrap (snapshot, asserts `<> 0` + scalar-subquery shape, no `_SESSION._sf_sample_*`); row-returning from_manifest body → verbatim (unchanged); a scalar that isn't count-restructurable → `_InvalidIdentifier`.
- **Done when:** compiler + tests pass; import-guard green; validation green.
- **Depends on:** US-001.

### US-004 — Engine engineered-determinism + load-bearing mixed-candidate pins (tests only)
- **Traces to:** DEC-006, DEC-002.
- **Description:** Behavioural pins via `FakeBigQueryClient`: a count-scalar restructured body with `failures=0` → dropped/`always-passes`; with `failures>0` → `kept`. The **mixed-candidate** test: 1× ingested count-scalar (routed to **source**, restructured) + 1× non-`from_manifest` candidate (routed to the sampled temp) on one model under `sample_strategy="oneshot"`/`materialised`; assert each dispatched SQL's table + the restructure shape.
- **Files:** `tests/prune/test_engine.py`.
- **TDD:** tautology→dropped; engineered-failing→kept; mixed-candidate per-test override (source vs `_SESSION._sf_sample_*`), asserting dispatched SQL not just the routed decision.
- **Done when:** engine pins pass; validation green.
- **Depends on:** US-002, US-003.

### US-005 — Committed dbt fixture with a scalar-count singular test + ingest loads test
- **Traces to:** DEC-008.
- **Description:** Add a singular test (`tests/<name>.sql`, single `ref()`, `SELECT count(*) … WHERE …`) to `tests/fixtures/dbt_project_expectations`; regenerate `target/manifest.json` via the pinned `uvx` dbt-duckdb flow (strip non-deterministic fields); document the regen command. Add a loads/ingest test proving the node ingests as a `from_manifest` candidate (not skip-recorded) and its associated model resolves.
- **Files:** `tests/fixtures/dbt_project_expectations/**` (test + regenerated manifest), regen script/doc, `tests/ingest/test_manifest_tests.py` (or a fixture-loads test).
- **Done when:** fixture committed; loads/ingest test proves candidate; validation green.
- **Depends on:** US-001, US-002, US-003.

### US-006 — Docs + CHANGELOG (worker-writable surfaces)
- **Traces to:** DEC-004, DEC-005, DEC-010.
- **Description:** `docs/ingest-ops.md` + `docs/prune-ops.md` — document count-of-rows graduation, the `0=pass` convention, the non-count skip residue, source-routing. `CHANGELOG.md` `[Unreleased]` — the behavior change (count-of-rows manifest tests now pruned, not skip-recorded).
- **Files:** `docs/ingest-ops.md`, `docs/prune-ops.md`, `CHANGELOG.md`.
- **Done when:** docs + CHANGELOG updated; `mkdocs build` (non-strict) clean; validation green.
- **Depends on:** US-002, US-003, US-004, US-005.

### US-007 — Quality Gate (code review ×4 + CodeRabbit)
- **Description:** Run the code reviewer 4 passes across the full changeset, fixing every real bug each pass; run CodeRabbit if available. Verify byte-identity `candidate_hash` guard, import-guard, both `_PROMPT_VERSION` goldens, and drift detectors all green. Validation must pass after all fixes.
- **Depends on:** US-001…US-006.

### US-008 — Patterns & Memory (priority 99)
- **Description:** Update `.claude/rules/` (orchestrator-only writes): `business-rule-tests.md` § "custom_sql THIRD source" (aggregate count-scalars graduated), `ingest-layer.md`, `prune-engine.md` (§ ingested manifest-compiled tests). Add a memory file capturing the count-scalar-restructure + `0=pass`-convention + pure-string-wrap-avoids-sqlglot-importer lessons; add the MEMORY.md pointer.
- **Depends on:** US-007.

## Phase 5+ — Beads Manifest

_(pending devolve)_
