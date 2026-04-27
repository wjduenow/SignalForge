# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

Pre-alpha. As of this writing the repo contains only `README.md` and `LICENSE` — no source, tests, or build configuration exist yet. Design is happening in the open on the `dev` branch; code is expected to land with the v0.1 milestone. When asked to "build" or "test" without scaffolding present, surface that fact rather than guessing at commands.

## What SignalForge is

A CLI that drafts dbt `schema.yml`, tests, and docs with an LLM, then **prunes** the candidates against real warehouse data so only signal-bearing artifacts ship. The differentiator vs. dbt Copilot / dbt-codegen / DinoAI / datapilot is the prune step — competitors generate; SignalForge generates *and grades*.

## Architectural commitments (load-bearing — preserve when implementing)

These are stated in the README as design principles, not aspirations. New code should respect them:

1. **Signal over volume.** A candidate test that always passes on warehouse samples must be dropped, not shipped. Always-pass = no signal = worse than nothing because it consumes reviewer attention. Code paths that emit artifacts without running them through the prune step are a bug.
2. **Evaluation in the loop.** The grading layer reuses [clauditor](https://github.com/wjduenow/clauditor)'s LLM-as-judge methodology. Doc/artifact quality is scored against a configurable rubric — don't add ungraded artifact classes.
3. **Warehouse-agnostic by design.** Adapters plug in behind a thin sampling/profiling interface. v0.1 ships **BigQuery** only (chosen for query-bytes pricing on sampled reads + `INFORMATION_SCHEMA.JOBS` history). Snowflake/Postgres come in v0.2; Databricks/Redshift later. Don't bake BigQuery-isms into core — keep the adapter seam clean from day one.
4. **OSS-first, Core-friendly.** No dependency on dbt Cloud. Must run against any dbt-core project, locally or in CI.
5. **Explainable diffs.** Every kept/dropped artifact ships with a one-line "why." Don't add black-box code paths that drop or keep artifacts without recording the reason.

## Pipeline shape (per README)

```
model.sql + manifest + project ctx
  -> LLM drafts candidate artifacts
  -> run candidates against warehouse samples
  -> drop always-pass tests; drop tests that fail on known-clean data
  -> emit graded YAML + diff with per-artifact "why"
```

The "drop tests that fail on known-clean data" branch is as important as the always-pass branch — both directions of noise need pruning.

## Roadmap anchors

v0.1 = single-model draft + warehouse prune, BigQuery adapter, CLI only. Don't pull v0.2+ scope (multi-warehouse, drift detection, GitHub Action, rubric customization, dbt Fusion / MCP) into v0.1 work unless the user explicitly asks. The roadmap table in `README.md` is the source of truth for scope boundaries.

## Conventions to set when scaffolding lands

When the first code goes in, prefer choices consistent with the README's stated intent:

- Python package named `signalforge`, installed via `pip install signalforge`, exposing a `signalforge` CLI entry point (the quick-start in the README commits to this shape).
- Apache-2.0 headers are not required in source files — the repo-level `LICENSE` covers it.
- Update this file once real build/test/lint commands exist.

## Related projects (so suggestions don't reinvent them)

- **clauditor** — the eval framework SignalForge's grading layer reuses. Reach for it before writing a new judge harness.
- **dbt-codegen** — rule-based YAML scaffolder. SignalForge *complements* it (codegen scaffolds; SignalForge drafts/prunes/grades). Don't duplicate codegen's rule-based generation.
- **dbt-osmosis** — schema propagation; orthogonal concern, not a competitor.
- **Recce** — PR-time data diff; complementary.


## Beads (issue tracker, available)

This repo has **bd (beads)** initialized for issue tracking. It is one tool among several — use it where it fits, not as the only path. The `/super-plan` workflow devolves stories into beads in its final phase; ad-hoc work doesn't have to.

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
bd prime              # Full command reference
```
