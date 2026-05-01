# Prune layer — operations guide

Operational reference for users of `signalforge.prune`. Companion to
[`docs/safety-ops.md`](safety-ops.md),
[`docs/draft-ops.md`](draft-ops.md),
[`docs/manifest-loader-ops.md`](manifest-loader-ops.md), and
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md), and to the
design record in [`plans/super/6-prune-engine.md`](../plans/super/6-prune-engine.md).

The prune layer sits between the LLM-drafting layer (#5) and the diff
renderer (#8). Every candidate test produced by the drafter goes through
one entry point — `signalforge.prune.prune_tests` — which compiles the
test to failing-rows SQL, runs it against the warehouse, classifies the
verdict, and writes a fail-closed JSONL audit record per decision.

This is the load-bearing differentiator (Architectural Commitment #1 in
[`CLAUDE.md`](../CLAUDE.md)) — competitors generate; SignalForge
generates *and* grades.

## Default posture

Sample-scope is the default. Architectural Commitment #1 (signal over
volume) penalises always-pass tests because they consume reviewer
attention without catching anything; sampling 100k rows is enough signal
to detect always-pass while keeping query bytes under control.

The layer is fail-closed on the audit-write boundary: any I/O error from
`PruneEvent` persistence aborts the run as `PruneAuditWriteError`
(DEC-016, mirrors safety's DEC-011 and draft's DEC-006). And the layer
is conservative on the verdict boundary: tests we cannot evaluate
(warehouse error, total-budget exhausted, ambiguous evidence) are
**kept**, not dropped — `kept-without-evidence` lands the test in front
of a human reviewer rather than silently losing potential signal.

User-facing tagline: **always-pass tests are dropped; everything we
cannot confidently drop is kept.**

## Public API

Import from `signalforge.prune`. The 14 names exported by `__all__`:

### Orchestrator

- **`prune_tests(model, adapter, candidates, manifest, *, config=None, project_dir=None, audit_path=None) -> PruneResult`** — End-to-end orchestrator. Compiles every `CandidateTest`, runs each through the warehouse adapter, writes one JSONL audit record per decision, returns the aggregate `PruneResult`. Mirrors `signalforge.draft.draft_schema` so the CLI / wrapper layers see one consistent end-to-end shape across pipeline stages. `project_dir` defaults to `Path.cwd()`. `audit_path` defaults to `<project_dir>/.signalforge/prune.jsonl`.

### Result shapes

- **`PruneResult`** — Aggregate verdict for one model. Frozen Pydantic model with fields `prune_schema_version: Literal[1]`, `model_unique_id: str`, `decisions: tuple[PruneDecision, ...]`, `elapsed_ms: int`, `signalforge_version: str`. Computed properties: `kept_decisions`, `dropped_decisions`, `kept_count`, `dropped_count`, `total_tests` — all derived from `decisions` (DEC-003) so a `PruneResult` reconstructed from a JSONL log carries identical views to a freshly produced one.

- **`PruneDecision`** — One verdict per candidate test. Carries `test_anchor: str` (`"column.<name>"` or `"model"`), `test: CandidateTest` (the typed discriminated union from the drafter, NOT a loose dict — DEC-004; the grader and diff renderer reuse the drafter's per-variant display logic), `decision: Literal["kept", "dropped"]`, `reason: DropReason`, `failures: int`, `sampled_rows: int | None`, `scope: Scope`, `elapsed_ms: int`, `compiled_sql_hash: str` (16 hex chars; blake2b-8 per DEC-005), `compiled_sql: str`, `why: str`, `sample_failures: tuple[dict[str, Any], ...] | None`.

### Configuration

- **`PruneConfig`** — User-facing knobs. Frozen Pydantic model with `extra="forbid"` (config-shaped per DEC-015 — typos fail loud). Field reference: see [Configuration](#configuration-signalforgeyml-prune-block) below.

- **`load_prune_config(project_dir, path=None) -> PruneConfig`** — Loads the `prune:` block from `signalforge.yml`. Resolves to `<project_dir>/signalforge.yml` when `path` is `None`. Returns defaults when the file is missing, empty, or the `prune:` key is absent. Raises `PruneConfigError` on parse / schema failures. Mirrors `load_safety_config` / `load_draft_config` so the CLI sees one calling convention across stages.

### Discriminator literals

- **`DropReason`** — `Literal["always-passes", "requires-future-data", "failed-on-known-clean-data", "kept", "kept-without-evidence"]`. Closed set so the diff renderer (#8) can branch on the literal value rather than sniffing prose.

- **`Scope`** — `Literal["sample", "full"]`. When `scope == "full"`, `PruneDecision.sampled_rows` is `None` (every row inspected).

### Audit

- **`PruneEvent`** — One JSONL audit record per `PruneDecision`. Constructed ONLY by `signalforge.prune.audit._build_prune_event` (DEC-018; AST-gated by `tests/test_audit_completeness.py`). `extra="ignore"` for forward-compat read-back. See [Audit JSONL schema](#audit-jsonl-schema) for the field set.

### Errors

`from signalforge.prune import errors`. Every exception subclasses
`PruneError` and carries a class-level `default_remediation` rendered on
a `↳ Remediation:` line by `__str__`.

- **`PruneError`** — Base class. Never raised directly.
- **`PruneConfigError`** — `signalforge.yml` `prune:` block failed parse or schema validation.
- **`PruneTrustedModelNotFoundError`** — `prune.trusted_models` references a `unique_id` not in the manifest. Subclass of `PruneConfigError`. Raised at orchestrator entry, BEFORE any warehouse call (DEC-008).
- **`PruneTimeoutError`** — Internal control-flow signal for budget-exhausted dispatch. Callers of `prune_tests` do NOT see this — the orchestrator routes the in-flight test plus every remaining un-started test to `kept-without-evidence` (DEC-011).
- **`PruneAuditWriteError`** — Fail-closed audit-write failure (`OSError` / `PermissionError` / encoding / `fsync`). Aborts the run; original cause exposed via `.cause` and `__cause__` (DEC-016).
- **`PruneAuditRecordTooLargeError`** — Serialised JSONL line exceeded the POSIX-atomic-append cap (4000 bytes). Raised BEFORE any file is opened so an oversize record leaves no on-disk artefact.

DEC-006 deliberately omits a `PruneCompilerError` class. Compilation
always succeeds; failures like `relationships(to: unknown)` emit a
structured `requires-future-data` drop reason rather than an exception.

## Configuration: `signalforge.yml` `prune:` block

Top-level namespace is `prune:` (DEC-020; sibling keys `safety:`,
`llm:`, future `grade:` are reserved for other stages and silently
ignored by the prune loader).

```yaml
safety:
  # ... (loaded by signalforge.safety)
llm:
  # ... (loaded by signalforge.draft)
prune:
  scope: sample            # "sample" | "full"
  sample_size: 100000      # rows
  test_timeout_seconds: 30
  total_budget_seconds: 600
  capture_failure_rows: 3
  trusted_models:
    - model.shop.dim_customers
  partition_filter:
    column: event_dt
    op: ">="
    value: "2026-01-01"
```

Field-by-field:

- **`scope`** — `"sample"` | `"full"`. Default `"sample"`. Whether candidate tests run against a deterministic warehouse sample or a full table scan. Switch to `"full"` only when the model is small enough that `sample_size` would scan most of it anyway.
- **`sample_size`** — Integer row count for sample scope. Default `100_000`. Passed to `WarehouseAdapter.sample_rows`. Increase when the always-pass false-positive rate on small samples hides real signal; decrease to cap query bytes on very wide tables (column-pruning does NOT apply through `FARM_FINGERPRINT(TO_JSON_STRING(t))` — see [Cost model](#cost-model-us-003-verification)).
- **`test_timeout_seconds`** — Per-test wall-clock budget. Default `30`. **Reserved for v0.2** — the adapter's `_default_job_config(timeout_ms=...)` plumbing exists (US-002 of issue #3) but `WarehouseAdapter.run_test_sql` does not yet accept a per-call timeout kwarg, so v0.1 does not enforce this knob. Per-test wall-clock control in v0.1 comes implicitly from `total_budget_seconds` plus the `WarehouseError` catch path: a test that exceeds the warehouse's own budget surfaces as a typed error → `kept-without-evidence`. See [v0.2 deferrals](#v02-deferrals).
- **`total_budget_seconds`** — Whole-run wall-clock budget. Default `600`. Once exceeded, every remaining test drains to `kept-without-evidence` with `why="Total prune budget (Ns) exceeded before evaluation."` (DEC-011). Conservative bias — no test is silently dropped because the run ran long.
- **`capture_failure_rows`** — Number of failing rows recorded on the `PruneDecision.sample_failures` field per failed test. Default `3`. Set to `0` to omit row-level evidence entirely (the audit record stays compact for very wide tables).
- **`trusted_models`** — List of manifest `unique_id`s whose data is treated as known-clean. A failure on a trusted model surfaces as `failed-on-known-clean-data` (drop, presumed buggy test) rather than `kept`. Opt-in only (Q1=B). Validated against the manifest at `prune_tests` entry — typos raise `PruneTrustedModelNotFoundError` BEFORE any warehouse call (DEC-008).
- **`partition_filter`** — Optional `PartitionFilter` ADT (`{column, op, value}`) scoping every sample query. Required by the warehouse adapter for tables with `num_rows >= 100M`; otherwise optional. Pydantic recursively validates the YAML mapping into the typed shape.

Unknown keys under `prune:` raise `PruneConfigError` (Pydantic
`extra="forbid"`, DEC-015). Typos like `scop:` or
`total_budget_secnds:` fail loud at load time rather than silently
no-op'ing.

## Drop-reason taxonomy

Every kept and dropped test ships with a structured `PruneDecision`
carrying a one-line `why`. The reasons are a closed `DropReason` literal
so the diff renderer (#8) can branch on the value.

| Reason | Decision | Why |
|--------|----------|-----|
| `always-passes` | dropped | Zero failing rows on the sampled or full set; no signal worth shipping. The load-bearing case for Architectural Commitment #1. |
| `requires-future-data` | dropped | A `relationships` test references a `to:` parent model not in the loaded manifest. No warehouse call issued — the compiler returns a `_RequiresFutureData` sentinel and the orchestrator routes it directly to this reason (DEC-026). |
| `failed-on-known-clean-data` | dropped | Test failed AND `model.unique_id` is in `prune.trusted_models`; the test is presumed buggy. Symmetric noise-direction split with `always-passes` — both directions of noise need pruning per `CLAUDE.md`. |
| `kept` | kept | Test failed on an untrusted model with non-zero failures. Reviewer should evaluate. |
| `kept-without-evidence` | kept | Could not evaluate — warehouse error (typed `WarehouseError` subclass) or total budget exceeded (DEC-011). Ship conservatively; reviewer decides. |

Conservative bias: when in doubt, keep. Architectural Commitment #1
penalises always-pass tests (no signal, consumes reviewer attention) but
does not penalise kept tests with ambiguous evidence — those land in
front of a human reviewer who can make the final call.

## Cost model (US-003 verification)

The deterministic-sample predicate

```sql
WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), bucket) < 1
```

serialises the entire row into the predicate. BigQuery cannot
column-prune through a function argument, so sample-mode reads **all
columns** of the table, not just the column under test. The Phase-1
estimate that pruning 30 candidate tests against a 100k-row sample would
cost approximately 24 MB / approximately one tenth of one US cent
assumed only the column under test would be read; the worst case is
50–500x that figure.

**Verified figure (US-003): pending in-environment run.** The diagnostic
probe at `tests/warehouse/test_sample_cost_probe.py` runs against
`bigquery-public-data.iowa_liquor_sales.sales` and records
`total_bytes_billed` for a 100k-row deterministic sample. To run it:

```bash
gcloud auth application-default login
SF_RUN_BQ=1 pytest -m bigquery tests/warehouse/test_sample_cost_probe.py -s
```

Probe thresholds (constants in the test):

- `_BYTES_WARN_AT = 500_000_000` (500 MB) — soft WARNING fires above this.
- `_BYTES_CEILING = 5_000_000_000` (5 GB) — assertion fires above this; the test fails.

Outcome interpretation:

- **`bytes_billed < 500 MB`** → Q4=A (one-query-per-test) is viable for v0.1 unchanged. The Phase-1 plan's cost target holds.
- **`bytes_billed >= 500 MB`** → adopt Q4=C (temp-table-materialised sample) in v0.2 so the per-test cost amortises across the candidate set rather than re-paying the full-row scan for every test.

Update this section once a maintainer with BigQuery credentials runs the
probe.

## Audit JSONL schema

Every `PruneDecision` produces exactly one JSONL record at
`audit_path` (default `<project>/.signalforge/prune.jsonl`). One record
per line; atomic concurrent appends via `O_APPEND | O_CREAT | 0o600` and
a single `os.write` (DEC-016). The third instance of the convention
across the codebase — mirrors `signalforge.safety.audit` (DEC-011 of
safety) and `signalforge.draft.audit` (DEC-006/008/013 of llm-drafter).

`PruneEvent` fields (~19 total):

| Field                  | Type                                | Meaning                                                                                          |
| ---------------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------ |
| `audit_schema_version` | integer (`Literal[1]`)              | Audit shape version. Currently `1`. Bump only on shape change; `extra="ignore"` handles additions. |
| `signalforge_version`  | PEP-440 version string              | Package version that produced the record.                                                        |
| `record_id`            | 32-hex-char string                  | Fresh `uuid4().hex` per record; gives reviewers a stable handle for a single decision.           |
| `timestamp`            | ISO-8601 UTC, microsecond, `Z`      | When the decision was finalised.                                                                 |
| `config_hash`          | 16 hex chars                        | First 16 hex chars of `SHA-256(canonical_config_json)`. Mirrors safety's `policy_hash` (DEC-005). |
| `model_unique_id`      | string                              | dbt `unique_id` of the pruned model.                                                             |
| `test`                 | discriminated-union object          | The original `CandidateTest` from the drafter (typed; not a loose dict — DEC-004).               |
| `test_anchor`          | string                              | `"column.<name>"` for column-scoped tests; literal `"model"` for model-level tests.              |
| `decision`             | `"kept"` \| `"dropped"`             | Top-level verdict.                                                                               |
| `reason`               | `DropReason` literal                | One of the five reasons in [Drop-reason taxonomy](#drop-reason-taxonomy).                        |
| `failures`             | integer                             | Failing-row count from the warehouse. `0` for `always-passes` and `requires-future-data`.        |
| `sampled_rows`         | integer or `null`                   | Sample size the test ran against. `null` for full-scope or no-warehouse-call decisions.          |
| `scope`                | `"sample"` \| `"full"`              | Mirrors `PruneConfig.scope`.                                                                     |
| `elapsed_ms`           | integer                             | Per-test wall-clock cost. `0` for budget-exhausted (test never ran).                             |
| `compiled_sql_hash`    | 16 hex chars                        | `blake2b(sql.encode(), digest_size=8).hexdigest()`. Stable empty-string hash for no-SQL outcomes. |
| `compiled_sql`         | string                              | The exact SELECT issued to the warehouse. Empty for `requires-future-data` and budget-exhausted. |
| `why`                  | string                              | One-line human-readable rationale. Architectural Commitment #5.                                  |
| `sample_failures`      | array of object or `null`           | Up to `capture_failure_rows` failing rows. `null` when capture is disabled or no failures.       |

**Fail-closed semantics.** `OSError` / `PermissionError` / encoding
failures from `os.write` / `os.fsync` propagate raw; the orchestrator
wraps them as `PruneAuditWriteError` and aborts the run.
`PruneAuditRecordTooLargeError` (size cap, raised before any file open)
also aborts the run. Don't wrap audit-write calls in defensive
try/except — propagation IS the defence (mirrors
`safety-layer.md` DEC-011).

**Schema-drift gate.** `tests/fixtures/prune/prune_event_v1.jsonl` is
the canonical schema fixture; `tests/prune/test_drift_detector.py`
pairs the production model (`extra="ignore"`) with a one-off
`extra="forbid"` strict model and validates against the fixture.
Adding a field to `PruneEvent` without updating the strict model OR
the fixture breaks the test loudly. Don't bypass.

## Audit log sensitivity

`prune.jsonl` contains the model's compiled SQL and (when
`capture_failure_rows > 0`) up to N rows of failing data per test.
Treat the file at-rest the same way you treat the safety audit:

- **Gitignore `.signalforge/`** (already configured in this repo's `.gitignore`).
- **Restrict at-rest permissions.** The writer creates the file at `0o600` on first call; the parent directory is created via `mkdir(parents=True, exist_ok=True)` (Python's `mkdir` does not tighten an existing directory's permissions, so verify the existing `.signalforge/` mode is `0o700` on shared hosts).
- **Don't ship as a build artifact.** Strip from container images and CI uploads.
- **Set `capture_failure_rows: 0`** for PII-laden models if the safety layer's redaction policy isn't enough — the prune layer captures real warehouse rows for failures, which can include PII not flagged for the LLM redactor.

## Running real-warehouse tests

The prune layer's integration tests share the warehouse adapter's
gating discipline. Default CI excludes them via the `-m 'not bigquery'`
filter; opt-in requires both the marker and an `SF_RUN_BQ=1` env var.

```bash
gcloud auth application-default login
SF_RUN_BQ=1 pytest -m bigquery
```

The prune-layer integration test (`tests/prune/test_integration_bigquery.py`)
requires ambient gcloud auth (matches the warehouse adapter's
`tests/warehouse/test_bigquery_integration.py`).

The diagnostic cost probe (US-003) at
`tests/warehouse/test_sample_cost_probe.py` runs under the same gate.
It is a documentation-grade probe — a soft WARNING fires at 500 MB; the
test fails only above the 5 GB sanity ceiling. See
[Cost model](#cost-model-us-003-verification).

## Debugging

Logger name: `signalforge.prune.engine` (and sibling modules under
`signalforge.prune`).

```python
import logging
logging.getLogger("signalforge.prune").setLevel(logging.DEBUG)
```

Levels:

- **WARNING** — One line per `kept-without-evidence` decision routed by a typed `WarehouseError`. Lazy-format JSON per DEC-017 (`signalforge_version`, `model_unique_id`, `test_anchor`, `error_class`). Never f-string-interpolate user-controlled strings into a logger call — a column name or model id containing ANSI escapes (`\x1b[31m...`) would inject into log viewers.
- **INFO** / **DEBUG** — reserved for future budget-loop / batching observability; v0.1 emits no INFO/DEBUG from the engine.

The prune layer never logs full row data. The audit JSONL is the single
durable record of decision-level detail; logger output is a hint that
the decision happened, not what was in it.

**Reading a fail-closed `PruneAuditWriteError`.** The cause is exposed
as `.cause` and on `__cause__`. Common causes:

- Parent directory not writable (no `+w` for the user, or `.signalforge/` is a symlink to a read-only mount).
- Disk full (`ENOSPC`).
- Oversize record (raises `PruneAuditRecordTooLargeError` instead — reduce `capture_failure_rows` or trim `compiled_sql` size by simplifying the candidate test; the cap is 4000 bytes for POSIX-atomic concurrent appends).

## v0.2 deferrals

The prune layer is intentionally narrow in v0.1. The following
concerns are explicitly deferred:

- **Per-decision `bytes_billed` recording (DEC-027).** The adapter does not surface job stats in v0.1; the diagnostic probe (US-003) reads them via `INFORMATION_SCHEMA.JOBS_BY_USER` out-of-band rather than through the adapter API. v0.2 extends the adapter's seam to return job stats so the `PruneDecision` can carry the figure natively.
- **Per-test `timeout_ms` threading.** `PruneConfig.test_timeout_seconds` is documented but not yet threaded through `WarehouseAdapter.run_test_sql` per call. The plumbing exists in `make_query_job_config` (DEC-013, AR-B2 of issue #6); surfacing it through the public adapter signature is a v0.2 task.
- **Test batching — Q4=B / Q4=C optimisations.** The Phase-1 plan catalogues two cost optimisations (per-column `COUNTIF` batching; temp-table-materialised sample). v0.1 does not adopt either. US-003 produces the data needed to evaluate the temp-table option in v0.2.
- **Multi-warehouse adapters.** Snowflake, Postgres, Databricks, Redshift adapters slot in behind `WarehouseAdapter` without prune changes once their adapters land. The prune compiler already dispatches on `Dialect.quote_char` (DEC-025), not on dialect `name`; no SQL paths are BigQuery-specific.
- **Confidence intervals on `always-passes`.** Surfacing "less than or equal to 3/N upper-bound failure rate at 95 percent confidence" (rule of three) on the decision record so reviewers can calibrate the always-pass verdict. Also covers great-expectations-style `mostly:` thresholds.
- **Historical always-pass evidence.** Running candidate tests against multiple `run_results.json` snapshots to assert "never failed in last N runs." The Phase-1 plan considers this for the `failed-on-known-clean-data` evidence channel and defers to v0.2.
- **dbt-utils test types.** `dbt_utils.unique_combination_of_columns`, `dbt_utils.accepted_range`, `dbt_utils.expression_is_true`, etc. The drafter's `CandidateTest` union has exactly four variants in v0.1; the prune compiler compiles exactly four. v0.2 territory.
- **`where:` test modifier and `severity: warn` / `mostly:`.** dbt-core supports a `where:` predicate on every test plus `severity` and `mostly` knobs; v0.1 prune does not consume any of these.
- **`prune_decision_id`-keyed checkpoint / resumption.** Long-running prune runs that resume from disk after a crash. v0.2.
- **LLM-generated rationale on `kept` decisions.** The grader (#7) produces rubric-scored rationale; prune writes only the structured drop reason plus failure count plus scope.

## CLI integration note

Tracked in [issue #9](https://github.com/wjduenow/SignalForge/issues/9).
The `signalforge generate` CLI will load the prune config via
`load_prune_config(...)` and invoke `prune_tests(...)` after the LLM
draft completes; the diff renderer (#8) consumes the returned
`PruneResult` to emit kept/dropped artifacts with their per-decision
`why` lines (Architectural Commitment #5 — explainable diffs).
