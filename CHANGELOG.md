# Changelog

All notable changes to SignalForge are documented here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [PEP 440](https://peps.python.org/pep-0440/) for version numbering.

## [Unreleased]

_Nothing yet — entries land here on `dev` and get promoted to a dated section at release time._

## [0.1.0] — 2026-05-20

First public release. Single-model draft + warehouse prune + LLM-as-judge grade + diff renderer, BigQuery only, CLI surface.

Distributed on PyPI as `signalforge-dbt` (the bare `signalforge` name is held by an unrelated DSP package). Import name and CLI command remain `signalforge`:

```bash
pip install signalforge-dbt
signalforge --version
```

### Added

- **`signalforge.manifest`** — Typed `Manifest` / `Model` (Pydantic v2), `load(project_dir, manifest_path=None) -> Manifest`, schema-version tolerance v9–v12, symlink-hardened path canonicalisation. (#2)
- **`signalforge.warehouse`** — `WarehouseAdapter` ABC + `from_profile` factory, `BigQueryAdapter` concrete, `load_profile` for dbt `profiles.yml`, deterministic hash-mod sampling with fail-loud sizing checks, `use_query_cache=False` default for reproducibility. (#3)
- **`signalforge.safety`** — Schema-only-default sampling-mode policy, fail-closed audit JSONL writer, column-name redaction via blake2b-4 hashes, four PII opt-out signals with documented precedence. (#4)
- **`signalforge.llm` + `signalforge.draft`** — Centralised `call_anthropic` SDK seam with full retry taxonomy, end-to-end `draft_schema(...)` drafter, typed `CandidateSchema` + discriminated-union `CandidateTest`, anchor-contract validator, fail-closed LLM response audit, `<MODEL_SQL>` prompt-injection envelope. (#5)
- **`signalforge.prune`** — `prune_tests(...)` orchestrator compiles dbt-style tests (`not_null`, `unique`, `accepted_values`, `relationships`) to BigQuery failing-rows SQL via `Dialect.quote_char` dispatch, routes outcomes through five `DropReason` literals, enforces total wall-clock budget, fail-closed `prune.jsonl` audit. Includes the v0.2 temp-table-materialised sample optimisation (`prune.sample_strategy: materialised` default; opt-out to `oneshot` for non-BigQuery adapters). (#6, #22)
- **`signalforge.grade`** — `grade_artifacts(...)` LLM-as-judge orchestrator, one call per `(artifact × criterion)`, per-criterion `score ∈ [0.0, 1.0]` + `passed: bool` with graceful degrade on retry exhaustion / parser failure / budget exhaustion, default four-criterion rubric (clarity, consistency, rationale, no-redundant) pinned by golden `rubric_hash`, fail-closed `grade.jsonl` per-call audit + `grade.json` end-of-run sidecar. (#7)
- **`signalforge.diff`** — `render_diff(...)` orchestrator with ANSI / Markdown / JSON renderers, kept/dropped/flagged tier table, unified diff against existing `schema.yml`, fail-closed `diff.json` sidecar, unconditional ANSI strip + Markdown HTML-entity escape on every user-content field, Markdown body truncation at the last hunk boundary below 60 KB. (#8)
- **`signalforge.cli`** — `signalforge` console-script entry point with `generate`, `lint`, and `version` subcommands. Four-tier exit-code taxonomy (`0` success, `1` load/parse, `2` input-validation / post-call invariant, `3` external-dep / fail-closed audit-write durability) wired to every typed error across nine `errors.py` modules, gated by an AST audit-completeness scan. No traceback ever leaks; structured stderr (`ERROR: <message>` + optional `↳ Remediation:`). Progress to stderr on TTY runs. (#9)
- **End-to-end smoke test** against `bigquery-public-data.austin_bikeshare.bikeshare_trips`, gated behind `@pytest.mark.e2e` + the `SF_RUN_BQ=1` / `ANTHROPIC_API_KEY` / `GOOGLE_CLOUD_PROJECT` env-var triple. Fixture project under `tests/fixtures/dbt_project_austin/`. (#10)
- **Codecov coverage reporting** wired through CI; `--cov-fail-under` floor enforced both locally and in CI. (#27)
- **README quick-start** covering install, auth (`gcloud auth application-default login` + `ANTHROPIC_API_KEY`), config, expected output, and troubleshooting. (#11)

### Architectural commitments locked

- Signal over volume — pruning is allowed to drop only with positive warehouse evidence; `kept-without-evidence` is a real outcome.
- Evaluation in the loop — every kept artifact gets a per-criterion grade with a one-line "why."
- Warehouse-agnostic by design — BigQuery-isms confined to `signalforge.warehouse.adapters.bigquery` + its `_client.py` shim; the ABC is dialect-neutral.
- OSS-first, Core-friendly — no dbt Cloud dependency; runs against any dbt-core project, locally or in CI.
- Explainable diffs — every kept/dropped/flagged artifact ships with a one-line "why"; every run produces a sidecar JSON with reproducibility hashes.

[Unreleased]: https://github.com/wjduenow/SignalForge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/wjduenow/SignalForge/releases/tag/v0.1.0
