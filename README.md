# SignalForge

> LLM-drafted dbt schema.yml, tests, and docs — pruned against real warehouse data so only signal-bearing tests ship.

**Status:** Pre-alpha. Designing in the open on the `dev` branch.

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

> **Status (v0.1, in progress):** Not yet on PyPI. The CLI shape below is the
> intended target — the CLI itself ships in a follow-up ticket of v0.1. Today
> the package installs from a clone with `pip install -e .[dev]`.

## Quick start

> ```bash
> pip install signalforge
> signalforge generate models/marts/customer_lifetime_value.sql
> ```

A first runnable version is targeted for v0.1 (single-model draft + warehouse prune, Snowflake adapter, CLI only).

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
