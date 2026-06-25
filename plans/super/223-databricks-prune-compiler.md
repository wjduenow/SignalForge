# 223 — Databricks: prune compiler emits valid Databricks SQL from DATABRICKS_DIALECT

## Meta

- **Ticket:** https://github.com/wjduenow/SignalForge/issues/223
- **Epic:** #219 (Databricks adapter). Depends on #221 (skeleton). Models on #121 (Snowflake compiler dialect).
- **Branch / worktree:** `feat/223-databricks-prune-compiler` @ `/home/wesd/Projects/worktrees/SignalForge/223-databricks-prune-compiler`
- **Phase:** devolved
- **Sessions:** 1 (2026-06-24)
- **Scoping resolved:** Q1 → ungated parse-guard (DEC-002 stands); Q2 → mirror Snowflake 16+16 (DEC-003 stands).

---

## Discovery

### What / Why / Who

**What:** Make `signalforge.prune.compiler._compile_test` emit valid Databricks/Spark-SQL for all 8 test primitives **purely from `DATABRICKS_DIALECT`** (zero `dialect.name` branches), and prove it with byte-exact fixtures + a `sqlglot` `databricks`-dialect parse-guard.

**Why:** Third warehouse on the dialect-driven prune seam (after BigQuery + Snowflake). Architectural Commitment #3 ("warehouse-agnostic by design") — the compiler stays vendor-neutral; a new warehouse drops in via a sibling `Dialect` constant + fixtures, no compiler edits. Validity (not just shape) must be machine-checked per the #121 lesson: *snapshot equality certifies shape, not validity — keep a parser/executor in the loop.*

