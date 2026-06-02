# Super Plan — #187: Faster default grader model (Haiku + per-provider fast defaults)

## Meta

- **Ticket:** [#187](https://github.com/wjduenow/SignalForge/issues/187) — *grade: switch default grader to Haiku-4-5 (Sonnet's reasoning depth is wasted on rubric scoring)*
- **Plus user addendum:** also set faster model defaults for OpenAI and Gemini providers.
- **Base branch:** `dev` (0.6.0.dev0). PRs target `dev`.
- **Worktree:** `../worktrees/SignalForge/187-fast-grade-defaults`
- **Branch:** `feature/187-fast-grade-defaults`
- **Phase:** devolved
- **PR:** [#193](https://github.com/wjduenow/SignalForge/pull/193) (base `dev`)
- **Sessions:** 1 (2026-06-02)

### Beads Manifest

- **Epic:** `SignalForge-dpy` — #187: Faster grade defaults — Haiku + per-provider fast models
- **Worktree (planning):** `../worktrees/SignalForge/187-fast-grade-defaults` (`feature/187-fast-grade-defaults`)

| Bead | Story | Depends on | Ready at devolve |
|---|---|---|---|
| `SignalForge-dpy.1` | US-001 — PROVIDER_FAST_MODELS + PROVIDER_SKU_PREFIXES constants | — | ✅ ready |
| `SignalForge-dpy.2` | US-002 — GradeConfig sentinel resolver + compat validator + token-cap bump | .1 | blocked |
| `SignalForge-dpy.3` | US-003 — Fixture + downstream test lockstep | .2 | blocked |
| `SignalForge-dpy.4` | US-004 — DraftConfig.cheap_model SKU alignment | — | ✅ ready |
| `SignalForge-dpy.5` | US-005 — Haiku calibration harness + writeup (gated) | .2 | blocked |
| `SignalForge-dpy.6` | US-006 — Docs + CHANGELOG + rule lockstep | .2 | blocked |
| `SignalForge-dpy.7` | Quality Gate — code review x4 + CodeRabbit | .3, .4, .5, .6 | blocked |
| `SignalForge-dpy.8` | Patterns & Memory | .7 | blocked |

*Note: `bd` auto-push to `origin/main` warns "no common ancestor" — the dolt remote tracks `main` but the active line is `dev`. Local beads DB is committed and intact; this is the pre-existing environment quirk, not a devolve failure.*

---

## What / Why

**What.** The grade layer is an LLM-as-judge: read an artifact (test SQL / column description / model rationale), score against ~4 rubric criterion texts, emit JSON `{score, evidence, reasoning}`. That is a small, well-bounded classification task — the Haiku-4-5 sweet spot. The default grader is currently `claude-sonnet-4-6`, whose reasoning depth is wasted here at ~3× the latency and ~3–5× the cost.

This ticket flips the **grade-stage default** to a faster/cheaper model, and — per the user's addendum — makes the **OpenAI and Gemini** grade providers resolve to a fast default model too (today, selecting a non-Anthropic provider forces the operator to also hand-pick a model, or the Anthropic default leaks across providers).

**Why.** Combined with #186 (grade parallelism), per-model grade phase drops from ~280s → ~10–15s. Cost per grade run drops materially. This is the cheap perf+cost win; it directly serves adoption (Architectural Commitment #4, OSS-first / "try it cheaply").

**Drafter stays on Sonnet** — the drafter's reasoning depth is load-bearing for SQL/description authoring. This ticket touches the grade stage only.

---

## Discovery

### Codebase findings (origin/dev, 0.6.0.dev0)

**Provider seam (multi-provider, post #135/#136/#137).**
- `src/signalforge/llm/providers.py` — process registry `provider_for(name) -> LLMProvider`; three registered: `anthropic`, `openai`, `gemini`. No model→provider auto-detection in this module.
- `src/signalforge/llm/cost/_rollup.py:75-92` — SKU-prefix dispatch for cost attribution: `claude-`→anthropic, `gpt-`→openai, `gemini-`→gemini.
- Provider capability flags (`supports_prompt_caching`, `supports_token_count`) gate cache markers + pre-send token counting. Anthropic both `True`; OpenAI/Gemini both `False`.

**Grade config — the landing surface.**
- `src/signalforge/grade/config.py:97` — `model: str = "claude-sonnet-4-6"` (single field).
- `:127` — `provider: str = "anthropic"`, validated against the registry (`provider_for`).
- `model` field validator only checks non-empty/non-whitespace; **no model↔provider compatibility check.**
- `grade/engine.py:364,371` — `call_llm_async(..., model=config.model, provider=config.provider, ...)`.
- **Consequence:** setting `provider: openai` while leaving `model` at its default sends `claude-sonnet-4-6` to OpenAI → runtime API error. Live tests (`test_smoke_real_api_openai.py:163`, `test_gemini_grade_live.py:141`) sidestep this by setting **both** `provider` and `model` explicitly (`gpt-4o`, `gemini-2.5-flash`).

**Draft config `cheap_model` placeholder.**
- `src/signalforge/draft/config.py:110` — `cheap_model: str = "claude-haiku-4-5-20251001"` — declared, documented "informational; not selected automatically," **never consumed** in `src/`.

**Pricing table — exact-match lookup.**
- `src/signalforge/llm/pricing.py` — `lookup(model)` is **exact key match**; unknown id → `EstimateUnknownModelError` (CLI tier 2).
- Anthropic keys: `claude-sonnet-4-6`, `claude-opus-4-7`, **`claude-haiku-4-5`** (note: bare SKU, NOT the dated `-20251001`).
- OpenAI keys: `gpt-4o`, **`gpt-4o-mini`**, `gpt-4.1`, `gpt-4-turbo`.
- Gemini keys: `gemini-2.5-pro`, `gemini-2.5-flash`, **`gemini-2.0-flash`**.
- `PRICE_TABLE_VERSION = "2026-05-28"`.
- **Risk:** the unused draft placeholder uses the dated `claude-haiku-4-5-20251001`, which is NOT a pricing key. Adopting that exact string as the grade default would break `--estimate`/cost-rollup. The bare SKU `claude-haiku-4-5` matches pricing and the `sonnet-4-6`/`opus-4-7` convention.

**Fastest/cheapest known SKU per provider (from pricing):**
| Provider | Cheapest known SKU | input $/MTok | output $/MTok |
|---|---|---|---|
| Anthropic | `claude-haiku-4-5` | 0.80 | 4.00 |
| OpenAI | `gpt-4o-mini` | 0.15 | 0.60 |
| Gemini | `gemini-2.0-flash` | 0.10 | 0.40 |
| Gemini (current live-test default) | `gemini-2.5-flash` | 0.30 | 2.50 |

**No CLI model flag.** `cli/generate.py` has no `--model`/`--grade-model`; model selection is config-only (`signalforge.yml grade:`).

**`_PROMPT_VERSION` is model-agnostic.** `grade/prompts.py:prompt_version_template` hashes `_SYSTEM_PROMPT + rubric block + envelope tags` — model id is **not** in the hash. Flipping the default model does **not** rotate the snapshot (`b1e609fae240ac1c`); `test_prompts.py:288` and `test_prompt_cache_stability.py:57` stay green.

**Tests impacted by a default-literal flip (model-agnostic unit suite via fakes):**
- `tests/fixtures/grade/grade_event_v1.jsonl` — carries `"model":"claude-sonnet-4-6"` (drift-detector fixture; data update, no assertion break).
- `tests/grade/test_models.py:91,139` — `_make_event()` helper default (fixture value, not assertion).
- `tests/grade/test_smoke_real_api.py` (gated `@pytest.mark.anthropic`) — uses the **default** config, so it would start calling the new default model. Shape-only assertions; no score pinned.
- `test_provider_neutrality*.py`, `test_gemini_neutrality.py` — provider-neutral; unaffected.
- `tests/research/` — **does not exist yet**; the issue asks to pin a calibration sample there per the #179 precedent.

### Rule constraints that bind this work

- **grade-layer.md** — DEC-023..027 locked defaults list (includes `model="claude-sonnet-4-6"`); must update docstring + locked list. `grade:` namespace, `extra="forbid"` inner / `extra="ignore"` outer. Drift detectors mandatory for `GradeEvent`/`GradingReport`. `_PROMPT_VERSION` rotation policy (text-only; stays stable here).
- **llm-drafter.md** — provider capability flags; `cheap_model` precedent (draft side); `llm:` namespace; per-provider byte-identity on `estimate_input_tokens`; non-clean finish_reason → `LLMResponseFormatError`.
- **testing-signal.md** — drift mirror + fixture lockstep; `apply_provider_override` per-test overlay (don't globally bump `max_output_tokens`); gated markers (`anthropic`/`openai`/`gemini`/`e2e`) + runtime skip; engineered determinism for LLM-driven assertions; 12 AST audit-completeness scans must still pass.
- **cli-layer.md** — four-tier exit codes; 7th AST scan over `errors.py` (new typed errors must map). Pricing `EstimateUnknownModelError` is tier 2.
- **No `workflow-project.md`** present.

### Scoping answers (session 1)

- **SCOPE-1 — Per-provider fast default = provider-keyed table + sentinel.** Add a `PROVIDER_FAST_MODELS` mapping `{anthropic: claude-haiku-4-5, openai: gpt-4o-mini, gemini: gemini-2.5-flash}`. Change `GradeConfig.model` default from `"claude-sonnet-4-6"` to a sentinel (`None`); resolve to the provider's fast model when unset. Setting just `provider: openai` Just Works. Explicit `model:` still honoured.
- **SCOPE-2 — Calibration ships as a gated story; maintainer runs the eval.** Add a re-grade concordance harness + `tests/research/187-*.md` writeup, gated behind the `anthropic` live-API marker. Decision rule: ≥85% pass/fail agreement vs the #179 Sonnet baseline → Haiku ships as resolved default. The maintainer runs it as a human pre-merge gate; code lands with Haiku as the resolved Anthropic default.
- **SCOPE-3 — Model ids:** `claude-haiku-4-5` / `gpt-4o-mini` / `gemini-2.5-flash`. All three are exact pricing keys.
- **SCOPE-4 — Reconcile `DraftConfig.cheap_model`** `claude-haiku-4-5-20251001` → `claude-haiku-4-5` (bare SKU, matches pricing + new grade default). Field stays unused; lockstep consistency only.

---

## Architecture Review (session 1)

| Area | Rating | Finding |
|---|---|---|
| Resolution timing | **concern → resolved** | None must resolve **at config-load**, not per-call, so `.model` is concrete everywhere downstream (estimate, engine, `GradeEvent.model`, cost-rollup). A `None` leaking past config-load crashes the cost rollup's prefix dispatch (`_rollup.py:75-92` → `CostRollupUnknownModelError`) and the estimate path's exact-match `pricing.lookup`. **Decision:** resolve in `GradeConfig`. |
| Frozen-model mechanism | **concern → resolved** | `GradeConfig` is `frozen=True` (`extra="forbid"`). A `@model_validator(mode="after")` doing `self.model = ...` raises `FrozenInstanceError`. **Decision (DEC-003):** resolve in a `@model_validator(mode="before")` that injects the provider's fast model into the raw dict when `model` is absent/None, so the field is concrete after field-validation and frozen-ness is preserved. Unknown provider → no injection → existing `provider` field-validator raises the proper `UnknownProviderError`. |
| Gemini truncation | **BLOCKER (needs user call)** | `GradeConfig.max_output_tokens` default is `256` (`grade/config.py:109`). Per `plans/super/155-gemini-truncation-e2e-gap.md`, `gemini-2.5-flash`'s verbose `reasoning` field routinely exceeds small caps and hits `MAX_TOKENS` → non-clean finish → `LLMResponseFormatError` → degraded grade (`score=None`). #155 deliberately kept the production `max_output_tokens=256` and used **per-test overlays** for Gemini. But #187 makes `gemini-2.5-flash` a *one-line production default* for `provider: gemini`, so 256 becomes a first-run footgun for Gemini operators. See refinement Q. |
| Model↔provider compat | **concern (improvement)** | No validator today ensures `model` matches `provider` (`provider: openai, model: claude-sonnet-4-6` parses, fails at runtime). The sentinel change makes a cheap fix natural: when `model` is explicitly set, check its SKU prefix matches `provider` (reusing the single prefix-dispatch source in `cost/_rollup.py`, not a duplicate). Fails loud at config-load. **Decision (DEC-006):** add it. |
| estimate byte-identity | **concern** | `--estimate` embeds `grader_model=grade_config.model` (resolved) in its report; any estimate snapshot test that pins the grade model id flips sonnet→haiku. Must update those snapshots in lockstep. Per-provider `estimate_input_tokens` byte-identity is unaffected (model id isn't in the token-count payload). |
| `_PROMPT_VERSION` snapshot | **pass** | Model id is NOT in the grade prompt-version hash (`prompts.py` hashes system prompt + rubric block + envelope only). `b1e609fae240ac1c` stays pinned; `test_prompts.py` / `test_prompt_cache_stability.py` green. |
| Unit suite (fakes) | **pass** | Grade unit tests inject `FakeAnthropicClient` / override `model="claude-fake"`; model-agnostic. Only `test_config.py` default assertions (`:190`, `:480`, `:508`), `test_models.py` `_make_event()` helper, and `grade_event_v1.jsonl` carry literal model ids → fixture/assertion updates, no logic break. |
| AST audit scans | **pass** | A module-level `PROVIDER_FAST_MODELS` constant trips no scan (scans gate event construction / SDK clients / errors.py — not data constants). No new typed errors → 7th scan unaffected. |
| Drift detectors | **concern** | `StrictGradeEvent` mirror + `grade_event_v1.jsonl` fixture carry `model`; update fixture to the resolved value, keep strict mirror in lockstep (testing-signal.md DEC-001). `GradeConfig` has no read-back fixture (it's write-only input). |
| Calibration | **pass (gated story)** | `tests/research/` does not exist; `docs/research/179-test-primitive-expansion-retest.md` is the writeup-format precedent. No reusable concordance harness exists — build a one-off per the #179 pattern, gated behind `@pytest.mark.anthropic`. |
| Constant home | **pass** | `PROVIDER_FAST_MODELS` lives in `llm/providers.py` (next to the registry; add to `__all__`), reusable by a future draft `--cheap`. Grade config imports it. |
| Draft per-provider footgun | **accepted / out of scope** | `DraftConfig` has the same latent `provider≠model` footgun, but the issue scopes the drafter to stay on Sonnet. Note as a v0.3 follow-up; this ticket only aligns `cheap_model`'s SKU (SCOPE-4). |

No blockers remain after the refinement decisions below.

---

## Refinement Log — Decisions

- **DEC-001 — Per-provider fast grade default via sentinel.** `GradeConfig.model` changes from `str = "claude-sonnet-4-6"` to `str | None = None`. When unset, it resolves to the calling provider's fast model. The resolved Anthropic default becomes `claude-haiku-4-5` (the ticket's headline change). Explicit `model:` is always honoured. *Traces: SCOPE-1, issue #187.*
- **DEC-002 — `PROVIDER_FAST_MODELS` table in `llm/providers.py`.** `{"anthropic": "claude-haiku-4-5", "openai": "gpt-4o-mini", "gemini": "gemini-2.5-flash"}`, added to `__all__`. Every value is an exact `pricing.PRICES` key (so `--estimate`/cost-rollup never raise `EstimateUnknownModelError`). Home chosen for reuse by a future draft `--cheap` and proximity to the provider registry. *Traces: SCOPE-1, SCOPE-3.*
- **DEC-003 — Resolve in a frozen-safe `@model_validator(mode="before")`.** `GradeConfig` is `frozen=True`; a `mode="after"` `self.model = …` raises. The before-validator injects `PROVIDER_FAST_MODELS[provider]` into the raw dict when `model` is absent/None, so `.model` is concrete after field-validation. Resolution at **config-load**, never per-call — a `None` reaching the engine / `GradeEvent.model` / cost-rollup is the failure mode this prevents. Unknown provider → no injection → existing `provider` field-validator raises `UnknownProviderError`. *Traces: architecture review "resolution timing" + "frozen-model mechanism".*
- **DEC-004 — Raise `GradeConfig.max_output_tokens` default 256 → 1024 (all providers).** Prevents `gemini-2.5-flash` truncation (`MAX_TOKENS` → `LLMResponseFormatError` → degraded grade) out of the box now that Gemini is a one-line production default. It is a cap, not a target — Haiku / gpt-4o-mini rarely approach it, so the Anthropic/OpenAI cost ceiling barely moves. Supersedes #155's "keep 256 production default" *for the production default only*; #155's per-test overlays remain valid for tighter test scoping. **Verification owed:** the live calibration story must confirm `gemini-2.5-flash` does not still truncate at 1024 on real artifacts; if it does, revisit (bump further or per-provider floor). *Traces: refinement Q, #155 DEC-008/DEC-009.*
- **DEC-005 — Calibration is a maintainer-run gated story.** Re-grade a pinned sample from the #179 Phase-B `grade.jsonl` (Sonnet baseline) with the new Haiku default; compute per-criterion pass/fail concordance. Decision rule: **≥85% agreement → Haiku ships as the resolved Anthropic default** (this PR); <85% → fall back to the issue's "opt-in knob" option in a follow-up. Harness + pinned sample live under `tests/research/187-haiku-calibration/`, gated by `@pytest.mark.anthropic` + runtime env skip; prose writeup at `docs/research/187-haiku-calibration.md` mirroring `docs/research/179-test-primitive-expansion-retest.md`. The eval is a human pre-merge gate (CI can't run live API); the PR records the result. *Traces: SCOPE-2, issue #187 "Empirical calibration step (required before merge)".*
- **DEC-006 — Add a model↔provider compatibility validator.** When `model` is explicitly set, reject a SKU-prefix/provider mismatch (`provider: openai, model: claude-…`) at config-load with `GradeConfigError`, instead of failing at runtime. Single source of truth: define `PROVIDER_SKU_PREFIXES = {"anthropic": "claude-", "openai": "gpt-", "gemini": "gemini-"}` in `llm/providers.py` and refactor `cost/_rollup.py` to import it (removes the existing duplicate prefix map). No new error class → 7th AST scan unaffected. *Traces: architecture review "model↔provider compat".*
- **DEC-007 — Reconcile `DraftConfig.cheap_model` SKU.** `claude-haiku-4-5-20251001` → `claude-haiku-4-5` (bare SKU; matches pricing + the new grade default + the `sonnet-4-6`/`opus-4-7` convention). Field remains unused; lockstep consistency only. *Traces: SCOPE-4.*
- **DEC-008 — Drafter stays on Sonnet; draft per-provider resolution is out of scope.** The analogous `DraftConfig` `provider≠model` footgun is noted as a v0.3 follow-up. *Traces: issue #187 ("The drafter stays on Sonnet").*
- **DEC-009 — 5-surface lockstep for the default-flip graduation.** The default change updates: (1) `.claude/rules/grade-layer.md` (DEC-026 + locked-defaults), (2) `docs/grade-ops.md` (default model, `max_output_tokens`, cost table) + `docs/llm-providers-ops.md` (per-provider fast SKU + the Gemini token note) + `docs/draft-ops.md` (cheap_model SKU), (3) `CLAUDE.md` public-API surface + the `GradeConfig` docstring, (4) the grade config/fixture tests, (5) this plan's DEC list. CHANGELOG entry under "Changed"; README "blessed IDs" only if it enumerates the grade default. *Traces: grade-layer.md/prune-engine.md 5-surface parity rule, cli-layer.md multi-surface parity.*

---

## Detailed Breakdown — Stories

> Validation command (all stories' final gate): `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`

### US-001 — `PROVIDER_FAST_MODELS` + `PROVIDER_SKU_PREFIXES` constants
**Description.** Add the two provider→string mapping constants to `src/signalforge/llm/providers.py` and export them in `__all__`. Refactor `src/signalforge/llm/cost/_rollup.py` to import `PROVIDER_SKU_PREFIXES` so the SKU-prefix dispatch has a single source of truth.
**Traces to:** DEC-002, DEC-006.
**TDD (write first):**
- `PROVIDER_FAST_MODELS` covers exactly the three registered providers (`anthropic`/`openai`/`gemini`).
- Every `PROVIDER_FAST_MODELS` value is a key in `signalforge.llm.pricing.PRICES` (guards the `--estimate` contract).
- `PROVIDER_SKU_PREFIXES` keys == `PROVIDER_FAST_MODELS` keys; each fast model id `startswith` its provider's prefix.
- `_rollup.py` prefix dispatch still classifies `claude-…`/`gpt-…`/`gemini-…` correctly after the refactor (existing rollup tests stay green).
**Files:** `src/signalforge/llm/providers.py` (constants + `__all__`), `src/signalforge/llm/cost/_rollup.py` (import the prefix map, drop the local copy), `tests/llm/test_providers.py` (or `test_pricing.py`) for the new invariants.
**Depends on:** none.
**Done When:**
- [ ] Both constants defined + in `__all__`; values are exact pricing keys.
- [ ] `_rollup.py` imports `PROVIDER_SKU_PREFIXES`; no duplicate prefix literals remain.
- [ ] New invariant tests pass; existing cost-rollup tests green.
- [ ] `make verify` / canonical validation passes.

### US-002 — GradeConfig sentinel resolver + compat validator + token-cap bump
**Description.** Migrate `GradeConfig.model` to `str | None = None` with a `mode="before"` resolver (DEC-003), add the explicit-model↔provider compatibility check (DEC-006), and raise the `max_output_tokens` default 256→1024 (DEC-004). Update the class docstring + the DEC-023..027 locked-defaults list.
**Traces to:** DEC-001, DEC-003, DEC-004, DEC-006.
**TDD (write first):**
- `GradeConfig().model == "claude-haiku-4-5"` (resolved Anthropic default).
- `GradeConfig(provider="openai").model == "gpt-4o-mini"`; `GradeConfig(provider="gemini").model == "gemini-2.5-flash"`.
- `GradeConfig(model="claude-sonnet-4-6").model == "claude-sonnet-4-6"` (explicit honoured).
- `GradeConfig(provider="openai", model="claude-sonnet-4-6")` → `ValidationError` (compat reject); same-provider explicit (`provider="openai", model="gpt-4o"`) passes.
- `GradeConfig(model="   ")` still rejected (empty-string guard runs before/independent of resolution).
- `GradeConfig().max_output_tokens == 1024`.
- `load_grade_config` with a `grade:` block omitting `model:` → resolves; with `model:` set → honoured; with mismatched provider/model → `GradeConfigError`.
- Unknown provider still raises `UnknownProviderError` (resolution doesn't mask it).
**Files:** `src/signalforge/grade/config.py`, `tests/grade/test_config.py`.
**Depends on:** US-001.
**Done When:**
- [ ] `model: str | None = None`; `mode="before"` resolver injects fast model; frozen-ness preserved (no `FrozenInstanceError`).
- [ ] Compat validator rejects explicit prefix/provider mismatch at load.
- [ ] `max_output_tokens` default == 1024; docstring + locked-defaults list updated.
- [ ] All TDD cases pass; `make verify` passes.

### US-003 — Fixture + downstream test lockstep
**Description.** Update committed fixtures and assertions that carry the old default model id, and any `--estimate` snapshot that pins the grade model id, so the suite reflects the resolved Haiku default. Keep the drift detector green.
**Traces to:** DEC-001, DEC-009; testing-signal.md DEC-001 (drift lockstep).
**TDD / checks:**
- `tests/grade/test_config.py` default assertions updated (`:190` resolved-value; review `:480`, `:508`).
- `tests/fixtures/grade/grade_event_v1.jsonl` `model` value + `StrictGradeEvent` mirror remain consistent (drift detector passes).
- `tests/grade/test_models.py` `_make_event()` helper reviewed (explicit fixture value — update only if a test asserts the default).
- Grep for estimate snapshots embedding the grade model id (`tests/cli/` estimate tests, `_estimate` report); update any in lockstep.
**Files:** `tests/grade/test_config.py`, `tests/fixtures/grade/grade_event_v1.jsonl`, `tests/grade/test_models.py`, plus any estimate snapshot fixture surfaced by grep.
**Depends on:** US-002.
**Done When:**
- [ ] Drift detector + grade unit suite green against the new default.
- [ ] No stale `claude-sonnet-4-6` default assertion remains where the resolved default now applies.
- [ ] `make verify` passes.

### US-004 — `DraftConfig.cheap_model` SKU alignment
**Description.** Change the unused `DraftConfig.cheap_model` default from the dated `claude-haiku-4-5-20251001` to the bare SKU `claude-haiku-4-5`; update its docstring + the DEC-017 reference.
**Traces to:** DEC-007.
**Files:** `src/signalforge/draft/config.py`, `tests/draft/test_config.py` (if it asserts the value).
**Depends on:** none (independent; can land any time before QG).
**Done When:**
- [ ] `cheap_model == "claude-haiku-4-5"`; docstring updated.
- [ ] Draft config tests green; `make verify` passes.

### US-005 — Haiku calibration harness + writeup (gated)
**Description.** Build the one-off concordance harness under `tests/research/187-haiku-calibration/`: re-grade a pinned sample of artifacts with the new Haiku default vs the #179 Sonnet baseline, compute per-criterion pass/fail agreement, and assert the ≥85% decision rule (or report it). Gate behind `@pytest.mark.anthropic` + a runtime env skip. Add the prose writeup at `docs/research/187-haiku-calibration.md` mirroring the #179 retest format. Also verify (live) that the new `max_output_tokens=1024` default keeps `gemini-2.5-flash` grading clean (no `score=None` degrade from truncation).
**Traces to:** DEC-004 (gemini verification), DEC-005.
**TDD / determinism:** per testing-signal.md "engineered determinism" — pin the sample artifacts + Sonnet baseline as committed data so the comparison is reproducible; the only live variable is the Haiku re-grade.
**Files:** `tests/research/187-haiku-calibration/` (gated test + pinned sample json), `docs/research/187-haiku-calibration.md`, `pyproject.toml` (only if a new marker is needed — reuse `anthropic`/`gemini`).
**Depends on:** US-002.
**Done When:**
- [ ] Gated harness runs under `pytest -m anthropic --no-cov` and emits the concordance metric; deselected by default CI.
- [ ] Writeup documents method, sample, decision rule, and (maintainer-filled) result.
- [ ] Gemini-at-1024 no-truncation check present (gated `@pytest.mark.gemini`).
- [ ] Default `pytest` (non-gated) + `make verify` pass.

### US-006 — Docs + CHANGELOG + rule lockstep (5-surface parity)
**Description.** Update every non-code surface for the default flip: `docs/grade-ops.md` (default model, `max_output_tokens=1024`, cost table with Haiku + per-provider rows), `docs/llm-providers-ops.md` (per-provider fast SKU + the Gemini token-cap note), `docs/draft-ops.md` (cheap_model SKU), `.claude/rules/grade-layer.md` (DEC-026 + locked defaults), `.claude/rules/llm-drafter.md` (`PROVIDER_FAST_MODELS` note), `CLAUDE.md` public-API surface, and a `CHANGELOG` "Changed" entry. README "blessed IDs" only if it enumerates the grade default.
**Traces to:** DEC-009.
**Files:** the docs/rules/CLAUDE.md/CHANGELOG files above.
**Depends on:** US-002 (docs reflect final shape).
**Done When:**
- [ ] All five parity surfaces agree on the new default + `max_output_tokens`.
- [ ] CHANGELOG entry added; cost table updated.
- [ ] `make verify` passes (doc-example round-trip tests, if any, green).

### US-007 — Quality Gate
**Description.** Run the code reviewer 4× across the full changeset, fixing every real bug each pass; run CodeRabbit if available. Canonical validation must pass after all fixes.
**Traces to:** all DECs.
**Depends on:** US-001..US-006.
**Done When:**
- [ ] 4 review passes complete; all real findings fixed.
- [ ] CodeRabbit (if available) addressed.
- [ ] `ruff check . && ruff format --check . && pyright && pytest` all pass; gated markers (`anthropic`/`gemini`) spot-run by maintainer.

### US-008 — Patterns & Memory
**Description.** Capture new patterns: the `PROVIDER_FAST_MODELS` sentinel-resolution pattern, the frozen-model `mode="before"` resolver convention, and the per-provider-default graduation. Update `.claude/rules/` / `docs/` / memory as warranted.
**Traces to:** DEC-001..DEC-009.
**Depends on:** US-007.
**Done When:**
- [ ] Rules/docs/memory updated with the sentinel-resolution + frozen-resolver patterns.
- [ ] `make verify` passes.

---

## Story dependency graph

```
US-001 ─┬─> US-002 ─┬─> US-003 ─┐
        │           ├─> US-005 ─┤
        │           └─> US-006 ─┤
US-004 ─────────────────────────┼─> US-007 (Quality Gate) ─> US-008 (Patterns & Memory)
                                 ┘
```

