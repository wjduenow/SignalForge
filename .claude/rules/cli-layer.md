# CLI layer (four-tier exit codes + no-traceback panic path + structured stderr)

Apply to every module under `signalforge.cli` and to any new code that maps a typed exception to an exit code, formats an error for stderr, or extends the user-facing command surface. The CLI wires the stages (manifest → safety → draft → prune → grade → diff) into a single command and is the sink where every typed error renders to a stable shape that CI parsers key on.

## Subpackage layout — flat, per-subcommand modules

```
src/signalforge/cli/
  __init__.py      # main(argv), top-level argparse parser, dispatch
  _helpers.py      # path canonicalisation, logging, error formatting, exit-code map, progress helpers
  errors.py        # CliError + CliPathError + CliInputError + CliInitDemo*
  generate.py
  init_demo.py
  lint.py
  version.py
```

One module per subcommand, no nested directories, no `__main__.py`. Every subcommand module exports exactly two public symbols: `add_parser(subparsers) -> None` and `cmd_<name>(args) -> int`. Top-level `main(argv: list[str] | None = None) -> int` accepts an explicit argv list — tests call `main([...])` directly and never spawn a subprocess.

## Library-surface pattern: CLI handler wraps a public lib module at the boundary

Subcommands with a useful programmatic surface ship as TWO layers — a public lib module (e.g. `signalforge.demo.copy_demo(...) -> Path`) and a thin CLI handler that wraps it. The split:

- **Lib module** owns its own typed-error hierarchy (e.g. `DemoError` base + concrete subclasses), returns work product (not just side effects), stays clean of CLI concerns.
- **CLI handler** argparse-parses, calls into the lib inside the single `try/except Exception` boundary, and wraps every lib error into a matching `Cli<Name>Error` so the CLI's catch surface stays homogeneous. CLI owns the next-steps message + exit-code mapping.

Defence-in-depth: both layers' errors land in `_EXCEPTION_TO_EXIT_CODE`. A contributor who adds a new `Demo*Error` and forgets the CLI wrapper still gets a sensible exit code via the MRO walk.

Exception (`prune-existing`): skip the per-class CLI wrapper when the lib's errors are already first-class in `_EXCEPTION_TO_EXIT_CODE` AND carry remediation — the boundary catch maps them directly. Wrap only when the CLI adds something (distinct remediation, or homogeneity the boundary catch doesn't already give you).

## Four-tier exit-code taxonomy

Every `cmd_<name>` handler returns an integer drawn from exactly four values. Wording is locked because CI parsers across repos key on the same boundary. **Do NOT invent a fifth category. Do NOT collapse 2 and 3.**

- **`0` — success.**
- **`1` — load-time / parse-layer failure.** "The request was well-formed but the surrounding state is not ready / not coherent." Examples: `ManifestNotFoundError`, `ProfileNotFoundError`, `ConfigNotFoundError`, `CliPathError`, the panic-path catch for any untyped `Exception`.
- **`2` — input-validation failure.** Pre-call input errors AND post-call invariant failures share this tier. Examples: `ModelNotFoundError`, `LLMOutputAnchorContractError`, `TableNotFoundError`, `GradeBelowThresholdError`, `CliInputError`.
- **`3` — Anthropic API / external dependency failure.** "Something outside our control went wrong; retry later." Examples: `LLMAuthError`, `LLMRateLimitError`, `WarehouseAuthError`, `BytesBilledExceededError`, every fail-closed audit-write durability error (`AuditWriteError`, `PruneAuditWriteError`, `GradeAuditWriteError`, `DiffSidecarWriteError`, `LLMResponseAuditWriteError`).

`map_exception_to_exit_code(exc)` walks `type(exc).__mro__` against `_EXCEPTION_TO_EXIT_CODE` so subclasses inherit their parent's tier. An unregistered concrete falls to tier 1 (the panic-path default); the 7th AST scan catches missing registrations at test time.

