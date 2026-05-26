# Super Plan — #121: Prune compiler Snowflake dialect support (`quote_char`, `QUALIFY`, `identifier_case=upper`)

## Meta

- **Ticket:** [#121](https://github.com/wjduenow/SignalForge/issues/121) — `feat: prune compiler Snowflake dialect support (quote_char, QUALIFY, identifier_case=upper)`
- **Epic:** [#118](https://github.com/wjduenow/SignalForge/issues/118) — Snowflake warehouse adapter (v0.2). Depends on the skeleton [#119](https://github.com/wjduenow/SignalForge/issues/119) (landed — `SNOWFLAKE_DIALECT` exists).
- **Branch:** `feature/121-prune-snowflake-dialect` (off `dev`; **PR targets `dev`**)
- **Phase:** devolved (PR [#127](https://github.com/wjduenow/SignalForge/pull/127); beads epic `bd_1-scaffolding-mfa`)
- **Sessions:** 1 (2026-05-25)

---

## Phase 1 — Discovery

### What / Why / Who

**What.** Make `signalforge.prune.compiler` emit valid **Snowflake** SQL for all four built-in dbt test types (`not_null` / `unique` / `accepted_values` / `relationships`) **and** `custom_sql`, driven **purely by the `Dialect` value object** — no `import snowflake` and no `import google.cloud` anywhere under `signalforge/prune/`, and no branching on dialect *name* (DEC-025).

**Why.** Architectural Commitment #3 ("warehouse-agnostic by design"). #119 landed the `SnowflakeAdapter` skeleton + `SNOWFLAKE_DIALECT` constant; this ticket makes the *compiler* — the one place that turns a candidate test into warehouse SQL — produce Snowflake-correct output so #122 (sampling) / #123 (estimate) / #124 (harness) can run real Snowflake prune verdicts.

**Who.** Operators with a Snowflake dbt project (v0.2+). Until the full adapter ops land (#122–#124) the compiler output is exercised by snapshot + fakesnow tests, not live queries.

### Codebase findings

The compiler is **already dialect-driven for the quote character** (`_compile_test` reads `dialect.quote_char` and dispatches on it — `tests/prune/test_compiler.py::test_compiler_dispatches_on_dialect_quote_char` pins this). But three further BigQuery-isms are hardcoded, and one declared `Dialect` capability is never consumed:

| Construct | Today (BigQuery, hardcoded) | Snowflake needs | `Dialect` carries it? | Site |
|---|---|---|---|---|
| Identifier quote char | `` ` `` via `quote_char` | `"` | ✅ yes | `_quote`, `_qualified_table_name` |
| Qualified-name quoting | whole path in **one** quote pair: `` `p.d.t` `` | **per-component**: `"DB"."SCH"."T"` | ❌ no | `compiler.py:210` `_qualified_table_name` |
| Row-hash sample predicate | `MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), B) < 1` | `MOD(ABS(HASH(*)), B) < 1` | ❌ no | `compiler.py:187` `_render_sample_cte` |
| Date/timestamp literal | `TIMESTAMP('…')` / `DATE('…')` | `'…'::TIMESTAMP` / `'…'::DATE` | ❌ no | `compiler.py:150-155` `_render_partition_filter` |
| Identifier case folding | `identifier_case` **declared but consumed nowhere** | fold→UPPER **before** quoting | ⚠️ declared, unused | `_quote` |

Other facts that shape the plan:

- **No other warehouse-specific function names** appear in the compiler — `MOD`/`ABS`/`FARM_FINGERPRINT`/`TO_JSON_STRING`/`TIMESTAMP`/`DATE` are the complete set, all confined to `_render_sample_cte` + `_render_partition_filter`.
- **`identifier_case` and `supports_qualify` are declared on `Dialect` but consumed nowhere** in `src/signalforge/`. Consuming `identifier_case` is therefore a **reserved-surface graduation** (prune-engine.md § "5-surface parity for v0.x → v0.(x+1) graduations").
- **`dialect` already flows engine → compiler.** `engine.py` calls `dialect = adapter.dialect()` then threads it into `_compile_test(...)`. No engine change needed.
- **Snapshot fixtures live at** `tests/fixtures/prune/compiled_sql/*.sql` (11 BigQuery files, byte-exact). Tests load them and assert `compiled == fixture_text`.
- **Snowflake test infra partially exists:** `snowflake-connector-python>=3,<4` is in `[dev]` / `[snowflake]`; `tests/warehouse/test_snowflake_*.py` exist (stub, client, confinement). **No `fakesnow` dependency** today.
- **`POSTGRES_DIALECT` is not exercised by any compiler test** — only `BIGQUERY_DIALECT` and one custom `quote_char='"'` dialect are.
- **`Dialect` is a code-constructed frozen dataclass** (not read back from disk) → no drift-detector fixture; adding fields is low-risk there. All 5 current fields are required (no defaults).

### Scoping answers (2026-05-25)

- **Dialect shape → declarative fields (not methods).** Add four BigQuery-defaulted data fields to the frozen `Dialect` dataclass; the compiler reads them. Keeps `Dialect` a pure value object and keeps every existing construction site + the dispatch test green without edits (defaults reproduce BigQuery byte-for-byte). See DEC-001.
- **Case folding → fold-then-quote, all identifiers.** Apply `dialect.identifier_case` folding to **every** identifier (columns AND each table-name component) **before** wrapping in `quote_char`. Snowflake → `"CUSTOMER_ID"`; BigQuery `"preserve"` is a no-op → snapshots byte-unchanged. See DEC-003.
- **Validation → snapshot + fakesnow-parse, defer live.** Byte-exact Snowflake snapshot fixtures are the primary gate; add `fakesnow` under a gated `@pytest.mark.snowflake` marker to parse/execute the emitted SQL for shape confidence. Real-Snowflake semantic validation (HASH determinism, true case-folding) defers to #124. See DEC-005.

---

## Phase 2 — Architecture Review

Focused review (the change is a contained SQL-fragment renderer + value-object extension; the load-bearing risks are injection-safety, BigQuery byte-regression, and Snowflake correctness the fake cannot fully certify).

| Area | Rating | Finding |
|---|---|---|
| **Security / SQL injection** | **pass** | No new injection surface. Identifiers still pass `validate_identifier` (regex `^[A-Za-z_][A-Za-z0-9_]*$`) at `TableRef` construction / compile seam (DEC-024); folding to UPPER and per-component quoting operate on already-validated tokens. `accepted_values` values still route through `escape_bq_string_literal` (single source, DEC-024) — backslash/quote escaping coincides between BigQuery and Snowflake (both backslash-escape inside single-quoted literals), so reuse is safe. The new `Dialect` template fields are **SignalForge-authored constants**, never user input — `.format(value=…)` on `timestamp_literal_template` interpolates only an already-escaped/ISO value. |
| **BigQuery byte-regression** | **concern → mitigated** | The single biggest risk: refactoring `_quote` / `_qualified_table_name` / `_render_sample_cte` / `_render_partition_filter` must leave **all 11 existing BigQuery snapshots byte-identical**. Mitigation: BigQuery-shaped defaults on the new fields + `identifier_case="preserve"` (no-op fold) + `quote_qualified_per_component=False` (whole-path) reproduce current output exactly. The existing snapshot suite IS the regression gate — US-002's AC requires it green with zero fixture edits. |
| **Data model (`Dialect` compat)** | **pass** | New fields are additive with BigQuery defaults → every existing `Dialect(...)` call site (3 constants + the dispatch-test custom dialect) stays valid unedited. `Dialect` is code-constructed, not deserialised → no drift fixture to update. `__all__` unchanged (no new exported name). |
| **Correctness the fake can't certify** | **concern → accepted** | fakesnow shims `HASH()` to a DuckDB equivalent → it certifies the SQL **parses/runs** but NOT that values match real Snowflake, and its db/schema namespacing isn't 1:1 with Snowflake so case-folding semantics may diverge. Accepted: snapshots pin the exact bytes we intend; fakesnow gives parse/shape confidence; real-Snowflake semantics (HASH determinism, true case-folding) are explicitly deferred to #124 (DEC-005). Documented honestly in the ops doc (DEC-006). |
| **Reproducibility** | **pass (with documented caveat)** | BigQuery `FARM_FINGERPRINT` is cross-time stable; Snowflake `HASH()` is deterministic only *within a Snowflake release*. For prune decisions (same input → same decision **within a run**, Architectural Commitment #5) this suffices. One-line caveat lands in `docs/prune-ops.md` (DEC-006). |
| **Testing strategy** | **pass** | Snapshot (byte-exact) + gated fakesnow parse/run + BigQuery regression + an explicit "no `snowflake`/`google.cloud` import under `prune/`" guard. See Phase 4. |
| **Observability** | **pass** | Compiler is a pure transform — no logging (unchanged). No new audit surface. |
| **Scope discipline** | **pass** | `QUALIFY` is **not** wired into a codepath — `unique` keeps `GROUP BY … HAVING COUNT(*) > 1` (dialect-portable; works on both warehouses). `supports_qualify=True` stays forward-compat metadata (DEC-004). Postgres compiler correctness is **out of scope** (#53 stub; op methods raise) — `POSTGRES_DIALECT` keeps BigQuery-default new-field values, documented as "corrected when Postgres ops land" (DEC-007). |

No blockers. Two concerns, both mitigated/accepted above.

---

## Phase 3 — Refinement Log

### DEC-001 — Extend `Dialect` with four BigQuery-defaulted declarative fields

Add to the frozen `Dialect` dataclass (`signalforge.warehouse.models`):

```python
sample_row_hash_expr: str = "ABS(FARM_FINGERPRINT(TO_JSON_STRING(t)))"
timestamp_literal_template: str = "TIMESTAMP('{value}')"
date_literal_template: str = "DATE('{value}')"
quote_qualified_per_component: bool = False
```

**Rationale.** Keeps `Dialect` a pure, inspectable value object (chosen over render-methods). BigQuery-shaped defaults mean every existing construction site (`BIGQUERY_DIALECT`, the dispatch-test custom dialect) renders byte-identical with zero edits. The compiler reads these fields instead of hardcoding BigQuery SQL — staying name-agnostic (DEC-025 preserved). `sample_row_hash_expr` is the inner expression (alias `t` referenced by the BigQuery form; Snowflake's `HASH(*)` ignores the harmless `AS t` alias); the CTE template stays `MOD({expr}, {bucket}) < 1`.

### DEC-002 — `SNOWFLAKE_DIALECT` field values

```python
SNOWFLAKE_DIALECT = Dialect(
    name="snowflake", supports_tablesample=True, supports_qualify=True,
    quote_char='"', identifier_case="upper",
    sample_row_hash_expr="ABS(HASH(*))",
    timestamp_literal_template="'{value}'::TIMESTAMP",
    date_literal_template="'{value}'::DATE",
    quote_qualified_per_component=True,
)
```

**Rationale.** `HASH(*)` is the documented Snowflake whole-row hash (variadic, all columns); `ABS` before `MOD` matches the BigQuery structure and yields a non-negative residue in `[0, bucket)` (Snowflake `MOD` follows the dividend's sign). `'{value}'::TYPE` is the idiomatic Snowflake cast. Per-component quoting because a single quoted string spanning dots would be read as one literal identifier named `db.schema.table`.

### DEC-003 — `identifier_case` consumed in `_quote`, applied to ALL identifiers before quoting (reserved-surface graduation)

`_quote(identifier, dialect)` folds per `dialect.identifier_case` (`"upper"` → `.upper()`, `"lower"` → `.lower()`, `"preserve"` → unchanged) BEFORE wrapping in `quote_char`. Applies to column identifiers AND each table-name component in `_qualified_table_name`.

**Rationale.** Snowflake folds *unquoted* identifiers to UPPER and matches *quoted* identifiers verbatim; a conventional `CREATE TABLE(customer_id …)` stores `CUSTOMER_ID`, so emitting `"customer_id"` would fail "invalid identifier". Folding to UPPER then quoting (`"CUSTOMER_ID"`) matches conventional Snowflake+dbt tables while preserving the injection-safe always-quote posture. This **graduates** `identifier_case` from declared-but-unused to behaviour-active → triggers the 5-surface parity rule (rule file + ops doc + CLAUDE.md + test + this DEC). BigQuery `"preserve"` is a no-op → snapshots unchanged. **Residual (documented):** a table genuinely created with quoted-lowercase DDL breaks under this default — acceptable, it's the rare case; the conventional majority is the right default.

### DEC-004 — `QUALIFY` is NOT wired; `supports_qualify` stays forward-compat metadata

`unique` keeps `GROUP BY col HAVING COUNT(*) > 1`. No `QUALIFY` codepath added.

**Rationale.** `GROUP BY … HAVING` works on both BigQuery and Snowflake → no name/flag branching needed. A `QUALIFY COUNT(*) OVER (PARTITION BY col) > 1` rewrite would change semantics (return failing *rows* vs the duplicated *key*) — a separate product decision, not dialect translation, and BigQuery supports QUALIFY too so it isn't even Snowflake-specific. The issue's "confirm the compiler can use it where applicable" resolves to: not applicable in v0.2; documented.

### DEC-005 — Validation: snapshot (primary) + gated fakesnow parse/run; live Snowflake deferred to #124

Byte-exact Snowflake snapshot fixtures are the authoritative gate. Add `fakesnow` as a dev/test dependency behind a registered `@pytest.mark.snowflake` marker (excluded from default `addopts`, run `uv run pytest -m snowflake --no-cov`); the marked test feeds each emitted Snowflake SQL into fakesnow against a tiny in-memory table and asserts it parses/executes and returns the expected failing-row shape.

**Rationale.** fakesnow can certify parse/shape but not `HASH(*)` value-semantics or true case-folding (it maps onto DuckDB). Mirrors the project's gated-marker convention (`bigquery` / `anthropic` / `cli_subprocess` / `e2e` / `wheel_smoke`). Real-Snowflake semantic validation is #124's harness scope.

### DEC-006 — Document the cross-version `HASH()` reproducibility caveat in the ops doc

`docs/prune-ops.md` gains a one-line note: BigQuery sampling is reproducible across time (`FARM_FINGERPRINT`); Snowflake sampling is reproducible only within a Snowflake release (`HASH()` may change across versions). Sufficient for within-run prune determinism (Commitment #5).

### DEC-007 — Postgres compiler correctness is out of scope

`POSTGRES_DIALECT` keeps the BigQuery-default values for the four new fields (its op methods raise — #53 stub — so the compiler is never invoked for Postgres). A docstring note records that these will be corrected when the Postgres adapter's warehouse ops land. **Do not** ship knowingly-wrong-but-untested Postgres SQL fragments in this ticket.

### DEC-008 — Explicit import-confinement guard for `prune/`

Add a test asserting no `import snowflake`, `from snowflake`, `import google.cloud`, or `from google.cloud` appears anywhere under `src/signalforge/prune/` (AST-based, mirroring the spirit of the warehouse-side `test_snowflake_client_confinement.py`). The issue lists this as an explicit AC; the compiler stays warehouse-agnostic by construction, and the guard prevents regression.

---

## Phase 4 — Detailed Breakdown

> Validation command (all stories): `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`
> Gated Snowflake test (US-004): `uv run pytest -m snowflake --no-cov`

### US-001 — Extend `Dialect` with four declarative dialect-fragment fields; set `SNOWFLAKE_DIALECT` values

**Description.** Add `sample_row_hash_expr`, `timestamp_literal_template`, `date_literal_template`, `quote_qualified_per_component` to the frozen `Dialect` dataclass with BigQuery-shaped defaults (DEC-001). Set `SNOWFLAKE_DIALECT`'s four values (DEC-002). Leave `POSTGRES_DIALECT` at defaults with a docstring note (DEC-007).

**Traces to:** DEC-001, DEC-002, DEC-007.

**Files:**
- `src/signalforge/warehouse/models.py` — add the four fields (with defaults) to `Dialect`; update `SNOWFLAKE_DIALECT` per DEC-002; add the `POSTGRES_DIALECT` docstring note (DEC-007); refresh the `Dialect` docstring to describe the new fields.

**TDD:**
- `SNOWFLAKE_DIALECT.sample_row_hash_expr == "ABS(HASH(*))"`, `.timestamp_literal_template == "'{value}'::TIMESTAMP"`, `.date_literal_template == "'{value}'::DATE"`, `.quote_qualified_per_component is True`.
- `BIGQUERY_DIALECT.sample_row_hash_expr == "ABS(FARM_FINGERPRINT(TO_JSON_STRING(t)))"`, `.quote_qualified_per_component is False` (defaults).
- Constructing `Dialect(name=…, supports_tablesample=…, supports_qualify=…, quote_char=…, identifier_case=…)` with NO new-field args still succeeds (defaults present) — guards the existing dispatch-test construction.

**AC:** four fields added with BigQuery defaults; `SNOWFLAKE_DIALECT` values set; existing `test_models.py` / `test_public_api.py` (incl. `test_all_is_sorted`) pass unedited; validation passes.
**Done when:** the constants carry the new fields and are unit-pinned.
**Depends on:** none.

### US-002 — Consume the new `Dialect` fields in the compiler; preserve BigQuery output byte-for-byte; add `prune/` import guard

**Description.** Make `_quote`, `_qualified_table_name`, `_render_sample_cte`, and `_render_partition_filter` dialect-driven by threading `dialect` (not bare `quote_char`) through the compile helpers: fold identifiers per `identifier_case` before quoting (DEC-003); render per-component vs whole-path qualified names per `quote_qualified_per_component`; render the sample predicate from `sample_row_hash_expr`; render date/timestamp literals from the templates. All 11 existing BigQuery snapshots MUST stay byte-identical. Add the import-confinement guard (DEC-008).

**Traces to:** DEC-001, DEC-002, DEC-003, DEC-008.

**Files:**
- `src/signalforge/prune/compiler.py` — change `_quote(identifier, dialect)` to fold-then-quote; `_qualified_table_name(table_ref, dialect)` to branch on `quote_qualified_per_component` (per-component: fold+quote each of `[project?, dataset, name]`, join with `.`; whole-path: current behaviour); `_render_sample_cte(... dialect)` to use `MOD({dialect.sample_row_hash_expr}, {bucket}) < 1`; `_render_partition_filter(pf, dialect)` to use the literal templates + folded/quoted column. Thread `dialect` through `_wrap_with_sample_or_partition` and every `_compile_*` helper (they currently take `quote_char: str`). `_compile_test` already has `dialect` in scope.
- `tests/prune/test_compiler.py` — keep the existing dispatch test green (custom `identifier_case="preserve"` + default per-component=False → whole-path, unchanged); add unit assertions for the folded/per-component Snowflake shape at the `_quote` / `_qualified_table_name` level.
- `tests/prune/test_compiler_import_guard.py` (new) — AST scan: no `snowflake` / `google.cloud` import under `src/signalforge/prune/`.

**TDD:**
- **Regression (load-bearing):** all 11 `tests/fixtures/prune/compiled_sql/*.sql` BigQuery snapshots compile byte-identical (no fixture edits).
- `_quote("customer_id", SNOWFLAKE_DIALECT) == '"CUSTOMER_ID"'`; `_quote("customer_id", BIGQUERY_DIALECT) == "`customer_id`"`.
- `_qualified_table_name(TableRef("db","sch","orders"), SNOWFLAKE_DIALECT) == '"DB"."SCH"."ORDERS"'`; BigQuery whole-path unchanged.
- Sample CTE for `SNOWFLAKE_DIALECT` contains `MOD(ABS(HASH(*)), <bucket>) < 1` and NO `FARM_FINGERPRINT`.
- Partition filter for a `datetime` under `SNOWFLAKE_DIALECT` renders `'…'::TIMESTAMP`, not `TIMESTAMP('…')`.
- Import guard: planted-violation self-check (a temp module string with `import snowflake` is flagged).

**AC:** compiler reads all four new fields + `identifier_case`; BigQuery snapshots byte-identical; no `snowflake`/`google.cloud` import under `prune/`; validation passes.
**Done when:** Snowflake fragments are produced from `Dialect` with zero name-branching and BigQuery output is unchanged.
**Depends on:** US-001.

### US-003 — Snowflake snapshot fixtures + snapshot tests for all four built-ins + `custom_sql`

**Description.** Add byte-exact Snowflake snapshot fixtures and tests covering `not_null` / `unique` / `accepted_values` / `relationships` (full + sample modes) and `custom_sql` (single-table full, single-table sample, multi-table full-scan), mirroring the BigQuery snapshot set.

**Traces to:** DEC-002, DEC-003 (the AC: "Snapshot tests pin compiled Snowflake SQL for all four built-in test types + `custom_sql`").

**Files:**
- `tests/fixtures/prune/compiled_sql/snowflake/*.sql` (new dir) — Snowflake equivalents of the 11 BigQuery fixtures: `"`-quoted, per-component qualified names, UPPER-folded identifiers, `HASH(*)` sample predicate, `::TIMESTAMP`/`::DATE` literals.
- `tests/prune/test_compiler.py` — add Snowflake snapshot tests (or parametrize the existing ones over `(dialect, fixture_subdir)`), calling `_compile_test(test, ref, SNOWFLAKE_DIALECT, manifest, …)` and asserting `compiled == fixture_text`.

**TDD:**
- Each of the 11 Snowflake fixtures matches compiled output byte-for-byte.
- `custom_sql` single-table sample fixture references the `sample` CTE alias and never the source table (the #116 materialised-sample substitution invariant, now under the Snowflake quote char).
- Snowflake fixtures contain `"` and never a backtick; contain `HASH(*)` and never `FARM_FINGERPRINT`.

**AC:** all four built-ins + `custom_sql` have pinned Snowflake snapshots; tests green; validation passes.
**Done when:** Snowflake SQL is byte-pinned for every test variant.
**Depends on:** US-002.

### US-004 — fakesnow gated parse/run validation

**Description.** Add `fakesnow` as a dev/test dependency behind a registered `@pytest.mark.snowflake` marker (excluded from default `addopts`); the marked test feeds each emitted Snowflake SQL into fakesnow against a tiny in-memory table and asserts it parses/executes and returns the expected failing-row shape (DEC-005).

**Traces to:** DEC-005.

**Files:**
- `pyproject.toml` — add `fakesnow` to `[dependency-groups].dev` and `[project.optional-dependencies].dev`; register `snowflake` marker in `[tool.pytest.ini_options].markers`; add `and not snowflake` to the default `addopts` deselection list.
- `tests/prune/test_compiler_fakesnow.py` (new) — `@pytest.mark.snowflake`; build a tiny fakesnow table, run each compiled Snowflake SQL, assert it executes and the failing-row count matches an engineered fixture (engineer determinism by rule semantics, not value-equality with real Snowflake — testing-signal.md).

**TDD:**
- Each built-in's Snowflake SQL parses + runs in fakesnow without error.
- An engineered always-pass case returns zero failing rows; an engineered violation returns ≥1 (semantics, not HASH values).
- Marker is deselected by default `pytest` and selected by `-m snowflake`.

**AC:** `uv run pytest -m snowflake --no-cov` green; default run unaffected; validation passes.
**Done when:** emitted Snowflake SQL is fakesnow-parseable/executable under the gated marker.
**Depends on:** US-003.

### US-005 — Quality Gate — code review ×4 + CodeRabbit

**Description.** Run the code reviewer four times across the full changeset, fixing every real bug each pass; run CodeRabbit if available. Project validation must pass after all fixes. **Specific focus:** BigQuery snapshot byte-stability (no fixture drift), injection-safety of folding + per-component quoting, and the `custom_sql` `{{ this }}` → sample-table substitution under the Snowflake quote char (the #116 QG bug class — assert the compiled SQL references the `sample` CTE and never the source table).

**Traces to:** all DECs.
**AC:** four review passes complete, all real findings fixed; CodeRabbit clean or findings triaged; full validation green.
**Done when:** no outstanding real findings; `uv run pytest` (default) + `uv run pytest -m snowflake --no-cov` both green.
**Depends on:** US-001, US-002, US-003, US-004.

### US-006 — Patterns & Memory — conventions + 5-surface graduation

**Description.** Record the new patterns and complete the `identifier_case` reserved-surface graduation across all five surfaces (prune-engine.md § 5-surface parity).

**Traces to:** DEC-003 (graduation), DEC-001, DEC-004, DEC-006.

**Files:**
- `.claude/rules/prune-engine.md` — document the `Dialect`-extension pattern (DEC-001), the `identifier_case` consumption (graduate from "declared" to "active"), `supports_qualify` staying forward-compat (DEC-004), and the BigQuery-byte-regression discipline.
- `docs/prune-ops.md` — Snowflake compiler section; the `HASH()` cross-version reproducibility caveat (DEC-006); the case-folding behaviour + residual.
- `CLAUDE.md` — public-API surface note: `Dialect` carries the four new fragment fields; `SNOWFLAKE_DIALECT` now drives a real compiler path.
- `.claude/rules/warehouse-adapters.md` — note the `Dialect` value object now carries SQL-fragment templates (so future vendor dialects know the seam).

**AC:** all five graduation surfaces updated in lockstep; validation passes.
**Done when:** rules + docs + CLAUDE.md reflect the landed behaviour.
**Depends on:** US-005.

---

## Phase 5 — Publish PR

*(pending)*

## Beads Manifest

- **Epic:** `bd_1-scaffolding-mfa` — #121: prune compiler Snowflake dialect support
- **Worktree:** `../worktrees/SignalForge/121-prune-snowflake-dialect` (branch `feature/121-prune-snowflake-dialect`, off `dev`)
- **PR:** [#127](https://github.com/wjduenow/SignalForge/pull/127) (draft, targets `dev`)
- **Tasks** (priority P2, all under the epic):
  - `bd_1-scaffolding-mfa.1` — US-001 Extend `Dialect` + set `SNOWFLAKE_DIALECT` — *ready*
  - `bd_1-scaffolding-mfa.2` — US-002 Consume fields in compiler; preserve BQ bytes; import guard — dep: .1
  - `bd_1-scaffolding-mfa.3` — US-003 Snowflake snapshot fixtures + tests — dep: .2
  - `bd_1-scaffolding-mfa.4` — US-004 fakesnow gated parse/run — dep: .3
  - `bd_1-scaffolding-mfa.5` — Quality Gate ×4 + CodeRabbit — dep: .1 .2 .3 .4
  - `bd_1-scaffolding-mfa.6` — Patterns & Memory (5-surface graduation) — dep: .5
