# Audit JSONL & sidecar consumer guide

Every `signalforge generate` run lands six durable artefacts under `<project_dir>/.signalforge/`. Four are append-only JSONL streams (one record per LLM call / prune decision / grade call); two are end-of-run sidecar JSON documents written as a single-document overwrite (`O_TRUNC` + write + `fsync`). The sidecar overwrite is not visible-atomic across the whole filesystem call — a concurrent reader during the write window can see an empty or partial file — so consumer pipelines should wait for the orchestrator to return before reading. This guide is the cross-cutting consumer surface — what's in each file, how to join them, and how to query them with `jq` or pandas.

For the per-stage *production* contracts (defaults, error remediation, cost guidance), keep reading the per-stage ops docs:

- [`docs/safety-ops.md`](safety-ops.md) — safety layer (PII redaction + LLM-request audit)
- [`docs/draft-ops.md`](draft-ops.md) — LLM draft pipeline (response audit)
- [`docs/prune-ops.md`](prune-ops.md) — test prune engine (per-decision audit)
- [`docs/grade-ops.md`](grade-ops.md) — quality grader (per-call audit + end-of-run sidecar)
- [`docs/diff-ops.md`](diff-ops.md) — diff renderer (end-of-run sidecar)

## Artefact table

| File | One-line content | Per-record version field | Drift-detector test |
|------|------------------|--------------------------|---------------------|
| `.signalforge/audit.jsonl` | One safety `AuditEvent` per `build_llm_request` call (LLM request leaving the warehouse boundary, plus column-name redaction map) | `audit_schema_version: 3` | `tests/safety/test_drift_detector.py` |
| `.signalforge/llm_responses.jsonl` | One `LLMResponseEvent` per successful LLM round-trip in the draft layer (prompt + response hashes, cache-token economics) | `audit_schema_version: 1` | `tests/draft/test_drift_detector.py` |
| `.signalforge/prune.jsonl` | One `PruneEvent` per candidate test routed through the prune orchestrator (decision, reason, compiled SQL hash, optional sample failures) | `audit_schema_version: 2` | `tests/prune/test_drift_detector.py` |
| `.signalforge/grade.jsonl` | One `GradeEvent` per `(artifact × criterion)` LLM-as-judge call (score, evidence, reasoning, response-token economics) | `audit_schema_version: 1` | `tests/grade/test_drift_detector.py` |
| `.signalforge/grade.json` | End-of-run `GradingReport` sidecar — aggregate `pass_rate` / `mean_score`, `aggregate_complete`, every per-result row | `grade_schema_version: 1` (top-level) | `tests/grade/test_drift_detector.py` |
| `.signalforge/diff.json` | End-of-run `DiffReport` sidecar — kept / kept-uncertain / dropped / flagged entries, proposed YAML, unified diff, reproducibility hashes | `schema_version: 1` + `audit_schema_version: 2` (top-level) | `tests/diff/test_drift_detector.py` |

The four JSONLs are **append-only** — one record per write, fail-closed (`O_APPEND | O_CREAT | 0o600`, single `os.write`, `os.fsync`). Multi-model runs in one process append to the same files. Records are size-capped at 4000 bytes per line so concurrent multi-model appends interleave cleanly on Linux: for writes that fit in a single page (~4 KB) into a regular file opened with `O_APPEND`, the kernel atomically combines the offset adjustment and the write under the inode lock. Strict POSIX only requires `PIPE_BUF`-sized atomicity for pipes and FIFOs — not regular files — but mainstream Linux filesystems (ext4, XFS, btrfs) extend the guarantee. Operators on exotic filesystems (network mounts, FUSE drivers without inode locking) should treat concurrent multi-model appends with care.

The two `.json` sidecars are **single-document overwrite** (`O_WRONLY | O_TRUNC`). Multi-model runs in one process leave only the **last** model's sidecars on disk. Operators who need per-model sidecars use the shell-loop pattern in [`docs/cli-ops.md`](cli-ops.md) (one process per model with `--project-dir`).

