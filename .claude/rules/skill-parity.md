# Skill parity (the bundled Claude Code skill is a CLI-parity surface)

Established by [#141](https://github.com/wjduenow/SignalForge/issues/141) (ship a SignalForge Claude Code skill + install command). The shipped artefacts:

- `src/signalforge/skills/signalforge/SKILL.md` — the user-facing skill that teaches Claude to drive `signalforge generate` / `lint` / `prune-existing` / `init-demo` / `install-skill` / `version` against a user's dbt project, including the zero-credential demo and the gated live e2e flow.
- `signalforge install-skill [<dest>]` — the CLI subcommand that copies the bundled skill out of the wheel into `<dest>/.claude/skills/signalforge/`. Lib seam at `signalforge.skill.install_skill(...)`; CLI handler at `signalforge.cli.install_skill`.
- `tests/cli/test_skill_cli_parity.py` — the parity gate that closes the loop.

Because SKILL.md documents the CLI surface, it is a **parity surface**: it must stay in lockstep with the actual CLI.

## The skill is the 6th parity surface (extends `cli-layer.md`)

A behaviour change to the CLI subcommand/flag surface — adding, renaming, or removing a subcommand; changing the flags or demo commands the skill names — updates `src/signalforge/skills/signalforge/SKILL.md` in the **same change**. The bundled skill is one more entry in `cli-layer.md`'s "a behaviour change touches N surfaces" rule; treat it exactly like the help string / ops doc / test surfaces already listed there.

## Enforcement is a gate, not a prompt (load-bearing)

Do **not** rely on the model remembering to update the skill during a `/ralph-run` (or any) session. `tests/cli/test_skill_cli_parity.py` is the **parity gate** — it parses the live CLI (`signalforge.cli._build_parser()` → walks the `_SubParsersAction.choices` mapping) plus the locked `_CANONICAL_DEMO_COMMANDS` tuple and asserts every token appears verbatim in `SKILL.md`. Three categories scanned (per #141 DEC-015):

1. **Every subcommand name** from the live argparse parser. Auto-grows when a new subcommand lands — the gate iterates the parser, never a hardcoded set, so adding `signalforge foo` and forgetting `SKILL.md` fails the gate without anyone editing the test.
2. **Four canonical demo command lines** (hardcoded in the test, mirrors the demo flow taught in SKILL.md): `signalforge init-demo`, `signalforge generate <model> --write`, `signalforge prune-existing <model> --schema <path>`, `signalforge install-skill`. Plain substring match; no whitespace / case normalisation (mirrors the envelope-breach guard pattern from `business-rule-tests.md`).
3. **The install-skill bootstrap line** — covered by category 2's fourth entry but documented as a separate concern in the test docstring.

The third test in the file (`test_parity_gate_catches_missing_subcommand_planted_violation`) is the **planted-violation self-check** required by `testing-signal.md` § "AST source-scan gates": it writes a synthetic SKILL.md missing one subcommand to `tmp_path`, drives the same factored-out helper the real gate uses, and asserts an `AssertionError` is raised. Without it, a refactor that broke the scan visitor would silently disable the gate at the exact moment a real violation needed catching.

Because `/ralph-run` runs `VALIDATE_CMD` on every bead, a change that drifts the CLI from the skill **fails validation until SKILL.md is updated** — the skill stays current automatically. The gate also encodes "**when appropriate**": it fires only on a relevant surface change, never on unrelated work. This is the same gate-over-prompt philosophy as the AST scans, drift detectors, and grep gates in `testing-signal.md`.

## Worker-writability — keep the skill in `src/`, never `.claude/`

Ralph workers **cannot write to `.claude/` in worktrees** (orchestrator-only — see the user memory `ralph-worker-claude-dir-perms`). The shipped skill therefore lives in `src/signalforge/skills/` and the parity gate in `tests/` — **both worker-writable** — so a worker that trips the gate fixes `SKILL.md` in `src/` itself. The maintainer-only skills (`release-manager`, `review-agentskills-spec`) stay under repo-root `.claude/skills/` and are excluded from the wheel + the install command:

- They live at repo-root `.claude/skills/`, outside `src/`, so the Hatch `include = ["src/signalforge/skills"]` cannot reach them by construction (DEC-022 of #141).
- `signalforge install-skill` enumerates from `importlib.resources.files("signalforge").joinpath("skills")` — the package-data tree only — so there's no code path that could install them.
- A defensive **negative assertion** in `tests/test_wheel_packaging.py` (`test_wheel_excludes_maintainer_only_claude_skills`) documents this intent: no `.claude/skills/*` paths appear in the built wheel.

Never move the shipped skill under `.claude/`: that would make it un-updatable by workers and defeat this rule.

## The two-name convention (load-bearing)

Two paths, one each side of the seam — easy to confuse, deliberately distinct:

- **Package-data tree:** `src/signalforge/skills/signalforge/SKILL.md` — plural `skills/` parent matches the install destination shape (`.claude/skills/signalforge/SKILL.md`) and allows future sibling skills (e.g. `skills/signalforge-grade/`) without restructuring. NOT a Python package — no `__init__.py` under `skills/` or `skills/signalforge/`. Mirrors `src/signalforge/_demo/`'s posture (package-data, not a Python package).
- **Python lib package:** `src/signalforge/skill/` — singular `skill/`, a real Python package with `__init__.py` + `errors.py`. Owns `install_skill(...)` and the `SkillError` hierarchy. Mirrors `signalforge.demo` exactly.

When adding a v0.2 sibling skill, add `src/signalforge/skills/<other-skill>/SKILL.md` (recursive Hatch include picks it up); the parity gate auto-grows for `<other-skill>`'s subcommands; the Python lib stays a single `signalforge.skill` module.

## What the gate cannot catch

The gate enforces the **mechanical** surface (subcommands / flags / demo commands present). It cannot judge whether the skill's **prose** is still accurate after a behaviour change. Back that with the optional clauditor self-grade (`clauditor grade src/signalforge/skills/signalforge/SKILL.md`, see #141 DEC-014 + US-008 — pre-release manual run, pinned in `assets/SKILL.eval.json`, surfaced via shields.io README badge) and reviewer attention — the gate is necessary, not sufficient.

## Reference

`#141` — the skill, the `install-skill` command, and the parity gate. `plans/super/141-claude-skill-install.md` — the full plan (24 DECs). `cli-layer.md` § "Multi-surface parity for behaviour changes" — the N-surface parity rule the skill extends + the exit-code taxonomy the install command follows. `python-build.md` — wheel packaging of the skill (`include` + `wheel_smoke`). `testing-signal.md` — the gate-over-prompt philosophy + planted-violation self-check requirement. `tests/cli/test_skill_cli_parity.py` — the gate. `tests/cli/test_5_surface_parity_*.py` — the per-subcommand parity-test precedent (orthogonal to this gate — that one pins ONE subcommand across five surfaces; this one scans the FULL CLI surface against ONE skill body).
