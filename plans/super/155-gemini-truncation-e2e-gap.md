# #155 — Gemini MAX_TOKENS truncation + per-provider full-pipeline e2e gap

## Meta

- **Issue:** [#155](https://github.com/wjduenow/SignalForge/issues/155)
- **Branch:** `feature/155-gemini-truncation-e2e-gap`
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/155-gemini-truncation-e2e-gap`
- **Phase:** `detailing` (stories generated; awaiting publish-PR approval)
- **Parent epic:** [#134](https://github.com/wjduenow/SignalForge/issues/134) (pluggable LLM provider for grading)
- **Sibling refs:** plans/super/{135,136,137}-*.md, plans/super/10-e2e-bigquery-smoke.md
- **Sessions:** 2026-05-28 (first)

## What & Why

Three findings surfaced by live validation of the #134 epic, all rooted in the same structural gap (no full-pipeline e2e for non-Anthropic providers).

1. **Bug (load-bearing).** `GeminiProvider.extract_text_blocks` (`src/signalforge/llm/providers.py:867-897`) only raises `LLMResponseFormatError` when **zero** text parts are collected. A `finish_reason="MAX_TOKENS"` response that produces a partial (truncated mid-string) text part silently returns, the truncated JSON reaches `parse_grade_response`, the grade engine wraps the resulting `GradeOutputError(violation_type="json_parse")` as a degraded result with `reasoning="call failed: GradeOutputError"` — masking the actionable typed degrade (`"call failed: GradeLLMError"`) that `llm-drafter.md` § "Gemini provider shape" DEC-005 of #137 contracts. The same class of bug exists latently in OpenAI (`finish_reason="length"`) and Anthropic (`stop_reason="max_tokens"`); the fix is provider-neutral.

2. **Flake (tactical).** `tests/grade/test_gemini_grade_live.py:142` sets `max_output_tokens=512`; Gemini 2.5-flash's verbose `reasoning` field routinely exceeds that on the smoke fixture's 5 pairs, hitting MAX_TOKENS. Verified passing at 2048.

3. **E2E gap (structural).** No live full-pipeline `signalforge generate` test exists for OpenAI or Gemini — only the BigQuery + Anthropic e2e (`tests/cli/test_e2e_bigquery_smoke.py`). The three grade-only live smokes exercise `grade_artifacts()` in isolation; they never see drafter, prune, diff, or sidecar seams with a non-Anthropic provider. Finding 1 is a worked example of drift the in-isolation test surfaced only because the rendered output failed parse — a full-pipeline smoke would have hit it the same way plus all surrounding contracts.

## Discovery (summary)

- **Bug location:** `src/signalforge/llm/providers.py:867-897` — early `if blocks:` return at line 888-889 swallows partial text. `finish_reason` is at `candidates[0].finish_reason.name`.
- **Existing safety-filter contract pin:** `tests/grade/test_gemini_neutrality.py:381` already asserts `bad.reasoning == "call failed: GradeLLMError"` — the fix makes MAX_TOKENS land on the same assertion.
- **E2E template:** `tests/cli/test_e2e_bigquery_smoke.py` (~49 LLM calls/run, 7 invariants). `tests/cli/test_e2e_snowflake_smoke.py:225-241` shows the `textwrap.dedent` `signalforge.yml` overlay pattern.
- **Markers:** `pyproject.toml` already registers `openai` + `gemini` and excludes them from default `addopts`.
- **Austin fixture Anthropic-isms:** Only `llm.model: claude-sonnet-4-6`. No provider key on grade. Models SQL is provider-agnostic.

## Architecture Review

| Area | Rating | Finding |
|---|---|---|
| Provider seam design | concern | Use new ABC method `is_clean_completion(response) -> bool` (Option B). Centralizes the rule, future-proofs vendors, no AST impact. → DEC-005 |
| Cost / cadence | pass | Full live suite ≈ **$0.30/run** (5 tests). Pre-release-only cadence in CONTRIBUTING. → DEC-010 |
| Test-fixture reusability | pass | Austin's `llm.model` pin is the only Anthropic-ism. Per-test `signalforge.yml` overlay (no `GradeConfig` defaults change). → DEC-009, DEC-012 |
| Helpers refactor | pass | One new `_e2e_helpers.apply_provider_override(project_dir, *, grade_provider, grade_model, grade_max_output_tokens)`. ~15 lines. → DEC-012 |
| Parametrize vs duplicate | pass | Keep 3 separate e2e files; failure ergonomics + cost transparency win. → DEC-011 |
| Regression risk (Finding 1) | **green / mechanical** | No literal-string pin on the old `"call failed: GradeOutputError"`. Drift detectors validate type not value. Empty `response_text_hash` sentinel already standard. |
| Retry classification | pass | `LLMResponseFormatError` raised post-call at `client.py:477`, outside the retry try/except → non-retryable as designed; no wasted retries on truncation. |
| AST scan / confinement | pass | No new vendor SDK constructions; scans 3/9/10 untouched. No logger lazy-format gate violation. |
| Audit-log fixture parity | pass | No committed JSONL/JSON fixture pins the old reasoning string. |

## Decisions

| ID | Decision | Rationale |
|---|---|---|
| **DEC-001** | Fix scope = all three providers (Anthropic + OpenAI + Gemini), not Gemini-only. | The rule's intent is provider-neutral typed degrade. OpenAI's `length` and Anthropic's `max_tokens` are latent versions of the same bug. Fixing one without the others guarantees a #155b. |
| **DEC-002** | Raise predicate = any non-clean-STOP finish_reason. | Future-proof against new finish_reason values the vendors add. "Allowlist of bad reasons" needs maintenance every SDK bump; "anything not in the explicit clean set" doesn't. |
| **DEC-003** | E2E scope = 2 new sibling files + parametrize BQ smoke over `grade.provider ∈ [anthropic, openai, gemini]`. | Sibling files give per-provider failure ergonomics; BQ parametrize covers the cross-provider diff-sidecar rendering contract the in-isolation grade smokes miss. ~$0.30/full-suite run. |
| **DEC-004** | ADR lives at `plans/super/155-*.md` (this doc). | Per-issue convention every other plan uses. Cross-link to 137-gemini-grading.md. |
| **DEC-005** | Provider seam = new abstract method `LLMProvider.is_clean_completion(response) -> bool` called by `call_llm` before `extract_text_blocks` (Option B). Each provider declares `_CLEAN_STOP_REASONS: frozenset[str]`. | Centralizes the rule in the orchestrator (where cross-provider invariants live), forces every future provider to declare its clean-set (can't silently forget), zero AST/confinement impact. |
| **DEC-006** | Anthropic's `stop_reason="tool_use"` is **unclean** in v0.3 (clean set = `{end_turn, stop_sequence}`). | Codebase doesn't use tools today; `tool_use` would signal system-prompt drift or unexpected LLM behaviour. When tool-use intentionally lands, the clean set expands deliberately. |
| **DEC-007** | Error-message text = provider-specific via override `LLMProvider.unclean_finish_reason_message(response) -> str`. Default in ABC; each concrete overrides to surface its vendor-native field name. | Operator-facing diagnostic stays vendor-accurate (`stop_reason` for Anthropic, `finish_reason` for OpenAI/Gemini). |
| **DEC-008** | Per-provider `max_output_tokens` floor table in `docs/grade-ops.md` + `docs/draft-ops.md`: Anthropic **1024**, OpenAI **1024**, Gemini **2048**. Documented as recommended floor for grading workloads, not enforced cap. | Honest floors from observed data. Gemini's verbose `reasoning` provably needs ≥1024; 2048 verified safe in #155 probe. |
| **DEC-009** | Gemini e2e sibling's `max_output_tokens=2048` lives in the test's `signalforge.yml` overlay, NOT a bumped `GradeConfig`/`DraftConfig` production default. | Tested-by-construction; no production-config change. Avoids over-budgeting Anthropic/OpenAI default calls. |
| **DEC-010** | Live-suite cadence = pre-release only, documented in `CONTRIBUTING.md`. NO `make e2e-live-all` wrapper, NO per-PR CI integration. | $0.30/full-suite × ~2-3 pre-release audits/month = ~$0.60-1.00/month for a one-maintainer project. Env-var gating is already explicit; a shell wrapper adds surface area without changing the contract. |
| **DEC-011** | Keep 3 separate e2e test files (`test_e2e_bigquery_smoke.py`, `test_e2e_openai_smoke.py`, `test_e2e_gemini_smoke.py`), do NOT collapse into one parametrized test. Parametrize is internal to BQ over `grade.provider`. | Per-file failure messages name the broken provider; per-file cost is transparent in CI logs; per-file marker gating aligns with the existing `@pytest.mark.openai` / `@pytest.mark.gemini` convention. |
| **DEC-012** | Add `_e2e_helpers.apply_provider_override(project_dir, *, grade_provider=None, grade_model=None, grade_max_output_tokens=None) -> None`. Reads `<project_dir>/signalforge.yml`, overlays the `grade:` block deltas, writes back. Non-destructive: unset knobs left alone. | Surgical edits to per-run temp copy; never modifies the committed fixture. Mirrors the Snowflake `textwrap.dedent` precedent at one level of abstraction. |

## Refinement Log

Session 1 (2026-05-28): 12 DECs captured. All architecture-review concerns resolved. Open issues: none. Ready for detailing.

## Stories (right-sized for Ralph)

Ordering: refactor → tests → impl → docs (per `cli-layer.md` § 5-surface parity and `testing-signal.md` § TDD).

### US-001 — `LLMProvider.is_clean_completion` ABC + 3 concrete impls + `call_llm` wire-in + happy-path tests
**Traces to:** DEC-001, DEC-002, DEC-005, DEC-006, DEC-007
**Description:** Add abstract method `is_clean_completion(response: object) -> bool` and `unclean_finish_reason_message(response: object) -> str` to `LLMProvider` ABC. Implement on all three concretes with per-provider `_CLEAN_STOP_REASONS` frozensets. Wire into `call_llm` AFTER `messages.create` returns and BEFORE `extract_text_blocks`. Raise `LLMResponseFormatError(strategy.unclean_finish_reason_message(response))` when `is_clean_completion` is `False`.
**TDD:** Write the 3 happy-path tests FIRST (one per provider, asserts `is_clean_completion(clean_response) is True`), confirm they fail (method doesn't exist), then implement.
**Files:**
- `src/signalforge/llm/providers.py` — add ABC methods + 3 concrete impls (~60 lines net).
- `src/signalforge/llm/client.py:~477` — add 2-line gate before `extract_text_blocks` call.
- `tests/llm/test_anthropic_provider_via_fake.py` (or sibling) — happy-path `is_clean_completion(end_turn) is True` test.
- `tests/llm/test_openai_provider_via_fake.py` — happy-path `is_clean_completion(stop) is True` test.
- `tests/llm/test_gemini_provider_via_fake.py` — happy-path `is_clean_completion(STOP) is True` test.
**Done when:** All four `uv run` checks pass (ruff/format/pyright/pytest). No new `_LOGGER.\w+\(f"` violations. AST scans 3/9/10 pass.
**Depends on:** none

### US-002 — Per-provider unclean-path tests + `llm-drafter.md` DEC-005 clarification
**Traces to:** DEC-001, DEC-002, DEC-005, DEC-006, DEC-007
**Description:** Write fake-driven tests pinning the unclean-path contract for each provider. Verify `tests/grade/test_gemini_neutrality.py:381`'s existing `assert bad.reasoning == "call failed: GradeLLMError"` still passes (it should — the path now fires earlier but lands at the same degrade). Update `.claude/rules/llm-drafter.md` § "Gemini provider shape" DEC-005 to reflect the new `is_clean_completion` factoring + extend to all three providers.
**TDD:** Tests first. Each asserts `pytest.raises(LLMResponseFormatError)` when provider receives a response with non-clean finish_reason (Anthropic `max_tokens`, OpenAI `length`, Gemini `MAX_TOKENS`, all with partial text present).
**Files:**
- `tests/llm/test_anthropic_provider_via_fake.py` — unclean test (Anthropic `max_tokens` with partial text → raise).
- `tests/llm/test_openai_provider_via_fake.py` — unclean test (OpenAI `length` with partial text → raise).
- `tests/llm/test_gemini_provider_via_fake.py` — unclean test (Gemini `MAX_TOKENS` with partial text → raise).
- `tests/llm/test_client.py` (or sibling) — integration test: `call_llm` raises `LLMResponseFormatError` on unclean finish_reason.
- `.claude/rules/llm-drafter.md` — update § Gemini DEC-005 + add brief § for the analogous Anthropic/OpenAI behaviour.
**Done when:** All four `uv run` checks pass. `test_gemini_neutrality.py:381` continues to pass without modification.
**Depends on:** US-001

### US-003 — Bump `test_gemini_grade_live.py` fixture + add per-provider `max_output_tokens` floor docs
**Traces to:** DEC-008
**Description:** Change `tests/grade/test_gemini_grade_live.py:142` from `max_output_tokens=512` to `max_output_tokens=2048`. Add a "Per-provider `max_output_tokens` recommended floors" table to `docs/grade-ops.md` and `docs/draft-ops.md` (Anthropic 1024 / OpenAI 1024 / Gemini 2048).
**Files:**
- `tests/grade/test_gemini_grade_live.py:142` — `512 → 2048`.
- `docs/grade-ops.md` — add 6-line floor table under "Cost guidance" or "Configuration" section.
- `docs/draft-ops.md` — add same 6-line floor table.
**Done when:** All four `uv run` checks pass. `mkdocs build` (non-strict) emits no new warnings for the touched files.
**Depends on:** none (independent of US-001/US-002)

### US-004 — Add `_e2e_helpers.apply_provider_override` helper
**Traces to:** DEC-012
**Description:** Add `apply_provider_override(project_dir: Path, *, grade_provider: str | None = None, grade_model: str | None = None, grade_max_output_tokens: int | None = None) -> None` to `tests/cli/_e2e_helpers.py`. Reads the existing `signalforge.yml`, applies the `grade:` block overlay, writes back. Non-destructive (unset knobs left alone). Refactor `tests/cli/test_e2e_bigquery_smoke.py` to use it for its baseline-Anthropic config (no behaviour change; proves the helper).
**TDD:** Tests first. Unit test the helper directly in `tests/cli/test_e2e_helpers.py` (does it exist? if not, create it). Assert: overlay preserves untouched keys, applies new keys, raises if `signalforge.yml` is missing.
**Files:**
- `tests/cli/_e2e_helpers.py` — add helper (~15 lines).
- `tests/cli/test_e2e_helpers.py` — add helper unit tests.
- `tests/cli/test_e2e_bigquery_smoke.py` — refactor to use the helper (no behaviour change).
**Done when:** All four `uv run` checks pass. `uv run pytest tests/cli/test_e2e_helpers.py` passes (no markers required).
**Depends on:** none

### US-005 — `test_e2e_openai_smoke.py` (new live e2e)
**Traces to:** DEC-003, DEC-009, DEC-010, DEC-011, DEC-012
**Description:** New full-pipeline `signalforge generate` e2e against BigQuery + OpenAI. Gated `@pytest.mark.e2e` + `@pytest.mark.openai`. Three-env-var skip gate: `SF_RUN_OPENAI=1`, `OPENAI_API_KEY`, `GOOGLE_CLOUD_PROJECT`. Uses Austin bikeshare fixture + `_e2e_helpers.apply_provider_override(project_dir, grade_provider="openai", grade_model="gpt-4o")`. Asserts the BQ smoke's 7 invariants (exit 0, sidecar exists, kept/dropped/flagged counts, always-passes drop, `aggregate_complete=True`, no traceback).
**Files:**
- `tests/cli/test_e2e_openai_smoke.py` (new) — ~100 lines mirroring BQ smoke.
**Done when:** All four `uv run` checks pass. Maintainer-only verification: `SF_RUN_BQ=1 SF_RUN_OPENAI=1 OPENAI_API_KEY=… ANTHROPIC_API_KEY=… GOOGLE_CLOUD_PROJECT=… uv run pytest -m openai --no-cov tests/cli/test_e2e_openai_smoke.py` passes against live APIs.
**Depends on:** US-004

### US-006 — `test_e2e_gemini_smoke.py` (new live e2e, with `max_output_tokens=2048` overlay per DEC-008/009)
**Traces to:** DEC-003, DEC-008, DEC-009, DEC-010, DEC-011, DEC-012
**Description:** New full-pipeline e2e against BigQuery + Gemini. Gated `@pytest.mark.e2e` + `@pytest.mark.gemini`. Three-env-var skip gate: `SF_RUN_GEMINI=1`, `GOOGLE_API_KEY`, `GOOGLE_CLOUD_PROJECT`. Uses `apply_provider_override(project_dir, grade_provider="gemini", grade_model="gemini-2.5-flash", grade_max_output_tokens=2048)`. Same 7 assertions as BQ smoke.
**Files:**
- `tests/cli/test_e2e_gemini_smoke.py` (new) — ~100 lines mirroring BQ smoke.
**Done when:** Same as US-005 with Gemini env vars.
**Depends on:** US-004 (and benefits from US-001/US-002 being in: if a Gemini call hits MAX_TOKENS despite the 2048 cap, the fixed `is_clean_completion` surfaces it as `GradeLLMError` cleanly rather than `GradeOutputError`).

### US-007 — Parametrize `test_e2e_bigquery_smoke.py` over `grade.provider`
**Traces to:** DEC-003, DEC-011, DEC-012
**Description:** Add `@pytest.mark.parametrize("grade_provider", ["anthropic", "openai", "gemini"])` to the BQ smoke. For `openai`/`gemini` variants, gate via `_skip_reason()` on the appropriate env vars AND apply the provider overlay via `_e2e_helpers.apply_provider_override`. Drafter stays Anthropic for fixture stability.
**Files:**
- `tests/cli/test_e2e_bigquery_smoke.py` — add parametrize decorator + env-gate logic per parameter + overlay call.
**Done when:** All four `uv run` checks pass. Maintainer-only: three variants run independently (`-k anthropic` / `-k openai` / `-k gemini`).
**Depends on:** US-004

### US-008 — `CONTRIBUTING.md` update — live-suite cadence + full env-var block
**Traces to:** DEC-010
**Description:** Add a "Live e2e suite (pre-release only)" subsection to `CONTRIBUTING.md` listing all 5 paid runs and the full env-var block to invoke them. Stress the "pre-release cadence, not per-PR" intent.
**Files:**
- `CONTRIBUTING.md` — ~15 lines added.
**Done when:** `mkdocs build` (non-strict) clean; the new env-var block matches the actual gates in US-005/US-006/US-007.
**Depends on:** US-005, US-006, US-007 (ensure the documented invocation matches the actually-shipped marker set)

### US-009 — Quality Gate (code review × 4 + CodeRabbit + canonical `uv run` quad)
**Traces to:** (all)
**Description:** Run the project's code-review skill 4 times across the full diff, fixing real bugs each pass. Run CodeRabbit if available. Final pass: `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` must be all-green.
**Files:** (varies; whatever the reviewers find)
**Done when:** All reviewer passes report no real bugs; canonical validation green.
**Depends on:** US-001 through US-008

### US-010 — Patterns & Memory (priority 99)
**Traces to:** (all)
**Description:** Update `.claude/rules/` and memory with new patterns learned in this ticket. Specifically:
- `.claude/rules/llm-drafter.md` § "Provider-neutral seam" — document the new `is_clean_completion` / `unclean_finish_reason_message` ABC methods and the per-provider `_CLEAN_STOP_REASONS` convention. Mention this is the post-#155 generalisation of the original #137 DEC-005 contract.
- `.claude/rules/testing-signal.md` § "End-to-end gated tests" — add subsection noting that `apply_provider_override` is the canonical helper for per-test provider overlays.
- Memory: file `fake-driven-tests-miss-finish-reason-drift.md` — recap the #155 lesson that fake-driven byte-identity tests pin rendered output but not call-shape / response-shape semantics; live tests catch this class of bug.
**Files:**
- `.claude/rules/llm-drafter.md`
- `.claude/rules/testing-signal.md`
- `~/.claude/projects/-home-wesd-Projects-SignalForge/memory/fake-driven-tests-miss-finish-reason-drift.md` + `MEMORY.md` pointer.
**Done when:** Memory file present + linked from MEMORY.md; rule files updated; canonical validation green.
**Depends on:** US-009

## Beads Manifest

*(populated after `/super-plan` devolve step)*

| Bead ID | Story | Status |
|---|---|---|
| TBD | Epic | — |
| TBD | US-001 | — |
| TBD | US-002 | — |
| TBD | US-003 | — |
| TBD | US-004 | — |
| TBD | US-005 | — |
| TBD | US-006 | — |
| TBD | US-007 | — |
| TBD | US-008 | — |
| TBD | US-009 | — |
| TBD | US-010 | — |

## References

- `.claude/rules/llm-drafter.md` § "Gemini provider shape (#137)" DEC-005 — the contract being violated and clarified.
- `.claude/rules/grade-layer.md` § "Conservative score-and-degrade taxonomy (DEC-002, DEC-015)" — confirms `LLMResponseFormatError` → `GradeLLMError` degrade path.
- `.claude/rules/testing-signal.md` § "End-to-end gated tests (issue #10)" — belt-and-suspenders gating pattern.
- `.claude/rules/cli-layer.md` § "Multi-surface parity for behaviour changes" — 5-surface checklist.
- `plans/super/137-gemini-grading.md` — the original Gemini provider plan (DEC-005 source).
- `plans/super/10-e2e-bigquery-smoke.md` — the e2e template plan.
- `plans/super/135-provider-neutral-llm-seam.md` — the `LLMProvider` ABC origin.