**Who:** Operators running SignalForge against Databricks (epic #219). Live execution is #226; this ticket is the offline compile+validate gate.

### Key codebase findings (de-risking prototype run during discovery)

The heavy lifting **already landed in #221**: `DATABRICKS_DIALECT` is fully defined (`src/signalforge/warehouse/models.py:308`) and the compiler is already 100% dialect-driven (every `_render_*` / `_compile_*` reads `Dialect` fields; the import-guard already forbids name-branching). A discovery prototype compiled **all 8 primitives × {full, sample} scope** (including the anomaly two-query split across mad/zscore/percentile + dow seasonality) with `DATABRICKS_DIALECT` and parsed every emitted statement through `sqlglot.parse_one(sql, dialect="databricks")`:

```
26/26 statements: OK   (0 FAILURES)
```

**Implications:**
- No `DATABRICKS_DIALECT` change is required — the #221 values are correct as-shipped. The AC's "verify/extend the Dialect fields" reduces to **verify** (+ pin with unit tests).
- The reserved-word CTE-alias risk (the Snowflake `"sample"` bug) **does not bite Databricks**: `SAMPLE` is not a Spark reserved word; unquoted `WITH sample AS …` parses cleanly under sqlglot's `databricks` dialect. `DATABRICKS_DIALECT` keeps the default `sample_cte_alias="sample"` (no override needed).
- The ticket is therefore **test-authoring + fixtures + one import-guard line + docs** — low-risk, well-bounded.

**Precedent to mirror (Snowflake #121/#171):**
- `tests/fixtures/prune/compiled_sql/snowflake/` — 16 top-level fixtures (4 built-ins + custom_sql×3 + row_count_between×2 + unique_combination×3, each + sample variants where applicable).
- `tests/fixtures/prune/compiled_sql/anomaly/snowflake/` — 16 anomaly fixtures (4 methods × 2 seasonality × {stats, violation}).
- `tests/prune/test_compiler.py` — **ungated** byte-exact snapshot tests (individual funcs for built-ins; parametrized `(method, seasonality)` for anomaly) + per-dialect unit tests (`test_quote_folds_and_quotes_for_snowflake`, `test_qualified_table_name_per_component_for_snowflake`, sample-CTE-uses-HASH, partition-filter cast-form) + belt-and-braces "BigQuery quoting must not leak" assertions.
- `tests/prune/test_compiler_fakesnow.py` — **gated** `@pytest.mark.snowflake` file co-locating fakesnow execution + the sqlglot parse-guard.
- `tests/prune/test_compiler_import_guard.py` — `_FORBIDDEN_PREFIXES = ("snowflake", "google.cloud")` (AST scan; planted-violation self-check asserts 9 hits).

### Facts that shape the plan

- `sqlglot>=30,<31` is a **base runtime dependency** (`[project].dependencies`), so `sqlglot.parse_one(..., dialect="databricks")` is importable in the **default** test suite — no marker needed for a pure-sqlglot parse-guard.
- The `databricks` pytest marker already exists and is already in the `addopts` deselection list (default runs exclude it). Its description already says "offline sqlglot/fake validation AND the gated live Free-Edition certification."
- Databricks has **no offline execution fake** (no fakesnow/DuckDB equivalent for Spark SQL). So sqlglot parse is the *only* automated validity check until the #226 live Free-Edition cert.
- Fixtures use the existing `fake_project.dataset.orders` 3-part TableRef. `fake_project` (12 chars) passes `validate_project_id` (6–30), so the Unity-Catalog short-catalog-name gotcha (`main`/`workspace` < 6 chars failing `TableRef.project`) is a **#224 concern, not #223**. Databricks renders the ref per-component-backtick: `` `fake_project`.`dataset`.`orders` ``.

---

## Architecture Review

| Area | Rating | Finding |
|---|---|---|
| Warehouse-agnostic seam | **pass** | Compiler already dialect-driven; prototype proves valid output for all 8 primitives. No `dialect.name` branch added. |
| Import-guard confinement | **pass** | Add `databricks` to `_FORBIDDEN_PREFIXES` + planted self-check (9→12 hits). No SDK import under `prune/` — there never was one (compiler reads `Dialect` only). |
| Validation tier (#121 lesson) | **concern → resolved** | Snapshot pins *shape*; sqlglot parse-guard pins *syntactic validity*; real-Spark *semantics* deferred to #226. Decision on parse-guard **gating** captured as DEC-002. |
| Reserved-word CTE alias | **pass** | Prototype confirms unquoted `sample` parses under `databricks` dialect; no `sample_cte_alias` override. Belt-and-braces assertion will pin it. |
| BigQuery/Snowflake regression | **pass** | This ticket only **adds** databricks fixtures + tests; compiler/models untouched, so existing fixtures are byte-unchanged by construction. A regression assertion makes it explicit. |
| Reproducibility caveat | **pass** | `xxhash64` is engine/release-stable, not cross-time (same caveat Snowflake's `HASH()` documented). Already noted in the `DATABRICKS_DIALECT` docstring. |
| Testing strategy | **pass** | Mirrors Snowflake exactly: ungated byte-exact snapshots + dialect unit tests + (gated?) sqlglot parse-guard + planted-violation self-check on the import-guard. |

No blockers. One concern (parse-guard gating) → DEC-002.

---

## Refinement Log

### DEC-001 — No `DATABRICKS_DIALECT` change; verify-and-pin only
The #221 dialect values are correct as-shipped (prototype: 26/26 statements parse). This ticket **verifies** (unit tests + fixtures + parse-guard) rather than edits the dialect. If any field were found wrong, the fix would land here — but none is. Rationale: avoid touching `models.py` keeps BigQuery/Snowflake byte-unchanged trivially true.

### DEC-002 — sqlglot parse-guard is **UNGATED** (deviates from issue's literal "gated under the `databricks` marker")
**Decision:** the `sqlglot.parse_one(sql, dialect="databricks")` parse-guard runs in the **default** suite (no marker), in a new `tests/prune/test_compiler_databricks.py`.
**Rationale:** (a) `sqlglot` is a base dependency — always importable in CI, zero added cost; (b) Databricks has **no** offline execution fake, so this parse-guard is the *sole* automated validity gate until #226's live cert — gating it behind a marker CI never runs would mean the validity gate only fires when a maintainer remembers `-m databricks`, exactly the gap the #121 lesson warns against; (c) testing-signal.md's "keep a parser/executor in the loop" is best served by running the parser on **every** CI run. The Snowflake parse-guard is gated only because it is *co-located* with fakesnow execution (which needs the gated dep) — there is no such coupling here.
**Deviation note:** the issue text says "gated under the `databricks` marker." This DEC consciously deviates; the byte-exact snapshot tests are also ungated, so shape + validity are both default-CI-checked. *(Pending user confirmation — see scoping Q1.)*

### DEC-003 — Fixture set mirrors the **Snowflake** set (16 top-level + 16 anomaly = 32), not the broader BigQuery set
Mirror the most-recent non-BQ dialect for review parity: 4 built-ins (+ sample variants) + custom_sql (full/sample/fullscan) + row_count_between (plain + where) + unique_combination (pair/three/where) = 16 top-level; anomaly = 4 methods × 2 seasonality × {stats, violation} = 16. Covers all 8 primitives + every anomaly shape. (BigQuery's extra `row_count_between_only_min/only_max` are omitted, matching Snowflake.)

### DEC-004 — Parse-guard lives in a **new** `tests/prune/test_compiler_databricks.py`
No fakesnow/execution to co-locate (Databricks has no in-memory fake), so a dedicated file (not an extension of `test_compiler_fakesnow.py`). Holds the parametrized parse-guard over all 32 fixtures + the import-guard is updated separately.

### DEC-005 — Belt-and-braces "no foreign-dialect leakage" assertions
Each Databricks snapshot test asserts the distinguishing markers: `xxhash64` present, `FARM_FINGERPRINT` absent (BigQuery), `HASH(*)` absent (Snowflake), `::` cast-literal absent (Snowflake). Backtick presence cannot distinguish BQ vs Databricks (both use `` ` ``), so it is not used as a discriminator.

---

## Detailed Breakdown

> Validation command (every story): `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`. Gated databricks tests: `uv run pytest -m databricks --no-cov`.

### US-001 — Import-guard: confine the `databricks` SDK out of `prune/`
- **Traces to:** DEC (warehouse-agnostic seam), AC "import-guard green".
- **Description:** Add `"databricks"` to `_FORBIDDEN_PREFIXES` in `tests/prune/test_compiler_import_guard.py`; extend the planted-violation self-check (`import databricks`, `from databricks import sql`, `import databricks.sql as dbx`, etc.) and bump the expected hit count.
- **Files:** `tests/prune/test_compiler_import_guard.py`.
- **Done when:** scan covers `databricks` + planted self-check asserts the new count; `test_no_warehouse_sdk_import_under_prune` green (no databricks import exists under `prune/`).
- **Depends on:** none.
- **TDD:** add planted-violation lines first, watch the count assertion fail, then bump.

### US-002 — Compiler dialect unit tests for Databricks
- **Traces to:** DEC-001, DEC-005, AC "verify the Dialect fields the compiler consumes".
- **Description:** Mirror the Snowflake per-dialect unit tests for `DATABRICKS_DIALECT`: `_quote` folds-lower + backtick-quotes (`` `customer_id` ``); `_qualified_table_name` per-component backtick (`` `fake_project`.`dataset`.`orders` ``, + two-part); sample-CTE uses `xxhash64(...) & 9223372036854775807` not `FARM_FINGERPRINT`; partition-filter renders `TIMESTAMP '…'` / `DATE '…'` typed-literal form; date-arithmetic (`DATE_TRUNC('DAY', …)` arg-order, `INTERVAL n unit` bare, `DAYOFWEEK(...)` Sunday=1). Each reads from `Dialect`, never hard-coded.
- **Files:** `tests/prune/test_compiler.py` (new `*_databricks_*` unit tests).
- **Done when:** unit tests pin every `Dialect` field the compiler consumes for Databricks; all green; full validation passes.
- **Depends on:** none.

### US-003 — Byte-exact Databricks fixtures + ungated snapshot tests
- **Traces to:** DEC-003, DEC-005, AC "fixtures committed; BigQuery + Snowflake fixtures byte-unchanged".
- **Description:** Generate the 32 fixtures (16 under `tests/fixtures/prune/compiled_sql/databricks/`, 16 under `…/anomaly/databricks/`) from real compiler output with `DATABRICKS_DIALECT`. Add **ungated** byte-exact snapshot tests in `test_compiler.py` mirroring the Snowflake set (individual funcs for built-ins/custom_sql/row_count_between/unique_combination; parametrized `(method, seasonality)` for the anomaly stats/violation pair). Include the DEC-005 leakage assertions. Add a regression assertion that BigQuery + Snowflake fixtures are byte-unchanged (or rely on their existing snapshot tests staying green — make the intent explicit in a comment/test).
- **Files:** `tests/fixtures/prune/compiled_sql/databricks/*.sql`, `tests/fixtures/prune/compiled_sql/anomaly/databricks/*.sql`, `tests/prune/test_compiler.py`.
- **Done when:** 32 fixtures committed; snapshot tests green; existing BQ/SF snapshot tests still green (byte-unchanged).
- **Depends on:** none (US-002 is independent but naturally lands first).

### US-004 — sqlglot `databricks`-dialect parse-guard (ungated per DEC-002) + marker doc
- **Traces to:** DEC-002, DEC-004, AC "the sqlglot Databricks-dialect parse-guard passes on every fixture".
- **Description:** New `tests/prune/test_compiler_databricks.py` with a parametrized parse-guard over **all 32** fixtures (`sqlglot.parse_one(fixture, dialect="databricks")` raises on invalid syntax). Ungated (DEC-002) — sqlglot is a base dep. Update the `DATABRICKS_DIALECT` docstring note ("certified offline by the #223 sqlglot databricks parse-guard") and `docs/warehouse-adapter-ops.md` / `docs/prune-ops.md` as needed to reflect the shipped offline gate.
- **Files:** `tests/prune/test_compiler_databricks.py`, `src/signalforge/warehouse/models.py` (docstring only), `docs/*-ops.md`.
- **Done when:** parse-guard green over all 32 fixtures in the default suite; docstring/ops reflect reality.
- **Depends on:** US-003 (needs the fixtures).

### US-005 — Quality Gate
- **Description:** Run code reviewer ×4 across the full changeset (fix all real bugs each pass); run CodeRabbit. Validation must pass after fixes, plus `uv run pytest -m databricks --no-cov`. Re-confirm the discovery prototype's 26/26 parse result is reflected in committed fixtures.
- **Depends on:** US-001..US-004.

### US-006 — Patterns & Memory (priority 99)
- **Description:** Update `.claude/rules/prune-engine.md` + `warehouse-adapters.md` with the Databricks-compiler-dialect section (3rd dialect instance; the "unquoted `sample` CTE is fine on Spark" finding; the ungated-parse-guard rationale if DEC-002 holds). Add a memory note (Databricks dialect compiler pattern). Update the `databricks` marker description if gating changed.
- **Depends on:** US-005.

---

## Scoping questions (resolved 2026-06-24)

1. **Parse-guard gating** (DEC-002): **ungated** default-suite parse-guard. ✅ Confirmed.
2. **Fixture-set scope** (DEC-003): **mirror Snowflake's 16+16**. ✅ Confirmed.

---

## Beads Manifest (devolved 2026-06-25)

- **Epic:** `bd_1-scaffolding-129`
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/223-databricks-prune-compiler` (`feat/223-databricks-prune-compiler`)
- **Tasks:**
  - `bd_1-scaffolding-129.1` — US-001 Import-guard (ready)
  - `bd_1-scaffolding-129.2` — US-002 Dialect unit tests (ready)
  - `bd_1-scaffolding-129.3` — US-003 Fixtures + snapshot tests (ready)
  - `bd_1-scaffolding-129.4` — US-004 sqlglot parse-guard + docs (blocked → .3)
  - `bd_1-scaffolding-129.5` — Quality Gate (blocked → .1 .2 .3 .4)
  - `bd_1-scaffolding-129.6` — Patterns & Memory (blocked → .5)

---

## Run complete (2026-06-25)

All 6 beads landed on `feat/223-databricks-prune-compiler` (sequential in-place; US-002/003/004 share `tests/prune/test_compiler.py`):
- US-001 `d89c935` — import-guard `databricks` prefix (planted count 9→14)
- US-002 `b0d7cea` — 7 Databricks dialect unit tests
- US-003 `1518a26` — 32 byte-exact fixtures + ungated snapshot tests
- US-004 `5b8b9a9` — ungated sqlglot `databricks` parse-guard (34 tests) + docstring/ops
- US-005 (Quality Gate) — 4 diverse review passes, **no real bugs**; two minor single-angle, self-mitigated test-signal observations (leakage assert is supplementary; snapshot equality is the real gate). CodeRabbit runs async on PR #256.
- US-006 `bf69b38` — `prune-engine.md` + `warehouse-adapters.md` patterns; `databricks` marker note; memory.

Final: `uv run pytest` → 4264 passed / 6 skipped; pyright 0 errors; ruff + format clean. No `DATABRICKS_DIALECT` field change needed (verified correct from #221). Live execution cert remains #226.