The committed JSONL/JSON fixtures (`tests/fixtures/{safety,draft,prune,grade,diff}/*v1*`) are the drift-detector reference set. They are byte-frozen — adding a field to a production model without also updating the strict-mirror model OR the fixture breaks the corresponding drift-detector test loudly.

## Canonical join keys

Every audit shape carries `model_unique_id` so cross-stage joins on the model under run are direct. Within a single stage, intra-run grouping uses a stage-specific identifier; cross-stage post-hoc joins typically pair `model_unique_id` with a timestamp window.

| Stage | Per-record run identifier | Per-record artifact identifier | Notes |
|-------|---------------------------|-------------------------------|-------|
| safety | (none) | (none — payload-level) | Records correspond 1-to-1 to `build_llm_request` calls; group via `(model_unique_id, timestamp)` ordering. |
| draft | (none) | (none — payload-level) | One record per successful LLM round-trip; pair with safety via `(model_unique_id, sent_sql_hash)` when the draft sends model SQL. |
| prune | `config_hash` (16-hex; constant across all records emitted by one `prune_tests` call) | `test_anchor` (`"column.<col>"` or `"model"`) | `record_id` is a per-record uuid4 hex; `config_hash` is the run grouper. |
| grade | `run_id` (uuid4 hex; constant across all `(artifact × criterion)` calls in one `grade_artifacts` invocation) | `artifact_id` (e.g. `column.<col>.description`, `test.column.<col>.<type>`) | `(run_id, artifact_id, criterion_id)` is the natural primary key for the per-call audit. |
| grade sidecar | `run_id` (matches the per-call JSONL above) | `artifact_id` per entry | Sidecar `results[]` is a frozen view of the JSONL records produced under the same `run_id`. |
| diff sidecar | `run_id` (uuid4 hex generated at `render_diff` entry) | `artifact_id` per `entries[]` | Diff `run_id` is **not** the same as grade `run_id` — they are produced by different orchestrators. Use `model_unique_id` for cross-stage joins. |

**Cross-stage joins worth knowing:**

- **grade JSONL ↔ grade sidecar** — direct match on `(run_id, artifact_id, criterion_id)`. The sidecar's `results[]` is a frozen view of the `GradingResult` shapes produced under the same `run_id`, plus run-level aggregates (`pass_rate`, `mean_score`, `aggregate_complete`). The JSONL's `GradeEvent` rows carry the same key triple plus the per-call audit metadata (timestamps, token economics, response-text hash) that the sidecar omits — join when you need the audit detail behind a sidecar row.
- **grade sidecar ↔ diff sidecar** — match on `artifact_id` within the same `model_unique_id`. The `_artifact_id` formatter is hoisted to `signalforge._common.artifact_id` and identity-shared between layers, so the string form is byte-equal by construction.
- **prune JSONL ↔ diff sidecar** — there is no direct join key. Prune emits `test_anchor` (`"column.<col>"` / `"model"`); diff emits the richer `artifact_id` (`"test.column.<col>.<test_type>"` / `"test.model.<test_type>"`). To reconstruct the diff `artifact_id` from a `PruneEvent`, format the test scope + column + type (and `args_hash` suffix when two tests in the same scope share a `test.type`). The shared helper is `signalforge._common.artifact_id.artifact_id_for(...)` — use it directly if you're writing Python, or apply the rule from `.claude/rules/grade-layer.md` § "`_artifact_id_for` canonical dotted-path format" by hand.
- **safety JSONL ↔ draft JSONL** — no shared content hash. The safety `AuditEvent` records the LLM-request envelope shape (mode, columns sent, redactions, row count) but not SQL or any SQL hash; the draft `LLMResponseEvent` records `sent_sql_hash` over `Model.raw_code` plus the response-token economics. For cross-stage attribution within one `signalforge generate` invocation, group by `(model_unique_id, signalforge_version)` and order by `timestamp` — safety's audit fires before draft's in the same run.

