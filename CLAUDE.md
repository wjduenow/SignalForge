# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

Pre-alpha. Eight issues shipped:

- **#1 (project scaffolding)** — `pyproject.toml` (Hatchling + src layout), `src/signalforge/__init__.py` with `__version__`, smoke test, ruff + pyright + pytest configs, GitHub Actions CI on PRs into `dev` and pushes to `main`, and `CONTRIBUTING.md`.
- **#2 (manifest loader)** — `signalforge.manifest` subpackage: typed `Manifest` / `Model` (Pydantic v2), `load(project_dir, manifest_path=None) -> Manifest`, single-model resolver by `unique_id` or file path, schema-version tolerance v9–v12, symlink-hardened path canonicalisation, soft 200 MB warning. See `docs/manifest-loader-ops.md` for the operational reference.
- **#3 (BigQuery warehouse adapter)** — `signalforge.warehouse` subpackage: `WarehouseAdapter` ABC + `from_profile` factory, `BigQueryAdapter` (the only v0.1 concrete adapter), `load_profile` for dbt `profiles.yml`, deterministic hash-mod sampling with fail-loud sizing checks, identifier-validation at construction time, `QueryJobConfig` defaults that pin `use_query_cache=False`. See `docs/warehouse-adapter-ops.md` for the operational reference.
- **#4 (PII safety layer)** — `signalforge.safety` subpackage: schema-only-default sampling-mode policy, fail-closed audit JSONL writer (`O_APPEND` + `fsync` + size cap), column-name redaction via stable blake2b-4 hashes (DEC-010), four opt-out signals (column meta, model meta, `tags:[pii]`, `meta.contains_pii`) with documented precedence, AST audit-completeness scan rejecting direct `LLMRequest` construction, drift-detector test for `AuditEvent`. `signalforge.yml` config namespace `{ safety: { ... } }` reserved for the safety layer. See `docs/safety-ops.md` for the operational reference and `.claude/rules/safety-layer.md` for the rules distilled from this ticket.
- **#5 (LLM draft pipeline)** — `signalforge.draft` + `signalforge.llm` subpackages: centralized `call_anthropic` SDK seam (full retry taxonomy via clauditor pattern), `draft_schema(model, adapter, policy, manifest, *, config)` end-to-end drafter, typed `CandidateSchema` + discriminated-union `CandidateTest`, anchor-contract validator (every test references a real column; whole-draft fail-loud), fail-closed `LLMResponseEvent` JSONL response audit adjacent to safety audit, `<MODEL_SQL>` prompt-injection envelope, deterministic `prompt_version` hash. See `docs/draft-ops.md`.
- **#6 (test prune engine)** — `signalforge.prune` subpackage: `prune_tests(model, adapter, candidates, manifest, *, config=None, audit_path=None) -> PruneResult` end-to-end orchestrator. Compiles dbt-style tests (`not_null`, `unique`, `accepted_values`, `relationships`) to BigQuery failing-rows SQL via `Dialect.quote_char`-driven dispatch (so v0.2 Snowflake/Postgres slot in unchanged), routes outcomes through five `DropReason` literals (`always-passes`, `requires-future-data`, `failed-on-known-clean-data`, `kept`, `kept-without-evidence`), enforces a total wall-clock budget (DEC-011), emits a fail-closed `prune.jsonl` audit (mirrors safety / draft fail-closed semantics; AST-gated single construction seam). Trusted-models opt-in via `signalforge.yml prune.trusted_models: [...]`, validated against the manifest at orchestrator entry (DEC-008). See `docs/prune-ops.md` for the operational reference, `plans/super/6-prune-engine.md` for the design, and `.claude/rules/prune-engine.md` for the rules distilled from this ticket.
- **#7 (quality grader)** — `signalforge.grade` subpackage: `grade_artifacts(model, candidate, prune_result, *, rubric=None, config=None, audit_path=None, sidecar_path=None, client=None, project_dir=None) -> GradingReport` end-to-end LLM-as-judge orchestrator. One LLM call per `(artifact × criterion)` pair (DEC-004); per-criterion `score: float ∈ [0.0, 1.0]` + `passed: bool` with graceful degrade (`score=None`) on retry exhaustion / parser failure / budget exhaustion (DEC-015); aggregate `pass_rate` / `mean_score` / `aggregate_complete` flags. Default rubric ships four locked criterion texts (clarity, consistency, rationale, no-redundant — DEC-016) pinned by a golden `rubric_hash`. Whole-run pre-flight `<ARTIFACT>` envelope-breach guard before any LLM call (DEC-008); fail-closed `grade.jsonl` per-call audit + clauditor-style `grade.json` sidecar end-of-run (DEC-006/012), both symlink-hardened at orchestrator entry. Sixth AST audit-completeness scan gates `GradeEvent` construction; logger grep-gate extends to `src/signalforge/grade/`. Single-criterion anchor contract (`returned.criterion_id == sent.criterion_id`) tightens the parser. `prune_result.model_unique_id` boundary check at entry. See `docs/grade-ops.md` for the operational reference, `plans/super/7-quality-grader.md` for the design, and `.claude/rules/grade-layer.md` for the rules distilled from this ticket.
- **#8 (diff renderer)** — `signalforge.diff` subpackage: `render_diff(model, candidate, prune_result, *, grading_report=None, existing_schema=None, config=None, output_path=None, sidecar_path=None, project_dir=None) -> DiffReport` end-to-end orchestrator that wires the canonical YAML emitter (`_emitter`), artifact-id formatter (`_artifact_id`, byte-equal mirror of the grade engine for cross-stage join parity), `JsonRenderer` / `AnsiRenderer` / `MarkdownRenderer` (private under `_renderers`, dispatched by `DiffConfig.render_kind`), the fifth fail-closed sidecar writer (`_sidecar`, `O_TRUNC` single-document overwrite + 10 MB cap), and the unconditional ANSI strip / Markdown HTML-entity escape (`_ansi_safety` / `_markdown_safety`) on every user-content field. Three boundary checks at entry (DEC-002) for `candidate.name` / `prune_result.model_unique_id` / optional `grading_report.model_unique_id`; tier classification (`Literal["kept", "dropped", "flagged"]` — `flagged` only fires when a grading report was provided); `existing_schema` size cap before any `yaml.safe_load` to defend against billion-laughs (DEC-006); Markdown body truncated at the last hunk boundary below 60 KB (DEC-005, fits a GitHub PR comment); three reproducibility hashes (`candidate_hash`, `prune_result_hash`, `grading_report_hash`) on every `DiffReport`; symlink-hardened path canonicalisation at the orchestrator (mirrors grade post-QG fix); model-validator on `DiffConfig` enforces `existing_schema_warn_at_bytes < existing_schema_size_limit_bytes` so DEC-014 is never dead code (post-QG fix); `DiffConfig.sidecar_size_limit_bytes` wired through `render_diff` to the writer's `size_limit_bytes` kwarg (post-QG fix). Logger grep-gate extends to `src/signalforge/diff/` (5 dirs); no new AST scan (no per-render audit-event class). See `docs/diff-ops.md` for the operational reference, `plans/super/8-diff-renderer.md` for the design, and `.claude/rules/diff-renderer.md` for the rules distilled from this ticket.

