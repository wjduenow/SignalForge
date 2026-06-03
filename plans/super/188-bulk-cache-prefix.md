# #188 — draft: bulk-mode shared cached prefix for `--select`

## Meta

- **Ticket:** https://github.com/wjduenow/SignalForge/issues/188
- **Phase:** devolved
- **PR:** https://github.com/wjduenow/SignalForge/pull/195 (draft, base `dev`)
- **Branch / worktree:** `feature/188-bulk-cache-prefix` at `../worktrees/SignalForge/188-bulk-cache-prefix` (cut from `origin/dev`, 0.6.0.dev0)
- **Labels:** enhancement, perf, draft
- **Sessions:** 1 (2026-06-03)

### Beads manifest

- **Epic:** `SignalForge-fzo`
- **Tasks:** `SignalForge-fzo.1` … `.10` = US-001 … US-010 (story number == suffix).
- **Ready at devolve:** `SignalForge-fzo.1` (US-001), `SignalForge-fzo.2` (US-002).
- **Dependency edges:** .3→.2; .4→.1,.3; .5→.1,.4; .6→.3; .7→.5; .8→.5; .9(QG)→.1….8; .10(P&M)→.9.
- _Note: `bd` Dolt remote auto-push reports "no common ancestor" in this environment; the graph is persisted locally. Run `bd dolt push` once the remote is reconciled._

## Ticket summary

The drafter's Anthropic prompt-cache hit rate across a multi-model `--select` batch is **0%** — empirically measured in the #179 Phase B retest (`docs/research/179-test-primitive-expansion-retest.md`, PR #182): every drafter call paid `cache_creation_input_tokens` from scratch (1,612–2,572), `cache_read_input_tokens = 0` on all 14 runs.

Root cause is by-design: `llm-drafter.md` § "Cached-block scope" (DEC-009) marks **only the model under draft + its direct refs/depends_on neighbours** as the cached block. That block differs per model, so nothing is byte-identical across a batch → no cache_read.

**Proposed change:** for `signalforge generate --select <expr>` runs matching ≥ 2 models, restructure the cached block to a **project-level shared prefix** that is byte-identical across every model in the batch. Pay `cache_creation` once on model 1; `cache_read` (≈12× cheaper input-side) on models 2..N. Gate behind a new `DraftConfig.cache_scope: Literal["per-model", "project"] = "per-model"` field; auto-promote to `"project"` when `--select` matched ≥ 2 models. Single-model positional runs keep per-model scope (preserves the existing cache-stability snapshot byte-for-byte).

## AC (derived from ticket)

- [ ] `DraftConfig.cache_scope: Literal["per-model", "project"] = "per-model"` field exists (`extra="forbid"`, `llm:` namespace).
- [ ] `--select` batch matching ≥ 2 models auto-promotes the per-model `DraftConfig` overlay to `cache_scope="project"`.
- [ ] Project-scope cached block is byte-identical across every model in the batch (the load-bearing cache-hit precondition).
- [ ] Project-scope cached block respects the 8000-token cap (DEC-024); oversize handled per DEC (below).
- [ ] `_PROMPT_VERSION` rotates for the project-scope template; both per-model and project goldens pinned in `tests/llm/test_prompt_cache_stability.py`.
- [ ] `<MODEL_SQL>` envelope-breach guard (DEC-007) unchanged — envelope stays in the dynamic block.
- [ ] Per-model draft quality preserved — the dynamic block names the model under draft + carries its full column/neighbour detail.
- [ ] Single-model positional run output + cache behaviour unchanged.
- [ ] `docs/draft-ops.md` + `docs/cli-ops.md` updated (operator-facing cache-scope semantics + cost model).
- [ ] Canonical validation passes: `ruff check . && ruff format --check . && pyright && pytest`.

## Discovery

### How the drafter prompt is built today (dev, 0.6.0.dev0)

`signalforge.draft.prompts.render_prompt(model, request, manifest, exclude_tests) -> (system, cached_block, dynamic_block, prompt_version)`:

