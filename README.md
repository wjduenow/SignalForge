[![codecov](https://codecov.io/gh/wjduenow/SignalForge/branch/dev/graph/badge.svg)](https://codecov.io/gh/wjduenow/SignalForge)

# SignalForge

> LLM-drafted dbt schema.yml, tests, and docs — pruned against real warehouse data so only signal-bearing tests ship.

**Status:** v0.1 alpha. Eleven issues shipped — single-model draft + warehouse prune, BigQuery adapter, `signalforge` CLI, `signalforge init-demo` for first-run UX. Designing in the open on the `dev` branch.

## Why this exists

Authoring `schema.yml`, tests, and documentation is the most-cited drudgery in the dbt ecosystem. AI tools that generate them already exist — dbt Copilot, dbt-codegen, Paradime DinoAI, Altimate datapilot — but their output is consistently described the same way: *noise*. Hundreds of `not_null` and `unique` tests that always pass. Generic docstrings that paraphrase the column name. Schemas that drift from the SELECT.

SignalForge generates the same artifacts, then asks a different question: **does this test produce signal?** Every candidate test is run against your real warehouse data. Tests that always pass are dropped. Docs are graded against a project-specific rubric. Only signal-bearing artifacts are written to disk.

## What it does

- **Drafts `schema.yml`** from your model SQL using an LLM with project-aware context (manifest, sibling models, your team's terminology).
- **Generates tests** — `not_null`, `unique`, `accepted_values`, `relationships`, plus dbt-expectations-style data tests where appropriate.
- **Prunes the noise.** Each candidate test runs against warehouse samples; tests that pass on every row of historical data add no signal and are dropped before they reach your repo.
- **Generates documentation** — column-level descriptions and model-level overviews — graded by an LLM-as-judge against a configurable rubric.
- **Reports what was kept and what was dropped**, with a one-line "why" per artifact. No black-box generation.

## How it works

```
┌──────────────┐    ┌─────────────┐    ┌──────────────┐    ┌──────────────┐
│ model.sql +  │ -> │ LLM drafts  │ -> │ Run tests    │ -> │ Quality-     │
│ manifest +   │    │ candidate   │    │ against the  │    │ graded YAML  │
│ project ctx  │    │ artifacts   │    │ warehouse    │    │ + diff       │
└──────────────┘    └─────────────┘    └──────────────┘    └──────────────┘
                                              │
                                              v
                                       Drop always-pass tests;
                                       drop tests that fail on
                                       known-clean data.
```

The grading layer reuses [clauditor](https://github.com/wjduenow/clauditor)'s LLM-as-judge methodology, applied to a new artifact class.

> **Status (v0.1):** Not yet on PyPI. Today the package installs from a
> clone with `pip install -e ".[dev]"` (quote the extras — `[dev]` is a
> glob in zsh).

## Quick start

The wheel ships a minimal dbt demo project (Austin bikeshare staging
model against the public
`bigquery-public-data.austin_bikeshare.bikeshare_trips` dataset),
copied out of the install via `signalforge init-demo`, so you can
run `signalforge` end-to-end against a real warehouse with no
infrastructure beyond a Google Cloud billing project and an
Anthropic API key. A run scans ~200–500 MB of BigQuery (well under
$0.01 at on-demand pricing) plus ~$0.13 of Anthropic spend (one
draft call + ~84 grade calls on Sonnet 4.6); end-to-end wall-clock
is roughly 5–6 minutes.

### 1. Install

SignalForge requires **Python 3.11+**. It is not yet published on PyPI — install from a clone:

```bash
git clone https://github.com/wjduenow/SignalForge.git
cd SignalForge
pip install -e ".[dev]"   # quote the extras — [dev] is a glob in zsh
```

Once v0.1 ships to PyPI, `pip install signalforge-dbt` will replace the
editable-install step. (The PyPI name has a `-dbt` suffix because
`signalforge` was already taken by an unrelated DSP package; the import
name and CLI command remain `signalforge`.)

### 2. Authenticate to BigQuery and Anthropic

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=<your-billing-project>   # any GCP project you have query access to
export ANTHROPIC_API_KEY=sk-ant-...
```

Use a fresh shell session (or `unset ANTHROPIC_API_KEY` after the
run) so the key doesn't persist in your bash history.

### 3. Minimum `signalforge.yml`

The fixture ships a working config; a minimum that exercises the full
pipeline is:

```yaml
# signalforge.yml — alongside dbt_project.yml
llm:
  model: claude-sonnet-4-6
safety:
  mode: aggregate-only   # schema-only is the default; aggregate-only sends column profiles, never row data
prune:
  sample_strategy: materialised   # v0.2 default; one temp-table CTAS feeds every per-test query
grade:
  min_pass_rate: 0.95
  min_mean_score: 0.95
  fail_on_below_threshold: false   # report-only; flip to true to exit 2 on flagged artifacts
```

Full reference: [docs/safety-ops.md](docs/safety-ops.md),
[docs/prune-ops.md](docs/prune-ops.md),
[docs/grade-ops.md](docs/grade-ops.md).

### 4. Prepare the fixture

Copy the bundled demo project to a writable directory and run
`signalforge` against it:

```bash
signalforge init-demo /tmp/sf-austin
```

### 5. Pre-flight check (`signalforge lint`)

Before paying for an LLM call, run the pre-flight validator. It loads
`signalforge.yml` (every per-stage block) and the dbt manifest — no
warehouse calls, no Anthropic calls, no network — and reports every
failure in one shot. Sub-second; catches typos like
`safety: { mdoel: ... }` that the `extra="forbid"` config models would
otherwise surface only after a billable `generate` run, plus manifest
schema-version mismatches (e.g. dbt 1.13 → v13, outside the supported
v9–v12 range) that would otherwise surface mid-pipeline:

```bash
signalforge lint --project-dir /tmp/sf-austin
```

On success, stdout is silent (git-style) and the exit code is `0`.
Failures are listed on stderr with the offending block(s) named —
single-failure runs use the `ERROR: <message>` shape; multi-failure
runs emit a header + one bullet per block. See
[`docs/cli-ops.md`](docs/cli-ops.md) § `signalforge lint` for the full
contract.

### 6. First run

```bash
signalforge generate models/staging/stg_bikeshare_trips.sql --project-dir /tmp/sf-austin
```

The bundled `profiles.yml` reads `GOOGLE_CLOUD_PROJECT` from your
environment, so no profile editing is required. `signalforge
init-demo` prints a next-steps message naming the env vars and the
exact commands to run; pass `--force` to atomically replace a
non-empty destination (refuses `/`, `$HOME`, and the current
working directory as a blast-radius guard).

Want to preview cost first? `signalforge generate --estimate <model>`
prints the projected USD + warehouse bytes without making any billable
Anthropic or warehouse call (one `count_tokens` round-trip per prompt
plus a single BigQuery `dryRun`). See
[`docs/cli-ops.md`](docs/cli-ops.md) § `--estimate` for the full
contract.

### 7. Expected output

The diff lists drafted column descriptions and signal-bearing tests
alongside dropped tests with a one-line "why". The kept/dropped/flagged
table looks like this (truncated):

```text
diff: model.austin.stg_bikeshare_trips  kept=8  dropped=2  flagged=1

TIER      ARTIFACT                      TEST            REASON                  SCORE    WHY
kept      column.trip_id.description                                            0.97     Description added; passed all grading criteria.
kept      test.column.trip_id.not_null  not_null                                —        Test returned non-zero failing rows on the warehouse sample.
dropped   test.column.region.not_null   not_null        always-passes           —        Test returned zero failing rows on the representative sample.
flagged   column.bike_id.description                                            0.45     Grading score 0.45 below threshold 0.95.
...
```

At least one `dropped` row with `always-passes` is mathematically
guaranteed — the fixture's staging SQL aliases a literal `'austin' AS
region` column, so any LLM-drafted `not_null` on it must always-pass
and the prune engine drops it. The strict 0.95 grade thresholds in
the fixture config typically surface at least one `flagged` artifact.

**A high drop rate is the working state, not the failure state.** A
typical staging model drops ~60-80% of the LLM-drafted tests as
always-passes — the LLM proposes broadly and the prune layer trims the
ones the warehouse data doesn't contradict. Internal testing on
`bigquery-public-data.austin_bikeshare.bikeshare_trips` shows 5 of 8
drafted tests dropped (62.5%); see
[`docs/prune-ops.md` § Expected drop rates](docs/prune-ops.md#expected-drop-rates)
for the per-test-type breakdown.

Two durable artefacts land under `/tmp/sf-austin/.signalforge/`:
`grade.json` (per-criterion LLM-judge scores) and `diff.json` (the
full rendered diff). The committed `.gitignore` covers `.signalforge/`.

### Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `User does not have bigquery.jobs.create permission in project bigquery-public-data` | `GOOGLE_CLOUD_PROJECT` not set; SDK fell back to the source project | Export `GOOGLE_CLOUD_PROJECT=<billing-project>` where you have the `BigQuery Job User` role |
| `Query exceeded max_bytes_billed (limit=100000000, ...)` | Editing the profile dropped or lowered `maximum_bytes_billed` | Keep `maximum_bytes_billed: 1000000000` (1 GB) — the smoke test ships this cap intentionally |
| `Manifest not found` / `dbt_project.yml not found at ...` | CLI walked up from the wrong cwd, or `--project-dir` doesn't directly contain `dbt_project.yml` | Either `cd` into the project root, or pass `--project-dir <abs-path>` pointing at the directory holding `dbt_project.yml` |
| `aggregate_complete=False` in `grade.json` | Network blip during a grade call exhausted retries | Re-run; if it persists, raise `grade.total_budget_seconds` in `signalforge.yml` |
| `LLM response did not match the CandidateSchema shape` | Anthropic response shape drifted vs. the parser | Set `ANTHROPIC_LOG=info` and inspect `~/.anthropic-debug/`; file an issue |

Full per-flag reference, exit-code taxonomy, and environment
variables: [docs/cli-ops.md](docs/cli-ops.md). For multi-model dbt
projects, see [Running across many
models](docs/cli-ops.md#running-across-many-models) for the
`--select` flag and shell-loop pattern. Maintainer-only walkthrough
of the same flow as a gated test (`pytest -m e2e --no-cov`):
[docs/e2e-smoke-test.md](docs/e2e-smoke-test.md).

## CLI

Four subcommands ship in v0.1:

```bash
signalforge generate <model>   # full pipeline; --mode, --min-score, --write/--dry-run, --format
signalforge init-demo [<dest>] # copy the bundled Austin demo project into <dest>; --force
signalforge lint               # validate signalforge.yml config blocks
signalforge version            # print the SignalForge version
```

`signalforge --help` prints the top-level help; each subcommand has its
own `--help` page. See [docs/cli-ops.md](docs/cli-ops.md) for the full
reference.

## Configuration

### Configuring the BigQuery adapter

SignalForge reads your dbt profile and instantiates a `BigQueryAdapter`
via `WarehouseAdapter.from_profile(profile)`. See
[docs/warehouse-adapter-ops.md](docs/warehouse-adapter-ops.md) for ADC
setup, cost defaults, sampling strategy (and the TABLESAMPLE
cost-asterisk), `PartitionFilter` use, and the typed-error reference.

## Data safety

Schema-only is the default. The LLM never sees row data unless you
explicitly opt in via `safety.mode: sample` in `signalforge.yml` (or
the `--mode sample` CLI flag). Even column *names* that match the
built-in PII patterns (`*email`, `*phone`, `*ssn`) — or that you flag
via dbt `tags: ["pii"]` / `meta.contains_pii: true` /
`meta.signalforge.sample: false` — are replaced with stable hashed
placeholders (`col_<8 hex>`) before reaching the LLM.

Note: the prune step runs warehouse SQL on every invocation regardless
of `safety.mode`. To skip prune entirely (no warehouse contact), set
`prune.enabled: false` in `signalforge.yml` — see
[docs/prune-ops.md](docs/prune-ops.md#configuration-signalforgeyml-prune-block).

Every LLM call produces one structured record at
`.signalforge/audit.jsonl` (default; configurable via
`safety.audit_path`). The file contains plaintext column-name metadata
and should be treated as sensitive: this repo's `.gitignore` already
covers `.signalforge/`; the writer creates the directory at `0o700`
and the audit file at `0o600`. The audit writer is fail-closed — if
the write fails, the LLM call is aborted (no silent drafts without an
audit trail). See [docs/safety-ops.md](docs/safety-ops.md) for the
JSONL schema.

Full reference — mode semantics, the four opt-out signals and their
precedence, the `signalforge.yml` schema, the audit schema, debugging,
and the typed-error reference — is in
[docs/safety-ops.md](docs/safety-ops.md).

## LLM drafting

### How drafting works

`signalforge.draft.draft_schema` takes a manifest model + warehouse
adapter + safety policy and returns a `DraftOutcome` carrying the
parsed `CandidateSchema`, the typed `LLMRequest` that was sent, and
the `LLMResult` from the LLM. One LLM call per model; pre-send token
counting, the full retry taxonomy, prompt caching, and a fail-closed
response audit are all owned by the layer.

```text
Manifest + Model + LLMRequest (from safety layer)
  -> render_prompt  (system + cached manifest summary + dynamic per-model SQL)
  -> call_anthropic (1 SDK seam, full retry taxonomy, prompt caching)
  -> parse_draft_response (JSON + anchor-contract validator)
  -> write_response_event (fail-closed JSONL audit)
  -> DraftOutcome(candidate, request, result)
```

### Auditability

Two parallel audit streams sit under `policy.audit_path.parent`:

- `audit.jsonl` (safety layer) records WHAT data went to the LLM —
  columns sent, redactions applied, sampling mode in effect.
- `llm_responses.jsonl` (draft layer) records WHAT the LLM produced —
  hashes of the response text, the parsed schema, and the SQL sent;
  token usage including cache creation/read; the `prompt_version`.

Both streams are fail-closed: an audit-write failure aborts the call,
the partial work is dropped, and an unaudited LLM call cannot
silently happen. A reviewer correlates the two streams by
`model_unique_id` + timestamp window. See
[docs/draft-ops.md](docs/draft-ops.md) for the response-audit schema,
the retry taxonomy, the cache pre-send checks, and the typed-error
reference.

## Roadmap

| Version | Scope                                                                              |
| ------- | ---------------------------------------------------------------------------------- |
| v0.1    | Single-model draft + warehouse prune; first warehouse adapter (BigQuery); CLI only |
| v0.2    | Additional warehouse adapters (Snowflake, Postgres); project-wide drift detection  |
| v0.3    | GitHub Action with PR comment integration                                          |
| v0.4    | Rubric customization; organization-wide style profiles                             |
| v1.0    | dbt Fusion engine compatibility; dbt MCP server consumption                        |

The architecture is warehouse-agnostic — adapters plug in behind a thin
sampling/profiling interface. BigQuery is the v0.1 target because of its
generous query-bytes pricing for sampled reads and its first-class
`INFORMATION_SCHEMA.JOBS` history for downstream cost analysis. Snowflake,
Databricks, Postgres, and Redshift are all on the roadmap; PRs welcome.

Detail is tracked in GitHub Issues against this repo.

## Design principles

1. **Signal over volume.** A test that always passes is worse than no test — it consumes review attention without catching anything. SignalForge's job is to produce fewer, better artifacts.
2. **Evaluation in the loop.** Generation without grading is what produced the current "AI-test fatigue." Every artifact SignalForge ships has been scored.
3. **OSS-first, Core-friendly.** No dependency on dbt Cloud. Runs against any dbt-core project, locally or in CI.
4. **Explainable diffs.** Every kept and dropped artifact has a one-line "why." Reviewers see what changed and what the tool's reasoning was.
5. **Permissive license.** Apache-2.0. Use it commercially, vendor it, embed it.

## Related projects

- **[clauditor](https://github.com/wjduenow/clauditor)** — the LLM-graded evaluation framework SignalForge's quality layer is built on.
- **[dbt-codegen](https://github.com/dbt-labs/dbt-codegen)** — the rule-based YAML scaffolder SignalForge complements (codegen scaffolds; SignalForge drafts, prunes, and grades).
- **[dbt-osmosis](https://github.com/z3z1ma/dbt-osmosis)** — schema.yml management and propagation; orthogonal concern.
- **[Recce](https://github.com/DataRecce/recce)** — PR-time data diff for dbt; complementary, addresses a different pain point.

## License

Apache-2.0. See [LICENSE](LICENSE).

## Contributing

Pre-alpha — issues welcome to shape the design. Open one against the `dev` branch describing the use case you'd like SignalForge to handle. Code contributions will open with the v0.1 milestone.
