# Super Plan — #183: Add `_ROW_COUNT_BETWEEN_SCOPE_INSTRUCTION`

## Meta

- **Ticket:** [#183](https://github.com/wjduenow/SignalForge/issues/183) — `#169 follow-on: Add _ROW_COUNT_BETWEEN_SCOPE_INSTRUCTION (DEC-012 worked example never landed)`
- **Branch:** `feature/183-row-count-between-scope`
- **Worktree:** `/Users/wesduenow/Projects/worktrees/SignalForge/183-row-count-between-scope`
- **Base branch:** `dev` (0.6.0.dev0 line) — **NOT `main`**. See DEC-001.
- **PR target:** `dev`
- **Phase:** devolved
- **Sessions:** 1 (2026-06-02)
- **Closest precedents:** #171 (`_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION`), #170 (`_UNIQUE_COMBINATION_SCOPE_INSTRUCTION`)
- **Parent epic:** #179 (test-primitive-expansion retest)

---

## Phase 1 — Discovery

### What

The `row_count_between` test primitive (shipped end-to-end by #169/#176) has a JSON-shape catalogue entry in `src/signalforge/draft/prompts.py::_TEST_CATALOGUE_LINES` but **no narrative SCOPE-instruction block** telling the LLM *when* to propose it. Its two siblings both have one:

| Variant | Catalogue line | SCOPE-instruction block |
|---|---|---|
| `row_count_between` (#169) | ✅ | ❌ **None — this ticket** |
| `unique_combination` (#170) | ✅ | ✅ `_UNIQUE_COMBINATION_SCOPE_INSTRUCTION` |
| `row_count_anomaly_by_period` (#171) | ✅ | ✅ `_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION` |

DEC-012 of `plans/super/169-row-count-between.md` specified the worked example that was supposed to ship but never landed.

### Why

Empirical retest (#179, `docs/research/179-test-primitive-expansion-retest.md`, PR #182):
- **Phase A:** `weekly_query_cost.sql` — the literal worked-example fixture in DEC-012, the exact bounded-aggregation shape — still drafted **0** `row_count_between` tests post-#169 ship.
- **Phase B (14 evaluable models):** 57.1% match-rate vs. the epic's projected 76% — an 18.9 pp miss, well outside the ±5 pp window. The missing scope instruction is the proximate cause for ~3 of the 6 misses (the three that don't also declare `unique_combination`).

### Who

SignalForge maintainers; downstream: any operator running `signalforge generate` against models with bounded-aggregation grain.

### Acceptance criteria (from ticket)

1. Add `_ROW_COUNT_BETWEEN_SCOPE_INSTRUCTION` to `src/signalforge/draft/prompts.py` mirroring the two sibling blocks; ship the DEC-012 worked example.
2. Plumb it through `_render_system_prompt` the same way (`row_count_between_allowed` → `row_count_between_scope`).
3. Rotate `_PROMPT_VERSION` per `business-rule-tests.md` § "Lockstep `_PROMPT_VERSION` rotation"; update `tests/llm/test_prompt_cache_stability.py::_EXPECTED_PROMPT_VERSION` + the rotation-history docstring in the same commit.
4. (Maintainer, post-merge) re-run `signalforge generate models/reporting/weekly_query_cost.sql ...` against the `intuit_airflow` substrate and assert the drafter now proposes `row_count_between`.

### Codebase Scout findings (verified against `dev`, not the stale local tree)

**Already complete (no work needed):** the `row_count_between` primitive is fully wired —
- `src/signalforge/draft/models.py`: `CandidateTestRowCountBetween` (`type`/`column: None`/`minimum`/`maximum`/`where`/`rationale`) in the `CandidateTest` discriminated union.
- `src/signalforge/draft/config.py`: `"row_count_between"` ∈ `VALID_TEST_TYPES` (the 8 drafter variants); `exclude_tests` validator already accepts it.
- `src/signalforge/draft/prompts.py:75-81`: `_TEST_CATALOGUE_LINES["row_count_between"]` JSON-shape entry (two forms: bare + `where`).
- Parser anchor-contract, prune compiler, artifact_id, diff emitter all already dispatch on it.

**The single gap:** no `_ROW_COUNT_BETWEEN_SCOPE_INSTRUCTION` constant; not referenced in `_SYSTEM_PROMPT_TEMPLATE` or `_render_system_prompt`.

**Exact plumbing precedent** (`src/signalforge/draft/prompts.py`):
- `_SYSTEM_PROMPT_TEMPLATE` line 301: `{custom_sql_scope}{unique_combination_scope}{row_count_anomaly_scope}` — append `{row_count_between_scope}` here (placement: before `{unique_combination_scope}` to mirror catalogue/primitive order — see DEC-004).
- `_render_system_prompt` lines 355-363: each `<x>_allowed = "<type>" in allowed` then `<x>_scope = _<X>_SCOPE_INSTRUCTION if <x>_allowed else ""`; passed to `.format(...)` at 364-369.
- `_PROMPT_VERSION` (lines 419-429): `blake2b(_SYSTEM_PROMPT + _MANIFEST_SUMMARY_TEMPLATE + _DATA_SECTION_TEMPLATES_json, digest_size=8)`. `_SYSTEM_PROMPT = _render_system_prompt(())` (line 376) — so referencing the new block in the no-exclusions render **auto-rotates** the hash at import time. No manual hash plumbing.
- Sibling constants: `_UNIQUE_COMBINATION_SCOPE_INSTRUCTION` (175-197), `_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION` (199-230).

**Test precedent** (`tests/draft/test_prompts.py`): #171 shipped 4 catalogue tests + 3 scope-instruction prose tests + 2 exclude-gating tests. `row_count_between` currently has the 4-ish catalogue/exclude tests (317-389) **but no scope-instruction prose tests** — the gap #183 fills. The existing `test_render_system_prompt_excludes_row_count_between_when_in_exclude_tests` only checks the catalogue type + SCOPE phrase removal; it must be extended to assert the new scope-instruction prose also disappears when excluded.

**Cache-stability pin** (`tests/llm/test_prompt_cache_stability.py`, `@pytest.mark.llm`, in default run): `_EXPECTED_PROMPT_VERSION = "c11a73cc95b31614"` (will rotate); `_CACHED_BLOCK_GOLDEN` is the **manifest summary**, NOT the system prompt — it stays untouched (DEC-012 of #169 confirms). Only `_EXPECTED_PROMPT_VERSION` + the docstring rotation-history block move. Failure message documents the lockstep update workflow.

### Convention rules in scope

- `.claude/rules/business-rule-tests.md` § "Lockstep `_PROMPT_VERSION` rotation (#169 DEC-012)": drafter-side `_PROMPT_VERSION` rotates when the cached system prompt changes; pin + snapshot move in same commit. Grade-side `_PROMPT_VERSION` is independent and **does not** move here (no rubric change). Memory `ralph-serialize-shared-registry-beads` — N/A (single bead, no parallel golden contention).
- `.claude/rules/llm-drafter.md` § "Cached-block scope (DEC-009)": the cached-block golden pins `_render_manifest_summary` bytes — unaffected by a system-prompt edit; do not touch it.
- `.claude/rules/testing-signal.md`: scope-instruction prose tests must be capable of failing (assert specific heuristic strings, not `assert True`).
- ANSI-safe lazy-format logger gate (6 dirs incl. `draft/`): N/A — no new logging.

### Reusable design moves

- Copy the `_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION` constant + plumbing shape verbatim; swap content.
- Copy #171's `tests/draft/test_prompts.py` scope-instruction test block; retarget to row_count_between heuristics.

### Open questions → resolved in refinement (see DECs)

1. Base/target branch (`dev`).
2. Richness of the scope-instruction prose (one-liner vs. full sibling-style block).
3. Whether to backfill #170's missing unique_combination prose tests (parity) or stay tightly scoped.
4. How to handle the empirical `intuit_airflow` re-validation (Ralph-runnable vs. maintainer-only post-merge step).

---

## Phase 2 — Architecture Review

Compressed — this is a static-prompt-text addition with no runtime/data/network surface.

| Area | Rating | Finding |
|---|---|---|
| Security | pass | Scope-instruction text is author-controlled static content baked into the system prompt, not operator/manifest input — the `<MODEL_SQL>` / business-rule envelope-breach guards do not apply to it. No injection surface. |
| Performance | pass | Adds ~12 lines to a cached system prompt; well under the 8000-token cap (DEC-024). Negligible token delta; the block is part of the auto-cached static prefix. |
| Data model | n/a | No schema, no migration, no audit-event change. |
| API design | pass | No public-surface change. `_ROW_COUNT_BETWEEN_SCOPE_INSTRUCTION` is `_`-private; `_PROMPT_VERSION` value rotates but is not a stable contract (it's a content hash by design). |
| Observability | n/a | No new logging. |
| Testing strategy | **concern → DEC-005** | CI can pin prose presence + exclude-gating + version rotation. The *empirical* coverage-uplift claim (57.1%→76%) is NOT CI-testable (needs Anthropic spend + external Snowflake substrate). Must be a documented maintainer post-merge step, not a Ralph story. |
| Prompt-cache correctness | **concern → DEC-003** | `_PROMPT_VERSION` + cache-stability pin MUST move in lockstep or the test fails loud. This is the one real failure mode; the lockstep rule covers it. |

No blockers.

---

## Phase 3 — Refinement Log

### DEC-001 — Base and PR target branch is `dev`, not `main`
**Decision:** Branch from and PR into `origin/dev`.
**Rationale:** `main` is at 0.5.0 and lacks #169/#170/#171/#179 entirely; every dependency of this ticket (the `row_count_between` primitive, the two sibling scope blocks, `business-rule-tests.md`, the #179 research doc) lives only on `dev` (0.6.0.dev0). PR #182 (referenced by the ticket) is on `dev` only. The repo's stated convention is "design happens in the open on `dev`." The initial local checkout (`feature/updated_docs`) was a stale v0.1 tree and is abandoned for this work.

### DEC-002 — Scope is prompt-side only; the primitive is already complete
**Decision:** Touch only `src/signalforge/draft/prompts.py` (constant + plumbing) and its tests + the cache pin. Do not touch `models.py`, `config.py`, `parser.py`, prune/diff/grade.
**Rationale:** Scout confirmed `CandidateTestRowCountBetween`, `VALID_TEST_TYPES` membership, the catalogue line, and all six dispatch sites already exist (#169/#176). The only missing artifact is the narrative scope block.

### DEC-003 — `_PROMPT_VERSION` rotates automatically; update the pin + docstring in lockstep
**Decision:** Let the hash rotate via the rendered `_SYSTEM_PROMPT` change; capture the new value into `_EXPECTED_PROMPT_VERSION` and add a rotation-history docstring entry for #183, same commit. Leave `_CACHED_BLOCK_GOLDEN` untouched.
**Rationale:** `business-rule-tests.md` lockstep rule + #169 DEC-012. The cached-block golden is the manifest summary, which a system-prompt edit does not change. Grade-side `_PROMPT_VERSION` is independent and unchanged (no rubric edit).

### DEC-004 — (proposed) Placeholder placement mirrors primitive order
**Decision:** Insert `{row_count_between_scope}` as `{custom_sql_scope}{row_count_between_scope}{unique_combination_scope}{row_count_anomaly_scope}` (6th primitive before 7th/8th), matching catalogue order.
**Rationale:** Keeps the rendered prompt's scope-block order aligned with `_TEST_CATALOGUE_LINES` order for reviewer legibility. (Any placement rotates the hash identically; this is a legibility choice.)

### DEC-005 — (proposed) Empirical re-validation is a documented maintainer step, not a Ralph story
**Decision:** The `intuit_airflow` re-run (AC #4) is captured in the plan + PR description as a maintainer post-merge validation, gated like the existing live-run docs; it is NOT a beads task.
**Rationale:** It needs an Anthropic API key (~$3-7), an external Snowflake-shaped dbt project, and ~95 min wall-clock — none available to a Ralph worker. CI-testable surface (prose presence, exclude-gating, version rotation) fully covers the mergeable contract.

### DEC-005 (accepted) — Empirical re-validation is a documented maintainer step, not a Ralph story
Confirmed. The `intuit_airflow` re-run lands in the plan + PR description as a maintainer post-merge validation, gated like existing live-run docs.

### DEC-006 — Full sibling-style scope-instruction block
**Decision:** Author ~10-15 lines matching #170/#171 depth — the DEC-012 worked example verbatim plus calibration guidance. **Proposed content** (final wording tuned during implementation; embeds the DEC-012 sentence):

```
`row_count_between` tests assert that the model's total row count (or the
count of rows matching an optional `where` predicate) falls within a
[`minimum`, `maximum`] band. When the SQL shows a bounded aggregation —
a `GROUP BY` over a date-window `WHERE` clause, or any rollup whose
cardinality is predictable from the grain (one row per day, per region,
per active account) — propose `row_count_between` with a calibrated
`minimum` >= 1 to catch upstream pipeline gaps (an empty load, a broken
join that drops every row). Set `maximum` only when an upper bound is
genuinely known (a fixed dimension cardinality, a capped lookback
window); leave it null when the table grows unboundedly over time. Use
the `where` form to bound a meaningful subset (e.g. rows for the current
period). Do NOT propose a bound you cannot justify from the SQL — a
vacuous `minimum: 0` with no `maximum` adds no signal.
```
**Rationale:** Best shot at the 57.1%→76% coverage uplift the retest measures; thin one-liner under-delivers vs. siblings.

### DEC-007 — Backfill #170's missing unique_combination prose tests
**Decision:** Add the absent `_UNIQUE_COMBINATION_SCOPE_INSTRUCTION` prose tests to `tests/draft/test_prompts.py` in the same PR (test-only; no source change for #170).
**Rationale:** #170 shipped its scope block without dedicated prose tests — a parity gap that would otherwise silently let the block drift. Closing it now is cheap and prevents the gap recurring. Isolated to a separate story so the core #183 change stays independently reviewable.

---

## Phase 4 — Detailed Breakdown

**Story order:** prompts source + lockstep pin (US-001) → test-only parity backfill (US-002) → Quality Gate (US-003) → Patterns & Memory (US-004).
**Validation command (every story):** `cd <worktree> && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

### US-001 — Add `_ROW_COUNT_BETWEEN_SCOPE_INSTRUCTION` + plumbing + lockstep `_PROMPT_VERSION` rotation
**Description:** Define the scope-instruction constant in `src/signalforge/draft/prompts.py`, plumb it through `_SYSTEM_PROMPT_TEMPLATE` + `_render_system_prompt`, and update the cache-stability pin in lockstep. The single substantive change for #183.
**Traces to:** DEC-002, DEC-003, DEC-004, DEC-006.
**Files:**
- `src/signalforge/draft/prompts.py` — add `_ROW_COUNT_BETWEEN_SCOPE_INSTRUCTION` (after the sibling constants, ~line 198); add `{row_count_between_scope}` to `_SYSTEM_PROMPT_TEMPLATE` line 301 in primitive order (`{custom_sql_scope}{row_count_between_scope}{unique_combination_scope}{row_count_anomaly_scope}`); add `row_count_between_allowed`/`row_count_between_scope` (~lines 355-363) and the `.format(...)` kwarg (~364-369).
- `tests/llm/test_prompt_cache_stability.py` — update `_EXPECTED_PROMPT_VERSION` to the rotated value; add a #183 entry to the rotation-history docstring. **Do NOT touch `_CACHED_BLOCK_GOLDEN`** (manifest summary; unaffected).
- `tests/draft/test_prompts.py` — add row_count_between scope-instruction prose tests; extend `test_render_system_prompt_excludes_row_count_between_when_in_exclude_tests` to assert the prose block also disappears when excluded.
**TDD:**
- `test_system_prompt_scope_teaches_bounded_aggregation_heuristic` — assert `"GROUP BY"`, `"bounded aggregation"`, `"minimum"` (≥1 pipeline-gap framing) appear in `_SYSTEM_PROMPT`.
- `test_system_prompt_scope_documents_maximum_and_where_calibration` — assert `maximum`/null-when-unbounded + `where`-form + "vacuous" anti-pattern prose present.
- `test_render_system_prompt_includes_row_count_between_scope_when_not_excluded` — prose present in default render.
- `test_render_system_prompt_excludes_row_count_between_scope_when_in_exclude_tests` — type, SCOPE phrase, AND prose all absent when `exclude_tests=("row_count_between",)`.
- `test_render_system_prompt_keeps_row_count_between_scope_when_other_types_excluded` — survives when only other types excluded.
**Done When:**
- [ ] `_ROW_COUNT_BETWEEN_SCOPE_INSTRUCTION` defined and rendered into the system prompt when `row_count_between` is allowed.
- [ ] Excluded via `exclude_tests` → block omitted (parser-side already enforced; prompt-side now too).
- [ ] `_EXPECTED_PROMPT_VERSION` updated to the rotated hash; rotation-history docstring gains a #183 entry; `_CACHED_BLOCK_GOLDEN` unchanged.
- [ ] New + extended tests in `tests/draft/test_prompts.py` pass; `test_prompt_cache_stability.py` passes.
- [ ] Validation command passes.

### US-002 — Backfill missing `_UNIQUE_COMBINATION_SCOPE_INSTRUCTION` prose tests (#170 parity)
**Description:** Test-only. Add the dedicated scope-instruction prose tests #170 omitted, mirroring the row_count_between/row_count_anomaly test shape.
**Traces to:** DEC-007.
**Depends on:** US-001 (establishes the prose-test pattern; avoids same-file churn collision).
**Files:** `tests/draft/test_prompts.py` only.
**TDD:**
- `test_system_prompt_scope_teaches_composite_key_grain_heuristic` — assert composite-key prose (`columns` ≥2, worked examples like `(order_id, line_item_id)`).
- `test_system_prompt_scope_warns_against_vacuous_pk_tuples` — assert the "do NOT propose over a primary key combined with any other column" anti-pattern prose.
- `test_render_system_prompt_excludes_unique_combination_scope_when_in_exclude_tests` — prose absent when excluded (extend if a partial test exists).
**Done When:**
- [ ] ≥2 new unique_combination scope-instruction prose tests, each capable of failing if the block drifts.
- [ ] Exclude-gating coverage for the unique_combination scope block.
- [ ] Validation command passes.

### US-003 — Quality Gate
**Description:** Run the code reviewer 4× across the full changeset, fixing all real bugs each pass; run CodeRabbit if available; confirm validation passes after all fixes.
**Depends on:** US-001, US-002.
**Done When:**
- [ ] 4 reviewer passes complete; all real findings fixed.
- [ ] CodeRabbit clean (or findings triaged).
- [ ] Validation command passes.
- [ ] (Maintainer note in PR) post-merge: re-run `signalforge generate models/reporting/weekly_query_cost.sql --project-dir . --profiles-dir <...> --format markdown` against the `intuit_airflow` substrate (#179 § "Reproducing this retest"); assert `row_count_between` now proposed. ~$3-7, ~not CI-gated.

### US-004 — Patterns & Memory
**Description:** Capture learnings: the stale-base-branch trap (always verify deps live on the intended base before scouting); note that scope-instruction blocks need paired prose tests (the #170 gap this PR closed). Update `.claude/rules/business-rule-tests.md` if a "every scope-instruction block ships paired prose tests" line is warranted.
**Depends on:** US-003.
**Done When:**
- [ ] Rule/doc/memory updated with the paired-prose-test convention.
- [ ] Plan `Phase` set to devolved; Beads Manifest filled.
- [ ] Validation command passes.

### Rules-compliance gate
- `business-rule-tests.md` lockstep rotation — satisfied by US-001's same-commit pin + docstring update.
- `testing-signal.md` no-`assert True` — all new tests assert specific heuristic substrings.
- `llm-drafter.md` cached-block contract — `_CACHED_BLOCK_GOLDEN` deliberately untouched.

---

## Phase 7 — Beads Manifest

- **Epic:** `SignalForge-jvu` — #183: Add _ROW_COUNT_BETWEEN_SCOPE_INSTRUCTION
- **Tasks:**
  - `SignalForge-jvu.1` — US-001: scope-instruction block + plumbing + lockstep `_PROMPT_VERSION` (ready)
  - `SignalForge-jvu.2` — US-002: backfill #170 unique_combination prose tests (blocked by .1)
  - `SignalForge-jvu.3` — Quality Gate (blocked by .1, .2)
  - `SignalForge-jvu.4` — Patterns & Memory (blocked by .3)
- **Dependency chain:** .1 → .2 → .3 → .4 (Quality Gate also directly depends on .1).
- **Ralph entry point:** `SignalForge-jvu.1` (only unblocked task).
- **Worktree:** `/Users/wesduenow/Projects/worktrees/SignalForge/183-row-count-between-scope` (branch `feature/183-row-count-between-scope`, base `dev`).
- **PR:** https://github.com/wjduenow/SignalForge/pull/191 (draft, base `dev`).
- **Note:** `bd` (0.63.3) is dolt-backed; auto-push to the dolt remote warns ("no common ancestor") but the local task graph is intact. No `bd sync` subcommand in this version.
