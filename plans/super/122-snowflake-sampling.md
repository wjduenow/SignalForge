# Super Plan — #122: Snowflake deterministic sampling (`sample_rows` HASH-mod) + `materialise_sample` (TEMP TABLE)

## Meta

- **Ticket:** https://github.com/wjduenow/SignalForge/issues/122
- **Epic:** #118 (Snowflake warehouse adapter, v0.2)
- **Branch / worktree:** `feature/122-snowflake-sampling` → `../worktrees/SignalForge/122-snowflake-sampling`
- **Base:** `dev` @ `1f8cc51` (includes #119 skeleton, #120 profile, #121 compiler-dialect)
- **Phase:** devolved
- **Sessions:** 1 (2026-05-26)

---

## Phase 1 — Discovery

### What / Why / Who

Implement the two sampling surfaces on `SnowflakeAdapter` (currently `NotImplementedError` stubs from the #119 skeleton):

- **`sample_rows`** — deterministic hash-mod sampling, the Snowflake analog of BigQuery's `MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), bucket)` → `MOD(ABS(HASH(*)), bucket)`. Same fail-loud sizing guards as BigQuery (`UnknownTableSizeError`, `SamplingRequiresPartitionFilterError`). Hash-mod over `TABLESAMPLE` for determinism + view/MV support (#3 DEC-006).
- **`materialise_sample`** — `CREATE TEMPORARY TABLE _sf_sample_<run_id> AS SELECT ...`; `run_id` derived the same way as BigQuery (blake2b over qualified name + version + n + canonical partition filter). The connection holds the session, so temp tables are visible to subsequent queries on the same connection — simpler than BigQuery's `_SESSION` + `connection_properties` dance.

This graduates Architectural Commitment #3 (warehouse-agnostic by design) — the second real adapter's sampling path — and is the precondition for pruning against Snowflake.

### Codebase findings

- `adapters/snowflake.py` (#119): `__init__` captures auth params only (NO injectable connection); `__enter__`/`__exit__` are no-ops; `sample_rows`/`column_stats`/`run_test_sql` raise `NotImplementedError`; `materialise_sample`/`estimate_query_bytes` inherit the ABC's typed-degrade default.
- `adapters/_snowflake_client.py` (#119): the one-shim-per-vendor SDK seam. Exposes `_SnowflakeClientProtocol` (connection: `cursor()` / `close()`) + `_SnowflakeCursorProtocol` (`execute()` / `fetchall()` / `close()`) and a lazy `make_real_client`. No `description` on the cursor protocol yet; no error-mapping helper.
- **#121 (merged) extended `Dialect`** with five SQL-fragment fields. `SNOWFLAKE_DIALECT` now carries `sample_row_hash_expr="ABS(HASH(*))"`, `sample_cte_alias='"sample"'` (quoted — `SAMPLE` is reserved), `timestamp_literal_template="'{value}'::TIMESTAMP"`, `date_literal_template="'{value}'::DATE"`, `quote_qualified_per_component=True`. The prune compiler is fully dialect-driven and already emits correct per-component-quoted Snowflake SQL → **#122 is independent of the (now-merged) #121**; the #116 substitution test can assert exact Snowflake quoting.
- BigQuery reference (`adapters/bigquery.py`): `client=` injection seam; `_compute_run_id` / `_canonical_partition_filter` / `_hash_session_id` helpers (module-private, blake2b recipes); `materialise_sample` → `TableRef(project=None, dataset="_SESSION", name=...)`; `__exit__` fail-soft session cleanup with the DEC-013/014 WARNING.
- `fakesnow` is NOT installed/declared; `snowflake-connector-python` is in `dev` + the `[snowflake]` extra. Per the epic, the canonical `FakeSnowflakeClient` + fakesnow harness + live e2e + ops docs are **#124's** deliverables.

### Scoping answers (2026-05-26)

1. **`run_test_sql`** → **implement it in #122.** The AC ("subsequent `run_test_sql` reads the temp table") demands a working consumer; no other sub-issue owns it; it's small. `column_stats` stays `NotImplementedError` (no AC needs it).
2. **Test doubles** → **hand-rolled `FakeSnowflakeClient` now** (pull that deliverable forward from #124). #124 layers fakesnow (SQL-parsing validation) + live e2e + docs on top.
3. **Table size** → **INFORMATION_SCHEMA.TABLES.ROW_COUNT** (cheap metadata; NULL for views/MVs → same unknown-size fallback as BigQuery).
4. **Temp `TableRef`** → **fully-qualified via the source DB/schema.**

---

## Phase 2 — Architecture Review

| Area | Rating | Finding |
|---|---|---|
| Security | concern | The INFORMATION_SCHEMA size query embeds schema/name as string literals → escape via `escape_bq_string_literal` (Snowflake uses backslash escaping too) even though identifiers are pre-validated at `TableRef` construction. `__repr__` credential redaction holds; raw `session_id` only in the cleanup WARNING (DEC-014 narrow exception). HASH-mod SQL is parameterless; `bucket`/`n` are ints. |
| Performance | pass | One cheap metadata query per sample; one CTAS per materialise; no N+1. Optional per-context row-count cache (mirror BigQuery's `_table_metadata_cache`). |
| Data model | pass | No migrations. Temp `TableRef` = fully-qualified via source DB/schema. |
| API design | concern | Skeleton `__init__` lacks an injectable connection → add `connection=` seam (mirror BigQuery `client=`). `run_test_sql` + `materialise_sample` graduate from stub. |
| Observability | pass | INFO on materialise (hashed session id); operator-actionable cleanup WARNING; lazy-format JSON logger (warehouse convention); `--quiet` must NOT suppress the cleanup WARNING. |
| Testing | concern | Hand-rolled fake can't *parse* SQL — subtle Snowflake-dialect errors only surface in #124's fakesnow + live e2e. Mitigate: keep SQL faithful to Snowflake docs, reuse `SNOWFLAKE_DIALECT` fields, engineer determinism. |

No blockers. Concerns are addressed by the decisions below.

---

## Phase 3 — Refinement (Decisions)

- **DEC-001 — Connection injection seam + lazy build.** Add `connection: _SnowflakeClientProtocol | None = None` to `SnowflakeAdapter.__init__` (mirrors BigQuery's `client=`). `_get_connection()` returns the live connection, lazily building it via `make_real_client(...)` from the stored auth params on first use. `from_profile` leaves `connection=None` (lazy). Tests inject a `FakeSnowflakeClient`.

- **DEC-002 — Connection-bound session state (#22 DEC-002, adapted).** The Snowflake *connection* embodies the session that scopes temp tables. Store the live connection as `self._active_session` (set on first `_get_connection()`). BigQuery stored a `session_id` *string* threaded via `connection_properties`; Snowflake stores the *connection object* because the connector holds the session. `self._session_started_at` (monotonic) is set at the first successful `materialise_sample` to drive the cleanup-WARNING `auto-expire` text. No `connection_properties` / `session_id` routing is needed — every op uses the one connection.

- **DEC-003 — `__exit__` fail-soft cleanup with a Snowflake-shaped operator-actionable WARNING.** `__exit__` closes `self._active_session` (which reaps session-scoped temp tables), swallowing any failure. State resets in a `finally` so a second `__exit__` is a no-op (cleanup-boundary fail-soft pattern). Success → INFO with `session_id_hash` (blake2b-4 of the connection's `session_id` when available, else a bare "session closed"). Failure → WARNING naming the temp-table identifier + raw `session_id` (DEC-014 narrow exception) and stating the temp table auto-drops when Snowflake reaps the idle session server-side. **No `bq`-style manual command** is offered: a Snowflake temp table is unreachable outside its owning session, so the honest durable fallback is the server-side session timeout. `--quiet` does NOT suppress this WARNING.

- **DEC-004 — `run_test_sql` implemented; `column_stats` stays stubbed.** `validate_test_sql(sql)` → wrap in `SELECT COUNT(*) AS failures FROM (<sql>) AS t` (with `ARRAY_AGG(OBJECT_CONSTRUCT(*))` over a `LIMIT`-bounded subquery when `capture_failures > 0`). Execute on `self._active_session`'s cursor so a materialised temp table is reachable. `column_stats` keeps its `NotImplementedError` (out of #122 scope; no AC).

- **DEC-005 — Table size via INFORMATION_SCHEMA.TABLES.ROW_COUNT (case-insensitive).** `_get_num_rows(table)` runs `SELECT ROW_COUNT FROM <database>.INFORMATION_SCHEMA.TABLES WHERE UPPER(TABLE_SCHEMA)=UPPER('<schema>') AND UPPER(TABLE_NAME)=UPPER('<name>')` (literals escaped per the Security finding; `<database>` quoted per dialect). `NULL`/empty (views/MVs) → unknown size. Decision mirrors BigQuery's `sample_rows` exactly: unknown + no `partition_filter` → `UnknownTableSizeError`; unknown + filter → `bucket=1000` (DEBUG-logged); `num_rows >= _LARGE_TABLE_THRESHOLD` + no filter → `SamplingRequiresPartitionFilterError`; else `bucket=max(num_rows//n,1)`. Reuse the BigQuery `_LARGE_TABLE_THRESHOLD` *value* (100M). When `table.project is None` (direct callers; the prune path always qualifies via `TableRef.from_model`), fall back to `CURRENT_DATABASE()` — documented edge.

- **DEC-006 — Deterministic SQL reuses `SNOWFLAKE_DIALECT` fields (byte-parity with the compiler).** `sample_rows` and the `materialise_sample` CTAS build `MOD(<dialect.sample_row_hash_expr>, <bucket>) < 1` and `ORDER BY <dialect.sample_row_hash_expr>` (so `LIMIT` truncation is deterministic), and render partition filters with `dialect.timestamp_literal_template` / `date_literal_template`. No hard-coded `HASH(*)` — reading the dialect keeps the adapter's sample SQL consistent with the prune compiler's sample CTE (Architectural Commitment #5).

- **DEC-007 — `materialise_sample` returns a fully-qualified `TableRef` via the source DB/schema.** `CREATE TEMPORARY TABLE <quoted db.schema._sf_sample_<run_id>> AS SELECT ...` colocates the temp table with the source; return `TableRef(project=table.project, dataset=table.dataset, name="_sf_sample_<run_id>")`. `run_id` reuses the shared blake2b-8 recipe (DEC-008). `partition_filter` lands once in the CTAS WHERE; per-test queries against the temp table do not re-apply it. `n <= 0` → `ValueError`; SDK/network/quota failure → `MaterialisationFailedError(cause=...)` (mirrors BigQuery).

- **DEC-008 — Hoist the run-id / hashing helpers to a shared module.** Move `_compute_run_id`, `_canonical_partition_filter`, `_hash_session_id` out of `adapters/bigquery.py` into a new `signalforge/warehouse/_sample_id.py`; both adapters import from it. Keeps the `run_id` recipe byte-identical across vendors and avoids a cross-adapter import (which would risk pulling the BigQuery SDK). BigQuery snapshots/fixtures must stay byte-identical — pure relocation, no behaviour change.

- **DEC-009 — Minimal Snowflake error mapping in the shim.** Add `map_snowflake_exception(exc, *, context)` to `adapters/_snowflake_client.py` (lazy-imports `snowflake.connector.errors`, so the one-shim-per-vendor rule holds). v0.2 minimal taxonomy: auth → `WarehouseAuthError`; programming/syntax → `QuerySyntaxError`; otherwise return the exception unchanged (caller re-raises). A full taxonomy mirroring `map_bq_exception` is deferred to #124. The adapter's `sample_rows` / `run_test_sql` / `materialise_sample` route SDK exceptions through it.

- **DEC-010 — Cursor protocol gains `description`.** Extend `_SnowflakeCursorProtocol` with a `description` member (DB-API: a sequence of column descriptors, element `[0]` is the column name) so `sample_rows` builds `dict` rows from tuple `fetchall()` results without depending on `DictCursor`. `FakeSnowflakeClient`'s cursor exposes `description` to match.

### Out of scope (explicit)

- `column_stats` (stays `NotImplementedError`), `estimate_query_bytes` (#123), fakesnow harness + live e2e + ops docs (#124), a full `map_snowflake_exception` taxonomy (#124/follow-up).

---

## Phase 4 — Detailed Breakdown

Canonical validation command (run after every story):
`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

### US-001 — Shared sampling-id helpers + cursor `description` + Snowflake error mapper

**Description:** Lay the cross-adapter foundations: relocate the deterministic `run_id` / hashing helpers to a shared module, extend the cursor protocol, and add a minimal Snowflake exception mapper to the shim. Pure plumbing — no Snowflake adapter behaviour yet.

**Traces to:** DEC-006, DEC-008, DEC-009, DEC-010.

**Files:**
- `src/signalforge/warehouse/_sample_id.py` (new) — `compute_run_id`, `canonical_partition_filter`, `hash_session_id` (moved verbatim from `bigquery.py`; drop the leading `_` so they're importable, or keep `_`-prefixed and import the private names).
- `src/signalforge/warehouse/adapters/bigquery.py` — delete the three local helpers; import from `_sample_id`. No behaviour change.
- `src/signalforge/warehouse/adapters/_snowflake_client.py` — add `description` to `_SnowflakeCursorProtocol`; add `map_snowflake_exception(exc, *, context)` (lazy SDK-error import; auth → `WarehouseAuthError`, programming → `QuerySyntaxError`, else passthrough); extend `__all__`.

**TDD:**
- BigQuery `materialise_sample` run-id / temp-table-name tests still pass byte-identical (relocation is invisible).
- `tests/warehouse/test_snowflake_client_confinement.py` still passes (the new SDK-error import is confined to the shim).
- `map_snowflake_exception` maps a fake auth error → `WarehouseAuthError`, a fake programming error → `QuerySyntaxError`, an arbitrary error → returned unchanged.
- `_SnowflakeCursorProtocol` still `runtime_checkable`-satisfied by a fake exposing `execute`/`fetchall`/`close`/`description`.

**Acceptance criteria:** Helpers live in `_sample_id`; BigQuery imports them; all existing BigQuery snapshot/fixture tests are byte-identical; the shim gains `description` + `map_snowflake_exception` with confinement intact; validation passes.

**Done when:** Validation green; no BigQuery test bytes changed.

**Depends on:** none.

---

### US-002 — `FakeSnowflakeClient` + connection seam + fail-soft `__exit__` cleanup

**Description:** Add the hand-rolled `FakeSnowflakeClient` test double and wire the adapter's connection lifecycle: injectable `connection=`, lazy `_get_connection()`, connection-bound `_active_session`, and the fail-soft `__exit__` cleanup with the operator-actionable WARNING.

**Traces to:** DEC-001, DEC-002, DEC-003, DEC-010.

**Files:**
- `tests/warehouse/_fake_snowflake.py` (new) — `FakeSnowflakeClient` (connection) + fake cursor with `expect_execute(matching=..., returns=rows|Exception)` / `description` / consume-one-per-call semantics + `assert_all_expectations_met`; a `session_id` attribute; `close()` that can be made to raise (to drive the cleanup WARNING). Never imported by production code.
- `src/signalforge/warehouse/adapters/snowflake.py` — add `connection=` kwarg + `self._active_session` / `self._session_started_at`; `_get_connection()` (lazy `make_real_client`); real `__enter__`/`__exit__`; `_cleanup_active_session()` (close-and-reap, swallow-and-WARN, INFO on success with hashed session id, state reset in `finally`); module `_LOGGER` (lazy-format JSON).

**TDD:**
- `connection=` injects a fake; `_get_connection()` returns it without building a real one.
- `__repr__` still shows only `account` + `warehouse` (regression of the #119 redaction).
- `__exit__` calls `conn.close()` once and resets `_active_session` to `None`; a second `__exit__` is a no-op.
- Cleanup-failure WARNING shape pinned: `conn.close()` raising → one WARNING containing the raw `session_id` + the temp-table-auto-reap remediation; `_active_session` still reset.
- Success-path INFO uses `session_id_hash`, never the raw id (assert raw id absent from the INFO record).
- `--quiet`-equivalent (logger at WARNING) still surfaces the cleanup WARNING (documented contract; assert it is emitted at WARNING level).

**Acceptance criteria:** Fake exists with `expect_*` API; adapter accepts/lazily builds a connection; `__exit__` is fail-soft + idempotent; WARNING + INFO shapes pinned; validation passes.

**Done when:** Validation green; lifecycle + cleanup tests pass.

**Depends on:** US-001.

---

### US-003 — `sample_rows` (INFORMATION_SCHEMA sizing + dialect HASH-mod + dict rows)

**Description:** Implement deterministic `sample_rows`: look up `ROW_COUNT` from INFORMATION_SCHEMA, apply the BigQuery-mirrored fail-loud sizing decision, build the `MOD(ABS(HASH(*)), bucket)` SQL from `SNOWFLAKE_DIALECT` fields, execute on the connection, and shape tuple results into dicts via `cursor.description`.

**Traces to:** DEC-005, DEC-006, DEC-009, DEC-010.

**Files:**
- `src/signalforge/warehouse/adapters/snowflake.py` — implement `sample_rows`; add `_get_num_rows(table)` (INFORMATION_SCHEMA query, case-insensitive, escaped literals, `CURRENT_DATABASE()` fallback when `project is None`), `_quote(ref)` (per-component double-quoting), `_render_partition_filter(pf)` (dialect literal templates), `_rows_to_dicts(cursor)` (via `description`). Route SDK errors through `map_snowflake_exception`.
- `tests/warehouse/test_snowflake_stub.py` — remove `test_sample_rows_raises_not_implemented`.
- `tests/warehouse/test_snowflake_sampling.py` (new) — the sample_rows suite.

**TDD:**
- **Determinism:** identical `(table, n, partition_filter)` → byte-identical executed SQL across two calls; SQL contains `MOD(ABS(HASH(*)), <bucket>) < 1` + `ORDER BY ABS(HASH(*))` + `LIMIT n`.
- **Sizing branches:** `ROW_COUNT` NULL + no filter → `UnknownTableSizeError`; NULL + filter → `bucket=1000`; `ROW_COUNT >= 100M` + no filter → `SamplingRequiresPartitionFilterError`; else `bucket=max(num_rows//n,1)`.
- `n <= 0` → `ValueError`.
- **Dict shaping:** tuple `fetchall()` + `description` → list of dicts keyed by column name.
- Partition filter renders via the Snowflake `'{value}'::TIMESTAMP` / `::DATE` templates; a string value is escaped.
- SDK programming error → `QuerySyntaxError` (via the mapper).

**Acceptance criteria:** `sample_rows` is deterministic, fail-loud on sizing, dialect-driven, returns dicts; stub assertion removed; validation passes.

**Done when:** Validation green; sampling suite passes.

**Depends on:** US-002.

---

### US-004 — `materialise_sample` + `run_test_sql` (temp-table reachability + #116 substitution)

**Description:** Implement `materialise_sample` (session temp table, qualified `TableRef`, pinned connection) and `run_test_sql` (COUNT(*) on the active connection), then pin the AC: a materialised sample is reachable by a follow-up `run_test_sql`, and the prune compiler — fed the returned `TableRef` with `SNOWFLAKE_DIALECT` — emits SQL referencing the temp table, not the source (#116 gotcha).

**Traces to:** DEC-002, DEC-004, DEC-006, DEC-007, DEC-008, DEC-009.

**Files:**
- `src/signalforge/warehouse/adapters/snowflake.py` — implement `materialise_sample` (reuse `_sample_id.compute_run_id`; CTAS with the dialect HASH-mod predicate; `validate_identifier` on the temp name; set `_active_session` + `_session_started_at`; INFO with hashed session id; return fully-qualified `TableRef`) and `run_test_sql` (`validate_test_sql` → COUNT(*) wrap, `ARRAY_AGG(OBJECT_CONSTRUCT(*))` when `capture_failures>0`, execute on `_active_session`, map errors, return typed `TestResult`).
- `tests/warehouse/test_snowflake_stub.py` — remove `test_run_test_sql_raises_not_implemented` + `test_materialise_sample_raises_not_supported` (keep `column_stats` + `estimate_query_bytes` stub assertions).
- `tests/warehouse/test_snowflake_materialise.py` (new) — materialise + run_test_sql + substitution suite.

**TDD:**
- Temp-table CTAS SQL: `CREATE TEMPORARY TABLE` + `_sf_sample_<run_id>` (run_id byte-identical to the shared recipe) + the deterministic HASH-mod predicate; returned `TableRef` = `(table.project, table.dataset, "_sf_sample_<run_id>")`.
- `materialise_sample` sets `_active_session` to the connection and `_session_started_at`.
- **Reachability:** after `materialise_sample`, a `run_test_sql` call executes on the *same* connection (assert the fake recorded both on one connection) and its COUNT(*) wrapper references the temp `TableRef` when the engine substitutes it.
- **#116 substitution:** compile a prune test (e.g. `not_null`) via `signalforge.prune.compiler` with `SNOWFLAKE_DIALECT` and the returned temp `TableRef` → compiled SQL contains `_sf_sample_<run_id>` and NOT the source table name (engineered so the two names differ).
- `run_test_sql`: zero failing rows → `passed=True, failure_count=0`; non-zero → `passed=False`; `capture_failures>0` populates `sample_failures`.
- `n <= 0` → `ValueError`; CTAS SDK failure → `MaterialisationFailedError(cause=...)`.

**Acceptance criteria:** Both methods implemented; temp table reachable via the pinned connection; substitution test pins the temp-table name in compiled SQL; stub assertions removed; validation passes.

**Done when:** Validation green; materialise/run_test_sql/substitution suite passes.

**Depends on:** US-003.

---

### US-005 — Quality Gate

**Description:** Run the code reviewer 4× across the full changeset, fixing every real bug each pass; run CodeRabbit if available; ensure validation passes after all fixes. Pay special attention to: SQL-literal escaping in the INFORMATION_SCHEMA query, the cleanup-WARNING contract (raw `session_id` only on failure; `--quiet` non-suppression), byte-parity of the relocated `_sample_id` helpers against BigQuery fixtures, and the lazy-format JSON logger (no f-string interpolation).

**Traces to:** all DECs.

**Acceptance criteria:** 4 reviewer passes complete with all real findings fixed; CodeRabbit findings triaged; validation green.

**Done when:** No outstanding real findings; validation passes.

**Depends on:** US-004.

---

### US-006 — Patterns & Memory

**Description:** Distil the Snowflake sampling/session/cleanup conventions into the rules + docs so the next adapter (or #124) inherits them.

**Traces to:** all DECs.

**Files:**
- `.claude/rules/warehouse-adapters.md` — add a "Snowflake sampling + connection-bound session (issue #122)" section: connection-bound `_active_session` (vs BigQuery's `session_id` string), INFORMATION_SCHEMA.ROW_COUNT sizing, dialect-field reuse for sample SQL, the Snowflake-shaped cleanup WARNING (no manual command), the shared `_sample_id` module.
- `CLAUDE.md` — public-API surface note: `SnowflakeAdapter.sample_rows` / `materialise_sample` / `run_test_sql` now implemented (`column_stats` / `estimate_query_bytes` still pending #123/#118).
- This plan — mark phase `devolved`.

**Acceptance criteria:** Rules + CLAUDE.md updated; validation passes.

**Done when:** Docs reflect the shipped behaviour.

**Depends on:** US-005.

---

## Rules compliance

- **warehouse-adapters.md** — deterministic hash-mod over TABLESAMPLE (DEC-006 of #3); fail-loud sizing (DEC-024); identifier validation at construction; one-shim-per-vendor SDK confinement (`map_snowflake_exception` in the shim); session-state on the adapter (#22 DEC-002, adapted to connection-bound); cleanup-boundary fail-soft (#22 DEC-013/014) with the WARNING reshaped for Snowflake's session-local temp tables; `__repr__` credential redaction; no eager SDK import.
- **prune-engine.md** — materialised-sample substitution gotcha (#116): pinned by the US-004 compiler test.
- **testing-signal.md** — no `assert True`; engineered determinism; hand-rolled fake (no MagicMock); the relocation must keep BigQuery fixtures byte-identical.
- **python-build.md** — `snowflake-connector-python` already under `[snowflake]` + dev; no new runtime dep (fakesnow deferred to #124).

## Beads Manifest

- **Epic:** `bd_1-scaffolding-lqr`
- **Tasks (linear chain):**
  - `bd_1-scaffolding-lqr.1` — US-001 Shared `_sample_id` helpers + cursor `description` + error mapper
  - `bd_1-scaffolding-lqr.2` — US-002 `FakeSnowflakeClient` + connection seam + `__exit__` cleanup (← .1)
  - `bd_1-scaffolding-lqr.3` — US-003 `sample_rows` (← .2)
  - `bd_1-scaffolding-lqr.4` — US-004 `materialise_sample` + `run_test_sql` + #116 substitution (← .3)
  - `bd_1-scaffolding-lqr.5` — US-005 Quality Gate (← .4)
  - `bd_1-scaffolding-lqr.6` — US-006 Patterns & Memory (← .5)
- **Worktree:** `../worktrees/SignalForge/122-snowflake-sampling`
- **Devolved:** 2026-05-26
