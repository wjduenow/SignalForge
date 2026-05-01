# Prune layer — operations guide

`signalforge.prune` is the test-pruning stage of the v0.1 pipeline:
runs every candidate test against warehouse data, drops the
always-pass and known-clean-fail ones, returns a typed `PruneResult`.
This is the load-bearing differentiator (Architectural Commitment #1
in [`CLAUDE.md`](../CLAUDE.md)) — competitors generate; SignalForge
generates *and* grades.

Companion to [`docs/safety-ops.md`](safety-ops.md),
[`docs/draft-ops.md`](draft-ops.md),
[`docs/manifest-loader-ops.md`](manifest-loader-ops.md), and
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md). Internal
design lives in
[`plans/super/6-prune-engine.md`](../plans/super/6-prune-engine.md).

This document is an **initial scaffold**. US-014 fills the body once
the implementation stories (US-001 through US-013) land. The sections
below are placeholders with TBDs marked explicitly so the gaps are
visible to the reviewer of the next PR.

## Public API

US-014 will populate this section. The expected surface (per the
Phase-1 plan, subject to amendment as stories land):

- `signalforge.prune.prune_tests(candidates, model, manifest, adapter, *, config) -> PruneResult`
- `PruneResult`, `PruneDecision`, `KeptTest`, `DroppedTest`
- `PruneConfig`, `load_prune_config`
- `DropReason`, `Scope` literals
- The `PruneError` hierarchy

(TBD — final shapes confirmed during US-014.)

## Configuration: `signalforge.yml` `prune:` block

Top-level namespace is `prune:` (DEC-025 of the safety layer reserves
sibling keys for future stages; the prune layer claims its own).

US-014 will populate this section with the field-by-field reference.
Expected fields (per the Phase-1 plan):

- `scope: "sample" | "full"` (default `sample`)
- `sample_size: int` (default `100_000`)
- `test_timeout_seconds: int` (default `30`)
- `total_budget_seconds: int` (default `600`)
- `capture_failure_rows: int` (default `3`)
- `trusted_models: list[str]` — drives the `failed-on-known-clean-data`
  drop reason (deferred for v0.1 unless the implementing story
  re-opens the question).

(TBD — final fields and defaults confirmed during US-014.)

## Drop-reason taxonomy

