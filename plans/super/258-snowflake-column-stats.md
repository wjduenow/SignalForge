# 258 — Snowflake `column_stats` parity with Databricks

## Meta

- **Ticket:** https://github.com/wjduenow/SignalForge/issues/258
- **Phase:** devolved
- **PR:** https://github.com/wjduenow/SignalForge/pull/262
- **Branch:** `feature/258-snowflake-column-stats`
- **Worktree:** `../worktrees/SignalForge/258-snowflake-column-stats`
- **Base:** `dev`
- **Sessions:** 1 (2026-07-01)

## What / Why

Decision ticket, follow-up from #224. The Databricks adapter shipped `column_stats`
(aggregate-only profiling) in #224/#227; the Snowflake adapter (#122) deliberately
left it as `NotImplementedError`. This creates an asymmetry: Databricks supports
`safety: aggregate-only`, Snowflake does not.

**Decide:** backfill `column_stats` for Snowflake (parity), or document the gap as
intentional. Architectural Commitment #3 ("warehouse-agnostic by design") and the
epic-#118 intent both point toward parity.

## Discovery findings

### The `ColumnStats` contract (unchanged — the shape to produce)
`signalforge.warehouse.models.ColumnStats` — `frozen=True`, no `extra="forbid"`:
- `count: int` (non-null count), `distinct: int`, `nulls: int`
- `min: ColumnMinMax = None`, `max: ColumnMinMax = None` (`int|float|str|bool|datetime|date|None`)
- `data_type: str` (raw warehouse type string)

ABC signature (`base.py`): `column_stats(self, table, column) -> ColumnStats`. ABC
docstring promises context-manager batching (DEC-008: multiple columns of one table
flush as one query at first read; `RuntimeError` outside `with adapter:` per DEC-025).

**Single consumer:** `signalforge.safety.aggregate.aggregate_columns` — called inside
`with adapter:`, loops per column, stores each `ColumnStats` under the real column
name (or `None` for redacted). Feeds `LLMRequest.aggregates` → drafter's
`_render_aggregate_section`. Drives `safety.mode: aggregate-only`.

### Databricks impl (the ticket's suggested model)
- **One query per column** (does NOT honor the ABC batching contract — the connection-bound
  adapters diverge from BigQuery here). `typeof(col)` inline gives `data_type`.
- SQL: `COUNT(col)`, `COUNT(DISTINCT col)`, `COUNT_IF(col IS NULL)`, `MIN`/`MAX`,
  `MAX(typeof(col)) AS data_type` — single aggregate.
