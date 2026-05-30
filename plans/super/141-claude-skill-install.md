# 141: SignalForge Claude Code skill + `install-skill` command

## Meta

- **Ticket:** [GH #141](https://github.com/wjduenow/SignalForge/issues/141)
- **Branch:** `feature/141-claude-skill-install`
- **Worktree:** `../worktrees/SignalForge/141-claude-skill-install`
- **Phase:** published
- **PR:** [#166](https://github.com/wjduenow/SignalForge/pull/166)
- **Epic:** TBD
- **Sessions:**
  - 2026-05-29 — Phase 1 discovery kickoff (parallel research)

## Ticket summary

Ship a user-facing **Claude Code skill** for SignalForge, bundled in the wheel, plus an
**install command** that drops it into a target project's `.claude/skills/` — mirroring
clauditor's `src/clauditor/skills/` + `clauditor setup` pattern. The skill teaches Claude
how to drive the `signalforge` CLI against a user's dbt project so adoption is "install the
package, run the skill," not "read the docs and assemble commands by hand."

**AC-1** `pip install signalforge-dbt` then `signalforge install-skill` drops a working
`SKILL.md` into `.claude/skills/signalforge/`, and a fresh Claude Code session activates it
on a relevant prompt.

**AC-2** `wheel_smoke` asserts the skill ships in the wheel.

**AC-3** Install command honours the four-tier exit codes + no-traceback floor; registered
in the exit-code AST scan.

**AC-4** README + docs link the skill; `mkdocs build` stays clean.

**AC-5** On request, the skill runs the zero-credential `init-demo` → `generate` demo
end-to-end; the live `pytest -m e2e` path is gated behind explicit user confirmation +
env-var checks (clean skip when unset, cost warning before running).

**AC-6** The skill's CLI surface is enforced by a parity gate running inside the canonical
`VALIDATE_CMD` (`uv run pytest`): adding/changing a subcommand or demo command without
updating `SKILL.md` fails the test — so `/ralph-run` keeps the skill current automatically,
without relying on the model remembering.

## Discovery

### Codebase findings (key seams)

- **CLI subcommand template — `init-demo` is the closest precedent.** Both `add_parser` and
  `cmd_init_demo` live in `src/signalforge/cli/init_demo.py`; registered from
  `src/signalforge/cli/__init__.py:84`. The new module is `src/signalforge/cli/install_skill.py`
  following the same shape verbatim.
- **Library-surface wrap pattern.** `signalforge.demo.copy_demo(dest, *, force=False) -> Path`
  is the public lib; `Demo*Error` hierarchy lives in `signalforge.demo.errors`. The CLI handler
  wraps lib errors into `CliInitDemo*Error` (`src/signalforge/cli/errors.py`). New
  `signalforge.skill` subpackage mirrors this: `install_skill(dest, *, force) -> Path` + a
  `Skill*Error` hierarchy + CLI-side `CliInstallSkill*Error` wrappers.
- **Exit-code registry.** `_EXCEPTION_TO_EXIT_CODE` in `src/signalforge/cli/_helpers.py` is
  the single source of truth. `init-demo`'s registrations (DemoPathError → 1,
  DemoDestExistsError → 2, CliInitDemoFixtureMissingError → 1, etc.) are the template.
- **Wheel packaging.** `[tool.hatch.build.targets.wheel]` in `pyproject.toml` carries
  `packages = ["src/signalforge"]` + `include = ["src/signalforge/_demo"]`. Add a sibling
  `include` entry for the skills tree. (See decision on path name below.)
- **`wheel_smoke` precedent.** `tests/test_wheel_packaging.py::_EXPECTED_DEMO_FILES` is a
  7-file tuple asserted present in the built `.whl`. The skills equivalent asserts
  `SKILL.md` + `SKILL.eval.json` (if grading) + any `assets/` files appear under the
  expected wheel path.
- **AST audit-completeness scan #7.** Already a depth-1∪depth-2 glob over
  `src/signalforge/*/errors.py`; new `signalforge/skill/errors.py` lands automatically.
  Update the count test (`test_scan_7_discovers_every_per_stage_errors_module` — bump
  12 → 13) AND add `SkillError` to `_EXCEPTION_MAPPING_EXCLUDED_BASES` if the hierarchy
  spans tiers 1+2 (mirrors `DemoError`/`IngestError`).
- **5-surface parity test precedent.** `tests/cli/test_5_surface_parity_init_demo.py`
  pins canonical tokens (subcommand name, key flags) across argparse help / handler docstring
  / `docs/cli-ops.md` / plan / test name. The skill ticket needs the same shape for the
  `install-skill` flags.
- **Skill ↔ CLI parity gate (NEW, separate test).** Distinct from the 5-surface parity gate:
  this one parses the live argparse subparser registry from `signalforge.cli._build_parser()`
  and asserts every registered subcommand name + the demo-flow commands appear verbatim in
  `src/signalforge/skills/signalforge/SKILL.md`. Mirrors the mechanical surface-scan idea.
- **Subprocess smoke pattern.** `tests/cli/test_subprocess_smoke.py` runs `signalforge
  install-skill --help` under `@pytest.mark.cli_subprocess` (default-deselected); asserts
  `returncode == 0`, subcommand-unique tokens in stdout, no traceback on stderr.
- **MkDocs nav.** `mkdocs.yml` has a flat `nav:` with a "CLI Reference: cli-ops.md" + a
  "Pipeline Stages" subsection. A "Claude Code Skill: skills.md" entry at the top level
  (after CLI Reference) fits naturally.
- **README quick-start.** `README.md:78-100` has `## Quick start` → `Install` subsection.
  The skill pointer fits as a follow-up sentence after `pip install`.

### Agent-skills spec (what the SKILL.md must contain)

From `.claude/skills/review-agentskills-spec/SKILL.md` + the `release-manager` example:

- **Frontmatter:** `name` (matches parent dir), `description` (activation triggers — "use
  when X, Y, or Z"), `compatibility` (hard requirements: dbt project, manifest.json present,
  optional warehouse profile + API keys), `disable-model-invocation` (omit; this skill
  reasons about the diff), `allowed-tools` (scoped Bash patterns).
- **`allowed-tools` scope** (zero-credential default; live e2e gated behind confirmation):
  - `Bash(signalforge *)` — every CLI invocation
  - `Bash(uv run signalforge *)` — uv-run variant
  - `Bash(cat *)`, `Bash(ls *)`, `Bash(grep *)` — inspecting fixtures + diff output
  - `Read`, `Write`, `Edit` — for the user's dbt project files only
  - (Conditional, behind explicit user opt-in) `Bash(uv run pytest -m e2e*)` for the live
    smoke; the skill body forces a confirmation gate before invoking
- **Body shape:** `# /signalforge — drafts and prunes dbt tests with an LLM` + numbered
  workflow sections covering (1) point at a dbt project; (2) zero-cred demo via
  `init-demo` → `generate`; (3) `prune-existing` for tests the user already has; (4)
  reading the kept/kept-uncertain/dropped/flagged diff + per-artifact "why"; (5) safety
  posture (schema-only default; sample is opt-in); (6) optional live e2e (gated).

### Convention/rule constraints (filtered)

Most-load-bearing per `.claude/rules/`:

- **`cli-layer.md`:** `add_parser`/`cmd_install_skill`; four-tier exit codes (0/1/2/3);
  library-surface wrap pattern (lib seam + thin CLI handler); typed-error registration in
  `_EXCEPTION_TO_EXIT_CODE`; no-traceback floor; single-boundary `try/except Exception`;
  path canonicalisation at orchestrator via `canonicalise_user_path(raw, project_dir)` (with
  caveat: install-skill has NO project_dir requirement — the user runs it before they have
  one configured, like `init-demo`); subprocess `--help` smoke under `cli_subprocess`;
  5-surface parity for new flags.
- **`python-build.md`:** Explicit `include = ["src/signalforge/skills"]`; `wheel_smoke` test
  pins the skill file set (dotfile-inclusion fragility noted — SKILL.md is not a dotfile so
  this is straightforward, but `assets/` recursion needs verification in the smoke test).
- **`docs-publishing.md`:** New `docs/skills.md` requires a `nav:` entry in `mkdocs.yml` in
  the same commit. No new docs deps.
- **`testing-signal.md`:** No `assert True`-shaped tests; strict markers (already set); AST
  source-scan gates if any new "must-call X" gate is added; marker-gated subprocess pattern.
- **`manifest-readers.md`:** Three symlink/containment traps apply to install-skill's
  destination-path validation.
- **`skill-parity.md` (anticipatory rule — file NOT YET on disk, lives in CLAUDE.md context
  only).** Specifies the parity gate contract: skill lives at
  `src/signalforge/skills/signalforge/SKILL.md` (worker-writable, NEVER `.claude/`); gate
  parses live CLI subparser registry; runs inside `VALIDATE_CMD`. The rule file is part of
  this ticket — Deliverable 7 ("parity-surface rule entry") ships it.
- **`safety-layer.md`:** NOT APPLICABLE. install-skill is a deterministic file copy with no
  LLM/warehouse/audit seam.

## Scoping decisions (Phase 1 close)

- **S-1 Destination policy:** Always overwrite SKILL.md (+ the files we ship); preserve any
  sibling files the user has added under `.claude/skills/signalforge/`. **No `--force` flag**
  in v0.1. Friendlier for upgrade-in-place ("re-run install-skill, get the new SKILL.md").
  We still refuse if SKILL.md *itself* is a symlink — writing follows the link, which is
  the same defence init-demo's `copy_demo` already implements for `--force`-against-symlink.
- **S-2 e2e demo paths:** Both. Zero-credential `init-demo` → `generate` is the always-on
  default. Live `pytest -m e2e` is opt-in — the skill body forces an explicit user
  confirmation, checks `SF_RUN_BQ` / `GOOGLE_CLOUD_PROJECT` / `ANTHROPIC_API_KEY`, and warns
  about warehouse + LLM cost before invoking. `allowed-tools` scopes the live path
  conditionally.
- **S-3 Self-grade badge:** Include in v0.1. Run `clauditor grade` against the SKILL.md,
  pin the score in `assets/SKILL.eval.json` (sibling of SKILL.md), and surface a shields.io
  badge from the README. (The CI-vs-local-pinning question is Phase 3 refinement.)
- **S-4 Skill src path:** `src/signalforge/skills/signalforge/SKILL.md` — plural `skills/`
  parent allows a future sibling skill (e.g., `skills/signalforge-grade/`) without
  restructuring; matches the install destination shape exactly; matches the anticipatory
  `skill-parity.md` rule verbatim.

## Architecture review

| Area | Rating | Findings |
|------|--------|----------|
| **Security** | pass | Mirror `signalforge.demo.copy_demo`'s symlink-cycle trap (`resolve(strict=True)` first, fall back to `strict=False` on `FileNotFoundError`/`NotADirectoryError`, catch both `RuntimeError` (≤3.12) and `OSError(errno.ELOOP)` (≥3.13)). Per S-1 we never `rmtree`, so the `--force`-against-symlink-dest hazard collapses; we still refuse to overwrite if `<dest>/.claude/skills/signalforge/SKILL.md` is a symlink (writing follows the link). Path canonicalisation rolled inline like `copy_demo` (NOT via `canonicalise_user_path`, which requires a project_dir — install-skill is the second "creates the project context" entry point alongside `init-demo`, and its module docstring will document this verbatim, citing the `copy_demo` precedent). |
| **API design** | concern | Default `<dest>` is `.` (CWD), so the install path becomes `<CWD>/.claude/skills/signalforge/SKILL.md` — operator runs the command from the dbt project root. Mirrors `init-demo`'s `./signalforge-demo/` ergonomics. Lib seam: `install_skill(dest: Path \| str = ".") -> Path` returns the absolute SKILL.md path. **Concern:** if `<dest>/.claude/skills/signalforge/` exists with an unmodelled file alongside SKILL.md, do we report what we preserved? Lock the answer in Phase 3. |
| **Packaging / wheel_smoke** | pass | `include = ["src/signalforge/skills"]` ships the tree recursively (confirmed by the `_demo` precedent — every nested file lands in the wheel without additional globs). `wheel_smoke` extends with a sibling `_EXPECTED_SKILL_FILES` tuple naming SKILL.md + SKILL.eval.json (+ any v0.1 assets). Dotfile-fragility note in `python-build.md` doesn't apply (SKILL.md is not a dotfile). |
| **Observability** | pass | One INFO log line at success: `{"installed": "<abs path>", "preserved_siblings": [...]}` (lazy-format JSON; raw paths are user-owned, no PII concerns). No DEBUG/WARNING/audit JSONL — install-skill is a deterministic file copy. |
| **Testing strategy** | pass | (1) lib seam unit tests (`tests/skill/test_install.py`) — happy / overwrite-existing / preserve-siblings / SkillDestUnsafeError / SkillPackageDataMissingError. (2) CLI handler tests (`tests/cli/test_install_skill.py`) — main(argv) paths exercising each exit code. (3) Subprocess `--help` smoke under `cli_subprocess` marker. (4) `wheel_smoke` extension. (5) Skill ↔ CLI parity gate (NEW — scope locked in Phase 3). (6) 5-surface parity for `install-skill` itself (no flags in v0.1, so canonical tokens reduce to the subcommand name). |
| **Docs** | pass | New `docs/skills.md` catalog + `mkdocs.yml` nav entry (one line under "CLI Reference"). README "Quick start" gains one sentence after `pip install signalforge-dbt` pointing at `signalforge install-skill`. The shields.io self-grade badge surfaces at the README top per `clauditor`'s precedent. |
| **AST scan #7 (typed-error registry)** | pass | New `signalforge/skill/errors.py` is the **13th** per-stage `errors.py` (current count: 12). Bump `test_scan_7_discovers_every_per_stage_errors_module` count + add `SkillError` to `_EXCEPTION_MAPPING_EXCLUDED_BASES` (its concretes will span tier 1 + tier 2, mirroring `DemoError`/`IngestError`). |
| **Worker-writability** | pass | All shipped artefacts land under worker-writable paths: SKILL.md + assets under `src/signalforge/skills/`; parity gate under `tests/`; rule file under `.claude/rules/skill-parity.md` (orchestrator-only edit). Per `ralph-worker-claude-dir-perms.md` memory, the orchestrator (not a worker) makes the one `.claude/rules/` edit. |
| **Worktree / branch** | pass | Worktree created at `/home/wesd/Projects/worktrees/SignalForge/141-claude-skill-install` on `feature/141-claude-skill-install` off `dev`. |

No blockers. Two concerns surface as Phase 3 refinement questions: (1) clauditor self-grade
operating model (CI vs pinned-at-release), (2) Skill ↔ CLI parity gate token scope.

## Refinement log

### Decisions

- **DEC-001 — Skill source path.** Package-data tree at
  `src/signalforge/skills/signalforge/SKILL.md` (plural `skills/` parent allows future
  sibling skills; matches `.claude/skills/<name>/SKILL.md` install destination shape;
  matches the anticipatory `skill-parity.md` rule verbatim). NO `__init__.py` under
  `skills/` or `skills/signalforge/` — the directory is package-data, NOT a Python
  package. Mirrors `src/signalforge/_demo/` exactly.

- **DEC-002 — Python lib subpackage name.** The runtime code lives at
  `src/signalforge/skill/` (singular) — a real Python package with `__init__.py`,
  `errors.py`, and the public `install_skill(...)` function. Singular name mirrors
  `signalforge.demo`; the package-data tree's plural name is the install destination
  convention, not the lib name. Two distinct paths, one each side of the seam.

- **DEC-003 — Destination policy.** `install_skill(dest, *, ...)` always overwrites
  every file SignalForge ships (SKILL.md + SKILL.eval.json + any `assets/*` we
  enumerate from the bundled tree); never touches any other file in the dest dir. No
  `--force` flag in v0.1. Friendlier for upgrade-in-place; eliminates the
  `--force`-against-symlink-dest hazard that `copy_demo` defends against because we
  never `rmtree`.

- **DEC-004 — Default destination.** Positional `<dest>` defaults to `"."` (CWD). The
  effective install path is `<CWD>/.claude/skills/signalforge/SKILL.md`. Mirrors
  `init-demo`'s default-to-CWD ergonomics. The operator runs from the dbt project root.

- **DEC-005 — Symlink defence (mirror `copy_demo` verbatim).** `install_skill` resolves
  `<dest>` via `.resolve(strict=True)` first; falls back to `.resolve(strict=False)` on
  `FileNotFoundError` / `NotADirectoryError` (common — dest dir may not exist yet);
  catches `RuntimeError` (Python ≤3.12) AND `OSError(errno.ELOOP)` (Python ≥3.13) on
  cycle detection. Wraps cycle failures as `SkillDestPathError` (tier 1). Additionally:
  if `<dest>/.claude/skills/signalforge/SKILL.md` exists AND is a symlink, raise
  `SkillDestUnsafeError` (tier 2) — writing would follow the link to an arbitrary
  destination.

- **DEC-006 — Path canonicalisation lives in the lib, not via `canonicalise_user_path`.**
  `canonicalise_user_path` enforces a `project_dir` containment boundary. `install-skill`
  is the second "creates the project context" entry point (alongside `init-demo`) where
  no project_dir applies. The lib seam rolls its own resolution mirroring
  `signalforge.demo.copy_demo`; the module docstring documents the precedent verbatim.

- **DEC-007 — Package-data lookup.** Mirror `copy_demo` verbatim:
  `files("signalforge").joinpath("skills").joinpath("signalforge")` wrapped in
  `as_file(...)` for zipapp/zipimport safety. Failure to find the bundled tree raises
  `SkillPackageDataMissingError` (tier 1) — signals a corrupted install.

- **DEC-008 — Error hierarchy.**
  - Lib (`signalforge.skill.errors`):
    - `SkillError(Exception)` — abstract base; `extra="forbid"` is N/A (not a Pydantic
      model); `__str__` renders `message` + optional `↳ Remediation:` line per
      `manifest-readers.md` § "Errors carry remediation."
    - `SkillDestPathError(SkillError)` — tier 1; symlink cycle / containment failure.
    - `SkillDestUnsafeError(SkillError)` — tier 2; dest is a file (not dir), SKILL.md is
      a symlink, dest permission denied at write time.
    - `SkillPackageDataMissingError(SkillError)` — tier 1; bundled SKILL.md absent.
  - CLI (`signalforge.cli.errors`):
    - `CliInstallSkillPathError(CliError)` — tier 1; wraps `SkillDestPathError`.
    - `CliInstallSkillDestUnsafeError(CliError)` — tier 2; wraps `SkillDestUnsafeError`.
    - `CliInstallSkillPackageDataMissingError(CliError)` — tier 1; wraps
      `SkillPackageDataMissingError`.
  - **Concretes span tiers 1 + 2**, so `SkillError` joins `DemoError` / `IngestError`
    pattern: register only in `_EXCEPTION_MAPPING_EXCLUDED_BASES`, never in the
    `_EXCEPTION_TO_EXIT_CODE` table.

- **DEC-009 — AST scan #7.** Bump
  `test_scan_7_discovers_every_per_stage_errors_module` count 12 → 13 in lockstep
  with `signalforge/skill/errors.py` landing. Add `SkillError` to
  `_EXCEPTION_MAPPING_EXCLUDED_BASES` (frozenset). Register every concrete CLI wrapper
  (`CliInstallSkillPathError` / `CliInstallSkillDestUnsafeError` /
  `CliInstallSkillPackageDataMissingError`) AND every lib concrete (`SkillDestPathError`
  / `SkillDestUnsafeError` / `SkillPackageDataMissingError`) in
  `_EXCEPTION_TO_EXIT_CODE` (defence-in-depth: both layers in the table even though MRO
  walk would resolve the lib raise via the CLI wrapper).

- **DEC-010 — Wheel packaging.** Extend `[tool.hatch.build.targets.wheel].include` to
  `["src/signalforge/_demo", "src/signalforge/skills"]`. The directory-level include
  is transitive — every nested file (SKILL.md, SKILL.eval.json, assets/*) ships
  recursively. Confirmed by the `_demo` precedent (recursively ships nested
  `models/staging/*.sql`, `target/*.json`).

- **DEC-011 — `wheel_smoke` extension.** Add `_EXPECTED_SKILL_FILES` tuple alongside
  `_EXPECTED_DEMO_FILES` in `tests/test_wheel_packaging.py`. v0.1 set:
  `("signalforge/skills/signalforge/SKILL.md",
    "signalforge/skills/signalforge/assets/SKILL.eval.json")`. Run via
  `uv run pytest -m wheel_smoke --no-cov`. Also add a NEGATIVE assertion: no
  `.claude/skills/*` paths appear in the built wheel (defence against accidentally
  including maintainer-only `release-manager` / `review-agentskills-spec` — they live
  at repo-root `.claude/skills/`, outside `src/`, so they're already excluded, but
  the negative assertion documents intent).

- **DEC-012 — e2e demo paths (both, with live gated).** Zero-credential default:
  `signalforge init-demo /tmp/signalforge-demo` → `signalforge generate
  models/staging/stg_bikeshare_trips.sql --write` (schema-only mode by default) →
  walk through the kept / kept-uncertain / dropped / flagged diff. Live e2e (opt-in):
  the skill body forces an explicit user confirmation ("This will run paid LLM +
  warehouse queries — proceed?"), checks `SF_RUN_BQ`, `GOOGLE_CLOUD_PROJECT`,
  `ANTHROPIC_API_KEY` (clean skip-with-reason when absent), then invokes
  `uv run pytest -m e2e --no-cov`. Cost warning before invocation.

- **DEC-013 — `allowed-tools` scope.** Comma-separated:
  `Bash(signalforge *), Bash(uv run signalforge *), Bash(uv run pytest -m e2e*),
  Bash(cat *), Bash(ls *), Bash(grep *), Bash(head *), Bash(tail *),
  Read, Write, Edit`. The `pytest -m e2e*` scope is required for the live-gated path
  per DEC-012; the skill body's confirmation gate is the user-facing defence.

- **DEC-014 — Self-grade operating model.** Pre-release manual run.
  Maintainer runs `clauditor grade src/signalforge/skills/signalforge/SKILL.md` locally
  before tagging a release; captures the score; pins it in
  `src/signalforge/skills/signalforge/assets/SKILL.eval.json`. README badge surfaces the
  pinned score via shields.io. Same commit updates SKILL.md + eval.json + README
  badge. No CI integration, no Anthropic key in repo secrets, no per-PR cost. Adds
  `clauditor` to `[dependency-groups].dev` if not already present.

- **DEC-015 — Parity gate scope.** New test
  `tests/cli/test_skill_cli_parity.py` scans for three categories of tokens, all of which
  must appear verbatim in `src/signalforge/skills/signalforge/SKILL.md`:
  1. Every subcommand name from the live argparse parser (auto-grows). Source:
     `signalforge.cli._build_parser()` → walk `parser._subparsers._group_actions[0].choices`.
     Current v0.2 set: `generate`, `lint`, `prune-existing`, `init-demo`, `install-skill`,
     `version`.
  2. The four canonical demo command lines: `signalforge init-demo`,
     `signalforge generate <model> --write`, `signalforge prune-existing <model> --schema
     <path>`, `signalforge install-skill`. Plain substring match — no regex, no whitespace
     normalisation (mirrors envelope-breach guard pattern from `business-rule-tests.md`).
  3. The install-skill bootstrap line itself (`signalforge install-skill`).
  The gate is mechanical, not semantic — semantic freshness lives in the clauditor self-grade.

- **DEC-016 — Parity gate is a NEW test, not an extension of 5-surface parity.** The
  5-surface parity tests in `tests/cli/test_5_surface_parity_*.py` pin canonical tokens for
  ONE subcommand across five surfaces (help/docstring/ops/plan/test). The skill parity
  gate scans the FULL CLI surface against ONE skill body. Different shape, different
  failure modes; keeping them as separate tests preserves the locality of each gate's
  failure message.

- **DEC-017 — Overwrite UX.** Single INFO line on success:
  `Installed SignalForge skill to <abs path>`. When an existing SKILL.md was overwritten,
  append `(replaced existing SKILL.md)`. No diff, no backup file. The operator can
  `git diff` if they had the file under version control. Lazy-format JSON; not via
  `_LOGGER` (the CLI writes to stdout for success messages, stderr for errors).

- **DEC-018 — `cli-layer.md` parity-surface entry.** Add a paragraph under the
  "Multi-surface parity for behaviour changes" section noting that the bundled skill is
  the Nth parity surface — a change to the CLI subcommand/flag surface updates
  `src/signalforge/skills/signalforge/SKILL.md` in the same commit, and the
  `tests/cli/test_skill_cli_parity.py` gate enforces it. Adds a "6th surface" entry to
  the list (currently: help/docstring/ops/test/DEC).

- **DEC-019 — `skill-parity.md` rule file.** The orchestrator (NOT a worker) writes
  `.claude/rules/skill-parity.md` in this PR per the
  `ralph-worker-claude-dir-perms.md` memory — workers cannot Write under `.claude/` in
  worktrees. The content is the contract written verbatim in DEC-013…DEC-018 above plus
  a pointer back to this plan + cli-layer.md.

- **DEC-020 — SKILL.md frontmatter.**
  ```yaml
  ---
  name: signalforge
  description: Use when the user wants to draft, prune, or grade dbt tests / docs with an LLM, has a dbt project (manifest.json + sql models), or asks about SignalForge. Drives the `signalforge` CLI end-to-end: drafts candidate tests, runs them against warehouse samples, drops the noise, and explains every kept/dropped artifact.
  compatibility: "Requires: signalforge installed (pip install signalforge-dbt). For the zero-credential demo: no warehouse needed. For real dbt projects: dbt-core + a populated manifest.json. For live e2e: a configured warehouse profile (BigQuery v0.1) + ANTHROPIC_API_KEY."
  metadata:
    signalforge-version: "0.X.Y"
  allowed-tools: Bash(signalforge *), Bash(uv run signalforge *), Bash(uv run pytest -m e2e*), Bash(cat *), Bash(ls *), Bash(grep *), Bash(head *), Bash(tail *), Read, Write, Edit
  ---
  ```
  No `disable-model-invocation` — the skill reasons about the per-artifact "why" output
  to help the operator interpret the diff. `signalforge-version` is updated by the
  release-manager skill in lockstep with the wheel version.

- **DEC-021 — SKILL.md body sections.** Numbered workflow:
  1. **Point at a dbt project** — verify `manifest.json` exists, name a model.
  2. **Zero-credential demo** — `init-demo` → `generate <model> --write` walkthrough.
  3. **Real project: draft + prune** — `generate <model> --write` with the safety
     posture (schema-only default; `--mode sample` is opt-in; document the cost).
  4. **Grade tests you already have** — `prune-existing <model> --schema <path>`.
  5. **Reading the diff** — kept / kept-uncertain / dropped / flagged tiers + the
     per-artifact "why" cascade.
  6. **Optional: live e2e demonstration** — gated behind explicit user confirmation,
     env-var checks, cost warning.
  7. **Troubleshooting** — common errors (`ModelNotFoundError`, `WarehouseAuthError`,
     `LLMCacheTooLargeError`) with one-line fixes; pointer to `docs/cli-ops.md`.

- **DEC-022 — Maintainer-only skill exclusion.** `release-manager` and
  `review-agentskills-spec` live at repo-root `.claude/skills/`, which is outside `src/`
  — they're never in the wheel by construction. install-skill enumerates from
  `files("signalforge").joinpath("skills")` (the package-data tree only), so there's
  no code path that could install them. The wheel_smoke negative assertion (DEC-011)
  documents this intent.

- **DEC-023 — Docs entry.** New `docs/skills.md` page describing the bundled skill +
  install command + the two demo paths (zero-cred and live-gated). `mkdocs.yml` `nav:`
  gains `- Claude Code Skill: skills.md` under "CLI Reference". README "Quick start"
  gets a one-sentence pointer after the `pip install` block. The README self-grade
  badge surfaces the clauditor score (DEC-014).

- **DEC-024 — 5-surface parity for the `install-skill` subcommand itself.** Canonical
  tokens (v0.1, no flags): `"install-skill"`. The test mirrors
  `test_5_surface_parity_init_demo.py` shape across (1) argparse help, (2) handler
  docstring, (3) `docs/cli-ops.md`, (4) this plan, (5) test docstring. The
  SKILL ↔ CLI parity gate (DEC-015) is orthogonal — that one scans the *full* CLI
  surface against SKILL.md; this one pins one subcommand across five surfaces.

### Session notes

- 2026-05-29 — Phase 1 discovery: parallel research locked the four scoping decisions
  (dest policy, e2e paths, self-grade inclusion, src path); architecture review pass
  surfaced two refinement concerns (self-grade ops, parity gate scope, overwrite UX);
  Phase 3 closed all 24 decisions. Plan now at `detailing` phase, ready for story
  generation.

## Detailed breakdown

The 11 stories below follow the natural architecture order: package-data + wheel
packaging → public lib seam → CLI handler → enforcement gates → docs/grade → rules
ledger → quality gate → memory.

**Acceptance check repeated for every story:**
`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`
(the canonical `VALIDATE_CMD`).

---

### US-001 — Bootstrap `src/signalforge/skills/signalforge/` tree + wheel packaging

Lay down the package-data skeleton (empty-but-shaped SKILL.md + SKILL.eval.json
placeholder under `assets/`), wire wheel packaging, and extend `wheel_smoke` to gate
the file set. Content of SKILL.md stays a placeholder (`# SignalForge skill — draft`)
until US-007 fills it in; this story owns the *shape*.

**Traces to:** DEC-001, DEC-010, DEC-011, DEC-022.

**Files:**
- `src/signalforge/skills/signalforge/SKILL.md` — placeholder body; full content lands
  in US-007.
- `src/signalforge/skills/signalforge/assets/SKILL.eval.json` — placeholder JSON
  (`{"score": null, "version": "0.0.0", "graded_at": null}`); pinned in US-008.
- `pyproject.toml` — extend `[tool.hatch.build.targets.wheel].include` to
  `["src/signalforge/_demo", "src/signalforge/skills"]`.
- `tests/test_wheel_packaging.py` — add `_EXPECTED_SKILL_FILES` tuple + assertion;
  add negative assertion that no `.claude/skills/*` paths appear in the wheel.

**Done when:** `uv build && unzip -l dist/*.whl | grep signalforge/skills/` shows
SKILL.md + assets/SKILL.eval.json; `uv run pytest -m wheel_smoke --no-cov` passes
including the negative `.claude/skills/*` assertion; full `VALIDATE_CMD` passes.

**TDD:** Not pure TDD — the wheel_smoke test IS the test for this story. Write the
expected file tuple + negative assertion FIRST (red), then update pyproject.toml
include + create the placeholder files (green).

**Depends on:** none.

---

### US-002 — Public `signalforge.skill` lib module + typed errors

Create the `signalforge.skill` Python package with `install_skill(dest) -> Path` and
the four-class typed-error hierarchy. Mirror `copy_demo`'s symlink/cycle defence
verbatim; mirror its `importlib.resources` lookup; never `rmtree`. AST scan #7 picks
up the new `errors.py` automatically (depth-1 glob).

**Traces to:** DEC-002, DEC-003, DEC-005, DEC-006, DEC-007, DEC-008, DEC-009.

**Files:**
- `src/signalforge/skill/__init__.py` — exports `install_skill`, the three lib errors,
  and `SkillError` base. `__all__` is the public contract.
- `src/signalforge/skill/errors.py` — `SkillError` base + three concretes.
- `tests/skill/test_install.py` — unit tests (see TDD below).
- `tests/test_audit_completeness.py` — bump `test_scan_7_discovers_every_per_stage_errors_module`
  count 12 → 13; add `SkillError` to `_EXCEPTION_MAPPING_EXCLUDED_BASES`.

**Done when:** `install_skill(tmp_path)` returns the absolute SKILL.md path under
`<tmp_path>/.claude/skills/signalforge/`; preserves any sibling files; symlink-cycle
dest raises `SkillDestPathError`; symlinked-SKILL.md dest raises
`SkillDestUnsafeError`; patched-away source raises `SkillPackageDataMissingError`;
AST scan #7 passes; full `VALIDATE_CMD` passes.

**TDD:** Write these tests FIRST:
1. `test_install_skill_to_fresh_dir_writes_skill_md` — happy path; assert returned
   path is absolute and exists.
2. `test_install_skill_overwrites_existing_skill_md_unchanged_otherwise` — pre-create
   `.claude/skills/signalforge/SKILL.md` with `"OLD"` + a sibling `notes.txt`;
   `install_skill` returns; assert SKILL.md content changed AND notes.txt untouched.
3. `test_install_skill_refuses_when_skill_md_is_symlink` — pre-create the dest
   tree with SKILL.md as a symlink; assert `SkillDestUnsafeError`.
4. `test_install_skill_with_cyclic_symlink_dest_raises_dest_path_error` — create
   a symlink cycle as dest; assert `SkillDestPathError`.
5. `test_install_skill_missing_package_data_raises` — monkeypatch
   `importlib.resources.files` to return a non-dir; assert
   `SkillPackageDataMissingError`.
6. `test_install_skill_dest_is_file_raises_unsafe` — pass an existing regular file
   as dest; assert `SkillDestUnsafeError`.

**Depends on:** US-001 (placeholder SKILL.md must exist in the source tree).

---

### US-003 — CLI `install-skill` subcommand + handler + exit-code mapping + subprocess smoke

Wire the subcommand into the argparse registry; add the three `CliInstallSkill*Error`
wrappers; register every typed error in `_EXCEPTION_TO_EXIT_CODE`; ship the
subprocess `--help` smoke under `cli_subprocess`.

**Traces to:** DEC-002, DEC-003, DEC-004, DEC-008, DEC-009, DEC-017, DEC-024.

**Files:**
- `src/signalforge/cli/install_skill.py` — `add_parser(subparsers)` + `cmd_install_skill(args) -> int`.
- `src/signalforge/cli/__init__.py` — register via
  `install_skill_cmd.add_parser(subparsers)` in `_build_parser()`.
- `src/signalforge/cli/errors.py` — three `CliInstallSkill*Error` wrapper classes.
- `src/signalforge/cli/_helpers.py` — register six new entries in
  `_EXCEPTION_TO_EXIT_CODE` (three lib + three CLI wrappers per DEC-009).
- `tests/cli/test_install_skill.py` — main([…]) tests for each exit-code path; assert
  no traceback on stderr.
- `tests/cli/test_subprocess_smoke.py` — add `test_signalforge_install_skill_help_via_subprocess`
  under `@pytest.mark.cli_subprocess`.

**Done when:**
- `signalforge install-skill <tmp>` returns 0, writes file, INFO line on stdout per
  DEC-017.
- `signalforge install-skill <file-not-dir>` returns 2, prints
  `ERROR: <message>` + remediation, no traceback.
- `signalforge install-skill <symlink-cycle>` returns 1, no traceback.
- `uv run pytest -m cli_subprocess --no-cov` passes the new `--help` smoke.
- Full `VALIDATE_CMD` passes.

**TDD:** Write these tests FIRST:
1. `test_install_skill_success_returns_zero_writes_file_prints_info` — happy path.
2. `test_install_skill_overwrite_appends_replaced_notice` — pre-create old SKILL.md;
   assert stdout contains `(replaced existing SKILL.md)` per DEC-017.
3. `test_install_skill_dest_is_file_returns_two_no_traceback` — tier 2.
4. `test_install_skill_dest_with_symlink_cycle_returns_one_no_traceback` — tier 1.
5. `test_install_skill_missing_package_data_returns_one_no_traceback` —
   monkeypatched.
6. `test_install_skill_default_dest_is_cwd` — `chdir(tmp_path)`, run
   `main(["install-skill"])`, assert file lands at `tmp_path/.claude/skills/signalforge/SKILL.md`.

**Depends on:** US-002.

---

### US-004 — SKILL ↔ CLI parity gate

The mechanical enforcement test that closes the
"forgot-to-update-SKILL.md-when-changing-the-CLI" loop. Lives under `tests/` so
workers can update it.

**Traces to:** DEC-015, DEC-016, DEC-019.

**Files:**
- `tests/cli/test_skill_cli_parity.py` — NEW test file.

**Done when:**
- Test reads `src/signalforge/skills/signalforge/SKILL.md` once.
- Walks `signalforge.cli._build_parser()._subparsers._group_actions[0].choices` to
  enumerate every registered subcommand; asserts each name appears as a substring of
  the SKILL.md body.
- Asserts the four canonical demo command lines (per DEC-015) appear verbatim.
- Asserts the install-skill bootstrap line (`signalforge install-skill`) appears.
- Failure prints which subcommand / demo command / bootstrap line was missing.
- Planted-violation self-check: a separate test inside the same file edits a copy of
  SKILL.md in `tmp_path` to remove `"generate"`, asserts the gate raises
  `AssertionError` — proves the gate can fail.

**TDD:** Write the planted-violation self-check FIRST (it's a red test for a gate
that doesn't exist yet → write the gate to make it green).

**Depends on:** US-001 (SKILL.md placeholder), US-003 (install-skill subcommand
registered). The SKILL.md placeholder from US-001 needs to be expanded enough to
contain the canonical tokens this test scans for — coordinated with US-007 which
writes the prose; US-004 may temporarily fail until US-007 lands. Sequence US-004 to
either land AFTER US-007 or to be merged together; document dependency.

---

### US-005 — 5-surface parity test for `install-skill`

Mirror `test_5_surface_parity_init_demo.py` for the new subcommand. v0.1 canonical
tokens: `"install-skill"` (no flags yet, so the surface is minimal).

**Traces to:** DEC-024.

**Files:**
- `tests/cli/test_5_surface_parity_install_skill.py` — NEW test mirroring the
  `init_demo` precedent.

**Done when:** test asserts `"install-skill"` appears in all five surfaces: (1)
argparse help (rendered from `add_parser`), (2) `cmd_install_skill` docstring, (3)
`docs/cli-ops.md` § Subcommands, (4) this plan
(`plans/super/141-claude-skill-install.md`), (5) the test docstring itself. Failure
names which surface lacks the token.

**Depends on:** US-003 (subcommand exists), US-007 (`docs/cli-ops.md` updated). Mirror
US-004's coordination — may need to land alongside US-007.

---

### US-006 — Docs: `docs/skills.md`, `mkdocs.yml` nav, `docs/cli-ops.md`, README pointer

Single docs story covering all four surfaces. Authoritative content for the skill
catalog page; updates README quick-start with the one-line pointer; extends
`docs/cli-ops.md` with the `install-skill` subcommand entry (Flag reference / Exit
codes / Stderr shapes).

**Traces to:** DEC-021, DEC-023.

**Files:**
- `docs/skills.md` — NEW. Describes the bundled skill, what it teaches, the install
  command, and both demo paths (zero-cred + live-gated). Pointer to clauditor
  self-grade.
- `mkdocs.yml` — add `- Claude Code Skill: skills.md` under "CLI Reference".
- `docs/cli-ops.md` — add `install-skill` entry to Subcommands section; map to exit
  codes; show stderr shapes for each tier-2/1 error.
- `README.md` — one-sentence pointer after `pip install signalforge-dbt`:
  `Run \`signalforge install-skill\` to drop the Claude Code skill into your project.`

**Done when:** `uv run --only-group docs mkdocs build` is clean; new nav entry
renders; README quick-start shows the pointer; `docs/cli-ops.md` § install-skill
matches the actual handler help text.

**Depends on:** US-003 (subcommand exists so help text + cli-ops entry can be
generated against the real handler).

---

### US-007 — Author the SKILL.md prose (the actual user-facing workflow)

Fill in the placeholder from US-001 with the real workflow per DEC-020 (frontmatter)
+ DEC-021 (body sections). This is the prose-heavy story; expect iteration with the
clauditor self-grade in US-008.

**Traces to:** DEC-012, DEC-013, DEC-020, DEC-021.

**Files:**
- `src/signalforge/skills/signalforge/SKILL.md` — replace placeholder with full body.

**Done when:**
- Frontmatter matches DEC-020 verbatim.
- All seven body sections from DEC-021 present.
- Both demo paths (zero-cred + live-gated) include the exact CLI invocations.
- Live-gated section enforces the user confirmation + env-var check + cost warning.
- SKILL ↔ CLI parity gate (US-004) passes against the new content.
- 5-surface parity (US-005) passes.

**Depends on:** US-001 (placeholder exists), US-003 (install-skill subcommand
registered so SKILL.md can reference it accurately).

---

### US-008 — Clauditor self-grade + README badge

Add `clauditor` to dev-deps if absent; run grading; pin the score in
`assets/SKILL.eval.json`; surface the shields.io badge on the README.

**Traces to:** DEC-014.

**Files:**
- `pyproject.toml` — add `clauditor` to `[dependency-groups].dev` if not present.
- `src/signalforge/skills/signalforge/assets/SKILL.eval.json` — replace placeholder
  with real graded JSON.
- `README.md` — add shields.io badge near the top (alongside any existing
  badges).
- `docs/skills.md` — add a "Self-grade" subsection pointing at the pinned score and
  the regeneration command.

**Done when:** `clauditor grade src/signalforge/skills/signalforge/SKILL.md` runs
clean against the SKILL.md from US-007; the JSON has a numeric `score`, a non-null
`graded_at` ISO-8601 UTC timestamp, and a `signalforge-version` matching
`signalforge.__version__`; README badge URL points at the pinned score; full
`VALIDATE_CMD` passes.

**Depends on:** US-007 (SKILL.md prose stable). Run AFTER US-007 lands so the score
reflects the real content.

---

### US-009 — Skill-parity rule file + cli-layer.md update (orchestrator)

The rule files under `.claude/rules/` are orthogonal to worker-writable code per
`ralph-worker-claude-dir-perms.md` memory — the orchestrator (this conversation OR
the maintainer in a closing PR commit) writes them, not a Ralph worker. Worker
implementations of US-001…US-008 reference these rules; this story lands them
durably.

**Traces to:** DEC-018, DEC-019.

**Files:**
- `.claude/rules/skill-parity.md` — NEW; written by the orchestrator.
- `.claude/rules/cli-layer.md` — add a paragraph under "Multi-surface parity for
  behaviour changes" naming the bundled skill as a parity surface; cross-link to
  skill-parity.md.

**Done when:** both files present, lint-clean, cross-referenced; ralph workers can
read them. No test gates this directly (rule files are read by humans + the model);
absence is caught at code-review time.

**Depends on:** US-003, US-004 (the contracts these rules document must exist).

---

### US-010 — Quality Gate

Run `code-review` x4 across the full diff; address each pass's findings; run
CodeRabbit if available; ensure `VALIDATE_CMD` is green; gated marker runs
(`wheel_smoke`, `cli_subprocess`) clean.

**Traces to:** ALL prior decisions.

**Done when:** four code-review passes complete with all real findings resolved;
CodeRabbit review posted + addressed; `VALIDATE_CMD` green; `uv run pytest -m
wheel_smoke --no-cov` green; `uv run pytest -m cli_subprocess --no-cov` green.

**Depends on:** US-001 … US-009.

---

### US-011 — Patterns & Memory

Capture durable lessons from this work. Likely additions:
- "Skill-shaped lib seam mirrors init-demo verbatim" — pattern for any future "ship a
  user-facing artifact into the user's project" subcommand.
- "Two-name convention: `skills/` (plural) for the package-data tree matching the
  install destination; `skill/` (singular) for the Python lib module matching
  `signalforge.demo`."
- "Parity gate over prompt — the model can't be relied on to update SKILL.md from
  context; the pytest gate is the durable enforcement."
- Memory file under `~/.claude/projects/-home-wesd-Projects-SignalForge/memory/`
  + MEMORY.md pointer per the harness memory protocol.

**Traces to:** Lessons learned from US-001 … US-010.

**Done when:** new memory files written; MEMORY.md updated with one-line pointers;
`.claude/rules/` changes (if any) reviewed.

**Depends on:** US-010.

## Risks & non-goals

**Non-goals:**
- No CI integration for clauditor grading (manual pre-release per DEC-014).
- No multi-skill install (v0.1 ships exactly one skill; the `skills/` plural parent
  anticipates v0.2+).
- No `--force` flag (per DEC-003).
- No `.bak` file on overwrite (per DEC-017).
- No diff-on-overwrite output (per DEC-017).

**Risks:**
- **R-1: SKILL.md prose churn drives badge churn.** Every SKILL.md edit triggers a
  new clauditor grade + eval.json + README badge update (3-file commit). Mitigation:
  group SKILL.md edits into PRs where possible; document the regen command in
  `docs/skills.md`.
- **R-2: SKILL ↔ CLI parity gate false negatives.** A subcommand could be added with
  a name that's also a common English word (e.g. if someone adds a `signalforge run`)
  — the substring scan would pass even if the SKILL.md doesn't actually teach the
  command. Acceptable for v0.1; the clauditor self-grade catches semantic gaps.
- **R-3: Anticipatory rule file (skill-parity.md) drift.** The rule file references
  contracts that other rules also reference. If we update one and forget the other,
  the rules drift. Mitigation: keep skill-parity.md short and link out to
  cli-layer.md / python-build.md rather than restating their contracts.


## Refinement log

_Pending Phase 3._

## Detailed breakdown

_Pending Phase 4._
