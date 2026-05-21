# Super plan — #105: `signalforge prune-existing` CLI subcommand

## Meta

- **Ticket:** [#105](https://github.com/wjduenow/SignalForge/issues/105) — feat: `signalforge prune-existing` CLI subcommand (prune external schema.yml tests)
- **Phase:** published (awaiting approval) — PR [#107](https://github.com/wjduenow/SignalForge/pull/107)
- **Branch / worktree:** `feature/105-prune-existing-cli` @ `/home/wesd/Projects/worktrees/SignalForge/105-prune-existing-cli` (off `dev`)
- **Blocked by:** #104 (library seam) — **landed** at `7a97a6d`, so this is unblocked.
- **Sessions:** 1 (2026-05-21)

---

## What & why

#104 shipped the **library** seam — `signalforge.ingest.read_schema(schema, model, *, project_dir=None) -> IngestResult` — which parses an externally-authored dbt `schema.yml` into the typed `CandidateSchema` the prune engine consumes. This issue adds the **operator-facing CLI** on top of it:

```
signalforge prune-existing <model> --schema <path>
```

runs **ingest → prune → diff** (no draft, no grade, **no LLM call**) and reports kept / kept-uncertain / dropped, plus a summary of skipped (unsupported) tests. The product story: *point SignalForge at your existing dbt tests and let the warehouse tell you which ones add no signal* — extends Architectural Commitment #1 ("signal over volume") to any generator's tests.

---

## Discovery (Phase 1)

### Codebase findings

- **Library is fully shipped.** `signalforge.ingest` (reader, parser, anchor, models, errors) landed in #104. `read_schema(schema: str | Path, model, *, project_dir=None) -> IngestResult`; `Path` = file, `str` = raw YAML.
- **Exit-code taxonomy is pre-wired.** All five `IngestError` concretes are already registered in `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` (`IngestSchema{NotFound,Parse,TooLarge}Error` → tier 1; `IngestModelNotFoundError` / `IngestAnchorContractError` → tier 2). `IngestError` is in `_EXCEPTION_MAPPING_EXCLUDED_BASES`; scan-7 discovery count is already 11. **#104 DEC-004 explicitly anticipated #105 inheriting the mapping with zero rework.**
- **Two patterns to mirror.** `signalforge.cli.generate.cmd_generate` for prune→diff wiring (`load_prune_config` → `prune_tests` → `load_diff_config` → `render_diff` → `render_to_text`; `prune_tests` owns the `with adapter:` block per #22 DEC-013). `signalforge.cli.init_demo` for the flat one-module-per-subcommand shape and the single `try/except Exception` boundary catch.
- **Bare-name resolver exists in the wrong place.** `_resolve_model_for_lint` lives in `lint.py`; `cli-layer.md` § "Bare-name model resolution" says *hoist it to `_helpers` rather than copy-paste* when a second subcommand needs it — which `prune-existing`'s positional `<model>` does.
- **Helper seams confirmed:** `print_stderr` (ANSI-safe sink), `should_emit_progress` / `emit_progress_entry` / `emit_progress_done`, `format_error_to_stderr`, `map_exception_to_exit_code`, `canonicalise_user_path` all present in `_helpers.py`.

### Key architecture concern surfaced in discovery

`prune_tests(model, adapter, candidates, manifest, *, config, audit_path, project_dir)` **never consumes a `SafetyPolicy`**, and this path makes **no LLM call**. DEC-011 of the #104 plan listed `--mode {schema-only,aggregate-only,sample}` (the safety sampling mode) in the inherited flag spec — but that knob shapes the LLM payload, which doesn't exist here. Keeping it would ship a **dead flag** (counter to "signal over volume"). The genuinely-relevant warehouse knobs are prune's `--scope` / `--sample-strategy` (#22). → resolved by DEC-002.

### Scoping answers

| Q | Answer |
|---|---|
| `--mode` (inert, no LLM)? | **Drop `--mode`; add `--scope {sample,full}` + `--sample-strategy {oneshot,materialised}`.** No dead flags. |
| Diff framing? | **Feed the external schema.yml as `existing_schema`** → unified diff shows what to remove from the operator's real file. |
| `--write` semantics? | **Read-only v1 — no `--write`.** Print the diff; sidecar to `.signalforge/diff.json` by default; `--dry-run` suppresses the sidecar. |

---

## Architecture review (Phase 2)

| Area | Rating | Finding |
|---|---|---|
| Security | **pass** | No new attack surface. `--schema` routes through `canonicalise_user_path` (symlink/containment, `CliPathError`); ingest applies `yaml.safe_load` + size-cap-before-parse (DEC-005 of #104); identifier safety is enforced downstream in prune (`_sql_safety`). No secrets, no new network beyond what `prune_tests` already does. |
| CLI / exit-code taxonomy | **pass** | Ingest errors are **already** in `_EXCEPTION_TO_EXIT_CODE` (#104 DEC-004) → no new error classes, no scan-7 count bump (DEC-006). The single boundary catch + `map_exception_to_exit_code` MRO walk gives correct tiers. Obligations: subprocess `--help` smoke + 5-surface parity for the new flag set. |
| Data model / API | **pass** | No new typed result models — reuses `IngestResult` / `PruneResult` / `DiffReport`. `cmd_prune_existing(args) -> int` + `add_parser(subparsers)` match the per-subcommand contract. |
| Reformatting noise | **concern (accepted)** | Feeding the external schema.yml as `existing_schema` (DEC-004) means the proposed YAML — re-emitted from the kept `CandidateSchema` via the diff emitter — may reorder keys / normalise whitespace vs the operator's hand-authored file, adding cosmetic diff lines. Accepted + documented in `cli-ops.md`; the kept/dropped table (the load-bearing signal) is unaffected. |
| Testing strategy | **pass** | In-process `main(argv)` against an Austin-aligned schema.yml fixture + fake warehouse adapter (mirrors generate's `_make_warehouse_adapter` patch seam, DEC-009). No-traceback floor on every test. Subprocess `--help` under `cli_subprocess` marker. |
| Observability | **pass** | Stderr stage progress (3 stages: ingest → prune → diff) via `should_emit_progress`; skipped-test report via `print_stderr` (ANSI-safe). Any `_LOGGER` use obeys the lazy-format JSON grep gate (covers `cli/`). |
| Performance | **pass** | Single-file YAML parse; warehouse cost is exactly what `prune_tests` already incurs (no draft/grade LLM calls — strictly cheaper than `generate`). |

**Blockers:** none.

---

## Refinement decisions (Phase 3)

### DEC-001 — Flat subcommand module `signalforge.cli.prune_existing`
One module, exporting exactly `add_parser(subparsers) -> None` and `cmd_prune_existing(args) -> int` (cli-layer.md DEC-009). Registered in `signalforge.cli.__init__._build_parser` alongside the other four subcommands. Subcommand name `prune-existing` (hyphen; argparse handles the `args.func` dispatch).

### DEC-002 — Flag set: drop `--mode`, add prune knobs
`prune_tests` does not read the safety policy and there is no LLM call, so `--mode` would be inert. Final flag set:

- positional `<model>` (bare-name / unique_id / file-path — DEC-008)
- `--schema <path>` (**required**)
- `--project-dir`, `--manifest`, `--profiles-dir`
- `--scope {sample,full}` (overrides `PruneConfig.scope`)
- `--sample-strategy {oneshot,materialised}` (overrides `PruneConfig.sample_strategy`)
- `--format {ansi,markdown,json}` (default `ansi`)
- `--dry-run` (suppresses sidecar — DEC-003; **no `--write`**)
- `--quiet` / `--verbose` / `--no-color`

`--scope` / `--sample-strategy` overrides applied via `PruneConfig.model_validate({**dump, ...})` (NOT `model_copy(update=...)`) so validators re-run — mirrors `generate` (#22 DEC-012). **Dropped vs. generate:** `--mode`, `--min-score`, `--estimate`, `--select`, `--write`.

### DEC-003 — Read-only w.r.t. the operator's schema.yml; no `--write`
The `--schema` file is hand-authored; silently overwriting it is surprising and destructive, and writing a *different* (model-dir) file like generate is equally confusing. So v1 is read-only: it prints the rendered diff to stdout and writes the `.signalforge/diff.json` sidecar by default (consistent with `render_diff` / generate defaults). `--dry-run` sets `write_sidecar=False` for a pure-stdout, zero-disk run (matches generate's `--dry-run` semantics, minus the schema.yml write that doesn't exist here). Re-pruning into the file is a possible v0.3 follow-up with a confirmation/backup story.

### DEC-004 — Feed the external schema.yml as `render_diff(existing_schema=...)`
The command's purpose is "which of *my* tests add no signal," so the deliverable is a unified diff against the operator's actual file. The same `--schema` content is passed to `render_diff`'s `existing_schema`. `grading_report=None` → the diff renders kept / kept-uncertain / dropped, never `flagged` (locked by #104 DEC-011). Reformatting-noise caveat documented (architecture review).

### DEC-005 — `--schema` path handling: canonicalise once, Path to reader, text to diff
`cmd_prune_existing` canonicalises `--schema` via `canonicalise_user_path(raw, project_dir)` (→ `CliPathError` on symlink/containment). It passes the **`Path`** to `read_schema` (so the full ingest typed-error surface — `IngestSchemaNotFoundError` / `IngestSchemaParseError` / `IngestSchemaTooLargeError` — fires and is exit-code-mapped), and reads the same canonicalised path's UTF-8 text to pass as `render_diff`'s `existing_schema`. A read failure for the existing-schema text reuses the same boundary catch.

### DEC-006 — NO bespoke `CliPruneExisting*` wrapper classes
The five `IngestError` concretes are **already** first-class in `_EXCEPTION_TO_EXIT_CODE` (#104 DEC-004 landed them precisely so #105 needs no rework) and already carry remediation. The single boundary `try/except Exception` (`format_error_to_stderr` + `map_exception_to_exit_code`) is the homogeneous catch surface — adding five mechanical wrappers would be ceremony that earns nothing (and contradicts #104 DEC-004's stated intent). This resolves the issue's loosely-worded AC ("each IngestError subclass wraps into a Cli* error") in favour of the pre-registered direct mapping. `ModelNotFoundError` (manifest layer) and `PruneError` / `WarehouseError` / `DiffError` subclasses are likewise already mapped. **Flagged for PR review** — if review prefers explicit wrappers, they are a small mechanical addition that the 7th AST scan would auto-gate.

### DEC-007 — Skipped-test report: stderr summary + `--verbose` detail
After `read_schema`, when `IngestResult.skipped` is non-empty and not `--quiet`, emit one `print_stderr` summary line grouped by `SkipReason`:

```
Skipped 3 unsupported tests: custom-or-generic-test×2, unsupported-test-type×1
```

Under `--verbose`, follow with one indented line per `SkippedTest` (`test_name`, `column`, `reason`, `detail`). `print_stderr` is the ANSI-safe sink (not `_LOGGER` — this is operator-facing info, not a log event; keeps it off the lazy-format grep gate). `--quiet` suppresses both.

### DEC-008 — Hoist the bare-name model resolver to `_helpers`
Move `_resolve_model_for_lint(manifest, key) -> Model` → `signalforge.cli._helpers._resolve_model_by_key(manifest, key) -> Model` (rename for generality). `lint.py` delegates to it (no behaviour change); `prune_existing.py` reuses it for its positional `<model>`. Pure refactor; existing lint resolver tests keep passing (re-pointed at the new location or via the lint delegation).

### DEC-009 — `prune_existing` owns a thin `_make_warehouse_adapter`
Define a trivial module-level `_make_warehouse_adapter(profile) -> WarehouseAdapter` (`return WarehouseAdapter.from_profile(profile)`) in `prune_existing.py`, mirroring generate's seam. Tests patch `signalforge.cli.prune_existing._make_warehouse_adapter` (patch-where-used) with a `FakeBigQueryClient`-backed adapter. Avoids importing a private symbol across sibling command modules and leaves generate's existing test patch target untouched. `prune_tests` owns the `with adapter:` block (#22 DEC-013), so the handler passes an un-entered adapter.

### DEC-010 — 3-stage progress UX
Progress to stderr mirrors generate but with three stages: `1/3 ingest`, `2/3 prune`, `3/3 diff`. `should_emit_progress(quiet, verbose)` gates (both stdout+stderr TTY); `--quiet` suppresses, `--verbose` forces on. Computed `<fact>` fields from objects in scope (candidate test count, kept/dropped counts) — never a hardcoded duration.

---

## Detailed breakdown (Phase 4)

Architecture ordering: shared refactor → contract surface (parser) → orchestrator → docs/parity → subprocess smoke → Quality Gate → Patterns & Memory. Every story's AC includes the canonical validation command: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.

### US-001 — Hoist bare-name model resolver to `_helpers`
**Traces to:** DEC-008.
**Description:** Move `_resolve_model_for_lint` from `lint.py` to `signalforge.cli._helpers` as `_resolve_model_by_key(manifest: Manifest, key: str) -> Model` (identical body: unique_id/file-path branch via `Manifest.get_model`; bare-name branch via `iter_models` with multi-match disambiguation + disabled-model exclusion). `lint.py` imports and delegates. No behaviour change.
**AC:** `_resolve_model_by_key` lives in `_helpers`; `lint.py` delegates; existing `tests/cli/test_lint.py::test_lint_resolves_model_*` pass unchanged (re-pointed if they patched the private name); validation command passes.
**Files:** `src/signalforge/cli/_helpers.py`, `src/signalforge/cli/lint.py`, `tests/cli/test_lint.py` (import path only if needed).
**Depends on:** none.

### US-002 — `prune-existing` `add_parser` (contract surface first)
**Traces to:** DEC-001, DEC-002, DEC-003.
**Description:** Add `src/signalforge/cli/prune_existing.py` with `add_parser(subparsers)` registering the full flag set (DEC-002): positional `<model>`, required `--schema`, `--project-dir`, `--manifest`, `--profiles-dir`, `--scope {sample,full}`, `--sample-strategy {oneshot,materialised}`, `--format {ansi,markdown,json}` (default `ansi`), `--dry-run`, `--quiet`/`--verbose`/`--no-color`. `set_defaults(func=cmd_prune_existing)` (stub `cmd_prune_existing` returning 0 lands fully in US-003). Register `prune_existing_cmd.add_parser(subparsers)` in `signalforge.cli.__init__._build_parser`. Help strings written per 5-surface parity (surface 1).
**AC:** `signalforge prune-existing --help` exits 0 and lists every flag; `--schema` is required (argparse exits 2 if omitted); `--scope`/`--sample-strategy`/`--format` reject typos via `choices` (exit 2); registered in `main`; no `--mode`/`--write`/`--min-score`; validation command passes.
**TDD:** parser builds; required `--schema` enforced; `choices` rejection; subcommand present in `_build_parser`.
**Files:** `src/signalforge/cli/prune_existing.py`, `src/signalforge/cli/__init__.py`, `tests/cli/test_prune_existing.py`.
**Depends on:** none.

### US-003 — `cmd_prune_existing` orchestrator (ingest → prune → diff, TDD)
**Traces to:** DEC-002 … DEC-010.
**Description:** Implement `cmd_prune_existing(args) -> int`. Steps inside one `try/except Exception` boundary (DEC-016 of cli-layer.md): set `NO_COLOR`/`DBT_PROFILES_DIR` env per existing CLI convention; resolve `project_dir`; load manifest; resolve `<model>` via `_resolve_model_by_key` (US-001); canonicalise `--schema` via `canonicalise_user_path` (DEC-005); `read_schema(schema_path, model, project_dir=project_dir)` → `IngestResult`; emit skipped-test report (DEC-007); load+override `PruneConfig` (`--scope`/`--sample-strategy` via `model_validate`, DEC-002); build adapter via `_make_warehouse_adapter` (DEC-009); `prune_tests(model, adapter, result.candidate, manifest, config=prune_config, project_dir=project_dir)`; read `--schema` text for `existing_schema` (DEC-004); `render_diff(model, result.candidate, prune_result, grading_report=None, existing_schema=<text>, config=diff_config, write_sidecar=not dry_run, project_dir=project_dir)`; `render_to_text` → stdout. 3-stage progress (DEC-010). Boundary catch → `format_error_to_stderr` + `map_exception_to_exit_code`.
**AC:** Against an Austin-aligned fixture (in-process `main(["prune-existing", <model>, "--schema", <path>, "--project-dir", <dir>])` with a fake adapter), exit 0; stdout carries the rendered diff; unsupported tests land in the stderr skipped summary; `IngestModelNotFoundError`/`IngestAnchorContractError` → exit 2, `IngestSchema*Error` → exit 1, no traceback on any path (`"Traceback" not in stderr`); `--dry-run` writes no sidecar; `--format json` emits JSON; validation command passes.
**TDD:** happy path (kept/dropped table + unified diff vs the fixture schema.yml); skipped-report (summary + `--verbose` detail); each error path → correct exit code + no-traceback floor; `--dry-run` no-disk; bare-name vs unique_id `<model>` resolution.
**Files:** `src/signalforge/cli/prune_existing.py`, `tests/cli/test_prune_existing.py`, `tests/fixtures/ingest/` (Austin-aligned `schema.yml` whose four supported test types reference real `stg_bikeshare_trips` columns + ≥1 unsupported test for the skip path; reuse/extend `schema_codegen_shaped.yml` where it fits).
**Depends on:** US-001, US-002.

### US-004 — `docs/cli-ops.md` § `prune-existing` + 5-surface parity test
**Traces to:** DEC-002 … DEC-007, cli-layer.md "Multi-surface parity".
**Description:** Add a `signalforge prune-existing` section to `docs/cli-ops.md` (flag reference, exit codes, stderr shapes incl. the skipped-test summary line, the read-only/no-`--write` note, the unified-diff-vs-your-file framing + reformatting caveat). Add `tests/cli/test_5_surface_parity_prune_existing.py` asserting the flag set + key example tokens appear consistently across argparse help, the `cmd_prune_existing` docstring, `docs/cli-ops.md`, and this plan's DEC list (mirrors `test_5_surface_parity_select.py`).
**AC:** parity test green across all surfaces; `uv run mkdocs build` clean; validation command passes.
**Files:** `docs/cli-ops.md`, `tests/cli/test_5_surface_parity_prune_existing.py`.
**Depends on:** US-003.

### US-005 — Subprocess `prune-existing --help` smoke
**Traces to:** cli-layer.md "Subprocess-gated smoke pattern" (DEC-018).
**Description:** Add a `@pytest.mark.cli_subprocess` test to `tests/cli/test_subprocess_smoke.py` running `subprocess.run(["signalforge", "prune-existing", "--help"])`: asserts `returncode == 0`, a subcommand-unique stdout token (e.g. `--schema`), and the no-traceback floor on stderr. Same marker as the existing four `--help` smokes (no new gated marker).
**AC:** `pytest -m cli_subprocess --no-cov` green incl. the new test; validation command passes.
**Files:** `tests/cli/test_subprocess_smoke.py`.
**Depends on:** US-002.

### US-006 — Quality Gate (code review ×4 + CodeRabbit)
**Traces to:** all implementation stories.
**Description:** Run the code reviewer 4 times across the full changeset, fixing every real bug each pass. Run CodeRabbit if available. Re-run the canonical validation command until green; run the gated markers (`pytest -m cli_subprocess --no-cov`).
**AC:** 4 review passes complete with fixes applied; validation command + gated markers pass.
**Depends on:** US-001 … US-005.

### US-007 — Patterns & Memory (priority 99)
**Traces to:** the whole feature.
**Description:** Update `.claude/rules/cli-layer.md` (note the hoisted `_resolve_model_by_key`; the read-only `prune-existing` precedent; DEC-006's "pre-mapped lib errors → no bespoke wrappers" pattern). Update `.claude/rules/ingest-layer.md` § "Deferred: `prune-existing`" to reflect it has **shipped** (remove the "fast-follow #105" deferral wording). Update `CLAUDE.md`: add #105 to the shipped-issues list and the public-API/CLI surface (`signalforge prune-existing`). Update repo memory if a non-obvious lesson emerged (e.g. the `--mode`-is-inert finding).
**AC:** rule files + CLAUDE.md reflect the shipped CLI; validation command passes.
**Depends on:** US-006.

---

## Story dependency graph

```
US-001 ─┐
US-002 ─┼─ US-003 ─┬─ US-004 ─┐
        └───────────┴─ US-005 ┴─ US-006 (Quality Gate) ── US-007 (Patterns & Memory)
```

(US-002 and US-005 share only the parser; US-005 can start once US-002 lands. US-004 needs the orchestrator docstring from US-003.)

---

## Beads manifest (Phase 7 — devolved)

_pending devolve_
