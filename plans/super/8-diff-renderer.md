# Issue #8 — Diff renderer: kept/dropped table + unified schema.yml diff

## Meta

- **Ticket:** [#8](https://github.com/wjduenow/SignalForge/issues/8)
- **Branch:** `feature/8-diff-renderer` (off `dev`)
- **Worktree:** `/home/wesd/dev/worktrees/SignalForge/feature/8-diff-renderer` (created via `git worktree add`)
- **Phase:** published (awaiting PR approval)
- **PR:** [#25](https://github.com/wjduenow/SignalForge/pull/25) (draft)
- **Sessions:** 1 (started 2026-05-01)
- **Plan author:** Claude Code (Opus 4.7, 1M context)
- **Milestone:** v0.1 (encodes Architectural Commitment #5 — explainable diffs; gates #9 CLI)
- **Labels:** `cli` (per the GitHub issue)

---

## Discovery

### Ticket summary (verbatim from GitHub issue #8)

> **Goal:** Show the user, in their terminal and in any future PR comment, what changed and why. No black-box generation.
>
> **Acceptance criteria:**
> - Unified diff between existing `schema.yml` (if any) and proposed
> - Per-artifact "kept/dropped" table with one-line reason
> - Color-coded terminal output (green = kept, red = dropped, yellow = flagged)
> - `--json` flag for machine-readable output
> - `--no-color` and TTY detection
> - Snapshot tests against fixture renderings
>
> **Notes:** The diff format becomes the GitHub Action PR comment in v0.3 — design for both consumers from day one.

The diff renderer (#8) sits between the quality grader (#7) and the CLI (#9). It is the first stage that:

- Does **not** call the LLM (no `signalforge.llm` involvement).
- Does **not** call the warehouse (no SQL safety / cost-budget involvement).
- Is the **first user-visible output surface** — writes to `sys.stdout` (and optionally a sidecar) for human review.
- Is the **only stage with three rendering targets** to coordinate from day one: terminal (ANSI), markdown (GitHub-flavored), JSON.

This encodes Architectural Commitment #5 (explainable diffs): every kept/dropped artifact ships with a one-line "why," and the operator can scan the diff before committing.

### Codebase findings (Subagent B — directly verified, file:line cited)

**Public surface alignment.** Every prior stage exposes the same three-layer surface:

- Config loader: `load_<stage>_config(project_dir, path=None) -> <Stage>Config`. Returns immutable Pydantic models; missing config files silently return defaults; explicit paths that don't exist raise typed errors. (`src/signalforge/grade/__init__.py:26`, mirrored across `safety`, `draft`, `prune`.)
- Orchestrator entry: data front-paired positionally, keyword-only optionals after `*` (`grade_artifacts(model, candidate, prune_result, *, rubric, config, audit_path, sidecar_path, client, project_dir)` — `src/signalforge/grade/__init__.py:27`).
- Error hierarchy: each stage exports a base `<Stage>Error` plus typed subclasses, every error carries `remediation: str` rendered as `↳ Remediation:` line.

**Three typed inputs the renderer consumes:**

- `CandidateSchema` (`src/signalforge/draft/models.py:176-200`) — the LLM-drafted artifacts: `name`, `description`, `rationale`, `columns: tuple[CandidateColumn, ...]`, `tests: tuple[CandidateTest, ...]` (model-level). Each `CandidateColumn` carries `description`, `rationale`, `tests`. Each `CandidateTest` is a discriminated union (`not_null | unique | accepted_values | relationships`) with a `rationale` field. All `frozen=True, extra="ignore"`.
- `PruneResult` (`src/signalforge/prune/models.py:112-175`) — `decisions: tuple[PruneDecision, ...]` where each decision carries `test_anchor`, `test`, `decision: Literal["kept","dropped"]`, `reason: DropReason` (one of five literals), `why: str`, `failures`, `compiled_sql`, `compiled_sql_hash`. Custom `__repr__` redacts SQL/sample failures (DEC-022). Computed `kept_decisions`, `dropped_decisions`.
- `GradingReport` (`src/signalforge/grade/models.py:145-251`) — `results: tuple[GradingResult, ...]` where each result carries `artifact_id` (dotted format like `"column.<col>.description"` per DEC-009 of #7), `criterion_id`, `score: float | None` (`None` on degraded path), `passed: bool`, `evidence`, `reasoning`, **and a computed property `one_line_why`** that returns the first sentence of reasoning, capped at 120 chars (`models.py:104-126`) — already designed for direct consumption by the diff renderer. Aggregate fields: `pass_rate`, `mean_score`, `aggregate_complete`, `passed`.

**On-disk `schema.yml` is a new responsibility.** Grep confirms no existing schema.yml loading anywhere in `src/`. The manifest loader (#2) parses `manifest.json`, not the YAML. PyYAML is already pinned (`pyproject.toml:15` — `PyYAML>=6,<7`), so `yaml.safe_load` is available without a new dep.

**Audit / sidecar paths.** Established convention: `<project_dir>/.signalforge/<stage>.jsonl` (audit) and `<project_dir>/.signalforge/<stage>.json` (sidecar). Both resolve via `canonicalise_path(raw_path, project_dir)` at orchestrator entry (`src/signalforge/grade/engine.py:762-765`). Symlinks pointing outside the project are rejected.

**Snapshot/fixture testing pattern.** No `syrupy` or snapshot library in use. Every stage commits plain JSON/JSONL fixtures under `tests/fixtures/<stage>/` and pairs them with a `tests/<stage>/test_drift_detector.py` that defines `Strict<X>(extra="forbid")` mirrors and validates the fixture against both the production model and the strict mirror. Update rule: production change = strict mirror change = fixture refresh in the same commit.

**No CLI yet.** No `[project.scripts]` entry in `pyproject.toml`. Issue #9 will add it. The diff renderer must therefore expose a library function that #9 calls.

### Domain research (Subagent D — diff format + dbt schema.yml)

**dbt `schema.yml` canonical shape:**

```yaml
version: 2
models:
  - name: <model_name>
    description: <markdown text>
    columns:
      - name: <col>
        description: <text>
        tests:
          - not_null
          - unique
          - accepted_values:
              values: ['a','b']
          - relationships:
              to: ref('parent')
              field: id
    tests: [ ... ]   # model-level
```

dbt-codegen (Jinja templates at `https://github.com/dbt-labs/dbt-codegen/blob/main/macros/generate_model_yaml.sql`) emits `name → description → columns:[name → data_type → description]`. dbt-osmosis (`https://github.com/z3z1ma/dbt-osmosis`) sorts column keys `name, data_type, constraints, description, meta, tags, policy_tags, tests/data_tests` via ruamel.yaml.

**Diff "noise" patterns to normalise on emit** (so the unified diff shows real changes, not formatter churn):

1. Stable key order (`name → description → columns → tests`) on both sides.
2. Force `default_flow_style=False`, `width=4096` to avoid spurious line wraps.
3. Force `sort_keys=False` (PyYAML otherwise alphabetises).
4. Sort tests within a column by `(type, args_hash)` for determinism; **keep columns in manifest declaration order** (reorder-on-column-shuffle is the #1 reviewer complaint per `docs/research/dbt-pain-deep-dive.md`).
5. Strip trailing whitespace, end with a single trailing newline.
6. Empty `tests: []` → omit the key.

**Library choices (minimum dep surface):**

- **YAML emit:** PyYAML — already a dep. `yaml.safe_dump(d, sort_keys=False, default_flow_style=False, width=4096, allow_unicode=True)`. ruamel.yaml only buys round-trip-with-comments which is a v0.2 ask.
- **Unified diff:** stdlib `difflib.unified_diff(a, b, fromfile, tofile, n=3)`. `n=3` matches GNU `diff -u` and keeps PR comments under GitHub's 65k-char limit. For "no existing file" → pass `[]` and `fromfile='/dev/null'` (mirrors `git diff --no-index`).
- **Terminal color:** raw ANSI escapes via stdlib (4 codes: green/red/yellow/reset; ~30 lines). `colorama` is unnecessary (Python 3.11+ on modern Windows enables VT100 by default). `rich` is heavyweight (~500 KB) and its auto-sizing breaks snapshot tests. `click.style` is "free" only if #9 pulls click anyway — coordinate.
- **Snapshot tests:** plain text fixtures under `tests/fixtures/diff/` matching the precedent of #5/#6/#7 (committed JSON/JSONL fixtures + a regen script). No new test deps.

**Three rendering surfaces, three concrete renderers behind one ABC:** `AnsiRenderer`, `MarkdownRenderer`, `JsonRenderer`. The orchestrator builds one typed `DiffReport`; the renderer projects.

**`--no-color` / TTY detection (NO_COLOR spec at `https://no-color.org/`):**

- Default: color iff `sys.stdout.isatty()` AND `NO_COLOR` env var unset AND `--no-color` flag unset.
- `NO_COLOR` env var (any value → disable color, per spec).
- `FORCE_COLOR` env var overrides TTY detection.
- `--no-color` flag is the explicit kill switch (highest precedence after FORCE_COLOR).

**Anti-patterns (Subagent D):**

- Don't import dbt-osmosis or dbt-codegen as libraries — they're CLI tools with non-stable internal APIs.
- Don't reuse clauditor's grading output rendering — `GradingReport` is the typed source of truth here.
- Don't write user-facing text via `_LOGGER` — that's the audit channel; diff goes to `sys.stdout`.
- Don't shell out to `git diff --no-index` — stdlib `difflib` produces compatible unified diff with zero subprocess.
- Don't pin context lines `n>3` — bigger PR comments hit GitHub's 65k-char limit.
- Don't auto-detect "yaml is identical to existing file" and skip the diff — the kept/dropped table is still useful.

### Project rules (`.claude/rules/`) audit (Subagent C — full constraint list)

`workflow-project.md` does not exist. The 9 rule files yield 11 universally-applicable constraints + 3 conditional constraints + several explicit exclusions.

**Definitely applies (universal):**

| Rule | Source | Application to #8 |
|---|---|---|
| ANSI-safe lazy-format JSON logger; never f-string-interpolate user content into `_LOGGER` | `safety-layer.md` DEC-022, `llm-drafter.md` DEC-011, `prune-engine.md` DEC-017, `grade-layer.md` DEC-029 | Extend grep gate (`tests/llm/test_logger_grep_gate.py`) to scan `src/signalforge/diff/` (5th dir). Renderer's user-facing output goes to stdout, not the logger; the gate still binds for any internal `_LOGGER` calls. |
| `extra="forbid"` on config-shaped Pydantic models; `extra="ignore"` on read-back/result models | `safety-layer.md` DEC-015, `prune-engine.md` DEC-010, `grade-layer.md` DEC-010 | `DiffConfig`, `_DiffConfigFile` inner content → `extra="forbid"`. `DiffReport`, any `DiffOutput` shape → `extra="ignore"`. Top-level `_DiffConfigFile` → `extra="ignore"` so unknown sibling stages don't break the loader. |
| Drift detectors (`Strict<X>(extra="forbid")` mirrors) for every `extra="ignore"` read-back model, validated against committed fixtures | `safety-layer.md` DEC-014, `prune-engine.md` DEC-010, `grade-layer.md` DEC-010 | `tests/diff/test_drift_detector.py` + `tests/fixtures/diff/diff_report_v1.json` and friends. |
| `signalforge.yml` top-level namespace per stage | `llm-drafter.md` DEC-027, `safety-layer.md` DEC-025, `prune-engine.md` DEC-020, `grade-layer.md` DEC-029 | Claim `diff:` as the new top-level key. Sibling keys silently ignored. |
| `load_<stage>_config(project_dir, path=None) -> <Stage>Config` signature | `prune-engine.md` API alignment, `grade-layer.md` API alignment | `load_diff_config(project_dir, path=None) -> DiffConfig`. Resolution: explicit `path` > `<project_dir>/signalforge.yml diff:` > defaults. |
| Orchestrator entry signature: data front-paired positionally; `*` then keyword-only optionals | `prune-engine.md` API alignment, `grade-layer.md` API alignment | See Q1 below for the canonical signature. |
| Error hierarchy: typed exceptions, `remediation: str` kwarg, user strings rendered via `repr()` to prevent ANSI injection | `manifest-readers.md` DEC-007, `warehouse-adapters.md` Error-hierarchy section | `DiffError` base + typed subclasses. |
| Hatchling + src layout + explicit wheel-target packages | `python-build.md` DEC-002, DEC-011 | New subpackage at `src/signalforge/diff/` inherits the existing wheel target (`packages = ["src/signalforge"]` from #1). |
| Symlink-hardened path canonicalisation at orchestrator entry, before any writer | `manifest-readers.md` symlink section, `grade-layer.md` post-QG-fix section, `warehouse-adapters.md` US-014 | Apply at `render_diff(...)` entry to any `output_path`/`sidecar_path`/`existing_schema_path`. Three traps from `manifest-readers.md` apply to writers too. |
| Custom `__repr__` on result-shaped models (suppress full-field repr for user-content payloads) | `prune-engine.md` DEC-022, `grade-layer.md` DEC-022 | `DiffReport.__repr__` shows only metadata (`model_unique_id`, `kept_count`, `dropped_count`, `flagged_count`, `has_existing_schema`); raw diff text and report rows accessible via field access only. |
| Python 3.11 single-version CI; pinned action SHAs | `ci-supply-chain.md` DEC-003, DEC-005 | No new workflow needed; existing CI runs on `dev` PRs. |

**Conditionally applies (decide in refinement — see Q5):**

| Rule | Source | Conditional question |
|---|---|---|
| Fail-closed audit JSONL pattern (`O_APPEND \| O_CREAT \| 0o600`, single `os.write`, `fsync`, no internal try/except, size cap before open) | `safety-layer.md` DEC-011, `llm-drafter.md` DEC-006, `prune-engine.md` DEC-016, `grade-layer.md` DEC-006 | Does diff renderer need an audit log? Diff is the first stage that arguably doesn't — it's render, not a decision step. **Recommend NO** (render is not a decision; the decisions were already audited by prune/grade). See Q5. |
| 7th AST audit-completeness scan to gate `DiffEvent` construction | `prune-engine.md` DEC-018, `grade-layer.md` DEC-029 (sixth scan) | Conditional on Q5. If no audit, no scan. |
| Sidecar-record size cap before file open (constant + `<X>RecordTooLargeError`) | `grade-layer.md` DEC-006 (`_GRADE_SIDECAR_RECORD_LIMIT_BYTES = 1_000_000`) | Applies if/when we write a sidecar JSON. |

**Explicitly does not apply** (note for completeness):

- LLM seam confinement / `# pyright: ignore` discipline (`llm-drafter.md` DEC-012) — no SDK calls.
- SQL identifier validation (`prune-engine.md` DEC-024, `warehouse-adapters.md` DEC-013) — no SQL emission.
- Warehouse cost-budget conventions / `_default_job_config` (`warehouse-adapters.md` DEC-005, DEC-015) — no warehouse calls.
- `<MODEL_SQL>` / `<ARTIFACT>` envelope-breach guards (`llm-drafter.md` DEC-007, `grade-layer.md` DEC-008) — no LLM input/output.
- LLM response audit + anchor contract (`llm-drafter.md` DEC-003, DEC-006) — no LLM responses to parse.

### Proposed scope (v0.1)

A new `signalforge.diff` subpackage that:

1. **Loads** the existing `<project_dir>/models/.../schema.yml` if present (PyYAML, `safe_load`); models the file shape as a frozen `extra="ignore"` Pydantic.
2. **Builds** a canonical proposed `schema.yml` from `CandidateSchema` (post-prune-filter to keep only `decision="kept"` tests).
3. **Computes** a unified diff via stdlib `difflib.unified_diff` between existing-canonical and proposed-canonical YAML strings (`n=3`, `/dev/null` for absent existing).
4. **Joins** the per-artifact decisions from `PruneResult` (kept/dropped + `why`) with `GradingReport` results to compute a "flagged" tier — kept artifacts whose grade is below threshold (`passed=False`) or in degraded state (`score=None`).
5. **Renders** to three surfaces behind a `Renderer` ABC:
   - `AnsiRenderer` — terminal output with ANSI green/red/yellow + TTY/NO_COLOR/FORCE_COLOR detection.
   - `MarkdownRenderer` — GitHub-flavored fenced ```diff block + pipe-table for kept/dropped.
   - `JsonRenderer` — machine-readable structured output for the `--json` flag and the v0.3 GH Action consumer.
6. **Returns** a typed `DiffReport` (orchestrator output) the CLI (#9) can serialise.
7. **No CLI flag wiring** — that's #9. The renderer exposes pure Python entry points; #9 maps `--json` / `--no-color` to renderer params.

Out of scope for v0.1 (defer to v0.2):

- ruamel.yaml round-trip / comment preservation on existing schema.yml.
- In-place edit of existing schema.yml ("apply" mode).
- Multi-model batched diffs (single-model per `render_diff(...)` call; orchestrator already accepts a list-shaped report for forward-compat).
- GitHub Action PR-comment driver (the renderer ships, but the workflow is v0.3).
- Structural (key-by-key) diff — line-based on canonical-emitted YAML covers v0.1.

### Scoping questions

The Discovery phase landed clear answers on most questions. Six remain where I want explicit user choice before the architecture review.

**Q1: Orchestrator signature and dependency surface.**

The renderer needs the typed model + the prior-stage outputs. Two shapes mirror precedent:

- **A. `render_diff(model, candidate, prune_result, grading_report, *, config=None, output_path=None, sidecar_path=None, project_dir=None) -> DiffReport`.** Mirrors `grade_artifacts(model, candidate, prune_result, *, ...)` exactly with `grading_report` added. Each stage's output threads forward.
- **B. Same as A but `grading_report` is optional** (`grading_report=None`). v0.1 ships diff with grading integration; users who skip grading still get kept/dropped without the yellow tier.
- **C. Compose `DraftOutcome | PruneResult | GradingReport` into one `PipelineResult` value object and pass that.** Cleaner long-term but requires retro-fit on prior stages.

**Recommendation: B.** Grading is a v0.1 feature but it's the last stage; some users may run draft+prune without grading (e.g., when iterating on prompts). Optional kwarg keeps the renderer usable without forcing a grade run, while making the integrated path the obvious one.

**Q2: Yellow "flagged" tier — what's the rule?**

The ticket lists `green = kept, red = dropped, yellow = flagged` but doesn't define "flagged." Three reasonable definitions:

- **A. Any kept artifact with at least one criterion `passed=False`** in `GradingReport`. (Threshold-failed grading.)
- **B. Any kept artifact with `score=None` (graceful-degrade path)** OR criterion `passed=False`. (A + degraded path.)
- **C. Any kept artifact whose `prune.reason == "kept-without-evidence"`** (prune couldn't evaluate) OR grading degraded. (Surface every "kept without positive signal.")

**Recommendation: B.** It surfaces both the explicit `passed=False` and the silent "we couldn't evaluate" failures, matching the `aggregate_complete` flag from the grader. Option C blurs the prune/grade boundary — the prune `kept-without-evidence` row already has its own `why` string, so the diff table renders that information faithfully without a yellow tag.

**Q3: How does the renderer locate an existing `schema.yml`?**

The dbt convention places schema.yml alongside model SQL. Three options:

- **A. The CLI (#9) finds it and passes the path/contents.** Renderer takes `existing_schema: str | None` (raw YAML text or `None` for "no file"). Renderer is pure.
- **B. Renderer accepts `existing_schema_path: Path | None`** and reads it (with `canonicalise_path`). Half-pure.
- **C. Renderer takes `project_dir` and discovers** by walking the manifest's `Model.original_file_path`. Fully integrated but couples renderer to manifest-loader semantics.

**Recommendation: A.** Keeps the diff renderer pure (string in, structured report out) — the CLI is the right place to know "where is this model's schema.yml on disk." Snapshot tests don't need a fake filesystem; just two strings. The CLI (#9) does the resolution and `canonicalise_path` once.

**Q4: Where does the proposed YAML come from?**

The renderer needs a canonical proposed-schema-YAML string. Options:

- **A. Renderer builds it itself** from `CandidateSchema` + `PruneResult` (filter to kept tests). Renderer owns the canonical-emit policy (key order, sort, quoting).
- **B. A separate `signalforge.diff.emit` (or `signalforge.draft.emit`) helper that's called before `render_diff`.** Two-step API.
- **C. Add an `emit_yaml()` method to `CandidateSchema`** in `signalforge.draft.models`. Couples draft to YAML.

**Recommendation: A.** The "canonical proposed YAML" is the renderer's concern: it owns the diff and therefore owns the emit policy. Keeping it in `signalforge.diff` (e.g., `signalforge.diff.emitter`) means the policy lives next to the diff machinery and the snapshot tests. Option C couples a model class to a serialisation format; option B fragments without buying anything.

**Q5: Does the diff renderer need a fail-closed audit log?**

The four prior stages all write `<stage>.jsonl`. Diff is the first stage where the rule auditor flagged this as conditional rather than mandatory.

- **A. No audit log; render is not a decision step. The decisions were already audited by prune/grade.** Skip the 7th AST scan.
- **B. Yes, write `diff.jsonl` per render call** (one event per `render_diff` invocation, summarising kept/dropped/flagged counts + `model_unique_id` + hashes). Add the 7th AST scan. Mirror prior stages' fail-closed pattern.
- **C. No JSONL but yes sidecar JSON** — the user-visible artefact for the GH Action consumer is itself the durable record. Apply the size cap + symlink-hardening but skip the JSONL.

**Recommendation: A** — but the user may want B for consistency. Render is genuinely not a decision; every "kept/dropped/flagged" tag the diff displays is a re-projection of an already-audited decision. The sidecar JSON (`diff.json`) is end-of-run, single-document, and natural for the v0.3 GH Action consumer; that's enough durable record for the renderer's purposes. **If we choose A, we still apply size cap + symlink-hardening to the sidecar write — just no per-call JSONL and no AST scan.**

**Q6: Should the renderer emit the sidecar JSON as well as the terminal output, or only on `--json`?**

- **A. Always emit the sidecar to `<project_dir>/.signalforge/diff.json` as the durable record.** Terminal output is the human view; sidecar is the machine view. `--json` flag swaps stdout to JSON.
- **B. Only emit the sidecar when `--json` is set.** Terminal output is the only output by default.
- **C. Always emit; `--json` flag determines whether stdout is also JSON.**

**Recommendation: C.** The sidecar is small (kB-scale per model) and is the v0.3 GH Action's input. Always writing it means the GH Action consumer doesn't need to parse stdout. `--json` controls stdout shape (terminal-friendly text vs. structured JSON), which is what the user asks for.

### Discovery answers (locked 2026-05-02 — user accepted all defaults)

- **Q1 → B.** `render_diff(model, candidate, prune_result, *, grading_report=None, config=None, existing_schema=None, output_path=None, sidecar_path=None, project_dir=None) -> DiffReport`. Grading is optional; renderer usable without a grade run.
- **Q2 → B.** Yellow "flagged" tier = kept artifacts with at least one criterion `passed=False` OR `score=None` (graceful-degrade). Joins on `artifact_id` produced by the renderer's own anchor formatter (mirrors grader DEC-009 of #7).
- **Q3 → A.** Renderer accepts `existing_schema: str | None` (raw YAML text). The CLI (#9) does the disk resolution and `canonicalise_path` once. Snapshot tests need only strings.
- **Q4 → A.** `signalforge.diff` owns the canonical YAML emit policy at `signalforge.diff.emitter` (next to diff machinery + snapshot tests).
- **Q5 → A.** No fail-closed JSONL audit log. Render is re-projection of already-audited decisions, not a decision step. Skip the 7th AST scan. **Sidecar JSON IS the durable record.**
- **Q6 → C.** Always emit sidecar `diff.json` (it's the v0.3 GH Action input); `--json` flag controls only stdout shape (terminal text vs. structured JSON to stdout). Sidecar gets size cap + symlink-hardening.

---

## Architecture Review

Five parallel reviews ran 2026-05-02 (security, performance, data-model + API design, observability, testing). Total: **3 blockers, 14 concerns, 38 passes**. Blockers must resolve before refinement closes; concerns become DEC-### items.

### Summary table (selected — full subagent reports archived in conversation)

| # | Area | Finding | Rating | Resolution path |
|---|---|---|---|---|
| AR-1 | API design | Three boundary checks at orchestrator entry are mandatory per `grade-layer.md` (lines 104–108): `candidate.name == model.name`, `prune_result.model_unique_id == model.unique_id`, `grading_report.model_unique_id == model.unique_id` (when provided). Locked signature does not surface them. | **blocker** | Add 3 typed mismatch errors to the `DiffError` hierarchy; raise before any work. |
| AR-2 | API design | Drift detectors mandatory for every `extra="ignore"` read-back model (`prune-engine.md` DEC-010, `grade-layer.md` DEC-010). Locked plan didn't enumerate them. | **blocker** | `tests/diff/test_drift_detector.py` + committed fixtures (`diff_report_v1.json`, `diff_entry_v1.json`). |
| AR-3 | API design | Public API surface in `signalforge.diff.__init__.py` not specified — particularly: are the three renderer classes (`AnsiRenderer`, `MarkdownRenderer`, `JsonRenderer`) public or `_`-prefixed? | **blocker** | Decide in refinement (see Q-AR-3). |
| AR-4 | Performance | GitHub PR-comment 65k-char limit. A typical 100-column model produces a unified diff well over 120k chars in Markdown form. Q6 → C (always-emit sidecar) helps but the Markdown body still needs a truncation strategy so the v0.3 GH Action can post a comment that doesn't fail. | concern | MarkdownRenderer truncates the diff body to ≤ 60k chars; appends `(N more lines — see sidecar diff.json)` footer; sidecar carries the full diff. Decide in refinement (see Q-AR-4). |
| AR-5 | Security | YAML deserialisation: `yaml.safe_load(existing_schema)` has no input-size cap. Billion-laughs and deeply-nested attacks pass through. | concern | `_EXISTING_SCHEMA_SIZE_LIMIT_BYTES = 1_000_000` (1 MB). Cap before `safe_load`. Typed `DiffInputTooLargeError`. |
| AR-6 | Security | ANSI escape injection in user-visible output. LLM-generated `description` / `why` / `evidence` strings can contain `\x1b[...m` and inject directly into terminal output (the logger gate does not apply to stdout). | concern | `_strip_ansi_escapes(text: str) -> str` helper; apply at AnsiRenderer entry to every user-content field BEFORE colorising. Test with `\x1b[31mEVIL\x1b[0m` payload. |
| AR-7 | Security | Markdown injection in PR-comment output (triple-backticks closing the fenced block; `</details>`; raw HTML; `[text](javascript:...)`). | concern | `_escape_markdown_scalar(text: str) -> str` — escape backticks, pipes, backslashes; no raw HTML pass-through. Test with all four payloads. |
| AR-8 | Security | Sidecar JSON size cap. Grade uses `_GRADE_SIDECAR_RECORD_LIMIT_BYTES = 1_000_000` for evidence-only payloads; diff sidecar contains the unified diff text which can be larger. | concern | `_DIFF_SIDECAR_RECORD_LIMIT_BYTES = 10_000_000` (10 MB; text-only — no warehouse rows). Pre-write check; typed `DiffSidecarRecordTooLargeError`. |
| AR-9 | Security | YAML emit edge cases: descriptions starting with `---`, `!`, embedding triple-backticks or newlines may emit ambiguously. | concern | Snapshot tests exercise each edge case; verify `yaml.safe_load(yaml.safe_dump(payload))` round-trips. |
| AR-10 | Performance | Terminal-width handling for `< 80` cols / piped output. The 120-char `why` cap from grade overflows narrow TTYs. | concern | AnsiRenderer detects narrow TTY (`<= 60` cols) and switches to compact mode: drop `why` column from table; emit each `why` as a follow-up line below the row it belongs to. |
| AR-11 | Performance | Soft warning when `len(existing_schema) > 10 MB` (mirrors `warehouse-adapters.md` DEC-023 profiles.yml warning at 1 MB). | concern | `_EXISTING_SCHEMA_WARN_AT = 10 * 1024 * 1024`; lazy-format JSON `WARNING` log. |
| AR-12 | API design | Orchestrator kwarg order: `existing_schema` is a data input, not a config knob, but currently lives mid-kwarg-block. | concern | Re-shape: `render_diff(model, candidate, prune_result, *, grading_report=None, existing_schema=None, config=None, output_path=None, sidecar_path=None, project_dir=None) -> DiffReport`. Keep all optionals keyword-only (consistent with grade); document that data inputs come first within the kw-only block. |
| AR-13 | API design | DiffConfig field set undefined; needs `extra="forbid"`. | concern | `context_lines: int = 3`, `max_why_chars: int = 80`, `narrow_terminal_threshold: int = 60`, `existing_schema_size_limit_bytes: int = 1_000_000`, `existing_schema_warn_at_bytes: int = 10_485_760`, `sidecar_size_limit_bytes: int = 10_000_000`. No mid-config nested types — single flat block. |
| AR-14 | API design | Renderer ABC signature: pure-return vs. streamed. Snapshot tests want `str` return for byte-for-byte assertion. | concern | `Renderer.render(report: DiffReport) -> str`. Orchestrator wires stdout / sidecar I/O. |
| AR-15 | API design | Tier modelling on `DiffEntry`: `Literal["kept", "dropped", "flagged"]` mirrors `prune.PruneDecision.decision` literal pattern. | concern | Adopt Literal. Reserve `flagged` only when `grading_report` is provided. |
| AR-16 | Observability | Log event policy: which events INFO vs. DEBUG vs. WARNING? | concern | INFO: "rendering diff for model X; N kept, M flagged"; INFO: "(no schema changes)" outcome; DEBUG: sidecar path/size; **no separate WARN before raising typed errors** (mirrors grade — exception IS the signal). |
| AR-17 | Observability | Sidecar fields beyond the obvious: include hashes of input artefacts so a reviewer can answer "what inputs produced this diff?" | concern | Add `candidate_hash`, `prune_result_hash`, `grading_report_hash` (each blake2b-8 of `model_dump_json(sort_keys=True)`). Plus `signalforge_version`, `audit_schema_version`, `run_id`, `duration_seconds`, counts. |
| AR-18 | Testing | Snapshot fixture matrix not enumerated. Need ~10 cases covering each surface × each input-condition combo. | concern | See Refinement Log for the locked matrix. |
| AR-X | (multiple) | The remaining 38 findings are explicit `pass` ratings — no action items beyond inheriting established patterns. | pass | — |

### Out-of-band passes worth calling out

- Sensitive-content scan of the sidecar: `PruneDecision.compiled_sql` and sample-row contents are NOT part of the sidecar (they're suppressed by `prune.PruneDecision.__repr__` per DEC-022 of #6 and the renderer doesn't pull them either). Sidecar carries LLM judgments + diff text only. PII-safe by construction.
- `difflib.unified_diff` performance: empirical 5000-line worst-case run in ~1.8 ms. Acceptable through v0.2.
- `Pydantic v2` field-declaration-order preservation in `model_dump()`: confirmed for v2.0+. Combined with `yaml.safe_dump(sort_keys=False)`, proposed-YAML emit is byte-deterministic for snapshot tests.
- Filesystem race on sidecar `O_TRUNC`: last-writer-wins is the project precedent (`grade-layer.md` DEC-006). Document; no code change.

## Refinement Log

### Open questions arising from architecture review

**Q-AR-3: Public vs. private renderer classes.**

- **A.** Public — export `Renderer`, `AnsiRenderer`, `MarkdownRenderer`, `JsonRenderer` from `signalforge.diff`. Users can compose / wrap them in their own apps; the ABC becomes part of the contract.
- **B.** Private — `_Renderer`, `_AnsiRenderer`, etc. Only `render_diff(...)` and a `render_kind: Literal["ansi","markdown","json"]` config field are public; renderer selection happens behind the orchestrator.
- **C.** Public ABC, private concretes — export `Renderer` (so users can plug in custom renderers) but keep the three default implementations private.

*Recommendation: B.* Mirrors `prune` / `grade` precedent (their `_compile_test` and `_artifact_id_for` are private; only the orchestrator + result types are public). v0.2 can promote to public if a real plug-in renderer use case lands. The CLI (#9) maps `--json` / TTY detection to the internal `render_kind`.

**Q-AR-4: Markdown 65k-char truncation strategy.**

- **A.** MarkdownRenderer always emits the full unified diff. Caller (v0.3 GH Action) handles truncation. Renderer is dumb.
- **B.** MarkdownRenderer hard-caps at `markdown_max_diff_chars = 60000` (configurable); when exceeded, truncates the diff body and appends `(N more lines — see sidecar diff.json for full diff)` footer. Kept/dropped/flagged table always renders fully.
- **C.** MarkdownRenderer exposes a `MarkdownDiffOverflowError` when output exceeds the cap; caller handles overflow.

*Recommendation: B.* The renderer owns Markdown's GitHub-comment compatibility; pushing it to the caller fragments the contract. The kept/dropped table is small (one line per artifact) and always fits, so the v0.3 PR comment always renders the table + a (possibly truncated) diff with a sidecar reference.

### Decisions

**DEC-001 — Orchestrator signature.** Final shape:

```python
def render_diff(
    model: Model,
    candidate: CandidateSchema,
    prune_result: PruneResult,
    *,
    grading_report: GradingReport | None = None,
    existing_schema: str | None = None,
    config: DiffConfig | None = None,
    output_path: Path | None = None,
    sidecar_path: Path | None = None,
    project_dir: Path | None = None,
) -> DiffReport: ...
```

**Why.** Mirrors `grade_artifacts` / `prune_tests` (`prune-engine.md`, `grade-layer.md` API alignment). Data-shaped optionals (`grading_report`, `existing_schema`) come first within the kw-only block; behaviour-knob `config` next; path-shaped optionals last. Resolves AR-12.

**How to apply.** All future renderer call sites use this shape; the CLI (#9) maps `--json` / `--no-color` flags to renderer parameters via `DiffConfig` and the `render_kind` selection.

**DEC-002 — Three boundary checks at orchestrator entry (resolves AR-1).** `render_diff` validates BEFORE any work:

1. `candidate.name == model.name` → raise `DiffCandidateModelMismatchError(candidate_name, model_name)` if mismatch.
2. `prune_result.model_unique_id == model.unique_id` → raise `DiffPruneResultModelMismatchError(prune_id, model_id)` if mismatch.
3. `grading_report.model_unique_id == model.unique_id` (when provided) → raise `DiffGradingReportModelMismatchError(grade_id, model_id)` if mismatch.

**Why.** Defence-in-depth at every typed-result handoff (`grade-layer.md` lines 104–108: "Apply the same check at any future orchestrator entry that takes a typed result from a sibling stage.").

**DEC-003 — Drift detectors mandatory (resolves AR-2).** `tests/diff/test_drift_detector.py` defines `StrictDiffReport`, `StrictDiffEntry` (`extra="forbid"` mirrors of the production `extra="ignore"` shapes), validated against committed fixtures `tests/fixtures/diff/diff_report_v1.json` and `tests/fixtures/diff/diff_entry_v1.json`. Production change = strict-mirror change = fixture refresh in the same commit.

**Why.** Mirrors `prune-engine.md` DEC-010 / `grade-layer.md` DEC-010 — the project's standard forward-compat gate.

**DEC-004 — Renderer concretes are private; orchestrator picks via `render_kind` (resolves AR-3, Q-AR-3 → B).** Public surface in `signalforge.diff.__init__` exposes `render_diff`, `load_diff_config`, `DiffReport`, `DiffEntry`, `DiffConfig`, the `DiffError` hierarchy. The three renderer classes live at `signalforge.diff._renderers` (private). `DiffConfig.render_kind: Literal["ansi","markdown","json"] = "ansi"` selects which one runs for stdout. The sidecar always uses the JSON renderer regardless.

**Why.** Mirrors `signalforge.prune._compile_test` / `signalforge.grade._artifact_id_for` precedent: only the typed-result + orchestrator + config + errors form the public contract. v0.2 can promote concrete renderers if a custom-renderer use case lands.

**DEC-005 — Markdown body truncation (resolves AR-4, Q-AR-4 → B).** `DiffConfig.markdown_max_diff_chars: int = 60_000`. When the rendered Markdown diff body exceeds the cap, MarkdownRenderer truncates the diff section (preserving complete hunks where possible) and appends:

```
... (N more lines truncated — see <project_dir>/.signalforge/diff.json for full diff)
```

Kept/dropped/flagged table always renders fully (one line per artifact; small).

**Why.** GitHub PR comments are 65 536 chars. The 60 000 cap leaves room for the table, the prelude, and the truncation footer.

**DEC-006 — YAML deserialisation safety (resolves AR-5).** `_EXISTING_SCHEMA_SIZE_LIMIT_BYTES = 1_000_000` (1 MB). `render_diff` checks `len(existing_schema.encode("utf-8")) <= limit` BEFORE calling `yaml.safe_load`. Oversize raises `DiffInputTooLargeError(size, limit)`.

**Why.** Pre-load size cap defends against billion-laughs / deep-nesting attacks regardless of `safe_load`'s constructor restrictions. Mirrors safety-layer / grade-layer "size cap before any open" pattern.

**DEC-007 — ANSI escape stripping in user content (resolves AR-6).** `signalforge.diff._ansi_safety.strip_ansi_escapes(text: str) -> str` — strict regex `r'\x1b\[[0-9;]*[a-zA-Z]'` (covers SGR plus all CSI). AnsiRenderer applies this to every user-content field (`description`, `rationale`, `evidence`, `reasoning`, `why`) BEFORE colourising. The renderer's own colour codes are added after stripping.

**Why.** The DEC-022 logger gate covers `_LOGGER` (audit channel). Stdout is a separate sink and needs its own defence. Stripping (not encoding) keeps the user-content readable; the renderer adds its own sanctioned colour codes on top.

**DEC-008 — Markdown injection escaping (resolves AR-7).** `signalforge.diff._markdown_safety.escape_markdown_scalar(text: str) -> str` — escapes triple-backticks (\`\`\` → \`\\`\`\`), pipe (`|` → `\|`), backslash (`\` → `\\`); raw HTML (`<...>`) is HTML-entity-encoded for table cells; **inside the fenced ```diff block, raw content passes through** (the diff is YAML; backticks etc. don't break a `diff` fence). Test fixtures exercise: triple-backticks in description, `</details>`, `[evil](javascript:...)`, pipe in column name.

**Why.** GitHub-flavored Markdown allows raw HTML by default; entity-encoding in table cells prevents an LLM-generated `<script>` tag from rendering. Inside the fenced diff block, GitHub doesn't interpret HTML — pass-through is safe and preserves the actual YAML.

**DEC-009 — Sidecar size cap (resolves AR-8).** `_DIFF_SIDECAR_RECORD_LIMIT_BYTES = 10_000_000` (10 MB; an order of magnitude above grade because diff text is bigger by nature). Pre-write size check; raises `DiffSidecarRecordTooLargeError(size, limit)` BEFORE any `os.open`. Per-document sidecar (single `os.write` looped on short returns, `os.fsync`, close, no internal try/except).

**Why.** Mirrors `grade-layer.md` DEC-006 with a higher cap calibrated for diff text. The 10 MB ceiling protects pathological 1000-column models without imposing on typical use.

**DEC-010 — DiffConfig field set + diff: namespace (resolves AR-13).** Top-level `signalforge.yml` key: `diff:` (claimed; sibling stages reserved & silently ignored — mirrors prune/grade DEC convention). `DiffConfig(extra="forbid")` carries:

```python
context_lines: int = 3              # difflib.unified_diff(n=...)
max_why_chars: int = 80             # truncation cap on per-row "why"
narrow_terminal_threshold: int = 60 # cols below which compact mode kicks in
markdown_max_diff_chars: int = 60_000
existing_schema_size_limit_bytes: int = 1_000_000
existing_schema_warn_at_bytes: int = 10_485_760  # 10 MB
sidecar_size_limit_bytes: int = 10_000_000
render_kind: Literal["ansi", "markdown", "json"] = "ansi"
respect_no_color_env: bool = True
```

`_DiffConfigFile` top-level → `extra="ignore"` (sibling stages); inner `diff:` content → `extra="forbid"` (typos like `contxt_lines` fail loud).

**Why.** Single flat config block per `prune-engine.md` DEC-020 / `grade-layer.md` DEC-029. Each cap is user-overridable downward only; the renderer doesn't read above its own defaults.

**DEC-011 — Renderer ABC pure-return signature (resolves AR-14).** `class Renderer(ABC): @abstractmethod def render(self, report: DiffReport) -> str`. Three concretes return text; orchestrator handles I/O (write to `output_path`, `sys.stdout`, sidecar). Snapshot tests assert `str == fixture_text` byte-for-byte.

**Why.** Pure-return is testable with no fake filesystem; mirrors prune/grade returning typed results rather than streaming output. Streaming is a v0.2 ask if real-world reports outgrow memory.

**DEC-012 — Tier model: `Literal["kept","dropped","flagged"]` (resolves AR-15).** `DiffEntry.tier: Literal["kept","dropped","flagged"]`. `flagged` is set only when `grading_report` is provided AND the entry's grading is below threshold (`passed=False` for any criterion OR `score=None` graceful-degrade). When `grading_report` is None, every kept entry has `tier="kept"`; no entry can be `flagged` without a grading run.

**Why.** Mirrors `prune.PruneDecision.decision: Literal["kept","dropped"]`. The `Literal` is exhaustively switchable by the renderer without enum overhead.

**DEC-013 — Terminal-width compact mode (resolves AR-10).** When `shutil.get_terminal_size().columns < config.narrow_terminal_threshold` (default 60), AnsiRenderer drops the `why` column from the kept/dropped table and emits each `why` as a wrapped follow-up line below its row. Snapshot fixture covers a 40-col case.

**Why.** Narrow terminals (CI logs, embedded terminals, pipes with FORCE_COLOR) need a layout that doesn't truncate to nonsense. Compact mode preserves all information at the cost of vertical space.

**DEC-014 — Soft warning on large existing schema (resolves AR-11).** `_LOGGER.warning("large existing schema.yml: %s", json.dumps({"bytes": n, "model_unique_id": ..., "warn_at": ...}))` when `len(existing_schema.encode("utf-8")) > config.existing_schema_warn_at_bytes`. **Lazy-format JSON** — never f-string. Hard cap (DEC-006) raises a typed error well above this warning level.

**Why.** Mirrors `warehouse-adapters.md` DEC-023 profiles.yml warning. Operators get a heads-up before the hard cap trips.

**DEC-015 — Log event policy (resolves AR-16).** Two `_LOGGER` events in the entire diff layer:

1. `INFO`: `"rendered diff: %s"` with `json.dumps({"model_unique_id": ..., "kept": k, "dropped": d, "flagged": f, "has_existing_schema": bool, "duration_seconds": s})`. Emitted at end of `render_diff` happy path.
2. `WARNING`: the DEC-014 large-schema warning.

No separate `WARNING` before raising typed errors — the exception IS the signal (mirrors grade DEC-006).

**Why.** Stdout is the user channel; the `_LOGGER` channel is for operators / log aggregators. INFO + the conditional WARN cover both successful runs and the soft-cap signal without flooding logs.

**DEC-016 — Sidecar field set with reproducibility hashes (resolves AR-17).** `DiffReport` carries (and serialises to sidecar):

```python
schema_version: Literal[1] = 1   # forward-compat sentinel
audit_schema_version: int = 1    # mirror prior stages
signalforge_version: str
model_unique_id: str
run_id: str                      # uuid4 stamped at orchestrator entry
duration_seconds: float
proposed_yaml: str
existing_yaml: str | None
unified_diff: str
entries: tuple[DiffEntry, ...]
kept_count: int
dropped_count: int
flagged_count: int
has_existing_schema: bool
candidate_hash: str              # blake2b-8 of candidate.model_dump_json(sort_keys=True)
prune_result_hash: str           # blake2b-8 of prune_result.model_dump_json(sort_keys=True)
grading_report_hash: str | None  # blake2b-8 if provided; None if grading_report is None
```

`DiffEntry` carries `artifact_id`, `test_type: str | None`, `tier`, `drop_reason: DropReason | None`, `why: str`, `score: float | None`, `passed: bool | None`. Both models are `frozen=True, extra="ignore"`.

**Why.** Reproducibility-hash discipline mirrors safety/draft/prune/grade. A reviewer querying "what inputs produced this diff?" reads three hashes and can reconstruct from the upstream JSONL audits. The `audit_schema_version` lets v0.2 readers gate.

**DEC-017 — Snapshot fixture matrix (resolves AR-18).** Ten cases under `tests/fixtures/diff/`:

1. `full_with_grade` — happy path: kept + dropped + flagged, grading provided, ANSI surface.
2. `full_with_grade.md` — same inputs, MarkdownRenderer surface.
3. `full_with_grade.json` — same inputs, JsonRenderer surface (the sidecar shape).
4. `no_existing_schema` — `existing_schema=None`; unified diff sources from `/dev/null`.
5. `kept_only` — every artifact `decision="kept"`; empty dropped table.
6. `dropped_only` — every artifact `decision="dropped"`; empty kept table.
7. `no_grading_report` — `grading_report=None`; no flagged tier; per-row score columns absent.
8. `plain_no_color` — ANSI surface with `NO_COLOR=1` (asserts no escape codes in output).
9. `narrow_terminal` — 40-col TTY (DEC-013 compact mode).
10. `injection_payloads` — descriptions contain `\x1b[31m`, triple-backticks, `</details>`, pipe, `---`, `!tag`. Verifies DEC-007 + DEC-008 + AR-9 escaping.

Companion regen script at `tests/fixtures/diff/regenerate.sh` (mirrors `tests/fixtures/regenerate.sh`).

**Why.** 10 cases = 3 × 3 grid (3 surfaces × happy/edge/hostile) plus the no-existing-schema branch, plus the injection coverage — small enough to maintain, broad enough to catch regressions. Plain text fixtures, byte-for-byte diff (no syrupy dep).

**DEC-018 — Single AST scan extension is NOT added (Q5 → A confirmed by AR-O1).** The 6th AST scan from #7 (gating `GradeEvent`) is the last of its kind in v0.1. No `DiffEvent` exists; the sidecar `DiffReport` is read-back, gated by the drift detector (DEC-003) instead.

**Why.** The diff renderer projects already-audited decisions; there's no new audit-event class to gate. If v0.2 introduces a per-render audit JSONL (e.g., for multi-model batched runs), revisit and add the 7th scan then.

**DEC-019 — Logger grep gate extends to `src/signalforge/diff/`.** `tests/llm/test_logger_grep_gate.py` adds `_DIFF_DIR`, scanning the 5th directory.

**Why.** Universal rule across all subpackages with `_LOGGER` calls — `safety-layer.md` DEC-022 / `llm-drafter.md` DEC-011 / `prune-engine.md` DEC-017 / `grade-layer.md` DEC-029.

**DEC-020 — Custom `__repr__` on result-shaped models.** `DiffReport.__repr__` shows `model_unique_id`, `kept_count`, `dropped_count`, `flagged_count`, `has_existing_schema`, `duration_seconds`. `DiffEntry.__repr__` shows `artifact_id`, `tier`, `drop_reason`, `score`. Raw YAML, unified diff, and prose `why`/`evidence`/`reasoning` are field-accessible only.

**Why.** Mirrors `prune-engine.md` DEC-022 / `grade-layer.md` DEC-022 — accidental log dumps stay metadata-sized.

**DEC-021 — Color-precedence for the AnsiRenderer.** Order of precedence (highest first): `DiffConfig.respect_no_color_env=False` (forces colour regardless) → `--no-color` CLI flag (wired by #9 to a renderer param) → `FORCE_COLOR=1` env → `NO_COLOR` env (any value) → `sys.stdout.isatty()`. ANSI stripping (DEC-007) runs UNCONDITIONALLY — the precedence only decides whether the renderer's own colour codes get emitted.

**Why.** Standard NO_COLOR spec (`https://no-color.org/`) plus the explicit `--no-color` kill switch. Decoupling user-content stripping from colour emission is the security boundary (AR-7 / DEC-007).

## Detailed Breakdown

Fourteen stories: 12 implementation + Quality Gate + Patterns & Memory. Validation command (per `CLAUDE.md`) appears in every implementation story's acceptance criteria:

```bash
ruff check . && ruff format --check . && pyright && pytest
```

Stories are ordered backend-first → orchestration → rendering surfaces → tests → public API → QG → memory, mirroring #7's super-plan ordering. Each story is sized for one Ralph context window.

### US-001 — Error hierarchy

**Description.** Create the `DiffError` base + typed subclasses. Every error carries `remediation: str` rendered via the project-standard `↳ Remediation:` line. User-supplied strings render via `repr()` to defend against ANSI/control-char injection in error messages.

**Traces to:** DEC-002, DEC-006, DEC-009.

**Files:**

- `src/signalforge/diff/errors.py` (new)

**Subclasses:**

- `DiffError` (base; `Exception`)
- `DiffCandidateModelMismatchError(candidate_name, model_name)` — DEC-002 boundary check
- `DiffPruneResultModelMismatchError(prune_id, model_id)` — DEC-002
- `DiffGradingReportModelMismatchError(grade_id, model_id)` — DEC-002
- `DiffInputTooLargeError(size, limit)` — DEC-006 (existing schema YAML)
- `DiffSidecarRecordTooLargeError(size, limit)` — DEC-009
- `DiffSidecarWriteError(cause)` — wraps OS errors from the sidecar writer

**Acceptance criteria:**

- All 7 classes exported from `signalforge.diff.errors`.
- `DiffError("msg", remediation="…").__str__()` renders both message and `↳ Remediation:` line, mirroring `manifest-readers.md` DEC-007.
- User-supplied strings (e.g., `model_name`) render via `repr()` (mirrors `warehouse-adapters.md` Error-hierarchy).
- Validation command passes.

**TDD:**

- Each subclass instantiable with kwargs; `str(err)` contains a remediation line.
- `DiffCandidateModelMismatchError("a", "b").__str__()` quotes both via `repr()` (e.g., `'a'`, `'b'`).
- A control-char-bearing model_name (`"\x1b[31mevil"`) renders as `'\\x1b[31mevil'` (escaped), not as a raw escape.

**Done when:** `ruff check signalforge.diff.errors` and `pyright` clean; tests pass.

**Depends on:** none (leaf).

---

### US-002 — Result models (`DiffReport`, `DiffEntry`)

**Description.** Define the two read-back shapes. Both `frozen=True, extra="ignore"`. `DiffEntry.tier: Literal["kept","dropped","flagged"]` per DEC-012. Custom `__repr__` per DEC-020 redacts prose / large fields.

**Traces to:** DEC-012, DEC-016, DEC-020.

**Files:**

- `src/signalforge/diff/models.py` (new)

**Field set (per DEC-016):**

`DiffEntry`:

- `artifact_id: str` (dotted format from US-006)
- `test_type: str | None` (e.g., `"not_null"`; `None` for column-doc artifacts)
- `tier: Literal["kept", "dropped", "flagged"]`
- `drop_reason: DropReason | None` (the prune literal; `None` when `tier != "dropped"`)
- `why: str` (one-line; truncated upstream)
- `score: float | None` (from `GradingResult.score` if graded)
- `passed: bool | None` (from `GradingResult.passed` if graded)

`DiffReport`:

- `schema_version: Literal[1] = 1`
- `audit_schema_version: int = 1`
- `signalforge_version: str`
- `model_unique_id: str`
- `run_id: str` (uuid4 stamped at orchestrator entry)
- `duration_seconds: float`
- `proposed_yaml: str`
- `existing_yaml: str | None`
- `unified_diff: str`
- `entries: tuple[DiffEntry, ...]`
- `kept_count: int`, `dropped_count: int`, `flagged_count: int` (computed)
- `has_existing_schema: bool` (computed: `existing_yaml is not None`)
- `candidate_hash: str`, `prune_result_hash: str`, `grading_report_hash: str | None`

**Acceptance criteria:**

- Both models `frozen=True, extra="ignore"`.
- `DiffEntry.tier` rejects values outside `{"kept","dropped","flagged"}` at validation time.
- `DiffReport.__repr__` shows only `model_unique_id`, `kept_count`, `dropped_count`, `flagged_count`, `has_existing_schema`, `duration_seconds`. Raw YAML / diff text / entries NOT in repr.
- `DiffEntry.__repr__` shows only `artifact_id`, `tier`, `drop_reason`, `score`. Prose `why` NOT in repr.
- Validation command passes.

**TDD:**

- `DiffEntry(tier="quux", ...)` raises `ValidationError`.
- `repr(report)` does not contain the unified diff text.
- `repr(entry)` does not contain the `why` string.
- `model_dump_json()` round-trips back via `model_validate_json()`.

**Done when:** Validation passes; reprs verified.

**Depends on:** US-001.

---

### US-003 — `DiffConfig` + `load_diff_config`

**Description.** Introduce the `diff:` namespace in `signalforge.yml`. `DiffConfig(extra="forbid")` carries the nine knobs from DEC-010; `_DiffConfigFile` top-level `extra="ignore"` (sibling stages reserved). `load_diff_config(project_dir, path=None) -> DiffConfig` matches the prior-stage signature.

**Traces to:** DEC-010.

**Files:**

- `src/signalforge/diff/config.py` (new)

**Field set (DEC-010):**

```python
context_lines: int = 3
max_why_chars: int = 80
narrow_terminal_threshold: int = 60
markdown_max_diff_chars: int = 60_000
existing_schema_size_limit_bytes: int = 1_000_000
existing_schema_warn_at_bytes: int = 10_485_760
sidecar_size_limit_bytes: int = 10_000_000
render_kind: Literal["ansi", "markdown", "json"] = "ansi"
respect_no_color_env: bool = True
```

**Acceptance criteria:**

- Typo in field name (e.g., `contxt_lines: 5`) raises `ValidationError` at `_DiffConfigFile.diff` validation.
- Sibling top-level keys (`safety:`, `llm:`, `prune:`, `grade:`) silently ignored.
- Missing config file returns `DiffConfig()` defaults.
- Explicit `path` that doesn't exist raises a typed error (mirrors `load_grade_config`).
- Validation command passes.

**TDD:**

- Loading `signalforge.yml` with `diff: {context_lines: 5}` returns `DiffConfig(context_lines=5, ...defaults...)`.
- Loading with `diff: {contxt_lines: 5}` (typo) fails loud.
- Loading with `safety: {...}` only (no `diff:` key) returns defaults.
- `load_diff_config(project_dir=tmp_path, path=tmp_path / "missing.yml")` raises typed error.

**Done when:** Validation passes; ValidationError fixture covers the typo.

**Depends on:** US-001.

---

### US-004 — Safety helpers (`_ansi_safety`, `_markdown_safety`)

**Description.** Two private modules with one function each. Strict regex; no normalisation, no whitespace handling — just escape/strip. Required by AnsiRenderer (DEC-007) and MarkdownRenderer (DEC-008).

**Traces to:** DEC-007, DEC-008.

**Files:**

- `src/signalforge/diff/_ansi_safety.py` (new)
- `src/signalforge/diff/_markdown_safety.py` (new)

**Functions:**

- `strip_ansi_escapes(text: str) -> str` — applies `re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)`. Covers SGR + all CSI.
- `escape_markdown_scalar(text: str, *, in_table_cell: bool = False) -> str` — escapes ` ``` ` → `\`\`\``, `|` → `\|`, `\` → `\\`. When `in_table_cell=True`, additionally HTML-entity-encodes `<` → `&lt;` and `>` → `&gt;` to neutralise raw HTML.

**Acceptance criteria:**

- `strip_ansi_escapes("\x1b[31mEVIL\x1b[0m")` → `"EVIL"`.
- `escape_markdown_scalar("```code```")` → `"\\`\\`\\`code\\`\\`\\`"`.
- `escape_markdown_scalar("</details>", in_table_cell=True)` → `"&lt;/details&gt;"`.
- `escape_markdown_scalar("col | name", in_table_cell=True)` → `"col \\| name"`.
- Validation command passes.

**TDD:**

- ANSI strip: idempotent on already-clean strings; preserves Unicode (`"héllo"` → `"héllo"`).
- Markdown escape: backticks, pipe, backslash, `</details>`, `[evil](javascript:...)` (the link survives escaping; GitHub's own sanitiser handles `javascript:` schemes — comment in test).
- Both functions never raise; pure str → str.

**Done when:** Both functions covered; validation passes.

**Depends on:** none (leaf).

---

### US-005 — Canonical YAML emitter (`_emitter`)

**Description.** Build the proposed YAML string from `CandidateSchema` + `PruneResult` (filter to `decision="kept"`). Deterministic, byte-identical across runs. Survives YAML edge-case descriptions (`---`, `!`, triple-backticks, newlines) via PyYAML's default-style auto-quoting.

**Traces to:** DEC-010 (config knob), AR-9.

**Files:**

- `src/signalforge/diff/_emitter.py` (new)

**Function:**

`emit_proposed_yaml(candidate: CandidateSchema, prune_result: PruneResult) -> str`

Algorithm:

1. Filter `prune_result.decisions` to `decision == "kept"`; build a set of kept `(test_anchor, test_type, args_hash)` triples.
2. Walk `candidate.columns` in declaration order. For each column, emit `name → description → tests` (filtered to kept tests).
3. Within a column's `tests`, sort by `(test_type, args_hash)` for determinism.
4. Append model-level `tests` (filtered to kept) at the bottom of the model block.
5. `yaml.safe_dump(payload, sort_keys=False, default_flow_style=False, width=4096, allow_unicode=True)`.

**Acceptance criteria:**

- Output starts with `version: 2\nmodels:\n  - name: …`.
- Determinism: two runs with identical inputs produce byte-identical strings.
- `yaml.safe_load(emit_proposed_yaml(...))` round-trips successfully.
- Edge-case descriptions (`---`, `!something`, `text with ``` backticks`, `multi\nline`) round-trip.
- Validation command passes.

**TDD:**

- Determinism test: two calls return identical bytes.
- Round-trip test for each of: `"---"`, `"!custom"`, `"\`\`\`code\`\`\`"`, `"multi\nline"`, `"plain text"`.
- Filtered tests: a candidate with 5 tests where prune dropped 2 → emitted YAML has 3 tests in canonical order.
- Column order: candidate has columns `[a, b, c]` → emitted YAML has them in same order regardless of test-sort.

**Done when:** Determinism + round-trip cases pass.

**Depends on:** none (consumes existing `signalforge.draft.models`, `signalforge.prune.models`).

---

### US-006 — Artifact-id formatter (`_artifact_id`)

**Description.** Mirror `signalforge.grade._artifact_id_for` exactly so the renderer can join `GradingResult.artifact_id` to its own `DiffEntry.artifact_id`. Six dotted shapes per grade DEC-009 + collision-disambiguating `args_hash` suffix per the grade post-QG fix.

**Traces to:** DEC-016 (DiffEntry.artifact_id), grade DEC-009.

**Files:**

- `src/signalforge/diff/_artifact_id.py` (new)

**Functions:**

- `artifact_id_for_column_doc(col: str) -> str` → `f"column.{col}.description"`
- `artifact_id_for_column_rationale(col: str) -> str` → `f"column.{col}.rationale"`
- `artifact_id_for_model_doc() -> str` → `"model.description"`
- `artifact_id_for_model_rationale() -> str` → `"model.rationale"`
- `artifact_id_for_test(test: CandidateTest, *, scope_hash: bool) -> str` — handles `test.column.<col>.<type>(.<args_hash>?)` and `test.model.<type>(.<args_hash>?)` cases.

**Acceptance criteria:**

- Output for representative inputs matches `signalforge.grade.engine._artifact_id_for` byte-for-byte.
- Two `accepted_values` tests on the same column with different `values` lists get distinct `args_hash`-suffixed artifact_ids (mirrors grade post-QG fix).
- Validation command passes.

**TDD:**

- Cross-stage parity test: import grade's helper; run both helpers on the same `CandidateColumn` + tests; assert outputs equal.
- Collision test: two `accepted_values(values=["a"])` and `accepted_values(values=["b"])` on column `x` → distinct ids with 8-hex suffix.
- Single test of a kind without collision: no `args_hash` suffix.

**Done when:** Cross-stage parity test passes.

**Depends on:** none (leaf).

---

### US-007 — Sidecar writer (`_sidecar`)

**Description.** Write `DiffReport` to a sidecar JSON file with grade-style fail-closed semantics. Pre-write size cap; symlink-hardened path canonicalisation at writer entry; `O_WRONLY|O_CREAT|O_TRUNC|0o600`; single `os.write`; `os.fsync`; close. **No internal try/except** — propagation is the defence (mirrors `grade-layer.md` DEC-006).

**Traces to:** DEC-009, AR-8.

**Files:**

- `src/signalforge/diff/_sidecar.py` (new)

**Functions:**

- `write_sidecar(report: DiffReport, *, sidecar_path: Path, project_dir: Path) -> None`
- `_DIFF_SIDECAR_RECORD_LIMIT_BYTES: Final[int] = 10_000_000` (DEC-009)

Algorithm:

1. Canonicalise `sidecar_path` against `project_dir` via `signalforge.warehouse._path_safety.canonicalise_path`. Failure → `DiffSidecarWriteError(cause=…)`.
2. Serialise `report.model_dump_json(indent=2)` to bytes.
3. If `len(payload_bytes) > _DIFF_SIDECAR_RECORD_LIMIT_BYTES` → raise `DiffSidecarRecordTooLargeError(size, limit)` BEFORE any `os.open`.
4. `fd = os.open(canonical_path, O_WRONLY|O_CREAT|O_TRUNC, 0o600)`.
5. Loop `os.write` until full payload written; `os.fsync(fd)`; close in `finally`.

**Acceptance criteria:**

- Oversize payload raises `DiffSidecarRecordTooLargeError` and the path doesn't exist after the call.
- Symlink pointing outside `project_dir` raises `DiffSidecarWriteError` with no on-disk artefact.
- Happy path produces a file with mode `0o600`, exact bytes, fsync'd.
- No `try/except` around `os.write` / `os.fsync` (only `try/finally` for `os.close`).
- Validation command passes.

**TDD:**

- Test: write a 100-byte report; `Path(p).read_bytes() == payload_bytes`; `Path(p).stat().st_mode & 0o777 == 0o600`.
- Test: stub `report.model_dump_json` to return a 11 MB string → assert `DiffSidecarRecordTooLargeError` and `not p.exists()`.
- Test: create `sidecar_path = project_dir / "evil.json"` symlinked to `/tmp/somewhere`; assert `DiffSidecarWriteError` and the target file is not created.
- AST/grep test in `test_sidecar.py`: scan `_sidecar.py` source for `try:` / `except` patterns; assert only the `try/finally` for `os.close` exists. (Mirrors `grade-layer.md` audit-write convention.)

**Done when:** Three negative paths covered + happy path + AST defence test.

**Depends on:** US-001, US-002.

---

### US-008 — `AnsiRenderer`

**Description.** Render a `DiffReport` to terminal-targeted text. Apply `strip_ansi_escapes` to every user-content field UNCONDITIONALLY (DEC-007 — runs even when colour is off). Colour-precedence per DEC-021. Narrow-TTY compact mode below `config.narrow_terminal_threshold` (DEC-013) drops the `why` column from the table and emits each `why` as a follow-up line.

**Traces to:** DEC-004, DEC-007, DEC-011, DEC-013, DEC-021.

**Files:**

- `src/signalforge/diff/_renderers.py` (new — module-level `Renderer` ABC + this class).

**Class:**

```python
class Renderer(ABC):
    @abstractmethod
    def render(self, report: DiffReport) -> str: ...

class AnsiRenderer(Renderer):
    def __init__(self, *, config: DiffConfig, force_color: bool | None = None): ...
```

`force_color: None` means "respect env"; `True`/`False` overrides. The CLI (#9) maps `--no-color` → `force_color=False`.

**Acceptance criteria:**

- ANSI strip applied to `why`, `description`, `evidence`, `reasoning` BEFORE adding colour codes.
- Colour-precedence (DEC-021): `respect_no_color_env=False` > `force_color=False` > `FORCE_COLOR` > `NO_COLOR` > `isatty()`.
- Narrow-TTY mode triggers when `shutil.get_terminal_size().columns < config.narrow_terminal_threshold`. Compact layout: table without `why` column; `why` printed below row, indented.
- Output structure: header line, kept/dropped/flagged table, blank line, unified-diff section.
- Validation command passes.

**TDD:**

- `description="\x1b[31mEVIL\x1b[0m"` → rendered output contains the literal text "EVIL" but **not** the escape codes from the description (the renderer's own `\x1b[32m` for "kept" colour codes ARE present and load-bearing).
- `NO_COLOR=1` set → no ANSI codes in output.
- `FORCE_COLOR=1` + `isatty()=False` → ANSI codes present.
- `force_color=False` → no ANSI codes regardless of env.
- Monkey-patch `shutil.get_terminal_size` to `(40, 24)` → table omits `why` column; `why` lines appear below.
- Snapshot fixture cases 1, 2, 4–10 (DEC-017) match byte-for-byte.

**Done when:** Snapshot fixtures pass; the four colour-precedence cases pass.

**Depends on:** US-002, US-004.

---

### US-009 — `MarkdownRenderer`

**Description.** Render a `DiffReport` to GitHub-flavored Markdown for the v0.3 PR-comment consumer. Hard cap at `config.markdown_max_diff_chars` (DEC-005) with truncation footer. Pipe-table for kept/dropped/flagged; user content escaped via `escape_markdown_scalar(..., in_table_cell=True)` (DEC-008).

**Traces to:** DEC-004, DEC-005, DEC-008, DEC-011.

**Files:**

- `src/signalforge/diff/_renderers.py` (extend with `MarkdownRenderer`).

**Acceptance criteria:**

- Output is valid GitHub-flavored Markdown (table + ```diff fenced block).
- Triple-backticks in a description are escaped in the table cell; backticks in the unified-diff body pass through (the diff is YAML; the ```diff fence wraps it).
- HTML entity-encoded for `<` / `>` in table cells.
- Pipe character escaped in table cells.
- When unified-diff body would exceed `markdown_max_diff_chars`, truncate at the last complete hunk boundary; append `... (N more lines truncated — see <project_dir>/.signalforge/diff.json for full diff)` footer.
- Kept/dropped/flagged table always renders fully (no truncation).
- Validation command passes.

**TDD:**

- Description with `</details>` → output contains `&lt;/details&gt;` in cell.
- Description with `col | name` → output contains `col \| name` in cell.
- Snapshot fixture case 1.md (full happy path).
- Truncation case: stub `unified_diff` to a 100k-char string; rendered output ≤ 65k chars total; ends with the footer.
- A diff that fits comfortably (e.g., 5 lines) emits without truncation footer.

**Done when:** Snapshot + truncation cases pass.

**Depends on:** US-002, US-004.

---

### US-010 — Orchestrator `render_diff` + `JsonRenderer`

**Description.** The user-facing entry point. Wires boundary checks (DEC-002), input-size cap (DEC-006), large-schema soft warning (DEC-014), `_emitter`, `difflib.unified_diff`, `_artifact_id`, hash computation, renderer selection (`config.render_kind`), sidecar write. Single INFO log at end of happy path (DEC-015). `JsonRenderer.render` is `report.model_dump_json(indent=2)` plus a guard against ANSI bytes — small enough to fold into this story.

**Traces to:** DEC-001, DEC-002, DEC-004, DEC-006, DEC-009, DEC-014, DEC-015, DEC-016, DEC-021.

**Files:**

- `src/signalforge/diff/engine.py` (new — orchestrator).
- `src/signalforge/diff/_renderers.py` (extend with `JsonRenderer`).

**Function:**

```python
def render_diff(
    model: Model,
    candidate: CandidateSchema,
    prune_result: PruneResult,
    *,
    grading_report: GradingReport | None = None,
    existing_schema: str | None = None,
    config: DiffConfig | None = None,
    output_path: Path | None = None,
    sidecar_path: Path | None = None,
    project_dir: Path | None = None,
) -> DiffReport: ...
```

Algorithm:

1. Resolve `config` (default `DiffConfig()`), `project_dir` (default `Path.cwd()`).
2. Boundary checks (DEC-002): mismatched ids raise typed errors before any work.
3. If `existing_schema is not None`: size check (DEC-006); soft-warn (DEC-014); `yaml.safe_load` to validate parseability (don't use the parsed value — just confirm it parses; we diff strings).
4. Build canonical proposed YAML via `_emitter.emit_proposed_yaml`.
5. Build canonical existing YAML if `existing_schema` present (round-trip through `_emitter._normalise_existing` to strip noise — but keep the original text alongside for `DiffReport.existing_yaml`).
6. Compute `unified_diff` via `difflib.unified_diff(existing_lines, proposed_lines, fromfile, tofile, n=config.context_lines)`.
7. Build per-artifact `DiffEntry` rows by walking `prune_result.decisions` and joining grade results via `_artifact_id`.
8. Compute `kept_count`, `dropped_count`, `flagged_count`. Hash inputs (`blake2b(model_dump_json(sort_keys=True), digest_size=8)`).
9. Construct `DiffReport`. Call `report.__class__._validate` (Pydantic auto).
10. Render to selected surface via `config.render_kind` → `Renderer` instance.
11. Write rendered text to `output_path` (default stdout if `output_path is None`).
12. Resolve `sidecar_path` (default `<project_dir>/.signalforge/diff.json`); call `_sidecar.write_sidecar(report, sidecar_path=…, project_dir=…)`.
13. Single `_LOGGER.info(...)` event per DEC-015.
14. Return `report`.

`JsonRenderer.render(report) -> str` simply returns `report.model_dump_json(indent=2)`. ANSI bytes never reach JSON because `_strip_ansi_escapes` runs at JSON-render entry on user-content fields (defence-in-depth: even if a caller skips AnsiRenderer, JSON output stays clean).

**Acceptance criteria:**

- All three boundary checks raise BEFORE any other work (test by injecting mismatched ids and asserting `_emitter` / `difflib` are not called via mocks).
- Oversize `existing_schema` raises `DiffInputTooLargeError` BEFORE `yaml.safe_load`.
- `existing_schema=None` produces a unified diff with `--- /dev/null` header.
- Sidecar always written when `sidecar_path` provided OR resolved-default exists.
- `_LOGGER.info` event present at happy-path end; no f-string interpolation (lazy-format JSON).
- Symlink-escape on `sidecar_path` raises `DiffSidecarWriteError`.
- `config.render_kind="json"` makes stdout JSON; sidecar always JSON.
- Validation command passes.

**TDD:**

- Three boundary-check negative tests (one per mismatch).
- `len(existing_schema) = 1_000_001` → `DiffInputTooLargeError`.
- `existing_schema=None` happy path → `report.has_existing_schema is False`; `report.unified_diff` has `/dev/null`.
- `grading_report=None` → no entries with `tier="flagged"`.
- Logger event captured via `caplog`; structure is `{"model_unique_id":..., "kept":..., "dropped":..., "flagged":..., "has_existing_schema":..., "duration_seconds":...}`.

**Done when:** Full happy path + each negative path covered.

**Depends on:** US-001, US-002, US-003, US-004, US-005, US-006, US-007, US-008, US-009.

---

### US-011 — Snapshot fixtures + `regenerate.sh` + drift detector

**Description.** Commit the 10 snapshot fixtures from DEC-017 + 2 drift-detector fixtures. Add `tests/fixtures/diff/regenerate.sh` mirroring `tests/fixtures/regenerate.sh`. Add `tests/diff/test_drift_detector.py` with `StrictDiffReport` / `StrictDiffEntry` mirrors per DEC-003.

**Traces to:** DEC-003, DEC-017.

**Files:**

- `tests/fixtures/diff/inputs/{1..10}/{model.json,candidate.json,prune.json,grading.json}` (input bundles)
- `tests/fixtures/diff/{1..10}.{ansi,md,json,txt}` (rendered fixtures)
- `tests/fixtures/diff/diff_report_v1.json`, `tests/fixtures/diff/diff_entry_v1.json` (drift fixtures)
- `tests/fixtures/diff/regenerate.sh`
- `tests/diff/test_renderers.py` (snapshot tests)
- `tests/diff/test_drift_detector.py` (drift tests)

**Cases (DEC-017):**

1. `full_with_grade.ansi` — happy path: kept + dropped + flagged.
2. `full_with_grade.md`
3. `full_with_grade.json`
4. `no_existing_schema.ansi`
5. `kept_only.ansi`
6. `dropped_only.ansi`
7. `no_grading_report.ansi`
8. `plain_no_color.txt` (NO_COLOR=1)
9. `narrow_terminal.ansi` (40-col TTY)
10. `injection_payloads.ansi` + `injection_payloads.md`

**Acceptance criteria:**

- `regenerate.sh` runs in <5 s and produces byte-identical fixtures (modulo `signalforge_version`-stamped fields, which the script masks).
- Each snapshot test asserts byte-equality (via `pathlib.Path(...).read_text() == fixture_text`).
- `StrictDiffReport.model_validate(fixture)` and `DiffReport.model_validate(fixture)` both succeed.
- Adding a planted extra field to `diff_report_v1.json` makes the strict-validate test fail.
- Validation command passes.

**TDD:**

- Snapshot byte-equality for each case.
- Drift planted-extra-field test (one per model).

**Done when:** All 10 snapshot tests + 2 drift tests green.

**Depends on:** US-008, US-009, US-010.

---

### US-012 — Public API + `docs/diff-ops.md` + logger grep gate

**Description.** Export the public surface from `src/signalforge/diff/__init__.py`; add operational docs at `docs/diff-ops.md` mirroring `docs/grade-ops.md`; extend the project-wide logger grep gate at `tests/llm/test_logger_grep_gate.py` to scan `src/signalforge/diff/` (the 5th directory per DEC-019).

**Traces to:** DEC-004, DEC-019.

**Files:**

- `src/signalforge/diff/__init__.py` (new)
- `docs/diff-ops.md` (new)
- `tests/llm/test_logger_grep_gate.py` (extend `_DIFF_DIR`)

**Public surface (per DEC-004):**

- `render_diff`
- `load_diff_config`
- `DiffReport`, `DiffEntry`, `DiffConfig`
- `DiffError` and 6 subclasses (US-001)

**Acceptance criteria:**

- `from signalforge.diff import render_diff, DiffReport, DiffConfig, load_diff_config` succeeds.
- `signalforge.diff.AnsiRenderer` raises `AttributeError` (concretes private per DEC-004).
- `docs/diff-ops.md` covers: orchestrator usage, config knobs, error semantics, sidecar shape, fixture matrix.
- Grep gate test passes for `src/signalforge/diff/`.
- A planted f-string violation in `signalforge.diff.engine` makes the gate fail.
- Validation command passes.

**TDD:**

- Import test: each public name resolves; each private name raises.
- Grep gate planted-violation test.
- Docs lint (no broken internal links).

**Done when:** All assertions green; docs reviewed against the prior `docs/grade-ops.md` for parity.

**Depends on:** US-001 through US-011.

---

### US-013 — Quality Gate

**Description.** Run code review four passes across the full changeset. Fix every real bug found each pass. Validation command must pass at the end.

**Traces to:** all of US-001 … US-012.

**Acceptance criteria:**

- Code reviewer agent invoked four times against the full diff. Each pass produces a written report; each real finding fixed in a follow-up commit.
- CodeRabbit review (if available in the repo's CI/PR setup) addressed.
- `ruff check . && ruff format --check . && pyright && pytest` passes after all fixes.

**Done when:** All review passes complete; validation green; no outstanding code-review findings.

**Depends on:** US-001 … US-012 (all implementation).

---

### US-014 — Patterns & Memory

**Description.** Distil the rules from DEC-001 … DEC-021 into `.claude/rules/diff-renderer.md` mirroring `.claude/rules/grade-layer.md` structure. Update `CLAUDE.md` "Repository status" entry for issue #8. Add the 8th milestone bullet.

**Traces to:** all decisions; the project's "patterns & memory" closure.

**Files:**

- `.claude/rules/diff-renderer.md` (new)
- `CLAUDE.md` (extend Repository-status section)

**Acceptance criteria:**

- `diff-renderer.md` covers: tier modeling, fail-closed sidecar pattern, ANSI/Markdown safety, boundary checks, drift detector, public-API discipline, MD truncation, large-schema warn, rendering precedence, log policy.
- `CLAUDE.md`'s "Pre-alpha. Seven issues shipped" line updated to "Eight issues shipped"; the new bullet matches the format of the prior seven.
- Public API surface line in `CLAUDE.md` lists `signalforge.diff` exports.
- Validation command passes (markdown-only changes; ruff/pyright unaffected).

**Done when:** Rule file written; `CLAUDE.md` reflects the new milestone.

**Depends on:** US-013.

---

## Beads Manifest

*To be filled at devolve time (post-PR-approval).*

## Beads Manifest

*To be filled at devolve time.*