`LLMHelperError` is deliberately NOT in the abstract-base exclusion list — it's raised directly in `signalforge.llm.client`, so it's a concrete leaf for taxonomy purposes.

See clauditor's `docs/rules/llm-cli-exit-code-taxonomy.md` for the source rule.

## Stderr message shape

Two shapes; CI parsers key on them.

- **Tier 1 / 3 and most tier 2** — single line: `ERROR: <message>`, optionally followed by `  ↳ Remediation: <text>` rendered by the typed error's own `__str__`.
- **Multi-violation tier 2** — header line plus `  - <violation>` bullets (two leading spaces, dash, space). Used by `LLMOutputAnchorContractError` and `cmd_lint`'s multi-block reporting.

`format_error_to_stderr(exc) -> str` is the **single source of truth**. The CLI is the sink; layer error classes do NOT override `__str__` for the header+bullet shape ("escape at the sink", same pattern as `diff-renderer.md`). When a future stage introduces a new multi-violation error class, extend `format_error_to_stderr` rather than overriding `__str__`.

## No traceback ever leaks

Every `cmd_<name>(args) -> int` wraps its pipeline in one `try / except Exception`. The except block calls `format_error_to_stderr(exc)`, prints to stderr, returns `map_exception_to_exit_code(exc)`. Belt-and-braces: `_safe_excepthook` is installed via `sys.excepthook` in `main()` unless `--verbose` is set; `KeyboardInterrupt` and `SystemExit` pass through unchanged.

**Don't** wrap individual stage calls in their own `try/except` "to be defensive" — an inner handler that swallows a typed error would return exit 0 even though the run failed. The single boundary catch is the contract.

The "no traceback" assertion is the floor of every CLI test: `"Traceback" not in capsys.readouterr().err`. New tests inherit it.

## `os.environ` mutation pattern for process-scoped flags

`--no-color` sets `NO_COLOR=1`; `--profiles-dir <PATH>` sets `DBT_PROFILES_DIR=<resolved-path>`. Both mutate at the start of `cmd_<name>` and are NOT wrapped in `try / finally` restoration — the CLI is one-process-per-invocation; the OS reaps env on exit. Restoration would be dead code and a footgun if a future maintainer assumed it worked across calls.

When introducing a new flag that signals a downstream library via env vars, follow the same pattern: mutate, don't restore.

## Path canonicalisation at the orchestrator

Every user-supplied path flows through `canonicalise_user_path(raw, project_dir)`, which wraps `signalforge.warehouse._path_safety.canonicalise_path` and re-raises as `CliPathError`. The three traps from `manifest-readers.md` apply (resolve before relative-to; catch `RuntimeError` on symlink cycles; default paths go through the same gate as overrides).

`--project-dir` is an **absolute assertion**, not a walk-up starting point. When supplied, the path must directly contain `dbt_project.yml` or the command exits 1. Walk-up is only the unflagged default (mirrors how `git` finds `.git`).

When introducing a new flag that takes a path, route it through `canonicalise_user_path` from the orchestrator. Don't trust the writer / loader to derive its own `project_dir`.

## Logger grep gate covers 6 dirs

Every `_LOGGER.{info,warning,debug,error}` call in `signalforge.cli.*` uses lazy-format with `json.dumps()` for any user-controlled string. Never f-string-interpolate — ANSI escapes in a model id or path would inject into log viewers; JSON encoding handles this; f-string interpolation does not.

The grep gate at `tests/llm/test_logger_grep_gate.py` scans `src/signalforge/{llm, draft, prune, grade, diff, cli}` and rejects any `_LOGGER\.\w+\(f"` hit. Extend to a seventh dir only when a new pipeline package ships.

The CLI is the orchestration layer (NOT a stage-0 reader) so it IS allowed to emit logs. `setup_logging(verbose, quiet)` is the single config site: INFO default, `--verbose` → DEBUG, `--quiet` → WARNING.

## 7th AST scan: every typed exception has an exit-code mapping