- **`system`** — `_render_system_prompt(exclude_tests)` (test catalogue + scope instructions). Identical across models in a batch (same `exclude_tests`).
- **`cached_block`** — `_render_manifest_summary(model, manifest)` (`prompts.py:545-571`): the model under draft + its direct refs/`depends_on` neighbours, columns sorted lexicographically for byte-stability. **Per-model → differs across the batch → the 0%-hit root cause.**
- **`dynamic_block`** — `_render_dynamic_block(model, request, exclude_tests)` (`prompts.py:766-797`): `<MODEL_SQL>…</MODEL_SQL>` envelope + mode-specific data section + `<BUSINESS_RULE id="N">` envelopes. Per-model; never cached.
- **`prompt_version`** — `_prompt_version_for(exclude_tests)` (`prompts.py:462-481`): `_PROMPT_VERSION` verbatim when `exclude_tests` empty, else `blake2b-8(_PROMPT_VERSION + "|exclude=" + canonical_json)`. `_PROMPT_VERSION` itself is `blake2b-8` over `(_SYSTEM_PROMPT + _MANIFEST_SUMMARY_TEMPLATE + _DATA_SECTION_TEMPLATES_JSON)` (`prompts.py:449-459`).

### Anthropic cache mechanics (confirmed)

The cache marker (`cache_control: {type: "ephemeral", ttl}`) lands on **block_1 = cached_block** in the user message; **block_2 = dynamic_block** is always billed at full input rate (`providers.py:486-519`). Anthropic caches the prefix = `system` + block_1. For a cache_read hit on models 2..N, **`system` + block_1 must be byte-identical** across the calls. Block ordering in the ticket is correct: static/shared content first (cached), per-model dynamic content second. Pre-send `messages.count_tokens` counts `system + cached_block` only (`client.py:348-424`); 8000-token cap raises `LLMCacheTooLargeError` before any `create`; below 1024 (Sonnet/Opus) / 2048 (Haiku) the marker is dropped + INFO logged.

### CLI batch driver (issue #37)

- `cmd_generate` (`cli/generate.py:1288-1437`) dispatches on `getattr(args, "select", None) is not None` → `_run_batch` else `_run_single_model`.
- `_run_batch` (`cli/generate.py:1191`) calls `select_models(manifest, expr)` → `matched` tuple; `total = len(matched)` at line 1240 — **the match count is known before the per-model loop.** Fresh adapter per model (DEC-010).
- `_run_single_model` (`cli/generate.py:739`) loads `draft_config = load_draft_config(project_dir)` (line ~934) and passes it straight to `draft_schema(...)`. **No draft-config overlay today** — but prune/grade/diff all use the canonical overlay pattern: `Config.model_validate({**config.model_dump(), **overrides})` (re-runs validators; mirrors `SafetyPolicy.with_mode`).

### Constraint inventory (from `.claude/rules/`)

