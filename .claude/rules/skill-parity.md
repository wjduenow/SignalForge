# Skill parity (the bundled Claude Code skill is a CLI-parity surface)

Specified by [#141](https://github.com/wjduenow/SignalForge/issues/141) (ship a SignalForge Claude Code skill + install command). **Anticipatory** — the skill and its parity gate are not yet implemented; this rule states the contract the #141 implementation must satisfy, and the convention every later change must hold once they land. Until #141 ships, the files named below do not exist yet.

SignalForge ships a user-facing Claude Code skill at `src/signalforge/skills/signalforge/SKILL.md` that teaches Claude to drive the `signalforge` CLI (`generate` / `lint` / `prune-existing` / `init-demo` + the e2e demo flow). That SKILL.md documents the CLI surface, so it is a **parity surface**: it must stay in lockstep with the actual CLI.

## The skill is the Nth parity surface (extends `cli-layer.md`)

A behaviour change to the CLI subcommand/flag surface — adding, renaming, or removing a subcommand; changing the flags or demo commands the skill names — updates `src/signalforge/skills/signalforge/SKILL.md` in the **same change**. The bundled skill is one more entry in `cli-layer.md`'s "a behaviour change touches N surfaces" rule; treat it exactly like the help string / ops doc / test surfaces already listed there.

## Enforcement is a gate, not a prompt (load-bearing)

Do **not** rely on the model remembering to update the skill during a `/ralph-run` (or any) session. A pytest **parity gate** under `tests/` parses the live CLI (subparser registry / `--help`) and asserts every registered subcommand plus the key demo commands/flags appears in `SKILL.md`. It mirrors the `tests/cli/test_5_surface_parity_*.py` precedent and runs inside the canonical `VALIDATE_CMD` (`uv run pytest`).

Because `/ralph-run` runs `VALIDATE_CMD` on every bead, a change that drifts the CLI from the skill **fails validation until SKILL.md is updated** — the skill stays current automatically. The gate also encodes "**when appropriate**": it fires only on a relevant surface change, never on unrelated work. This is the same gate-over-prompt philosophy as the AST scans, drift detectors, and grep gates in `testing-signal.md`.

## Worker-writability — keep the skill in `src/`, never `.claude/`

Ralph workers **cannot write to `.claude/` in worktrees** (orchestrator-only). The shipped skill therefore lives in `src/signalforge/skills/` and the parity gate in `tests/` — **both worker-writable** — so a worker that trips the gate fixes `SKILL.md` in `src/` itself. The maintainer-only skills (`release-manager`, `review-agentskills-spec`) stay under repo-root `.claude/skills/` and are excluded from the wheel + the install command. Never move the shipped skill under `.claude/`: that would make it un-updatable by workers and defeat this rule.

## What the gate cannot catch

The gate enforces the **mechanical** surface (subcommands / flags / demo commands present). It cannot judge whether the skill's **prose** is still accurate after a behaviour change. Back that with the optional clauditor self-grade (`clauditor grade .../SKILL.md`, #141) and reviewer attention — the gate is necessary, not sufficient.

## Reference

`#141` — the skill, the `install-skill` command, and the parity gate. `cli-layer.md` — the N-surface parity rule + the exit-code taxonomy the install command follows. `python-build.md` — wheel packaging of the skill (`include` + `wheel_smoke`). `testing-signal.md` — the gate-over-prompt philosophy. `tests/cli/test_5_surface_parity_*.py` — the parity-test precedent to mirror.