- Complex-type MIN/MAX skip (#227): orderable (`array`/`struct`/`binary`) → run with
  MIN/MAX, **post-process nulls** them; non-orderable (`map`/`variant`) → Spark rejects
  MIN/MAX at analysis time (`INVALID_ORDERING_TYPE`) → **catch `QuerySyntaxError`, retry
  reduced aggregate** (no MIN/MAX). Databricks needs this dance because `typeof()` is only
  known *after* the query runs.
- `try/finally` cursor close (fixed in PR #257).

### BigQuery impl (the original reference)
- Context-manager **batching** (pending dict + results dict, flush on first read).
- Reads schema **up front** → pre-filters MIN/MAX for complex types (`GEOGRAPHY`, `JSON`,
  `BYTES`, `ARRAY<>`, `STRUCT<>`, `RANGE<>`) — no failed-query retry needed.

### Snowflake current state
- `column_stats` → `NotImplementedError` (references epic #118).
- Connection-bound session: `_get_connection()` lazily builds + pins `_active_session`;
  `connection=` injection seam for tests.
- Cursor helpers: `_execute_scalar` closes in `try/finally` (correct); **`_execute`,
  `_execute_to_dicts`, `run_test_sql` LEAK** (no `finally` close). Documented latent gap
  in `warehouse-adapters.md`; convention is "every cursor-opening method closes in
  try/finally" (Databricks fixed its equivalents in #257).
- Fold-then-quote: `_quote(ref)` / `_fold(id)` — `identifier_case="upper"`, per-component
  quoting. No single-column `_quote_identifier` helper yet.
- `run_test_sql` decodes `ARRAY_AGG(OBJECT_CONSTRUCT(*))` VARIANT via `json.loads`;
  uppercase aliases resolved case-insensitively (`{k.lower(): v}`).
- **Snowflake has NO runtime `typeof()` for base columns.** Data type comes from
  `INFORMATION_SCHEMA.COLUMNS.DATA_TYPE` (catalog) — returns `NUMBER`, `TEXT`, `DATE`,
  `TIMESTAMP_NTZ`, `ARRAY`, `OBJECT`, `VARIANT`, `GEOGRAPHY`, `GEOMETRY`, `BINARY`, …
- `map_snowflake_exception`: `TableNotFoundError` / `ColumnNotFoundError` /
  `QuerySyntaxError` / `WarehouseAuthError`.

### Key design divergence surfaced by discovery
Because Snowflake exposes the column type via the catalog **before** the aggregate runs,
it can **pre-filter MIN/MAX (BigQuery-style)** rather than doing Databricks'
post-process-nulling + reduced-aggregate-retry. A literal "mirror Databricks" is possible
but arguably *worse* for Snowflake — the retry dance exists only because Databricks lacks
up-front type info. See DEC-002.

### Testing tiers (from #124 precedent)
- Offline: hand-fake behavior tests + `fakesnow` execution (INFORMATION_SCHEMA + basic
  aggregates execute) + `sqlglot`-parse for the sub-cases fakesnow can't run.
- **Live gated `@pytest.mark.snowflake` + `SF_RUN_SNOWFLAKE=1`** — the #124/#227 lesson:
  fakes/parse certify SHAPE, live certifies ACCEPTANCE. Complex-type MIN/MAX skip
  especially (the #227 map/variant analysis-time failure a fake can't disprove).

### Parity surfaces to flip (backfill case)
- `docs/warehouse-adapter-ops.md` (§ Known limitations lines ~732–735; § Snowflake ~935–938).
- `README.md` (lines 95, 489, 512 — the "one combination not yet implemented" language).
- `.claude/rules/warehouse-adapters.md` (the "left `column_stats` as `NotImplementedError`"
  statements + #258 references).
- `CHANGELOG.md` `[Unreleased]`.

## Scoping answers (session 1, 2026-07-01)

- **Backfill?** → **Backfill (implement)** `column_stats` for Snowflake, enabling
  `safety: aggregate-only`.
- **data_type source / complex-type skip?** → **Catalog pre-filter (BigQuery-style).**
  Query `INFORMATION_SCHEMA.COLUMNS.DATA_TYPE` up front; omit MIN/MAX from the aggregate
  for unorderable types. One lookup serves both the `data_type` field and the skip
  decision. NO Databricks-style retry-on-failure. (See DEC-002.)
- **Cursor-leak fix scope?** → **Fix all three** (`_execute`, `_execute_to_dicts`,
  `run_test_sql`) with `try/finally` in the same PR — documented convention, one-liners.
  (See DEC-005.)
- **Test tiers?** → **Offline + gated live (full).** Hand-fake + fakesnow/sqlglot offline
  PLUS gated `@pytest.mark.snowflake` + `SF_RUN_SNOWFLAKE=1` live cert (scalar +
  complex-type `column_stats` + aggregate-only `generate` smoke), maintainer-run before
  merge. (See DEC-006.)

## Architecture Review (session 1)

| Area | Rating | Finding |
|---|---|---|
| Security / SQL-injection | **pass** | Mirror `_get_num_rows` verbatim: `validate_identifier("column", column)` first; catalog WHERE embeds schema/table/column as string literals via `escape_bq_string_literal` (Snowflake uses backslash escaping — the BQ helper is correct, per the adapter's own `_render_partition_filter` note) with `UPPER(COLUMN_NAME)=UPPER('...')` case-insensitive match; `project=None` → unqualified `INFORMATION_SCHEMA`. `data_type` from catalog is used only for control-flow + the returned field + lazy-JSON logs — never re-interpolated into SQL. No injection vectors. |
| Correctness / Snowflake semantics | **concern → resolved** | (a) NULL count via **`COUNT(*) - COUNT(col)`** (standard, fakesnow-executable, portable). (b) **Decimal coercion** — Snowflake `NUMBER` → Python `Decimal`, NOT in `ColumnStats.min/max`'s `int\|float\|str\|bool\|datetime\|date` union → coerce `Decimal`→`float` before constructing (must-fix; see DEC-004). (c) Skip-set is a **conservative superset** — over-skip loses signal (harmless), under-skip raises; the gated live cert refines it (DEC-003). (d) The batching-vs-per-column axis is the one open decision → refinement Q. (e) `with adapter:` guard follows from the batching decision. |
| Performance | **concern → resolved by DEC-002** | Snowflake MUST catalog-lookup the type (no runtime `typeof`), so pure per-column = 1+2N queries. Mitigated by batching the catalog lookup per-table (all column types in one `INFORMATION_SCHEMA.COLUMNS` query) and — if full BQ-style batching is chosen — one aggregate for all columns (2 queries/table total). See refinement Q. |
| Data Model | **pass** | `ColumnStats` shape unchanged. Frozen, produced in-process, never read back from disk → **no drift detector** (mirror ingest-layer rule; document in test module docstring). |
| API Design | **pass** | ABC `column_stats(table, column) -> ColumnStats` signature unchanged. |
| Observability | **pass** | If batching: optional large-batch WARNING mirroring BQ's `_COLUMN_BATCH_WARN_AT` (DEC-023), lazy-JSON. No new log surfaces otherwise. Logger grep-gate already scans `warehouse/`. |
| Testing Strategy | **pass** | fakesnow executes `COUNT`/`COUNT(DISTINCT)`/`MIN`/`MAX`/`INFORMATION_SCHEMA` → default suite covers every branch (scalar, complex-skip, empty, error-map, folding, identifier-validation) via hand-fake + fakesnow. Gated `@pytest.mark.snowflake` live cert: scalar `column_stats` + complex-type + aggregate-only `generate` smoke against read-only TPCH (SELECT-only, no CTAS). No drift detector. |

**No blockers.** One open architecture decision (batching model) → refinement. Everything else resolved with the decisions below.

## Refinement Log — Decisions

- **DEC-001 — Backfill `column_stats` for Snowflake.** Implement it (enable `safety:
  aggregate-only` on Snowflake), rather than documenting the gap. *Rationale:*
  Architectural Commitment #3 (warehouse-agnostic by design), epic-#118 parity intent,
  Databricks precedent. Closes the last v0.2 Snowflake gap.

- **DEC-002 — Catalog pre-filter for `data_type` + MIN/MAX skip (BigQuery-style), NOT
  Databricks retry-on-failure.** Snowflake has no runtime `typeof()`; read the column type
  from `INFORMATION_SCHEMA.COLUMNS.DATA_TYPE` up front, then omit MIN/MAX from the
  aggregate for unorderable types. One lookup serves both the `data_type` field and the
  skip decision. *Rationale:* deterministic, no failed-query round-trip; Snowflake knows
  the type before running (unlike Databricks, whose retry dance exists only because Spark
  computes `typeof` at runtime).

- **DEC-003 — Conservative skip-set, validated live.** `_is_complex_snowflake_type`
  returns True for `{ARRAY, OBJECT, VARIANT, GEOGRAPHY, GEOMETRY}` (UPPER-compared against
  the catalog `DATA_TYPE`; strip any `<...>` parametric tail). BINARY treated as orderable
  (Snowflake MIN/MAX supports it). *Rationale:* with pre-filter, an UNDER-skipped
  unorderable column raises and (under full-batch) fails the whole batch; an OVER-skipped
  orderable column merely returns `min=max=None` (harmless — geo min/max is meaningless
  anyway). So bias to a superset. Mirrors BigQuery's `_is_complex_type` posture exactly;
  correctness rests on the set being complete, which the **gated live complex-type cert
  validates before merge** (the #227 lesson: only a live run settles which types the
  warehouse actually rejects). *Escape hatch if live/production surfaces a mis-classified
  type:* extend `_is_complex_snowflake_type` (preferred) or add a batch-level
  reduced-aggregate retry — do NOT reach for per-column retry.

- **DEC-004 — Coerce `Decimal` → `float` for min/max.** Snowflake `NUMBER` surfaces as
  Python `Decimal`, which is NOT in `ColumnStats.min/max`'s `int|float|str|bool|datetime|
  date|None` union. Coerce `isinstance(v, Decimal)` → `float(v)` before constructing
  `ColumnStats`. *Rationale:* satisfies the type contract; float precision is ample for a
  min/max shown in an LLM prompt. (BigQuery avoids this because its client pre-coerces
  NUMERIC→float.)

- **DEC-005 — NULL count via `COUNT(*) - COUNT(col)`.** *Rationale:* standard SQL,
  fakesnow/DuckDB-executable offline, portable; avoids leaning on `COUNT_IF`
  vendor-specifics.

- **DEC-006 — Full BigQuery-style batching.** `__enter__`/`__exit__` initialise
  `_column_stats_pending: dict[TableRef, list[str]]` + `_column_stats_results:
  dict[TableRef, dict[str, ColumnStats]]` (reset to `None` in `__exit__`'s `finally`,
  coexisting with the existing connection-session state). `column_stats` validates the
  identifier, raises `RuntimeError` if pending/results is `None` (**DEC-025 guard — free
  under this model**), returns a cache hit, else queues the column and calls
  `_flush_column_stats_batch(table)`. The flush runs **1** `INFORMATION_SCHEMA.COLUMNS`
  query for all the table's column types + **1** aggregate for every queued column (MIN/MAX
  gated per-column by DEC-003) = 2 queries/table. Honors the ABC DEC-008 contract; fewest
  Snowflake round-trips. Copy the `bigquery.py` `_flush_column_stats_batch` structure.
  Optional large-batch WARNING mirroring BQ's `_COLUMN_BATCH_WARN_AT` (DEC-023), lazy-JSON.

- **DEC-007 — Fix all three cursor leaks in the same PR.** Add `try/finally` cursor
  `close()` to `_execute`, `_execute_to_dicts`, and `run_test_sql` (only `_execute_scalar`
  currently closes). *Rationale:* documented convention ("every cursor-opening adapter
  method closes in try/finally"; Databricks fixed its equivalents in PR #257); one-liners;
  `column_stats` consumes `_execute` + `_execute_to_dicts` so the fix is on the critical
  path anyway.

- **DEC-008 — Offline covers all branches; gated live certifies acceptance.** Default
  suite: hand-fake unit tests (every branch — scalar / complex-skip / empty / error-map /
  folding / identifier-validation / Decimal-coercion) + fakesnow offline execution
  (real DuckDB runs the aggregate + `INFORMATION_SCHEMA`) + sqlglot-parse for any
  sub-case fakesnow can't run. Gated `@pytest.mark.snowflake` + `SF_RUN_SNOWFLAKE=1`:
  scalar + complex-type `column_stats` (against an engineered **writable** table, since
  read-only TPCH has no ARRAY/OBJECT/VARIANT column) + an aggregate-only `generate` smoke
  (against read-only TPCH — `column_stats` is SELECT-only, no CTAS). **No drift detector**
  (`ColumnStats` is frozen, produced in-process, never read back from disk — mirror the
  ingest-layer rule; document in the test module docstring). Gated tests run `--no-cov`, so
  the impl body must be fully covered by the default suite.

- **DEC-009 — Flip the parity surfaces + close #258.** `docs/warehouse-adapter-ops.md`
  (§ Known limitations ~732–735; § Snowflake ~935–938), `README.md` (lines ~95, ~489,
  ~512 — the "one combination not yet implemented" language), `.claude/rules/
  warehouse-adapters.md` (the "left `column_stats` as `NotImplementedError`" statements +
  the `#258` references), `CHANGELOG.md` `[Unreleased]`. The rules-file edit is
  **orchestrator-only-writable** (Ralph workers can't write `.claude/`), so it lands in the
  Patterns & Memory story; the worker-writable doc surfaces (ops doc / README / CHANGELOG)
  land in US-005.

## Detailed Breakdown

**Files (all under the worktree):**
`src/signalforge/warehouse/adapters/snowflake.py`,
`tests/warehouse/test_snowflake_adapter.py`,
`tests/warehouse/_fake_snowflake.py`,
`tests/warehouse/test_snowflake_adapter_fakesnow.py`,
`tests/warehouse/test_snowflake_columnstats_live.py` (new, gated),
`tests/cli/test_e2e_snowflake_smoke.py` (extend),
`docs/warehouse-adapter-ops.md`, `README.md`, `CHANGELOG.md`,
`.claude/rules/warehouse-adapters.md` (orchestrator-only, P&M story).

Validation command (every story): `uv sync --dev && uv run ruff check . && uv run ruff
format --check . && uv run pyright && uv run pytest`.

---

### US-001 — Cursor-leak fix (`_execute` / `_execute_to_dicts` / `run_test_sql`)
- **Traces to:** DEC-007.
- **Description:** Wrap each of the three cursor-opening methods' execute/fetch in
  `try/finally cursor.close()` (mirror `_execute_scalar` + the Databricks PR #257 shape).
  For `_execute_to_dicts`, the close must happen AFTER `_rows_to_dicts` reads
  `cursor.description` — outer `try/finally` (close) wrapping inner `try/except` (exception
  mapping), per the Databricks pattern.
- **TDD:** extend `FakeSnowflakeConnection`/`_FakeSnowflakeCursor` to track `cursors` +
  `closed` (mirror `_fake_databricks.py`); tests
  `test_execute_closes_cursor_on_{success,failure}`,
  `test_execute_to_dicts_closes_cursor_on_{success,failure}`,
  `test_run_test_sql_closes_cursor_on_{success,failure}`.
- **Done when:** all three methods close the cursor on success AND failure; new tests pass;
  validation green.
- **Depends on:** none.

### US-002 — `column_stats` full-batch implementation + unit tests
- **Traces to:** DEC-001, DEC-002, DEC-003, DEC-004, DEC-005, DEC-006.
- **Description:** Replace the `NotImplementedError`. Add `__enter__`/`__exit__` batching
  state; `column_stats` (validate → guard → cache → queue → flush); `_flush_column_stats_
  batch` (one `_get_column_types` catalog query mirroring `_get_num_rows` escaping + one
  aggregate for all queued columns, MIN/MAX gated); `_get_column_types(table) ->
  dict[str,str]`; module-level `_COMPLEX_SNOWFLAKE_TYPES` frozenset +
  `_is_complex_snowflake_type`; `COUNT(*)-COUNT(col)` null count; `Decimal`→`float`
  coercion; case-insensitive result-alias resolution (`{k.lower(): v}`); optional
  large-batch WARNING (lazy-JSON). Copy the `bigquery.py` flush structure.
- **TDD (hand-fake, all default-suite for coverage):** `returns_populated_columnstats`,
  `carries_through_string_and_none_minmax`, `query_shape_and_folding` (catalog + aggregate
  SQL shape, `"`-quoting, UPPER-fold), `resolves_aliases_case_insensitively`,
  `none/absent_data_type_coerces_to_empty_string`, `validates_column_identifier` (DEC-013,
  no query issued), `two_part_table_quoting` (`project=None` → unqualified
  `INFORMATION_SCHEMA`), `programming_error_maps_to_query_syntax_error`,
  `column_not_found_maps_with_context`, `complex_type_nulls_min_max` (parametrized
  ARRAY/OBJECT/VARIANT/GEOGRAPHY/GEOMETRY), `scalar_type_preserves_min_max`,
  `empty_table` (count=0, min/max NULL), `decimal_min_max_coerced_to_float`,
  `batches_all_queued_columns_in_one_flush`, `raises_runtime_error_outside_with_block`
  (DEC-025 guard).
- **Done when:** `column_stats` returns a populated `ColumnStats`; every branch covered by
  the default suite; validation green.
- **Depends on:** US-001.

### US-003 — fakesnow offline execution tests
- **Traces to:** DEC-008.
- **Description:** Add fakesnow-executed cases to `test_snowflake_adapter_fakesnow.py`
  driving the real adapter through DuckDB: scalar aggregate + `INFORMATION_SCHEMA.COLUMNS`
  round-trip execute; `COUNT(DISTINCT)`/`MIN`/`MAX`/`COUNT(*)-COUNT(col)` execute end-to-end
  on engineered rows (rule-semantic assertions, not value pins). sqlglot-parse fallback for
  any sub-case fakesnow can't run (comment the gap inline). Add the "no drift detector —
  frozen, in-process, never read back" note to the test module docstring.
- **Done when:** fakesnow cases pass under `uv run pytest -m snowflake --no-cov`; parse
  fallbacks (if any) documented; validation green.
- **Depends on:** US-002.

### US-004 — Gated live certification
- **Traces to:** DEC-008, DEC-003 (validates the skip-set).
- **Description:** New `tests/warehouse/test_snowflake_columnstats_live.py`
  (`@pytest.mark.snowflake`, `SF_RUN_SNOWFLAKE=1` + conn-var `_skip_reason()` gating,
  `--no-cov`): create an engineered **writable** table (mirror `test_snowflake_prune_live.py`)
  with scalar + ARRAY/OBJECT/VARIANT columns; assert scalar `column_stats` populates
  count/distinct/nulls/min/max/data_type and complex-type columns return `min=max=None`
  without error (**this is the #227-style skip-set validation**). Extend
  `test_e2e_snowflake_smoke.py` with an aggregate-only `generate` smoke against read-only
  TPCH (`safety.mode: aggregate-only`, `prune.scope: full`) asserting exit 0 + diff sidecar
  + no traceback.
- **Done when:** the gated live suite passes when run by the maintainer with creds;
  self-skips cleanly without them; default suite unaffected.
- **Depends on:** US-002.

### US-005 — Parity docs (ops doc + README + CHANGELOG)
- **Traces to:** DEC-009.
- **Description:** Flip `docs/warehouse-adapter-ops.md` (§ Known limitations ~732–735 and
  § Snowflake ~935–938) from "unsupported" to "supported (issue #258)"; update `README.md`
  lines ~95, ~489, ~512 (drop the "one combination not yet implemented" Snowflake carve-out
  — `aggregate-only` now works on Snowflake too); add a `CHANGELOG.md` `[Unreleased]` entry.
  (The `.claude/rules/warehouse-adapters.md` update is orchestrator-only → P&M story.)
- **Done when:** no doc/README/CHANGELOG surface still states Snowflake `column_stats` is
  unimplemented; validation green (skill-parity gate unaffected — no CLI change).
- **Depends on:** US-002.

### US-006 — Quality Gate — code review ×4 + CodeRabbit
- **Description:** Run the code reviewer 4× across the full changeset, fixing every real
  bug each pass; run CodeRabbit; ensure validation passes after all fixes. Diverse angles:
  correctness (SQL shape, Decimal coercion, batch-fails-on-bad-column interaction),
  Snowflake semantics (skip-set completeness), tests (branch coverage + gated-live shape),
  docs/parity.
- **Done when:** 4 review passes complete, all real findings fixed, validation green.
- **Depends on:** US-003, US-004, US-005.

### US-007 — Patterns & Memory (priority 99)
- **Description:** Update `.claude/rules/warehouse-adapters.md` — flip the "Snowflake left
  `column_stats` as `NotImplementedError`" statements + `#258` references to "shipped
  (#258)"; record the durable lessons (catalog pre-filter vs Databricks retry;
  full-batch-fails-on-one-bad-column → conservative skip-set validated live; `Decimal`→
  `float` coercion; `COUNT(*)-COUNT(col)` null count; the #258 close-out of the Snowflake
  aggregate-only gap). Add a memory file if a cross-cutting lesson warrants it. **Done by
  the orchestrator** (Ralph workers can't write `.claude/`).
- **Done when:** rules file reflects shipped parity; validation green.
- **Depends on:** US-006.




## Beads Manifest (devolved, session 1)

- **Epic:** `bd_1-scaffolding-om0`
- **Tasks:**
  - `bd_1-scaffolding-om0.1` — US-001 Cursor-leak fix (ready)
  - `bd_1-scaffolding-om0.2` — US-002 column_stats full-batch impl + unit tests (dep .1)
  - `bd_1-scaffolding-om0.3` — US-003 fakesnow offline execution tests (dep .2)
  - `bd_1-scaffolding-om0.4` — US-004 Gated live certification (dep .2)
  - `bd_1-scaffolding-om0.5` — US-005 Parity docs — ops/README/CHANGELOG (dep .2)
  - `bd_1-scaffolding-om0.6` — US-006 Quality Gate (dep .3, .4, .5)
  - `bd_1-scaffolding-om0.7` — US-007 Patterns & Memory — orchestrator-run (dep .6)
- **Worktree:** `../worktrees/SignalForge/258-snowflake-column-stats`
- **PR:** https://github.com/wjduenow/SignalForge/pull/262