| Constraint | Source | Implication for #188 |
|---|---|---|
| Cached block byte-identical → cache hit | Anthropic mechanics | Project prefix MUST NOT depend on which model is under draft |
| 8000-token cap, pre-send `count_tokens` | `llm-drafter.md` DEC-024/009 | Project prefix must fit; needs compression + oversize policy |
| `_PROMPT_VERSION` rotates on cached-template shape change | `llm-drafter.md` DEC-009; `business-rule-tests.md` DEC-012 | New project-scope template ⇒ rotate; two goldens pinned in lockstep |
| Cache-stability snapshot pins rendered bytes | `testing-signal.md`; `test_prompt_cache_stability.py` | Add a second (project-scope) golden + version pin |
| Dual-zero anomaly WARNING only | `llm-drafter.md` DEC-014 | `cache_creation==0` alone (healthy hit) must NOT warn |
| `<MODEL_SQL>` / `<BUSINESS_RULE>` breach guard = boring substring | `business-rule-tests.md` DEC-009 (#163) | Stays in dynamic block; unchanged |
| Business rules render to DYNAMIC block, never cached | `business-rule-tests.md` DEC-001 | Per-model `meta.signalforge.business_rules` must stay dynamic |
| `DraftConfig` `extra="forbid"`, `_DraftConfigFile` `extra="ignore"`, `llm:` namespace | `llm-drafter.md` DEC-027 | Add `cache_scope` under `llm:` |
| ANSI-safe lazy-format logger; grep gate scans `cli,draft,llm,...` | `llm-drafter.md` DEC-011 | New INFO/WARNING lines: lazy-format JSON, no f-strings |
| 5-surface parity for behaviour changes | `cli-layer.md` DEC-017 | help/docstring/ops-doc/test/DEC in lockstep (config-only narrows surface 1) |
| Fresh adapter per model; sidecar last-writer-wins; cache caveat doc | `cli-layer.md` DEC-010/003/015 | Update DEC-015 "cache doesn't amortise" — now it DOES in project scope |
| AST construction-seam scans; new errors → exit-code map (scan 7) | `cli-layer.md` DEC-024 | Any new `*Error` registers tier; likely tier 2 |

### Net-new vs existing

- **`meta.signalforge.business_rules`** — EXISTS (issue #116/#163), rendered into the **dynamic** block, dict-guard read.
- **`_PROJECT_BUSINESS_RULES` (project-level cached business rules)** — DOES NOT exist; net-new if in scope.
- **`DraftConfig.cache_scope`** — net-new.
- **Project-level manifest-summary renderer** — net-new (`_render_project_summary` or similar).

## Scoping questions (answered 2026-06-03)

- **Q1 — Prefix scope:** **Whole project, compressed.** The cached prefix lists every manifest model as `name (N cols)` (no per-column detail); full column detail for the model-under-draft + its refs/`depends_on` neighbours moves into the per-model dynamic block. Fits large projects under 8000 tokens and cache-hits across separate `--select` invocations of the same project within TTL.
- **Q2 — Oversize policy:** **Auto-fallback to per-model.** If the compressed project prefix still exceeds 8000 tokens, that model degrades to per-model cache scope + one INFO line; the batch completes (exit 0). Conservative-degrade house pattern.
- **Q3 — Project business rules:** **Include now.** Add a project-level cached business-rules block (`_PROJECT_BUSINESS_RULES`) aggregating `meta.signalforge.business_rules` across the project. Per-model business rules continue to render in the dynamic block too (see refinement for the dedup/placement decision).
- **Q4 — Operator surface:** **Config + auto-promote + CLI flag.** `DraftConfig.cache_scope` (default `per-model`) in the `llm:` block; `--select` ≥ 2 models auto-promotes to `project`; plus a `--cache-scope {per-model,project}` flag mirroring `--mode`/`--scope`/`--format`. Full 5-surface parity.

## Architecture Review

Three parallel reviews (control-flow, business-rules+security, prompt-version+tests). Ratings and resolutions:

| Area | Rating | Finding | Resolution |
|---|---|---|---|
| **A. Oversize-fallback control flow** | **blocker** | The 8000-token gate lives inside `call_llm` (`client.py:348-424`); falling back to per-model needs a *different* rendered cached block, but `call_llm` can't render prompts (layering). | **Catch-and-retry in the drafter (DEC-006).** `call_llm` keeps raising `LLMCacheTooLargeError`; `draft_from_request` catches it and, *only when `cache_scope=="project"`*, re-renders per-model and retries once. No layering violation, zero happy-path cost. |
| **B. Project block byte-stability** | **blocker** | No deterministic ordering specified for project-wide aggregation; dict iteration / selector order could make the prefix differ across runs → silent 0% hit despite "working" code. | **Explicit total order (DEC-007).** Project summary iterates `sorted(manifest.nodes)` by `unique_id`; columns sorted by name; business rules collected model-then-column in that same order with a global 1-indexed counter. Pinned by a cross-model byte-identity test. |
| **C. Injection-surface widening** | **blocker** | Project prefix carries *every* model's description + column names (raw, unenveloped today). A poisoned description influences every model in the batch. | **`<PROJECT_MANIFEST>` envelope + scope-aware defence line (DEC-008).** Wrap the project summary in a fenced envelope with a boring-substring breach guard; the injection-defence line is added *only in the project-scope system prompt variant* so the per-model `_SYSTEM_PROMPT` stays byte-identical (preserves the AC). |
| **D. `_PROMPT_VERSION` composition** | concern | `cache_scope` is a second orthogonal dimension alongside `exclude_tests`. | **Two base constants + dispatch (DEC-009).** `_PROMPT_VERSION_PER_MODEL` (== today's `_PROMPT_VERSION`, unchanged) and `_PROMPT_VERSION_PROJECT`; `_prompt_version_for(exclude_tests, cache_scope)` selects the base then folds `scope=` + `exclude=` into the canonical hash. |
| **E. Per-model golden must not drift** | pass | New `_PROJECT_SUMMARY_TEMPLATE` + render fn are isolated; per-model hash inputs unchanged ⇒ existing golden stable — *as long as* the project-scope defence text is NOT added to the shared `_SYSTEM_PROMPT` (see C resolution). | Guarded by the existing `test_prompt_version_pinned_*`; add an assert that project version ≠ per-model version. |
| **F. Config-overlay plumbing** | concern | `_run_single_model` loads `DraftConfig` fresh with no overlay; prune/grade/diff all use `Config.model_validate({**dump, **overrides})`. | **Mirror the precedent (DEC-010).** Add a `draft_overrides: dict \| None` kwarg to `_run_single_model`; `_run_batch` computes `{"cache_scope":"project"}` when `total>=2` and the operator hasn't pinned a scope. |
| **G. Per-batch render efficiency** | concern | Project prefix is identical across the batch but rendered+counted once per model (N `count_tokens`). | **Accept (DEC-011).** Matches today's one-count-per-call cost profile exactly — not a regression. Batch-level memoisation deferred to a v0.x follow-up; documented, not silently capped. |
| **H. Sub-minimum on tiny projects** | concern | A tiny project's compressed prefix may fall below the 1024/2048 cache minimum → marker dropped, no benefit. | **Benign degradation (DEC-012).** `call_llm` already drops the marker + logs INFO; outcome is correct (just uncached). Auto-promote does NOT special-case batch size; the existing drop path covers it. |
| **I. Business-rules dedup/placement** | concern | If project rules are cached AND per-model rules stay in the dynamic block, the model-under-draft's own rules render twice; interacts with the parser cardinality gate. | **Refinement question (below).** |
| **J. Grade-side `_PROMPT_VERSION`** | pass | Drafter-only change; grade prompt untouched. | No grade golden rotation. Note in PR. |
| **K. `cache_scope` field drift detection** | pass | `DraftConfig` is `extra="forbid"`; typo fails loud. | Add the field + extend the `StrictDraftConfig`/fixture drift pair. |
| **L. ANSI-safe logger + new errors** | pass | New INFO (fallback) line must be lazy-format JSON; any new `*Error` registers in the exit-code map (scan 7). | Follow the gate; `<PROJECT_MANIFEST>` breach reuses `PromptEnvelopeBreachError` (already mapped). |

**Blockers A, B, C all have concrete resolutions (DEC-006/007/008) — cleared to proceed.** One concern (I) needs an operator decision in refinement.

## Refinement Log

### Decisions

- **DEC-001 — `DraftConfig.cache_scope` field.** `cache_scope: Literal["per-model", "project"] = "per-model"`, under the `llm:` namespace, `DraftConfig` stays `extra="forbid"` (typo fails loud), `_DraftConfigFile` stays `extra="ignore"`. Default preserves all current behaviour. _(Q1, Q4; llm-drafter.md DEC-027.)_
- **DEC-002 — Auto-promote on `--select` ≥ 2 models.** `_run_batch` overlays `cache_scope="project"` when `len(matched) >= 2` AND the operator has not explicitly pinned a scope (via `--cache-scope` or a non-default `llm.cache_scope` in YAML). Single-model positional runs never promote. _(Q4.)_
- **DEC-003 — `--cache-scope {per-model,project}` flag.** Mirrors `--mode`/`--scope`/`--format`. Precedence: explicit `--cache-scope` flag > YAML `llm.cache_scope` (if non-default) > auto-promote. Full 5-surface parity (help / docstring / docs / test / DEC). _(Q4; cli-layer.md DEC-017.)_
- **DEC-004 — Project prefix = whole-project compressed summary.** One line per manifest model: `name (N cols)`, no per-column detail. Full column detail for the model-under-draft + its refs/`depends_on` neighbours moves into the per-model dynamic block. Byte-identical across the batch. _(Q1; review B/C.)_
- **DEC-005 — `<PROJECT_MANIFEST>` envelope + scope-aware defence line.** The project summary is wrapped in `<PROJECT_MANIFEST>…</PROJECT_MANIFEST>`; the injection-defence instruction naming it is added **only in the project-scope system-prompt variant** so the per-model `_SYSTEM_PROMPT` (and its golden) stay byte-identical. _(Review C; preserves AC "single-model unchanged".)_
- **DEC-006 — Oversize fallback by catch-and-retry in the drafter.** `call_llm` keeps raising `LLMCacheTooLargeError` at the 8000-token gate. `draft_from_request` catches it and, **only when `cache_scope=="project"`**, re-renders the cached block per-model and retries `call_llm` once; emits one INFO (lazy-format JSON) naming the model + token count. A per-model-scope oversize re-raises (it's a real error). Run continues, exit 0. _(Q2; review A — resolves the layering blocker without `call_llm` rendering.)_
- **DEC-007 — Deterministic total order for the project block.** Iterate `sorted(manifest.nodes)` by `unique_id`; within a model, columns sorted by name; project business rules collected model-level-then-column-sorted in that same `unique_id` order, with a single global 1-indexed `<BUSINESS_RULE id="N">` counter. No reliance on dict-insertion or selector order. Pinned by a cross-model byte-identity test. _(Review B — the load-bearing cache-hit precondition.)_
- **DEC-008 — Breach guard fails closed (distinct from oversize).** `_render_project_summary` runs a boring-substring scan for `</PROJECT_MANIFEST>`; project business-rule aggregation scans for `</BUSINESS_RULE>` at aggregation time. A breach raises `PromptEnvelopeBreachError` (extended with a nullable `model_unique_id` / `rule_source="project"` so the message names the project aggregation). Breach does **not** fall back to per-model — unlike capacity (DEC-006), a breach is a security signal the operator must fix; it surfaces per model via the existing continue-on-failure path (tier 2). _(Review B/C.)_
- **DEC-009 — Two `_PROMPT_VERSION` base constants + dispatch.** `_PROMPT_VERSION_PER_MODEL` (byte-identical to today's `_PROMPT_VERSION`; the bare name stays as an alias) and `_PROMPT_VERSION_PROJECT` (adds `_PROJECT_SUMMARY_TEMPLATE` + the project-scope defence text to its hash inputs). `_prompt_version_for(exclude_tests, cache_scope)` selects the base, then folds `"|scope=" + cache_scope + "|exclude=" + canonical_json` into a blake2b-8 when either dimension is non-default. _(Review D; llm-drafter.md `_prompt_version_for` precedent.)_
- **DEC-010 — `draft_overrides` overlay kwarg.** `_run_single_model` gains `draft_overrides: dict[str, str] | None = None`, applied as `DraftConfig.model_validate({**draft_config.model_dump(), **draft_overrides})` (re-runs validators). Mirrors the prune/grade/diff overlay precedent verbatim. _(Review F.)_
- **DEC-011 — Per-batch render cost accepted.** The project prefix renders + counts once per model (one `count_tokens` per call — the same cost profile as today's per-model path). Batch-level memoisation is deferred to a v0.x follow-up and documented, not silently capped. _(Review G.)_
- **DEC-012 — No tiny-project special-casing.** When the compressed project prefix is below the 1024/2048 cache minimum, `call_llm`'s existing marker-drop + INFO covers it (correct, just uncached). Auto-promote does not gate on batch size or a token floor. _(Review H.)_
- **DEC-013 — Business-rules placement: cached context + per-model instruction.** Project-wide rules render in the cached `<PROJECT_MANIFEST>` block (shared context, ordered per DEC-007). The model-under-draft's own rules **also** render in its dynamic `<BUSINESS_RULE>` section (the crisp drafting instruction). The current model's own rules duplicate (typically 1–3; intentional). The parser cardinality gate is unchanged — it reads `model.business_rules` from the Model object, not the prompt. _(Q3, refinement Q5.)_
- **DEC-014 — Two cache-stability goldens, lockstep rotation.** `tests/llm/test_prompt_cache_stability.py` pins both per-model (unchanged) and project goldens + both version constants. Per-model rotates on `_SYSTEM_PROMPT`/`_MANIFEST_SUMMARY_TEMPLATE`/`_DATA_SECTION_TEMPLATES` change; project rotates on `_SYSTEM_PROMPT`(project variant)/`_PROJECT_SUMMARY_TEMPLATE`/`_DATA_SECTION_TEMPLATES` change. Grade-side `_PROMPT_VERSION` is untouched (drafter-only change). _(Review E/J; business-rule-tests.md DEC-012.)_
- **DEC-015 — `render_prompt` gains keyword `cache_scope` (default `"per-model"`).** The positional / single-model path passes the default → byte-identical output to today. The drafter passes `config.cache_scope`. _(Review A/F.)_

### Session notes

- 2026-06-03: Discovery + 4 scoping answers (whole-project compressed / auto-fallback / include project rules / config+flag). Architecture review (3 parallel) cleared 3 blockers with DEC-006/007/008. Refinement Q5 resolved rules placement (DEC-013). Ready to detail.

## Detailed Breakdown

Architecture ordering: config → prompt rendering → drafter integration → CLI → tests/docs. Validation command for every story: `ruff check . && ruff format --check . && pyright && pytest`.

### US-001 — `DraftConfig.cache_scope` field + drift detection

**Description:** Add the `cache_scope` config field to `DraftConfig` and extend the config drift-detection pair so a YAML typo fails loud.
**Traces to:** DEC-001.
**Files:** `src/signalforge/draft/config.py` (field + docstring), `tests/draft/test_config.py` (load with/without `cache_scope`, typo-fails-loud, `StrictDraftConfig` drift), config fixture(s) under `tests/fixtures/`.
**Depends on:** none.
**TDD:** `cache_scope` defaults to `"per-model"`; valid `"project"` loads; invalid value rejected by `Literal`; unknown sibling key under `llm:` rejected by `extra="forbid"`; drift detector still passes against the updated fixture.
**Done When:**
- [ ] `DraftConfig.cache_scope: Literal["per-model","project"] = "per-model"` exists with a docstring naming #188 + auto-promote semantics.
- [ ] `load_draft_config` round-trips the field from `signalforge.yml` `llm.cache_scope`.
- [ ] Drift-detection test updated; typo (`cache_scop`) fails loud.
- [ ] `make verify` (`ruff check . && ruff format --check . && pyright && pytest`) passes.

### US-002 — Project-summary renderer + `<PROJECT_MANIFEST>` envelope + deterministic order

**Description:** Add `_render_project_summary(manifest)` (compressed `name (N cols)` lines), `_PROJECT_SUMMARY_TEMPLATE`, and `_read_project_business_rules(manifest)` (deterministic project-wide aggregation), all wrapped in a `<PROJECT_MANIFEST>` envelope with boring-substring breach guards. Extend `PromptEnvelopeBreachError` for the project source.
**Traces to:** DEC-004, DEC-005 (envelope only — defence-text wiring is US-003), DEC-007, DEC-008, DEC-013.
**Files:** `src/signalforge/draft/prompts.py`, `src/signalforge/draft/errors.py`, `tests/draft/test_prompts.py`.
**Depends on:** none.
**TDD:** project summary lists every model `name (N cols)` in `sorted(unique_id)` order; rendering for model A vs model B yields a byte-identical project block (cache-hit precondition); project business rules aggregate model-then-column with a global 1-indexed counter, deterministic regardless of input order; `</PROJECT_MANIFEST>` in a model description raises `PromptEnvelopeBreachError`; `</BUSINESS_RULE>` in any project rule raises with `rule_source="project"`; empty-rules / `custom_sql`-excluded project produces no `<BUSINESS_RULE>` block.
**Done When:**
- [ ] `_render_project_summary` + `_read_project_business_rules` deterministic + byte-identical across the model under draft.
- [ ] `<PROJECT_MANIFEST>` + project `<BUSINESS_RULE>` breach guards raise (boring substring; opening tag / fragments allowed).
- [ ] `PromptEnvelopeBreachError` carries nullable `model_unique_id` + `rule_source`; still registered in the exit-code map (scan 7 green).
- [ ] `make verify` passes.

### US-003 — Dual `_PROMPT_VERSION` + scope-aware system prompt + `render_prompt` dispatch

**Description:** Add the two base version constants and `_prompt_version_for(exclude_tests, cache_scope)`; make `_render_system_prompt` scope-aware (project variant appends the `<PROJECT_MANIFEST>` injection-defence line; per-model byte-identical); thread `cache_scope` through `render_prompt` to dispatch per-model vs project cached block, with full neighbour detail moved into the dynamic block in project scope.
**Traces to:** DEC-004, DEC-005, DEC-009, DEC-013, DEC-015.
**Files:** `src/signalforge/draft/prompts.py`, `tests/draft/test_prompts.py`.
**Depends on:** US-002.
**TDD:** `_PROMPT_VERSION_PER_MODEL` == today's pinned value (no drift); `_PROMPT_VERSION_PROJECT` differs from per-model; `_prompt_version_for((), "per-model")` == per-model base; scope + exclude compose into distinct hashes; per-model `_render_system_prompt(())` byte-identical to today; project-scope system prompt contains the defence line; `render_prompt(..., cache_scope="project")` emits the shared project block + a dynamic block carrying `<MODEL_SQL>` + this model's own rules + this model's neighbour detail.
**Done When:**
- [ ] Per-model version + system prompt byte-identical to current (existing goldens unchanged).
- [ ] Project version pinned and ≠ per-model; composition with `exclude_tests` deterministic.
- [ ] `render_prompt` dispatches on `cache_scope`; project dynamic block has full per-model detail + repeated own-rules.
- [ ] `make verify` passes.

### US-004 — Drafter integration: thread `cache_scope` + oversize catch-and-retry

**Description:** Thread `config.cache_scope` through `draft_schema`/`draft_from_request` into `render_prompt`; implement the oversize fallback by catching `LLMCacheTooLargeError` and re-rendering per-model + retrying `call_llm` once when scope was project; emit the INFO breadcrumb.
**Traces to:** DEC-006, DEC-015.
**Files:** `src/signalforge/draft/schema.py`, `tests/draft/test_schema.py` (or the relevant drafter test module), using `FakeAnthropicClient` count-token stubs.
**Depends on:** US-001, US-003.
**TDD:** project scope under cap → project block sent (assert cached-block bytes); project scope over 8000 → one INFO + per-model block sent on retry + run succeeds; per-model scope over cap → `LLMCacheTooLargeError` propagates (no retry); INFO is lazy-format JSON (passes the grep gate); response audit still written once on the successful (retried) path.
**Done When:**
- [ ] `cache_scope` flows config → `render_prompt`; default path byte-identical to today.
- [ ] Oversize project scope falls back to per-model + INFO + exit 0; per-model oversize re-raises.
- [ ] Logger grep gate green (no f-strings); audit-completeness scans green.
- [ ] `make verify` passes.

### US-005 — CLI `--cache-scope` flag + auto-promote + overlay

**Description:** Add the `--cache-scope` flag; add the `draft_overrides` overlay kwarg to `_run_single_model`; auto-promote to project in `_run_batch` when `len(matched) >= 2` unless the operator pinned a scope; enforce precedence (flag > YAML non-default > auto).
**Traces to:** DEC-002, DEC-003, DEC-010.
**Files:** `src/signalforge/cli/generate.py` (argparse + `_run_single_model` + `_run_batch`), `src/signalforge/cli/_helpers.py` if needed, `tests/cli/` (batch + single-model overlay tests).
**Depends on:** US-001, US-004.
**TDD:** `--select` matching ≥ 2 promotes to project (assert the overlaid `DraftConfig.cache_scope`); single-model positional never promotes; `--cache-scope per-model` on a ≥2 batch stays per-model (flag wins); `--cache-scope project` on a single-model run forces project; YAML `llm.cache_scope: project` honoured when no flag; overlay re-validates via `model_validate`; no-traceback floor on all CLI tests.
**Done When:**
- [ ] Flag parsed (choices, help); overlay applied via `model_validate`.
- [ ] Auto-promote + precedence correct and tested across the matrix.
- [ ] Single-model output shape unchanged byte-for-byte.
- [ ] `make verify` passes.

### US-006 — Cache-stability goldens (per-model unchanged + project new)

**Description:** Extend `tests/llm/test_prompt_cache_stability.py` with the project-scope version pin + cached-block golden + the cross-model byte-identity assertion; document the lockstep rotation policy in the test docstring.
**Traces to:** DEC-007, DEC-014.
**Files:** `tests/llm/test_prompt_cache_stability.py`.
**Depends on:** US-003.
**TDD:** existing `_EXPECTED_PROMPT_VERSION` + `_CACHED_BLOCK_GOLDEN` unchanged (per-model assert still green); new `_EXPECTED_PROMPT_VERSION_PROJECT` + `_CACHED_BLOCK_GOLDEN_PROJECT` pinned; rendering the project block for two different models in the fixture is byte-identical and matches the golden; assert project version ≠ per-model version.
**Done When:**
- [ ] Per-model golden + version untouched.
- [ ] Project golden + version pinned; cross-model byte-identity asserted; lockstep rotation documented.
- [ ] `make verify` passes.

### US-007 — CLI flag reference + 5-surface parity

**Description:** Document the `--cache-scope` flag in the CLI reference, correct the existing "cache doesn't amortise across siblings" caveat in the multi-model section (it now does in project scope), and ship the 5-surface parity test for `--cache-scope`. (Conceptual / stakeholder-facing explainer is US-008.)
**Traces to:** DEC-002, DEC-003, cli-layer.md DEC-015/017.
**Files:** `docs/cli-ops.md` (flag reference + "Running across many models" caveat), `tests/cli/test_5_surface_parity_cache_scope.py`.
**Depends on:** US-005.
**TDD:** parity test asserts the same example tokens (`--cache-scope`, `per-model`, `project`) appear in argparse help, `docs/cli-ops.md`, and this plan; ship it only after the surfaces exist (no `pytest.skip` placeholders).
**Done When:**
- [ ] `docs/cli-ops.md` `--cache-scope` flag reference added; sibling-cache caveat corrected.
- [ ] 5-surface parity test green.
- [ ] `make verify` passes.

### US-008 — Feature documentation: bulk-mode shared cache (what / how / which models)

**Description:** Write the operator- and stakeholder-facing explainer for the bulk-mode shared cached prefix: **what** it is and the problem it solves (drafter cache-hit rate 0% → ~95% across a `--select` batch); **how** it works (one shared project-level prefix cached once, then read at ~12× cheaper input on every subsequent model, with per-model detail in the uncached dynamic block); automatic activation on `--select` ≥ 2 models + the operator override; the oversize auto-fallback to per-model; the cost model with worked numbers; and — explicitly — **which providers/models it applies to**. The applicability matrix is the load-bearing addition: prompt caching is an **Anthropic-only** capability, so the feature delivers savings only for Anthropic models (with the per-family minimum cacheable sizes); on OpenAI / Gemini providers the prompt is still restructured but there is no cache and therefore **no benefit and no penalty** — operators on those providers should know it's effectively a no-op.
**Traces to:** DEC-001…DEC-006, DEC-012, DEC-013; llm-drafter.md § "Cached-block scope"; grade-layer.md § provider prompt-caching note.
**Files:** `docs/draft-ops.md` (new "Bulk-mode shared cache" section: what / how / cost model / oversize fallback + a **provider & model applicability** subsection), `README.md` (short cross-link from the perf/feature area), `src/signalforge/skills/signalforge/SKILL.md` (if it teaches `--select` batches).
**Depends on:** US-005.
**TDD:** docs-only; covered by the canonical validation (`make verify`). The machine-checked token parity lives in US-007.
**Done When:**
- [ ] `docs/draft-ops.md` explains what the feature is, how the shared prefix works, automatic activation, the oversize fallback, and the cost model.
- [ ] A **provider & model applicability** subsection names Anthropic models as benefiting (Sonnet/Opus min cacheable 1024 tokens, Haiku 2048) and states OpenAI / Gemini get no cache benefit and no penalty (the restructure is a no-op there).
- [ ] `README.md` cross-links the section; `SKILL.md` updated if it covers `--select` batch runs.
- [ ] `make verify` passes.

### US-009 — Quality Gate (code review ×4 + CodeRabbit)

**Description:** Run the code reviewer four times across the full changeset, fixing every real bug each pass; run CodeRabbit if available; validation must pass after all fixes.
**Traces to:** all DECs.
**Files:** as needed across the changeset.
**Depends on:** US-001 … US-008.
**Done When:**
- [ ] 4 reviewer passes complete; all real findings fixed.
- [ ] CodeRabbit findings triaged/fixed.
- [ ] `make verify` passes.

### US-010 — Patterns & Memory

**Description:** Distil the new patterns into the rule files and docs; record durable insights in memory.
**Traces to:** all DECs.
**Files:** `.claude/rules/llm-drafter.md` (project cache scope, dual `_PROMPT_VERSION`, `<PROJECT_MANIFEST>` envelope, catch-and-retry fallback), `.claude/rules/cli-layer.md` (draft-config overlay + auto-promote + corrected cache caveat), `.claude/rules/business-rule-tests.md` (project-rule aggregation + placement), `.claude/rules/testing-signal.md` (second cache-stability golden + cross-model byte-identity), plan reference.
**Depends on:** US-009.
**Done When:**
- [ ] Rule files updated with the new graduation/patterns; cross-links added.
- [ ] `make verify` passes.

## Story dependency graph

```
US-001 ─┬──────────────► US-004 ──► US-005 ─┬─► US-007 ┐
US-002 ──► US-003 ──┴──► US-006              └─► US-008 ┤
                                                        ├─► US-009 ──► US-010
                  (US-006 also feeds US-009) ───────────┘
```
- US-001: none
- US-002: none
- US-003: US-002
- US-004: US-001, US-003
- US-005: US-001, US-004
- US-006: US-003
- US-007: US-005
- US-008: US-005   (parallel with US-007)
- US-009 (Quality Gate): US-001…US-008
- US-010 (Patterns & Memory): US-009
