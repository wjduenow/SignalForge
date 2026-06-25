# Super Plan — #224: Databricks deterministic sampling + materialise_sample + get_row_count sizing

## Meta

- **Ticket:** [#224](https://github.com/wjduenow/SignalForge/issues/224) — *Databricks: deterministic sampling + materialise_sample + get_row_count sizing*
- **Epic:** [#219](https://github.com/wjduenow/SignalForge/issues/219) (Databricks adapter). Depends on #221 (skeleton), #222 (profile), #223 (prune compiler). Models on Snowflake #122 / #139 / #140.
- **Branch / worktree:** `feature/224-databricks-sampling`
- **Phase:** complete — implemented + reviewed on PR [#257](https://github.com/wjduenow/SignalForge/pull/257) (epic `bd_1-scaffolding-v4o`)
- **Sessions:** 1 (2026-06-25)

---

## Phase 1 — Discovery

### What / Why

`DatabricksAdapter` graduated through the skeleton (#221), profile parsing (#222), and prune-compiler cert (#223). #224 is the **sampling surface** — the adapter's first real warehouse I/O — so that `signalforge generate` / `prune-existing` can sample, size, and run candidate tests against a live Databricks SQL warehouse. This is the direct analogue of the Snowflake sampling work (#122 sampling, #139 projection-subquery sample shape, #140 `get_row_count` seam).

The product payoff: **`oneshot` and `materialised` sample-mode prune work end-to-end against the #220 live Free-Edition target** (certified in #226), so a Databricks operator gets the same draft → prune → grade → diff pipeline as BigQuery/Snowflake.

### Codebase findings (Scout)

- **Skeleton** `src/signalforge/warehouse/adapters/databricks.py`: `__init__` (captures `host`/`http_path`/`token`/`catalog`/`schema` + forward-compat OAuth), `__repr__` (shows only `host`/`http_path`/`catalog`), `dialect()` → `DATABRICKS_DIALECT`, lazy `_get_connection()` (builds via `make_real_client`, caches on `self._connection`, records `self._active_session`), `__enter__`/`__exit__` → `_cleanup_active_session()` (fail-soft, idempotent). `sample_rows` / `column_stats` / `run_test_sql` raise `NotImplementedError("…#219…")`. `materialise_sample` / `estimate_query_bytes` / `get_row_count` / `run_stats_query` inherit the ABC typed degrade.
- **Snowflake template** `adapters/snowflake.py`: `sample_rows` (→ `_resolve_sample_bucket` + `render_sample_select(..., order_by_hash=True)` + `_execute_to_dicts`), `materialise_sample` (`CREATE TEMPORARY TABLE <qualified_temp> AS <select_body>`, `_compute_run_id`, returns qualified `TableRef(project=table.project, dataset=table.dataset, name=temp)`), `run_test_sql` (`COUNT(*)` wrap; capture via `ARRAY_AGG(OBJECT_CONSTRUCT(*))` → `json.loads` when string), `_get_num_rows` (`INFORMATION_SCHEMA.TABLES.ROW_COUNT`), `get_row_count`, `_resolve_sample_bucket` (fail-loud sizing), `_quote` (fold-then-quote per component), `_execute`/`_execute_to_dicts`/`_rows_to_dicts`, connection-bound `_get_connection`/`_cleanup_active_session`.
- **Shared helpers**: `_sample_sql.render_sample_select(table_sql, *, dialect, sample_bucket, sample_size, extra_where=None, order_by_hash)` (reads `sample_row_hash_expr` / `sample_hash_in_projection` / `sample_hash_alias`); `_sample_id._compute_run_id(table, n, partition_filter)` (blake2b-8) / `_hash_session_id` / `_canonical_partition_filter`.
- **ABC** `base.py`: abstract `sample_rows` / `column_stats` / `run_test_sql`; degrade-default `materialise_sample` (→ `MaterialisationNotSupportedError`), `get_row_count` (→ `RowCountNotSupportedError`), `estimate_query_bytes`, `run_stats_query`. `from_profile` databricks branch wires every field.
- **`DATABRICKS_DIALECT`** (`models.py`): `quote_char='`'`, `identifier_case='lower'`, `supports_qualify=True`, `sample_row_hash_expr="(xxhash64(to_json(struct(*))) & 9223372036854775807)"`, `sample_hash_in_projection=False`, `quote_qualified_per_component=True`, `timestamp_literal_template="TIMESTAMP '{value}'"`, `date_literal_template="DATE '{value}'"`. **No dialect change needed** for #224.
- **`TableRef`** (`models.py`): `project: str | None` validated by `validate_project_id` (6–30 chars) — **blocks short Unity Catalog catalogs** like `main` (4 chars). `dataset` / `name` via `validate_identifier`.
- **Tests**: `tests/warehouse/test_databricks_stub.py`, `tests/warehouse/test_databricks_client_confinement.py`. Snowflake fakes: `tests/warehouse/_fake_snowflake.py::FakeSnowflakeConnection` (`expect_execute` / `assert_all_expectations_met` / `close_raises` / cursor `description`).

### Research findings (Databricks SQL semantics)

1. **Sampling clause position** — Databricks has **no** Snowflake-style `HASH(*)`/`002079` predicate restriction; `xxhash64(...)` and the masked `MOD(...)` work inline in `WHERE`/`ORDER BY`. Emit the **inline-predicate** shape (`sample_hash_in_projection=False`, already set). `SELECT * EXCEPT(col)` is also supported (not needed). *(high confidence)*
2. **`get_row_count`** — `DESCRIBE DETAIL` has **no top-level `numRows`** column (it lives in the `statistics` map, only after `ANALYZE TABLE COMPUTE STATISTICS`, Delta-only → commonly NULL/stale). `information_schema.tables` likewise has no `row_count`. **`SELECT COUNT(*)` is the reliable source** (metadata-only/cheap on Delta). *(high confidence)*
3. **`materialise_sample`** — `CREATE TEMPORARY TABLE … AS SELECT` **is** supported (session-scoped, true freeze, connection-bound session like Snowflake; auto-reaps on connection close, no writable-schema requirement). `CREATE OR REPLACE TEMPORARY VIEW` exists but re-executes per reference (not a freeze). *(medium-high — recent DBSQL feature; certify live in #226)*
4. **Failure capture** — `to_json(struct(*))` returns a JSON **string** → `json.loads` per row. Prefer per-row `to_json(struct(*))` + `LIMIT k` over `collect_list` (avoids `ARRAY<STRING>` marshalling assumptions). *(high on string; medium on array marshalling → #226)*
5. **`MOD` / `&`** — both `MOD(a,b)` and `a % b` valid; `&` bitwise-AND on BIGINT valid; the `& 9223372036854775807` mask yields a non-negative residue so `MOD(...) < 1` bucketing is sound (strictly better than `ABS()`, which leaves `ABS(Long.MIN_VALUE)` negative in non-ANSI mode — the PR #254 CodeRabbit catch).

### Scoping decisions (user, 2026-06-25)

- **materialise_sample** → implement true materialisation via `CREATE TEMPORARY TABLE` (not degrade, not view).
- **get_row_count** → `SELECT COUNT(*)`.
- **TableRef** → relax `project` validation to accept SQL identifiers.
- **Scope** → implement `sample_rows` + `get_row_count` + `run_test_sql` + `materialise_sample` + **`column_stats`** (ahead of Snowflake, which stubbed it); open a follow-up issue to decide Snowflake `column_stats` parity.

---

## Phase 2 — Architecture Review

| Area | Rating | Finding / resolution |
|---|---|---|
| Security — SQL injection | **pass** | Every identifier through `validate_identifier`; `validate_test_sql` on candidate SQL; relaxed `project` still identifier-shape-gated (no length-only loosening that admits non-identifiers). |
| Sampling correctness | **pass** | No `HASH(*)` predicate restriction → inline shape already correct; sign-bit mask sound; reuse `render_sample_select` (byte-parity with the #223 compiler CTE). |
| `get_row_count` reliability | **concern → resolved (DEC-003)** | `DESCRIBE DETAIL→numRows` doesn't exist; use `COUNT(*)`; route errors/uncountable through the existing fail-loud sizing. |
| `materialise_sample` integration | **concern → resolved (DEC-004)** | Mirror Snowflake's **qualified** temp-ref (`CREATE TEMPORARY TABLE <cat>.<sch>.<temp>`, return qualified `TableRef`) → reuses TableRef + compiler quoting verbatim, **zero model churn**. Qualified-temp acceptance + connector session-persistence are **#226 live-cert blockers** (documented fallback: bare-name or `CREATE OR REPLACE TABLE` + explicit `DROP`). |
| `TableRef` model | **concern → resolved (DEC-005)** | Relax `project` to accept a `validate_identifier`-valid string OR a GCP project id; keeps TableRef dialect-neutral; fixes the latent short-Snowflake-DB case too. |
| Observability / cleanup | **pass** | Connection-bound session; fail-soft `__exit__` WARNING (no manual command — temp tables session-local, like Snowflake); raw session id only in the failure WARNING. |
| Testing strategy | **concern → resolved (DEC-010)** | Fakes (behaviour) + ungated sqlglot `databricks` parse-guard (shape/syntax) + gated `@pytest.mark.databricks` live (validity, #226). The #124/#171 "snapshots pin invalid SQL byte-for-byte" trap is explicitly a #226 live-cert item, not a #224 gate. |
| Failure-row capture | **concern → resolved (DEC-007)** | `to_json(struct(*))` → JSON strings → `json.loads` per row; per-row capture (not `collect_list`); array marshalling is a #226 cert item. |
| `[databricks]` packaging | **pass** | Extra stays mirrored in the dev group (lightweight connector; per `python-build.md`); SDK import stays lazy in the shim. |

No open **blockers**. The two residual risks (qualified-temp-table validity, capture marshalling) are medium-confidence vendor-divergences deliberately deferred to the #226 live Free-Edition cert, with localized fallbacks documented.

---

## Phase 3 — Refinement Log (Decisions)

- **DEC-001 — Method scope.** Implement `sample_rows`, `get_row_count`, `run_test_sql`, `materialise_sample`, `column_stats`. `estimate_query_bytes` + `run_stats_query` keep the ABC degrade. Live cert deferred to #226. *(Mirrors Snowflake #122, plus `column_stats` which Snowflake stubbed.)*
- **DEC-002 — Sampling shape: inline predicate, no dialect change.** Reuse `render_sample_select(dialect=DATABRICKS_DIALECT, order_by_hash=True)`; `sample_hash_in_projection=False` is correct because Databricks accepts `xxhash64(...)`/`MOD(...)` in `WHERE`/`ORDER BY` (no `HASH(*)` restriction). Never hard-code the hash expression — read it from the dialect (byte-parity with the compiler CTE).
- **DEC-003 — `get_row_count` via `SELECT COUNT(*)`.** `DESCRIBE DETAIL` exposes no reliable top-level `numRows`; `information_schema.tables` has no `row_count`. `get_row_count` runs `SELECT COUNT(*) FROM <quoted>` (metadata-only/cheap on Delta) and returns the int; any `WarehouseError` (or a future non-countable case) returns `None`, routing through the shared `_resolve_sample_bucket` fail-loud sizing (unknown+no-filter → `UnknownTableSizeError`; ≥100M+no-filter → `SamplingRequiresPartitionFilterError`; unknown+filter → `bucket=1000`; else `max(num_rows//n, 1)`). Document the rejected `DESCRIBE DETAIL` approach + the Delta-cheap-count note.
- **DEC-004 — `materialise_sample` = qualified `CREATE TEMPORARY TABLE`, mirroring Snowflake #122.** `CREATE TEMPORARY TABLE <quoted cat.sch._sf_sample_<run_id>> AS <render_sample_select body>`; reuse `_compute_run_id`; return qualified `TableRef(project=table.project, dataset=table.dataset, name=temp)`. Connection-bound session (DEC-006); fail-soft `__exit__` WARNING with **no manual command** (temp tables session-local). **Qualified-temp-table acceptance + connector session persistence are #226 live-cert blockers**; documented fallback if rejected: bare-name temp table (needs a TableRef bare-ref convention) or `CREATE OR REPLACE TABLE` in the configured schema + explicit `DROP`. Shape certified now via sqlglot parse-guard + fakes.
- **DEC-005 — Relax `TableRef.project` to identifier-or-project.** Replace the strict `validate_project_id`-only gate with "valid `validate_identifier` string OR valid GCP project id" (new `validate_catalog_or_project` helper in `_sql_safety.py`). Keeps TableRef dialect-neutral (no new field); admits short Unity Catalog catalogs (`main`); fixes the latent short-Snowflake-DB case. The value remains identifier-shape-gated → no injection vector (it is always quoted downstream). Update the drift fixture/strict mirror if the validator surface is pinned.
- **DEC-006 — Connection-bound session state.** Store the connection object as `self._active_session` (the connection IS the session); `connection=` injection seam + lazy `_get_connection()` (already in the skeleton). All ops run on the one connection.
- **DEC-007 — `run_test_sql` capture: `to_json(struct(*))` → strings → `json.loads`.** No-capture: `SELECT COUNT(*) AS failures FROM (<sql>) AS t`. Capture: count + per-row `to_json(struct(*))` (LIMIT k) read back as strings and `json.loads`-ed (do **not** assume a single outer `json.loads` like Snowflake's VARIANT). Case-insensitive column resolution via `cursor.description`. Array/marshalling shape is a #226 cert item.
- **DEC-008 — `_quote` fold-then-quote.** `identifier_case='lower'` fold + backtick per component (`quote_qualified_per_component=True`), identical to the #223 compiler's folding, so CREATE-vs-REFERENCE never diverge (the #124 lesson). Reuse/share the compiler's fold helper where practical.
- **DEC-009 — Error mapping (minimal, extend the shim).** Extend `map_databricks_exception` (lazy SDK-error import, confined to `_databricks_client.py`): auth-flavoured → `WarehouseAuthError`; table-not-found → `TableNotFoundError`; invalid-identifier → `ColumnNotFoundError`; residual programming → `QuerySyntaxError`; else passthrough. No new `WarehouseError` subclass. Full taxonomy + live cert is #226. Confinement test stays green.
- **DEC-010 — Testing tiers.** (a) Hand-rolled `FakeDatabricksConnection` (cursor `description`, `expect_execute`, `assert_all_expectations_met`, `close_raises`) — behaviour assertions, no execution. (b) **Ungated** sqlglot `databricks`-dialect parse-guard over the emitted sample / materialise / test SQL (default suite; sqlglot is a base dep; mirrors #223 DEC-002). (c) Gated `@pytest.mark.databricks` live tests authored but cert deferred to #226. Plus determinism, sizing-branch, dict-row, cleanup-WARNING, repr-redaction, and the #116 `custom_sql {{ this }}` substitution tests.
- **DEC-011 — `column_stats` implemented now; Snowflake parity tracked.** Implement aggregate-only profiling for Databricks (ahead of Snowflake, which stubbed `column_stats`). Open a follow-up GitHub issue to decide whether Snowflake should backfill `column_stats` for parity.
- **DEC-012 — `[databricks]` extra stays dev-group-mirrored.** Per `python-build.md` (lightweight, not constraints-pinned) — contrast `[airflow]`. SDK import stays lazy in the shim.

---

## Phase 4 — Detailed Breakdown (Stories)

> Validation (every story): `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`. Gated `databricks` live tests run separately (`uv run pytest -m databricks --no-cov`) and are NOT required to pass in #224 (cert is #226).

### US-001 — Relax `TableRef.project` to accept Unity Catalog catalog names
- **Traces to:** DEC-005.
- **Description:** Add `validate_catalog_or_project(field, value)` to `_sql_safety.py` (valid `validate_identifier` string OR valid GCP project id); route `TableRef.__post_init__`'s `project` check through it. Keeps TableRef dialect-neutral.
- **Files:** `src/signalforge/warehouse/_sql_safety.py`, `src/signalforge/warehouse/models.py` (`TableRef.__post_init__`), `tests/warehouse/test_models.py` (or `test_sql_safety.py`), any drift fixture pinning the validator.
- **AC:** `TableRef(project="main", dataset="s", name="t")` constructs; `TableRef(project="my-gcp-proj-123", ...)` still constructs; an injection-shaped `project` (`"a;b"`, whitespace, quotes) still raises; `validate_project_id` callers unchanged. Validation passes.
- **Done when:** short catalog (`main`) + GCP project + latent short-DB cases pass; injection cases rejected.
- **Depends on:** none.
- **TDD:** parametrized accept/reject cases for `validate_catalog_or_project`; `TableRef` construction for `main` / `workspace` / GCP-id / injection.

### US-002 — Connection seam + `FakeDatabricksConnection` + fail-soft cleanup + error mapping
- **Traces to:** DEC-006, DEC-009, DEC-010, and the repr-redaction rule.
- **Description:** Add `FakeDatabricksConnection` (mirrors `FakeSnowflakeConnection`: cursor `description`, `expect_execute`, `assert_all_expectations_met`, `close_raises`). Confirm/extend the connection-bound `_get_connection` + `_active_session`. Implement fail-soft `__exit__`/`_cleanup_active_session` WARNING (no manual command; raw session id only in the failure WARNING; INFO uses `_hash_session_id`). Extend `map_databricks_exception` per DEC-009.
- **Files:** `tests/warehouse/_fake_databricks.py` (new), `src/signalforge/warehouse/adapters/databricks.py`, `src/signalforge/warehouse/adapters/_databricks_client.py`, `tests/warehouse/test_databricks_client_confinement.py`, `tests/warehouse/test_databricks_adapter.py` (new).
- **AC:** injected fake drives all subsequent stories; `__exit__` swallows a `close()` failure and emits one WARNING (no traceback; raw id present); idempotent second `__exit__` is a no-op; `__repr__` never shows `token`/`schema`/`client_secret`; error mapper returns the right typed errors; confinement scan green. Validation passes.
- **Done when:** fake + cleanup WARNING + error mapping land with tests.
- **Depends on:** none (parallel-safe with US-001).
- **TDD:** cleanup-success INFO (hashed id), cleanup-failure WARNING (raw id, no traceback), idempotent re-exit, repr-redaction, error-mapper cases.

### US-003 — `get_row_count` + sizing + `sample_rows`
- **Traces to:** DEC-002, DEC-003, DEC-008.
- **Description:** Implement `get_row_count` (`SELECT COUNT(*)` → int, errors → `None`), `_resolve_sample_bucket` (shared fail-loud sizing), `_quote`/`_execute`/`_execute_to_dicts`, and `sample_rows` (`render_sample_select(dialect=DATABRICKS_DIALECT, order_by_hash=True)`, inline shape). Add the ungated sqlglot parse-guard over the emitted sample SQL.
- **Files:** `src/signalforge/warehouse/adapters/databricks.py`, `tests/warehouse/test_databricks_adapter.py`, `tests/warehouse/test_databricks_sql_parse.py` (new, ungated sqlglot guard).
- **AC:** `sample_rows` emits inline-predicate SQL referencing `xxhash64` + masked `MOD(...) < 1`, byte-deterministic for fixed inputs; sizing branches fire (`UnknownTableSizeError`, `SamplingRequiresPartitionFilterError`, bucket math); `get_row_count` returns the count and `None` on error; dict rows shaped via `description`; sqlglot parses every emitted statement under `databricks`. Validation passes.
- **Done when:** sample + sizing + row-count land with determinism, sizing, dict-row, and parse-guard tests.
- **Depends on:** US-001, US-002.
- **TDD:** determinism (same inputs → identical SQL), each sizing branch, `get_row_count` happy + error→None, dict-row shaping, sqlglot parse.

### US-004 — `materialise_sample` + `run_test_sql`
- **Traces to:** DEC-004, DEC-007, DEC-008.
- **Description:** `materialise_sample` (`CREATE TEMPORARY TABLE <qualified temp> AS <sample body>`, `_compute_run_id`, qualified `TableRef` return, INFO with hashed session id). `run_test_sql` (COUNT wrap; capture via per-row `to_json(struct(*))` + LIMIT, `json.loads` each; case-insensitive column resolution). Extend the sqlglot parse-guard. Add the #116 `custom_sql {{ this }}` substitution test (source-name → quoted temp ref; assert no full-scan leakage).
- **Files:** `src/signalforge/warehouse/adapters/databricks.py`, `tests/warehouse/test_databricks_adapter.py`, `tests/warehouse/test_databricks_sql_parse.py`.
- **AC:** materialise emits a qualified `CREATE TEMPORARY TABLE … AS …`, returns the qualified temp ref, raises `MaterialisationFailedError` on cursor error; `run_test_sql` returns `TestResult` with correct `failure_count` and `json.loads`-ed sample failures; the `{{ this }}` test confirms substitution into the temp ref; sqlglot parses what it supports. Validation passes. *(Live validity of qualified temp tables + connector session persistence is a #226 item — note in the test docstring.)*
- **Done when:** materialise + run_test_sql + substitution + parse-guard land.
- **Depends on:** US-003.
- **TDD:** materialise SQL shape + run-id determinism + qualified ref; cursor-error → `MaterialisationFailedError`; `run_test_sql` pass/fail + capture `json.loads`; `custom_sql {{ this }}` substitution.

### US-005 — `column_stats` (aggregate profiling) + Snowflake-parity follow-up
- **Traces to:** DEC-001, DEC-011.
- **Description:** Implement `column_stats(table, column) -> ColumnStats` via aggregate SQL (null fraction, distinct count, min/max as the `ColumnStats` contract requires), reusing `_quote`/`_execute`. Author a GitHub follow-up issue: "Decide whether Snowflake should backfill `column_stats` for parity with Databricks (#224)."
- **Files:** `src/signalforge/warehouse/adapters/databricks.py`, `tests/warehouse/test_databricks_adapter.py`, `tests/warehouse/test_databricks_sql_parse.py`.
- **AC:** `column_stats` returns a populated `ColumnStats` against the fake; emitted SQL parses under sqlglot `databricks`; identifier folding correct; the follow-up issue exists and is linked. Validation passes.
- **Done when:** `column_stats` lands with tests; the Snowflake-parity issue is filed.
- **Depends on:** US-002 (needs the connection seam); independent of US-003/US-004.
- **TDD:** `column_stats` shape against the fake; sqlglot parse; folding.

### US-006 — Quality Gate
- **Traces to:** all implementation DECs.
- **Description:** Run the code reviewer 4× across the full changeset (fix every real bug each pass), run CodeRabbit if available, full validation green after fixes. Diverse reviewer angles: (1) SQL-injection/identifier safety, (2) adapter/warehouse conventions (fold-then-quote, dialect reuse, cleanup fail-soft), (3) test signal (determinism, sizing branches, parse-guard, planted-violation where relevant), (4) docs/UX + the #226 cert-item flags.
- **Depends on:** US-001…US-005.

### US-007 — Patterns & Memory
- **Traces to:** the whole ticket.
- **Description:** Add a "Databricks sampling (issue #224)" section to `.claude/rules/warehouse-adapters.md` (the 5 Snowflake conventions as applied to Databricks: connection-bound session, dialect-field reuse, `COUNT(*)` sizing, qualified temp-ref materialise, cleanup fail-soft WARNING + the #226 live-cert items: qualified-temp validity, connector session persistence, capture marshalling). Update `docs/warehouse-adapter-ops.md` (Databricks adapter section + `prune.sample_strategy` guidance + cost note). Update the relevant memory file. Note the TableRef relaxation (DEC-005) and the Snowflake `column_stats` parity follow-up.
- **Depends on:** US-006.

---

## Phase 5 — Publish PR

- Commit the plan doc, push `feature/224-databricks-sampling`, open a draft PR titled `#224: Databricks deterministic sampling + materialise_sample + get_row_count (plan)`.

## Beads Manifest

- **Epic:** `bd_1-scaffolding-v4o`
- **Tasks:**
  - `bd_1-scaffolding-v4o.1` — US-001 Relax TableRef.project *(ready)*
  - `bd_1-scaffolding-v4o.2` — US-002 Connection seam + fake + cleanup + errors *(ready)*
  - `bd_1-scaffolding-v4o.3` — US-003 get_row_count + sizing + sample_rows *(deps .1, .2)*
  - `bd_1-scaffolding-v4o.4` — US-004 materialise_sample + run_test_sql *(deps .3)*
  - `bd_1-scaffolding-v4o.5` — US-005 column_stats + Snowflake-parity follow-up *(deps .2)*
  - `bd_1-scaffolding-v4o.6` — US-006 Quality Gate *(deps .1–.5)*
  - `bd_1-scaffolding-v4o.7` — US-007 Patterns & Memory *(deps .6)*
- **Worktree:** `feature/224-databricks-sampling`
- **Snowflake column_stats parity follow-up:** [#258](https://github.com/wjduenow/SignalForge/issues/258)
- **Phase:** complete — all 7 stories merged on PR #257; live cert deferred to #226
