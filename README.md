[![codecov](https://codecov.io/gh/wjduenow/SignalForge/branch/dev/graph/badge.svg)](https://codecov.io/gh/wjduenow/SignalForge)

# SignalForge

> LLM-drafted dbt schema.yml, tests, and docs — pruned against real warehouse data so only signal-bearing tests ship.

**Status:** v0.1 alpha. Nine issues shipped — single-model draft + warehouse prune, BigQuery adapter, `signalforge` CLI. Designing in the open on the `dev` branch.

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

```bash
git clone https://github.com/wjduenow/SignalForge.git
cd SignalForge
pip install -e ".[dev]"
signalforge generate models/marts/customer_lifetime_value.sql
```

(SignalForge is not yet published on PyPI — once v0.1 ships there,
`pip install signalforge` will replace the clone-and-editable-install
incantation above.)

The CLI walks up from the current working directory to find
`dbt_project.yml`, loads the manifest, drafts candidate `schema.yml`
artefacts via the LLM, prunes always-pass / known-clean-fail tests
against warehouse samples, grades the survivors against a configurable
rubric, and prints the diff. The full per-flag reference, the
four-tier exit-code taxonomy, environment variables, and a worked
example are in [docs/cli-ops.md](docs/cli-ops.md).

## Trying it out

The repo ships a minimal dbt fixture under
`tests/fixtures/dbt_project_austin/` pointing at the public
`bigquery-public-data.austin_bikeshare.bikeshare_trips` dataset, so
you can run `signalforge` end-to-end against a real warehouse with no
infrastructure beyond a Google Cloud billing project and an Anthropic
API key. A run scans <100 MB of BigQuery (≈$0.13 at on-demand pricing)
and completes in under a minute of wall-clock.

```bash
# Authenticate to Google Cloud (first run only)
gcloud auth application-default login

# Set the BigQuery billing project (any GCP project you have query access to)
export GOOGLE_CLOUD_PROJECT=<your-billing-project>

# Set your Anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...

# Run the canonical example
cd tests/fixtures/dbt_project_austin/
signalforge generate models/staging/stg_bikeshare_trips.sql
```

What to expect: the diff lists several kept artifacts (column
descriptions + signal-bearing tests), at least one dropped test with
reason `always-passes` (the staging model is engineered with literal
and `COALESCE`'d columns to give the LLM mathematically-guaranteed
always-pass tests to drop), and at least one `flagged` artifact —
the fixture pins tight grade thresholds (`min_pass_rate: 0.95` /
`min_mean_score: 0.95`) so the LLM-as-judge scrutiny is real.

Use a fresh shell session (or `unset ANTHROPIC_API_KEY` after the
run) so the key doesn't persist in your bash history.

The same flow runs as a gated maintainer test (`pytest -m e2e --no-cov`).
For the full walkthrough — what the test proves, prerequisites,
cost ceiling, troubleshooting — see
[docs/e2e-smoke-test.md](docs/e2e-smoke-test.md). Full CLI flag
reference and exit codes: [docs/cli-ops.md](docs/cli-ops.md).

## CLI

Three subcommands ship in v0.1:

```bash
signalforge generate <model>   # full pipeline; --mode, --min-score, --write/--dry-run, --format
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
