---
name: review-agentskills-spec
description: Review the current agentskills.io specification and evaluate whether clauditor's spec-conformance logic needs updates. Use when the user asks to audit upstream spec drift, check for new required fields, or after an agentskills.io announcement. Previews proposed code changes and offers to open a GitHub issue.
compatibility: "Requires network access to fetch https://agentskills.io/specification. Optional: the gh CLI for issue creation."
metadata:
  clauditor-version: "0.0.0-dev"
disable-model-invocation: true
allowed-tools: WebFetch, Read, Grep, Glob, Bash(gh issue create:*)
---

# /review-agentskills-spec — Audit clauditor against the upstream agentskills.io spec

You help the user detect drift between the public
[agentskills.io specification](https://agentskills.io/specification) and
what clauditor currently enforces. End-to-end: fetch the spec, inspect
clauditor's coverage, preview proposed changes, and — only if the user
confirms — open a GitHub issue.

## Scope

- **Read-only** on the clauditor codebase. No file edits.
- **Preview-only** for proposed changes. Nothing lands until the user
  approves a GitHub issue.
- **Targets spec drift**, not general spec compliance of a user's skill.
  Division of labor: **`clauditor lint <SKILL.md>`** checks a user
  skill against the current spec (user-facing conformance); this skill
  audits the upstream spec itself plus Claude Code's frontmatter
  documentation against clauditor's enforcement (maintainer-facing
  drift detection).

## Workflow

1. **Fetch the current spec.** Use `WebFetch` on
   <https://agentskills.io/specification>. Also fetch any linked
   subpages (e.g. the `llms.txt` index) if the spec has changed shape
   since the last audit. Extract every required and optional field,
   format constraint, and validation rule into a mental checklist.

2. **Inventory clauditor's current coverage.** `Read`/`Grep` the
   spec-adjacent modules:
   - `src/clauditor/paths.py` — `SKILL_NAME_RE`, `derive_skill_name`,
     name/parent-dir matching.
   - `src/clauditor/_frontmatter.py` — YAML subset parser and its
     tolerated shapes.
   - `src/clauditor/spec.py` — `SkillSpec.from_file`, layout support
     (modern `<dir>/SKILL.md` vs legacy `<name>.md`).
   - `src/clauditor/conformance.py` — once issue #71 lands, the
     canonical set of static conformance rules.
   - `tests/test_bundled_skill.py` — the frontmatter contract the
     bundled skill pins in place.

   Build an inventory table: `spec rule → clauditor module → current
   behavior`.

3. **Diff spec vs coverage.** For each spec rule, classify clauditor's
   coverage as one of:
   - `matches` — clauditor enforces exactly this rule.
   - `missing` — spec requires it; clauditor does not check.
   - `drifted` — clauditor enforces a different rule (stricter,
     looser, or renamed).
   - `over-enforced` — clauditor checks something the spec does not
     require (not a bug; flag so the user can decide).

4. **Preview proposed changes.** For every non-`matches` row, produce a
   concrete suggestion: the file to touch, the seam for the edit, and
   the rule anchor from `.claude/rules/` that applies (e.g.
   `pure-compute-vs-io-split.md` for new pure helpers,
   `llm-cli-exit-code-taxonomy.md` for new CLI error paths). Preview
   only — do NOT write any files.

   Render the preview as a single markdown report the user can read
   top-to-bottom:

   ```markdown
   ## agentskills.io spec drift report

   Fetched: <URL> (<ISO-8601 timestamp>)

   ### Deltas
   - **<rule name>** (status: missing / drifted / over-enforced)
     - Spec: <what the spec says>
     - Clauditor: <what clauditor does today>
     - Proposed change: <file + seam>
     - Rule anchor: <.claude/rules/...>

   ### No-change rows
   <count> rules match clauditor's current behavior.
   ```

5. **Ask: "Open a GitHub issue for these deltas?"** On yes, run
   `gh issue create` with the preview markdown as the issue body and
   a title of `agentskills.io spec drift: <N> deltas detected`. On no,
   end cleanly with the preview as the final output.

## Common errors

- **`WebFetch failed`** — network error or rate-limit. Retry once;
  then ask the user to paste the spec text if the fetch keeps failing.
- **`gh: command not found`** — the gh CLI is not installed. Offer to
  print the issue body to stdout so the user can paste it into the
  GitHub web UI.
- **`0 deltas detected`** — report that cleanly and skip the
  issue-creation prompt entirely. No drift is a valid outcome.

## Output shape

A single markdown report per run. Keep it terse: one bullet per delta,
one line of proposed change, one line of rule anchor. Long
explanations belong in the GitHub issue body, not the chat response.
