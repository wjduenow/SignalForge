# Super Plan — #225: Databricks `estimate_query_bytes` (degrade-first, then EXPLAIN COST)

## Meta

- **Ticket:** [#225](https://github.com/wjduenow/SignalForge/issues/225) — `Databricks: estimate_query_bytes (degrade-first, then EXPLAIN COST)`
- **Epic:** #219 (Databricks adapter). Depends on #221 (skeleton). Models on Snowflake estimate #123 (degrade) + #130 (EXPLAIN).
- **Milestone:** v0.x (Databricks adapter epic #219)
- **Phase:** published
- **Branch:** `feature/225-databricks-estimate` (based on `dev`)
- **Sessions:** 1 (2026-06-25)

---

## Phase 1 — Discovery

### What / Why / Who

**What.** Make `signalforge generate --estimate` render a real warehouse cost projection for Databricks profiles. Two phases:
- **Phase 1 (degrade):** confirm + pin the `--estimate` flow degrades cleanly for Databricks today — the ABC default raises `EstimateNotSupportedError`, the engine captures it as a supplementary failure (#36 DEC-005), the CLI renders `<unavailable: EstimateNotSupportedError>` and exits 0. Pinned engine-level + CLI-level with a real `DatabricksAdapter()`.
- **Phase 2 (real estimate):** override `DatabricksAdapter.estimate_query_bytes(sql) -> int` using `EXPLAIN COST <sql>` → parse `Statistics(sizeInBytes=...)` from the optimized logical plan. Raise the existing `EstimateUnavailableError` when no parseable / usable figure exists (never fabricate `0`).

**Why.** #225 is the Databricks twin of Snowflake #123 (Phase 1 degrade) + #130 (Phase 2 EXPLAIN). Databricks has no BigQuery-style `dry_run` byte count; the closest primitive is Spark's `EXPLAIN COST`, which annotates each logical-plan node with cost-based-optimizer `Statistics(sizeInBytes=...)`.

**Who.** Operators running `--estimate` against a Databricks profile who today would see `<unavailable>` and want a cost preview before committing to a real prune scan.

### Dependency status — RESOLVED

The #224 sampling surface merged into `dev` (commit `7d1c9a1`, 2026-06-25). The `DatabricksAdapter` is fleshed out: `__init__(..., connection=None)` injectable seam, `_get_connection()` lazy accessor, `_execute` / `_execute_to_dicts` cursor helpers routing SDK errors through `map_databricks_exception`, plus `sample_rows` / `get_row_count` / `materialise_sample` / `run_test_sql` / `column_stats`. **`estimate_query_bytes` is one of two methods still inheriting the ABC degrade** (the other is `run_stats_query`, out of scope — #225 is the dedicated estimate ticket).

`estimate_query_bytes` is **stateless** — no session, no temp table (unlike `materialise_sample`). It needs a single `EXPLAIN COST` query through a cursor. Fully unit-testable today against `FakeDatabricksConnection.expect_execute(...)`.

`EstimateUnavailableError(WarehouseError)` **already exists** (shipped in #130, `warehouse/errors.py:495`, exported, registered tier-3 in `_EXCEPTION_TO_EXIT_CODE`, scan-7 compliant). #225 reuses it — **no new error class** (the major delta vs #130's US-001).

### Codebase findings (Scout)

| Surface | Location | Relevance |
|---|---|---|
| ABC default (degrade) | `warehouse/base.py` `estimate_query_bytes` → raises `EstimateNotSupportedError` | The method #225 overrides on `DatabricksAdapter`. |
| Databricks adapter | `warehouse/adapters/databricks.py:53` docstring: "`estimate_query_bytes` / `run_stats_query` inherit the ABC typed degrade … until #225 lands." | The override surface. |
| Cursor seam | `databricks.py:373` `_execute` / `:400` `_execute_to_dicts` | Run EXPLAIN + map exceptions. **No `_execute_scalar`** (Snowflake-only) — add a sibling. |
| Exception mapper | `warehouse/adapters/_databricks_client.py` `map_databricks_exception` | Same `raise mapped from exc` / passthrough convention as Snowflake/BQ. |
| Snowflake reference impl | `warehouse/adapters/snowflake.py:122` `_parse_explain_json_bytes`, `:911` `_execute_scalar`, `:953` `estimate_query_bytes` | The exact shape to mirror (validate → prefix → scalar exec → pure parse). |
| BQ reference impl | `warehouse/adapters/bigquery.py` `estimate_query_bytes` (dry_run) | The original int-bytes contract. |
| Estimate engine (consumer) | `cli/_estimate.py:564` `dry_run_bytes = adapter.estimate_query_bytes(...)` in try/except → `warehouse_unavailable_reason = f"{type(exc).__name__}: {str(exc)[:200]}"` → renderer `<unavailable: <ErrorClass>>` (`:762`) | The #36 DEC-005 supplementary-failure path. **No engine logic change needed** — overriding the adapter suffices for the happy path. |
| Primitive label | `cli/_estimate.py:138` `_PRIMITIVE_LABEL_BY_ADAPTER` (`BigQueryAdapter` → "BigQuery dryRun", `SnowflakeAdapter` → "Snowflake EXPLAIN") | Add `"DatabricksAdapter": "Databricks EXPLAIN COST"` so the renderer labels the source. |
| Typed error (reuse) | `warehouse/errors.py:495` `EstimateUnavailableError` | Already exists, exported, tier-3, scan-7 compliant. Reused as-is. |
| Test fake | `tests/warehouse/_fake_databricks.py:119` `expect_execute(matching, returns, description)` | Queue an EXPLAIN COST response. |
| Parse-guard precedent | `tests/warehouse/test_databricks_sql_parse.py` (#224) | sqlglot-parse pattern (not directly applicable — EXPLAIN COST is run, not compiled — but the fixture-pin posture mirrors). |

### Convention constraints (Convention Checker — `.claude/rules/`)

- **`warehouse-adapters.md`** — the governing rule:
  - *"ABC graceful-degrade methods graduate per-adapter via the warehouse's native primitive"* — the generalised recipe distilled from #130: **(1) parse in a pure module-level fn** (unit-testable without a connection) raising a typed "supported-but-unavailable-for-THIS-query" error (never fabricate `0`); **(2) pin shape with a hand-crafted fixture + a maintainer-gated live test** (snapshot/fixture equality certifies shape, not that the warehouse accepts the SQL — keep a parser/executor in the loop, the #121/#124/#171 lesson); **(3) flipping a degrade means rewriting that phase's degrade tests, not deleting them.** #225 is the *third* instance (BQ native, Snowflake #130, Databricks #225).
  - *one-shim-per-vendor* — `databricks-sql-connector` import stays confined to `_databricks_client.py`. The adapter calls `_get_connection()` / `map_databricks_exception`; it must NOT import the connector.
  - *errors carry remediation + `_format_value`* — reused `EstimateUnavailableError` already complies; the `detail=` strings must be operator-useful.
  - *`__repr__` redaction* — unchanged; estimate adds no credential surface.
  - *5-surface graduation parity* (degrade → active): rule file + ops doc + `CLAUDE.md` + tests + this plan's DECs, in lockstep.
  - *the #226 live-cert ledger* — #224 established that "shape-certified ≠ live-certified"; the EXPLAIN COST validity (real Databricks accepting `EXPLAIN COST` + the real plan-text shape matching the fixture) is a **named #226 live-cert item**, flagged in code/test docstrings, never claimed certified by #225.
- **`cli-layer.md`** — 7th AST scan requires every concrete `*Error` in `_EXCEPTION_TO_EXIT_CODE`. `EstimateUnavailableError` already registered (no churn). No new subcommand/flag (no skill-parity surface change).
- **`testing-signal.md`** — workers can't run live Databricks. Hand-craft (or maintainer-capture from Free Edition) a real `EXPLAIN COST` plan-text fixture; pin the pure parser against it; gate the live call behind a marker (deferred to #226 per issue text). Engineer determinism: assert the parsed int equals the fixture's `sizeInBytes`, never a live planner value.
- **`python-build.md`** — `databricks-sql-connector` ships under the `[databricks]` extra (+ dev group, already wired by #221). No build change.

### The crux — Spark `EXPLAIN COST` output is TEXT, not JSON (divergence from #130)

Snowflake's `EXPLAIN USING JSON` returns a machine-readable JSON cell (`GlobalStats.bytesAssigned`). Spark/Databricks `EXPLAIN COST` returns a multi-line **plan-text** string in a single cell (column `plan`), shaped roughly:

```
== Optimized Logical Plan ==
Aggregate [...], Statistics(sizeInBytes=8.0 B, rowCount=1)
+- Project [...], Statistics(sizeInBytes=12.3 MiB)
   +- Relation spark_catalog.default.foo[...] parquet, Statistics(sizeInBytes=12.3 MiB, rowCount=5.00E+5)

== Physical Plan ==
...
```

Three parse challenges, none present in #130:
1. **Human-readable binary units.** `sizeInBytes=12.3 MiB` → `12.3 × 1024²` bytes. Units span `B / KiB / MiB / GiB / TiB / PiB / EiB` (1024-based); value may be `int`, `float`, or scientific.
2. **The `8.0 EiB` no-stats sentinel.** When a node has no CBO statistics, Spark prints `spark.sql.defaultSizeInBytes` = `Long.MaxValue` ≈ `8.0 EiB` (`9223372036854775807` B). A parse that returns ~9.2e18 would report ~9 exabytes of "cost" — meaningless. This is the Databricks analogue of #130's "no parseable figure," but shaped as a **present-but-sentinel** value rather than an absent field → must route to `EstimateUnavailableError`.
3. **Which node's `sizeInBytes`?** The root (top of Optimized Logical Plan) reflects *output* size (tiny for `SELECT COUNT(*)`); the leaf `Relation` reflects *scan* size (the cost proxy ≈ `total_bytes_processed`). Picking a node is a design decision (see scoping Q).

### Synthesis / proposed scope

A single issue shipping **both phases** (mirroring #130): pin the Phase-1 degrade at engine + CLI, then graduate to a real `EXPLAIN COST` estimate with a pure `_parse_explain_cost_bytes` parser + hand-crafted/maintainer-captured fixture, deferring the **live** validity cert to #226. Estimated 6-7 stories + Quality Gate + Patterns & Memory.

---

### Scoping answers (2026-06-25)

- **Phase scope:** **Both phases in #225** — pin the engine+CLI graceful degrade AND override with a real `EXPLAIN COST` estimate. (Mirrors #130.)
- **Which `sizeInBytes`:** **Max across all plan nodes** — almost always the leaf table scan, the closest analogue to BigQuery `total_bytes_processed` / Snowflake `bytesAssigned`; robust to plan shape; the no-stats sentinel propagates so `max == sentinel` cleanly signals "unavailable."
- **No-stats sentinel:** **Raise `EstimateUnavailableError`** when the max is the Spark `Long.MaxValue` default (`~8.0 EiB`), with an operator-actionable `detail` (run `ANALYZE TABLE`). Never report a 9-exabyte cost.
- **Fixture source:** **Maintainer captures from Databricks Free Edition** (live access confirmed per project memory). A story provides the capture command; the pure parser is *also* covered by synthetic inline cases so the implementation chain is not blocked on the manual capture.

---

## Phase 2 — Architecture Review

| Area | Rating | Finding |
|---|---|---|
| Security | **pass** | `validate_test_sql(sql)` gates injection (rejects `;`, `--`, unbalanced parens) BEFORE the trusted-constant `EXPLAIN COST ` prefix is prepended (DEC-008). No new credential surface; `__repr__` redaction unchanged. `EXPLAIN COST` is planner-only — no scan, no mutation. |
| Performance | **pass** | `EXPLAIN COST` is a planner-only call — no partition scan, no DBU compute beyond planning. The estimate engine makes exactly one `estimate_query_bytes` call per `--estimate` (#36). |
| Data model | **pass** | No schema / migration. Reuses the existing `EstimateUnavailableError`; adds one (or two) captured fixtures. |
| API design | **pass** | Overrides an existing ABC method; same `int`-bytes contract the `--estimate` engine already consumes. No signature change. |
| Observability | **pass (note)** | No new routine adapter logging (adapter convention: sparing WARNING only on deviation). The degrade case is surfaced by the estimate engine's existing WARNING + `warehouse_unavailable_reason` (#36 DEC-005). |
| Testing | **concern → addressed** | (a) Workers can't run live Databricks → the pure parser is pinned by **synthetic inline plan-text cases** (US-001) so the impl chain isn't blocked; a **maintainer-captured real fixture** (US-005) certifies shape; **live validity is deferred to #226** (the gated `databricks` marker already exists). (b) The text parse is materially more fragile than #130's JSON parse (binary units, scientific notation, the sentinel, multi-node selection) → a thorough table-driven parser test is load-bearing. |

**No blockers.** One concern (testing), addressed by DEC-007/DEC-010 and the US-001 table-driven coverage.

---

## Phase 3 — Refinement (Decisions)

**DEC-001 — Both phases ship in #225.** *(user)* Pin the engine+CLI graceful degrade AND graduate to a real `EXPLAIN COST` estimate, mirroring how #130 did Snowflake in one issue. *Consequence:* because the override lands in the same PR, Databricks never separately ships an `EstimateNotSupportedError` degrade pin — see DEC-012.

**DEC-002 — `EXPLAIN COST <sql>`, parse `Statistics(sizeInBytes=N <unit>)` from the optimized logical plan text.** *(user / issue)* Databricks has no BigQuery `dry_run`; `EXPLAIN COST` is the CBO-stats primitive. The result is a single-cell multi-line plan string; each node carries `Statistics(sizeInBytes=<num> <binary-unit>[, rowCount=...])`.

**DEC-003 — Return the MAX `sizeInBytes` across all plan nodes.** *(user)* The maximum is almost always the leaf table scan — the "bytes scanned" cost proxy (analogue of `total_bytes_processed` / `bytesAssigned`). Robust to plan shape; root-output size understates cost, leaf-node matching is fragile. The sentinel propagates from a stats-less leaf up through ancestors, so `max == sentinel` ⟺ the scan has no stats ⟺ genuinely unavailable (DEC-004).

**DEC-004 — No-stats sentinel (`~8.0 EiB` / `Long.MaxValue`) → `EstimateUnavailableError`.** *(user)* Spark prints `spark.sql.defaultSizeInBytes` (`= Long.MaxValue`, formatted `8.0 EiB`) when a node lacks CBO statistics. Detect `max_bytes >= _SPARK_DEFAULT_SIZE_SENTINEL_BYTES` (`= 8 * 1024**6 = 9223372036854775808`) and raise `EstimateUnavailableError(detail="no table statistics; run ANALYZE TABLE … COMPUTE STATISTICS")`. Never report the ~9-exabyte figure (it conflates "huge scan" with "no stats" and misleads on cost).

**DEC-005 — No parseable `sizeInBytes` at all → `EstimateUnavailableError`.** When the plan text carries zero `Statistics(sizeInBytes=…)` matches (plan-shape change across Databricks runtime versions, metadata-only query), raise `EstimateUnavailableError(detail="EXPLAIN COST plan carried no sizeInBytes statistics")`. *Never fabricate `0`* (mirrors #130 DEC-002): a `return 0` would silently report `$0` cost on a future plan-shape change.

**DEC-006 — Reuse the existing `EstimateUnavailableError` — NO new error class.** *(scout)* #130 shipped `EstimateUnavailableError(WarehouseError)` (exported, registered tier-3 in `_EXCEPTION_TO_EXIT_CODE`, scan-7 compliant) with exactly the "supports estimation but couldn't extract the figure for THIS query" semantics #225 needs. No `errors.py` / `__all__` / exit-code / scan-7 churn.

**DEC-007 — Pure module-level `_parse_explain_cost_bytes(cell: str) -> int`.** Does ALL parsing — no connection, no warehouse call, no logging:
- Accepts the plan-text `str` (single EXPLAIN COST cell). If handed a non-`str` (e.g. the connector returned a list/None), raise `EstimateUnavailableError`.
- Regex-extracts every `Statistics(sizeInBytes=<num> <unit>)`; `<num>` accepts int / decimal / scientific; `<unit> ∈ {B, KiB, MiB, GiB, TiB, PiB, EiB}` (1024-based conversion table). Convert each to bytes (`int(value * 1024**power)`), take the **max** (DEC-003).
- No matches → `EstimateUnavailableError` (DEC-005). `max >= sentinel` → `EstimateUnavailableError` (DEC-004). Otherwise return the max as a non-negative `int`.
- Defensive: a parsed value that is negative or non-finite raises. (No `bool` path — the text parse yields floats, not Python bools.)
Pinned by **synthetic inline table-driven cases** (US-001) AND a **maintainer-captured fixture** (US-005). Engineer determinism: assert the parsed int equals the fixture's known `sizeInBytes`, never a live planner value.

**DEC-008 — Validate inner SQL FIRST, then prepend trusted `EXPLAIN COST `.** Call `validate_test_sql(sql)` (already imported in the adapter) on the caller SQL, THEN build `f"EXPLAIN COST {sql}"`. The injection boundary is the user SQL; the literal prefix is trusted constant text (mirrors #130 DEC-004).

**DEC-009 — Add a `_execute_scalar` sibling; route SDK errors through `map_databricks_exception`.** The estimate path has no `TableRef` in scope, so it needs a no-table cursor helper (mirrors Snowflake DEC-008). Add `DatabricksAdapter._execute_scalar(sql) -> Any`: open a cursor, `execute` + `fetchall` in a `try`, map any exception via `map_databricks_exception(exc, context={})` (`raise mapped from exc`; passthrough re-raises), close the cursor in `finally` (the per-method cursor-close convention #224 established). Return the first row's first cell (mapping rows → first value; tuple/list rows → `[0]`), or `None` for an empty result. *Single-row assumption* (Spark EXPLAIN returns one row holding the whole plan) is a documented #226 live-cert item.

**DEC-010 — Maintainer-captured fixture + capture command; live cert deferred to #226.** *(user)* A story (US-005) provides the `EXPLAIN COST` capture command (Free-Edition, PAT) + a regen note alongside the fixture, and pins the parser against the committed real-capture fixture (plus a no-stats variant). The worker ships a faithful placeholder so CI is green; the maintainer swaps in the real capture. **#225 ships NO gated live test** — per the issue text, validity is certified by the gated live test in **#226** (the `databricks` pytest marker + `SF_RUN_DATABRICKS` env gate already exist).

**DEC-011 — `_PRIMITIVE_LABEL_BY_ADAPTER["DatabricksAdapter"] = "Databricks EXPLAIN COST"`.** *(scout)* So the estimate renderer labels the cost source correctly (mirrors `"Snowflake EXPLAIN"`). The label is adapter-class-keyed and asserted in the engine test.

**DEC-012 — The engine+CLI graceful-degrade pin is keyed on `EstimateUnavailableError`, NOT `EstimateNotSupportedError`.** Because both phases ship together (DEC-001), the override removes the `EstimateNotSupportedError` path within the same PR, so pinning that path then rewriting it would be pure churn (the #130 rewrite only happened because #123 shipped the degrade in a *prior* PR). Instead: the **happy path** (real bytes) is pinned at adapter + engine + CLI; the **degrade path** (a no-stats table → `EstimateUnavailableError` → `<unavailable: EstimateUnavailableError>`, exit 0, no-traceback floor) is pinned at engine + CLI. This fully satisfies the issue's Phase-1 intent (graceful degrade pinned engine+CLI) and Phase-2 intent (real estimate) without intra-PR churn. This is exactly the post-#130 Snowflake end-state.

**DEC-013 — 5-surface graduation parity (degrade → active).** Update in lockstep: (1) `warehouse-adapters.md` (Databricks now overrides `estimate_query_bytes` via `EXPLAIN COST`; the graduate-degrade recipe becomes a 3-instance precedent; add the `EXPLAIN COST` shape + sentinel to the #226 live-cert ledger), (2) `docs/warehouse-adapter-ops.md` § query-bytes estimation (Databricks = `EXPLAIN COST` estimate + `EstimateUnavailableError` degrade + the **planner-estimate / `ANALYZE TABLE` freshness caveat**, mirroring the Snowflake `EXPLAIN` caveat), (3) `CLAUDE.md` if it states Databricks estimate status, (4) tests (US-001…US-005), (5) this plan's DECs.

---

## Phase 4 — Detailed Breakdown (stories)

> Validation command (every story's AC): `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

### US-001 — pure `_parse_explain_cost_bytes` parser + table-driven synthetic tests
- **Description:** Add the module-level pure function `_parse_explain_cost_bytes(cell) -> int` to `warehouse/adapters/databricks.py` (with a `_SPARK_DEFAULT_SIZE_SENTINEL_BYTES = 8 * 1024**6` constant and a 1024-based unit table). Extract every `Statistics(sizeInBytes=<num> <unit>)`, convert to bytes, return the max; raise `EstimateUnavailableError` on no-match (DEC-005) or sentinel (DEC-004). Reuses the existing `EstimateUnavailableError` import.
- **Traces to:** DEC-002, DEC-003, DEC-004, DEC-005, DEC-006, DEC-007.
- **TDD:** single B/KiB/MiB/GiB value → correct bytes; decimal (`12.3 MiB`) and scientific (`5.0E+2 KiB`) values; multi-node plan → returns the MAX node, not root/first; the `8.0 EiB` sentinel → `EstimateUnavailableError` (detail names `ANALYZE TABLE`); a plan with no `sizeInBytes` → `EstimateUnavailableError`; non-`str` cell (`None` / `list`) → `EstimateUnavailableError`; a real-shaped multi-line plan snippet parses to the expected leaf-scan bytes.
- **Files:** `src/signalforge/warehouse/adapters/databricks.py`, `tests/warehouse/test_databricks_estimate.py` (new).
- **AC:** parser fully covered by synthetic inline cases; no connection needed; validation passes.
- **Done when:** every malformed/sentinel/no-stats shape raises the typed error; the max-selection + unit conversion are pinned.
- **Depends on:** none.

### US-002 — `_execute_scalar` sibling + `DatabricksAdapter.estimate_query_bytes` override
- **Description:** Add `_execute_scalar(sql) -> Any` (DEC-009) and override `estimate_query_bytes(sql) -> int`: `validate_test_sql(sql)` → `f"EXPLAIN COST {sql}"` → `_execute_scalar(...)` → `_parse_explain_cost_bytes(cell)`. An empty result (`None`) → `EstimateUnavailableError`. Update the adapter module + class docstrings (remove the "`estimate_query_bytes` inherits the ABC degrade until #225 lands" note; add the EXPLAIN-COST + #226-live-cert note).
- **Traces to:** DEC-002, DEC-008, DEC-009, DEC-012.
- **TDD:** inject `FakeDatabricksConnection.expect_execute(matching=r"^EXPLAIN COST", returns=<fixture cell rows>, description=...)` → returns the expected int; SQL with `;` → `validate_test_sql` rejects BEFORE any cursor call; a connector exception → mapped `WarehouseError` re-raised `from`; the executed SQL starts with `EXPLAIN COST ` and embeds the validated SQL verbatim; `_execute_scalar` closes the cursor on success AND failure (the #224 cursor-leak regression shape); an empty `fetchall` → `EstimateUnavailableError`.
- **Files:** `src/signalforge/warehouse/adapters/databricks.py`, `tests/warehouse/test_databricks_estimate.py`.
- **AC:** the method no longer inherits the ABC degrade; fake-driven happy + injection-guard + mapped-error + cursor-close paths pinned.
- **Done when:** `EXPLAIN COST` happy path returns real bytes via the fake; the one-shim rule holds (no `databricks.sql` import in the adapter).
- **Depends on:** US-001.

### US-003 — estimate-engine label + engine-level happy/degrade pin
- **Description:** Add `"DatabricksAdapter": "Databricks EXPLAIN COST"` to `_PRIMITIVE_LABEL_BY_ADAPTER` (`cli/_estimate.py`, DEC-011). Pin the engine path with a `DatabricksAdapter(connection=FakeDatabricksConnection())`: a real-plan fixture → `report.estimated_*` carries real bytes + `warehouse_estimate_source == "Databricks EXPLAIN COST"`; a no-stats plan → `report.warehouse_unavailable_reason.startswith("EstimateUnavailableError:")` (DEC-012). Mirrors the Snowflake engine tests in `tests/cli/test_estimate_engine.py`.
- **Traces to:** DEC-011, DEC-012.
- **TDD:** happy → real `estimated_bytes` + adapter-derived source label; no-stats → `EstimateUnavailableError` supplementary degrade (engine returns a report, never raises); the source label is adapter-derived (not hardcoded).
- **Files:** `src/signalforge/cli/_estimate.py`, `tests/cli/test_estimate_engine.py`.
- **AC:** engine reports real bytes for Databricks; no-stats degrades cleanly; label correct.
- **Done when:** both engine paths pinned against an injected Databricks fake.
- **Depends on:** US-002.

### US-004 — CLI-level happy + graceful-degrade pin (`generate --estimate`)
- **Description:** Add a Databricks variant to `_install_estimate_patches` (it already "substitutes a different adapter") in `tests/cli/test_generate_estimate.py`. Pin: `generate --estimate` against a Databricks profile (fake-wired adapter) → stdout shows a real estimate (happy); a no-stats plan → `<unavailable: EstimateUnavailableError>`, **exit 0, no-traceback floor** (`"Traceback" not in err`). This is the issue's Phase-1 "graceful degrade pinned at CLI" AC, keyed on `EstimateUnavailableError` per DEC-012.
- **Traces to:** DEC-012.
- **TDD:** happy CLI run → exit 0, stdout carries the rendered Databricks estimate + `"Databricks EXPLAIN COST"`; no-stats CLI run → exit 0, stdout `<unavailable: EstimateUnavailableError>`, `"Traceback" not in err`.
- **Files:** `tests/cli/test_generate_estimate.py`.
- **AC:** CLI happy + degrade paths pinned; no-traceback floor held; exit 0 on degrade.
- **Done when:** the CLI surface mirrors the Snowflake `--estimate` pins for Databricks.
- **Depends on:** US-003.

### US-005 — maintainer-captured `EXPLAIN COST` fixture + regen command + real-fixture parser pin
- **Description:** Commit `tests/fixtures/warehouse/databricks/explain_cost_sample.txt` (a real Free-Edition `EXPLAIN COST` capture with populated stats) and `explain_cost_no_stats.txt` (the `8.0 EiB` sentinel shape), with a maintainer regen/capture command documented alongside (PAT + `databricks-sql` one-liner). Add a parser test asserting `_parse_explain_cost_bytes(<real fixture>)` equals the known captured byte count and the no-stats fixture raises `EstimateUnavailableError`. **⚠ Manual maintainer step:** the worker ships a faithful placeholder fixture so CI stays green + writes the capture command; the maintainer runs the capture and swaps in the real plan text. Live end-to-end validity is **#226**.
- **Traces to:** DEC-010, DEC-007.
- **TDD:** real fixture → known int (engineered determinism); no-stats fixture → `EstimateUnavailableError`; fixture-shape comment documents the captured runtime version.
- **Files:** `tests/fixtures/warehouse/databricks/explain_cost_sample.txt`, `tests/fixtures/warehouse/databricks/explain_cost_no_stats.txt`, `tests/warehouse/test_databricks_estimate.py`.
- **AC:** parser pinned against a real-shaped capture; regen command documented; #226 live-cert flagged.
- **Done when:** committed fixtures parse to their known values; the capture command is reproducible by the maintainer.
- **Depends on:** US-001.

### US-006 — 5-surface docs parity
- **Description:** Update `docs/warehouse-adapter-ops.md` § query-bytes estimation (Databricks = real `EXPLAIN COST` estimate + `EstimateUnavailableError` degrade + the planner-estimate / `ANALYZE TABLE`-freshness caveat), `.claude/rules/warehouse-adapters.md` (Databricks overrides `estimate_query_bytes`; the graduate-degrade recipe is now a 3-instance precedent; add the `EXPLAIN COST` shape + `8.0 EiB` sentinel to the #226 live-cert ledger), and `CLAUDE.md` if it states the Databricks estimate status.
- **Traces to:** DEC-013.
- **Files:** `docs/warehouse-adapter-ops.md`, `.claude/rules/warehouse-adapters.md`, `CLAUDE.md` (if applicable).
- **AC:** all surfaces name the new behaviour + the reused `EstimateUnavailableError` + the planner-estimate caveat consistently; no stale "inherits the degrade" claim for Databricks; docs build (`uv run --only-group docs mkdocs build`) clean.
- **Done when:** the graduation is documented across surfaces in lockstep.
- **Depends on:** US-004.

### US-007 — Quality Gate (code review ×4 + CodeRabbit)
- **Description:** Run the code reviewer 4 passes across the full changeset, fixing every real bug each pass; run CodeRabbit if available. Validation must pass after fixes. Re-run `ruff format --check .` against latest `dev` post-merge (drive-by-format drift guard).
- **Depends on:** US-001…US-006.

### US-008 — Patterns & Memory (priority 99)
- **Description:** Distill into `.claude/rules/warehouse-adapters.md` + memory: "the graceful-degrade ABC method now has THREE graduation instances (BQ native dry_run, Snowflake #130 EXPLAIN JSON, Databricks #225 EXPLAIN COST text). When the warehouse's estimate primitive returns *text* (not JSON), the pure parse fn must handle a present-but-sentinel 'no-stats' value (Spark `8.0 EiB` / `Long.MaxValue`) as a degrade, distinct from JSON's absent-field degrade — never report the sentinel. When both degrade-phase and override-phase ship in ONE PR, key the engine+CLI graceful-degrade pin on the *live* degrade error (`EstimateUnavailableError`), not the transient `EstimateNotSupportedError`."
- **Depends on:** US-007.

---

## Rules compliance gate

- one-shim-per-vendor: estimate calls `_get_connection()`/`map_databricks_exception`; **no `databricks.sql` import in the adapter** ✓ (US-002).
- errors carry remediation + `_format_value`: reused `EstimateUnavailableError` already compliant; `detail=` strings operator-useful ✓ (US-001/002).
- scan-7 exit-code registration: `EstimateUnavailableError` already registered (no churn) ✓.
- no new routine logging (degrade WARNING owned by the estimate engine) ✓.
- workers can't run live Databricks → synthetic parser cases + maintainer-captured fixture + live cert deferred to #226 ✓ (US-001/005).
- 5-surface parity for the degrade→active graduation ✓ (US-006).
- never fabricate `0` / never report the sentinel ✓ (DEC-004/005).

---

## Beads Manifest

_(filled on devolve)_
