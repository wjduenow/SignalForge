# Ingest layer (external dbt schema.yml → CandidateSchema reader)

Established by issue #104 (ingest external dbt tests). Apply to every module under `signalforge.ingest` and to any new code that parses an externally-authored dbt `schema.yml` into the typed `CandidateSchema` the prune engine consumes.

The ingest layer is a **stage-0 reader** (same class as `signalforge.manifest`): deterministic YAML-to-typed-objects, no warehouse calls, no LLM calls, no fail-closed audit writer. It exists so SignalForge can prune tests authored *anywhere* — a hand-written `schema.yml`, dbt-codegen output, dbt Copilot, DinoAI — not just its own LLM drafts. The product story: **point SignalForge at your existing dbt tests and let the warehouse tell you which ones add no signal** (extends Architectural Commitment #1, "signal over volume").

## Public surface

`signalforge.ingest.read_schema(schema, model, *, project_dir=None) -> IngestResult`. Mirrors the adjacent-stage signature convention (data + model front-paired positionally; keyword-only optionals after `*`; `project_dir` for path resolution — same shape as `prune_tests` / `render_diff` / `grade_artifacts`). Returns a typed `IngestResult(candidate: CandidateSchema, skipped: tuple[SkippedTest, ...])`.

Public names re-exported from `signalforge.ingest`: `read_schema`, `IngestResult`, `SkippedTest`, `SkipReason`, and the `IngestError` hierarchy. Internal helpers (`parse_test_entry`, `validate_anchor_contract`, the `_`-prefixed reader internals) are NOT in the public `__all__` — they are consumed only by `read_schema`.

## `str` is raw YAML; `Path` is a file (DEC, the input contract)

`read_schema`'s `schema: str | Path` parameter is disambiguated by type, not by sniffing the value:

- **`Path`** → a file. Canonicalise via `signalforge._common.path_safety.canonicalise_path(schema, project_dir)` (project_dir defaults to the file's parent when `None`); catch `PathContainmentError` and re-raise as `IngestSchemaParseError`. A missing file → `IngestSchemaNotFoundError`.
- **`str`** → raw YAML content. No file read, no path canonicalisation.

Document this in every docstring. The CLI fast-follow (#105) always passes a `Path`. Do NOT add path-sniffing heuristics to a `str` ("does it look like a path?") — that re-introduces the ambiguity this contract removes.

## Manifest-reader conventions apply verbatim

This layer is governed by `manifest-readers.md`. The load-bearing obligations:

- **Pydantic v2 `frozen=True, extra="ignore"`** on production read/result models (`IngestResult`, `SkippedTest`). Forward-compat for upstream schema additions.
- **No drift detector here.** The standard `extra="forbid"` strict-mirror + fixture pattern is mandatory ONLY for models read back *from disk* (JSONL audits / sidecars). `IngestResult` / `SkippedTest` are produced in-process and handed straight to prune — never serialised and re-read — so no drift detector is required. `tests/ingest/test_models.py` documents this explicitly so a future reviewer doesn't add a spurious one.
- **Errors carry `remediation`.** Every `Ingest*Error` subclasses `IngestError` (base renders message + `↳ Remediation:` line via `__str__`); user-supplied strings (paths, model names) render through the repr-safe `_format_value` so a crafted name can't inject ANSI / newlines into a log viewer.
- **NO logging / metrics in any ingest module.** Stage-0 readers are silent; observability lives in the stage that *consumes* the data (prune). Zero `logging` / `_LOGGER` / `print`. The logger grep-gate does not even need to scan this package — there is nothing to scan.

## Safe YAML load with size cap before parse (DEC-005)

Mirror the diff renderer's `existing_schema` defence: check `len(content_bytes) <= _INGEST_SCHEMA_SIZE_LIMIT_BYTES` (5 MB, same order of magnitude as diff) BEFORE calling `yaml.safe_load`, so a billion-laughs / deep-nesting payload never reaches the parser. Oversize raises `IngestSchemaTooLargeError(size, limit)` before any parse. `yaml.safe_load` ONLY — never `yaml.load` or a custom `Loader=`.

## Fail loud on missing columns; skip-and-record everything we can't model

Two distinct dispositions, deliberately different:

- **Anchor contract — fail loud (DEC-002, DEC-007).** A test referencing a column absent from the `Model` means the YAML is stale/wrong vs. the manifest. `validate_anchor_contract(candidate, frozenset(model.columns.keys()))` collects EVERY violation (column-name not on model; per-column test `column` ≠ parent; test `column` not on model; model-level test `column` not on model) and raises one `IngestAnchorContractError(violations=...)` listing all of them — never short-circuits. **Re-implemented in-layer**, NOT imported from `draft.parser._validate_anchor_contract`: the draft validator raises the LLM-typed `LLMOutputAnchorContractError`, which would surface an LLM-taxonomy error from a non-LLM path and create a cross-layer import. The bodies are small; duplication beats the taxonomy smell.
- **Unsupported tests — skip and record (DEC-003).** Anything that isn't one of the four supported types (`not_null`, `unique`, `accepted_values`, `relationships`) is dropped into `IngestResult.skipped` as a structured `SkippedTest`, never failed loud. Generators emit custom / dbt-expectations / namespaced tests we can't evaluate yet; tolerating them is the point.

`SkipReason` is a closed `Literal` of exactly three values: `"unsupported-test-type"` (a bare string we don't model), `"custom-or-generic-test"` (a namespaced / dotted / dict-bodied custom test like `dbt_utils.*` / `dbt_expectations.*`), `"malformed-supported-test"` (a supported type whose required args are missing/empty — e.g. `accepted_values` with no `values`, `relationships` missing `to`/`field`, OR a supported type used at model level where `column` would be empty). Adding a fourth reason is a contract change — extend the `Literal` and the docs in lockstep.

## dbt syntax tolerance (DEC-006, DEC-008, DEC-009)

The parser (`parse_test_entry`) is the compatibility surface across dbt versions:

- **Both `tests:` and `data_tests:` keys** are accepted and **unioned** (dbt 1.8 renamed `tests:` → `data_tests:`; dbt itself only *warns* on the legacy key, so we don't fail loud). Identical tests appearing under both keys **dedupe** by `(type, column, sorted-args)` — the args are SORTED so two `accepted_values` with the same value set in different order collapse to one.
- **Inline args AND `arguments:`-nested args** (dbt 1.8+) are both read for `accepted_values` (`values`) and `relationships` (`to` / `field`).
- **Config keys are stripped** before the required-arg check: `config`, `severity`, `where`, `name`, `tags`, `error_if`, `warn_if`, `store_failures`, `limit`. They must not be mistaken for args.
- **`ref()` / `source()` unwrap** in `relationships.to`: `ref('m')` → `m`, `ref('pkg','m')` → `m` (last positional), `source('s','t')` → `s.t`; an unrecognised string is carried verbatim. Bounded string parse — NO Jinja engine, NO new dependency.
- **Identifier validation is deferred to prune.** The reader carries raw identifier strings (column names, `relationships.to`) into the `CandidateSchema` unchanged; `signalforge.prune` runs `_sql_safety.validate_identifier` before quoting (prune-engine.md DEC-024). The reader must NOT build SQL or import `from google.cloud import bigquery` — warehouse-agnostic by construction.
- **A supported test type at model level is not representable** — all four `CandidateTest` subtypes require a non-empty `column`. `parse_test_entry(..., column=None)` for a supported type routes to `SkippedTest(reason="malformed-supported-test")` rather than building a `CandidateTest(column="")` (which would raise an un-typed Pydantic `ValidationError` out of `read_schema`).

## Exit-code lockstep — the 11th `errors.py` (cli-layer.md)

`signalforge.ingest.errors` is the eleventh per-stage `errors.py`. When it landed (US-001):

- Every concrete registered in `_EXCEPTION_TO_EXIT_CODE`: `IngestSchemaNotFoundError` / `IngestSchemaParseError` / `IngestSchemaTooLargeError` → tier 1 (load/parse); `IngestModelNotFoundError` / `IngestAnchorContractError` → tier 2 (input-validation).
- `IngestError` added to `_EXCEPTION_MAPPING_EXCLUDED_BASES`. Like `DemoError`, the concretes span tiers 1 and 2, so the base gets **no** single fallback-tier entry in `_EXCEPTION_TO_EXIT_CODE` — it lives only in the excluded set. (Contrast the nine single-tier bases that DO get a dual-registration fallback.)
- `test_scan_7_discovers_every_per_stage_errors_module` count bumped 10 → 11.

## `prune-existing` CLI subcommand (shipped — issue #105)

Issue #104 shipped the **library seam only**; the operator-facing `signalforge prune-existing <model> --schema <path>` subcommand (ingest → prune → diff, **no LLM**) shipped in fast-follow [#105](https://github.com/wjduenow/SignalForge/issues/105). Design: `plans/super/105-prune-existing-cli.md` (DEC-001 … DEC-010). Three findings from #105 worth carrying forward:

- **No bespoke `CliPruneExisting*` wrappers (#105 DEC-006).** The five `IngestError` concretes were registered in `_EXCEPTION_TO_EXIT_CODE` by #104 specifically so #105 inherits the taxonomy with zero rework — the CLI handler's single `try/except Exception` boundary (`format_error_to_stderr` + `map_exception_to_exit_code` MRO walk) handles them directly. This *deviates* from cli-layer.md's library-surface wrap pattern (which `init-demo` follows): when a lib's typed errors are already first-class in the exit-code table and already carry remediation, per-class CLI wrappers are ceremony. Wrap only when the CLI adds value (distinct remediation, homogeneity that the boundary catch doesn't already provide).
- **`--mode` is inert on a no-LLM path (#105 DEC-002).** `prune_tests` never consumes the `SafetyPolicy`; the safety sampling mode shapes the LLM payload, which `prune-existing` never builds. The relevant warehouse knobs are prune's `--scope` / `--sample-strategy`. Don't copy `generate`'s flag set wholesale onto a stage that skips the LLM — audit each flag against what the path actually consumes.
- **Read-only by design (#105 DEC-003/DEC-004).** No `--write` (the `--schema` file is hand-authored; overwriting it is surprising). The diff prints to stdout + a `.signalforge/diff.json` sidecar (default-on; `--dry-run` suppresses it). The external schema.yml is fed to `render_diff(existing_schema=...)` so the unified diff shows what to *remove* from the operator's real file; `grading_report=None` → kept/kept-uncertain/dropped, never `flagged`.

The bare-name model resolver was hoisted to `signalforge.cli._helpers._resolve_model_by_key` (cli-layer.md § "Bare-name model resolution") so `prune-existing` and `lint` share it. The empty-candidate case (every external test skip-recorded → zero candidates) short-circuits in `prune.engine` BEFORE `with adapter:` so an all-unsupported schema.yml incurs no warehouse cost (see `prune-engine.md`).

## Reference

`plans/super/104-ingest-external-tests.md` — DEC-001 … DEC-011. `src/signalforge/ingest/` — current implementation. `docs/ingest-ops.md` — operational reference. `tests/ingest/` — test suite (`test_parser.py` variant matrix, `test_anchor.py` collect-all, `test_reader.py` orchestrator + the disabled-prune acceptance check, `test_models.py` with the no-drift-detector note). `tests/fixtures/ingest/schema_codegen_shaped.yml` — dbt-codegen-shaped fixture. See-Also: `manifest-readers.md` (the reader precedent), `prune-engine.md` (what `prune_tests` accepts), `cli-layer.md` (exit-code lockstep, the deferred CLI).

## Manifest-compiled-SQL source: `read_manifest_tests` (issue #154)

A SECOND ingest source alongside `read_schema` (schema.yml) and `read_test_files`
(`tests/*.sql`): `signalforge.ingest.read_manifest_tests(manifest, model, *, project_dir=None)
-> IngestResult` reads dbt-compiled test nodes (`Manifest.tests`, see `manifest-readers.md`
§ GenericTest) and turns each into a prunable candidate. It closes the un-graded half of the
prune gate for dbt-expectations / dbt-utils / in-house generic tests (Architectural Commitment #1).

- **Reuses `CandidateTestCustomSQL`, no 7th variant (DEC-001).** A row-returning + deterministic
  test with populated `compiled_code` becomes a model-level `CandidateTestCustomSQL(sql=compiled_code,
  column=None, rationale=<synthesized>, from_manifest=True)`. The `from_manifest` marker
  (`Field(default=False, exclude=True)`) distinguishes ingested-from-drafted downstream WITHOUT a
  schema/audit bump (`exclude=True` keeps `candidate_hash` byte-identical — guard that invariant
  with an explicit test; a mutation dropping it passed the whole suite at QG time).
- **Four-gate classification, all reusing `signalforge.ingest._compiled_sql` (sqlglot-AST):**
  presence (`compiled_code` non-null) → `is_row_returning` (skip bare-scalar `SELECT COUNT(*)`;
  DEC-004) → `is_deterministic_sql` (skip `TABLESAMPLE`/`RAND`/`CURRENT_TIMESTAMP`/…; DEC-012) →
  `validate_ingested_sql` (comment-tolerant safety scan; DEC-013). **Regex/substring is unsafe
  here — a column named `random_id` false-positives; use AST.** NOTE: dbt-expectations wraps
  EVERY macro (incl. `expect_table_row_count_to_be_between`) in a row-returning `validation_errors`
  shell, so those ARE prunable. **Post-#267 the row-returning gate no longer skip-records a bare
  count-of-rows scalar:** `is_prunable_count_scalar` (single top-level bare `exp.Count`, no GROUP BY;
  incl. `COUNT(DISTINCT)`) graduates it to a `from_manifest` candidate (carrying `compiled_code`
  verbatim — stage-0 builds no SQL), and the prune compiler restructures it to a failing-rows form
  (pure f-string, no sqlglot in the compiler). Only a NON-count scalar (`AVG`/`SUM`/`MIN`/`MAX`,
  multi-aggregate, arithmetic-on-count) still skip-records. Classify-at-ingest (bool) / restructure-at-
  compiler is forced by the stage-0 no-SQL-building rule.
- **`SkipReason` stays the closed 3-value Literal (DEC-014).** No-`compiled_code` / non-count aggregate /
  non-deterministic / unparseable → an existing reason (`custom-or-generic-test` /
  `malformed-supported-test`), never a 4th. When EVERY associated node lacks `compiled_code`, emit
  ONE summary `SkippedTest` with a "run `dbt compile`" remediation — the AC's "not a silent skip"
  surface (soft, never a hard abort).
- **Envelope-safe rationale (DEC-011).** The synthesized rationale (macro name + arg summary) is set
  at frozen-model CONSTRUCTION (never mutated) and stripped of `</ARTIFACT>` (+ the `<\s*/ARTIFACT>`
  whitespace-split variant) so a hostile macro arg can't fail-close the whole grade run. The macro
  identity rides on the rationale → the diff `why` cascade surfaces it (DEC-015).
- **Stage-0 preserved.** No logging, no warehouse/LLM calls, no SQL building, no `bigquery` import.
  The `_compiled_sql` helpers are self-contained (they do NOT import the warehouse `_strip_string_literals`
  private — a QG fix; a stage-0 reader must not couple to another layer's `_`-internal).