`tests/test_audit_completeness.py::test_every_typed_error_is_in_exit_code_mapping_table` walks every `src/signalforge/*/errors.py`, collects each `class <Name>Error(...):` via `ast.ClassDef`, and asserts the class is registered in `_EXCEPTION_TO_EXIT_CODE`. Excludes the eleven per-stage abstract bases (frozenset `_EXCEPTION_MAPPING_EXCLUDED_BASES`: `ManifestError`, `WarehouseError`, `SafetyError`, `LLMError`, `DraftError`, `PruneError`, `GradeError`, `DiffError`, `CliError`, `DemoError`, `IngestError`); subclasses inherit via MRO.

**Dual registration.** Nine of the eleven abstract bases are ALSO registered in `_EXCEPTION_TO_EXIT_CODE` at a single fallback tier (`ManifestError`/`DiffError`/`CliError` → 1; `DraftError` → 2; `LLMError`/`WarehouseError`/`GradeError`/`SafetyError`/`PruneError` → 3). Two independent roles: the frozenset excludes bases from the AST scan; the table entry is a forward-compat safety net so a new concrete subclass that forgets a table entry still gets the parent's tier via the MRO walk rather than the panic-path tier 1. The AST scan still fails loud on the missing per-class entry — fallback is safety net, not substitute. `DemoError` and `IngestError` are the two deliberate exceptions: their concretes span tiers 1 and 2, so no single fallback tier fits — each appears only in the frozenset, and a forgotten table entry falls through to tier 1 (AST scan catches it).

Companion test `test_scan_7_discovers_every_per_stage_errors_module` asserts the scan walks exactly eleven `errors.py` files. A new stage's `errors.py` bumps the count AND adds its abstract base to the excluded set in lockstep. `test_exit_code_mapping_has_at_least_one_entry_per_tier` guards against mass rename/deletion.

If a new module legitimately needs to declare an `*Error` subclass without an exit-code mapping (e.g. an abstract intermediate), update `_EXCEPTION_MAPPING_EXCLUDED_BASES` AND document the addition. Don't suppress the test.

## Subprocess-gated smoke pattern

In-process `main(argv)` testing is the primary pattern but cannot catch `[project.scripts]` typos, broken editable installs, or console-script wrapper changes after a wheel rebuild. Gated subprocess tests (`tests/cli/test_subprocess_smoke.py`, `@pytest.mark.cli_subprocess`) run the installed script. Maintainers run `pytest -m cli_subprocess --no-cov` before declaring a CLI PR ready (`--no-cov` because `--cov-fail-under` in `addopts` would fail marker-specific runs).

