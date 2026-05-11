# 36: `signalforge generate --estimate` — pre-flight cost preview

## Meta

- **Ticket:** [GH #36](https://github.com/wjduenow/SignalForge/issues/36)
- **Branch:** `feature/36-estimate-cost-preview`
- **Worktree:** `../worktrees/SignalForge/36-estimate-cost-preview`
- **Phase:** devolved
- **PR:** [#63](https://github.com/wjduenow/SignalForge/pull/63) (draft)
- **Epic:** `bd_1-scaffolding-bmz`
- **Sessions:**
  - 2026-05-11 — initial discovery + research
  - 2026-05-11 — Phase 1 scoping answers, Phase 2 architecture review
  - 2026-05-11 — Phase 3 refinement (15 DECs), Phase 4 detailing (8 stories)
  - 2026-05-11 — Phase 5 published as draft PR #63
  - 2026-05-11 — Phase 6 approved, Phase 7 devolved to beads

## Ticket summary

Add `--estimate` flag to `signalforge generate`. When set, the CLI:

1. Loads manifest + every config (same prelude as today).
2. Renders draft + grade prompts.
3. Calls `client.messages.count_tokens(...)` — never `messages.create(...)`.
4. Computes USD via static per-model price table (with versioned identifier in output).
5. Computes estimated warehouse bytes (heuristic; optional BQ `dryRun`).
6. Prints three sections to stdout (draft cost / grade cost with per-criterion breakdown / warehouse bytes), exits 0.

**AC-1** No billable `messages.create`; no billable warehouse `query` (only `count_tokens` + optional `dryRun`).
**AC-2** Output: draft USD, grade USD (per-criterion), total LLM USD, warehouse bytes, model, price-table version.
**AC-3** Mutex with `--write` and `--dry-run`.
**AC-4** Test pins zero `messages.create` calls.
**AC-5** `docs/cli-ops.md` § Flag reference updated per 5-surface parity rule.

## Discovery

### Codebase findings (key seams)

- **CLI handler** — `src/signalforge/cli/generate.py`
  - `add_parser` (~line 106–296) — mutex group for `--write`/`--dry-run` at line 197 is the precedent.
  - `cmd_generate` (~line 386–678) — pipeline orchestrator; project-dir resolution (447), manifest load (470), warehouse profile load (474), safety/draft/grade config (493–501), then draft → prune → grade → diff.
  - `_make_anthropic_client` / `_make_warehouse_adapter` — injection seams for tests.
- **LLM seam** — `src/signalforge/llm/client.py`, `src/signalforge/llm/_client.py`
  - `_AnthropicMessagesProtocol.count_tokens(**kwargs)` already exposed (line 53 of `_client.py`).
  - `call_anthropic` already issues `client.messages.count_tokens(...)` pre-send for cache-size check (lines 238–305 of `client.py`); pattern is reusable.
  - LLMError taxonomy (`LLMAuthError`, `LLMConnectionError`, `LLMRateLimitError`, `LLMServerError`) all → tier 3 exit code.
- **Draft prompts** — `src/signalforge/draft/prompts.py`
  - `render_prompt(...)` returns `(system, cached_block, dynamic_block, prompt_version)` — pure function, no API call.
- **Grade prompts** — `src/signalforge/grade/prompts.py`, `src/signalforge/grade/engine.py`
  - `render_rubric_block` (cached) + `render_dynamic_block` (per-pair) both pure functions.
  - Artifact count formula already used in `generate.py:572–587` (`2*len(columns) + 2 + sum(len(tests))`).
  - `DEFAULT_RUBRIC` ships 4 criteria when `grade_config.rubric is None`.
- **Test infrastructure**
  - `tests/llm/_fake.py::FakeAnthropicClient._FakeMessages.expect_count_tokens` exists; `_create_calls` and `_count_calls` already tracked per kwargs.
  - `tests/cli/test_generate.py::_install_happy_patches` is the patching template.
- **Exit-code mapping** — `src/signalforge/cli/_helpers.py::_EXCEPTION_TO_EXIT_CODE`
  - All existing LLM errors already mapped. No new error class needed.
- **Warehouse** — `src/signalforge/warehouse/adapters/bigquery.py`
  - BigQuery SDK supports `QueryJobConfig(dry_run=True)`. Not currently used. v0.2 ticket #22 mentions it; not exposed on the adapter ABC.

### Rules constraints (informing detailing)

1. **`cli-layer.md` — 5-surface parity (HARD)** — every flag change updates: argparse help, handler docstring, `docs/cli-ops.md` § Flag reference, test name, DEC in this plan.
2. **`cli-layer.md` — four-tier exit codes** — `--estimate` reuses existing tiers (0 success; 3 if `count_tokens` fails). No new exception class → 7th AST scan stays satisfied.
3. **`cli-layer.md` — no traceback (DEC-016)** — handler's existing `try/except Exception` boundary covers the new branch automatically.
4. **`cli-layer.md` — logger grep gate (6 dirs)** — any new `_LOGGER` call uses lazy-format JSON.
5. **`testing-signal.md`** — no `assert True`; test must be capable of failing. Use existing `FakeAnthropicClient` to pin `len(messages._create_calls) == 0`.
6. **`llm-drafter.md` DEC-024** — `count_tokens` is already wired through the single SDK seam (`_client.py`); reuse, don't duplicate.
7. **`llm-drafter.md` DEC-012** — every `# pyright: ignore` for the Anthropic SDK stays in `_client.py`. No new ignore comments outside that file.
8. **`safety-layer.md` DEC-015 / pricing module shape** — if a new `signalforge.llm.pricing` module ships, the price table is `extra="forbid"` config-shape (frozen `dataclass(frozen=True)` or Pydantic with `extra="forbid"`).
9. **CLAUDE.md — public API surface** — if pricing module is public, list it in CLAUDE.md "Public API surface". If CLI-internal, no entry needed.
10. **No new AST scan, no new audit-event** — `--estimate` produces no durable artifact in v0.1.

### Scoping decisions (locked Phase 1)

- **SD-1 — Price-table location:** `signalforge.llm.pricing` shared module. `PRICE_TABLE_VERSION: str` constant + `PRICES: dict[str, ModelPricing]` mapping. Public on the LLM seam; lands in `CLAUDE.md` § Public API surface.
- **SD-2 — Warehouse-bytes accuracy:** heuristic for the test-loop multiplier (tests/column × sample size) **plus** one BigQuery `dryRun` round-trip for the per-row byte estimate. Requires new `WarehouseAdapter.estimate_query_bytes(sql) -> int` method (default impl raises `EstimateNotSupportedError`; `BigQueryAdapter` overrides via `QueryJobConfig(dry_run=True)` reading `total_bytes_processed`). Mirrors v0.2 graceful-degrade pattern from issue #22.
- **SD-3 — Missing API key:** hard error, exit tier 3 (`LLMAuthError` from existing seam). No offline fallback.
- **SD-4 — Output format:** plain text only on stdout. JSON / sidecar deferred to a future ticket if CI integrations ask.

### Candidate-test-count source (resolved by SD-2)

The bytes-per-row dryRun lets us avoid the chicken-and-egg on test count by using a **scope-driven** multiplier: `~3.5 tests/column avg` from the canonical fixture (column-level not_null/unique/accepted_values + occasional model-level relationships) is a stable enough heuristic when the BYTES side is data-driven. Document the test-count assumption in the output footer; do NOT add a `--estimate-tests-per-column` knob in v0.1.

## Architecture review

Inline review (scope is bounded: one flag, one new module, one new ABC method, no new audit-event).

| # | Area | Rating | Finding / mitigation |
|---|------|--------|----------------------|
| 1 | **Security — prompt-injection envelope** | pass | `--estimate` renders draft/grade prompts using existing pure-function helpers (`render_prompt`, `render_dynamic_block`). The `</MODEL_SQL>` / `</ARTIFACT>` envelope-breach guards (DEC-007 `llm-drafter.md`; DEC-008 `grade-layer.md`) fire BEFORE `count_tokens` because they live inside the renderers. A malicious manifest field gets caught at the same gate as the real-run path. |
| 2 | **Security — pricing table tampering** | pass | Price table is in-package Python module (not user-config). Operators wanting custom prices fork the module locally; `PRICE_TABLE_VERSION` makes drift visible in CLI output. |
| 3 | **Security — `dryRun` SQL** | concern → resolved | The SQL passed to `estimate_query_bytes` is the same `not_null` / `unique` / `accepted_values` / `relationships` SQL the prune compiler already emits. Identifier validation (`_sql_safety.validate_identifier`, DEC-024 `prune-engine.md`) already runs at compile time. dryRun doesn't loosen safety. |
| 4 | **Performance — count_tokens fan-out** | concern → accepted | ~48 sequential `count_tokens` calls per estimate (~12 artifacts × 4 criteria + 1 draft). Each is ~100–300ms; expect 5–15s wall-clock. Sequential matches grade DEC-004. No optimisation in v0.1; document expected duration in the help text. |
| 5 | **Performance — dryRun cost** | pass | BigQuery dryRun is free (not billed; no quota impact for free tier). One round-trip per `--estimate` run. |
| 6 | **Data model — `ModelPricing` shape** | pass | `dataclass(frozen=True, slots=True)` with `input_per_mtok: float`, `output_per_mtok: float`, `cache_write_5m_per_mtok: float`, `cache_read_per_mtok: float`. Cache-write field is the 5m-TTL Sonnet/Opus number; drafter uses 5m TTL (`llm-drafter.md`), grader uses 1h TTL — pricing module ships both keys to cover both call sites. |
| 7 | **Data model — `WarehouseAdapter.estimate_query_bytes`** | concern → resolved | New abstract-ish method: default impl on ABC raises `EstimateNotSupportedError(WarehouseError)`, registered in `_EXCEPTION_TO_EXIT_CODE` at tier 3. `BigQueryAdapter` overrides. Mirrors `materialise_sample` precedent verbatim (`warehouse-adapters.md`, issue #22 DEC-005). Update `docs/warehouse-adapter-ops.md` § Error reference. |
| 8 | **API design — public surface delta** | pass | Three new public names: `signalforge.llm.pricing.PRICE_TABLE_VERSION`, `signalforge.llm.pricing.PRICES` (and `ModelPricing`, `EstimateUnknownModelError`). One new ABC method `WarehouseAdapter.estimate_query_bytes`. One new error class `EstimateNotSupportedError`. All land in `CLAUDE.md` § Public API surface. |
| 9 | **API design — flag mutex** | pass | Add `--estimate` to existing `write_group` mutex at `generate.py:197`. argparse emits standard mutex error; CLI's panic-path boundary maps to tier 1 (CliInputError) consistently. |
| 10 | **API design — pre-flight ordering** | concern | `--estimate` should run the full prelude (manifest + safety + draft + prune + grade configs + warehouse profile load + adapter construction) so operator catches typos. Skip only the actual `draft_schema` / `prune_tests` / `grade_artifacts` / `render_diff` orchestrator calls. |
| 11 | **API design — exit code on partial failure** | concern | If `count_tokens` succeeds but `estimate_query_bytes` raises (auth, quota, network), do we exit 3 OR print partial estimate + WARN? Recommend: print partial; warehouse-bytes section shows `<unavailable: reason>`; exit 0. Mirrors `prune-engine.md` DEC-009 conservative-bias (degrade rather than fail). |
| 12 | **Observability — logging** | pass | Single INFO at end of `--estimate` path via lazy-format JSON (model id, total tokens, total USD, total bytes, duration). Covered by 6-dir grep gate. |
| 13 | **Observability — no audit-event** | pass | No durable artefact in v0.1 → no 8th AST scan, no fail-closed writer. |
| 14 | **Testing — zero `messages.create`** | pass | `FakeAnthropicClient._create_calls` already tracked. Test asserts `len(fake.messages._create_calls) == 0` and `len(fake.messages._count_calls) >= 1`. |
| 15 | **Testing — warehouse fake parity** | concern | `FakeBigQueryClient` (under `tests/warehouse/_fake.py`) needs `expect_dry_run(sql_matching, returns_bytes)` to mirror `expect_query`. Adapter test pins the dry_run kwarg flows correctly. |
| 16 | **Testing — output snapshot** | concern | Stdout shape is NEW (the ticket pins fields but not formatting). Pin format with a snapshot test against committed fixture so QG passes 1–4 catch any drift. |
| 17 | **5-surface parity** | blocker (until met) | Five surfaces required: argparse help, handler docstring, `docs/cli-ops.md` § Flag reference, test name, DEC in this plan. Encoded as story-level requirement. |

### Net: no hard blockers, three concerns to settle in refinement

- **C-A — Exit code policy on partial estimate** (#11): degrade-and-exit-0 vs hard-fail.
- **C-B — Output shape lock** (#16): exact stdout format for the snapshot test.
- **C-C — Price-table model coverage** (#6): which models ship in `PRICES` for v0.1?

## Refinement log

| # | Decision | Rationale |
|---|----------|-----------|
| **DEC-001** | Price-table lives in new `signalforge.llm.pricing` module | Shared seam future-proofs v0.2 grading-cost projection. Public on the LLM namespace (CLAUDE.md § Public API surface). Locks `PRICE_TABLE_VERSION: str` constant + `PRICES: dict[str, ModelPricing]` shape. |
| **DEC-002** | `ModelPricing` shape: `dataclass(frozen=True, slots=True)` with four cents/MTok fields | `input_per_mtok`, `output_per_mtok`, `cache_write_5m_per_mtok`, `cache_read_per_mtok`. Ships both 5m and 1h cache numbers when relevant (drafter uses 5m TTL per `llm-drafter.md`; grader uses 1h). All `float`; no Decimal in v0.1 — output rounds to four decimal places, so float precision is adequate. |
| **DEC-003** | v0.1 `PRICES` covers `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5` | Three current 4.x SKUs. Drafter default (`claude-sonnet-4-6` per Austin fixture) plus the documented model ids. Unknown model raises `EstimateUnknownModelError(LLMError)` with remediation pointing to `signalforge/llm/pricing.py`. Older 3.x SKUs out of scope. |
| **DEC-004** | New `WarehouseAdapter.estimate_query_bytes(sql: str) -> int` ABC method | Default impl on the ABC raises `EstimateNotSupportedError(WarehouseError)` with remediation `"Use --estimate with a BigQuery profile, or wait for v0.3 multi-warehouse estimation support."`. `BigQueryAdapter` overrides using `QueryJobConfig(dry_run=True)`, reads `job.total_bytes_processed`. Mirrors `materialise_sample` precedent (#22). Registered in `_EXCEPTION_TO_EXIT_CODE` at tier 3. |
| **DEC-005** | Partial-failure degrade for warehouse-bytes ONLY | If `estimate_query_bytes` raises ANY `WarehouseError` subclass, the warehouse section renders `bytes-per-row: <unavailable: <ErrorClass>>`, total bytes shows `<unknown>`, stderr WARNING with one-line reason. Exit 0. Mirrors `prune-engine.md` DEC-009 conservative-bias verbatim. Every other failure (LLM, config, manifest) propagates through the existing `cmd_generate` panic boundary. |
| **DEC-006** | ANTHROPIC_API_KEY missing → tier 3 hard error | No offline fallback. `count_tokens` requires auth; surfacing `LLMAuthError` via the existing seam keeps the contract uniform. |
| **DEC-007** | Stdout shape locked verbatim (snapshot-tested) | Three sections (Draft / Grade / Warehouse) + totals + footer with price-table version and tests/column heuristic. See the preview snapshot in this plan. Pinned by `tests/cli/test_estimate_output_snapshot.py` against `tests/fixtures/estimate/output_happy.txt`. 80-col friendly. |
| **DEC-008** | Add `--estimate` to existing `--write` / `--dry-run` mutex group | argparse handles the standard mutex error; CLI maps to tier 1 (`CliInputError`) consistently with other mutex violations. |
| **DEC-009** | Full prelude runs before estimate short-circuit | Manifest + safety + draft + prune + grade + diff configs + warehouse profile + adapter construction all run, validating the operator's setup. Only the four orchestrators (`draft_schema`, `prune_tests`, `grade_artifacts`, `render_diff`) get skipped. Operator catches typos in `--profiles-dir` / `signalforge.yml` BEFORE making an LLM call. |
| **DEC-010** | Estimate engine lives in `signalforge.cli._estimate` (private) | CLI-internal helper returning typed `EstimateReport`. NOT promoted to `signalforge.estimate` in v0.1 — wait for a real second caller (CI integration, programmatic API) before graduating. Mirrors `_helpers.py` / `_pricing` precedent for CLI-internal modules. |
| **DEC-011** | Renderer in `signalforge.cli._estimate.render(report) -> str` (pure) | Pure-function renderer takes the typed report, returns formatted text. Snapshot-tested. Mirrors diff renderer's pure-return ABC pattern (`diff-renderer.md` DEC-011) at smaller scale (one renderer; no ABC needed). |
| **DEC-012** | Heuristic: `3.5 tests/column` average | Derived from the canonical Austin e2e fixture (`stg_bikeshare_trips`: 11 columns yielding ~38 candidate tests pre-prune). Documented in CLI footer; not user-configurable in v0.1. |
| **DEC-013** | Single INFO log at end-of-run via lazy-format JSON | One emission with `{run_id, model_unique_id, draft_tokens, grade_tokens, total_usd, total_bytes, duration_seconds, price_table_version}`. Falls under the 6-dir grep gate. No DEBUG / WARNING beyond DEC-005's partial-failure WARN. |
| **DEC-014** | No new audit-event class, no new fail-closed writer | `--estimate` produces no durable artefact in v0.1. No 8th AST scan; AST-scan count stays at 7. |
| **DEC-015** | 5-surface parity story-gated | The Docs story (US-006) is the parity gate. Story acceptance requires all five surfaces (argparse help, handler docstring, `docs/cli-ops.md` § Flag reference, test name, this DEC) updated in the same PR. |

## Detailed breakdown

Stories follow the project's natural dependency order: data models → adapter expansion → engine → renderer → CLI wiring → docs/parity → Quality Gate → Patterns & Memory.

Every implementation story includes the canonical validation command in its acceptance criteria:

```bash
pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest
```

---

### US-001 — `signalforge.llm.pricing` module

**Description.** Add a new public module `signalforge.llm.pricing` carrying the model-price table, version constant, frozen `ModelPricing` dataclass, and `EstimateUnknownModelError`. This is the single source of truth for per-model USD math used by `--estimate` (and reusable by v0.2 cost-projection callers).

**Traces to:** DEC-001, DEC-002, DEC-003.

**Acceptance criteria.**
- `signalforge.llm.pricing.PRICE_TABLE_VERSION: str` constant exists (literal `"2026-05-11"` for v0.1).
- `signalforge.llm.pricing.ModelPricing` is `@dataclass(frozen=True, slots=True)` with four `float` fields: `input_per_mtok`, `output_per_mtok`, `cache_write_5m_per_mtok`, `cache_read_per_mtok`.
- `signalforge.llm.pricing.PRICES: Mapping[str, ModelPricing]` contains entries for `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5` with current per-MTok USD numbers sourced from Anthropic's published pricing.
- `signalforge.llm.pricing.lookup(model: str) -> ModelPricing` returns the entry or raises `EstimateUnknownModelError(model: str)` (subclass of `LLMError`) with remediation `"Add the model to signalforge.llm.pricing.PRICES or use a supported model: claude-sonnet-4-6, claude-opus-4-7, claude-haiku-4-5."`.
- `EstimateUnknownModelError` registered in `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` at tier 2 (input-validation — the operator's config picked a model we can't price).
- 7th AST scan (`tests/test_audit_completeness.py::test_every_typed_error_is_in_exit_code_mapping_table`) passes.
- `signalforge.llm.__init__` re-exports `PRICE_TABLE_VERSION`, `PRICES`, `ModelPricing`, `lookup`, `EstimateUnknownModelError`.
- Validation command passes.

**Done when.** `from signalforge.llm import pricing; pricing.lookup("claude-sonnet-4-6")` returns a `ModelPricing` with non-zero fields; `pricing.lookup("nope")` raises `EstimateUnknownModelError` with remediation text.

**Files.**
- `src/signalforge/llm/pricing.py` (new — ~80 lines).
- `src/signalforge/llm/__init__.py` (add re-exports).
- `src/signalforge/llm/errors.py` (add `EstimateUnknownModelError`).
- `src/signalforge/cli/_helpers.py` (register in `_EXCEPTION_TO_EXIT_CODE`).
- `tests/llm/test_pricing.py` (new).
- `CLAUDE.md` § Public API surface (add bullet under `signalforge.llm`).

**Depends on.** none.

**TDD.**
- `test_lookup_returns_modelpricing_for_known_model_claude_sonnet_4_6` — fields are non-zero.
- `test_lookup_raises_estimateunknownmodelerror_for_unknown_model` — exception message includes the model id and remediation.
- `test_modelpricing_is_frozen` — assigning to a field raises.
- `test_price_table_version_is_a_nonempty_string`.
- `test_pricing_module_exports` — `signalforge.llm.PRICE_TABLE_VERSION` / `PRICES` / `lookup` / `ModelPricing` / `EstimateUnknownModelError` importable.
- `test_estimateunknownmodelerror_is_in_exit_code_mapping_at_tier_2`.

---

### US-002 — `WarehouseAdapter.estimate_query_bytes` ABC method + BigQuery override

**Description.** Add the `estimate_query_bytes(sql: str) -> int` method to `WarehouseAdapter` (default raises `EstimateNotSupportedError`). Implement on `BigQueryAdapter` via `QueryJobConfig(dry_run=True)` reading `job.total_bytes_processed`. Mirrors the `materialise_sample` precedent verbatim. Extend `FakeBigQueryClient` with `expect_dry_run(...)`.

**Traces to:** DEC-004.

**Acceptance criteria.**
- `WarehouseAdapter.estimate_query_bytes(self, sql: str) -> int` exists on the ABC. Default implementation raises `EstimateNotSupportedError` with the locked remediation: `"Use --estimate with a BigQuery profile, or wait for v0.3 multi-warehouse estimation support."`.
- `BigQueryAdapter.estimate_query_bytes(sql)` constructs `QueryJobConfig(dry_run=True, use_query_cache=False)` (NO `maximum_bytes_billed` — dry_run doesn't bill bytes), calls `client.query(sql, job_config=...)`, returns `int(job.total_bytes_processed)`. Raises typed `WarehouseError` subclasses on auth / quota / network failures (reuses existing taxonomy; no new error class beyond `EstimateNotSupportedError`).
- `EstimateNotSupportedError(WarehouseError)` registered in `_EXCEPTION_TO_EXIT_CODE` at tier 3.
- `FakeBigQueryClient.expect_dry_run(sql_matching: str, returns_bytes: int)` queues an expectation matching the SQL regex; consumed by one `client.query(...)` call with `job_config.dry_run is True`. Asserts `dry_run` is set; raises `AssertionError` if `dry_run` is not set on a queued dry_run expectation, or if a `client.query` call with `dry_run=True` arrives without an `expect_dry_run` queued.
- `tests/warehouse/test_bigquery_estimate.py` covers happy path, auth failure, network failure, quota failure (each reuses existing `WarehouseError` subclasses).
- `signalforge.warehouse.__init__` exports `EstimateNotSupportedError`.
- 7th AST scan passes (new error in the mapping table).
- `docs/warehouse-adapter-ops.md` § Error reference adds `EstimateNotSupportedError`; § new section "Query-bytes estimation" documents the method.
- `CLAUDE.md` § Public API surface (v0.1 + v0.2 additions) adds `WarehouseAdapter.estimate_query_bytes` and `EstimateNotSupportedError`.
- Validation command passes.

**Done when.** A `BigQueryAdapter` with a `FakeBigQueryClient` that has one `expect_dry_run(matching=r"SELECT", returns_bytes=12345)` queued, when called with `adapter.estimate_query_bytes("SELECT 1")`, returns `12345` and asserts all expectations met. A bare `WarehouseAdapter` subclass without an override, when called with `estimate_query_bytes("SELECT 1")`, raises `EstimateNotSupportedError`.

**Files.**
- `src/signalforge/warehouse/base.py` (add ABC method).
- `src/signalforge/warehouse/errors.py` (add `EstimateNotSupportedError`).
- `src/signalforge/warehouse/adapters/bigquery.py` (add override).
- `src/signalforge/warehouse/adapters/_client.py` (extend `_BQClientProtocol` if needed for `total_bytes_processed` — verify; likely already covered via `Any`).
- `src/signalforge/warehouse/__init__.py` (export new error).
- `src/signalforge/cli/_helpers.py` (register `EstimateNotSupportedError`).
- `tests/warehouse/_fake.py` (add `expect_dry_run`).
- `tests/warehouse/test_bigquery_estimate.py` (new).
- `tests/warehouse/test_base.py` or equivalent (assert default raises `EstimateNotSupportedError`).
- `docs/warehouse-adapter-ops.md` (update).
- `CLAUDE.md` (update public API surface).

**Depends on.** US-001 (not strictly, but US-001 lands the `_EXCEPTION_TO_EXIT_CODE` test pattern this story repeats).

**TDD.**
- `test_estimate_query_bytes_dry_run_returns_total_bytes_processed`.
- `test_estimate_query_bytes_uses_dry_run_true_in_job_config`.
- `test_estimate_query_bytes_does_not_set_maximum_bytes_billed` (dry_run doesn't bill).
- `test_estimate_query_bytes_propagates_warehouse_auth_error`.
- `test_estimate_query_bytes_propagates_warehouse_connection_error`.
- `test_warehouse_adapter_abc_default_raises_estimatenotsupportederror`.
- `test_estimatenotsupportederror_remediation_locked_verbatim`.

---

### US-003 — `signalforge.cli._estimate` engine + `EstimateReport` typed result

**Description.** Pure-function engine that takes the prelude products (model, manifest, configs, adapter, anthropic client) and returns a typed `EstimateReport` capturing draft tokens, per-criterion grade tokens, estimated bytes, and computed USD. Issues exactly one `count_tokens` per stage-prompt + one `dry_run` query; never `messages.create` or non-dry-run `query`.

**Traces to:** DEC-005, DEC-007, DEC-010, DEC-011, DEC-012, DEC-013.

**Acceptance criteria.**
- `signalforge.cli._estimate.EstimateReport` Pydantic `BaseModel(ConfigDict(frozen=True, extra="ignore"))` with fields: `model_unique_id`, `drafter_model: str`, `grader_model: str`, `draft_input_tokens: int`, `draft_output_tokens_estimate: int`, `draft_usd: float`, `grade_artifacts_count: int`, `grade_criteria_count: int`, `grade_per_criterion: tuple[CriterionEstimate, ...]`, `grade_usd: float`, `total_llm_usd: float`, `warehouse_bytes_per_row: int | None`, `warehouse_total_bytes: int | None`, `warehouse_unavailable_reason: str | None`, `tests_per_column_heuristic: float = 3.5`, `sample_size: int`, `price_table_version: str`, `duration_seconds: float`. Custom `__repr__` shows minimal fields per `prune-engine.md` DEC-022.
- `signalforge.cli._estimate.estimate(model, manifest, draft_config, grade_config, prune_config, adapter, anthropic_client, *, project_dir=None) -> EstimateReport` returns the typed report.
- Calls `client.messages.count_tokens(...)` exactly once per (draft prompt, grade-criterion prompt) pair. Never calls `messages.create`. Test pins this via `FakeAnthropicClient.messages._create_calls` length 0.
- Calls `adapter.estimate_query_bytes(...)` exactly once with a representative `not_null` SQL compiled against the model's source table. If it raises ANY `WarehouseError`, the report's `warehouse_unavailable_reason` field captures `f"{type(exc).__name__}: {str(exc)[:200]}"` and `warehouse_bytes_per_row`/`warehouse_total_bytes` are `None`. Emits one lazy-format JSON WARNING. Does NOT propagate. All non-`WarehouseError` exceptions propagate.
- Draft USD = `(draft_input_tokens / 1_000_000) * pricing.input_per_mtok + (draft_output_tokens_estimate / 1_000_000) * pricing.output_per_mtok`. `draft_output_tokens_estimate` derived from `DraftConfig.max_output_tokens` (use the config knob; document the assumption).
- Grade USD = sum over criteria of `(criterion_input_tokens / 1_000_000) * pricing.input_per_mtok + (estimated_output_tokens / 1_000_000) * pricing.output_per_mtok`. Estimated output tokens for grade: 50 per call (per-criterion JSON shape is short; documented).
- Single end-of-run INFO via lazy-format JSON (DEC-013 fields).
- `estimate` is NOT exported on `signalforge.cli.__init__` (private to the CLI).
- Validation command passes.

**Done when.** Calling `estimate(...)` against the Austin fixture (with `FakeAnthropicClient` returning known token counts and `FakeBigQueryClient` returning known bytes) produces an `EstimateReport` whose `total_llm_usd` matches the hand-calculated value to four decimal places.

**Files.**
- `src/signalforge/cli/_estimate.py` (new — engine + types).
- `tests/cli/test_estimate_engine.py` (new).
- `tests/fixtures/estimate/` — input fixtures (re-use Austin where possible).

**Depends on.** US-001, US-002.

**TDD.**
- `test_estimate_calls_count_tokens_for_draft_and_each_grade_criterion`.
- `test_estimate_never_calls_messages_create` (asserts `len(_create_calls) == 0`).
- `test_estimate_calls_dry_run_once_for_warehouse_bytes`.
- `test_estimate_total_llm_usd_matches_hand_calculation`.
- `test_estimate_degrades_on_warehouse_auth_error_and_continues`.
- `test_estimate_warehouse_unavailable_reason_carries_error_class_name`.
- `test_estimate_propagates_llm_auth_error` (non-degraded path).
- `test_estimate_propagates_estimateunknownmodelerror`.
- `test_estimate_report_repr_omits_per_criterion_payloads` (DEC-022 mirror).

---

### US-004 — `signalforge.cli._estimate.render` text renderer + snapshot test

**Description.** Pure-function renderer that takes `EstimateReport` and returns the three-section stdout text per DEC-007. Snapshot-tested against a committed fixture.

**Traces to:** DEC-007, DEC-011.

**Acceptance criteria.**
- `signalforge.cli._estimate.render(report: EstimateReport) -> str` is a pure function returning the formatted text matching DEC-007's preview shape byte-for-byte (snapshot-tested).
- The warehouse section renders `<unavailable: <ErrorClass>>` when `report.warehouse_unavailable_reason is not None` (DEC-005); total bytes renders `<unknown>`.
- Output ends with a single trailing newline.
- Footer carries `Price table: <version>  |  Heuristic: ~3.5 tests/column (canonical fixture average)`.
- Snapshot fixture `tests/fixtures/estimate/output_happy.txt` pins the happy-path byte sequence.
- Snapshot fixture `tests/fixtures/estimate/output_warehouse_unavailable.txt` pins the partial-failure shape.
- Validation command passes.

**Done when.** `render(report)` for the canonical Austin estimate matches `output_happy.txt` byte-for-byte; for a warehouse-failure variant matches `output_warehouse_unavailable.txt`.

**Files.**
- `src/signalforge/cli/_estimate.py` (extend with `render`).
- `tests/cli/test_estimate_render.py` (new).
- `tests/fixtures/estimate/output_happy.txt` (new).
- `tests/fixtures/estimate/output_warehouse_unavailable.txt` (new).

**Depends on.** US-003.

**TDD.**
- `test_render_matches_happy_snapshot_byte_for_byte`.
- `test_render_partial_failure_warehouse_unavailable_shape`.
- `test_render_ends_with_single_trailing_newline`.
- `test_render_footer_carries_price_table_version_and_heuristic`.
- `test_render_per_criterion_section_lists_each_active_criterion`.

---

### US-005 — `--estimate` flag wiring in `signalforge generate`

**Description.** Add `--estimate` to the existing argparse mutex group with `--write` / `--dry-run`. Add a short-circuit branch in `cmd_generate` that runs the full prelude (manifest → configs → profile → adapter → anthropic client), invokes `_estimate.estimate(...)`, prints `_estimate.render(...)` to stdout, returns 0. Skips `draft_schema` / `prune_tests` / `grade_artifacts` / `render_diff`.

**Traces to:** DEC-005, DEC-006, DEC-008, DEC-009, DEC-013.

**Acceptance criteria.**
- `signalforge generate --estimate <model>` exits 0 and prints the rendered estimate to stdout. No artefact written (mirrors `--dry-run` for the file side).
- `signalforge generate --estimate --write <model>` exits 2 (argparse mutex; `CliInputError`).
- `signalforge generate --estimate --dry-run <model>` exits 2 (same mutex).
- `signalforge generate --estimate <model>` with no `ANTHROPIC_API_KEY` exits 3 (`LLMAuthError` from `count_tokens`).
- The handler's existing `try/except Exception` boundary covers the new path (no traceback leaks; DEC-016 of `cli-layer.md`).
- `add_parser` help string for `--estimate` is one line, mentions `count_tokens` + `dryRun` and that no billable calls are made.
- `cmd_generate` docstring documents the `--estimate` contract (surface 2 of 5).
- Logger-grep gate passes (no f-string in any new `_LOGGER` call).
- A new test `test_generate_estimate_zero_messages_create_calls` (AC-4 of the ticket) asserts `len(fake.messages._create_calls) == 0` and `len(fake.messages._count_calls) >= 1` (at least one `count_tokens` issued).
- A test `test_generate_estimate_mutex_with_write` asserts argparse error → exit 2.
- A test `test_generate_estimate_mutex_with_dry_run` asserts argparse error → exit 2.
- A test `test_generate_estimate_full_prelude_runs` asserts warehouse profile load failures (bad creds) surface as tier 3 BEFORE any LLM call.
- A test `test_generate_estimate_warehouse_failure_degrades` asserts warehouse error during dry_run renders `<unavailable>` and exits 0.
- A test `test_generate_estimate_no_traceback_leaks` asserts `"Traceback" not in capsys.readouterr().err` on all happy and error paths.
- Validation command passes.

**Done when.** `pytest tests/cli/test_generate_estimate.py` passes; `pytest -m cli_subprocess --no-cov` passes (console-script regression check).

**Files.**
- `src/signalforge/cli/generate.py` (add flag to mutex group; short-circuit branch in `cmd_generate`; docstring update).
- `tests/cli/test_generate_estimate.py` (new — covers all flag scenarios).

**Depends on.** US-003, US-004.

**TDD.**
- All AC test names above written FIRST as failing tests.

---

### US-006 — Docs + 5-surface parity (`docs/cli-ops.md`, `CLAUDE.md`)

**Description.** Land surfaces 3 + 5 of the cli-layer 5-surface parity rule: `docs/cli-ops.md` § Flag reference adds `--estimate` (paraphrased from the handler docstring); `CLAUDE.md` § Public API surface adds the new `signalforge.llm.pricing` exports and the `WarehouseAdapter.estimate_query_bytes` ABC method + `EstimateNotSupportedError` + `EstimateUnknownModelError`. Also adds a brief `--estimate` § to README's Quick Start (one sentence pointing at the flag).

**Traces to:** DEC-015 (the parity gate); DEC-001, DEC-002, DEC-004 surface 3 mirrors.

**Acceptance criteria.**
- `docs/cli-ops.md` has a `--estimate` entry under the appropriate flag-reference section. Documents: contract (count_tokens + optional dryRun, no billable calls), mutex with `--write`/`--dry-run`, exit codes (0 happy, 0 with `<unavailable>` warehouse, 3 on LLM auth), output shape (point to the snapshot fixture), price-table version reference.
- `CLAUDE.md` § "Public API surface (v0.1 + v0.2 additions)" adds:
  - One bullet under `signalforge.llm` listing `pricing.PRICE_TABLE_VERSION`, `pricing.PRICES`, `pricing.ModelPricing`, `pricing.lookup`, `EstimateUnknownModelError`.
  - One bullet extending `signalforge.warehouse` with `WarehouseAdapter.estimate_query_bytes` + `EstimateNotSupportedError`.
- `README.md` § Quick Start adds a one-line pointer to `--estimate` under the existing example commands (no full walkthrough — the ops doc is the source of truth).
- The five surfaces are reconciled:
  1. argparse help (from US-005)
  2. handler docstring (from US-005)
  3. `docs/cli-ops.md` § Flag reference (this story)
  4. test name (`test_generate_estimate_*` — US-005)
  5. DEC in `plans/super/36-*.md` (this plan)
- Validation command passes.

**Done when.** `grep -rn "estimate" docs/cli-ops.md CLAUDE.md README.md` shows the new entries; `pytest` passes.

**Files.**
- `docs/cli-ops.md` (extend).
- `CLAUDE.md` (extend Public API surface).
- `README.md` (one-line pointer in Quick Start).

**Depends on.** US-001, US-002, US-005.

---

### US-007 — Quality Gate — code review x4 + CodeRabbit

**Description.** Run the code-review skill four times across the full changeset, fixing all real bugs each pass. Run CodeRabbit if available. Validate the full validation command passes after every fix.

**Acceptance criteria.**
- Code review pass 1 completed; all real bugs fixed.
- Code review pass 2 completed; all real bugs fixed.
- Code review pass 3 completed; all real bugs fixed (focus area: 5-surface parity drift).
- Code review pass 4 completed; all real bugs fixed (focus area: cross-stage error propagation, dryRun fake parity).
- CodeRabbit review run (or noted as unavailable).
- `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest` passes.
- `pytest -m cli_subprocess --no-cov` passes.

**Done when.** Four review passes are recorded in the PR thread / closeout note; validation passes.

**Files.** All files touched by US-001..US-006.

**Depends on.** US-001, US-002, US-003, US-004, US-005, US-006.

---

### US-008 — Patterns & Memory (priority 99)

**Description.** Distil any patterns established by this work into `.claude/rules/` updates. Specifically: a v0.2 reservation in `cli-layer.md` for the degrade-rather-than-fail pattern on supplementary sub-stages (mirrors prune DEC-009 generalisation); a 5-surface parity confirmation in this plan's DEC log; an entry in `warehouse-adapters.md` extending the `estimate_query_bytes` precedent into the "non-BQ-adapter graceful degrade" group with `materialise_sample`.

**Acceptance criteria.**
- `.claude/rules/cli-layer.md` updated with one new note: "Estimate-style commands degrade on supplementary sub-stage failures (DEC-005 of #36) — apply the conservative-bias routing pattern from `prune-engine.md` DEC-009 verbatim to any future preview/dry-run flag that has multiple data sources." Includes a See-Also pointer.
- `.claude/rules/warehouse-adapters.md` "Non-BQ adapter graceful degrade" section extended to list `estimate_query_bytes` alongside `materialise_sample`.
- This plan's `Refinement log` re-cross-checked against the implementation; any drift documented as a post-hoc DEC.
- Validation command passes.

**Done when.** Rules updates merged in the same PR.

**Files.**
- `.claude/rules/cli-layer.md` (extend).
- `.claude/rules/warehouse-adapters.md` (extend).
- `plans/super/36-estimate-cost-preview.md` (any post-hoc DECs).

**Depends on.** US-007.

## Beads manifest

- **Epic:** `bd_1-scaffolding-bmz` — 36: signalforge generate --estimate cost preview (P1, external-ref `gh-36`).
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/36-estimate-cost-preview` (branch `feature/36-estimate-cost-preview`, tracking `origin/feature/36-estimate-cost-preview`).

| Bead ID | Story | Priority | Depends on |
|---------|-------|----------|------------|
| `bd_1-scaffolding-bmz.1` | US-001: `signalforge.llm.pricing` module | P1 | — (READY) |
| `bd_1-scaffolding-bmz.2` | US-002: `WarehouseAdapter.estimate_query_bytes` + BigQuery dryRun | P1 | US-001 |
| `bd_1-scaffolding-bmz.3` | US-003: estimate engine + `EstimateReport` typed result | P1 | US-001, US-002 |
| `bd_1-scaffolding-bmz.4` | US-004: text renderer + snapshot fixtures | P1 | US-003 |
| `bd_1-scaffolding-bmz.5` | US-005: `--estimate` flag wiring in `signalforge generate` | P1 | US-003, US-004 |
| `bd_1-scaffolding-bmz.6` | US-006: docs + 5-surface parity | P1 | US-001, US-002, US-005 |
| `bd_1-scaffolding-bmz.7` | US-007: Quality Gate — code review x4 + CodeRabbit | P1 | US-001..US-006 |
| `bd_1-scaffolding-bmz.8` | US-008: Patterns & Memory — rules updates | P4 | US-007 |

Run `bd ready` from the worktree to see the next available task.
