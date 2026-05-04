# Issue #9 — CLI: `signalforge generate` command, config, exit codes

## Meta

- **Ticket:** [#9](https://github.com/wjduenow/SignalForge/issues/9)
- **Branch:** `feature/9-cli-entrypoint` (off `dev`)
- **Worktree:** `/home/wesd/dev/worktrees/SignalForge/feature/9-cli-entrypoint`
- **Phase:** devolved (PR #26 ready-for-review; epic `bd_1-scaffolding-9vj` + 12 stories live in beads)
- **PR:** [#26](https://github.com/wjduenow/SignalForge/pull/26) (draft)
- **Sessions:** 1 (started 2026-05-03)
- **Plan author:** Claude Code (Opus 4.7, 1M context)
- **Milestone:** v0.1 (the user-facing entry point — the LAST v0.1 ticket; ships the runnable `signalforge` command)
- **Labels:** `cli` (per the GitHub issue)

---

## Discovery

### Ticket summary (verbatim from GitHub issue #9)

> **Goal:** The user-facing entry point. `signalforge generate models/marts/foo.sql` runs the full pipeline.
>
> **Acceptance criteria:**
> - `signalforge` console script registered in `pyproject.toml`
> - Subcommands: `generate <model>`, `version`, `lint` (validate config)
> - Flags: `--mode`, `--min-score`, `--dry-run`, `--write` (vs default print-diff)
> - Reads `signalforge.yml` config from project root
> - Exit codes per the four-tier taxonomy from clauditor's rules: 0 success / 1 load / 2 input / 3 API
> - `--help` is informative; subcommand help works
> - Smoke test: `signalforge --version`, `signalforge lint --help`
>
> **Notes:** Mirror clauditor's CLI structure: per-subcommand module under `cli/`

The CLI (#9) sits at the top of the dependency stack and is the **first user-facing surface** of the project. It does not introduce new pipeline behaviour; it wires existing stages — `manifest.load` → `safety.build_llm_request` (via `draft.draft_schema`) → `prune.prune_tests` → `grade.grade_artifacts` → `diff.render_diff` — into a single command. Every other v0.1 ticket has shipped; this one is the cap.

This encodes Architectural Commitment #4 (OSS-first, Core-friendly): the runnable surface a user installs from PyPI and runs against any dbt-core project, locally or in CI. The README's quick-start at lines 45–48 commits to the exact shape `signalforge generate models/marts/customer_lifetime_value.sql`.

### Codebase findings (Subagent B — directly verified, file:line cited)

**Public surface of every existing stage** is already consistent — every stage exposes:

- **Config loader:** `load_<stage>_config(project_dir, path=None) -> <Stage>Config`. Returns immutable Pydantic models. Each loader claims its own top-level key in `signalforge.yml`: `safety:` / `llm:` / `prune:` / `grade:` / `diff:`. Missing config files silently return defaults; explicit-path-missing raises typed errors (uniform across stages).
- **Orchestrator entry:** data front-paired positionally, keyword-only optionals after `*`. Examples:
  - `signalforge.manifest.load(project_dir, manifest_path=None) -> Manifest` — `src/signalforge/manifest/loader.py:1`
  - `signalforge.warehouse.load_profile(project_dir, target=None) -> DbtProfileTarget` — `src/signalforge/warehouse/profiles.py:10`
  - `signalforge.warehouse.WarehouseAdapter.from_profile(profile) -> WarehouseAdapter` — `src/signalforge/warehouse/base.py:1`
  - `signalforge.safety.load_safety_config(project_dir, path=None) -> SafetyPolicy` — `src/signalforge/safety/config.py:20`
  - `signalforge.draft.draft_schema(model, adapter, policy, manifest, *, config) -> DraftOutcome` — `src/signalforge/draft/schema.py:1`
  - `signalforge.prune.prune_tests(model, adapter, candidates, manifest, *, config=None, audit_path=None, project_dir=None) -> PruneResult` — `src/signalforge/prune/engine.py:1`
  - `signalforge.grade.grade_artifacts(model, candidate, prune_result, *, rubric=None, config=None, audit_path=None, sidecar_path=None, client=None, project_dir=None) -> GradingReport` — `src/signalforge/grade/engine.py:1`
  - `signalforge.diff.render_diff(model, candidate, prune_result, *, grading_report=None, existing_schema=None, config=None, output_path=None, sidecar_path=None, write_sidecar=True, project_dir=None) -> DiffReport` — `src/signalforge/diff/engine.py:1`
- **Error hierarchy:** each stage exports a base `<Stage>Error` plus typed subclasses. Total error families exposed at the public surface: `ManifestError` (6 subclasses), `WarehouseError` (~14), `SafetyError` (9), `LLMError` (6), `DraftError` (2), `PruneError` (5), `GradeError` (8), `DiffError` (6). Each base class renders `↳ Remediation:` line via `__str__`. Pattern-match on type, never on message text.

**Model resolver.** `Manifest.get_model(key: str | Path) -> Model` at `src/signalforge/manifest/models.py:47`. Accepts either a dbt `unique_id` (`"model.proj.customers"`) or a file path (relative or absolute). Symlink-canonicalised against `project_dir`. Raises `ModelNotFoundError`, `ModelDisabledError`, `ModelPathOutsideProjectError`, `ModelMissingSqlError`. The CLI's `generate <model>` arg routes here directly — no new resolver code needed.

**SamplingMode enum.** `signalforge.safety.SamplingMode` at `src/signalforge/safety/models.py` — `str + Enum` with three members: `"schema-only"`, `"aggregate-only"`, `"sample"`. `--mode {schema-only|aggregate-only|sample}` maps 1:1.

**GradeConfig thresholds.** `min_pass_rate: float = 0.7` and `min_mean_score: float = 0.5` at `src/signalforge/grade/config.py:60`. `fail_on_below_threshold: bool = False` is **a v0.1 no-op explicitly reserved for v0.2 enforcement (DEC-005 of #7)**. `--min-score` is the natural surface for `min_mean_score`. **The grade layer's docstring says "v0.2 will wire this into the CLI exit-code path" — meaning the CLI ticket has a real choice to make about whether `--min-score` is a v0.1 reporting-only knob or whether we wire `fail_on_below_threshold` early. See SQ-04.**

**diff renderer.** `render_diff` always returns a `DiffReport` (which carries `unified_diff` text). When `output_path` is supplied, the renderer writes ANSI/Markdown text via the fail-closed atomic-write seam. When `write_sidecar=True` (the default), JSON sidecar lands at `<project_dir>/.signalforge/diff.json`. The `DiffConfig.render_kind` literal `"ansi"|"markdown"|"json"` selects the human-facing renderer; sidecar is always JSON regardless. **The diff layer's DEC-021 colour-precedence chain and `respect_no_color_env: bool = True` are already in place — the CLI just wires `--no-color` / `NO_COLOR` env detection through.**

**No CLI yet.** No `[project.scripts]` in `pyproject.toml` (verified line-by-line). No `cli/` subpackage. The CLI will be the **9th** subpackage under `src/signalforge/` (after manifest, warehouse, safety, llm, draft, prune, grade, diff).

**Logging convention.** Each module owns its own `_LOGGER = logging.getLogger("signalforge.<stage>")` (or `__name__`). No project-wide config. The CLI is the right place to configure the root logger — and the only place that should call `logging.basicConfig()`.

**dbt project root convention.** `manifest.load(project_dir, ...)` expects `project_dir` to be the dbt project root (the dir containing `dbt_project.yml`). `load_profile` reads `<project_dir>/dbt_project.yml` for the profile name. **No automatic root-detection (e.g., walk-up-to-find-`dbt_project.yml`) exists today.** The CLI must either default to `Path.cwd()` and instruct the user to `cd` into the project, OR walk up to find `dbt_project.yml`. See SQ-01.

### Convention findings (Subagent C — rules and CLAUDE.md)

The Convention Checker mined every `.claude/rules/*.md` and `CLAUDE.md`. Twenty applicable constraints; the load-bearing ones:

1. **Grep gate must extend to 6 directories.** `tests/llm/test_logger_grep_gate.py` currently scans `{llm, draft, prune, grade, diff}` (5 dirs as of #8). The diff-renderer rule explicitly cites #9: *"Extend the scan when the CLI (#9, sixth dir) ships, rather than copy-pasting a per-layer gate; the single test is the source of truth."* Every `_LOGGER.{info,warning,...}` call in `src/signalforge/cli/` must use lazy `%s` + `json.dumps(...)`, never f-strings.

2. **Symlink-hardened path canonicalisation** at the orchestrator entry. The CLI accepts `--config`, `--manifest`, `--profiles`, `--output`, `--sidecar-path` — every one must go through `signalforge.warehouse._path_safety.canonicalise_path(raw_path, project_dir)`. Three traps: `.resolve()` before `.is_relative_to()`; catch `RuntimeError` on cycles; gate the *default* path through the same helper.

3. **No new AST audit-completeness scan.** As of #7 there are 6 scans; #8 (diff) added zero. The CLI does NOT introduce a new audit-event class (the CLI orchestrates; results live in stage models). Confirmed: no 7th scan required.

4. **Top-level YAML namespace.** Every prior stage claimed its own key. The diff-renderer rule explicitly *reserves `cli:` for the future*. The CLI ticket can either claim `cli:` for new behaviour-knobs, OR refrain from adding a config block at all (the CLI's job is largely orchestration; flags are sufficient for v0.1). See SQ-03.

5. **Config-shaped models use `extra="forbid"`; read-back models use `extra="ignore"`.** If the CLI defines a `CliConfig` Pydantic model for a `cli:` block, follow the established `_CliConfigFile(extra="ignore")` outer + `CliConfig(extra="forbid")` inner pattern. Drift detector mandatory (test-only `StrictCliConfig(extra="forbid")` validating committed fixtures).

6. **API alignment with adjacent stages.** If the CLI introduces a config loader, it must match `load_cli_config(project_dir, path=None) -> CliConfig`. Resolution order: explicit `path` > `<project_dir>/signalforge.yml cli:` > defaults. Mirrors all five existing stage loaders verbatim.

7. **`__repr__` rule** on result-shaped models — likely **not applicable**. The CLI orchestrates; it doesn't produce new user-content-bearing models. If a CLI helper does (e.g., a `RunSummary` carrying paths and counts), apply the rule.

8. **Architectural commitments** are load-bearing for the CLI:
   - **Signal over volume** — no `--skip-prune` flag (would let always-pass tests ship).
   - **Evaluation in the loop** — every kept artifact must be graded by default. If a `--no-grade` escape hatch is offered, document the trade-off. See SQ-05.
   - **Warehouse-agnostic by design** — no BigQuery-specific flags (e.g., no `--gcp-project`); the warehouse comes from `profiles.yml`.
   - **OSS-first, Core-friendly** — must run against any dbt-core project locally or in CI. No dbt Cloud dependency.
   - **Explainable diffs** — every kept/dropped artifact ships with a one-line "why." The CLI must surface the diff (or a summary) on every successful run.

9. **Pipeline shape from CLAUDE.md.** Ordering is non-negotiable: safety → draft → prune → grade → diff. The CLI enforces this sequence with no skips and no reordering.

10. **No `workflow-project.md` file exists** in the repo. No project-specific scoping questions or extra review areas to layer in.

11. **Smoke test floor** from `testing-signal.md` DEC-010. The CLI ticket's acceptance criteria explicitly call for: `signalforge --version` works, `signalforge lint --help` works. These are the smoke floor for the layer; they belong in `tests/cli/test_smoke.py`.

12. **CI supply chain** — pinned action SHAs, scoped tokens, single Python (3.11). If the CLI ticket adds a workflow (e.g., gated CLI integration test), follow the pattern. Most likely CI changes are: install the package, ensure `signalforge --version` returns `0.1.0.dev0`. **Default: no new workflow file; the existing CI already runs `pytest` which exercises the in-process `main(argv)` smoke tests.**

### Domain research (Subagent D — clauditor's CLI structure + exit-code taxonomy)

clauditor (the sister project this ticket explicitly says to mirror) publishes the four-tier exit-code taxonomy as a standalone rule file: `.claude/rules/llm-cli-exit-code-taxonomy.md`. The taxonomy verbatim (we will port this rule into SignalForge's rules dir as part of this ticket; see SQ-09):

> - **0 — success.** Artifact written (or printed to stdout via `--json`, `--dry-run`, etc.).
> - **1 — load-time / parse-layer failure.** Missing prior sidecar the command reads from, existing output without `--force`, model returned unparseable JSON, OS/disk error writing the final artifact. "The request was well-formed but the surrounding state is not ready / not coherent."
> - **2 — input-validation failure.** Pre-call input errors (oversize token budget, missing required skill file, malformed spec layout, `--from-capture` / `--from-iteration` pointing at a missing target) AND post-call invariant failures (LLM output structurally valid JSON but violates a domain invariant). "The LLM call either shouldn't happen or its output cannot be trusted."
> - **3 — Anthropic API failure.** Auth error, rate-limit exhaustion, 5xx, or connection failure surfaced from the LLM SDK. "Something outside our control went wrong; retry later."
>
> Do NOT invent a fifth category. Do NOT collapse categories 2 and 3 into one "bad exit"; pipelines need the split to decide retry vs don't-retry.

**Mapped to SignalForge's existing error hierarchy** (proposal — refined in Architecture Review):

- **Exit 0** — `generate` completes; `lint` validates clean; `version` prints.
- **Exit 1 — load.** `ManifestError` family (project_dir / manifest.json missing); `ProfileNotFoundError`, `ProfileTargetNotFoundError`; `SafetyError.ConfigNotFoundError` / `InvalidConfigError`; `DraftConfigNotFoundError` / `DraftConfigInvalidError`; `PruneConfigError`; `GradeConfigError`; `DiffError` config errors.
- **Exit 2 — input.** `ModelNotFoundError`, `ModelDisabledError`, `ModelPathOutsideProjectError`, `ModelMissingSqlError`; `ColumnNotFoundError`, `InvalidIdentifierError`; `SafetyError.ColumnNotInModelError`, `InvalidSamplingModeError`; `PruneTrustedModelNotFoundError`; `DiffCandidateModelMismatchError`, `DiffPruneResultModelMismatchError`, `DiffGradingReportModelMismatchError`, `DiffInputTooLargeError`; `LLMOutputError` (LLM response did not parse to valid candidates — invariant violation); `GradePromptEnvelopeBreachError`.
- **Exit 3 — API.** `LLMError` family (auth / rate-limit / 5xx / connection / cache too large/small); `WarehouseError` connectivity-flavoured subclasses (`BytesBilledExceededError`, `QuerySyntaxError` if from real query, `WarehouseAuthError`); `PruneTimeoutError`, `PruneAuditWriteError`; `GradeLLMError`, `GradeBudgetExceededError`, `GradeAuditWriteError`; `DiffSidecarWriteError`; `SafetyError.AuditWriteError`, `AuditRecordTooLargeError`.

**Ambiguity: `TableNotFoundError`** in warehouse — is that input (the model points at a nonexistent table — the user's mistake) or API (the warehouse is in a state we didn't expect)? **Tentative call: exit 2 (input)** — the model definition is wrong. Confirm in refinement.

**Mirrors from clauditor (verified at HEAD of `wjduenow/clauditor` main):**

- **Layout.** `src/clauditor/cli/` is **flat** — one module per subcommand, no nested directories, no `__main__.py`. 16 subcommand modules total (clauditor has many; SignalForge starts with three). Console script entry: `clauditor = "clauditor.cli:main"` in `pyproject.toml`.

- **Library.** Pure stdlib `argparse`. clauditor demonstrates argparse scales to 16 subcommands cleanly. No Click or Typer in the dep list. **Recommendation: argparse for SignalForge too** — zero new runtime deps.

- **Subcommand module shape.** Every module exposes exactly two public symbols:
  - `add_parser(subparsers: argparse._SubParsersAction) -> None` — registers the subparser, no return value.
  - `cmd_<name>(args: argparse.Namespace) -> int` — handler, returns the exit code (0/1/2/3).

- **Top-level entry point.** `def main(argv: list[str] | None = None) -> int` accepts an explicit argv list (defaults to `sys.argv[1:]`). This is what makes in-process testing trivial. Tests call `main([...])` directly and assert on the returned `int`. No subprocess, no CliRunner.

- **Stable stderr message shape.**
  - Exit 1 and 3 print a single `ERROR: <message>` line.
  - Exit 2 prints a header line plus one `  - <message>` line per error.
  - **CI parsers key on the shape** — load-bearing.

- **No top-level `try/except` in `main()`.** Every stage orchestrator already follows the "never raise an unwrapped exception" contract — typed errors flow up, the CLI's `cmd_<name>` does the explicit `try/except` per category and returns the right exit code. **One layer's exception → one CLI handler → one exit code.**

- **Test pattern.** `tests/test_cli.py`-style: `main([...])` + `capsys` from pytest. Assert on returned `int` and captured stdout/stderr. Parametrize grouped-behaviour assertions across commands (e.g., "every command that takes a manifest path errors-2 on missing manifest with no traceback"). The shape `assert "Traceback" not in err` enforces the "no uncaught exception ever leaks a traceback to the user" rule.

- **Diverge: wire `--version` properly.** clauditor's README claims `clauditor --version` works but the CLI doesn't actually register it (gap). SignalForge already exports `signalforge.__version__` from `src/signalforge/__init__.py` (Hatchling-dynamic) — registering `--version` is one line in `argparse`:

  ```python
  parser.add_argument("--version", action="version", version=f"signalforge {signalforge.__version__}")
  ```

- **Diverge: shared helpers in `cli/_helpers.py`, not the package `__init__.py`.** clauditor uses lazy-import-from-`__init__` to break a circular dependency. Cleaner: put shared helpers in `src/signalforge/cli/_helpers.py` and import directly. No cycle, no `# noqa: E402,F401` noise.

- **Diverge: `--version` flag AND `version` subcommand.** The ticket's acceptance criteria call for both. clauditor has neither. Both are trivial — `--version` is `argparse`'s `action="version"`; `version` subcommand prints the same string and exits 0.

### Proposed scope (CLI v0.1)

A flat `src/signalforge/cli/` subpackage containing:

- `__init__.py` — package entry; defines `def main(argv: list[str] | None = None) -> int`; builds the top-level argparse parser; dispatches to subcommand `cmd_<name>(args) -> int`.
- `_helpers.py` — shared helpers (path canonicalisation gate; structured-stderr formatter; exit-code mapper from typed exception → 1/2/3; logger setup).
- `_config.py` — minimal `CliConfig` (if any; see SQ-03) + `load_cli_config(project_dir, path=None)`. May be omitted in v0.1 if no `cli:` block is needed (flags suffice).
- `generate.py` — `signalforge generate <model>` — wires manifest → safety → draft → prune → grade → diff.
- `version.py` — `signalforge version` — prints `signalforge {__version__}` and exits 0.
- `lint.py` — `signalforge lint` — loads every `signalforge.yml` config block (`safety:`, `llm:`, `prune:`, `grade:`, `diff:`, `cli:`), reports any errors as exit-1 (load) or exit-2 (input invariants).

`pyproject.toml` adds:

```toml
[project.scripts]
signalforge = "signalforge.cli:main"
```

`tests/cli/`:

- `test_smoke.py` — `signalforge --version`, `signalforge lint --help`, no traceback ever leaks.
- `test_main.py` — top-level dispatch, unknown command, no command, `--help`.
- `test_generate.py` — happy path (with fakes for adapter + LLM), each error category routes to the right exit code, `--mode` / `--min-score` / `--dry-run` / `--write` behaviours.
- `test_lint.py` — config validation across all six top-level keys.
- `test_version.py` — both `--version` flag and `version` subcommand.
- `test_exit_codes.py` — parametrized: every typed exception in the project maps to the right exit code with the right stderr shape. **This is the load-bearing test that makes the four-tier taxonomy a contract, not a guideline.**
- `test_drift_detector.py` — only if a `CliConfig` ships with config-block fields.

`tests/llm/test_logger_grep_gate.py` extends to 6 directories.

A new `.claude/rules/cli-layer.md` distils the rules established in this ticket (mirrors every prior stage having its own rule file).

### Scoping questions

**SQ-01 — Project root discovery.** `manifest.load(project_dir, ...)` expects `project_dir` to be the dbt project root. The CLI needs to find this. Options:

- **A.** Default to `Path.cwd()`. User must `cd` into the project. Fail loudly with a remediation hint if `dbt_project.yml` is not in cwd.
- **B.** Walk up from cwd to find `dbt_project.yml`. Mirrors how `git` finds `.git`. More forgiving; a touch of magic.
- **C.** Require an explicit `--project-dir` (or accept it as an env var `SIGNALFORGE_PROJECT_DIR`). Most explicit; least friendly.
- **D.** Hybrid: walk up, **but** allow `--project-dir` as override and emit a debug log noting which dir was discovered.

**Recommendation: D.** Walk-up matches user expectation (every dbt CLI does this), the override is a cheap escape hatch for monorepos, and the debug log is one `_LOGGER.debug` call.

**SQ-02 — Default `--write` vs print-diff behaviour.** The ticket calls for `--write` (vs default print-diff). What does "print-diff" actually print?

- **A.** Render the diff to stdout using `DiffConfig.render_kind` (default ANSI), no file written.
- **B.** Render to stdout AND emit the JSON sidecar to `<project_dir>/.signalforge/diff.json` (the diff layer's default). User gets both human-readable and machine-consumable output without `--write`.
- **C.** Render to stdout, no sidecar by default; sidecar requires `--sidecar` or `--write`.

**Recommendation: B.** `render_diff(write_sidecar=True)` is the diff layer's existing default — preserve it. The sidecar is fail-closed and atomic; it's not a side-effect risk. `--write` is then specifically about writing the proposed `schema.yml` to disk (modifying the user's repo), which is the loud action `--dry-run` should suppress.

**SQ-03 — Does the CLI need a `cli:` config block?**

- **A.** No `cli:` block in v0.1. Flags are sufficient. Reserve the namespace silently (the YAML loader's outer `extra="ignore"` already does this). Saves a config model + drift detector + fixture.
- **B.** Ship `cli:` with one or two knobs that DON'T fit on the command line — e.g., `cli.default_mode`, `cli.default_min_score` so a project can set defaults that flags can override. Adds a config model + drift detector but matches every other stage's "claim your namespace" pattern.
- **C.** Ship `cli:` with knobs that affect orchestration semantics (e.g., `cli.fail_on_grade_below_threshold: bool` — the v0.2 reservation from #7 wired earlier). Binds the CLI to a v0.2 commitment.

**Recommendation: A.** `cli:` adds surface area without solving a real v0.1 problem. The ticket's acceptance criteria don't mention a `cli:` block. Reserve the key by way of the outer-`extra="ignore"` (existing behaviour) without writing a model or fixture. v0.2 can claim the key when there's a concrete knob that wants to live there.

**SQ-04 — `--min-score` semantics.** GradeConfig ships `min_pass_rate` and `min_mean_score`, and `fail_on_below_threshold` is a v0.1 no-op explicitly reserved for v0.2. Three options:

- **A.** `--min-score N` maps to `GradeConfig.min_mean_score` and is **reporting-only in v0.1**. The grade layer respects the v0.2 reservation; the diff just shows below-threshold artifacts as `flagged` (already implemented in #8). Exit code stays 0.
- **B.** `--min-score N` maps to `min_mean_score` AND wires `fail_on_below_threshold=True` so a below-threshold run exits non-zero (e.g., exit 2 — input/invariant). Brings the v0.2 behaviour forward into v0.1 explicitly.
- **C.** Defer `--min-score` to v0.2 entirely. Drop from v0.1 acceptance criteria.

**Recommendation: A.** Match the grade layer's documented v0.2 reservation. Adds a flag that's "soft" (informs the threshold for the rendered diff's `flagged` tier) without breaking the v0.1 contract. The diff already renders `flagged` tier when grading is below threshold (`signalforge.diff` DEC-012). Wiring `fail_on_below_threshold` is genuinely a v0.2 contract — bringing it forward conflicts with the rule file.

**SQ-05 — Is grading optional?** The CLI runs all five stages by default. Should it offer `--no-grade` to skip the (LLM-cost-incurring) grade pass?

- **A.** No `--no-grade`. Grading is part of every run — Architectural Commitment #2 (evaluation in the loop). Cost is a v0.2 budget conversation, not a v0.1 escape hatch.
- **B.** Yes, `--no-grade`. Cost-conscious users skip grading; the diff renders without a grading report (kept/dropped only, no `flagged` tier — already supported by `render_diff(grading_report=None)`).
- **C.** `--grade {default|skip|...}` — three-way selector that matches the safety-mode pattern.

**Recommendation: A** in v0.1 (commit to the architectural principle). If user feedback wants it, B is a clean follow-up: `render_diff` already accepts `grading_report=None` and treats it correctly. **But** if the user says "I want a `--no-grade`," B is trivial — three lines of code.

**SQ-06 — `lint` subcommand scope.** The ticket says `lint (validate config)`. What does it validate?

- **A.** Just call every config loader and report errors. Cheap, fast, no warehouse / no LLM calls.
- **B.** A + check that `dbt_project.yml` and `profiles.yml` are loadable (`load_profile` works) and that the profile's `target` is reachable for auth (a `WarehouseAdapter.from_profile` round-trip). Adds a real warehouse call — slower but more useful.
- **C.** A + B + dry-run a `manifest.load` for a sentinel model to confirm the manifest is parseable.

**Recommendation: A.** "Validate config" in the ticket is most naturally the `signalforge.yml` blocks and the dbt `profiles.yml` parse (no auth check, no warehouse round-trip). Fast. `signalforge lint` should run in <1 second and never make a network call. Auth/connectivity goes under a future `signalforge doctor` command (or `--check-warehouse` flag in v0.2).

**SQ-07 — `--manifest` and `--profiles` overrides.** Should the CLI offer overrides for the manifest path and the profiles.yml location?

- **A.** Yes to both. `--manifest <path>` and `--profiles <path>` (the latter is dbt's standard `--profiles-dir` flag — match dbt's name).
- **B.** Yes to `--manifest`; rely on env var `DBT_PROFILES_DIR` for the profiles path (dbt's standard env var).
- **C.** Neither. Always use defaults. Forces discipline.

**Recommendation: A.** Trivial to add (already pass through to `manifest.load(manifest_path=...)` and `load_profile`); matches dbt's `--profiles-dir` ergonomics; saves users from monorepo gymnastics. Both paths flow through `canonicalise_path` for symlink safety.

**SQ-08 — Stderr message shape for exit 2.** clauditor's pattern is "header line + bullets" for exit 2:

```
ERROR: model 'customers_v2' has 3 validation errors:
  - column 'phantom' not in model
  - column 'foo' has duplicate not_null tests
  - test 'relationships' references missing model 'orders'
```

Does SignalForge follow this verbatim or simplify?

- **A.** Match clauditor verbatim — header + bullets for 2; single line for 1 and 3. CI parsers key on the shape. Best for tool interop.
- **B.** Simplify — single line for every category; the user reads the full reason. Simpler code, less consistent across tools.

**Recommendation: A.** The contract is mechanical; CI integration is the goal of the four-tier taxonomy.

**SQ-09 — Port the four-tier exit-code rule from clauditor?**

The taxonomy itself is a set of 4 tiers (clauditor's wording). Two ways to land it in this repo:

- **A.** Port `.claude/rules/llm-cli-exit-code-taxonomy.md` from clauditor verbatim, add SignalForge-specific examples (exception → tier mapping). One canonical source.
- **B.** Write a fresh `.claude/rules/cli-layer.md` that includes the four-tier taxonomy alongside other CLI-specific rules (logger gate, path safety, stderr shape).

**Recommendation: B.** SignalForge's pattern is one rule file per layer (`safety-layer.md`, `llm-drafter.md`, etc.). `cli-layer.md` matches. The four-tier taxonomy is one section inside it; cite clauditor's source rule in the See-Also footer.

**SQ-10 — `--dry-run` semantics.** The ticket says `--dry-run` is a flag. What does it suppress?

- **A.** `--dry-run` skips every LLM and warehouse call; prints what *would* be drafted (model summary, prompt preview, etc.). Useful for cost estimation.
- **B.** `--dry-run` runs the full pipeline (LLM + warehouse + grade) but does NOT write the proposed `schema.yml` to disk and does NOT write the JSON sidecar. Effectively `--write=false` + `--no-sidecar`.
- **C.** `--dry-run` runs to the diff stage, prints the diff to stdout, but does not write any files.

**Recommendation: C** — most useful in practice. Runs the full pipeline, shows the diff, doesn't touch the user's repo. **`--dry-run` and `--write` are mutually exclusive**: `--dry-run` is the "show me what would happen" knob; `--write` is the "actually do it" knob; their combination is undefined.

### Scoping answers (session 1 — 2026-05-03)

| Q | Answer | Notes |
|---|--------|-------|
| SQ-01 | **D** | Walk-up to find `dbt_project.yml`; `--project-dir` overrides; debug-log the discovered dir |
| SQ-02 | **B** | Default print-diff = stdout + JSON sidecar (preserves diff-layer's `write_sidecar=True` default) |
| SQ-03 | **C** | **Diverges from rec.** Ship `cli:` block in v0.1 with `cli.fail_on_grade_below_threshold` (and likely `cli.default_mode`, `cli.default_min_score`). **Tension to resolve in Architecture Review:** this graduates the grade-layer's documented v0.2 reservation (`GradeConfig.fail_on_below_threshold` no-op) — see AR-Documentation. |
| SQ-04 | **A** | `--min-score` is reporting-only; sets the flag-threshold for the `flagged` tier in the diff; never affects exit code by itself. Exit-code-on-below-threshold is the SQ-03 `cli:` config knob (separate concern, separate seam) |
| SQ-05 | **A** | No `--no-grade`. Architectural Commitment #2 (evaluation in the loop) is non-negotiable in v0.1 |
| SQ-06 | **A** | `lint` is config-only — no warehouse, no LLM, no network. Sub-second target |
| SQ-07 | **A** | Both `--manifest <path>` and `--profiles-dir <path>` (matching dbt's flag name); both flow through `canonicalise_path` |
| SQ-08 | **A** | Stderr shape: exit 1 and 3 → single `ERROR: <msg>` line; exit 2 → header + `  - <msg>` bullets. CI parsers key on the shape |
| SQ-09 | **B** | New `.claude/rules/cli-layer.md`; the four-tier taxonomy is one section inside; cite clauditor's source rule in See-Also |
| SQ-10 | **C** | `--dry-run` runs the full pipeline (LLM + warehouse), prints the diff, writes nothing. `--dry-run` and `--write` mutually exclusive |

### Implications of the SQ-03 / SQ-04 split

The combination is intentional, not contradictory: **two independent surfaces**, layered.

- `--min-score N` (CLI flag, SQ-04) → drives the diff renderer's `flagged` tier classification. The user sees which artifacts fall below the bar. Always reporting-only; never affects exit code.
- `cli.fail_on_grade_below_threshold: true` (signalforge.yml `cli:` block, SQ-03) → if set, the CLI exits **2** (input/invariant failure) when grade pass-rate or mean-score falls below the threshold. Default `false` — opt-in.

This means we have a `cli:` block in v0.1 with at least one knob: `fail_on_grade_below_threshold: bool = False`. Likely two more from SQ-03 C's spirit: `default_mode: SamplingMode | None = None` (overrides safety's default; CLI flag still wins); `default_min_score: float | None = None` (overrides the threshold; CLI flag still wins). **All three knobs follow the precedence chain CLI flag > `cli:` block > stage default.**

A new `CliConfig` Pydantic model + `load_cli_config(project_dir, path=None)` + drift detector + fixture is now in scope.

---

## Architecture Review

_Phase 2 — runs after scoping answers were captured. The CLI is unusual among the v0.1 stages: it does not call the LLM or the warehouse directly, but it orchestrates every stage that does. Review areas adapted accordingly._

### Reviewer ratings

| Area | Rating | Headline |
|---|---|---|
| AR-Exit-code coverage | **BLOCKER** | 2 referenced errors aren't exported from `signalforge.draft.__init__`; 1 ambiguous tier; 12 unmapped exceptions |
| AR-Observability | **CONCERN** | Silent multi-minute pauses; render_diff doesn't return rendered text; 4 sub-concerns to resolve |
| AR-Testing | **CONCERN** | Subprocess smoke test missing as belt-and-braces; AST-scan recommended over hand-maintained mapping list |
| AR-SQ-03 graduation impact | **BLOCKER** | Reviewer pushes back on SQ-03=C surface area; recommends a HYBRID: graduate grade-layer's `fail_on_below_threshold` (one knob, real wiring) and skip the `cli:` config block in v0.1 |

---

### AR-B1 — BLOCKER: missing exports from `signalforge.draft`

`src/signalforge/draft/errors.py` defines `DraftConfigNotFoundError` (line 331) and `DraftConfigInvalidError` (line 353), but **neither is re-exported** from `src/signalforge/draft/__init__.py`. The plan's Exit-1 mapping (load) references both. The CLI's exception → exit-code table cannot land before these are exposed.

**Resolution:** Add both to `signalforge.draft.__init__` re-exports as part of the CLI ticket. One-line fix; deserves a tiny stand-alone story so the CLI's tests can `import` them without sneaking through the private module path.

The same audit also finds **6 unexported subclasses** that the plan tier-maps but doesn't expose: `LLMOutputJSONError`, `LLMOutputValidationError`, `LLMOutputAnchorContractError`, `PromptEnvelopeBreachError`, `LLMResponseAuditRecordTooLargeError`, `LLMResponseAuditWriteError`, plus `LLMResponseFormatError` from `signalforge.llm`. **Refinement decision:** do these need exporting too? My take is yes for all — the CLI tier-maps them, so they should be importable from the public surface — but it's a refinement question.

### AR-B2 — BLOCKER: `TableNotFoundError` tier ambiguity (and 11 others)

Twelve typed exceptions are unmapped in the plan. Most are subclass-of-mapped-base, so they inherit tier from the parent base. The audit found **one** real ambiguity:

- **`TableNotFoundError`** — tier 1 (load: warehouse schema drift since the manifest was generated) vs tier 2 (input: the model's table reference is wrong). My take: **tier 2** — this means the model's `materialized` is for a table that does not exist; treating it as input keeps "warehouse connectivity = exit 3, table mistake = exit 2" clean. **Refinement decision required.**

The 11 others (mostly `*Error` base classes and audit-too-large variants) are unambiguous; mapping just needs to be made explicit in the rule file. See AR-T2 below for an AST-scan that prevents future drift.

### AR-O1 — CONCERN: silent multi-minute pauses

No stage emits user-facing progress. A `signalforge generate` run on a real warehouse:

- Drafting (LLM): 30–90 s.
- Pruning (warehouse queries × N tests): 30 s – 5 min.
- Grading (LLM × M artifacts × N criteria — sequential per `grade-layer.md` DEC-027): 1–10 min.
- Diff: <100 ms.

User sees a blank terminal for the entire duration unless they pipe the existing `_LOGGER` channel somewhere. This is a UX failure for the first user-facing command in the project.

**Refinement decision:** what to do about it?
- **A.** Ship without progress. Document "this can take minutes."
- **B.** Add a stderr progress line per stage (`[1/5] safety: building LLM request...`). One line per stage entry, no polling, no spinners. ANSI-stripped per the user's `--no-color`/`NO_COLOR` decision.
- **C.** B + a `--quiet` flag that suppresses progress.
- **D.** Spinners / live counters / fancy progress (rejected — adds a dep, uneven with non-TTY environments).

My take: **C**. Five `print(..., file=sys.stderr)` calls. Honor TTY detection so non-TTY pipelines don't get noise. No new dependency.

### AR-O2 — CONCERN: `render_diff` doesn't return the rendered ANSI/Markdown body

This was caught by the observability reviewer reading `src/signalforge/diff/engine.py` carefully. **`DiffReport` carries `unified_diff: str` but NOT the rendered ANSI/Markdown text.** The renderer's output is only written when `output_path` is supplied (atomic-write seam); the renderer ABC and concretes are private (`_renderers`, per DEC-004 of #8).

So under SQ-02=B ("default = stdout + JSON sidecar"), the CLI's three options are:

- **A.** Write to a tmpfile via `output_path=tmpfile`, then read it back and print to stdout. Adds an O(diff-size) file round-trip per run.
- **B.** Promote the `Renderer` ABC and concretes to the public surface (`signalforge.diff` re-exports them). Diff-layer DEC-004 explicitly stated they're private.
- **C.** Add a tiny public helper to the diff layer: `render_to_text(report, *, config, project_dir) -> str` that internally dispatches the same renderers. Keeps DEC-004 ("Renderers are private") intact; gives the CLI a one-liner.

My take: **C**. This is a small, principled extension to the diff layer's public surface. Worth a tiny story inside the CLI ticket: "Add `signalforge.diff.render_to_text` helper." Mirrors the way `signalforge.diff.render_diff` is the public orchestrator for the disk-write side.

### AR-O3 — CONCERN: untyped exception fallback

The clauditor "no traceback ever leaks" rule + the four-tier exit-code rule together require: every exception caught at the `cmd_<name>` boundary maps to {0, 1, 2, 3}. **What happens to a bug that raises an untyped `Exception`?** Plan must answer.

**Options:**
- **A.** Bare `except Exception` at the top of `cmd_<name>` → exit 1 ("load" — system not in a coherent state) with `ERROR: an unexpected error occurred. Please file an issue.` + remediation hint. Strip traceback. Belt-and-braces via `sys.excepthook`.
- **B.** Same, but exit 99 ("internal error") — explicitly a fifth category. clauditor's rule says NO ("do not invent a fifth category").
- **C.** Same, but `--verbose` shows the traceback while non-verbose hides it.

My take: **A + a `--verbose` flag that elevates the panic-path traceback to stderr** (and bumps `_LOGGER` to DEBUG). Best-of-both: clean default for users, debugging path for maintainers. Add `sys.excepthook` as belt-and-braces so even an exception inside an `except` clause doesn't leak.

### AR-O4 — CONCERN: `LLMOutputAnchorContractError.violations` formatting

The whole-draft fail-loud `LLMOutputAnchorContractError` from #5 carries `violations: tuple[str, ...]`. clauditor's exit-2 stderr shape is "header + bullets". Two ways to feed the violations through:

- **A.** Override `__str__` on `LLMOutputAnchorContractError` so `str(exc)` already produces the header + bullets. CLI catches and `print(str(exc), file=sys.stderr)`. **Couples the error to the CLI rendering convention.**
- **B.** CLI knows about `hasattr(exc, "violations")` and formats specifically. Keeps the error generic; couples the CLI to draft-layer knowledge.
- **C.** Add a public `signalforge.cli._helpers.format_error_to_stderr(exc) -> str` that knows about every error shape that needs special multi-violation handling. Single seam in the CLI; no per-error coupling in the layer.

My take: **C**. Mirrors the "escape at the sink" pattern from the diff renderer (DEC-008). The CLI is the sink; it owns the formatting.

### AR-T1 — CONCERN: subprocess smoke test missing as belt-and-braces

In-process `main(argv)` testing is the right primary pattern (clauditor's choice, fast, deterministic). **But** it doesn't catch:

- `[project.scripts]` wiring in `pyproject.toml` getting deleted/typoed.
- `pip install -e ".[dev]"` followed by `which signalforge` returning nothing.
- Console-script wrapper differences after a wheel rebuild.

**Recommendation:** add one gated subprocess test:

```python
@pytest.mark.cli_subprocess
def test_signalforge_version_via_subprocess():
    result = subprocess.run(["signalforge", "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.startswith("signalforge ")
```

Add the `cli_subprocess` marker to `pyproject.toml` `[tool.pytest.ini_options].markers`, default to `-m 'not cli_subprocess and not bigquery and not anthropic'`. Maintainers run it before declaring a PR ready (mirrors the `bigquery` integration-test gate from `warehouse-adapters.md`).

### AR-T2 — PASS+: AST-scan for exception → exit-code mapping completeness

Strongly recommended over a hand-maintained parametrize list. Pattern is already established (six AST scans in `tests/test_audit_completeness.py`; logger grep gate; LLMRequest-construction scan). The seventh scan would walk every `class *Error(Exception)` in `src/signalforge/*/errors.py` and assert every class appears in the CLI's mapping table. **A new typed exception lands without a tier mapping → test fails loud.**

This makes the four-tier taxonomy a contract, not a guideline. Mirrors `tests/llm/test_logger_grep_gate.py`'s "scan a directory; reject any hit" shape.

### AR-S3 — BLOCKER (or near-blocker): SQ-03=C surface-area pushback

The dedicated reviewer for SQ-03 examined the cost-vs-value of shipping a `cli:` config block and **pushed back hard**. Summary of the case:

**Cost of `cli.fail_on_grade_below_threshold` (and any other `cli:` knobs):**

- New `CliConfig` Pydantic model (`extra="forbid"`).
- New `_CliConfigFile` outer wrapper (`extra="ignore"` for sibling-stage tolerance).
- New `load_cli_config(project_dir, path=None)` loader matching every other stage.
- New drift detector test in `tests/cli/test_drift_detector.py` with `StrictCliConfig` mirror.
- New fixture file `tests/fixtures/cli/cli_config_v1.yaml`.
- New entry in `.claude/rules/cli-layer.md` documenting the namespace.
- Maintenance: every future signalforge.yml schema test must round-trip the `cli:` block too.

**Value:**

- Operator can set `fail_on_grade_below_threshold=True` in the project's `signalforge.yml` to make the CLI exit 2 on threshold failure.

**Equivalent shell wrapper (zero new code):**

```bash
signalforge generate <model> && jq -e '.passed' < .signalforge/grade.json > /dev/null
```

**Reviewer's recommended HYBRID** (worth strong consideration):

1. **Graduate `GradeConfig.fail_on_below_threshold` in the grade layer.** It stops being a v0.2 reservation; the grade engine raises a new `GradeBelowThresholdError` when `passed=False` and `fail_on_below_threshold=True`.
2. **The CLI catches `GradeBelowThresholdError` → exit 2.** No new CLI knob; the CLI just respects the existing grade-config field.
3. **Skip the `cli:` config block entirely in v0.1.**
4. **`docs/cli-ops.md` documents** the `grade.fail_on_below_threshold` mechanism is what controls the CLI's exit code.
5. **Doc cascade required:** update `.claude/rules/grade-layer.md` to remove the v0.2 reservation language; update `GradeConfig.fail_on_below_threshold`'s docstring; add `GradeBelowThresholdError` to `signalforge.grade`'s public surface; tests for the new raise path inside `tests/grade/test_engine.py`.

**My take:** the reviewer is right. SQ-03=C as originally described would ship surface area whose entire job is to duplicate a knob the grade layer was always going to graduate eventually. Doing it as a grade-layer graduation produces ONE knob (instead of two) and lands the v0.2 reservation properly. The CLI ticket's "wire up exit-2-on-threshold-failure" is then a 5-line catch-and-exit rather than a 200-line config-loader-with-drift-detector.

**Recommend overriding SQ-03 to: ship the GRADE-LAYER graduation (Option A), skip the `cli:` config block entirely.** Asking for confirmation before locking in.

### AR-S4 — PASS: `--format` flag wires DEC-021 of #8

`DiffConfig.render_kind: Literal["ansi", "markdown", "json"]` is already exported. The diff-renderer rule explicitly cited #9 as the implementer for the `--format` flag. The CLI ticket should add it. One-line argparse + one config field passthrough. Add to scope.

### AR-D1 — PASS: documentation cascade

`docs/cli-ops.md` is a v0.1 deliverable (every other stage has one). README quick-start gets a CLI usage section. Existing docs (`docs/grade-ops.md`, `docs/diff-ops.md`) get one-line nods to "the CLI exits 2 on `grade.fail_on_below_threshold=True` (graduated in #9)" and "the CLI's `--format` flag drives `render_kind` (graduated in #9)" respectively. New rule file `.claude/rules/cli-layer.md` with the four-tier exit-code taxonomy as one section.

---

### Summary: blockers to resolve in refinement (Phase 3)

1. **AR-B1.** Confirm: add the 8 missing exception exports to `signalforge.draft.__init__` and `signalforge.llm.__init__` (DraftConfigNotFoundError, DraftConfigInvalidError, LLMOutputJSONError, LLMOutputValidationError, LLMOutputAnchorContractError, PromptEnvelopeBreachError, LLMResponseAuditRecordTooLargeError, LLMResponseAuditWriteError, LLMResponseFormatError). One small story; keeps the public surface aligned with what the CLI catches.
2. **AR-B2.** Confirm tier for `TableNotFoundError` (recommend: 2 — input). Confirm the 11 inherit-from-base mappings are explicit in the new rule file.
3. **AR-S3.** Confirm: revert SQ-03=C to "graduate the grade layer's `fail_on_below_threshold` knob, skip the `cli:` config block in v0.1." Triggers a documentation cascade in `.claude/rules/grade-layer.md` and `docs/grade-ops.md`. Reduces v0.1 surface area significantly.

### Concerns to resolve in refinement

4. **AR-O1.** Decide: progress-line shape (recommend C: stderr lines, TTY-detected, suppressible by `--quiet`).
5. **AR-O2.** Decide: how the CLI gets rendered text from the diff layer (recommend C: add public `signalforge.diff.render_to_text(report, *, config, project_dir) -> str` helper).
6. **AR-O3.** Decide: untyped exception fallback (recommend A + `--verbose`: exit 1, no traceback by default, traceback under `--verbose`, `sys.excepthook` belt-and-braces).
7. **AR-O4.** Decide: where multi-violation formatting lives (recommend C: `cli/_helpers.py::format_error_to_stderr`).
8. **AR-T1.** Decide: add subprocess-gated smoke test (recommend yes; gate behind `@pytest.mark.cli_subprocess`).
9. **AR-T2.** Confirm: add 7th AST scan in `tests/test_audit_completeness.py` enforcing exception → exit-code mapping completeness.

---

## Refinement Log

### Session 1 — 2026-05-03

User confirmed all four blocker decisions and accepted reviewer recommendations across the six concerns. Captured decisions:

| ID | Source | Decision | Rationale |
|---|---|---|---|
| **DEC-001** | SQ-01=D | Project root: walk up from cwd to find `dbt_project.yml`; `--project-dir <PATH>` overrides; emit DEBUG log noting which dir was discovered | Mirrors how `git` finds `.git` and how every dbt CLI walks up; `--project-dir` is the cheap escape hatch for monorepos |
| **DEC-002** | SQ-02=B | Default print-diff = stdout (rendered) + JSON sidecar at `<project_dir>/.signalforge/diff.json` (`render_diff(write_sidecar=True)` default preserved) | Matches the diff layer's default-on sidecar; sidecar is fail-closed and atomic so it's not a side-effect risk; `--write` then specifically gates writing the proposed `schema.yml` to disk |
| **DEC-003** | SQ-03=C-superseded-by-AR-S3 | Reverted: NO `cli:` config block in v0.1. Threshold-fail behaviour lives in the grade layer (DEC-011 below) | Reviewer's hybrid: the v0.2 reservation `GradeConfig.fail_on_below_threshold` was always going to graduate as a grade-layer wiring; doing it via a `cli:` block duplicates the eventual real knob and ships ~200 lines of config plumbing whose value is one shell pipe away |
| **DEC-004** | SQ-04=A | `--min-score N` is reporting-only: feeds `GradeConfig.min_mean_score` which drives the diff renderer's `flagged` tier. Does NOT affect exit code by itself; threshold-fail exit is governed by `grade.fail_on_below_threshold` (DEC-011) | Two surfaces with two jobs: reporting flag for the threshold; config knob for the exit-code consequence. Operator can have one without the other |
| **DEC-005** | SQ-05=A | No `--no-grade` flag in v0.1 | Architectural Commitment #2 (evaluation in the loop) is non-negotiable. `render_diff(grading_report=None)` already supports the v0.2 escape hatch if user feedback wants it |
| **DEC-006** | SQ-06=A | `lint` is config-only (no warehouse, no LLM, no network); sub-second target | Auth/connectivity belongs in a future `signalforge doctor` command (v0.2). Lint should be the cheap pre-flight check operators can run on every save |
| **DEC-007** | SQ-07=A | Both `--manifest <PATH>` and `--profiles-dir <PATH>` flags. Both flow through `canonicalise_path(raw_path, project_dir)` for symlink safety | Matches dbt's `--profiles-dir` ergonomics; trivial to pass through to existing loaders |
| **DEC-008** | SQ-08=A | Stderr shape: exit 1 and 3 → single `ERROR: <msg>` line; exit 2 → header line + `  - <msg>` bullets per error | CI parsers key on the shape; mirrors clauditor's contract verbatim |
| **DEC-009** | SQ-09=B | Distil rules into a new `.claude/rules/cli-layer.md`; the four-tier exit-code taxonomy is one section inside; cite clauditor's source rule in See-Also footer | Matches the project's one-rule-per-layer pattern; portable into other Anthropic-CLI projects later |
| **DEC-010** | SQ-10=C | `--dry-run` runs the FULL pipeline (LLM + warehouse + grade), prints the diff to stdout, writes nothing (no `schema.yml`, no JSON sidecar). `--dry-run` and `--write` are mutually exclusive at argparse level | Most useful in practice; `--dry-run` is "show me what would happen"; `--write` is "do it"; combination undefined |
| **DEC-011** | AR-S3 | **Graduate** `GradeConfig.fail_on_below_threshold` from v0.2 reservation to v0.1 wiring. Add new `GradeBelowThresholdError` to `signalforge.grade` public surface; `grade_artifacts(...)` raises it when `config.fail_on_below_threshold=True` AND `report.passed=False`. The CLI catches → exits 2. Doc cascade: `.claude/rules/grade-layer.md` v0.2-reservation block updated; `GradeConfig.fail_on_below_threshold` docstring updated; `docs/grade-ops.md` updated. NO `cli:` config block in v0.1 | Reviewer's hybrid; one knob with one owner (the grade layer); CLI is a thin catch-and-exit |
| **DEC-012** | AR-B2 | `TableNotFoundError` → tier 2 (input). The model's table reference is wrong, not the warehouse | Keeps the boundary clean: warehouse-connectivity = exit 3; model-mistake = exit 2 |
| **DEC-013** | AR-B1 | Re-export 8 typed exceptions through their stage `__init__.py` so the CLI can import them from the public surface: `DraftConfigNotFoundError`, `DraftConfigInvalidError`, `LLMOutputJSONError`, `LLMOutputValidationError`, `LLMOutputAnchorContractError`, `PromptEnvelopeBreachError`, `LLMResponseAuditRecordTooLargeError`, `LLMResponseAuditWriteError`, `LLMResponseFormatError` | The CLI tier-maps these classes, so they belong on the public surface. One-line fix per stage; small standalone story |
| **DEC-014** | AR-O1 | Stderr progress lines (`[N/M] <stage>: <verb>...`), TTY-detected, suppressed under `--quiet`. No new dependency (no spinners, no live counters) | Mirrors `git`'s `Cloning into 'repo'...` UX; one `print(..., file=sys.stderr)` per stage entry |
| **DEC-015** | AR-O2 | Add public helper `signalforge.diff.render_to_text(report, *, config, project_dir) -> str` to the diff layer. Internally dispatches the same private `_renderers` machinery as `render_diff(output_path=...)`. Keeps `_renderers` private (DEC-004 of #8 preserved) while giving the CLI the rendered text it needs for stdout under DEC-002 | Cheaper than promoting the renderer ABC to public; smaller surface; one-line CLI usage |
| **DEC-016** | AR-O3 | Untyped `Exception` at the `cmd_<name>` boundary → exit 1 with `ERROR: an unexpected error occurred. <msg>` on stderr, no traceback. `--verbose` flag elevates the panic-path traceback to stderr (and bumps `_LOGGER` to DEBUG). `sys.excepthook` belt-and-braces strips tracebacks even if an exception escapes the main `try/except` | clauditor's "no traceback ever leaks" rule + the four-tier-no-fifth-category rule, reconciled. `--verbose` is the maintainer escape hatch |
| **DEC-017** | AR-O4 | Multi-violation formatting (e.g., `LLMOutputAnchorContractError.violations`) lives in `cli/_helpers.py::format_error_to_stderr(exc) -> str`. CLI is the sink; CLI owns the formatting. No special-case `__str__` overrides on the error classes | "Escape at the sink" pattern from diff-renderer DEC-008 generalised |
| **DEC-018** | AR-T1 | Add ONE subprocess smoke test gated behind `@pytest.mark.cli_subprocess`. Test: `subprocess.run(["signalforge", "--version"])` returns 0 and stdout starts with `"signalforge "`. Marker registered in `pyproject.toml`; default pytest run excludes it (mirrors the `bigquery` integration-test gate) | Belt-and-braces for the `[project.scripts]` wiring; in-process tests can't catch console-script breakage |
| **DEC-019** | AR-T2 | Add 7th AST scan in `tests/test_audit_completeness.py` enforcing every `class *Error(Exception):` under `src/signalforge/*/errors.py` is referenced by the CLI's exception → exit-code mapping table. Parametrized companion test in `tests/cli/test_exit_codes.py` raises each error at the CLI boundary and asserts the right exit code + stderr shape | Makes the four-tier taxonomy a contract, not a guideline. Mirrors the existing AST-scan pattern (six scans as of #7) |
| **DEC-020** | AR-S4 | `--format {ansi,markdown,json}` flag wires `DiffConfig.render_kind` (DEC-021 of #8 graduation). Default = `ansi` (matches `DiffConfig.render_kind` default). `json` format prints the JSON sidecar's contents to stdout instead of the rendered diff (the sidecar is still written by default per DEC-002) | Diff layer was already ready; CLI just adds the argparse flag and a config passthrough |

### Session 2 — 2026-05-03 (post-publish review refinements)

External plan-review pass against `plans/super/9-cli-entrypoint.md` surfaced seven concrete issues (4 substantive, 3 nice-to-haves). All seven verified against the actual codebase before resolving — see notes in each row. Resolutions captured as DEC-021 through DEC-027 and folded into US-001, US-002, US-005, US-007, and US-008.

| ID | Issue | Resolution | Verified against |
|---|---|---|---|
| **DEC-021** | US-002 raise-vs-sidecar ordering ambiguous | The `GradeBelowThresholdError` raise lands AFTER `write_grading_report(...)` returns and BEFORE `return report`. New test `test_grade_below_threshold_writes_sidecar_before_raising` pins the invariant. Operator gets a complete `grade.json` for diagnosis even on threshold-fail | `src/signalforge/grade/engine.py:889-931` — current flow is build → write_sidecar → log → return; insertion point is between log and return |
| **DEC-022** | `render_to_text` referenced a nonexistent `_pick_renderer` and a nonexistent `report.config_used` | Helper uses the existing `signalforge.diff.engine._build_renderer(config or DiffConfig(), project_dir=project_dir)` dispatcher. Caller supplies config explicitly OR accepts `DiffConfig()` defaults. Helper does NOT introspect the report; `DiffReport` carries no config | `src/signalforge/diff/models.py:144` (no `config_used`), `src/signalforge/diff/engine.py:600` (`_build_renderer` is the actual symbol) |
| **DEC-023** | `--no-color` plan claimed `force_color=False` field that doesn't exist | `--no-color` sets `os.environ["NO_COLOR"] = "1"` for the call; `DiffConfig.respect_no_color_env=True` (default) honours it via the diff layer's existing precedence chain (DEC-021 of #8). The unconditional ANSI strip on user-content fields is unaffected — that's the security boundary, not a UX knob | `src/signalforge/diff/config.py:139` — only `respect_no_color_env: bool = True` exists; no `force_color` field |
| **DEC-024** | AST-scan target unclear about exclude list | Scan walks `src/signalforge/*/errors.py` (verified — every typed exception in the project lives in an `errors.py` module). Explicit exclude list is the 9 abstract base classes: `ManifestError`, `WarehouseError`, `SafetyError`, `LLMError`, `DraftError`, `PruneError`, `GradeError`, `DiffError`, `CliError`. The exclude constant is the seam for v0.2 if a new abstract intermediate lands | `grep -rln "^class.*Error" src/signalforge/ --include="*.py"` returns exactly the 8 stage `errors.py` files |
| **DEC-025** | US-005 missing pipeline-order test | Add `test_generate_calls_stages_in_documented_order` — `MagicMock` parent with `mock_calls` ordering assertion against the documented `safety → draft → prune → grade → diff` shape from CLAUDE.md "Pipeline shape" | CLAUDE.md `## Pipeline shape (per README)` |
| **DEC-026** | DEC-014 progress lines hardcoded duration hints ("30s", "few minutes") | Drop the predictions; replace with live values (`(model claude-sonnet-4-6)`, `(32 calls)`) plus a paired post-hoc `done in 4.2s` line at stage exit. Real measurement after the fact, not stale estimate | n/a — pure UX call |
| **DEC-027** | DEC-001 `--project-dir` interaction with walk-up under-specified | When `--project-dir <PATH>` is supplied the CLI does NOT walk up from `<PATH>` — the override is an absolute assertion and exits 1 if `<PATH>/dbt_project.yml` is missing. Walk-up is only the unflagged default. Two new tests pin both arms | n/a — explicit choice |

### Session 3 — 2026-05-04 (Quality Gate corrections — US-011)

QG passes 1–3 surfaced two genuine defects across the implementation. Both fixed in standalone commits with regression tests; no follow-up work needed.

| ID | Pass | Issue | Resolution |
|---|---|---|---|
| **QG-001** | Pass 1 | `--profiles-dir` was routed through `canonicalise_user_path`, which contains paths inside `project_dir`. The dbt convention places `profiles.yml` at `~/.dbt/` (intentionally outside the project tree), so every realistic `--profiles-dir` value would have exited 1 with `CliPathError`. The existing test masked the bug by placing the profiles dir inside the project | Bypass the project-dir containment gate for `--profiles-dir`; apply `expanduser` + `resolve(strict=False)` for symlink-loop safety; the warehouse loader retains its own existence/shape gate on the resolved file. New test `test_generate_profiles_dir_accepts_out_of_tree_path` pins the corrected behaviour |
| **QG-002** | Pass 3 | DEC-004 / DEC-002 / US-006 all claimed `--min-score` "drives the diff renderer's `flagged` tier", but the code only mutates `grade_config.min_mean_score`, an **aggregate-verdict** threshold consumed by `GradingReport.passed` and (when `fail_on_below_threshold=true`) by `GradeBelowThresholdError`. The diff renderer's `flagged` tier flips on per-criterion `GradingResult.passed`, set verbatim by the LLM judge — never reads `min_mean_score`. The flag works as implemented; the documented contract was wrong | DEC-004 corrected: `--min-score` is a reporting-only override of the aggregate-verdict threshold, not a driver of the per-criterion `flagged` classification. Help text, module docstring, ops doc (two locations), test name (`test_generate_min_score_overrides_aggregate_threshold`), and test docstring updated to match the actual code semantics. Adding a per-criterion threshold override would be a v0.2 feature, not a doc fix |

### Implications synthesised across decisions

- **Public-surface plumbing in scope.** DEC-013 + DEC-015 add 9 public symbols (8 exception re-exports + `render_to_text`). One small story up front so the CLI's imports work cleanly.
- **Grade-layer graduation in scope.** DEC-011 is real grade-engine work, not just CLI work. One dedicated story; lands BEFORE the CLI's `generate` story.
- **No `cli:` config block.** Drops `CliConfig` Pydantic model + `_CliConfigFile` + `load_cli_config` + `tests/cli/test_drift_detector.py` + `tests/fixtures/cli/cli_config_v1.yaml` from scope. Roughly **−250 lines of code and tests**.
- **`docs/cli-ops.md` and `docs/grade-ops.md` and `docs/diff-ops.md`** all touched. CLI gets a new ops doc; grade gets the v0.2-reservation-graduation update; diff gets the `render_to_text` documentation.
- **Three rule files touched.** `.claude/rules/cli-layer.md` (new); `.claude/rules/grade-layer.md` (graduation update); `.claude/rules/diff-renderer.md` (note that `render_kind` and the rendered-text helper are now wired by #9).

---

## Detailed Breakdown

### Story map

13 stories total: 10 implementation, 1 Quality Gate, 1 Patterns & Memory, plus a one-line Logger Grep Gate extension folded into US-003.

```
US-001 (public-surface plumbing) ──┐
US-002 (grade graduation)  ────────┤
                                   ├──→ US-003 (CLI scaffold + version + logger gate ext)
                                   │       │
                                   │       ├──→ US-004 (cli/lint.py)
                                   │       └──→ US-005 (cli/generate.py — core orchestration)
                                   │             │
                                   │             ├──→ US-006 (generate: mode/threshold/output flags)
                                   │             └──→ US-007 (generate: observability — progress, --quiet, --verbose, --no-color)
                                   │
                                   ├──→ US-008 (7th AST scan + parametrized exit-code tests)
                                   ├──→ US-009 (subprocess-gated smoke test)
                                   ├──→ US-010 (documentation: cli-ops.md + README + cli-layer.md rule)
                                   ├──→ US-011 (Quality Gate)
                                   └──→ US-012 (Patterns & Memory)
```

`tests/cli/test_smoke.py` and `tests/cli/test_main.py` are deliverables of US-003; `tests/cli/test_lint.py` ships with US-004; `tests/cli/test_generate.py` ships across US-005 / US-006 / US-007 (one growing test file).

---

### US-001 — Public-surface plumbing (8 exception re-exports + `render_to_text` helper)

**Description.** Tighten the public surface so the CLI can import everything it needs without sneaking through private modules. Re-exports 8 typed exceptions (DEC-013) and adds the new `signalforge.diff.render_to_text(report, *, config, project_dir) -> str` public helper (DEC-015).

**Traces to:** DEC-013, DEC-015, DEC-022.

**Acceptance criteria:**

- `from signalforge.draft import DraftConfigNotFoundError, DraftConfigInvalidError, LLMOutputJSONError, LLMOutputValidationError, LLMOutputAnchorContractError, PromptEnvelopeBreachError, LLMResponseAuditRecordTooLargeError, LLMResponseAuditWriteError` works.
- `from signalforge.llm import LLMResponseFormatError` works.
- `from signalforge.diff import render_to_text` works; signature `render_to_text(report: DiffReport, *, config: DiffConfig | None = None, project_dir: Path | None = None) -> str`. Returns the same text that `render_diff(..., output_path=...)` would have written. Internally calls the existing `signalforge.diff.engine._build_renderer(config or DiffConfig(), project_dir=project_dir)` then `renderer.render(report)`. **`DiffReport` does not carry the config used by the original `render_diff` call** (verified against `src/signalforge/diff/models.py:144` — the model has `unified_diff`, hashes, counts, but no `config_used` field), so the caller must supply config explicitly OR accept `DiffConfig()` defaults; the helper does NOT reach into the report. `MarkdownRenderer` requires `project_dir` — when `render_kind="markdown"` and `project_dir is None`, fall through to the renderer's existing handling (passes `None` through; the renderer already tolerates it).
- `tests/draft/test_public_api.py` (or equivalent) is updated to assert all 8 new public-surface names; `tests/llm/test_public_api.py` updated for `LLMResponseFormatError`; `tests/diff/test_public_api.py` updated for `render_to_text`.
- New unit test `tests/diff/test_render_to_text.py` asserts: byte-equal output to `render_diff(output_path=tmpfile).read_text()` for ANSI / Markdown / JSON renderers.
- `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest` passes.

**Done when:** all 9 names are importable from public surfaces; `test_render_to_text` passes; full validation suite green.

**Files:**

- `src/signalforge/draft/__init__.py` — add 7 re-exports.
- `src/signalforge/llm/__init__.py` — add 1 re-export.
- `src/signalforge/diff/__init__.py` — add `render_to_text` re-export.
- `src/signalforge/diff/engine.py` — add public `render_to_text` function (small wrapper around the existing `_build_renderer` + `renderer.render(report)` pair).
- `tests/draft/test_public_api.py` — extend.
- `tests/llm/test_public_api.py` — extend (or create if absent).
- `tests/diff/test_public_api.py` — extend.
- `tests/diff/test_render_to_text.py` — new.

**Depends on:** none.

**TDD:**

1. Write a test that imports each of the 9 new public names from its public surface; assert the import succeeds and the symbol is the right type. Test fails (ImportError).
2. Add the re-exports. Test passes.
3. Write `test_render_to_text_byte_equal_to_render_diff_file_output` — calls both, asserts byte equality across all three renderer kinds. Test fails (function doesn't exist).
4. Implement `render_to_text`. Test passes.

---

### US-002 — Grade-layer graduation: `GradeBelowThresholdError`

**Description.** Graduate `GradeConfig.fail_on_below_threshold` from v0.2 reservation no-op to v0.1 wiring. Add `GradeBelowThresholdError`; raise inside `grade_artifacts(...)` when `config.fail_on_below_threshold=True` AND the produced `GradingReport.passed=False`. Update doc and rule cascade.

**Traces to:** DEC-011, DEC-021.

**Acceptance criteria:**

- New `GradeBelowThresholdError(GradeError)` defined in `src/signalforge/grade/errors.py`. Carries `pass_rate: float`, `mean_score: float`, `min_pass_rate: float`, `min_mean_score: float`, `aggregate_complete: bool` as fields. Renders a remediation that names the failing thresholds.
- `GradeBelowThresholdError` re-exported from `signalforge.grade.__init__` (in the `GradeError` family list).
- `grade_artifacts(...)` checks `config.fail_on_below_threshold and not report.passed` AFTER the report is built AND the sidecar JSON is written, but BEFORE the function returns. **Order is load-bearing** — verified against `src/signalforge/grade/engine.py:889-931`, the existing flow is `build report` → `write_grading_report(...)` → `_LOGGER.info(...)` → `return report`. The new raise lands between the sidecar write and the return: a threshold-fail run leaves both a complete `grade.jsonl` (per-pair audit) AND a complete `grade.json` (sidecar) on disk so the operator can diagnose *why* the run fell below threshold. Raising before the sidecar would defeat that durable hand-off.
- `GradeConfig.fail_on_below_threshold` docstring updated: removes "v0.1 no-op" language; explains the raise behaviour; cites #9 as the consumer.
- `.claude/rules/grade-layer.md` — `## v0.2 reservations` block updated. The entry for `fail_on_below_threshold` is rewritten as "Graduated in #9 — raises `GradeBelowThresholdError`." Remaining v0.2 reservations (`GradeBudgetExceededError` is already raised; `GradeThresholds` is still a forward-compat shape) stay listed.
- `docs/grade-ops.md` — adds a section "Threshold-fail behaviour" describing the config knob, the raise, and the CLI's exit-2 mapping (forward reference to docs/cli-ops.md).
- New tests in `tests/grade/test_engine.py`:
  - `test_fail_on_below_threshold_true_passing` — `passed=True`, `fail_on_below_threshold=True` → returns report cleanly.
  - `test_fail_on_below_threshold_true_failing_pass_rate` — `pass_rate < min_pass_rate`, `fail_on_below_threshold=True` → raises.
  - `test_fail_on_below_threshold_true_failing_mean_score` — `mean_score < min_mean_score`, `fail_on_below_threshold=True` → raises.
  - `test_fail_on_below_threshold_false_failing` — `passed=False`, `fail_on_below_threshold=False` → returns report (default behaviour preserved).
  - `test_grade_below_threshold_error_carries_aggregate_complete_flag` — partial-aggregate (graceful-degrade) report still raises with `aggregate_complete=False` correctly populated.
  - `test_grade_below_threshold_writes_sidecar_before_raising` — invokes `grade_artifacts` with `fail_on_below_threshold=True` against a failing report; catches the raise; asserts the sidecar JSON exists at `resolved_sidecar_path` and parses to a `GradingReport` whose `passed=False`. Pins the load-bearing ordering invariant.
- The grade-layer drift detector (`tests/grade/test_drift_detector.py`) is already extra-forbid for `GradeConfig`; existing behaviour continues to pass.
- Validation suite green.

**Done when:** new error class lands on the public surface, four new test cases pass, doc/rule cascade complete, validation suite green.

**Files:**

- `src/signalforge/grade/errors.py` — add `GradeBelowThresholdError`.
- `src/signalforge/grade/__init__.py` — re-export.
- `src/signalforge/grade/engine.py` — wire the raise after `grade_artifacts(...)` builds the report.
- `src/signalforge/grade/config.py` — update `fail_on_below_threshold` docstring.
- `.claude/rules/grade-layer.md` — update v0.2-reservations block.
- `docs/grade-ops.md` — add threshold-fail section.
- `tests/grade/test_engine.py` — five new tests.

**Depends on:** none (US-001 is independent).

**TDD:**

1. Write `test_fail_on_below_threshold_true_failing_pass_rate` — expects the raise. Test fails (currently a no-op; report returned).
2. Implement the raise in `grade_artifacts`. Test passes.
3. Repeat for the other four test cases; iterate until all five pass.
4. Update docstring + rule + ops doc.

---

### US-003 — CLI scaffold + `version` subcommand + logger grep-gate extension

**Description.** Lay the CLI subpackage foundation. Adds `[project.scripts]` entry to `pyproject.toml`. Creates `src/signalforge/cli/` with `__init__.py` (top-level `main(argv)`), `_helpers.py` (path canonicalise wrapper, `format_error_to_stderr`, `map_exception_to_exit_code`, `setup_logging`, `_safe_excepthook`), and `version.py`. Wires `--version` flag in the top-level parser. Smoke test for `signalforge --version` and `signalforge --help`. Extends `tests/llm/test_logger_grep_gate.py` to scan the 6th directory.

**Traces to:** DEC-007 (paths via canonicalise wrapper), DEC-008 (stderr shape helpers), DEC-013 (imports the re-exports from US-001), DEC-016 (panic-path; `sys.excepthook`), DEC-017 (`format_error_to_stderr` seam).

**Acceptance criteria:**

- `pyproject.toml` carries `[project.scripts] signalforge = "signalforge.cli:main"`.
- `pip install -e ".[dev]"` followed by `signalforge --version` prints `signalforge 0.1.0.dev0` (matches `signalforge.__version__` PEP 440 shape) and exits 0.
- `signalforge` (no args) prints help and exits 2 (argparse's standard for missing subcommand).
- `signalforge --help` prints the top-level help (description, subcommand list) and exits 0.
- `signalforge version` (subcommand) prints the same string as `--version` and exits 0. Both surfaces share one implementation.
- `cli/__init__.py` defines `def main(argv: list[str] | None = None) -> int`. Supports being called both as a function (in-process tests) and via the console-script entry point.
- `cli/_helpers.py` exports:
  - `canonicalise_user_path(raw: str | Path | None, project_dir: Path) -> Path | None` — wraps `signalforge.warehouse._path_safety.canonicalise_path`; returns `None` when input is `None`; raises `CliPathError` (new) on containment failure.
  - `setup_logging(verbose: bool, quiet: bool) -> None` — calls `logging.basicConfig(...)` once; level = `DEBUG` if verbose else `WARNING` if quiet else `INFO`; `StreamHandler(sys.stderr)`; format `"%(asctime)s [%(levelname)s] %(name)s: %(message)s"`.
  - `format_error_to_stderr(exc: Exception) -> str` — single source of truth for stderr formatting: knows the multi-violation shape for `LLMOutputAnchorContractError` and the single-line shape for everything else; renders `↳ Remediation:` line for typed errors that carry one.
  - `map_exception_to_exit_code(exc: Exception) -> int` — single mapping table of typed exception → tier (1, 2, or 3). Untyped `Exception` → 1 with the panic-path remediation message. **The mapping table is the load-bearing artefact for DEC-019's AST scan.**
  - `_safe_excepthook(exc_type, exc_value, traceback) -> None` — strips tracebacks from anything that escapes the main try/except; installed in `main()` if `--verbose` is NOT set.
  - New typed errors `CliError`, `CliPathError`, `CliInputError` exported via `cli/__init__.py`.
- Logger uses `_LOGGER = logging.getLogger("signalforge.cli")`; every call uses `%s` + `json.dumps(...)` for any user-controlled string.
- `tests/llm/test_logger_grep_gate.py` extends to scan `src/signalforge/cli/` (6th dir).
- New test files:
  - `tests/cli/test_smoke.py` — `--version`, `--help`, `version` subcommand. No traceback in stderr.
  - `tests/cli/test_main.py` — unknown command, no command, top-level dispatch.
- All tests use `main(argv)` + `capsys`. No subprocess (US-009 adds the gated subprocess test).
- Validation suite green.

**Done when:** the `signalforge` console script exists and exits cleanly on `--version` / `--help` / `version` / unknown args; helpers are usable by US-004 onwards; logger grep gate covers the 6th dir.

**Files:**

- `pyproject.toml` — `[project.scripts]` entry.
- `src/signalforge/cli/__init__.py` — `main(argv)`, top-level argparse parser, subparsers, dispatch.
- `src/signalforge/cli/_helpers.py` — five helpers + three new error classes.
- `src/signalforge/cli/version.py` — `add_parser` + `cmd_version`.
- `src/signalforge/cli/errors.py` — typed CLI errors (`CliError`, `CliPathError`, `CliInputError`).
- `tests/cli/test_smoke.py` — smoke floor.
- `tests/cli/test_main.py` — top-level dispatch tests.
- `tests/llm/test_logger_grep_gate.py` — extend `_DIRS_TO_SCAN` tuple.

**Depends on:** US-001 (some helpers will need to import from the new public surfaces; even if the version subcommand doesn't, the helpers do).

**TDD:**

1. Write `test_signalforge_version_returns_zero_and_prints_pep440` — `main(["--version"])` → 0; capsys stdout starts with `"signalforge "`. Test fails (no `main` yet).
2. Build skeletal `main` that parses `--version` and exits 0. Test passes.
3. Write `test_signalforge_no_args_prints_help_and_exits_two` — `main([])` → 2. Iterate.
4. Write `test_signalforge_unknown_command_exits_two` — `main(["nonexistent"])` → 2.
5. Write `test_signalforge_version_subcommand_matches_flag` — `main(["version"])` and `main(["--version"])` produce the same stdout.
6. Write `test_no_traceback_ever_in_stderr_on_unknown_command` — assert `"Traceback" not in capsys.readouterr().err`.
7. Implement `format_error_to_stderr` against a test that constructs each known exception type and asserts the rendered shape. (May overlap with US-008.)

---

### US-004 — `cli/lint.py` — config-only validator

**Description.** `signalforge lint` validates the five existing config blocks (`safety:`, `llm:`, `prune:`, `grade:`, `diff:`) in `signalforge.yml`. No warehouse, no LLM, no network. Sub-second target.

**Traces to:** DEC-006, DEC-008.

**Acceptance criteria:**

- `signalforge lint` exits 0 when every config block parses cleanly (or when a block is absent, since each loader returns defaults silently per the per-stage convention).
- `signalforge lint` exits 1 (load) when `signalforge.yml` itself is unreadable / invalid YAML.
- `signalforge lint` exits 1 (load) when any config block raises a typed `*ConfigError` / `*ConfigNotFoundError` / `*ConfigInvalidError` — uses DEC-008 stderr shape (single `ERROR: ...` line per block).
- When multiple blocks fail, `lint` reports ALL of them (not short-circuit on first), header + bullets per DEC-008.
- `signalforge lint --help` prints subcommand help and exits 0.
- `signalforge lint --config <PATH>` accepts an explicit config path (not the default `<project_dir>/signalforge.yml`); routes through `canonicalise_user_path` (DEC-007).
- `signalforge lint --project-dir <PATH>` accepts the project root override (DEC-001 walk-up still applies when omitted).
- Stdout is silent on success (git-style); a single line `lint: ok` is acceptable for visual feedback under verbose.
- New tests in `tests/cli/test_lint.py`:
  - happy path (all blocks present and valid) → exit 0
  - happy path (no `signalforge.yml` at all) → exit 0 (each loader returns defaults silently per its DEC)
  - invalid `safety:` block (bad mode) → exit 1, stderr names the block
  - invalid `prune:` block (bad timeout) → exit 1
  - multiple invalid blocks → exit 1, stderr lists all in DEC-008 header+bullets shape
  - `--config /nonexistent.yml` → exit 1 with path-not-found error
  - `--help` → exit 0, non-empty stdout
- Validation suite green.

**Done when:** five config blocks validated in <1 s on a representative project; multi-error reporting works.

**Files:**

- `src/signalforge/cli/lint.py` — `add_parser` + `cmd_lint`.
- `src/signalforge/cli/__init__.py` — register the `lint` subcommand in `main()`.
- `tests/cli/test_lint.py` — new test file.
- `tests/fixtures/cli/` — fixture `signalforge.yml` files for the multi-block scenarios (one valid, one with multiple bad blocks). May reuse existing fixtures if available.

**Depends on:** US-003.

**TDD:**

1. Write `test_lint_returns_zero_on_valid_config_blocks`. Fails (no `lint` subcommand).
2. Add the subcommand; iterate.
3. Write the multi-error reporting test. Confirms DEC-008 stderr shape.

---

### US-005 — `cli/generate.py` — core orchestration

**Description.** The big one. `signalforge generate <model>` wires manifest → safety → draft → prune → grade → diff. Walks up to find `dbt_project.yml` (DEC-001). Accepts `--manifest`, `--profiles-dir`, `--project-dir`. No flag knobs yet — those land in US-006/US-007.

**Traces to:** DEC-001, DEC-007, DEC-008, DEC-011, DEC-013 (catches every typed exception at the boundary), DEC-015 (reads diff output via `render_to_text`), DEC-016, DEC-017, DEC-025 (stage-order test), DEC-027 (project-dir override semantics).

**Acceptance criteria:**

- `signalforge generate <model>` runs the full pipeline against a fixture manifest + `FakeAnthropicClient` + `FakeBigQueryClient`. Exits 0 on the happy path.
- The `<model>` arg accepts both forms: a dbt `unique_id` (`"model.proj.customers"`) and a file path (`"models/marts/customers.sql"`). Routes to `Manifest.get_model(key)`.
- Project root: walks up from cwd to the nearest dir containing `dbt_project.yml`. `--project-dir <PATH>` overrides — when supplied, the CLI **does not walk up from the override**; it treats the override as an absolute assertion that `<PATH>/dbt_project.yml` exists, and exits 1 with remediation if it doesn't. (Walk-up is the convenience for unflagged invocations; the explicit flag is the precise mode.) `_LOGGER.debug` notes the discovered or asserted dir (DEC-001). If neither walk-up nor override finds it, exit 1 with remediation. New tests: `test_generate_project_dir_override_missing_dbt_project_yml_exits_one` and `test_generate_no_project_dir_walks_up_from_subdirectory`.
- `--manifest <PATH>` overrides `<project_dir>/target/manifest.json`. `--profiles-dir <PATH>` overrides the default profiles search. Both flow through `canonicalise_user_path` (DEC-007).
- Pipeline order is the documented `safety → draft → prune → grade → diff`; no skipping (DEC-005), no reordering.
- The diff is rendered to stdout via the new `signalforge.diff.render_to_text(report, ...)` (DEC-015). The JSON sidecar lands at `<project_dir>/.signalforge/diff.json` per `render_diff(write_sidecar=True)` default (DEC-002).
- Every typed exception from any stage is caught at the `cmd_generate` boundary and routed to the right exit code via `_helpers.map_exception_to_exit_code` (DEC-013, DEC-016). Stderr shape per DEC-008. No traceback ever leaks (DEC-016).
- `GradeBelowThresholdError` (from US-002) catches → exit 2 with stderr message that names the failing thresholds (DEC-011).
- `signalforge generate --help` prints subcommand help and exits 0.
- New tests in `tests/cli/test_generate.py`:
  - happy path (fakes wired) → exit 0; stdout contains the rendered diff; sidecar is written.
  - `--project-dir` walk-up (no `dbt_project.yml` found, no override) → exit 1 with remediation.
  - `<model>` accepts `unique_id`; `<model>` accepts file path; `<model>` accepts a path that doesn't exist → exit 2.
  - `GradeBelowThresholdError` raised by `grade_artifacts` → CLI exits 2 with the right stderr message.
  - `LLMRateLimitError` raised by the draft seam → CLI exits 3.
  - `LLMOutputAnchorContractError` with three violations → CLI exits 2 with header + 3 bullets (DEC-008 + DEC-017).
  - panic path: an untyped `Exception` raised by the LLM client → CLI exits 1 with no traceback in stderr (DEC-016).
  - `test_generate_calls_stages_in_documented_order` — patches every stage entry point (`load_safety_config`, `draft_schema`, `prune_tests`, `grade_artifacts`, `render_diff` — or, more precisely, the boundaries the CLI itself crosses) with `unittest.mock.MagicMock`s sharing a single parent mock; calls `main(["generate", "<model>"])`; asserts `parent.mock_calls` matches the documented `safety → draft → prune → grade → diff` ordering. Pins the CLAUDE.md "Pipeline shape" commitment as a contract — a future refactor that reorders stages fails this test loudly.
- Validation suite green.

**Done when:** end-to-end happy path runs against fakes; six representative exit-code scenarios pass; no traceback leaks.

**Files:**

- `src/signalforge/cli/generate.py` — `add_parser` + `cmd_generate`. Probably 200–300 lines including the project-root walk-up and the stage-by-stage orchestration.
- `src/signalforge/cli/__init__.py` — register subcommand.
- `tests/cli/test_generate.py` — new test file.
- `tests/cli/_factories.py` — small helper module for tests to inject `FakeAnthropicClient` and `FakeBigQueryClient` via `unittest.mock.patch` against `signalforge.cli.generate._make_anthropic_client` and `_make_warehouse_adapter`.

**Depends on:** US-001, US-002, US-003.

**TDD:**

1. Write `test_generate_happy_path_against_fakes` — patches both factory functions to return fakes; calls `main(["generate", "<model_unique_id>"])`; asserts exit 0 + stdout contains the rendered diff. Fails (no generate command).
2. Implement skeletal `cmd_generate` that calls every stage in order. Iterate.
3. Write `test_generate_raises_grade_below_threshold_exits_two`. Implements the catch.
4. Write `test_generate_unknown_model_exits_two`. Iterate.
5. Write `test_generate_no_traceback_on_panic`. Implement `_safe_excepthook` if not already done.

---

### US-006 — Generate flags: `--mode`, `--min-score`, `--write`/`--dry-run`, `--format`

**Description.** Layer in the runtime knob flags on top of US-005. `--mode` overrides safety policy; `--min-score` drives the `flagged` tier; `--write` writes proposed `schema.yml` to disk; `--dry-run` is the negative; `--format` selects renderer.

**Traces to:** DEC-002, DEC-004, DEC-010, DEC-020.

**Acceptance criteria:**

- `--mode {schema-only,aggregate-only,sample}` overrides `safety.mode` from `signalforge.yml`. Precedence: flag > `safety.mode` > library default. Invalid value → argparse rejects → exit 2.
- `--min-score N` (`0.0 <= N <= 1.0`) overrides `grade.min_mean_score`. Precedence: flag > `grade.min_mean_score` > library default. Out-of-range → exit 2 with remediation. **Reporting-only:** the diff renderer's `flagged` tier picks up the new threshold; exit code stays 0 unless `grade.fail_on_below_threshold=True` raises (DEC-011 path).
- `--write` writes the proposed `schema.yml` to `<project_dir>/<model_dir>/schema.yml` (or merged into an existing `schema.yml` per the diff renderer's contract). Atomic-write seam.
- `--dry-run` runs the FULL pipeline (LLM + warehouse + grade), prints the diff, writes nothing — neither the `schema.yml` nor the JSON sidecar (per DEC-010 — `--dry-run` overrides DEC-002's default-on sidecar).
- `--write` and `--dry-run` are mutually exclusive at argparse level: passing both → exit 2 with `argparse` error.
- `--format {ansi,markdown,json}` wires `DiffConfig.render_kind` (DEC-020). `json` format: stdout receives the JSON sidecar's contents instead of the rendered ANSI/Markdown diff.
- Tests in `tests/cli/test_generate.py`:
  - `test_generate_mode_overrides_safety_config` for each of the three modes.
  - `test_generate_min_score_drives_flagged_tier` — assert that lowering `--min-score` flips an artifact's tier from `flagged` to `kept` in the rendered output.
  - `test_generate_write_writes_schema_yml`.
  - `test_generate_dry_run_writes_nothing` — asserts neither `schema.yml` nor `.signalforge/diff.json` is written.
  - `test_generate_write_and_dry_run_mutex` — exit 2.
  - `test_generate_format_json_prints_json_to_stdout`.
- Validation suite green.

**Done when:** all five flags work end-to-end; mutex is enforced; `--format json` round-trips against the JSON sidecar fixture.

**Files:**

- `src/signalforge/cli/generate.py` — extend.
- `tests/cli/test_generate.py` — extend.

**Depends on:** US-005.

**TDD:**

1. Write `test_generate_dry_run_writes_nothing`. Confirms negative behaviour.
2. Implement `--dry-run` plumbing.
3. Write `test_generate_write_and_dry_run_mutex`. Add the argparse `add_mutually_exclusive_group(...)`.
4. Iterate per flag.

---

### US-007 — Generate observability: `--quiet`, `--verbose`, `--no-color`, stderr progress lines

**Description.** Layer in the user-experience knobs. Stderr progress lines per stage (DEC-014); `--quiet` suppresses them; `--verbose` raises log level to DEBUG and surfaces panic-path tracebacks (DEC-016); `--no-color` flows through to `DiffConfig.respect_no_color_env`.

**Traces to:** DEC-014, DEC-016, DEC-023 (`--no-color` wiring), DEC-026 (progress-line shape).

**Acceptance criteria:**

- Default (TTY): one stderr line per stage entry. Format `[N/M] <stage>: <verb> <fact>...` where `N` is the stage number (1–5), `M=5` (number of pipeline stages), and `<fact>` is a value derived from the live run — never a hardcoded duration hint (those rot as model speeds and warehouse sizes change). Examples:
  - `[1/5] safety: building LLM request...`
  - `[2/5] draft: calling LLM (model claude-sonnet-4-6)...`
  - `[3/5] prune: running 12 candidate tests against warehouse...`
  - `[4/5] grade: scoring 8 artifacts × 4 criteria (32 calls)...`
  - `[5/5] diff: rendering...`
- Each progress line is paired with a final stderr line at stage exit reporting wall-clock duration: `[N/M] <stage>: done in 4.2s` (truncated to one decimal). This is the post-hoc signal of "did that stage actually take a long time?" — replaces the deleted "this can take Xs" prediction with a real measurement after the fact.
- Default (non-TTY: piped, redirected): no progress lines. Detected via `sys.stderr.isatty()` AND `sys.stdout.isatty()` (both must be terminals to emit progress; either being a pipe disables).
- `--quiet` suppresses progress lines AND raises log level to `WARNING` (only hard errors hit `_LOGGER`).
- `--verbose` does NOT add MORE progress lines (default is already informative); it raises log level to `DEBUG` and surfaces panic-path tracebacks via `sys.excepthook` (don't install the strip in `--verbose` mode).
- `--no-color` strips ANSI from stdout. **Wiring (verified against `src/signalforge/diff/config.py:139` — `DiffConfig` exposes only `respect_no_color_env: bool = True`; there is no `force_color` field):** when `--no-color` is passed, the CLI sets `os.environ["NO_COLOR"] = "1"` for the duration of the `cmd_generate` call and leaves `DiffConfig.respect_no_color_env=True` (the default) so the AnsiRenderer's existing precedence chain (DEC-021 of #8) honours the env var. The unconditional ANSI strip on user-content fields (DEC-007 of #8) still runs regardless of this flag — that's the security boundary, not a UX knob.
- Honor `NO_COLOR` env var without the flag: precedence chain is the diff layer's existing one (`NO_COLOR` > `FORCE_COLOR` > TTY when `respect_no_color_env=True`); `--no-color` is just a CLI-side ergonomic equivalent to setting `NO_COLOR=1` in the environment.
- Tests in `tests/cli/test_generate.py`:
  - `test_generate_emits_progress_to_stderr_in_tty` — patch `sys.stderr.isatty()` to return `True`; assert stderr contains five progress lines.
  - `test_generate_no_progress_in_non_tty` — default capsys (non-TTY); assert stderr has no progress lines.
  - `test_generate_quiet_suppresses_progress`.
  - `test_generate_verbose_shows_panic_traceback` — verify `--verbose` re-installs the default `sys.excepthook`; an unhandled Exception leaves a traceback in stderr.
  - `test_generate_no_color_strips_ansi` — `--no-color`; assert no `\x1b[` bytes in stdout.
  - `test_generate_NO_COLOR_env_strips_ansi` — env var set, no flag; same assertion.
- Validation suite green.

**Done when:** progress lines work in TTY, off in non-TTY; `--quiet` and `--verbose` flags work as specified; color knobs respect the precedence chain.

**Files:**

- `src/signalforge/cli/generate.py` — extend.
- `src/signalforge/cli/_helpers.py` — extend `setup_logging` with `quiet` / `verbose`; add `should_emit_progress() -> bool` TTY-detection helper.
- `tests/cli/test_generate.py` — extend.

**Depends on:** US-005, US-006.

**TDD:**

1. Write `test_generate_emits_progress_to_stderr_in_tty`. Fails (no progress lines).
2. Implement progress-line emission per stage entry. Test passes.
3. Write `test_generate_no_progress_in_non_tty`. Implements the TTY gate.
4. Iterate per flag.

---

### US-008 — Exit-code AST scan + parametrized tests

**Description.** The load-bearing tests that make the four-tier exit-code taxonomy a contract. Adds a 7th AST scan in `tests/test_audit_completeness.py` enforcing every `class *Error(Exception):` has a tier mapping. Adds `tests/cli/test_exit_codes.py` parametrizing over every typed exception and asserting the right exit code + stderr shape.

**Traces to:** DEC-008, DEC-019, DEC-024 (AST-scan target + exclude list).

**Acceptance criteria:**

- New scan in `tests/test_audit_completeness.py`: AST-walks every `src/signalforge/*/errors.py` file, collects every `class <Name>Error(<base>):` declaration, and asserts `<Name>Error` appears as a key in `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` (or whatever the canonical mapping table is named). **Scan target verified:** every typed exception in the project lives in an `errors.py` module — confirmed by `grep -rln "^class.*Error" src/signalforge/ --include="*.py"` returning exactly the 8 per-stage `errors.py` files (manifest, warehouse, safety, llm, draft, prune, grade, diff). The CLI's own `errors.py` makes nine. **Explicit excludes** (the `_EXCLUDED_BASES: frozenset[str]` in the scan): the abstract base per stage that subclasses inherit from — `ManifestError`, `WarehouseError`, `SafetyError`, `LLMError`, `DraftError`, `PruneError`, `GradeError`, `DiffError`, plus the new `CliError`. Plus any `*Error` class that is itself abstract/intermediate (none today, but the constant is the seam if v0.2 introduces one). A new typed exception lands without a mapping → test fails loud.
- Sanity test: at least one entry exists in the mapping table for each tier (1, 2, 3) — guards against accidental mass-rename.
- New parametrized tests in `tests/cli/test_exit_codes.py`:
  - For each typed exception in the mapping table, raise it from a synthetic stage call (using `unittest.mock.patch` to make the relevant orchestrator function raise it), call `main(["generate", "<model>"])` against the fakes, and assert:
    - exit code matches the table.
    - stderr starts with `"ERROR: "` (single line for tiers 1 and 3; header for tier 2).
    - no `"Traceback"` in stderr.
- Specific tier-2-bullet test: `LLMOutputAnchorContractError` with three violations → stderr matches `r"^ERROR: .+\n  - .+\n  - .+\n  - .+"` (header + exactly three bullets).
- `TableNotFoundError` → exit 2 (DEC-012). Specifically tested.
- Untyped `Exception` raised → exit 1 with `ERROR: An unexpected error occurred...` in stderr; no traceback.
- Validation suite green.

**Done when:** AST scan passes; parametrized exit-code tests pass for every typed exception; tier-2 bullet shape verified.

**Files:**

- `tests/test_audit_completeness.py` — add 7th scan.
- `tests/cli/test_exit_codes.py` — new file, ~200 lines.
- `src/signalforge/cli/_helpers.py` — `_EXCEPTION_TO_EXIT_CODE` mapping table fully populated; `map_exception_to_exit_code` reads from it.

**Depends on:** US-001 (need the public-surface re-exports), US-005 (need the CLI orchestration).

**TDD:**

1. Write the AST scan first. Fails (mapping table is incomplete).
2. Populate the mapping table until the scan passes.
3. Write the parametrized exit-code tests. Iterate by raising each exception type and asserting the contract.

---

### US-009 — Subprocess-gated smoke test

**Description.** One belt-and-braces test that runs `signalforge --version` via `subprocess.run(...)`. Gated behind a `cli_subprocess` marker so default `pytest` runs skip it.

**Traces to:** DEC-018.

**Acceptance criteria:**

- New marker `cli_subprocess` registered in `pyproject.toml` `[tool.pytest.ini_options].markers`.
- Default `addopts` exclusion list extended: `-m 'not bigquery and not anthropic and not cli_subprocess'`.
- New test `tests/cli/test_subprocess_smoke.py::test_signalforge_version_via_subprocess`:
  - `subprocess.run(["signalforge", "--version"], capture_output=True, text=True, timeout=5)`.
  - asserts `result.returncode == 0`.
  - asserts `result.stdout.startswith("signalforge ")`.
  - asserts `result.stderr == ""`.
- Marker-gated: default `pytest` run skips it; `pytest -m cli_subprocess` runs it.
- README's CONTRIBUTING note (or `docs/cli-ops.md`) documents that contributors should run `pytest -m cli_subprocess` once before declaring a CLI PR ready (mirrors `pytest -m bigquery` for the BQ adapter).
- Validation suite green.

**Done when:** marker registered, default-excluded, test runs cleanly under the explicit selector.

**Files:**

- `pyproject.toml` — markers entry; `addopts` update.
- `tests/cli/test_subprocess_smoke.py` — one-test file.
- `CONTRIBUTING.md` (or `docs/cli-ops.md`) — note about running the marker.

**Depends on:** US-003.

---

### US-010 — Documentation: `docs/cli-ops.md` + README + `.claude/rules/cli-layer.md`

**Description.** Three new/updated documents.

**Traces to:** DEC-001 through DEC-020 (the documentation cascade as a whole).

**Acceptance criteria:**

- New `docs/cli-ops.md` covering:
  - Installation (`pip install signalforge` and `pip install -e ".[dev]"` for development).
  - Subcommands: `generate`, `lint`, `version`. Each with full flag reference.
  - The four-tier exit-code taxonomy (cite the new `.claude/rules/cli-layer.md` and clauditor's source rule).
  - Stderr message shape for each tier (per DEC-008).
  - Environment variables (`NO_COLOR`, `FORCE_COLOR`).
  - Project-root discovery (DEC-001) including the walk-up + override + DEBUG-log mechanism.
  - Worked example: a complete `signalforge generate` session against a sample dbt project, with annotated stderr progress lines and stdout diff.
  - Cross-references to every stage's existing ops doc (`docs/safety-ops.md`, `docs/draft-ops.md`, `docs/prune-ops.md`, `docs/grade-ops.md`, `docs/diff-ops.md`).
- README updated:
  - "Status" block changed: "Pre-alpha. Nine issues shipped" / "v0.1 complete (CLI shipped via #9)".
  - Quick-start block: replace the conditional warning ("the post-#9 `--mode` CLI flag") with the actual command.
  - New "CLI" section near the top with the canonical example command and a one-line link to `docs/cli-ops.md`.
- New `.claude/rules/cli-layer.md`:
  - Established by issue #9.
  - Sections (mirroring the established rule-file shape):
    - Subpackage layout (`src/signalforge/cli/` flat; `__init__.py` + `_helpers.py` + per-subcommand modules).
    - Four-tier exit-code taxonomy (one section, the canonical place — cites clauditor's rule in See-Also).
    - Stderr message shape (DEC-008 — header+bullets for tier 2; single line for 1 and 3).
    - "No traceback ever leaks" rule + `sys.excepthook` belt-and-braces (DEC-016).
    - Format-error-at-the-sink pattern (DEC-017 — `_helpers.format_error_to_stderr` is the single source of truth).
    - Path canonicalisation at the orchestrator (every user-supplied path → `canonicalise_path`).
    - Logger grep gate now covers 6 dirs (DEC-019 of `diff-renderer.md` graduated by this ticket).
    - 7th AST scan in `tests/test_audit_completeness.py` enforces every typed exception → tier mapping (DEC-019 of this plan).
    - Subprocess-gated smoke test pattern (DEC-018).
    - Progress-to-stderr UX (DEC-014).
    - Reference: `plans/super/9-cli-entrypoint.md` (this plan), `src/signalforge/cli/`, `docs/cli-ops.md`, `tests/cli/`.
- Updated `.claude/rules/grade-layer.md` — `## v0.2 reservations` block: `fail_on_below_threshold` entry rewritten to "Graduated in #9" (lands in US-002, but the ops-doc cross-references land here).
- Updated `.claude/rules/diff-renderer.md` — `## v0.2 reservations` block: the entry citing `render_kind` and the rendered-text helper updated to "Graduated in #9 — `--format` flag wires `render_kind`; `signalforge.diff.render_to_text(...)` is the public stdout helper."
- Validation suite green.

**Done when:** all three documents land; README accurately reflects the shipped CLI; rule files cross-reference each other correctly.

**Files:**

- `docs/cli-ops.md` — new.
- `README.md` — update Status, Quick-start, add CLI section.
- `.claude/rules/cli-layer.md` — new.
- `.claude/rules/grade-layer.md` — update v0.2 block.
- `.claude/rules/diff-renderer.md` — update v0.2 block.

**Depends on:** US-001 through US-009 (this is the post-implementation doc consolidation).

---

### US-011 — Quality Gate

**Description.** Run code reviewer × 4 across the full diff for the ticket; fix every real bug found per pass; run CodeRabbit if available; full validation suite green.

**Traces to:** every DEC.

**Acceptance criteria:**

- Code reviewer pass 1: full diff reviewed; every real finding fixed in a follow-up commit (NOT amended).
- Pass 2: same diff plus pass-1 fixes; iterate until reviewer reports nothing actionable or only suggestions.
- Pass 3 + 4: same. Mirrors the pattern from #4 / #6 / #7 / #8 (each shipped 3–4 review passes).
- CodeRabbit review on the PR (if the project has it wired); fix findings.
- Validation green: `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest && pytest -m cli_subprocess`.

**Done when:** four review passes complete; full validation green; PR ready to merge.

**Files:** any file the reviews flag.

**Depends on:** US-001 through US-010.

---

### US-012 — Patterns & Memory

**Description.** Final pass to capture learnings. Refine `.claude/rules/cli-layer.md` based on what surfaced in implementation. Update CLAUDE.md repo-status block.

**Traces to:** DEC-009, DEC-019.

**Acceptance criteria:**

- `CLAUDE.md` "Repository status" section updated:
  - "Pre-alpha. Eight issues shipped." → "v0.1 alpha. Nine issues shipped."
  - Add the #9 (CLI entrypoint) bullet matching the format of every prior bullet (the bullet structure: ticket number, title, plus a one-paragraph summary of what shipped). Cross-references to `docs/cli-ops.md` and `.claude/rules/cli-layer.md`.
  - Public API surface (v0.1) section updated: add `signalforge.cli.main`, the new `CliError`/`CliPathError`/`CliInputError` family, and any other names the CLI exports.
- `.claude/rules/cli-layer.md` — refined with any patterns discovered during implementation (e.g., specific gotchas around argparse subparser registration order, the cwd-vs-project_dir layering, anything else).
- Memory file (`/home/wesd/.claude/projects/-home-wesd-Projects-SignalForge/memory/`): if any cross-cutting pattern emerged worth saving (e.g., "CLI factories for fakes go in `cli/_factories.py` not the package `__init__`"), capture it. **Do not save anything derivable from the codebase or git history.**
- Validation green.

**Done when:** CLAUDE.md reflects shipped state; rule file is consolidated; memory updated only if there's something genuinely new and non-derivable.

**Files:**

- `CLAUDE.md` — update.
- `.claude/rules/cli-layer.md` — refine.
- Memory dir — optional; only if a pattern is genuinely cross-conversation-useful.

**Depends on:** US-011.

---

## Beads Manifest

Phase 7 devolve completed 2026-05-04 (session 3).

**Worktree:** `/home/wesd/dev/worktrees/SignalForge/feature/9-cli-entrypoint`
**Branch:** `feature/9-cli-entrypoint`
**PR:** [#26](https://github.com/wjduenow/SignalForge/pull/26) (ready-for-review)

**Epic:** `bd_1-scaffolding-9vj` — `9: CLI entrypoint`

| Story | Bead ID | Priority | Depends on |
|---|---|---|---|
| US-001 — Public-surface plumbing | `bd_1-scaffolding-9vj.1` | P2 | (none) |
| US-002 — Grade-layer graduation: `GradeBelowThresholdError` | `bd_1-scaffolding-9vj.2` | P2 | (none) |
| US-003 — CLI scaffold + `version` + logger gate ext | `bd_1-scaffolding-9vj.3` | P2 | US-001 |
| US-004 — `cli/lint.py` config-only validator | `bd_1-scaffolding-9vj.4` | P2 | US-003 |
| US-005 — `cli/generate.py` core orchestration | `bd_1-scaffolding-9vj.5` | P2 | US-001, US-002, US-003 |
| US-006 — Generate flags: `--mode`/`--min-score`/`--write`/`--dry-run`/`--format` | `bd_1-scaffolding-9vj.6` | P2 | US-005 |
| US-007 — Generate observability: `--quiet`/`--verbose`/`--no-color`/progress | `bd_1-scaffolding-9vj.7` | P2 | US-005, US-006 |
| US-008 — Exit-code AST scan + parametrized tests | `bd_1-scaffolding-9vj.8` | P2 | US-001, US-005 |
| US-009 — Subprocess-gated smoke test | `bd_1-scaffolding-9vj.9` | P2 | US-003 |
| US-010 — Documentation: `cli-ops.md` + README + `cli-layer.md` | `bd_1-scaffolding-9vj.10` | P2 | US-001..US-009 |
| US-011 — Quality Gate (code reviewer ×4 + CodeRabbit + validation) | `bd_1-scaffolding-9vj.11` | P2 | US-001..US-010 |
| US-012 — Patterns & Memory | `bd_1-scaffolding-9vj.12` | P3 | US-011 |

**Initial ready set (no blockers):** US-001, US-002 — both can start in parallel. US-003 unblocks once US-001 lands; US-005 unblocks once US-001/US-002/US-003 all land; the rest cascade per the dependency graph.

**Verify with:** `bd ready` from any worktree on this repo (bd is worktree-aware).
