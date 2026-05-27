# Super Plan — #139: Snowflake sample-mode prune — projection-subquery sample shape

## Meta

- **Ticket:** [#139](https://github.com/wjduenow/SignalForge/issues/139) — `Snowflake sample-mode prune: HASH(*) invalid in WHERE/ORDER BY — needs projection-subquery sample shape`
- **Type:** bug · **Priority:** P2
- **Milestone:** v0.2 (Snowflake adapter epic #118; scope of #121/#122; discovered by #124's live harness)
- **Beads ref:** `bd_1-scaffolding-cdp`
- **Phase:** detailing (awaiting approval)
- **Branch:** `feature/139-snowflake-sample-shape` (based on `dev`)
- **Sessions:** 1 (2026-05-27)

---

## Phase 1 — Discovery

### What / Why / Who

**What.** The deterministic-sample SQL emitted for Snowflake places the dialect's row-hash expression (`ABS(HASH(*))`) inline in `WHERE` / `ORDER BY`:

```sql
... WHERE MOD(ABS(HASH(*)), <bucket>) < 1 ... ORDER BY ABS(HASH(*)) ...
```

Snowflake rejects this — `002079 (42601): Use of * as a function argument`. `HASH(*)` is valid **only in the SELECT projection**, never in a `WHERE`/`ORDER BY` predicate (verified live: `SELECT ABS(HASH(*))` works; the predicate form fails). BigQuery's `ABS(FARM_FINGERPRINT(TO_JSON_STRING(t)))` is valid inline everywhere, so the architectural gap was invisible until a live Snowflake run.

**Why.** Until fixed, Snowflake prune works only with `prune.scope: full` (no sampling), and `safety: sample` is broken (it calls `sample_rows`). The whole point of the prune step (Architectural Commitment #1, signal over volume) leans on cheap sampling; full-scan-only is a cost regression for Snowflake users.

**Who.** Operators running SignalForge against a Snowflake profile with `prune.scope: sample` (the default-ish cheap path) or `safety: sample`.

### Root cause

A single **inline `sample_row_hash_expr` string** in the #121 `Dialect` design cannot express the **projection-subquery shape** Snowflake needs: compute the hash in an inner `SELECT`, reference the computed column by alias in the outer `WHERE`/`ORDER BY`. The `Dialect` field is a *string*, but the fix needs a *structural* abstraction.

### Codebase findings (Scout)

`sample_row_hash_expr` is consumed in exactly **two** SQL-building sites; a third (BigQuery adapter) hardcodes its hash and is independent.

| Surface | Location | Reads `sample_row_hash_expr`? | Notes |
|---|---|---|---|
| `Dialect` definition | `src/signalforge/warehouse/models.py:57-164` | — | frozen `@dataclass`; `BIGQUERY_DIALECT`, `SNOWFLAKE_DIALECT` (sets `sample_row_hash_expr="ABS(HASH(*))"`, `sample_cte_alias='"sample"'`), `POSTGRES_DIALECT`. |
| Prune compiler sample CTE | `src/signalforge/prune/compiler.py:180-218` (`_render_sample_cte`) | **yes** — line 210 `MOD({dialect.sample_row_hash_expr}, {bucket}) < 1` | Used for **both** dialects. CTE has **no ORDER BY** (only `WHERE MOD(...)<1 LIMIT n`). Outer queries select **specific** columns from the CTE alias (line 362/402/449/558), and `custom_sql` rewrites its `FROM` to the alias (line 749). |
| `SnowflakeAdapter.sample_rows` | `src/signalforge/warehouse/adapters/snowflake.py:577-624` | **yes** — line 616 reads it; 618 (WHERE) + 622 (`ORDER BY {hash_expr}`) | `SELECT * FROM <quoted> AS t WHERE ... ORDER BY ... LIMIT n`; returns dict rows. |
| `SnowflakeAdapter.materialise_sample` | `src/signalforge/warehouse/adapters/snowflake.py:631-759` | **yes** — line 706 reads it; 713 (WHERE) + 721 (`ORDER BY`) | `CREATE TEMPORARY TABLE <quoted_temp> AS SELECT * FROM <src> AS t WHERE ... ORDER BY ... LIMIT n`. |
| BigQuery adapter (reference) | `src/signalforge/warehouse/adapters/bigquery.py:517,529,661,676` | **no** — hardcodes `ABS(FARM_FINGERPRINT(TO_JSON_STRING(t)))` inline | Works because `FARM_FINGERPRINT` is valid in any clause. **Out of scope** — keep untouched. |
| Shared sample-id seam | `src/signalforge/warehouse/_sample_id.py:29-99` | — | naming recipe only; **builds no SQL**. |

**Import-guard note:** `tests/prune/test_compiler_import_guard.py` forbids only `snowflake` / `google.cloud` imports under `prune/`. A new helper in the **warehouse** layer is importable by the compiler (it already imports `signalforge.warehouse.models` + `signalforge.warehouse._sql_safety`).

**Fixtures & guards:**
- Snowflake compiled-SQL snapshots: `tests/fixtures/prune/compiled_sql/snowflake/*.sql` — the five `*_sample.sql` files carry the buggy `MOD(ABS(HASH(*)), n)` form. BigQuery equivalents under `tests/fixtures/prune/compiled_sql/*.sql` **must stay byte-identical**.
- `tests/prune/test_compiler_fakesnow.py:315-330` — parses every Snowflake fixture through `sqlglot.parse_one(sql, dialect="snowflake")` (gated `@pytest.mark.snowflake`). (sqlglot currently parses `HASH(*)`-in-WHERE without complaint — snapshot/parse equality certifies *shape*, not Snowflake *acceptance*; only live execution does.)
- `tests/warehouse/test_snowflake_adapter_fakesnow.py:342-401` — asserts the adapter emits `MOD(ABS(HASH(*)), 10) < 1` + `ORDER BY ABS(HASH(*))` and sqlglot-parses them.
- `tests/warehouse/test_snowflake_prune_live.py:286` — live e2e currently pinned to `scope="full"` to dodge this bug.

### Convention constraints (Convention Checker)

- **Dialect-driven, never name-branched** (`prune-engine.md` DEC-025): compiler reads `Dialect` fields; never `if dialect.name ==`. Enforced by `test_compiler_import_guard.py`.
- **BigQuery byte-identity is the regression gate**: new `Dialect` fields default to BigQuery-shaped values so the 11 BQ snapshot fixtures + every `Dialect(...)` construction site stay byte-identical.
- **3-tier validation** (`prune-engine.md` DEC-005 / `warehouse-adapters.md` #124): byte-exact snapshots → gated fakesnow-execute + sqlglot-parse → gated **live** Snowflake. "A new dialect's SQL needs a parser/executor in the loop — snapshot equality certifies shape, not validity." This bug is the canonical example: snapshots + sqlglot both passed the invalid form; only live caught it.
- **Reuse `_sample_id`** for `run_id` (already in place); reuse `Dialect` fields, never hardcode `HASH(*)` in the adapter (`warehouse-adapters.md` #122).
- **5-surface graduation** when a reserved/declarative field changes meaning: rule file + ops doc + CLAUDE.md + test + plan DEC in lockstep.
- **Gated marker**: `uv run pytest -m snowflake --no-cov`; live tier gated on `SF_RUN_SNOWFLAKE=1` + conn vars + writable schema.
- No `workflow-project.md`; conventions live in `CLAUDE.md` + `.claude/rules/`.

### Scoping answers

1. **Shape abstraction → structural flag + alias fields.** Add `sample_hash_in_projection: bool = False` and `sample_hash_alias: str = "_sf_sample_hash"` to `Dialect`. BigQuery keeps the default (`False` → inline); Snowflake sets `True`.
2. **Renderer location → shared warehouse-layer helper, leave BigQuery adapter alone.** New `src/signalforge/warehouse/_sample_sql.py` consumed by the prune compiler + `SnowflakeAdapter`. BigQuery's adapter keeps its hardcoded inline form (out of scope).
3. **Hash-column leak → `SELECT * EXCLUDE (<alias>)`.** The projection path wraps the hash in an inner `SELECT t.*, <hash> AS <alias>` and the outer `SELECT * EXCLUDE (<alias>)` strips it, so returned rows and the materialised temp table carry only original columns.

---

## Phase 2 — Architecture Review

| Area | Rating | Finding |
|---|---|---|
| Security | **pass** | The hash alias `_sf_sample_hash` and `EXCLUDE` token are hardcoded constants — no new injection surface. Table/identifier validation (`_sql_safety`) and typed `PartitionFilter` rendering are unchanged; the helper takes a pre-rendered partition predicate from each caller's existing `_render_partition_filter`. |
| Performance | **pass** | Projection-subquery computes `HASH(*)` once per row in an inner projection — equivalent cost to the inline form; `EXCLUDE` is a planner-level column prune (no runtime cost). Materialised sample stays bounded by `LIMIT n` + `maximum_bytes_billed`. |
| Data model | **pass (improvement)** | `SELECT * EXCLUDE (alias)` guarantees the materialised temp table's schema equals the source schema — strictly *more* correct than the risk of an extra column leaking into the temp table that downstream prune queries. |
| API design | **pass** | Two new `Dialect` fields with BigQuery-safe defaults → backward compatible. `Dialect` is a frozen dataclass (no Pydantic `extra="forbid"` drift detector); fields appended at the end with defaults keep all 4 construction sites (`models.py`, `test_models.py`, `_fake_adapter.py`, `test_compiler.py`) valid. `POSTGRES_DIALECT` keeps defaults (correct — Postgres stub never invokes the compiler). |
| Observability | **pass** | No logging changes. The existing adapter INFO/WARNING surfaces are unaffected. |
| Testing | **concern → addressed** | The live tier is the only true certifier (`HASH(*)` cannot run under fakesnow). **Risk:** can `ORDER BY` reference a column that `SELECT * EXCLUDE` removes from output? Addressed in DEC-004 with a fallback. The offline guards (snapshots + sqlglot) prove shape; the maintainer's live run is the merge-gating certification. |

**No blockers.** One concern (live-cert ownership + the `ORDER BY` / `EXCLUDE` interaction) carried into the decisions below.

---

## Phase 3 — Refinement Log

### DEC-001 — `Dialect` gains a structural sample-shape flag, not a richer template

Add two declarative fields to `Dialect` (frozen dataclass, `src/signalforge/warehouse/models.py`):

```python
sample_hash_in_projection: bool = False   # BigQuery inline; Snowflake True
sample_hash_alias: str = "_sf_sample_hash"
```

`SNOWFLAKE_DIALECT` sets `sample_hash_in_projection=True`. BigQuery/Postgres keep defaults. **Rationale:** the chosen option (structural flag) matches the existing "declarative SQL-fragment field" pattern from #121 (`quote_qualified_per_component`, `sample_cte_alias`, …) — a boolean + alias is the minimum that distinguishes the two shapes without a new type or drift mirror. A `SampleShape` enum (rejected) is heavier for two states; full template strings (rejected) are byte-fragile.

### DEC-002 — Single shared renderer `warehouse/_sample_sql.py`, name-agnostic

New module `src/signalforge/warehouse/_sample_sql.py`:

```python
def render_sample_select(
    table_sql: str,          # already-quoted FROM target
    *,
    dialect: Dialect,
    sample_bucket: int,
    sample_size: int,
    extra_where: str | None = None,   # pre-rendered partition predicate (caller owns it)
    order_by_hash: bool,              # compiler CTE: False; adapters: True
) -> str: ...
```

Reads `dialect.sample_row_hash_expr`, `dialect.sample_hash_in_projection`, `dialect.sample_hash_alias`. Two branches, switched on the **boolean** (never `dialect.name`):

- **Inline (BigQuery, `sample_hash_in_projection=False`)** — byte-identical to today's compiler CTE body / would-be adapter form:
  ```
  SELECT * FROM <table_sql> AS t WHERE MOD(<hash_expr>, <bucket>) < 1[ AND <extra_where>][ ORDER BY <hash_expr>] LIMIT <n>
  ```
- **Projection-subquery (Snowflake, `sample_hash_in_projection=True`)**:
  ```
  SELECT * EXCLUDE (<alias>) FROM (SELECT t.*, <hash_expr> AS <alias> FROM <table_sql> AS t) WHERE MOD(<alias>, <bucket>) < 1[ AND <extra_where>][ ORDER BY <alias>] LIMIT <n>
  ```

Consumers: prune compiler `_render_sample_cte` and `SnowflakeAdapter.sample_rows` / `materialise_sample`. **BigQuery adapter is NOT wired** (out of scope — keeps its hardcoded inline form and its own tests). The compiler's import guard is satisfied (warehouse-layer import, no SDK/name branch).

**Rationale:** one place owns the shape so the compiler CTE and the adapter sample SELECT stay byte-consistent (Architectural Commitment #5). Caller owns partition-filter rendering (each already has a dialect-correct `_render_partition_filter`), so the helper stays purely about sample shape and avoids re-plumbing partition logic.

### DEC-003 — BigQuery byte-identity is the load-bearing regression gate

The inline branch must reproduce the **exact** current bytes, including spacing: `MOD(<expr>, <bucket>) < 1` (space after comma, spaces around `<`). The compiler CTE has **no `ORDER BY`** today, so `_render_sample_cte` calls the helper with `order_by_hash=False`; the BQ compiler `*_sample.sql` fixtures are the gate. (The BigQuery *adapter* sample SQL — which *does* carry `ORDER BY` — is untouched, so its form is irrelevant to this helper.)

### DEC-004 — `SELECT * EXCLUDE (alias)` with the filter/order at the outer level; live-cert the `ORDER BY`-of-excluded-column interaction

The projection shape puts `WHERE`/`ORDER BY` at the **outer** level (where `<alias>` is a real column of the derived table), and `EXCLUDE` on the outer projection. The one genuine uncertainty is whether Snowflake's `ORDER BY <alias>` is legal when `<alias>` is `EXCLUDE`-d from the output. **Primary form** (assumed legal — Snowflake resolves `ORDER BY` against input columns):

```sql
SELECT * EXCLUDE (_sf_sample_hash) FROM (
  SELECT t.*, ABS(HASH(*)) AS _sf_sample_hash FROM <src> AS t
) WHERE MOD(_sf_sample_hash, <bucket>) < 1 ORDER BY _sf_sample_hash LIMIT <n>
```

**Fallback (if live rejects ORDER-BY-of-excluded-column):** drop the outer `ORDER BY` from the emitted SQL and rely on the deterministic `MOD` filter for sample membership (matches the compiler CTE, which already has no `ORDER BY`); determinism of *which* rows under `LIMIT` is a pre-existing property, not a regression of this fix. The live test (US-004) is the decision point; the implementer picks primary-or-fallback based on what live Snowflake accepts and pins the chosen form in the fixtures.

The `_sf_sample_hash` alias is emitted **unquoted**; Snowflake folds it to `_SF_SAMPLE_HASH` consistently in both the projection and the `EXCLUDE`, so they resolve to the same identifier. (If live shows a mismatch, quote both consistently — implementer detail, live-certified.)

### DEC-005 — 5-surface graduation for the new `Dialect` fields

`sample_hash_in_projection` / `sample_hash_alias` are new load-bearing fields. Update in lockstep: (1) `warehouse-adapters.md` `Dialect`-fields section + the live-harness-findings note (mark `bd_1-scaffolding-cdp` fixed); (2) `prune-engine.md` "Compiler is dialect-driven" field list; (3) `docs/warehouse-adapter-ops.md` "Known limitations on live Snowflake" (remove the HASH(*) limitation, document sample-mode now works); (4) tests pin the active behaviour; (5) this plan's DECs.

### DEC-006 — Re-enable the live materialised prune e2e at `scope="sample"`

`tests/warehouse/test_snowflake_prune_live.py` moves from `scope="full"` to a sample-mode run (`PruneConfig(scope="sample", sample_strategy="materialised")`) against the engineered writable table, asserting the always-passes `not_null` drop. This is the AC's live certification. Gated `@pytest.mark.snowflake` + `SF_RUN_SNOWFLAKE=1`; maintainer-run before merge.

---

## Phase 4 — Detailed Breakdown

> Validation command (every story): `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`. Snowflake-gated tiers: `uv run pytest -m snowflake --no-cov` (offline fakesnow/sqlglot always; live requires `SF_RUN_SNOWFLAKE=1` + conn vars + writable `SNOWFLAKE_DATABASE`/`SCHEMA`).

### US-001 — `Dialect` shape fields + shared `render_sample_select` helper

**Description.** Add `sample_hash_in_projection` / `sample_hash_alias` to `Dialect`; set them on `SNOWFLAKE_DIALECT`. Create `warehouse/_sample_sql.py::render_sample_select` with the inline + projection-subquery branches.
**Traces to:** DEC-001, DEC-002, DEC-003, DEC-004.
**Files:** `src/signalforge/warehouse/models.py` (fields + `SNOWFLAKE_DIALECT`), `src/signalforge/warehouse/_sample_sql.py` (new), `tests/warehouse/test_sample_sql.py` (new), `tests/warehouse/test_models.py` (Dialect construction stays valid).
**TDD:**
- Inline branch with `order_by_hash=False`, no `extra_where` → byte-exact current BQ compiler-CTE body.
- Inline branch with `order_by_hash=True` + `extra_where` → `... AND <pf> ORDER BY <expr> LIMIT n`.
- Projection branch (Snowflake dialect) → exact `SELECT * EXCLUDE (_sf_sample_hash) FROM (SELECT t.*, ABS(HASH(*)) AS _sf_sample_hash FROM <t> AS t) WHERE MOD(_sf_sample_hash, b) < 1 ORDER BY _sf_sample_hash LIMIT n`.
- Projection branch with `extra_where` places it as `AND <pf>` at the outer level.
- Helper never references `dialect.name` (assert via reading the source / behaviour with a synthetic dialect).
**Done when:** helper unit tests pass; `Dialect` constructs with new defaults; BQ default reproduces inline bytes; full validation green.
**Depends on:** none.

### US-002 — Wire the prune compiler sample CTE to the shared helper + regenerate Snowflake compiler snapshots

**Description.** Replace the inline string-building in `_render_sample_cte` with a call to `render_sample_select(..., order_by_hash=False)`. Regenerate the five Snowflake `*_sample.sql` fixtures to the projection-subquery form; confirm BQ `*_sample.sql` fixtures are **byte-unchanged**.
**Traces to:** DEC-002, DEC-003, DEC-004.
**Files:** `src/signalforge/prune/compiler.py` (`_render_sample_cte`), `tests/fixtures/prune/compiled_sql/snowflake/{not_null,unique,accepted_values,relationships,custom_sql}_sample.sql`, `tests/prune/test_compiler.py` (sample-mode assertions), `tests/prune/test_compiler_fakesnow.py` (sqlglot parse guard now certifies the new form), `tests/prune/test_compiler_import_guard.py` (still green — sanity).
**Done when:** Snowflake sample fixtures show `SELECT * EXCLUDE (_sf_sample_hash) FROM (SELECT t.*, ABS(HASH(*)) AS _sf_sample_hash ...)`; BQ fixtures unchanged (`git diff` empty for `compiled_sql/*.sql`); `uv run pytest -m snowflake --no-cov` sqlglot parse passes; full validation green.
**Depends on:** US-001.

### US-003 — Wire `SnowflakeAdapter.sample_rows` + `materialise_sample` to the shared helper

**Description.** Replace the inline WHERE/ORDER BY hash building in both methods with `render_sample_select(..., order_by_hash=True)`. `materialise_sample` wraps the result as `CREATE TEMPORARY TABLE <temp> AS <select>`. Update the fakesnow/sqlglot adapter tests to assert the new SQL.
**Traces to:** DEC-002, DEC-004.
**Files:** `src/signalforge/warehouse/adapters/snowflake.py` (`sample_rows`, `materialise_sample`), `tests/warehouse/test_snowflake_adapter_fakesnow.py` (assert `SELECT * EXCLUDE`, `SELECT t.*, ABS(HASH(*)) AS _sf_sample_hash`, sqlglot-parse the new forms), `tests/warehouse/test_snowflake_adapter.py` if present.
**Done when:** adapter emits the projection-subquery form for both methods; CTAS wraps it; fakesnow/sqlglot guards updated and `uv run pytest -m snowflake --no-cov` green; full validation green.
**Depends on:** US-002.

### US-004 — Re-enable live materialised prune e2e at `scope="sample"` + docs/5-surface graduation

**Description.** Flip `test_snowflake_prune_live.py` to a materialised sample-mode run asserting the always-passes drop. Update the three doc surfaces (DEC-005) and confirm the live run is documented as the certification step. Pick primary-vs-fallback `ORDER BY` form (DEC-004) per what live Snowflake accepts and pin fixtures accordingly.
**Traces to:** DEC-004, DEC-005, DEC-006.
**Files:** `tests/warehouse/test_snowflake_prune_live.py` (scope=sample, materialised; module docstring), `docs/warehouse-adapter-ops.md` (§ Known limitations — remove HASH(*) limitation), `.claude/rules/warehouse-adapters.md` (Dialect fields + live-harness note: `bd_1-scaffolding-cdp` fixed), `.claude/rules/prune-engine.md` (compiler dialect field list).
**Done when:** live test runs sample-mode and drops an always-passes test (maintainer-certified with `SF_RUN_SNOWFLAKE=1`); docs/rules updated; if live forced the fallback form, US-002/US-003 fixtures + this plan's DEC-004 note are updated to match. Full validation green.
**Depends on:** US-003.

### US-005 — Quality Gate (code review ×4 + CodeRabbit)

**Description.** Run the code reviewer 4 times across the full changeset, fixing every real bug each pass; run CodeRabbit if available. Validation must pass after fixes.
**Done when:** 4 review passes complete, all real findings fixed, `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` green, `uv run pytest -m snowflake --no-cov` (offline) green, live tier certified by maintainer.
**Depends on:** US-004.

### US-006 — Patterns & Memory (priority 99)

**Description.** Record the durable lesson: the #121 lesson generalised — a single inline SQL-fragment string can't express every dialect's clause-position constraints; structural shape needs a structural field. Confirm rule updates from US-004 landed; add a memory entry if the live-cert workflow surfaced anything new.
**Done when:** `.claude/rules/` + `docs/` consistent with shipped behaviour; plan DECs final; memory updated if warranted.
**Depends on:** US-005.

---

## Rules compliance gate

- ✅ Compiler stays name-agnostic — helper switches on a boolean `Dialect` field, not `dialect.name` (import-guard green).
- ✅ BigQuery byte-identity — inline branch reproduces current bytes; BQ fixtures are the regression gate (US-002 done-when).
- ✅ 3-tier validation — snapshots (US-002) + fakesnow/sqlglot (US-002/US-003) + live (US-004).
- ✅ Reuse `_sample_id` (unchanged) + `Dialect` fields (no hardcoded `HASH(*)` left in the adapter).
- ✅ 5-surface graduation (DEC-005 / US-004).
- ✅ Gated marker discipline (`-m snowflake --no-cov`).

---

## Phase 5 — Beads Manifest

_(filled on devolve)_

- Epic: —
- Tasks: US-001 … US-006 + Quality Gate + Patterns & Memory
- Worktree: `../worktrees/SignalForge/139-snowflake-sample-shape`