**Multi-model in-process iteration** (one `signalforge generate --select <expr>` process running N models sequentially):

- Each model contributes its own records to the four append-only JSONLs. Group by `model_unique_id` and you get per-model slices.
- The two sidecars are last-writer-wins. If you need a per-model sidecar persisted, run one process per model (the shell-loop pattern in [`docs/cli-ops.md`](cli-ops.md) § "Running across many models").
- There is **no top-level `run_id` that ties safety / draft / prune / grade together across all stages of one `generate` call**. Stage identifiers (`config_hash`, `run_id`) are produced independently by each stage's orchestrator. For post-hoc cross-stage joins, pair `model_unique_id` with timestamp ordering or with the stage-specific identifier when the stage exposes one.

## Worked examples — `jq`

The examples below run against the committed drift-detector fixtures (`tests/fixtures/{safety,draft,prune,grade,diff}/*v1*`) so you can copy-paste them and see live output without a real warehouse run.

### Drop-reason histogram across the prune JSONL

```bash
$ jq -r '[.model_unique_id, .reason] | @tsv' tests/fixtures/prune/prune_event_v1.jsonl \
    | sort | uniq -c | sort -rn
      1 model.sf_demo.fct_orders	requires-future-data
      1 model.sf_demo.fct_orders	kept
      1 model.sf_demo.fct_orders	failed-on-known-clean-data
      1 model.sf_demo.fct_orders	always-passes
      1 model.sf_demo.dim_customers	kept-without-evidence
```

