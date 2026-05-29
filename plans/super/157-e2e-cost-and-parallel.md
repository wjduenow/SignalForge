# 157 — E2E suite: real-measured cost docs + parallelization

## Meta

- **Ticket:** [#157](https://github.com/wjduenow/SignalForge/issues/157)
- **Branch:** `feature/157-e2e-cost-parallel`
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/157-e2e-cost-parallel`
- **Phase:** devolved (beads created 2026-05-29; ready for `/ralph-run`)
- **Created:** 2026-05-29
- **Sessions:** 1

## Ticket summary

The first full live-e2e run after #155 took **39 min wall-clock for 6 tests** vs the ~3-6 min and ~$0.30/run estimate baked into the plan + docs. Two asks:

1. **Update cost+duration docs to real-measured.** Three surfaces drift: `plans/super/155-gemini-truncation-e2e-gap.md` DEC-010 ("~$0.30/full-suite run"), `CONTRIBUTING.md` § "Live e2e suite (pre-release only)" (inherits that figure), and `docs/grade-ops.md` Cost guidance § ("$0.18/model on Sonnet 4.6" — built from a stale "~12 artifacts × 4 criteria = 48 calls" assumption when the Austin fixture is actually ~29 artifacts × 4 criteria = 108-116 calls per test).
2. **Evaluate parallelizing the e2e suite for wall-clock speed.** The 6 tests are mutually independent; `pytest-xdist` at the test level could cut ~39 min → ~13-18 min. Grade engine stays sequential (per `grade-layer.md` DEC-004/DEC-027); parallelism is purely at the test-node level.

Out of scope (separately filed): the Gemini-grader's `max_output_tokens=2048` floor was already raised to 4096 in #158/PR #160 — independent.

## Discovery (Phase 1)

### Codebase scout findings (Subagent B)

**E2E test surface — exactly 6 nodes collected by `pytest -m e2e`:**

| File | Test func | Parametrize | Gate env vars |
|---|---|---|---|
| `tests/cli/test_e2e_bigquery_smoke.py` | `test_e2e_signalforge_generate_against_austin_bikeshare` | `grade_provider ∈ {anthropic, openai, gemini}` | All variants: `SF_RUN_BQ=1`, `ANTHROPIC_API_KEY`, `GOOGLE_CLOUD_PROJECT`. `openai`: +`SF_RUN_OPENAI=1`+`OPENAI_API_KEY`. `gemini`: +`SF_RUN_GEMINI=1`+`GOOGLE_API_KEY`. |
| `tests/cli/test_e2e_business_rules.py` | `test_e2e_custom_sql_business_rules_end_to_end` | — | `SF_RUN_BQ=1`, `ANTHROPIC_API_KEY`, `GOOGLE_CLOUD_PROJECT` |
| `tests/cli/test_e2e_openai_smoke.py` | sibling smoke | — | drafter Anthropic + grader OpenAI (5 env vars per `testing-signal.md`) |
| `tests/cli/test_e2e_gemini_smoke.py` | sibling smoke | — | drafter Anthropic + grader Gemini (5 env vars) |

Six nodes total. (`test_e2e_snowflake_smoke.py` is gated by `snowflake`, not `e2e`; `test_e2e_estimate_openai.py` uses its own marker.)

**Shared helpers — parallel-safe:** `tests/cli/_e2e_helpers.py` provides `copy_fixture_to_tmp(tmp_path)`, `apply_provider_override(...)`, `read_prune_decisions(...)`, `read_diff_report(...)`, `inject_model_business_rules(...)`. Every helper either reads committed fixtures (read-only) or writes under `tmp_path` — no shared mutable state, no env mutation. `apply_provider_override` is the per-test grader-swap seam (`testing-signal.md` § "Per-test provider overlay").

**Audit JSONL carries everything pricing needs:**
- `LLMResponseEvent` (drafter, `.signalforge/llm_responses.jsonl`) — `model`, `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`.
- `GradeEvent` (grader, `.signalforge/grade.jsonl`) — same five fields (cache fields default 0 for OpenAI/Gemini).

**Pricing table already exists.** `src/signalforge/llm/pricing.py` has a frozen `PRICES: MappingProxyType` indexed by model id, `PRICE_TABLE_VERSION = "2026-05-28"`, `lookup(model) -> ModelPricing`. Active SKUs cover everything the suite uses (Anthropic Sonnet 4.6, OpenAI gpt-4o, Gemini 2.5 Flash). **Implication:** computing real measured USD is `sum(input * input_price + output * output_price + cache_* * cache_*_price) / 1e6` over both JSONLs — no new pricing surface needed.

**`pytest-xdist` is NOT a dependency.** No `-n` in `addopts`. No serial-only flag. No architectural barrier in `[tool.pytest.ini_options]`.

### Convention checker findings (Subagent C)

Constraints from `.claude/rules/*.md` that bear on this plan:

- **`testing-signal.md` § "End-to-end gated tests"** — the "belt-and-suspenders gating" rule (marker + runtime `_skip_reason()`); the `tmp_path` isolation rule; the "per-test provider overlay via `apply_provider_override`" seam. xdist must NOT break any of those. The 5-env-var gate for full-pipeline e2e (drafter API key + grader API key + their `SF_RUN_*` opt-ins + `GOOGLE_CLOUD_PROJECT`) is the contract.
- **`grade-layer.md` DEC-004 / DEC-027** — grade engine MUST stay sequential per-`(criterion, artifact)`. Parallelism is at the pytest node level, never inside a grade run.
- **`testing-signal.md` § "Engineered determinism"** — assertions must remain deterministic across runs. Parallel execution doesn't change determinism (each test owns its `tmp_path`), but a maintainer running with `-n auto` must still get the same kept/dropped counts.
- **`ci-supply-chain.md`** — every long-lived branch trigger needs lockstep updates. `pytest-xdist` going into `[dependency-groups].dev` flows through `uv sync --dev` automatically; no workflow changes needed *unless* CI starts opting into `-n`.
- **`python-build.md`** — `[dependency-groups].dev` + `[project.optional-dependencies].dev` mirror each other; new dep lands in both.
- **`cli-layer.md` § "5-surface parity"** — N/A here; no CLI behaviour change.

**No `workflow-project.md` exists** — using baseline scoping questions only.

### Existing doc surfaces to update (paths + headings)

1. `plans/super/155-gemini-truncation-e2e-gap.md` — DEC-010 ("~$0.30/full-suite run"), plus the "Cost / cadence" row in the architecture-review table.
2. `CONTRIBUTING.md` § "Live e2e suite (pre-release only)" (around line 96-130).
3. `docs/grade-ops.md` § Cost guidance — the "$0.18/model on Sonnet 4.6" reference figure + the per-provider floor table.

### Scoping decisions

- **DEC-Q1 — Scope:** Both asks ship together in one plan/PR. Single coherent change.
- **DEC-Q2 — Cost source:** Ship a re-runnable rollup helper (walks `.signalforge/*.jsonl` × `signalforge.llm.pricing`) AND ship the measured baseline computed by it. Helper makes future re-measurement boring; baseline gives the docs a concrete number today.
- **DEC-Q3 — Xdist shape:** Add `pytest-xdist` to `[dependency-groups].dev` + mirror; document `uv run pytest -m e2e -n 3 --no-cov` as the recommended maintainer invocation in CONTRIBUTING. **No `addopts` change** — default behaviour stays sequential.
- **DEC-Q4 — Measurement:** Maintainer re-runs the live suite as part of implementation (one story explicitly for this). Helper exists before the run so figures aren't hand-reconstructed.

## Architecture Review (Phase 2)

No blockers; three concerns to resolve in refinement. Reviewed the proposed shape (rollup helper as `signalforge.llm.pricing.rollup_audit_dir(...)` library function + thin `scripts/measure_e2e_cost.py` wrapper, `pytest-xdist` as opt-in dev dep, live re-run as part of impl).

| Review area | Rating | Findings |
|---|---|---|
| **Security** | pass | Helper reads only token-count + model-id fields, never echoes `evidence`/`reasoning`. Path safety must route through `signalforge._common.path_safety.canonicalise_path` (the convention for any user-supplied path; `manifest-readers.md`). No credentials surface. Three parallel BigQuery temp-tables on the maintainer's billing project at Austin scale (≪100M rows after sample) are well inside slot quotas. |
| **Performance / cost** | concern | **Anthropic 50 RPM is the tight gate.** With drafter (Anthropic, singleton) + three parallel Anthropic graders the suite can hit ~50 calls in one epoch and trigger the `WARNING: rate limit` retry path (`llm-drafter.md` DEC-005). Gemini 60 RPM and OpenAI 500 RPM are comfortable at `-n 3`. **Wall-clock bound:** test-level parallelism is capped by the *longest* test (the BQ smokes at ~8 min each). With 3 BQ variants + 3 standalone smokes, `-n 3` can in principle pack three ~8 min tests into one ~8 min wave + three shorter tests into another, so the floor is ~16-18 min vs 39 serial. Real speedup needs measurement — DEC-Q4 covers that. **Recommendation:** document `-n 3` with the rate-limit caveat + monitoring guidance, allow maintainer to downgrade to `-n 2` if Anthropic retries spike. |
| **Data model / API** | pass | The rollup return shape ships as `@dataclass(frozen=True) CostReport` (NOT Pydantic), so it sidesteps the `extra="ignore"` + drift-detector contract — it's a pure compute output, never serialised to a JSONL/sidecar that downstream consumers read back. No `audit_schema_version` bump: the helper consumes existing `LLMResponseEvent` / `GradeEvent` fields only (`input_tokens` / `output_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` / `model`). |
| **Observability / fail-soft** | concern | Helper needs ~3 typed errors: `CostRollupAuditMissingError` (neither JSONL present), `CostRollupMalformedRecordError(line_num, reason)` (bad JSONL line), `CostRollupUnknownModelError(model_id)` (pricing-table miss). Each carries `default_remediation` per the `manifest-readers.md` rule. **Decision:** does it need its own subpackage `signalforge.llm.cost` with `errors.py`, or extend `signalforge.llm.pricing` with the new functions + typed errors? The latter avoids growing scan-7's "exactly 11 errors.py modules" count (`cli-layer.md`). |
| **Testing strategy** | pass | Helper is unit-testable against `tests/fixtures/draft/llm_response_*.json` + `tests/fixtures/grade/grade_event_v1.jsonl` + the frozen `PRICES` table. Add deterministic micro-fixtures for the rollup arithmetic. **pytest-xdist interaction with maintainer-only markers:** `cli_subprocess` (5 tests in one file → no parallel collision risk on the installed wheel), `wheel_smoke` (one test, builds wheel into a temp dir → no shared-state risk). Recommendation: only `e2e` gets the `-n 3` recommendation; serial stays the default for `cli_subprocess` / `wheel_smoke` invocations. |
| **CONTRIBUTING / docs** | concern | `CONTRIBUTING.md` § "Live e2e suite (pre-release only)" at line 96 IS the section to update (line 106 has the `$0.30` figure). **Latent doc gap to fix in this plan:** `test_e2e_business_rules.py` IS marked `@pytest.mark.e2e` but is NOT listed in CONTRIBUTING's enumeration (lines 119-148 list only the four BQ/OpenAI/Gemini/Snowflake files). Plan must add the business_rules entry to the list. `docs/grade-ops.md` § Cost guidance needs per-provider USD rows (Anthropic + OpenAI + Gemini) — current text has only the Sonnet figure. `plans/super/155-…md` DEC-010 update is the small change. `docs/cost-estimate-ops.md` documents the `--estimate` preview, separate concern, no cross-reference needed. |

### Concerns to resolve in Refinement

- **C1.** Concurrency level — recommend `-n 3`, `-n 2`, or no specific number?
- **C2.** Helper home — `signalforge.llm.pricing` extended in-place, or new `signalforge.llm.cost` subpackage?
- **C3.** Doc-gap scope — fix the missing `business_rules` enumeration entry as part of this plan, or file separately?

## Refinement Log (Phase 3)

### Decisions

- **DEC-001 — `-n 3` is the recommended xdist concurrency, with documented Anthropic rate-limit caveat.**
  - *Rationale:* Anthropic 50 RPM is the tight gate; OpenAI 500 RPM + Gemini 60 RPM are comfortable. `-n 3` gives a target wall-clock of ~13-18 min vs ~39 serial; if `_LOGGER.warning("…rate limit…")` events spike in stderr during a real run, the maintainer downgrades to `-n 2`. CONTRIBUTING documents the tuning knob explicitly.

- **DEC-002 — Cost rollup ships as a new `signalforge.llm.cost` subpackage with its own `errors.py`.**
  - *Rationale:* Aligns with the per-stage `errors.py` convention. The 3 typed errors all map to CLI tier 2 (input-validation); the `CostError` base also gets a dual-registration at tier 2 (the safety-net pattern from `cli-layer.md` — same shape as `ManifestError` → tier 1). Scan-7's glob currently walks `src/signalforge/*/errors.py` (depth 1) — extending it to also discover `src/signalforge/*/*/errors.py` is a one-line shape change + a bump of the expected-paths list from 11 → 12. This is the first sub-stage `errors.py`; the convention generalises cleanly for future sub-packages.

- **DEC-003 — `test_e2e_business_rules.py` enumeration doc gap is fixed in the same CONTRIBUTING update.**
  - *Rationale:* Same surface, same touch, logical to bundle. Avoids a follow-up ticket for a one-line list addition.

- **DEC-004 — Return shape is `@dataclass(frozen=True) CostReport`, not Pydantic.**
  - *Rationale:* The rollup output is a pure compute result, never serialised to a JSONL/sidecar that downstream consumers read back. A frozen dataclass sidesteps the `extra="ignore"` + drift-detector contract that `manifest-readers.md` mandates for any read-back Pydantic model. Carries `per_provider: Mapping[str, ProviderRollup]` (also frozen dataclass) + `total_usd: float` + `pricing_table_version: str` (stamps `signalforge.llm.pricing.PRICE_TABLE_VERSION`).

- **DEC-005 — Helper is read-only; no fail-closed writer; symlink-hardened path canonicalisation at entry.**
  - *Rationale:* Routes the supplied `project_dir` through `signalforge._common.path_safety.canonicalise_path` per `manifest-readers.md` § "Symlink-hardened path resolution". Hardcodes the `.signalforge/` subdir relative to canonicalised `project_dir` (matches the convention in `signalforge.grade` and `signalforge.draft`). No new fail-closed writer to register in scan-8.

- **DEC-006 — `scripts/measure_e2e_cost.py` is repo-only, NOT shipped in wheel.**
  - *Rationale:* Per `python-build.md`, the wheel's `[tool.hatch.build.targets.wheel]` explicitly lists what ships. The maintainer audit script lives alongside future regen scripts and never appears on a user's `pip install signalforge-dbt`. First entry in a `scripts/` directory; sets the precedent.

- **DEC-007 — `pytest-xdist` lands in `[dependency-groups].dev` AND `[project.optional-dependencies].dev`, mirrored.**
  - *Rationale:* `python-build.md` § "uv-managed dev environment" mandates the two lists stay in sync for `uv sync --dev` + `pip install -e ".[dev]"` parity. No `addopts` change — opt-in invocation only.

- **DEC-008 — Maintainer live re-run is its own story.** Bead is marked maintainer-only at devolve; closes when measured baseline lands in this plan's refinement log.

- **DEC-009 — Durable convention captured in `testing-signal.md` § "End-to-end gated tests"** — the "parallel-safe via per-test tmp_path + apply_provider_override" rule plus the rate-limit-caveat invocation pattern. Patterns & Memory story owns this.

### Session notes

- Codebase Scout confirmed token-cost fields are already on both `LLMResponseEvent` (drafter) and `GradeEvent` (grader); no `audit_schema_version` bump needed. Pricing table at `signalforge.llm.pricing.PRICES` already covers every SKU the suite uses.
- Caught the Subagent claim that CONTRIBUTING's "Live e2e suite" section doesn't exist (line 96 verifies it does, with the $0.30 figure at line 106). Doc-update story works from the existing section.
- Latent fix bundled in: CONTRIBUTING's e2e enumeration omits `test_e2e_business_rules.py` (which IS `@pytest.mark.e2e`-marked). DEC-003 covers.

## Detailed Breakdown (Phase 4)

Eight stories. Six implementation + Quality Gate + Patterns & Memory. Each AC ends with the canonical validation command (`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`). US-005 is maintainer-only; the rest are Ralph-eligible.

---

### US-001 — `signalforge.llm.cost` subpackage skeleton + errors

**Description.** Create the new subpackage with `__init__.py` re-exports, an `errors.py` carrying `CostError(LLMError)` base + 3 concretes, and a stub `_rollup.py` whose public function signature exists but raises `NotImplementedError`. Wire the 3 concretes into `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` at tier 2; dual-register `CostError` at tier 2 (single-tier safety net per `cli-layer.md`). Extend scan-7 to walk nested sub-stage `errors.py` files; bump the expected-paths list 11 → 12 to include `llm/cost/errors.py`. Add `CostError` to `_EXCEPTION_MAPPING_EXCLUDED_BASES`.

**Traces to:** DEC-002.

**Files:**
- `src/signalforge/llm/cost/__init__.py` — re-exports `rollup_audit_dir`, `CostReport`, `ProviderRollup`, `CostError`, `CostRollupAuditMissingError`, `CostRollupMalformedRecordError`, `CostRollupUnknownModelError`.
- `src/signalforge/llm/cost/errors.py` — typed-error hierarchy. Each concrete carries `default_remediation`; messages render user-supplied strings via the `_format_value` repr-safe helper (the standard from `manifest-readers.md`).
- `src/signalforge/llm/cost/_rollup.py` — stub `def rollup_audit_dir(project_dir: Path | str, *, audit_dir: str = ".signalforge") -> CostReport: raise NotImplementedError` + `CostReport` / `ProviderRollup` frozen-dataclass definitions (real shape, so the imports/tests in US-002 can pin against them).
- `src/signalforge/cli/_helpers.py` — register 3 concretes at tier 2; dual-register `CostError` at tier 2; add `CostError` to `_EXCEPTION_MAPPING_EXCLUDED_BASES`.
- `tests/test_audit_completeness.py` — extend scan-7's glob to also walk `_SIGNALFORGE_DIR.glob("*/*/errors.py")`; bump expected-paths list 11 → 12 (add `llm/cost/errors.py`); bump the `test_scan_7_discovers_every_per_stage_errors_module` count assertion.
- `tests/llm/cost/__init__.py` — empty (test dir bootstrap, no `tests/__init__.py` per `testing-signal.md`).
- `tests/llm/cost/test_errors.py` — assert: every concrete inherits `CostError`; each carries non-empty `default_remediation`; each appears in `_EXCEPTION_TO_EXIT_CODE` mapped to tier 2; `CostError` base maps to tier 2; `CostError` is in `_EXCEPTION_MAPPING_EXCLUDED_BASES`.

**Done when:** Subpackage importable; scan-7 + AST scan-7-mapping tests green; canonical validation passes.

**Acceptance criteria:**
- `from signalforge.llm.cost import rollup_audit_dir, CostReport, ProviderRollup, CostError, CostRollupAuditMissingError, CostRollupMalformedRecordError, CostRollupUnknownModelError` succeeds.
- Calling `rollup_audit_dir(...)` raises `NotImplementedError` (stub).
- Scan-7 (`test_every_typed_error_is_in_exit_code_mapping_table`) passes with 12 modules discovered.
- `test_scan_7_discovers_every_per_stage_errors_module` count assertion bumped 11 → 12; expected-paths list adds `llm/cost/errors.py`.
- Each of `CostRollupAuditMissingError` / `CostRollupMalformedRecordError` / `CostRollupUnknownModelError` maps to exit code **2**.
- `CostError` is in `_EXCEPTION_MAPPING_EXCLUDED_BASES` AND has a dual-registration table entry at tier 2.
- Canonical validation passes: `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.

**Depends on:** none.

**TDD:**
- Test: importing the subpackage exposes the seven public names.
- Test: `CostError` is a subclass of `LLMError` (preserves the hierarchy).
- Test: each concrete carries a non-empty `default_remediation` string.
- Test: each concrete's `__str__` renders `message + ↳ Remediation: …` (the `manifest-readers.md` rendering contract).
- Test: scan-7 sees `llm/cost/errors.py` and all four classes are mapped.

---

### US-002 — Rollup engine (TDD)

**Description.** Implement `rollup_audit_dir(project_dir, *, audit_dir=".signalforge") -> CostReport`. Walks `project_dir/audit_dir/llm_responses.jsonl` + `project_dir/audit_dir/grade.jsonl`, deserialises each line via the existing `LLMResponseEvent` / `GradeEvent` models, multiplies the four token fields against `signalforge.llm.pricing.lookup(model).*`, and returns a `CostReport` carrying per-provider per-model rollups + grand total. Project-dir canonicalised at entry via `_common.path_safety.canonicalise_path`. Missing both JSONLs → `CostRollupAuditMissingError`; missing one → degraded with the other (operator-friendly). Bad JSONL line → `CostRollupMalformedRecordError(line_num, reason)`. Unknown model id → `CostRollupUnknownModelError(model_id)`.

**Traces to:** DEC-002, DEC-004, DEC-005.

**Files:**
- `src/signalforge/llm/cost/_rollup.py` — full implementation (replace the stub from US-001). `CostReport` + `ProviderRollup` shapes finalised:
  ```python
  @dataclass(frozen=True)
  class ProviderRollup:
      provider: str  # "anthropic" / "openai" / "gemini"
      per_model: Mapping[str, ModelRollup]  # model id -> token + USD
      subtotal_usd: float

  @dataclass(frozen=True)
  class ModelRollup:
      model: str
      input_tokens: int
      output_tokens: int
      cache_creation_input_tokens: int
      cache_read_input_tokens: int
      total_usd: float
      call_count: int

  @dataclass(frozen=True)
  class CostReport:
      per_provider: Mapping[str, ProviderRollup]
      total_usd: float
      pricing_table_version: str  # = signalforge.llm.pricing.PRICE_TABLE_VERSION at run time
      audit_files_consumed: tuple[str, ...]  # ("llm_responses.jsonl", "grade.jsonl") subset
  ```
- `tests/llm/cost/test_rollup.py` — TDD-first. Uses the committed fixtures from `tests/fixtures/draft/` + `tests/fixtures/grade/` plus a small handcrafted fixture for the multi-provider mixed case.
- `tests/llm/cost/test_path_safety.py` — symlink-loop + outside-project rejection tests.

**Done when:** All TDD cases below pass; no `NotImplementedError` left.

**Acceptance criteria:**
- Computes correct per-provider per-model USD from a known-input fixture (assertion is on a hand-computed value — small fixtures, easy arithmetic).
- Handles three provider mixes: Anthropic-only (with cache fields populated), OpenAI-only (cache fields = 0), Gemini-only (cache fields = 0).
- Aggregates correctly when both audit files contain records.
- Raises `CostRollupAuditMissingError` when both JSONLs absent.
- Returns a degraded `CostReport` (with `audit_files_consumed` reflecting the subset) when only one JSONL present.
- Raises `CostRollupMalformedRecordError(line_num=N, reason="<excerpt>")` on a corrupt JSONL line.
- Raises `CostRollupUnknownModelError(model_id=X)` when a record references a model absent from `PRICES`.
- Rejects path outside `project_dir` (symlink containment) via `PathContainmentError` → wrapped as `CostRollupAuditMissingError` (no new path-error class).
- `CostReport.pricing_table_version == signalforge.llm.pricing.PRICE_TABLE_VERSION`.
- Canonical validation passes.

**Depends on:** US-001.

**TDD (test cases listed before implementation):**
- `test_rollup_empty_project_raises_missing_audit_error`
- `test_rollup_only_llm_responses_returns_degraded_report` — `audit_files_consumed == ("llm_responses.jsonl",)`.
- `test_rollup_only_grade_returns_degraded_report` — `audit_files_consumed == ("grade.jsonl",)`.
- `test_rollup_both_jsonls_returns_full_report`
- `test_rollup_anthropic_uses_cache_pricing` — cached input tokens × cache_read_price; uncached × input_price.
- `test_rollup_openai_zero_cache_pricing` — confirms OpenAI's `cache_write_price_per_million == 0.0`.
- `test_rollup_gemini_zero_cache_pricing`
- `test_rollup_mixed_provider_aggregates_correctly` — Anthropic drafter + Gemini grader in one project.
- `test_rollup_malformed_jsonl_line_raises_typed_error` — `line_num` + `reason` populated.
- `test_rollup_unknown_model_raises_typed_error`
- `test_rollup_pins_pricing_table_version`
- `test_rollup_call_count_matches_jsonl_line_count`
- `test_rollup_rejects_audit_path_outside_project_dir` — symlink to `/etc/passwd`-shaped attempt.
- `test_rollup_rejects_symlink_loop_in_project_dir`
- `test_rollup_grand_total_equals_sum_of_provider_subtotals` — invariant check.

---

### US-003 — `scripts/measure_e2e_cost.py` wrapper

**Description.** Thin script that argparse-parses a `project_dir` arg, calls `rollup_audit_dir(...)`, and pretty-prints per-provider per-model + grand total to stdout. Maps typed errors to non-zero exits matching the CLI taxonomy (exit 2 for any `CostError`). Mirrors `cli-layer.md`'s "no traceback ever leaks" rule via one boundary `try/except Exception`. Not shipped in wheel (verified by `wheel_smoke` test extension).

**Traces to:** DEC-006.

**Files:**
- `scripts/measure_e2e_cost.py` — first entry in a `scripts/` directory. Shebang `#!/usr/bin/env python3`. Self-contained: imports from `signalforge.llm.cost`, no other repo modules. Argparse: `--project-dir` (required), `--audit-dir` (default `.signalforge`), `--format {text,json}` (default `text`).
- `tests/scripts/test_measure_e2e_cost.py` — subprocess smoke (NOT gated; runs against a tiny committed fixture). Asserts exit 0 on happy path, exit 2 on missing-audit path, no traceback on stderr.
- `tests/test_wheel_packaging.py` (or wherever the `wheel_smoke` marker test lives) — assert `scripts/` is NOT inside the built wheel. Mirrors `python-build.md`'s `wheel_smoke` shape.

**Done when:** Script runs end-to-end against the committed fixture; wheel smoke confirms `scripts/` excluded; canonical validation passes.

**Acceptance criteria:**
- `python scripts/measure_e2e_cost.py --project-dir <tests/fixtures/.../project>` exits 0 and prints a per-provider table + grand total.
- `--format=json` emits machine-readable JSON with the same data.
- Missing both JSONLs exits 2 with the typed error's remediation rendered to stderr.
- Stderr never contains "Traceback" (the `cli-layer.md` floor applies even though this isn't a registered CLI subcommand).
- `uv run pytest -m wheel_smoke --no-cov` confirms `scripts/` is NOT inside `dist/*.whl`.
- Canonical validation passes.

**Depends on:** US-002.

**TDD:** Light. One subprocess test per exit code (0 / 2-for-missing / 2-for-unknown-model); one wheel-smoke assertion that `scripts/` is excluded.

---

### US-004 — `pytest-xdist` dev dep + CONTRIBUTING parallel-invocation doc

**Description.** Add `pytest-xdist` to both `[dependency-groups].dev` and `[project.optional-dependencies].dev` (mirror per `python-build.md`). No `addopts` change. Rewrite `CONTRIBUTING.md` § "Live e2e suite (pre-release only)" to document the parallel invocation `uv run pytest -m e2e -n 3 --no-cov` with the Anthropic 50-RPM caveat + downgrade-to-`-n 2` guidance. Add the missing `test_e2e_business_rules.py` entry to the e2e enumeration. Add a note that `cli_subprocess` / `wheel_smoke` markers stay serial. No measured-cost figures in this story — those land in US-006 after US-005.

**Traces to:** DEC-001, DEC-003, DEC-007.

**Files:**
- `pyproject.toml` — add `pytest-xdist` (version-pin to a recent stable, e.g. `pytest-xdist>=3.6,<4`) to both lists.
- `uv.lock` — regenerated by `uv sync --dev`.
- `CONTRIBUTING.md` § "Live e2e suite (pre-release only)" — restructure to:
  - List 5 e2e files now (BQ smoke, OpenAI smoke, Gemini smoke, Snowflake smoke, **business_rules**).
  - Document `pytest -m e2e -n 3 --no-cov` as recommended.
  - Document the Anthropic rate-limit caveat + monitoring hint (`grep "rate limit" pytest-stderr.log`).
  - Document the downgrade path (`-n 2` or `-n 1`).
  - Document that `cli_subprocess` and `wheel_smoke` markers stay serial (no `-n` flag).
  - Cross-reference `scripts/measure_e2e_cost.py` for post-run cost rollup.

**Done when:** `pytest-xdist` importable in dev shell; CONTRIBUTING reads coherently; canonical validation passes.

**Acceptance criteria:**
- `python -c "import xdist"` succeeds in a `uv sync --dev` shell.
- `uv run pytest -m e2e -n 3 --collect-only` exits cleanly (does NOT need the live env vars — `--collect-only` just verifies xdist can plan the run).
- CONTRIBUTING.md enumeration lists 5 e2e files (incl. business_rules).
- CONTRIBUTING.md mentions `-n 3` AND the rate-limit caveat AND the downgrade path AND `scripts/measure_e2e_cost.py`.
- A test that grep-asserts the CONTRIBUTING.md surface contains all five enumerated test files passes (parity gate, mirrors the `cli-layer.md` 5-surface pattern).
- Canonical validation passes.

**Depends on:** US-003 (CONTRIBUTING references the script).

**TDD:** A parity test under `tests/` greps the CONTRIBUTING.md surface for each of the 5 e2e file basenames + the `-n 3` invocation + the `pytest-xdist` rate-limit caveat phrasing. (Defensive — exists to catch a future regression that drops one entry; covers the `business_rules` doc-gap and prevents it from recurring.)

---

### US-005 — Maintainer live re-run + measured baseline capture

**Description.** **Maintainer-only.** Maintainer runs `uv run pytest -m e2e -n 3 --no-cov` (or `-n 2` / `-n 1` if Anthropic retries spike during a dry-run) against their billing project. After the run, for each test, points `scripts/measure_e2e_cost.py` at the test's `tmp_path` `.signalforge/` dir (recoverable from `/tmp/pytest-of-<user>/pytest-current/` or via a `tmp_path` retention flag). Aggregates results: per-test wall-clock + per-test USD + per-provider USD + grand total + measured wall-clock for the `-n 3` parallel run vs an `-n 1` serial baseline for at least one comparison data point. Pastes the numbers into this plan's refinement log under a new "Measured baseline (YYYY-MM-DD)" subsection.

**Traces to:** DEC-008.

**Files:**
- `plans/super/157-e2e-cost-and-parallel.md` § Refinement Log — new "Measured baseline" subsection. Block format: a wall-clock table per test, a USD-rollup table per provider, the `-n 3` vs `-n 1` comparison data point, the pricing-table version stamp.

**Done when:** Measured baseline lands in the plan doc; bead is closed by the maintainer with a notes link pointing to the run's `pytest-stderr.log`.

**Acceptance criteria:**
- Plan doc carries: per-test wall-clock seconds, per-test USD breakdown, per-provider grand total, the `-n 3` vs serial wall-clock comparison, the `PRICE_TABLE_VERSION` stamp.
- Any rate-limit retries observed are noted (or "none observed").
- Notes record the actual concurrency the maintainer ran (`-n 1` / `-n 2` / `-n 3`).
- Canonical validation passes (the doc edit is a pure markdown change).

**Depends on:** US-003, US-004.

**TDD:** N/A (manual measurement).

**Operational notes for maintainer (paste into bead description at devolve):**
- Capture `/tmp/pytest-of-$USER/pytest-current/` BEFORE the next test invocation overwrites it.
- For comparable concurrency measurement, run `pytest -m e2e -n 1 --no-cov` immediately after the `-n 3` run on the same project to get a serial wall-clock data point. Optional but useful.
- Sanity-check totals against Anthropic's billing dashboard if available.

---

### US-006 — Lift measured baseline into the 3 doc surfaces

**Description.** Once US-005 lands a measured baseline in the plan's refinement log, lift the numbers into the three user-facing doc surfaces: `plans/super/155-gemini-truncation-e2e-gap.md` DEC-010 + the architecture-review "Cost / cadence" row; `CONTRIBUTING.md` § "Live e2e suite (pre-release only)" (replace the `$0.30` figure); `docs/grade-ops.md` § Cost guidance (add per-provider rows). Frame numbers per the `warehouse-adapters.md` precedent: "calibration signal, not a billing guarantee" + date-stamp with `PRICE_TABLE_VERSION`.

**Traces to:** all of DEC-Q1, DEC-Q2, DEC-001, DEC-008.

**Files:**
- `plans/super/155-gemini-truncation-e2e-gap.md` — update DEC-010's `$0.30/full-suite run` figure + the architecture-review table's "Cost / cadence" row.
- `CONTRIBUTING.md` § "Live e2e suite (pre-release only)" — replace `≈ $0.30 per full-suite run` (line 106) with the measured figure + date-stamp + pricing-table version.
- `docs/grade-ops.md` § Cost guidance — replace the single Sonnet 4.6 line with a per-provider table (Anthropic, OpenAI, Gemini) showing input/output prices × the measured per-test calls. Add the "calibration signal, not billing guarantee" framing.

**Done when:** All 3 surfaces reflect the measured baseline + the framing caveat; canonical validation passes.

**Acceptance criteria:**
- No surface still contains `$0.30` as the suite-cost reference.
- `docs/grade-ops.md` § Cost guidance has a 3-row provider table (Anthropic, OpenAI, Gemini).
- Each surface date-stamps the measurement and includes `PRICE_TABLE_VERSION`.
- Each surface includes the "calibration signal, not a billing guarantee" framing.
- A parity test (extend US-004's grep gate) asserts the three surfaces all quote the same headline number (gate against future drift).
- Canonical validation passes.

**Depends on:** US-005.

**TDD:** Extend the US-004 parity gate to assert the same dollar figure appears across the three surfaces (gate-over-prompt per `testing-signal.md` § "Gate-over-prompt").

---

### US-Quality-Gate — Code review × 4 + CodeRabbit + canonical validation

**Description.** Run the code reviewer 4 times across the full changeset, fixing all real bugs found each pass. Run CodeRabbit review if available. Canonical validation must pass green after all fixes. **No traceback / no lazy-format f-string-logger regression** floors carry across.

**Done when:** 4 code-review passes show no remaining real bugs; CodeRabbit review (if available) clean or addressed; canonical validation green.

**Acceptance criteria:**
- Each of 4 code-review passes is logged in the bead notes with the resulting fix commits.
- All `.claude/rules/` constraints identified in Discovery (Subagent C) are honoured by the final diff.
- Canonical validation passes: `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.
- Logger grep gate (`tests/llm/test_logger_grep_gate.py`) green — extending to the new `signalforge.llm.cost` if/when it logs.

**Depends on:** US-001 … US-006 (all implementation stories).

---

### US-Patterns-and-Memory — Durable convention capture

**Description.** Update `.claude/rules/testing-signal.md` § "End-to-end gated tests" with the durable parallel-safe convention: tests using `apply_provider_override` + `tmp_path` isolation + `copy_fixture_to_tmp` are xdist-safe; document the Anthropic rate-limit caveat + `-n 3` recommendation. Add a brief mention of `signalforge.llm.cost.rollup_audit_dir` as the post-run cost-audit surface. Memory entries for any non-obvious lessons learned (e.g. "scan-7 expanded to walk nested errors.py").

**Traces to:** DEC-009.

**Files:**
- `.claude/rules/testing-signal.md` — extend the e2e § with the parallel-safety convention + `-n 3` invocation + rate-limit caveat + post-run cost-rollup pointer.
- Memory entry (one of, if learned anything non-obvious): "scan-7 generalises to nested `errors.py`" — but only if the implementation revealed something not derivable from CLAUDE.md.

**Done when:** Rule file updated; canonical validation passes; new memory written (if applicable) with the standard frontmatter shape.

**Acceptance criteria:**
- `testing-signal.md` § "End-to-end gated tests" carries the parallel-safety convention.
- Convention names `apply_provider_override` + `tmp_path` + `copy_fixture_to_tmp` as the load-bearing isolation primitives.
- Mentions the rollup helper and CONTRIBUTING.md as the cross-references.
- Canonical validation passes.

**Depends on:** US-Quality-Gate (runs last, captures lessons across the whole set).

---

### Right-sizing check

| Story | Files | Risk | Ralph-shaped? |
|---|---|---|---|
| US-001 | ~5 new + 1 edit | low (mechanical) | ✓ |
| US-002 | 1 new (full impl) + ~2 tests | medium (pricing arithmetic) | ✓ |
| US-003 | 1 new script + 2 tests | low | ✓ |
| US-004 | 1 config + 1 doc + 1 test | low | ✓ |
| US-005 | manual run + 1 doc edit | n/a (maintainer-only) | ✗ — gate at devolve |
| US-006 | 3 doc edits + 1 test ext. | low | ✓ |
| US-QG | review across diff | n/a | ✓ |
| US-Patterns | 1 rule edit (+ memory) | n/a | ✓ |

All Ralph-eligible stories fit in one context window. US-005 is the explicit maintainer hand-off; bead description names the maintainer at devolve.

### Rules compliance audit

- ✓ `cli-layer.md` — tier-2 mapping, dual registration, scan-7 extension, no-traceback floor.
- ✓ `manifest-readers.md` — `extra="forbid"` not applicable (using frozen dataclass per DEC-004); typed errors carry `default_remediation`; symlink-hardened path canonicalisation.
- ✓ `testing-signal.md` — deterministic fixtures, no `assert True`-shaped tests, planted-violation regression for scan-7 extension.
- ✓ `python-build.md` — dual-list dev dep (DEC-007); `scripts/` excluded from wheel (DEC-006 + US-003 wheel_smoke).
- ✓ `ci-supply-chain.md` — no workflow changes; `uv sync --dev` flows through automatically.
- ✓ `docs-publishing.md` — `docs/grade-ops.md` edit propagates to the published site via `mkdocs.yml` nav (already configured).
- ✓ `llm-drafter.md` / `grade-layer.md` — grade engine stays sequential per DEC-004/DEC-027; parallelism is only at the test-node level.
- ✓ `safety-layer.md` — no audit-event construction outside its blessed module (helper is read-only).
- N/A `prune-engine.md`, `diff-renderer.md`, `warehouse-adapters.md`, `ingest-layer.md`, `business-rule-tests.md`, `skill-parity.md` — no touch.

## Beads Manifest (Phase 7)

Created 2026-05-29 via `bd create` from the worktree at
`/home/wesd/Projects/worktrees/SignalForge/157-e2e-cost-parallel`.

- **Epic:** `bd_1-scaffolding-e1a` — 157: E2E cost docs + parallelization
- **Children (8):**
  - `bd_1-scaffolding-e1a.1` — US-001: subpackage skeleton + errors *(ready — no deps)*
  - `bd_1-scaffolding-e1a.2` — US-002: rollup engine (TDD) *(blocked by .1)*
  - `bd_1-scaffolding-e1a.3` — US-003: scripts/measure_e2e_cost.py *(blocked by .2)*
  - `bd_1-scaffolding-e1a.4` — US-004: pytest-xdist + CONTRIBUTING *(blocked by .3)*
  - `bd_1-scaffolding-e1a.5` — US-005: **maintainer live re-run** *(blocked by .3, .4; assignee: wjduenow)*
  - `bd_1-scaffolding-e1a.6` — US-006: lift measured baseline into 3 docs *(blocked by .5)*
  - `bd_1-scaffolding-e1a.7` — Quality Gate *(blocked by .1–.6)*
  - `bd_1-scaffolding-e1a.8` — Patterns & Memory *(blocked by .7)*

`bd ready` immediately after devolve: only US-001 (.1) is unblocked, as expected from the dependency graph.

**Next steps:**
1. Run Ralph: `/ralph-run` (will pick up `bd_1-scaffolding-e1a.1` first).
2. Ralph will stop at US-005 (maintainer-only); maintainer runs the live suite manually, pastes measured baseline into this plan's refinement log, then closes `.5`.
3. Ralph resumes US-006 → Quality Gate → Patterns & Memory.
4. When done: `/closeout`.
