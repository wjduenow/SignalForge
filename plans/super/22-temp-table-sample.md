# Issue #22 — Adopt Q4=C (temp-table-materialised sample) for v0.2 sample-mode prune

> Source: <https://github.com/wjduenow/SignalForge/issues/22>
> Branch: `feature/22-temp-table-sample` (worktree at `../worktrees/SignalForge/22-temp-table-sample`)
> Base: `dev`

## Meta

- **Phase:** published
- **PR:** <https://github.com/wjduenow/SignalForge/pull/30>
- **Sessions:** 1
- **Last session:** 2026-05-05
- **Owner:** wjduenow

---

## Discovery

### What

Adopt Q4=C — materialise the deterministic 100k-row sample once per prune run into a temp `TableRef`, then compile each candidate test against the materialised table instead of the production model. Add a `prune.sample_strategy: Literal["oneshot", "materialised"]` config flag (default `materialised`) so the v0.1 Q4=A path is retained for debugging. Re-run AR-B1 to record the post-Q4=C cost figure in `docs/prune-ops.md`.

### Why

US-003 / AR-B1 probe ran 2026-05-01 against `bigquery-public-data.iowa_liquor_sales.sales`:

- BigQuery analyzer estimated **9,924,771,840 bytes (~9.92 GB)** for one 100k-row deterministic sample — **~99× the Phase-1 estimate**.
- Root cause: `WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), bucket) < 1` in `sample_rows()` — `TO_JSON_STRING(t)` serialises the entire row, so BigQuery cannot column-prune through the predicate. Sample-mode reads **all columns** of the table for **every test**.
- A 30-test run on a 24-column 30M-row table = ~30 × 9.92 GB ≈ **297 GB**, well past the 1 TB/month free tier on a single model.
- The 100 MB `maximum_bytes_billed` cap (DEC-005 of #3) blocks execution today — the safety net works, but sample-mode is unusable until materialisation lands.

Q4=C amortises the full-row scan over all tests in the candidate set. Per-test cost drops to ~`(sample_rows × bytes_per_test_column)` — the figure Phase-1 originally assumed.

### Acceptance criteria (verbatim from issue + implicit)

- [ ] Per-test `bytes_billed` for the AR-B1 probe target drops below 100 MB without raising `cost_limit_bytes`.
- [ ] `docs/prune-ops.md` Cost model section records the post-Q4=C figure + run date.
- [ ] Q4=A path retained behind a config flag, defaulting OFF.
- [ ] No change to `DropReason` taxonomy or audit JSONL schema (strategy switch is below those layers).
- [ ] *(Implicit)* Probe re-run via `SF_RUN_BQ=1 pytest -m bigquery tests/warehouse/test_sample_cost_probe.py --no-cov`.
- [ ] *(Implicit)* Probe's `_BYTES_WARN_AT = 500_000_000` warn threshold stays quiet for normal `materialised` runs.

### Codebase findings (Subagent B)

- **Adapter seam** — `src/signalforge/warehouse/adapters/bigquery.py:326-413`: `sample_rows()` builds the deterministic sample SQL today; called via `_default_job_config(stage="warehouse_sample")` (line 398). Will need a sibling stage label `warehouse_sample_materialise` (DEC-015 of #3 — every query routes through `_default_job_config`).
- **Client shim** — `src/signalforge/warehouse/adapters/_client.py:30-46`: `_BQClientProtocol` exposes `query/get_table/list_rows`. No session/temp-table surface yet. Will extend (DEC-012 of #5 — every BQ SDK ignore confined here).
- **ABC** — `src/signalforge/warehouse/base.py:40-83`: 5 abstract methods today. No materialisation surface; v0.2 adds one (vendor-parity per DEC-001 of #3).
- **Compile seam** — `src/signalforge/prune/compiler.py:92`: `_compile_test()` returns SQL string OR `_RequiresFutureData` / `_InvalidIdentifier` sentinels. Compiles against `TableRef.from_model(model)` today; v0.2 swaps to the materialised `TableRef` when strategy=materialised.
- **Total-budget watchdog** — `src/signalforge/prune/engine.py`: budget gate runs around the per-test loop. Materialisation must complete BEFORE the loop so the cost is amortised; budget exhaustion mid-materialisation routes remaining tests to `kept-without-evidence` (DEC-011 of #6).
- **Config** — `src/signalforge/prune/config.py:62-133`: `PruneConfig` uses `extra="forbid"` (line 74); fields: `scope`, `sample_size`, `test_timeout_seconds`, `total_budget_seconds`, `capture_failure_rows`, `trusted_models`, `partition_filter`. New `sample_strategy` field slots in cleanly.
- **Test fakes** — `tests/warehouse/_fake.py:95-115`: `expect_query` / `expect_get_table` / `expect_list_rows` exist. A new `expect_query(matching=r"^CREATE.*TEMP.*TABLE")` works without a new helper, but a dedicated `expect_materialise_sample` is cleaner for the unit-test surface.
- **Probe test** — `tests/warehouse/test_sample_cost_probe.py`: gated `@pytest.mark.bigquery`, `SF_RUN_BQ=1`. `_BYTES_CEILING = 5_000_000_000`, `_BYTES_WARN_AT = 500_000_000`. `--no-cov` required (per `testing-signal.md` Coverage section).
- **Models** — `src/signalforge/warehouse/models.py:87-150`: `TableRef` is a frozen Pydantic v2 model with `project | None`, `dataset`, `name`. No `is_temporary` / `ttl_seconds` markers. `PartitionFilter` (160-177): `column`, `op`, `value`. `Dialect` (57-79): no `supports_create_temp_table` flag yet — adding one keeps the compiler dialect-driven (DEC-025 of #6).
- **v0.2 reservations already noted** — `docs/prune-ops.md:195-196,315`, `tests/warehouse/test_sample_cost_probe.py:85` ("v0.2 escalation to Q4=C (temp-table-materialised sample)"), `plans/super/6-prune-engine.md:226,268,333` (Q4 alternatives table; AR-B1; DEC-012 live-verify-then-escalate).
- **Errors** — `src/signalforge/warehouse/errors.py`: 15-class `WarehouseError` hierarchy. No `TemporaryTableError` yet. `src/signalforge/prune/errors.py`: 6-class `PruneError` hierarchy. The conservative drop-reason taxonomy says "route to `kept-without-evidence` rather than introduce a sixth `DropReason`" — but a *typed warehouse error* (`MaterialisationFailedError` etc.) may still be useful for the CLI's tier-3 mapping (`cli-layer.md` 7th AST scan).

### Convention constraints (Subagent C)

27 constraints distilled; the load-bearing ones for this issue:

- **C1, C2, C11** — Materialisation seam lands on `WarehouseAdapter` ABC (vendor parity), concrete-adapter import lazy in `from_profile`, no BigQuery-isms in `signalforge/prune/` — the prune compiler stays dialect-driven.
- **C3, C10** — Temp-table `TableRef.name` must pass `validate_identifier` (regex `^[A-Za-z_][A-Za-z0-9_]*$`); identifier validation at construction time.
- **C4** — Deterministic sampling preserved (`MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), bucket) < 1` + `ORDER BY FARM_FINGERPRINT(...)`) so "same input → same prune decision" holds (Architectural Commitment #5).
- **C5** — Materialisation query routes through `_default_job_config(stage="warehouse_sample_materialise")` for `use_query_cache=False`, bytes cap, labels.
- **C6** — `BigQueryAdapter.__repr__` shows only `project` + `location`; any added state (cached materialisations) stays out of repr.
- **C7** — `tests/warehouse/_fake.py` gets `expect_materialise_sample(...)` (mirrors `expect_query` API; no `MagicMock`).
- **C8** — `DropReason` stays at 5 values. Materialisation failure → `kept-without-evidence` (`why="sample materialisation failed: ..."`).
- **C9** — Fail-closed audit semantics unchanged. If materialisation raises, the per-test loop never starts, every test gets a `kept-without-evidence` decision, each gets a JSONL line, the writer propagates any audit failure as `PruneAuditWriteError`.
- **C13** — No new audit-event type. `PruneEvent` schema is locked; the strategy switch is below the audit layer (issue acceptance).
- **C14, C22** — `PruneConfig` uses `extra="forbid"`, so the new `sample_strategy` field strict-validates typos. If the field changes the JSONL shape (it shouldn't), refresh `tests/fixtures/prune/prune_event_v1.jsonl` + the strict drift detector in one commit.
- **C15, C16** — Any new typed error (`MaterialisationFailedError`?) lands at exit-code tier 3 (external-dep) per `cli-layer.md` 7th AST scan — must be added to `_EXCEPTION_TO_EXIT_CODE` table in lockstep.
- **C12, C21** — Lazy-format JSON logger; existing grep gate at `tests/llm/test_logger_grep_gate.py` covers `prune` directory.
- **C18, C20** — Probe re-run uses `pytest -m bigquery --no-cov` (coverage gate skipped for marker-only runs).
- **C24** — Materialisation is deterministic — same `model.raw_code` + `sample_size` + `partition_filter` produce the same materialised rows across runs.
- **C26** — Single-threaded sequential. Materialisation runs once before the per-test loop; budget timeout mid-materialisation marks remaining tests `kept-without-evidence`.

---

## Architecture review (2026-05-05)

### Summary table

| Review | Rating | Blockers | Concerns |
|--------|--------|----------|----------|
| Security | concern | — | session_id leak surface (R-OBS-1); cleanup-on-crash race (R-SEC-1); fail-closed audit per-decision invariant during failure path (R-SEC-2) |
| Performance | blocker | Cost amortisation depends on per-test column-pruned SELECT (R-PERF-1) | Budget-watchdog start order (R-PERF-2); `Table.num_rows` missing → routing (R-PERF-3); probe needs (a)+(b)+(c) split (R-PERF-4) |
| Data model | blocker | Non-deterministic temp-table name breaks snapshot fixtures + `compiled_sql_hash` (R-DM-1) | `_SESSION` namespace fits `TableRef` cleanly — verify (R-DM-2); compiled_sql content drift requires fixture regen (R-DM-3) |
| API design | blocker | New errors must be re-exported from `signalforge.warehouse` + registered in `_EXCEPTION_TO_EXIT_CODE` + listed in CLAUDE.md v0.2 surface (R-API-1) | `partition_filter` should be optional in ABC signature for parity with `sample_rows` (R-API-2); `MaterialisationNotSupportedError.default_remediation` text TBD (R-API-3); add "v0.2 reservations" section to `prune-engine.md` (R-API-4) |
| Observability | blocker | Don't log raw `session_id` — use `blake2b-4` hash (R-OBS-1); CLI tier-3 mapping for both new errors (subsumed by R-API-1) | `why` field — class only vs class+message (R-OBS-2); add post-Q4=C audit reading guide to `docs/prune-ops.md` (R-OBS-3); probe parametrize-vs-split (R-OBS-4) |
| Testing | concern | — | Snapshot strategy (subsumed by R-DM-1); `PruneConfig` drift mirror needs verification (R-TEST-1); probe split into two tests (R-TEST-2); explicit `expect_materialise_sample` helper on `FakeBigQueryClient` (R-TEST-3); coverage floor — ABC default impl needs a test (R-TEST-4) |

### Distinct blockers (deduplicated)

The blocker surface collapses to four genuinely independent items; the rest are downstream concerns or duplicate framings of the same fix.

- **B1 (compiled-SQL determinism)** — non-deterministic temp-table name `_sf_sample_<run_id>` breaks the byte-equal snapshot fixtures (DEC-023 of #6) AND the `compiled_sql_hash` reproducibility invariant (DEC-005 of #6). Two viable strategies; pick one in refinement (R-REF-1).
- **B2 (typed-error registration triple-surface)** — `MaterialisationFailedError` + `MaterialisationNotSupportedError` must land in (a) `src/signalforge/warehouse/errors.py`, (b) `signalforge.warehouse.__init__.__all__`, (c) `signalforge/cli/_helpers.py` `_EXCEPTION_TO_EXIT_CODE` (both → tier 3), and (d) `CLAUDE.md` "Public API surface" v0.2 amendment. The 7th AST scan catches (c) at test time. One coherent landing surface.
- **B3 (session-id redaction)** — never log the raw BigQuery session id; emit `blake2b-4(session_id)` (mirrors `safety-layer.md` DEC-010 column-name hashing). The redaction also extends to `BigQueryAdapter.__repr__` (DEC-022 of #3 — only `project` + `location` exposed).
- **B4 (cost-amortisation column-pruning assumption)** — the 1–10 MB per-test target requires the compiler to emit column-scoped SELECTs against the temp table. v0.1's `_compile_test` already does this for dbt-style tests (`unique`, `not_null`, `accepted_values`, `relationships` all reference one column at a time), so the assumption holds — but pin it as a refinement decision and add a test.

### Distinct concerns (deduplicated)

- **R-API-2** — make `partition_filter` kw-only with default `None` on the ABC; size-check enforcement lives in the BigQuery override (mirrors `sample_rows` precedent).
- **R-PERF-3** — when `Table.num_rows is None`, materialisation raises `UnknownTableSizeError` at orchestrator entry and the orchestrator routes every test to `kept-without-evidence` (preserves the conservative-bias rule, no new typed error needed).
- **R-PERF-2** — the total-budget watchdog starts ticking at orchestrator entry (BEFORE materialisation). Materialisation cost is part of the total budget; v0.2 does NOT add a separate `materialisation_timeout_seconds` knob. Forward-compat; v0.3 batch runner can graduate it.
- **R-DM-2** — verify `_SESSION` is the canonical BigQuery session-table dataset before locking; `TableRef.qualified_name` already supports the three-part form `project._SESSION.<name>` if so.
- **R-API-3** — `MaterialisationNotSupportedError.default_remediation`:
  > "Set `prune.sample_strategy: oneshot` in `signalforge.yml` to fall back to per-test sampling, or wait for v0.3 multi-warehouse support."
- **R-OBS-2** — `why` field includes class name + truncated message: `f"sample materialisation failed: {type(exc).__name__}: {str(exc)[:200]}"`. Stays under the 4000-byte audit cap (~50 byte impact per record).
- **R-OBS-3** — add a one-paragraph "audit reading guide" to `docs/prune-ops.md`: temp-table SQL of the form `FROM \`<project>._SESSION._sf_sample_<hash>\`` is the materialisation signal in `compiled_sql`; oneshot runs continue to reference the source table directly.
- **R-PERF-4 / R-OBS-4 / R-TEST-2** — probe gets two `@pytest.mark.bigquery` tests: `test_sample_rows_cost_baseline_oneshot` (preserves the AR-B1 9.92 GB measurement) and `test_sample_rows_cost_materialised` (v0.2 result, asserts < 100 MB per-test).
- **R-TEST-3** — `tests/warehouse/_fake.py` gets explicit `expect_materialise_sample(source_ref, sample_size, partition_filter=None, *, returns: TableRef | Exception)` helper, mirroring the established `expect_query` / `expect_get_table` / `expect_list_rows` API (no implicit regex matching).
- **R-TEST-4** — ABC default-impl test instantiates a minimal stub subclass that overrides every other abstract method but inherits `materialise_sample` default; lands in `tests/warehouse/test_base.py`.
- **R-TEST-1** — `tests/prune/test_drift_detector.py` covers `PruneEvent` (read-back, `extra="ignore"`); `PruneConfig` is `extra="forbid"` so adding a Literal field doesn't need a strict mirror — confirm by reading the test before merge.
- **R-SEC-1** — TTL alone handles cleanup; `BigQueryAdapter.__exit__` does NOT need to explicitly close the session in v0.2. Session_id collision risk is mitigated by `uuid4().hex` per-run derivation.
- **R-SEC-2** — failure path: orchestrator iterates every candidate and writes one `kept-without-evidence` `PruneEvent` per test (NOT a single summary record). Pinned by R-PRUNE-INTEGRATION-4 in Phase 4.
- **R-DM-3** — committed `tests/fixtures/prune/prune_event_v1.jsonl` regeneration in the same PR that ships materialisation (compiled_sql content shifts).
- **R-API-4** — add a "v0.2 reservations" section to `.claude/rules/prune-engine.md` documenting `sample_strategy`, the two new typed errors, and the `_SESSION.<temp>` audit signal (mirrors `grade-layer.md` / `diff-renderer.md` precedent).

---

## Discovery — answers (locked 2026-05-05)

| # | Question | Choice | Rationale |
|---|----------|--------|-----------|
| Q1 | Materialisation seam location | **C** — ABC method with typed `MaterialisationNotSupportedError` default impl | Vendor-clean seam; failure is a typed `WarehouseError` (CLI exit code 3) instead of `NotImplementedError`. Routes through `kept-without-evidence` per the conservative-bias rule. |
| Q2 | BigQuery temp-table mechanic | **A** — BigQuery sessions (`CREATE TEMP TABLE` in `session_id`) | Auto-cleanup at session end; no operator dataset setup; SDK noise stays confined to `_client.py`. Issue's "TTL ~1h" maps to session timeout. |
| Q3 | Failure routing on materialisation error | **A** — All remaining tests → `kept-without-evidence` with `why="sample materialisation failed: <typed_error>"` | Conservative-bias rule (DEC-006/011 of #6). 5-value `DropReason` taxonomy preserved (issue acceptance). Audit JSONL gets one line per test as usual. |
| Q4 | Skip-materialisation threshold for small tables | **A** — Always materialise when `sample_strategy=materialised` | v0.2 ships one fix; small-table optimisation is v0.3 polish. One code path; predictable cost. |
| Q5 | Partition filter on materialised table | **A** — Filter applied ONCE in materialisation WHERE clause; per-test queries unfiltered | Pairs with Q2A (sessions, no partitioning surface). Compile-seam stays simple when `sample_strategy=materialised`. |
| Q6 | Cross-call reuse within CLI session | **A** — Always fresh per `prune_tests` entry | No v0.2 caller for cached materialisation; v0.3 batch runner can graduate to caching when consumer exists (mirrors `grade-layer.md` v0.2 reservation pattern). |
| Q7 | Default for `sample_strategy` | **A** — `materialised` (issue's stated default) | Matches issue. Users debugging unexpected behaviour can switch to `oneshot`. |

---

## Refinement (locked 2026-05-05)

### Decisions

- **DEC-001 — Compiled-SQL determinism via seeded run_id (R1=B).** `run_id = blake2b-12(model.unique_id + signalforge_version + sample_size + canonical_json(partition_filter))` (16-hex output). Temp-table name = `_sf_sample_<run_id>`. Same input → byte-equal compiled SQL across runs; `compiled_sql_hash` invariant preserved unchanged. Rationale: simpler than snapshot normalisation; sessions provide namespace isolation so two concurrent runs on the same model don't collide on the temp-table identifier.

- **DEC-002 — BigQuery session lifecycle is adapter state (Option X internal).** `BigQueryAdapter` carries `self._active_session_id: str | None` for the duration of the prune run. `materialise_sample` mints a fresh `uuid4().hex` session_id, runs `CREATE TEMP TABLE` inside it, stores the id, returns the temp `TableRef`. Subsequent `run_test_sql` calls use the same session_id via `connection_properties=[ConnectionProperty(key="session_id", value=...)]` so they can read `_SESSION._sf_sample_<run_id>`. **`BigQueryAdapter.__exit__` belt-and-braces closes the session explicitly via `CALL BQ.ABORT_SESSION()`** (DEC-013) — happy path runs, normal-error paths, and KeyboardInterrupt all fire `__exit__`. Session TTL (1h default) is the durable fallback for hard process death (SIGKILL, OOM) where `__exit__` cannot fire. The ABC stays clean: no `session_id` kwarg on `run_test_sql`, no BQ-isms in `signalforge.prune`.

- **DEC-003 — Session-id redacted on the happy path; surfaced raw only in the cleanup-failure WARNING.** Logs emit `session_id_hash = blake2b-4(session_id).hexdigest()` (8 hex chars) for every normal-operation event. Raw session_id stays in `BigQueryAdapter._active_session_id`, in the BQ `QueryJobConfig.connection_properties`, and in the **DEC-014 cleanup-failure WARNING** (the one narrow user-facing exception). Never in audit JSONL, never in error messages on the happy path, never in `__repr__` (DEC-022 of #3 unchanged). Mirrors `safety-layer.md` DEC-010 column-name redaction with one documented break.

- **DEC-004 — ABC method signature: `materialise_sample(table, n, *, partition_filter=None, ttl_seconds=3600) -> TableRef`** (R2=A). Optional `partition_filter` mirrors `sample_rows` parity; size-check enforcement lives in the BigQuery override (DEC-024 of #3).

- **DEC-005 — `why` field on materialisation failure includes class + truncated message** (R3=B). Format: `f"sample materialisation failed: {type(exc).__name__}: {str(exc)[:200]}"`. ~250 bytes per record under the 4000-byte JSONL audit cap.

- **DEC-006 — `MaterialisationNotSupportedError.default_remediation`** (R4): `"Set 'prune.sample_strategy: oneshot' in signalforge.yml to fall back to per-test sampling, or wait for v0.3 multi-warehouse materialisation support."`

- **DEC-007 — Probe ships as two `@pytest.mark.bigquery` tests** (R5=A). `test_sample_rows_cost_baseline_oneshot` asserts the AR-B1 9.92 GB measurement holds for `sample_strategy=oneshot` (regression guard); `test_sample_rows_cost_materialised` asserts the per-test bytes drop below 100 MB for `sample_strategy=materialised`.

- **DEC-008 — New typed errors are `WarehouseError` subclasses, both → CLI tier 3.** `MaterialisationFailedError(WarehouseError)` wraps any SDK / network / quota failure during the materialisation query (`cause` kwarg pattern; mirrors `LLMResponseAuditWriteError`). `MaterialisationNotSupportedError(WarehouseError)` is the ABC default-impl raise. Both registered in `_EXCEPTION_TO_EXIT_CODE`; the 7th AST scan catches misses.

- **DEC-009 — Conservative-bias failure routing.** Any exception thrown by `adapter.materialise_sample(...)` (whether `MaterialisationFailedError`, `UnknownTableSizeError`, `SamplingRequiresPartitionFilterError`, or any other `WarehouseError` subclass) is caught at the orchestrator entry. Every candidate test routes to `kept-without-evidence` with the DEC-005 `why` shape; one `PruneEvent` per candidate is written to the audit JSONL (fail-closed audit preserved).

- **DEC-010 — Total-budget watchdog ticks across both phases.** Materialisation cost counts against `PruneConfig.total_budget_seconds`; budget exhaustion mid-materialisation marks every remaining test `kept-without-evidence` with the existing `why="total prune budget exceeded before evaluation"`. v0.2 does NOT add a separate `materialisation_timeout_seconds` knob (graduated to v0.3 batch runner if needed).

- **DEC-011 — `signalforge generate` exposes `--scope {sample,full}` and `--sample-strategy {oneshot,materialised}` flags** (revised 2026-05-05). Reverses the prior "config-only" position. Operators flipping between thorough (`--scope full`) and cheap (`--sample-strategy materialised`, the default) modes per-run no longer need to edit `signalforge.yml`. Config file remains the durable default; flags are per-invocation overrides; both flags optional and independent (set one, the other, both, or neither).

- **DEC-012 — CLI override mechanism re-validates the config.** `cmd_generate` applies overrides via `PruneConfig.model_validate({**config.model_dump(), "scope": override_or_existing, "sample_strategy": override_or_existing})` so every Pydantic validator re-runs (typos still fail loud; field validators on the new field re-fire). Mirrors `safety-layer.md` DEC-018 (`SafetyPolicy.with_mode`) and the `DiffConfig.render_kind` graduation in #9 — the canonical project pattern for "CLI flag overrides config-file value." Don't use `model_copy(update=...)` here; that path silently skips `@model_validator(mode="after")`.

- **DEC-013 — Explicit cleanup via `BQ.ABORT_SESSION()` in `__exit__`; TTL is the safety net.** `BigQueryAdapter.__exit__` checks `self._active_session_id`; if non-`None`, it issues `client.query("CALL BQ.ABORT_SESSION();", job_config=QueryJobConfig(connection_properties=[ConnectionProperty(key="session_id", value=self._active_session_id)]))` to immediately tear down the session and every `_SESSION.*` table inside it. Best-effort: any exception from the abort itself is **swallowed** (cleanup never blocks the user; their actual work succeeded). On success, one `INFO` log: `"session closed"` with `{"session_id_hash": ..., "ttl_remaining_seconds": ...}`. On failure, the DEC-014 cleanup-failure WARNING fires. `self._active_session_id = None` always set in a `finally` block so subsequent `__exit__` calls are no-ops. Session TTL (default 3600s) handles the hard-death case where `__exit__` doesn't fire (SIGKILL, OOM, segfault).

- **DEC-014 — Cleanup-failure WARNING is operator-facing and contains the raw session_id.** When `BQ.ABORT_SESSION()` fails, the adapter emits a single `_LOGGER.warning(...)` with this exact multi-line shape (lazy %s positional, NOT f-string — passes the grep gate):

  ```
  BigQuery session cleanup failed; session will auto-expire in <N>s (BigQuery TTL).
    Session ID: <raw session_id>
    Reason: <exception class name>
    To clean up immediately:
      bq query --connection_property=session_id=<raw> --use_legacy_sql=false "CALL BQ.ABORT_SESSION();"
  ```

  Three reasons the raw session_id appears here (deliberate exception to DEC-003):

  1. **It's the only piece of info the operator needs to act.** Without it, the manual `bq` command is unconstructable. A hash defeats the purpose.
  2. **Audience is the same principal who owns the session.** BigQuery rejects `BQ.ABORT_SESSION()` calls from other identities — the session_id is only useful to its owner, who is the user reading their own stderr.
  3. **The surface is bounded.** Raw session_id leaks ONLY on the cleanup-failure path, never on the happy path, never in audit JSONL, never in `__repr__`. Bulk log aggregators receive at most one such WARNING per failed cleanup, not per query.

  `<N>` is `max(1, int(ttl_seconds - elapsed_in_session))`. Floor at 1 avoids "auto-expire in 0s" confusion; if the session has actually expired, the abort would have succeeded with "session not found" and the WARNING wouldn't fire.

  The WARNING surfaces to stderr automatically via the CLI's `setup_logging` (default level is INFO; WARNING always shown unless `--quiet`). No special CLI plumbing required — Python's logging hierarchy handles it.

---

## Detailed breakdown

Story ordering follows the SignalForge pipeline-stack pattern: foundations (config + errors) → ABC → concrete → fakes → orchestrator → CLI surface → docs → quality gate → patterns. Each story is sized for one Ralph context window.

Validation command (referenced in every "Done When"): `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`.

---

### US-001 — `PruneConfig.sample_strategy` field + new typed errors

**Description.** Add `sample_strategy: Literal["oneshot", "materialised"] = "materialised"` to `PruneConfig` (`extra="forbid"` enforces typo loud-fail). Add two new typed errors to `signalforge/warehouse/errors.py`: `MaterialisationFailedError(WarehouseError)` (wraps SDK failures via `cause` kwarg) and `MaterialisationNotSupportedError(WarehouseError)` (ABC default-impl raise). Re-export both from `signalforge.warehouse.__init__.__all__`.

**Traces to.** Q7, R4, DEC-006, DEC-008.

**TDD.**
- `test_prune_config_accepts_oneshot_and_materialised_literals` — both values parse.
- `test_prune_config_rejects_typo_in_sample_strategy` — `extra="forbid"` catches `materialized` (US spelling) loudly.
- `test_prune_config_default_sample_strategy_is_materialised` — pin DEC-007.
- `test_load_prune_config_handles_v01_yaml_without_sample_strategy_field` — backward-compat: v0.1 YAML loads with `materialised` default.
- `test_materialisation_failed_error_str_format` — message + `↳ Remediation:` line.
- `test_materialisation_not_supported_error_carries_dec006_remediation` — exact-match the DEC-006 remediation string.
- `test_both_new_errors_inherit_from_warehouse_error` — `isinstance` check (downstream tier-3 inheritance gate).

**Done when.**
- [ ] `PruneConfig.sample_strategy` field exists with `Literal["oneshot", "materialised"]` and default `"materialised"`.
- [ ] `MaterialisationFailedError`, `MaterialisationNotSupportedError` defined in `src/signalforge/warehouse/errors.py`, both subclass `WarehouseError`, both ship `default_remediation`.
- [ ] Both errors exported from `signalforge.warehouse.__init__.__all__`.
- [ ] All seven new tests pass.
- [ ] `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest` passes.

**Files.**
- `src/signalforge/prune/config.py` — add field + docstring.
- `src/signalforge/warehouse/errors.py` — two new classes + `__all__` extension.
- `src/signalforge/warehouse/__init__.py` — re-exports.
- `tests/prune/test_config.py` — config tests.
- `tests/warehouse/test_errors.py` — error tests.

**Depends on.** None.

---

### US-002 — `WarehouseAdapter.materialise_sample` ABC method + default impl

**Description.** Add abstract method `materialise_sample(table, n, *, partition_filter=None, ttl_seconds=3600) -> TableRef` to `WarehouseAdapter`. The default impl raises `MaterialisationNotSupportedError`; subclasses override (BigQuery in US-003). Method is NOT `@abstractmethod` because the default impl IS the v0.2 behaviour for non-BQ adapters.

**Traces to.** Q1, R2, DEC-004, DEC-008.

**TDD.**
- `test_materialise_sample_default_impl_raises_not_supported` — minimal stub subclass that overrides the other 5 abstract methods inherits the default `materialise_sample`; calling it raises `MaterialisationNotSupportedError` with DEC-006 remediation.
- `test_materialise_sample_signature_matches_dec_004` — introspect the method signature; assert positional `table, n` + kw-only `partition_filter, ttl_seconds`.

**Done when.**
- [ ] `WarehouseAdapter.materialise_sample` defined with DEC-004 signature.
- [ ] Default impl raises `MaterialisationNotSupportedError` (no `NotImplementedError`).
- [ ] Stub-subclass test passes.
- [ ] Validation command passes.

**Files.**
- `src/signalforge/warehouse/base.py` — new method.
- `tests/warehouse/test_base.py` — stub-subclass test (new file or extend).

**Depends on.** US-001 (uses `MaterialisationNotSupportedError`).

---

### US-003 — `BigQueryAdapter.materialise_sample` implementation (sessions + temp table + cleanup)

**Description.** Override `materialise_sample` on `BigQueryAdapter`. Mint a fresh BQ session via `uuid4().hex`; run `CREATE TEMP TABLE _sf_sample_<run_id> AS SELECT * FROM \`<source>\` WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), <bucket>) < 1 [AND <partition_filter>]` inside the session via `connection_properties=[ConnectionProperty(key="session_id", value=session_id)]`. Compute `run_id = blake2b-12(model.unique_id + signalforge_version + sample_size + canonical_json(partition_filter))` for snapshot determinism (DEC-001). Validate temp-table name via `validate_identifier`. Route through `_default_job_config(stage="warehouse_sample_materialise")`. On success: store `self._active_session_id = session_id`, store `self._session_started_at = monotonic()`, store `self._session_ttl_seconds = ttl_seconds`; return `TableRef(project=client.project, dataset="_SESSION", name="_sf_sample_<run_id>")`. On failure: wrap as `MaterialisationFailedError(cause=...)`. Extend `run_test_sql` to honour `_active_session_id` when set (passes the same `connection_properties` so per-test queries can resolve `_SESSION._sf_sample_<run_id>`). Emit one `INFO` log via lazy-format JSON on success: `{"model": ..., "sample_rows": ..., "session_id_hash": blake2b-4(...), "duration_ms": ...}`. **Implement `__exit__` cleanup per DEC-013/DEC-014:** if `_active_session_id` is set, issue `CALL BQ.ABORT_SESSION()` in the same session; on success, `INFO` log `{"session_id_hash": ..., "ttl_remaining_seconds": ...}` and reset state; on failure, emit the DEC-014 multi-line WARNING (raw session_id + manual `bq query` command + TTL fallback note), swallow the exception, reset state in `finally`. Never log raw `session_id` outside the DEC-014 WARNING.

**Traces to.** Q1/Q2/Q5, B3, DEC-001, DEC-002, DEC-003, DEC-013, DEC-014, R-DM-2.

**TDD.**
- `test_materialise_sample_returns_tableref_with_session_dataset` — `dataset="_SESSION"`, `name="_sf_sample_<16-hex>"`.
- `test_materialise_sample_temp_table_name_passes_validate_identifier` — strict regex match.
- `test_materialise_sample_run_id_is_deterministic_per_inputs` — same `(model, sample_size, partition_filter)` → same temp-table name.
- `test_materialise_sample_run_id_changes_with_signalforge_version` — pin the version field's role in the hash.
- `test_materialise_sample_create_temp_table_sql_byte_equal_fixture` — pin the `CREATE TEMP TABLE ... AS SELECT ... WHERE MOD(...) ...` against a snapshot fixture.
- `test_materialise_sample_routes_through_default_job_config_with_correct_stage_label` — assert `"warehouse_sample_materialise"`.
- `test_materialise_sample_applies_partition_filter_in_where_clause` — filter lands in the materialisation query, not in per-test SQL.
- `test_materialise_sample_uses_connection_properties_for_session` — assert `ConnectionProperty(key="session_id", value=<uuid_hex>)` in the job config.
- `test_materialise_sample_wraps_warehouse_sdk_errors_as_materialisation_failed` — induces a `google.api_core.exceptions.Forbidden` via fake; asserts `MaterialisationFailedError(cause=...)`.
- `test_run_test_sql_uses_active_session_id_after_materialise` — sets `adapter._active_session_id`; asserts subsequent `run_test_sql` query carries the session id.
- `test_materialise_sample_logs_session_id_hash_not_raw` — capture log records; assert `session_id_hash` key present, raw session_id absent (regex search for any 32-hex value matching the minted id).
- `test_materialise_sample_default_job_config_use_query_cache_is_false` — DEC-015 of #3 invariant preserved.
- `test_bigquery_adapter_exit_closes_active_session` — set `_active_session_id`, call `__exit__`; assert `CALL BQ.ABORT_SESSION();` issued in the same session.
- `test_bigquery_adapter_exit_no_op_when_no_active_session` — `__exit__` without prior materialise; assert no abort call.
- `test_bigquery_adapter_exit_runs_after_materialise_failure_set_session_state` — induce a partial materialise that sets `_active_session_id` then raises `MaterialisationFailedError`; assert `__exit__` still issues abort.
- `test_bigquery_adapter_exit_swallows_close_errors` — fake's abort raises `google.api_core.exceptions.NotFound`; `__exit__` does not propagate; `_active_session_id` is `None` after.
- `test_bigquery_adapter_exit_logs_warning_with_raw_session_id_on_failure` — assert WARNING body contains the raw 32-hex session_id (deliberate DEC-014 break).
- `test_bigquery_adapter_exit_warning_contains_manual_kill_command` — assert WARNING body contains `bq query --connection_property=session_id=<raw> --use_legacy_sql=false "CALL BQ.ABORT_SESSION();"` with raw session_id substituted.
- `test_bigquery_adapter_exit_warning_mentions_ttl_fallback` — assert WARNING body contains `auto-expire in <N>s` with `N >= 1`.
- `test_bigquery_adapter_exit_warning_mentions_exception_class_name` — exception class name in WARNING body.
- `test_bigquery_adapter_exit_resets_session_state_in_finally_even_on_close_failure` — assert `_active_session_id` is `None` after `__exit__` regardless of whether abort succeeded.
- `test_bigquery_adapter_exit_success_logs_session_id_hash_only` — happy-path `INFO` log uses `session_id_hash`, never raw (DEC-003 happy-path invariant).
- `test_bigquery_adapter_exit_success_does_not_emit_warning` — happy-path emits no WARNING (only INFO).

**Done when.**
- [ ] `BigQueryAdapter.materialise_sample` implemented per DEC-001/DEC-002.
- [ ] `BigQueryAdapter.run_test_sql` honours `_active_session_id`.
- [ ] `BigQueryAdapter.__exit__` implements DEC-013 explicit cleanup + DEC-014 failure WARNING.
- [ ] One `INFO` log per materialise + one INFO per successful close; both use `session_id_hash` only.
- [ ] On close failure: one WARNING with raw session_id + manual command + TTL note; no exception propagated.
- [ ] All twenty-two new tests pass.
- [ ] Logger grep gate (`tests/llm/test_logger_grep_gate.py`) still passes.
- [ ] AST audit-completeness scans still pass.
- [ ] Validation command passes.

**Files.**
- `src/signalforge/warehouse/adapters/bigquery.py` — `materialise_sample` override + `_active_session_id` attr + `run_test_sql` extension.
- `src/signalforge/warehouse/adapters/_client.py` — only if the `_BQClientProtocol` needs explicit session-property surface (review during impl; the existing `job_config: Any = None` is loose enough).
- `tests/warehouse/test_bigquery_unit.py` — new unit tests (or new module `test_materialise_sample.py`).
- `tests/fixtures/warehouse/sample_materialise_v1.sql` — pinned `CREATE TEMP TABLE` snapshot.

**Depends on.** US-002.

---

### US-004 — `FakeBigQueryClient` helpers: `expect_materialise_sample` + `expect_abort_session`

**Description.** Extend `tests/warehouse/_fake.py` with two explicit helpers, mirroring the established `expect_query` / `expect_get_table` / `expect_list_rows` API. Each call consumes one matching expectation; non-matching calls raise `AssertionError("unexpected ...: ...")`. `Exception` returns propagate.

1. `expect_materialise_sample(source_ref, sample_size, partition_filter=None, *, returns: TableRef | Exception) -> None` — registers an expectation for the materialise path.
2. `expect_abort_session(session_id, *, returns: None | Exception = None) -> None` — registers an expectation for the cleanup path. `returns=None` simulates successful abort; `returns=Exception(...)` simulates abort failure (drives DEC-014 WARNING in US-003 tests).

**Traces to.** R-TEST-3, DEC-013.

**TDD.**
- `test_expect_materialise_sample_consumes_one_call` — register one expectation; one call passes; second call raises.
- `test_expect_materialise_sample_returns_exception_propagates` — register `returns=MaterialisationFailedError(...)`; calling raises.
- `test_expect_materialise_sample_assert_all_expectations_met` — registered-but-not-called fails the assertion.
- `test_expect_abort_session_consumes_one_call` — register one expectation keyed by session_id; one call passes; second call raises.
- `test_expect_abort_session_session_id_mismatch_raises` — registering for `session_a` and calling with `session_b` fails loudly.
- `test_expect_abort_session_returns_exception_propagates` — register `returns=NotFound(...)`; calling raises (drives the swallow-and-warn path in US-003).
- `test_expect_abort_session_returns_none_succeeds` — happy-path simulation.

**Done when.**
- [ ] Both helpers added to `FakeBigQueryClient` (matched against `client.query(...)` calls with the relevant SQL pattern + session_id).
- [ ] Seven new tests pass.
- [ ] Validation command passes.

**Files.**
- `tests/warehouse/_fake.py` — helper + internal expectation queue.
- `tests/warehouse/test_fake.py` — meta-tests (or extend existing).

**Depends on.** US-003 (helper shape mirrors the production method's signature).

---

### US-005 — Prune orchestrator integration: dispatch on `sample_strategy`, route failures conservatively

**Description.** Wire `materialise_sample` into `prune_tests`. When `config.sample_strategy == "materialised"`, call `adapter.materialise_sample(...)` BEFORE the per-test loop; on success, every test's compiled SQL references the returned temp `TableRef` (replace the existing `TableRef.from_model(model)` substitution at the compiler call site). On any exception (any `WarehouseError` subclass), catch it, route every candidate test to `kept-without-evidence` with `why=f"sample materialisation failed: {type(exc).__name__}: {str(exc)[:200]}"`, and write one `PruneEvent` per candidate to the audit JSONL (fail-closed audit preserved). When `sample_strategy == "oneshot"`, the v0.1 path runs unchanged. Refresh `tests/fixtures/prune/prune_event_v1.jsonl` to reflect the new `compiled_sql` content under `materialised` mode.

**Traces to.** Q3, DEC-005, DEC-009, DEC-010, R-PERF-2/R-PERF-3, R-SEC-2, R-DM-1/R-DM-3.

**TDD.**
- `test_prune_tests_with_materialised_strategy_calls_materialise_sample_once` — assert single `expect_materialise_sample` consumed before the per-test loop.
- `test_prune_tests_with_oneshot_strategy_skips_materialise_sample` — assert no materialise call; v0.1 path traversed.
- `test_prune_tests_compiled_sql_references_temp_table_under_materialised` — assert `_SESSION._sf_sample_<16-hex>` in every decision's `compiled_sql`.
- `test_prune_tests_compiled_sql_hash_is_deterministic_under_materialised` — run twice; assert identical hashes (DEC-001).
- `test_prune_tests_materialisation_failed_routes_all_to_kept_without_evidence` — fake raises `MaterialisationFailedError`; assert N decisions, all `decision="kept", reason="kept-without-evidence", why=<DEC-005 shape>`.
- `test_prune_tests_unknown_table_size_routes_all_to_kept_without_evidence` — fake raises `UnknownTableSizeError`; same shape (DEC-009 conservative-bias rule).
- `test_prune_tests_materialisation_failure_writes_one_audit_per_test` — N candidates → N JSONL lines; DEC-016 of #6 preserved.
- `test_prune_tests_total_budget_includes_materialisation` — pin DEC-010: budget watchdog ticks from orchestrator entry through materialisation.
- `test_prune_tests_uses_adapter_as_context_manager` — pin that `prune_tests` invokes the adapter inside a `with` block so `BigQueryAdapter.__exit__` fires and DEC-013 cleanup runs. Without this test, the cleanup work in US-003 is unreachable from the orchestrator.
- `test_prune_tests_adapter_exit_fires_after_normal_completion` — fake records `__exit__` calls; assert exactly one fires after a successful materialised run.
- `test_prune_tests_adapter_exit_fires_after_materialisation_failure` — fake raises `MaterialisationFailedError`; assert `__exit__` still fires (cleanup runs even on the failure path that routes to `kept-without-evidence`).
- `test_prune_tests_budget_exhausted_during_materialisation_marks_all_kept_without_evidence` — `_sleep` reassignment trick to fast-forward the watchdog mid-materialise.
- `test_prune_tests_materialised_strategy_against_pinned_fixture` — end-to-end snapshot test loading `prune_event_v1.jsonl` post-regen.

**Done when.**
- [ ] Orchestrator dispatches on `config.sample_strategy`.
- [ ] Materialisation runs before the per-test loop; failures route conservatively.
- [ ] `tests/fixtures/prune/prune_event_v1.jsonl` regenerated to match the new `compiled_sql` content under `materialised`.
- [ ] All ten new tests pass.
- [ ] Existing prune drift detector tests still pass.
- [ ] Logger grep gate still passes.
- [ ] Validation command passes.

**Files.**
- `src/signalforge/prune/engine.py` — dispatch + failure-routing helper.
- `tests/prune/test_engine.py` — new tests.
- `tests/fixtures/prune/prune_event_v1.jsonl` — regenerated.
- `tests/prune/test_drift_detector.py` — verify no schema mirror change required (`PruneConfig` is `extra="forbid"`).

**Depends on.** US-001, US-002, US-003, US-004.

---

### US-006 — `signalforge generate` flags `--scope` and `--sample-strategy`

**Description.** Add two flags to `signalforge generate`: `--scope {sample,full}` (overrides `prune.scope`) and `--sample-strategy {oneshot,materialised}` (overrides `prune.sample_strategy`). Both are optional and independent. When supplied, `cmd_generate` re-validates the config via `PruneConfig.model_validate({**dump, "scope": ..., "sample_strategy": ...})` (DEC-012) so validators re-run. When absent, config-file values apply unchanged. Help text follows cli-layer.md's multi-surface parity rule (help string + handler docstring + DEC + test name aligned in lockstep).

**Traces to.** DEC-011, DEC-012, user request 2026-05-05.

**TDD.**
- `test_generate_scope_flag_overrides_config_value` — config has `scope: sample`; `--scope full` makes the orchestrator see `scope=full`.
- `test_generate_sample_strategy_flag_overrides_config_value` — config has `sample_strategy: materialised`; `--sample-strategy oneshot` makes the orchestrator see `sample_strategy=oneshot`.
- `test_generate_both_flags_independent` — set one without the other; the unset axis falls through to config.
- `test_generate_no_flag_uses_config_value` — neither flag set; config values apply unchanged.
- `test_generate_invalid_scope_returns_exit_2` — `--scope invalid` → argparse rejection mapped to tier-2 exit.
- `test_generate_invalid_sample_strategy_returns_exit_2` — same shape.
- `test_generate_help_text_lists_new_flags` — `signalforge generate --help` output mentions both flag names + their value lists.
- `test_generate_override_re_runs_pydantic_validators` — pin DEC-012: tweak a `PruneConfig` `@field_validator` in a test fixture to assert it re-fires under the override path (mirrors `safety-layer.md` DEC-018 pin).

**Done when.**
- [ ] Both flags registered in `cmd_generate`'s argparse parser via the existing `add_parser` extension point.
- [ ] Override mechanism uses `PruneConfig.model_validate(...)` (NOT `model_copy(update=...)`).
- [ ] `cmd_generate`'s docstring documents the override precedence (flag > config).
- [ ] All eight new tests pass.
- [ ] Existing CLI in-process tests (`tests/cli/`) pass unchanged.
- [ ] No traceback leaks on invalid flag values (cli-layer.md DEC-016 floor).
- [ ] Validation command passes.

**Files.**
- `src/signalforge/cli/generate.py` — argparse extensions + override application in `cmd_generate`.
- `tests/cli/test_generate.py` — eight new tests.

**Depends on.** US-001 (`PruneConfig.sample_strategy` field exists), US-005 (orchestrator honours both axes).

---

### US-007 — CLI exit-code mapping for new typed errors

**Description.** Register `MaterialisationFailedError` and `MaterialisationNotSupportedError` in `signalforge/cli/_helpers.py::_EXCEPTION_TO_EXIT_CODE`, both → tier 3 (external-dep / fail-closed). Add the two errors to the parametrized factory in `tests/cli/test_exit_codes.py`. The 7th AST scan auto-validates registration; this story ensures the parametrized contract covers the new types end-to-end.

**Traces to.** B2, DEC-008, R-OBS-6.

**TDD.**
- `test_materialisation_failed_error_maps_to_tier_3` — parametrized; CLI exit code is 3.
- `test_materialisation_not_supported_error_maps_to_tier_3` — parametrized; CLI exit code is 3.
- `test_audit_completeness_scan_passes_for_new_errors` — 7th AST scan still green (verifies registration via the existing test harness).

**Done when.**
- [ ] Both errors registered in `_EXCEPTION_TO_EXIT_CODE` (tier 3).
- [ ] Both factory branches added to `tests/cli/test_exit_codes.py::_construct_exception` (or equivalent helper).
- [ ] 7th AST scan still passes.
- [ ] Validation command passes.

**Files.**
- `src/signalforge/cli/_helpers.py` — registry entries.
- `tests/cli/test_exit_codes.py` — factory branches + parametrize cases.

**Depends on.** US-001 (errors must exist).

---

### US-008 — Probe re-run scaffolding + cleanup verification

**Description.** Restructure `tests/warehouse/test_sample_cost_probe.py` into three `@pytest.mark.bigquery` tests (DEC-007 + DEC-013): `test_sample_rows_cost_baseline_oneshot` (asserts the 9.92 GB AR-B1 measurement under `sample_strategy=oneshot` — regression guard for the legacy path), `test_sample_rows_cost_materialised` (asserts the per-test bytes drop below 100 MB under `sample_strategy=materialised`), and `test_materialised_session_cleaned_up_after_exit` (positive proof of cleanup: after `BigQueryAdapter.__exit__` fires, querying the temp table by name fails with "session not found" / "table not found"). All three set `os.environ["SF_RUN_BQ"]` gate; tests stay marker-gated; default CI does NOT run them. Maintainer runs `SF_RUN_BQ=1 pytest -m bigquery tests/warehouse/test_sample_cost_probe.py --no-cov` before declaring the PR ready.

**Traces to.** DEC-007, DEC-013, R-PERF-4, R-OBS-4, R-TEST-2.

**TDD.**
- `test_probe_module_imports_and_exposes_three_test_functions` — sanity-check the module shape (always-collectable, marker-gated execution).
- `test_probe_constants_unchanged` — pin `_BYTES_CEILING` and `_BYTES_WARN_AT` values; existing v0.1 thresholds preserved.

**Done when.**
- [ ] Three `@pytest.mark.bigquery` tests defined.
- [ ] All three gated by `SF_RUN_BQ` env var.
- [ ] Default `pytest` run skips all three (existing marker-exclusion behaviour preserved).
- [ ] Two scaffolding tests pass.
- [ ] Cleanup-verification test asserts the temp table is gone post-`__exit__` (positive proof of DEC-013).
- [ ] PR description includes the maintainer run command and placeholders for the post-Q4=C figures (filled during US-010 quality gate).
- [ ] Validation command passes.

**Files.**
- `tests/warehouse/test_sample_cost_probe.py` — three-test layout (oneshot baseline, materialised target, cleanup verification).

**Depends on.** US-005 (the orchestrator's `sample_strategy` dispatch is what the probe exercises), US-003 (cleanup behaviour for the third test).

---

### US-009 — Documentation surfaces (5-surface parity from `cli-layer.md`)

**Description.** Update every doc surface affected by the v0.2 strategy change, in lockstep:

1. **`docs/prune-ops.md` Cost model section** — add post-Q4=C subsection with placeholders for the figures (filled in US-010 after maintainer probe-run); add audit-reading guide note explaining `_SESSION._sf_sample_<hash>` as the materialisation signal in `compiled_sql`; document the new `sample_strategy` config field with example YAML.
2. **`docs/warehouse-adapter-ops.md`** — add `warehouse_sample_materialise` to the stage-label list; document `materialise_sample` ABC method + BigQueryAdapter session-state pattern; document the v0.2 → v0.3 migration story for non-BQ adapters; add a **"Session cleanup & manual recovery"** section covering DEC-013 + DEC-014 (the three-layer cleanup model — explicit `__exit__` close, TTL fallback, manual `bq query --connection_property=session_id=<raw> --use_legacy_sql=false "CALL BQ.ABORT_SESSION();"`); add an `INFORMATION_SCHEMA.JOBS_BY_PROJECT` query template for ops to spot orphaned `signalforge_stage='warehouse_sample_materialise'` sessions older than 2× TTL.
3. **`.claude/rules/prune-engine.md`** — add a "v0.2 reservations / additions" section documenting `sample_strategy`, the two new typed errors, the `_SESSION.<temp>` audit signal, and DEC-010 (total-budget includes materialisation).
4. **`.claude/rules/warehouse-adapters.md`** — document `materialise_sample` ABC method + adapter session-state pattern (mirrors DEC-008/DEC-025 batching-state precedent); add a new sub-section **"Best-effort cleanup in `__exit__` (DEC-013) with user-actionable failure WARNING (DEC-014)"** covering the three-layer model (explicit close → swallow-and-warn → TTL fallback), the WARNING shape verbatim, and the rule that raw session_id surfaces ONLY in the WARNING. This becomes the canonical reference for any future v0.3 stateful-adapter cleanup work.
5. **`CLAUDE.md`** — amend "Public API surface (v0.1)" to "Public API surface (v0.1 + v0.2 additions)" and list the four new exports: `signalforge.warehouse.MaterialisationFailedError`, `signalforge.warehouse.MaterialisationNotSupportedError`, `WarehouseAdapter.materialise_sample`, `PruneConfig.sample_strategy`.

**Traces to.** R-API-4, R-OBS-3, B2 (CLAUDE.md surface), DEC-002/006/007/008/010/013/014.

**TDD.** Docs-only — no new test code. Validation: existing doc-link tests pass; existing markdown linter passes.

**Done when.**
- [ ] All five doc surfaces updated.
- [ ] Cost-model section uses placeholder text (e.g., `<TBD: post-Q4=C figure to be filled during quality gate>`) for the post-Q4=C bytes; audit reading guide present.
- [ ] No broken cross-references.
- [ ] Validation command passes.

**Files.**
- `docs/prune-ops.md`
- `docs/warehouse-adapter-ops.md`
- `.claude/rules/prune-engine.md`
- `.claude/rules/warehouse-adapters.md`
- `CLAUDE.md`

**Depends on.** US-005 (final shape of the dispatch surface), US-006 (CLI flag surface to document), US-007 (exit-code mapping confirms the typed errors), US-008 (probe shape stable).

---

### US-010 — Quality Gate (code review × 4 + CodeRabbit + maintainer probe-run)

**Description.** Run the project's quality-gate sequence:

1. **Code reviewer × 4.** Spawn `code-reviewer` agent (or equivalent) four times across the full changeset. Fix every real bug found each pass; minor stylistic feedback can be deferred to "Patterns & Memory."
2. **CodeRabbit review** (if available in the GitHub PR).
3. **Maintainer probe-run.** Run `SF_RUN_BQ=1 pytest -m bigquery tests/warehouse/test_sample_cost_probe.py --no-cov` against a real BQ project. Capture the figures. Update `docs/prune-ops.md` Cost model section, replacing the placeholders from US-009 with the actual post-Q4=C bytes_billed + run date. Confirm per-test bytes_billed < 100 MB for `materialised` strategy (issue acceptance criterion).
4. **Validation final.** `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest && pytest -m cli_subprocess --no-cov`.

**Traces to.** Issue acceptance criteria (#1, #2, #3, #4 — every checkbox).

**Done when.**
- [ ] Four code-reviewer passes complete; all real bugs fixed.
- [ ] CodeRabbit comments addressed (or explicitly deferred with justification).
- [ ] Maintainer probe-run executed; per-test `bytes_billed < 100 MB` for `materialised` confirmed.
- [ ] `docs/prune-ops.md` Cost model section reflects the actual post-Q4=C figures + run date (placeholders gone).
- [ ] Final validation command passes.
- [ ] All four issue acceptance checkboxes ticked in the PR description.

**Files.** Whatever the reviewer passes turn up; final cost-figure substitution in `docs/prune-ops.md`.

**Depends on.** US-001 through US-009.

---

### US-011 — Patterns & Memory (priority 99)

**Description.** Distil the patterns this issue established into the rule files and memory so future v0.2/v0.3 work doesn't relearn them. New patterns:

1. **Adapter session-state pattern.** `BigQueryAdapter._active_session_id` is the v0.2-shaped precedent for any future "stateful adapter context" surface (Snowflake transactions, Postgres prepared-statement caches). Document in `.claude/rules/warehouse-adapters.md` so v0.3 adapters inherit the convention.
2. **Seeded-determinism over snapshot normalisation.** DEC-001's `blake2b-12(stable_inputs)` recipe for derived identifiers is a project pattern (mirrors the LLM drafter's `prompt_version`). Add a one-paragraph note to `.claude/rules/testing-signal.md` covering when to use seeded determinism vs snapshot normalisation.
3. **Conservative-bias routing across `WarehouseError` subclasses.** DEC-009 generalises `prune-engine.md`'s 5-value `DropReason` taxonomy: any orchestrator-entry warehouse exception routes the *whole* candidate set to `kept-without-evidence`, not the test that happened to be running. Document in `.claude/rules/prune-engine.md` as a refinement of the existing C8 constraint.
4. **5-surface parity for v0.x → v0.(x+1) graduations.** `cli-layer.md`'s 5-surface rule (help / docstring / ops-doc / test name / DEC) extended for non-CLI graduations: rule file / ops doc / CLAUDE.md public-API / test / DEC. Add a brief reference in `.claude/rules/prune-engine.md`'s v0.2 reservations section.
5. **Best-effort cleanup with user-actionable failure WARNING (DEC-013/DEC-014).** A reusable pattern across the project: when housekeeping (cleanup, retries, audit-write) fails, never block the user's actual work, but emit a single WARNING that gives them (a) the identifier needed to act manually, (b) the exact command to run, and (c) the durable fallback (TTL, retry, etc.). Mirrors `safety-layer.md` DEC-011 (fail-closed audit propagation) but applied to the *cleanup* boundary instead of the *primary* boundary. Document this distinction in `.claude/rules/warehouse-adapters.md` (cleanup) AND `.claude/rules/safety-layer.md` (primary work) so a future maintainer doesn't confuse the two.

6. **`bd remember` insights.** Use `bd remember` for any cross-session insight about the materialisation seam that doesn't fit the rule-file shape (e.g., "BigQuery session creation latency observed at ~Xms — informs future budget-knob calibration"; "BigQuery session abort failures observed at <X%> in production — informs whether DEC-013's swallow-and-warn rate is acceptable or needs an alerting threshold").

**Traces to.** Project-wide rule maintenance per the super-plan workflow.

**Done when.**
- [ ] All five rule-file / memory updates landed.
- [ ] No duplicate guidance across rule files (e.g., session-state pattern lives in `warehouse-adapters.md`, referenced from `prune-engine.md`, not duplicated).
- [ ] Validation command passes (markdown lint).

**Files.**
- `.claude/rules/warehouse-adapters.md`
- `.claude/rules/prune-engine.md`
- `.claude/rules/testing-signal.md`
- One or more `bd remember "..."` invocations.

**Depends on.** US-010.

---


