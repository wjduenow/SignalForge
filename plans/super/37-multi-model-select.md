# #37 — cli: add `--select` for multi-model batch + multi-model cookbook

## Meta

- **Ticket:** https://github.com/wjduenow/SignalForge/issues/37
- **Phase:** devolved (beads created; ready for Ralph)
- **PR:** https://github.com/wjduenow/SignalForge/pull/66
- **Branch / worktree:** `feature/37-multi-model-select` at `../worktrees/SignalForge/37-multi-model-select`
- **Labels:** enhancement, cli, docs
- **Priority:** HIGH (codebase review 2026-05-11, functional finding #3)

## Ticket summary

`signalforge generate` is single-model only. The roadmap defers project-wide drift detection to v0.2 — but there's no documented stopgap for the "I have 50 models" user. The ticket has two phases:

- **Phase A (docs only):** add `## Running across many models` section to `docs/cli-ops.md` documenting the shell-loop pattern (`find models -name '*.sql' | xargs -n1 -P4 signalforge generate`) with parallelism caveats (per-adapter `_active_session_id`, prompt-cache implications) and anti-pattern callouts.
- **Phase B (code):** add `--select <expr>` to `signalforge generate` accepting dbt-style selectors (`tag:staging`, `path:models/marts/*`, bare `<unique_id>`). Sequential in v0.2; parallel deferred to v0.3. Aggregated summary line at end.

## AC (verbatim from ticket)

- [ ] `docs/cli-ops.md` ships a `## Running across many models` section with shell-loop pattern + parallelism caveats + prompt-cache implications.
- [ ] `--select <expr>` flag supports `tag:`, `path:`, and bare unique_id forms.
- [ ] Positional `<model>` arg keeps current behaviour.
- [ ] Aggregated summary line at end of multi-model run.
- [ ] New test exercises `--select tag:foo` against a multi-model fixture.
- [ ] `signalforge.yml` per-stage config remains scoped per-model (no shared mutable state between iterations).

## Discovery

### Codebase scout findings

- **`cmd_generate` orchestration** (`src/signalforge/cli/generate.py:386–695`) is sequential and state-isolated per call: project_dir → manifest load → `manifest.get_model(args.model)` → profile → adapter → safety → draft → prune → grade → diff → render_to_text. All shared state (manifest, profile, policy) is resolved once per call; every stage takes a model-specific view. Safe to iterate.
- **Manifest layer has no selector surface.** `Manifest.iter_models()` and `Manifest.get_model(key)` exist; no tag-filter, path-glob, or selector helpers. We add them. `Model` carries `unique_id`, `tags: list[str]`, `config.tags: list[str]`, `path: str`, `original_file_path: str` — every field a selector needs is already there.
- **Argparse** (`src/signalforge/cli/__init__.py` `_build_parser`, plus `cli/generate.py add_parser`) — positional `model` arg only; no `--select` exists.
- **Adapter state risk (CRITICAL):** `BigQueryAdapter._active_session_id` / `_session_started_at` / `_session_ttl_seconds` are per-instance, cleaned in `__exit__`. The current `cmd_generate` instantiates one adapter then enters/exits it inside `prune_tests`. For multi-model in-process iteration, we MUST construct a fresh adapter per model — reusing across iterations would leak session_id and elapsed-TTL state across models.
- **Audit/sidecar overwrite risk (CRITICAL):** every default path is project-shared, not per-model:
  - `.signalforge/audit.jsonl` (safety) — append-only, safe.
  - `.signalforge/llm_response.jsonl` (draft) — append-only, safe.
  - `.signalforge/prune.jsonl` (prune) — append-only, safe.
  - `.signalforge/grade.jsonl` (grade) — append-only, safe.
  - `.signalforge/grade.json` (grade sidecar) — **`O_TRUNC` overwrite per run** (locked by `grade-layer.md` DEC-006/012).
  - `.signalforge/diff.json` (diff sidecar) — **`O_TRUNC` overwrite per run** (locked by `diff-renderer.md` DEC-009).
  - So three appendable JSONLs naturally survive iteration; two sidecars get clobbered. This drives the key design decision below.
- **No multi-model fixture exists.** `tests/fixtures/dbt_project_austin/` is single-model. We add a `tests/fixtures/dbt_project_multi/` with ≥2 staging models for the integration test.
- **5-surface parity rule** (`.claude/rules/cli-layer.md`) applies: argparse help + handler docstring + `docs/cli-ops.md` flag reference + test name + DEC in this plan.

### Convention checker — rule constraints that apply

**Hard blockers:**
- `cli-layer.md` DEC-024 — every new exception must land in `_EXCEPTION_TO_EXIT_CODE` (7th AST scan).
- `cli-layer.md` DEC-007/027 — user-supplied paths route through `canonicalise_user_path(raw, project_dir)`; failures re-raise as `CliPathError`.
- `prune-engine.md` DEC-016 — one `PruneEvent` per candidate, fail-closed, even mid-error.
- `safety-layer.md` DEC-011 — never wrap audit-write seam in try/except.
- `testing-signal.md` — strict markers + tmp_path isolation for tests that produce `.signalforge/` artefacts.

**Soft constraints:**
- `cli-layer.md` 5-surface parity for the new flag.
- `prune-engine.md` DEC-017 + grep gate — lazy-format JSON logger across CLI multi-model loop.
- `cli-layer.md` DEC-026 — progress emission TTY-gated; `<fact>` derives from in-scope objects, no hardcoded duration hints.
- `prune-engine.md` DEC-009 — conservative-bias routing on warehouse errors per model.
- `testing-signal.md` — engineered determinism for LLM-driven assertions; multi-surface drift on user-facing argv shapes.

**No `.claude/workflow-project.md` exists** — no extra scoping questions, review areas, or chunking from project workflow.

### Selector grammar — dbt subset

The ticket explicitly asks for three forms; we adopt them verbatim from dbt's grammar:
- `tag:<name>` — matches if `<name>` ∈ `Model.tags ∪ Model.config.tags` (union; dbt collapses these).
- `path:<glob>` — matches if `fnmatch(Model.original_file_path, glob)`. Globs are shell-style (`*`, `?`, `[seq]`), not regex. dbt uses path prefix semantics; we use fnmatch for v0.2 simplicity (matches the ticket's `models/marts/*` example).
- Bare value — if starts with `model.`, treated as `unique_id`; else treated as `original_file_path` (matches existing positional model arg semantics).

dbt's full grammar (intersections via space, exclusions via `--exclude`, graph operators `+` / `n+`, set operators) is **out of scope for v0.2** — explicitly v0.3.

## Scoping decisions (Phase 1)

- **Q1 — sidecars (`grade.json` / `diff.json`):** **last-writer-wins**. Multi-model runs overwrite the two `O_TRUNC` sidecars per iteration; only the final model's sidecars persist on disk. The four append-only JSONLs survive iteration. Document loudly in `cli-ops.md` and in the `--select` help text. Pairs naturally with Phase A's shell-loop cookbook (operator who wants per-model sidecars uses xargs with per-call cwd).
- **Q2 — PR shape:** Phase A + B ship in **one PR**. Cookbook covers both the shell-loop (process-level parallelism, prompt-cache implications, BQ session-id isolation across processes) AND `--select` (in-process v0.2 sequential path).
- **Q3 — error handling:** **continue, max() exit code**. Per-model exit codes collected; failed models named in the aggregated summary with their tier + exception class; final exit code = `max(per_model_exit_codes)` across the four-tier taxonomy.
- **Q4 — selector grammar:** **comma-separated multi-expression**. Single expression OR `<expr1>,<expr2>,...,<exprN>` taking the union. Atoms: `tag:<name>`, `path:<glob>`, or bare value (routes to existing positional semantics — `model.` prefix → unique_id, else file path). dbt's space-separated convention diverges; documented. Intersections / exclusions / graph operators deferred to v0.3.

## Architecture review

| Area | Rating | Findings |
| --- | --- | --- |
| Security | **pass** | Path-glob applies fnmatch to `Model.original_file_path` strings already in the manifest (no filesystem traversal at selector time; manifest paths are canonicalised at `manifest.load` per `manifest-readers.md`). Tag is string compare. Bare value routes to existing `get_model` which already validates. Comma-parsing must strip whitespace and reject empty atoms; trivially testable. |
| Performance | **concern** | (a) Sequential N-model loop: acceptable per ticket (parallel deferred to v0.3); each model is full pipeline ~10-30s. (b) Adapter recreated per model: adds ~100-500ms BQ client init per iteration — necessary to avoid `_active_session_id` bleed. (c) Anthropic prompt cache: system prompt + rubric blocks are stable across models (high cache-hit; document in cookbook). Dynamic block (`<MODEL_SQL>` + neighbours) changes per model (cache miss on that block — expected). (d) **Unbounded cost risk:** `--select tag:staging` against a 200-model project could rack significant warehouse + LLM spend. v0.1 has per-call `cost_limit_bytes` (BQ) but no batch-level cap. See refinement Q3. |
| Data model | **pass** | No schema, migration, or config-shape changes. Selector lives in CLI args only; `PruneConfig` / `DraftConfig` / etc. unchanged. |
| API design | **concern** | (a) Positional `<model>` + `--select` interaction: mutex (require exactly one). v0.2-cli-friendly precedent: argparse `add_mutually_exclusive_group(required=True)`. (b) New error class for selector parse failures + zero-match (likely `CliSelectorParseError(CliInputError)` and `CliSelectorNoMatchError(CliInputError)` — both tier 2). Must land in `_EXCEPTION_TO_EXIT_CODE` per `cli-layer.md` DEC-024 (7th AST scan). (c) Selector validation: `tag:` requires non-empty name; `path:` requires non-empty glob; bare value non-empty. (d) Comma-parsing — `--select ` (empty) and `--select ,tag:foo` (empty atom) reject loudly. |
| Observability | **concern** | (a) Per-model progress shape: prefix existing stage progress with `[i/N] <model_unique_id>` header line before each model. (b) Aggregated summary: where does it go? Decision needed (refinement Q1) — stderr (matches "metadata" framing; rendered diffs stay on stdout) vs stdout (operator's primary artifact). (c) Logging: one `_LOGGER.info` at batch start with selector text + matched-model count (lazy-format JSON; grep gate). (d) `--quiet` / `--verbose` semantics across batch: `--quiet` suppresses per-model progress AND batch progress; `--verbose` enables both. Aggregated summary line at end always emits (even under `--quiet`) when ≥1 model failed. |
| Testing | **pass-with-action** | (a) Need `tests/fixtures/dbt_project_multi/` with ≥2 models bearing varied tags + paths. (b) Engineered determinism: same trick as e2e (`tests/fixtures/dbt_project_austin/`) — at least one model with an always-passes column to pin a `dropped` outcome. (c) Selector parser: pure unit tests, no fixtures. (d) Integration test exercising `--select tag:foo` against the multi-fixture (in-process `main()`, not real warehouse — uses fakes). (e) Mutex test: positional + `--select` both supplied → argparse rejects with usage; either absent → argparse rejects. (f) Backward-compat test: positional `<model>` still routes to single-model path. |

**No blockers.** All concerns route to refinement questions below.

## Refinement log

### Decisions

- **DEC-001 — Selector grammar.** `--select <expr>` where `<expr>` is `<atom>[,<atom>]*`. An atom is one of:
  - `tag:<name>` — non-empty `<name>`; matches if `<name> ∈ (set(Model.tags) ∪ set(Model.config.tags))`.
  - `path:<glob>` — non-empty `<glob>`; matches if `fnmatch(Model.original_file_path, <glob>)`.
  - bare `<value>` — non-empty; if `<value>.startswith("model.")` route as `unique_id`; else route as `original_file_path` (mirrors existing positional `<model>` semantics, which `Manifest.get_model` already implements).
  - Multi-expression is union (set-OR). Whitespace around the comma is stripped. Empty atoms (`--select ` or `--select ,tag:foo`) raise `CliSelectorParseError`. Match results are deduplicated by `unique_id` and ordered by `unique_id` lexicographic sort (deterministic for the integration test + summary).
  - dbt's space-separated convention diverges; documented in `--help` and `cli-ops.md`.

- **DEC-002 — Positional and `--select` are mutex.** `parser.add_mutually_exclusive_group(required=True)` on the `generate` subparser. Argparse renders the standard mutex error if both / neither are supplied. The positional `<model>` retains exact v0.1 single-model semantics.

- **DEC-003 — Sidecars last-writer-wins.** `.signalforge/grade.json` and `.signalforge/diff.json` are `O_TRUNC` overwrite per `cmd_generate` call (locked by `grade-layer.md` DEC-006/012 and `diff-renderer.md` DEC-009). Multi-model in-process iteration overwrites these per model; only the final model's sidecars persist. Documented in `cli-ops.md`'s `## Running across many models` section AND in the `--select` help string with one line. The four append-only JSONLs (`safety.audit.jsonl`, `llm_response.jsonl`, `prune.jsonl`, `grade.jsonl`) survive iteration. Operators wanting per-model sidecars use the shell-loop pattern (Phase A cookbook), one process per model.

- **DEC-004 — Continue on per-model failure; `exit_code = max(per_model_exit_codes)`.** Each model in the batch is wrapped in its own `try/except Exception` (mirrors the per-handler boundary catch from `cli-layer.md` DEC-016). Failed models accumulate `(model_unique_id, exit_code, exception_class_name)` tuples. After the batch loop, `exit_code` for the whole run is `max(per_model_exit_codes, default=0)` across the four-tier taxonomy (0 < 1 < 2 < 3 — the tier integer is also the severity rank, conveniently). Failures named in the summary (DEC-009). No traceback ever leaks per `cli-layer.md` DEC-016.

- **DEC-005 — Aggregated summary → stderr.** Stdout carries the rendered diffs in invocation order (one per model); stderr carries the summary. The summary always emits when (a) ≥2 models matched, OR (b) ≥1 model failed in any case. Format (locked verbatim by `test_batch_summary_shape_to_stderr`):
  ```
  Generated <K> kept / <L> dropped / <J> flagged across <M> models in <T>s
  ```
  Plus, when any model failed:
  ```
  <N> models failed:
    - <model_unique_id>        exit <code>  (<ExceptionClass>)
    - ...
  ```
  Failed-model list capped at 50 entries; overflow renders `  ... and <K> more` (DEC-009).

- **DEC-006 — Zero-match is tier 2 (input-validation).** A well-formed selector resolving to zero models raises `CliSelectorNoMatchError(CliInputError)`. Stderr shape: `ERROR: --select '<expr>' matched zero models in this project` + remediation `Check the selector syntax with 'signalforge generate --help' or verify your dbt project has models matching the criteria.` Maps to exit 2. Mirrors `ModelNotFoundError`'s tier (the bare positional case for an unknown model).

- **DEC-007 — Two new CLI errors registered at tier 2.** `CliSelectorParseError(CliInputError)` and `CliSelectorNoMatchError(CliInputError)` both land in `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` at tier 2. 7th AST scan (`tests/test_audit_completeness.py`) gates this — adding the error class without the table entry breaks the test loudly.

- **DEC-008 — No batch-level cost cap in v0.2.** DraftConfig per-call retries / token budgets and BQ `maximum_bytes_billed` already cap per-call cost. Cumulative cost is the operator's responsibility; cookbook calls this out loudly and recommends previewing match count via a no-op (`signalforge lint` will route through, OR a future `signalforge select --dry-run` is the v0.3 add). `cli.max_models_per_run` config field reserved as a v0.3 forward-compat surface in the rule file but NOT shipped here.

- **DEC-009 — Failed-model list always named in summary, cap 50.** No flag to suppress. The four-tier taxonomy is what tooling keys on; the prose list is for the human operator. Truncation at 50 with `  ... and <K> more` line for runaway-batch sanity. Test pins the cap.

- **DEC-010 — Fresh `BigQueryAdapter` per model.** The batch driver constructs `adapter = _make_warehouse_adapter(profile)` inside the per-model loop, not once at batch start. Prevents `_active_session_id` / `_session_started_at` / `_session_ttl_seconds` bleed across iterations. Adds ~100-500ms BQ client init per model — acceptable trade-off vs. state-corruption risk (`warehouse-adapters.md` DEC-002 of #22 generalised).

- **DEC-011 — Phase A + B ship in ONE PR.** Cookbook and `--select` flag co-released. The cookbook references `--select` as the in-process alternative to the shell-loop. The 5-surface parity rule (`cli-layer.md`) is easier to satisfy in one PR than across two.

- **DEC-012 — Manifest selector helpers live in `signalforge.manifest.select`.** New module exposing two public symbols: `parse_selector(expr: str) -> tuple[SelectorAtom, ...]` and `select_models(manifest: Manifest, expr: str) -> tuple[Model, ...]`. Plus the typed `SelectorAtom` discriminated-union value object (`TagAtom`, `PathAtom`, `BareAtom` — Pydantic v2 frozen with `extra="forbid"`). New error: `SelectorParseError(ManifestError)` raised by `parse_selector` on syntactic problems. Zero-match is NOT a manifest-layer error; `select_models` returns an empty tuple. The CLI layer converts empty to `CliSelectorNoMatchError`, and parse-errors to `CliSelectorParseError` (with `cause=...`).

- **DEC-013 — Multi-model fixture at `tests/fixtures/dbt_project_multi/`.** Three models: `models/staging/stg_a.sql` (`tags: ['staging']`), `models/staging/stg_b.sql` (`tags: ['staging']`), `models/marts/fct_x.sql` (`tags: ['marts']`). At least one model carries an engineered always-passes column (`'literal' AS source`) so the always-pass drop path is mathematically guaranteed. Hand-crafted `target/manifest.json` + `regenerate.sh`. Paired loads test in `tests/manifest/test_multi_fixture_loads.py`. Mirrors `tests/fixtures/dbt_project_austin/` precedent (per `testing-signal.md` end-to-end gated tests section).

- **DEC-014 — Per-model progress prefix `[i/N] <model_unique_id>`.** When the batch driver runs (`--select` matched ≥1 model), each per-model iteration emits one stderr line `[i/N] <model_unique_id>` before that model's existing stage progress fires. Same TTY-gating rules as `cli-layer.md` DEC-026 — TTY default, `--quiet` suppresses, `--verbose` forces. Single-model path (positional `<model>`) does NOT emit the prefix (preserves v0.1 output shape).

- **DEC-015 — Anthropic prompt cache across in-process iterations.** Cached block in the drafter is system prompt + neighbours (per `llm-drafter.md` DEC-009). Across N in-process iterations, the system-prompt portion is byte-stable; the dynamic block (`<MODEL_SQL>` + per-model neighbours) varies. So cache hits land on the system-prompt cache marker after model 1; per-model dynamic content is a cache miss (expected — that's the model under draft). Documented in the cookbook's "parallelism caveats" subsection: parallel shell-loop processes each have separate cache state; in-process `--select` shares it.

- **DEC-016 — fnmatch for `path:`, not regex or dbt path-prefix.** `path:<glob>` uses Python's `fnmatch.fnmatchcase` against `Model.original_file_path`. Globs are shell-style (`*`, `?`, `[seq]`). dbt uses a path-prefix-with-implicit-wildcard convention which is more complex; v0.2 picks the simpler form and documents it. Operators write `path:models/staging/*` (matches the ticket's literal example) rather than dbt's `path:models/staging`. Migration to dbt-compat semantics is a v0.3 ask.

- **DEC-017 — 5-surface parity test for `--select`.** New test `tests/cli/test_5_surface_parity_select.py` reads (a) the argparse help string for `--select`, (b) the cookbook section of `docs/cli-ops.md`, (c) the relevant DEC bullet from this plan file, (d) the test name itself, and asserts the example selectors (`tag:staging`, `path:models/marts/*`) appear consistently across all surfaces. Mirrors `cli-layer.md`'s 5-surface rule mechanically. Not a generic test — bespoke for this flag; future flags get their own copy or extend this one.

### Session notes

Session 1 (2026-05-11): Discovery + architecture + refinement complete. Single session.

## Detailed breakdown

Stories ordered by architectural layer (bottom-up: manifest → CLI errors → orchestrator refactor → flag wiring → summary → fixtures → integration tests → docs → quality gate → patterns).

Validation command for every AC: `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`.

---

### US-001 — Manifest selector module (`signalforge.manifest.select`)

**Description:** New module exposing `parse_selector(expr)` and `select_models(manifest, expr)` plus the typed `SelectorAtom` discriminated union. Pure manifest-layer code; no warehouse / LLM contact. Pairs with new `SelectorParseError(ManifestError)`.

**Traces to:** DEC-001 (grammar), DEC-012 (module location).

**Acceptance Criteria:**
- `signalforge/manifest/select.py` exists. Exports `SelectorAtom` (discriminated union: `TagAtom(kind=Literal["tag"], name)`, `PathAtom(kind=Literal["path"], glob)`, `BareAtom(kind=Literal["bare"], value)`). Frozen Pydantic v2 with `extra="forbid"`.
- `parse_selector(expr: str) -> tuple[SelectorAtom, ...]` splits on comma, strips whitespace, classifies each atom, rejects empty atoms / empty tag / empty glob / empty bare with `SelectorParseError` (carries `remediation`).
- `select_models(manifest, expr) -> tuple[Model, ...]` returns matched models deduped by `unique_id`, sorted by `unique_id`. Tag match: union of `Model.tags` and `Model.config.tags`. Path match: `fnmatch.fnmatchcase(Model.original_file_path, glob)`. Bare with `model.` prefix: matches `unique_id`. Bare without: matches `original_file_path` exact.
- `SelectorParseError(ManifestError)` with `default_remediation`.
- Re-exported from `signalforge.manifest.__init__`: `parse_selector`, `select_models`, `SelectorAtom`, `SelectorParseError`.
- Public-API test pins exports.
- Validation passes.

**Done when:** New module + tests merged, ruff/pyright/pytest pass.

**Files:**
- `src/signalforge/manifest/select.py` (new)
- `src/signalforge/manifest/errors.py` (add `SelectorParseError`)
- `src/signalforge/manifest/__init__.py` (re-exports)
- `tests/manifest/test_select.py` (new — unit tests below)

**Depends on:** none.

**TDD:**
- `test_parse_selector_single_tag` — `"tag:staging"` → `(TagAtom(name="staging"),)`.
- `test_parse_selector_single_path` — `"path:models/marts/*"` → `(PathAtom(glob="models/marts/*"),)`.
- `test_parse_selector_bare_unique_id` — `"model.proj.x"` → `(BareAtom(value="model.proj.x"),)`.
- `test_parse_selector_bare_filepath` — `"models/x.sql"` → `(BareAtom(value="models/x.sql"),)`.
- `test_parse_selector_multi_expression_union` — `"tag:staging,path:models/marts/*"` → tuple of two atoms.
- `test_parse_selector_strips_whitespace` — `" tag:staging , path:models/marts/* "` → 2 atoms; no whitespace in payloads.
- `test_parse_selector_rejects_empty_atom` — `",tag:foo"` and `"tag:foo,"` and `""` → `SelectorParseError`.
- `test_parse_selector_rejects_empty_tag` — `"tag:"` → `SelectorParseError`.
- `test_parse_selector_rejects_empty_path` — `"path:"` → `SelectorParseError`.
- `test_select_models_tag_match_union_of_tags_and_config_tags` — fixture model with tag in `Model.tags` and another with tag in `Model.config.tags`; both match.
- `test_select_models_path_glob_match` — `path:models/staging/*` matches staging models only.
- `test_select_models_bare_routes_unique_id_for_model_prefix` — round-trip with `get_model`.
- `test_select_models_multi_expression_union_is_deduped` — overlapping atoms; result has unique models.
- `test_select_models_ordered_by_unique_id` — output is sorted.
- `test_select_models_zero_match_returns_empty_tuple` — `tag:nonexistent` → `()` (CLI layer raises, not manifest).

**Rules applied:** `manifest-readers.md` (Pydantic v2 frozen / `extra="forbid"` for config-shaped atoms — atoms are read-back-as-input-typed, treat as forbid for typo safety), `testing-signal.md` (no-assert-True, strict-markers).

---

### US-002 — CLI error classes for selector failures

**Description:** Add `CliSelectorParseError` and `CliSelectorNoMatchError` under `signalforge.cli.errors`; register both in `_EXCEPTION_TO_EXIT_CODE` at tier 2. Both subclass `CliInputError` so the existing exception ladder catches them. AST audit scan covers the registration.

**Traces to:** DEC-007 (two new error classes + tier).

**Acceptance Criteria:**
- `CliSelectorParseError(CliInputError)` — accepts `expr: str, cause: SelectorParseError | None = None` kwargs. `__init__` derives the message from `cause` when provided; otherwise uses `expr`. Default remediation pinned.
- `CliSelectorNoMatchError(CliInputError)` — accepts `expr: str` kwarg. Message: `f"--select {expr!r} matched zero models in this project"`. Default remediation: `"Check the selector syntax with 'signalforge generate --help' or verify your dbt project has models matching the criteria."`
- Both registered in `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` at tier 2.
- 7th AST scan (`tests/test_audit_completeness.py::test_every_typed_error_is_in_exit_code_mapping_table`) discovers them and passes.
- `tests/cli/test_exit_codes.py` parametrized table extended with both error classes → exit 2.
- `format_error_to_stderr(exc)` renders `ERROR: <message>` + `  ↳ Remediation: <text>`.
- Validation passes.

**Done when:** Errors land, registered, AST scan passes, exit-code parametrized test extended.

**Files:**
- `src/signalforge/cli/errors.py` (extend)
- `src/signalforge/cli/_helpers.py` (extend `_EXCEPTION_TO_EXIT_CODE`)
- `tests/cli/test_exit_codes.py` (extend table)
- `tests/cli/test_format_error_to_stderr.py` (extend — verify both shape)

**Depends on:** US-001.

**TDD:**
- `test_cli_selector_parse_error_in_exit_code_table` — registered at tier 2.
- `test_cli_selector_no_match_error_in_exit_code_table` — registered at tier 2.
- `test_cli_selector_parse_error_stderr_shape` — `format_error_to_stderr` renders the two-line ERROR+Remediation shape.
- `test_cli_selector_no_match_error_stderr_shape` — same.
- AST scan test (`test_every_typed_error_is_in_exit_code_mapping_table`) re-runs cleanly post-change.

**Rules applied:** `cli-layer.md` DEC-008 (stderr shape), DEC-024 (7th AST scan).

---

### US-003 — Refactor `cmd_generate` into single-model core + batch driver

**Description:** Extract the existing single-model pipeline body from `cmd_generate` into a private `_run_single_model(...)` helper that returns `_SingleModelOutcome` (rendered text + per-model exit code + counts + exception info if it failed). `cmd_generate` becomes a thin dispatcher: if `args.select` is set, route to the batch driver `_run_batch(...)` which iterates `select_models(...)` and calls `_run_single_model` per model with a fresh adapter. Otherwise, single-model path as today. No behavioural change for the single-model case yet — flag wiring lands in US-004.

**Traces to:** DEC-010 (fresh adapter), DEC-011 (single PR).

**Acceptance Criteria:**
- `_run_single_model(model, manifest, profile, args, *, batch_index=None, batch_count=None, project_dir) -> _SingleModelOutcome` exists. `batch_index` / `batch_count` drive the `[i/N]` progress prefix when both non-None (DEC-014).
- Each call to `_run_single_model` constructs its own `BigQueryAdapter` via `_make_warehouse_adapter(profile)` (DEC-010). Single-model path also constructs adapter inside this helper — keeps single source of truth.
- `_run_batch(manifest, profile, args, *, project_dir) -> _BatchOutcome` exists; iterates matched models from `select_models(manifest, args.select)`; wraps each `_run_single_model` call in its own `try/except Exception` (per `cli-layer.md` DEC-016 boundary semantics); accumulates per-model outcomes; returns `_BatchOutcome` with `per_model: tuple[_SingleModelOutcome, ...]` + `total_exit_code: int = max(...)`.
- Empty matched set raises `CliSelectorNoMatchError(expr=args.select)` BEFORE any model iteration.
- Single-model `cmd_generate` (positional `<model>`) is unchanged in observable behaviour — same stage order, same audit shape, same stdout output. Test `test_generate_calls_stages_in_documented_order` still passes.
- Validation passes.

**Done when:** Refactor compiles, all existing CLI tests pass unchanged, new helpers covered by unit tests via in-process `main(["generate", ...])`.

**Files:**
- `src/signalforge/cli/generate.py` (refactor)
- `tests/cli/test_generate.py` (extend with refactor-preservation tests)

**Depends on:** US-001, US-002.

**TDD:**
- `test_single_model_path_unchanged_post_refactor` — positional `<model>` exercises identical stage sequence and audit shape as before.
- `test_batch_driver_fresh_adapter_per_model` — fake adapter factory records call count; batch with N matched models calls factory N times (not once).
- `test_batch_driver_continues_after_per_model_failure` — first model raises `LLMRateLimitError`; second and third complete; total `_BatchOutcome.total_exit_code == max(3, 0, 0) == 3`.
- `test_batch_driver_zero_match_raises_cli_selector_no_match` — selector matching zero models raises before any model iteration.

**Rules applied:** `cli-layer.md` DEC-009 (subpackage layout), DEC-016 (per-handler boundary catch), `warehouse-adapters.md` DEC-002-of-#22 generalised (fresh adapter per logical run for stateful adapters).

---

### US-004 — argparse `--select` flag + mutex with positional `<model>`

**Description:** Wire `--select <expr>` into the `generate` subparser. Positional `<model>` + `--select` become a `mutually_exclusive_group(required=True)`. Help text pins the grammar with examples. Handler routes to the batch driver when `args.select` is set.

**Traces to:** DEC-001 (grammar examples in help), DEC-002 (mutex), DEC-011 (single PR), DEC-016 (`fnmatch` documented), DEC-017 (5-surface parity setup).

**Acceptance Criteria:**
- argparse `mutually_exclusive_group(required=True)` containing the positional `model` AND `--select`. Both supplied → argparse error (exit 2 from argparse). Neither → argparse error.
- `--select` help text includes:
  - One-line grammar description.
  - Three concrete examples (`tag:staging`, `path:models/marts/*`, `tag:staging,path:models/marts/*`).
  - One-line caveat: `Multi-model runs overwrite .signalforge/grade.json and .signalforge/diff.json per model; only the last model's sidecars persist. Use the shell-loop pattern (docs/cli-ops.md § Running across many models) for per-model sidecars.`
- `cmd_generate` dispatcher: when `args.select`, calls `_run_batch(...)` and surfaces `CliSelectorParseError(cause=...)` if `select_models` raises `SelectorParseError`; otherwise routes through `_run_single_model` once with `args.model`.
- Backward compat: every existing test using positional `<model>` passes unchanged.
- Validation passes.

**Done when:** Flag wired, mutex enforced, parse-error path returns exit 2 with the documented stderr shape, no-match path returns exit 2.

**Files:**
- `src/signalforge/cli/generate.py` (`add_parser`, `cmd_generate`)
- `tests/cli/test_generate_select_flag.py` (new)

**Depends on:** US-003.

**TDD:**
- `test_positional_model_alone_works_unchanged` — backward compat.
- `test_select_flag_alone_routes_to_batch_driver` — `main(["generate", "--select", "tag:staging", "--project-dir", str(fixture)])` resolves and runs.
- `test_positional_and_select_mutex_argparse_error` — both → exit 2, stderr names usage.
- `test_neither_positional_nor_select_argparse_error` — neither → exit 2.
- `test_select_parse_failure_returns_exit_2_with_cli_selector_parse_error` — `"--select tag:"` → exit 2, `CliSelectorParseError` stderr shape.
- `test_select_zero_match_returns_exit_2_with_cli_selector_no_match` — `"--select tag:nonexistent"` → exit 2, `CliSelectorNoMatchError` stderr shape.

**Rules applied:** `cli-layer.md` DEC-008 (stderr shape), DEC-017 (5-surface parity setup), `manifest-readers.md` (path safety via existing `canonicalise_user_path`).

---

### US-005 — Aggregated summary + per-model progress prefix

**Description:** Add `format_batch_summary(outcome: _BatchOutcome) -> str` to `signalforge.cli._helpers`; emit via stderr at the end of `_run_batch`. Add `[i/N] <model_unique_id>` stderr line at the head of each `_run_single_model` call inside the batch path. Both gated by `should_emit_progress` (TTY + `--quiet`/`--verbose`).

**Traces to:** DEC-005 (summary format + sink), DEC-009 (failure list shape), DEC-014 (progress prefix).

**Acceptance Criteria:**
- `format_batch_summary(outcome)` returns the locked format from DEC-005. Single line headline; conditional failure block when ≥1 failure; first 50 failed models listed; overflow `  ... and <K> more`.
- Summary always emits on stderr at end of `_run_batch` when (a) ≥2 models matched OR (b) ≥1 model failed. Summary suppressed when (single-matched-model run AND zero failures) — preserves clean single-model UX when `--select` happens to resolve to one model.
- `[i/N] <model_unique_id>` stderr line emits at the head of each `_run_single_model` call inside `_run_batch`. TTY-gated. `--quiet` suppresses. `--verbose` forces.
- Single-model path (positional `<model>`) emits NEITHER the prefix NOR the summary (preserves v0.1 output shape).
- Validation passes.

**Done when:** Format pinned by test; emission paths gated correctly; backward compat preserved.

**Files:**
- `src/signalforge/cli/_helpers.py` (extend with `format_batch_summary` and `emit_batch_progress_entry`)
- `src/signalforge/cli/generate.py` (wire emission into `_run_batch`)
- `tests/cli/test_batch_summary.py` (new)
- `tests/cli/test_batch_progress.py` (new)

**Depends on:** US-003.

**TDD:**
- `test_format_batch_summary_headline_shape` — exact-string match for `K kept / L dropped / J flagged across M models in Ts`.
- `test_format_batch_summary_no_failures_omits_failure_block` — no failure section when all 0.
- `test_format_batch_summary_names_failures_with_tier_and_class` — pin format.
- `test_format_batch_summary_truncates_failure_list_at_50` — 100 failures → first 50 named + `  ... and 50 more`.
- `test_batch_summary_emits_to_stderr` — capsys check.
- `test_batch_summary_suppressed_for_single_matched_zero_failures` — `--select` resolving to one model with success: no summary.
- `test_batch_progress_prefix_emits_to_stderr_under_tty` — fake TTY; `[1/3]` etc. shows.
- `test_batch_progress_prefix_suppressed_under_quiet` — `--quiet` removes prefix.
- `test_batch_progress_prefix_absent_in_single_model_path` — positional path: no prefix.

**Rules applied:** `cli-layer.md` DEC-026 (progress UX, TTY-gating).

---

### US-006 — Multi-model test fixture (`dbt_project_multi/`)

**Description:** New fixture with three models bearing varied tags and paths. Hand-crafted `target/manifest.json` + `regenerate.sh`. Engineered determinism on at least one model (always-passes column). Paired loads test.

**Traces to:** DEC-013 (fixture shape).

**Acceptance Criteria:**
- `tests/fixtures/dbt_project_multi/` exists with:
  - `dbt_project.yml`
  - `signalforge.yml` (mirrors `dbt_project_austin/` shape — `safety.mode: aggregate-only`, `prune.enabled: false` to keep the integration test fast and offline; LLM seam stubbed by fakes in US-007).
  - `models/staging/stg_a.sql` — `tags: ['staging']`, has an engineered literal column (`'austin' AS source`) for the always-passes drop guarantee.
  - `models/staging/stg_b.sql` — `tags: ['staging']`, varied column shape.
  - `models/marts/fct_x.sql` — `tags: ['marts']`.
  - `target/manifest.json` hand-crafted, validates via `signalforge.manifest.load(...)`.
  - `regenerate.sh` mirrors `dbt_project_austin/regenerate.sh`.
- `tests/manifest/test_multi_fixture_loads.py` ships in the same commit; in-process `load(...)` + assertions on the three models' tags + paths + unique_ids. No env vars required.
- Validation passes.

**Done when:** Fixture lands, loads test passes, regenerate.sh documented as maintainer-only.

**Files:**
- `tests/fixtures/dbt_project_multi/dbt_project.yml` (new)
- `tests/fixtures/dbt_project_multi/signalforge.yml` (new)
- `tests/fixtures/dbt_project_multi/models/staging/stg_a.sql` (new)
- `tests/fixtures/dbt_project_multi/models/staging/stg_b.sql` (new)
- `tests/fixtures/dbt_project_multi/models/marts/fct_x.sql` (new)
- `tests/fixtures/dbt_project_multi/target/manifest.json` (new, hand-crafted)
- `tests/fixtures/dbt_project_multi/regenerate.sh` (new)
- `tests/manifest/test_multi_fixture_loads.py` (new)

**Depends on:** none (parallel with US-001/2/3/4/5).

**TDD:**
- `test_dbt_project_multi_loads` — `manifest.load(fixture_dir)` returns three models.
- `test_dbt_project_multi_has_engineered_always_passes_column` — at least one model has a known literal column.
- `test_dbt_project_multi_tag_distribution` — pin tags so US-007 integration tests are stable.

**Rules applied:** `testing-signal.md` (fixture regeneration via uvx, engineered determinism, no `tests/__init__.py`).

---

### US-007 — CLI integration tests for `--select`

**Description:** Exercise the full batch driver against the multi-model fixture via in-process `main([...])`. Cover grammar, mutex, zero-match, multi-expression union, partial failure, summary shape. LLM and warehouse interaction stubbed via existing fakes (no env vars).

**Traces to:** DEC-001, DEC-002, DEC-004, DEC-005, DEC-006, DEC-007, DEC-009, DEC-017.

**Acceptance Criteria:**
- All assertions pin observable behaviour: stderr shape, stdout shape, exit code, `.signalforge/` JSONL append behaviour, sidecar last-writer-wins behaviour.
- Each test uses `tmp_path` fixture isolation (copy fixture before invocation) per `testing-signal.md` DEC-008.
- `test_5_surface_parity_for_select_flag` reads (a) argparse help, (b) `docs/cli-ops.md`, (c) this plan's DEC-017 bullet, asserts example selectors appear in each.
- Validation passes.

**Done when:** All tests below pass; integration coverage of the new flag is end-to-end.

**Files:**
- `tests/cli/test_select_integration.py` (new)
- `tests/cli/test_5_surface_parity_select.py` (new)
- `tests/cli/_select_helpers.py` (new — fakes / fixture-copy helper)

**Depends on:** US-001 through US-006.

**TDD:**
- `test_select_tag_routes_to_batch` — `--select tag:staging` matches 2 models; main() returns 0; stdout has 2 rendered diffs in unique_id order.
- `test_select_path_glob` — `--select path:models/staging/*` matches 2 staging models.
- `test_select_multi_expression_union` — `--select tag:staging,tag:marts` matches all three; deduped by unique_id.
- `test_select_bare_unique_id_routes_to_batch_with_single_match` — `--select model.proj.stg_a` runs one model via the batch driver (because args.select is set), emits summary because ≥1 failure OR ≥2 models is false here — so no summary. Backward shape preserved aside from batch driver path.
- `test_positional_model_still_works` — backward compat.
- `test_positional_and_select_mutex_argparse_error_exits_2` — argparse error exit code.
- `test_select_zero_match_exits_2_with_cli_selector_no_match_error` — stderr shape.
- `test_select_parse_failure_exits_2_with_cli_selector_parse_error` — `tag:` empty → exit 2.
- `test_batch_partial_failure_collects_max_exit_code` — patch one model to fail at draft tier-3; assert batch exit code = 3; stderr summary names that model with `(LLMRateLimitError)`.
- `test_batch_summary_shape_to_stderr` — DEC-005 format match.
- `test_batch_progress_prefix_emits_under_tty` — capsys + monkeypatched isatty.
- `test_batch_quiet_suppresses_progress_but_emits_summary_on_failure` — `--quiet` + one failure → no prefix; summary still emits.
- `test_5_surface_parity_for_select_flag` — DEC-017 mechanical check.
- `test_sidecar_last_writer_wins_across_batch` — run 3-model batch; assert only the last model's `.signalforge/diff.json` and `.signalforge/grade.json` persist.

**Rules applied:** `cli-layer.md` (every contract surface), `testing-signal.md` (engineered determinism, multi-surface drift, tmp_path isolation), `prune-engine.md` (audit JSONL append survives iteration).

---

### US-008 — Docs: cookbook + flag reference

**Description:** Add `## Running across many models` section to `docs/cli-ops.md`. Document `--select` under the flag-reference section. Update README or top-level orientation if needed.

**Traces to:** DEC-005, DEC-010, DEC-011, DEC-015, DEC-017 (5-surface parity).

**Acceptance Criteria:**
- `docs/cli-ops.md` ships a `## Running across many models` section with subsections:
  - **In-process: `--select <expr>`** — grammar, three examples, semantics (sequential, fresh adapter per model, prompt-cache survival across iterations on the system-prompt portion only), sidecar last-writer-wins caveat.
  - **Process-level: shell-loop pattern** — `find models -name '*.sql' | xargs -n1 -P4 signalforge generate --project-dir <DIR>` (or equivalent). Parallelism caveats: per-process `BigQueryAdapter` session isolation (safe); separate Anthropic prompt cache per process (cost trade-off — each process pays cache-creation tokens); sidecars overwrite per process unless the operator uses per-call `--project-dir` overlays or per-process cwd. Anti-pattern callout: don't share `.signalforge/` across concurrent processes if you care about which model "owns" the surviving sidecars.
  - **Cumulative cost** — operators are responsible; recommend matching N models before commit; recommend `--max-bytes-billed` (already exists) for per-call BQ cap.
- `--select` appears in the `## Flag reference` section with the same grammar + caveats.
- The README's `## Trying it out` (or `## Quick start`) section adds one-line cross-link to the new section.
- 5-surface parity test (US-007) passes against the new docs.
- Validation passes.

**Done when:** Docs land, cross-references intact, 5-surface parity test passes.

**Files:**
- `docs/cli-ops.md` (extend)
- `README.md` (one-line cross-link)

**Depends on:** US-001 through US-006 (docs reference shipped behaviour).

**Rules applied:** `cli-layer.md` (5-surface parity), `testing-signal.md` (multi-surface drift defence — test US-007's parity check binds this).

---

### US-009 — Quality Gate

**Description:** Run code reviewer 4 times across the full changeset, fixing all real bugs found each pass. Run CodeRabbit if available. Project validation passes after all fixes.

**Traces to:** All DECs.

**Acceptance Criteria:**
- Four code-review passes complete; every real bug fixed (cosmetic-only findings are documented as not-fix-this-pass, not silently dropped).
- CodeRabbit review (if available) run; findings triaged.
- Final `ruff check . && ruff format --check . && pyright && pytest` passes.
- `pytest -m cli_subprocess --no-cov` passes (subprocess gated smoke).
- 7th AST scan in `tests/test_audit_completeness.py` passes (new errors registered).
- Logger grep gate (`tests/llm/test_logger_grep_gate.py`) passes (any new `_LOGGER` calls use lazy-format).
- No traceback leaks from any new path under `--verbose=False`.

**Done when:** All passes complete; canonical validation green; gated markers green.

**Files:** (review-driven; any in the changeset)

**Depends on:** US-001 through US-008.

---

### US-010 — Patterns & Memory

**Description:** Update `.claude/rules/cli-layer.md` with the new patterns learned: (a) multi-model batch driver shape, (b) DEC-010 fresh-adapter-per-model rule for stateful adapters generalised, (c) DEC-005 batch-summary format pinning, (d) DEC-014 progress prefix shape, (e) DEC-017 5-surface parity test recipe. Update `.claude/rules/manifest-readers.md` if the selector module introduces a pattern worth pinning (it does: typed atom discriminated union for user-facing string grammar).

**Traces to:** Codifying DEC-001 through DEC-017 into rules for future work.

**Acceptance Criteria:**
- `cli-layer.md` gains a `## Multi-model batch driver pattern (issue #37, v0.2)` section with:
  - Batch driver vs. single-model dispatcher shape.
  - Fresh adapter per model rule (cross-link to `warehouse-adapters.md` DEC-002-of-#22 generalisation).
  - Aggregated summary stderr emission + format-pinning test.
  - Per-model progress prefix `[i/N] <unique_id>` TTY-gated.
  - 5-surface parity test recipe (mechanical, per-flag).
  - Sidecar last-writer-wins documented constraint + cookbook escape hatch.
- `manifest-readers.md` gains a `## User-facing string-grammar selectors (issue #37)` short section: discriminated-union typed atoms; `parse_<grammar>(expr) -> tuple[Atom, ...]` + `select_<entity>(manifest, expr) -> tuple[Entity, ...]` pattern.
- `CLAUDE.md` v0.2 additions section updated with the new public-API exports (`parse_selector`, `select_models`, `SelectorAtom`, `SelectorParseError`, `CliSelectorParseError`, `CliSelectorNoMatchError`).
- Validation passes.

**Done when:** Rules + CLAUDE.md updated; this plan file finalised with all surfaces cross-linked.

**Files:**
- `.claude/rules/cli-layer.md` (extend)
- `.claude/rules/manifest-readers.md` (extend)
- `CLAUDE.md` (extend `## Public API surface (v0.1 + v0.2 additions)` and the milestone bullet)

**Depends on:** US-009.

---

## Beads manifest

- **Epic:** `bd_1-scaffolding-4v1` — #37: cli --select for multi-model batch + cookbook
- **Tasks:**
  - `bd_1-scaffolding-4v1.1` — US-001: Manifest selector module (`signalforge.manifest.select`) — *ready*
  - `bd_1-scaffolding-4v1.2` — US-002: CLI error classes for selector failures — deps: US-001
  - `bd_1-scaffolding-4v1.3` — US-003: Refactor cmd_generate — extract _run_single_model + add _run_batch — deps: US-001, US-002
  - `bd_1-scaffolding-4v1.4` — US-004: argparse --select flag + mutex with positional <model> — deps: US-003
  - `bd_1-scaffolding-4v1.5` — US-005: Aggregated batch summary + per-model progress prefix — deps: US-003
  - `bd_1-scaffolding-4v1.6` — US-006: Multi-model test fixture (`dbt_project_multi/`) — *ready*
  - `bd_1-scaffolding-4v1.7` — US-007: CLI integration tests for --select — deps: US-001..US-006
  - `bd_1-scaffolding-4v1.8` — US-008: Docs — cookbook section + --select flag reference — deps: US-001..US-006
  - `bd_1-scaffolding-4v1.9` — Quality Gate — code review x4 + validation — deps: US-001..US-008
  - `bd_1-scaffolding-4v1.10` — Patterns & Memory — update rules + CLAUDE.md — deps: Quality Gate
- **Worktree:** `../worktrees/SignalForge/37-multi-model-select` on `feature/37-multi-model-select`.

### Next steps

1. Run Ralph: `/ralph-run`
2. Monitor: `bd list --status=in_progress`
3. When done: `/closeout`
