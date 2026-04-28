# Issue #2 — Read dbt manifest.json and resolve a single model

## Meta

- **Ticket:** [#2](https://github.com/wjduenow/SignalForge/issues/2)
- **Branch:** `feature/2-manifest-loader` (off `dev`)
- **Worktree:** `<local-worktree>/SignalForge/feature/2-manifest-loader` (created via `bark new feature/2-manifest-loader --from dev`)
- **Phase:** devolved (epic `bd_1-scaffolding-28p` + 9 tasks live in `.beads/`; PR [#15](https://github.com/wjduenow/SignalForge/pull/15) draft)
- **Sessions:** 1 (started 2026-04-27)
- **Plan author:** Claude Code (Opus 4.7, 1M context)
- **Milestone:** v0.1
- **Labels:** `scaffolding`

## Discovery

### Ticket summary

Build the read-only manifest layer of SignalForge: parse a dbt project's `target/manifest.json` into a typed Python object and expose a function that returns one model's "context bundle" (raw SQL, declared columns, ref/source dependencies, tags, materialization). No drafting, no warehouse calls, no LLM — just the deterministic JSON-to-typed-object front door that everything downstream reads from.

This is **stage 0** of the SignalForge pipeline: `model.sql + manifest + project ctx -> LLM drafts candidate artifacts`. Without it, the LLM has no project-aware context, the prune step has no `unique_id` to attribute results back to, and the "explainable diff" has no anchor.

### Acceptance criteria (from ticket)

1. `signalforge.manifest.load(project_dir: Path) -> Manifest` returns a typed object.
2. Resolve a model by `unique_id` or by file path.
3. Surface: model SQL, source/ref dependencies, declared columns (if any), tags, materialization.
4. Tolerate manifest schema versions v9–v12 (dbt 1.6–1.8); document Fusion v20 as future work.
5. Unit tests against fixture manifests (small + medium).
6. Skip nodes in the `disabled` parallel dict.

Ticket notes:

- Reference: `docs/temp/dbt-claude-technical-surface.md` Section 1 (clauditor repo). **Caveat surfaced in research:** that doc is gitignored under `clauditor/docs/temp/` — it's not in the public clauditor repo, so contributors outside the maintainer's machine cannot read it. Phase 3 must decide how to handle this (inline / vendor / cite).
- Don't depend on `dbt-core` Python runtime — read the JSON directly so we don't pull a heavy dep.

### Schema-version reality check (clauditor §1.1, surfaced in discovery)

The ticket says "v9–v12 (dbt 1.6–1.8)". The clauditor doc maps versions slightly differently:

| Schema | dbt versions |
| --- | --- |
| v9 | 1.5 |
| v10 | 1.6 |
| v11 | 1.7 |
| v12 | 1.8, 1.9, 1.10, 1.11 (no bump) |
| v20 (Fusion) | additive over v12 — out of scope for v0.1 |

So "v9–v12" actually spans dbt 1.5 through 1.11. To confirm in Phase 3 — keep ticket's stated range or extend.

### Key technical surface (clauditor §1.1)

- **Top-level keys (v12):** `metadata`, `nodes`, `sources`, `macros`, `docs`, `exposures`, `metrics` (v1.6+), `groups` (v1.5+), `selectors`, **`disabled`** (parallel dict — disabled nodes do NOT appear in `nodes`), `parent_map`, `child_map`, `group_map`, `saved_queries` (v1.7+), `semantic_models` (v1.6+), `unit_tests` (v1.8+).
- **Per-model fields needed:** `unique_id`, `name`, `resource_type`, `original_file_path`, `path`, `package_name`, `database`/`schema`/`alias`, `config.materialized` and `config.tags`/`config.meta`, top-level `tags`, `description`, `columns.<col>.{name,data_type,description,constraints,meta,tags}`, `depends_on.{nodes,macros}`, `refs[]` (`{name, package, version}` post-1.5), `sources[]` (list of `[source_name, table_name]`), `raw_code`, `compiled_code` (null until `dbt compile`), `language`, `access` (v1.5+), `version`/`latest_version` (v1.5+), `primary_key`, `constraints`.
- **Parsing gotchas:**
  - `nodes` is flat — filter by `resource_type == "model"` (tests/seeds/snapshots/analyses share the dict; distinguished by `unique_id` prefix).
  - `parent_map`/`child_map` are authoritative — don't rebuild edges from `depends_on.nodes` (misses source/macro edges).
  - `compiled_code` is null after `dbt parse`; `raw_code` is always populated.
- **Size notes:** medium projects (~500 models) = 5–15 MB / 1–3M tokens; large (~2000) = 30–100+ MB. Not load-bearing for this ticket but flagged for downstream stages.

### Codebase findings

- **Package state:** post-#1 scaffold. `src/signalforge/__init__.py` is 3 lines (docstring + `__version__ = "0.1.0.dev0"`); `tests/test_smoke.py` is the only test. No subpackages, no runtime deps, no prior dbt/manifest work in the tree (greenfield).
- **Runtime deps:** none. **Dev deps:** `ruff`, `pyright`, `pytest`. Stdlib `json` is sufficient — no Pydantic or other JSON libs in the tree.
- **No existing typed-object pattern.** This ticket sets the precedent — frozen dataclasses, Pydantic, or `TypedDict` are all candidates.
- **No `tests/fixtures/`** yet. Layout decision is open.
- **Pyright config:** `typeCheckingMode = "standard"`, `pythonVersion = "3.11"`, `reportMissingImports = "error"`. Manifest module must be fully typed.
- **CLI seam:** none yet. Ticket is library-only; that matches CLAUDE.md (CLI is a later v0.1 ticket, not this one).
- **Validation command (CLAUDE.md, canonical):** `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`.

### Project rules (`.claude/rules/`) audit

All three existing rules apply. Specific bites for this ticket:

- **`python-build.md`** — code lives under `src/signalforge/`; tests under `tests/`. No `tests/__init__.py`. If the manifest layer becomes a subpackage, the wheel target packages list does **not** need to change (`packages = ["src/signalforge"]` already covers everything underneath).
- **`testing-signal.md`** — every test must be capable of failing. Fixture-based tests must exercise both success and failure paths (e.g., a malformed manifest, a `disabled` lookup, a missing `unique_id`). Strict markers in pytest config already in place — any new marker (e.g. `@pytest.mark.fixture`) must be declared.
- **`ci-supply-chain.md`** — only relevant if this ticket adds new actions or changes CI; current plan adds neither, so it's informational here.

CLAUDE.md commitments that bite this ticket:

- **Architectural Commitment #3 (warehouse-agnostic).** The manifest layer is *upstream* of the warehouse adapter — the right place to keep adapter-agnostic. No BigQuery-isms here.
- **Architectural Commitment #4 (OSS-first / dbt-core-friendly).** Reading `manifest.json` works against any dbt-core project; the explicit "no `dbt-core` runtime dep" instruction operationalises this.

### Out of scope (explicit)

- **LLM draft generation itself** (a later ticket; this one builds the input).
- **`catalog.json` / `run_results.json` / `sources.json` reading** — these are downstream signals.
- **BigQuery adapter, sampling, profiling, prune logic** — separate v0.1 work.
- **Manifest size handling** (chunking/streaming/parent_map-only-then-fetch) — concern for the LLM stage.
- **Fusion v20 support** — ticket explicitly says "document as future work."
- **Multi-model batching / project walking** — AC says "resolve a single model"; iteration shape is downstream.
- **`dbt parse` invocation** — ticket reads an *already-generated* manifest; doesn't call dbt.

### Scoping decisions (Phase 1)

- **DEC-001 — Pydantic v2 for the typed model layer** (Q1=B). `Manifest` and resolved `Model` are Pydantic v2 `BaseModel` subclasses. *Why:* manifest schemas evolve across v9–v12 and Fusion v20; Pydantic gives free per-version validation, JSON-serialisability for fixtures and downstream caching, and a clean `model_validate(...)` entry point. Adds one runtime dep — acceptable for stage-0 infrastructure that everything downstream reads through. *How to apply:* import as `pydantic` (>=2.x); `model_config = ConfigDict(frozen=True, extra="ignore")` on every model so unknown fields (Fusion v20 additions, future v13) don't crash.
- **DEC-002 — Subpackage from day one** (Q2=B). Layout: `src/signalforge/manifest/__init__.py` (public API: `load`, `Manifest`, `Model`, error classes), `models.py` (Pydantic types), `loader.py` (file IO + version detection + parsing), `errors.py` (typed exception hierarchy). *Why:* the scope spans loading, version detection, error mapping, and resolution — three or four files reads cleaner than one 400-line module, and we'll add `catalog.py` / `run_results.py` siblings in later tickets without restructuring. *How to apply:* keep `__init__.py` as a thin re-export so callers say `from signalforge.manifest import load`, never `from signalforge.manifest.loader import load`.
- **DEC-003 — File-path resolver accepts relative AND absolute** (Q3=B). The resolver normalises any input into an `original_file_path`-style key (`"models/marts/dim_users.sql"`), then matches against the manifest. *Why:* callers in CLI / IDE-integration scenarios will hand us absolute paths from `$CWD`; callers in scripts will hand us project-relative paths; forcing one form is friction. *How to apply:* accept `str | Path`; if absolute, attempt `Path.relative_to(project_dir)` and raise `ModelPathOutsideProjectError` if the path escapes the project root.
- **DEC-004 — Surface `raw_code` only for v0.1** (Q4=A). The resolved `Model.sql` is `raw_code` from the manifest. `compiled_code` is not exposed in v0.1. *Why:* `compiled_code` is null after `dbt parse` and only populated after `dbt compile` — surfacing it creates a footgun where downstream code silently ships templated Jinja or `None`. The drafting layer can request compilation explicitly later. *How to apply:* if `raw_code` is missing/empty on a node, raise `ModelMissingSqlError` rather than falling back to `compiled_code`.
- **DEC-005 — Fixtures: committed dbt project + generated manifest** (Q5=B). `tests/fixtures/dbt_project_small/` and `tests/fixtures/dbt_project_medium/` each contain a real (tiny) dbt project + the pre-generated `target/manifest.json`. `dbt-core` joins `[project.optional-dependencies].dev` so a maintainer can regenerate fixtures via a documented `make fixtures` target (or shell script); CI itself only runs `pytest` against the committed JSON — no `dbt-core` in the test runtime path. *Why:* hand-authored JSON drifts silently from real dbt output (the very class of bug this ticket exists to read); committing a real project keeps fixtures honest. *How to apply:* fixtures live next to `tests/`; regeneration script is documented in `CONTRIBUTING.md` (or a new `tests/fixtures/README.md`); the schema-version coverage is fixtures × dbt versions (one per supported schema).
- **DEC-006 — Vendor the dbt research files into `docs/research/`** (Q6=custom). The seven dbt-prefixed files from `clauditor/docs/temp/` are now copied to `docs/research/` in this worktree (`dbt-ai-tools-deep-dive.md`, `dbt-claude-technical-surface.md`, `dbt-pain-deep-dive.md`, `dbt-research-index.md`, `dbt-research.pdf`, `dbt-tool-design-sketches.md`, `dbt-tooling-opportunity-report.md`). *Why:* the original location is gitignored under clauditor — outside contributors couldn't act on the references. Copy-once preserves the snapshot; future updates can be redone by the maintainer. *How to apply:* commit these files alongside the manifest module; cite as `docs/research/dbt-claude-technical-surface.md §1.1` in code/plans, not the clauditor path.

### Phase 1 housekeeping defaults (set unless flagged in Phase 2/3)

- Schema-version detection: parse `metadata.dbt_schema_version` URL (e.g. `.../v12.json`); fall back to feature-presence sniff if URL is malformed.
- Disabled-but-asked-for resolution: raise `ModelDisabledError` (subclass of `ModelNotFoundError`).
- Unknown / unsupported version: raise `UnsupportedManifestVersionError` carrying the version string.
- Sources surfacing: edges only (per-model `refs` and `sources` lists); no source-side column expansion in v0.1.

---

## Architecture Review

Reviewed by five parallel subagents (security, performance, data model, API design, observability + testing) against the Phase 1 locked shape (DEC-001 … DEC-006 + housekeeping defaults). One blocker, several concerns, broad agreement on data-model and API shape.

### Findings table

| Area | Rating | Notes |
| --- | --- | --- |
| Security — path traversal in resolver | **blocker** | `Path.relative_to(project_dir)` does **not** resolve symlinks. A symlink `models/evil.sql -> /etc/passwd` inside the project escapes validation. Manifest-supplied `path` strings can also contain `..`. Mitigation: `.resolve(strict=True)` then `is_relative_to(project_dir.resolve())`. |
| Security — manifest DoS / memory | **concern** | Stdlib `json.load` on a 100 MB+ manifest expands to ~300–500 MB of Python objects. No depth or size guard. Mitigation: document expected sizes; consider a soft `MAX_MANIFEST_BYTES` advisory check (warn if exceeded, fail if absurd). |
| Security — `dbt-core` as dev dep | **concern** | Adds ~120 transitive packages to dev install. No CVEs flagged. Lighter alternative: keep `dbt-core` *out* of `[dev]` extras and document fixture regeneration via `uvx dbt-core` (or `pipx run dbt-core`) so contributors not regenerating fixtures aren't forced to install it. |
| Security — Pydantic v2 / `extra="ignore"` | **pass** | Frozen+ignore is correct for production. Strong suggestion: tests should construct models with `extra="forbid"` to detect fixture drift early. |
| Security — vendored research files | **pass** | All seven dbt-prefixed files scanned; only placeholder `${{ secrets.* }}` patterns and example env-var names — no real credentials, no internal URLs. Safe to ship. |
| Performance — cold-load latency | **pass** | Pydantic v2 (Rust core) on a 100 MB manifest is ~2–5s; acceptable for once-per-CLI-invocation use. |
| Performance — repeated `load()` calls | **pass** | Out of scope for v0.1; callers cache. |
| Performance — resolver complexity | **concern** | O(n) scan over ~2k models is fine in wall time but loose. Build a `unique_id → Model` dict during `load()`; expose `Manifest.get_model(...)` explicitly (don't make callers iterate `nodes`). |
| Performance — `parent_map` / `child_map` | **pass** | Parse them (Pydantic walks the tree anyway) but don't surface in `Model`. AC #3 needs per-model `refs` + `sources`, not the global graph. |
| Performance — memory profile docs | **concern** | Document expected resident memory (small / medium / large) so users size CI runners appropriately. |
| Data model — version representation | **pass** | Single `Model` with optional fields wins over per-version unions. Pydantic `extra="ignore"` carries us through Fusion v20 additive fields without code changes. |
| Data model — field naming | **pass** | Keep dbt's snake_case (`raw_code`, `unique_id`, `original_file_path`) for 1:1 doc fidelity. No renames. |
| Data model — refs normalisation | **pass** | Always dict-shape `Ref(name, package, version)`. Pre-1.5 string refs (out of supported range) need not be supported. |
| Data model — columns shape | **pass** | Preserve `dict[str, Column]` (dbt-faithful); add a `columns_list` property as a convenience iterator. |
| Data model — config nesting | **concern** | dbt's `config` has 30+ fields. Surface only `materialized`, `tags`, `meta` — but `config.tags` collides with top-level `tags`. Pick a naming strategy (nested `Model.config.tags` vs flattened `Model.config_tags`). |
| Data model — frozen + extra | **pass** | `extra="ignore"` in production; `extra="forbid"` in fixture-validation tests to catch schema drift. |
| Data model — god-object risk | **pass** | Stay manifest-only in v0.1. Future enrichment (catalog, run_results, freshness) goes into a composing `ModelContext` in v0.2, not into `Model` itself. |
| Data model — validators | **pass** | Light guards: `unique_id` starts with `model.`; `raw_code` non-empty when present (else `ModelMissingSqlError`). |
| API — `load()` signature | **concern** | Add optional `manifest_path: Path \| str \| None = None` override. Default is `project_dir / "target/manifest.json"`. Unblocks CI/cache scenarios where the manifest is staged separately. |
| API — resolver placement | **pass** | `Manifest.get_model(key)` is the primary; an optional top-level `signalforge.manifest.get_model(manifest, key)` thin wrapper is cheap and matches the stage-by-stage pipeline shape. |
| API — `unique_id` vs path detection | **pass** | Auto-detect on `"model."` prefix. dbt forbids dots in model names, so collision is structurally impossible. |
| API — error hierarchy | **concern** | Add `ManifestError` base class + `ManifestNotFoundError` (the file is missing — distinct from a model missing inside a present manifest). All custom errors carry a `remediation: str` rendered in `__str__`. |
| API — public surface (`__all__`) | **pass** | `load`, `Manifest`, `Model`, seven error classes. Internals (`_loader_helpers`, etc.) stay private. |
| API — `Manifest` methods | **pass** | Add `iter_models()` (one line, unblocks debugging) and a `schema_version: str` property. Don't surface `nodes` / `disabled` directly in `__all__`. |
| API — versioning policy | **pass** | Document a private-field `_` prefix convention; semver-bind public field names. |
| Observability — structured logging | **pass** | Defer to v0.2. The "explainable diffs" promise lives in the prune/grade layer; this layer is deterministic JSON-to-objects. |
| Observability — metrics | **pass** | Defer to v0.2. No metrics emission in v0.1. |
| Observability — error remediation | **concern** | Custom errors should include actionable remediation (`ModelMissingSqlError` → "Run `dbt parse` or `dbt compile` first"). Render via `__str__`. |
| Testing — fixture coverage | **concern** | Recommended matrix: 4 small (one per v9 / v10 / v11 / v12) + 1 medium (v12 only) + 5 error-path (malformed JSON / missing version URL / unsupported version / disabled lookup / empty raw_code). ~6 MB committed. |
| Testing — strict markers | **pass** | Declare `unit`, `integration`, `error` markers in `pyproject.toml`'s `[tool.pytest.ini_options]`. |
| Testing — property-based / hypothesis | **pass** | Out of scope for v0.1. Real dbt projects are the mutation test. |
| Testing — coverage gating | **pass** | No CI coverage gate — consistent with DEC-004 from #1 (no `pytest-cov` in v0.1). Document intent in `CONTRIBUTING.md`. |
| Testing — signal-over-volume regression set | **pass** | Reviewer sketched 7 fail-capable tests (version detection, schema validation, resolve-by-id, resolve-by-path, disabled, unsupported version, missing SQL). Each fails on a real regression. Carry forward into Phase 4. |

### Blockers

- **B1 — Symlink path traversal in resolver** (security). Must be resolved before Phase 3 lock — the mitigation tightens DEC-003 with `.resolve()` + `is_relative_to()`. Surfaced as R1 below.

### Concerns to resolve in Phase 3

Carried forward as refinement questions R1 … R6 below.

## Refinement Log

### Phase 3 decisions (resolved 2026-04-27)

Six refinement questions (R1–R6) plus the bundled validated defaults from Phase 2.

- **DEC-007 — Symlink-hardened path resolver** (R1=A, resolves blocker B1). DEC-003's resolver is tightened: on absolute or relative path input, the resolver calls `.resolve(strict=True)` on both the input path and `project_dir`, then validates `resolved_input.is_relative_to(resolved_project_dir)`. Manifest-supplied `original_file_path` strings are also `.resolve()`d before any FS access. Failure mode → `ModelPathOutsideProjectError`. *Why:* `Path.relative_to()` alone does not follow symlinks; a malicious or accidental symlink in `models/` could escape the project root. *How to apply:* the resolver helper is its own private function (`_canonicalise_path`) so the same hardening covers `load()`'s `manifest_path` override (DEC-010) and `Manifest.get_model()`'s file-path branch.
- **DEC-008 — Soft manifest-size warning at 200 MB** (R2=B). `loader.load()` calls `os.path.getsize(manifest_path)`; if > `MAX_MANIFEST_BYTES = 200 * 1024 * 1024`, emit a single `warnings.warn(f"Manifest is {size_mb} MB; expect ~{3 * size_mb} MB resident memory")` (UserWarning) and proceed. No hard fail in v0.1. *Why:* most projects fit comfortably; absurd sizes warrant a heads-up but not refusal. *How to apply:* the constant is module-level, easily monkey-patched in tests; the warning string includes the 3× memory rule of thumb so users can plan CI runner sizing.
- **DEC-009 — `dbt-core` in `[project.optional-dependencies].dev`** (R3=A, confirms DEC-005). Pin to a single recent version (`dbt-core>=1.8,<2.0`) — it natively regenerates v12 fixtures. Older schema versions (v9 / v10 / v11) are regenerated via a script using ephemeral `uvx`/`pipx` installs of `dbt-core==1.5.x` / `1.6.x` / `1.7.x`. *Why:* the dev install pays once for the happy-path regen; other versions are maintainer chores documented but not pre-installed. *How to apply:* pin in pyproject; document the multi-version regen recipe in `tests/fixtures/README.md`.
- **DEC-010 — `load()` takes optional `manifest_path` override** (R4=A). Signature: `def load(project_dir: Path | str, manifest_path: Path | str | None = None) -> Manifest`. Defaults to `project_dir / "target/manifest.json"`. The override is also subject to DEC-007's symlink hardening relative to `project_dir`. *Why:* CI / workspace caching pre-stages manifests outside `target/`; forcing a fixed path forces busy-work. *How to apply:* if `manifest_path` is provided and missing → `ManifestNotFoundError`; if outside `project_dir` after resolve → `ModelPathOutsideProjectError`.
- **DEC-011 — Nested `Config` model on `Model`** (R5=A). `Model.config` is a typed `Config(BaseModel)` with `tags`, `meta`, `materialized`. Top-level `Model.tags` is the `tags` field at the model level (dbt manifest has both — they're populated independently). Callers say `model.config.tags` vs `model.tags` and the dbt-faithful naming is preserved. *Why:* avoids invented prefix names like `config_tags`; matches dbt schema docs verbatim; leaves room to add `Config.contract` / `Config.grants` etc. without renaming. *How to apply:* `Config` lives in `models.py` next to `Model`; `extra="ignore"` so dbt's other 25+ config fields fall away cleanly.
- **DEC-012 — Fixture matrix: 4 small + 1 medium + 5 error-path** (R6=A). Layout:
  - `tests/fixtures/dbt_project_small/` — single tiny dbt project (3–5 models, 1–2 sources). Pre-generated `manifest_v9.json`, `manifest_v10.json`, `manifest_v11.json`, `manifest_v12.json` committed.
  - `tests/fixtures/dbt_project_medium/` — ~50-model dbt project, v12 only.
  - `tests/fixtures/error_paths/` — five hand-crafted JSON files: `malformed.json`, `missing_version_url.json`, `unsupported_v99.json`, `disabled_only.json`, `empty_raw_code.json`.
  - `tests/fixtures/README.md` — regeneration recipes per schema version.
  *Why:* covers AC #5 ("small + medium") and the seven Phase 2 regression tests with one fixture per dimension. *How to apply:* the same tiny dbt project is reused across versions to keep storage tight; only the manifests differ.
- **DEC-013 — `ManifestError` base class + `ManifestNotFoundError`** (validated default). Hierarchy:
  ```
  ManifestError                            # base for all manifest-layer errors
   ├─ ManifestNotFoundError                # target/manifest.json absent
   ├─ UnsupportedManifestVersionError      # version outside v9–v12 (incl. v20 Fusion)
   ├─ ModelNotFoundError                   # key not found in nodes or disabled
   │   └─ ModelDisabledError               # found in disabled dict
   ├─ ModelPathOutsideProjectError         # resolved path escapes project root
   └─ ModelMissingSqlError                 # raw_code is null / empty
  ```
  *Why:* downstream stages (prune / grade / emit) can `except ManifestError` once; specific errors give actionable messages. The model-vs-manifest distinction matters for users diagnosing missing files vs missing models.
- **DEC-014 — Custom errors carry `remediation: str`** (validated default). All `ManifestError` subclasses accept a `remediation: str` constructor kwarg, defaulted to a class-level remediation string. `__str__` renders `f"{self.message}\n  ↳ Remediation: {self.remediation}"`. *Why:* the README's "explainable diffs" principle starts at the loader — failures in stage 0 should be self-documenting. *How to apply:* defaults: `ManifestNotFoundError` → "Run `dbt parse` or check `project_dir`"; `ModelMissingSqlError` → "Run `dbt parse` first — `raw_code` is empty"; `UnsupportedManifestVersionError` → "Supported: v9–v12 (dbt 1.5–1.11). v20 (Fusion) tracked as future work."
- **DEC-015 — pytest markers `unit`, `integration`, `error`** (validated default). Declared in `[tool.pytest.ini_options]` markers list under `pyproject.toml`. *Why:* `testing-signal.md` requires strict markers; pre-declaring satisfies the rule and self-documents the test taxonomy. *How to apply:* unit = no FS I/O beyond fixture path; integration = full `load()` round-trip; error = asserts an exception is raised with expected remediation.
- **DEC-016 — Light Pydantic validators** (validated default). Two field validators on `Model`:
  - `unique_id` must start with `"model."` (else raise `ValidationError` — Pydantic's, not our `ManifestError`; this is a parse-time concern).
  - `raw_code`, when not None, must be non-empty after `.strip()`. (Empty/null `raw_code` is a *resolver*-time signal handled by `ModelMissingSqlError`, not a parse-time fault — the loader must still parse manifests with empty raw_code.)
  *Why:* catches obvious manifest corruption at parse time; the strip check anchors the empty-raw_code branch. *How to apply:* `field_validator("unique_id")` raises; `field_validator("raw_code")` returns the stripped value or None — leaves the resolver to raise the typed error.
- **DEC-017 — Validated defaults bundle** (Phase 2 recommendations accepted as-is). One DEC entry covering everything not separately numbered above:
  - **Single `Model` class with optional fields** — no per-version discriminated unions.
  - **dbt-faithful snake_case names** — `raw_code`, `unique_id`, `original_file_path`. No renames.
  - **`Ref` is always dict-shape** `{name, package, version}`. Pre-1.5 string refs aren't in the supported range.
  - **`columns: dict[str, Column]`** — preserve the dbt key shape; expose `Manifest.<model>.columns_list: list[Column]` as a property for ergonomic iteration.
  - **`Manifest.get_model(key: str | Path) -> Model`** — primary resolver; auto-detects `unique_id` (starts `model.`) vs path. Builds an internal `unique_id → Model` dict on load for O(1) lookups.
  - **`Manifest.iter_models() -> Iterator[Model]`** + **`Manifest.schema_version: str`** property — debugging / introspection surface.
  - **`extra="ignore"` in production**; **`extra="forbid"` in fixture-validation tests** — drift detector.
  - **No logging, metrics, coverage gating, or hypothesis** in v0.1 — defer to v0.2 when the LLM stage gives them a concrete consumer.
  - **`__all__` = `["load", "Manifest", "Model", *<error_classes>]`** — internals (`_loader`, `_path_helpers`) prefixed `_`.
  - **No `compiled_code` / no `parent_map` / no `child_map` surfaced** in the public API. They're parsed (Pydantic walks them) but not exposed; later tickets add them as needs surface.
  - **Stay manifest-only** — future enrichment (catalog, run_results, freshness) goes into a composing `ModelContext` in v0.2, never into `Model` itself.
  *Why:* every item above carries a Phase 2 reviewer recommendation with a concrete rationale (recorded in the Architecture Review table); accepting the bundle as one DEC keeps the log readable.

## Detailed Breakdown

Nine stories: seven implementation + Quality Gate + Patterns & Memory. Ordering follows the natural Python module dependency chain (deps → fixtures → errors → models → loader → public API → tests/docs → gate → patterns). Validation command (CLAUDE.md is source of truth): `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`.

### US-001 — Add Pydantic v2 + dbt-core deps and pytest markers

**Description.** Wire the runtime and dev dependencies this ticket needs and pre-declare pytest markers so test collection stays strict. No application code yet.

**Traces to:** DEC-001, DEC-009, DEC-015.

**Files.**

- `pyproject.toml` (modify):
  - `[project] dependencies = ["pydantic>=2.5,<3"]` (new — first runtime dep in the package).
  - `[project.optional-dependencies] dev` adds `"dbt-core>=1.8,<2"` next to existing `ruff`, `pyright`, `pytest`.
  - `[tool.pytest.ini_options]` add `markers = ["unit: unit-level tests with no FS I/O beyond fixtures", "integration: tests exercising the full load() round-trip", "error: tests asserting an exception with its remediation"]`.

**TDD.** Not applicable — pure config wiring.

**Acceptance.**
- `pip install -e ".[dev]"` resolves Pydantic v2 and dbt-core 1.8+ in a fresh venv.
- `python -c "import pydantic; print(pydantic.VERSION)"` prints `2.x`.
- `pytest --collect-only` does not warn about unknown markers when a placeholder `@pytest.mark.unit` test is added (and reverts).

**Done when.** Validation command passes (no real test changes yet — markers are just declared).

**Depends on.** none.

---

### US-002 — Test fixtures: tiny dbt project + multi-version manifests + error-path JSON

**Description.** Build the fixture corpus per DEC-012. One small dbt project (3–5 models, 1–2 sources) reused to generate four schema versions; one medium-sized v12 project; five hand-crafted error-path JSON files; a regeneration recipe.

**Traces to:** DEC-005, DEC-009, DEC-012.

**TDD.** Not applicable — fixture construction.

**Files.**

- `tests/fixtures/dbt_project_small/` (new):
  - `dbt_project.yml`, `profiles.yml.example` (DuckDB profile — no warehouse access required at fixture-gen time).
  - `models/staging/stg_users.sql`, `models/staging/stg_orders.sql`, `models/marts/dim_users.sql`, `models/marts/fct_orders.sql`. ~5 models with refs + at least one disabled-via-config example.
  - `models/sources.yml` with at least one source referenced by a staging model.
  - `target/manifest_v9.json`, `manifest_v10.json`, `manifest_v11.json`, `manifest_v12.json` (committed; pre-generated).
- `tests/fixtures/dbt_project_medium/` (new): a synthesised ~50-model project (ok to script-generate the SQL files); only `target/manifest_v12.json` committed.
- `tests/fixtures/error_paths/` (new):
  - `malformed.json` — truncated JSON.
  - `missing_version_url.json` — valid JSON but `metadata.dbt_schema_version` absent.
  - `unsupported_v99.json` — version URL points at v99.
  - `disabled_only.json` — a model present **only** in the `disabled` dict (not in `nodes`).
  - `empty_raw_code.json` — a model with `raw_code: ""`.
- `tests/fixtures/README.md` (new): per-version regeneration recipes. v12 uses the in-`[dev]` `dbt-core>=1.8`; v9/v10/v11 use ephemeral `uvx dbt-core==1.5.x|1.6.x|1.7.x dbt parse`. Documents the DuckDB profile so contributors don't need a real warehouse.
- `tests/fixtures/regenerate.sh` (new): orchestrates the four versions; idempotent.

**Acceptance.**
- All committed JSON files parse with stdlib `json.load` (sanity smoke).
- The four small manifests' `metadata.dbt_schema_version` URLs end in `v9.json`, `v10.json`, `v11.json`, `v12.json`.
- The medium manifest contains ≥ 50 entries in `nodes` with `resource_type == "model"`.
- `regenerate.sh` runs to completion against `dbt_project_small/` and produces a manifest matching the committed v12 file (modulo timestamp fields — script should normalise/strip these).

**Done when.** All committed fixtures load cleanly; the regeneration script is documented and dry-run-tested locally.

**Depends on.** US-001.

---

### US-003 — Manifest errors module

**Description.** The typed exception hierarchy per DEC-013 + DEC-014, with `remediation: str` rendered in `__str__`. Pure-Python — no Pydantic, no FS I/O.

**Traces to:** DEC-013, DEC-014.

**TDD.** Yes. Workflow: write failing tests for the hierarchy + `__str__` rendering, then implement.

Specific tests:
- `ManifestError` is the base; every other error class is a subclass.
- `ModelDisabledError` is also a subclass of `ModelNotFoundError` (so `except ModelNotFoundError` catches both).
- Each error class has a sensible default `remediation`.
- `str(err)` includes both the message and a `Remediation:` line.
- Constructing with an explicit `remediation=` overrides the default.

**Files.**

- `src/signalforge/manifest/__init__.py` (new): empty for now (US-006 fills the re-exports).
- `src/signalforge/manifest/errors.py` (new): seven classes per DEC-013 + the `remediation` machinery.
- `tests/manifest/test_errors.py` (new), marked `@pytest.mark.unit`. ≥ 5 assertions covering: hierarchy, `__str__` shape, default remediation, override remediation, `ModelDisabledError`-is-also-`ModelNotFoundError`.

**Acceptance.**
- Validation command passes.
- `pytest tests/manifest/test_errors.py -m unit` shows the new tests passing and there is at least one assertion that would fail if the inheritance graph is broken (e.g. `assert issubclass(ModelDisabledError, ModelNotFoundError)`).

**Done when.** Errors module + its unit tests committed and green; no other module imports `errors.py` yet.

**Depends on.** US-001.

---

### US-004 — Manifest Pydantic models module

**Description.** Define the typed shape per DEC-001, DEC-011, DEC-016, DEC-017. `Manifest`, `Model`, `Column`, `Ref`, `Config` Pydantic v2 BaseModel subclasses. Frozen + extra=ignore. Light validators on `Model.unique_id` and `Model.raw_code`. `columns_list` property. No file IO, no version detection — that's US-005.

**Traces to:** DEC-001, DEC-011, DEC-016, DEC-017.

**TDD.** Yes. Tests written against the four small fixtures from US-002.

Specific tests (file: `tests/manifest/test_models.py`, mostly `@pytest.mark.unit`):
- `Model.model_validate(...)` succeeds for a representative node from `manifest_v12.json` and round-trips the snake_case fields (`unique_id`, `raw_code`, `original_file_path`).
- `Model.unique_id` validator raises `pydantic.ValidationError` when given `"foo.bar"` (not starting with `model.`).
- `Model.raw_code` validator returns `None` when fed an empty/whitespace string (so the resolver can raise `ModelMissingSqlError` cleanly later).
- `Model.config.tags` and `Model.tags` are independent fields.
- `Model.columns_list` returns the same items as `Model.columns.values()` in the same order.
- `Manifest.model_validate(...)` succeeds against each of `manifest_v9.json`, `_v10.json`, `_v11.json`, `_v12.json` (parametrised) — proves `extra="ignore"` survives the cross-version differences.
- A second class-level config override with `extra="forbid"` is constructed in one test, fed `manifest_v12.json`, and asserted to raise (drift detector for fixtures).
- `Ref` parses both shapes? — only dict shape is in our supported range; assert dict shape parses, document that pre-1.5 strings are out of scope (no test that *accepts* the string form).

**Files.**

- `src/signalforge/manifest/models.py` (new): `Manifest`, `Model`, `Column`, `Ref`, `Config`. Each `model_config = ConfigDict(frozen=True, extra="ignore")`. `columns_list` as a `@computed_field` or `@property`.
- `tests/manifest/test_models.py` (new), parametrised across the four small manifest fixtures.

**Acceptance.**
- Validation command passes; pyright is clean against `models.py`.
- The parametrised fixture-loading test passes for all four schema versions.
- The `extra="forbid"` drift-detector test asserts `pydantic.ValidationError` (a *would-fail* test if we ever silently expand the schema without updating `Model`).

**Done when.** Models + tests committed and green.

**Depends on.** US-002, US-003.

---

### US-005 — Manifest loader module

**Description.** The `loader.py` heart of the module: `load()` + version detection + soft-size warning + symlink-hardened path resolver + `Manifest.get_model()` indexing. Implements DEC-007, DEC-008, DEC-010, plus the resolver / `iter_models` / `schema_version` machinery from DEC-017.

**Traces to:** DEC-007, DEC-008, DEC-010, DEC-013, DEC-014, DEC-017.

**TDD.** Yes — this is the highest-stakes module. Carry forward the seven Phase 2 regression tests verbatim.

Specific tests (file: `tests/manifest/test_loader.py`):
- *(unit)* Version detection: `_detect_version("https://schemas.getdbt.com/dbt/manifest/v12.json")` returns `12`. Malformed URL falls back to feature-sniff (`unit_tests` key present → 12).
- *(integration)* `load(small_project_dir) -> Manifest` for the v12 fixture; assert `manifest.schema_version == "v12"` and `len(list(manifest.iter_models())) == 5` (or whatever the fixture has).
- *(integration)* `load(small_project_dir, manifest_path=…)` accepts an explicit override pointing at `manifest_v9.json`; assert version is `v9`.
- *(integration)* `manifest.get_model("model.demo.dim_users")` returns the same `Model` as the file-path lookup `manifest.get_model("models/marts/dim_users.sql")` and `manifest.get_model("/abs/path/to/models/marts/dim_users.sql")`.
- *(integration)* `manifest.get_model(...)` for a node that exists only in `manifest.disabled` raises `ModelDisabledError`; the same call for a totally-unknown id raises `ModelNotFoundError`.
- *(error)* Path-traversal: a symlink inside the fixture project pointing to `/etc/hostname` is created in a `tmp_path` copy; `manifest.get_model("models/symlink.sql")` raises `ModelPathOutsideProjectError`. (Skip on Windows — symlinks need admin there.)
- *(error)* `load(...)` against a project whose `target/manifest.json` is absent raises `ManifestNotFoundError`; the rendered `str(err)` includes "Run `dbt parse`".
- *(error)* `load(...)` against `error_paths/unsupported_v99.json` (passed via `manifest_path`) raises `UnsupportedManifestVersionError`; rendered string includes the supported range.
- *(error)* `load(...)` against `error_paths/empty_raw_code.json` succeeds at *load* time (loader doesn't pre-validate raw_code), but `manifest.get_model("model.demo.empty")` raises `ModelMissingSqlError`. (This proves the empty-raw-code branch fires at resolve-time, not at parse-time per DEC-016.)
- *(unit)* The 200 MB warning: monkey-patch `MAX_MANIFEST_BYTES` down to 100 bytes and assert `pytest.warns(UserWarning, match="MB resident memory")` when loading any real fixture.

**Files.**

- `src/signalforge/manifest/loader.py` (new):
  - `MAX_MANIFEST_BYTES = 200 * 1024 * 1024`
  - `def load(project_dir, manifest_path=None) -> Manifest`
  - `_canonicalise_path(path, project_dir) -> Path` (private; symlink hardening per DEC-007)
  - `_detect_version(metadata_url, manifest_dict) -> int` (private)
  - `Manifest.get_model(key)` is a method on the model itself (declared in `models.py` per DEC-017) but its *implementation body* — building the `unique_id → Model` index, dispatching to file-path lookup — is a private helper here that `models.py` calls via a `@model_validator(mode="after")` to populate the index. Alternative: `get_model` lives in `loader.py` as a free function and `Manifest.get_model` calls it. Pick the cleaner shape during implementation; either preserves the public API.
- `tests/manifest/test_loader.py` (new): the ≥ 9 tests above.

**Acceptance.**
- Validation command passes.
- All seven Phase 2 regression tests appear in the suite and pass.
- The path-traversal test creates a real symlink and confirms it's rejected (skipped only on Windows, with a documented marker).
- The size-warning test uses `pytest.warns(UserWarning)`.

**Done when.** Loader + tests committed and green.

**Depends on.** US-002, US-003, US-004.

---

### US-006 — Public API re-exports + manifest subpackage `__init__.py`

**Description.** Tie the subpackage together by publishing the documented surface (DEC-017): `load`, `Manifest`, `Model`, and the seven error classes. Internal helpers (`_loader_helpers`, etc.) are not re-exported.

**Traces to:** DEC-017, DEC-022 (the `__all__` policy in DEC-017's bundle).

**TDD.** Yes — small but worth a fail-capable check.

Specific tests (file: `tests/manifest/test_public_api.py`, `@pytest.mark.unit`):
- `from signalforge.manifest import load, Manifest, Model, ManifestError, ManifestNotFoundError, ModelNotFoundError, ModelDisabledError, ModelPathOutsideProjectError, ModelMissingSqlError, UnsupportedManifestVersionError` succeeds.
- `signalforge.manifest.__all__` matches the list above (set equality — order is documentation).
- `from signalforge.manifest._loader_helpers import _detect_version` raises `ImportError` *or* the symbol does not exist as a top-level attribute. (Prevents accidental promotion of internals.)

**Files.**

- `src/signalforge/manifest/__init__.py` (modify): the re-export block + `__all__`.
- `tests/manifest/test_public_api.py` (new).

**Acceptance.**
- All names in `__all__` are importable; no extras.
- Internals (`_*`-prefixed names) are reachable as `signalforge.manifest._loader.<x>` only via the dotted module, never bare on the package.

**Done when.** Tests pass; pyright is clean.

**Depends on.** US-003, US-004, US-005.

---

### US-007 — Documentation: research index, ops guide, CONTRIBUTING update

**Description.** Three documentation drops — the vendored research files (already copied in Phase 1) get an index; a new ops guide records expected memory profiles per DEC-008; `CONTRIBUTING.md` documents the new pytest markers and fixture regeneration recipe.

**Traces to:** DEC-006, DEC-008, DEC-009, DEC-012, DEC-015.

**TDD.** Not applicable — documentation.

**Files.**

- `docs/research/README.md` (new): one-paragraph framing per file (`dbt-claude-technical-surface.md` is "Section 1.1 is the canonical schema reference for the manifest module"; the others are background). State that these are pinned snapshots from `clauditor/docs/temp/` and how to refresh them.
- `docs/manifest-loader-ops.md` (new): the size-vs-memory table (`small ~50 KB → ~50 MB resident`, `medium ~5 MB → ~50–150 MB`, `large ~30+ MB → ~300–500+ MB`); the 200 MB soft-warning threshold and how to override it; the multi-version regeneration recipe (cross-link to `tests/fixtures/README.md`).
- `CONTRIBUTING.md` (modify): add a "Test markers" subsection (declares `unit`/`integration`/`error` and when to use each); add a "Regenerating fixtures" cross-link.
- `README.md` (modify): one-line update to the v0.1 status callout from #1 — replace "CLI ships in a later v0.1 ticket" with "library API lands first; CLI in a later v0.1 ticket" once the manifest API is real.

**Acceptance.**
- Markdown lint clean (or no lint configured — not gating).
- Each new doc is ≤ 80 lines.
- `docs/research/README.md` references each of the seven vendored files at least once.

**Done when.** Files committed; preview rendered locally (`gh pr view --web` after Phase 5).

**Depends on.** US-005.

---

### US-008 — Quality Gate

**Description.** Sweep the full changeset before merge. Run code-reviewer 4 times; on each pass, fix every real bug found before the next pass. Run CodeRabbit if available. End on green validation.

**Traces to:** all DEC-### in this plan.

**TDD.** N/A.

**Acceptance.**
- 4 sequential code-reviewer passes; each pass either finds no issues or every found issue is fixed before the next.
- CodeRabbit (if available) — all real issues addressed.
- Final validation green: `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`.
- `git status` clean.

**Done when.** All passes report no remaining real issues *and* validation exits 0.

**Depends on.** US-001 … US-007.

---

### US-009 — Patterns & Memory

**Description.** Capture conventions established here so future Claude Code sessions inherit them.

**Traces to:** DEC-001, DEC-007, DEC-008, DEC-013, DEC-014, DEC-017.

**Files.**

- `.claude/rules/manifest-readers.md` (new, ≤ 40 lines): "external-format readers in this repo". Codifies: Pydantic v2 with `frozen=True, extra="ignore"`; symlink-hardened path resolution as `_canonicalise_path` helper; `ManifestError` base + `remediation: str` pattern for explainable errors; "no logging / metrics in stage-0 modules — defer to the stage that *consumes* the data".
- `.claude/rules/testing-signal.md` (modify): append a section noting fixture regeneration via ephemeral `uvx`/`pipx` for cross-version coverage; and the fixture-drift detector pattern (`extra="forbid"` in one test).
- `CLAUDE.md` (modify): under "Repository status", note that the manifest module has shipped; add a `## Public API surface` mini-section listing `signalforge.manifest` as the first stable v0.1 surface.

**Acceptance.**
- Rule files ≤ 40 lines each, project-tone-consistent.
- Future `/super-plan` Convention Checker subagent picks them up in Phase 1.
- Auto-memory entries (if any) record any genuinely surprising lessons (e.g. the symlink trap, the `extra="forbid"`-in-tests trick).

**Done when.** Files committed; `git status` clean.

**Depends on.** US-008.

---

### Story dependency graph

```
US-001 ──┬──► US-002 ──┐
         └──► US-003 ──┼──► US-004 ──► US-005 ──► US-006 ──► US-008 ──► US-009
                       │                                  ▲
                       └──────────────────────────────────┘
                                                          │
                                            US-007 ───────┘
```

US-002 and US-003 can run in parallel after US-001. US-004 needs both. US-005 follows US-004. US-006 follows US-005. US-007 follows US-005 (docs need the API to exist). US-008 (Quality Gate) waits on all implementation. US-009 (Patterns & Memory) waits on US-008.

### Rules-compliance gate

Each story above respects the project's three rules:

- **`python-build.md`** — code under `src/signalforge/manifest/`; tests under `tests/manifest/`; no `tests/__init__.py` or `tests/manifest/__init__.py`. The wheel target list `packages = ["src/signalforge"]` already covers the new subpackage (DEC-002).
- **`testing-signal.md`** — every test sketched above can fail on a real regression (verified per-story). Markers declared in US-001 satisfy strict-markers. No `assert True`-shaped tests.
- **`ci-supply-chain.md`** — no CI changes in this ticket (no new workflows or actions). Informational only.

## Beads Manifest

Created 2026-04-27 from the worktree (`bd` is worktree-aware — auto-discovers the canonical `.beads/` in the original checkout; no symlink or env var needed). Database prefix `bd_1-scaffolding` is fixed from when bd was first initialised under #1; suffixes are unique per epic.

**Epic:** `bd_1-scaffolding-28p` — *2: dbt manifest loader (read manifest.json + resolve a single model)* (P1, `external-ref=gh-2`)

| Bead ID | Story | Priority | Blocked by |
| --- | --- | --- | --- |
| `bd_1-scaffolding-28p.1` | US-001 — pyproject deps (Pydantic v2 + dbt-core) + pytest markers | P1 | — (READY) |
| `bd_1-scaffolding-28p.2` | US-002 — Test fixtures (tiny dbt project + 4 schema versions + medium + 5 error-path) | P2 | `.1` |
| `bd_1-scaffolding-28p.3` | US-003 — Manifest errors module (TDD) | P2 | `.1` |
| `bd_1-scaffolding-28p.4` | US-004 — Manifest Pydantic models module (TDD) | P2 | `.2`, `.3` |
| `bd_1-scaffolding-28p.5` | US-005 — Manifest loader module (TDD; carries 7 regression tests) | P1 | `.4` |
| `bd_1-scaffolding-28p.6` | US-006 — Public `__init__.py` re-exports + private-symbol guard | P3 | `.5` |
| `bd_1-scaffolding-28p.7` | US-007 — Documentation (research index + ops guide + CONTRIBUTING update) | P3 | `.5` |
| `bd_1-scaffolding-28p.8` | US-008 — Quality Gate (4× code-reviewer + CodeRabbit) | P2 | `.6`, `.7` |
| `bd_1-scaffolding-28p.9` | US-009 — Patterns & Memory | P4 | `.8` |

10 `blocks` edges. `bd ready` returns only `.1` (and the epic itself).

**To start work:** `cd` to either the local worktree or the canonical checkout (bd works from both — auto-discovers the canonical `.beads/`), run `bd ready`, claim `.1` with `bd update bd_1-scaffolding-28p.1 --status=in_progress`, and follow the US-001 spec in this plan doc. After US-001 lands, `bd ready` will return `.2` and `.3` in parallel.

**Note on Dolt sync:** Same caveat as #1 — bark configured a Dolt remote pointing at `git+ssh://git@github.com/wjduenow/SignalForge.git`, but GitHub does not host Dolt servers. Local `.beads/` is the only persistence; resolve when picking a long-term stance per [#13](https://github.com/wjduenow/SignalForge/issues/13).
