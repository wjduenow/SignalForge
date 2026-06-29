# Super Plan — #226: Databricks test harness + gated live e2e + ops docs

## Meta

- **Ticket:** [#226](https://github.com/wjduenow/SignalForge/issues/226) — `Databricks: test harness + gated live e2e + ops docs`
- **Epic:** [#219](https://github.com/wjduenow/SignalForge/issues/219) (Databricks adapter) — **the epic-closer; lands last**
- **Models on:** Snowflake test+docs [#124](https://github.com/wjduenow/SignalForge/issues/124)
- **Phase:** published
- **Branch:** `feature/226-databricks-test-docs` (based on `dev`)
- **Worktree:** `../worktrees/SignalForge/226-databricks-test-docs`
- **Sessions:** 1 (2026-06-29)

---

## Phase 1 — Discovery

### What / Why / Who

**What.** The closing ticket of the Databricks epic: certify the adapter end-to-end with (a) the full `map_databricks_exception` taxonomy, (b) an offline harness (`FakeDatabricksConnection` + sqlglot parse tier), (c) a confinement scan, (d) gated live e2e tests against a real Databricks Free-Edition workspace, (e) a hand-crafted manifest seed + loads-only test, and (f) consolidated ops docs + README + CHANGELOG.

**Why.** #221–#225 shipped the adapter surfaces (skeleton, prune compiler dialect, sampling, estimate) each with offline tests. Several pieces were *explicitly deferred to #226* in their own docstrings/rules — the live-warehouse certification (the #224/#225 "live-cert ledger"), and the consolidated operator docs. This ticket closes those gaps so a user can run SignalForge against Databricks with confidence and a documented cost posture.

**Who.** Operators pointing SignalForge at a Databricks SQL warehouse; the maintainer who runs the live-certification harness against the Free-Edition rig.

### Dependency status — much of the nominal scope was cannibalised by #221/#223/#224/#225

Verified by direct read of `dev` (2026-06-29):

| Surface | State | Evidence |
|---|---|---|
| `map_databricks_exception` full taxonomy | **SHIPPED** (#224 DEC-009) — auth→`WarehouseAuthError`, table-not-found→`TableNotFoundError`, invalid-identifier→`ColumnNotFoundError`, residual→`QuerySyntaxError`, else passthrough; lazy SDK import | `adapters/_databricks_client.py:170-257` |
| Taxonomy offline unit tests | **SHIPPED** (no marker, default suite) | `tests/warehouse/test_databricks_adapter.py` |
| `FakeDatabricksConnection` + `expect_*` | **SHIPPED** | `tests/warehouse/_fake_databricks.py` |
| sqlglot parse-guards (compiler + adapter SQL) | **SHIPPED** (ungated, #223 DEC-002 / #224) | `tests/prune/test_compiler_databricks.py`, `tests/warehouse/test_databricks_sql_parse.py` |
| Confinement scan | **SHIPPED** (#221) | `tests/warehouse/test_databricks_client_confinement.py` |
| `databricks` marker + `[databricks]` extra | **REGISTERED** | `pyproject.toml` |
| EXPLAIN COST parser + fixtures | **SHIPPED** (#225) | `tests/warehouse/test_databricks_estimate.py`, `tests/fixtures/warehouse/databricks/` |
| Free-Edition env contract + cert (green 2026-06-24) | **DOCUMENTED** | `docs/research/databricks-test-environment.md` |
| `tests/cli/_e2e_helpers.py` | **warehouse-agnostic, reusable as-is** | — |
| **Gated live e2e (`SF_RUN_DATABRICKS`)** | **MISSING — zero tests carry the marker** | — |
| **`tests/fixtures/databricks/` dbt project + manifest seed + loads test** | **MISSING** (load-bearing — the e2e needs it) | — |
| **Ops-doc § "Databricks adapter"** | **PARTIAL** — scattered #224/#225 notes; no consolidated section | `docs/warehouse-adapter-ops.md` |
| **README warehouse roadmap row** | **STALE** — says Databricks "remains on the roadmap" | `README.md:95` |
| **CHANGELOG `[Unreleased]`** | **EMPTY** | `CHANGELOG.md:7` |

**Net remaining scope:**
1. Three gated live e2e tests + a shared skip-gate helper.
2. Hand-crafted `tests/fixtures/databricks/` dbt project + `manifest.json` seed + generator + loads-only test.
3. Consolidated Databricks ops-doc section.
4. README warehouse-coverage update + CHANGELOG `[Unreleased]` entry.
5. A maintainer live-certification pass that fixes any #124-class adapter bugs inline.

### Scoping answers (user, 2026-06-29)

- **Q1 — live e2e shapes:** **All three** (mirror #124) — `prune_live` (materialised CTAS vs a writable `workspace` engineered table) + `estimate_live` (live `EXPLAIN COST`) + `e2e_smoke` (full `generate` pipeline vs read-only `samples.*`). → DEC-001.
- **Q2 — full-pipeline source + strategy:** **`samples.nyctaxi.trips` + `oneshot`** (`prune.scope: sample` / `sample_strategy: oneshot` — exercises the #224 oneshot+`get_row_count` path). → DEC-002.
- **Q3 — safety mode:** **`schema-only`** for the full pipeline + a **thin live `column_stats` assertion** in `prune_live` (certifies the #224 aggregate path without making the pipeline depend on it). → DEC-003.
- **Q4 — live-discovered bugs:** **Fix all inline** in #226; budget a maintainer live-debugging pass. → DEC-005.

### Convention constraints (`.claude/rules/`)

- **`testing-signal.md`** — gated e2e = belt-and-suspenders (`@pytest.mark.databricks` + runtime `_skip_reason()`, one distinct reason per missing env var); `tmp_path` isolation for any committed fixture the CLI writes into; **engineered determinism** (mathematically-guaranteed always-pass via a natural-NOT-NULL source column — never assert live hash/row values); hand-crafted manifest seed + loads-only test when a fixture would otherwise need live `dbt parse`; marker-specific runs use `--no-cov`. **Source-as-model alias trick:** declare only REAL source columns, never engineered literals (the #124 QG lesson — oneshot queries the source directly).
- **`warehouse-adapters.md`** — one-shim-per-vendor (every `databricks-sql-connector` import/type-ignore stays in `_databricks_client.py`, already guarded); fakes are hand-rolled `expect_*` (no `MagicMock`), live under `tests/warehouse/`, never imported by production; `map_databricks_exception` reuses existing typed errors (no new class). The "live-cert ledger" items (qualified CTAS acceptance, session persistence across CTAS→`run_test_sql`, `to_json(struct(*))` per-row decode, `column_stats` complex-type MIN/MAX, `EXPLAIN COST` shape + 8.0 EiB sentinel) are flagged `#226` in the adapter docstrings.
- **`prune-engine.md`** — "a new dialect's SQL needs a parser/executor in the loop, not just snapshot equality"; the ungated sqlglot Databricks parse-guard is the syntax gate; live is the validity gate. No new `DropReason`, no new AST scan.
- **`cli-layer.md`** — no new typed error, no exit-code-table churn (tests+fixtures+docs + small adapter bug-fixes reusing existing errors).
- **`docs-publishing.md`** — `warehouse-adapter-ops.md` is already in `mkdocs.yml` nav; no nav change needed.

No `workflow-project.md` exists → no project-specific scoping/review additions.

---

## Phase 2 — Architecture Review

All eight load-bearing live-e2e assumptions were verified against shipped code — **every one HOLDS**. No blockers.

| Area | Rating | Finding |
|---|---|---|
| Testing strategy | **concern** (addressed) | Core of the ticket. Gating + determinism + `tmp_path` + offline loads test all map 1:1 from #124. The one open risk: `samples.nyctaxi.trips`' always-pass column is **not documented in-repo** (DEC-004) — mitigated by a warm-up `DESCRIBE`/`COUNT_IF(<col> IS NULL)==0` guard + maintainer confirmation in the live pass. |
| Security / secrets | **pass** | Creds only from env at runtime; `profiles.yml` rewritten in `tmp_path`, never committed. `schema-only` default keeps the LLM-bound PII surface minimal. Source-as-model alias points at a public read-only dataset. |
| Data model / fixture correctness | **concern** (addressed) | Hand-crafted `manifest.json` must satisfy the Model-field contract (`database=samples`, `schema=nyctaxi`, `alias=trips`, `resolve_this().qualified_name == "samples.nyctaxi.trips"`, REAL columns, no engineered literals). The default-suite loads-only test enforces it offline; the `_gen_manifest.py` generator keeps it byte-stable. |
| Cost / quota | **concern** (addressed) | Free-Edition fair-use quota; `nyctaxi.trips` is larger than `tpch.region`. Bounded by `oneshot` LIMIT-sampling, metadata-only `EXPLAIN COST`, and a single-aggregate `column_stats`. Cost guidance (2X-Small serverless, auto-stop, fair-use) lands in module docstrings + ops doc + README (DEC-009/010). |
| Live-cert robustness | **concern** (addressed) | Four #124-class candidates flagged: (1) session persistence across `materialise_sample`→`run_test_sql`; (2) `to_json(struct(*))` per-row JSON-string decode; (3) `column_stats` complex-type MIN/MAX (DEC-011 divergence); (4) `EXPLAIN COST` `Statistics(sizeInBytes=…)` format drift. "Fix all inline" (DEC-005) + a budgeted maintainer live pass cover these. The adapter docstrings already flag each as a `#226` item. |
| API design | **pass** | No new public API. Reuses the shipped adapter + warehouse-agnostic `_e2e_helpers`. |
| Observability | **pass** | No new log sites; cleanup WARNING + INFO already shipped. Loads test is silent (stage-0). |

**Verified-HOLDS assumptions (evidence in session research):** `TableRef` accepts `samples` (4–7-char catalog via `validate_catalog_or_project`, #224); `get_row_count` = read-only `COUNT(*)` through the vendor-neutral seam (`prune/engine.py:981`); `oneshot` = inline `MOD(xxhash64(...) & MASK, bucket) < 1` (no CTAS); `materialise_sample` colocates the temp in the source `catalog.schema` (writable `workspace`); `EXPLAIN COST` degrades on the 8.0 EiB / `Long.MaxValue` sentinel.

---

## Phase 3 — Refinement Log (Decisions)

- **DEC-001 — Three gated live e2e files (mirror #124).** `tests/warehouse/test_databricks_prune_live.py` (materialised CTAS vs a writable `workspace` engineered table + the thin `column_stats` live assert), `tests/warehouse/test_databricks_estimate_live.py` (live `EXPLAIN COST`, certifies the #225 deferred ledger), `tests/cli/test_e2e_databricks_smoke.py` (full `generate` pipeline). *Rationale:* matches Snowflake's three-file shape; each certifies a distinct surface (materialise+session+capture, estimate, full pipeline). [Q1=A]
- **DEC-002 — Full-pipeline source = `samples.nyctaxi.trips`, `prune.scope: sample` + `sample_strategy: oneshot`.** *Rationale:* exercises the #224 `oneshot`+`get_row_count` path against read-only shared data (no CTAS) — a stronger cert than Snowflake #124's `scope: full` (which predated the `get_row_count` seam). [Q2=C]
- **DEC-003 — `safety: schema-only` for the full pipeline; thin live `column_stats` assert lives in `prune_live`.** *Rationale:* keeps the headline e2e minimal (PII/cost) while still certifying the #224 aggregate path. **Divergence from the ticket text:** the ticket's "column_stats-deferred note" described Snowflake; for Databricks `column_stats` **is implemented** (#224 DEC-011), so the ops doc states it is *available*, not deferred. [Q3=A]
- **DEC-004 — Always-pass column = a reliably-NOT-NULL `nyctaxi.trips` column (candidate `tpep_pickup_datetime`).** The seed declares ONLY real `nyctaxi.trips` columns (no engineered literals — #124 QG lesson, load-bearing because `oneshot` queries the source directly). Each live test opens with a warm-up guard (`DESCRIBE samples.nyctaxi.trips` + assert `COUNT_IF(<col> IS NULL) == 0`) so a wrong column choice fails loud with a clear message; the maintainer confirms/swaps the column during the live pass. *Rationale:* the table schema is not documented in-repo, so the column choice is verified at runtime rather than assumed.
- **DEC-005 — Fix all live-discovered bugs inline in #226.** The maintainer live pass fixes any #124-class adapter bug (case-folding, JSON-string decode, session persistence, `EXPLAIN COST` format, complex-type `column_stats`) in the same PR, reusing existing typed errors. No survivor bead unless an issue is genuinely out-of-scope-large. [Q4=B]
- **DEC-006 — Hand-crafted seed at `tests/fixtures/databricks/`** (mirrors `tests/fixtures/snowflake/` flat naming) — `dbt_project.yml`, `models/staging/{sources.yml,stg_nyctaxi_trips.sql}`, `profiles.yml` (placeholder), `_gen_manifest.py` (maintainer-only generator), `target/manifest.json` (committed, byte-stable), `README.md`. Workers can't run live `dbt parse`. Model node: `database="samples"`, `schema="nyctaxi"`, `alias="trips"`, real columns with `data_type: null`.
- **DEC-007 — Belt-and-suspenders gating + per-test isolation.** `@pytest.mark.databricks` + a runtime `_skip_reason()` (one distinct reason per missing env var: `SF_RUN_DATABRICKS=1`, `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN`; plus `ANTHROPIC_API_KEY` for the full-pipeline test). `tmp_path` isolation; `profiles.yml` rewritten from env at runtime, never committed. The shared gate lives in `tests/warehouse/_databricks_live.py` (created by the first live-test story, reused by the others — serialized to avoid a shared-helper merge collision).
- **DEC-008 — Reuse `tests/cli/_e2e_helpers.py` as-is** (`copy_fixture_to_tmp`, `read_prune_decisions`, `read_diff_report`) — warehouse-agnostic, no changes.
- **DEC-009 — Ops doc: consolidate into a dedicated `## Databricks adapter` section** mirroring the Snowflake section's subsections (install, profile keys/auth, dialect, connection-bound session/materialise, estimate, error taxonomy, known limitations, **cost guidance** — Free-Edition serverless 2X-Small + auto-stop + fair-use quota, offline harness, live-test invocation). No mkdocs nav change.
- **DEC-010 — README + CHANGELOG.** README warehouse-coverage prose moves Databricks from "remains on the roadmap" to "ships sampling + estimation (#224–#225); gated live cert via #226", with a Databricks subsection carrying the Free-Edition cost guardrails + doc pointers. CHANGELOG `[Unreleased]` entry naming the harness, the three live e2e, the ops docs, and any inline adapter fixes.
- **DEC-011 — No new error class, no exit-code-table churn, no new AST scan.** #226 is tests + fixtures + docs + small adapter bug-fixes that reuse existing typed errors. The confinement scan and parse-guards already exist.

---

## Phase 4 — Detailed Breakdown (Stories)

Ordering: fixture/seed → loads test → live tests (shared gate first, then the three) → docs → maintainer live cert → Quality Gate → Patterns & Memory.

**Project validation command** (every story's AC): `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`. Gated runs use `uv run pytest -m databricks --no-cov` (self-skips without env vars).

### US-001 — Databricks dbt-project fixture + manifest seed + generator + README
- **Traces to:** DEC-006, DEC-004
- **Description:** Create `tests/fixtures/databricks/` mirroring `tests/fixtures/snowflake/`: a one-model dbt project over `samples.nyctaxi.trips` with a hand-crafted, byte-stable `target/manifest.json`.
- **Files:** `tests/fixtures/databricks/{dbt_project.yml, profiles.yml, README.md, _gen_manifest.py, models/staging/sources.yml, models/staging/stg_nyctaxi_trips.sql, target/manifest.json}`
- **Acceptance:** Model node `database="samples"`, `schema="nyctaxi"`, `alias="trips"`, `unique_id="model.signalforge_test_nyctaxi.stg_nyctaxi_trips"`; columns are REAL `nyctaxi.trips` columns (`tpep_pickup_datetime`, `tpep_dropoff_datetime`, `trip_distance`, `fare_amount`, `pickup_zip`, `dropoff_zip`) each `data_type: null`; model SQL is SELECT-only over `{{ source(...) }}` with NO engineered literals/`COALESCE`; `README.md` documents the hand-crafted rationale + the maintainer-only `dbt parse` regen path. Validation green.
- **Done when:** the fixture dir exists with all seven files and the manifest is byte-stable from `_gen_manifest.py`.
- **Depends on:** none.

### US-002 — Loads-only test for the seed (default suite)
- **Traces to:** DEC-006
- **Description:** A default-suite (no-marker) test that `signalforge.manifest.load()`s the seed and asserts the Model-field contract.
- **Files:** `tests/warehouse/test_databricks_seed_loads.py`
- **TDD:** `model.name == "stg_nyctaxi_trips"`; `model.unique_id`/`package_name`/`original_file_path` correct; `model.database=="samples"`, `model.schema_=="nyctaxi"`, `model.alias=="trips"`, `resolve_this().qualified_name == "samples.nyctaxi.trips"`; `"tpep_pickup_datetime" in model.columns`; no engineered columns present; exactly one enabled model; `"COALESCE" not in model.raw_code`.
- **Acceptance:** runs + passes in the default suite (no env vars, no warehouse, no LLM). Validation green.
- **Done when:** the loads test passes under plain `uv run pytest`.
- **Depends on:** US-001.

### US-003 — `estimate_live` gated test + shared live-skip-gate helper
- **Traces to:** DEC-001, DEC-007
- **Description:** Create the shared `tests/warehouse/_databricks_live.py` (TRUTHY set, `_REQUIRED_CONN_VARS`, `_skip_reason()`, a live-adapter builder from env) and the first live test certifying `estimate_query_bytes` via live `EXPLAIN COST`.
- **Files:** `tests/warehouse/_databricks_live.py` (new), `tests/warehouse/test_databricks_estimate_live.py`
- **Acceptance:** `@pytest.mark.databricks`; self-skips with one distinct reason per missing env var; asserts `isinstance(bytes_est, int) and bytes_est > 0` for `SELECT * FROM samples.nyctaxi.trips`. Collected-but-skipped cleanly in the default suite; `uv run pytest -m databricks --no-cov` is green-or-skipped offline. Validation green.
- **Done when:** the helper + estimate_live test exist and self-skip cleanly without env vars.
- **Depends on:** none.

### US-004 — `prune_live` gated test (materialised) + thin `column_stats` live assert
- **Traces to:** DEC-001, DEC-003, DEC-004, DEC-007
- **Description:** Live warehouse+prune-only test: CREATE a per-run-unique engineered table in the writable `workspace` catalog with a literal-NOT-NULL column, run `prune_tests` with `scope="sample"` + `sample_strategy="materialised"`, assert ≥1 `always-passes` drop; plus a thin `column_stats` assertion certifying the #224 aggregate path; warm-up `COUNT_IF(<col> IS NULL)==0` guard.
- **Files:** `tests/warehouse/test_databricks_prune_live.py`
- **Acceptance:** `@pytest.mark.databricks` + shared `_skip_reason()`; engineered table name carries a per-run UUID suffix; asserts at least one `decision=="dropped" and reason=="always-passes"`; `column_stats` assertion checks `count`/`distinct`/`nulls` are ints and `data_type` is a non-empty str; teardown drops the engineered table in `finally`. Self-skips cleanly offline. Validation green.
- **Done when:** the test exists, self-skips without env vars, and exercises the materialised CTAS + column_stats paths when run live.
- **Depends on:** US-003 (shared gate helper).

### US-005 — `e2e_smoke` full-pipeline gated test (oneshot, schema-only)
- **Traces to:** DEC-001, DEC-002, DEC-003, DEC-007, DEC-008
- **Description:** Full `signalforge generate` e2e against read-only `samples.nyctaxi.trips`: copy the seed fixture to `tmp_path`, rewrite `profiles.yml` (type `databricks`, host/http_path/token/catalog/schema from env) + `signalforge.yml` (`safety: schema-only`, `prune.scope: sample`/`sample_strategy: oneshot`, `grade.total_budget_seconds: 600`), run `main([...])`, assert exit 0 + non-empty diff + ≥1 `always-passes` drop + `aggregate_complete is True` + no traceback.
- **Files:** `tests/cli/test_e2e_databricks_smoke.py`
- **Acceptance:** `@pytest.mark.databricks` + five-var gate (`SF_RUN_DATABRICKS`, `ANTHROPIC_API_KEY`, `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN`); reuses `copy_fixture_to_tmp`/`read_prune_decisions`/`read_diff_report`; warm-up null-guard on the always-pass column; `"Traceback" not in stderr`. Self-skips cleanly offline. Validation green.
- **Done when:** the test exists, self-skips without env vars, and drives the full pipeline when run live.
- **Depends on:** US-001, US-002, US-003 (shared gate helper).

### US-006 — Ops doc: consolidated `## Databricks adapter` section
- **Traces to:** DEC-009, DEC-003
- **Description:** Add a dedicated Databricks section to `docs/warehouse-adapter-ops.md` mirroring the Snowflake section's subsections; fold in the scattered #224/#225 notes.
- **Files:** `docs/warehouse-adapter-ops.md`
- **Acceptance:** section covers install (`pip install "signalforge-dbt[databricks]"`), profile keys + PAT auth + the env-var contract, the `DATABRICKS_DIALECT` (backtick quoting, `identifier_case='lower'`, `xxhash64` masked sampling), connection-bound session + `materialise_sample` colocation, `EXPLAIN COST` estimate + sentinel, the full error taxonomy, **`column_stats` is available** (NOT deferred — the Snowflake-divergence note), known limitations, **cost guidance** (Free-Edition serverless 2X-Small, auto-stop, fair-use quota), the offline harness, and the `uv run pytest -m databricks --no-cov` invocation. `uv run --only-group docs mkdocs build` succeeds; no nav change. Validation green.
- **Done when:** the section renders and the doc builds.
- **Depends on:** none.

### US-007 — README warehouse-coverage update + CHANGELOG entry
- **Traces to:** DEC-010
- **Description:** Update README's warehouse prose + add a Databricks subsection with cost guardrails + pointers; add the CHANGELOG `[Unreleased]` entry. (Combined to keep the two release-facing doc files in one bead — avoids a parallel-worker conflict on release docs.)
- **Files:** `README.md`, `CHANGELOG.md`
- **Acceptance:** README no longer says Databricks "remains on the roadmap" — reflects shipped sampling+estimation (#224–#225) + gated live cert (#226) + the Free-Edition cost guardrails; CHANGELOG `[Unreleased]` names the test harness, the three gated live e2e, the ops docs, and (placeholder for) any inline adapter fixes from the live pass. Validation green.
- **Done when:** both files updated and consistent with the shipped surface.
- **Depends on:** none.

### US-008 — Maintainer live certification pass + inline adapter fixes
- **Traces to:** DEC-005, DEC-004, all live-test stories
- **Description:** **Maintainer-run (not Ralph-completable — needs the live Free-Edition rig).** Export the env contract, run `uv run pytest -m databricks --no-cov` live, confirm/swap the always-pass column, and fix any #124-class adapter bug inline (session persistence across CTAS→`run_test_sql`, `to_json(struct(*))` decode, `EXPLAIN COST` format drift, complex-type `column_stats`, case-folding). Re-run green.
- **Files:** `src/signalforge/warehouse/adapters/{databricks.py,_databricks_client.py}` (only if live bugs surface), the live test files (column confirmation), CHANGELOG (fix entries).
- **Acceptance:** `uv run pytest -m databricks --no-cov` is green against the live rig; any adapter fix reuses existing typed errors (no new class, no exit-code/AST-scan churn); the `#226 live-cert item` docstring flags in the adapter are updated to "certified". Default suite stays green.
- **Done when:** the live suite passes end-to-end and inline fixes (if any) are committed.
- **Depends on:** US-002, US-004, US-005, US-006, US-007.

### US-009 — Quality Gate
- **Description:** Run the code reviewer 4× across the full changeset, fixing every real bug each pass; run CodeRabbit if available; project validation green after all fixes.
- **Acceptance:** four review passes complete; all real findings fixed; `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` green; offline `uv run pytest -m databricks --no-cov` self-skips cleanly.
- **Depends on:** US-008 (so the live fixes are in the reviewed changeset).

### US-010 — Patterns & Memory (priority 99)
- **Description:** Distil the durable conventions into `.claude/rules/warehouse-adapters.md` (Databricks live-cert: mark the #224/#225 ledger items certified or note survivors; the test-harness shape) and `testing-signal.md` if a new pattern emerged; update memory.
- **Acceptance:** rules + memory updated; `MEMORY.md` pointer added if a new memory file is written.
- **Depends on:** US-009.

---

## Phase 5 — Publish PR

_(pending)_

## Beads Manifest

_(pending devolve)_
