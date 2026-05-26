# Diff renderer (kept/dropped table + unified diff + sidecar)

Apply to every module under `signalforge.diff` and to any new code that classifies an artifact into a tier, renders a kept/dropped/flagged table, emits a unified `schema.yml` diff, or writes a diff sidecar JSON.

Encodes Architectural Commitment #5 ("explainable diffs"): every kept/dropped/flagged artifact ships with a one-line "why," every run produces a unified diff against the committed `schema.yml`, and the operator gets a per-run JSON sidecar (the v0.3 GitHub Action consumes it directly).

## Tier classification

`DiffEntry.tier` is `Literal["kept", "kept-uncertain", "dropped", "flagged"]` of exactly four values.

- `"kept"` — survived prune with positive evidence (`PruneDecision.reason == "kept"`) and (if graded) passed the rubric. Ships in proposed `schema.yml`.
- `"kept-uncertain"` — survived prune but couldn't be positively evaluated (`PruneDecision.reason == "kept-without-evidence"`; sources per `prune-engine.md` § "Conservative-bias routing template"). Ships (conservative-bias: "drop only with positive evidence"), but the distinct tier separates "shipped without evidence" from "shipped because it caught a real failing row."
- `"dropped"` — dropped by the prune engine; the matched `DropReason` literal travels on `DiffEntry.drop_reason`.
- `"flagged"` — survived prune **with positive evidence** AND a `GradingReport` was provided AND grading is below threshold.

Three load-bearing invariants:

1. **`flagged` only fires when `grading_report is not None`.** A prune-only run never gets surprise `flagged` rows. Mirrors `grade-layer.md`'s "graceful degrade, never silent drop."
2. **Origin dominates over grading for `kept-uncertain`.** A kept-without-evidence row stays `kept-uncertain` even when an attached `GradingResult` fails the rubric — collapsing to `flagged` is a category error. `_tier_for_kept` (in `signalforge.diff.engine`) checks `decision.reason == "kept-without-evidence"` BEFORE the grading-aggregate dispatch.
3. **`kept-uncertain` is a prune-origin signal, never a doc/rationale signal.** Doc and rationale rows call the classifier with `decision=None`, which never routes to `kept-uncertain`. A kept-with-no-grading doc row uses the `_DOC_KEPT_NO_GRADING_WHY` carve-out (`tier="kept"`, `why="kept (no grading)"`).

`DiffReport` always renders the four-tier header row; empty counts produce empty columns, not missing ones — absence of a tier is itself signal.

`audit_schema_version` was bumped 1 → 2 when `kept-uncertain` graduated; consumers gate on `>= 2`. For a future fifth `Tier` literal, apply the 5-surface graduation rule from `prune-engine.md`.

## Kept-row `why` precedence: rationale → evidence → fallback

Cascade for kept-tier rows (in `_entry_for_test` and `_entries_for_doc`):

1. **Candidate `rationale`** (drafter-emitted, primary).
2. **First non-empty `GradingResult.evidence`** (grader-emitted) — iterated in criterion-order; first non-empty wins, for deterministic precedence.
3. **Fallback** — `decision.why` for tests, `description` for docs.

Each tier flows through `_truncate_why(text, max_chars)` (hard-cut at `max_chars - 1` + U+2026 ellipsis; whitespace-only input returns `""` so the cascade falls through).

**Kept-uncertain rows bypass the cascade:** when `decision.reason == "kept-without-evidence"`, surface `decision.why` directly. The prune message names the actual cause ("total prune budget exceeded", etc.); the drafter's rationale describes an unevaluated test and would mislead. Truncation still applies. Pinned by `test_kept_uncertain_why_uses_decision_why_not_rationale`.

Invariants:

- **One source per row, no concatenation.** First tier to hit wins; others discarded.
- **Cascade on whitespace, not just `None`.** Empty-or-blank `rationale` is treated as absent so it doesn't suppress the fallback.
- **`reasoning` is not in the cascade** (boilerplate like `"passed all grading criteria"`); `evidence` carries the qualitative content.
- **Every tier obeys `max_why_chars`** — including `decision.why`, `description`, and the flagged-tier `_flagged_why` (which counts its `"failed grading: <id> — "` prefix in the cap).
- **Escape sinks still apply** — the unconditional ANSI strip + Markdown HTML-entity escape cover this path; pinned by `test_kept_row_rationale_with_hostile_content_is_escaped_in_markdown`.

