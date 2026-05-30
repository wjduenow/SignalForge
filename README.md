[![codecov](https://codecov.io/gh/wjduenow/SignalForge/branch/dev/graph/badge.svg)](https://codecov.io/gh/wjduenow/SignalForge) [![docs](https://img.shields.io/badge/docs-signalforge-blue?logo=materialformkdocs)](https://wjduenow.github.io/SignalForge/)

# SignalForge

> LLM-drafted dbt schema.yml, tests, and docs — pruned against real warehouse data so only signal-bearing tests ship.

## Why this exists

Authoring `schema.yml`, tests, and documentation is the most-cited drudgery in the dbt ecosystem. AI tools that generate them already exist — dbt Copilot, dbt-codegen, Paradime DinoAI, Altimate datapilot — but their output is consistently described the same way: *noise*. Hundreds of `not_null` and `unique` tests that always pass. Generic docstrings that paraphrase the column name. Schemas that drift from the SELECT.

SignalForge generates the same artifacts, then asks a different question: **does this test produce signal?** Every candidate test is run against your real warehouse data. Tests that always pass are dropped. Docs are graded against a project-specific rubric. Only signal-bearing artifacts are written to disk.

And you don't have to start from SignalForge's own drafts. Point it at a `schema.yml` that dbt Copilot, dbt-codegen, DinoAI, datapilot — or your own hands — already produced, and it prunes *that*: `signalforge prune-existing <model> --schema <path>` runs the same warehouse-backed prune over your existing tests, no LLM call required.

## What it does

- **Drafts `schema.yml`** from your model SQL using an LLM with project-aware context (manifest, sibling models, your team's terminology).
- **Generates tests** — `not_null`, `unique`, `accepted_values`, `relationships`, plus dbt-expectations-style data tests where appropriate.
- **Drafts custom business-rule tests** — the fifth test type beyond `not_null` / `unique` / `accepted_values` / `relationships`. Declare a rule in plain English (`meta.signalforge.business_rules: "total_amount must never be negative"`) and SignalForge writes a singular `tests/*.sql` test for it, prunes it against your warehouse, and ships only the rules your data can actually violate. No declared rules? It infers checkable invariants from your SQL. Worked example: [Custom business-rule tests](#custom-business-rule-tests-worked-example).
- **Prunes the noise.** Each candidate test runs against warehouse samples; tests that pass on every row of historical data add no signal and are dropped before they reach your repo.
- **Generates documentation** — column-level descriptions and model-level overviews — graded by an LLM-as-judge against a configurable rubric.
- **Reports what was kept and what was dropped**, with a one-line "why" per artifact. No black-box generation.
- **Prunes tests you already have.** Point it at an existing `schema.yml` — from dbt-codegen, dbt Copilot, DinoAI, datapilot, or hand-written — and the warehouse tells you which of *those* tests add no signal. Same prune step, no LLM call (`signalforge prune-existing`).

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

There's a second entry point that skips the LLM entirely. If you already have a `schema.yml` (from another generator or written by hand), `signalforge prune-existing` reads its tests and runs them straight through the prune step:

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ existing     │ -> │ Run tests    │ -> │ diff: which  │
│ schema.yml   │    │ against the  │    │ tests add    │
│ + manifest   │    │ warehouse    │    │ signal       │
└──────────────┘    └──────────────┘    └──────────────┘
```

No draft, no grade, no LLM call — just "which of these tests earn their place?" Tests SignalForge can't evaluate (custom / dbt-expectations / namespaced generics) are reported as skipped, never silently dropped.

## Supported warehouses

SignalForge ships two production warehouse adapters today: **BigQuery** (the original target — exercised end-to-end by `signalforge init-demo` and the quick start below) and **Snowflake** (full sampling, materialised-sample CTAS, and `EXPLAIN`-based bytes estimation; one combination — `safety: aggregate-only` / Snowflake `column_stats` — is not yet implemented, every other mode/scope/strategy combination is functional). **Postgres** ships as a typed `NotImplementedError` stub; **Databricks** and **Redshift** remain on the roadmap.

The architecture is warehouse-agnostic — adapters plug in behind a thin sampling/profiling interface (`WarehouseAdapter.from_profile`), so new vendors slot in without touching the draft / prune / grade / diff stages. Per-warehouse setup (auth, cost guardrails, profile-field requirements) lives in [Configuration](#configuration).

> **Live on PyPI** — `pip install signalforge-dbt`. The quick start below runs against BigQuery (the bundled `init-demo` fixture targets the Austin bikeshare public dataset). Snowflake users wire their own dbt profile and project — see [Configuration](#configuration) and [docs/snowflake-e2e-setup.md](docs/snowflake-e2e-setup.md).

## Supported LLM providers

SignalForge calls an LLM at exactly two stages: the **drafter** (one call per `generate` run, produces the candidate `schema.yml` + tests + docs) and the **grader** (one call per `(artifact × rubric criterion)` pair, scores the kept artifacts against a rubric). The other five stages — manifest, safety, prune, ingest, diff — are LLM-free. `signalforge prune-existing` and `signalforge lint` issue zero LLM calls.

Three providers are supported behind a single provider-neutral seam:

| Provider | Install | Env var | Prompt caching | Server-side JSON |
|---|---|---|---|---|
| **Anthropic** (default) | base `pip install signalforge-dbt` | `ANTHROPIC_API_KEY` | ✅ `cache_control` (5m / 1h) | parser-side |
| **OpenAI** | `pip install signalforge-dbt[openai]` | `OPENAI_API_KEY` | ❌ | ✅ `response_format` |
| **Google Gemini** | `pip install signalforge-dbt[gemini]` | `GOOGLE_API_KEY` | ❌ (deferred) | ✅ `response_mime_type` |

The drafter and grader resolve their providers independently — a common pattern is Anthropic drafter (benefits from prompt caching across `--select` siblings) + Gemini grader (cheaper per-token rates on the multi-call fan-out). All three providers integrate with `signalforge generate --estimate` for pre-flight cost preview.

Full reference, capability matrix, cost / caching tradeoffs, and the "adding a fourth provider" recipe live in [docs/llm-providers-ops.md](docs/llm-providers-ops.md).

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

SignalForge requires **Python 3.11+**.

```bash
pip install signalforge-dbt
```

Verify the CLI is on your PATH:

```bash
signalforge --version
```

The PyPI distribution name is **`signalforge-dbt`** (the bare `signalforge` name
is held by an unrelated DSP package); the import package and CLI command are both
**`signalforge`**.

**Prefer an isolated CLI install?** `uv tool install signalforge-dbt` (or
`pipx install signalforge-dbt`) puts the `signalforge` command on your PATH
without adding it to a project environment.

**Working from a clone (contributing)?** Install the dev toolchain with
`uv sync --dev` — see [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

Run `signalforge install-skill` to drop the [Claude Code skill](docs/skills.md)
into your project's `.claude/skills/signalforge/` and let Claude drive
SignalForge end-to-end.

### 2. Authenticate to BigQuery and your LLM provider

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=<your-billing-project>   # any GCP project you have query access to
export ANTHROPIC_API_KEY=sk-ant-...
```

Use a fresh shell session (or `unset ANTHROPIC_API_KEY` after the
run) so the key doesn't persist in your bash history.

Anthropic is the default LLM provider; OpenAI and Google Gemini ship
behind the same provider-neutral seam. To switch providers, install
the matching extra (`signalforge-dbt[openai]` / `signalforge-dbt[gemini]`),
set the matching env var (`OPENAI_API_KEY` / `GOOGLE_API_KEY`), and
add `llm.provider:` / `grade.provider:` to `signalforge.yml`. The
drafter and grader knobs are independent — a mixed-provider run
(e.g. Anthropic drafter + Gemini grader) is supported. Capability
matrix and cost tradeoffs: [docs/llm-providers-ops.md](docs/llm-providers-ops.md).

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
  sample_strategy: materialised   # default; one temp-table CTAS feeds every per-test query
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
runs emit a header + one bullet per block. Pass `--model <name>` to
also confirm a specific model resolves in the manifest (accepts a
bare name, a `unique_id`, or a file path). See
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
alongside dropped tests with a one-line "why". Every artifact lands
in one of four tiers — `kept` (survived prune with positive
evidence), `kept-uncertain` (kept, but the warehouse couldn't be
reached to evaluate it — e.g. a budget or connectivity issue),
`dropped` (prune found it adds no signal), and `flagged` (kept, but
graded below the quality threshold). The table looks like this
(truncated):

```text
diff: model.austin.stg_bikeshare_trips  kept=8  kept-uncertain=0  dropped=2  flagged=1

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

### Custom business-rule tests (worked example)

The four generic test types catch *structural* invariants (nullability,
uniqueness, referential integrity). They cannot catch a **business
rule** — "a refund never exceeds its order," "discount percent stays in
0–100." For those, declare the rule in plain English in your model's
`meta`, and SignalForge drafts a singular SQL test for it.

```yaml
# models/marts/dim_customers.yml
models:
  - name: dim_customers
    config:
      meta:
        signalforge:
          business_rules: "total_amount must never be negative"
```

On the next `signalforge generate dim_customers`, the LLM translates the
rule into a failing-rows SELECT — a `custom_sql` candidate test:

```sql
-- signalforge:generated a1b2c3d4
select * from {{ this }} where total_amount < 0
```

That candidate runs through the **same prune step** as every other test.
The decision is data-driven:

- **Kept** if the warehouse has any negative-total rows — the rule
  catches real bad data, so the test earns its place. With `--write` it
  is materialised to `tests/dim_customers__total_amount_custom_sql_a1b2c3d4.sql`.
- **Dropped (`always-passes`)** if no row ever violates it — the rule is
  true but the data never tests it, so shipping it would be review-noise.

You can also list multiple rules, scope a rule to a column, or skip the
`meta` entirely and let SignalForge **infer** checkable invariants from
your SQL. The drafted SQL may reference `{{ this }}`, `{{ ref('m') }}`,
and `{{ source('s','t') }}` (control-flow Jinja is not supported). A
multi-table rule (a `JOIN`) runs full-scan within the warehouse bytes
cap rather than against a sample. See
[`docs/draft-ops.md` § Custom business-rule tests](docs/draft-ops.md#custom-business-rule-tests-custom_sql)
and [`docs/prune-ops.md` § `custom_sql` evaluation](docs/prune-ops.md#custom_sql-evaluation).

Already have hand-authored singular `tests/*.sql` files? `prune-existing`
ingests and prunes those too — see
[Prune the tests you already have](#prune-the-tests-you-already-have).

### Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `User does not have bigquery.jobs.create permission in project bigquery-public-data` | `GOOGLE_CLOUD_PROJECT` not set; SDK fell back to the source project | Export `GOOGLE_CLOUD_PROJECT=<billing-project>` where you have the `BigQuery Job User` role |
| `Query exceeded max_bytes_billed (limit=100000000, ...)` | Editing the profile dropped or lowered `maximum_bytes_billed` | Keep `maximum_bytes_billed: 1000000000` (1 GB) — the bundled demo `profiles.yml` ships this cap intentionally so the materialised-sample scan clears the adapter's 100 MB default |
| `Manifest not found` / `dbt_project.yml not found at ...` | CLI walked up from the wrong cwd, or `--project-dir` doesn't directly contain `dbt_project.yml` | Either `cd` into the project root, or pass `--project-dir <abs-path>` pointing at the directory holding `dbt_project.yml` |
| `aggregate_complete=False` in `grade.json` | Network blip during a grade call exhausted retries | Re-run; if it persists, raise `grade.total_budget_seconds` in `signalforge.yml` |
| `LLM response did not match the CandidateSchema shape` | Anthropic response shape drifted vs. the parser | Set `ANTHROPIC_LOG=info` and inspect `~/.anthropic-debug/`; file an issue |

Full per-flag reference, exit-code taxonomy, and environment
variables: [docs/cli-ops.md](docs/cli-ops.md). For multi-model dbt
projects, see [Running across many
models](docs/cli-ops.md#running-across-many-models) for the
`--select` flag and shell-loop pattern. Maintainer-only walkthrough
of the same flow as a gated test (`pytest -m e2e --no-cov`):
[docs/e2e-smoke-test.md](docs/e2e-smoke-test.md). To run the
Snowflake-backed gated tests (`pytest -m snowflake --no-cov`),
start from [docs/snowflake-e2e-setup.md](docs/snowflake-e2e-setup.md)
(account setup, cost guardrails, and the `.env.example` template).

## Prune the tests you already have

If you already have a `schema.yml` — written by hand, or generated by
dbt-codegen / dbt Copilot / DinoAI / datapilot — you don't need
SignalForge to redraft it. Point `prune-existing` at it and the
warehouse tells you which of those tests add signal. There's no LLM
call, so the only requirement is warehouse access (a dbt profile).

```bash
# From inside your dbt project (with target/manifest.json present):
signalforge prune-existing customers --schema models/marts/schema.yml
```

What you get on stdout is a diff of *your* file: a kept / kept-uncertain
/ dropped table with a one-line "why" per test, plus a unified diff
showing exactly which tests to remove. Tests SignalForge doesn't yet
evaluate — custom generics, `dbt_utils.*`, `dbt_expectations.*` — are
summarised on stderr as skipped (run with `--verbose` for the per-test
breakdown), never silently dropped.

Your **singular `tests/*.sql` business-rule tests are pruned in the same
run.** Each `.sql` whose `ref()` / `source()` / `{{ this }}` resolves to
this model is read as a `custom_sql` candidate and pruned alongside the
schema.yml tests, deduped against any matching schema.yml `custom_sql`.
Override the directory with `--tests-dir`; files referencing other models
are ignored, and a `.sql` carrying unsupported control-flow Jinja folds
into the skipped report.

It is **read-only by design**: there is no `--write` flag, so your
hand-authored file is never overwritten. The rendered diff goes to
stdout and a machine-readable copy to `.signalforge/diff.json`
(`--dry-run` suppresses even that). Apply the removals yourself from the
diff. See [docs/cli-ops.md § `signalforge
prune-existing`](docs/cli-ops.md#signalforge-prune-existing-model-schema-path)
for the full flag set and [docs/ingest-ops.md](docs/ingest-ops.md) for
which dbt test shapes are supported vs. skipped.

## CLI

The CLI exposes five subcommands, all shipped on PyPI:

```bash
signalforge generate <model>                     # full draft -> prune -> grade -> diff pipeline for one model
signalforge prune-existing <model> --schema <p>  # prune an existing schema.yml's tests (ingest -> prune -> diff, no LLM)
signalforge init-demo [<dest>]                   # copy the bundled Austin demo project into <dest>
signalforge lint                                 # validate signalforge.yml config blocks (no LLM/warehouse calls)
signalforge version                              # print the SignalForge version
```

Key `generate` flags: `--project-dir`, `--manifest`, `--profiles-dir`
(point at the project / manifest / profile); `--mode
{schema-only,aggregate-only,sample}` and `--min-score` (pipeline
behaviour); `--write` / `--dry-run` and `--format {ansi,markdown,json}`
(output); `--estimate` (cost preview, no billable calls); `--select
<expr>` (run across many models); `--scope`, `--sample-strategy`; and
the `--quiet` / `--verbose` / `--no-color` observability triad.
`prune-existing` takes the required `--schema <path>` plus
`--project-dir`, `--manifest`, `--profiles-dir`, `--scope`,
`--sample-strategy`, `--format {ansi,markdown,json}`, `--dry-run`, and
the `--quiet` / `--verbose` / `--no-color` triad — it is read-only by
design (no `--write`) and makes no LLM call. `init-demo` takes
`--force`; `lint` takes `--config`, `--manifest`, `--model`,
`--project-dir`.

`signalforge --help` prints the top-level help; each subcommand has its
own `--help` page. See [docs/cli-ops.md](docs/cli-ops.md) for the full
reference, exit-code taxonomy, and environment variables.

## Configuration

SignalForge reads your existing dbt `profiles.yml` and dispatches on
`type:` — no second profile to maintain. The dispatch happens in
`WarehouseAdapter.from_profile(profile)`; each adapter then exposes the
same `sample_rows` / `materialise_sample` / `run_test_sql` /
`estimate_query_bytes` surface to the rest of the pipeline.

### BigQuery

A standard `type: bigquery` dbt target works. Authenticate via
Application Default Credentials and set the billing project:

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=<your-billing-project>
```

Cost is bounded by `maximum_bytes_billed` (100 MB default; the bundled
demo `profiles.yml` raises it to 1 GB so the materialised-sample scan
clears the cap). `use_query_cache` is forced off for reproducibility.
Full reference — ADC setup, sampling strategy (and the TABLESAMPLE
cost-asterisk), `PartitionFilter` use, and the typed-error reference —
is in [docs/warehouse-adapter-ops.md](docs/warehouse-adapter-ops.md).

### Snowflake

A standard `type: snowflake` dbt target works — `account`, `user`,
`warehouse`, plus either `password`, key-pair (`private_key_path` +
`private_key_passphrase`), or SSO (`authenticator: externalbrowser`).
`database` / `schema` / `role` are optional at profile level; SignalForge
will not override them at runtime.

Recommended cost guardrails before pointing it at a real Snowflake
account: create a **resource monitor** (e.g. 1-credit daily cap), use
an **X-Small warehouse with aggressive auto-suspend**, and start with
`prune.scope: sample` + `prune.sample_strategy: materialised`. Setup
walkthrough (incl. an `.env.example`) is in
[docs/snowflake-e2e-setup.md](docs/snowflake-e2e-setup.md); adapter
reference (sampling, session cleanup, `EXPLAIN`-based bytes estimation,
known limitations) is in
[docs/warehouse-adapter-ops.md § Snowflake adapter](docs/warehouse-adapter-ops.md).

> **Known limitation:** `safety: aggregate-only` (Snowflake `column_stats`)
> is not yet implemented. Every other combination is functional.

### Pipeline-stage configuration

Cross-cutting behaviour (sampling mode, prune scope, grade thresholds,
diff rendering) is configured per stage in `signalforge.yml` — see
[docs/safety-ops.md](docs/safety-ops.md),
[docs/prune-ops.md](docs/prune-ops.md),
[docs/grade-ops.md](docs/grade-ops.md), and
[docs/diff-ops.md](docs/diff-ops.md). `signalforge lint` validates the
file with no LLM or warehouse calls.

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
  -> call_llm       (provider-neutral seam, full retry taxonomy, prompt caching)
  -> parse_draft_response (JSON + anchor-contract validator)
  -> write_response_event (fail-closed JSONL audit)
  -> DraftOutcome(candidate, request, result)
```

`call_llm` dispatches the vendor-specific request shape / response
parse / exception classification to the registered `LLMProvider`
strategy (Anthropic / OpenAI / Gemini). See
[docs/llm-providers-ops.md](docs/llm-providers-ops.md) for the
capability matrix, the per-provider gotchas (Gemini truncation, the
`finish_reason` degrade path, server-side JSON modes), and the
recipe for adding a fourth provider.

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

Shipped:

| Version | Released   | Scope                                                                                                                                                          |
| ------- | ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| v0.1    | 2026-05-20 | Single-model draft + warehouse prune + LLM-as-judge grade + diff renderer; BigQuery adapter; `signalforge` CLI (`generate`, `lint`, `version`)                  |
| v0.2    | 2026-05-21 | Ingest externally-authored `schema.yml`; `signalforge prune-existing` (no-LLM prune path); `signalforge init-demo` first-run UX; uv tooling; Python 3.11–3.13   |
| v0.3    | 2026-05-27 | Snowflake warehouse adapter (full sampling, materialised-sample CTAS, `EXPLAIN`-based bytes estimation); custom business-rule tests (`custom_sql`, the 5th test type) — drafted from `meta.signalforge.business_rules` or LLM inference, then pruned like any other test |
| v0.4    | 2026-05-28 | **Multi-provider LLM support** — OpenAI (#136) and Google Gemini (#137) behind the provider-neutral seam established in #135 (Anthropic remains the default). `--estimate` is provider-aware: Anthropic uses `messages.count_tokens` (live SDK call), OpenAI uses local `tiktoken`, Gemini uses native `client.models.count_tokens` |

Planned:

| Version | Scope                                                                                                            |
| ------- | ---------------------------------------------------------------------------------------------------------------- |
| v0.5    | **Installable Claude Code skill** — `signalforge install-skill` ships a SKILL.md that teaches Claude to drive the CLI |
| v0.6    | **Airflow operator** — drop SignalForge into a scheduled DAG for periodic schema drift / signal-rot detection    |
| v0.7    | **GitHub Action** — PR-time invocation with inline comment integration (kept/dropped/flagged surfaced on the PR) |
| v0.8    | **Rubric customization** — project-specific grading criteria; organization-wide style profiles                   |
| v1.0    | **dbt Fusion engine compatibility** — dbt MCP server consumption; first-class Fusion integration                 |

Warehouse coverage beyond BigQuery + Snowflake — Postgres (stub today),
Databricks, Redshift — slots in behind the existing `WarehouseAdapter`
ABC and is roadmap-tracked but not version-pinned; PRs welcome.

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

Issues welcome to shape the design. Open one against the `dev` branch describing the use case you'd like SignalForge to handle.
