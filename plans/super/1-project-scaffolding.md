# Issue #1 — Project scaffolding: pyproject, src layout, ruff, pytest, CI

## Meta

- **Ticket:** [#1](https://github.com/wjduenow/SignalForge/issues/1)
- **Branch:** `feature/1-scaffolding` (off `dev`)
- **Worktree:** created via `bark new feature/1-scaffolding --from dev` (path is local-machine; not recorded here).
- **Phase:** implemented (all 7 stories merged on `feature/1-scaffolding`; PR #14 ready for review; bd epic `bd_1-scaffolding-mxk` closed).
- **Sessions:** 1 (started 2026-04-27)
- **Plan author:** Claude Code (Opus 4.7)

## Discovery

### Ticket summary

Stand up the Python project so `pip install -e .[dev]` works and `pytest` runs against an empty test suite. This is the very first implementation ticket against the v0.1 milestone — pure scaffolding, no SignalForge logic yet.

### Acceptance criteria (from ticket)

1. `pyproject.toml` with `[project]` metadata (name=`signalforge`, license=Apache-2.0, requires-python>=3.10)
2. `src/signalforge/__init__.py` with `__version__`
3. `tests/` directory with sentinel `test_smoke.py`
4. Ruff + pyright config (or mypy) wired
5. `pytest` config in `pyproject.toml`
6. GitHub Actions workflow: ruff + pytest on PRs into `dev` and pushes to `main`
7. `CONTRIBUTING.md` describing the dev branch workflow

Ticket notes:
- src layout (not flat) for editable installs
- Lock to one Python version in CI for v0.1; widen later

### Codebase findings

- **Repo state:** pre-alpha. Tracked files: `README.md`, `LICENSE`, `CLAUDE.md`. Untracked: `.claude/`, `skills/`. No source, no tests, no build config — true greenfield.
- **`.claude/rules/`:** present but empty — no project-specific rule constraints to enforce.
- **No `workflow-project.md`:** no project-specific scoping/review/chunking patterns to layer on top of the baseline workflow.
- **CLAUDE.md commitments:** package name `signalforge`, CLI entry point `signalforge`, `pip install signalforge` shape, Apache-2.0 license. CLI entry is *not* in this ticket's AC — defer to a later ticket. Pure scaffolding for now.
- **Branch:** currently on `dev`; the project's stated convention is feature branches off `dev`, merging to `dev` for milestones, with `main` as the released line.

### Out of scope (explicit)

- CLI entry point / Click/Typer wiring (the `signalforge generate ...` quick-start in README) — belongs to the next ticket.
- BigQuery adapter, LLM client, prune logic — v0.1 feature work, not scaffolding.
- Multi-version Python matrix (the ticket explicitly says lock to one for v0.1).

### Scoping decisions (Phase 1)

- **DEC-001 — Repo-policy mirror clauditor.** `.gitignore` excludes synced glaude commands/agents and all `.claude/skills/*` except maintainer-owned `release-manager` and `review-agentskills-spec`. CLAUDE.md, the maintainer skills, and plan docs are tracked. Duplicate root `skills/` dir deleted (verified identical to `.claude/skills/` modulo Windows `:Zone.Identifier` ADS files; `*:Zone.Identifier` added to ignore). Worktree created via `bark new feature/1-scaffolding --from dev`.
- **DEC-002 — Build backend + type checker: Hatchling + pyright.** Modern PEP 621 default; dynamic version sourced from `signalforge.__version__`; pyright is mainstream for Python AI tooling.
- **DEC-003 — Pin CI to Python 3.11.** `requires-python>=3.10` declared in metadata, but CI matrix is locked to a single 3.11 runner for v0.1 per ticket note. Widen later.
- **DEC-004 — Minimal dev deps.** `[project.optional-dependencies].dev = ["ruff", "pyright", "pytest"]` only. No pre-commit, no pytest-cov in v0.1.

### Phase 1 housekeeping decisions (resolved 2026-04-27)

- **DEC-005 — Beads block trimmed in `CLAUDE.md`.** Stripped the "use bd for ALL task tracking" / "do NOT use TaskCreate" prohibitions and the mandatory end-of-session push workflow. Kept a short availability note. *Why:* the bark-installed block conflicted with `/super-plan` and Claude Code's native task tools. *How to apply:* `bd` is one tool among several; use it where it fits.
- **DEC-006 — `AGENTS.md` tracked, beads block trimmed identically to CLAUDE.md.** *Why:* AGENTS.md is becoming a cross-tool standard worth committing; the prohibitions don't belong. *How to apply:* edit AGENTS.md and CLAUDE.md in lockstep when the project's tool stance changes.
- **DEC-007 — Track `.claude/settings.json`; ignore `.claude/plugins/`.** *Why:* `settings.json` here only contains `bd prime` hooks (no user paths) — safe to share. `plugins/` is install/cache state. *How to apply:* if `settings.json` ever grows machine-specific entries, split shareable bits into a `settings.shared.json` or move to `.claude/hooks.json`.
- **DEC-008 — Devolve Phase 7 to `bd` (beads).** *Why:* infrastructure already initialized by bark; matches the synced `/super-plan` workflow. *How to apply:* Phase 7 creates an epic + tasks via `bd create`, with dependency edges per the chunked stories.

Committed in `2a76dfa` on `feature/1-scaffolding`.

## Architecture Review

Reviewed inline (no subagents) — the entire universe of code is the diff this ticket produces, so there is no pre-existing surface to scout. Categories not applicable to a scaffolding ticket are marked N/A explicitly so the structure is preserved.

| Area | Rating | Notes |
| --- | --- | --- |
| Security — auth / input / secrets | **pass** | No endpoints, no inputs, no secrets in pyproject or workflow. |
| Security — GHA permissions | **concern** | Workflow must declare top-level `permissions: contents: read` (or explicit per-job). Default token permissions vary by org settings. |
| Security — GHA action pinning | **concern** | `actions/checkout` and `actions/setup-python` should be pinned to commit SHA (not `@v4`). Mitigates supply-chain risk if a tag is force-moved. |
| Performance | **N/A** | No code paths exercised. CI runtime is trivial. |
| Data model | **N/A** | No models in this ticket. |
| API design | **N/A** | No API in this ticket. |
| Observability | **N/A** | No app code; CI logs go to GitHub Actions natively. |
| Testing — scaffold quality | **concern** | A literal `assert True` smoke test is no signal (it can't fail). Should at minimum `from signalforge import __version__; assert __version__` to verify install + import path. Reinforces the project's "signal over volume" principle from day one. |
| Testing — pytest config | **concern** | `[tool.pytest.ini_options]` should set `testpaths = ["tests"]` and `addopts = "-ra --strict-markers"`. Defaults silently swallow markers and discovery quirks. |
| Build backend — hatchling dynamic version | **pass** | `[tool.hatch.version] path = "src/signalforge/__init__.py"` reads `__version__` via regex. Standard pattern. Pair with `dynamic = ["version"]` in `[project]`. |
| Build backend — src layout wheel target | **concern** | Hatchling won't auto-find `src/signalforge` — must declare `[tool.hatch.build.targets.wheel] packages = ["src/signalforge"]`. Easy to forget; `pip install -e .` succeeds while wheel build silently produces an empty package. |
| Repo hygiene — CONTRIBUTING.md scope | **concern** | "describing the dev branch workflow" — bare branch policy, or also bark/worktree convention + `/super-plan`? Affects whether `bark` is a contributor expectation or an internal tool. |
| Repo hygiene — README quick-start drift | **concern** | README shows `pip install signalforge` and `signalforge generate ...` — neither works after this ticket. Add a "Not yet on PyPI / CLI lands in #N" note, or accept the drift until the CLI ticket. |
| Repo hygiene — `__version__` value | **concern** | Pick `"0.0.0"` (placeholder) vs `"0.1.0.dev0"` (PEP 440 pre-release marker for the v0.1 milestone). |

### Phase 2 housekeeping note (informational, not blocking)

- Bark's `bd init` ran against the original (non-worktree) checkout's `.beads/` directory (not the worktree). Phase 7 turned out to work fine from any worktree because bd is worktree-aware via `bd context` — auto-discovers the canonical `.beads/` from any cwd inside the repo. `.beads/` now ignored repo-wide via `.gitignore`.

### Blockers

None. Eight `concern`s — all resolved through explicit choices in Phase 3 below; none require code re-architecture.

## Refinement Log

### Phase 3 decisions (resolved 2026-04-27)

- **DEC-009 — GHA: top-level `permissions: contents: read` + SHA-pinned actions** (R1=A). `actions/checkout` and `actions/setup-python` referenced by commit SHA with a `# v4.x.y` trailing comment for human readability. Resolves Security concerns 2 & 3. *Why:* matches OpenSSF Scorecard / Dependabot's preferred form; mitigates supply-chain risk if a tag is force-moved.
- **DEC-010 — Smoke test imports `__version__`; pytest opts strict** (R2=A). `tests/test_smoke.py` runs `from signalforge import __version__; assert __version__`. `[tool.pytest.ini_options]` sets `testpaths=["tests"]` and `addopts="-ra --strict-markers"`. *Why:* enforces the project's "signal over volume" principle from the very first test; strict markers catch typos loudly instead of silently.
- **DEC-011 — Hatchling explicit wheel packages** (R3=A). `[tool.hatch.build.targets.wheel] packages = ["src/signalforge"]`. *Why:* src layout + hatchling auto-discovery is fragile across renames; explicit wins.
- **DEC-012 — `CONTRIBUTING.md` minimal scope** (R4=A). ~25 lines: branch policy (`feature/...` off `dev`, PR to `dev`, `main` is released), Apache-2.0 reminder. No bark/`/super-plan` walkthrough yet. *Why:* low-friction for v0.1; expand when external contributors arrive.
- **DEC-013 — README v0.1 status callout** (R5=A). Add a short `> **Status:** Not yet on PyPI; CLI ships in a later v0.1 ticket.` block above the quick-start. *Why:* prevents reader's first action from being a silent failure.
- **DEC-014 — `__version__ = "0.1.0.dev0"`** (R6=A). PEP 440 pre-release marker for the v0.1 milestone. *Why:* signals active development on v0.1; bumps cleanly to `"0.1.0"` at first PyPI release.

### Out-of-band tracking

- **GitHub Issue:** [#13](https://github.com/wjduenow/SignalForge/issues/13) — open question on long-term beads ↔ `/super-plan` integration. Filed because the friction (manual CLAUDE.md/AGENTS.md trim per worktree) is worth tracking but isn't blocking #1.

## Detailed Breakdown

Five implementation stories + Quality Gate + Patterns & Memory. Ordering follows the natural Python-package dependency chain (build foundation → tooling configs → tests → CI → docs). Validation command set by this work (canonical recipe; CLAUDE.md §Validation is the source of truth): `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`.

### US-001 — `pyproject.toml` foundation + src layout + `__version__`

**Description.** Create the build foundation: PEP 621 `[project]` metadata, hatchling backend, dynamic version, src-layout package, and `[project.optional-dependencies].dev`. This unblocks `pip install -e .[dev]`.

**Traces to:** DEC-002, DEC-003, DEC-004, DEC-011, DEC-014.

**Files.**

- `pyproject.toml` (new)
  - `[build-system] requires = ["hatchling>=1.18"], build-backend = "hatchling.build"`
  - `[project]`: `name = "signalforge"`, `dynamic = ["version"]`, `description`, `requires-python = ">=3.10"`, `license = "Apache-2.0"`, `authors`, `readme = "README.md"`. No runtime deps yet.
  - `[project.optional-dependencies] dev = ["ruff", "pyright", "pytest"]` (no version pins in v0.1; widen later via DEC-004 follow-up).
  - `[project.urls]` Homepage + Issues pointing at `https://github.com/wjduenow/SignalForge`.
  - `[tool.hatch.version] path = "src/signalforge/__init__.py"`
  - `[tool.hatch.build.targets.wheel] packages = ["src/signalforge"]`
- `src/signalforge/__init__.py` (new): `__version__ = "0.1.0.dev0"`. Single line + a docstring naming the package.

**Acceptance.**

- `pip install -e .[dev]` succeeds in a fresh venv with Python 3.11.
- `python -c "import signalforge; print(signalforge.__version__)"` prints `0.1.0.dev0`.
- `python -m build` (or `hatch build`) produces a wheel whose `RECORD` includes `signalforge/__init__.py`.

**Done when.** `pip install -e .[dev]` and the import + version smoke check both pass on a fresh venv.

**Depends on.** none.

---

### US-002 — Ruff + pyright config

**Description.** Wire up linting and type checking. Both configs live in `pyproject.toml`. Pick a starter rule set that's strict enough to be useful but not so strict it derails empty modules.

**Traces to:** DEC-002.

**Files.**

- `pyproject.toml` (modify): add
  - `[tool.ruff]`: `line-length = 100`, `target-version = "py310"` (matches `requires-python` floor).
  - `[tool.ruff.lint]`: `select = ["E", "F", "W", "I", "UP", "B", "SIM"]`, `ignore = []`. (E/F/W = pycodestyle+pyflakes core; I = isort; UP = pyupgrade; B = bugbear; SIM = simplify.)
  - `[tool.ruff.format]`: empty block (accept defaults).
  - `[tool.pyright]`: `include = ["src", "tests"]`, `pythonVersion = "3.11"` (matches CI; widen later), `typeCheckingMode = "standard"`, `reportMissingImports = "error"`.

**Acceptance.**

- `ruff check .` exits 0 against the empty package.
- `ruff format --check .` exits 0.
- `pyright` exits 0 against `src/` and `tests/`.

**Done when.** All three commands above exit 0 in the worktree.

**Depends on.** US-001.

---

### US-003 — `tests/` + smoke test + pytest config

**Description.** Stand up the test framework with a non-trivial sentinel that doubles as an install/import smoke check.

**Traces to:** DEC-010.

**TDD.**

The story *is* the first test, so the workflow inverts slightly: write the smoke test first, watch it fail with `ModuleNotFoundError` (proving collection works), then verify it passes after US-001 lands.

Specific assertions:

- `from signalforge import __version__` succeeds.
- `__version__` is a non-empty string.
- (Optional) `__version__` matches a basic PEP 440 shape (`re.match(r"\d+\.\d+\.\d+", __version__)`).

**Files.**

- `tests/test_smoke.py` (new): the assertions above. No `tests/__init__.py` (pytest's rootdir handles it; matches src-layout convention).
- `pyproject.toml` (modify): `[tool.pytest.ini_options]` with `testpaths = ["tests"]`, `addopts = "-ra --strict-markers"`, `minversion = "7.0"`.

**Acceptance.**

- `pytest` collects 3 tests and all pass.
- `pytest --collect-only` shows `tests/test_smoke.py::test_version_is_non_empty_string`, `::test_version_matches_pep440_shape`, and `::test_import_has_no_error_chain`.
- Adding a bogus `@pytest.mark.does_not_exist` to the smoke test causes pytest to *error* (not warn) at collection time. Note: under pytest 9.x this requires both `addopts = "-ra --strict-markers"` AND a separate `strict_markers = true` ini setting — `--strict-markers` in `addopts` does not propagate to `getini("strict_markers")`. See `.claude/rules/testing-signal.md`.

**Done when.** `pytest` exits 0 with three passing tests, and the strict-markers behavior is verified once locally before reverting the bogus marker.

**Depends on.** US-001.

---

### US-004 — GitHub Actions CI workflow

**Description.** Single CI workflow that runs ruff + pyright + pytest on PRs into `dev` and pushes to `main`. Pinned, scoped, cached.

**Traces to:** DEC-003, DEC-009.

**Files.**

- `.github/workflows/ci.yml` (new). Shape:
  - `name: ci`
  - `on: { pull_request: { branches: [dev] }, push: { branches: [main] } }`
  - `permissions: { contents: read }`
  - `concurrency: { group: "ci-${{ github.ref }}", cancel-in-progress: true }`
  - `jobs.lint-test`:
    - `runs-on: ubuntu-latest`
    - Steps:
      1. `actions/checkout@<sha> # v4.2.x`
      2. `actions/setup-python@<sha> # v5.x.y` with `python-version: "3.11"` and `cache: "pip"`
      3. `pip install -e .[dev]`
      4. `ruff check .`
      5. `ruff format --check .`
      6. `pyright`
      7. `pytest`

SHAs to be looked up against the latest `v4` / `v5` tag at implementation time and recorded as `# vX.Y.Z` trailing comments per DEC-009.

**Acceptance.**

- A trivial PR into `dev` triggers the workflow and it passes against the `feature/1-scaffolding` branch.
- The workflow file passes `actionlint` (or equivalent lint) without warnings.
- A test that intentionally fails (locally only — do not commit) is caught by the workflow when pushed to a throwaway branch.

**Done when.** The workflow completes green on the PR for #1 (Phase 5 will create the PR; this story is "done" once the workflow file is committed and locally validated).

**Depends on.** US-002, US-003.

---

### US-005 — `CONTRIBUTING.md` + README v0.1 status callout

**Description.** Two documentation touches: a fresh, lean `CONTRIBUTING.md`, and a one-paragraph status callout in `README.md` so the existing `pip install signalforge` quick-start doesn't lead readers off a cliff.

**Traces to:** DEC-012, DEC-013.

**Files.**

- `CONTRIBUTING.md` (new, ~25 lines):
  - Branching: `feature/<n>-<short-name>` off `dev`; PR back into `dev`; `main` is the released line, only `dev` → `main` merges.
  - Local dev: `pip install -e .[dev]`, then `ruff check . && ruff format --check . && pyright && pytest`.
  - License reminder: contributions are Apache-2.0; the repo-level `LICENSE` covers per-file headers.
  - Issues: link to `https://github.com/wjduenow/SignalForge/issues`. State that v0.1 ships as design-in-the-open on `dev`.
  - Explicitly out of scope for this iteration: `bark`, `/super-plan`, `bd` — none of these are contributor expectations yet (tracked under #13).
- `README.md` (modify): insert a short callout block just above the existing `## Quick start` heading, e.g.:
  ```
  > **Status (v0.1, in progress):** Not yet on PyPI. The CLI shape below is the
  > intended target — the CLI itself ships in a follow-up ticket. Today the
  > package installs from a clone with `pip install -e .[dev]`.
  ```

**Acceptance.**

- `CONTRIBUTING.md` exists, is ≤ 40 lines, and links to the issue tracker.
- `README.md` quick-start no longer makes claims that fail at execution time for a v0.1 reader.

**Done when.** Both files render correctly on GitHub (preview locally with `gh pr view --web` after Phase 5).

**Depends on.** US-001 (so the `pip install -e .[dev]` instruction in CONTRIBUTING is real).

---

### US-006 — Quality Gate

**Description.** Sweep the full changeset for issues before merge. Run code reviewer 4 times; on each pass, fix every real bug found before the next pass. Run CodeRabbit if available. End on green validation.

**Traces to:** all DEC-### in this plan (gate covers everything).

**Acceptance.**

- 4 sequential code-reviewer passes; each pass either finds no issues or every found issue is fixed before the next pass starts.
- CodeRabbit review (if available) — all real issues addressed.
- Final validation green: `pip install -e .[dev] && ruff check . && ruff format --check . && pyright && pytest` exits 0.
- Git status clean (no uncommitted work).

**Done when.** All passes report no remaining real issues *and* the validation command exits 0.

**Depends on.** US-001, US-002, US-003, US-004, US-005.

---

### US-007 — Patterns & Memory

**Description.** Capture conventions established in this ticket so future Claude Code sessions inherit them without re-deriving.

**Traces to:** DEC-009, DEC-010, DEC-011, DEC-013.

**Files.**

- `.claude/rules/python-build.md` (new): src layout + hatchling dynamic-version pattern; explicit `[tool.hatch.build.targets.wheel] packages` requirement.
- `.claude/rules/ci-supply-chain.md` (new): GHA SHA-pinning convention with `# vX.Y.Z` trailing comment; top-level `permissions: contents: read`; `concurrency` cancel-in-progress.
- `.claude/rules/testing-signal.md` (new): "no `assert True` smoke tests — every test must be capable of failing"; `--strict-markers` is a hard rule.
- Update `CLAUDE.md` validation command section to reference the canonical 4-step run (`pip install -e .[dev] && ruff check . && pyright && pytest`).

**Acceptance.**

- All three new rule files exist, are ≤ 40 lines each, and follow the project's existing CLAUDE.md tone.
- `CLAUDE.md` includes the validation command as the canonical "how to verify" recipe.
- Memory entries (auto-memory): note that issue #13 captures the beads ↔ super-plan stance; don't re-litigate.

**Done when.** Files committed, `git status` clean. Future `/super-plan` runs in this repo discover and apply these rules in Phase 1's Convention Checker subagent.

**Depends on.** US-006.

---

### Story dependency graph

```
US-001 ──┬──► US-002 ──┐
         ├──► US-003 ──┼──► US-004 ──┐
         └──► US-005 ─────────────────┴──► US-006 ──► US-007
```

US-002, US-003, US-005 can run in parallel after US-001. US-004 waits on US-002+US-003. US-006 waits on all implementation stories. US-007 waits on US-006.

## Beads Manifest

Created 2026-04-27 via `bd create` + `bd link` from the worktree (bd is worktree-aware via `bd context` — auto-discovers the canonical `.beads/` in the original (non-worktree) checkout; no symlink or env var needed).

**Epic:** `bd_1-scaffolding-mxk` — *1: Project scaffolding (pyproject, src layout, ruff, pytest, CI)* (P1, `external-ref=gh-1`)

| Bead ID | Story | Priority | Blocked by |
| --- | --- | --- | --- |
| `bd_1-scaffolding-mxk.1` | US-001 — `pyproject.toml` foundation + src layout + `__version__` | P1 | — (READY) |
| `bd_1-scaffolding-mxk.2` | US-002 — Ruff + pyright config | P2 | `.1` |
| `bd_1-scaffolding-mxk.3` | US-003 — `tests/` + smoke test + pytest config | P2 | `.1` |
| `bd_1-scaffolding-mxk.4` | US-004 — GitHub Actions CI workflow | P2 | `.2`, `.3` |
| `bd_1-scaffolding-mxk.5` | US-005 — `CONTRIBUTING.md` + README v0.1 status callout | P3 | `.1` |
| `bd_1-scaffolding-mxk.6` | US-006 — Quality Gate (4× code-reviewer + CodeRabbit) | P2 | `.1`, `.2`, `.3`, `.4`, `.5` |
| `bd_1-scaffolding-mxk.7` | US-007 — Patterns & Memory | P4 | `.6` |

11 `blocks` edges. `bd ready` returns only `.1` (and the epic itself).

**To start work:** `cd` to either the worktree or the canonical checkout (bd works from both), run `bd ready`, claim `.1` with `bd update bd_1-scaffolding-mxk.1 --status=in_progress`, and follow the US-001 spec in this plan doc.

**Note on Dolt sync:** Bark configured a Dolt remote pointing at `git+ssh://git@github.com/wjduenow/SignalForge.git`, but GitHub does not host Dolt servers — `bd dolt push` will not work against this URL. Local `.beads/` is the only persistence for now. Resolve when picking a long-term stance per #13.
