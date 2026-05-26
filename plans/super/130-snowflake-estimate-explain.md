# Super Plan — #130: Snowflake `estimate_query_bytes` (EXPLAIN-based estimation)

## Meta

- **Ticket:** [#130](https://github.com/wjduenow/SignalForge/issues/130) — `feat: Snowflake estimate_query_bytes — EXPLAIN-based estimation (Phase 2 of #123)`
- **Milestone:** v0.2 (Snowflake adapter epic #118)
- **Phase:** detailing (awaiting approval)
- **Branch:** `feature/130-snowflake-estimate-explain` (based on `dev`)
- **Sessions:** 1 (2026-05-26)

---

## Phase 1 — Discovery

### What / Why / Who

**What.** Override `SnowflakeAdapter.estimate_query_bytes(sql) -> int` so `signalforge generate --estimate` renders a *real* warehouse cost projection for Snowflake profiles instead of the `<unavailable: EstimateNotSupportedError>` degrade #123 shipped. Snowflake has no BigQuery-style `dry_run` (bytes-without-billing); the closest primitive is `EXPLAIN`, which reports the planner's estimated partitions/bytes.

**Why.** #123 was Phase 1 — it confirmed the `--estimate` flow degrades cleanly for Snowflake (the ABC default raises `EstimateNotSupportedError`, the engine captures it as a supplementary failure per #36 DEC-005, the CLI renders `<unavailable: …>` and exits 0). #130 is Phase 2: replace the degrade with the actual estimate now that the connection seam exists.

**Who.** Operators running `--estimate` against a Snowflake profile who today see "unavailable" and want a cost preview before committing to a real prune scan.

### Dependency status — RESOLVED

The ticket was filed "blocked on live Snowflake connectivity (#118/#122)." **#122 merged into `dev` (commit pulled 2026-05-26).** The `SnowflakeAdapter` is now fully fleshed out:

- `__init__(..., client=None)` — injectable `_SnowflakeClientProtocol`; `None` triggers a lazy `make_real_client(...)` build.
- `_get_connection()` — lazy connection accessor (`src/signalforge/warehouse/adapters/snowflake.py`).
- `_execute(sql, *, table)` / `_execute_to_dicts(...)` — run `cursor.execute` → `fetchall`, route SDK exceptions through `map_snowflake_exception` (`raise mapped from exc`; passthrough re-raises original).
- `sample_rows` / `column_stats` / `run_test_sql` / `materialise_sample` — all implemented.
- **`estimate_query_bytes` — the ONLY method still inheriting the ABC default** (the `EstimateNotSupportedError` degrade). This is exactly #130's surface.

`estimate_query_bytes` is **stateless** — no session, no temp table, no materialisation lifecycle (unlike `materialise_sample`). It needs a single `EXPLAIN` query through a cursor, mirroring how `BigQueryAdapter.estimate_query_bytes` issues one `dry_run` query. So #130 is a clean single-method override, fully unit-testable today against the existing `FakeSnowflakeConnection.expect_execute(...)` API.

### Codebase findings (Scout)

| Surface | Location | Relevance |
|---|---|---|
| ABC default (degrade) | `warehouse/base.py:139` `estimate_query_bytes` → raises `EstimateNotSupportedError` | The method #130 overrides on `SnowflakeAdapter`. |
| BQ reference impl | `warehouse/adapters/bigquery.py:974` | `validate_test_sql(sql)` → one query → read int bytes → `map_bq_exception`. The shape to mirror. |
| Connection/exec seam | `warehouse/adapters/snowflake.py` `_get_connection` / `_execute` | How to run EXPLAIN + map exceptions. |
| Exception mapper | `warehouse/adapters/_snowflake_client.py:123` `map_snowflake_exception` | Same return convention as `map_bq_exception`. |
| Estimate engine (consumer) | `cli/_estimate.py:524` `dry_run_bytes = adapter.estimate_query_bytes(representative_sql)` inside try/except | Catches ANY exception → `warehouse_unavailable_reason = f"{type(exc).__name__}: {str(exc)[:200]}"` → renderer prints `<unavailable: <ErrorClass>>` (`_estimate.py:717`). The supplementary-failure degrade path (#36 DEC-005). **No engine change needed** — overriding the adapter is sufficient for the happy path. |
| Test fake | `tests/warehouse/_fake_snowflake.py` `FakeSnowflakeConnection.expect_execute(matching, returns, description)` | Queue an EXPLAIN response; cursor `execute`/`fetchall`/`description` already modelled. |
| Error style | `warehouse/errors.py:449` `EstimateNotSupportedError` | `default_remediation` ClassVar + `_format_value` repr-safe args + tier-3-via-`WarehouseError`. |
| Exit-code table | `cli/_helpers.py:408` `EstimateNotSupportedError: 3` | Where the new typed error registers. |

### Convention constraints (Convention Checker — `.claude/rules/`)

- **`warehouse-adapters.md`** — the governing rule. Specific obligations:
  - *"ABC graceful-degrade methods"* — non-BQ adapters override `estimate_query_bytes` "when their warehouse provides a primitive that estimates cost." Snowflake's `EXPLAIN` is named explicitly as the v0.3+ override path.
  - *one-shim-per-vendor* — the `snowflake.connector` import stays confined to `_snowflake_client.py`. The adapter calls `_get_connection()` / `map_snowflake_exception`; it must NOT import the connector.
  - *errors carry remediation* — every new `WarehouseError` subclass ships a `default_remediation` ClassVar; user strings render via `_format_value` (`repr()`).
  - *`__repr__` redaction* — unchanged; estimate adds no new credential surface.
  - *"Verify each non-BQ adapter's degrade with its adapter-specific NotSupported error (issue #123)"* — **this is the surface #130 changes**, so the #123 engine + CLI degrade tests keyed on `EstimateNotSupportedError` MUST be rewritten (see DEC-007).
  - *"adapter emits sparing WARNING only on deviation"* — no new routine logging; the degrade WARNING already lives in the estimate engine (#36 DEC-005).
- **`cli-layer.md`** — 7th AST scan (`test_every_typed_error_is_in_exit_code_mapping_table`) requires every concrete `*Error` to have an explicit `_EXCEPTION_TO_EXIT_CODE` entry. A new `WarehouseError` subclass must be registered (tier 3) even though `WarehouseError` already has a fallback entry.
- **`testing-signal.md`** — workers can't run live Snowflake. Hand-craft a captured `EXPLAIN USING JSON` fixture; pin the pure parser against it; gate the live call behind `@pytest.mark.snowflake` with a maintainer-only regen note (the e2e hand-crafted-seed pattern). Engineer determinism: assert the parsed int equals the fixture's `bytesAssigned`, never a live `HASH`/planner value.
- **`prune-engine.md` / `warehouse-adapters.md` (#121)** — the "a new dialect's SQL needs a parser/executor in the loop, not just snapshot equality" lesson: the gated `@pytest.mark.snowflake` live/`fakesnow` path is the validity check; the fixture pins shape only.

### Scoping answers

- **Q (scope vs blocker):** resolved by reality — #122 merged, so this is a clean single-method override, not a blocked/deferred build.
- **Q (EXPLAIN format):** **`EXPLAIN USING JSON` → parse `GlobalStats.bytesAssigned`** (DEC-001).
- **Q (missing-stat fallback):** **raise a typed `WarehouseError` → graceful degrade** via the existing #36 supplementary-failure path (DEC-002).

---

## Phase 2 — Architecture Review

| Area | Rating | Finding |
|---|---|---|
| Security | **pass** | `validate_test_sql(sql)` gates injection (rejects `;`, `--`, unbalanced parens) BEFORE the `EXPLAIN USING JSON ` prefix is prepended (DEC-004). No new credential surface; `__repr__` redaction unchanged. EXPLAIN does not scan or mutate data. |
| Performance | **pass** | `EXPLAIN` is a planner-only call — no partition scan, no bytes billed. One call per `--estimate` invocation (the engine makes exactly one `estimate_query_bytes` call, #36). |
| Data model | **pass** | No schema/migration. One new typed error (`EstimateUnavailableError`) + one captured fixture. |
| API design | **pass** | Overrides an existing ABC method; same `int`-bytes contract the `--estimate` engine already consumes. No signature change. |
| Observability | **pass (note)** | No new routine logging (adapter convention: sparing WARNING only on deviation). The degrade case is surfaced by the estimate engine's existing WARNING (#36 DEC-005). |
| Testing | **concern → addressed** | Live Snowflake unavailable to workers → hand-crafted fixture + pure-parser unit test + gated live test (DEC-006). The three #123 degrade tests asserting `EstimateNotSupportedError` break and must be rewritten (DEC-007) — a real `SnowflakeAdapter()` with no client now attempts a lazy `make_real_client` connect (failing with a connection error, not `EstimateNotSupportedError`), so those tests must inject a fake instead. |

**No blockers.** One concern (testing), addressed by DEC-006/DEC-007.

---

## Phase 3 — Refinement (Decisions)

**DEC-001 — `EXPLAIN USING JSON`, parse `GlobalStats.bytesAssigned`.** *(user)* Run `EXPLAIN USING JSON <validated-sql>`; the result is a single row / single cell carrying a JSON document with a top-level `GlobalStats` object (`partitionsTotal`, `partitionsAssigned`, `bytesAssigned`). Parse `GlobalStats.bytesAssigned` as the `int`-bytes estimate. *Rationale:* machine-readable, stable, single-cell — trivially fixture-pinnable; maps directly to the int contract (mirrors BQ `total_bytes_processed`). Tabular EXPLAIN's column layout drifts across Snowflake releases and needs row aggregation.

**DEC-002 — missing/unparseable `bytesAssigned` → raise typed error → graceful degrade.** *(user)* When EXPLAIN succeeds but the plan lacks a parseable `GlobalStats.bytesAssigned` (metadata-only query, plan-shape change, malformed cell), raise a typed `WarehouseError`. The estimate engine catches it as a supplementary failure and renders `<unavailable: …>`. *Rationale:* never fabricate a number; a `return 0` would conflate "metadata-only" with "parser broke" and silently report `$0` cost on a future plan-shape change.

**DEC-003 — new typed error `EstimateUnavailableError(WarehouseError)`, CLI tier 3.** Distinct from `EstimateNotSupportedError` (which means "this adapter does no estimates at all"). `EstimateUnavailableError` means "the adapter supports estimation but couldn't extract the figure for THIS query." Carries a `default_remediation` ClassVar; args render via `_format_value`. Register explicitly in `_EXCEPTION_TO_EXIT_CODE` (tier 3) — required by scan-7 even though `WarehouseError`'s fallback is also tier 3. The estimate engine's `f"{type(exc).__name__}: …"` capture renders `<unavailable: EstimateUnavailableError>`.

**DEC-004 — validate inner SQL, then prepend `EXPLAIN USING JSON `.** Call `validate_test_sql(sql)` on the caller-supplied SQL first (mirrors BQ), THEN build `f"EXPLAIN USING JSON {sql}"` to execute. The `;`/`--`/paren rejects apply to the user SQL; the literal `EXPLAIN USING JSON ` prefix is trusted constant text. *Rationale:* the injection boundary is the user SQL, not our prefix.

**DEC-005 — route SDK exceptions through `map_snowflake_exception`.** The EXPLAIN execution mirrors `_execute`: `cursor.execute(...)` / `fetchall()` in a `try`, map any exception via `map_snowflake_exception(exc, context={...})`, `raise mapped from exc` (passthrough re-raises original). The estimate engine then catches the mapped `WarehouseError` as a supplementary failure. *(Reuse `_execute`/`_execute_to_dicts` rather than duplicating the cursor dance — see DEC-008.)*

**DEC-006 — pure parser + fixture + gated live test.** A module-level pure function `_parse_explain_json_bytes(cell) -> int` does all parsing (accepts the result cell whether the connector returns a `str` or a pre-parsed `dict`; raises `EstimateUnavailableError` on missing `GlobalStats`/`bytesAssigned`/non-int). Pinned against a hand-crafted `tests/fixtures/warehouse/snowflake/explain_using_json_sample.json` (documented shape; maintainer regen note for live capture). Unit tests drive `estimate_query_bytes` via `FakeSnowflakeConnection.expect_execute(matching=r"^EXPLAIN USING JSON", returns=[...], description=[...])`. A `@pytest.mark.snowflake`-gated test exercises a real `EXPLAIN USING JSON` (maintainer-only, `uv run pytest -m snowflake --no-cov`). *Rationale:* `testing-signal.md` — workers can't run live Snowflake; the fixture pins shape, the gated test certifies validity.

**DEC-007 — rewrite the three #123 degrade tests.** #123 pinned the Snowflake degrade with a real `SnowflakeAdapter()` (no client) asserting `<unavailable: EstimateNotSupportedError>`. #130 changes that behaviour:
- `tests/warehouse/test_snowflake_stub.py::test_estimate_query_bytes_raises_not_supported` → replace with `test_estimate_query_bytes_returns_explain_bytes` (inject fake, assert real int) + `test_estimate_query_bytes_degrades_on_missing_stat` (inject fake whose EXPLAIN cell lacks `bytesAssigned`, assert `EstimateUnavailableError`).
- `tests/cli/test_estimate_engine.py` Snowflake-degrade test (`:359`) → inject a `FakeSnowflakeConnection` returning a captured EXPLAIN; assert the engine reports a real `estimated_bytes` (no degrade), OR a paired test injecting a no-stat EXPLAIN → `warehouse_unavailable_reason.startswith("EstimateUnavailableError:")`.
- `tests/cli/test_generate_estimate.py` Snowflake-degrade test (`:349`) → CLI-level: inject fake → stdout shows a real estimate; paired no-stat test → `<unavailable: EstimateUnavailableError>`, exit 0, no-traceback floor.
Update each test's docstring + the `warehouse-adapters.md` #123 note that says Snowflake inherits the degrade.

**DEC-008 — reuse `_execute_to_dicts`/`_execute` for the EXPLAIN call.** Don't duplicate the cursor `execute`/`fetchall`/exception-map dance. `estimate_query_bytes` builds the EXPLAIN SQL, calls the existing helper to get the result cell, then hands it to `_parse_explain_json_bytes`. The estimate has no `TableRef` in scope — pass a synthetic/None-tolerant context to the helper (small refactor: let the helper accept `table: TableRef | None` for context, or add a thin `_execute_scalar(sql)` sibling). *Rationale:* one cursor-handling path; the parser stays a pure, separately-testable unit.

**DEC-009 — 5-surface doc parity (graduation #123→#130).** Update in lockstep: (1) `warehouse-adapters.md` (Snowflake now overrides `estimate_query_bytes`; #123 degrade note corrected), (2) `docs/warehouse-adapter-ops.md` § Query-bytes estimation (Snowflake = real EXPLAIN estimate; `EstimateUnavailableError` degrade), (3) `CLAUDE.md` public-API surface (`EstimateUnavailableError` added; Snowflake no longer raises `EstimateNotSupportedError`), (4) tests (DEC-006/007), (5) this plan's DEC list. Note the **estimate-accuracy caveat**: `EXPLAIN` figures are planner *estimates* and may differ from actual scanned bytes / vary across Snowflake releases (mirrors the `HASH()` reproducibility caveat from #121).

---

## Phase 4 — Detailed Breakdown (stories)

> Validation command (every story's AC): `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

### US-001 — `EstimateUnavailableError` typed error + exit-code registration
- **Description:** Add `EstimateUnavailableError(WarehouseError)` to `warehouse/errors.py` with a locked `default_remediation`, repr-safe args, and the `__all__` export. Register it in `cli/_helpers.py` `_EXCEPTION_TO_EXIT_CODE` at tier 3 and the import block.
- **Traces to:** DEC-003.
- **TDD:** test class exists + `WarehouseError` subclass; `__str__` renders message + `↳ Remediation:`; remediation locked-verbatim test; `map_exception_to_exit_code(EstimateUnavailableError(...)) == 3`; scan-7 (`test_every_typed_error_is_in_exit_code_mapping_table`) passes.
- **Files:** `src/signalforge/warehouse/errors.py`, `src/signalforge/warehouse/__init__.py`, `src/signalforge/cli/_helpers.py`, `tests/warehouse/test_errors.py`, `tests/cli/test_exit_codes.py`.
- **AC:** new error registered + exported; validation passes.
- **Done when:** scan-7 green with the new concrete; tier-3 mapping pinned.
- **Depends on:** none.

### US-002 — pure `_parse_explain_json_bytes` parser + fixture
- **Description:** Add a module-level pure function in `warehouse/adapters/snowflake.py` that takes an `EXPLAIN USING JSON` result cell (`str` JSON or pre-parsed `dict`) and returns `int(GlobalStats.bytesAssigned)`, raising `EstimateUnavailableError` on missing `GlobalStats`/`bytesAssigned`/non-int/unparseable JSON. Ship `tests/fixtures/warehouse/snowflake/explain_using_json_sample.json` (hand-crafted, documented shape) + a no-stat variant.
- **Traces to:** DEC-001, DEC-002, DEC-006.
- **TDD:** parses fixture → expected int; accepts dict input; missing `GlobalStats` → `EstimateUnavailableError`; missing `bytesAssigned` → raises; non-int `bytesAssigned` → raises; malformed JSON str → raises; the locked fixture int is asserted (engineered determinism).
- **Files:** `src/signalforge/warehouse/adapters/snowflake.py`, `tests/fixtures/warehouse/snowflake/*.json`, `tests/warehouse/test_snowflake_estimate.py` (new).
- **AC:** pure parser fully covered; no connection needed in these tests.
- **Done when:** parser raises typed error on every malformed shape; fixture pinned.
- **Depends on:** US-001.

### US-003 — `SnowflakeAdapter.estimate_query_bytes` override
- **Description:** Override `estimate_query_bytes(sql) -> int`: `validate_test_sql(sql)` → build `f"EXPLAIN USING JSON {sql}"` → run via the existing cursor helper (DEC-008, exceptions through `map_snowflake_exception`) → `_parse_explain_json_bytes(cell)`. Update the adapter module docstring (estimate is now implemented; remove the "inherits ABC default" note).
- **Traces to:** DEC-001, DEC-004, DEC-005, DEC-008.
- **TDD:** inject `FakeSnowflakeConnection.expect_execute(r"^EXPLAIN USING JSON", returns=<fixture cell>)` → returns expected int; SQL with `;` → `validate_test_sql` rejects before any cursor call; connector exception → mapped `WarehouseError` re-raised `from`; EXPLAIN SQL string starts with `EXPLAIN USING JSON ` and embeds the validated SQL verbatim.
- **Files:** `src/signalforge/warehouse/adapters/snowflake.py`, `tests/warehouse/test_snowflake_estimate.py`.
- **AC:** happy path returns real bytes via the fake; injection guard intact; validation passes.
- **Done when:** the method no longer inherits the ABC degrade; fake-driven happy + error paths pinned.
- **Depends on:** US-002.

### US-004 — rewrite #123 degrade tests (engine + CLI + stub)
- **Description:** Update the three tests that assert `SnowflakeAdapter()` degrades with `EstimateNotSupportedError` to the new reality: happy path returns a real estimate (fake injected); a no-stat EXPLAIN degrades with `EstimateUnavailableError`. Fix docstrings.
- **Traces to:** DEC-007.
- **TDD:** `test_snowflake_stub.py` — replace the not-supported assertion with returns-bytes + degrades-on-missing-stat. `test_estimate_engine.py` — Snowflake engine path reports real `estimated_bytes` (fake), plus a no-stat path → `warehouse_unavailable_reason.startswith("EstimateUnavailableError:")`. `test_generate_estimate.py` — CLI stdout shows the real estimate; no-stat path → `<unavailable: EstimateUnavailableError>`, exit 0, `"Traceback" not in err`.
- **Files:** `tests/warehouse/test_snowflake_stub.py`, `tests/cli/test_estimate_engine.py`, `tests/cli/test_generate_estimate.py`.
- **AC:** no test asserts Snowflake raises `EstimateNotSupportedError`; new behaviour pinned at adapter + engine + CLI levels.
- **Done when:** full suite green; the #123 class-name-keyed assertions are replaced, not deleted-and-forgotten.
- **Depends on:** US-003.

### US-005 — gated live `@pytest.mark.snowflake` test + maintainer regen note
- **Description:** Add a `@pytest.mark.snowflake`-gated test that runs a real `EXPLAIN USING JSON` against a live Snowflake (skipped unless env/marker present) and asserts a positive int. Document the fixture-regen command (maintainer-only).
- **Traces to:** DEC-006.
- **TDD:** gated test skips cleanly without creds (belt-and-suspenders: marker + runtime skip-reason); asserts `estimate_query_bytes(...) > 0` and that the captured plan matches the committed fixture shape.
- **Files:** `tests/prune/test_compiler_fakesnow.py`-style gated module (or `tests/warehouse/test_snowflake_estimate_live.py`), regen note alongside the fixture.
- **AC:** `uv run pytest` (default) deselects it; `uv run pytest -m snowflake --no-cov` collects it.
- **Done when:** marker registered; runtime skip names the missing prerequisite.
- **Depends on:** US-003.

### US-006 — 5-surface docs parity
- **Description:** Update `docs/warehouse-adapter-ops.md` § Query-bytes estimation (Snowflake = real EXPLAIN estimate + `EstimateUnavailableError` degrade + accuracy caveat), `CLAUDE.md` public-API surface (`EstimateUnavailableError`; Snowflake no longer raises `EstimateNotSupportedError`), and the `warehouse-adapters.md` #123 note.
- **Traces to:** DEC-009.
- **Files:** `docs/warehouse-adapter-ops.md`, `CLAUDE.md`, `.claude/rules/warehouse-adapters.md`.
- **AC:** all surfaces name the new behaviour + error consistently; planner-estimate caveat documented.
- **Done when:** docs build (`uv run --only-group docs mkdocs build`) clean; no stale "inherits the degrade" claim for Snowflake.
- **Depends on:** US-004.

### US-007 — Quality Gate (code review ×4 + CodeRabbit)
- **Description:** Run the code reviewer 4 passes across the full changeset, fixing every real bug each pass; run CodeRabbit if available. Validation must pass after fixes.
- **Depends on:** US-001…US-006.

### US-008 — Patterns & Memory (priority 99)
- **Description:** Distill into `.claude/rules/warehouse-adapters.md` the generalised pattern: "a graceful-degrade ABC method graduates per-adapter via the warehouse's native primitive; pin the parse with a hand-crafted fixture + gated live test; flipping a degrade means rewriting the prior phase's degrade tests, not deleting them." Update memory if a non-obvious gotcha surfaced.
- **Depends on:** US-007.

---

## Rules compliance gate

- one-shim-per-vendor: estimate calls `_get_connection()`/`map_snowflake_exception`; **no `snowflake.connector` import in the adapter** ✓ (US-003).
- errors carry remediation + `_format_value` ✓ (US-001).
- scan-7 explicit exit-code registration ✓ (US-001).
- no new routine logging (degrade WARNING owned by engine) ✓.
- hand-crafted fixture + gated live test (workers can't run live Snowflake) ✓ (US-002/005).
- 5-surface parity for the #123→#130 graduation ✓ (US-006).

---

## Beads Manifest

*(filled on devolve)*
- Epic: —
- Tasks: —
- Worktree: `feature/130-snowflake-estimate-explain`
