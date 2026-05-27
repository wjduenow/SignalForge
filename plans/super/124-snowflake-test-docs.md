# Super Plan — #124: Snowflake test harness + gated live e2e + ops docs

## Meta

- **Ticket:** [#124](https://github.com/wjduenow/SignalForge/issues/124) — `test+docs: fakesnow harness + FakeSnowflakeClient + gated live e2e (TPCH_SF1) + ops docs`
- **Milestone:** v0.2 (Snowflake adapter epic [#118](https://github.com/wjduenow/SignalForge/issues/118)) — **lands last**
- **Phase:** devolved
- **Branch:** `feature/124-snowflake-test-docs` (based on `dev`)
- **Sessions:** 1 (2026-05-26)

---

## Phase 1 — Discovery

### What / Why / Who

**What.** The closing ticket of the Snowflake epic: certify the Snowflake adapter end-to-end with (a) a fakesnow-backed offline harness that runs the adapter's *own* emitted SQL through a real Snowflake-flavored engine, (b) a hand-rolled `FakeSnowflakeConnection` `expect_*` harness (already shipped — verify/extend), (c) the full `map_snowflake_exception` taxonomy (deferred to #124 by #122), (d) gated live e2e tests against a real warehouse + the TPCH sample dataset, and (e) consolidated ops docs + rules distillation.

**Why.** #119–#123 + #130 shipped the adapter surfaces (profile parsing, dialect, sampling, materialise, run-test, estimate) each with offline tests, but several pieces were *explicitly deferred to #124* in their own docstrings/rules: the full error taxonomy, the live-warehouse sample-mode certification (fakesnow can't execute `HASH(*)`), and the consolidated operator docs. This ticket closes those gaps so a v0.2 operator can run SignalForge against Snowflake with confidence and a documented cost posture.

**Who.** Operators pointing SignalForge at a Snowflake warehouse; maintainers who need a live-certification harness and a documented offline test story for the adapter.

### Dependency status — RESOLVED (this lands last)

All adapter surfaces are merged into `dev`. Confirmed by direct read:

| Surface | State | Evidence |
|---|---|---|
| `SnowflakeAdapter` ops | `sample_rows` / `materialise_sample` / `run_test_sql` / `estimate_query_bytes` all implemented; `column_stats` raises `NotImplementedError` (deferred) | `src/signalforge/warehouse/adapters/snowflake.py` |
| `map_snowflake_exception` | **minimal** — auth → `WarehouseAuthError`, `ProgrammingError` → `QuerySyntaxError`, else passthrough. Docstring: *"a full taxonomy mirroring `map_bq_exception` is deferred to #124"* | `_snowflake_client.py:123` |
| `fakesnow` dev-dep | **already present** in `[dependency-groups].dev` + `[project.optional-dependencies].dev` | `pyproject.toml` |
| `snowflake` marker | **already registered + deselected** in `addopts` | `pyproject.toml:99,114` |
| `FakeSnowflakeConnection` | **already exists** (`expect_execute` / `assert_all_expectations_met` / `close_raises`) | `tests/warehouse/_fake_snowflake.py` |
| Compiler fakesnow + sqlglot guard | **already exists** (`@pytest.mark.snowflake`); runs the *compiler's* SQL, `scope="full"` only. Docstring: *"Real-Snowflake sample-mode semantics are deferred to #124's live harness."* | `tests/prune/test_compiler_fakesnow.py` |
| Gated live test | **estimate-only** (`SELECT 1` EXPLAIN); no full-pipeline / sampling e2e yet | `tests/warehouse/test_snowflake_estimate_live.py` |
| Ops docs | scattered Snowflake notes (profile, estimate, error table); no consolidated section | `docs/warehouse-adapter-ops.md` |

**Net remaining scope** (much of the ticket's nominal scope was cannibalised by prior tickets):
1. Full `map_snowflake_exception` taxonomy + offline unit tests.
2. fakesnow-backed **adapter** harness (execute non-`HASH` adapter SQL; sqlglot-parse the rest).
3. Hand-crafted TPCH manifest seed + loads-only validation test.
4. Two gated live e2e tests (warehouse+prune-only; full `generate` pipeline against TPCH_SF1).
5. Consolidated Snowflake ops-doc section + `.claude/rules/warehouse-adapters.md` distillation.

### Key architecture finding (surfaced during scoping)

**#122's `materialise_sample` colocates the temp table in the *source* db/schema** (`CREATE TEMPORARY TABLE "<src db>"."<src schema>"._sf_sample_<run_id>`). `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1` is **read-only shared data** — a CTAS there fails with a permission error. Therefore the two e2e tests must use *different* sample strategies (see DEC-002/DEC-003). This is the load-bearing design constraint for the live e2e.

### Scoping answers (user)

- **Q1 — e2e shape:** **BOTH** a warehouse+prune-only e2e AND a full `generate`-pipeline e2e (DEC-001).
- **Q2 — fakesnow depth:** **Execute non-`HASH` adapter SQL through fakesnow; sqlglot-parse the `HASH(*)` sample-mode SQL** (DEC-004).
- **Q3 — error taxonomy:** **Full mirror of `map_bq_exception`** — add `TableNotFoundError` + `ColumnNotFoundError`, reusing existing `WarehouseError` subclasses (DEC-005).

### Convention constraints (`.claude/rules/`)

- **`testing-signal.md`** — gated e2e = belt-and-suspenders (`@pytest.mark.snowflake` + runtime `_skip_reason()` with one distinct reason per missing env var); `tmp_path` isolation for any committed fixture the CLI writes into; engineered determinism (mathematically-guaranteed always-pass, never assert live `HASH`/planner values); hand-crafted manifest seed + loads-only test when a fixture would otherwise need live `dbt parse`; marker-specific runs use `--no-cov`.
- **`warehouse-adapters.md`** — one-shim-per-vendor: every `snowflake-connector-python` import/type-ignore stays in `_snowflake_client.py` (guarded by `test_snowflake_client_confinement.py`); fakes are hand-rolled `expect_*` (no `MagicMock`), live under `tests/warehouse/`, never imported by production; `map_snowflake_exception` returns the mapped exc (passthrough unchanged); Snowflake cleanup WARNING has no manual command + no countdown.
- **`prune-engine.md`** — "a new dialect's SQL needs a parser/executor in the loop, not just snapshot equality"; sqlglot Snowflake-dialect parse-guard is the syntax gate; fakesnow asserts rule-semantics, never `HASH()` value-equality.
- **`docs-publishing.md`** — extending `docs/warehouse-adapter-ops.md` (already in `mkdocs.yml` nav line 51) needs **no** nav/mkdocs change.
- **`python-build.md` / `ci-supply-chain.md`** — `fakesnow` already in `[dependency-groups].dev`; gated markers carry no Python-version pin and CI never collects them; run gated suite on the matrix ceiling (`uv run --python 3.13 pytest -m snowflake --no-cov`).
- **`cli-layer.md`** — 7th AST scan requires every concrete `*Error` to be in `_EXCEPTION_TO_EXIT_CODE`. The full taxonomy reuses **existing** `WarehouseError` subclasses (`TableNotFoundError`, `ColumnNotFoundError`, already tier-3) → **no new error classes, no exit-code-table change, no scan churn**.

---

## Phase 2 — Architecture Review

| Area | Rating | Finding |
|---|---|---|
| Security | **concern → addressed** | Live tests need real Snowflake creds + (full-pipeline) `ANTHROPIC_API_KEY` — all from env vars, never committed; belt-and-suspenders gating prevents accidental collection. The hand-crafted TPCH seed embeds no credentials. `map_snowflake_exception` is pure + its error messages render via `WarehouseError._format_value` (`repr()`-safe) — no raw-credential leak. The **read-only-DB CTAS** finding (DEC-002/003) is the substantive one. |
| Performance / Cost | **concern → addressed** | Live Snowflake bills compute. Ticket mandates cost guidance: **set a resource monitor first**, **XS warehouse**, **aggressive auto-suspend**, LIMIT-bounded queries. TPCH_SF1 (~1.5 GB) on XS + `oneshot` sampling keeps scans tiny. fakesnow runs in-memory (free). Documented prominently in the ops doc + each e2e module docstring (DEC-009). |
| Data model | **pass** | No schema/migration. Manifest seed + TPCH references are test data only. |
| API design | **pass** | `map_snowflake_exception` keeps its return convention (mapped exc / passthrough). Full mirror reuses existing typed errors; no signature or public-surface change. `BytesBilledExceededError` deliberately omitted — Snowflake has no bytes-billed cap (DEC-005). |
| Observability | **pass** | No new routine logging. The shim stays logger-free (logging lives in the adapter). |
| Testing strategy | **pass (core)** | Offline-green floor: full taxonomy + fakesnow-adapter unit tests pass with no credentials (snowflake-connector + fakesnow are dev-deps, so `sfe.*` error classes construct offline). Both live tests skip cleanly without env vars. Loads-only seed test runs in the default suite. Gated runs use `--no-cov`. |

**No blockers.** The two concerns (read-only-DB CTAS; live cost) are resolved by DEC-002/003/009.

---

## Phase 3 — Refinement Log (Decisions)

- **DEC-001 — Two gated e2e tests.** A lean warehouse+prune-only e2e (isolates the genuinely-new Snowflake warehouse path; deterministic without LLM) **and** a full `generate`-pipeline e2e against TPCH_SF1 (closest mirror of the bikeshare e2e). *Rationale:* user chose "Both"; the two exercise complementary surfaces (materialised-sample lifecycle vs. full LLM→prune→diff).

- **DEC-002 — Warehouse+prune-only e2e writes to the maintainer's WRITABLE schema.** Creates a tiny engineered table (e.g. a 2-column table with one guaranteed-non-null column) in `SNOWFLAKE_DATABASE.SNOWFLAKE_SCHEMA` (from env), runs `prune_tests` with **hand-crafted candidate tests** (no LLM), exercises the **materialised** sample strategy + `always-passes` drop, and tears the table down in a `finally`. *Rationale:* materialised sampling needs write access; a self-owned tiny table gives full control + exercises the CTAS/session/cleanup path that the read-only DB cannot.

- **DEC-003 — Full-pipeline e2e uses `sample_strategy: oneshot` against read-only TPCH_SF1.** No CTAS into the read-only shared DB; `oneshot` `sample_rows` reads TPCH directly. Engineered always-pass via a model whose SQL projects a literal/`COALESCE` column (LLM reliably proposes `not_null`; `not_null` on a literal is mathematically always-pass). Gated additionally on `ANTHROPIC_API_KEY`. *Rationale:* the read-only-DB CTAS finding forces `oneshot`; engineered determinism per `testing-signal.md`.

- **DEC-004 — fakesnow adapter harness: execute-where-possible + sqlglot-parse-the-rest.** New `tests/warehouse/test_snowflake_adapter_fakesnow.py` (`@pytest.mark.snowflake`). Uses `fakesnow.patch()` so a real `SnowflakeAdapter` (injected fakesnow connection) executes its non-`HASH` SQL offline (`run_test_sql` COUNT-wrapper over engineered rows → rule-semantic failing-row shape; `materialise_sample` CTAS executes/creates a table; `_get_num_rows` INFORMATION_SCHEMA sizing where fakesnow supports it). For the `HASH(*)` hash-mod sample-mode SQL (which fakesnow's DuckDB backend cannot execute), assert the adapter's emitted SQL **parses** under sqlglot's Snowflake dialect. *Rationale:* honors the #121 "parser/executor in the loop" lesson for the adapter's own SQL without faking `HASH` semantics; the live harness (DEC-002) covers real `HASH` execution. *Caveat to verify during implementation:* fakesnow support for `CREATE TEMPORARY TABLE`, `ARRAY_AGG(OBJECT_CONSTRUCT(*))`, and `INFORMATION_SCHEMA.TABLES.ROW_COUNT` — any unsupported construct degrades that sub-case to sqlglot-parse-only with an inline comment.

- **DEC-005 — Full `map_snowflake_exception` mirror, reusing existing typed errors.** Add: Snowflake `ProgrammingError` "object does not exist" (errno 002003 / SQLSTATE 02000-ish / message marker) → `TableNotFoundError`; "invalid identifier" (errno 000904) → `ColumnNotFoundError`; keep auth → `WarehouseAuthError`, residual `ProgrammingError` → `QuerySyntaxError`, everything else passthrough. **No new `WarehouseError` subclass** (so no exit-code-table / AST-scan churn). `BytesBilledExceededError` omitted (no Snowflake equivalent). Split happens BEFORE the broad `ProgrammingError → QuerySyntaxError` fallthrough. *Rationale:* the shim docstring + `warehouse-adapters.md` name the full taxonomy as #124; mirroring `map_bq_exception`'s coverage is the contract.

- **DEC-006 — Error taxonomy tested offline via crafted real connector exceptions.** Unit tests construct genuine `snowflake.connector.errors.ProgrammingError(...)` / `DatabaseError` / `OperationalError` / `ForbiddenError` instances (the connector is a dev-dep, so this is offline) and assert the mapped typed error + that the typed `.table` / `.column` fields are populated from `context`. Also a passthrough test (unmapped exc returned unchanged). *Rationale:* offline-green; mirrors how `map_bq_exception` is unit-tested.

- **DEC-007 — `FakeSnowflakeConnection` extended only if a gap appears.** The existing `expect_execute(returns=<Exception>)` already drives error-injection and the `close_raises` path covers cleanup-fail-soft. Assess during US-002/US-004; add a helper only if a real need surfaces (e.g. a multi-cursor session assertion). Default: no change. *Rationale:* avoid speculative fake surface.

- **DEC-008 — Hand-crafted TPCH manifest seed + loads-only test.** Ship a minimal `manifest.json` seed (one model targeting a TPCH_SF1 table with an engineered always-pass column) under `tests/fixtures/snowflake/` + a regen note documenting the maintainer-only `dbt parse` reproduction, validated by an in-process `signalforge.manifest.load(...)` loads-only test that runs in the **default** suite (no env vars). *Rationale:* `testing-signal.md` hand-crafted-seed rule — workers can't run live `dbt parse`.

- **DEC-009 — Cost guidance is prominent and verbatim-ish.** The ops doc + both live-e2e module docstrings carry the cost posture: resource monitor FIRST, XS warehouse, aggressive auto-suspend, the env-var list, and `uv run pytest -m snowflake --no-cov`. *Rationale:* a live test that bills real money must not be runnable without the operator seeing the cost guardrails.

- **DEC-010 — Extend `docs/warehouse-adapter-ops.md`; no new file.** A consolidated "Snowflake adapter (v0.2)" section folding the scattered notes (profile keys, dialect, session lifecycle, sampling, materialise + cleanup, estimate, full error taxonomy, cost guidance, running gated tests) + a distillation paragraph in `.claude/rules/warehouse-adapters.md`. *Rationale:* file already in mkdocs nav → no `mkdocs.yml` change (`docs-publishing.md`).

- **DEC-011 — No marker / dependency / pyproject changes.** `snowflake` marker, `fakesnow`, and `snowflake-connector-python` are already wired by prior tickets; verify in the QG, don't re-add. *Rationale:* avoid churn + shared-file collisions (per the serialize-shared-registry memory).

---

## Phase 4 — Detailed Breakdown (Stories)

> Validation command (every story's AC): `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`. Gated Snowflake suites additionally: `uv run --python 3.13 pytest -m snowflake --no-cov` (maintainer-only; skips cleanly without env vars).

### US-001 — Full `map_snowflake_exception` taxonomy + offline unit tests

- **Description:** Expand `map_snowflake_exception` in `_snowflake_client.py` to mirror `map_bq_exception`'s coverage: split `ProgrammingError` into `TableNotFoundError` ("object does not exist"), `ColumnNotFoundError` ("invalid identifier"), and residual `QuerySyntaxError`, keeping auth → `WarehouseAuthError` and passthrough. Reuse existing typed errors only.
- **Traces to:** DEC-005, DEC-006.
- **Acceptance criteria:**
  - `ProgrammingError` with an object-not-exist marker (errno `002003` and/or message) → `TableNotFoundError(table=<context table or "<unknown>">)`; invalid-identifier marker (errno `000904`) → `ColumnNotFoundError(table=..., column=<extracted>)`; other `ProgrammingError` → `QuerySyntaxError`. Split runs before the syntax fallthrough.
  - Auth mapping (`ForbiddenError` / auth-marker `DatabaseError`/`OperationalError`) unchanged; unmapped exceptions returned unchanged.
  - One-shim rule preserved (lazy `from snowflake.connector import errors`); `test_snowflake_client_confinement.py` still green.
  - Unit tests construct real `sfe.*` instances offline and assert each mapping + the populated typed `.table`/`.column` fields + the passthrough case.
  - Full validation command passes.
- **Done when:** `tests/warehouse/test_snowflake_client.py` (or a new `test_snowflake_exception_mapping.py`) exercises every taxonomy arm offline and passes; pyright clean.
- **Files:** `src/signalforge/warehouse/adapters/_snowflake_client.py` (mapper body; possibly a private `_extract_invalid_identifier` regex helper); `tests/warehouse/test_snowflake_client.py` or new mapping test module.
- **Depends on:** none.
- **TDD:** write the mapping-arm tests first (object-not-exist→Table, invalid-identifier→Column, other-programming→Syntax, forbidden→Auth, auth-message→Auth, transient→passthrough), then implement the split.

### US-002 — fakesnow-backed adapter harness (execute + sqlglot-parse)

- **Description:** New gated test module driving a real `SnowflakeAdapter` against an in-memory fakesnow connection so the adapter's *own* emitted SQL executes offline where the engine allows, and parses under sqlglot's Snowflake dialect where `HASH(*)` blocks execution.
- **Traces to:** DEC-004.
- **Acceptance criteria:**
  - `@pytest.mark.snowflake`; deselected from default `addopts`; `fakesnow = pytest.importorskip("fakesnow")`.
  - Executes the adapter's `run_test_sql` COUNT-wrapper over engineered rows and asserts rule-semantic failing-row shape (≥1 vs 0), never `HASH` value-equality.
  - Executes `materialise_sample`'s CTAS against fakesnow (creates a table) **or**, if fakesnow lacks the construct, falls back to sqlglot-parse with an inline comment naming the gap.
  - The hash-mod **sample-mode** SQL the adapter emits (`sample_rows` / `materialise_sample`) is asserted to **parse** under `sqlglot` Snowflake dialect (the syntax gate the #121 reserved-word bug demonstrated).
  - Runs green under `uv run --python 3.13 pytest -m snowflake --no-cov`; collected 0 / skipped in the default suite.
- **Done when:** the module exercises run-test execution + materialise (exec or parse) + sample-mode parse, all under the marker.
- **Files:** `tests/warehouse/test_snowflake_adapter_fakesnow.py` (new).
- **Depends on:** none.

### US-003 — Hand-crafted TPCH manifest seed + loads-only test

- **Description:** A minimal committed `manifest.json` seed describing one dbt model over a `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1` table with an engineered always-pass column, plus a regen note and an in-process loads-only validation test (no env vars, runs in the default suite).
- **Traces to:** DEC-008, DEC-003.
- **Acceptance criteria:**
  - Seed lives under `tests/fixtures/snowflake/` with a `README.md` regen note (maintainer-only `dbt parse` reproduction).
  - The model's compiled SQL projects a literal / `COALESCE` column guaranteeing an always-pass `not_null` candidate.
  - `signalforge.manifest.load(<fixture_dir>)` succeeds and yields the model — asserted by a test in the **default** suite (no marker, no env vars).
  - Full validation command passes.
- **Done when:** the loads-only test passes offline and the seed is consumable by US-005.
- **Files:** `tests/fixtures/snowflake/{manifest.json, dbt_project.yml?, README.md}`; `tests/.../test_snowflake_seed_loads.py` (new).
- **Depends on:** none.

### US-004 — Warehouse+prune-only gated live e2e (materialised sample)

- **Description:** A gated live test that creates a tiny engineered table in the maintainer's writable schema, runs `prune_tests` with hand-crafted candidates under the **materialised** strategy, asserts an `always-passes` drop, and tears the table down.
- **Traces to:** DEC-001, DEC-002, DEC-009.
- **Acceptance criteria:**
  - `@pytest.mark.snowflake` + `_skip_reason()` gating on `SF_RUN_SNOWFLAKE=1` + `SNOWFLAKE_ACCOUNT/USER/PASSWORD/WAREHOUSE` + `SNOWFLAKE_DATABASE/SCHEMA` (writable target), each missing var → distinct reason.
  - Creates a tiny table (e.g. one guaranteed-non-null column) in the writable schema; `prune_tests` with a hand-crafted `not_null` candidate over it under `sample_strategy="materialised"`; asserts ≥1 decision with `reason="always-passes"` (`decision="dropped"`).
  - Table + session torn down in `finally` (idempotent; tolerates partial setup).
  - Module docstring carries the cost guidance (resource monitor, XS warehouse, auto-suspend) + the run command.
  - Skips cleanly without env vars; no `Traceback` on the skip path.
- **Done when:** runs against a real Snowflake (maintainer) and asserts the always-passes drop; self-skips otherwise.
- **Files:** `tests/warehouse/test_snowflake_prune_live.py` (new).
- **Depends on:** none (independent of US-001/002).

### US-005 — Full `generate`-pipeline gated live e2e (TPCH_SF1, oneshot)

- **Description:** A gated live test that runs the full `generate` CLI flow (LLM draft → prune → diff) against the read-only TPCH_SF1 dataset using `sample_strategy: oneshot`, asserting an `always-passes` drop on the engineered literal column.
- **Traces to:** DEC-001, DEC-003, DEC-009; reuses US-003 seed.
- **Acceptance criteria:**
  - `@pytest.mark.snowflake` + `_skip_reason()` gating on `SF_RUN_SNOWFLAKE=1` + Snowflake conn vars + **`ANTHROPIC_API_KEY`** (full-stack three+-var gate), distinct reason per missing var.
  - Uses the US-003 seed copied into `tmp_path`; `signalforge.yml` sets `prune.sample_strategy: oneshot` (no CTAS into read-only DB).
  - Invokes `main(["generate", <model>, "--project-dir", <tmp>])`; asserts exit 0, a `.signalforge/diff.json` sidecar exists, kept+dropped+flagged ≥ 1, ≥1 `always-passes` drop, `GradingReport.aggregate_complete is True`, and **no `Traceback` in stderr**.
  - `tmp_path` isolation so committed fixtures aren't polluted.
  - Skips cleanly without env vars.
- **Done when:** runs the full pipeline against live TPCH (maintainer) and asserts the differentiator (`always-passes` drop); self-skips otherwise.
- **Files:** `tests/cli/test_e2e_snowflake_smoke.py` (new); reuse `tests/cli/_e2e_helpers.py`.
- **Depends on:** US-003.

### US-006 — Consolidated Snowflake ops docs + rules distillation

- **Description:** Add a consolidated "Snowflake adapter (v0.2)" section to `docs/warehouse-adapter-ops.md` and distil the new conventions into `.claude/rules/warehouse-adapters.md`.
- **Traces to:** DEC-005, DEC-009, DEC-010; documents US-001/004/005.
- **Acceptance criteria:**
  - Ops section covers: profile keys + auth scope, dialect (UPPER folding, quoted `"sample"` CTE), connection-bound session lifecycle, sampling + materialise + cleanup (no manual command, server-side reap), estimate (EXPLAIN), the **full error taxonomy** table rows, cost guidance (resource monitor / XS / auto-suspend), and **how to run the gated tests** (`uv run pytest -m snowflake --no-cov` + env-var matrix for each test).
  - No `mkdocs.yml` change (file already in nav); `uv run mkdocs build` emits no new broken-link warnings for the section.
  - `.claude/rules/warehouse-adapters.md` gains a short #124 distillation (full taxonomy mapping; the read-only-DB CTAS → strategy split; the fakesnow-execute-+-sqlglot-parse adapter-harness convention) with a Reference pointer to this plan.
  - Full validation command passes (docs edits don't break it).
- **Done when:** the ops section + rule distillation land and read coherently against the shipped code.
- **Files:** `docs/warehouse-adapter-ops.md`; `.claude/rules/warehouse-adapters.md`.
- **Depends on:** US-001, US-004, US-005.

### US-007 — Quality Gate (code review ×4 + CodeRabbit)

- **Description:** Run the code reviewer 4× across the full changeset, fixing all real bugs each pass; run CodeRabbit if available; full validation must pass; verify DEC-011 (no marker/dep regressions) and the offline-green floor (default suite passes with no Snowflake env vars).
- **Traces to:** all DECs.
- **Acceptance criteria:** validation command green; gated suite skips cleanly with no env vars and (maintainer) runs green with them; sqlglot parse-guard + confinement test still pass; no new `*Error` slipped into the exit-code table.
- **Done when:** four review passes complete with fixes applied and validation green.
- **Files:** as needed.
- **Depends on:** US-001 … US-006.

### US-008 — Patterns & Memory (priority 99)

- **Description:** Update `.claude/rules/`, `docs/`, and/or memory with patterns learned (the read-only-DB CTAS strategy split; fakesnow `HASH(*)` execution gap → sqlglot-parse fallback; the full Snowflake error taxonomy). Close out the epic if this is the last #118 ticket.
- **Traces to:** all DECs.
- **Acceptance criteria:** durable conventions captured where the next contributor will find them; `CHANGELOG.md` updated if the release process expects it.
- **Done when:** patterns recorded; plan phase set to `devolved`.
- **Files:** `.claude/rules/warehouse-adapters.md`, memory, `CHANGELOG.md` (if applicable).
- **Depends on:** US-007.

### Dependency graph

```
US-001 ─┐
US-002 ─┤
US-003 ─┴─> US-005 ─┐
US-004 ───────────────┤
US-001, US-004, US-005 ─> US-006 ─> US-007 ─> US-008
```

(US-001, US-002, US-003, US-004 are mutually independent and can run in parallel; US-005 needs US-003; US-006 needs US-001/004/005; QG gates all; P&M last.)

---

## Phase 5 — Publish PR

Draft PR opened against `dev` (2026-05-26); breakdown approved in-session same day.

## Beads Manifest

- **Epic:** `bd_1-scaffolding-88t` — #124: Snowflake test harness + gated live e2e + ops docs
- **Branch:** `feature/124-snowflake-test-docs` (PR #133, base `dev`)
- **Tasks:**

| Bead | Story | Depends on |
|---|---|---|
| `bd_1-scaffolding-88t.1` | US-001 — Full `map_snowflake_exception` taxonomy + offline tests | — |
| `bd_1-scaffolding-88t.2` | US-002 — fakesnow adapter harness (execute + sqlglot-parse) | — |
| `bd_1-scaffolding-88t.3` | US-003 — TPCH manifest seed + loads-only test | — |
| `bd_1-scaffolding-88t.4` | US-004 — Warehouse+prune-only live e2e (materialised) | — |
| `bd_1-scaffolding-88t.5` | US-005 — Full generate-pipeline live e2e (TPCH_SF1, oneshot) | .3 |
| `bd_1-scaffolding-88t.6` | US-006 — Ops docs + rules distillation | .1, .4, .5 |
| `bd_1-scaffolding-88t.7` | Quality Gate — review ×4 + CodeRabbit | .1–.6 |
| `bd_1-scaffolding-88t.8` | Patterns & Memory (P3) | .7 |

Ready at devolve: `.1`, `.2`, `.3`, `.4` (mutually independent).
