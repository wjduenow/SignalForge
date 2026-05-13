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
  errors.py      # CliError + CliPathError + CliInputError + CliInitDemo*
  generate.py    # add_parser + cmd_generate (the full pipeline)
  init_demo.py   # add_parser + cmd_init_demo (copy bundled demo to disk; issue #47)
  lint.py        # add_parser + cmd_lint (config-only validator)
  version.py     # add_parser + cmd_version (prints signalforge __version__)
```

Flat layout — one module per subcommand, no nested directories, no `__main__.py`. Mirrors clauditor's CLI shape (16 subcommand modules in clauditor; SignalForge ships four as of #47). Every subcommand module exports exactly two public symbols: `add_parser(subparsers) -> None` (registers the subparser, no return) and `cmd_<name>(args) -> int` (handler returning the exit code). The top-level `main(argv: list[str] | None = None) -> int` accepts an explicit argv list (defaults to `sys.argv[1:]`) — that's what makes in-process testing trivial: tests call `main([...])` directly, assert on the returned `int` and capsys output, and never spawn a subprocess.

When v0.2 adds a new subcommand (`signalforge doctor`, `signalforge profile`, ...), match the precedent: one new module under `src/signalforge/cli/`, register via `add_parser(subparsers)` from `main()`, return an int from `cmd_<name>(args)`.

## Library-surface pattern: CLI handler wraps a public lib module at the boundary (issue #47)

Issue #47 introduces a new pattern for subcommands that have a useful programmatic surface — `signalforge init-demo` ships both as a CLI subcommand AND as a public Python function `signalforge.demo.copy_demo(dest, *, force=False) -> Path`. The split:

- **`signalforge.demo`** (subpackage) — the public library entry point. Owns its own typed-error hierarchy (`DemoError` base + `DemoPathError`, `DemoDestExistsError`, `DemoDestUnsafeError`, `DemoFixtureMissingError`). Errors carry an optional `remediation` and a `cause` kwarg mirrored from every other `signalforge.*.errors` module's layer-base pattern. The library function returns useful work product (`Path`) — not just side effects — so notebook / script callers have a clean programmatic surface.
- **`signalforge.cli.init_demo`** (CLI module) — thin handler that argparse-parses its inputs, calls into `signalforge.demo.copy_demo(...)` inside the single `try/except Exception` boundary (DEC-016), and wraps every `DemoError` subclass into the matching `CliInitDemo*Error` (tier 2 for input-validation failures, tier 1 for broken-install / filesystem failures). The CLI owns the next-steps message + exit-code mapping; the library function stays clean of CLI concerns.

This is "two-layer error wrapping" — library typed errors are public (catchable by library callers), CLI typed errors are public (registered in `_EXCEPTION_TO_EXIT_CODE`), and the CLI handler does the translation at the boundary. The 7th AST scan walks BOTH `demo/errors.py` AND `cli/errors.py` so missing tier mappings on either side fail loud. Defence-in-depth: the lower-level `DemoError` subclasses are ALSO registered in `_EXCEPTION_TO_EXIT_CODE` with the same tiers as their CLI wrappers, so a v0.2 contributor who adds a new `Demo*Error` subclass and forgets to wire the CLI wrapper still gets a sensible exit code via `map_exception_to_exit_code`'s MRO walk.

When v0.2 adds a similar dual-surface subcommand (e.g., `signalforge fetch-rubrics` library-callable from a notebook, or `signalforge configure` from a CI bootstrap script), follow this pattern: public lib module with its own `errors.py`, CLI handler wraps at the boundary, both layers' errors land in the exit-code mapping.

## Four-tier exit-code taxonomy (DEC-008, DEC-019, DEC-024)

Every `cmd_<name>` handler returns an integer drawn from exactly four values. Ported from clauditor's `llm-cli-exit-code-taxonomy.md` rule; the wording is locked because CI parsers across repos key on the same boundary. **Do NOT invent a fifth category. Do NOT collapse 2 and 3.**

- **`0` — success.** Artifact written / printed; pipeline completed cleanly.
- **`1` — load-time / parse-layer failure.** "The request was well-formed but the surrounding state is not ready / not coherent." Examples: `ManifestNotFoundError`, `ProfileNotFoundError`, `ConfigNotFoundError`, `DraftConfigInvalidError`, `DiffError` (config-shape), `CliPathError`, the panic-path catch for any untyped `Exception` that escapes a `cmd_<name>` boundary.
- **`2` — input-validation failure.** "The LLM call either shouldn't happen or its output cannot be trusted." Pre-call input errors AND post-call invariant failures share this tier. Examples: `ModelNotFoundError`, `ModelDisabledError`, `LLMOutputAnchorContractError` (invariant violation — the LLM response was structurally valid JSON but violated the anchor contract), `TableNotFoundError` (DEC-012 — the model's table reference is wrong, not the warehouse), `GradeBelowThresholdError` (DEC-011), `DiffCandidateModelMismatchError`, `CliInputError`.
- **`3` — Anthropic API / external dependency failure.** "Something outside our control went wrong; retry later." Examples: `LLMAuthError`, `LLMRateLimitError`, `LLMServerError`, `LLMConnectionError`, `WarehouseAuthError`, `BytesBilledExceededError`, `GradeLLMError`, every fail-closed audit-write durability error (`AuditWriteError`, `PruneAuditWriteError`, `GradeAuditWriteError`, `DiffSidecarWriteError`, `LLMResponseAuditWriteError` — when these fire the disk hand-off didn't happen, which is an external-dep state we couldn't recover).

The mapping table lives at `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`. `map_exception_to_exit_code(exc)` walks `type(exc).__mro__` against the table so subclasses inherit their parent's tier; an unregistered type (or a bare `Exception`) lands at tier 1 per DEC-016 (the panic path).

The 7th AST scan in `tests/test_audit_completeness.py` (DEC-024) walks every `src/signalforge/*/errors.py` (ten modules as of #47: the eight pipeline stages plus `cli/errors.py` plus the new `demo/errors.py`) and asserts every concrete `class <Name>Error(...):` declaration appears in the mapping table. Excluded bases: the ten per-stage abstract bases (`ManifestError`, `WarehouseError`, `SafetyError`, `LLMError`, `DraftError`, `PruneError`, `GradeError`, `DiffError`, `CliError`, `DemoError`). **NOTE:** `LLMHelperError` is deliberately NOT excluded — it is raised directly in `signalforge.llm.client` (three sites), so it's a concrete leaf for taxonomy purposes and must appear in `_EXCEPTION_TO_EXIT_CODE`. A new typed exception lands without a tier mapping → test fails loud.

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
2. **Abstract bases land in `_EXCEPTION_MAPPING_EXCLUDED_BASES`; nine of the ten are also registered in `_EXCEPTION_TO_EXIT_CODE` — dual registration is deliberate (issue #59).** The ten per-stage abstract bases (`ManifestError`, `WarehouseError`, `SafetyError`, `LLMError`, `DraftError`, `PruneError`, `GradeError`, `DiffError`, `CliError`, `DemoError` — eight pipeline stages plus the CLI base plus the demo-layer base added in #47) are listed in the frozenset constant. **Nine of those ten** are also registered in `_EXCEPTION_TO_EXIT_CODE` at a single fallback tier (`ManifestError`/`DiffError`/`CliError` → tier 1, `DraftError` → tier 2, `LLMError`/`WarehouseError`/`GradeError`/`SafetyError`/`PruneError` → tier 3). The two roles are independent: the frozenset excludes the bases from the 7th AST scan (so the scan does not flag them as unmapped concretes); the table entry provides a forward-compat **fallback tier** so a v0.x contributor who adds a new concrete `*Error` subclass under an existing base AND forgets the table entry still gets a sensible exit code via `map_exception_to_exit_code`'s MRO walk (rather than dropping to the panic-path tier 1 default). The 7th AST scan still fails loud on the missing concrete entry — the fallback is a safety net, not a substitute for the per-class entry. **`DemoError` is deliberately the exception.** Its four concretes split across two tiers (`DemoPathError`/`DemoFixtureMissingError` → tier 1; `DemoDestExistsError`/`DemoDestUnsafeError` → tier 2), so no single fallback tier honestly fits — a base-level entry would silently route half its subclasses to the wrong tier. `DemoError` therefore appears only in the frozenset; a new `Demo*Error` concrete that forgets a table entry falls through the MRO walk to the panic-path tier 1 default (and the AST scan catches it loud at test time, which is the intended forcing function). `LLMHelperError` is deliberately NOT in the excluded set despite living one level below `LLMError` — it's raised directly in `signalforge.llm.client` (concrete leaf for taxonomy purposes), so the AST scan must require an explicit mapping for it. The earlier formulation of this rule said "abstract bases land in the frozenset, NOT the table" — that wording was the historical aspiration; the implementation has carried dual registration for nine of the ten bases since v0.1 and the nine-out-of-ten posture is what every reviewer actually sees.
3. **An unregistered concrete class falls to tier 1 via the panic path.** `map_exception_to_exit_code` returns `1` for any class without an MRO match, mirroring `signalforge.cli._helpers._safe_excepthook`'s tier-1 default for bare `Exception`. The 7th AST scan ensures unregistered concretes are caught at test time, not runtime — but the runtime fallback is the safety net.

If v0.2 introduces a new intermediate abstract base (e.g., a `WarehouseTransientError` that sits between `WarehouseError` and the concrete `WarehouseRateLimitError`), add it to `_EXCEPTION_MAPPING_EXCLUDED_BASES` AND document the addition. The exclusion list is a contract surface, not a convenience cache. The same rule applies if a new pipeline subpackage ships its own `errors.py` (an eleventh stage, e.g., `signalforge.cache`): the companion test `test_scan_7_discovers_every_per_stage_errors_module` asserts the scan walks exactly ten `errors.py` files, so the count must be bumped in lockstep with the new stage's abstract base getting added to the excluded-bases set. Issue #47 set the precedent by adding `signalforge.demo` (the 10th `errors.py`) and `DemoError` (the 10th excluded base) in lockstep.

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

`tests/test_audit_completeness.py::test_every_typed_error_is_in_exit_code_mapping_table` is the 7th AST scan in the project (after the six landed by #4 / #5 / #6 / #7). Walks every `*/errors.py` under `src/signalforge/`, collects each `class <Name>Error(...):` declaration via `ast.ClassDef`, and asserts the class is registered in `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`. Excludes the ten per-stage abstract bases from the *required-mapping* check via the frozenset constant `_EXCEPTION_MAPPING_EXCLUDED_BASES`; concrete subclasses inherit via the MRO walk in `map_exception_to_exit_code`. **Nine of the ten** bases are themselves also registered in the table (`DemoError` is the deliberate exception — its concretes span tiers 1 and 2, so no single fallback tier fits) — see § "`_EXCEPTION_TO_EXIT_CODE` mapping table convention" rule 2 above for the dual-registration rationale (forward-compat fallback tier, not a contradiction).

A companion test `test_scan_7_discovers_every_per_stage_errors_module` asserts the scan walks exactly ten `errors.py` files (one per stage subpackage, including `demo/errors.py` from #47). A future stage that forgets to ship `errors.py` (or moves the CLI errors to a sibling location) breaks this test loudly.

Sanity test `test_exit_code_mapping_has_at_least_one_entry_per_tier` asserts every tier (1, 2, 3) has at least one entry in the table — guards against an accidental mass-rename / deletion.

If a new module legitimately needs to declare an `*Error` subclass that should NOT have an exit-code mapping (e.g., a v0.2 abstract intermediate base), update `_EXCEPTION_MAPPING_EXCLUDED_BASES` AND document the addition in this rule's exclusion list. Don't suppress the test; the four-tier taxonomy is a contract, not a guideline.

## Subprocess-gated smoke pattern (DEC-018)

In-process `main(argv)` testing is the right primary pattern (clauditor's choice, fast, deterministic). But it cannot catch:

- `[project.scripts]` wiring in `pyproject.toml` getting deleted / typoed.
- `pip install -e ".[dev]"` followed by `which signalforge` returning nothing.
- Console-script wrapper differences after a wheel rebuild.

**Gated subprocess smoke tests under one marker** — `tests/cli/test_subprocess_smoke.py` ships five tests, all decorated with `@pytest.mark.cli_subprocess`: `test_signalforge_version_via_subprocess` exercises `signalforge --version` (asserts `returncode == 0`, stdout starts with `"signalforge "`, **stderr is empty**); plus four `<subcommand> --help` smokes — `test_signalforge_generate_help_via_subprocess`, `test_signalforge_lint_help_via_subprocess`, `test_signalforge_version_help_via_subprocess`, `test_signalforge_init_demo_help_via_subprocess` — each asserts `returncode == 0`, subcommand-specific tokens in stdout, and a **no-traceback floor** on stderr (`"Traceback" not in stderr`, not stderr-is-empty; argparse's help-rendering path is allowed to emit warnings on some Python builds). All five run against the installed console script. The marker is registered in `pyproject.toml` `[tool.pytest.ini_options].markers` and `addopts` excludes it by default (`-m 'not bigquery and not anthropic and not cli_subprocess'`). Maintainers run `pytest -m cli_subprocess --no-cov` once before declaring a CLI PR ready (mirrors `pytest -m bigquery --no-cov` for the BigQuery adapter, `pytest -m anthropic --no-cov` for the LLM seam). The `--no-cov` flag is required because `--cov-fail-under` in `addopts` would fail marker-specific runs that exercise only a fraction of the codebase.

The `--version` test catches `[project.scripts]` wiring regressions; the `<subcommand> --help` tests catch subparser-registration regressions (a deletion or import-time failure inside any `signalforge.cli.<sub>` module would pass the top-level `--version` smoke but break the real subcommand). When v0.2 adds a new subcommand or changes the console-script wiring, add a parallel `<subcommand> --help` smoke under the same `@pytest.mark.cli_subprocess` marker (and assert a subcommand-unique token in stdout, plus the no-traceback floor) rather than introducing a second gated marker — the single **marker** is the source of truth for "the wheel actually exposes the script."

## Progress to stderr UX (DEC-014, DEC-026)

`cmd_generate` emits one stderr progress line per stage entry plus a paired `done in <X>` line at stage exit. The `<fact>` field is computed from objects already in scope at the entry point (model id, candidate test count, `kept_count × criteria_count`) — never a hardcoded duration hint, because model speeds and warehouse sizes change and stale estimates rot. The `done in <X>` line is the post-hoc real measurement; together they replace any "this can take Xs" prediction with live + measured signal.

TTY-gated by default: `should_emit_progress(quiet, verbose)` returns `True` only when both `sys.stderr.isatty()` and `sys.stdout.isatty()` (either being a pipe disables emission). `--quiet` suppresses regardless of TTY; `--verbose` forces progress on regardless of TTY (the operator explicitly opted in). Non-TTY runs (CI logs, `2>/dev/null`, redirected to a file) emit no progress lines so the captured logs aren't littered with stage chatter.

The four progress helpers (`should_emit_progress`, `format_elapsed`, `emit_progress_entry`, `emit_progress_done`) live in `_helpers.py`; the orchestrator makes a single decision once at startup and passes the bool through stage by stage. Don't introspect TTY-ness mid-pipeline.

## Estimate-style commands degrade on supplementary sub-stage failures (DEC-005 of #36)

Issue #36's `--estimate` flag has TWO data sources — `count_tokens` (load-bearing; the LLM cost half is the whole point) and `adapter.estimate_query_bytes(...)` BigQuery dryRun (supplementary; the warehouse-bytes line is nice-to-have). DEC-005 of `plans/super/36-estimate-cost-preview.md` locks the policy: a `WarehouseError` from the supplementary step is captured into a `warehouse_unavailable_reason` field on the typed report, the renderer prints `<unavailable: <ErrorClass>>`, the engine emits ONE stderr WARNING via lazy-format JSON, and the command still exits 0. The load-bearing source (LLM) failure propagates as today through the existing panic boundary; the supplementary source degrades.

Apply this pattern verbatim to any future preview / dry-run / `--explain`-style CLI flag that has more than one data source where some are supplementary. Three load-bearing rules:

1. **Identify which sources are load-bearing vs supplementary BEFORE writing the engine.** Load-bearing source failures propagate (typed error → existing panic boundary → mapped exit code). Supplementary source failures are caught at the engine boundary and surfaced as typed `*_unavailable_reason: str | None` fields on the report.
2. **Mirror `prune-engine.md` DEC-009 conservative-bias verbatim.** The captured reason is `f"{type(exc).__name__}: {str(exc)[:200]}"`. The WARNING emission is one line, lazy-format JSON. The renderer's `<unavailable: <ErrorClass>>` shape uses the class-name prefix split (`reason.split(":", 1)[0]`). No paraphrasing — these strings are operator-actionable surfaces; CI parsers may key on them.
3. **Pin BOTH the report-field AND the WARNING via tests.** The QG pass-4 lesson (#36 B-3): a test that only pins the `*_unavailable_reason` field does NOT catch a refactor that silently drops the `_LOGGER.warning(...)` breadcrumb. Add a `caplog`-based test alongside the field-shape test. Without the WARNING, operators staring at `<unavailable: ...>` in stdout have no out-of-band signal that the run was degraded.

This generalises both `prune-engine.md` DEC-009 (conservative-bias routing) and `warehouse-adapters.md` cleanup-boundary fail-soft (operator-actionable WARNING) into the CLI-flag surface. It is NOT the same as the panic-path catch (DEC-016 of this file) — that catch maps escaping exceptions to exit codes; this pattern catches inside the engine so the CLI never sees the supplementary error in the first place.

See-Also: `prune-engine.md` § "Conservative drop-reason taxonomy (DEC-006, DEC-011)" for the original conservative-bias routing template; `warehouse-adapters.md` § "Cleanup-boundary fail-soft pattern" for the WARNING-shape contract.

## API alignment with adjacent stages

Every subcommand module exports the same two public symbols:

```python
def add_parser(subparsers: argparse._SubParsersAction) -> None: ...
def cmd_<name>(args: argparse.Namespace) -> int: ...
```

Top-level entry: `def main(argv: list[str] | None = None) -> int`. Tests call `main([...])` directly; the console-script wrapper calls `sys.exit(main())`. No top-level `try/except` in `main()` — every stage orchestrator already follows the "never raise an unwrapped exception" contract, typed errors flow up, the CLI's `cmd_<name>` does the explicit `try/except` per category and returns the right exit code. **One layer's exception → one CLI handler → one exit code.**

The `--version` flag uses argparse's `action="version"` (which raises `SystemExit` after printing); `main()` catches `SystemExit` and returns its `code` so the contract `-> int` holds whether the user typed `--version` or `version`.

When introducing a new subcommand, match the precedent verbatim. Don't add per-subcommand `try/except` ladders; the single boundary at `cmd_<name>` is the contract.

## Multi-model batch driver pattern (issue #37, v0.2)

Issue #37 lands `--select <expr>` for multi-model batch execution in one `signalforge generate` process. The dispatcher in `cmd_generate` routes to either the single-model path (positional `<model>`) or `_run_batch` (when `--select` is supplied). The two paths are mutex via `add_mutually_exclusive_group(required=True)` — argparse rejects both/neither at parser time, exit 2.

**Dispatcher / driver shape.** Three private helpers in `signalforge.cli.generate`:

- `_SingleModelOutcome` — frozen dataclass with `model_unique_id`, `exit_code`, kept/dropped/flagged counts, `rendered_text` (stdout content for this model), `duration_seconds`, `exception_class_name` (set on failure for the aggregated summary).
- `_BatchOutcome` — frozen dataclass with `per_model: tuple[_SingleModelOutcome, ...]` and `total_exit_code = max(...)` across the four-tier taxonomy.
- `_run_single_model(model, manifest, profile, args, *, project_dir, batch_index=None, batch_count=None) -> _SingleModelOutcome` — runs the full safety → draft → prune → grade → diff pipeline for one model. Constructs its OWN `BigQueryAdapter` via `_make_warehouse_adapter(profile)` so each call gets fresh adapter state. `batch_index` / `batch_count` drive the `[i/N] <unique_id>` progress prefix when both non-None.
- `_run_batch(manifest, profile, args, *, project_dir) -> _BatchOutcome` — calls `select_models(manifest, args.select)`, wraps `SelectorParseError → CliSelectorParseError(cause=...)`, raises `CliSelectorNoMatchError` BEFORE any iteration on empty match.

`cmd_generate` is a thin dispatcher: `if getattr(args, "select", None) is not None: _run_batch(...)` else `_run_single_model(...)`. **Use `is not None`, NOT truthiness** — an empty-string `--select ""` is argparse-accepted (the mutex group treats it as "provided") and MUST route to the parser so it raises `CliSelectorParseError`, not fall through to the single-model branch where `args.model is None`. Pinned by `test_select_empty_string_routes_to_parse_error`.

**Fresh adapter per model** (DEC-010 of #37, generalising `warehouse-adapters.md` DEC-002-of-#22). Stateful adapters carry per-call state in instance fields (`BigQueryAdapter._active_session_id` is the v0.2 instance; v0.3 Snowflake / Postgres will have their own). The batch driver constructs a new adapter inside the per-model loop, NOT once at batch start — otherwise state from model N would leak into model N+1's audit and warehouse session. Adds ~100-500ms BQ client init per model; acceptable vs. state-corruption risk.

**Continue-on-failure with `max()` aggregation** (DEC-004). Each `_run_single_model` lives inside its own `try/except Exception` boundary (mirrors DEC-016). A per-model failure records `exception_class_name`, exit-code (via `map_exception_to_exit_code`), and keeps going. The batch's `total_exit_code = max(per_model_exit_codes)` across the four-tier taxonomy (severity rank = tier integer). Failed models named in the aggregated summary with their tier + exception class; cap 50, overflow `... and <K> more`.

**Aggregated summary → stderr** (DEC-005). `format_batch_summary(outcome) -> str` in `cli/_helpers.py` is the single formatter. Headline format (locked verbatim, pinned by test):

```
Generated <K> kept / <L> dropped / <J> flagged across <M> models in <T>s
```

Plus optional failure block when ≥1 model failed. Summary emits when `(matched ≥ 2 OR failed ≥ 1) AND NOT quiet`. Stdout carries rendered diffs in `unique_id` lex order; stderr carries the summary so operators piping `> diffs.txt` get just the diffs.

**Defence-in-depth: scrub control chars in failure-bullet ids.** The failure bullets are column-padded; a `\n` / `\r` / `\t` in any `model_unique_id` would corrupt the CI-parser-keyable column geometry. Real dbt unique_ids never contain control chars (Pydantic-strict-typed at manifest load), but `format_batch_summary` replaces them with single spaces before measuring + emitting. Pinned by `test_format_batch_summary_sanitises_control_chars_in_unique_id`.

**Per-model `[i/N]` progress prefix** (DEC-014). When the batch driver runs AND `_run_single_model` receives non-None `batch_index`/`batch_count` AND `should_emit_progress(quiet, verbose)` returns True, each iteration emits one stderr line `[i/N] <model_unique_id>` before the model's existing stage progress fires. `--quiet` suppresses; `--verbose` forces on regardless of TTY. Single-model positional path emits NEITHER the prefix NOR the summary — preserves v0.1 output shape byte-for-byte.

**Anthropic prompt cache behaviour in batch** (DEC-015). The drafter's *explicitly cache-marked* block is the per-model manifest summary (`<MODEL_SQL>` + neighbours), which changes each iteration — so the marked block does NOT amortise across siblings. Cost savings within one process come from Anthropic's *automatic* caching of the static system prompt only (once it crosses the auto-cache size threshold). Document this honestly in the operator-facing cookbook; the marked-cache claim looks attractive but doesn't materialise on batch runs.

**Sidecar last-writer-wins** (DEC-003). `.signalforge/grade.json` and `.signalforge/diff.json` are `O_TRUNC` overwrite per `cmd_generate` call (locked by `grade-layer.md` DEC-006/012 and `diff-renderer.md` DEC-009). Multi-model in-process iteration overwrites these per model; only the final model's sidecars persist. The four append-only JSONLs (`audit.jsonl`, `llm_responses.jsonl`, `prune.jsonl`, `grade.jsonl`) survive iteration because each record is ≤ 4000 bytes (well under `PIPE_BUF = 4096` on Linux) — POSIX guarantees atomic concurrent appends. Operators who want per-model sidecars use the shell-loop pattern (`docs/cli-ops.md § Running across many models`), one process per `--project-dir`.

**5-surface parity test pattern** (DEC-017). For any new CLI flag whose grammar / examples appear across multiple surfaces, ship a bespoke parity test that reads each surface and asserts the same example tokens appear. `tests/cli/test_5_surface_parity_select.py` is the issue-#37 instance — hard-asserts that `tag:staging`, `path:models/marts/*`, and `tag:staging,path:models/marts/*` appear in argparse help, the cookbook section of `docs/cli-ops.md`, and the plan file. When v0.3 flags ship, copy this test verbatim and re-target. **Don't ship the test with `pytest.skip` branches for surfaces that haven't landed yet** — those become dead code the moment the gating PR merges. Either ship the test gated by a sentinel string check that converts to a hard assert once the surface exists, OR ship the test AFTER all surfaces are committed.

**No new AST scan, no new fail-closed writer.** The batch layer does not introduce a new audit-event class (the per-model writers — safety / draft / prune / grade — already cover the contract; the batch driver just iterates). The 7th AST scan (`test_every_typed_error_is_in_exit_code_mapping_table`) auto-covers the two new errors `CliSelectorParseError` and `CliSelectorNoMatchError` because they live in `cli/errors.py`. Logger grep gate auto-covers new lazy-format calls in `cli/` (6th dir; unchanged).

**Two new exit-code-table entries.** Both `CliSelectorParseError` and `CliSelectorNoMatchError` are tier 2 (input-validation): the operator's selector was syntactically malformed OR resolved to nothing in this project. Mirrors `ModelNotFoundError`'s tier (the positional bare-name case).

When v0.3 introduces parallel batch execution (deferred from #37), the per-model boundary catch needs to coordinate with whatever concurrency primitive lands. The current sequential pattern lays the groundwork: outcome-as-typed-result + max-aggregation generalises cleanly.

## Bare-name model resolution lives in the CLI subcommand, not the manifest layer (issue #49)

`Manifest.get_model(key)` accepts the unique_id form (`model.<pkg>.<name>`) and the file-path form (`models/path/to/<name>.sql`); a bare name like `customers` routes through the file-path branch and surfaces a confusing `ModelNotFoundError` even when a model with that name exists. This is the gotcha pinned by `testing-signal.md` § "Multi-surface drift on user-facing model arguments".

Issue #49 shipped `signalforge lint --model <name>` with a private resolver `_resolve_model_for_lint` in `signalforge.cli.lint` that augments the manifest layer's resolver with a third branch:

```python
if key.startswith("model.") or "/" in key or key.endswith(".sql"):
    return manifest.get_model(key)               # unique_id or file path
matches = [m for m in manifest.iter_models() if m.name == key]
if len(matches) == 1: return matches[0]          # bare name, unambiguous
if len(matches) > 1: raise ModelNotFoundError(... "matches N enabled models" ...)
raise ModelNotFoundError(... "No enabled model with name <key>" ...)
```

Three load-bearing conventions:

1. **The bare-name branch lives in the CLI subcommand, NOT the manifest layer.** `Manifest.get_model` stays a strict two-form resolver because library callers (the prune engine, the drafter, future programmatic consumers) work in unique_ids and don't want bare-name disambiguation cost on every call. The CLI is the only surface where operators type by hand, and that's where the convenience belongs. Mirrors the precedent from `cli-layer.md` § "Stderr message shape" — escape and convenience at the sink, not in the layer.
2. **Multiple matches fail loud with a disambiguation list, not first-wins.** A cross-package collision (two dbt packages each defining a `customers` model) should never silently pick one — the operator's intent is ambiguous. The list is capped at 5 unique_ids + `(+K more)` to keep stderr readable; the remediation tells them to switch to the unique_id or file-path form.
3. **Disabled models do NOT match bare-name lookup.** `iter_models()` yields only enabled `resource_type == "model"` nodes; disabled-package models surface only via the unique_id form (which routes through `get_model` and raises `ModelDisabledError` separately). This keeps the bare-name UX honest — the operator sees only models they could actually run against — while preserving the ability to ask explicitly about a disabled model via its unique_id.

When `cmd_generate` (or any future subcommand that takes a model arg) wants the same affordance, hoist `_resolve_model_for_lint` to `signalforge.cli._helpers` rather than copy-pasting. The two consumers' contracts are identical; the only thing v0.1 ships separately is the lint surface because issue #49's scope was bounded.

## Reference

`plans/super/9-cli-entrypoint.md` — DEC-001 … DEC-027. `plans/super/37-multi-model-select.md` — DEC-001 … DEC-017 (multi-model batch additions). Issue #49 — `cmd_lint` manifest load + `--model` bare-name resolver. `src/signalforge/cli/` — current implementation. `docs/cli-ops.md` — operational reference. `tests/cli/` — in-process and subprocess test suite. `tests/test_audit_completeness.py::test_every_typed_error_is_in_exit_code_mapping_table` — 7th AST scan (DEC-024). `tests/llm/test_logger_grep_gate.py` — lazy-format logger gate (6 dirs as of #9). `tests/cli/test_exit_codes.py` — parametrized exception → exit-code contract. `tests/cli/test_5_surface_parity_select.py` — 5-surface parity for `--select` (issue #37 DEC-017). `tests/cli/test_lint.py::test_lint_resolves_model_*` — bare-name / unique_id / file-path forms (issue #49).

See-Also: clauditor's `.claude/rules/llm-cli-exit-code-taxonomy.md` is the source of the four-tier rule; SignalForge ports it as one section inside this file rather than a standalone rule (DEC-009 of #9 — one rule file per pipeline layer).
