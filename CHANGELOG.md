# Changelog

All notable changes to SignalForge are documented here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [PEP 440](https://peps.python.org/pep-0440/) for version numbering.

## [Unreleased]

_Nothing yet — entries land here on `dev` and get promoted to a dated section at release time._

## [0.4.0] — 2026-05-30

### Added

- **Column-type awareness for the drafter (#159).** `signalforge.manifest.load(project_dir)` now auto-merges column types from a sibling `target/catalog.json` (produced by `dbt docs generate`) into `Column.data_type` on the in-memory `Manifest`. The drafter's prompt — cached manifest summary AND dynamic data-section schema — both already rendered `data_type` when present; populating it from catalog.json closes the dbt-parse-only gap so cooperative LLMs see real warehouse types (`INT64`, `STRING`, `TIMESTAMP`, …) instead of `UNKNOWN`. No CLI flag, no config knob — pure sibling auto-discovery; missing or malformed catalog degrades silently. Case-insensitive column matching (`lower(col_name)`) handles Snowflake's uppercase / BigQuery's preserve / Postgres's lowercase identifier conventions without configuration.
- **OpenAI as a grading + drafting provider (#136).** Set `grade.provider: openai` or `llm.provider: openai` in `signalforge.yml`; requires the `[openai]` install extra and `OPENAI_API_KEY`. Ships four pricing SKUs (`gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4-turbo`); `--estimate` works via tiktoken (no extra API round-trip). Server-side JSON enforcement via `response_format={"type": "json_object"}`. v0.3 ships without prompt caching (no Anthropic-style cache discount); follow-up to evaluate OpenAI prompt caching.
- **Google Gemini as a grading + drafting provider (#137).** Set `grade.provider: gemini` or `llm.provider: gemini` in `signalforge.yml`; requires the `[gemini]` install extra (`pip install signalforge-dbt[gemini]`) and `GOOGLE_API_KEY`. Recommended SKU for both drafter and judge is `gemini-2.5-flash` (also registered: `gemini-2.5-pro`, `gemini-2.0-flash`). Server-side JSON enforcement via `response_mime_type="application/json"`. `--estimate` cost-preview is wired through Gemini's native `client.models.count_tokens` (US-007 of #137; DEC-016) — first-party token counter, one extra API round-trip per estimate, comparable to the Anthropic shape. Ships **without prompt caching** in v0.3 — `LLMProvider` strategy reports `supports_prompt_caching=False` / `supports_token_count=False`, so `call_llm` skips the `cache_control` marker, the `extended-cache-ttl` beta header, and the pre-send `count_tokens` gate; budget per-call cost accordingly under `provider: gemini` (especially for the grader's 4-criterion × ~12-artifact fan-out). Explicit Gemini context caching is tracked as a follow-up.

### Fixed

- **Drafter rejects type-incoherent `custom_sql` business-rule tests at parse time (#159).** `_validate_anchor_contract` gains a sqlglot AST type-coherence check: for each `custom_sql` candidate, parse the SQL via `sqlglot.parse_one(dialect="bigquery")`, walk binary comparison nodes (`<>`, `=`, `<`, `>`, `<=`, `>=`), look each operand's column name up in the model's `Column.data_type` map, and for the two declared type strings test compatibility via sqlglot's `TypeAnnotator.COERCES_TO` table (bidirectional). When both types are known and incompatible (e.g. `INT64` vs `STRING`) a violation is appended; otherwise the check skips silently. Note the mechanism: the schema map is the lookup, NOT a `schema=` kwarg fed to sqlglot's annotator. The check is the parser-side belt-and-braces of a dual-defence with the type-aware prompt (catalog.json merge above); violations join the existing `LLMOutputAnchorContractError.violations` tuple — no new error class. Skip-when-uncertain policy: only bare `Column <op> Column` is flagged; `CAST` / `SAFE_CAST` / `COALESCE` / `IFNULL` / function calls / subqueries / literals / `NULL` / window functions / unknown-type columns / parse errors all skip silently (zero false-positives on legitimate SQL is the contract; the prune engine's `kept-without-evidence` routing remains the safety net). sqlglot promoted from a dev-only transitive to a runtime dep, pinned at `sqlglot>=30,<31` in `[project].dependencies`.
- **`--estimate` grader-side token counts no longer double-count the rubric (#136 US-008 QG).** The pre-US-005 inline Anthropic call passed the rubric in BOTH the `system=` kwarg AND embedded in the cached user-content block, counting it twice per criterion. The first QG fix preserved that for Anthropic byte-identity, which then triple-counted the rubric for OpenAI (system→`system + text` concat → rubric prefix in text). Corrected to match the runtime grader call: rubric in `system=` once, artifact envelope in user content. Real-API `--estimate` figures for the grade-side shift down by ~one rubric per criterion (was: bug → over-report; now: matches what gets billed). Fake-driven byte-identity golden unchanged (canned token counts are call-shape-agnostic).
- **`estimate(...)` engine parameter renamed `anthropic_client` → `client` (#136 US-008 QG).** Post-US-005 the slot was already typed `object | None` and forwarded verbatim to whichever provider strategy is active; the old name implied Anthropic-only and would mislead a future #137 Gemini wiring. CLI in `generate.py` already passed `None` for non-Anthropic providers; the rename surfaces that without behaviour change.

## [0.3.0] — 2026-05-27

### Added

- **Snowflake warehouse adapter (epic #118 — #119–#124, #130).** The second concrete `WarehouseAdapter`, graduating the ABC + factory seam through a real vendor (Architectural Commitment #3 — warehouse-agnostic by design). `WarehouseAdapter.from_profile` dispatches `type: snowflake` dbt profiles via a unified `DbtProfileTarget` with a per-type cross-field validator and `validate_snowflake_account` (#120); the `snowflake-connector-python` shim is confined to `adapters/_snowflake_client.py` (one-shim-per-vendor, #119). The prune compiler emits valid Snowflake SQL purely from `SNOWFLAKE_DIALECT` — `quote_char='"'`, `identifier_case='upper'` (fold-then-quote so dbt-lowercased identifiers resolve against upper-folded objects), per-component qualified-name quoting, the quoted `"sample"` CTE alias, and `'…'::TIMESTAMP` partition literals — never branching on dialect name (#121). Deterministic `sample_rows` (HASH-mod) + `materialise_sample` (session-scoped `CREATE TEMPORARY TABLE`) with connection-bound session state and fail-soft `__exit__` cleanup (#122). `estimate_query_bytes` runs `EXPLAIN USING JSON` and parses `GlobalStats.bytesAssigned`, raising the new typed `EstimateUnavailableError` when the plan carries no figure (#123 degrade-first → #130 real estimate; planner-estimate accuracy caveat documented). Full `map_snowflake_exception` taxonomy (reusing existing typed errors), a fakesnow + sqlglot offline harness, two gated `@pytest.mark.snowflake` live e2e tests against `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`, and a consolidated `docs/warehouse-adapter-ops.md` § "Snowflake adapter (v0.2)" (#124).
- **Custom business-rule tests (`custom_sql`) — the 5th dbt test type (#116).** A full singular-test `SELECT` (returns failing rows), drafted from `meta.signalforge.business_rules` (natural-language, column- or model-level) or LLM inference, then pruned and graded like the four built-ins — an always-pass business rule is dropped, not shipped. Bounded `{{ this }}` / `ref()` / `source()` Jinja resolution (no Jinja engine; control-flow rejected loudly), dialect-driven compilation, and emission as proposed `tests/*.sql` files via `generate --write [--force]` with a `-- signalforge:generated` ownership marker that never overwrites hand-authored files. `prune-existing --tests-dir` ingests and prunes existing singular tests alongside `schema.yml` tests.
- **Snowflake e2e contributor docs (#138).** Setup guide for the gated Snowflake tests (`SF_RUN_SNOWFLAKE=1` + connection env vars, resource-monitor/XS-warehouse cost guidance), an `.env.example`, and a README pointer.

### Fixed

- **Snowflake sample-mode SQL — projection-subquery shape (#139).** `HASH(*)` is valid only in a `SELECT` projection on Snowflake (rejected as a `WHERE`/`ORDER BY` predicate, error 002079). Added structural `Dialect.sample_hash_in_projection` + `sample_hash_alias` and a shared `warehouse/_sample_sql.render_sample_select`, so the adapter's `sample_rows`/`materialise_sample` and the prune compiler's sample CTE all emit `SELECT * EXCLUDE (…) FROM (SELECT t.*, ABS(HASH(*)) AS … ) WHERE MOD(…) < 1 ORDER BY …`. Fixes `prune.sample_strategy: materialised` on live Snowflake; BigQuery's inline form is byte-unchanged.
- **Vendor-neutral sample-bucket row count (#140).** New `WarehouseAdapter.get_row_count` ABC seam (concrete-default `RowCountNotSupportedError`; BigQuery + Snowflake implementations) replaces a BigQuery-only `_get_client` crack in `prune.engine`, so `prune.sample_strategy: oneshot` sample-mode works on any adapter that implements the seam. Fixes `oneshot` sample-bucket sizing on Snowflake.
- **Drafter/grader tolerate a JSON prose preamble (#144).** `claude-sonnet-4-6` reproducibly narrates before the JSON object on the business-rules path, and the model rejects an assistant-turn prefill (HTTP 400 "does not support assistant message prefill"). New `signalforge._common.json_payload.extract_json_payload` strips a leading preamble — decoding at the first `{`/`[` only and returning the text unchanged on failure, so truncated/garbage responses still fail loud — applied in both `draft.parser` and `grade.parser`.
- **Snowflake adapter case-folding (#124).** `SnowflakeAdapter._quote` (and `_get_num_rows`'s database prefix) now fold identifiers to UPPER before quoting — byte-identical to the prune compiler — so a conventionally-cased (dbt-lowercased) identifier resolves against the real upper-folded Snowflake object, and a materialised temp table the adapter CREATEs matches the name the compiler REFERENCEs. Surfaced by the gated live materialised path.
- **Snowflake `run_test_sql` capture-failures parsing (#124).** The `ARRAY_AGG(OBJECT_CONSTRUCT(*))` sample column comes back from the connector as a JSON-string VARIANT (not a Python list); `run_test_sql` now `json.loads`-es it before building sample-failure dicts. Surfaced by the gated live full-scope prune.

> **Known (live Snowflake):** `safety: aggregate-only` (Snowflake `column_stats`) is not yet implemented. Every other combination is functional — `safety: schema-only` / `safety: sample`, with `prune.scope: full` or `prune.scope: sample` under both `prune.sample_strategy: materialised` (#139) and `oneshot` (#140).

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

[Unreleased]: https://github.com/wjduenow/SignalForge/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/wjduenow/SignalForge/releases/tag/v0.4.0
[0.3.0]: https://github.com/wjduenow/SignalForge/releases/tag/v0.3.0
[0.2.0]: https://github.com/wjduenow/SignalForge/releases/tag/v0.2.0
[0.1.0]: https://github.com/wjduenow/SignalForge/releases/tag/v0.1.0