Design is happening in the open on the `dev` branch; remaining feature work (CLI #9) lands next.

## Public API surface (v0.1)

- `signalforge.manifest.load`, `Manifest`, `Model`, and the `ManifestError` hierarchy. Documented in `docs/manifest-loader-ops.md`.
- `signalforge.warehouse.load_profile`, `DbtProfileTarget`, the `WarehouseAdapter` ABC + `from_profile` factory, the `BigQueryAdapter` concrete, the typed value objects (`Dialect`, `BIGQUERY_DIALECT`, `TableRef`, `PartitionFilter`, `ColumnStats`, `TestResult`), and the `WarehouseError` hierarchy. Documented in `docs/warehouse-adapter-ops.md`.
- `signalforge.safety.load_safety_config`, `SafetyPolicy`, `build_llm_request`, `aggregate_columns`, `redact_rows`, the typed shapes (`SamplingMode`, `RedactionReason`, `RedactionRecord`, `AuditEvent`, `LLMRequest`), and the `SafetyError` hierarchy (10 classes). Documented in `docs/safety-ops.md`.
- `signalforge.llm.call_anthropic`, `LLMResult`, the `LLMError` hierarchy. Documented in `docs/draft-ops.md`.
- `signalforge.draft.draft_schema`, `draft_from_request`, `DraftOutcome`, `CandidateSchema` family, `DraftConfig`, `load_draft_config`, the `DraftError` hierarchy. Documented in `docs/draft-ops.md`.
- `signalforge.prune.prune_tests`, `PruneResult`, `PruneDecision`, `PruneConfig`, `load_prune_config`, `PruneEvent`, the typed literals (`DropReason`, `Scope`), and the `PruneError` hierarchy (six classes). Documented in `docs/prune-ops.md`.
- `signalforge.grade.grade_artifacts`, `GradingReport`, `GradingResult`, `GradeConfig`, `load_grade_config`, `Criterion`, `Rubric`, `GradeThresholds`, `DEFAULT_RUBRIC`, `GradeEvent`, the typed literal (`GradeOutputViolationType`), and the `GradeError` hierarchy (nine classes). Documented in `docs/grade-ops.md`.
- `signalforge.diff.render_diff`, `load_diff_config`, `DiffConfig`, `DiffReport`, `DiffEntry`, the typed literal (`Tier`), and the `DiffError` hierarchy (seven classes). Documented in `docs/diff-ops.md`.

Internals (`_loader_helpers`, `_sql_safety`, `_path_safety`, `_test_result_repr`, `adapters/_client`, `_classify_column`, `_compute_policy_hash`, `_resolve_redact_patterns`, `_emitter`, `_renderers`, `_sidecar`, `_artifact_id`, `_ansi_safety`, `_markdown_safety`, etc.) are `_`-prefixed and not part of the public contract.

## Validation

Canonical validation command for this repo (run locally; CI runs the same four checks):

```bash
pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest
```

Quote the `".[dev]"` — bare `.[dev]` is a glob in zsh.

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
