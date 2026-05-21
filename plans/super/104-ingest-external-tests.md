# Issue #104 — ingest external dbt schema.yml tests into CandidateSchema

## Meta

- **Ticket:** [#104](https://github.com/wjduenow/SignalForge/issues/104)
- **Branch:** `feature/104-ingest-external-tests` (off `dev`)
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/104-ingest-external-tests`
- **Phase:** devolved (PR [#106](https://github.com/wjduenow/SignalForge/pull/106); beads created)
- **Plan author:** Claude Code (Opus 4.7, 1M context)
- **Milestone:** v0.2 (extends Architectural Commitment #1 — "prune any generator's tests, not just our own LLM drafts")
- **Labels:** none on the issue

---

## Ticket summary

`signalforge.prune.prune_tests(model, adapter, candidates: CandidateSchema, manifest, ...)` only consumes a typed `CandidateSchema` produced by SignalForge's own LLM draft layer. There is no path to prune tests authored elsewhere — a hand-written `schema.yml` or output from another generator (dbt-codegen, dbt Copilot, DinoAI, datapilot).

This issue adds a reader/adapter that parses standard dbt `schema.yml` test syntax and emits a `CandidateSchema`. Product story: **point SignalForge at your existing dbt tests and let the warehouse tell you which ones add no signal.** Surfaced concretely while planning the SignalForge vs. dbt-codegen comparison article (`docs/temp/dbt-codegen-comparison-plan.md`, Phase 4).

The four v0.1 supported test types map directly:
- `not_null` → `CandidateTestNotNull`
- `unique` → `CandidateTestUnique`
- `accepted_values` → `CandidateTestAcceptedValues`
- `relationships` → `CandidateTestRelationships`

Tests that don't map (custom / dbt-expectations / singular) are skipped + recorded, not failed loud.

---

## Discovery findings

### CandidateSchema / CandidateTest (`src/signalforge/draft/models.py`)

- `CandidateSchema`: `schema_version: Literal[1] = 1`, `name: str` (non-empty), `description: str` (**required**), `rationale: str | None = None`, `columns: tuple[CandidateColumn, ...]`, `tests: tuple[CandidateTest, ...] = ()`. No factory — constructed via `model_validate(...)`. `frozen=True, extra="ignore"`.
- `CandidateColumn`: `name` (non-empty), `description: str` (required), `rationale: str | None`, `tests: tuple[CandidateTest, ...] = ()`, `meta: dict | None`.
- Four `CandidateTest` subtypes, discriminator `type`:
  - `CandidateTestNotNull`: `column` (non-empty), `rationale?`.
  - `CandidateTestUnique`: `column` (non-empty), `rationale?`.
  - `CandidateTestAcceptedValues`: `column` (non-empty), `values: tuple[str, ...]` (non-empty), `rationale?`.
  - `CandidateTestRelationships`: `column`, `to`, `field` (all non-empty), `rationale?`.
- **Implication:** external YAML often lacks `description`. We must supply a default (e.g. `""`) since `description` is required on `CandidateSchema`/`CandidateColumn`.

### Prune entry (`src/signalforge/prune/engine.py::prune_tests`)

```python
def prune_tests(model, adapter, candidates: CandidateSchema, manifest, *,
                config=None, audit_path=None, project_dir=None) -> PruneResult
```

Takes a **single** `CandidateSchema` for one model. No column-existence validation at entry — deferred to compile time (the compiler routes a bad identifier to `kept-without-evidence` via SQL-safety reject). So our reader's anchor check is additive value, not a prune precondition.

### Reusable anchor-contract validator (`src/signalforge/draft/parser.py:86`)

`_validate_anchor_contract(candidate: CandidateSchema, model_columns: frozenset[str], *, exclude_tests=frozenset()) -> tuple[str, ...]` collects every violation (whole-draft fail-loud), raises `LLMOutputAnchorContractError(violations=...)`. Reusable as-is for column-existence checks — but it lives in the `draft` layer and raises a *draft*-typed error. **Decision needed (DEC):** import-and-reuse vs. mirror in the ingest layer with an ingest-typed error.

### Model columns (`src/signalforge/manifest/models.py`)

`Model.columns: dict[str, Column]`; `model.columns_list` ordered. Column-existence set = `frozenset(model.columns.keys())`.

### Reader precedent (`src/signalforge/manifest/loader.py`, `select.py`)

Pydantic v2 `ConfigDict(frozen=True, extra="ignore", populate_by_name=True)`. Path canonicalisation via `signalforge._common.path_safety.canonicalise_path` (raises neutral `PathContainmentError`, layer wraps to typed error). Public entry returns a typed object. Errors carry `remediation`.

### Subpackage layout

14 subpackages under `src/signalforge/`. Standard shape: `__init__.py` (re-exports + `__all__`), `models.py`, `errors.py`, plus orchestrator modules. The diff renderer's `existing_schema` size-cap-before-`yaml.safe_load` (`diff` layer DEC-006) is the precedent for safe YAML loading.

### dbt schema.yml syntax (input format)

- File: `version: 2` + `models: [{name, description?, columns?: [...]}]`.
- Column-level tests under `tests:` (legacy) or `data_tests:` (dbt 1.8+).
- Test entries are either a bare string (`not_null`, `unique`) or a single-key dict (`accepted_values: {values: [...]}`, `relationships: {to: ref('m'), field: id}`). dbt 1.8+ may nest args under `arguments:`.
- A schema.yml can hold **multiple** models — reader must select the target model by `name`.

### Rules constraints (all `.claude/rules/*.md` consulted; no `workflow-project.md`)

| # | Obligation | Source |
|---|---|---|
| 1 | Pydantic v2 `frozen=True, extra="ignore", populate_by_name=True` in production | manifest-readers.md |
| 2 | Symlink-hardened path canonicalisation (3 traps); use `_common.path_safety` | manifest-readers.md |
| 3 | Errors carry `remediation`; `__str__` renders `↳ Remediation:` | manifest-readers.md |
| 4 | **No logging/metrics in stage-0 reader modules** | manifest-readers.md |
| 5 | Drift detector (`extra="forbid"` strict mirror + fixture) for read-back models | testing-signal.md |
| 6 | New `errors.py` → 11th per-stage module: register concretes in `_EXCEPTION_TO_EXIT_CODE`, add `IngestError` to `_EXCEPTION_MAPPING_EXCLUDED_BASES`, bump `test_scan_7_discovers_every_per_stage_errors_module` count 10→11 | cli-layer.md |
| 7 | Four-tier exit codes (1 load/parse, 2 input-validation, 3 external-dep) | cli-layer.md |
| 8 | CLI library-surface pattern: lib module owns `IngestError`; CLI handler wraps each into `CliPruneExisting*Error` at the boundary | cli-layer.md |
| 9 | New subcommand → 5-surface parity + subprocess `--help` smoke under `@pytest.mark.cli_subprocess` | cli-layer.md |
| 10 | Warehouse-agnostic: no `from google.cloud import bigquery` | prune-engine.md |
| 11 | No `assert True` tests; strict markers; AST scans over regex | testing-signal.md |
| 12 | Canonical validation: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` | CONTRIBUTING.md |

---

## Decisions

### DEC-001 — New `signalforge.ingest` subpackage (not under prune)

Sibling subpackage `src/signalforge/ingest/` with its own `errors.py`. Cleanest seam, reusable by future stages, matches the issue's "small `signalforge.ingest` seam" phrasing. Cost: it's the 11th per-stage `errors.py` (rule #6 lockstep). Rejected: `signalforge.prune.ingest` — muddies prune's single responsibility (compile + run + route) and couples a generic reader to one consumer.

### DEC-002 — Missing column → fail loud, whole-file, collect all

A test referencing a column absent from the `Model` means the YAML is stale or wrong vs. the manifest. Mirror the drafter's anchor-contract spirit: collect every violation across the file, raise one typed `IngestAnchorContractError(violations=...)` listing all. Distinct from *unsupported test types*, which are skip+record (DEC-003). Rationale: a missing column is a correctness error the operator must fix; an unsupported type is an expected, tolerable gap.

### DEC-003 — Unsupported tests → typed `IngestResult` with structured skip records

`read_schema(...)` returns a typed `IngestResult` carrying `candidate: CandidateSchema` + `skipped: tuple[SkippedTest, ...]`. Each `SkippedTest` records `test_name: str`, `column: str | None` (None for model-level), `reason: Literal[...]`. Supports the explainability commitment and the future CLI's "N tests skipped, here's why" report. The four supported types convert; everything else (custom data tests, dbt-expectations, singular tests, generic tests we don't model) is skipped + recorded.

### DEC-004 — `prune-existing` CLI subcommand split to fast-follow [#105](https://github.com/wjduenow/SignalForge/issues/105)

The operator initially opted to bundle the CLI, then split it: #104 ships the **library seam only** (`read_schema → IngestResult`); the `signalforge prune-existing` subcommand moves to fast-follow #105 (blocked by #104). Rationale: the CLI roughly doubled the surface (argparse wiring, 5-surface parity, subprocess smoke) and the library is independently valuable + testable. The new `errors.py` still registers in `_EXCEPTION_TO_EXIT_CODE` now (rules #6–#7) so #105 inherits the exit-code mapping with no rework. DEC-011's flag design is retained in this plan as the spec #105 implements.

### DEC-005 — Safe YAML load with size cap before parse

Mirror diff-renderer DEC-006: check `len(raw.encode("utf-8")) <= limit` BEFORE `yaml.safe_load` to defend against billion-laughs / deep-nesting. `yaml.safe_load` only (never `load`). Oversize raises `IngestSchemaTooLargeError(size, limit)` before the parser sees the payload.

### DEC-006 — Accept both `tests:` and `data_tests:` keys; both inline and `arguments:`-nested args

dbt renamed `tests:` → `data_tests:` in 1.8. Accept both (union); document the precedence if both present (TBD in refinement). Accept `accepted_values`/`relationships` args inline (`{values: [...]}`) and under `arguments:` (dbt 1.8+ shape). This is the load-bearing compatibility surface — refinement will enumerate the exact variants and pin them in fixtures.

---

## Refinement decisions (Phase 3)

### DEC-007 — Re-implement the anchor check in the ingest layer

`signalforge.ingest` ships its own small anchor validator raising `IngestAnchorContractError(violations=...)` (whole-file, collect-all per DEC-002). Rejected importing `draft.parser._validate_anchor_contract`: it raises the LLM-typed `LLMOutputAnchorContractError`, which would surface an LLM-taxonomy error from a non-LLM path and create a cross-layer `ingest → draft` import. The validator body is small (set-membership + per-test checks); duplication is cheaper than the taxonomy smell. The error shape (multi-violation tier-2) reuses the CLI's existing header+bullets sink (`format_error_to_stderr`).

### DEC-008 — Union both `tests:` and `data_tests:`, dedupe by (type, column, sorted-args)

Collect tests from both keys (most permissive — nothing silently dropped, matches "signal over volume"). Dedupe identical tests appearing under both keys by `(type, column, frozenset/tuple of args)` so a file carrying the same test in both keys yields one `CandidateTest`. Rejected `data_tests`-wins (silently drops legacy-only entries) and fail-loud (dbt itself only *warns* on the deprecated key — we shouldn't be stricter than dbt).

### DEC-009 — Best-effort unwrap of `ref()` / `source()` in `relationships.to`

Parse `ref('m')` → `"m"`, `ref("pkg", "m")` → `"m"`, `source('s', 't')` → `"s.t"`; if no pattern matches, carry the raw string verbatim. Gives prune's relationships compiler a usable target (better `requires-future-data` vs. `kept` resolution). Unwrap is a bounded regex/string parse — no Jinja engine. Pinned by fixtures for each shape.

### DEC-010 — `description` defaults to `""` when absent

External YAML frequently omits `description`. `CandidateSchema`/`CandidateColumn` require `description: str`, so the reader supplies `""` when the key is absent. The prune step does not consume `description`; a sentinel like `"(no description)"` would pollute any later diff render. `rationale` stays `None` (already optional).

### DEC-011 — `prune-existing` flag spec (implemented in fast-follow [#105](https://github.com/wjduenow/SignalForge/issues/105))

Retained here as the spec #105 inherits. The flow runs ingest → prune → diff with no LLM call. Flag set reuses `generate`'s relevant flags: positional `<model>`, `--schema <path>` (required), `--project-dir`, `--manifest`, `--profiles-dir`, `--mode {schema-only,aggregate-only,sample}`, `--write`/`--dry-run` (mutex), `--format {ansi,markdown,json}`, `--quiet`/`--verbose`/`--no-color`. **Dropped:** `--min-score` and any grade-only flags (no grading report → diff renders kept/kept-uncertain/dropped, never `flagged`). The skipped-test report renders as a stderr summary line (`Skipped <N> unsupported tests: <type×count ...>`) plus per-item detail under `--verbose`. **Not in #104 scope** — no CLI module, parity test, or subprocess smoke lands in this issue.

---

## Detailed breakdown (Phase 4)

Architecture ordering: scaffold/errors → typed models → pure parsers (TDD) → orchestrator (TDD) → CLI → docs/parity → Quality Gate → Patterns & Memory. Every story's AC includes the canonical validation command: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.

### US-001 — `signalforge.ingest` package scaffold + `errors.py` + exit-code lockstep

**Traces to:** DEC-001, rules #6–#7.
**Description:** Create `src/signalforge/ingest/{__init__.py,errors.py}`. Define the `IngestError` base (carries `remediation`, `__str__` renders `↳ Remediation:`) and concrete subclasses: `IngestSchemaNotFoundError`, `IngestSchemaParseError`, `IngestSchemaTooLargeError` (tier 1); `IngestModelNotFoundError`, `IngestAnchorContractError` (tier 2). Register every concrete in `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`; register `IngestError` as a fallback tier-1 entry; add `IngestError` to `_EXCEPTION_MAPPING_EXCLUDED_BASES`. Bump `tests/test_audit_completeness.py::test_scan_7_discovers_every_per_stage_errors_module` count 10 → 11.
**AC:** New errors module exists; 7th AST scan passes (every concrete mapped); excluded-bases set includes `IngestError`; scan-7 discovery count is 11; validation command passes.
**Done when:** `uv run pytest tests/test_audit_completeness.py` green; importing `signalforge.ingest` succeeds.
**Files:** `src/signalforge/ingest/__init__.py`, `src/signalforge/ingest/errors.py`, `src/signalforge/cli/_helpers.py`, `tests/test_audit_completeness.py`.
**Depends on:** none.

### US-002 — Typed ingest result models

**Traces to:** DEC-003.
**Description:** Add `src/signalforge/ingest/models.py`: `SkipReason = Literal["unsupported-test-type","custom-or-generic-test","malformed-supported-test"]`; `SkippedTest` (Pydantic frozen `extra="ignore"`: `test_name: str`, `column: str | None`, `reason: SkipReason`, `detail: str = ""`); `IngestResult` (`candidate: CandidateSchema`, `skipped: tuple[SkippedTest, ...]`). Custom `__repr__` if any field is verbose (mirrors prune/grade convention). Re-export from `__init__.py`.
**AC:** Models importable; `SkipReason` is a closed literal; validation command passes. (No disk drift-detector required — `IngestResult` is produced in-process, not read back from JSONL; note this explicitly in the test module so a future reviewer doesn't add a spurious detector.)
**TDD:** construct each `SkippedTest` reason; assert `extra="ignore"` tolerates an unknown key; assert `IngestResult` round-trips a minimal `CandidateSchema`.
**Files:** `src/signalforge/ingest/models.py`, `src/signalforge/ingest/__init__.py`, `tests/ingest/test_models.py`.
**Depends on:** US-001.

### US-003 — dbt test-entry parser (pure mapping, TDD)

**Traces to:** DEC-006, DEC-008, DEC-009.
**Description:** Pure function in `src/signalforge/ingest/parser.py` mapping a single dbt test entry (`str | dict`) + owning column name → `CandidateTest | SkippedTest`. Handles: bare string (`not_null`/`unique` → supported; else skip `unsupported-test-type`); single-key dict where key is the test name; inline args (`{values: [...]}`, `{to:, field:}`) AND `arguments:`-nested args (dbt 1.8+); ignore interleaved config keys (`config`, `severity`, `where`, `name`, `tags`, `error_if`, `warn_if`); `accepted_values` with missing/empty `values` → skip `malformed-supported-test`; `relationships` missing `to`/`field` → skip `malformed-supported-test`; `ref()`/`source()` unwrap (DEC-009); any non-supported key (`dbt_utils.*`, `dbt_expectations.*`, custom) → skip `custom-or-generic-test`.
**AC:** Every variant has a test; `accepted_values`/`relationships` extract from both inline and `arguments:` shapes; `ref('m')`/`ref("pkg","m")`/`source('s','t')` unwrap correctly; config keys ignored; validation command passes.
**TDD:** enumerate ~15 entry shapes (4 supported × {inline, arguments}, bare strings, malformed, custom-namespaced, config-interleaved, ref/source variants).
**Files:** `src/signalforge/ingest/parser.py`, `tests/ingest/test_parser.py`.
**Depends on:** US-002.

### US-004 — Anchor-contract validator (fail-loud, collect-all)

**Traces to:** DEC-002, DEC-007.
**Description:** `src/signalforge/ingest/anchor.py`: validate every produced `CandidateTest`/`CandidateColumn` references a column in `frozenset(model.columns.keys())`. Collect ALL violations (column-level `name` not on model; per-column test `column` ≠ parent; test `column` not on model; model-level test `column` not on model), raise one `IngestAnchorContractError(violations=...)` listing every violation. Never short-circuit.
**AC:** Multiple violations surface in one error; a clean candidate produces no error; error renders via CLI multi-violation sink shape; validation command passes.
**TDD:** clean case (no raise); single violation; multiple violations collected; model-level test on missing column.
**Files:** `src/signalforge/ingest/anchor.py`, `tests/ingest/test_anchor.py`.
**Depends on:** US-002.

### US-005 — `read_schema` orchestrator (TDD)

**Traces to:** DEC-005, DEC-006, DEC-008, DEC-010, plus US-003/US-004.
**Description:** Public entry `read_schema(schema: str | Path, model: Model, *, project_dir: Path | None = None) -> IngestResult` in `src/signalforge/ingest/reader.py`. Steps: if `schema` is a path, canonicalise via `_common.path_safety.canonicalise_path` (wrap `PathContainmentError` → `IngestSchemaParseError`) and read; size-cap-before-`yaml.safe_load` (DEC-005, raise `IngestSchemaTooLargeError`); `yaml.safe_load` (malformed → `IngestSchemaParseError`); select the model block by `name == model.name` (absent → `IngestModelNotFoundError`); iterate columns + model-level tests, union `tests:`/`data_tests:` and dedupe (DEC-008), call US-003 parser per entry, default `description=""` (DEC-010); assemble `CandidateSchema`; run US-004 anchor check; return `IngestResult(candidate, skipped)`. **No logging** (rule #4). Re-export `read_schema` from `__init__.py`.
**AC:** Given a multi-model dbt schema.yml with the four supported types (column + model level), produces a valid `CandidateSchema` that `prune_tests` accepts; unsupported types land in `skipped`; missing-column raises `IngestAnchorContractError`; oversize/ malformed/ missing-model raise their typed errors; path-string and string-content inputs both work; validation command passes.
**TDD:** happy path (fixture schema.yml → IngestResult with expected candidate + skips); each error path; string vs path input; multi-model selection.
**Files:** `src/signalforge/ingest/reader.py`, `src/signalforge/ingest/__init__.py`, `tests/ingest/test_reader.py`, `tests/fixtures/ingest/schema_codegen_shaped.yml` (+ edge fixtures).
**Depends on:** US-003, US-004.

### US-006 — `ingest-ops.md` operational doc

**Traces to:** docs-publishing.md, multi-surface parity (library half).
**Description:** Add `docs/ingest-ops.md` (operational reference: input format, the four supported types, the skip taxonomy + `SkipReason` values, `read_schema` signature + `IngestResult` shape, error reference with remediation, the `tests:`/`data_tests:` union + `ref()`/`source()` unwrap rules, a worked example). Add a `nav:` entry in `mkdocs.yml` (per docs-publishing.md "new ops doc → add nav entry"). No CLI doc here — `docs/cli-ops.md` § `prune-existing` lands in #105.
**AC:** `uv run mkdocs build` clean; nav entry present; validation command passes.
**Files:** `docs/ingest-ops.md`, `mkdocs.yml`.
**Depends on:** US-005.

### US-007 — Quality Gate (code review ×4 + CodeRabbit)

**Traces to:** all implementation stories.
**Description:** Run the code reviewer 4 times across the full changeset, fixing every real bug each pass. Run CodeRabbit if available. Re-run the canonical validation command until green.
**AC:** 4 review passes complete with fixes applied; validation command passes.
**Depends on:** US-001…US-006.

### US-008 — Patterns & Memory (priority 99)

**Traces to:** the whole feature.
**Description:** Create `.claude/rules/ingest-layer.md` distilling this ticket's conventions (reader shape, fail-loud anchor, skip-record taxonomy, no-logging, error-tier mapping, the union/dedupe + ref-unwrap rules). Update `CLAUDE.md`: add #104 to the shipped-issues list and the public-API surface (`signalforge.ingest.read_schema`, `IngestResult`, `SkippedTest`, the `IngestError` hierarchy). Note the deferred `prune-existing` CLI under the v0.2 / fast-follow #105 pointer (do **not** claim the CLI ships here). Update the cli-layer.md scan-7 count note (10→11 errors modules). Update repo memory if a non-obvious lesson emerged.
**AC:** New rule file lands; CLAUDE.md reflects the new (library) surface; #105 referenced as the CLI fast-follow; validation command passes.
**Depends on:** US-007.

---

## Story dependency graph

```
US-001 ── US-002 ─┬─ US-003 ─┐
                  └─ US-004 ─┴─ US-005 ── US-006 ── US-007 (Quality Gate) ── US-008 (Patterns & Memory)
```

(The `prune-existing` CLI subcommand + its docs/parity/subprocess work live in fast-follow [#105](https://github.com/wjduenow/SignalForge/issues/105), blocked on this issue.)

---

## Architecture review (Phase 2)

| Area | Rating | Finding |
|---|---|---|
| Security | **pass** | Two attack surfaces, both mitigated by decided pattern: (a) malicious YAML → `yaml.safe_load` only + size-cap-before-parse (DEC-005, mirrors diff DEC-006); (b) path traversal / symlink → `_common.path_safety.canonicalise_path` (rule #2). `relationships.to` carrying `ref('x')`/raw SQL is not an injection vector at ingest — prune's `_sql_safety.validate_identifier` gates every identifier before it reaches SQL (prune-engine.md DEC-024). No secrets, no network. |
| Input validation / robustness | **concern** | The load-bearing surface. Must handle: `tests:` vs `data_tests:` keys; bare-string vs single-key-dict test entries; inline args vs `arguments:` nesting (dbt 1.8+); config keys (`config`/`severity`/`where`/`name`/`tags`) interleaved with args and ignored; multiple models in one file (select by `name`); malformed supported-type entries (e.g. `accepted_values` with no `values`). Each variant needs a pinned fixture. Drives DEC-006 + refinement Q on precedence. |
| Data model / API design | **concern** | `read_schema(...) -> IngestResult` signature must align with adjacent stages (model + data front-paired, keyword-only optionals, `project_dir` for path resolution). `IngestResult`/`SkippedTest` shapes + `description` default need locking (refinement). `SkippedTest.reason` is a closed `Literal` → needs a drift detector if read back; if purely in-process, lighter. |
| CLI / exit-code taxonomy | **concern** | New `errors.py` = 11th per-stage module: register every concrete in `_EXCEPTION_TO_EXIT_CODE`, add `IngestError` to `_EXCEPTION_MAPPING_EXCLUDED_BASES`, bump `test_scan_7_discovers_every_per_stage_errors_module` 10→11 — all in lockstep or the AST scan fails. **This lockstep stays in #104** (US-001) so the library's typed errors are mapped from day one. The `prune-existing` subcommand itself (library-surface wrap, 5-surface parity, `--help` subprocess smoke) moves to fast-follow #105. |
| Testing strategy | **pass** | Obligations well-understood: drift detector for any read-back model; dbt-codegen-shaped fixture `schema.yml`; per-variant fixtures; no-`assert True`; coverage floor holds. AST scan auto-detects the new errors.py. |
| Observability | **pass** | Reader emits zero logs (rule #4, stage-0). CLI handler is the orchestration layer and MAY emit a stderr skip-summary line (lazy-format JSON, grep-gate applies if `_LOGGER` used). |
| Performance | **pass** | Single-file parse; no added warehouse cost beyond what `prune_tests` already incurs. |

**Blockers:** none. **Concerns** (input-variant coverage, API shapes, CLI lockstep) carry into refinement and become explicit stories in Phase 4.

---

## Detailed breakdown (Phase 4)

_pending_

---

## Beads manifest (Phase 7 — devolved)

- **Epic:** `bd_1-scaffolding-ky3` — #104: ingest external dbt schema.yml tests into CandidateSchema
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/104-ingest-external-tests` (branch `feature/104-ingest-external-tests` off `dev`)
- **Tasks:**

| Bead | Story | Depends on |
|------|-------|-----------|
| `bd_1-scaffolding-ky3.1` | US-001 scaffold + errors.py + exit-code lockstep | — |
| `bd_1-scaffolding-ky3.2` | US-002 typed result models | .1 |
| `bd_1-scaffolding-ky3.3` | US-003 dbt test-entry parser | .2 |
| `bd_1-scaffolding-ky3.4` | US-004 anchor-contract validator | .2 |
| `bd_1-scaffolding-ky3.5` | US-005 read_schema orchestrator | .3, .4 |
| `bd_1-scaffolding-ky3.6` | US-006 docs/ingest-ops.md | .5 |
| `bd_1-scaffolding-ky3.7` | US-007 Quality Gate | .6 |
| `bd_1-scaffolding-ky3.8` | US-008 Patterns & Memory | .7 |

Ready to start: `bd_1-scaffolding-ky3.1` (US-001). Fast-follow CLI work tracked in [#105](https://github.com/wjduenow/SignalForge/issues/105).
