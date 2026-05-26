# Super Plan — #123: Snowflake `estimate_query_bytes` degrade-first

## Meta

- **Ticket:** [#123](https://github.com/wjduenow/SignalForge/issues/123) — feat: Snowflake estimate_query_bytes — degrade-first (EstimateNotSupportedError), EXPLAIN follow-up
- **Epic:** #118 (Snowflake adapter)
- **Milestone:** v0.2
- **Branch:** `feature/123-snowflake-estimate-degrade` (off `dev`)
- **Phase:** published (PR [#128](https://github.com/wjduenow/SignalForge/pull/128), awaiting approval)
- **Sessions:** 1 (2026-05-26)

---

## Phase 1 — Discovery

### What / Why / Who

- **What:** Confirm that `signalforge generate --estimate` degrades cleanly for a Snowflake profile — `estimate_query_bytes` raises `EstimateNotSupportedError` (the ABC default, which `SnowflakeAdapter` inherits), the `--estimate` engine catches it as a supplementary failure, and the run renders `<unavailable: EstimateNotSupportedError>` without failing.
- **Why:** Snowflake has no BigQuery-style dry-run (no `dry_run=True` returning bytes-without-billing). The closest is `EXPLAIN` (estimated partitions/bytes), which is deferred. Until then, the cost-preview flow must surface the typed error gracefully rather than crash — the v0.2 graceful-degrade contract for non-BigQuery adapters (`warehouse-adapters.md` § graceful-degrade ABC methods; #36 DEC-004/005).
- **Who:** Operators running `--estimate` against a Snowflake `profiles.yml`.

### Acceptance criteria (from the ticket)

1. `--estimate` against a Snowflake profile shows `<unavailable: EstimateNotSupportedError>` (or equivalent) without failing the run.
2. Remediation on `EstimateNotSupportedError` names the v0.x graduation path, locked + pinned (mirror the BQ-only error precedent).

### Codebase findings (already in place — this is largely a confirm-and-pin ticket)

| Surface | State |
|--|--|
| `SnowflakeAdapter.estimate_query_bytes` | **Not overridden** — inherits the ABC default in `warehouse/base.py:139` which raises `EstimateNotSupportedError(adapter_name=type(self).__name__)`. Documented intent in `adapters/snowflake.py` docstring + `warehouse-adapters.md`. |
| ABC degrade error | `EstimateNotSupportedError(WarehouseError)` in `warehouse/errors.py:449`. `default_remediation` locked verbatim: `"Use --estimate with a BigQuery profile, or wait for v0.3 multi-warehouse estimation support."` |
| Remediation pin | `tests/warehouse/test_bigquery_estimate.py:220::test_estimatenotsupportederror_remediation_locked_verbatim` — **AC #2 already satisfied.** |
| `--estimate` engine catch | `cli/_estimate.py:523` — `try: adapter.estimate_query_bytes(...) except WarehouseError as exc:` sets `warehouse_unavailable_reason = f"{type(exc).__name__}: {str(exc)[:200]}"`, emits one lazy-format JSON WARNING, continues. |
| Renderer | `cli/_estimate.py::render` — when `warehouse_unavailable_reason is not None`, prints `bytes-per-row: <unavailable: <ErrorClass>>`, `total bytes: <unknown>`, `Total estimated warehouse: <unknown>`. ErrorClass = first `:`-chunk of the reason. |
| `_build_representative_sql` | Calls only `adapter.dialect()` (Snowflake returns `SNOWFLAKE_DIALECT` — works) + `TableRef.from_model`. Never calls `sample_rows`/`column_stats`/`run_test_sql` (the `NotImplementedError` skeleton methods), so the only Snowflake-specific call on the `--estimate` path is `estimate_query_bytes`. |
| `from_profile` dispatch | `profile.type == "snowflake"` → `SnowflakeAdapter` (post-#120 unified `DbtProfileTarget` parses Snowflake targets). |
| Snowflake profile fixture | `tests/fixtures/profiles/snowflake_password.yml` (from #120). |
| Docs | `docs/warehouse-adapter-ops.md` lines 365–404 already describe the Snowflake `estimate_query_bytes` → `EstimateNotSupportedError` degrade + the locked remediation + v0.2→v0.3 migration story. |

### The actual gap

No test exercises a **real `SnowflakeAdapter`** through the `estimate(...)` engine or the `--estimate` CLI asserting that **`EstimateNotSupportedError` specifically** degrades cleanly. The existing degrade test (`tests/cli/test_estimate_engine.py::test_estimate_degrades_on_warehouse_auth_error_and_continues`) uses a fake adapter raising `WarehouseAuthError`. AC #1 ("against a Snowflake profile") is therefore unproven end-to-end.

### Scoping answers (this session)

- **Test depth:** Engine + full CLI (both `estimate(...)` engine-level and in-process `main(["generate","--estimate",...])`).
- **Override?** Keep inheriting the ABC default — **zero production code change.** (Matches the skeleton's documented intent and `warehouse-adapters.md`: "`estimate_query_bytes` are NOT overridden — the ABC's typed degrade is the correct v0.2 behaviour.")
- **EXPLAIN (Phase 2):** Defer; file a follow-up issue. Keeps this issue small per the ticket.

---

## Phase 2 — Architecture Review

This ticket adds **tests + a one-line doc cross-reference only** (no production code change — DEC-001). Review areas are correspondingly light.

| Area | Rating | Finding |
|--|--|--|
| Security | pass | No production change. Test inputs are repo fixtures. |
| Performance | pass | No production change; tests are fast (no network — `SnowflakeAdapter.estimate_query_bytes` raises before any connection; `count_tokens` is faked). |
| Data model | pass | No schema/model change. |
| API design | pass | Public surface unchanged. `estimate_query_bytes` ABC contract already shipped (#36). |
| Observability | pass | Degrade WARNING already emitted + pinned by `test_estimate_emits_warning_on_warehouse_degrade`. New tests assert the rendered `<unavailable:>` shape. |
| Testing strategy | pass | Engine-level + in-process CLI test, plus the existing unit pin in `test_snowflake_stub.py`. |

**Blockers:** none. **Concerns:** none.

### Rules consulted (`.claude/rules/`)

- **`warehouse-adapters.md`** — "ABC graceful-degrade methods": default impl raises typed `<Name>NotSupportedError`; orchestrator catches + degrades, never propagates. Both errors → CLI tier 3, remediations locked verbatim + pinned. → drives DEC-001, DEC-005.
- **`cli-layer.md`** — "Multi-source CLI commands degrade on supplementary failures" (#36 DEC-005): supplementary (`estimate_query_bytes`) failures captured as `*_unavailable_reason`, never propagate; pin BOTH the report field AND the WARNING. No-traceback floor (DEC-016). → drives DEC-002, DEC-005.
- **`testing-signal.md`** — no `assert True`-shaped tests; each test must be able to fail. Assert on the **specific** `EstimateNotSupportedError` class name, not the generic `WarehouseError`, so a future narrowing of the catch is caught. → drives DEC-005.

---

## Phase 3 — Refinement Log (Decisions)

### DEC-001 — Keep inheriting the ABC default; no production override
`SnowflakeAdapter` does **not** override `estimate_query_bytes`. The ABC default raise (`warehouse/base.py:139`) is the correct v0.2 behaviour and is the documented skeleton convention (`adapters/snowflake.py` docstring; `warehouse-adapters.md`). Overriding would duplicate the ABC body and deviate from the rule. **This ticket ships zero production code.**

### DEC-002 — Verify at two levels: engine + in-process CLI
- **Engine level** (`tests/cli/test_estimate_engine.py`): construct a real `SnowflakeAdapter()`, run `estimate(...)`, assert `warehouse_unavailable_reason` starts with `"EstimateNotSupportedError:"` and that the LLM-cost half still computes (`total_llm_usd > 0`, `warehouse_total_bytes is None`).
- **CLI level** (`tests/cli/test_generate_estimate.py`): in-process `main(["generate", "--estimate", <model>, ...])` with `_make_warehouse_adapter` patched to a real `SnowflakeAdapter()` and `_make_anthropic_client` patched to a `FakeAnthropicClient` (count_tokens queued). Assert exit 0, stdout contains `<unavailable: EstimateNotSupportedError>`, **no `"Traceback"` in stderr** (DEC-016 floor).

### DEC-003 — `SnowflakeAdapter()` needs no fake client on the estimate path
`estimate_query_bytes` raises before any connection, and `--estimate` never invokes the `NotImplementedError` skeleton methods (`_build_representative_sql` uses only `dialect()` + `TableRef.from_model`). So the test constructs a bare `SnowflakeAdapter()` (or with the `snowflake_password.yml`-derived params) — no `_snowflake_client` / fake connection required. The CLI test patches `_make_warehouse_adapter` directly (mirrors the existing estimate-test seam in `tests/cli/test_generate_estimate.py::_install_estimate_patches`), so it does not depend on `load_profile` parsing the Snowflake fixture; an optional secondary assertion can additionally route through `WarehouseAdapter.from_profile(load_profile(...))` against `snowflake_password.yml` to prove the dispatch wiring.

### DEC-004 — Defer EXPLAIN-based estimation; file a follow-up
Phase 2 of the ticket (EXPLAIN parsing `bytesAssigned`/partitions) is **out of scope** — it needs live Snowflake connectivity (#118/#122), which isn't built. The Patterns & Memory story files a tracking issue and records the deferral in `warehouse-adapter-ops.md`'s migration-story section.

### DEC-005 — Assert on the specific error class, with no-traceback + exit-0 floor
Tests assert `EstimateNotSupportedError` by name (engine: `warehouse_unavailable_reason.startswith("EstimateNotSupportedError:")`; CLI: `"<unavailable: EstimateNotSupportedError>" in stdout`). A generic `WarehouseError` assertion would still pass if a refactor narrowed the engine's `except` — the specific-class assertion is the signal. The CLI test also pins exit 0 and the `"Traceback" not in stderr` floor.

---

## Phase 4 — Detailed Breakdown

> **Validation command (every story):** `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

### US-001 — Engine-level Snowflake degrade test
- **Description:** Add a test to `tests/cli/test_estimate_engine.py` that wires a real `SnowflakeAdapter()` through `estimate(...)` and asserts the warehouse-bytes section degrades on `EstimateNotSupportedError` while the LLM-cost half still computes.
- **Traces to:** DEC-001, DEC-002, DEC-003, DEC-005.
- **Acceptance criteria:**
  - New test `test_estimate_degrades_on_snowflake_estimate_not_supported` (or similar) constructs `SnowflakeAdapter()` + a `FakeAnthropicClient` with count_tokens queued (one drafter + one per `DEFAULT_RUBRIC` criterion).
  - Asserts `report.warehouse_unavailable_reason is not None` and `.startswith("EstimateNotSupportedError:")`.
  - Asserts `report.warehouse_total_bytes is None` and `report.warehouse_bytes_per_row is None`.
  - Asserts `report.total_llm_usd > 0` (the LLM half is unaffected).
  - Asserts `render(report)` contains `<unavailable: EstimateNotSupportedError>` and `Total estimated warehouse: <unknown>`.
  - Validation command passes.
- **Done when:** the new engine test passes and fails if the engine's `except WarehouseError` is narrowed to exclude `EstimateNotSupportedError`.
- **Files:** `tests/cli/test_estimate_engine.py` (add test; reuse existing `make_model`/`make_manifest` factories + `FakeAnthropicClient`/`FakeCountTokensResponse`).
- **Depends on:** none.
- **TDD:** test-only story; the assertions above ARE the test cases.

### US-002 — CLI `--estimate` against a Snowflake profile test
- **Description:** Add an in-process `main(["generate", "--estimate", ...])` test that patches `_make_warehouse_adapter` to a real `SnowflakeAdapter()` and proves the full short-circuit degrades cleanly, plus a doc cross-reference confirming the Snowflake degrade is verified.
- **Traces to:** DEC-002, DEC-003, DEC-005.
- **Acceptance criteria:**
  - New test in `tests/cli/test_generate_estimate.py` reuses/extends `_install_estimate_patches` to inject a real `SnowflakeAdapter()` (via a `fake_adapter=` parameter or a Snowflake-specific patch helper) and a `FakeAnthropicClient` with count_tokens queued.
  - `main([...])` returns exit code 0.
  - Captured stdout contains `<unavailable: EstimateNotSupportedError>`.
  - `"Traceback" not in` captured stderr (DEC-016 floor).
  - (Optional secondary assertion) `WarehouseAdapter.from_profile(load_profile(<snowflake_password.yml>))` returns a `SnowflakeAdapter`, pinning the dispatch wiring.
  - `docs/warehouse-adapter-ops.md` migration-story section gains a one-line note that the Snowflake `--estimate` degrade is verified end-to-end (ref #123).
  - Validation command passes.
- **Done when:** the CLI test passes and the doc note is present.
- **Files:** `tests/cli/test_generate_estimate.py` (add test; may add a small Snowflake patch helper); `docs/warehouse-adapter-ops.md` (one-line cross-reference).
- **Depends on:** US-001 (shared understanding; not strictly ordered, but keeps the engine pin first).
- **TDD:** test-only; assertions above ARE the test cases.

### US-003 — Quality Gate
- **Description:** Run the code reviewer 4× across the changeset, fixing all real issues each pass; run CodeRabbit if available; ensure validation passes.
- **Traces to:** all stories.
- **Acceptance criteria:** 4 reviewer passes complete with all real bugs fixed; CodeRabbit clean (or findings triaged); `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` passes.
- **Done when:** no outstanding real findings; validation green.
- **Files:** as needed from review.
- **Depends on:** US-001, US-002.

### US-004 — Patterns & Memory (priority 99)
- **Description:** File the EXPLAIN follow-up issue (DEC-004), record the deferral, and capture any new pattern.
- **Traces to:** DEC-004.
- **Acceptance criteria:**
  - A GitHub follow-up issue is filed for EXPLAIN-based Snowflake estimation (parsing `bytesAssigned`/partitions), linked from #123, noting the dependency on live Snowflake connectivity (#118/#122).
  - `docs/warehouse-adapter-ops.md` notes the deferral pointer (EXPLAIN tracked separately).
  - If a reusable pattern emerged (e.g. "verify each non-BQ adapter's `--estimate` degrade with the adapter-specific NotSupported error"), add a one-line note to `.claude/rules/warehouse-adapters.md` and/or auto-memory.
  - Validation command passes.
- **Done when:** follow-up issue filed + linked; docs/rules updated.
- **Files:** `docs/warehouse-adapter-ops.md`, possibly `.claude/rules/warehouse-adapters.md`.
- **Depends on:** US-003.

---

## Phase 7 — Beads Manifest

_(filled on devolve)_
