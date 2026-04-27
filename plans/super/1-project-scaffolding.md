# Issue #1 — Project scaffolding: pyproject, src layout, ruff, pytest, CI

## Meta

- **Ticket:** [#1](https://github.com/wjduenow/SignalForge/issues/1)
- **Branch:** `feature/1-scaffolding` (off `dev`)
- **Worktree:** `/home/wesd/dev/worktrees/SignalForge/feature/1-scaffolding` (created via `bark`)
- **Phase:** architecture
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

- Bark's `bd init` ran against the original repo at `/home/wesd/Projects/SignalForge/.beads/` (not the worktree). Phase 7 must run from the original checkout, or re-init beads in the worktree. `.beads/` now ignored repo-wide via `.gitignore`.

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

*(Phase 4 — pending)*

## Beads Manifest

*(Phase 7 — pending)*
