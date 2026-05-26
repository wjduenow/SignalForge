# Diff layer — operations guide

Operational reference for users of `signalforge.diff`. Companion to
[`docs/safety-ops.md`](safety-ops.md),
[`docs/draft-ops.md`](draft-ops.md),
[`docs/prune-ops.md`](prune-ops.md),
[`docs/grade-ops.md`](grade-ops.md),
[`docs/manifest-loader-ops.md`](manifest-loader-ops.md), and
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md), and to the
design record in
[`plans/super/8-diff-renderer.md`](../plans/super/8-diff-renderer.md).

The diff layer is the terminal stage of SignalForge's draft → prune →
grade → diff pipeline. It consumes the upstream typed results
(`Model`, `CandidateSchema`, `PruneResult`, optional `GradingReport`)
and produces:

1. A **unified diff** between the existing committed `schema.yml` and
   the canonical proposed YAML emitted from the candidate (kept tests
   only).
2. A **kept / kept-uncertain / dropped / flagged table** with one row per candidate
   artefact and a one-line `why` for every entry.
3. A typed :class:`DiffReport` for in-process consumption (returned to
   the caller).
4. An optional on-disk **JSON sidecar** that mirrors the report
   verbatim — the durable hand-off to the diff-rendering CLI (#9), to
   PR-comment posters, or to GitHub Action runs.

This is the load-bearing operationalisation of Architectural
Commitment \#5 in [`CLAUDE.md`](../CLAUDE.md) — **explainable diffs**:
every kept and dropped artefact ships with a one-line `why`, and the
diff itself is a one-glance summary of what would change if the
operator merged the proposed YAML.

## Default posture

The diff layer is **render-only** in v0.1 — it does not gate the run.
A flagged tier surfaces visually but does not return a non-zero exit
code; the operator's eyes are the merge boundary. v0.2 may wire
`fail_on_below_threshold` (currently a no-op held by `GradeConfig`,
not a diff knob) into the CLI exit path.

The layer is fail-closed on three boundaries:

* **Boundary checks (DEC-002).** `candidate.name` must match
  `model.name`; `prune_result.model_unique_id` must match
  `model.unique_id`; the optional `grading_report.model_unique_id`
  must match `model.unique_id`. Mismatch raises a typed
  `Diff*ModelMismatchError` BEFORE any rendering work.
* **Existing-schema size cap (DEC-006).** `existing_schema` is checked
  against a 10 MB byte cap BEFORE any `yaml.safe_load`. Defends against
  billion-laughs / deep-nesting attacks regardless of `safe_load`'s
  constructor restrictions.
* **Sidecar size cap (DEC-009).** The serialised JSON is checked
  against a 10 MB cap BEFORE any `os.open`. Above the cap raises
  `DiffSidecarRecordTooLargeError`; below the cap proceeds to a
  single-document overwrite via `O_WRONLY | O_CREAT | O_TRUNC`,
  followed by `os.fsync`.

Unlike the safety / draft / prune / grade audits, the diff layer does
**not** ship a JSONL audit (DEC-018). The sidecar is a **read-back**
JSON that mirrors `DiffReport` — the kept/dropped decisions were
already audited upstream. The diff layer's job is to **project** those
decisions into an operator-readable form, not to re-record them.

User-facing tagline: **every kept/dropped artefact ships with a
one-line "why"; the diff is a unified diff against the existing
schema.yml; the sidecar is a durable receipt of what was rendered.**

## Public API

Import from `signalforge.diff`. The names exported by `__all__`:

### Orchestrator

- **`render_diff(model, candidate, prune_result, *, grading_report=None, existing_schema=None, config=None, output_path=None, sidecar_path=None, write_sidecar=True, project_dir=None) -> DiffReport`** — End-to-end orchestrator. Boundary-checks the inputs, computes the canonical proposed YAML, builds the unified diff, builds the per-row entry tuple (assigning `tier="kept" | "kept-uncertain" | "dropped" | "flagged"` per DEC-012, with `kept-uncertain` added in issue #50), computes the three reproducibility hashes, dispatches to the configured renderer, optionally writes the rendered text to `output_path` and the JSON sidecar to `sidecar_path`. Mirrors `signalforge.grade.grade_artifacts` and `signalforge.prune.prune_tests` in calling-convention shape: keyword-only optionals, model-front-paired, sequential execution. `project_dir` defaults to `Path.cwd()` and is used for symlink-hardened canonicalisation of `output_path` / `sidecar_path` (DEC-016 of `prune-engine.md`). The sidecar is **on by default** (`write_sidecar=True`); when `sidecar_path` is omitted the sidecar lands at `<project_dir>/.signalforge/diff.json`. Pass `write_sidecar=False` to skip the sidecar entirely (library callers running in-process without a durable artefact).

### Result shapes

- **`DiffReport`** — Aggregate output for one model. Frozen Pydantic model carrying `schema_version: Literal[1]`, `audit_schema_version: Literal[2]` (bumped 1 → 2 in issue #50; external sidecar consumers gate on `>= 2` for the four-tier taxonomy), `signalforge_version: str`, `model_unique_id: str`, `run_id: str`, `duration_seconds: float`, `proposed_yaml: str`, `existing_yaml: str | None`, `unified_diff: str`, `entries: tuple[DiffEntry, ...]`, `kept_count: int`, `kept_uncertain_count: int` (added in issue #50), `dropped_count: int`, `flagged_count: int`, `has_existing_schema: bool`, `candidate_hash: str`, `prune_result_hash: str`, `grading_report_hash: str | None`. Custom `__repr__` collapses to identity + aggregate counts (DEC-020) so accidental `_LOGGER.warning("report: %s", report)` does not dump multi-megabyte diff content into log sinks. The full content remains accessible via field access / `model_dump()`.

- **`DiffEntry`** — One row in the rendered table. Carries `artifact_id: str` (canonical dotted-path mirroring `grade.GradingResult.artifact_id`), `test_type: str | None`, `tier: Tier`, `drop_reason: DropReason | None`, `why: str`, `score: float | None`, `passed: bool | None`. Custom `__repr__` omits the prose `why` to protect against accidental log dumps.

- **`Tier`** — `Literal["kept", "kept-uncertain", "dropped", "flagged"]`. Closed enumeration of per-entry tiers (DEC-012; `kept-uncertain` added in issue #50). `flagged` is set only when a grading report is provided AND the artefact's grading is below threshold (any criterion failed OR `score=None` graceful-degrade) AND the prune decision had positive evidence. `kept-uncertain` fires when the per-row `PruneDecision.reason == "kept-without-evidence"` (origin dominates over grading — a test we couldn't evaluate cannot meaningfully fail a rubric). `kept` and `dropped` mirror the prune layer's binary verdict for the with-evidence path.

### Configuration

- **`DiffConfig`** — User-facing knobs. Frozen Pydantic model with `extra="forbid"` (config-shaped per `safety-layer.md` DEC-015 — typos fail loud). Field reference: see [Configuration](#configuration-signalforgeyml-diff-block) below.

- **`load_diff_config(project_dir, path=None) -> DiffConfig`** — Loads the `diff:` block from `signalforge.yml`. Resolves to `<project_dir>/signalforge.yml` when `path` is `None`. Returns defaults when the file is missing, empty, or the `diff:` key is absent. Raises `DiffError` on parse / schema failures. Mirrors `load_safety_config` / `load_draft_config` / `load_prune_config` / `load_grade_config` so the CLI sees one calling convention across stages.

### Errors

`from signalforge.diff import errors`. Every exception subclasses
`DiffError` and carries a class-level `default_remediation` rendered
on a `↳ Remediation:` line by `__str__`.

- **`DiffError`** — Base class. Reused as a typed wrapper by `load_diff_config` for config-load failures (US-003 deferred a dedicated `DiffConfigError` to v0.2 — the base class with a remediation is sufficient for v0.1).
- **`DiffCandidateModelMismatchError`** — `candidate.name` does not match `model.name` (DEC-002). Raised at orchestrator entry BEFORE any rendering work.
- **`DiffPruneResultModelMismatchError`** — `prune_result.model_unique_id` does not match `model.unique_id` (DEC-002). Same fail-fast posture as the candidate boundary check.
- **`DiffGradingReportModelMismatchError`** — `grading_report.model_unique_id` does not match `model.unique_id` (DEC-002). Only raised when `grading_report` is provided.
- **`DiffInputTooLargeError`** — `existing_schema` byte length exceeded `existing_schema_size_limit_bytes` (default 10 MB). Refused BEFORE any `yaml.safe_load` to defend against billion-laughs / deep-nesting attacks (DEC-006).
- **`DiffSidecarRecordTooLargeError`** — Serialised sidecar JSON exceeded `sidecar_size_limit_bytes` (default 10 MB). Refused BEFORE any `os.open` so an oversize payload leaves no on-disk artefact (DEC-009).
- **`DiffSidecarWriteError`** — Fail-closed wrapper around OS errors raised inside the sidecar writer (`OSError` / `PermissionError` / encoding / `fsync` / symlink containment failure). Original cause exposed via `.cause` and `__cause__`.

## Configuration: `signalforge.yml` `diff:` block

Top-level namespace is `diff:` (claimed per the convention from
`safety-layer.md` DEC-025 / `llm-drafter.md` DEC-027 / `prune-engine.md`
DEC-020 / `grade-layer.md` DEC-029 — every pipeline stage gets one
top-level key). Sibling keys (`safety:`, `llm:`, `prune:`, `grade:`,
future `cli:`) are reserved for other stages and silently ignored by
the diff loader.

The full schema (every knob, every default, all v0.1 types):

```yaml
# signalforge.yml — diff stage configuration (v0.1)
diff:
  context_lines: 3                      # difflib.unified_diff(n=...) — diff -u default
  max_why_chars: 80                     # per-row "why" truncation cap
  narrow_terminal_threshold: 60         # cols below which AnsiRenderer compacts
  markdown_max_diff_chars: 60000        # Markdown body truncation cap (DEC-005)
  existing_schema_size_limit_bytes: 10485760    # 10 MB hard cap (DEC-006)
  existing_schema_warn_at_bytes: 1000000        # 1 MB soft warning (DEC-014)
  sidecar_size_limit_bytes: 10000000    # 10 MB sidecar JSON cap (DEC-009)
  render_kind: ansi                     # ansi | markdown | json (DEC-004)
  respect_no_color_env: true            # honour NO_COLOR / FORCE_COLOR / isatty (DEC-021)
```

A minimal `signalforge.yml` is just `diff: {}` (or no `diff:` key at
all) — every field has a locked default from DEC-010 and the loader
returns `DiffConfig()` silently.

Field-by-field:

- **`context_lines`** — Number of context lines around each hunk in the unified diff. Passed through to `difflib.unified_diff(n=...)`. Default `3` (the `diff -u` default). Tighten to `1` for compact PR comments; widen to `7` for review-heavy contexts where reviewers need more surrounding YAML.
- **`max_why_chars`** — Hard truncation cap on the per-row `why` column in the kept/dropped table. Beyond this, the renderer truncates with an ellipsis. Default `80`. Keeps the table readable at any terminal width without forcing the upstream layers (prune, grade) to second-guess display constraints.
- **`narrow_terminal_threshold`** — Below this column count, the `AnsiRenderer` drops the `why` column from the kept/dropped table and emits each `why` as a wrapped follow-up line below its row (DEC-013). Default `60`. Tested against a 40-col snapshot fixture.
- **`markdown_max_diff_chars`** — Truncation cap on the rendered Markdown diff body (DEC-005). Default `60_000`. GitHub PR comments are 65_536 chars; the 60_000 cap leaves room for the table, the prelude, and the truncation footer (`... (N more lines truncated — see <project_dir>/.signalforge/diff.json for full diff)`). The kept/dropped/flagged table always renders fully (one line per artifact; small).
- **`existing_schema_size_limit_bytes`** — Hard cap on `existing_schema` YAML byte length, enforced BEFORE any `yaml.safe_load` call (DEC-006). Default `10_485_760` (10 MB). Defends against billion-laughs / deep-nesting attacks regardless of `safe_load`'s constructor restrictions. Above the cap raises `DiffInputTooLargeError`.
- **`existing_schema_warn_at_bytes`** — Soft warning threshold for `existing_schema` YAML byte length (DEC-014). Default `1_000_000` (1 MB). The renderer emits one `WARNING` log line when the payload exceeds this threshold but stays below the hard cap; mirrors `signalforge.warehouse.profiles` DEC-023 ("operators get a heads-up before the hard cap trips").
- **`sidecar_size_limit_bytes`** — Hard cap on the diff sidecar JSON byte length, enforced BEFORE any `os.open` (DEC-009). Default `10_000_000` (10 MB — an order of magnitude above the grade sidecar's 1 MB cap because diff text is naturally larger). Above the cap raises `DiffSidecarRecordTooLargeError`.
- **`render_kind`** — `Literal["ansi", "markdown", "json"]`. Default `"ansi"`. Selects which renderer concrete drives stdout output (DEC-004). The sidecar always uses the JSON renderer regardless of this setting; `render_kind` only governs the human-facing output. See [Renderer kinds](#renderer-kinds-dec-004) below.
- **`respect_no_color_env`** — When `True` (the default), the AnsiRenderer honours `NO_COLOR` and `FORCE_COLOR` environment variables along with `sys.stdout.isatty()` (DEC-021). When `False`, colour is forced regardless — useful for tests and non-tty pipelines that want ANSI output.

Unknown keys under `diff:` raise `DiffError` (Pydantic
`extra="forbid"`). Typos like `contxt_lines:` or `max_why_chrs:`
fail loud at load time rather than silently no-op'ing. The outer
`_DiffConfigFile` wrapper uses `extra="ignore"` at the top level so
sibling top-level keys from other stages don't break the loader.

## Renderer kinds (DEC-004)

Three concretes ship under `signalforge.diff._renderers` (private per
DEC-004 — only the typed-result + orchestrator + config + errors form
the public API). Each implements `Renderer.render(report: DiffReport) -> str`,
a pure-return signature: the renderer produces the rendered text and
the orchestrator handles I/O. Pure-return is testable against
byte-for-byte snapshot fixtures; streaming is a v0.2 ask (DEC-011).

| `render_kind` | Output                                            | Use case                                                                                             |
| ------------- | ------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `ansi`        | ANSI-coloured text + box-drawn kept/dropped table | Default. Local terminal, `signalforge generate` interactive run.                                     |
| `markdown`    | GitHub-flavoured Markdown with fenced diff blocks | PR comments, GitHub Actions step summary, anywhere a Markdown renderer parses the output.            |
| `json`        | Single JSON document mirroring `DiffReport`       | The sidecar shape. Always emitted for the `sidecar_path` write regardless of `render_kind`.          |

The `AnsiRenderer` honours **NO_COLOR** spec precedence (DEC-021):
`config.respect_no_color_env=False` (forces colour) → `--no-color` CLI
flag (wired by #9 to a renderer parameter) → `FORCE_COLOR=1` env →
`NO_COLOR` env (any value) → `sys.stdout.isatty()`. ANSI escape
**stripping** (DEC-007) on every user-content field
(`description`, `rationale`, `evidence`, `reasoning`, `why`) runs
**unconditionally** — the precedence only decides whether the
renderer's own colour codes get emitted on top. A drafted column
description containing `\x1b[31mevil` cannot inject colour into the
rendered output regardless of the operator's terminal setting.

The `MarkdownRenderer` escapes Markdown-injection vectors in table
cells (DEC-008): triple-backticks, pipe (`|`), backslash (`\`). Raw
HTML (`<...>`) is HTML-entity-encoded for table cells. **Inside the
fenced ```diff block, raw content passes through** — the diff is
YAML, and GitHub doesn't interpret HTML inside a fenced `diff` block.
Snapshot fixtures pin the escaping behaviour.

The `JsonRenderer` round-trips `DiffReport.model_dump_json()` so a
sidecar-reading consumer gets the same view as the orchestrator's
return value.

## Sidecar JSON schema

> **Consumer guide.** For cross-stage joins (grade sidecar ↔ diff sidecar
> on `artifact_id`), `jq` / pandas worked examples, the forward-compat
> policy (including the `audit_schema_version: 2` bump from issue #50),
> and the redaction surface, see [`docs/audits.md`](audits.md). This
> section is the diff-layer production contract.

End-of-run write — **on by default** (`render_diff(..., write_sidecar=True)`
is the default; pass `write_sidecar=False` to skip). When
`sidecar_path` is `None` and `write_sidecar=True`, the sidecar lands at
`<project_dir>/.signalforge/diff.json`. Mirrors grade / prune's
"always-write" posture for the durable hand-off; library callers that
need an in-process render without a disk artefact opt out via
`write_sidecar=False`.

The sidecar is the durable hand-off for cases where the operator needs
structured access (CI, PR comments, dashboards, reproducibility
checks); the renderer's stdout output is the primary human-facing
surface.

When written, the file lives at `sidecar_path` (caller-supplied) or at
`<project_dir>/.signalforge/diff.json` (the default convention).
Single-document overwrite via `O_WRONLY | O_CREAT | O_TRUNC` (DEC-009);
a re-run replaces the prior sidecar atomically (subject to platform
truncate semantics). The sidecar size cap is 10 MB
(`_DIFF_SIDECAR_RECORD_LIMIT_BYTES`).

The sidecar carries the `DiffReport` shape verbatim (the same shape
the orchestrator returns):

```json
{
  "schema_version": 1,
  "audit_schema_version": 3,
  "signalforge_version": "0.1.0.dev0",
  "model_unique_id": "model.shop.dim_customers",
  "run_id": "a1b2c3d4e5f6478890aabbccddeeff00",
  "duration_seconds": 0.041,
  "proposed_yaml": "version: 2\nmodels:\n  - name: dim_customers\n    ...",
  "existing_yaml": "version: 2\nmodels:\n  - name: dim_customers\n    ...",
  "unified_diff": "--- a/models/dim_customers.yml\n+++ b/models/dim_customers.yml\n@@ ...",
  "entries": [
    {
      "artifact_id": "column.email.description",
      "test_type": null,
      "tier": "kept",
      "drop_reason": null,
      "why": "kept by prune; clarity 0.85, consistency 0.90",
      "score": 0.875,
      "passed": true
    },
    {
      "artifact_id": "test.column.email.unique",
      "test_type": "unique",
      "tier": "kept-uncertain",
      "drop_reason": null,
      "why": "total prune budget exceeded before evaluation",
      "score": null,
      "passed": null
    }
  ],
  "proposed_test_files": [
    {
      "path": "tests/dim_customers__total_amount_custom_sql_a1b2c3d4.sql",
      "sql": "-- signalforge:generated a1b2c3d4\n\nselect * from {{ ref('dim_customers') }} where total_amount < 0\n"
    }
  ],
  "kept_count": 8,
  "kept_uncertain_count": 1,
  "dropped_count": 2,
  "flagged_count": 1,
  "has_existing_schema": true,
  "candidate_hash": "0123456789abcdef",
  "prune_result_hash": "fedcba9876543210",
  "grading_report_hash": "abcdef0123456789"
}
```

**`audit_schema_version: 3`** is the issue-#116 bump (was `2` after
issue #50, `1` in v0.1). Issue #116 added the `proposed_test_files`
array — the tuple of standalone `.sql` test files emitted for every
KEPT singular `custom_sql` business-rule test (these are NOT
schema.yml blocks, so they never appear in `proposed_yaml` /
`unified_diff`; each carries a slug-safe relative `path` and the SQL
body with its `-- signalforge:generated <hash>` header marker). The
prior issue-#50 bump (`1 → 2`) added the `kept-uncertain` tier literal
+ `kept_uncertain_count`. External sidecar consumers (CI parsers, the
v0.3 GitHub Action) gate on `audit_schema_version >= 3` to consume the
proposed-test-files array, and on `>= 2` for the four-tier taxonomy; a
reader gating on `== 1` / `== 2` will reject newer sidecars (the bump is
intentionally a hard break — the sidecar is `O_TRUNC` write-only and
prior runs' bytes are not preserved).

**`run_id` correlation.** `run_id` is a fresh `uuid4().hex` stamped at
orchestrator entry. The diff layer doesn't have a JSONL audit, but the
field exists so future cross-stage tooling (e.g. a multi-model batch
runner) can correlate one diff sidecar with sibling grade JSONLs that
ran in the same logical "render" event.

**Drift gates.** `tests/fixtures/diff/diff_report_v1.json` and
`diff_entry_v1.json` are the canonical schema fixtures;
`tests/diff/test_drift_detector.py` pairs each production model
(`extra="ignore"`) with a one-off `extra="forbid"` strict mirror and
validates against the fixture. Adding a field to `DiffReport` /
`DiffEntry` without updating the strict mirror OR the fixture breaks
the test loudly. Don't bypass.

## Reproducibility / hash fields (DEC-016)

Three hash fields land on every `DiffReport`, all 16-hex-char
`blake2b` with `digest_size=8`. The cross-stage hash domain is
consistent — a reviewer querying "which prune-result drove this diff"
can compare bytes verbatim against the prune sidecar / JSONL's hashes.

- **`candidate_hash`** — `blake2b-8` of `candidate.model_dump_json(sort_keys=True)`. Fingerprints the post-draft (and post-anchor-contract-validated) candidate schema. Same `candidate_hash` across two runs = same drafted artefacts = same kept/dropped table modulo prune-side variation.
- **`prune_result_hash`** — `blake2b-8` of `prune_result.model_dump_json(sort_keys=True)`. Fingerprints the prune verdict. Two runs against the same warehouse data with the same trusted-models / sample-size config should produce the same prune hash.
- **`grading_report_hash`** — `blake2b-8` of `grading_report.model_dump_json(sort_keys=True)` when provided; `None` otherwise. Fingerprints the rubric verdicts (and indirectly the rubric itself via `grading_report.rubric_hash`).

A reproducibility check for "did the diff change because the candidate
changed, or because prune verdicts shifted?" reads to: compare the
three hashes between two `DiffReport`s. Any single hash differing
points to that stage's input changing.

## Decision matrix — the kept / kept-uncertain / dropped / flagged table

| Tier             | Source                                                                                                                                                                                                                                                                                                                                                                            | One-line `why` example                                              | Display in `ansi` renderer            |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | ------------------------------------- |
| `kept`           | `PruneDecision.decision == "kept"` AND `PruneDecision.reason == "kept"` (positive prune evidence). No grading report OR grading passed.                                                                                                                                                                                                                                              | "kept by prune; clarity 0.85, consistency 0.90"                     | green, full row                       |
| `kept-uncertain` | `PruneDecision.decision == "kept"` AND `PruneDecision.reason == "kept-without-evidence"` (issue #50). The prune layer could not positively evaluate the test — total budget exhausted, identifier rejected by SQL safety check, warehouse call raised, `prune.enabled: false`, or sample materialisation failed. **Origin dominates over grading** — never collapses to `flagged`. | "total prune budget exceeded before evaluation"                     | cyan, full row, no score              |
| `dropped`        | `PruneDecision.decision == "dropped"`. `drop_reason` carries the prune layer's `DropReason` literal.                                                                                                                                                                                                                                                                                | "always-passes (sample of 10000 rows; 0 failing rows)"              | red, dimmed row                       |
| `flagged`        | Kept by prune (with positive evidence) AND grading is below threshold (any criterion failed OR `score=None` degraded sentinel).                                                                                                                                                                                                                                                    | "passed prune; clarity 0.30 (failed)"                               | yellow, full row with score badge     |

`flagged` is set only when `grading_report is not None` AND the entry's
grading is below threshold AND the prune decision had positive evidence.
Without a grading report, no entry can be `flagged`; without a prune
drop, no entry can be `dropped`; and a kept-without-evidence prune
decision projects to `kept-uncertain` regardless of grading attachment.
The diff layer never *creates* signal; it only **projects** upstream
signal.

**Why kept-uncertain is its own tier (issue #50).** Architectural
Commitment #5 ("explainable diffs") and the prune layer's conservative
bias (`docs/rules/prune-engine.md` DEC-006, DEC-011) jointly say:
a test we couldn't positively evaluate is shipped (kept) but the
reviewer must see "we shipped this without evidence" distinctly from
"this test caught a real failing row." Pre-#50 the diff renderer
collapsed both into `tier="kept"` (green) and the only signal was the
prose `why` field; the load-bearing conservative-bias message was
visually invisible. Issue #50 added the fourth tier so reviewers
scanning the kept column see the kept-uncertain rows immediately.

**`why` cascade for kept-uncertain.** Kept-uncertain rows bypass the
rationale → evidence → fallback cascade used for ordinary kept rows
(`why` priority is `CandidateTest.rationale` → first non-empty
`GradingResult.evidence` → `decision.why` for plain kept rows). For
kept-uncertain rows, the prune layer's `decision.why` is surfaced
directly — it carries the deterministic load-bearing message ("total
prune budget exceeded before evaluation" / "sample materialisation
failed: …" / "identifier rejected by SQL safety check") that names
the actual cause. The drafter's rationale and the grader's evidence
are not meaningful for a test we couldn't evaluate.

## Operational notes

### Symlink-hardened path canonicalisation

Every caller-supplied path that the orchestrator opens routes through
`signalforge.warehouse._path_safety.canonicalise_path` BEFORE the
write — `output_path` for the rendered text and `sidecar_path` for the
JSON sidecar. A symlink at `<project>/.signalforge/diff.json`
pointing outside the project tree is rejected with
`DiffSidecarWriteError(cause=...)` (the underlying cause is a
`signalforge.warehouse.errors.ProfileNotFoundError`). Mirrors the
post-QG fix recorded in `prune-engine.md` and `grade-layer.md`: trust
the writer to derive `project_dir` is unsafe; the orchestrator is the
place that knows the true `project_dir`.

The writer's own `canonicalise_path` call stays as defence-in-depth.

### Soft warning on large existing schema

`render_diff` emits one `WARNING` log line when
`len(existing_schema.encode("utf-8")) > existing_schema_warn_at_bytes`
(default 1 MB):

```text
WARNING signalforge.diff.engine: large existing schema.yml: {"bytes": 1234567, "model_unique_id": "model.shop.dim_customers", "warn_at": 1000000}
```

Lazy-format JSON per DEC-014 — never f-string. The soft warning
(default 1 MB) fires below the hard cap
(`existing_schema_size_limit_bytes`, default 10 MB), giving operators
a heads-up before the hard cap trips and a typed
`DiffInputTooLargeError` is raised.

### Log event policy (DEC-015)

Two `_LOGGER` events in the entire diff layer — both lazy-format JSON:

1. **INFO** — One line at the end of every `render_diff` happy path:

   ```text
   INFO signalforge.diff.engine: rendered diff: {"model_unique_id": "...", "kept": 8, "kept_uncertain": 1, "dropped": 2, "flagged": 1, "has_existing_schema": true, "duration_seconds": 0.041}
   ```
2. **WARNING** — The DEC-014 large-schema warning (above). One line per
   `render_diff` call when `existing_schema` exceeds
   `existing_schema_warn_at_bytes` (default 1 MB) but stays below the
   hard cap (default 10 MB).

No separate `WARNING` before raising typed errors — the exception IS
the signal (mirrors grade DEC-006). The diff layer never logs full
rendered text or YAML content; the sidecar JSON is the durable
record of decision-level detail. The custom `__repr__` on `DiffReport`
and `DiffEntry` defends accidental
`_LOGGER.warning("report: %s", report)` calls from dumping multi-megabyte
diff content into log sinks.

### ANSI escape stripping (DEC-007)

Every user-content field the AnsiRenderer touches (`description`,
`rationale`, `evidence`, `reasoning`, `why`) is routed through
`signalforge.diff._ansi_safety.strip_ansi_escapes` BEFORE colourising.
The strict regex is `r'\x1b\[[0-9;]*[a-zA-Z]'` (covers SGR plus all
CSI). The renderer adds its own sanctioned colour codes after
stripping; user content cannot inject. Mirrors the DEC-022 logger gate
(audit channel) at the stdout sink (a separate sink with separate
defences).

### Markdown injection escaping (DEC-008)

`signalforge.diff._markdown_safety.escape_markdown_scalar` handles
table cells: triple-backticks (` ``` ` → `` `\``` ``), pipe (`|` → `\|`),
backslash (`\` → `\\`). Raw HTML (`<...>`) is HTML-entity-encoded for
table cells; **inside the fenced ```diff block, raw content passes
through** (the diff is YAML; backticks don't break a `diff` fence;
GitHub doesn't interpret HTML inside fenced blocks).

Test fixtures exercise the escaping for: triple-backticks in
description, `</details>`, `[evil](javascript:...)`, pipe in column
name.

## Snapshot fixture matrix (DEC-017)

Ten cases under `tests/fixtures/diff/`, regenerable via
`tests/fixtures/diff/regenerate.sh`. Plain-text fixtures, byte-for-byte
diff (no syrupy dep). The matrix is 3 surfaces × 3 happy/edge/hostile
cases plus the no-existing-schema branch and an injection-payload
fixture:

| Fixture                       | Inputs                                                          | Surface     | What it covers                                                          |
| ----------------------------- | --------------------------------------------------------------- | ----------- | ----------------------------------------------------------------------- |
| `full_with_grade`             | Kept + dropped + flagged, grading provided                      | `ansi`      | Happy path, all three tiers represented.                                |
| `full_with_grade.md`          | Same inputs                                                     | `markdown`  | Markdown table + fenced diff + truncation footer behaviour.             |
| `full_with_grade.json`        | Same inputs                                                     | `json`      | The sidecar shape; round-trips via Pydantic.                            |
| `no_existing_schema`          | `existing_schema=None`                                          | `ansi`      | Unified diff sources from `/dev/null`; first-time-rendering case.       |
| `kept_only`                   | Every artefact `decision="kept"`                                | `ansi`      | Empty dropped table; all-pass UX.                                       |
| `dropped_only`                | Every artefact `decision="dropped"`                             | `ansi`      | Empty kept table; rare but possible.                                    |
| `no_grading_report`           | `grading_report=None`                                           | `ansi`      | No flagged tier; per-row score columns absent.                          |
| `plain_no_color`              | `NO_COLOR=1`                                                    | `ansi`      | Asserts no escape codes in output (DEC-021 precedence).                 |
| `narrow_terminal`             | 40-col TTY                                                      | `ansi`      | DEC-013 compact mode (drops `why` column, emits as wrapped follow-up).  |
| `injection_payloads`          | Descriptions contain `\x1b[31m`, triple-backticks, `</details>` | `ansi` + `markdown` | DEC-007 + DEC-008 + AR-9 escaping verifies ANSI strip + Markdown escape. |

## Audit-log sensitivity

The diff sidecar can carry verbatim fragments of the artefact text
(column descriptions, model docs, test rationales) and the full
`unified_diff` body. Treat at-rest the same way you treat the safety /
draft / prune / grade audits:

- **Gitignore `.signalforge/`** (already configured in this repo's `.gitignore`).
- **Restrict at-rest permissions.** The writer creates files at `0o600` on first call; the parent directory is created via `mkdir(parents=True, exist_ok=True)` (Python's `mkdir` does not tighten an existing directory's permissions, so verify the existing `.signalforge/` mode is `0o700` on shared hosts).
- **Don't ship as a build artefact.** Strip from container images and CI uploads; if the diff sidecar is the input to a PR comment, post the rendered Markdown / JSON contents into the comment and delete the file from the runner.
- **Symlink-hardened paths.** Both `output_path` and `sidecar_path` route through `signalforge.warehouse._path_safety.canonicalise_path` at orchestrator entry. A symlinked `.signalforge/diff.json -> /etc/passwd` is rejected as `DiffSidecarWriteError` before the `os.open` ever fires.

## Debugging

Logger name: `signalforge.diff.engine` (and sibling modules under
`signalforge.diff`).

```python
import logging
logging.getLogger("signalforge.diff").setLevel(logging.DEBUG)
```

Levels:

- **INFO** — One line per `render_diff` happy-path invocation, lazy-format JSON per DEC-015 (`model_unique_id`, `kept`, `kept_uncertain`, `dropped`, `flagged`, `has_existing_schema`, `duration_seconds`). Mirrors `safety-layer.md` DEC-022 / `llm-drafter.md` DEC-011 / `prune-engine.md` DEC-017 / `grade-layer.md` DEC-029 — never f-string-interpolate user-controlled strings into a logger call. The logger grep gate at `tests/llm/test_logger_grep_gate.py` enforces this for `src/signalforge/diff/` (DEC-019 of #8).
- **WARNING** — One line when `existing_schema` exceeds `existing_schema_warn_at_bytes` (DEC-014).
- **DEBUG** — Reserved for future per-row latency observability; v0.1 emits no DEBUG from the engine.

The diff layer never logs full rendered text or YAML content. The
sidecar JSON is the single durable record of decision-level detail;
logger output is a hint that the rendering happened, not what was in
it.

**Reading a fail-closed `DiffSidecarWriteError`.** The cause is exposed
as `.cause` and on `__cause__`. Common causes:

- Parent directory not writable (no `+w` for the user, or `.signalforge/` is a symlink to a read-only mount).
- Disk full (`ENOSPC`).
- Symlink containment violation (the sidecar / output path canonicalises outside `<project_dir>`). The cause is a `signalforge.warehouse.errors.ProfileNotFoundError`.
- Oversize record (raises `DiffSidecarRecordTooLargeError` instead — for the sidecar, a 10 MB cap suggests a runaway candidate / prune payload that the upstream stages should already have rejected).

## Failure modes / typed-error cross-reference

| Class                                  | When raised                                                                                       | Where it surfaces                       | How to fix                                                                                                              |
| -------------------------------------- | ------------------------------------------------------------------------------------------------- | --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `DiffError`                            | Base class; also raised by `load_diff_config` on parse / schema failure (US-003 deferred a typed config error to v0.2). | `signalforge.diff.errors`               | Catch it to handle every diff-layer failure uniformly.                                                                  |
| `DiffCandidateModelMismatchError`      | `candidate.name != model.name` (DEC-002).                                                         | `render_diff` orchestrator entry.       | Confirm the candidate the drafter produced was for `model`. Stale `CandidateSchema` from a different draft = rebuild.   |
| `DiffPruneResultModelMismatchError`    | `prune_result.model_unique_id != model.unique_id` (DEC-002).                                      | `render_diff` orchestrator entry.       | Confirm `prune_tests` ran against the same model. A stale `PruneResult` from a sibling model is the usual cause.        |
| `DiffGradingReportModelMismatchError`  | `grading_report.model_unique_id != model.unique_id` (DEC-002).                                    | `render_diff` orchestrator entry.       | Confirm `grade_artifacts` ran against the same model.                                                                   |
| `DiffInputTooLargeError`               | `existing_schema` byte length exceeded the cap (default 10 MB; DEC-006).                          | `render_diff` orchestrator (pre-`yaml.safe_load`). | Trim the existing `schema.yml` (likely a runaway dbt-osmosis or codegen output). Or raise the cap if the YAML is legitimately large. |
| `DiffSidecarRecordTooLargeError`       | Serialised sidecar JSON exceeded the cap (default 10 MB; DEC-009).                                | `write_sidecar` (pre-`os.open`).        | Investigate the candidate / prune payload — a sidecar above 10 MB suggests a 1000-column model with hostile descriptions. |
| `DiffSidecarWriteError`                | Path containment failure or any underlying I/O failure on the write path.                         | `write_sidecar` and `_write_rendered_text` (wrapped at orchestrator entry). | Verify `<project_dir>/.signalforge/` is writable, has disk space, and is not a symlink escaping the project tree. Inspect `.cause`. |

## Regen instructions for fixtures

The diff-layer snapshot fixtures under `tests/fixtures/diff/` are
**hand-authored** (they don't depend on a live LLM run). The
companion `tests/fixtures/diff/regenerate.sh` script provides a
deterministic regeneration path that writes the fixtures via the
production renderer with pinned inputs — useful when a renderer
template change is intentional. To regenerate after a model field /
renderer change:

1. Update the production model (`signalforge.diff.models` —
   `DiffReport` / `DiffEntry`) or the renderer
   (`signalforge.diff._renderers`).
2. Update the matching strict mirror in
   `tests/diff/test_drift_detector.py`. Both must change in the same
   commit, or the strict-validates-fixture check fails.
3. Run `tests/fixtures/diff/regenerate.sh` to refresh the snapshot
   fixtures from the production renderer.
4. Run `pytest tests/diff/ -v` — the strict model must validate the
   fixture, AND every snapshot test must pass byte-for-byte.

`tests/fixtures/diff/diff_report_v1.json` and `diff_entry_v1.json`
are the schema-drift fixtures (independent of the snapshot fixtures);
edit by hand to add / remove fields. Keep the values readable (no
test fixture should be a wall of placeholder hashes).

## CLI integration note

Tracked in [issue #9](https://github.com/wjduenow/SignalForge/issues/9).
The `signalforge generate` CLI will load the diff config via
`load_diff_config(...)` and invoke `render_diff(...)` after the grade
step completes (or directly after prune if `--no-grade` is supplied).
Renderer selection (`--render=ansi|markdown|json`) overrides
`config.render_kind`; `--no-color` overrides
`config.respect_no_color_env` to `False` and the AnsiRenderer's
precedence ladder routes through to a plain ANSI strip.

The CLI will also surface the kept / dropped / flagged counts to the
operator's stdout summary, but **will not** gate exit code on
`flagged_count > 0` in v0.1 — the diff layer is render-only, and the
operator's eyes are the merge boundary. v0.2 may wire a
`--fail-on-flagged` CLI flag for CI-strict deployments.

## References

- Design record: [`plans/super/8-diff-renderer.md`](../plans/super/8-diff-renderer.md).
- Grade-layer counterpart (the layer the diff renderer mirrors most
  patterns from for fail-closed sidecar + `__repr__` discipline +
  drift-detector convention):
  [`docs/grade-ops.md`](grade-ops.md).
- Prune-layer counterpart (the layer the diff renderer mirrors for
  symlink-hardened path canonicalisation at orchestrator entry):
  [`docs/prune-ops.md`](prune-ops.md).
- Drafter counterpart (the layer the diff renderer mirrors for the
  `<MODEL_SQL>`-style envelope-breach guard semantics, even though the
  diff layer's defences are at the stdout / Markdown sinks rather than
  the LLM-prompt sink):
  [`docs/draft-ops.md`](draft-ops.md).
- Manifest reader conventions
  (`frozen` / `extra="ignore"` / drift-detector pattern):
  [`docs/rules/manifest-readers.md`](../docs/rules/manifest-readers.md).

Cross-reference DECs (from `plans/super/8-diff-renderer.md`):
DEC-002 (boundary checks), DEC-003 (drift detectors), DEC-004
(renderer concretes private; orchestrator picks via `render_kind`),
DEC-005 (Markdown body truncation), DEC-006 (existing-schema YAML
size cap), DEC-007 (ANSI escape stripping in user content), DEC-008
(Markdown injection escaping), DEC-009 (sidecar size cap), DEC-010
(`DiffConfig` field set + `diff:` namespace), DEC-011 (renderer ABC
pure-return signature), DEC-012 (`Tier` literal), DEC-013
(terminal-width compact mode), DEC-014 (soft warning on large
existing schema), DEC-015 (two-event log policy), DEC-016 (sidecar
field set with reproducibility hashes), DEC-017 (snapshot fixture
matrix), DEC-018 (no `DiffEvent` AST scan in v0.1), DEC-019 (logger
grep gate extends to `src/signalforge/diff/` — fifth directory),
DEC-020 (custom `__repr__` on result-shaped models), DEC-021
(NO_COLOR precedence for the AnsiRenderer).
