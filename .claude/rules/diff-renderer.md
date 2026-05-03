# Diff renderer (kept/dropped table + unified diff + sidecar)

Established by issue #8 (diff renderer). Apply to every module under `signalforge.diff` and to any new code that classifies an artifact into a tier, renders a kept/dropped/flagged table, emits a unified `schema.yml` diff, or writes a diff sidecar JSON.

The diff layer sits between the quality grader (#7) and the CLI (#9). It encodes Architectural Commitment #5 ("explainable diffs") at the post-grade boundary — every kept/dropped/flagged artifact ships with a one-line "why," every run produces a unified diff against the existing committed `schema.yml`, and the operator gets a per-run JSON sidecar that the v0.3 GitHub Action will consume directly.

## Tier classification with no-grading-report degrade (DEC-012)

`DiffEntry.tier` is a `Literal["kept", "dropped", "flagged"]` of exactly three values:

- `"kept"` — artifact survived prune (and grade, if a report was provided) and ships in the proposed `schema.yml`.
- `"dropped"` — artifact was dropped by the prune engine; the matched `DropReason` literal travels on `DiffEntry.drop_reason`.
- `"flagged"` — artifact survived prune AND a `GradingReport` was provided AND its grading is below threshold (`passed=False` for any criterion OR a graceful-degrade `score=None` was recorded).

The load-bearing invariant: **`flagged` only fires when `grading_report is not None`.** When the caller omits the grading report, every kept entry is plain `"kept"`; no entry can be `flagged` without a grading run. This mirrors the conservative degrade pattern from `grade-layer.md` ("graceful degrade, never silent drop") — the operator running a prune-only pipeline doesn't get surprise `flagged` rows that imply judgements they never asked for.

`DiffReport` always renders the kept/dropped/flagged table; an empty `dropped_count` produces an empty section, not a missing one. The renderer never elides a tier — the absence of dropped tests is itself signal.

If you add a fourth `Tier` literal in v0.2 (e.g. `"warning"`, `"deferred"`), update production `Tier` AND `StrictDiffEntry` (the drift detector) AND the committed fixtures `tests/fixtures/diff/diff_entry_v1.json` / `diff_report_v1.json` AND the renderer dispatch in `signalforge.diff._renderers` in the same change.

## Fail-closed sidecar JSON (DEC-009, mirrors grade DEC-006/012)

`signalforge.diff._sidecar.write_sidecar` is the project's **fifth** fail-closed writer (after safety, draft, prune, grade). The contract is identical:

1. **Propagation IS the defence.** Open with `O_WRONLY | O_CREAT | O_TRUNC | 0o600`, single `os.write` (looped on short returns), `os.fsync`, close. Path-canonicalisation failures wrap as `DiffSidecarWriteError(cause=...)` (only because the helper raises a warehouse-layer name); nothing else is wrapped. The sole `try / finally` around `os.close(fd)` does NOT suppress write/fsync failures — `contextlib.suppress(OSError)` guards only the close. The AST defence in `tests/diff/test_sidecar.py` asserts there is exactly one `Try` node in the module and that it has no `except` handlers around the write path.

2. **Size cap before any file open.** `_DIFF_SIDECAR_RECORD_LIMIT_BYTES = 10_000_000` (10 MB — an order of magnitude above grade's 1 MB because diff text is naturally larger than evidence-only payloads). Oversize raises `DiffSidecarRecordTooLargeError` BEFORE any `os.open` so an oversize payload leaves no on-disk artefact.

3. **Single-document overwrite, not append.** End-of-run only; every `render_diff` call replaces the prior sidecar atomically via `O_TRUNC`. Concurrent runs against the same `sidecar_path` produce different `run_id`s and last-writer-wins (mirrors grade-layer.md). Operators are expected to use a per-run path or accept overwrite semantics.

The sidecar path is opt-in (`sidecar_path=None` skips the write entirely) — unlike safety / prune / grade, the diff layer does not require a durable on-disk artefact for every invocation. The CLI (#9) will wire a default; library callers can render in-process without ever touching the disk.

## Symlink-hardened path canonicalisation at the orchestrator (mirrors grade post-QG fix)

`render_diff` is the place that knows the true `project_dir`. The writer's own `canonicalise_path` derivation against `project_dir.parent.parent` would be unsafe for caller-supplied paths — a caller passing `sidecar_path=/tmp/diff.json` would let any symlink slip the gate.

The fix (and its precedent in `grade-layer.md`): the orchestrator calls `canonicalise_path(raw_output_path, resolved_project_dir)` and `canonicalise_path(raw_sidecar_path, resolved_project_dir)` BEFORE handing off to the writers. Failures wrap as `DiffSidecarWriteError`. The writer's own canonicalise stays as defence-in-depth, but the load-bearing gate is the engine's. The same gate covers `output_path` (rendered text destination), not just the sidecar.

When introducing a sixth fail-closed writer (e.g., a CLI run-history audit), apply the same engine-level canonicalisation. Don't trust the writer to derive its own `project_dir`.

## `existing_schema` size cap before any `yaml.safe_load` (DEC-006)

`existing_schema` is operator-supplied YAML text. `yaml.safe_load` is safe against arbitrary code execution but is NOT safe against pathological payloads — billion-laughs (deeply nested anchor expansion) and arbitrary deep-nesting can consume gigabytes of memory before the parser yields. Defence: literal byte-length cap on the encoded UTF-8 input.

`render_diff` checks `len(existing_schema.encode("utf-8")) <= config.existing_schema_size_limit_bytes` BEFORE calling `yaml.safe_load`. Oversize raises `DiffInputTooLargeError(size, limit)` — the parser never sees a hostile payload. Mirrors the safety / grade "size cap before any open" pattern, applied at the YAML deserialiser instead of the file-open seam.

`existing_schema` is not the only externally-controlled YAML in the layer (the `signalforge.yml` config file is too), but the size of an operator's `schema.yml` is the only payload the orchestrator can plausibly receive at runtime. The config loader's failure mode is `DiffError` with remediation; the renderer's is the typed `DiffInputTooLargeError`.

## `existing_schema` soft-warn / hard-cap invariant (DEC-014, post-QG fix)

The renderer ships two thresholds: `existing_schema_warn_at_bytes` (soft warning, default 1 MB) and `existing_schema_size_limit_bytes` (hard cap, default 10 MB). The DEC-014 soft-warn fires when the payload exceeds warn-at but stays below the hard cap. **If `warn_at_bytes >= size_limit_bytes`, the warning is dead code.**

The post-QG fix: `DiffConfig.@model_validator(mode="after")` raises `ValueError` at config-load time when `warn_at >= size_limit`. The original implementation shipped with inverted defaults (warn_at=10 MB, size_limit=1 MB) which silently disabled DEC-014; the test suite caught the dead branch but the production defaults made the soft-warn unreachable. The validator now fails loud rather than silently disabling the contract.

The pattern: any future "soft-warn before hard-cap" pair gets a model-level validator that asserts `warn < cap`. Mirror the same shape for warning thresholds in the prune budget (v0.2) or any other tiered cap.

## `sidecar_size_limit_bytes` wired through orchestrator (post-QG fix)

`DiffConfig.sidecar_size_limit_bytes` was originally exported but never consumed — `write_sidecar` only knew the module-level `_DIFF_SIDECAR_RECORD_LIMIT_BYTES = 10_000_000`. The post-QG fix wires the config field through `render_diff` to `write_sidecar` via a `size_limit_bytes` kwarg; the writer falls back to the module constant only when the kwarg is `None` (the test-facing seam).

The pattern: every user-overridable cap on a config block must have one path from `DiffConfig.<field>` → `render_diff(... config=...)` → `write_sidecar(... size_limit_bytes=config.<field>)`. A config field that's exported but not threaded is a silent no-op; future config additions need an end-to-end test that pins the orchestrator-level error when the cap is exceeded (`tests/diff/test_engine.py` carries the precedent).

## ANSI strip runs UNCONDITIONALLY on user content (DEC-007)

`signalforge.diff._ansi_safety.strip_ansi_escapes(text)` — strict regex `r'\x1b\[[0-9;]*[a-zA-Z]'` covers SGR plus all CSI. Both `AnsiRenderer` and `MarkdownRenderer` invoke this on every user-content field (`description`, `rationale`, `evidence`, `reasoning`, `why`, `drop_reason`, `artifact_id`) BEFORE the renderer's own colour codes / Markdown escapes are added.

The load-bearing invariant: **the strip is unconditional, not gated on the colour-precedence chain (DEC-021).** A malicious manifest field carrying `\x1b[31mEVIL\x1b[0m` renders as the literal text `EVIL` even when colour is forced ON via `respect_no_color_env=False` or `FORCE_COLOR=1`. The colour precedence only governs whether the renderer's *own* sanctioned SGR codes get emitted; user-content stripping is the security boundary.

The CSI regex does NOT cover OSC (`\x1b]...`), DCS (`\x1bP...`), or other non-CSI escapes — out of scope for v0.1 because the upstream sources (manifests, LLM output) overwhelmingly emit only CSI sequences when they emit anything. Extend the regex when a real-world incident demonstrates otherwise; do not pre-emptively broaden.

The `_LOGGER` lazy-format gate (DEC-019) covers the audit channel; this strip covers the stdout / Markdown / sidecar sinks. They are independent defences against the same threat surface.

## Markdown table-cell escape with HTML entities, raw passthrough inside fenced diff (DEC-008)

`signalforge.diff._markdown_safety.escape_markdown_scalar(text, in_table_cell=False)` escapes:

- Backslash (`\\`) — first, so subsequent escapes cannot be unwound by a crafted trailing `\`.
- Backtick (`` ` ``) — leaks the rest of the line into a code span until the next backtick.
- Pipe (`|`) — backslash-escaped outside tables; HTML-entity-encoded (`&#124;`) inside table cells because the GFM table parser tokenises pipes BEFORE applying inline-escape rules. Backslash-pipe still breaks column counts in some renderers.
- Inside table cells, also entity-encode `\n` / `\r` / `\t` (`&#10;` / `&#13;` / `&#9;`) so the row geometry survives.

**Inside the fenced ` ```diff ` block, raw content passes through.** The diff body is YAML; backticks etc. don't break a `diff` fence, and GitHub doesn't interpret HTML inside the fence. Pass-through preserves the actual YAML — escaping inside the fence would corrupt the diff's bytes. Fixtures exercise triple-backticks in `description`, `</details>`, `[evil](javascript:...)`, and pipe in column names; the `injection_payloads` snapshot pins the expected byte-output.

The pattern: escape at the sink. Markdown is one sink; the JSON sidecar uses Pydantic's `model_dump_json` (which JSON-encodes); the ANSI sink runs `strip_ansi_escapes` (above). Each sink owns its escaping pass — don't centralise into a single "sanitise" helper because each sink has different rules for what's safe.

## Markdown body truncation at the last hunk boundary (DEC-005)

GitHub PR comments are 65 536 chars. `DiffConfig.markdown_max_diff_chars: int = 60_000` leaves room for the table, the prelude, and the truncation footer. When the rendered Markdown diff body exceeds the cap, `MarkdownRenderer` truncates the diff section and appends:

```text
... (N more lines truncated — see <project_dir>/.signalforge/diff.json for full diff)
```

The truncation is at the **last complete hunk boundary** below the cap, not a mid-hunk character cut. Truncating mid-hunk produces a malformed unified-diff body that breaks downstream tooling (Recce / GitHub's diff viewer). Preserving complete hunks costs at most one hunk's worth of size headroom; the 60 000 cap was chosen with that headroom included.

The kept/dropped/flagged table always renders fully (one line per artifact; small). The unified-diff body is the only field that can outgrow the cap.

Operators are pointed at the sidecar for the full diff; the truncation footer carries the exact path. The CLI (#9) will wire `--sidecar-path` so the footer's reference is always meaningful.

## Three boundary checks at orchestrator entry (DEC-002)

`render_diff` validates BEFORE any rendering work:

1. `candidate.name == model.name` → `DiffCandidateModelMismatchError`.
2. `prune_result.model_unique_id == model.unique_id` → `DiffPruneResultModelMismatchError`.
3. `grading_report.model_unique_id == model.unique_id` (when provided) → `DiffGradingReportModelMismatchError`.

Mirrors `grade-layer.md`'s `prune_result.model_unique_id` boundary check verbatim — a stale typed result from a sibling stage would silently drive misleading kept/dropped/flagged tallies into the rendered diff. Convention as boundary; the `_unique_id`/`_name` linkage is the v0.1 contract for every typed-result handoff between pipeline stages.

When v0.2 introduces a new typed-result handoff (e.g., a multi-model batch result), apply the same `<arg>.<id_field> == model.<id_field>` check at orchestrator entry. Without it, a stale result silently corrupts the downstream artifact.

## Reproducibility hash fields on every DiffReport (DEC-016)

`DiffReport` carries three 16-hex `blake2b-8` (digest_size=8) fingerprints:

- `candidate_hash` — blake2b-8 of `candidate.model_dump_json(by_alias=True)` re-encoded through `json.dumps(sort_keys=True, separators=(",", ":"))`. Stable across field-construction order.
- `prune_result_hash` — same recipe, applied to the `PruneResult`.
- `grading_report_hash` — same recipe applied to the `GradingReport` when provided; `None` when the caller omitted it.

A reviewer querying "what inputs produced this diff?" reads three hashes from the sidecar and reconstructs from the upstream JSONL audits (`safety.jsonl`, `llm_response.jsonl`, `prune.jsonl`, `grade.jsonl`). The double-pass (Pydantic JSON → Python dict → canonical JSON) avoids relying on Pydantic's internal field ordering, which is declared stable in v2 but not contractually guaranteed across point releases.

`DiffReport` also carries `schema_version: Literal[1] = 1` and `audit_schema_version: int = 1` — frozen at constants in production code. Bump when the sidecar JSON schema evolves; v0.2 readers gate on these.

## Single INFO log per `render_diff` call, lazy-format JSON (DEC-015)

Two `_LOGGER` events in the entire diff layer:

1. `INFO`: `"rendered diff: %s"` with `json.dumps({"run_id": ..., "model_unique_id": ..., "render_kind": ..., "kept": k, "dropped": d, "flagged": f, "has_existing_schema": bool, "duration_seconds": s, "candidate_hash": ..., "prune_result_hash": ..., "grading_report_hash": ...})`. Emitted at end of the happy path.
2. `WARNING`: the DEC-014 large-schema warning when `existing_schema` exceeds `warn_at_bytes` but stays below the hard cap.

No separate `WARNING` before raising typed errors — the exception IS the signal (mirrors grade DEC-006). No `DEBUG` calls in v0.1; if an operator needs a deeper trace, the reproducibility hashes plus the upstream JSONL audits cover the post-mortem path.

Stdout is the operator channel; `_LOGGER` is for log aggregators / CI runs. INFO + the conditional WARN cover both successful runs and the soft-cap signal without flooding logs.

## ANSI-safe lazy-format JSON logger + grep gate (DEC-019, fifth dir)

Same rule as `safety-layer.md` DEC-022 / `llm-drafter.md` DEC-011 / `prune-engine.md` DEC-017 / `grade-layer.md` DEC-029. Never f-string-interpolate user-controlled strings into a `_LOGGER` call:

```python
_LOGGER.info("rendered diff: %s", json.dumps({"model_unique_id": ..., "kept": k, ...}))
```

The grep gate at `tests/llm/test_logger_grep_gate.py` now scans `src/signalforge/{llm,draft,prune,grade,diff}` (5 dirs) and rejects any `_LOGGER\.\w+\(f"` hit. The regex covers every f-string permutation (`f"`, `f'`, `rf"`, `fr'`, ...). Extend the scan when the CLI (#9, sixth dir) ships, rather than copy-pasting a per-layer gate; the single test is the source of truth.

## No new AST scan for the diff layer (DEC-018)

The 6th AST scan from #7 (gating `GradeEvent`) is the last of its kind in v0.1. The diff renderer has no audit-event class — the sidecar `DiffReport` is read-back, gated by the drift detector (mandatory below) instead. There is no `DiffEvent`; if v0.2 introduces a per-render audit JSONL (e.g., for multi-model batched runs), revisit and add the 7th scan then with the same exclusion-list pattern as the prior five.

The pattern that DOES survive: every fail-closed writer module gets an AST defence test that pins the `try / finally` shape (no `except` around write/fsync). `tests/diff/test_sidecar.py::test_sidecar_writer_has_no_except_around_write` is the diff-layer instance; mirror the assertion when adding a sixth fail-closed writer.

## `_artifact_id` parity with grade layer (DEC-009 + post-QG fix)

`signalforge.diff._artifact_id.artifact_id_for(...)` is a **byte-equal mirror** of `signalforge.grade.engine._artifact_id_for`. The diff renderer joins grade-sidecar JSON to its rendered diff via the `(run_id, artifact_id, criterion_id)` triple; if the two formatters ever disagreed on a single dotted-path shape, every grade row whose `artifact_id` depended on the disagreement would silently drop out of the join.

Cross-stage parity is exercised by `tests/diff/test_artifact_id.py::test_cross_stage_parity_with_grade_engine` — the single allowed cross-stage import seam in the diff test suite. When the formatter's grammar evolves (new artifact shape; `args_hash` rule change), update both the diff and grade copies in lockstep AND extend the parity test.

The collision rule from `grade-layer.md` (8-hex `args_hash` suffix when two tests in the same scope share a `test.type`; ordinal `:1`/`:2` suffix on exact-duplicate args) applies verbatim. The `compute_args_hashes(candidate)` helper pre-computes per-test hashes keyed by `id(test)` so the orchestrator's per-decision walk doesn't recompute on every row.

## Custom `__repr__` on result-shaped models (DEC-020, mirrors prune/grade)

Pydantic v2's default `__repr__` emits every field. `DiffReport` carries `proposed_yaml`, `existing_yaml`, `unified_diff` (potentially multi-megabyte) plus the full `entries` tuple. `DiffEntry` carries `why` (potentially quoting artifact text generated upstream).

`DiffEntry.__repr__` shows only `artifact_id`, `tier`, `drop_reason`, `score`. `DiffReport.__repr__` shows only `model_unique_id`, `kept_count`, `dropped_count`, `flagged_count`, `has_existing_schema`, `duration_seconds`. Raw YAML, unified diff, and prose `why`/`evidence`/`reasoning` stay accessible via field access / `model_dump()`; they just don't slip out the casual debug-print path.

Apply to any future result-shaped model whose fields include user-content payloads or multi-megabyte text bodies. The pattern is "minimal `__repr__`; rich access via fields" — don't override `__str__` (Pydantic uses it for serialisation).

## Drift detectors are mandatory for read-back models (DEC-003)

Every `extra="ignore"` production model — `DiffEntry`, `DiffReport` — pairs with a `Strict<X>(extra="forbid")` mirror in `tests/diff/test_drift_detector.py`, validated against committed fixtures (`tests/fixtures/diff/{diff_entry_v1.json,diff_report_v1.json}`). Adding a field to production without updating the strict mirror OR the fixture breaks the test loudly.

The `extra=` placement convention from `safety-layer.md` DEC-015 applies verbatim:

- `DiffConfig`, `_DiffConfigFile` inner content → `extra="forbid"` (config-shaped; typos like `contxt_lines:` must fail loud).
- `_DiffConfigFile` top level → `extra="ignore"` (sibling stages reserved).
- `DiffEntry`, `DiffReport` → `extra="ignore"` (read-back; forward-compat).

There is no `DiffEvent` to gate (no per-render JSONL); the `DiffReport` sidecar is the single read-back artefact and the drift detector covers it.

## API alignment with adjacent stages

`render_diff(model, candidate, prune_result, *, grading_report=None, existing_schema=None, config=None, output_path=None, sidecar_path=None, project_dir=None) -> DiffReport`. Matches the precedent from `grade_artifacts` / `prune_tests` / `draft_schema`:

- Model + data front-paired positionally.
- Keyword-only optionals separated by `*`.
- Data-shaped optionals (`grading_report`, `existing_schema`) come first inside the kw-only block.
- Behaviour-knob `config` next.
- Path-shaped optionals last (`output_path`, `sidecar_path`, `project_dir`).
- `project_dir` kwarg for orchestrator-level path resolution.

`load_diff_config(project_dir, path=None) -> DiffConfig` matches `load_grade_config` / `load_prune_config` / `load_draft_config` / `load_safety_config`. Resolution order: explicit `path` > `<project_dir>/signalforge.yml diff:` > defaults. Explicit-path-missing raises `DiffError`; default-path-missing returns defaults silently (mirrors grader behaviour exactly).

When introducing a new stage entry in v0.2 or beyond (CLI, multi-model batch runner), match the precedent. Diverging is more code than the alignment.

## `signalforge.yml` top-level namespace: `diff:` (DEC-010)

The diff-stage block is `{ diff: { context_lines, max_why_chars, narrow_terminal_threshold, markdown_max_diff_chars, existing_schema_size_limit_bytes, existing_schema_warn_at_bytes, sidecar_size_limit_bytes, render_kind, respect_no_color_env } }`. Sibling top-level keys (`safety:`, `llm:`, `prune:`, `grade:`, future `cli:`) are reserved for other stages and silently ignored by the diff loader.

`DiffConfig` uses `extra="forbid"`; the wrapping `_DiffConfigFile` uses `extra="ignore"` at the top level. Mirrors `grade-layer.md` DEC-029 / `prune-engine.md` DEC-020 / `llm-drafter.md` DEC-027 / `safety-layer.md` DEC-025 verbatim. Each numeric knob carries a `field_validator` that rejects non-positive values — a zero or negative cap would silently disable the protection or render an empty table.

When introducing a new pipeline-stage config, claim its own top-level key. Don't pile under `diff:` — each stage's behaviour-knob block stays separate.

## Renderer ABC — pure-return, orchestrator owns I/O (DEC-011)

`signalforge.diff._renderers.Renderer` is an ABC with one abstract method: `render(self, report: DiffReport) -> str`. Three concretes return text; the orchestrator (`render_diff`) handles I/O — write to `output_path`, return to caller, dispatch sidecar. Snapshot tests assert `str == fixture_text` byte-for-byte.

Pure-return is testable with no fake filesystem; mirrors prune/grade returning typed results rather than streaming output. Streaming is a v0.2 ask if real-world reports outgrow memory. The three concretes (`AnsiRenderer`, `MarkdownRenderer`, `JsonRenderer`) are private (`_renderers`) per DEC-004; only the typed-result + orchestrator + config + errors form the public contract. `JsonRenderer` is also the one the sidecar writer uses regardless of `config.render_kind` — the human-facing surface and the durable artefact share the JSON shape so a v0.2 GH Action consumer parses identical bytes whether the operator selected ANSI or Markdown for stdout.

When v0.2 adds a custom renderer (e.g., `HtmlRenderer` for a web preview), promote `Renderer` to the public surface in lockstep with the new concrete. Don't expose a single concrete without the ABC — callers should bind to the abstract method, not the implementation.

## v0.2 reservations (forward-compat surface, currently no-op)

Two surface decisions ship in v0.1 but are reserved for v0.2:

- `DiffConfig.render_kind: Literal["ansi", "markdown", "json"] = "ansi"` — exported but the CLI hasn't wired the selection flag yet. v0.2's CLI (#9) will map `--format` to `render_kind`; library callers can already select via the config.
- `DiffReport.audit_schema_version: int = 1` — frozen; v0.2 readers gate on this when the sidecar JSON shape evolves.

Document these explicitly in their docstrings. The pattern: ship the surface in v0.1 so v0.2 is a behaviour change, not an API break.

## Reference

`plans/super/8-diff-renderer.md` — DEC-001 … DEC-021. `src/signalforge/diff/` — current implementation. `docs/diff-ops.md` — operational reference. `tests/diff/test_drift_detector.py` — schema-drift gate. `tests/diff/test_sidecar.py` — fail-closed writer + AST defence. `tests/diff/test_public_api.py` — public-surface pin. `tests/diff/test_artifact_id.py::test_cross_stage_parity_with_grade_engine` — cross-stage parity gate. `tests/llm/test_logger_grep_gate.py` — lazy-format logger gate (5 dirs as of #8). `tests/fixtures/diff/{diff_entry_v1.json,diff_report_v1.json}` — committed sidecar/entry fixtures.
