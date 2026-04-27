# Issue #1 — Project scaffolding: pyproject, src layout, ruff, pytest, CI

## Meta

- **Ticket:** [#1](https://github.com/wjduenow/SignalForge/issues/1)
- **Branch:** TBD (pending worktree decision in Phase 1 scoping)
- **Phase:** discovery
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

### Open scoping questions

See "Scoping questions" section below — being presented to user now. Answers will be appended here.

## Architecture Review

*(Phase 2 — pending)*

## Refinement Log

*(Phase 3 — pending)*

## Detailed Breakdown

*(Phase 4 — pending)*

## Beads Manifest

*(Phase 7 — pending)*
