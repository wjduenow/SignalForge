# CLI layer (four-tier exit codes + no-traceback panic path + structured stderr)

Established by issue #9 (CLI entrypoint). Apply to every module under `signalforge.cli` and to any new code that maps a typed exception to an exit code, formats an error for stderr, or extends the user-facing command surface.

The CLI sits at the top of the v0.1 dependency stack and is the **first user-facing surface** of the project. It does not implement new pipeline behaviour; it wires the existing stages — manifest → safety → draft → prune → grade → diff — into a single command. Architectural Commitment #4 ("OSS-first, Core-friendly") makes the CLI the load-bearing surface a user installs and runs against any dbt-core project; Architectural Commitment #5 ("explainable diffs") makes it the sink where every typed error from any layer renders to a stable shape that CI parsers can key on.

## Subpackage layout — flat, per-subcommand modules (DEC-009)

```
src/signalforge/cli/
  __init__.py    # main(argv), top-level argparse parser, dispatch
  _helpers.py    # canonicalise_user_path, setup_logging, format_error_to_stderr,
                 # map_exception_to_exit_code, _safe_excepthook, _EXCEPTION_TO_EXIT_CODE,
                 # progress helpers (should_emit_progress / format_elapsed /
                 # emit_progress_entry / emit_progress_done)
  errors.py      # CliError + CliPathError + CliInputError
  generate.py    # add_parser + cmd_generate (the full pipeline)
  lint.py        # add_parser + cmd_lint (config-only validator)
  version.py     # add_parser + cmd_version (prints signalforge __version__)
```

Flat layout — one module per subcommand, no nested directories, no `__main__.py`. Mirrors clauditor's CLI shape (16 subcommand modules in clauditor; SignalForge ships three for v0.1). Every subcommand module exports exactly two public symbols: `add_parser(subparsers) -> None` (registers the subparser, no return) and `cmd_<name>(args) -> int` (handler returning the exit code). The top-level `main(argv: list[str] | None = None) -> int` accepts an explicit argv list (defaults to `sys.argv[1:]`) — that's what makes in-process testing trivial: tests call `main([...])` directly, assert on the returned `int` and capsys output, and never spawn a subprocess.

When v0.2 adds a new subcommand (`signalforge doctor`, `signalforge profile`, ...), match the precedent: one new module under `src/signalforge/cli/`, register via `add_parser(subparsers)` from `main()`, return an int from `cmd_<name>(args)`.

## Four-tier exit-code taxonomy (DEC-008, DEC-019, DEC-024)

Every `cmd_<name>` handler returns an integer drawn from exactly four values. Ported from clauditor's `llm-cli-exit-code-taxonomy.md` rule; the wording is locked because CI parsers across repos key on the same boundary. **Do NOT invent a fifth category. Do NOT collapse 2 and 3.**

