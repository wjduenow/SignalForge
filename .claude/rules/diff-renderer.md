# Diff renderer (kept/dropped table + unified diff + sidecar)

Established by issue #8. Apply to every module under `signalforge.diff` and to any new code that classifies an artifact into a tier, renders a kept/dropped/flagged table, emits a unified `schema.yml` diff, or writes a diff sidecar JSON.

The diff layer sits between the quality grader (#7) and the CLI (#9). It encodes Architectural Commitment #5 ("explainable diffs") at the post-grade boundary — every kept/dropped/flagged artifact ships with a one-line "why," every run produces a unified diff against the existing committed `schema.yml`, and the operator gets a per-run JSON sidecar that the v0.3 GitHub Action will consume directly.

## Tier classification with no-grading-report degrade + kept-uncertain origin signal (DEC-012, issue #50)

`DiffEntry.tier` is `Literal["kept", "kept-uncertain", "dropped", "flagged"]` of exactly four values.

- `"kept"` — survived prune with positive evidence (`PruneDecision.reason == "kept"`) and (if graded) passed the rubric. Ships in the proposed `schema.yml`.
- `"kept-uncertain"` (issue #50) — survived prune but the layer could not positively evaluate it (`PruneDecision.reason == "kept-without-evidence"`). Multiple sources route here per `prune-engine.md` § "Conservative-bias routing template". Ships in the proposed `schema.yml` because the conservative-bias contract says "drop only with positive evidence" — but with a distinct tier so reviewers see "shipped without evidence" separately from "shipped because it caught a real failing row."
- `"dropped"` — dropped by the prune engine; the matched `DropReason` literal travels on `DiffEntry.drop_reason`.
- `"flagged"` — survived prune **with positive evidence** AND a `GradingReport` was provided AND grading is below threshold.

Three load-bearing invariants:

1. **`flagged` only fires when `grading_report is not None`.** A prune-only run never gets surprise `flagged` rows that imply judgements the operator didn't ask for. Mirrors `grade-layer.md`'s "graceful degrade, never silent drop."
2. **Origin dominates over grading for `kept-uncertain` (issue #50).** A kept-without-evidence row stays `kept-uncertain` even when an attached `GradingResult` fails the rubric — collapsing to `flagged` would be a category error. The classifier (`_tier_for_kept` in `signalforge.diff.engine`) checks `decision.reason == "kept-without-evidence"` BEFORE the grading-aggregate dispatch.
3. **`kept-uncertain` is a prune-origin signal, never a doc/rationale signal.** Doc and rationale rows call the classifier with `decision=None`, which never routes to `kept-uncertain`. A kept-with-no-grading doc row uses the pre-existing `_DOC_KEPT_NO_GRADING_WHY` carve-out (`tier="kept"`, `why="kept (no grading)"`).

`DiffReport` always renders the kept/kept-uncertain/dropped/flagged header row; empty counts produce empty columns, not missing ones. The renderer never elides a tier — absence of kept-uncertain is itself signal.

Issue #50's bump 1 → 2 of `audit_schema_version` is the precedent for any future fifth `Tier` literal: external sidecar consumers gate on `>= 2` for the four-tier taxonomy. Apply the 5-surface graduation rule from `prune-engine.md` § "5-surface parity for v0.x → v0.(x+1) graduations" when adding a fifth.

## Kept-row `why` precedence: rationale → evidence → fallback (DEC-022)

Issue #41. Architectural Commitment #5 ("explainable diffs") requires every kept/dropped/flagged artifact to ship with a one-line "why."

Cascade for kept-tier rows (in `_entry_for_test` and `_entries_for_doc`):

1. **Candidate `rationale`** (drafter-emitted, primary).
2. **First non-empty `GradingResult.evidence`** (grader-emitted, secondary) — iterated in criterion-order; first non-empty wins for deterministic precedence.
3. **Existing fallback** — `decision.why` for tests, `description` for docs.

Each tier flows through `_truncate_why(text, max_chars)` (hard-cut at `max_chars - 1` + U+2026 ellipsis; whitespace-only input returns `""` so the cascade falls through).

**Issue #50 carve-out — kept-uncertain rows bypass the cascade.** When `decision.reason == "kept-without-evidence"`, the engine surfaces `decision.why` directly. The prune-emitted message names the actual cause ("total prune budget exceeded", "sample materialisation failed", "identifier rejected"); the drafter's rationale describes a test we couldn't evaluate and would mislead. Truncation still applies. Pinned by `tests/diff/test_engine.py::test_kept_uncertain_why_uses_decision_why_not_rationale`.

Load-bearing invariants:

- **One source per row, no concatenation.** Whichever tier hits first wins; others are discarded.
- **Cascade on whitespace, not just `None`.** An empty-or-blank `rationale` is treated as absent so an empty-rationale draft doesn't suppress the fallback.
- **`reasoning` is not in the cascade.** It reads as boilerplate (`"passed all grading criteria"`); `evidence` carries the qualitative per-criterion content.
- **Every tier obeys `max_why_chars`.** Including `decision.why`, `description`, and the flagged-tier `_flagged_why` budget (which counts its `"failed grading: <id> — "` prefix in the cap).
- **Escape sinks (DEC-007, DEC-008) still apply.** The unconditional ANSI strip + Markdown HTML-entity escape cover the new path; pinned by `test_kept_row_rationale_with_hostile_content_is_escaped_in_markdown`.

A fourth source (e.g. operator note in v0.3) appends AFTER `evidence` — update this DEC, both `_entry_*` helpers, and the cascade tests in lockstep.

## Fail-closed sidecar JSON (DEC-009, mirrors grade DEC-006/012)

`signalforge.diff._sidecar.write_sidecar` is the project's fifth fail-closed writer. Contract:

1. **Propagation IS the defence.** Open with `O_WRONLY | O_CREAT | O_TRUNC | 0o600`, single `os.write` (looped on short returns), `os.fsync`, close. The sole `try / finally` around `os.close(fd)` does NOT suppress write/fsync failures — `contextlib.suppress(OSError)` guards only the close. The AST defence in `tests/diff/test_sidecar.py` asserts there is exactly one `Try` node in the module with no `except` handlers around the write path.
2. **Size cap before any file open.** `_DIFF_SIDECAR_RECORD_LIMIT_BYTES = 10_000_000` (10 MB — order of magnitude above grade's 1 MB because diff text is naturally larger). Oversize raises `DiffSidecarRecordTooLargeError` BEFORE any `os.open`.
3. **Single-document overwrite, not append.** End-of-run only; every `render_diff` replaces the prior sidecar atomically via `O_TRUNC`. Concurrent runs against the same path produce different `run_id`s; last-writer-wins.

Sidecar is **on by default** (`write_sidecar=True`). With `sidecar_path=None`, lands at `<project_dir>/.signalforge/diff.json` (mirrors grade's always-write posture). Pass `write_sidecar=False` to skip — useful for library callers rendering in-process without disk.

## Symlink-hardened path canonicalisation at the orchestrator (mirrors grade post-QG fix)

`render_diff` knows the true `project_dir`. The orchestrator calls `canonicalise_path(raw_output_path, resolved_project_dir)` and `canonicalise_path(raw_sidecar_path, resolved_project_dir)` BEFORE handing off to the writers. Failures wrap as `DiffSidecarWriteError`. The writer's own canonicalise stays as defence-in-depth, but the load-bearing gate is the engine's. Same applies to `output_path`.

## `existing_schema` size cap before any `yaml.safe_load` (DEC-006)

`yaml.safe_load` is safe against code execution but NOT against billion-laughs (nested anchor expansion) or arbitrary deep-nesting. `render_diff` checks `len(existing_schema.encode("utf-8")) <= config.existing_schema_size_limit_bytes` BEFORE calling `yaml.safe_load`. Oversize raises `DiffInputTooLargeError(size, limit)` — the parser never sees a hostile payload. Mirrors the "size cap before any open" pattern, applied at the YAML deserialiser instead of the file-open seam.

## `existing_schema` soft-warn / hard-cap invariant (DEC-014)

`DiffConfig.@model_validator(mode="after")` raises `ValueError` at config-load time when `warn_at_bytes >= size_limit_bytes` — otherwise the DEC-014 soft-warn would be dead code. Apply the same shape to any future "soft-warn before hard-cap" pair: a model-level validator that asserts `warn < cap`.

## `sidecar_size_limit_bytes` wired through orchestrator

Every user-overridable cap on a config block must have one path from `DiffConfig.<field>` → `render_diff(... config=...)` → `write_sidecar(... size_limit_bytes=config.<field>)`. An exported-but-unwired field is a silent no-op; new config additions need an end-to-end test that pins the orchestrator-level error when the cap is exceeded.

## ANSI strip runs UNCONDITIONALLY on user content (DEC-007)

`signalforge._common.ansi_safety.strip_ansi_escapes(text)` — full ECMA-48 / ISO 6429 CSI regex `r'\x1b\[[0-?]*[ -/]*[@-~]'`. Covers SGR (colour/style), cursor-movement/screen-clearing, tilde-terminated and intermediate-byte CSI variants. (Broadened during US-014 from `\x1b\[[0-9;]*[a-zA-Z]` which missed tilde-terminated key/mode sequences like `\x1b[3~` and bracketed-paste markers.) Promoted to `_common` in issue #60 so the CLI's `print_stderr` sink shares the same regex; `signalforge.diff._ansi_safety` is now a back-compat re-export.

Both `AnsiRenderer` and `MarkdownRenderer` invoke this on every user-content field (`description`, `rationale`, `evidence`, `reasoning`, `why`, `drop_reason`, `artifact_id`) BEFORE adding their own colour codes / Markdown escapes. The CLI's `signalforge.cli._helpers.print_stderr` invokes it on every stderr write (issue #60).

**The strip is unconditional, not gated on the colour-precedence chain (DEC-021).** A malicious manifest field carrying `\x1b[31mEVIL\x1b[0m` renders as literal `EVIL` even when colour is forced ON via `respect_no_color_env=False` or `FORCE_COLOR=1`. Colour precedence governs only the renderer's *own* sanctioned SGR codes; user-content stripping is the security boundary.

Does NOT cover OSC (`\x1b]...`), DCS (`\x1bP...`), or other non-CSI escapes — out of scope for v0.1; broaden only on a real-world incident.

## Markdown table-cell escape with HTML entities, raw passthrough inside fenced diff (DEC-008)

`signalforge.diff._markdown_safety.escape_markdown_scalar(text, in_table_cell=False)`:

- Backslash first (so subsequent escapes can't be unwound by a trailing `\`).
- Backtick (would leak into a code span).
- Pipe — backslash-escaped outside tables; HTML-entity-encoded (`&#124;`) inside table cells (GFM tokenises pipes BEFORE inline-escape rules).
- Inside table cells, also entity-encode `\n` / `\r` / `\t` so row geometry survives.

**Inside the fenced ` ```diff ` block, raw content passes through.** The body is YAML; escaping would corrupt its bytes. Fixtures exercise triple-backticks in `description`, `</details>`, `[evil](javascript:...)`, and pipes in column names; the `injection_payloads` snapshot pins the byte-output.

The pattern: escape at the sink. Markdown / JSON / ANSI each own their escaping pass; don't centralise — each sink has different rules.

## Markdown body truncation at the last hunk boundary (DEC-005)

GitHub PR comments cap at 65 536 chars. `DiffConfig.markdown_max_diff_chars: int = 60_000` leaves room for table + prelude + footer. When the rendered diff exceeds the cap, `MarkdownRenderer` truncates at the **last complete hunk boundary** below the cap (NOT a mid-hunk character cut — that would produce malformed unified-diff that breaks Recce/GitHub) and appends:

```text
... (N more lines truncated — see <project_dir>/.signalforge/diff.json for full diff)
```

The kept/dropped/flagged table always renders fully (small). Only the unified-diff body can outgrow the cap. Operators are pointed at the sidecar for the full content.

## Three boundary checks at orchestrator entry (DEC-002)

`render_diff` validates BEFORE any rendering work:

1. `candidate.name == model.name` → `DiffCandidateModelMismatchError`.
2. `prune_result.model_unique_id == model.unique_id` → `DiffPruneResultModelMismatchError`.
3. `grading_report.model_unique_id == model.unique_id` (when provided) → `DiffGradingReportModelMismatchError`.

Mirrors `grade-layer.md` verbatim — convention as boundary; the `<arg>.<id> == model.<id>` linkage is the v0.1 contract for every typed-result handoff between stages. When a new typed-result handoff lands in v0.2, apply the same check at orchestrator entry.

## Reproducibility hash fields on every DiffReport (DEC-016)

Three 16-hex `blake2b-8` (digest_size=8) fingerprints: `candidate_hash`, `prune_result_hash`, `grading_report_hash` (latter is `None` when the caller omitted the report). Recipe: `blake2b-8` of `<obj>.model_dump_json(by_alias=True)` re-encoded through `json.dumps(sort_keys=True, separators=(",", ":"))`. The double-pass (Pydantic JSON → dict → canonical JSON) avoids relying on Pydantic's internal field ordering.

A reviewer querying "what inputs produced this diff?" reads three hashes from the sidecar and reconstructs from the upstream JSONL audits.

`DiffReport` also carries `schema_version: Literal[1] = 1` and `audit_schema_version: Literal[2] = 2` — frozen at constants. Bump when the sidecar JSON shape evolves; v0.2 readers gate on these.

## Single INFO log per `render_diff` call, lazy-format JSON (DEC-015)

Two `_LOGGER` events in the entire layer:

1. `INFO` end-of-happy-path with `run_id`, `model_unique_id`, `render_kind`, tier counts, `has_existing_schema`, `duration_seconds`, the three reproducibility hashes.
2. `WARNING` for the DEC-014 large-schema condition (over warn-at, under hard-cap).

No separate WARNING before raising typed errors — the exception IS the signal (mirrors grade DEC-006). No DEBUG in v0.1.

## ANSI-safe lazy-format JSON logger + grep gate (DEC-019)

Same rule as the other four pipeline layers (`safety-layer.md` DEC-022 / `llm-drafter.md` DEC-011 / `prune-engine.md` DEC-017 / `grade-layer.md` DEC-029). The grep gate at `tests/llm/test_logger_grep_gate.py` scans `src/signalforge/{llm,draft,prune,grade,diff,cli}` (6 dirs as of #9) and rejects any `_LOGGER\.\w+\(f"` hit.

## Fail-closed writer AST defence (DEC-018)

Every fail-closed writer module gets a test that pins the `try / finally` shape (no `except` around write/fsync). `tests/diff/test_sidecar.py::test_sidecar_writer_has_no_except_around_write` is the diff-layer instance. The diff sidecar `DiffReport` is read-back, gated by the drift detector below — there is no `DiffEvent` class.

## `_artifact_id` parity with grade layer (issue #42)

`signalforge.diff._artifact_id.artifact_id_for(...)`, `_model_test_args_hash(...)`, and `compute_args_hashes(...)` are **re-exports of the shared seam** `signalforge._common.artifact_id`. The diff renderer joins grade-sidecar JSON to its rendered diff via the `(run_id, artifact_id, criterion_id)` triple — identity-equal function objects across the two consuming layers make silent drift impossible.

Cross-stage parity is pinned by `tests/diff/test_artifact_id.py::test_cross_stage_parity_is_function_identity` (`is` equality across `signalforge.diff._artifact_id`, `signalforge.grade.engine`, and `signalforge._common.artifact_id`). The cross-stage import from `signalforge.grade.engine` is the **single allowed cross-stage seam** between `signalforge.diff` and `signalforge.grade`; production diff code must NOT import from `signalforge.grade` at runtime.

When the formatter's grammar evolves, edit only `src/signalforge/_common/artifact_id.py`; both layers pick it up. The collision rule (8-hex `args_hash` suffix when two tests share scope + type; ordinal `:1`/`:2` on exact-duplicate args) lives in `grade-layer.md` and applies verbatim.

## Custom `__repr__` on result-shaped models (DEC-020, mirrors prune/grade)

Pydantic v2's default `__repr__` emits every field. `DiffReport` carries `proposed_yaml`, `existing_yaml`, `unified_diff` (potentially multi-megabyte) plus the full `entries` tuple. `DiffEntry` carries `why` (potentially quoting upstream artifact text).

`DiffEntry.__repr__` shows `artifact_id`, `tier`, `drop_reason`, `score`. `DiffReport.__repr__` shows `model_unique_id`, kept/dropped/flagged counts, `has_existing_schema`, `duration_seconds`. Raw YAML, unified diff, and prose `why`/`evidence`/`reasoning` stay accessible via field access / `model_dump()`; they just don't slip out the casual debug-print path. Don't override `__str__` (Pydantic uses it for serialisation).

## Drift detectors are mandatory for read-back models (DEC-003)

Every `extra="ignore"` production model — `DiffEntry`, `DiffReport` — pairs with a `Strict<X>(extra="forbid")` mirror in `tests/diff/test_drift_detector.py`, validated against committed fixtures (`tests/fixtures/diff/{diff_entry_v1.json,diff_report_v1.json}`). Adding a field without updating the strict mirror OR the fixture breaks the test loudly.

`extra=` placement convention from `safety-layer.md` DEC-015 applies: config-shaped (`DiffConfig`, `_DiffConfigFile` inner) → `extra="forbid"`; `_DiffConfigFile` top level → `extra="ignore"` (sibling stages reserved); read-back (`DiffEntry`, `DiffReport`) → `extra="ignore"`.

## API alignment with adjacent stages

`render_diff(model, candidate, prune_result, *, grading_report=None, existing_schema=None, config=None, output_path=None, sidecar_path=None, project_dir=None) -> DiffReport`. Mirrors `grade_artifacts` / `prune_tests` / `draft_schema`: model + data front-paired positionally; keyword-only optionals after `*` in order (data-shaped → behaviour-knob → paths); `project_dir` for orchestrator-level path resolution.

`load_diff_config(project_dir, path=None) -> DiffConfig` matches `load_grade_config` / `load_prune_config` / `load_draft_config` / `load_safety_config`. Resolution order: explicit `path` > `<project_dir>/signalforge.yml diff:` > defaults. Explicit-path-missing raises `DiffError`; default-path-missing returns defaults silently (mirrors grader).

## `signalforge.yml` top-level namespace: `diff:` (DEC-010)

The diff-stage block is `{ diff: { context_lines, max_why_chars, narrow_terminal_threshold, markdown_max_diff_chars, existing_schema_size_limit_bytes, existing_schema_warn_at_bytes, sidecar_size_limit_bytes, render_kind, respect_no_color_env } }`. Sibling top-level keys are reserved and silently ignored by the diff loader. Each numeric knob carries a `field_validator` that rejects non-positive values — a zero or negative cap would silently disable the protection.

## Renderer ABC — pure-return, orchestrator owns I/O (DEC-011)

`signalforge.diff._renderers.Renderer` is an ABC with one abstract method: `render(self, report: DiffReport) -> str`. Three concretes (`AnsiRenderer`, `MarkdownRenderer`, `JsonRenderer`) return text; `render_diff` handles I/O. Snapshot tests assert `str == fixture_text` byte-for-byte.

The three concretes are private (`_renderers`) per DEC-004; only the typed-result + orchestrator + config + errors form the public contract. `JsonRenderer` is also what the sidecar writer uses regardless of `config.render_kind` — the human-facing surface and the durable artefact share the JSON shape so a v0.3 GH Action consumer parses identical bytes whether the operator selected ANSI or Markdown for stdout.

## Schema-version surfaces

- `DiffReport.audit_schema_version: Literal[2] = 2` — bumped 1 → 2 by issue #50 when `kept-uncertain` graduated. External sidecar consumers gate on `>= 2`.
- `DiffConfig.render_kind` — graduated by #9 (`--format {ansi,markdown,json}` wires it). CLI re-validates via `DiffConfig.model_validate({**dump, "render_kind": override})` so the soft-warn / hard-cap invariant validator re-runs.
- `signalforge.diff.render_to_text(report, *, config=None, project_dir=None) -> str` — graduated by #9 as the public stdout helper. Internally builds the renderer via `_build_renderer(config or DiffConfig(), project_dir=project_dir)`. Keeps DEC-004 (renderers private) intact; the caller supplies config explicitly or accepts `DiffConfig()` defaults — the helper does NOT introspect the report.

## Reference

`plans/super/8-diff-renderer.md` — DEC-001 … DEC-021. `src/signalforge/diff/` — current implementation. `docs/diff-ops.md` — operational reference. `tests/diff/test_drift_detector.py`, `tests/diff/test_sidecar.py`, `tests/diff/test_public_api.py`, `tests/diff/test_artifact_id.py::test_cross_stage_parity_is_function_identity`. `tests/llm/test_logger_grep_gate.py` — lazy-format logger gate (5 dirs as of #8). `tests/fixtures/diff/{diff_entry_v1.json,diff_report_v1.json}`.