In a real run with hundreds of decisions, group by `.reason` (or `(.model_unique_id, .reason)`) to see which model accounts for the bulk of always-pass drops. See the drop-reason taxonomy in [`docs/prune-ops.md`](prune-ops.md#drop-reason-taxonomy).

### Graceful-degrade rate per criterion from the grade sidecar

`GradingResult.score == null` is the graceful-degrade signal (retries exhausted, parser failure, or per-call budget exhaustion — see [`docs/grade-ops.md`](grade-ops.md) § "Threshold-fail behaviour"). The sidecar's `aggregate_complete` flag is `false` whenever any result degraded.

```bash
$ jq -r '
    .results[]
    | [.criterion_id, (if .score == null then "degraded" else "scored" end)]
    | @tsv
  ' tests/fixtures/grade/grade_report_v1.json \
  | sort | uniq -c
      1 clarity	scored
      1 consistency	scored
      1 rationale	degraded
```

Across many runs (one `grade.json` per `signalforge generate` invocation; archive them per CI run), pivot the same query by `criterion_id` to see if any one criterion is failing the LLM judge more often than the others.

### Tier breakdown from the diff sidecar

The four tiers (`kept`, `kept-uncertain`, `dropped`, `flagged`) tell the reviewer at a glance what the run produced. `kept-uncertain` means "we shipped this without positive evidence" (prune couldn't evaluate it — budget exceeded, warehouse error, SQL safety rejection, or `prune.enabled: false`).

```bash
$ jq -r '.entries[] | .tier' tests/fixtures/diff/diff_report_v1.json \
    | sort | uniq -c | sort -rn
      2 kept
      1 kept-uncertain
      1 flagged
      1 dropped
```

### LLM token-cost roll-up across the grade JSONL

Cost-per-run from the grade layer: input + output + cached tokens per `(artifact × criterion)` call. Multiply by the per-model price table in `signalforge.llm.pricing` for dollars; the example below shows totals.

```bash
$ jq -r '
    [.model_unique_id, .model, .input_tokens, .output_tokens,
     .cache_creation_input_tokens, .cache_read_input_tokens]
    | @tsv
  ' tests/fixtures/grade/grade_event_v1.jsonl
model.shop.dim_customers	claude-sonnet-4-6	1820	140	0	1500
```

The draft layer's `llm_responses.jsonl` carries the same token shape (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`) — sum the two streams per `model_unique_id` for the full per-run LLM cost.

(Warehouse-bytes cost lives on the `--estimate` preview report — see [`docs/cli-ops.md`](cli-ops.md) § "Estimate-style commands" — not in the durable run audits. The `prune.jsonl` `compiled_sql_hash` lets you correlate prune decisions back to a specific compiled SQL string if you saved one, but bytes-scanned is not currently emitted into `.signalforge/`.)

### Join grade sidecar ↔ diff sidecar by `artifact_id`

```bash
$ jq -s '
    (.[0].results | map({key: .artifact_id, value: {score, passed}}) | from_entries) as $grade
    | .[1].entries
    | map(. + {grade: ($grade[.artifact_id] // null)})
    | .[]
    | [.artifact_id, .tier, (.score // "—" | tostring),
       (.grade.passed // "—" | tostring)]
    | @tsv
  ' tests/fixtures/grade/grade_report_v1.json tests/fixtures/diff/diff_report_v1.json
column.customer_id.description	kept	0.85	—
test.column.customer_id.not_null	kept	—	—
test.column.email.not_null	kept-uncertain	—	—
test.column.customer_id.unique	dropped	—	—
column.email.description	flagged	0.45	—
```

The `grade.passed` column is `—` on every row of this fixture pair because the fixtures were assembled independently — the grade-sidecar fixture covers `column.email.description` and `test.column.email.not_null`, while the diff-sidecar fixture covers `customer_id` artefacts plus the same `email.description` (mocked with a different score). In a real run, the two files produced by one `signalforge generate` invocation share `model_unique_id` and the join populates fully.

## Worked examples — pandas

The same three queries in pandas. All assume `jq`-free Python with `pyarrow` available (`pip install pandas pyarrow`); replace `read_json` with the path on your disk.

```python
import json
import pandas as pd

# Prune decisions — drop-reason histogram
prune = pd.read_json(".signalforge/prune.jsonl", lines=True)
print(prune.groupby(["model_unique_id", "reason"]).size().sort_values(ascending=False))

# Graceful-degrade rate per criterion from the grade sidecar
with open(".signalforge/grade.json") as f:
    grade_report = json.load(f)
grade_results = pd.DataFrame(grade_report["results"])
grade_results["degraded"] = grade_results["score"].isna()
print(grade_results.groupby("criterion_id")["degraded"].mean().sort_values(ascending=False))

# Tier breakdown from the diff sidecar
with open(".signalforge/diff.json") as f:
    diff_report = json.load(f)
diff_entries = pd.DataFrame(diff_report["entries"])
print(diff_entries["tier"].value_counts())

# LLM token-cost roll-up — draft + grade per model
draft = pd.read_json(".signalforge/llm_responses.jsonl", lines=True)
grade_jsonl = pd.read_json(".signalforge/grade.jsonl", lines=True)
token_cols = ["input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens"]
combined = (
    pd.concat([draft.assign(stage="draft"), grade_jsonl.assign(stage="grade")])
      .groupby(["model_unique_id", "stage"])[token_cols]
      .sum()
)
print(combined)

# Grade ↔ diff join on artifact_id
joined = diff_entries.merge(
    grade_results.rename(columns={"passed": "grade_passed"}),
    on="artifact_id",
    how="left",
    suffixes=("", "_grade"),
)
print(joined[["artifact_id", "tier", "score_grade", "grade_passed"]])
```

`pd.read_json(..., lines=True)` is the right reader for JSONL streams. Use `dtype_backend="pyarrow"` (pandas 2.x) to avoid float-from-NaN coercion if you need to distinguish `null` scores from `0.0`.

## Forward-compat policy

Every event and report model across the five stages uses Pydantic v2 `extra="ignore"` — `AuditEvent` (safety), `LLMResponseEvent` (draft), `PruneEvent` (prune), `GradeEvent` + `GradingReport` (grade), `DiffEntry` + `DiffReport` (diff). This is the project's standard forward-compat seam: a future SignalForge release can add fields to any of these without breaking a downstream JSONL reader that knows only the old schema.

**What is NOT a breaking change** (does not bump `audit_schema_version`):

- Adding a new optional field. Existing readers ignore it; new readers consume it.
- Adding a new value to a non-`Literal` string field (e.g., a new `ModelPricing` SKU). Readers tolerate unknown values.

**What IS a breaking change** (bumps `audit_schema_version`):

- Removing a field, renaming a field, or changing a field's type (string → list, int → string).
- Changing the semantics of an existing field (e.g., `score` switching from `[0.0, 1.0]` to `[0.0, 100.0]`).
- Changing a `Literal[...]` discriminator's allowed values — adding, removing, or renaming. `DropReason` (prune) and `Tier` (diff) are closed `Literal` unions; an old typed reader fails Pydantic validation on a new literal value, and a new reader misses records carrying a dropped one. The prune layer has been deliberately conservative — five `DropReason` values as of v0.2 — for exactly this reason; any expansion ships a rule-file update plus a fixture refresh plus an `audit_schema_version` bump. Issue #50 widened `Tier` from three values to four and bumped `DiffReport.audit_schema_version` from 1 to 2 alongside the change.

`audit_schema_version` is per-shape, not project-wide. The pinned values today:

- `AuditEvent.audit_schema_version: int = 3` (safety — bumped 1 → 2 in issue #54 for the `draft_skip_*` redaction reasons, 2 → 3 in issue #55 for the `policy_hash` recipe change)
- `LLMResponseEvent.audit_schema_version: int = 1` (draft)
- `PruneEvent.audit_schema_version: int = 2` (prune — bumped 1 → 2 in issue #55 when `config_hash` migrated to the `blake2b-8` recipe)
- `GradeEvent.audit_schema_version: Literal[1] = 1` (grade per-call)
- `GradingReport.grade_schema_version: Literal[1] = 1` (grade sidecar — separate field from the per-call event)
- `DiffReport.schema_version: Literal[1] = 1` (diff sidecar overall) plus `DiffReport.audit_schema_version: Literal[2] = 2` (diff entries — bumped from 1 in issue #50 alongside the `kept-uncertain` tier literal)

Consumer policy: gate on `version >= N` (forward-compatible reads of newer minor versions), not `version == N` (strict equality, breaks on every additive bump). External CI parsers that key on the four-tier `DiffEntry.tier` taxonomy specifically should gate on `DiffReport.audit_schema_version >= 2`.

Drift-detector tests (`tests/{safety,draft,prune,grade,diff}/test_drift_detector.py`) pair every `extra="ignore"` production model with a one-off `Strict<X>(extra="forbid")` mirror validated against the committed fixture. Adding a field to production without also updating the strict mirror OR the fixture breaks the test loudly — the test is the safety net for accidental schema drift.

## Privacy note

### What's redacted

- **Column names** in safety records — every column the LLM sees in `schema-only` and `aggregate-only` modes is replaced with a stable `f"col_{blake2b(name.encode(), digest_size=4).hexdigest()}"` 8-hex-char hash (DEC-010 of [`safety-layer.md`](../.claude/rules/safety-layer.md)). The `(real → hashed)` map is recorded inside the safety JSONL's `redactions[]` array; the LLM only ever sees the hash. Use the audit log itself to map back.
- **Sample row values** in `sample` mode — `RedactionRecord.redacted = true` on every column that matched a policy opt-out signal (column meta, model meta, `tags: [pii]`, `meta.contains_pii`, or pattern match against `redact_patterns`). The seven `RedactionReason` literals are documented in [`docs/safety-ops.md`](safety-ops.md) § "Per-column opt-out".
- **BigQuery session IDs in error WARNINGs** — surfaced as `blake2b-4` hashes on the happy path (`session_id_hash`); the raw `session_id` appears **only** in the cleanup-failure WARNING where the operator needs it to construct the manual `bq query --connection_property=session_id=...` recovery command. Never in audit JSONL, never on the happy path.

### What's NOT redacted

- **Model unique IDs and file paths** — `model.<pkg>.<name>` appears verbatim in every stage's audit. Treat these as project-scoped identifiers, not PII.
- **Raw column types** — `STRING`, `INT64`, `TIMESTAMP`, etc. Schema metadata, not data.
- **Compiled SQL** — `PruneEvent.compiled_sql` is the literal SQL the warehouse ran (column names are real here, not hashed — the warehouse needs them). If a column name itself is PII (e.g., `customer_ssn` literally as a column name), the prune compile-time SQL leaks it via the file. Operators uneasy about column-name leakage to the prune audit set the column's `meta.contains_pii: true` and run in `schema-only` mode; the prune layer still compiles the SQL against the real name (no choice) but never sends it to the LLM.
- **Hashes** — `signalforge_version`, `policy_hash`, `config_hash`, `rubric_hash`, `prompt_version`, `sent_sql_hash`, `response_text_hash`, `parsed_schema_hash`, `compiled_sql_hash`, `candidate_hash`, `prune_result_hash`, `grading_report_hash`. These are deterministic content addresses. They never carry user content; they let two reviewers confirm "yes, same inputs → same record."
- **LLM judge evidence quotes** — `GradingResult.evidence` carries short verbatim quotes from artifact text (column descriptions, test rationales) so the reviewer can audit the judge. The audit captures this verbatim. If your column descriptions carry PII, the grade JSONL carries that PII too.
- **`PruneEvent.sample_failures`** — only populated when `prune.capture_failure_rows: true` (default `false`); the failing rows are typed `dict[str, Any]` and contain raw warehouse data. Default-off; if you turn it on, treat `prune.jsonl` as PII-equivalent for that run.
- **Stack traces / error strings** — typed-error `cause` chains can carry warehouse error messages that quote table names, query fragments, or (rarely) row contents from query-plan tooltips. Audit at the operator-WARNING level, not the LLM-input level — see [`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md) for the WARNING-shape contract.

### Audit log rotation and retention

The fail-closed writers append unboundedly. Operators in regulated environments should configure log rotation (or copy + truncate) at a cadence that respects their data-retention policy. The four JSONLs use `O_APPEND` + `O_CREAT` with `0o600` permissions; the two sidecars are `O_TRUNC` single-document and superseded every run. See [`docs/safety-ops.md`](safety-ops.md) § "Audit log rotation" for the safety-specific rotation pattern that applies symmetrically to draft, prune, and grade JSONLs.

## References

- Pinned schemas (production models): `signalforge.safety.models.AuditEvent`, `signalforge.draft.audit.LLMResponseEvent`, `signalforge.prune.models.PruneEvent`, `signalforge.grade.models.GradeEvent` / `GradingReport`, `signalforge.diff.models.DiffEntry` / `DiffReport`.
- Drift-detector fixtures: `tests/fixtures/safety/audit_events_sample.jsonl`, `tests/fixtures/draft/llm_response_audit_sample.jsonl`, `tests/fixtures/prune/prune_event_v1.jsonl`, `tests/fixtures/grade/grade_event_v1.jsonl` + `grade_report_v1.json`, `tests/fixtures/diff/diff_report_v1.json`.
- Rule files: [`.claude/rules/safety-layer.md`](../.claude/rules/safety-layer.md), [`.claude/rules/llm-drafter.md`](../.claude/rules/llm-drafter.md), [`.claude/rules/prune-engine.md`](../.claude/rules/prune-engine.md), [`.claude/rules/grade-layer.md`](../.claude/rules/grade-layer.md), [`.claude/rules/diff-renderer.md`](../.claude/rules/diff-renderer.md).
