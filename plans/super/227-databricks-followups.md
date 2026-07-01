# Super Plan — #227: Databricks known-quirk follow-ups (reconciliation + residual disposition)

## Meta

- **Ticket:** [#227](https://github.com/wjduenow/SignalForge/issues/227) — Databricks: known-quirk follow-ups (`column_stats` + live-surfaced edges)
- **Epic:** [#219](https://github.com/wjduenow/SignalForge/issues/219) — Databricks warehouse adapter (this is the final child, #226 → #227)
- **Branch:** `feature/227-databricks-followups`
- **Worktree:** `../worktrees/SignalForge/227-databricks-followups`
- **Phase:** published
- **Sessions:** 1 (2026-06-30)

---

## Phase 1 — Discovery

### What / Why / Who

#227 is the **tracking parent for the residual set** deliberately deferred from the first Databricks ship. Its acceptance is a *disposition contract*, not a feature build:

> Each deferred item either implemented or split into its own tracked issue; **nothing silently dropped.**

**The load-bearing discovery: #227's four nominal scope items were almost entirely absorbed by #224 / #225 / #226 as they landed.** This is the same "cannibalised-by-earlier-children" shape #226's own plan noted. Reconciling the ticket text against the shipped code:

| #227 nominal scope item | Actual disposition |
|---|---|
| Implement `column_stats` (`aggregate-only`) for Databricks | ✅ **Shipped in #224** (DEC-011), AHEAD of Snowflake. Scalar path **live-certified in #226**. |
| Address dialect quirks the #226 live run surfaces | ✅ **Done in #226** — the live pass found *three* real bugs (qualified `CREATE TEMPORARY TABLE` → `CREATE OR REPLACE TABLE` + DROP; `struct(*)` rejected in a Sort node → projection-subquery sample shape; cross-vendor `QuerySyntaxError` message → vendor-neutral), all fixed inline + re-certified live. |
| Graduate `materialise_sample` to a real impl | ✅ **Shipped in #224** (qualified `CREATE OR REPLACE TABLE`), corrected by #226. Live-certified. |
| Graduate `estimate_query_bytes` to `EXPLAIN COST` | ✅ **Shipped in #225**. Live-certified in #226. |

So the "big rocks" are **all done**. What remains is a *residual set* that #226's close already named canonically (CHANGELOG `[Unreleased]`: *"The only residual shape-only paths are complex-type `column_stats` MIN/MAX and the `to_json(struct(*))` failing-row capture branch"*), plus two items the adapter docstrings flag as follow-ups.

### The genuine residual set (what #227 must dispose of)

| ID | Residual | Current state | Size |
|---|---|---|---|
| **R1** | `column_stats` **complex-type MIN/MAX divergence** — Databricks emits `MIN`/`MAX` unconditionally; BigQuery skips them (→`None`) for ARRAY/STRUCT/MAP/JSON/BINARY/GEOGRAPHY per the `ColumnStats` DEC-016 contract. Databricks derives `data_type` inline via `typeof()` in the same aggregate, so it can't know the type before building the query. | Shipped, documented as follow-up in the `column_stats` docstring + CHANGELOG. Scalar path is the live-certified surface; complex-type MIN/MAX is shape-only. | **Small** — a pure post-process that nulls `min`/`max` when the returned `data_type` is complex (no extra round-trip); fake-testable. |
| **R2** | `run_test_sql` **`to_json(struct(*))` failing-row capture branch** not live-exercised. #226's live tests engineered *always-pass* candidates (0 failing rows) so the capture branch never fired live. | Shipped, `json.loads`-per-row decode, fake-tested offline. Named as the residual shape-only path in the CHANGELOG. | **Small code / needs live rig** — a gated live test with an engineered *failing* candidate + assert captured rows decode. Live run is a maintainer survivor. |
| **R3** | `_render_partition_filter` **`str`-valued escape** relies on Spark's default `escapedStringLiterals=false`. Uses `escape_bq_string_literal` (backslash escaping). A `str`-typed partition filter is rare (partition cols are near-always date/timestamp). | Shipped; datetime/date branches live-adjacent, `str` branch shape-only. Not named in CHANGELOG/ops — only in `.claude/rules/warehouse-adapters.md`. | **Doc-only** — document the assumption as a known limitation; no code change warranted. |
| **R4** | `run_stats_query` **inherits the ABC typed degrade** (`StatsQueryNotSupportedError`) → `row_count_anomaly_by_period` routes to `kept-without-evidence` on Databricks. | Deliberately out of scope for #225; adapter docstring says "out of scope, pending its own ticket". No Databricks `run_stats_query` test. | **Large** — mirrors #171 (the anomaly primitive) across a warehouse: two-query stats/violation split, Spark result-row shaping, DOW handling, live cert. A full ticket in its own right. |

### Sibling follow-up already filed (NOT #227's job)

- **[#258](https://github.com/wjduenow/SignalForge/issues/258)** — decide whether Snowflake backfills `column_stats` for parity with Databricks. Already tracked; #227 does not touch it.

### Key files

- `src/signalforge/warehouse/adapters/databricks.py` — `column_stats` (R1), `run_test_sql` capture (R2), `_render_partition_filter` (R3), `run_stats_query` inherited degrade (R4).
- `tests/warehouse/test_databricks_adapter.py` — offline fake-driven tests (R1 post-process test lands here).
- `tests/warehouse/test_databricks_prune_live.py` — gated `@pytest.mark.databricks` live suite (R2 capture-live test lands here).
- `CHANGELOG.md` `[Unreleased]`, `docs/warehouse-adapter-ops.md` § Databricks — residual-set reconciliation (R1/R3 disposition, canonical known-limitations statement).
- Epic **#219** checklist — tick #220–#227; **#227** itself closed on completion.

### Convention constraints (`.claude/rules/`)

- **`warehouse-adapters.md`** — dialect-driven never name-branched; one-shim-per-vendor; graceful degrade via `*NotSupportedError`; the `ColumnStats` complex-type skip contract (DEC-016) is what R1 aligns to; gated-live-cert is the real merge gate (snapshots/parse certify shape, not acceptance — the #226 lesson).
- **`testing-signal.md`** — no `assert True`; gated live tests carry marker + runtime `_skip_reason()`; engineered determinism for the R2 failing-row assertion; `--no-cov` for marker runs.
- **`cli-layer.md`** — no exit-code-table churn (R1/R2/R3 add no error class; R4, if filed, reuses the existing degrade); scan-7 count untouched.
- **`docs-publishing.md`** — ops-doc changes are user-facing; no `nav:` change (editing an existing section).
- **`python-build.md`** — `[databricks]` extra unchanged; no dependency churn.

### Scoping answers (user, 2026-06-30)

The user chose **maximal scope — implement everything in #227, no deferral to child issues:**

- **R1** → **Implement now.** Align to the `ColumnStats` complex-type skip contract via a pure post-process (null min/max when the returned `data_type` is complex).
- **R2** → **Add gated live test now.** Engineered *failing* candidate that fires the `to_json(struct(*))` capture branch; the live run is a maintainer survivor.
- **R4** → **Implement now in #227.** Override `run_stats_query` for Databricks + certify the anomaly Spark SQL live.
- **R3** → **Code fix (not doc-only)** + **#227 closes epic #219.**

Sizing confirmed during discovery:
- R1 — small, pure post-process, fake-testable, no extra round-trip.
- R2 — small code (a gated live test), live run is a maintainer survivor.
- R4 — **adapter method is thin** (mirror BigQuery `run_stats_query`: `validate_test_sql` + `_execute_to_dicts`; the 16 Databricks anomaly SQL fixtures already exist from #223 and the engine's `_parse_anomaly_stats` is dialect-agnostic). **The real cost is live cert** — the anomaly Spark SQL has only been sqlglot-parsed, never executed (the #226 failure class).
- R3 — small; exact escape rule pinned in refinement (a str-valued partition filter is rare; the fix is bounded to `_render_partition_filter`'s `else` branch + test).

---

## Phase 2 — Architecture Review

Focused review (adapter-internal work following well-established precedent — not a 6-subagent fan-out). Axes: contract-alignment, testing/live-cert risk, convention compliance, scope-boundary.

| Area | Rating | Finding |
|---|---|---|
| **Contract alignment (R1)** | pass | Nulling min/max on complex `data_type` brings Databricks to the `ColumnStats` DEC-016 contract BigQuery already honours. Pure post-process on the returned dict — no extra query, no round-trip. Complex-type prefix set must match BigQuery's (ARRAY/STRUCT/MAP/JSON/BINARY/GEOGRAPHY). |
| **Live-cert risk (R4)** | **concern** | The anomaly Spark SQL (`PERCENTILE_CONT … WITHIN GROUP`, `DATE_TRUNC`, `DAYOFWEEK`, the masked `xxhash64` interplay) has been **sqlglot-parsed only** (#223), never executed on Spark. #226 proved three first-try-plausible constructs passed the offline tier and failed live. R4 **must** budget a gated live anomaly cert; the offline fake test certifies the adapter wiring, NOT that Spark accepts the SQL. Result-row type coercion (Decimal vs float, period-key type) is the likely live-only failure surface. |
| **Live-cert risk (R2)** | concern | Same class — the capture branch is fake-tested; live cert needs an *engineered failing* candidate (0-failing always-pass tests never fire it). Engineered determinism: a `custom_sql`/`not_null` guaranteed to return ≥1 failing row on the fixture. |
| **Testing strategy** | pass | R1 offline-fake-testable in `test_databricks_adapter.py`. R2/R4-live gated `@pytest.mark.databricks` + runtime `_skip_reason()`, `--no-cov`. R4 adapter override gets an offline fake test (canned dict rows) + the gated live cert. All follow `testing-signal.md`. |
| **Convention compliance** | pass | No new error class (R1/R2/R3 add none; R4 reuses the existing degrade path it replaces). No exit-code-table / scan-7 churn. One-shim rule untouched (no new SDK surface). Dialect stays un-name-branched. `[databricks]` extra unchanged. |
| **R3 code-fix form** | **concern** | The "code fix" form is non-obvious: a session `SET spark.sql.parser.escapedStringLiterals` is intrusive/global; changing the shared `escape_bq_string_literal` risks the other adapters. Least-risk form = a Databricks-local str-literal escape (quote-doubling `''` + backslash handling correct for Spark's default) confined to `_render_partition_filter`'s `else` branch. Pin the exact rule in refinement (DEC). |
| **Scope boundary** | pass | #258 (Snowflake `column_stats` parity) stays out. R4 is the one item that could balloon — but the adapter method is thin and the SQL already exists, so it's contained. Epic #219 closure is bookkeeping. |
| **Observability / security** | pass | No new logging surfaces beyond the existing adapter WARNING/INFO. No PII/injection change — identifiers still `validate_identifier`'d; stats SQL `validate_test_sql`'d (defence-in-depth, mirrors BigQuery). |

**Blockers:** none. **Concerns → refinement:** (1) R4 live-cert scope + result-row coercion; (2) R2 engineered-failing-candidate design; (3) R3 exact escape rule.

## Phase 3 — Refinement Log (Decisions)

- **DEC-001 (R1 — `column_stats` complex-type MIN/MAX skip).** Align Databricks to the `ColumnStats` complex-type contract (BigQuery DEC-016) via a **pure post-process, no extra round-trip.** The single aggregate already fetches `data_type = MAX(typeof(col))`; after building the `ColumnStats`, null `min_value`/`max_value` when `data_type` is complex. Mirror BigQuery's shape with a Databricks-appropriate set: parametric prefixes `{array, struct, map}` + scalar complex `{binary, variant}` (Spark `typeof()` emits lowercase DDL; Spark has no GEOGRAPHY, and JSON is `string`). A small `_is_complex_spark_type(type_str)` helper + fake test with a `struct<…>` / `array<…>`-typed column. Scalar path is unchanged (already live-certified #226).

- **DEC-002 (R4 — `run_stats_query` override).** Override the ABC degrade with a thin method mirroring `BigQueryAdapter.run_stats_query`: `validate_test_sql(sql)` (defence-in-depth; the compiler already validated) → `_execute_to_dicts(sql)` → return `tuple(rows)`. **No wrap** — the stats SQL is the verbatim SELECT, not a failing-rows test. Reuses the connection-bound session. The 16 Databricks anomaly SQL fixtures already exist (#223); the engine's `_parse_anomaly_stats` is dialect-agnostic and coerces via `float()`/`int()` (Decimal-safe, so no adapter-side coercion needed). Offline fake test: canned dict rows returned verbatim + `validate_test_sql` rejects a bad SQL. This replaces the inherited `StatsQueryNotSupportedError` degrade for Databricks.

- **DEC-003 (R4 — live anomaly cert).** The anomaly Spark SQL (`PERCENTILE_CONT … WITHIN GROUP`, `DATE_TRUNC`, `DAYOFWEEK`, masked `xxhash64`) has been **sqlglot-parsed only** (#223) — never executed on Spark. Per the #226 lesson (three plausible constructs passed the offline tier and failed live), R4 ships a **gated `@pytest.mark.databricks` anomaly cert** that runs a `row_count_anomaly_by_period` prune end-to-end against the live rig and asserts a real decision is produced (NOT `kept-without-evidence` from the degrade). The offline fake test certifies adapter wiring; **only the live run certifies Spark accepts the SQL.** Result-row coercion (Decimal/period-key) is the likely live-only failure surface; fix inline + re-cert if it surfaces (the #226 pattern). The live run is a **maintainer survivor** (rig unavailable to Ralph) — tracked like #247 if not run before merge.

- **DEC-004 (R2 — live capture cert).** The `to_json(struct(*))` capture branch never fired live (#226's candidates were always-pass → 0 failing rows). Ship a **gated `@pytest.mark.databricks` capture cert** with an **engineered failing candidate** (guaranteed ≥1 failing row — e.g. a `custom_sql` returning a constant row, or `not_null` on a known-NULL column) + `capture_failures > 0`, asserting `TestResult.failure_rows` decode from the `to_json` branch. Maintainer survivor, same as DEC-003.

- **DEC-005 (R3 — Spark-correct str-literal partition escape).** Replace the `escape_bq_string_literal` reuse in `_render_partition_filter`'s `str` branch with a Databricks-local `_escape_spark_string_literal(value)`: double `'` → `''` (Spark-idiomatic; safe for the quote char in both `escapedStringLiterals` modes) and `\` → `\\` (correct under Spark's default `escapedStringLiterals=false`). **No `SET` conf** (intrusive/session-global) and **no change to the shared BQ helper** (would touch other adapters). Confined to the adapter + a unit test with a value containing `'` and `\`. This is the least-risk code form honouring the "code fix, not doc-only" choice; the escaping was already correct for the default mode, so the fix decouples + explicitly tests Databricks escaping rather than repairing a live bug.

- **DEC-006 (bookkeeping — the acceptance bar).** #227 reconciles the residual set **canonically** so nothing is silently dropped: CHANGELOG `[Unreleased]` + `docs/warehouse-adapter-ops.md § Databricks` updated (R1 now contract-aligned; R3 fixed; R2/R4 offline-tested + live-certified-or-survivor-tracked; the "residual shape-only paths" note is retired for R1/R2). Tick epic **#219**'s checklist (#220–#227) and **close epic #219** — #227 is the epic-closer. #258 (Snowflake `column_stats` parity) stays out of scope (separately tracked).

## Phase 4 — Detailed Breakdown (Stories)

Ordering: adapter methods (serialized — all edit `databricks.py`, so same-file merge collisions per the Ralph serialize-same-file lesson) → gated live tests → docs/epic → maintainer live cert → QG → P&M.

### US-001 — R1: `column_stats` complex-type MIN/MAX skip
- **Description:** Post-process `column_stats` to null `min_value`/`max_value` when the returned `data_type` is a complex Spark type, aligning to the `ColumnStats` DEC-016 contract.
- **Traces to:** DEC-001.
- **Files:** `src/signalforge/warehouse/adapters/databricks.py` (`column_stats` + a `_is_complex_spark_type` helper + set constants `{array, struct, map}` prefixes / `{binary, variant}` scalar); `tests/warehouse/test_databricks_adapter.py`.
- **TDD:** fake returns `data_type="struct<a:int>"` → assert `min_value is None and max_value is None`; `data_type="array<string>"` → same; scalar `data_type="bigint"` → min/max preserved; empty-table `data_type=""` → min/max preserved (unchanged).
- **AC:** complex-typed columns return `None` min/max; scalar path byte-unchanged; validation command green.
- **Done when:** the four TDD cases pass; scalar live-cert behaviour (#226) untouched.
- **Depends on:** none.

### US-002 — R3: Spark-correct str-literal partition escape
- **Description:** Replace the shared BQ escape in `_render_partition_filter`'s `str` branch with a Databricks-local Spark-correct escape.
- **Traces to:** DEC-005.
- **Files:** `src/signalforge/warehouse/adapters/databricks.py` (`_escape_spark_string_literal` + `_render_partition_filter` else branch); `tests/warehouse/test_databricks_adapter.py`.
- **TDD:** value `"a'b"` → `'a''b'`; value `"a\\b"` → `'a\\\\b'`; datetime/date branches unchanged (byte-equal to current); a str-partition-filter renders a valid single-quoted literal.
- **AC:** str partition values escape via the Spark-local helper; datetime/date branches unchanged; no change to `escape_bq_string_literal`; validation command green.
- **Done when:** TDD cases pass; `sqlglot` `databricks`-parse of a str-partition sample still parses.
- **Depends on:** US-001 (same file — serialize).

### US-003 — R4: `run_stats_query` adapter override
- **Description:** Override the ABC `StatsQueryNotSupportedError` degrade with a thin `validate_test_sql` + `_execute_to_dicts` method mirroring BigQuery, so `row_count_anomaly_by_period` evaluates on Databricks instead of degrading.
- **Traces to:** DEC-002.
- **Files:** `src/signalforge/warehouse/adapters/databricks.py` (`run_stats_query` override + docstring header update: drop the "inherits the ABC typed degrade" line); `tests/warehouse/test_databricks_adapter.py`.
- **TDD:** fake with canned dict rows → `run_stats_query(sql)` returns them as a tuple verbatim; a bad SQL (`;`/`--`) → `validate_test_sql` rejects before execution; cursor closed on success + failure (mirror the existing `_execute_to_dicts` cursor-close pins).
- **AC:** override returns the SELECT's rows as `tuple[dict, ...]`; no longer raises `StatsQueryNotSupportedError`; validation command green.
- **Done when:** offline fake test passes; the docstring header + ops-doc "only `run_stats_query` degrades" line are updated in lockstep (the ops-doc edit lands in US-005).
- **Depends on:** US-002 (same file — serialize).

### US-004 — R2 + R4: gated live certification tests
- **Description:** Add two `@pytest.mark.databricks` gated tests: (a) anomaly-prune end-to-end asserting a real decision (not the degrade), (b) failing-candidate capture asserting `to_json` rows decode. Both self-skip without the rig.
- **Traces to:** DEC-003, DEC-004.
- **Files:** `tests/warehouse/test_databricks_prune_live.py` (or a sibling gated module); reuse the existing live-skip-gate helper + fixture.
- **TDD:** anomaly test — engineered history over `samples.nyctaxi.trips` (or the seed) with a period band, assert `PruneDecision` is a real `kept`/`dropped`, `stats` populated; capture test — engineered failing candidate (`capture_failures=k`), assert `len(result.failure_rows) >= 1` and each decodes.
- **AC:** both tests carry marker + runtime `_skip_reason()`; deselected by default; run under `-m databricks --no-cov`; self-skip cleanly when env unset.
- **Done when:** tests exist and self-skip in CI/Ralph (no rig); ready for the maintainer live pass.
- **Depends on:** US-003.

### US-005 — Docs + epic reconciliation
- **Description:** Reconcile the residual set canonically and close the epic.
- **Traces to:** DEC-006.
- **Files:** `CHANGELOG.md` `[Unreleased]` (retire the R1/R2 "residual shape-only" note; record R1 contract-alignment, R3 fix, R4 `run_stats_query`); `docs/warehouse-adapter-ops.md § Databricks` (update the "only `run_stats_query` degrades" line, add the R1/R3 disposition + the two live survivors); epic **#219** checklist (`gh issue edit 219` — tick #220–#227); adapter docstring header (drop the `run_stats_query` degrade line).
- **AC:** every #227 deferred item shows a disposition (implemented/certified/survivor); no item silently dropped; #258 explicitly out of scope; validation command green.
- **Done when:** docs match the shipped state; epic checklist ticked; ready to close #219 + #227.
- **Depends on:** US-004.

### US-006 — Maintainer live certification pass + inline fixes
- **Description:** Maintainer runs the gated `-m databricks` suite (incl. US-004's anomaly + capture tests) against the live Free-Edition rig; fix inline any Spark SQL-acceptance / result-coercion bugs the anomaly/capture paths surface; re-cert. (Ralph cannot run this — no rig; self-skips. Track as a survivor issue if the rig is unavailable at merge, per #247.)
- **Traces to:** DEC-003, DEC-004.
- **Files:** possible inline fixes in `src/signalforge/warehouse/adapters/databricks.py` + fixture regen if a live shape correction is needed (the #226 pattern).
- **AC:** `SF_RUN_DATABRICKS=1 … uv run pytest -m databricks --no-cov` green (or the residual gap tracked as a named survivor issue — nothing silently dropped).
- **Done when:** live suite green OR survivor filed.
- **Depends on:** US-004.

### US-007 — Quality Gate
- **Description:** Code reviewer x4 (fix all real bugs each pass) + CodeRabbit; validation green after fixes.
- **Depends on:** US-005, US-006.

### US-008 — Patterns & Memory (priority 99)
- **Description:** Update `.claude/rules/warehouse-adapters.md` (retire the R1/R2 shape-only residual notes; record the `run_stats_query` graduation + Spark str-escape convention), and memory (Databricks residual disposition + the "anomaly SQL parsed-not-executed → live cert" reinforcement of the #226 lesson).
- **Depends on:** US-007.

## Phase 5 — Publish PR

- Draft PR: https://github.com/wjduenow/SignalForge/pull/261 (base `dev`)
- Awaiting review + approval to devolve.

## Beads Manifest

_(pending devolve)_