A fourth source (e.g. operator note) appends AFTER `evidence` — update both `_entry_*` helpers and the cascade tests in lockstep.

## Fail-closed sidecar JSON

`signalforge.diff._sidecar.write_sidecar` is the project's fifth fail-closed writer:

1. **Propagation IS the defence.** Open with `O_WRONLY | O_CREAT | O_TRUNC | 0o600`, single `os.write` (looped on short returns), `os.fsync`, close. The sole `try / finally` around `os.close(fd)` does NOT suppress write/fsync failures — `contextlib.suppress(OSError)` guards only the close. The AST defence in `tests/diff/test_sidecar.py` asserts exactly one `Try` node in the module, with no `except` around the write path (`test_sidecar_writer_has_no_except_around_write`).
2. **Size cap before any file open.** `_DIFF_SIDECAR_RECORD_LIMIT_BYTES = 10_000_000` (10 MB — above grade's 1 MB because diff text is larger). Oversize raises `DiffSidecarRecordTooLargeError` BEFORE any `os.open`.
3. **Single-document overwrite, not append.** End-of-run only; `O_TRUNC` replaces the prior sidecar atomically. Concurrent runs get different `run_id`s; last-writer-wins.

Sidecar is **on by default** (`write_sidecar=True`); with `sidecar_path=None` it lands at `<project_dir>/.signalforge/diff.json`. Pass `write_sidecar=False` for in-process library callers. The sidecar `DiffReport` is read-back, gated by the drift detector below — there is no `DiffEvent` class.

## Symlink-hardened path canonicalisation at the orchestrator

`render_diff` calls `canonicalise_path(raw_output_path, resolved_project_dir)` and `canonicalise_path(raw_sidecar_path, resolved_project_dir)` BEFORE handing off to the writers; failures wrap as `DiffSidecarWriteError`. The writer's own canonicalise stays as defence-in-depth, but the load-bearing gate is the engine's. Same applies to `output_path`.

## `existing_schema` size cap before any `yaml.safe_load`

`yaml.safe_load` is safe against code execution but NOT against billion-laughs / deep-nesting. `render_diff` checks `len(existing_schema.encode("utf-8")) <= config.existing_schema_size_limit_bytes` BEFORE `yaml.safe_load`. Oversize raises `DiffInputTooLargeError(size, limit)` — the parser never sees a hostile payload.

## `existing_schema` soft-warn / hard-cap invariant

`DiffConfig.@model_validator(mode="after")` raises `ValueError` at config-load time when `warn_at_bytes >= size_limit_bytes` — otherwise the soft-warn is dead code. Apply the same shape to any future "soft-warn before hard-cap" pair.

## `sidecar_size_limit_bytes` wired through orchestrator

Every user-overridable cap must have one path from `DiffConfig.<field>` → `render_diff(... config=...)` → `write_sidecar(... size_limit_bytes=config.<field>)`. An exported-but-unwired field is a silent no-op; new config additions need an end-to-end test pinning the orchestrator-level error when the cap is exceeded.

## ANSI strip runs UNCONDITIONALLY on user content

`signalforge._common.ansi_safety.strip_ansi_escapes(text)` — full ECMA-48 / ISO 6429 CSI regex `r'\x1b\[[0-?]*[ -/]*[@-~]'`. Covers SGR, cursor-movement/screen-clearing, tilde-terminated and intermediate-byte CSI variants (incl. `\x1b[3~` and bracketed-paste markers). `signalforge.diff._ansi_safety` is a back-compat re-export of the `_common` helper.

Both `AnsiRenderer` and `MarkdownRenderer` invoke this on every user-content field (`description`, `rationale`, `evidence`, `reasoning`, `why`, `drop_reason`, `artifact_id`) BEFORE adding their own colour/Markdown escapes. The CLI's `print_stderr` invokes it on every stderr write.

**The strip is unconditional, not gated on the colour-precedence chain.** A field carrying `\x1b[31mEVIL\x1b[0m` renders as literal `EVIL` even when colour is forced ON (`respect_no_color_env=False` / `FORCE_COLOR=1`). Colour precedence governs only the renderer's *own* sanctioned SGR codes; user-content stripping is the security boundary.

Does NOT cover OSC (`\x1b]...`), DCS (`\x1bP...`), or other non-CSI escapes — broaden only on a real-world incident.

## Markdown table-cell escape with HTML entities, raw passthrough inside fenced diff

`signalforge.diff._markdown_safety.escape_markdown_scalar(text, in_table_cell=False)`:

- Backslash first (so later escapes can't be unwound by a trailing `\`).
- Backtick (would leak into a code span).
- Pipe — backslash-escaped outside tables; HTML-entity-encoded (`&#124;`) inside table cells (GFM tokenises pipes BEFORE inline-escape rules).
- Inside table cells, also entity-encode `\n` / `\r` / `\t` so row geometry survives.

**Inside the fenced ` ```diff ` block, raw content passes through** — the body is YAML; escaping would corrupt its bytes. The `injection_payloads` snapshot pins the byte-output (triple-backticks in `description`, `</details>`, `[evil](javascript:...)`, pipes in column names).

The pattern: escape at the sink. Markdown / JSON / ANSI each own their escaping pass; don't centralise — each sink has different rules.

## Markdown body truncation at the last hunk boundary

GitHub PR comments cap at 65 536 chars. `DiffConfig.markdown_max_diff_chars: int = 60_000` leaves room for table + prelude + footer. When the diff exceeds the cap, `MarkdownRenderer` truncates at the **last complete hunk boundary** below the cap (NOT a mid-hunk character cut — that produces malformed unified-diff that breaks Recce/GitHub) and appends:

```text
... (N more lines truncated — see <project_dir>/.signalforge/diff.json for full diff)
```

The kept/dropped/flagged table always renders fully; only the unified-diff body can outgrow the cap.

## Three boundary checks at orchestrator entry

`render_diff` validates BEFORE any rendering work:

1. `candidate.name == model.name` → `DiffCandidateModelMismatchError`.
2. `prune_result.model_unique_id == model.unique_id` → `DiffPruneResultModelMismatchError`.
3. `grading_report.model_unique_id == model.unique_id` (when provided) → `DiffGradingReportModelMismatchError`.

Mirrors `grade-layer.md` — the `<arg>.<id> == model.<id>` linkage is the contract for every typed-result handoff between stages. Apply the same check at any new handoff's orchestrator entry.

## Reproducibility hash fields on every DiffReport

Three 16-hex `blake2b-8` (digest_size=8) fingerprints: `candidate_hash`, `prune_result_hash`, `grading_report_hash` (`None` when the report was omitted). Recipe: `blake2b-8` of `<obj>.model_dump_json(by_alias=True)` re-encoded through `json.dumps(sort_keys=True, separators=(",", ":"))`. The double-pass avoids relying on Pydantic's internal field ordering. A reviewer reads three hashes from the sidecar to reconstruct "what inputs produced this diff?" from the upstream JSONL audits.

`DiffReport` also carries `schema_version: Literal[1] = 1` and `audit_schema_version: Literal[2] = 2` — frozen at constants. Bump when the sidecar JSON shape evolves; readers gate on these.

## Single INFO log per `render_diff` call, lazy-format JSON

Two `_LOGGER` events total: (1) `INFO` end-of-happy-path with `run_id`, `model_unique_id`, `render_kind`, tier counts, `has_existing_schema`, `duration_seconds`, the three hashes; (2) `WARNING` for the large-schema condition (over warn-at, under hard-cap). No WARNING before raising typed errors — the exception IS the signal. No DEBUG in v0.1.

The grep gate at `tests/llm/test_logger_grep_gate.py` scans `src/signalforge/{llm,draft,prune,grade,diff,cli}` and rejects any `_LOGGER\.\w+\(f"` hit.

## `_artifact_id` parity with grade layer

`signalforge.diff._artifact_id.artifact_id_for(...)`, `_model_test_args_hash(...)`, `compute_args_hashes(...)` are **re-exports of the shared seam** `signalforge._common.artifact_id`. The diff renderer joins grade-sidecar JSON via the `(run_id, artifact_id, criterion_id)` triple — identity-equal function objects make silent drift impossible.

Pinned by `tests/diff/test_artifact_id.py::test_cross_stage_parity_is_function_identity` (`is` equality across `signalforge.diff._artifact_id`, `signalforge.grade.engine`, `signalforge._common.artifact_id`). The import from `signalforge.grade.engine` is the **single allowed cross-stage seam** between `signalforge.diff` and `signalforge.grade`; production diff code must NOT import from `signalforge.grade` at runtime.

When the formatter's grammar evolves, **edit only `src/signalforge/_common/artifact_id.py`**; both layers pick it up. The collision rule (8-hex `args_hash` suffix when two tests share scope + type; ordinal `:1`/`:2` on exact-duplicate args) lives in `grade-layer.md` and applies verbatim.

## Custom `__repr__` on result-shaped models

Pydantic v2's default `__repr__` emits every field; `DiffReport` carries `proposed_yaml`/`existing_yaml`/`unified_diff` (multi-megabyte) + the full `entries` tuple, `DiffEntry` carries `why` (quoting upstream artifact text). `DiffEntry.__repr__` shows `artifact_id`, `tier`, `drop_reason`, `score`; `DiffReport.__repr__` shows `model_unique_id`, kept/dropped/flagged counts, `has_existing_schema`, `duration_seconds`. The verbose fields stay accessible via field access / `model_dump()`. Don't override `__str__` (Pydantic uses it for serialisation).

## Drift detectors are mandatory for read-back models

`DiffEntry`, `DiffReport` (both `extra="ignore"`) each pair with a `Strict<X>(extra="forbid")` mirror in `tests/diff/test_drift_detector.py`, validated against `tests/fixtures/diff/{diff_entry_v1.json,diff_report_v1.json}`. Adding a field without updating the strict mirror OR the fixture breaks the test loudly.

`extra=` placement (per `safety-layer.md`): config-shaped (`DiffConfig`, `_DiffConfigFile` inner) → `extra="forbid"`; `_DiffConfigFile` top level → `extra="ignore"` (sibling stages reserved); read-back (`DiffEntry`, `DiffReport`) → `extra="ignore"`.

## API alignment with adjacent stages

`render_diff(model, candidate, prune_result, *, grading_report=None, existing_schema=None, config=None, output_path=None, sidecar_path=None, project_dir=None) -> DiffReport`. Mirrors `grade_artifacts` / `prune_tests` / `draft_schema`: model + data front-paired positionally; keyword-only optionals after `*` (data-shaped → behaviour-knob → paths).

`load_diff_config(project_dir, path=None) -> DiffConfig` matches the sibling loaders. Resolution: explicit `path` > `<project_dir>/signalforge.yml diff:` > defaults. Explicit-path-missing raises `DiffError`; default-path-missing returns defaults silently.

## `signalforge.yml` top-level namespace: `diff:`

Block: `{ diff: { context_lines, max_why_chars, narrow_terminal_threshold, markdown_max_diff_chars, existing_schema_size_limit_bytes, existing_schema_warn_at_bytes, sidecar_size_limit_bytes, render_kind, respect_no_color_env } }`. Sibling top-level keys reserved and silently ignored. Each numeric knob has a `field_validator` rejecting non-positive values — a zero/negative cap would silently disable the protection.

## Renderer ABC — pure-return, orchestrator owns I/O

`signalforge.diff._renderers.Renderer` is an ABC with one abstract method `render(self, report: DiffReport) -> str`. Three private concretes (`AnsiRenderer`, `MarkdownRenderer`, `JsonRenderer`) return text; `render_diff` handles I/O. Snapshot tests assert `str == fixture_text` byte-for-byte. Only the typed-result + orchestrator + config + errors are public. `JsonRenderer` is also what the sidecar writer uses regardless of `config.render_kind` — the human surface and durable artefact share JSON so a v0.3 GH Action parses identical bytes whether ANSI or Markdown was selected for stdout.

## Schema-version surfaces

- `DiffReport.audit_schema_version: Literal[2] = 2` — consumers gate on `>= 2`.
- `DiffConfig.render_kind` — graduated as `--format {ansi,markdown,json}`. CLI re-validates via `DiffConfig.model_validate({**dump, "render_kind": override})` so the soft-warn / hard-cap validator re-runs.
- `signalforge.diff.render_to_text(report, *, config=None, project_dir=None) -> str` — public stdout helper; builds the renderer via `_build_renderer(config or DiffConfig(), project_dir=project_dir)`. Keeps renderers private; the caller supplies config or accepts `DiffConfig()` defaults — does NOT introspect the report.

## Reference

`plans/super/8-diff-renderer.md` (DEC records), `src/signalforge/diff/` (implementation), `docs/diff-ops.md` (ops reference). Tests: `tests/diff/{test_drift_detector,test_sidecar,test_public_api,test_artifact_id}.py`, `tests/llm/test_logger_grep_gate.py`. Fixtures: `tests/fixtures/diff/{diff_entry_v1.json,diff_report_v1.json}`.
