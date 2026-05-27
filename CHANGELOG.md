# Changelog

All notable changes to SignalForge are documented here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [PEP 440](https://peps.python.org/pep-0440/) for version numbering.

## [Unreleased]

### Added

- **Snowflake test harness + ops docs (#124)** — closes the Snowflake adapter epic (#118). Full `map_snowflake_exception` taxonomy mirroring the BigQuery mapper (`ProgrammingError` "object does not exist" → `TableNotFoundError`, "invalid identifier" → `ColumnNotFoundError`, residual → `QuerySyntaxError`; auth → `WarehouseAuthError`; else passthrough), reusing existing typed errors. A fakesnow-backed adapter harness executes the adapter's non-`HASH` SQL offline and sqlglot-parses the `HASH(*)` sample-mode SQL. Two gated `@pytest.mark.snowflake` live e2e tests, certified against a real warehouse — a warehouse+prune-only run (engineered table, always-passes drop) and a full `generate`-pipeline run against `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`, both at `safety: schema-only` + `prune.scope: full` (the combination that works on live Snowflake today; sample-mode is deferred, see Fixed/Known). A consolidated "Snowflake adapter (v0.2)" section in `docs/warehouse-adapter-ops.md` (profile keys, dialect, session lifecycle, sampling, estimate, error taxonomy, cost guidance, known limitations, running the gated tests).

### Fixed

- **Snowflake adapter case-folding (#124).** `SnowflakeAdapter._quote` (and `_get_num_rows`'s database prefix) now fold identifiers to UPPER before quoting — byte-identical to the prune compiler — so a conventionally-cased (dbt-lowercased) identifier resolves against the real upper-folded Snowflake object, and a materialised temp table the adapter CREATEs matches the name the compiler REFERENCEs. Surfaced by the gated live materialised path.
- **Snowflake `run_test_sql` capture-failures parsing (#124).** The `ARRAY_AGG(OBJECT_CONSTRUCT(*))` sample column comes back from the connector as a JSON-string VARIANT (not a Python list); `run_test_sql` now `json.loads`-es it before building sample-failure dicts. Surfaced by the gated live full-scope prune.

> **Known (live Snowflake, v0.2):** sample-mode prune (`oneshot` / `materialised`), `safety: sample`, and `safety: aggregate-only` are not yet functional — use `safety: schema-only` + `prune.scope: full`. Tracked as follow-up bugs (`HASH(*)` projection-subquery sample shape; vendor-neutral `get_table_metadata` seam; Snowflake `column_stats`).

## [0.2.0] — 2026-05-21

Adds external-test ingestion and a no-LLM prune path, migrates dev tooling to uv, widens the supported Python range to 3.11–3.13, and publishes the docs site.

### Added

- **`signalforge.ingest`** — `read_schema(schema, model, *, project_dir=None) -> IngestResult` parses an externally-authored dbt `schema.yml` (hand-written, dbt-codegen, dbt Copilot, DinoAI, …) into the typed `CandidateSchema` the prune engine consumes, so SignalForge can prune any generator's tests, not just its own LLM drafts. Supported dbt test types (`not_null`, `unique`, `accepted_values`, `relationships`) map directly; everything else is skip-and-recorded. Stale column references fail loud via `IngestAnchorContractError`. (#104)
- **`signalforge prune-existing <model> --schema <path>`** — operator-facing CLI subcommand running ingest → prune → diff with no LLM call. Point it at an existing dbt `schema.yml` and the warehouse tells you which tests add no signal. Read-only by design; renders a diff of what to remove plus a `.signalforge/diff.json` sidecar. (#105)

### Changed

- Dev tooling migrated to **uv** (`uv sync --dev`, committed `uv.lock`); CI Python matrix widened to **3.11 / 3.12 / 3.13**. `pip install -e ".[dev]"` still works. (#95, #96)
- Python **3.13 compatibility** for the path-safety layer — the symlink-loop guards now handle 3.13's `OSError(ELOOP)` resolution change across all three canonicalisation sites. (#109)
- Documentation site published at https://wjduenow.github.io/SignalForge/ via MkDocs Material, redeployed on every push to `main`. (#97)

### Fixed

- Silenced the pydantic `UserWarning` emitted for the deliberate `LLMRequest.schema` field-name shadow, scoped to the class definition (no global filter mutation). (#93)

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

[Unreleased]: https://github.com/wjduenow/SignalForge/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/wjduenow/SignalForge/releases/tag/v0.2.0
[0.1.0]: https://github.com/wjduenow/SignalForge/releases/tag/v0.1.0
