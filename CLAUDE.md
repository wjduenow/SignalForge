# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What SignalForge is

A CLI that drafts dbt `schema.yml`, tests, and docs with an LLM, then **prunes** the
candidates against real warehouse data so only signal-bearing artifacts ship. The
differentiator vs. dbt Copilot / dbt-codegen / DinoAI / datapilot is the prune step —
competitors generate; SignalForge generates *and grades*.

## Pipeline shape

```
model.sql + manifest + project ctx
  -> LLM drafts candidate artifacts
  -> run candidates against warehouse samples
  -> drop always-pass tests; drop tests that fail on known-clean data
  -> emit graded YAML + diff with per-artifact "why"
```

The "drop tests that fail on known-clean data" branch is as important as the always-pass
branch — both directions of noise need pruning.

## Architectural commitments (load-bearing — preserve when implementing)

Stated in the README as design principles, not aspirations. New code must respect them:

1. **Signal over volume.** A candidate test that always passes on warehouse samples must be dropped, not shipped. Always-pass = no signal = worse than nothing because it consumes reviewer attention. Code paths that emit artifacts without running them through the prune step are a bug.
2. **Evaluation in the loop.** The grading layer reuses [clauditor](https://github.com/wjduenow/clauditor)'s LLM-as-judge methodology. Doc/artifact quality is scored against a configurable rubric — don't add ungraded artifact classes.
3. **Warehouse-agnostic by design.** Adapters plug in behind a thin sampling/profiling interface. v0.1 ships **BigQuery**; v0.2 adds the **Snowflake** seam. Don't bake BigQuery-isms into core — keep the adapter seam clean.
4. **OSS-first, Core-friendly.** No dependency on dbt Cloud. Must run against any dbt-core project, locally or in CI.
5. **Explainable diffs.** Every kept/dropped artifact ships with a one-line "why." Don't add black-box code paths that drop or keep artifacts without recording the reason.

## Architecture map

The pipeline is a chain of subpackages, each with a distilled rules file in
`docs/rules/` and an operational reference in `docs/*-ops.md`.
**The rules files are the working contract** — read the relevant one (the table below
maps layer → file) before touching a layer. They are *not* auto-loaded into context;
load the file for the layer you're working in.

| Subpackage | Role | Rules file |
|---|---|---|
| `signalforge.manifest` | dbt `manifest.json` reader, model selectors, ref/source registry | `manifest-readers.md` |
| `signalforge.warehouse` | `WarehouseAdapter` ABC + `from_profile`; BigQuery + Snowflake adapters | `warehouse-adapters.md` |
| `signalforge.safety` | PII redaction + fail-closed audit; schema-only default | `safety-layer.md` |
| `signalforge.llm` / `signalforge.draft` | Anthropic SDK seam + LLM drafter | `llm-drafter.md` |
| `signalforge.prune` | compile candidate tests → SQL → drop/keep decisions | `prune-engine.md` |
| `signalforge.grade` | LLM-as-judge rubric scoring + sidecar | `grade-layer.md` |
| `signalforge.diff` | kept/dropped/flagged table + unified diff + sidecar | `diff-renderer.md` |
| `signalforge.cli` | console-script entry, four-tier exit codes | `cli-layer.md` |
| `signalforge.ingest` | external `schema.yml` / `tests/*.sql` reader (prune any generator's tests) | `ingest-layer.md` |
| `signalforge.demo` | bundled Austin demo project (`init-demo`) | — |

Cross-cutting rules: `business-rule-tests.md` (the `custom_sql` 5th test type, threaded
through every stage), `testing-signal.md`, `python-build.md`, `ci-supply-chain.md`,
`docs-publishing.md`.

The **public API** of each subpackage is its `__all__`; the contract detail is in the
matching `docs/*-ops.md`. Internals are `_`-prefixed and not part of the public contract.

## History

`CHANGELOG.md` is the curated, release-facing record (versioned sections). Per-issue design
decisions live in `plans/super/<n>-<topic>.md` (DEC-numbered ADRs). The `docs/rules/`
files distil the durable working conventions from those plans. Don't re-narrate shipped
work here — those three places own it.

## Roadmap anchors

v0.1 = single-model draft + warehouse prune, BigQuery adapter, CLI only. v0.2 adds the
Snowflake seam, external-test ingestion (`prune-existing`), and uv tooling. Don't pull
later-version scope (drift detection, GitHub Action, dbt Fusion / MCP) into earlier work
unless the user explicitly asks. The roadmap table in `README.md` is the source of truth
for scope boundaries.

## Validation

Canonical local command (CI runs the same four checks across a 3.11 / 3.12 / 3.13 matrix):

```bash
uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
```

The repo is uv-managed (see `docs/rules/python-build.md`); `uv.lock` is committed.
`pip install -e ".[dev]"` still works for contributors without uv. Gated test markers
(`bigquery`, `anthropic`, `cli_subprocess`, `e2e`, `wheel_smoke`, `snowflake`) are excluded
by default and run with `--no-cov` (see `testing-signal.md`).

## Project facts

- Import package `signalforge`; distributed on PyPI as `signalforge-dbt` (the bare `signalforge` name is held by an unrelated DSP package); installed via `pip install signalforge-dbt`; exposes a `signalforge` CLI entry point.
- Apache-2.0; the repo-level `LICENSE` covers it — no per-file headers.
- Design happens on the `dev` branch; PRs target `dev`, not `main`.
- Docs site: https://wjduenow.github.io/SignalForge/ (MkDocs Material, redeploys on push to `main`; see `docs-publishing.md`).

## Related projects (so suggestions don't reinvent them)

- **clauditor** — the eval framework the grading layer reuses. Reach for it before writing a new judge harness.
- **dbt-codegen** — rule-based YAML scaffolder. SignalForge *complements* it; don't duplicate its rule-based generation.
- **dbt-osmosis** — schema propagation; orthogonal, not a competitor.
- **Recce** — PR-time data diff; complementary.

## Beads (issue tracker, available)

This repo has **bd (beads)** initialized for issue tracking. One tool among several — use it
where it fits. The `/super-plan` workflow devolves stories into beads in its final phase;
ad-hoc work doesn't have to.

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
bd prime              # Full command reference
```
