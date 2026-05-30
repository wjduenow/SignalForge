# Claude Code Skill

SignalForge ships a [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills)
that teaches Claude to drive the `signalforge` CLI end-to-end against a dbt
project. With the skill installed, a Claude Code session in the project root
recognises requests like "draft tests for `dim_customers`," "prune my existing
`schema.yml`," or "run the demo," picks the right `signalforge` subcommand and
flags, and explains the kept / kept-uncertain / dropped / flagged diff back to
the user.

The skill is bundled inside the `signalforge-dbt` wheel under
`src/signalforge/skills/signalforge/` and installed into your project with one
command (issue [#141](https://github.com/wjduenow/SignalForge/issues/141)).

## Install

```bash
signalforge install-skill [<dest>]
```

Drops the bundled skill into `<dest>/.claude/skills/signalforge/SKILL.md`.
`<dest>` defaults to the current working directory, so the common invocation
from a dbt project root is just `signalforge install-skill`.

| Aspect | Behaviour |
| --- | --- |
| Default `<dest>` | current working directory (`.`) |
| Install path | `<dest>/.claude/skills/signalforge/SKILL.md` |
| Overwrite policy | Always replaces every file SignalForge ships (no `--force` flag). **Preserves** every other file in the destination tree — your hand-edited `.claude/` siblings are untouched. |
| Overwrite signal | On success, stdout prints `Installed SignalForge skill to <abs path>`; appends `(replaced existing SKILL.md)` when an existing file was overwritten. |

The CLI handler wraps the public `signalforge.skill.install_skill(dest)`
library entry point at the `cmd_install_skill` boundary and re-raises the
three `SkillError` subclasses as `CliInstallSkill*Error` wrappers so the
four-tier exit-code taxonomy stays homogeneous. Exit codes:

| Tier | Exit | Causes |
| --- | --- | --- |
| Load | `1` | `CliInstallSkillPathError` (symlink cycle on `<dest>`); `CliInstallSkillPackageDataMissingError` (broken wheel install — the bundled skill tree could not be located via `importlib.resources`). |
| Input | `2` | `CliInstallSkillDestUnsafeError` — `<dest>` exists as a regular file, OR the existing `SKILL.md` is a symlink (writing would follow the link). |
| API | `3` | n/a — install-skill makes no network / warehouse / LLM call. |

Pointer to [docs/cli-ops.md § `signalforge install-skill`](cli-ops.md#signalforge-install-skill-dest)
for the full flag table and stderr shapes.

## What the skill teaches

The SKILL.md body is a numbered workflow that walks Claude through the full
SignalForge surface (DEC-021 of
[`plans/super/141-claude-skill-install.md`](../plans/super/141-claude-skill-install.md)):

1. **Point at a dbt project** — verify `target/manifest.json` exists, name a
   model to work on.
2. **Zero-credential demo** — `signalforge init-demo` followed by
   `signalforge generate <model> --write` against the bundled Austin
   bikeshare fixture. No warehouse needed; runs entirely from the wheel.
3. **Real project: draft + prune** — `signalforge generate <model> --write`
   with the safety posture (schema-only default; `--mode sample` is opt-in;
   document the cost) and `--estimate` for a pre-flight cost preview.
4. **Grade tests you already have** — `signalforge prune-existing <model>
   --schema <path>` runs the prune step (no LLM call) over an externally
   authored `schema.yml`, so the warehouse tells you which existing tests
   add signal.
5. **Reading the diff** — kept / kept-uncertain / dropped / flagged tiers
   and the per-artifact "why" cascade (rationale → evidence → fallback).
6. **Optional: live e2e demonstration** — gated behind explicit user
   confirmation, env-var checks, and a cost warning. Runs the maintainer
   `pytest -m e2e --no-cov` flow against the public BigQuery dataset.
7. **Troubleshooting** — common errors (`ModelNotFoundError`,
   `WarehouseAuthError`, `LLMCacheTooLargeError`, etc.) with one-line fixes
   and a pointer to [docs/cli-ops.md](cli-ops.md).

The skill always activates against the live `signalforge` CLI on the user's
PATH — `signalforge --version` is the first thing it runs to confirm the
install resolved.

## Two demo paths

The skill body offers two ways to demonstrate SignalForge to a user. Pick
based on whether the user has warehouse credentials ready.

### Zero-credential demo (default)

`signalforge init-demo` copies the bundled Austin bikeshare demo project out
of the wheel into a writable directory; `signalforge generate <model>
--write` then runs the full draft + prune + grade + diff pipeline against
that fixture.

**No warehouse access required.** The drafter still calls Anthropic (so the
demo needs `ANTHROPIC_API_KEY`), but the prune step works against the local
fixture rather than a live warehouse. This is the default path the skill
recommends — fastest time-to-signal, zero cloud setup. See
[docs/cli-ops.md § `signalforge init-demo`](cli-ops.md#signalforge-init-demo-dest)
for the dest-policy and overwrite story.

### Live e2e (opt-in, gated)

The full end-to-end smoke runs `uv run pytest -m e2e --no-cov` against the
public `bigquery-public-data.austin_bikeshare.bikeshare_trips` dataset. The
skill body **forces an explicit user confirmation** before triggering this
path: it checks that `SF_RUN_BQ=1`, `GOOGLE_CLOUD_PROJECT`, and
`ANTHROPIC_API_KEY` are all set, warns about the LLM + warehouse cost (a
single run typically lands well under \$0.15 of Anthropic spend plus
~200–500 MB of BigQuery scan), and only then invokes the gated test. See
[docs/e2e-smoke-test.md](e2e-smoke-test.md) for the maintainer-facing
walkthrough of the same flow.

## Parity gate

A pytest gate at `tests/cli/test_skill_cli_parity.py` parses the live CLI
(every registered subcommand from the `argparse` subparser registry) and
asserts that each subcommand AND the four canonical demo commands
(`signalforge init-demo`, `signalforge generate ... --write`,
`signalforge prune-existing ... --schema`, `signalforge --version`) appear
in `SKILL.md`. The gate runs inside the canonical `VALIDATE_CMD`
(`uv run pytest`), so a CLI change that drifts the surface from the skill
fails validation until `SKILL.md` is updated in the same change.

This is the gate-over-prompt enforcement described in
[`.claude/rules/skill-parity.md`](https://github.com/wjduenow/SignalForge/blob/dev/.claude/rules/skill-parity.md)
— the contributor never has to remember to update SKILL.md; the test
suite makes drift impossible. The gate is mechanical only (subcommand
names + demo command tokens present); reviewer attention still backs prose
accuracy.

## Maintainer-only skills (excluded)

Two skills live at repo-root `.claude/skills/` rather than under `src/`:
`release-manager` (drives the PyPI release flow) and
`review-agentskills-spec` (the maintainer's reference for the
agentskills-spec project). Both are **outside the wheel by construction**
— Hatch's `tool.hatch.build.targets.wheel.packages = ["src/signalforge"]`
declaration only ships the `src/` tree, so a `pip install signalforge-dbt`
user never sees them and `signalforge install-skill` cannot install them.

This intent is documented by a negative assertion in
`tests/test_wheel_packaging.py::test_wheel_excludes_maintainer_only_claude_skills`,
which builds the wheel and asserts neither maintainer-only skill name
appears in the artefact's file list.

## Reference

- [docs/cli-ops.md § `signalforge install-skill`](cli-ops.md#signalforge-install-skill-dest)
  — full flag table, exit-code mapping, stderr shapes for the
  `install-skill` subcommand.
- [docs/e2e-smoke-test.md](e2e-smoke-test.md) — operator walkthrough of
  the live e2e flow the skill's gated demo path triggers.
- [`.claude/rules/skill-parity.md`](https://github.com/wjduenow/SignalForge/blob/dev/.claude/rules/skill-parity.md)
  — contributor rule that documents the SKILL ↔ CLI parity gate.
- [`plans/super/141-claude-skill-install.md`](https://github.com/wjduenow/SignalForge/blob/dev/plans/super/141-claude-skill-install.md)
  — design record (DEC-001 … DEC-024), including the seven SKILL.md body
  sections (DEC-021), the bundled-skill-vs-maintainer-skill split
  (DEC-022), and this docs entry (DEC-023).