- **`0` — success.** Artifact written / printed; pipeline completed cleanly.
- **`1` — load-time / parse-layer failure.** "The request was well-formed but the surrounding state is not ready / not coherent." Examples: `ManifestNotFoundError`, `ProfileNotFoundError`, `ConfigNotFoundError`, `DraftConfigInvalidError`, `DiffError` (config-shape), `CliPathError`, the panic-path catch for any untyped `Exception` that escapes a `cmd_<name>` boundary.
- **`2` — input-validation failure.** "The LLM call either shouldn't happen or its output cannot be trusted." Pre-call input errors AND post-call invariant failures share this tier. Examples: `ModelNotFoundError`, `ModelDisabledError`, `LLMOutputAnchorContractError` (invariant violation — the LLM response was structurally valid JSON but violated the anchor contract), `TableNotFoundError` (DEC-012 — the model's table reference is wrong, not the warehouse), `GradeBelowThresholdError` (DEC-011), `DiffCandidateModelMismatchError`, `CliInputError`.
- **`3` — Anthropic API / external dependency failure.** "Something outside our control went wrong; retry later." Examples: `LLMAuthError`, `LLMRateLimitError`, `LLMServerError`, `LLMConnectionError`, `WarehouseAuthError`, `BytesBilledExceededError`, `GradeLLMError`, every fail-closed audit-write durability error (`AuditWriteError`, `PruneAuditWriteError`, `GradeAuditWriteError`, `DiffSidecarWriteError`, `LLMResponseAuditWriteError` — when these fire the disk hand-off didn't happen, which is an external-dep state we couldn't recover).

The mapping table lives at `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`. `map_exception_to_exit_code(exc)` walks `type(exc).__mro__` against the table so subclasses inherit their parent's tier; an unregistered type (or a bare `Exception`) lands at tier 1 per DEC-016 (the panic path).

The 7th AST scan in `tests/test_audit_completeness.py` (DEC-024) walks every `src/signalforge/*/errors.py` (nine modules: the eight stage layers plus `cli/errors.py`) and asserts every concrete `class <Name>Error(...):` declaration appears in the mapping table. Excluded bases: the nine per-stage abstract bases (`ManifestError`, `WarehouseError`, `SafetyError`, `LLMError`, `LLMHelperError`, `DraftError`, `PruneError`, `GradeError`, `DiffError`, `CliError`). A new typed exception lands without a tier mapping → test fails loud.

See clauditor's `.claude/rules/llm-cli-exit-code-taxonomy.md` for the source rule in the See-Also footer.

## Stderr message shape (DEC-008, DEC-017)

Two shapes. CI parsers key on them.

- **Tier 1 / 3 errors and most tier 2 errors** — single line: `ERROR: <message>`, optionally followed by `  ↳ Remediation: <text>` when the typed error carries one. The remediation footer is rendered by the typed error's own `__str__` (the layer-base pattern from `signalforge.safety.errors` and its siblings); the CLI passes it through unchanged.
- **Multi-violation tier 2 errors** — header line plus `  - <violation>` bullets per entry. Used by the drafter's `LLMOutputAnchorContractError` (whole-draft fail-loud anchor contract; DEC-022 of `llm-drafter.md`) and by `cmd_lint`'s multi-block reporting. Bullets render as exactly `  - <text>` (two leading spaces, dash, space, content).

`signalforge.cli._helpers.format_error_to_stderr(exc) -> str` is the **single source of truth** for the stderr shape across every typed exception the CLI catches. The CLI is the sink; the layer error classes do NOT override `__str__` for the header+bullet shape (DEC-017 — "escape at the sink", same pattern as `diff-renderer.md` DEC-008's per-sink Markdown / ANSI / JSON escaping). When a future stage introduces a new multi-violation error class, extend `format_error_to_stderr` rather than overriding `__str__` on the error.

## No traceback ever leaks (DEC-016)

Every `cmd_<name>(args) -> int` handler wraps the entire pipeline in one `try / except Exception` (`# noqa: BLE001` is documented at the catch site). The except block calls `format_error_to_stderr(exc)`, prints the result to stderr, and returns `map_exception_to_exit_code(exc)`. The handler never propagates an exception out of its own boundary — which means the operator never sees `Traceback (most recent call last):` followed by Python frames in a non-verbose run.

Belt-and-braces: `_safe_excepthook` is installed via `sys.excepthook = _safe_excepthook` in `main()` unless `--verbose` is set. The hook strips tracebacks from anything that escapes the main `try/except` (e.g., a bug raised inside an `except` clause that bypasses the CLI's own catch). `KeyboardInterrupt` and `SystemExit` are passed through to Python's default hook unchanged — Ctrl-C and clean exits keep their semantics.

`--verbose` skips the install so maintainers debugging a panic-path bug see the full traceback. **Don't** wrap individual stage calls in their own `try/except` "to be defensive" — the single boundary catch is the contract; an inner handler that swallows a typed error would defeat the four-tier mapping (the exit code would be `0` even though the run failed).

The "no traceback" assertion is the floor of every CLI test: every test asserts `"Traceback" not in capsys.readouterr().err`. New tests inherit this assertion — a panic-path regression breaks the test loudly.

## `os.environ` mutation pattern for process-scoped flags (DEC-023)

Two CLI flags mutate `os.environ` for the current process to wire signal into libraries that read from it: `--no-color` sets `NO_COLOR=1` so any downstream library honouring the [no-color.org](https://no-color.org) convention strips colour, and `--profiles-dir <PATH>` sets `DBT_PROFILES_DIR=<resolved-path>` so the warehouse profile loader picks the operator-supplied path even on import paths the CLI does not directly invoke.

**The mutation is one-shot, not scoped.** v0.1 deliberately does NOT wrap the env mutation in `try / finally` to restore the prior value at command exit. The CLI is a one-process-per-invocation surface — `main()` returns an int, the console-script wrapper calls `sys.exit(...)`, the process dies. There is no parent process whose env we'd be polluting; the OS reaps it. Adding `try / finally` restoration would be dead code in v0.1 AND a footgun if a future maintainer assumed it worked across calls.

This is reserved as a v0.2 refinement when an in-process batch runner (multiple `main()` calls in one Python process — e.g., a notebook-driven pipeline) lands. At that point, `os.environ` mutation needs explicit save / restore, and a context-manager helper (`with _env_override(NO_COLOR="1"):`) becomes the right shape. Don't pre-emptively wrap in v0.1 — the QG passes 2 + 4 specifically validated the unwrapped pattern.

When introducing a new CLI flag that needs to signal a downstream library via env vars (likely candidates: `LOG_LEVEL`, `DBT_TARGET`, future cache-control vars), follow the same pattern: mutate at the start of `cmd_<name>`, document the v0.2 in-process-batch reservation, do NOT add restoration. The single source of truth for the v0.1-vs-v0.2 split is `docs/cli-ops.md` § "Flag reference" — keep its phrasing aligned with this rule (per the multi-surface parity rule below).

## `_EXCEPTION_TO_EXIT_CODE` mapping table convention (DEC-024)

The exit-code mapping is a `dict[type[BaseException], int]` keyed by exception class **identity**, not name. `map_exception_to_exit_code(exc)` walks `type(exc).__mro__` so a subclass inherits its registered parent's tier — adding a new subclass under an existing base is zero-config; adding a new top-level error class requires an entry.

Three load-bearing invariants:

1. **One entry per concrete error class.** Every concrete `class <Name>Error(...):` declaration in any `src/signalforge/*/errors.py` module gets exactly one entry in the table. Adding multiple entries (the same class registered at two tiers) is a typo — the dict semantics keep only the last one and the test won't notice.
2. **Abstract bases land in `_EXCEPTION_MAPPING_EXCLUDED_BASES`, not the table.** The nine per-stage abstract bases (`ManifestError`, `WarehouseError`, `SafetyError`, `LLMError`, `LLMHelperError`, `DraftError`, `PruneError`, `GradeError`, `DiffError`, `CliError` — eight pipeline stages plus the CLI base) are listed in the frozenset constant and excluded by the AST scan. Subclasses inherit via the MRO walk; registering an abstract base directly would make `map_exception_to_exit_code(exc_with_unrelated_concrete_base)` accidentally return the abstract-base tier, defeating the per-class precision.
3. **An unregistered concrete class falls to tier 1 via the panic path.** `map_exception_to_exit_code` returns `1` for any class without an MRO match, mirroring `signalforge.cli._helpers._safe_excepthook`'s tier-1 default for bare `Exception`. The 7th AST scan ensures unregistered concretes are caught at test time, not runtime — but the runtime fallback is the safety net.

If v0.2 introduces a new intermediate abstract base (e.g., a `WarehouseTransientError` that sits between `WarehouseError` and the concrete `WarehouseRateLimitError`), add it to `_EXCEPTION_MAPPING_EXCLUDED_BASES` AND document the addition. The exclusion list is a contract surface, not a convenience cache. The same rule applies if a new pipeline subpackage ships its own `errors.py` (a tenth stage, e.g., `signalforge.cache`): the companion test `test_scan_7_discovers_every_per_stage_errors_module` asserts the scan walks exactly nine `errors.py` files, so the count must be bumped in lockstep with the new stage's abstract base getting added to the excluded-bases set.

## Multi-surface parity for behaviour changes (QG pass-3 lesson)

A behaviour change in the CLI touches **five surfaces**. The QG pass-3 review caught a `--min-score` contract drift where the help text and the docstring agreed but the ops doc and the test docstring disagreed about what the flag actually drove (it gates `cmd_generate`'s exit code via `GradeBelowThresholdError`, not the diff renderer's tier classification). Pass 4 caught a similar drift where the env-mutation phrasing in `--profiles-dir` / `--no-color` was correct in code + helpers but stale in `docs/cli-ops.md`.

When changing a flag's contract or a stderr message shape, update **all five surfaces in the same commit**:

1. **The argparse help string** (`add_argument(..., help=...)`) — what the operator sees on `signalforge generate --help`.
2. **The handler / helper docstring** — what an editor / IDE / `pydoc` shows; what a future maintainer reads first.
3. **The ops doc** — `docs/cli-ops.md` § "Flag reference" or § "Exit codes" or § "Stderr shapes". This is the surface external CI parsers and downstream tooling key on.
4. **The test name** — `test_generate_min_score_below_threshold_returns_exit_2` should match the contract; renaming / re-targeting the test on a contract change is part of the change.
5. **The test docstring AND the DEC in `plans/super/9-cli-entrypoint.md`** — the ADR-style record of why the contract is what it is. Don't leave the plan stale relative to shipped behaviour.

The pass-3 / pass-4 lessons are: surfaces 3 and 5 are the ones most often forgotten because they sit furthest from the code change. Codify the 5-surface parity check into every flag-modifying or stderr-shape-modifying PR's review checklist. The single source of truth is wherever the contract is most precisely stated (usually the DEC); the other four surfaces paraphrase from there.

When introducing a new flag, write surfaces 1-3 first (help / docstring / ops doc), then write the test (surface 4) against those, then back-fill the DEC (surface 5) with the rationale. The DEC is the load-bearing surface for v0.2 reviewers asking "why does this flag work this way" — keeping it aligned with the other four is the difference between a self-documenting CLI and a forensic exercise.

## Path canonicalisation at the orchestrator (DEC-007, DEC-027)

Every user-supplied path the CLI accepts (`--config`, `--manifest`, `--profiles-dir`, eventually `--output`, `--sidecar-path`) flows through `signalforge.cli._helpers.canonicalise_user_path(raw, project_dir)`, which wraps `signalforge.warehouse._path_safety.canonicalise_path` and re-raises as `CliPathError` so the CLI's own catch surface stays homogeneous.

The three traps from `manifest-readers.md` apply:
1. `Path.relative_to()` does NOT follow symlinks. Use `.resolve()` first.
2. `Path.resolve()` raises `RuntimeError` on cycles regardless of `strict=`. Wrap.
3. The "default" path (e.g. `target/manifest.json`) goes through the same gate as a user-supplied override.

`--project-dir` is an **absolute assertion**, not a walk-up starting point (DEC-027). When supplied, the CLI does NOT walk up from `<PATH>` — the path must directly contain `dbt_project.yml` or `cmd_generate` / `cmd_lint` exits 1. Walk-up is only the unflagged default, mirroring how `git` finds `.git` from a subdirectory. This split is deliberate: the flag is the precise mode (operator knows where the project is); walk-up is the convenience (operator just `cd`'d into a model directory).

When introducing a new flag that takes a path, route it through `canonicalise_user_path` from the orchestrator. Don't trust the writer / loader to derive its own `project_dir`; the engine-level gate is the load-bearing one (mirrors `grade-layer.md` and `diff-renderer.md` post-QG fixes verbatim).

## Logger grep gate now covers 6 dirs (DEC-019 of diff-renderer.md graduated)

Every `_LOGGER.{info,warning,debug,error}` call in `signalforge.cli.*` uses lazy-format with `json.dumps()` for any user-controlled string:

```python
_LOGGER.debug(
    "resolved project_dir: %s",
    json.dumps({"project_dir": str(current), "source": "walk-up"}),
)
```

**Never** f-string-interpolate user-controlled values into a logger call. A model id or path containing ANSI escapes (`\x1b[31m...`) would inject into log viewers. JSON encoding handles this; f-string interpolation does not.

The grep gate at `tests/llm/test_logger_grep_gate.py` now scans `src/signalforge/{llm, draft, prune, grade, diff, cli}` (six directories as of #9; the `diff-renderer.md` DEC-019 reservation is graduated by this ticket) and rejects any `_LOGGER\.\w+\(f"` hit. The regex covers every f-string permutation (`f"`, `f'`, `rf"`, `fr'`, ...). Extend the scan to a seventh directory only when an entirely new pipeline package ships; the single test is the source of truth.

The CLI is the orchestration layer (NOT a stage-0 reader/parser per the `manifest-readers.md` rule), so it IS allowed to emit logs — `setup_logging(verbose, quiet)` is the single configuration call site, INFO is the default, `--verbose` raises to DEBUG, `--quiet` raises to WARNING.

## 7th AST scan: every typed exception has an exit-code mapping (DEC-019, DEC-024)

`tests/test_audit_completeness.py::test_every_typed_error_is_in_exit_code_mapping_table` is the 7th AST scan in the project (after the six landed by #4 / #5 / #6 / #7). Walks every `*/errors.py` under `src/signalforge/`, collects each `class <Name>Error(...):` declaration via `ast.ClassDef`, and asserts the class is registered in `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`. Excludes the nine per-stage abstract bases (frozenset constant `_EXCEPTION_MAPPING_EXCLUDED_BASES`); subclasses inherit via the MRO walk in `map_exception_to_exit_code`.

A companion test `test_scan_7_discovers_every_per_stage_errors_module` asserts the scan walks exactly nine `errors.py` files (one per stage subpackage). A future stage that forgets to ship `errors.py` (or moves the CLI errors to a sibling location) breaks this test loudly.

Sanity test `test_exit_code_mapping_has_at_least_one_entry_per_tier` asserts every tier (1, 2, 3) has at least one entry in the table — guards against an accidental mass-rename / deletion.

If a new module legitimately needs to declare an `*Error` subclass that should NOT have an exit-code mapping (e.g., a v0.2 abstract intermediate base), update `_EXCEPTION_MAPPING_EXCLUDED_BASES` AND document the addition in this rule's exclusion list. Don't suppress the test; the four-tier taxonomy is a contract, not a guideline.

## Subprocess-gated smoke pattern (DEC-018)

In-process `main(argv)` testing is the right primary pattern (clauditor's choice, fast, deterministic). But it cannot catch:

- `[project.scripts]` wiring in `pyproject.toml` getting deleted / typoed.
- `pip install -e ".[dev]"` followed by `which signalforge` returning nothing.
- Console-script wrapper differences after a wheel rebuild.

**One gated subprocess test** — `tests/cli/test_subprocess_smoke.py::test_signalforge_version_via_subprocess` — runs `subprocess.run(["signalforge", "--version"], ...)` against the installed console script, asserts `returncode == 0`, asserts stdout starts with `"signalforge "`, asserts stderr is empty. Decorated with `@pytest.mark.cli_subprocess`. The marker is registered in `pyproject.toml` `[tool.pytest.ini_options].markers` and `addopts` excludes it by default (`-m 'not bigquery and not anthropic and not cli_subprocess'`). Maintainers run `pytest -m cli_subprocess` once before declaring a CLI PR ready (mirrors `pytest -m bigquery` for the BigQuery adapter, `pytest -m anthropic` for the LLM seam).

When v0.2 adds a new subcommand or changes the console-script wiring, extend the same subprocess test rather than adding a second gated marker; the single test is the source of truth for "the wheel actually exposes the script."

## Progress to stderr UX (DEC-014, DEC-026)

`cmd_generate` emits one stderr progress line per stage entry plus a paired `done in <X>` line at stage exit. The `<fact>` field is computed from objects already in scope at the entry point (model id, candidate test count, `kept_count × criteria_count`) — never a hardcoded duration hint, because model speeds and warehouse sizes change and stale estimates rot. The `done in <X>` line is the post-hoc real measurement; together they replace any "this can take Xs" prediction with live + measured signal.

TTY-gated by default: `should_emit_progress(quiet, verbose)` returns `True` only when both `sys.stderr.isatty()` and `sys.stdout.isatty()` (either being a pipe disables emission). `--quiet` suppresses regardless of TTY; `--verbose` forces progress on regardless of TTY (the operator explicitly opted in). Non-TTY runs (CI logs, `2>/dev/null`, redirected to a file) emit no progress lines so the captured logs aren't littered with stage chatter.

The four progress helpers (`should_emit_progress`, `format_elapsed`, `emit_progress_entry`, `emit_progress_done`) live in `_helpers.py`; the orchestrator makes a single decision once at startup and passes the bool through stage by stage. Don't introspect TTY-ness mid-pipeline.

## API alignment with adjacent stages

Every subcommand module exports the same two public symbols:

```python
def add_parser(subparsers: argparse._SubParsersAction) -> None: ...
def cmd_<name>(args: argparse.Namespace) -> int: ...
```

Top-level entry: `def main(argv: list[str] | None = None) -> int`. Tests call `main([...])` directly; the console-script wrapper calls `sys.exit(main())`. No top-level `try/except` in `main()` — every stage orchestrator already follows the "never raise an unwrapped exception" contract, typed errors flow up, the CLI's `cmd_<name>` does the explicit `try/except` per category and returns the right exit code. **One layer's exception → one CLI handler → one exit code.**

The `--version` flag uses argparse's `action="version"` (which raises `SystemExit` after printing); `main()` catches `SystemExit` and returns its `code` so the contract `-> int` holds whether the user typed `--version` or `version`.

When introducing a new subcommand, match the precedent verbatim. Don't add per-subcommand `try/except` ladders; the single boundary at `cmd_<name>` is the contract.

## Reference

`plans/super/9-cli-entrypoint.md` — DEC-001 … DEC-027. `src/signalforge/cli/` — current implementation. `docs/cli-ops.md` — operational reference. `tests/cli/` — in-process and subprocess test suite. `tests/test_audit_completeness.py::test_every_typed_error_is_in_exit_code_mapping_table` — 7th AST scan (DEC-024). `tests/llm/test_logger_grep_gate.py` — lazy-format logger gate (6 dirs as of #9). `tests/cli/test_exit_codes.py` — parametrized exception → exit-code contract.

See-Also: clauditor's `.claude/rules/llm-cli-exit-code-taxonomy.md` is the source of the four-tier rule; SignalForge ports it as one section inside this file rather than a standalone rule (DEC-009 of #9 — one rule file per pipeline layer).