Every kept and dropped test ships with a structured `PruneDecision`
carrying a one-line `why`. The reasons are a closed set so the diff
renderer (#8) can branch on the literal value rather than sniffing
prose.

| Reason | Decision | Why |
|--------|----------|-----|
| `always-passes` | dropped | Zero failing rows on the sampled or full set; no signal worth shipping. |
| `requires-future-data` | dropped | A `relationships` test references a manifest-absent parent model. No warehouse call issued. |
| `failed-on-known-clean-data` | dropped | Test failed AND the model is in `prune.trusted_models`; the test is presumed buggy. |
| `kept` | kept | Test failed on an untrusted model; reviewer should evaluate. |
| `kept-without-evidence` | kept | Could not evaluate (warehouse error or total budget exceeded); ship conservatively. |

Conservative bias: when in doubt, keep. Architectural Commitment #1
("signal over volume") penalises always-pass tests (no signal,
consumes reviewer attention) but does not penalise kept tests with
ambiguous evidence — those land in front of a human reviewer who can
make the final call.

## Cost model (US-003 verification)

The deterministic-sample predicate

```sql
WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), bucket) < 1
```

serialises the entire row into the predicate. BigQuery cannot
column-prune through a function argument, so sample-mode reads **all
columns** of the table, not just the column under test. The
Phase-1 estimate that pruning 30 candidate tests against a 100k-row
sample would cost approximately 24 MB / approximately one tenth of one
US cent assumed only the column under test would be read; the worst
case is 50–500x that figure.

**Verified figure (US-003):** TBD — populate after the diagnostic
probe (`tests/warehouse/test_sample_cost_probe.py`) runs against
`bigquery-public-data.iowa_liquor_sales.sales`. The probe records
`total_bytes_billed` for a 100k-row deterministic sample. The
threshold for action:

- If `bytes_billed < 500 MB`: Q4=A (one-query-per-test) is viable for
  v0.1 unchanged.
- If `bytes_billed >= 500 MB`: v0.2 should adopt Q4=C
  (temp-table-materialised sample) so the per-test cost amortises
  across the candidate set rather than re-paying the full-row scan
  for every test.

(US-014 replaces TBD with the live figure once US-003 has been run by
a maintainer with BigQuery credentials.)

## Audit JSONL

US-014 will populate this section if the implementing story chooses
to emit a fail-closed `PruneEvent` JSONL (mirroring the safety
audit's DEC-011 contract and the LLM response audit's DEC-006 / DEC-008
contracts). The default expectation is that prune **does** emit one
JSONL line per `PruneDecision` so the explainable-diffs commitment
(Architectural Commitment #5) survives the v0.2 transition to
multi-warehouse adapters where the underlying SQL is no longer
homogeneous.

(TBD — confirmed during US-014.)

## Running real-warehouse tests

The prune layer's integration tests share the warehouse adapter's
gating discipline. Default CI excludes them via the
`-m 'not bigquery'` filter; opt-in requires both the marker and an
`SF_RUN_BQ=1` env var.

```bash
gcloud auth application-default login
SF_RUN_BQ=1 pytest -m bigquery
```

The diagnostic cost probe (US-003) lives at
`tests/warehouse/test_sample_cost_probe.py` and runs under the same
gate. It is a documentation-grade probe — a soft WARNING fires at
500 MB; the test fails only above the 5 GB sanity ceiling.

(US-014 expands this section with the prune-specific integration
tests once US-013 lands.)

## Debugging

US-014 will populate this section with logger names, levels, and the
ANSI-safe lazy-format JSON discipline that the safety / drafter layers
already follow (DEC-022 of safety; DEC-011 of llm-drafter).

(TBD — confirmed during US-014.)

## Typed-error reference

US-014 will populate the table once US-005 lands the `PruneError`
hierarchy. Mirrors the safety / draft / warehouse error tables.

(TBD — confirmed during US-014.)

## v0.2 deferrals

The prune layer is intentionally narrow in v0.1. The following
concerns are explicitly deferred:

- **Per-decision `bytes_billed` recording (DEC-027 of issue #6).**
  The adapter does not surface job stats in v0.1; the diagnostic
  probe (US-003) reads them via `INFORMATION_SCHEMA.JOBS_BY_USER`
  out-of-band rather than through the adapter API. v0.2 extends the
  adapter's seam to return job stats so the `PruneDecision` can
  carry the figure natively.
- **Per-test `timeout_ms` threading.** US-002 plumbed
  `timeout_ms` into `make_query_job_config` (DEC-013 of issue #6,
  AR-B2); surfacing it through `WarehouseAdapter.run_test_sql`'s
  public signature is a v0.2 task. v0.1 prune sets the timeout on
  the job config but cannot enforce it per-call without the
  surfaced parameter.
- **Test batching — Q4=B / Q4=C optimisations.** The Phase-1 plan
  catalogues two cost optimisations (per-column `COUNTIF` batching;
  temp-table-materialised sample) that v0.1 does not adopt. US-003
  produces the data needed to evaluate the temp-table option in
  v0.2.
- **Multi-warehouse adapters.** Snowflake, Postgres, Databricks,
  Redshift adapters slot in behind `WarehouseAdapter` without prune
  changes once their adapters land. The prune compiler already uses
  `Dialect.quote_char` and `Dialect.identifier_case` to stay
  warehouse-agnostic; no SQL paths are BigQuery-specific.
- **Historical always-pass evidence.** Running candidate tests
  against multiple `run_results.json` snapshots to assert "never
  failed in last N runs." The Phase-1 plan considers this for the
  `failed-on-known-clean-data` evidence channel and defers to v0.2.
- **dbt-utils test types.** `dbt_utils.unique_combination_of_columns`,
  `dbt_utils.accepted_range`, `dbt_utils.expression_is_true`, etc.
  The drafter's `CandidateTest` union has exactly four variants;
  prune compiles exactly four. v0.2 territory.
- **`where:` test modifier and `severity: warn` / `mostly:`.**
  dbt-core supports a `where:` predicate on every test plus a
  `severity` and `mostly` knob; v0.1 prune does not consume any of
  these.
- **`prune_decision_id`-keyed checkpoint / resumption.** Long-running
  prune runs that resume from disk after a crash. v0.2.
- **Confidence intervals on `always-passes`.** Surfacing
  "less than or equal to 3/N upper-bound failure rate at 95 percent
  confidence" on the decision record. Nice to have; v0.2.
- **LLM-generated rationale on `kept` decisions.** The grader (#7)
  produces rubric-scored rationale; prune writes only the structured
  drop reason plus failure count plus scope.

(US-014 expands the v0.2 list as additional deferrals surface during
implementation.)
