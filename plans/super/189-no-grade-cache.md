# Super Plan — #189: `--no-grade` flag + persistent grade cache for fast iteration

## Meta

- **Ticket:** [#189](https://github.com/wjduenow/SignalForge/issues/189) — *cli: `--no-grade` flag + persistent grade cache for fast iteration*
- **Base branch:** `dev` (0.6.0.dev0). PRs target `dev`.
- **Worktree:** `../worktrees/SignalForge/189-no-grade-cache`
- **Branch:** `feature/189-no-grade-cache`
- **Phase:** devolved
- **Sessions:** 1 (2026-06-03)

### Beads Manifest

- **Epic:** `bd_1-scaffolding-63t` — #189: --no-grade flag + persistent grade cache
- **PR:** [#196](https://github.com/wjduenow/SignalForge/pull/196) (draft, base `dev`)
- **Worktree:** `../worktrees/SignalForge/189-no-grade-cache` (`feature/189-no-grade-cache`)

| Bead | Story | Depends on | Ready at devolve |
|---|---|---|---|
| `bd_1-scaffolding-63t.1` | US-001 — GradeEvent.audit_schema_version: Literal→int (prereq) | — | ✅ ready |
| `bd_1-scaffolding-63t.2` | US-002 — GradeEvent.cache_hit + `_build_grade_event` kwarg + v2 fixture | .1 | blocked |
| `bd_1-scaffolding-63t.3` | US-003 — signalforge.grade.cache module: keys, record, I/O | — | ✅ ready |
| `bd_1-scaffolding-63t.4` | US-004 — Cache typed errors + exit-code registration | — | ✅ ready |
| `bd_1-scaffolding-63t.5` | US-005 — GradeConfig.cache_enabled knob | — | ✅ ready |
| `bd_1-scaffolding-63t.6` | US-010 — Cost-rollup cache-hit zero-cost test | .2 | blocked |
| `bd_1-scaffolding-63t.7` | US-006 — Wire cache into the grade engine | .2, .3, .5 | blocked |
| `bd_1-scaffolding-63t.8` | US-008 — `signalforge cache clear --grade` subcommand | .3, .4 | blocked |
| `bd_1-scaffolding-63t.9` | US-007 — `--no-grade` + `--no-cache` + `[N/4]` progress | .7 | blocked |
| `bd_1-scaffolding-63t.10` | US-009 — Docs + SKILL.md + 5-surface parity test | .8, .9 | blocked |
| `bd_1-scaffolding-63t.11` | Quality Gate — code review × 4 + CodeRabbit | .1–.10 | blocked |
| `bd_1-scaffolding-63t.12` | Patterns & Memory | .11 | blocked |

*Note on numbering:* `bd` assigns child suffixes in creation order, so US-010 lands as `.6` (created before US-006 to keep its dep on `.2` valid) and US-006 lands as `.7`. The README order in the plan body matches story-naming, not bead-numbering.
- **Composes with:** #186 (grade asyncio parallelism — already shipped), #187 (Haiku/per-provider fast defaults — already shipped, Sonnet stayed the Anthropic default).

### Two parts, two scopes (operator-facing motivation from the ticket)

| Part | Surface | Wall-time win | Failure shape |
|---|---|---|---|
| 1. `--no-grade` | CLI flag on `signalforge generate` | ~340s → ~50s/model (one-shot prototyping skip) | None — operator-chosen bypass; exits 0 |
| 2. Persistent grade cache | `.signalforge/grade-cache/` + GradeEvent.`cache_hit` field | ~50s grade → ~5s grade (re-run after one-column edit) | Fail-soft (miss → re-grade) — derived state, not audit |

Per the ticket's composition table, both parts compose **multiplicatively** with the already-shipped #186 + #187 wins. `--no-grade` is the operator-facing escape valve; the cache is the multi-iteration amortiser.

---

## Discovery

### Ticket summary

Two operator UX wins for the grade-heavy iteration loop. The current pipeline pays full grade cost (~280s for ~70 artifacts × 4 criteria on a typical model) on every `signalforge generate` re-run, including the common case where the operator just tweaked one column description. The ticket proposes:

1. **`--no-grade`** — boolean flag on `signalforge generate` that skips the grade stage entirely. Drafter + safety + prune + diff still run; `grading_report=None` flows through to `render_diff` (already supported per `diff-renderer.md` § "Tier classification with no-grading-report degrade" — the `flagged` tier already fires only when `grading_report is not None`). Sidecar effects: `diff.json` still produced (kept/kept-uncertain/dropped only); `grade.json` NOT produced; `grade.jsonl` empty.

2. **Persistent grade cache** — content-addressed cache under `.signalforge/grade-cache/`. Cache lookup runs BEFORE the LLM call (and before the #186 asyncio dispatch). Cache miss → run grade, write entry. Cache hit → skip LLM call, return cached `GradingResult`, still write a `GradeEvent` to `grade.jsonl` with a new `cache_hit: true` field (audit completeness invariant survives).

### Codebase findings (origin/dev, 0.6.0.dev0)

**`cmd_generate` orchestration shape (`src/signalforge/cli/generate.py`):**
- The grade stage block sits between prune and diff at lines 1061–1083: `emit_progress_entry(4, "grade", …)` → `grade_artifacts(model, candidate, prune_result, config=grade_config, project_dir=project_dir)` → `emit_progress_done(4, "grade", …)`.
- The current pipeline is **5 stages** (safety, draft, prune, grade, diff). `--no-grade` removes one, so the diff progress line would re-number to `[4/4]` or stay `[5/5]` with a "skipped" entry — a UX decision (see Refinement).
- `render_diff(...)` at lines 1121–1130 already accepts `grading_report=None` (signature at `src/signalforge/diff/engine.py:803`). DEC-002 boundary checks at lines 919–920 gate the mismatch error only when `grading_report is not None` — already correct.
- No `--no-grade` / skip-stage precedent exists. `--dry-run` skips writes (not stages); `prune_existing` skips draft by reading from a file. The closest argparse pattern is the standalone `--no-color` flag.

**Grade orchestrator + audit seam (`src/signalforge/grade/`):**
- `grade_artifacts(model, candidate, prune_result, *, rubric=None, config=None, audit_path=None, sidecar_path=None, client=None, project_dir=None) -> GradingReport` (engine.py:818-829).
- Per #186, the orchestrator is sync prefix → `asyncio.run(_grade_artifacts_async_core(...))` → sync suffix. **Cache lookups belong in the sync prefix**, BEFORE the TaskGroup dispatch. Cache hits resolve before any semaphore acquisition.
- Per-pair audit write via `audit.write_grade_event(event, audit_path=...)` after parsing (engine.py per #186 shape: shielded `loop.run_in_executor(...)`).
- `_build_grade_event` (audit.py:82-138) is the SOLE construction site for `GradeEvent` — enforced by the 6th AST scan in `tests/test_audit_completeness.py`. Any cache-rehydration path that constructs `GradeEvent` must either live inside `signalforge.grade.audit` OR go through `_build_grade_event` with cache-hit fields injected (the latter is the clean choice).

**Hash recipes already on `GradeEvent` (DEC-010 of grade-layer.md):**
- `rubric_hash` — blake2b-8 of canonical rubric JSON (constant per run).
- `prompt_version_template` — blake2b-8 of `_SYSTEM_PROMPT + rubric block + envelope tags` (constant per run).
- **`criterion_prompt_hash`** — `blake2b-8(criterion.id + "\x00" + criterion.criterion + "\x00" + envelope_tags)`. **Per-criterion, stable across artifacts.** NUL-byte separator prevents id/text concat collisions.
- **`response_text_hash`** — blake2b-8 of the raw LLM **response** text. Empty-string sentinel on degraded path.
- `args_hash` — only when `artifact_id` collisions need disambiguation.

**Critical clarification on the cache-key shape (resolved during scout):**
The ticket says the key is `blake2b-8(criterion_id + "|" + response_text_hash)`. But `response_text_hash` per the GradeEvent surface is the hash of the **LLM's emitted response**, not the input. A cache LOOKUP must be derived from what we'd SEND, not what we'd RECEIVE. The natural input-side hash composition is:

```python
cache_key = blake2b-8(
    criterion_prompt_hash + "\x00" +
    artifact_text_hash    + "\x00" +
    model_id              + "\x00" +
    prompt_version_template
)
```

Where `artifact_text_hash = blake2b-8(extract_artifact_text(candidate, artifact_id))`. This composition cleanly invalidates on:
- criterion text change (rotates `criterion_prompt_hash`)
- artifact text change (rotates `artifact_text_hash`)
- grade model change (different `model_id`)
- system-prompt / rubric-list change (rotates `prompt_version_template`)

This is the conservative, defensible recipe. The ticket-body recipe (`response_text_hash` of a non-existent prior response) doesn't work for lookup. The Refinement phase will lock the recipe.

**Drift detector + audit_schema_version (DEC-010 of #6 / DEC-014 of #4):**
- `tests/grade/test_drift_detector.py` ships `StrictGradeEvent(extra="forbid")` paired with `tests/fixtures/grade/grade_event_v1.jsonl`. Adding `cache_hit: bool = False` triggers:
  1. Update `StrictGradeEvent` to include `cache_hit`.
  2. Bump `audit_schema_version: Literal[1] → Literal[2]` (field stays `int`-typed for replay).
  3. Refresh fixture (or add v2 fixture alongside v1) showing both shapes.

**Diff layer (already correct):**
`render_diff(grading_report=None, …)` produces a diff with kept/kept-uncertain/dropped only (no `flagged`). Already supported — no diff-layer change needed for Part 1. Quote per `diff-renderer.md`: "`flagged` only fires when `grading_report is not None`. A prune-only run never gets surprise `flagged` rows."

**Cost rollup (`src/signalforge/llm/cost/_rollup.py`):**
- Walks BOTH `llm_responses.jsonl` AND `grade.jsonl` (lines 444–448), each independent-optional.
- **`--no-grade`:** empty / absent `grade.jsonl` → rollup processes only drafter — already handled.
- **Cache hits:** still produce a `GradeEvent` record with token-count fields. A cache-hit record has zero LLM input/output tokens (we didn't call the LLM); the rollup arithmetic multiplies each token field by price, so a cache-hit row contributes $0. **No rollup change needed** — verify with a test fixture, though.

**Parity surfaces (cli-layer.md § "Multi-surface parity" + skill-parity.md):**
- A new CLI flag touches **six** surfaces: (1) argparse help string, (2) handler/helper docstring, (3) `docs/cli-ops.md`, (4) test name, (5) test docstring + the DEC, (6) `src/signalforge/skills/signalforge/SKILL.md`.
- The `tests/cli/test_skill_cli_parity.py` gate auto-grows for new subcommand names (it iterates `_build_parser()`) but does NOT auto-grow for FLAGS — flag mentions on SKILL.md are pinned only by reviewer attention + the bespoke 5-surface parity test per testing-signal.md DEC-017 of #37.
- Precedent: `tests/cli/test_5_surface_parity_select.py` reads three selector examples from five surfaces (help string, plan, ops doc, README — adapted; this ticket would target SKILL.md as the 5th explicit surface) and asserts byte-exact substring match.

### Rule constraints that bind this work

Distilled from a full sweep of `.claude/rules/*.md` (no `workflow-project.md` exists).

**For Part 1 (`--no-grade`):**
- **`cli-layer.md` § "Multi-surface parity":** 6 surfaces, all in same commit. Surfaces 3 + 5 are most often forgotten.
- **`cli-layer.md` § "Four-tier exit codes":** Skipping a stage at operator choice does NOT introduce a new typed error or new exit-code tier. Exits 0 on success.
- **`cli-layer.md` § "Progress to stderr UX":** "`cmd_generate` emits one stderr progress line per stage entry plus a paired `done in <X>` line at exit." Skipped stages have a UX decision attached (omit vs. label as skipped — see Refinement). TTY-gated.
- **`cli-layer.md` § "Multi-source CLI commands degrade on supplementary failures":** Does **not** apply — this is operator-chosen architectural bypass, not failure handling.
- **`skill-parity.md`:** Parity gate auto-grows for new subcommand names (not flags); the bespoke 5-surface parity test is the gate for flag-name lockstep.
- **`testing-signal.md` § "5-surface parity test pattern (DEC-017)":** For any new flag whose grammar/examples appear across multiple surfaces, ship a bespoke parity test that reads each surface and asserts the same example tokens appear verbatim.

**For Part 2 (grade cache):**
- **`grade-layer.md` § "Reproducibility hash fields" (DEC-010, DEC-019):** Five fingerprints; cache key composition uses `criterion_prompt_hash` (already on the event) + a NEW `artifact_text_hash` (derived from `extract_artifact_text(candidate, artifact_id)`) + `model_id`.
- **`grade-layer.md` § "Conservative score-and-degrade taxonomy (DEC-002, DEC-015)":** Degraded results (`score=None`) MUST NOT be cached. Cache writes are post-score, post-degrade-check; only `score is not None` results land in the cache.
- **`grade-layer.md` § "Fail-closed JSONL + sidecar JSON" (DEC-006, DEC-012):** The grade `.jsonl` audit + `grade.json` sidecar STAY fail-closed. The cache is a **derived/optional state**, NOT an audit, so the cache writer is **fail-soft**: write failure → WARNING + skip the write (next run re-grades and re-writes). This is the inverse of the audit posture and the design choice mirrors `warehouse-adapters.md` § "Cleanup-boundary fail-soft pattern".
- **`grade-layer.md` § "Single GradeEvent construction seam (DEC-029, sixth AST scan)":** Cache-hit `GradeEvent`s must still flow through `_build_grade_event` (with `cache_hit=True` passed in) — no new construction sites needed; the AST scan stays unchanged.
- **`grade-layer.md` § "Drift detectors mandatory" (DEC-010 of #6):** Adding `cache_hit: bool = False` to `GradeEvent` → update `StrictGradeEvent` + fixture + bump `audit_schema_version` 1 → 2.
- **`grade-layer.md` § "ANSI-safe lazy-format logger" (DEC-029):** Cache hit/miss log lines use `_LOGGER.info("cache hit", extra=…)` shape with positional `%s` + `json.dumps({...})` (NOT f-strings); per the grep gate at `tests/llm/test_logger_grep_gate.py`.
- **`grade-layer.md` § "Symlink-hardened path canonicalisation":** `.signalforge/grade-cache/` MUST canonicalise at orchestrator entry via `signalforge._common.path_safety.canonicalise_path`. Failures wrap as `GradeAuditWriteError` (or a new `GradeCacheError`).
- **`grade-layer.md` § "`signalforge.yml` top-level namespace: `grade:`":** Cache knobs live under `grade:` (NOT a new `grade_cache:` block). Provisional knobs: `grade.cache_enabled: bool = True`, maybe `grade.cache_ttl_seconds: int | None = None` (None = no TTL).
- **`grade-layer.md` § "One LLM call per (artifact × criterion); parallel via asyncio":** Cache lookup runs **synchronously, in the sync prefix**, BEFORE the TaskGroup. Cache hits do NOT enter the semaphore; cache misses do.
- **`safety-layer.md` § "AuditEvent reproducibility fields" (DEC-014):** `audit_schema_version: int` (not `Literal[N]`) so audit replay survives version bumps. The 1 → 2 bump here mirrors the safety layer's 1→2→3→4 trajectory.

**Cross-cutting:**
- **`testing-signal.md` § "AST single-construction-seam scans":** 12 scans as of #186; the 6th covers `GradeEvent`. No new scan needed if the cache writer/reader routes through `_build_grade_event`.
- **`testing-signal.md` § "No `assert True`-shaped tests":** Cache tests must assert HIT/MISS behaviour distinctly.
- **`testing-signal.md` § "Dispatch-order-agnostic assertions under asyncio (#186)":** When mixing cache hits + async misses, audit JSONL arrival order is non-deterministic. Use `tests/grade/_helpers.py::_sort_grade_events` for snapshot tests.
- **`python-build.md` § "Shipping package data":** Cache is operator-side (`.signalforge/grade-cache/` under project dir) — not packaged. No wheel-build change.
- **`docs-publishing.md`:** `docs/cli-ops.md` + `docs/grade-ops.md` updates ride the docs site automatically (push to main).

---

## Scoping answers (Phase 1 → Phase 2)

1. **Cache-key composition** — **Four-part input hash:** `blake2b-8(criterion_prompt_hash + \x00 + artifact_text_hash + \x00 + model_id + \x00 + prompt_version_template)`. Invalidates cleanly on criterion-text change, artifact-text change, grade-model swap, and system-prompt/rubric-shape change. `artifact_text_hash` is NEW (computed from `extract_artifact_text(candidate, artifact_id)` per `grade-layer.md` § `_artifact_id_for`).
2. **Cache-write posture** — **Fail-soft + WARNING.** Mirrors the cleanup-boundary fail-soft pattern (`warehouse-adapters.md` § "Cleanup-boundary fail-soft"); inverse of the audit fail-closed contract. One WARNING line on write failure; live grade still succeeds; run exits 0.
3. **Degrade caching** — **Never cache `score=None`.** Per `grade-layer.md` DEC-015, degraded results signal "could not positively evaluate"; caching that record silently replays the failure forever. Cache writes are post-score, gated on `score is not None`.
4. **Skipped-stage progress UX** — **Re-number to `[N/4]`.** When `--no-grade` is set, the pipeline is genuinely four stages — honest count. No "grade: skipped" line. The orchestrator decides once at startup and threads the resolved `total` through `emit_progress_entry/done(stage_n, name, body, *, total=...)`.
5. **Cache lifecycle scope** — **Ship `signalforge cache clear --grade` subcommand in this issue.** Adds one CLI subcommand (auto-picked-up by the skill-parity gate per `skill-parity.md`), plus operator-facing `rm -rf` is always available as a manual escape. NO TTL knob in this ticket (content-addressed key handles invalidation; TTL is a follow-up if demand emerges).
6. **PR scope** — **One PR against `dev`.** Both parts share the `audit_schema_version` 1 → 2 bump; `--no-grade` ships a value-now win even before the cache amortises multi-iteration sessions. Estimated ~8 stories + Quality Gate + Patterns & Memory.

### Locked invariants flowing into Phase 2

- **Cache file shape:** one small JSON file per entry under `.signalforge/grade-cache/<8-hex-prefix>/<full-64-hex>.json` (two-level sharding) OR flat `.signalforge/grade-cache/<full-64-hex>.json` — Architecture Review decides shape (flat works for ~280 entries; sharded scales to millions if the cache survives across many projects/runs).
- **Cache record content:** the serialised `GradingResult` (score / passed / evidence / reasoning / artifact_id / criterion_id) plus the input hashes used to derive the key (for forensic verification). Reproducibility hashes from the original write also travel so a cache hit reconstructs a valid `GradeEvent` with the original `rubric_hash` / `prompt_version_template` / `criterion_prompt_hash` / `response_text_hash`.
- **Cache audit invariant:** cache hits still write a `GradeEvent` to `grade.jsonl` via `_build_grade_event(...)`. New field `cache_hit: bool = False` on `GradeEvent`. `audit_schema_version: Literal[1] → Literal[2]` (typed `int` for replay survival per `safety-layer.md` DEC-014).
- **Cost rollup behaviour:** a cache-hit `GradeEvent` carries zero `input_tokens` / `output_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` (we did not call the LLM); the rollup multiplies by price → $0 contribution. Pin with a fixture test.
- **Concurrency:** cache lookup runs synchronously in `_grade_artifacts_async_core`'s sync prefix, BEFORE the `asyncio.TaskGroup` dispatch. Cache hits do not enter the semaphore. The asyncio path under #186 only dispatches misses.
- **`signalforge cache clear --grade`:** new subcommand under `signalforge.cli.cache` (one module, one handler). Subgroup-style: `cache clear` (mirrors how `git remote add` / `git remote remove` shape an internal namespace). Future siblings: `cache clear --drafter` (clears `.signalforge/llm_responses.jsonl`?) — out of scope here; subcommand starts with one `--grade` flag.

---

## Architecture Review

Four parallel reviews (Security / Performance / Data Model / Testing / API+CLI Design). Findings rated `pass` / `concern` / `blocker`. Blockers MUST be resolved before Phase 3.

### Summary table

| Area | Verdict | Headline |
|---|---|---|
| Security | concern (2 blockers buried) | Cache record size unbounded; cache key needs `provider` axis; PII / 0o600 / cache poisoning concerns |
| Performance | pass (1 concern) | Wall-time win is real but smaller than ticket suggests post-#186 (~10–20s/model, not 290s) |
| Data model | blocker | `GradeEvent.audit_schema_version` is currently `Literal[1]`, NOT `int` — must change to `int` BEFORE the 1→2 bump, else v1 fixture round-trip breaks |
| Testing | concern | DEC for `--no-grade` grammar must be locked in Phase 3 before parity test can be written; logger lazy-format gate auto-enforces but design must be explicit |
| API + CLI design | concern | Nested `cache clear` deviates from cli-layer.md DEC-009 (documented exception); `--no-cache` per-run bypass is NOT covered by the plan today |

### Blocker 1 — `GradeEvent.audit_schema_version` is `Literal[1]`, not `int` (Data Model)

Per `safety-layer.md` DEC-014, audit-event schema-version fields are typed `int` (not `Literal[N]`) so older JSONL records round-trip across version bumps. The current `src/signalforge/grade/models.py:292` declares `audit_schema_version: Literal[1] = 1`. Bumping to `Literal[2]` would fail Pydantic validation against the v1 fixture (`extra="ignore"` doesn't help — it governs unknown FIELDS, not field-value validation).

**Fix:** First story of the implementation flips `audit_schema_version: Literal[1] = 1` → `int = 1` on the production model. `StrictGradeEvent(extra="forbid")` keeps `Literal[1]` (it pins the v1 fixture shape). Then the cache_hit story bumps the production default to `2` and adds a v2 fixture; `Strict<v2>` mirror documents the v2 shape; v1 fixture stays as a replay anchor.

### Blocker 2 — Cache record size cap (Security)

LLM-emitted `evidence` + `reasoning` are arbitrary-length. Without a size cap, a single pathological response could plant a cache file of unbounded size (and successive cache writes fill disk). Mirrors the existing audit-cap precedent (`_GRADE_AUDIT_RECORD_LIMIT_BYTES = 4000`, `_GRADE_SIDECAR_RECORD_LIMIT_BYTES = 1_000_000`).

**Fix:** `_GRADE_CACHE_RECORD_LIMIT_BYTES = 16_000` (4× the per-pair audit cap; comfortable headroom for ~10KB worst-case `reasoning`). Check at write time BEFORE `os.open`. Oversize → fail-soft skip with one WARNING (mirroring DEC-002).

### Blocker 3 — `provider` MUST be in the cache key (Security)

The four-part recipe locked in Phase 1 uses `model_id`, but per `grade-layer.md`/`llm-drafter.md` the provider is registry-validated (`anthropic` / `openai` / `gemini`) and the SKU set is plugin-growable. Two providers could theoretically expose the same SKU string. Worse: a custom plugin provider could reuse `claude-sonnet-4-6` as a label. Provider is the load-bearing scope.

**Fix:** Extend the recipe to FIVE parts: `blake2b-8(criterion_prompt_hash + \x00 + artifact_text_hash + \x00 + provider + \x00 + model_id + \x00 + prompt_version_template)`. Same defence shape as the rest of the codebase's "provider+model" pairing (e.g. `PROVIDER_DEFAULT_MODELS`).

### Concern 1 — `--no-grade` wall-time win is smaller post-#186 (Performance)

Ticket headline: 340s → 50s per model (~290s win). Post-#186 actual: grade phase is ~30s wall-time concurrent (was ~280s sequential), so `--no-grade` saves ~10–30s per model in 0.6.0.dev0. **Still a real iteration-loop win** — at ~50 re-runs/session this is ~10–25min saved — but the headline math conflates pre-#186 sequential time with post-#186 wall-time.

**Fix:** Calibrate the ticket-body framing in the eventual CHANGELOG / release notes. Don't mislead operators with the 290s figure. Architecturally — no change.

### Concern 2 — Nested `cache clear` deviates from `cli-layer.md` DEC-009 (CLI Design)

`cli-layer.md` § "Subpackage layout" mandates flat-per-subcommand modules (`signalforge.cli.<name>.py`). Phase 1's answer #5 chose `signalforge cache clear --grade` (nested subaction), justified by forward-compat for a future `cache clear --drafter`. This breaks the flat convention by ONE precedent.

**Fix:** Add a DEC documenting the exception. The alternative shapes (hyphenated flat `cache-clear`, or verb-first `clear-grade-cache`) lose the namespace claim and the `git remote add / remove` idiom. Plan stays with nested; the deviation is documented.

### Concern 3 — `--no-cache` per-run flag is NOT covered (CLI Design)

The plan covers config-file `grade.cache_enabled: bool = True` but no per-run `--no-cache` flag on `signalforge generate`. Operators wanting to bypass cache for one run (debugging, calibration, after a manual fixture edit) would have to edit yaml or `rm -rf` the cache first.

**Decision needed (Refinement):** ship `--no-cache` alongside `--no-grade` (cheap, symmetric, one extra 5-surface parity entry) OR document the gap explicitly. Defaulting to **ship `--no-cache`**: same operator-UX win pattern, trivial implementation (one bool gate at cache-lookup entry).

### Concern 4 — PII / file mode / `cache clear` symlink safety (Security)

Three smaller items:
- **0o600 on cache files** — mirrors fail-closed writer convention (`safety-layer.md` DEC-011 et al.). Cache records may quote PII-bearing artifact text via `evidence`/`reasoning`.
- **`.gitignore` coverage** — `.signalforge/` is already blanket-ignored (line 29 of `.gitignore`). Cache files inherit. Pin with one test.
- **`cache clear --grade` symlink hardening** — canonicalise the resolved cache dir via `_common.path_safety.canonicalise_path` BEFORE `rm -rf`. Reject symlinks pointing outside the project tree. No `--confirm` flag (kept simple; the destruction is operator-explicit + scoped to one directory).

### Concern 5 — DEC for `--no-grade` grammar must be locked before parity test (Testing)

The 5-surface parity test (testing-signal.md DEC-017) keys on a DEC in the plan. Phase 3 will lock DEC-001..DEC-NNN explicitly; the `--no-grade` grammar DEC must be in the list before the test scans for it.

**Fix:** Phase 3 produces a complete DEC list. The first DEC is the `--no-grade` grammar lock.

### Resolved during review

- **AST scan count** — stays at 12 (the 6th covers GradeEvent; cache rehydration MUST route through `_build_grade_event(..., cache_hit=True, ...)` so no new construction site appears).
- **Cost-rollup behaviour** — cache-hit `GradeEvent`s with zero token counts contribute $0 (rollup arithmetic is trivially multiply-by-zero). Pin with one fixture test; no rollup code change.
- **Skill parity gate** — auto-grows for the new `cache` subcommand name; SKILL.md must mention `cache clear --grade` in same commit.
- **Logger lazy-format gate** — already scans `grade/`; new cache module under `signalforge.grade.cache` auto-enforces.
- **Disk I/O at scale** — flat directory is fine for ext4/btrfs/APFS at v0.6 scale (~280 entries typical, ~28K cumulative is operationally safe). Sharding is a future ticket if demand emerges.
- **Concurrent writes** — `O_CREAT | O_EXCL` first, fall back to skipping on `EEXIST` (cache is content-addressed, byte-identical) is the safe pattern; mirrors the fail-soft posture.
- **PII redaction surface** — cache files inherit the same redaction posture as `grade.jsonl` (LLM-emitted text may quote artifact descriptions, but column NAMES are already hashed by the safety layer before they reach the LLM, so cache files contain hashed column refs in the worst case). No new redaction work needed.

---

## Refinement Log

Every load-bearing decision lands here. Stories trace to these DECs in Phase 4.

### DEC-001 — `--no-grade` flag grammar locked

`signalforge generate <model> --no-grade` is the canonical invocation. Bare boolean flag (`action="store_true"`, no value). Mutex with NOTHING — `--no-grade --write`, `--no-grade --dry-run`, `--no-grade --mode sample`, `--no-grade --estimate` are all valid combinations. The flag's effect: in `cmd_generate`, the grade-stage block at `cli/generate.py:1061-1083` is wrapped in `if not args.no_grade:` and `grade_report` defaults to `None` (already supported by `render_diff`).

Rationale: matches the `--no-color` precedent (`cli/generate.py:432`); bare boolean flags are the codebase pattern. Locked verbatim so the 5-surface parity test can pin the literal token `--no-grade`.

### DEC-002 — `--no-cache` flag grammar locked

`signalforge generate <model> --no-cache` is the canonical invocation. Bare boolean. Mutex with nothing. Effect: bypasses both cache READ (no lookup) and cache WRITE (no entry written) for one run. Cache files from prior runs are NOT deleted — operator must use `cache clear --grade` or `rm -rf` for that.

**Precedence when both `--no-grade` and `--no-cache` are set:** `--no-grade` implicitly wins (no grade calls = no cache lookups/writes). The cache layer never sees either flag; the grade-stage bypass short-circuits upstream. Document the precedence in `docs/cli-ops.md`; no special argparse handling.

### DEC-003 — Progress UX re-numbers to `[N/4]` under `--no-grade`

When `args.no_grade` is set, `cmd_generate` calls `emit_progress_entry/done(stage_n, name, body, *, total=4)` for the four remaining stages (safety / draft / prune / diff). NO `[X/5] grade: skipped` line emitted. The orchestrator resolves `total` ONCE at startup based on `args.no_grade` and threads it through.

Implementation: `_helpers.emit_progress_entry(stage_n, name, body, *, total=5)` already takes `total` as a kwarg per its current signature; the callsite in `cmd_generate` computes `total = 4 if args.no_grade else 5` and passes it. Stage numbers are static (`1 safety / 2 draft / 3 prune / 4 grade / 5 diff` becomes `1 safety / 2 draft / 3 prune / 4 diff` when grade is skipped).

### DEC-004 — Five-part cache key composition

```python
cache_key = blake2b(
    criterion_prompt_hash    + "\x00" +  # already on GradeEvent
    artifact_text_hash       + "\x00" +  # NEW
    provider                 + "\x00" +  # NEW (security: prevents cross-provider SKU collision)
    model_id                 + "\x00" +
    prompt_version_template,             # already on GradeEvent
    digest_size=8
).hexdigest()  # 16-hex chars
```

`artifact_text_hash = blake2b(extract_artifact_text(candidate, artifact_id).encode("utf-8"), digest_size=8).hexdigest()`. NUL-byte separators prevent id/text concat collisions (mirrors `criterion_prompt_hash` recipe in `grade-layer.md` DEC-010).

`provider` is the registered provider name (`anthropic` / `openai` / `gemini`), NOT the resolved client class. A custom plugin provider gets its own scope.

Cache invalidation axes: criterion-text change, artifact-text change, provider swap, model swap, system-prompt/rubric-list change. Five clean axes; no implicit / time-based invalidation.

### DEC-005 — Cache write posture: fail-soft + WARNING

Cache writes are derived/optional state. Mirrors the cleanup-boundary fail-soft pattern (`warehouse-adapters.md` § "Cleanup-boundary fail-soft"). On write failure (OSError, ENOSPC, EACCES, oversize record): emit one WARNING line via lazy-format JSON, skip the write, let the live grade succeed. Next run re-grades and re-writes. Tier-3 typed error class `GradeCacheWriteError` exists in `signalforge.grade.errors` for catch-and-warn shape but never propagates from `grade_artifacts`.

Distinct from the FAIL-CLOSED audit writers (DEC-006 of #6) — cache failure must NOT abort the run.

### DEC-006 — `_GRADE_CACHE_RECORD_LIMIT_BYTES = 16_000`

Pre-write size cap. Mirrors the per-pair audit cap (`_GRADE_AUDIT_RECORD_LIMIT_BYTES = 4000`) but with 4× headroom for the `evidence`+`reasoning` worst-case (~10KB observed in real-world grade runs).

Check happens BEFORE `os.open` (no on-disk artefact on oversize). Oversize raises `GradeCacheRecordTooLargeError(size, limit, ...)` — a `GradeCacheWriteError` subclass — which `write_cache` catches per DEC-005 and routes to the fail-soft WARNING.

Locked at 16_000 (not 16_384 / 16_000 / 20_000) for the same reason 4000 is locked at the audit layer: easy operator math + comfortable headroom + bounded disk usage (~280 entries × 16KB max = ~4.5MB worst-case cache footprint per project).

### DEC-007 — Degraded results (`score=None`) never land in the cache

Per `grade-layer.md` § "Conservative score-and-degrade taxonomy" (DEC-015), `score=None` signals "could not positively evaluate." Caching that record would silently replay the failure on every re-run, preventing recovery from transient LLM/network blips.

Cache writes are gated on `result.score is not None`. Cache reads never construct a degraded `GradingResult` — a malformed cache file (e.g. an injected `score: null` poisoning attempt) → cache miss + WARNING.

### DEC-008 — `GradeEvent.audit_schema_version: int` (not `Literal`) — prerequisite for the 1→2 bump

`src/signalforge/grade/models.py:292` currently declares `audit_schema_version: Literal[1] = 1`. This is the wrong shape per `safety-layer.md` § "AuditEvent reproducibility fields" (DEC-014) — the field must be typed `int` so older audit JSONLs round-trip across version bumps.

**First story flips the type** (mechanical type-change only; no semantic change; `StrictGradeEvent` mirror stays `Literal[1]` because the v1 fixture still validates against it). **Cache-hit story then bumps the production default** to `2` and ships:
- A new `Strict<v2>` mirror documenting the v2 shape.
- A v2 fixture (`tests/fixtures/grade/grade_event_v2.jsonl`) populated with `cache_hit: true`.
- The v1 fixture stays as the replay anchor (`int` field accepts both `1` and `2`).

### DEC-009 — `GradeEvent.cache_hit: bool = False` field placement

Inserted into `GradeEvent` immediately after `response_text_hash` (line 306 in current `models.py`), BEFORE `model`. Keeps reproducibility hashes adjacent; cache_hit sits at the hash/token boundary as a sentinel. Default `False` so v1 records (without `cache_hit`) round-trip cleanly via `extra="ignore"` on the production model.

### DEC-010 — `_build_grade_event(..., cache_hit: bool = False, ...)` signature

The SOLE construction seam (per the 6th AST scan in `tests/test_audit_completeness.py`) gains a keyword-only `cache_hit` parameter, default `False`. Cache-hit dispatch from `signalforge.grade.cache._maybe_use_cached_result` ultimately calls `_build_grade_event(..., cache_hit=True, input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0, ...)`. The AST scan stays unchanged — no new construction site appears.

### DEC-011 — `CacheRecord` Pydantic model (flat duplication, not wrapping)

Public model in `signalforge.grade.cache`. Frozen, `extra="ignore"`, `populate_by_name=True`. Flat field set:

```python
class CacheRecord(BaseModel):
    cache_schema_version: int = 1  # future-proofed; reader degrades to miss on mismatch

    # Verdict body (mirrors GradingResult fields)
    artifact_id: str
    criterion_id: str
    score: float  # NEVER None per DEC-007
    passed: bool
    evidence: str = ""
    reasoning: str = ""

    # Forensic input hashes (verify cache file matches the key it lives under)
    criterion_prompt_hash: str
    artifact_text_hash: str
    provider: str
    model: str
    prompt_version_template: str

    # Output-side reproducibility (from original write — carried into reconstructed GradeEvent)
    response_text_hash: str
    rubric_hash: str

    # Original-write timestamp (forensic; the cache-hit GradeEvent gets current-run timestamp)
    original_timestamp: datetime
```

Flat duplication chosen over wrapping (`CacheRecord.result: GradingResult`) — operator UX (`jq '.score' cache.json`), one source of truth via Pydantic field unification, ~20 bytes saved per record. Paired with `Strict<v1>` mirror + a `tests/fixtures/grade/grade_cache_record_v1.json` fixture.

### DEC-012 — Cache file layout: flat `<project>/.signalforge/grade-cache/<16-hex>.json`, 0o600

`<project_dir>/.signalforge/grade-cache/<cache_key>.json` where `<cache_key>` is the 16-hex blake2b-8 digest from DEC-004. Single-level flat directory (sharding deferred to a future ticket if accumulated entries cross ~10K).

File mode 0o600 (owner-only read/write) at `os.open` — mirrors fail-closed writer convention (`safety-layer.md` DEC-011, et al.). `evidence`/`reasoning` may quote artifact text; 0o600 prevents world-readable exposure.

Cache directory creation: lazy `Path.mkdir(parents=True, exist_ok=True)` on first write per run. Read path tolerates missing directory (returns `None` for every lookup).

### DEC-013 — Cache lookup runs synchronously in the sync prefix, BEFORE asyncio TaskGroup

Per `grade-layer.md` § "One LLM call per (artifact × criterion); parallel via asyncio" (DEC-004 of #6, graduated by #186). Cache lookup folds into the existing sync-prefix iteration that already materialises `(artifact, criterion)` pairs (envelope-breach scan + `_iterate_artifacts` materialisation). Cache hits resolve into a `GradingResult` immediately; cache misses enter the `asyncio.TaskGroup` semaphore as today.

Concurrency interaction: cache hits do NOT acquire the `Semaphore(max_concurrent_calls)`. The semaphore caps in-flight LLM calls only.

### DEC-014 — Concurrent-write safety: O_CREAT | O_EXCL, EEXIST → skip

Cache writes use `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)`. On `FileExistsError` (concurrent re-run wrote first), the writer logs DEBUG "cache entry already present" and returns — content-addressed key guarantees byte-identical content, so the existing file is correct. No O_TRUNC fallback (would risk partial reads under concurrency).

Followed by `os.write` (looped on short returns) → `os.fsync` → `os.close`. Single `try/finally` for `os.close(fd)`. No `except` around write/fsync — but DEC-005 fail-soft posture means the orchestrator catches any escaping `OSError` from `write_cache` and routes to WARNING.

### DEC-015 — `signalforge cache clear --grade` subcommand

New top-level subcommand `cache` with one sub-action `clear` and one flag `--grade`. Module: `src/signalforge/cli/cache.py`. Handler: `cmd_cache(args)`.

**Documented deviation from `cli-layer.md` § "Subpackage layout — flat, per-subcommand modules":** the flat convention says one module per top-level subcommand. `cache` IS a top-level subcommand (so one module is correct), but it carries a nested `add_subparsers()` for sub-actions. This is a precedent break (the first nested subcommand in the codebase) justified by forward-compat for a future `cache clear --drafter` / `cache stats` / `cache list` family. Documented in `docs/cli-ops.md` § "Cache operations" + the cli-layer.md rule will graduate a "nested-subcommand precedent" note when this ships.

NO `--confirm` flag — destructive scope is bounded by `.signalforge/grade-cache/` and the operator typed `--grade` explicitly. Symlink-hardened delete: canonicalise the cache dir path via `_common.path_safety.canonicalise_path` BEFORE `shutil.rmtree`; reject if the resolved path escapes `<project_dir>/.signalforge/`. Exit 0 on success (including the idempotent "directory already absent" case).

Future siblings (`cache stats`, `cache list`) are out of scope.

### DEC-016 — `GradeConfig` adds two cache knobs

```python
class GradeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    # ... existing fields ...
    cache_enabled: bool = True
```

No `cache_ttl_seconds` knob (content-addressed key handles invalidation; TTL is a future ticket if demand emerges). `extra="forbid"` makes `cache_enable: false` (missing `d`) fail loud at config-load.

### DEC-017 — New typed errors in `signalforge.grade.errors`

Three new error classes, all subclass `GradeError`:

- `GradeCacheReadError(GradeError)` — cache file present but unreadable / unparseable. Tier 3 (external dependency, disk I/O). Default remediation: "Delete the cache file or run `signalforge cache clear --grade`."
- `GradeCacheWriteError(GradeError)` — write failure. Tier 3. **Never propagates** out of the engine (fail-soft per DEC-005); the class exists so the WARNING line names the failure type. `GradeCacheRecordTooLargeError(size: int, limit: int, ...)` is a subclass.
- `GradeCachePathError(GradeError)` — symlink containment violation (`.signalforge/grade-cache/` resolves outside `project_dir`). Tier 1 (load-time / parse-layer; operator config problem). Default remediation names the canonicalisation gate.

All three registered in `_EXCEPTION_TO_EXIT_CODE`. The 7th AST scan auto-grows for them. Scan count unchanged at 12.

### DEC-018 — `signalforge.grade.cache` module public surface

```python
# src/signalforge/grade/cache.py
__all__ = [
    "CacheRecord",
    "compute_cache_key",
    "lookup_cache",
    "write_cache",
    "clear_cache",
]

def compute_cache_key(
    *,
    criterion_prompt_hash: str,
    artifact_text_hash: str,
    provider: str,
    model: str,
    prompt_version_template: str,
) -> str: ...

def lookup_cache(cache_dir: Path, key: str) -> CacheRecord | None: ...
def write_cache(cache_dir: Path, key: str, record: CacheRecord) -> None: ...  # fail-soft per DEC-005
def clear_cache(cache_dir: Path) -> None: ...
```

Private helpers (`_cache_file_path`, `_check_size`, `_safe_open`) sit under `_`-prefixed names. The `cache_dir` parameter is the already-canonicalised path (the grade engine canonicalises at orchestrator entry per `grade-layer.md` § "Symlink-hardened path canonicalisation").

### DEC-019 — 5-surface parity test for `--no-grade` AND `--no-cache`

`tests/cli/test_5_surface_parity_no_grade.py`. Mirrors `tests/cli/test_5_surface_parity_select.py` shape. Asserts the literal tokens `--no-grade` AND `--no-cache` appear in each of the five surfaces (argparse help string, this plan's DEC-001/DEC-002, `docs/cli-ops.md`, the test's own docstring/example block, `src/signalforge/skills/signalforge/SKILL.md`). Substring match (no whitespace/case normalisation).

NOT a separate test per flag — one test file pinning both flags is the right granularity (each is one bool flag with no parameter grammar).

### DEC-020 — Cost-rollup test fixture for cache-hit records

`tests/llm/cost/test_rollup.py` gets one new test: `test_cost_rollup_treats_cache_hit_as_zero_cost`. Constructs a synthetic `GradeEvent` with `cache_hit=True` and all four token-count fields at 0, runs `rollup_audit_dir`, asserts the grade-USD contribution is exactly `0.0`. Pins the "zero-tokens-per-cache-hit" contract from DEC-010.

### Decisions deliberately deferred (out of scope here)

- **TTL knob.** No `grade.cache_ttl_seconds` in this ticket. Content-addressed key handles invalidation cleanly; TTL surfaces if pricing rotates or an operator demands it.
- **Sharded cache layout.** Flat `.signalforge/grade-cache/<16-hex>.json` is fine for v0.6 typical scale. Sharded `<key[:2]>/<key>.json` becomes useful at ~10K entries; ship later if demand emerges.
- **`cache stats` / `cache list` sub-actions.** Forward-compat is reserved by the nested subcommand shape (DEC-015). Implementation deferred.
- **Cache hit-rate telemetry.** No metric counter / log-line aggregation in this ticket beyond per-hit/miss DEBUG lines. Operators can `grep cache_hit .signalforge/grade.jsonl` post-run for now.

---

## Detailed Breakdown

Twelve beads — ten implementation stories, one Quality Gate, one Patterns & Memory.

**Canonical validation command (every story's AC ends with):**
```bash
uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
```

### US-001 — Type-fix prerequisite: `GradeEvent.audit_schema_version: int`

**Description.** Mechanical type-change: flip `audit_schema_version: Literal[1] = 1` to `audit_schema_version: int = 1` on the production `GradeEvent` model. Strict drift-mirror keeps `Literal[1]`. This unblocks the 1 → 2 bump in US-002 without breaking v1 fixture round-trip (Pydantic `Literal[2]` would reject the v1 fixture's `"audit_schema_version": 1`).

**Traces to.** DEC-008.

**Files.**
- `src/signalforge/grade/models.py` — line 292: `audit_schema_version: Literal[1] = 1` → `audit_schema_version: int = 1`. No other change.
- `tests/grade/test_drift_detector.py` — `StrictGradeEvent.audit_schema_version` stays `Literal[1] = 1` (documents the current fixture shape, fails loud if the fixture grows new values).

**TDD.**
- Write `test_grade_event_audit_schema_version_is_int_typed` — constructs `GradeEvent(audit_schema_version=99, ...)` and asserts no `ValidationError` (the production model now tolerates any int).
- Write `test_strict_grade_event_still_rejects_non_literal_one` — `StrictGradeEvent(audit_schema_version=2, ...)` raises `ValidationError`.

**Acceptance criteria.**
1. Production `GradeEvent.audit_schema_version` is typed `int` (not `Literal[N]`).
2. Existing v1 fixture (`tests/fixtures/grade/grade_event_v1.jsonl`) still validates against production `GradeEvent`.
3. `StrictGradeEvent` mirror keeps `Literal[1]`.
4. Canonical validation command passes.

**Done when.** Both new tests pass and `uv run pytest tests/grade/test_drift_detector.py tests/grade/test_models.py -v` is green.

**Depends on.** None.

---

### US-002 — `GradeEvent.cache_hit` field + `_build_grade_event` kwarg + v2 fixture

**Description.** Adds `cache_hit: bool = False` to `GradeEvent` immediately after `response_text_hash`. Adds keyword-only `cache_hit: bool = False` parameter to `_build_grade_event(...)` in `signalforge.grade.audit`. Bumps the production default `audit_schema_version` from 1 to 2. Ships a v2 strict drift-mirror + a populated v2 fixture. v1 fixture stays.

**Traces to.** DEC-008, DEC-009, DEC-010.

**Files.**
- `src/signalforge/grade/models.py` — add `cache_hit: bool = False` after the existing `response_text_hash` field; bump `audit_schema_version: int = 1` → `audit_schema_version: int = 2`. Update the `__repr__` redaction surface to keep `cache_hit` in the compact repr (it's a non-sensitive bool).
- `src/signalforge/grade/audit.py` — `_build_grade_event(*, ..., cache_hit: bool = False)` keyword-only. Pass `cache_hit=cache_hit` into `GradeEvent(...)`.
- `tests/grade/test_drift_detector.py` — add `Strict<v2>GradeEvent` mirror with `cache_hit: bool = False` + `audit_schema_version: Literal[2] = 2`. Validate against new v2 fixture.
- `tests/fixtures/grade/grade_event_v2.jsonl` — NEW. One line with `cache_hit: true`, one with `cache_hit: false`, `audit_schema_version: 2` on both.
- `tests/grade/test_models.py` — extend `_make_event()` helper with `cache_hit: bool = False` parameter.

**TDD.**
- `test_grade_event_default_cache_hit_is_false` — construct via `_build_grade_event(...)` without `cache_hit`; assert `event.cache_hit is False`.
- `test_grade_event_cache_hit_true_round_trips_through_jsonl` — build with `cache_hit=True`, dump, reload, assert preserved.
- `test_strict_v2_grade_event_validates_v2_fixture` — fixture-driven.
- `test_v1_fixture_still_validates_against_production_grade_event` — backward-compat.
- `test_build_grade_event_construction_seam_still_solely_in_audit_module` — AST scan 6 stays green.

**Acceptance criteria.**
1. `GradeEvent.cache_hit: bool = False` exists; default keeps v1 fixture round-trippable.
2. `_build_grade_event` accepts `cache_hit` kwarg; downstream `GradeEvent` constructions in `audit.py` thread it through.
3. `audit_schema_version` defaults to `2` for new events.
4. v2 fixture committed; v1 fixture still validates.
5. The 6th AST scan in `tests/test_audit_completeness.py` still passes (sole construction in `signalforge.grade.audit`).
6. Canonical validation command passes.

**Done when.** All five new tests pass and the existing audit-completeness AST scan stays green.

**Depends on.** US-001.

---

### US-003 — `signalforge.grade.cache` module: keys, record, I/O

**Description.** Net-new module. Public surface per DEC-018: `compute_cache_key(*, criterion_prompt_hash, artifact_text_hash, provider, model, prompt_version_template) -> str`, `CacheRecord` Pydantic model, `lookup_cache(cache_dir, key)`, `write_cache(cache_dir, key, record)`, `clear_cache(cache_dir)`. Implements the 5-part key recipe (DEC-004), the fail-soft write posture (DEC-005), the 16KB size cap (DEC-006), the don't-cache-degraded rule (DEC-007), the flat 0o600 layout (DEC-012), and the O_EXCL concurrent-write safety (DEC-014).

**Traces to.** DEC-004, DEC-005, DEC-006, DEC-007, DEC-011, DEC-012, DEC-013, DEC-014, DEC-018.

**Files.**
- `src/signalforge/grade/cache.py` — NEW. ~250 lines.
- `src/signalforge/grade/__init__.py` — export `CacheRecord` + public functions.
- `tests/grade/test_cache.py` — NEW unit-test file.
- `tests/fixtures/grade/grade_cache_record_v1.json` — NEW fixture for drift detector.
- `tests/grade/test_drift_detector.py` — add `StrictCacheRecord` mirror.

**TDD.** Write these first, all failing:
- `test_compute_cache_key_is_deterministic` — same inputs → same 16-hex output, twice.
- `test_compute_cache_key_changes_on_each_input_axis` — flip one of 5 inputs by 1 bit, assert different output. (5 sub-cases via parametrize.)
- `test_compute_cache_key_provider_in_recipe_prevents_collision` — `(anthropic, sonnet)` ≠ `(openai, sonnet)`.
- `test_lookup_cache_returns_none_on_missing_file`.
- `test_lookup_cache_returns_none_on_missing_dir`.
- `test_lookup_cache_returns_none_on_malformed_json` (treated as cache miss, no raise).
- `test_lookup_cache_returns_none_on_schema_version_mismatch` (v2 record in v1 reader → miss).
- `test_lookup_cache_round_trips_a_written_record`.
- `test_write_cache_creates_dir_lazily`.
- `test_write_cache_uses_0o600_file_mode`.
- `test_write_cache_o_excl_skips_on_existing_file` (concurrent-write safety).
- `test_write_cache_fails_soft_on_oserror` (mock os.open to raise; assert no exception escapes; WARNING emitted).
- `test_write_cache_rejects_oversize_record_with_warning` (synthesise a 20KB `reasoning` field; assert no on-disk artefact + WARNING).
- `test_write_cache_refuses_degraded_results` (record with `score=None` → raises `ValueError` at the CacheRecord layer or `write_cache` no-ops).
- `test_clear_cache_removes_dir_idempotently` (works on missing dir).
- `test_clear_cache_refuses_symlink_escape` (cache dir is a symlink to /tmp; assert raises `GradeCachePathError` and does NOT remove the symlink target).
- `test_strict_cache_record_validates_committed_fixture` (drift detector).

**Acceptance criteria.**
1. `compute_cache_key` produces 16-hex blake2b-8 keys deterministically across the five-part recipe.
2. `lookup_cache` returns `CacheRecord | None` — gracefully None on every degenerate path (missing file/dir, malformed JSON, schema-version mismatch).
3. `write_cache` is fail-soft per DEC-005: any `OSError`/oversize/concurrent-conflict → one WARNING + no-op. Never raises.
4. `clear_cache` is symlink-hardened — uses `_common.path_safety.canonicalise_path` before `shutil.rmtree`; raises `GradeCachePathError` on escape.
5. `CacheRecord` flat shape per DEC-011; cache_schema_version: int = 1; degraded results refused.
6. Lazy-format JSON in every `_LOGGER.*` call (the grep gate at `tests/llm/test_logger_grep_gate.py` is the auto-gate).
7. Canonical validation command passes; new tests all green.

**Done when.** All 17 new tests pass and the logger grep gate auto-extends to `src/signalforge/grade/cache.py` cleanly.

**Depends on.** US-001 (so the v2 GradeEvent shape is settled and the cache writes integrate cleanly). US-002 not strictly required for the standalone cache module but lands before US-006 wires it in.

---

### US-004 — Cache typed errors + exit-code registration

**Description.** Add three error classes in `signalforge.grade.errors`: `GradeCacheReadError`, `GradeCacheWriteError`, `GradeCachePathError`, plus `GradeCacheRecordTooLargeError` as a `GradeCacheWriteError` subclass. Register each in `_EXCEPTION_TO_EXIT_CODE` in `signalforge.cli._helpers`. The 7th AST scan auto-checks the registration. `GradeCacheWriteError` is registered for catch-and-warn diagnostics but never propagates from `grade_artifacts` (fail-soft per DEC-005).

**Traces to.** DEC-005, DEC-006, DEC-017.

**Files.**
- `src/signalforge/grade/errors.py` — add the four classes with `default_remediation` text.
- `src/signalforge/cli/_helpers.py` — register the three "top-level" ones (the TooLarge subclass inherits its parent's tier via MRO walk).
- `tests/cli/test_exit_codes.py` — parametrize the three new error classes against their expected tiers.
- `tests/grade/test_errors.py` — test the `default_remediation` text for each.

**TDD.**
- `test_grade_cache_read_error_maps_to_tier_3`.
- `test_grade_cache_path_error_maps_to_tier_1`.
- `test_grade_cache_record_too_large_inherits_tier_3_via_mro`.
- `test_default_remediation_text_locked_for_each_cache_error`.

**Acceptance criteria.**
1. Four error classes exist with locked `default_remediation` text.
2. Three primary errors registered in `_EXCEPTION_TO_EXIT_CODE`; the TooLarge subclass inherits via MRO.
3. The 7th AST scan in `tests/test_audit_completeness.py::test_every_typed_error_is_in_exit_code_mapping_table` passes.
4. Canonical validation command passes.

**Done when.** All four new tests pass and `uv run pytest tests/test_audit_completeness.py -v` stays green.

**Depends on.** None (can run in parallel with US-003).

---

### US-005 — `GradeConfig.cache_enabled` knob

**Description.** Single new boolean field on `GradeConfig`: `cache_enabled: bool = True`. Respects `extra="forbid"` (typos fail loud). No TTL knob in this ticket per DEC-016.

**Traces to.** DEC-016.

**Files.**
- `src/signalforge/grade/config.py` — add the field.
- `tests/grade/test_config.py` — add a test for the new field's default + extra-forbid behaviour.
- `docs/grade-ops.md` — document `grade.cache_enabled` under the existing config table.

**TDD.**
- `test_grade_config_cache_enabled_defaults_true`.
- `test_grade_config_typo_cache_enable_missing_d_fails_loud` (asserts `ValidationError` at config-load on `cache_enable: false`).

**Acceptance criteria.**
1. `GradeConfig(cache_enabled=False)` parses; default is True.
2. Typo'd key fails loud per `extra="forbid"`.
3. Doc update lands in same commit.
4. Canonical validation command passes.

**Done when.** Both new tests pass.

**Depends on.** None.

---

### US-006 — Wire cache into the grade engine

**Description.** Engine surgery in `signalforge.grade.engine`. (1) Sync-prefix loop: canonicalise the cache dir via `_common.path_safety.canonicalise_path(project_dir / ".signalforge" / "grade-cache", project_dir)`; for each `(artifact, criterion)` pair, compute the cache key (DEC-004) and call `lookup_cache`. (2) Cache HIT → build a `GradingResult` from the `CacheRecord` and call `_build_grade_event(..., cache_hit=True, input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0)`; write the event via the existing fail-closed audit writer; skip the asyncio dispatch for this pair. (3) Cache MISS → fall through to the existing async dispatch; after parse, write the `CacheRecord` via `write_cache` (DEC-005 fail-soft) BEFORE the standard audit write (so a cache-write WARNING doesn't masquerade as the live-grade audit). (4) Honour `config.cache_enabled` — when False, skip both lookup AND write (`signalforge generate --no-cache` will flip this knob at orchestrator entry; US-007 wires it).

**Traces to.** DEC-004, DEC-005, DEC-007, DEC-010, DEC-013, DEC-016, DEC-018.

**Files.**
- `src/signalforge/grade/engine.py` — surgical changes to `grade_artifacts` (canonicalise cache dir) and `_grade_artifacts_async_core` (sync-prefix cache lookup, async-suffix cache write). Use the patterns existing for the audit canonicalisation as the precedent.

**TDD.**
- `test_grade_engine_cache_hit_skips_llm_call` — fake client with zero queued expectations; cache pre-populated for every pair; assert `assert_all_expectations_met()` passes.
- `test_grade_engine_cache_miss_writes_entry` — empty cache; one LLM call queued; assert cache dir contains one new `<16-hex>.json` post-run.
- `test_grade_engine_cache_disabled_skips_lookup_and_write` — `GradeConfig(cache_enabled=False)`; pre-populated cache; assert LLM is still called.
- `test_grade_engine_artifact_text_change_invalidates_cache` — pre-populate; flip column description; assert cache miss + LLM call.
- `test_grade_engine_model_change_invalidates_cache` — pre-populate with model A; re-run with model B; assert miss.
- `test_grade_engine_provider_change_invalidates_cache` — pre-populate with `provider=anthropic`; switch to `provider=openai`; assert miss.
- `test_grade_engine_degraded_result_not_cached` — force a degrade (rate-limit retries exhausted in fake); assert NO cache file written.
- `test_grade_engine_cache_write_failure_is_fail_soft` — patch `write_cache` to raise; assert `grade_artifacts` returns the live `GradingReport` AND one WARNING is logged.
- `test_grade_engine_cache_hit_event_has_zero_tokens` — pre-populate; assert the resulting `GradeEvent` in `grade.jsonl` has all four token fields == 0 AND `cache_hit=True`.
- `test_grade_engine_cache_hit_dispatch_order_preserved_with_async_misses` — mixed hit/miss pairs under `max_concurrent_calls=10`; assert audit JSONL sorts cleanly via `_sort_grade_events`.

**Acceptance criteria.**
1. Cache hit produces a `GradeEvent` with `cache_hit=True` + zero token counts; live LLM call skipped.
2. Cache miss writes a `CacheRecord` post-grade (fail-soft on write failure).
3. `cache_enabled=False` short-circuits both lookup AND write.
4. Cache invalidation works on all five recipe axes (criterion, artifact text, provider, model, prompt-version).
5. Degraded results never land in the cache.
6. Cache write failure → WARNING but `grade_artifacts` succeeds.
7. AST scan 6 still passes (cache-hit events constructed via `_build_grade_event` only).
8. Canonical validation command passes.

**Done when.** All 10 new tests pass and the existing grade-engine test suite stays green.

**Depends on.** US-002, US-003, US-005.

---

### US-007 — `--no-grade` + `--no-cache` CLI flags + `[N/4]` progress UX

**Description.** Add `--no-grade` and `--no-cache` bare boolean flags to `cmd_generate`. `--no-grade` wraps the existing grade block (`cli/generate.py:1061-1083`) in `if not args.no_grade:` and defaults `grade_report = None` (already supported by `render_diff`). `--no-cache` flips `grade_config = grade_config.model_copy(update={"cache_enabled": False})` BEFORE invoking `grade_artifacts(...)`. Progress: `cmd_generate` computes `total = 4 if args.no_grade else 5` at startup and threads through `emit_progress_entry(..., total=total)` / `emit_progress_done(..., total=total)`. Precedence: `--no-grade` implicitly wins when both are set (no grade code runs → no cache code runs).

**Traces to.** DEC-001, DEC-002, DEC-003.

**Files.**
- `src/signalforge/cli/generate.py` — argparse additions; orchestration block wrap; `total` threading.
- `src/signalforge/cli/_helpers.py` — confirm `emit_progress_entry/done` already accept `total` kwarg; if not, surgical extension.
- `tests/cli/test_generate.py` — five new tests (per the existing test surface).

**TDD.**
- `test_no_grade_skips_grade_stage` — fake adapter + fake LLM; assert `grade_artifacts` is NEVER invoked (use a sentinel-replacing monkeypatch).
- `test_no_grade_omits_grade_sidecar` — `.signalforge/grade.jsonl` and `.signalforge/grade.json` absent post-run.
- `test_no_grade_diff_has_no_flagged_tier` — assert `DiffReport.flagged_count == 0`.
- `test_no_grade_progress_renumbers_to_4` — capture stderr; assert `[1/4]` ... `[4/4]`, never `[X/5]`.
- `test_no_grade_exits_zero_on_success`.
- `test_no_cache_disables_cache_layer` — pre-populate cache; with `--no-cache`, assert LLM is called (cache lookup skipped) AND cache dir contents unchanged post-run.
- `test_no_grade_implicitly_wins_over_no_cache` — both flags set; assert no grade events AND no cache reads/writes.

**Acceptance criteria.**
1. Both flags accepted by argparse; help-string text matches the DEC-001/DEC-002 grammar.
2. `--no-grade` skips grade stage; diff renders without flagged tier.
3. `--no-cache` disables cache lookup AND write for one run; existing cache files untouched.
4. Progress renumbers to `[N/4]` under `--no-grade`.
5. Both flags can combine with `--write`, `--dry-run`, `--mode sample`, `--estimate`.
6. Canonical validation command passes.

**Done when.** All seven new tests pass.

**Depends on.** US-006.

---

### US-008 — `signalforge cache clear --grade` subcommand

**Description.** New top-level subcommand `cache` with one nested sub-action `clear` and one flag `--grade`. Module: `src/signalforge/cli/cache.py`. Handler `cmd_cache(args)` dispatches on the sub-action. `clear --grade` canonicalises `<project_dir>/.signalforge/grade-cache/` via `_common.path_safety.canonicalise_path`, then `shutil.rmtree` (ignore-errors=False). On a missing dir: log INFO "no grade cache to clear" and exit 0. On a symlink escape: raise `GradeCachePathError` (tier 1).

**Traces to.** DEC-015.

**Files.**
- `src/signalforge/cli/cache.py` — NEW. Implements `add_parser(subparsers)` registering `cache` + nested sub-action; `cmd_cache(args)` dispatcher.
- `src/signalforge/cli/__init__.py` — register the new module's `add_parser`.
- `tests/cli/test_cache.py` — NEW tests.

**TDD.**
- `test_cache_clear_grade_removes_cache_dir`.
- `test_cache_clear_grade_idempotent_on_missing_dir`.
- `test_cache_clear_grade_refuses_symlink_escape` — symlink `.signalforge/grade-cache → /tmp`; assert exit 1 + `GradeCachePathError`-derived stderr; assert `/tmp` contents intact.
- `test_cache_clear_grade_help_text_lists_grade_flag`.
- `test_cache_subcommand_appears_in_parser_choices` (auto-grow of skill-parity gate is implicit here).

**Acceptance criteria.**
1. `signalforge cache clear --grade` removes `.signalforge/grade-cache/` and exits 0.
2. Idempotent on missing dir.
3. Symlink-hardened — rejects targets outside `<project_dir>/.signalforge/`.
4. `signalforge cache --help` and `signalforge cache clear --help` produce sensible text.
5. Skill-parity gate auto-passes (subcommand `cache` listed in SKILL.md per US-009).
6. Canonical validation command passes.

**Done when.** All five new tests pass; skill-parity gate green.

**Depends on.** US-003 (uses `clear_cache(...)` from the cache module). US-004 (uses `GradeCachePathError`).

---

### US-009 — Docs + SKILL.md + 5-surface parity test

**Description.** Update all five DEC-019 surfaces in lockstep:
1. **Argparse help strings** — already landed in US-007 and US-008.
2. **DECs in this plan** — already present (DEC-001, DEC-002).
3. **`docs/cli-ops.md`** — new cookbook sections: "Skip grading for fast iteration (`--no-grade`)", "Bypass the grade cache for one run (`--no-cache`)", "Clear the grade cache (`signalforge cache clear --grade`)".
4. **Test docstring** — the parity test's own `__doc__` lists `--no-grade`, `--no-cache`, `cache clear --grade` as the tokens it pins.
5. **`src/signalforge/skills/signalforge/SKILL.md`** — extend the `signalforge generate` section with the two flags; add a small `signalforge cache clear --grade` paragraph.

`docs/grade-ops.md` also gets a "Grade cache" section documenting cache layout, key recipe (high-level), invalidation axes, `grade.cache_enabled` knob.

**Traces to.** DEC-001, DEC-002, DEC-015, DEC-016, DEC-019.

**Files.**
- `docs/cli-ops.md` — three new cookbook sections.
- `docs/grade-ops.md` — new "Grade cache" section.
- `src/signalforge/skills/signalforge/SKILL.md` — flag additions + new `cache` paragraph.
- `tests/cli/test_5_surface_parity_no_grade.py` — NEW; mirrors `test_5_surface_parity_select.py` shape.

**TDD.** The parity test IS the test; written last per the surface-parity rule.

- `test_no_grade_token_appears_in_all_5_surfaces` — substring search for `--no-grade` in (1) help-string capture, (2) plan file, (3) cli-ops.md, (4) this file's docstring, (5) SKILL.md.
- `test_no_cache_token_appears_in_all_5_surfaces` — same for `--no-cache`.
- `test_cache_clear_grade_token_appears_in_4_surfaces` — substring for `cache clear --grade` in (1) help, (3) cli-ops.md, (4) docstring, (5) SKILL.md. (Plan doc carries the DEC-015 description; if the literal token doesn't appear there verbatim, the test omits surface 2 OR the plan is amended in this story.)

**Acceptance criteria.**
1. Five surfaces contain the literal `--no-grade` and `--no-cache` tokens.
2. Four surfaces (skipping the parity test itself) contain `cache clear --grade`.
3. SKILL-parity gate (`tests/cli/test_skill_cli_parity.py`) passes — the `cache` subcommand auto-grows.
4. Both new docs sections render in MkDocs without warnings (`uv run mkdocs build`).
5. Canonical validation command passes.

**Done when.** All three parity tests pass plus the skill-parity gate stays green.

**Depends on.** US-007, US-008.

---

### US-010 — Cost-rollup cache-hit test

**Description.** One new test fixture + one new test asserting that cache-hit `GradeEvent`s contribute $0 to the cost rollup. Pins DEC-020.

**Traces to.** DEC-020.

**Files.**
- `tests/llm/cost/test_rollup.py` — add the new test. Existing `_grade_record(...)` helper already supports token-count parameters.

**TDD.**
- `test_cost_rollup_treats_cache_hit_grade_event_as_zero_cost` — fixture with one cache-hit `GradeEvent` (`cache_hit=True`, all four token counts == 0) AND one cache-miss event (non-zero tokens). Run `rollup_audit_dir(project_dir)`. Assert (a) the total grade USD equals only the miss contribution, (b) the cache-hit row is present in the audit walk (rollup doesn't skip it).

**Acceptance criteria.**
1. One new test pins the zero-cost-per-cache-hit contract.
2. Existing rollup tests stay green.
3. Canonical validation command passes.

**Done when.** New test passes.

**Depends on.** US-002 (needs the new `cache_hit` field on `GradeEvent`).

---

### Quality Gate — code review × 4 + CodeRabbit

**Description.** Run the code-review skill 4 times across the full PR diff, fixing every real bug found each pass. Run CodeRabbit if available. Validation must pass after all fixes.

Per the `qg-diverse-reviewer-angles-catch-cross-surface-drift` memory, use four distinct angles:
- **Correctness** — security, cache poisoning, fail-soft propagation, schema-version round-trip.
- **Conventions** — adherence to all `.claude/rules/*.md` files identified in Discovery (especially `grade-layer.md`, `cli-layer.md`, `safety-layer.md` posture rules).
- **Tests** — coverage of every new code branch; engineered determinism; fixture parity; AST-scan compliance.
- **Docs + UX** — cli-ops.md / grade-ops.md / SKILL.md prose freshness; help-string clarity; `cache clear --grade` operator-message text.

Per `qg-pass-3-defer-defensive-tests-fails-codecov` memory — any Pass-3 "defensive" tests covering lines in the diff get upgraded to must-fix.

**Acceptance criteria.**
1. Four code-review passes complete with all findings either fixed or annotated as deliberate.
2. CodeRabbit review (if available) — all findings addressed.
3. `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` passes.
4. Coverage at or above the project `--cov-fail-under` floor.

**Done when.** All of the above pass and the PR is ready for human review.

**Depends on.** US-001 through US-010 (all implementation).

---

### Patterns & Memory — update conventions and docs

**Description.** Distil durable lessons from #189 into the rules files and the memory store. Always last; always included.

Expected updates:
- **`.claude/rules/grade-layer.md`** — new section on "Grade cache (persistent content-addressed)": 5-part key recipe, fail-soft posture vs. fail-closed audit, the 16KB cap, the don't-cache-degraded rule, the DEC-009 `Literal → int` lesson.
- **`.claude/rules/cli-layer.md`** — note the first nested-subcommand precedent (`cache clear`); document the layout pattern for future siblings.
- **MEMORY.md** — append pointers to new memory files capturing:
  - `grade-cache-five-part-key-includes-provider.md` — security lesson.
  - `audit-schema-version-must-be-int-not-literal.md` — generalised lesson (any future audit-event class).
  - `cache-write-fail-soft-vs-audit-fail-closed.md` — the orthogonal posture rule.

**Acceptance criteria.**
1. Each new rule section reads as durable convention, not a re-narration of #189.
2. New memory files follow the user/feedback/project/reference taxonomy.
3. MEMORY.md grows by the appropriate number of one-line pointers.
4. No tests required (this story is doc-only).

**Done when.** All three rule files updated and memory pointers committed.

**Depends on.** Quality Gate.

---

### Beads dependency graph (for Phase 7 devolve)

```text
US-001 (type fix)
  └── US-002 (cache_hit field + fixture)
       ├── US-006 (engine wiring)         ──> US-007 (CLI flags)  ──┐
       └── US-010 (cost-rollup test)                                 │
                                                                     ├── US-009 (docs+SKILL+parity)
US-003 (cache module)                                                │
  ├── US-006 (engine wiring)                                         │
  └── US-008 (cache clear subcommand) ─────────────────────────────────┤
US-004 (errors+exit-codes)                                           │
  └── US-008 (uses GradeCachePathError)                              │
US-005 (config knob)                                                 │
  └── US-006 (engine reads config.cache_enabled)                     │

US-001..US-010 ──────────> Quality Gate ──────────> Patterns & Memory
```

Total: 10 implementation stories + Quality Gate + Patterns & Memory = **12 beads**.

Three stories are independently startable (`US-001`, `US-003`, `US-004`, `US-005`) — Ralph can parallelise.
