# Issue #35 — safety: reframe schema-only docs + add `prune.enabled` knob

## Meta

- **Ticket:** [#35](https://github.com/wjduenow/SignalForge/issues/35)
- **Branch:** `feature/35-prune-enabled-doc-reframe` (off `dev`)
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/35-prune-enabled-doc-reframe`
- **Phase:** devolved
- **Sessions:** 1 (started 2026-05-11)
- **Plan author:** Claude Code (Opus 4.7, 1M context)
- **Milestone:** v0.2 (first-run UX repair — onboarding promise of "I can try this safely")
- **Labels:** `cli`, `safety`, `docs` (per the GitHub issue)

---

## Discovery

### Ticket summary (verbatim from GitHub issue #35)

> The `safety.mode` config knob has three values (`schema-only`, `aggregate-only`, `sample`) and defaults to `schema-only`. A first-time user reading the README naturally reads "schema-only" as "no warehouse contact." That mental model is wrong: `safety.mode` only governs what the *LLM* sees, not what the *prune step* does. The prune engine **always** runs SQL against the warehouse — independent of safety mode — because it has to in order to detect always-pass tests.
>
> **Doc reframe (small):**
> 1. In `docs/safety-ops.md` § Configuration, rename the conceptual framing — `safety.mode` is "what the LLM sees," not "whether the warehouse is contacted."
> 2. In `docs/cli-ops.md` § `generate` and `README.md` Quick Start, add a one-line callout: "The prune step runs warehouse SQL on every invocation regardless of `safety.mode`. To skip the prune layer entirely, see `prune.enabled` (v0.2)."
>
> **Code (larger):**
> 3. Add `prune.enabled: bool = true` to `PruneConfig` (`extra="forbid"`; default keeps current behaviour). When `false`, `prune_tests` short-circuits and routes every candidate to `kept-without-evidence` with `why="prune disabled in signalforge.yml"`.
> 4. The CLI's `cmd_generate` emits a startup INFO when `prune.enabled=false` so the operator gets a visible signal.
>
> **Acceptance criteria** are reproduced verbatim under each story below.

### Why this is HIGH priority

- **Load-bearing first-run UX trap.** A user expecting "schema-only = no warehouse calls" sees bytes-billed in their BQ console and reasonably distrusts the tool.
- **Architectural Commitment #4 (OSS-first, Core-friendly).** "I can try this safely" is a load-bearing onboarding promise.
- **Blocks "I'd run this in CI" adoption** — users uncertain about cost/contact behaviour won't wire it into automated paths.

### Codebase findings (directly verified, file:line cited)

**`PruneConfig` (`src/signalforge/prune/config.py:62-149`).** Frozen Pydantic v2 model, `extra="forbid"` per DEC-015 (typos like `scop:` fail loud). Eight existing fields: `scope`, `sample_size`, `test_timeout_seconds`, `total_budget_seconds`, `capture_failure_rows`, `trusted_models`, `partition_filter`, `sample_strategy`. Adding a ninth `enabled: bool = True` is mechanically trivial; the loader's outer `_PruneConfigFile` (`extra="ignore"`) is unchanged.

**`prune_tests` orchestrator (`src/signalforge/prune/engine.py:588-1049`).** The current entry resolves config, validates `trusted_models`, resolves `TableRef`, opens `with adapter:`, dispatches by `sample_strategy`, iterates per-test. The natural short-circuit point is **immediately after config resolution** (line 719), *before* `_validate_trusted_models` — disabling prune should not require a valid trusted-models list to even check.

**Materialisation-failed branch (`engine.py:825-867`) is the precedent.** When `adapter.materialise_sample` raises, the orchestrator emits one `WARNING`, then drains every candidate to `kept-without-evidence` via `_decide_kept_without_evidence_materialisation_failed` and writes one `PruneEvent` per candidate. The disabled-prune path mirrors this shape exactly — same fail-closed audit invariant; only the `why` text and the lack of a triggering exception differ. Refactor target: a single `_decide_kept_without_evidence_disabled` helper.

**Existing `kept-without-evidence` `why` strings (`engine.py:170-189`).** Each shape is a small helper (`_why_kept_without_evidence_warehouse_error`, `_why_kept_without_evidence_budget`, `_why_materialisation_failed`). Add `_why_prune_disabled` returning the ticket's locked string `"prune disabled in signalforge.yml"`.

**CLI prune dispatch (`src/signalforge/cli/generate.py:529-559`).** `load_prune_config(project_dir)` returns the config; `--scope` / `--sample-strategy` overrides are applied via `PruneConfig.model_validate`; `prune_tests` is called with the resolved config. The natural INFO emission point is between `prune_config` resolution and `prune_tests` invocation, gated on `not prune_config.enabled`. The existing `emit_progress_entry(3, "prune", ...)` already fires there — the INFO is a sibling, not a replacement (per Q2=A: INFO at the prune stage progress block).

**Drift detector (`tests/prune/test_drift_detector.py`).** Covers `PruneDecision`, `PruneResult`, `PruneEvent` — production `extra="ignore"` read-back models. **`PruneConfig` is already `extra="forbid"` in production (DEC-015), so no drift gate is needed for the config itself** (line 15-16 of the drift-detector module comment confirms this). No drift-test changes required for the new field. The new `_decide_kept_without_evidence_disabled` decision will produce a `PruneEvent` with `reason="kept-without-evidence"` — already covered by `prune_event_v1.jsonl`'s existing rows for the same reason (no new fixture update needed unless we want to demonstrate the disabled path specifically; see SQ-01).

**Engine test patterns (`tests/prune/test_engine.py:144-921`).** Established hand-rolled `FakeAdapter` / `FakeBigQueryClient` pattern via `tests/warehouse/_fake.py`. The closest precedent is `test_prune_tests_total_budget_exceeded_marks_remaining_kept_without_evidence` (line 340) and the materialisation-failed branch tests in the v0.2 additions block (line 986+) — both verify that no warehouse calls occur, every candidate routes to `kept-without-evidence`, and one `PruneEvent` per candidate lands in the audit JSONL.

**Doc surfaces to touch.**
- `docs/safety-ops.md:14-30` § "Default posture" — reframe.
- `docs/safety-ops.md:37-45` § `schema-only` — the current "No warehouse queries are issued" line is correct *within the safety-mode framing* but misleading without the prune callout. Add a paragraph or footnote.
- `docs/cli-ops.md` § `generate` — one-line callout (the ticket's verbatim text).
- `docs/prune-ops.md:85-119` § "Configuration: signalforge.yml prune: block" — document the new `enabled` field next to `scope`.
- `README.md` § "Quick start" (line 44 onward, ends ~line 200) — one-line callout near the safety-mode reference (line 200).

### Convention findings (rules + CLAUDE.md)

Twelve applicable rules. Load-bearing ones for this ticket:

1. **`prune-engine.md` DEC-006 / DEC-011 — conservative-bias routing across `WarehouseError` subclasses.** The disabled-prune branch is a *new* "we have no warehouse evidence" path; it MUST route to `kept-without-evidence` (decision="kept"), not "dropped". Mirrors the existing materialisation-failure routing verbatim. The 5-value `DropReason` literal stays locked — no sixth value.

2. **`prune-engine.md` DEC-016 — fail-closed audit.** Every per-candidate decision must produce a `PruneEvent` JSONL row via `_write_prune_event`. The disabled branch is no exception: skipping the audit on a "fast path" would violate the invariant. **Decision DEC-001 (below) locks this in** — write one `PruneEvent` per candidate even when disabled.

3. **`prune-engine.md` DEC-015 — config-shaped models use `extra="forbid"`.** `PruneConfig.enabled: bool = True` ships with the existing `extra="forbid"` config; a typo like `enable:` will fail loud at config load. No code change needed for the validation — the existing `_PruneConfigFile`/`PruneConfig` separation handles it.

4. **`prune-engine.md` DEC-017 / logger grep gate.** Any new `_LOGGER` call (the INFO in `cmd_generate`) MUST use lazy `%s` + `json.dumps(...)` — never f-strings. The grep gate at `tests/llm/test_logger_grep_gate.py` scans `src/signalforge/{llm,draft,prune,grade,diff,cli}` (6 dirs as of #9) and rejects any `_LOGGER\.\w+\(f"` hit. The CLI INFO must follow.

5. **`prune-engine.md` v0.2 reservations § "5-surface parity for v0.x → v0.(x+1) graduations"** (lines 386-394 of the rule). This ticket adds a new behaviour-active surface (`prune.enabled`), not a graduation, but the same parity check applies in spirit:
   - Rule file — update `.claude/rules/prune-engine.md` to document `prune.enabled` in the same way `sample_strategy` is documented (story US-006).
   - Ops doc — `docs/prune-ops.md` § Configuration (story US-002).
   - CLAUDE.md — extend the "v0.2 additions" bullet under "Public API surface" (story US-006).
   - Test — pin `prune_tests` short-circuits when disabled (story US-005).
   - DEC — captured in this plan (DEC-001 through DEC-004 below).

6. **`safety-layer.md` DEC-011 — fail-closed audit.** The doc reframe in `docs/safety-ops.md` must keep the safety-layer's own contract intact ("schema-only does not contact the warehouse — but the prune step still does"). The reframe is a clarification of scope, not a relaxation of any safety-layer invariant.

7. **`cli-layer.md` DEC-017 — stderr message shape.** The new INFO is NOT a typed-error stderr message; it's a routine log line. It goes through `_LOGGER.info`, NOT `format_error_to_stderr`. The CLI's panic-path / exit-code contract is untouched.

8. **`testing-signal.md` DEC-010 — no `assert True`-shaped tests.** Story US-005 must verify (a) zero warehouse calls when disabled, (b) one `PruneEvent` per candidate lands in the audit JSONL, (c) every decision has `reason="kept-without-evidence"` and `why="prune disabled in signalforge.yml"`. The fake-adapter assertion API (`assert_all_expectations_met` returning zero unexpected calls) is the load-bearing assertion.

9. **`manifest-readers.md` DEC-007 — symlink-hardened path resolution.** The audit-path symlink-harden gate in `prune_tests` (engine.py:744-763) is upstream of the short-circuit, so the disabled branch still benefits from it. We must position the short-circuit *after* audit-path resolution so audit writes still go through the symlink-hardened path. **DEC-002 (below) locks the short-circuit position.**

10. **`python-build.md`** — no build changes; new field lands inside existing module.

11. **`ci-supply-chain.md`** — no workflow changes; existing CI exercises the new code via `pytest`.

12. **No `workflow-project.md`** — no project-specific scoping questions or extra review areas.

---

## Architecture Review

| Area | Rating | Findings |
|---|---|---|
| Security | pass | No new attack surface. The disabled-prune path issues zero warehouse calls and zero LLM calls; the only new I/O is audit-JSONL writes via the existing fail-closed seam (already symlink-hardened). |
| Performance | pass | Disabling prune is strictly faster than the default path (no warehouse calls). The N audit-JSONL writes per run are bounded by candidate count (typically ~30 per model); each write is one ~250-byte append + fsync. |
| Data model | pass | Net add of one `bool` field on `PruneConfig`. Default `True` preserves current behaviour. No migration / no backward-compat concern: v0.1 `signalforge.yml` files without the field load with the default. |
| API design | pass | `PruneConfig.enabled: bool = True` follows the existing config-knob convention (DEC-015). No new function signature; `prune_tests` keeps its existing signature. The short-circuit is internal to the orchestrator. |
| Observability | concern → resolved | Originally considered a WARNING vs INFO trade-off (Q2 asked the user). User chose INFO at the prune stage progress block (DEC-004). Sufficient: the INFO is visible at default verbosity and pairs with the existing `emit_progress_entry`. |
| Testing strategy | pass | Unit test for the short-circuit path (no warehouse calls, audit rows produced); drift detector unchanged (`PruneConfig` is already `extra="forbid"`); existing engine tests untouched. |
| Architectural commitments | concern | `prune.enabled=false` lets always-pass tests ship — directly counter to Commitment #1 (signal over volume). Mitigation: the `kept-without-evidence` routing makes the "no warehouse evidence" framing explicit in the diff; default stays `True`; INFO emission keeps the trade-off visible. The ticket explicitly asks for this escape hatch as a first-run UX repair, so accepting the trade-off is the right call. |

**No blockers.** All concerns resolved.

---

## Refinement Log

### DEC-001 — Disabled-prune writes one `PruneEvent` per candidate to the audit JSONL

Q1 asked whether the disabled path should write audit rows or skip them entirely. **Decision: write one `PruneEvent` per candidate.**

**Rationale:** mirrors the materialisation-failed branch (`engine.py:842-859`) verbatim — same fail-closed audit invariant: every prune_tests call produces a JSONL row per candidate, regardless of why the candidate ended up kept-without-evidence. The disabled path is conceptually a "we chose not to gather evidence" branch, not a no-op. Two operator-facing benefits:

1. The diff renderer (#8) keys on the audit's `(run_id, test_anchor)` triple — without audit rows, the diff would show candidates with no correlating audit lines, breaking the cross-stage join.
2. An operator who enables prune later in a follow-up run can `jq` over the prior `prune.jsonl` to see exactly which tests went unchecked.

**Cost:** one fsync per candidate (typically ~30). Negligible vs. the alternative warehouse-roundtrip path the operator is opting out of.

### DEC-002 — Short-circuit position: after audit-path resolution, before `_validate_trusted_models`

Position the disabled-prune short-circuit at engine.py inside `prune_tests`, **after** the audit-path resolution + symlink-harden block (current line 763), **before** `_validate_trusted_models` (line 722).

**Rationale:**
- The audit-path resolution and symlink-hardening MUST still run because we still write audit rows (DEC-001). This is the symlink-hardened gate from `manifest-readers.md` DEC-007.
- `_validate_trusted_models` should NOT run on the disabled path — an operator who disabled prune shouldn't need to keep a valid `trusted_models` list. Disabling prune is a "stop talking to my warehouse" escape; failing on a stale trusted-models entry would defeat the UX promise.
- The `TableRef.from_model` call (line 724) should NOT run either — it raises `ManifestProjectNotFoundError` / `ManifestSchemaNotFoundError` on shape problems that are irrelevant when we're not running any SQL.
- The `with adapter:` block (line 783) should NOT be entered either — the adapter is not invoked at all on the disabled path. No `materialise_sample`, no per-test loop.

The position is: between current line 763 (audit path resolved) and line 768 (`_compute_config_hash`). Compute `config_hash` first (cheap, in-process), then short-circuit.

### DEC-003 — `why` text is locked verbatim: `"prune disabled in signalforge.yml"`

Per the ticket. The CLI does not (yet) ship a `--no-prune` flag; the field is only set via the config file. If a CLI flag lands later, this string becomes stale — but that's a v0.3 follow-up, not a v0.2 concern. Pin the text in a stability test (US-005) so a future maintainer either updates the test deliberately or stays in sync.

### DEC-004 — CLI emits one INFO line at prune-stage entry when `enabled=false`

Q2 asked INFO vs WARNING vs both. **Decision: INFO at the prune stage progress block.** Per ticket and user selection.

Shape (lazy-format JSON per `prune-engine.md` DEC-017 grep gate):

```python
_LOGGER.info(
    "prune disabled in signalforge.yml; routing all candidates to kept-without-evidence: %s",
    json.dumps({
        "model_unique_id": model.unique_id,
        "candidate_count": candidate_test_count,
    }),
)
```

Position: in `cmd_generate` (`src/signalforge/cli/generate.py`), immediately after `prune_config` resolution (line 540), gated on `if not prune_config.enabled:`. Fires once per `signalforge generate` invocation. The existing `emit_progress_entry(3, "prune", ...)` still fires (with a different `<fact>` since `candidate_test_count` is the same shape).

**Not a WARNING:** the operator explicitly opted in via config — surfacing a WARNING on every run would be nagging, not signal. The trade-off is documented in the README + safety-ops + prune-ops; the INFO confirms the short-circuit fired.

**Not at startup:** the prune stage is where the short-circuit observably happens; the INFO is placed there so the operator sees it in stage-order with the progress narrative.

### DEC-005 — `enabled` lives on `PruneConfig`, not on a top-level `cli:` block

The ticket explicitly says `PruneConfig.enabled` (line 22 of the issue body). No top-level `cli:` block. Mirrors the convention from `prune-engine.md` DEC-020 ("each stage's behaviour-knob block stays separate") — the prune layer's enable/disable knob lives in the prune block. The `extra="forbid"` validator on `PruneConfig` enforces typos fail loud.

### DEC-006 — One PR for docs + code (Q4=A)

Per user choice. Smaller blast radius for review (5-7 files), easier to revert as a unit, and the doc references the new knob immediately. The Quality Gate story runs CodeRabbit + code-reviewer x4 across the full diff.

### DEC-007 — No new `DropReason` literal

The disabled-prune branch routes to the existing `"kept-without-evidence"` literal. The 5-value `DropReason` stays locked (per `prune-engine.md` decision-matrix invariant). If we discover in v0.3 that operators need to distinguish "disabled" from "couldn't evaluate" in the diff, we can promote a new literal then — for v0.2, the `why` text carries the distinguishing signal.

---

## Detailed Breakdown

Seven stories, ordered by dependency. Each is one context window for Ralph.

---

### US-001 — `PruneConfig.enabled` field + config-loader pass-through

**Description:** Add `enabled: bool = True` to `PruneConfig` (`src/signalforge/prune/config.py`). The existing `extra="forbid"` validator catches typos automatically; the existing `_PruneConfigFile` outer (`extra="ignore"`) does not need to change.

**Traces to:** DEC-005.

**Acceptance criteria:**
- `PruneConfig.enabled: bool = True` lands with a docstring explaining the trade-off (signal over volume) and pointing operators at the diff's `kept-without-evidence` framing.
- A `signalforge.yml` with `prune.enabled: false` loads and `load_prune_config(project_dir).enabled == False`.
- A `signalforge.yml` with `prune.enabld: false` (typo) raises `PruneConfigError` (existing `extra="forbid"` behaviour).
- A `signalforge.yml` without the field (the v0.1 case) loads with `enabled=True` (default).
- `ruff check . && ruff format --check . && pyright && pytest` passes.

**Done when:** the field exists, the four cases above are covered by tests in `tests/prune/test_config.py`, and the full validation command is green.

**Files:**
- `src/signalforge/prune/config.py` — add field + docstring.
- `tests/prune/test_config.py` — add tests for default, explicit-false, explicit-true, typo.

**Depends on:** none.

**TDD:**
- `test_load_prune_config_enabled_defaults_to_true_when_field_absent`
- `test_load_prune_config_enabled_false_when_explicitly_set`
- `test_load_prune_config_enabled_typo_raises_config_error`

---

### US-002 — Engine short-circuit + `_decide_kept_without_evidence_disabled` helper

**Description:** Add a `_decide_kept_without_evidence_disabled` helper (mirrors `_decide_kept_without_evidence_materialisation_failed`) and a `_why_prune_disabled` helper (mirrors `_why_materialisation_failed`). In `prune_tests`, after audit-path resolution and config-hash computation, branch on `not resolved_config.enabled`: drain every candidate to `kept-without-evidence` via the new helper, write one `PruneEvent` per candidate via `_write_audit_or_abort`, and return the `PruneResult` without entering `with adapter:` or `_validate_trusted_models`.

**Traces to:** DEC-001, DEC-002, DEC-003, DEC-007.

**Acceptance criteria:**
- New helper `_why_prune_disabled() -> str` returns the literal `"prune disabled in signalforge.yml"`.
- New helper `_decide_kept_without_evidence_disabled(test, test_anchor, scope) -> PruneDecision` produces `decision="kept"`, `reason="kept-without-evidence"`, `failures=0`, `sampled_rows=None`, `elapsed_ms=0`, `compiled_sql=""`, `compiled_sql_hash=_build_compiled_sql_hash_or_empty("")`, `why=_why_prune_disabled()`, `sample_failures=None`.
- `prune_tests` short-circuits when `resolved_config.enabled is False`: every candidate produces one `PruneDecision` via the new helper, one `PruneEvent` is written to the audit JSONL per decision (`_write_audit_or_abort`), and the function returns a `PruneResult` *without* invoking `_validate_trusted_models`, `TableRef.from_model`, `adapter.dialect`, `adapter.materialise_sample`, `adapter.run_test_sql`, or entering `with adapter:`.
- The audit-path symlink-hardening still runs (the gate is upstream of the short-circuit per DEC-002).
- The `config_hash` is computed and stamped on every audit row (so a reviewer can correlate the disabled run with its config).
- `ruff check . && ruff format --check . && pyright && pytest` passes (US-005 verifies behaviour; this story focuses on the implementation seam).

**Done when:** the short-circuit lives in `prune_tests`, the two helpers ship, and existing engine tests stay green (default `enabled=True` preserves all current behaviour).

**Files:**
- `src/signalforge/prune/engine.py` — add `_why_prune_disabled`, `_decide_kept_without_evidence_disabled`, and the short-circuit branch in `prune_tests`.

**Depends on:** US-001.

**TDD:** behaviour-pinning tests live in US-005 (the integration story); this story is the implementation seam.

---

### US-003 — CLI INFO emission when prune disabled

**Description:** In `src/signalforge/cli/generate.py` `cmd_generate`, immediately after `prune_config` resolution (and after any `--scope` / `--sample-strategy` overrides), emit one INFO line via the module's `_LOGGER` when `not prune_config.enabled`. Use lazy-format JSON per `prune-engine.md` DEC-017 (the grep gate at `tests/llm/test_logger_grep_gate.py` covers `src/signalforge/cli/`).

**Traces to:** DEC-004.

**Acceptance criteria:**
- `cmd_generate` emits exactly one `_LOGGER.info("prune disabled in signalforge.yml; routing all candidates to kept-without-evidence: %s", json.dumps({"model_unique_id": ..., "candidate_count": ...}))` line when `prune_config.enabled is False`.
- No INFO emitted when `prune_config.enabled is True` (the default).
- The existing `emit_progress_entry(3, "prune", ...)` still fires unchanged.
- The grep gate `tests/llm/test_logger_grep_gate.py` passes (no f-string in the new logger call).
- A CLI in-process test (`tests/cli/test_generate.py` or sibling) using a fake adapter + injected fake LLM verifies the INFO is emitted on the disabled path via `caplog`.

**Done when:** the INFO line ships, the test pins it, and the grep gate stays green.

**Files:**
- `src/signalforge/cli/generate.py` — add the gated `_LOGGER.info` call.
- `tests/cli/test_generate.py` (or the appropriate sibling) — add an in-process test asserting the INFO is emitted when `prune.enabled=false` in the project's `signalforge.yml`.

**Depends on:** US-001, US-002.

**TDD:**
- `test_cmd_generate_emits_info_when_prune_disabled`
- `test_cmd_generate_does_not_emit_disabled_info_when_prune_enabled` (default)

---

### US-004 — Docs reframe: `docs/safety-ops.md`, `docs/cli-ops.md`, `docs/prune-ops.md`, `README.md`

**Description:** Four-doc reframe. The wording stays close to the ticket's verbatim guidance; the load-bearing change is conceptual scope clarification, not new prose.

**Traces to:** Acceptance criteria (1) and (2) of the GitHub issue body verbatim.

**Acceptance criteria:**
- **`docs/safety-ops.md`** § "Default posture" and § "schema-only" mode are reframed so `safety.mode` is "what the LLM sees," not "whether the warehouse is contacted." Add an explicit note in both sections: *"The prune step (`signalforge.prune.prune_tests`) runs warehouse SQL on every invocation regardless of `safety.mode`. To skip the prune layer entirely, see [`prune.enabled`](prune-ops.md#configuration-signalforgeyml-prune-block) in `docs/prune-ops.md`."*
- **`docs/cli-ops.md`** § `generate` adds a one-line callout: *"The prune step runs warehouse SQL on every invocation regardless of `safety.mode`. To skip the prune layer entirely, set `prune.enabled: false` in `signalforge.yml`."*
- **`docs/prune-ops.md`** § "Configuration: signalforge.yml prune: block" documents the new `enabled: bool = true` field next to `scope`, including the trade-off (signal-over-volume) and the operator-visible effect on the diff (every candidate appears as `kept-without-evidence` with `why="prune disabled in signalforge.yml"`).
- **`README.md`** § "Quick start" adds a one-line callout near the existing `safety.mode: sample` reference (currently line 200): *"Note: the prune step runs warehouse SQL on every invocation regardless of `safety.mode`. To skip prune entirely, set `prune.enabled: false` in `signalforge.yml`."*

**Done when:** all four docs ship the reframe, all internal links resolve, and the prose passes a manual scan against the ticket's acceptance criteria (1) and (2).

**Files:**
- `docs/safety-ops.md`
- `docs/cli-ops.md`
- `docs/prune-ops.md`
- `README.md`

**Depends on:** US-001 (so the field exists when documented), US-002 (so the `why` text is locked).

**TDD:** docs are not unit-testable; the rules-compliance gate in the Quality Gate story re-reads them against the issue body acceptance criteria.

---

### US-005 — Pin the disabled-prune short-circuit with a unit test

**Description:** Add a focused test in `tests/prune/test_engine.py` that verifies the disabled-prune branch: no adapter context-manager entry, no warehouse calls, no LLM calls, one `PruneEvent` per candidate in the JSONL, every decision is `kept-without-evidence` with `why="prune disabled in signalforge.yml"`. Also pin the `why` string as a stability assertion so a future maintainer who renames it sees the test break loudly.

**Traces to:** DEC-001, DEC-002, DEC-003.

**Acceptance criteria:**
- Test name: `test_prune_tests_short_circuits_when_enabled_false`.
- Fake adapter is constructed but receives ZERO method calls beyond its `__enter__` / `__exit__` — assertions: `fake.assert_all_expectations_met()` passes with an empty expectation set, AND a sentinel assertion verifies the `with adapter:` block was NOT entered. The simplest realisation: the fake's `__enter__` raises (or sets a flag), and the test asserts the flag stays unset.
- `prune_tests` returns a `PruneResult` with `len(decisions) == candidate_count`, every decision has `reason="kept-without-evidence"` and `why=="prune disabled in signalforge.yml"` (verbatim — DEC-003 stability gate).
- The audit JSONL at `<tmp_path>/.signalforge/prune.jsonl` has exactly `candidate_count` lines, each validates as a `PruneEvent` with the same `reason` and `why`.
- A second test (`test_prune_tests_disabled_does_not_validate_trusted_models`) constructs a `PruneConfig` with `enabled=False` AND `trusted_models=("model.proj.nonexistent",)` and asserts `prune_tests` returns successfully WITHOUT raising `PruneTrustedModelNotFoundError`. This pins DEC-002.

**Done when:** the two tests pass, and they would fail if any of the DEC-001/002/003 invariants regressed.

**Files:**
- `tests/prune/test_engine.py` — add the two tests.
- `tests/prune/fixtures` or inline factory helpers — minimal `Model` + `Manifest` + `CandidateSchema` with 2-3 candidates is enough.

**Depends on:** US-002.

**TDD:**
- `test_prune_tests_short_circuits_when_enabled_false`
- `test_prune_tests_disabled_does_not_validate_trusted_models`

---

### US-006 — Update CLAUDE.md public-API surface + `.claude/rules/prune-engine.md`

**Description:** Reflect the new `prune.enabled` field in the two project-orientation surfaces a future maintainer reads first:

- **CLAUDE.md** § "Public API surface (v0.1 + v0.2 additions)" — extend the `PruneConfig` line under the v0.2 additions block with `enabled: bool = True`.
- **`.claude/rules/prune-engine.md`** — under "v0.2 reservations / additions (issue #22)", add a new bullet documenting `PruneConfig.enabled` and pointing to this plan's DEC-001 through DEC-004. The bullet sits alongside the existing `sample_strategy` bullet.

**Traces to:** `prune-engine.md` 5-surface parity rule (lines 386-394 of the rule file).

**Acceptance criteria:**
- CLAUDE.md mentions `PruneConfig.enabled: bool = True` in the v0.2 additions surface.
- `.claude/rules/prune-engine.md` documents the field in the same style as `sample_strategy`: one short paragraph naming the field, its default, the conservative-bias routing it triggers (DEC-001), and the locked `why` text (DEC-003).
- The 5-surface parity rule's mapping is honoured: rule file ✓, ops doc (US-002) ✓, CLAUDE.md ✓, test (US-005) ✓, DEC (this plan) ✓.

**Done when:** both files ship the edits, and a manual scan confirms the five surfaces describe the same contract.

**Files:**
- `CLAUDE.md`
- `.claude/rules/prune-engine.md`

**Depends on:** US-001, US-002, US-003, US-004, US-005.

**TDD:** rule files are not unit-testable; the Quality Gate story re-reads them for cross-surface consistency.

---

### US-007 — Quality Gate: code-review x4 + CodeRabbit + validation

**Description:** Run the `code-review` skill four times over the full changeset, fixing all real bugs found each pass. Run CodeRabbit review if available. Run the canonical validation command and ensure it passes after every fix. Verify cross-surface consistency between the five surfaces (rule, ops doc, CLAUDE.md, test, DEC) per `prune-engine.md`'s 5-surface parity rule.

**Traces to:** project Quality Gate convention.

**Acceptance criteria:**
- Four passes of `code-review` complete; every real bug surfaced is fixed; cosmetic findings are noted but may be deferred.
- CodeRabbit review runs (if a draft PR exists by this point); flagged issues are addressed or explicitly accepted.
- `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest` passes from a clean working tree.
- The grep gate `tests/llm/test_logger_grep_gate.py` is green (no f-string in any new `_LOGGER` call).
- A final manual cross-surface check: the four prose surfaces (`docs/safety-ops.md`, `docs/cli-ops.md`, `docs/prune-ops.md`, `README.md`) and the two rule surfaces (`CLAUDE.md`, `.claude/rules/prune-engine.md`) all describe the same contract — `prune.enabled` semantics, the locked `why` string, the INFO emission shape, and the conservative-bias routing rationale.

**Done when:** four review passes done, all bugs fixed, validation green, cross-surface check passes.

**Files:** whatever the review surfaces.

**Depends on:** US-001 through US-006.

---

### US-008 — Patterns & Memory: doc the conservative-bias routing template

**Description:** This ticket establishes a third conservative-bias routing path (after materialisation-failure and budget-exceeded). The pattern — "any new 'we chose not to / couldn't gather warehouse evidence' branch routes every candidate to `kept-without-evidence` with a typed `why`, preserves the fail-closed audit, and emits a single one-line operator signal" — should be lifted from this ticket and the materialisation-failed ticket (#22) into the rule's prose so a future v0.3 contributor sees the template before re-inventing it.

**Traces to:** `prune-engine.md` Conservative drop-reason taxonomy (DEC-006 / DEC-011) section.

**Acceptance criteria:**
- `.claude/rules/prune-engine.md` § "Conservative drop-reason taxonomy" gains a paragraph naming three sources of `kept-without-evidence` (budget exhaustion, warehouse-error path, operator-chosen disable), explicitly noting that "we chose not to evaluate" (the new path) shares the same routing as "we couldn't evaluate" (the existing paths) — both preserve the fail-closed audit invariant.
- The paragraph cross-references this plan and #22's plan as precedents.

**Done when:** the rule file edit lands; a future maintainer adding a v0.3 "operator-chosen skip" branch (e.g., `--no-grade`) has a precedent to mirror.

**Files:**
- `.claude/rules/prune-engine.md`

**Depends on:** US-007 (so the codebase reflects the finished pattern before the rule prose is locked).

---

## Verification (end-to-end)

After all eight stories ship:

1. **Default behaviour preserved.** From a clean clone:
   ```bash
   pip install -e ".[dev]"
   pytest  # all existing tests pass, default prune.enabled=True path unchanged
   ```

2. **Disabled-prune in-process smoke test:**
   ```bash
   pytest tests/prune/test_engine.py::test_prune_tests_short_circuits_when_enabled_false -v
   pytest tests/cli/test_generate.py::test_cmd_generate_emits_info_when_prune_disabled -v
   ```

3. **Cross-surface scan.** Manually `grep -rn "prune\.enabled\|prune_disabled\|prune disabled in signalforge.yml" docs/ README.md CLAUDE.md .claude/rules/` and confirm every reference describes the same contract.

4. **Live disabled-prune dry-run (optional, requires `SF_RUN_BQ=1` + `ANTHROPIC_API_KEY` + `GOOGLE_CLOUD_PROJECT`):**
   ```bash
   # In a project with prune.enabled: false set in signalforge.yml
   signalforge generate models/staging/stg_bikeshare_trips.sql --project-dir tests/fixtures/dbt_project_austin
   # Expect: INFO line about prune disabled; no BQ jobs visible in console; .signalforge/diff.json shows every candidate as kept-without-evidence
   ```

---

## Beads Manifest

- **Epic ID:** `bd_1-scaffolding-yxl`
- **PR (plan):** [#64](https://github.com/wjduenow/SignalForge/pull/64) (draft, base `dev`)
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/35-prune-enabled-doc-reframe`

| Story | Beads ID | Depends on |
|---|---|---|
| US-001 — PruneConfig.enabled field | `bd_1-scaffolding-yxl.1` | none (ready) |
| US-002 — engine short-circuit | `bd_1-scaffolding-yxl.2` | US-001 |
| US-003 — CLI INFO emission | `bd_1-scaffolding-yxl.3` | US-002 |
| US-004 — docs reframe | `bd_1-scaffolding-yxl.4` | US-002 |
| US-005 — engine short-circuit tests | `bd_1-scaffolding-yxl.5` | US-002 |
| US-006 — CLAUDE.md + rule file | `bd_1-scaffolding-yxl.6` | US-005 |
| US-007 — Quality Gate | `bd_1-scaffolding-yxl.7` | US-001…US-006 |
| US-008 — Patterns & Memory | `bd_1-scaffolding-yxl.8` | US-007 |

**Ready queue at devolve:** US-001 (the epic itself surfaces as ready too — it's the umbrella, not a unit of work).