The marker ships a `--version` test (asserts `returncode == 0`, stdout starts with `"signalforge "`, **stderr is empty**) plus a `<subcommand> --help` smoke per subcommand (each asserts `returncode == 0`, a subcommand-unique token in stdout, and a **no-traceback floor** on stderr — `"Traceback" not in stderr`, NOT stderr-is-empty, since argparse's help path may emit warnings on some Python builds). `--version` catches `[project.scripts]` wiring regressions; `--help` catches subparser-registration regressions.

When adding a subcommand, add a parallel `--help` smoke under the same marker rather than introducing a second gated marker — the single **marker** is the source of truth for "the wheel actually exposes the script."

## Progress to stderr UX

`cmd_generate` emits one stderr progress line per stage entry plus a paired `done in <X>` line at exit. The `<fact>` field is computed from objects already in scope (model id, candidate test count) — never a hardcoded duration hint.

TTY-gated: `should_emit_progress(quiet, verbose)` returns `True` only when both stderr and stdout are TTYs. `--quiet` suppresses; `--verbose` forces on. The orchestrator decides once at startup and threads the bool through stages — don't introspect TTY-ness mid-pipeline.

## Multi-source CLI commands degrade on supplementary failures

When a CLI command (e.g. `--estimate`) has multiple data sources where some are supplementary (e.g. `count_tokens` is load-bearing for cost preview; `adapter.estimate_query_bytes` is nice-to-have), supplementary failures must NOT propagate. Three rules:

1. **Classify load-bearing vs supplementary BEFORE writing the engine.** Load-bearing failures propagate through the panic boundary; supplementary failures are caught at the engine and surfaced as typed `*_unavailable_reason: str | None` fields on the report.
2. **Conservative-bias capture verbatim.** Captured reason: `f"{type(exc).__name__}: {str(exc)[:200]}"`. Renderer prints `<unavailable: <ErrorClass>>` via `reason.split(":", 1)[0]`. One stderr WARNING via lazy-format JSON. No paraphrasing.
3. **Pin BOTH the report-field AND the WARNING via tests.** A test that only pins the field misses a refactor that silently drops the `_LOGGER.warning(...)` breadcrumb.

This is NOT the panic-path catch — that maps escaping exceptions to exit codes; this catches inside the engine so the CLI never sees the supplementary error. See `prune-engine.md` § "Conservative drop-reason taxonomy" and `warehouse-adapters.md` § "Cleanup-boundary fail-soft pattern".

## Multi-surface parity for behaviour changes

A behaviour change in the CLI touches **five surfaces**, all updated in the same commit: (1) argparse help string, (2) handler/helper docstring, (3) `docs/cli-ops.md` (Flag reference / Exit codes / Stderr shapes), (4) test name, (5) test docstring + the DEC in `plans/super/9-cli-entrypoint.md`. Surfaces 3 and 5 are most often forgotten (furthest from the code). The DEC is the single source of truth; the other four paraphrase from there. When introducing a new flag, write surfaces 1–3 first, then the test against those, then back-fill the DEC.

## API alignment with adjacent stages

`add_parser(subparsers) -> None` and `cmd_<name>(args) -> int` for every subcommand; `main(argv: list[str] | None = None) -> int` at the top. No top-level `try/except` in `main()` — typed errors flow up; `cmd_<name>` does the explicit catch and returns the right exit code. **One layer's exception → one CLI handler → one exit code.**

`--version` uses argparse's `action="version"` (raises `SystemExit` after printing); `main()` catches `SystemExit` and returns its `code` so the `-> int` contract holds.

## Multi-model batch driver pattern

`--select <expr>` runs a batch in one process. Mutex with positional `<model>` via `add_mutually_exclusive_group(required=True)`. The dispatcher in `cmd_generate` routes to `_run_single_model(...)` or `_run_batch(...)`.

**Dispatch on `is not None`, NOT truthiness.** `--select ""` is argparse-accepted (mutex group sees it as "provided") and MUST route to the parser to raise `CliSelectorParseError`, not fall through to the single-model branch. Pinned by `test_select_empty_string_routes_to_parse_error`.

**Fresh adapter per model.** Stateful adapters carry per-call state (e.g. `BigQueryAdapter._active_session_id`). Construct a new adapter inside the per-model loop, not once at batch start — otherwise state from model N leaks into N+1.

**Continue-on-failure with `max()` aggregation.** Each per-model run lives inside its own `try/except Exception` (mirrors the single-boundary catch). Records `exception_class_name` + exit code, keeps going. Batch's `total_exit_code = max(per_model_exit_codes)` across the four-tier taxonomy. Failed models named in the aggregated summary; cap 50, overflow `... and <K> more`.

**Aggregated summary → stderr.** `format_batch_summary(outcome) -> str` in `_helpers.py`. Headline (locked, pinned by test):

```
Generated <K> kept / <L> dropped / <J> flagged across <M> models in <T>s
```

Plus optional failure block. Emits when `(matched ≥ 2 OR failed ≥ 1) AND NOT quiet`. Stdout carries rendered diffs in `unique_id` lex order; stderr carries the summary so `> diffs.txt` captures just diffs. `format_batch_summary` scrubs control chars in `model_unique_id` before column-padding so a `\n`/`\r`/`\t` can't corrupt the geometry.

**Per-model `[i/N]` progress prefix.** Emitted before the model's stage progress when `_run_single_model` receives non-None `batch_index`/`batch_count` AND `should_emit_progress` returns True. Single-model positional path emits NEITHER prefix NOR summary — preserves single-model output byte-for-byte.

**Sidecar last-writer-wins.** `.signalforge/grade.json` and `.signalforge/diff.json` are `O_TRUNC` per call; in-process iteration overwrites per model — only the final model's sidecars persist. The four append-only JSONLs survive iteration (≤4000 bytes/record < `PIPE_BUF`). Operators wanting per-model sidecars use the shell-loop pattern in `docs/cli-ops.md`.

**Anthropic cache caveat.** The drafter's explicitly cache-marked block changes per iteration, so it does NOT amortise across siblings; savings within one process come only from Anthropic's automatic caching of the static system prompt.

**5-surface parity test pattern.** For any new flag whose grammar/examples appear across multiple surfaces, ship a bespoke parity test that reads each surface and asserts the same example tokens appear (`tests/cli/test_5_surface_parity_select.py` is the precedent). Don't ship `pytest.skip` branches for surfaces that haven't landed — those become dead code on merge.

## Bare-name model resolution lives in the CLI subcommand, not the manifest layer

`Manifest.get_model(key)` accepts unique_id (`model.<pkg>.<name>`) and file-path (`models/.../<name>.sql`) forms; a bare name like `customers` routes through the file-path branch and surfaces a confusing `ModelNotFoundError`. The shared resolver `signalforge.cli._helpers._resolve_model_by_key(manifest, key) -> Model` adds a bare-name branch (unique_id/file-path forms delegate to `get_model`; else match `iter_models()` by `name`). Three rules:

1. **The bare-name branch lives in the CLI, NOT the manifest layer.** Library callers (prune, drafter) work in unique_ids and shouldn't pay the disambiguation cost. The CLI is the only surface where operators type by hand — convenience at the sink.
2. **Multiple matches fail loud with a disambiguation list, not first-wins.** Cross-package collisions are ambiguous; list capped at 5 + `(+K more)`.
3. **Disabled models do NOT match bare-name lookup.** `iter_models()` yields only enabled nodes; disabled models surface only via the unique_id form (which raises `ModelDisabledError`).

`cmd_lint` keeps a thin `_resolve_model_for_lint` delegating wrapper; `prune-existing` reuses `_resolve_model_by_key` directly for its positional `<model>`. Any future subcommand taking a model arg uses the shared helper rather than copy-pasting.

## `prune-existing` conventions (no-LLM stage CLI)

`signalforge prune-existing <model> --schema <path>` runs ingest → prune → diff with **no LLM call**. Two conventions for a no-LLM stage CLI:

1. **Audit each inherited flag against what the path consumes.** `--mode` (a `SafetyPolicy` knob) is inert here because `prune_tests` never reads the safety policy, so it was dropped in favour of prune's `--scope` / `--sample-strategy`.
2. **Read-only by default.** No `--write` for a command whose `--schema` source is hand-authored; print the diff + a default-on `.signalforge/diff.json` sidecar (`--dry-run` suppresses it), feeding the external schema.yml as `render_diff(existing_schema=...)` for a "what to remove from your file" unified diff.

(Error-wrapping: see the library-surface pattern's `prune-existing` exception above.)

## Reference

`plans/super/9-cli-entrypoint.md` and `plans/super/37-multi-model-select.md` — DEC records. `src/signalforge/cli/` — implementation. `docs/cli-ops.md` — operational reference. Key tests: `tests/test_audit_completeness.py` (7th AST scan), `tests/llm/test_logger_grep_gate.py`, `tests/cli/test_exit_codes.py`, `tests/cli/test_5_surface_parity_select.py`, `tests/cli/test_lint.py`. See-Also: clauditor's `docs/rules/llm-cli-exit-code-taxonomy.md` (source of the four-tier rule).
