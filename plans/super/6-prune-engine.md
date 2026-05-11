# Issue #6 — Test prune engine: drop always-pass and known-clean-fail tests

## Meta

- **Ticket:** [#6](https://github.com/wjduenow/SignalForge/issues/6)
- **Branch:** `feature/6-prune-engine` (off `dev`)
- **Worktree:** `/home/wesd/dev/worktrees/SignalForge/feature/6-prune-engine` (created via `git worktree add`)
- **Phase:** devolved (epic + 16 tasks live in beads 2026-04-30; PR [#20](https://github.com/wjduenow/SignalForge/pull/20) draft)
- **Sessions:** 1 (started 2026-04-30)
- **Plan author:** Claude Code (Opus 4.7, 1M context)
- **Milestone:** v0.1 (the **load-bearing** ticket — Architectural Commitment #1; gates #7 grader and #8 diff renderer)
- **Labels:** `evaluation`

## Discovery

### Ticket summary

Run every candidate test against warehouse data and drop the ones with no signal. From the issue body:

1. `signalforge.prune.prune_tests(candidates, adapter) -> PruneResult`
2. Each candidate test compiled to SQL via the adapter's dialect helper
3. Test "passes always" = zero failing rows on the sampled set OR zero failing rows on the full table (configurable)
4. Drop reasons recorded: `always-passes`, `requires-future-data`, `failed-on-known-clean-data`, `kept`
5. Conservative defaults: keep tests we cannot evaluate (don't silently drop)
6. Per-test runtime budget; tests over budget marked `kept-without-evidence`
7. Unit tests with synthetic data fixtures; integration test on a public BQ dataset

The ticket explicitly notes: "This is the load-bearing premise of the project. If pruning hits >80% of candidates the story works; <20% means we're regenerating nothing useful and the design needs rework."

This is the first ticket to actually execute against the warehouse with data-driven decisions. Every later stage (#7 grader operates on kept tests; #8 diff renderer operates on the kept/dropped split; #9 CLI wires prune into the flow) consumes the `PruneResult` contract.

### Codebase findings (Subagent B — directly verified)

**Upstream surfaces already shipped:**

- **`signalforge.draft.CandidateSchema`** (`src/signalforge/draft/models.py:176-200`) — frozen Pydantic v2; carries `schema_version: Literal[1]`, `name`, `description`, `rationale: str | None`, `columns: tuple[CandidateColumn, ...]`, `tests: tuple[CandidateTest, ...]` (model-level tests). `CandidateColumn` (`models.py:151-173`) carries `name`, `description`, `rationale`, `tests: tuple[CandidateTest, ...]`, `meta: dict[str, Any] | None`. The drafter's anchor-contract validator (`signalforge.draft.parser._validate_anchor_contract`) guarantees every `CandidateColumn.tests[].column == column.name` and every test's `column` references a real model column — the prune layer can assume this holds on input.
- **`CandidateTest` discriminated union** (`models.py:133-148`) — exactly four variants:
  - `CandidateTestNotNull(type="not_null", column, rationale?)`
  - `CandidateTestUnique(type="unique", column, rationale?)`
  - `CandidateTestAcceptedValues(type="accepted_values", column, values: tuple[str, ...], rationale?)`
  - `CandidateTestRelationships(type="relationships", column, to: str, field: str, rationale?)`
  No `where:` modifier. No dbt-utils test types. v0.1 prune compiles exactly these four; anything else is a parser-rejected case upstream.
- **`signalforge.warehouse.WarehouseAdapter`** ABC (`base.py:31-121`) exposes everything prune needs:
  - `dialect() -> Dialect` — returns `BIGQUERY_DIALECT(quote_char="`", supports_qualify=True, ...)` in v0.1
  - `run_test_sql(sql: str, *, capture_failures: int = 0) -> TestResult` — the prune seam. SQL contract: a single SELECT returning failing rows; the adapter wraps with `COUNT(*) AS failures` (+ `ARRAY_AGG(...)` if `capture_failures>0`) and validates pre-flight via `_sql_safety.validate_test_sql`. Returns `TestResult(passed: bool, failure_count: int, sample_failures: list[dict] | None, row_schema: list[tuple[str, str]] | None)`. **The adapter already exists; prune does NOT need to add a new method.**
  - `sample_rows(table, n, *, partition_filter=None) -> list[dict]` — deterministic hash-mod sampling (DEC-006 of #3). Prune uses this only if it materialises a sample to a temp table for batched evaluation; the typical case is "send the test SQL with the warehouse's hash-mod predicate inline."
  - `column_stats(table, column) -> ColumnStats` — context-manager-batched aggregates. Prune may call to detect "warehouse-enforced uniqueness" hints (e.g., `count == distinct → unique always-passes`).
- **`TableRef.from_model(model: Model) -> TableRef`** (`models.py:149`) — the way prune resolves a candidate to a warehouse identity. Raises `ManifestProjectNotFoundError` / `ManifestSchemaNotFoundError` if model lacks `database` / `schema`.
- **`signalforge.warehouse.errors`** — 15 typed `WarehouseError` subclasses. Prune catches `BytesBilledExceededError`, `TableNotFoundError`, `ColumnNotFoundError`, `QuerySyntaxError`, `SamplingRequiresPartitionFilterError`, `UnknownTableSizeError` to route to drop reasons (`kept-without-evidence`).
- **`signalforge.manifest.Manifest`** (`src/signalforge/manifest/models.py`) — `models: dict[str, Model]` keyed by `unique_id`. Prune needs the manifest to resolve `relationships(to: model_name, field)` against parent models — the parent must exist in the manifest, else `requires-future-data`.

**No prior prune code.** No test-compiler exists. No usage of `run_test_sql` outside of warehouse unit/integration tests. Prune is the first production caller.

**Test fakes pattern** — `tests/safety/_fake_adapter.py::FakeAdapter` and `tests/warehouse/_fake.py::FakeBigQueryClient` both expose `expect_*` APIs. `FakeAdapter` deliberately raises `AssertionError("FakeAdapter does not support run_test_sql ...")` — the safety layer doesn't run tests. Prune tests need a `FakePruneAdapter` (preferred: extend `FakeAdapter` with `expect_run_test_sql(matching=..., returns=TestResult|Exception)`) **OR** test prune end-to-end against `BigQueryAdapter(client=FakeBigQueryClient(...))` (the warehouse-level fakes are already proven and let us exercise the SQL the test compiler emits).

**No safety-layer dependency.** Prune runs SQL into the warehouse, not into an LLM. `LLMRequest` / `AuditEvent` are upstream concerns. Prune does NOT call `build_llm_request` — the AST audit-completeness scan (`tests/safety/test_public_api.py`) gates `LLMRequest` construction to `signalforge.safety.request` only.

**No anthropic dependency.** Prune does not call the LLM. The drafter (#5) draws rationale text in advance; prune writes its own decision-time rationale (the drop reason + failure count + scope), not LLM-generated.

**Validation command** (per `CLAUDE.md`): `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`. Quote `".[dev]"` — `[dev]` is a glob in zsh.

**Sibling open issues** confirm scope walls:
- **#7 (grader)** — consumes `PruneResult.kept` for rubric scoring. Prune's output is grader's input.
- **#8 (diff renderer)** — consumes `PruneResult` to render the kept/dropped table + per-artifact "why" line. The drop-reason taxonomy and the per-decision rationale text are renderer-load-bearing.
- **#9 (CLI)** — wires `signalforge generate` end-to-end: load manifest → safety policy → draft → **prune** → render → write. Constructs the adapter via `WarehouseAdapter.from_profile`; injects into prune.
- **#10 (smoke test)** — exercises the whole pipeline against `bigquery-public-data`. Includes prune.
- **#7/#8/#9 are downstream consumers; their behaviour is stable as long as the `PruneResult` contract holds.**

### Domain research (Subagent D — dbt test compilation + statistical pruning)

**Failing-rows SQL patterns** (BigQuery dialect, backtick-quoted identifiers):

| Test | Failing-rows SELECT |
|------|--------------------|
| `not_null(col)` | `SELECT \`col\` FROM \`p.d.t\` WHERE \`col\` IS NULL` |
| `unique(col)` | `SELECT \`col\` FROM \`p.d.t\` WHERE \`col\` IS NOT NULL GROUP BY \`col\` HAVING COUNT(*) > 1` |
| `accepted_values(col, values)` | `SELECT \`col\` FROM \`p.d.t\` WHERE \`col\` IS NOT NULL AND \`col\` NOT IN ({values})` |
| `relationships(child_col, to=parent, field)` | `SELECT child.\`child_col\` FROM \`p.d.t_child\` AS child LEFT JOIN \`p.d.t_parent\` AS parent ON child.\`child_col\` = parent.\`field\` WHERE child.\`child_col\` IS NOT NULL AND parent.\`field\` IS NULL` |

**Critical NULL-exclusion conventions** (must match dbt-core verbatim, else prune verdicts diverge from `dbt test` runtime verdicts):

- `unique` excludes NULLs (multiple NULLs are NOT considered duplicates)
- `accepted_values` excludes NULLs (combine with `not_null` if NULL-disallowed)
- `relationships` excludes NULL on the child side (orphan-NULL is a `not_null` concern)

**Always-pass targets** (the prune's high-confidence drops): `not_null` on a `REQUIRED`-mode column; `unique` on a primary-key surrogate (`GENERATE_UUID()`, `FARM_FINGERPRINT()`, `ROW_NUMBER()`); `accepted_values` against a column built from a closed `CASE WHEN ... THEN 'a' ... END`.

**Sample vs. full evaluation:**
- Sample: ~free in BigQuery (column footprint × sample bytes). False-negative rate = rule of three (≤ 3/N at 95% CI for "no failures observed in N rows"). 100k-row sample → ≤ ~3-in-100k false negative.
- Full: correct but bytes-scanned proportional to column footprint × table size. Already partition-filter-gated by adapter's `SamplingRequiresPartitionFilterError` for tables ≥ 100M rows.

**Per-test budget mechanism** (BigQuery):
- `QueryJobConfig.job_timeout_ms` is the cost-aware path — server-side cancellation.
- `QueryJob.result(timeout=...)` is Python-side wait only; query keeps running and bills.
- `signal.alarm` is unsafe (main-thread-only, no Windows, conflicts with pytest).
- The adapter's existing `_default_job_config` (DEC-015 of #3) is the single seam for `QueryJobConfig` — extending with `timeout_ms` is the right shape.

**Cost-saving optimisations:**
- **Per-column COUNTIF batching** — one query, multiple verdicts: `SELECT COUNTIF(col IS NULL) AS nn, COUNTIF(col NOT IN (...)) AS av FROM ...`. Cost win at scale; brittle to a single SQL syntax error (one bad test breaks the batch).
- **Sample-materialised temp table** — write the deterministic sample once into a session temp table; query it N times for free. BigQuery temp tables are session-scoped + cached for ~24h.
- **dbt itself does neither** — one query per test. Whatever batching SignalForge does is a project-specific differentiator.

**"Known-clean" evidence channels** (the four-drop-reason taxonomy says `failed-on-known-clean-data` requires evidence the data is clean; how do we know?):
- A — **dbt `run_results.json` history**: extends manifest loader to parse `target/run_results.json`; checks "model has been built successfully N times AND its dbt-defined tests have passed M times." Technically richest. Scope creep against #2.
- B — **User-marked trusted models in `signalforge.yml`**: `prune.trusted_models: ["model.shop.dim_customers", "model.shop.fact_orders"]`. Explicit. Cheapest v0.1.
- C — **Time-since-deploy heuristic**: messy; relies on run-history substrate; conflates "no failures" with "no test was looking."
- D — **Defer the reason entirely for v0.1**: emit only `always-passes`, `requires-future-data`, `kept`, `kept-without-evidence`. The ticket lists `failed-on-known-clean-data` but doesn't specify the evidence channel; deferring is conservative and within "keep tests we cannot evaluate." (See Q1.)

**Determinism + decision identity:**
- Adapter's hash-mod sampling is already deterministic. Prune adds zero non-determinism (no `CURRENT_TIMESTAMP`, no `RAND()`, no unseeded RNG, no dict-iteration-order-dependent SQL).
- Mirror existing precedent: `prune_decision_id = blake2b-8(canonical(model_unique_id, test_type, test_args, scope, sample_bucket))` — matches `LLMResponseEvent`'s `parsed_schema_hash` family.

**Prior-art cross-reference:**
- **dbt-core**: failing-rows SQL pattern (we copy verbatim).
- **great-expectations**: validation-result document idea (rich per-test verdict with row counts, observed values, decision rationale) — pattern to borrow for `PruneDecision`.
- **dbt-codegen**: rule-based scaffolder; complementary; no overlap.
- **Recce / dbt-checkpoint / dbt-coverage**: complementary; no overlap.
- **Nobody else runs candidate tests against real warehouse data and drops noise.** This is the differentiator the ticket protects.

### Project rules (`.claude/rules/`) audit (Subagent C)

`.claude/rules/` has seven files (no `workflow-project.md`). Phase 4 stories validate against:

1. **safety-layer.md** (load-bearing) — DEC-011 fail-closed audit semantics if prune writes a `PruneEvent` JSONL (no try/except inside writer; size cap before file open; propagation IS the defence). DEC-022 ANSI-safe lazy-format JSON logging — every `_LOGGER.{info,warning,debug,error}` in `signalforge.prune.*` uses lazy-format with `json.dumps()` for any user-controlled string; **never** f-string. Grep gate at `tests/prune/test_logger_grep_gate.py`. DEC-025 `signalforge.yml` namespace — claim a NEW top-level `prune:` key; do NOT pile under `safety:`.
2. **manifest-readers.md** — Pydantic v2 frozen + `extra="ignore"` on read-back models; pair with `extra="forbid"` drift detector. Typed exceptions subclass a module base (`PruneError`) with `remediation: str` kwarg; `__str__` renders both message and `↳ Remediation:` line. No logs in stage-0 modules; observability lives at the executor seam where stage labels are known.
3. **python-build.md** — src layout under `src/signalforge/prune/`. No new wheel target; existing `packages = ["src/signalforge"]` covers the subpackage. No `tests/__init__.py`.
4. **testing-signal.md** (load-bearing) — every test must be capable of failing. Strict markers — both settings. **Hand-rolled fakes only** (no `MagicMock`, no `pytest-bigquery-mock`). Pair every `extra="ignore"` model with a one-off `extra="forbid"` drift detector against a committed JSON fixture.
5. **warehouse-adapters.md** — call the ABC, not concrete classes. Trust adapter-validated identifiers. Trust adapter's `use_query_cache=False` + `maximum_bytes_billed` defaults. Inherit deterministic-sampling fail-loud behaviour. **Do NOT bake BigQuery-isms into prune core** — Architectural Commitment #3.
6. **llm-drafter.md** — applicable patterns even though prune doesn't call an LLM:
   - Module-level `_sleep` / `_rand_uniform` aliases for test-time injection (DEC-004) — relevant if prune has retry / wait logic.
   - Whole-output fail-loud collection (DEC-003 / DEC-022) — if prune validates that all candidate tests reference real columns, collect every violation, never short-circuit. Mirrors the anchor-contract pattern.
   - Fail-closed response audit (DEC-006 / DEC-008 / DEC-013) — applies if prune writes a JSONL audit (Q3 below).
   - AST audit-completeness scan extension (DEC-013) — if prune emits a `PruneEvent` type, gate construction to `signalforge.prune.audit` via `tests/test_audit_completeness.py`.
7. **ci-supply-chain.md** — no new workflow needed; prune runs in the existing `pytest` step (Python 3.11, BigQuery integration tests behind `-m bigquery` marker, mirrored as `-m prune_integration` if needed).

### CLAUDE.md commitments that bite this ticket

- **#1 Signal over volume.** This is THE ticket. The prune step IS the signal-vs-volume gate. The drafter (#5) intentionally over-generates; prune intentionally drops noise. A prune layer that drops < 20% of candidates indicates the design failed; > 80% indicates it's working. We design conservatively (kept-without-evidence is a real outcome, not a fallback) but lean aggressive on the always-pass branch.
- **#3 Warehouse-agnostic.** Prune calls `WarehouseAdapter.run_test_sql(sql)` and `WarehouseAdapter.dialect()`. The dialect provides quote_char + identifier_case so prune's compiler can produce per-warehouse SQL. No `from google.cloud import bigquery` in prune code. v0.2 Snowflake/Postgres adapters slot in without prune changes.
- **#5 Explainable diffs.** Every kept/dropped test ships with a structured `PruneDecision` carrying `decision: kept | dropped`, `reason: always-passes | requires-future-data | failed-on-known-clean-data | kept | kept-without-evidence`, `failures: int`, `sampled_rows: int`, `scope: sample | full`, `elapsed_ms: int`, `compiled_sql_hash: str`, the literal SQL (for `--explain`), and the human-readable "why" line the diff renderer (#8) consumes.
- **Roadmap anchor.** v0.1 = single-model draft + warehouse prune, BigQuery only. Multi-warehouse prune is v0.2 (but the design must NOT close it off — keep the dialect seam clean).

### Out of scope (explicit)

- **The CLI** — issue #9. This ticket exposes `prune_tests(...)` plus the `PruneResult` contract. CLI wiring is #9.
- **The grader** — issue #7. Prune outputs kept tests with reasons; grader scores them.
- **The diff renderer** — issue #8. Prune returns typed `PruneResult`; rendering to a human-readable kept/dropped table is #8.
- **dbt-utils test types** — `dbt_utils.unique_combination_of_columns`, `dbt_utils.accepted_range`, etc. The drafter's `CandidateTest` union has exactly four variants; prune compiles exactly four. v0.2 territory.
- **`where:` test modifier** — dbt-core supports `tests: [- not_null: where: "is_active = true"]`. The drafter doesn't emit it today; prune doesn't consume it. v0.2.
- **`severity: warn`** — dbt severity escalation; v0.2 alongside `mostly:` for confidence-based passing.
- **Historical always-pass** — running tests against multiple `run_results.json` snapshots to assert "never failed in last N runs." v0.2.
- **Multi-model prune in one call** — v0.1 is single-model (drafter is single-model). Batching multiple models is v0.2 if/when the drafter goes multi-model.
- **`prune_decision_id`-keyed checkpoint / resumption** — long-running prune runs that resume from disk after a crash. v0.2.
- **Snowflake/Postgres dialect support** — adapter is BigQuery-only in v0.1. The prune compiler uses `Dialect.quote_char` + `Dialect.identifier_case` to stay portable, but no Snowflake-specific SQL paths.
- **Confidence intervals on always-pass** — surfacing "≤ 3/N upper-bound failure rate at 95% CI" on the decision record. Nice to have; v0.2.
- **`mostly:` thresholds** — great-expectations-style "test passes if ≥ 99% of rows satisfy." v0.2.
- **LLM-generated rationale on `kept` decisions** — the grader (#7) does this, not prune.

### Phase 1 housekeeping defaults (set unless flagged in Phase 2/3)

- New subpackage at `src/signalforge/prune/`:
  - `__init__.py` re-exports the public surface
  - `engine.py` — `prune_tests(...)` entry + the per-test executor
  - `compiler.py` — `compile_test(test, table_ref, dialect, manifest) -> str` (the four failing-rows patterns)
  - `models.py` — `PruneResult`, `PruneDecision`, `DropReason` literal, `Scope` literal, `PruneConfig`
  - `errors.py` — `PruneError` hierarchy
  - `audit.py` — fail-closed `PruneEvent` JSONL writer (if Q3 = yes)
  - `config.py` — `load_prune_config(path) -> PruneConfig`
- Public API surface:
  - `signalforge.prune.prune_tests(candidates: CandidateSchema, model: Model, manifest: Manifest, adapter: WarehouseAdapter, *, config: PruneConfig | None = None) -> PruneResult`
  - `PruneResult(model_unique_id: str, kept: tuple[KeptTest, ...], dropped: tuple[DroppedTest, ...], decisions: tuple[PruneDecision, ...], elapsed_ms: int, signalforge_version: str, prune_schema_version: int = 1)`
  - `PruneDecision(test_anchor: str, test_type: str, test_args: dict, decision: Literal["kept","dropped"], reason: DropReason, failures: int, sampled_rows: int | None, scope: Scope, elapsed_ms: int, compiled_sql_hash: str, compiled_sql: str, why: str)`
  - `DropReason = Literal["always-passes", "requires-future-data", "failed-on-known-clean-data", "kept", "kept-without-evidence"]`
  - `Scope = Literal["sample", "full"]`
- `signalforge.yml` config block: top-level key `prune:`; fields `scope: "sample" | "full"` (default `"sample"`), `sample_size: int` (default 100_000), `test_timeout_seconds: int` (default 30), `total_budget_seconds: int` (default 600), `capture_failure_rows: int` (default 3 — matches existing `TestResult.sample_failures` capture cap). Outer wrapper `extra="ignore"`; inner `PruneConfig` `extra="forbid"`.
- `PruneConfig` and config-shaped models use `extra="forbid"` (typo-fail-loud); read-back models (`PruneDecision`, `PruneResult`, `PruneEvent`) use `extra="ignore"` paired with one-off `extra="forbid"` drift detectors.
- Identifier validation reuse: prune's compiler does NOT re-validate `TableRef.dataset` / `TableRef.name` (already validated at `TableRef.__init__`). It DOES `repr()`-quote any value-list (`accepted_values.values`) before SQL interpolation; the `accepted_values` interpolation seam needs explicit string-escaping to prevent quote-injection (`O'Brien` → `'O\'Brien'`).
- Tests live under `tests/prune/`. No `__init__.py`. Synthetic-fixture unit tests use a hand-rolled `expect_run_test_sql(matching=..., returns=...)` extension to `FakeBigQueryClient` (NOT to `FakeAdapter` — the safety fake's `run_test_sql` raises by design). Integration test under `@pytest.mark.bigquery` (existing marker) hits `bigquery-public-data.iowa_liquor_sales.sales` (or similar small public table).
- AST audit-completeness scan extended to gate `PruneEvent` construction to `signalforge.prune.audit` only.
- Logger grep-gate scan extended to `src/signalforge/prune/`.
- `signalforge.draft` — adapter's pyright shim model is mirrored if prune adds anything that needs pyright-noise containment. Anticipated: nothing new (we reuse the adapter's `run_test_sql` seam).

## Phase 1 Scoping Questions

Five questions. All scope-shaping. Answer with letters; "I'll write more" / free-text accepted.

---

**Q1. The `failed-on-known-clean-data` drop reason — evidence channel.**

The ticket lists this as one of four drop reasons: a candidate test ran, returned ≥ 1 failure, AND we have evidence the data is clean ⇒ the test is buggy. How do we know data is clean?

- **A.** Defer the reason for v0.1. Emit only `always-passes`, `requires-future-data`, `kept`, `kept-without-evidence`. Most conservative; ships fastest. v0.2 picks the channel.
- **B.** User-marked trusted models in `signalforge.yml` (`prune.trusted_models: [...]`). Explicit, cheapest v0.1, no new artefact loaders. The user opts-in per model; non-trusted models never emit `failed-on-known-clean-data`.
- **C.** Parse `target/run_results.json` for dbt run/test history. Technically richest — "this model has built successfully 30 times and its dbt-defined tests have all passed" is strong evidence. Extends issue #2's manifest loader scope significantly.
- **D.** Aggressive default: any candidate test that fails on a tiny fraction of rows (e.g., < 0.1%) is presumed buggy. Risky — silently drops legitimate findings. **Not recommended.**

---

**Q2. Sample-vs-full evaluation default.**

The ticket: "Test 'passes always' = zero failing rows on the sampled set OR zero failing rows on the full table (configurable)."

- **A.** Sample-mode default; user opts up to full per run via `prune.scope: full` in `signalforge.yml`. Cheap, fast, ~3-in-N false-negative rate (rule of three). The deterministic-sample contract from #3 already gives reproducibility.
- **B.** Full-mode default; user opts down to sample for cost. Correct, but expensive at scale (every test reads the full column on every run). Already-partition-filter-gated by `SamplingRequiresPartitionFilterError` for tables ≥ 100M rows.
- **C.** Hybrid: sample-mode for the always-pass claim + full-mode confirmation only for tests the prune is about to drop. Spends bytes only on high-value drop decisions; defends against false-positives in always-pass. More complex.

---

**Q3. Decision audit — fail-closed JSONL or in-memory result only?**

Safety (#4) and draft (#5) both write fail-closed JSONL audits adjacent to their stage outputs (`audit.jsonl`, `llm_response.jsonl`). The justification: "every privacy-relevant action leaves a durable receipt." Prune's actions are warehouse cost decisions, not privacy decisions — but the explainability principle (Commitment #5) suggests every kept/dropped test ships with a "why" the reviewer can replay.

- **A.** Fail-closed `prune.jsonl` audit at the same path as the safety/draft audits. Mirrors precedent. Adds a small amount of complexity (size-cap, fsync, AST scan extension). Reviewers can pull the JSONL and replay any decision.
- **B.** In-memory `PruneResult` only; serialisation to disk is the diff renderer's job (#8). Simpler v0.1. Trade-off: if the renderer doesn't serialise everything, the audit trail dies at process exit.
- **C.** Both — in-memory `PruneResult` for #8 to render + JSONL audit for the explainability/replay path. Defaults to "audit on" with `prune.audit.enabled: false` opt-out.

---

**Q4. Test batching strategy.**

Per-test cost is dominated by column footprint × bytes scanned. Without batching, N tests on the same model = N column scans.

- **A.** One query per test (dbt's pattern). Simplest. Most expensive at scale. Works fine for v0.1's single-model unit-of-work.
- **B.** Per-column COUNTIF batching (`SELECT COUNTIF(col IS NULL) AS nn, COUNTIF(col NOT IN (...)) AS av FROM ...`). One read, multiple verdicts. Cost win; brittle (one syntax error breaks the batch — mitigation: per-test fallback). Doesn't help `relationships` (that's a JOIN, batches with other `relationships` on the same parent only).
- **C.** Sample-materialised temp table — write the deterministic sample once into a session temp table; query it N times for free. BigQuery temp tables are session-scoped and free for reads (you pay only the materialisation cost). Larger one-time cost but free per-test thereafter. Cleanest for #1's "signal over volume" because pruning N candidates costs the same as pruning 1.
- **D.** Defer all batching to v0.2. v0.1 ships A. Acceptable given the v0.1 unit-of-work is one model.

---

**Q5. Per-test runtime budget — server-side, Python-side, or both?**

`kept-without-evidence` is the budget-exceeded outcome.

- **A.** Server-side `QueryJobConfig.job_timeout_ms` only. BigQuery cancels the job; minimal Python complexity. Tail bytes-billed possible (BigQuery cancels opportunistically).
- **B.** Python-side `concurrent.futures.ThreadPoolExecutor.submit(...).result(timeout=...)` with explicit `query_job.cancel()` in the `finally`. Tighter wall-clock control; bills full query if cancellation is slow.
- **C.** Both — server-side cap + Python-side circuit-breaker (e.g., server cap 30s, Python cap 35s with cancel). Defence-in-depth against an SDK call that hangs. More moving parts.

---

### Locked answers (2026-04-30, "keep defaults")

| Q | Pick | Implication |
|---|------|-------------|
| Q1 | **B** — User-marked trusted models in `signalforge.yml` | `prune.trusted_models: [...]` opts-in per model. Non-trusted models never emit `failed-on-known-clean-data`; their failure-on-data outcomes route to `kept`. Cheapest v0.1; explicit; no `run_results.json` loader scope-creep. |
| Q2 | **A** — Sample-mode default; `prune.scope: full` opt-up | Inherits adapter's deterministic hash-mod sampling. `sample_size: 100_000` default. Per-test override field on `PruneConfig`. |
| Q3 | **A** — Fail-closed `prune.jsonl` audit | Mirrors safety + draft precedent (`O_APPEND \| O_CREAT \| 0o600`, fsync, size cap before open, propagation IS the defence). New AST scan gate for `PruneEvent` construction in `signalforge.prune.audit` only. |
| Q4 | **A** — One query per test (defer batching to v0.2) | Simplest. v0.1 unit-of-work is one model — N is bounded by candidate count per model (typically 5–30 tests). COUNTIF batching + temp-table materialisation are v0.2 candidates. |
| Q5 | **A** — Server-side `QueryJobConfig.job_timeout_ms` only | BigQuery cancels server-side. Default `test_timeout_seconds: 30`. Extend `_default_job_config` (warehouse-adapters.md DEC-015) with a `timeout_ms` kwarg — that's the existing single-seam for query config. |

These five choices keep v0.1 tight: no new artefact loader, no new optimisation engine, no defence-in-depth complexity beyond what's already proven in adjacent layers.

## Architecture Review

Six parallel reviews (security, performance, data-model, API-design, observability, testing). Verdicts:

| Area | Pass | Concern | Blocker |
|------|------|---------|---------|
| Security | 12 | 0 | 0 |
| Performance | 7 | 4 | 1 |
| Data Model | 9 | 3 | 3 |
| API Design | 7 | 3 | 0 |
| Observability | 12 | 1 | 0 |
| Testing | 10 | 1 | 0 |

### Blockers (must resolve before refinement)

- **AR-B1 (Performance) — `TO_JSON_STRING(t)` reads ALL columns of the row.** The adapter's deterministic sampler is `WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), bucket) < 1`. `TO_JSON_STRING(t)` serialises the entire row — BigQuery cannot column-prune through a function argument. **Implication:** sample-mode is NOT the cheap "read 1 column × 100k rows" the Phase 1 cost model assumes; it's "read ALL columns × full table" to compute the predicate. The Phase 1 24MB / 30 tests / ~$0.0001 figure is wrong by potentially 50–500×. Either (a) the adapter MUST materialise the deterministic sample once into a session-scoped temp table and prune queries that (this is Q4=C from Phase 1 — we deferred it; reopening); (b) prune issues SQL inline, accepting the full-row cost; or (c) the adapter exposes a "sample column subset" mode. Live verification against `bigquery-public-data` recommended before lock.

- **AR-B2 (Performance) — Adapter's `_default_job_config` does not yet accept `timeout_ms`.** Q5=A (server-side `QueryJobConfig.job_timeout_ms`) requires extending `make_query_job_config` (`src/signalforge/warehouse/adapters/_client.py:76`) and `_default_job_config` (`src/signalforge/warehouse/adapters/bigquery.py:221`) with a `timeout_ms` kwarg threaded into `QueryJobConfig.job_timeout_ms`. **This is a prerequisite story (warehouse adapter extension) before prune can enforce per-test budgets.** Adds one upstream story (US-W1).

- **AR-B3 (Data Model) — Drift detectors are mandatory, not optional.** Each `extra="ignore"` read-back model (`PruneResult`, `PruneDecision`, `PruneEvent`) MUST pair with a one-off `Strict<Model>(extra="forbid")` validated against a committed JSON fixture in `tests/prune/test_drift_detector.py`. This is non-negotiable per `manifest-readers.md` and `safety-layer.md`; a missing detector loses forward-compat safety. (Surfaced as a BLOCKER because the Phase 1 housekeeping defaults didn't make it explicit; locking it as a story now.)

### Concerns (worth resolving in refinement)

**Performance**
- **AR-C1 — `relationships` parent join cost in sample-mode.** The failing-rows pattern joins the sampled child to the **full parent**. On large parents this can dominate cost. Document the asymmetry in ops; defer "sample parent too" to v0.2.
- **AR-C2 — Total-budget semantics need precision.** When `total_budget_seconds` exceeds, do we (a) cancel in-flight + skip remaining (each marked `kept-without-evidence`), or (b) let in-flight finish + skip only not-yet-started? Recommend (a) for tighter wall-clock guarantees.
- **AR-C3 — `partition_filter` knob missing from `PruneConfig`.** Full-mode on tables ≥ 100M rows REQUIRES a partition filter (adapter raises `SamplingRequiresPartitionFilterError`). User has no config seam to provide one. Add `prune.partition_filter: dict | None` (rendered to `PartitionFilter` ADT — typed value object, not raw string) or document that full-mode on huge tables is unsupported in v0.1.

**Data Model**
- **AR-C4 — `KeptTest` / `DroppedTest` redundancy.** Phase 1 housekeeping mentioned both `kept`/`dropped` tuples AND a unified `decisions`. **Drop the type duplication.** `PruneResult` carries one `decisions: tuple[PruneDecision, ...]` plus computed properties `.kept_decisions` / `.dropped_decisions` (filter by `decision` field). One source of truth; one drift-detector burden.
- **AR-C5 — `test_args: dict` is loose.** A v0.1 reader can't validate v0.2 test-arg shapes. Recommendation: serialise the original `CandidateTest` discriminated union directly (`test: CandidateTest`) so the typed shape round-trips; v0.1 readers fail loud on a v0.2 test type (acceptable — they shouldn't process unknown drop reasons either).
- **AR-C6 — Hash conventions.** Mixed precedent in repo: `safety.policy_hash` is `sha256(...)[:16]` (16 hex); `draft` uses `blake2b(..., digest_size=8).hexdigest()` (16 hex). Lock: `compiled_sql_hash` → blake2b-8 (matches draft); `config_hash` → sha256-16 (matches safety policy_hash). Document both helpers in `prune/audit.py`.

**API Design**
- **AR-C7 — Signature order divergence from drafter.** Drafter is `(model, adapter, policy, manifest, *, config)`. Prune's `(candidates, model, manifest, adapter, *, config)` breaks the visual pattern. Recommendation: align to `(model, adapter, candidates, manifest, *, config)` — model+adapter front-paired (same scaffolding the CLI threads through stages), then primary input (candidates), then context (manifest), then config.
- **AR-C8 — `PruneCompilerError` is misnamed.** Compilation always succeeds; failures (e.g., `relationships(to: unknown_model)`) emit a drop reason, not an exception. **Drop the class.** Final error hierarchy: `PruneError` → `PruneConfigError` → `PruneTrustedModelNotFoundError`; `PruneTimeoutError`, `PruneAuditWriteError`, `PruneAuditRecordTooLargeError`. Six classes, no compiler exception.
- **AR-C9 — `PruneEvent` re-export ambiguity.** Re-export the type for downstream callers/typing, but the AST audit-completeness scan gates *construction* to `signalforge.prune.audit` only. Mirrors draft's `LLMResponseEvent` precedent exactly.

**Trusted-models validation timing (DEC-elect)**
- `PruneConfig.trusted_models` is a tuple of unique_id strings. Pydantic can't cross-validate against the manifest at config-load time (manifest isn't loaded yet). **Validate at `prune_tests(...)` entry**: iterate `config.trusted_models`, raise `PruneTrustedModelNotFoundError(unique_id=...)` on first miss. Loud failure on typos.

**Observability**
- **AR-C10 — `bytes_billed` per decision deferred to v0.2.** `TestResult` doesn't carry `total_bytes_billed` today; adding it is portability-sensitive (Snowflake/Postgres adapters won't have a direct analogue). v0.1 records `elapsed_ms` only; ops doc notes the gap.

**Testing**
- **AR-C11 — Integration test cost.** One end-to-end test against `bigquery-public-data` per ticket. Behind `@pytest.mark.bigquery` (existing marker; CI excludes via `-m 'not bigquery'`). Slow, but ticket-required and non-optional for closing the bytes/cost loop on AR-B1.

### Architecture review summary

Two genuine architecture surprises emerged:
1. **The cost model is wrong** (AR-B1) — `TO_JSON_STRING(t)` is the entire row. Phase 1 understated sample-mode cost; we may need to reopen Q4 (deferred batching) and adopt Q4=C (temp-table-materialised sample) as the v0.1 default rather than Q4=A.
2. **Adapter prerequisite work** (AR-B2) — `QueryJobConfig.job_timeout_ms` plumbing isn't wired in the warehouse adapter today. Q5=A requires a small upstream story (US-W1: extend `_default_job_config` with `timeout_ms`).

Everything else is concern-level: data-model tightening (AR-C4–C6), API-shape alignment with the drafter (AR-C7–C9), and config polish (AR-C3 partition_filter, trusted-models validation timing). All resolvable in Phase 3 refinement.

## Refinement Log

Architecture-blocker resolutions (Q-AR1=B, Q-AR2=B) plus all concerns folded into 28 numbered decisions.

### Cost-model resolution (Q-AR1=B)

The `TO_JSON_STRING(t)` finding (AR-B1) is real: BigQuery cannot column-prune through a function argument, so the deterministic-sample predicate reads every column. We **live-verify before locking strategy**. v0.1 ships **Q4=A (one query per test)** as the default; the diagnostic probe story (US-003) measures `total_bytes_billed` against a small public table and either confirms the cost model is acceptable for v0.1 or escalates to Q4=C (temp-table-materialised sample). The escalation is a v0.2 follow-up only if the probe produces a result the user is unwilling to ship.

### Adapter prerequisite resolution (Q-AR2=B)

Adapter extension (`timeout_ms` plumbing through `make_query_job_config` and `_default_job_config`) folds into this ticket as the second story (US-002), not a separate ticket. Single self-contained PR; ~10 LOC plus tests.

### Decisions

- **DEC-001 — Subpackage layout.** `src/signalforge/prune/{__init__.py, engine.py, compiler.py, models.py, errors.py, audit.py, config.py}`. `_`-prefixed helpers stay non-public.
- **DEC-002 — `prune_tests()` signature.** `prune_tests(model: Model, adapter: WarehouseAdapter, candidates: CandidateSchema, manifest: Manifest, *, config: PruneConfig | None = None) -> PruneResult`. Mirrors drafter's `(model, adapter, ...)` front-pairing (AR-C7).
- **DEC-003 — Drop `KeptTest` / `DroppedTest`.** `PruneResult` has a single `decisions: tuple[PruneDecision, ...]` plus computed properties `kept_decisions` / `dropped_decisions`. One source of truth (AR-C4).
- **DEC-004 — Preserve typed `CandidateTest` on decision.** `PruneDecision.test: CandidateTest` carries the original discriminated union. No loose `test_args: dict`. v0.1 readers fail loud on a v0.2 test type — acceptable; they couldn't process unknown drop reasons either (AR-C5).
- **DEC-005 — Hash conventions (locked).** `compiled_sql_hash` = `blake2b(sql.encode(), digest_size=8).hexdigest()` (16 hex; matches draft). `config_hash` = `sha256(canonical.encode()).hexdigest()[:16]` (16 hex; matches `safety.policy_hash`). Helpers in `prune/audit.py` (AR-C6).
- **DEC-006 — Error hierarchy (locked).** Six classes: `PruneError(SignalForgeError)` → `PruneConfigError` → `PruneTrustedModelNotFoundError`; siblings `PruneTimeoutError`, `PruneAuditWriteError`, `PruneAuditRecordTooLargeError`. **No `PruneCompilerError`** — compilation always succeeds; `relationships(to: unknown)` emits a `requires-future-data` drop reason, not an exception (AR-C8).
- **DEC-007 — `PruneEvent` re-exported, construction-gated.** Re-exported from `signalforge.prune.__init__` for downstream typing. AST audit-completeness scan (`tests/test_audit_completeness.py`) gates `Call(func=Name(id="PruneEvent"))` to `signalforge/prune/audit.py` only — mirrors `LLMResponseEvent` precedent exactly (AR-C9).
- **DEC-008 — Trusted-models validation timing.** `prune.trusted_models: tuple[str, ...]` of `unique_id` strings is validated at `prune_tests(...)` entry, NOT at config-load (manifest isn't loaded yet). Each entry must appear in `manifest.models`; otherwise `PruneTrustedModelNotFoundError(unique_id=...)` raised before any warehouse call. Mirrors Q1=B intent.
- **DEC-009 — `partition_filter` config knob.** `PruneConfig.partition_filter: PartitionFilter | None = None` — the typed value object from `signalforge.warehouse.models`, NOT a raw string. YAML loader renders the dict-shaped input via Pydantic into the typed `PartitionFilter`. Required for full-mode on tables ≥ 100M rows (AR-C3).
- **DEC-010 — Drift detectors are mandatory.** `tests/prune/test_drift_detector.py` defines `StrictPruneResult`, `StrictPruneDecision`, `StrictPruneEvent` (each `extra="forbid"`) and validates a committed `tests/fixtures/prune/prune_event_v1.jsonl` fixture. Adding a field to production without updating the strict mirror OR the fixture breaks the test loudly (AR-B3).
- **DEC-011 — Total-budget semantics.** When `total_budget_seconds` is exceeded, the orchestrator (a) cancels the in-flight test (best-effort `query_job.cancel()`), and (b) marks every remaining un-started test as `kept-without-evidence` with `why="total prune budget exceeded before evaluation"`. No partial-evaluation results (AR-C2).
- **DEC-012 — Live cost verification first (Q-AR1=B).** US-003 measures `total_bytes_billed` from a deterministic-sample run on `bigquery-public-data.iowa_liquor_sales.sales`. If bytes-billed is within ~10× the per-column-only estimate, Q4=A ships v0.1 unchanged. If 50–500× the estimate, escalate to Q4=C (temp-table materialisation) as a v0.1 amendment.
- **DEC-013 — Adapter extension folded (Q-AR2=B).** US-002 extends `make_query_job_config` (`src/signalforge/warehouse/adapters/_client.py`) and `_default_job_config` (`src/signalforge/warehouse/adapters/bigquery.py`) with a `timeout_ms: int | None = None` kwarg threaded into `QueryJobConfig.job_timeout_ms`. ~10 LOC plus tests. Lands before prune's engine.
- **DEC-014 — `PruneEvent` is flat, not nested.** Mirrors `safety.AuditEvent` and `draft.LLMResponseEvent`. Fields: `audit_schema_version: Literal[1] = 1`, `signalforge_version`, `record_id` (uuid4), `timestamp` (ISO 8601 UTC, Z-suffixed), `config_hash`, `model_unique_id`, plus the flattened `PruneDecision` fields (`test`, `decision`, `reason`, `failures`, `sampled_rows`, `scope`, `elapsed_ms`, `compiled_sql_hash`, `compiled_sql`, `why`, `sample_failures`).
- **DEC-015 — `extra=` placement (locked).** `PruneConfig`, `_PruneConfigFile` → `extra="forbid"`. `PruneResult`, `PruneDecision`, `PruneEvent` → `extra="ignore"` paired with `extra="forbid"` Strict mirrors per DEC-010. Mirrors safety-layer.md DEC-015.
- **DEC-016 — Audit at `<project>/.signalforge/prune.jsonl`.** Hardcoded path relative to safety/draft audit's parent (`.signalforge/`). Symlink-hardened via `signalforge.warehouse._path_safety.canonicalise_path` at writer entry. Fail-closed (`O_APPEND | O_CREAT | 0o600` + fsync; size cap `_PRUNE_AUDIT_RECORD_LIMIT_BYTES = 4000` checked before `os.open`; **no try/except inside writer** — propagation is the defence). Mirrors safety/draft semantics exactly.
- **DEC-017 — ANSI-safe lazy-format JSON logger.** Every `_LOGGER.{info,warning,debug,error}` in `signalforge.prune.*` uses lazy-format with `json.dumps()` for any user-controlled string. `_LOGGER = logging.getLogger(__name__)` per module. Grep gate at `tests/prune/test_logger_grep_gate.py` extends the existing `tests/llm/test_logger_grep_gate.py` scan to `src/signalforge/prune/` (or merges the two). Mirrors safety-layer.md DEC-022.
- **DEC-018 — AST audit-completeness scan extension.** `tests/test_audit_completeness.py` adds a fifth scan: `PruneEvent` construction confined to `src/signalforge/prune/audit.py` only. Sanity check that at least one construction exists in the blessed module. Mirrors llm-drafter.md DEC-013.
- **DEC-019 — Module-level `_sleep` alias.** `signalforge.prune.engine` declares `_sleep = time.sleep` at module scope so tests can reassign without monkey-patching globally. Used for the total-budget enforcement loop. Mirrors llm-drafter.md DEC-004.
- **DEC-020 — `prune:` top-level namespace in `signalforge.yml`.** Outer `_PruneConfigFile(extra="ignore")` so unknown sibling stages (`safety:`, `llm:`, `grade:`) don't break the loader. Inner `PruneConfig(extra="forbid")` so typos like `scop:` fail loud. Mirrors llm-drafter.md DEC-027.
- **DEC-021 — Public re-export surface.** `signalforge/prune/__init__.py` exposes: `prune_tests`, `PruneResult`, `PruneDecision`, `PruneConfig`, `load_prune_config`, `DropReason`, `Scope`, `PruneEvent`, `PruneError`, `PruneConfigError`, `PruneTrustedModelNotFoundError`, `PruneTimeoutError`, `PruneAuditWriteError`, `PruneAuditRecordTooLargeError`. `_compile_test`, `_render_why`, `_compute_*_hash`, `_write_prune_event`, `_sleep` are not exported.
- **DEC-022 — `repr()`-quote user input in error messages.** `_format_value(v) = repr(v)` helper. A model unique_id containing `\x1b[31m` doesn't inject into log viewers. `__repr__` on `PruneResult` shows only `model_unique_id`, `kept_count`, `dropped_count`, `elapsed_ms` — no test bodies, no SQL. Mirrors warehouse-adapters.md DEC-022.
- **DEC-023 — NULL-exclusion in compiler matches dbt-core verbatim.** `unique` includes `WHERE col IS NOT NULL`; `accepted_values` includes `WHERE col IS NOT NULL AND col NOT IN (...)`; `relationships` includes child-side `WHERE child.col IS NOT NULL`. Snapshot fixtures pin exact SQL bytes; divergence from dbt-core verdicts is a regression.
- **DEC-024 — `accepted_values.values` escaping reuses adapter helper.** `signalforge.warehouse._sql_safety.escape_bq_string_literal` already exists for the adapter's needs. Prune compiler imports and applies it before single-quoting `accepted_values` literals. No new escaping helper.
- **DEC-025 — Compiler is dialect-driven, not BigQuery-specific.** `_compile_test(test, table_ref, dialect: Dialect, manifest)` reads `dialect.quote_char`, `dialect.identifier_case`, `dialect.supports_qualify` to render SQL. **No `from google.cloud import bigquery` in `signalforge/prune/`.** v0.2 Snowflake/Postgres adapters return their own `Dialect` and the compiler routes accordingly.
- **DEC-026 — `relationships` parent join uses adapter's `TableRef.from_model`.** Resolves the `to` model name to a `TableRef` via the manifest's `Model` lookup; if the model is absent, the compiler returns a sentinel and the engine routes to `requires-future-data` (no warehouse call).
- **DEC-027 — `bytes_billed` per decision deferred to v0.2.** `PruneDecision.elapsed_ms` only in v0.1. `TestResult` stays unchanged. Documented in `docs/prune-ops.md`. v0.2 may extend `TestResult` and add `bytes_billed` to the audit payload.
- **DEC-028 — Single-threaded prune in v0.1.** No `concurrent.futures`, no `asyncio`. Tests are evaluated sequentially against a single adapter context. Concurrency is a v0.2 candidate (Snowflake's per-warehouse parallelism shape may differ from BigQuery's slot model).

## Detailed Breakdown

16 stories. US-001…US-014 are implementation; QG and PM are the standard tail. Project ordering: scaffold → adapter prereq → diagnostic probe → primitives (models, config, errors, compiler) → audit → orchestrator (depends on all primitives) → drift detector → cross-cutting tests → integration → re-exports → docs → quality gate → patterns.

Each story embeds **TDD** where the surface is logic-shaped (compiler, engine, audit, errors, models). Migration- or wiring-shaped stories (scaffold, re-exports, docs) skip TDD.

### US-001 — Subpackage scaffold + `prune:` namespace

- **Description:** Create the empty `signalforge.prune` subpackage skeleton and reserve the `prune:` top-level key in `signalforge.yml`.
- **Traces to:** DEC-001, DEC-020
- **Acceptance Criteria:** `src/signalforge/prune/{__init__.py, engine.py, compiler.py, models.py, errors.py, audit.py, config.py}` exist as stubs; `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest` passes.
- **Done when:** `import signalforge.prune` succeeds; `signalforge.prune.__doc__` exists; subpackage appears in `pyright` output with no errors.
- **Files:** `src/signalforge/prune/__init__.py` (docstring + module exports placeholder); `src/signalforge/prune/{engine,compiler,models,errors,audit,config}.py` (each: module docstring, `from __future__ import annotations`, `_LOGGER = logging.getLogger(__name__)` where applicable). No `tests/prune/__init__.py`.
- **Depends on:** none.

### US-002 — Warehouse adapter `timeout_ms` plumbing (folded US-W1)

- **Description:** Extend `make_query_job_config` and `_default_job_config` with a `timeout_ms: int | None = None` kwarg threaded into `QueryJobConfig.job_timeout_ms`. Required for prune's per-test budget (Q5=A).
- **Traces to:** DEC-013, AR-B2
- **Acceptance Criteria:** `make_query_job_config(stage, ..., timeout_ms=30_000)` produces a `QueryJobConfig` with `job_timeout_ms == 30_000`; `timeout_ms=None` leaves the field unset; existing call sites unchanged. Validation suite passes.
- **Done when:** New unit test `tests/warehouse/test_default_job_config_timeout.py` covers (a) None default, (b) explicit value, (c) propagation through `_default_job_config(self, *, stage, timeout_ms=...)`. A long-running query in the integration suite uses the new kwarg with a 1s timeout and observes `JobCancelledError`-equivalent.
- **Files:** `src/signalforge/warehouse/adapters/_client.py` (extend `make_query_job_config`); `src/signalforge/warehouse/adapters/bigquery.py` (extend `_default_job_config`); `tests/warehouse/test_default_job_config_timeout.py` (new); `docs/warehouse-adapter-ops.md` (one-line addendum about the kwarg).
- **TDD:** Yes.
  - Test: `make_query_job_config` with `timeout_ms=None` → `job.job_timeout_ms is None`
  - Test: `make_query_job_config` with `timeout_ms=30_000` → `job.job_timeout_ms == 30_000`
  - Test: `_default_job_config(stage="warehouse_test", timeout_ms=30_000)` propagates value
  - Test: existing callers (no `timeout_ms` arg) still produce the same config (regression guard)
- **Depends on:** US-001.

### US-003 — Diagnostic cost probe (resolves AR-B1)

- **Description:** Live-verify the deterministic-sample cost model against `bigquery-public-data`. Confirms whether Q4=A (one-query-per-test) is viable for v0.1 or whether we must escalate to Q4=C (temp-table materialisation).
- **Traces to:** DEC-012, AR-B1
- **Acceptance Criteria:** `tests/warehouse/test_sample_cost_probe.py::test_sample_rows_bytes_billed_within_budget` runs `BigQueryAdapter.sample_rows` against a small public table (`bigquery-public-data.iowa_liquor_sales.sales` or similar), asserts `total_bytes_billed` is within a documented bound (e.g., `< 100_000_000` for the 100k-row deterministic sample), and emits a `WARNING` log if cost is ≥ 10× the column-footprint estimate. Marked `@pytest.mark.bigquery`. Result documented in `docs/prune-ops.md`.
- **Done when:** Test passes against live BigQuery (gated on `SF_RUN_BQ=1`); documented bytes-billed figure recorded in `docs/prune-ops.md` cost-model section. If the test reveals 50–500× the estimate, opens a v0.1 amendment ticket to add Q4=C; v0.1 ships unchanged otherwise.
- **Files:** `tests/warehouse/test_sample_cost_probe.py` (new); `docs/prune-ops.md` (new — initial scaffold; full content lands in US-014).
- **TDD:** No (this is the diagnostic itself).
- **Depends on:** US-001.

### US-004 — `PruneResult`, `PruneDecision`, primitive types

- **Description:** Define the prune layer's read-back data classes: `PruneResult`, `PruneDecision`, `DropReason`, `Scope`. All frozen, tuples for sequences, `extra="ignore"`, computed-property aggregates on `PruneResult`.
- **Traces to:** DEC-003, DEC-004, DEC-014, DEC-015
- **Acceptance Criteria:** `PruneDecision` carries `test: CandidateTest` (typed discriminated union; not a loose dict); `PruneResult.kept_decisions` / `.dropped_decisions` are `@property` filters; `model_dump_json` round-trips through a fixture.
- **Done when:** `tests/prune/test_models.py` covers (a) frozen-set raises, (b) tuple coercion (list input → tuple), (c) `kept_decisions`/`dropped_decisions` correctness, (d) `extra="ignore"` accepts unknown fields, (e) `DropReason` Literal rejects typos.
- **Files:** `src/signalforge/prune/models.py`; `tests/prune/test_models.py`.
- **TDD:** Yes.
  - Test: `PruneResult.decisions` accepts a list, stores a tuple
  - Test: `PruneResult.kept_decisions` returns only `decision == "kept"` records
  - Test: `PruneDecision(reason="phantom-reason")` raises `ValidationError`
  - Test: setting any field on a constructed `PruneDecision` raises (frozen)
  - Test: `PruneDecision(test=CandidateTestNotNull(column="c"))` round-trips via `model_dump_json`
- **Depends on:** US-001.

### US-005 — `PruneConfig` + `load_prune_config`

- **Description:** Define `PruneConfig` (`extra="forbid"`) and `_PruneConfigFile` wrapper (`extra="ignore"`). Load `signalforge.yml`, validate, return `PruneConfig`. Defaults match Phase 1 housekeeping.
- **Traces to:** DEC-009, DEC-015, DEC-020
- **Acceptance Criteria:** `load_prune_config(Path("signalforge.yml"))` returns `PruneConfig(scope="sample", sample_size=100_000, test_timeout_seconds=30, total_budget_seconds=600, capture_failure_rows=3, trusted_models=(), partition_filter=None)` when the file is absent or `prune:` block is empty. Typos (`scop:`) raise `PruneConfigError`. Sibling stages (`safety:`, `llm:`) are tolerated. `partition_filter` accepts a dict matching `PartitionFilter` shape and renders to the typed object.
- **Done when:** `tests/prune/test_config.py` covers (a) round-trip with a full YAML, (b) defaults when keys absent, (c) typo at `prune:` → `PruneConfigError`, (d) sibling top-level keys ignored, (e) `partition_filter: {column: dt, op: ">=", value: "2026-01-01"}` parses to a `PartitionFilter`.
- **Files:** `src/signalforge/prune/config.py`; `tests/prune/test_config.py`; `tests/fixtures/prune/signalforge_full.yml` (new); `tests/fixtures/prune/signalforge_typo.yml` (new); `tests/fixtures/prune/signalforge_minimal.yml` (new).
- **TDD:** Yes.
  - Test: `load_prune_config(missing_path)` returns defaults
  - Test: typo at `prune.scop` → `PruneConfigError` with line/col info
  - Test: sibling `safety:` block tolerated by `_PruneConfigFile`
  - Test: `partition_filter` dict round-trips through Pydantic into `PartitionFilter`
  - Test: `yaml.safe_load` rejects Python object constructors
- **Depends on:** US-001, US-004.

### US-006 — `PruneError` hierarchy

- **Description:** Define six error classes: `PruneError(SignalForgeError)`, `PruneConfigError`, `PruneTrustedModelNotFoundError`, `PruneTimeoutError`, `PruneAuditWriteError`, `PruneAuditRecordTooLargeError`. Each carries `default_remediation`; `__str__` renders both message and `↳ Remediation:` line; user input is `repr()`-quoted.
- **Traces to:** DEC-006, DEC-022
- **Acceptance Criteria:** `str(PruneTrustedModelNotFoundError(unique_id="model.shop.\x1b[31mevil"))` does NOT contain a raw ANSI escape; the unique_id is `repr()`-quoted. Each subclass renders message + remediation.
- **Done when:** `tests/prune/test_errors.py` covers each subclass + the `repr()`-quoting defence.
- **Files:** `src/signalforge/prune/errors.py`; `tests/prune/test_errors.py`.
- **TDD:** Yes.
  - Test: `str(PruneError("msg"))` contains `↳ Remediation:`
  - Test: `default_remediation` per subclass renders when `remediation=None`
  - Test: `PruneTrustedModelNotFoundError(unique_id="\x1b[31m")` `__str__` doesn't leak ANSI
  - Test: each subclass inherits `PruneError`
- **Depends on:** US-001.

### US-007 — Compiler — failing-rows SQL for the four test types

- **Description:** Implement `_compile_test(test, table_ref, dialect, manifest) -> str | _RequiresFutureData` for `not_null`, `unique`, `accepted_values`, `relationships`. Uses `dialect.quote_char` for identifier quoting. Reuses `signalforge.warehouse._sql_safety.escape_bq_string_literal` for `accepted_values.values`. NULL-exclusion matches dbt-core. `relationships(to: unknown)` returns the `_RequiresFutureData` sentinel (not an exception).
- **Traces to:** DEC-023, DEC-024, DEC-025, DEC-026
- **Acceptance Criteria:** Snapshot fixtures `tests/fixtures/prune/compiled_sql/{not_null,unique,accepted_values,relationships}.sql` pin exact SQL bytes. NULL-exclusion assertions per test type. Adversarial `accepted_values` strings (`O'Brien`, `\x1b[31m`, `\n`, `; DROP TABLE x;--`) round-trip through `escape_bq_string_literal` + the compiler without breaking SQL or producing injection.
- **Done when:** `tests/prune/test_compiler.py` covers ~20 cases: 4 happy-path snapshots, 3 NULL-exclusion assertions, ~9 adversarial values, identifier-backtick verification, `_RequiresFutureData` sentinel for unknown `relationships.to`, determinism (same input → same SQL), hash stability.
- **Files:** `src/signalforge/prune/compiler.py`; `tests/prune/test_compiler.py`; `tests/fixtures/prune/compiled_sql/*.sql` (new); `tests/fixtures/prune/regenerate.sh` (new — documents how to refresh snapshots).
- **TDD:** Yes.
  - Test: `_compile_test(NotNull(column="c"), ref, BIGQUERY_DIALECT, manifest)` matches `not_null.sql` snapshot
  - Test: `_compile_test(Unique(column="c"), ...)` SQL contains `IS NOT NULL`
  - Test: `_compile_test(AcceptedValues(column="c", values=("a",)), ...)` SQL contains `IS NOT NULL AND` and `NOT IN`
  - Test: `_compile_test(Relationships(column="c", to="parent", field="id"), ...)` SQL contains `LEFT JOIN` + `child.\`c\` IS NOT NULL` + `parent.\`id\` IS NULL`
  - Test: `accepted_values=(O'Brien,)` produces `'O\\'Brien'` in SQL (escaped, balanced quotes)
  - Test: `accepted_values=(; DROP TABLE x;--,)` is fully quoted; `validate_test_sql` does not reject the wrapped result
  - Test: `Relationships(to="model.unknown")` returns `_RequiresFutureData(reason=...)` sentinel
  - Test: same input produces same SQL byte-for-byte across two invocations
  - Test: `compiled_sql_hash` is `blake2b-8(sql.encode()).hexdigest()` (16 hex)
- **Depends on:** US-001, US-004 (for `CandidateTest` re-import only).

### US-008 — Audit (fail-closed `prune.jsonl` + `PruneEvent`)

- **Description:** Implement `signalforge.prune.audit` with `PruneEvent` model + `_write_prune_event(event, path)` writer. Fail-closed: `O_APPEND | O_CREAT | 0o600`, fsync, size cap before `os.open`, no try/except inside writer. Audit-completeness AST scan extension gates `PruneEvent` construction to this module.
- **Traces to:** DEC-007, DEC-014, DEC-016, DEC-018
- **Acceptance Criteria:** `_write_prune_event(event, audit_dir / "prune.jsonl")` produces a single JSONL line; oversize record (≥ 4000 bytes) raises `PruneAuditRecordTooLargeError` BEFORE `os.open` (no on-disk artefact); `OSError`/`PermissionError`/encoding failure propagate raw. AST scan in `tests/test_audit_completeness.py` rejects `PruneEvent(...)` calls outside `signalforge/prune/audit.py`.
- **Done when:** `tests/prune/test_audit.py` covers (a) happy-path single-line write, (b) `O_APPEND|O_CREAT|0o600` mode, (c) `os.fsync` invoked, (d) oversize raises before `os.open`, (e) `OSError` propagates raw, (f) ANSI in `compiled_sql` round-trips through JSON safely, (g) concurrent appends from 10 threads × 50 writes don't interleave (POSIX `O_APPEND` atomicity); `tests/test_audit_completeness.py::test_prune_event_construction_only_in_prune_audit_module` passes.
- **Files:** `src/signalforge/prune/audit.py`; `tests/prune/test_audit.py`; `tests/test_audit_completeness.py` (extend with the prune scan).
- **TDD:** Yes.
  - Test: writer emits one JSONL line with `audit_schema_version: 1`
  - Test: writer opens with `O_APPEND | O_CREAT | 0o600`
  - Test: writer calls `os.fsync` before close
  - Test: oversize record raises `PruneAuditRecordTooLargeError`; `audit.jsonl` does not exist
  - Test: `OSError` from `os.write` propagates as-is (no wrapping)
  - Test: `PruneEvent(compiled_sql="\x1b[31m")` JSON round-trip is ANSI-safe (escaped to ``)
  - Test: 10-thread × 50-write concurrent append produces 500 well-formed lines
  - Test: AST scan rejects `PruneEvent(...)` in any module other than `prune/audit.py`
- **Depends on:** US-001, US-004, US-006.

### US-009 — Engine (`prune_tests` orchestrator)

- **Description:** Implement `prune_tests(model, adapter, candidates, manifest, *, config)` end-to-end: validate trusted_models against manifest; iterate candidates; compile each test (or route to `requires-future-data`); call `adapter.run_test_sql` with per-test `timeout_ms`; route the outcome to the correct `DropReason`; enforce total budget; write audit; return `PruneResult`. Module-level `_sleep` alias for deterministic test override.
- **Traces to:** DEC-002, DEC-008, DEC-011, DEC-019, plus all DROP-reason routing
- **Acceptance Criteria:** Six routing branches verified — `always-passes`, `requires-future-data`, `failed-on-known-clean-data` (only for trusted models), `kept-without-evidence` (per-test timeout), `kept-without-evidence` (total budget exceeded), `kept`. Audit row written per decision. `PruneAuditWriteError` propagation aborts the run (no `PruneResult`). `PruneTrustedModelNotFoundError` raised at entry on typo. Total-budget exceedance cancels in-flight + marks remaining `kept-without-evidence`.
- **Done when:** `tests/prune/test_engine.py` covers ~20 cases against an extended `FakeBigQueryClient.expect_run_test_sql(...)`.
- **Files:** `src/signalforge/prune/engine.py`; `tests/prune/test_engine.py`; `tests/warehouse/_fake.py` (extend with `expect_run_test_sql(matching, returns)` helper).
- **TDD:** Yes.
  - Test: 5 always-pass + 3 failed + 2 timeout → `PruneResult.dropped_decisions` count = 5+2; reasons match
  - Test: `relationships(to: missing_model)` short-circuits — no `run_test_sql` call; `dropped`, `requires-future-data`
  - Test: per-test `timeout_ms` exceeded → adapter raises (or returns timeout marker); engine routes `kept-without-evidence`; `why` includes elapsed_ms
  - Test: total-budget exceeded after N tests → remaining (N+1…end) marked `kept-without-evidence`, no further `run_test_sql` invocations
  - Test: trusted model in `config.trusted_models` + test failure → `failed-on-known-clean-data`
  - Test: non-trusted model + test failure → `kept`
  - Test: `config.trusted_models = ("model.unknown",)` raises `PruneTrustedModelNotFoundError` at entry; no warehouse call
  - Test: `PruneAuditWriteError` from audit writer aborts the run; no `PruneResult` returned
  - Test: `WarehouseError` subclass (`TableNotFoundError`, `ColumnNotFoundError`, `QuerySyntaxError`, `BytesBilledExceededError`) routes to `kept-without-evidence` with the error message in `why`
  - Test: `_sleep` alias is reassignable (test injects a recorder; assert call count equals total-budget loop iterations)
  - Test: deterministic `config_hash` across two identical configs
- **Depends on:** US-002, US-004, US-005, US-006, US-007, US-008.

### US-010 — Drift detectors

- **Description:** `tests/prune/test_drift_detector.py` defines `StrictPruneResult`, `StrictPruneDecision`, `StrictPruneEvent` (each `extra="forbid"` mirrors of the production models) and validates a committed JSONL fixture. Adding a field to production without updating the strict mirror or the fixture breaks the test loudly.
- **Traces to:** DEC-010, DEC-015
- **Acceptance Criteria:** `tests/fixtures/prune/prune_event_v1.jsonl` (3-5 events covering happy path + each drop reason) validates against `StrictPruneEvent`. Three strict mirrors each pair with field-set assertions against the production model.
- **Done when:** Test passes; deliberately adding a field to `PruneEvent` without updating the mirror breaks the test.
- **Files:** `tests/prune/test_drift_detector.py`; `tests/fixtures/prune/prune_event_v1.jsonl`; `tests/fixtures/prune/prune_result_v1.json`; `tests/fixtures/prune/prune_decision_v1.json`.
- **TDD:** Yes.
  - Test: `StrictPruneEvent.model_validate_json(line)` for every line in fixture passes
  - Test: `StrictPruneEvent.model_fields.keys() == PruneEvent.model_fields.keys()` (field-set parity)
  - Same two tests for `StrictPruneResult` + `StrictPruneDecision`
- **Depends on:** US-004, US-008.

### US-011 — Logger grep gate extension

- **Description:** Extend `tests/llm/test_logger_grep_gate.py` (or create `tests/prune/test_logger_grep_gate.py`) to scan `src/signalforge/prune/` for `_LOGGER\.\w+\(f"` patterns. Reject any hits.
- **Traces to:** DEC-017
- **Acceptance Criteria:** Test runs against current `signalforge.prune.*` and finds zero violations. Deliberately adding `_LOGGER.info(f"...{user_input}...")` to any prune module breaks the test.
- **Done when:** Grep gate covers all three modules (`llm`, `draft`, `prune`).
- **Files:** Extend `tests/llm/test_logger_grep_gate.py` OR create `tests/prune/test_logger_grep_gate.py` and update its scan paths.
- **TDD:** No (test-only).
- **Depends on:** US-009 (so there's something for the gate to scan).

### US-012 — Integration test against `bigquery-public-data`

- **Description:** End-to-end prune against a small public BQ table. One comprehensive test under `@pytest.mark.bigquery` (existing marker; CI excludes via `-m 'not bigquery'`).
- **Traces to:** ticket acceptance criterion #7, AR-C11
- **Acceptance Criteria:** `tests/prune/test_integration_bigquery.py::test_prune_iowa_liquor_sales` runs `prune_tests` against four candidate tests on `bigquery-public-data.iowa_liquor_sales.sales` (or similar). Asserts at least one always-pass drop AND at least one kept failure. Marked `@pytest.mark.bigquery` + `@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), ...)`. Documented in `docs/prune-ops.md` "running real-warehouse tests".
- **Done when:** Test passes against live BigQuery (gated on `SF_RUN_BQ=1`); excluded from default `pytest` run.
- **Files:** `tests/prune/test_integration_bigquery.py` (new).
- **TDD:** No (integration).
- **Depends on:** US-009.

### US-013 — Public re-exports

- **Description:** Populate `signalforge/prune/__init__.py` with the public surface. Re-export `PruneEvent` for typing (construction is AST-gated to `audit.py` per DEC-007).
- **Traces to:** DEC-021
- **Acceptance Criteria:** `from signalforge.prune import prune_tests, PruneResult, PruneDecision, PruneConfig, load_prune_config, DropReason, Scope, PruneEvent, PruneError, PruneConfigError, PruneTrustedModelNotFoundError, PruneTimeoutError, PruneAuditWriteError, PruneAuditRecordTooLargeError` works. `signalforge.prune.__all__` lists each. `pyright` clean.
- **Done when:** Smoke test `tests/prune/test_smoke.py::test_public_api_imports` passes.
- **Files:** `src/signalforge/prune/__init__.py`; `tests/prune/test_smoke.py`.
- **TDD:** No (re-exports).
- **Depends on:** US-004, US-005, US-006, US-008, US-009.

### US-014 — Operational reference doc

- **Description:** Write `docs/prune-ops.md` mirroring `docs/safety-ops.md` and `docs/draft-ops.md` shape. Cover: public API + signature; config block reference; drop-reason taxonomy; cost model + the AR-B1 verification result from US-003; how to invoke against BigQuery; audit JSONL schema (event fields + record contract); running real-warehouse tests; v0.2 deferrals (`bytes_billed`, batching, multi-warehouse).
- **Traces to:** DEC-027, all v0.1 surfaces
- **Acceptance Criteria:** Doc exists; cross-linked from the `## What's shipped` section of `CLAUDE.md`.
- **Done when:** `docs/prune-ops.md` published; `CLAUDE.md` "Repository status" section updated to list `#6 (prune engine)`; `## Public API surface (v0.1)` section in `CLAUDE.md` lists the prune surface.
- **Files:** `docs/prune-ops.md` (new); `CLAUDE.md` (extend).
- **TDD:** No (docs).
- **Depends on:** US-003, US-009, US-012.

### Quality Gate (QG)

- **Description:** Run code reviewer four times across the full changeset, fixing all real bugs found each pass. Run CodeRabbit review. Project validation (`ruff check . && ruff format --check . && pyright && pytest`) must pass after all fixes.
- **Acceptance Criteria:** Four code-review passes, CodeRabbit pass, validation suite green.
- **Done when:** All four reviews complete; outstanding CodeRabbit comments resolved or deferred with justification; CI green.
- **Files:** Whatever each pass surfaces.
- **Depends on:** US-001 through US-014.

### Patterns & Memory (PM)

- **Description:** Distill the rules learned in this ticket into `.claude/rules/prune-engine.md`; update `CLAUDE.md` to reference it; add `bd remember` entries for any patterns that should be reachable cross-ticket (e.g., "when extending the audit family, always extend the AST audit-completeness scan").
- **Acceptance Criteria:** New `.claude/rules/prune-engine.md` exists with the standard "Established by issue #6 ..." preamble; `CLAUDE.md` lists it; `.claude/rules/llm-drafter.md`-shape conventions covered (single seam, fail-closed audit, AST gates, ANSI-safe logger, dialect-driven SQL).
- **Done when:** Rules file lands; `CLAUDE.md` cross-links it; pattern memory entries created.
- **Files:** `.claude/rules/prune-engine.md` (new); `CLAUDE.md`.
- **Depends on:** QG.

## Beads Manifest

Devolved 2026-04-30. Epic + 14 implementation tasks + Quality Gate + Patterns & Memory.

- **Worktree:** `/home/wesd/dev/worktrees/SignalForge/feature/6-prune-engine`
- **Branch:** `feature/6-prune-engine` (off `dev`)
- **PR:** [#20](https://github.com/wjduenow/SignalForge/pull/20) (draft, plan only)

| Story | Beads ID |
|-------|----------|
| Epic — 6: Test prune engine | `bd_1-scaffolding-y8y` |
| US-001 Subpackage scaffold | `bd_1-scaffolding-y8y.1` |
| US-002 Adapter `timeout_ms` plumbing | `bd_1-scaffolding-y8y.2` |
| US-003 Diagnostic cost probe | `bd_1-scaffolding-y8y.3` |
| US-004 `PruneResult` / `PruneDecision` / primitives | `bd_1-scaffolding-y8y.4` |
| US-005 `PruneConfig` + `load_prune_config` | `bd_1-scaffolding-y8y.5` |
| US-006 `PruneError` hierarchy | `bd_1-scaffolding-y8y.6` |
| US-007 Compiler — failing-rows SQL × 4 | `bd_1-scaffolding-y8y.7` |
| US-008 Audit — `prune.jsonl` + AST scan | `bd_1-scaffolding-y8y.8` |
| US-009 Engine — `prune_tests` orchestrator | `bd_1-scaffolding-y8y.9` |
| US-010 Drift detectors | `bd_1-scaffolding-y8y.10` |
| US-011 Logger grep gate extension | `bd_1-scaffolding-y8y.11` |
| US-012 BQ integration test | `bd_1-scaffolding-y8y.12` |
| US-013 Public re-exports | `bd_1-scaffolding-y8y.13` |
| US-014 `docs/prune-ops.md` | `bd_1-scaffolding-y8y.14` |
| QG Quality Gate | `bd_1-scaffolding-y8y.15` |
| PM Patterns & Memory | `bd_1-scaffolding-y8y.16` |

Dependencies wired so `bd ready` returns only the epic + US-001 at start; subsequent tasks unblock as predecessors close.
