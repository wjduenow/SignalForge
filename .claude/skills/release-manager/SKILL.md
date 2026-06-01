---
name: release-manager
description: Cut a signalforge-dbt release. Test releases publish to TestPyPI via a prerelease GitHub Release; full releases publish to PyPI via a non-prerelease GitHub Release.
compatibility: "Requires: gh CLI, git, uv (build + validation), uvx (for twine), pip (clean-room TestPyPI install test). Must be run from the SignalForge repo root."
metadata:
  signalforge-version: "0.5.0"
disable-model-invocation: true
allowed-tools: Bash(git *), Bash(gh *), Bash(uv *), Bash(uvx *), Bash(grep *), Bash(cat *), Bash(sleep *), Bash(pip *), Bash(python *), Bash(curl *), Bash(awk *), Bash(rm *), Bash(date *), Read, Edit, Write
---

# /release-manager — Cut a signalforge-dbt release

You help the maintainer cut a release of `signalforge-dbt` (PyPI distribution name; the import package and CLI remain `signalforge`).

SignalForge cuts releases from two branches — **TestPyPI from `dev`, real PyPI from `main`**:

- **Test release** — cut from **`dev`**, published as a **prerelease** GitHub Release → `publish-testpypi` job → https://test.pypi.org/project/signalforge-dbt/
- **Full release** — cut from **`main`**, published as a **non-prerelease** GitHub Release → `publish-pypi` job → https://pypi.org/project/signalforge-dbt/

The publish workflow (`.github/workflows/publish.yml`) is the technical switch — it routes on the GitHub Release `prerelease` flag. The branch each release is cut from is the maintainer discipline this skill enforces in pre-flight: a test release tags `dev` HEAD and a full release tags `main` HEAD. The two line up — `dev` is the in-development line that feeds TestPyPI; `main` is the released line that feeds PyPI.

Both routes use PyPI Trusted Publishers; each index has its own environment (`testpypi` / `pypi`) — these must be configured (or "pending") on the respective index before the first publish.

## Step 0 — Choose release type

Ask the user:
> **Release type?**
> - `test` — publish to TestPyPI as a pre-release, cut from **`dev`** (version must have a dev/alpha/beta/rc suffix, e.g. `0.1.0rc1`)
> - `full` — publish to real PyPI as a stable release, cut from **`main`** (clean version, e.g. `0.1.0`)

Record the choice and follow the matching workflow below.

---

## Pre-flight

Run these checks and STOP if any fail — report the problem clearly and do not proceed.

**Branch check (differs by release type):**

- **Test release**: `git branch --show-current` must return `dev`. Test releases tag `dev` HEAD and publish to TestPyPI. If the user is on another branch, STOP and ask them to `git checkout dev` (and ensure the rc commit is on `dev`) first.
- **Full release**: normally `git branch --show-current` returns `main`. But when **dev is ahead of main with the release content** (common — main was last released as v(N-1), dev carries v(N) work), starting on `main` produces an empty release branch with no v(N) content. Detect via `git log --oneline origin/main..origin/dev`: any output → dev-ahead case. Proceed from `dev` instead, and document the deviation in the pre-flight summary. Step 3 ("Open release PR") covers the dev-ahead branching shape (base `release/X.Y.Z` on dev OR on main + cherry-pick of the post-release-N-1 commits — see Step 3 § "Dev-ahead-of-main shape").

**Checks for both modes:**

1. **Clean working tree**: `git status --porcelain` must be empty.
   - Modified/staged entries (`M`, `A`, `D`, `R`) → STOP and ask the user to resolve.
   - Only untracked entries (`??`) → list them and ask: stash (`git stash push -u`), add to `.gitignore`, commit, or inspect first. After the release completes, `git stash pop` if a stash was created.
2. **Up to date with origin**:
   - `git fetch origin {branch} && git status` must show "up to date".
3. **Canonical validation passes** (per `CLAUDE.md`):
   ```bash
   uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
   ```
4. **CHANGELOG `[Unreleased]` is current**. Read `CHANGELOG.md` and show the user the current `[Unreleased]` section. Ask: "Does this cover everything shipping in this release?" Pause for confirmation — for **full** releases the `[Unreleased]` content gets promoted to `[X.Y.Z]` and used as the GitHub Release body. Empty / stale `[Unreleased]` means an empty / stale release page. If the user wants to edit it, stop here, let them edit, then re-run pre-flight.
5. **Stale-`[Unreleased]` carryover guard** (full releases only — load-bearing, see Step 9). Compare `[Unreleased]` against the existing `[X.(Y-1).0]` section. If they are byte-identical, the previous release's backmerge didn't clear `[Unreleased]` — promoting it now would publish v(N-1)'s release notes verbatim as v(N), and the release PR will conflict at every CHANGELOG line on merge into main. Detect with:
   ```bash
   awk '/^## \[Unreleased\]/{p=1; next} /^## \[/{p=0} p' CHANGELOG.md > /tmp/sf-unreleased.txt
   awk -v ver="$(grep '^## \[0\.' CHANGELOG.md | sed -n 2p | sed -E 's/^## \[([^]]+)\].*/\1/')" \
       '$0 ~ "^## \\["ver"\\] "{p=1; next} /^## \[/{p=0} p' CHANGELOG.md > /tmp/sf-prev.txt
   diff -q /tmp/sf-unreleased.txt /tmp/sf-prev.txt
   ```
   If `diff` reports "identical" (no output), STOP and surface the issue: dev's `[Unreleased]` is a stale duplicate of the previous release. The user must either edit `[Unreleased]` to reflect what's actually new in v(N) (drop the v(N-1)-duplicate bullets) OR confirm the release truly contains nothing new and a separate decision is needed. Don't proceed until resolved.

**Report a pre-flight summary** — always, even when every check passes:

```
Pre-flight checks:
- Branch (test→dev | full→main, or dev-ahead-of-main deviation): PASS|FAIL
- Clean working tree: PASS|FAIL
- Up to date with origin: PASS|FAIL
- ruff check / format / pyright / pytest: PASS|FAIL
- CHANGELOG [Unreleased] reviewed: PASS|FAIL
- CHANGELOG [Unreleased] not a stale duplicate of previous release (full only): PASS|FAIL
```

Then continue to "Determine version" (on all-PASS) or STOP with the failing check highlighted.

## Determine version

The single source of truth for the version is `src/signalforge/__init__.py`:

```bash
candidate=$(grep '^__version__' src/signalforge/__init__.py | cut -d'"' -f2)
```

**For a test release:** version must have a pre-release suffix (`rcN`, `aN`, `bN`, `.devN`). If the current version is already a pre-release (e.g. `0.1.0rc1`), use it as the candidate. If it is a clean version (e.g. `0.1.0`), STOP and tell the user to bump to a pre-release version first.

TestPyPI rejects re-uploads of the same `(name, version)`. Check before tagging:

```bash
if curl -sf "https://test.pypi.org/pypi/signalforge-dbt/${candidate}/json" >/dev/null; then
  echo "Version ${candidate} already on TestPyPI — bump required"
fi
```

If the candidate already exists on TestPyPI, propose the next bump (e.g. `0.1.0rc1` → `0.1.0rc2`) and ask the user to confirm. On confirmation, edit `src/signalforge/__init__.py` to set `__version__ = "{next_rc_version}"` and re-run the TestPyPI check.

**For a full release:** strip the pre-release suffix. If the current version is already clean (e.g. `0.1.0`), use it as-is.

Show the user:

```
Current version : {current}
Release version : {release}
Next dev version: {next}   ← only shown for full release; ask user to confirm
```

`{next}` is the upcoming dev version (e.g. release `0.1.0` → next dev `0.2.0.dev0`). Ask the user to confirm before proceeding.

---

## Test release workflow

### Step 1 — Build and verify

```bash
rm -rf dist/
uv build
uvx twine check dist/*
```

Both artifacts must show `PASSED`. STOP and report if either fails.

### Step 2 — Commit if version changed

If the version in `src/signalforge/__init__.py` was not changed (used as-is), skip this step. Otherwise commit the bump to `dev` — test releases are cut from `dev` and the prerelease tag points at `dev` HEAD:

```bash
git add src/signalforge/__init__.py
git commit -m "chore: bump to {release_version} for test release"
git push origin dev
```

If `dev` has branch protection that blocks direct pushes, open a release-bump PR (`release/{release_version}`) targeting `dev` instead and stop here for the user to merge.

### Step 3 — Tag and create GitHub pre-release

```bash
git tag v{release_version}
git push origin v{release_version}
gh release create v{release_version} \
  --title "v{release_version} (pre-release)" \
  --generate-notes \
  --prerelease \
  --repo wjduenow/SignalForge
```

The `--prerelease` flag is what routes `publish.yml` to the `publish-testpypi` job.

### Step 4 — Monitor publish workflow

```bash
gh run watch --repo wjduenow/SignalForge
```

Wait for the `publish-testpypi` job to complete. Report any failures (most common: trusted-publisher pending entry not yet configured on test.pypi.org, or the (name, version) already exists).

### Step 5 — Verify on TestPyPI

```bash
sleep 15
pip index versions signalforge-dbt --index-url https://test.pypi.org/simple/ 2>/dev/null | head -3
```

Confirm `{release_version}` appears. Then install in a clean venv to smoke-test.

**The clean-room interpreter MUST be ≥3.11** — `signalforge-dbt` declares `requires-python = ">=3.11"`, so `pip` on a 3.10-or-older interpreter silently *ignores* the new release ("Could not find a version that satisfies …", only the pre-3.11-floor `0.1.0rc1` installs). A bare `python -m venv` picks up whatever `python3` is on PATH, which may be 3.10. Provision the interpreter explicitly via `uv`:

```bash
rm -rf /tmp/sf-testpypi-check
uv venv --python 3.11 /tmp/sf-testpypi-check
uv pip install --python /tmp/sf-testpypi-check/bin/python --no-cache \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  "signalforge-dbt=={release_version}"
/tmp/sf-testpypi-check/bin/signalforge --version
```

Pin **3.11** (the `requires-python` floor) so the smoke test verifies installability on the *minimum* supported interpreter. `uv venv --python 3.11` auto-fetches 3.11 if it isn't already present, so this works regardless of the host's default `python3`.

If the install fails with "Could not find a version that satisfies the requirement" right after a successful publish, it's almost always TestPyPI's simple-index (Fastly) cache lagging the upload by a minute or two — confirm the artifact exists via the per-version JSON (`curl -sf "https://test.pypi.org/pypi/signalforge-dbt/{release_version}/json"`), wait, and retry. (Distinguish this from the Python-version mismatch above: the version-mismatch error names "Requires-Python >=3.11" in the pip output; the cache-lag error just shows an older `(from versions: …)` list.)

Report: TestPyPI URL `https://test.pypi.org/project/signalforge-dbt/{release_version}/`.

---

## Full release workflow

> **Note:** `main` typically has branch protection — direct pushes are rejected. The release-version commit and the next-dev bump both ship via PR.

### Step 1 — Bump to release version

Edit `src/signalforge/__init__.py`: set `__version__ = "{release_version}"`.

**Bump skill metadata in lockstep.** The bundled Claude Code skill carries the version stamp in two places that mirror `__version__`. Grep + edit both so the shipped wheel's skill metadata matches the release:

```bash
grep -rln 'signalforge-version\|"version":' src/signalforge/skills/ 2>/dev/null
```

Expected hits (as of v0.5.0):
- `src/signalforge/skills/signalforge/SKILL.md` — frontmatter `  signalforge-version: "{old}"` → `"{release_version}"`
- `src/signalforge/skills/signalforge/assets/SKILL.eval.json` — `"version": "{old}"` → `"{release_version}"`

A new bundled skill in v0.X may add more — the grep is the source of truth. Bumping these in lockstep is what keeps the wheel's `--version` reading and `SKILL.md`'s self-reported version honest.

### Step 1b — Promote CHANGELOG `[Unreleased]` to `[{release_version}]`

Edit `CHANGELOG.md` so this release has its own dated section the GitHub Release body can quote verbatim:

1. Insert a new dated header **directly after** the existing `## [Unreleased]` line:
   ```
   ## [Unreleased]

   _Nothing yet — entries land here on `dev` and get promoted to a dated section at release time._

   ## [{release_version}] — {today_iso}
   ```
   where `{today_iso}` = `date +%Y-%m-%d`. Leave the existing entries in place under the new `[{release_version}]` header.
2. Update the bottom reference-link table:
   - Change the `[Unreleased]` link to `compare/v{release_version}...HEAD`.
   - Add `[{release_version}]: https://github.com/wjduenow/SignalForge/releases/tag/v{release_version}` directly below it.

### Step 2 — Build and verify

```bash
rm -rf dist/
uv build
uvx twine check dist/*
```

Both artifacts must show `PASSED`. STOP and report if either fails.

### Step 3 — Open release PR

Push the version bump, the CHANGELOG promotion, AND the skill-metadata bumps from Step 1 together on a release branch, then open a PR — direct push to `main` is blocked by branch protection.

**Two branching shapes depending on whether dev is ahead of main:**

**(a) Normal case (main has the release content)** — branch from main:

```bash
git checkout -b release/{release_version}
git add src/signalforge/__init__.py CHANGELOG.md src/signalforge/skills/
git commit -m "chore: release {release_version}"
git push -u origin release/{release_version}
```

**(b) Dev-ahead-of-main case** — branch from main + cherry-pick the post-v(N-1) commits from dev (or, when that's a single squash-merge commit, just cherry-pick it):

```bash
git checkout main && git pull origin main
git checkout -b release/{release_version}
# Find the dev commits not on main:
git log --oneline origin/main..origin/dev
# Cherry-pick the one(s) that add v(N) content (often a single squash-merge):
git cherry-pick <sha>
# Then bump version + promote CHANGELOG + bump skill metadata as in Step 1:
# (edit src/signalforge/__init__.py, CHANGELOG.md, and the skill metadata files)
git add src/signalforge/__init__.py CHANGELOG.md src/signalforge/skills/
git commit -m "chore: release {release_version}"
git push -u origin release/{release_version}
```

Verify the cherry-pick reproduces dev's tree: `git diff release/{release_version} origin/dev --stat` should show only `CHANGELOG.md` + `src/signalforge/__init__.py` + the skill metadata files (i.e. only your release-prep edits). If more files differ, dev has work the cherry-pick missed — extend the cherry-pick or rebuild the branch.

**Common to both shapes — open the PR:**

```bash
gh pr create --base main --head release/{release_version} \
  --title "chore: release {release_version}" \
  --body "Cuts v{release_version} to PyPI. Pre-flight passed (ruff/pyright/pytest); \`uv build\` + \`uvx twine check\` PASSED on wheel and sdist. CHANGELOG promoted from \`[Unreleased]\`."
```

STOP and ask the user to merge the PR via GitHub. Expect codecov + CodeRabbit comments on this main-targeting PR — both run on PRs into main, neither on PRs into dev. Once merged, continue.

### Step 4 — Pull main, tag, push tag

```bash
git checkout main
git pull origin main
git tag v{release_version}      # tags main HEAD (the merge commit)
git push origin v{release_version}
```

### Step 5 — Create GitHub Release (non-prerelease)

Extract the just-promoted CHANGELOG section into a temp file and use it as the release body:

```bash
awk -v ver="{release_version}" '
  $0 ~ "^## \\["ver"\\] " { capturing=1; next }
  capturing && /^## \[/ { exit }
  capturing { print }
' CHANGELOG.md > .release-notes.md

gh release create v{release_version} \
  --title "v{release_version}" \
  --notes-file .release-notes.md \
  --repo wjduenow/SignalForge

rm .release-notes.md
```

No `--prerelease` flag — this routes `publish.yml` to the `publish-pypi` job. `--notes-file` (not `--generate-notes`) uses the curated CHANGELOG section verbatim instead of an auto-generated PR list.

### Step 6 — Monitor publish workflow

```bash
gh run watch --repo wjduenow/SignalForge
```

Wait for the `publish-pypi` job to complete. Report any failures (most common: trusted-publisher pending entry not yet configured on pypi.org with environment `pypi`).

### Step 7 — Verify on PyPI

```bash
curl -sf "https://pypi.org/pypi/signalforge-dbt/{release_version}/json" \
  | python -c "import json,sys; d=json.load(sys.stdin); print('version:', d['info']['version'])"
```

Confirm `{release_version}` appears.

### Steps 8 + 9 — Combined dev PR (next-dev bump + backmerge + CHANGELOG cleanup)

After the release publishes, dev needs three things and they fit naturally in one PR:

1. **Bump `__version__`** to `{next_dev_version}` (e.g. release `0.5.0` → next dev `0.6.0.dev0`).
2. **Bump skill metadata** in lockstep — same files as Step 1 (`SKILL.md` frontmatter + `assets/SKILL.eval.json`), now to `{next_dev_version}`.
3. **Backmerge main → dev** so dev = main (v{release_version}) + future work, and **clear dev's stale `[Unreleased]` carryover** via `git checkout --theirs CHANGELOG.md` (load-bearing — see below).

```bash
git checkout dev
git pull origin dev
git checkout -b chore/post-{release_version}-bump-and-backmerge
git merge origin/main --no-edit
```

The merge will conflict on `src/signalforge/__init__.py` (dev has `{release_version}.dev0`, main has `{release_version}`), the skill metadata files (same shape), and `CHANGELOG.md` (dev's stale `[Unreleased]` vs main's promoted-and-cleared shape). Resolve as follows:

```bash
# __init__.py — write the next-dev value (not ours, not theirs)
cat > src/signalforge/__init__.py <<EOF
"""SignalForge: LLM-drafted, warehouse-pruned dbt artifacts."""

__version__ = "{next_dev_version}"
EOF

# Skill metadata — same treatment (find the conflict markers, write next-dev value)
# Each file has one or two lines with conflict markers around the version string

# CHANGELOG.md — LOAD-BEARING: take main's version verbatim
git checkout --theirs CHANGELOG.md
```

**Why `git checkout --theirs CHANGELOG.md` is load-bearing:** main's post-release CHANGELOG has the empty `[Unreleased]` "Nothing yet" placeholder + correct descending-version order. Dev's version still carries the previous release's `[Unreleased]` content (because the prior cycle's backmerge didn't clear it) AND a duplicate `[X.(Y-1).0]` section from its own promotion. Trying to hand-merge yields a mess. Taking main's CHANGELOG resets dev to the clean shape, which is exactly what prevents the **stale-`[Unreleased]` carryover trap** the pre-flight item 5 guards against next release.

```bash
git add -A
# Re-run canonical validation to confirm the resolution is consistent:
uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest

git commit -m "chore: begin {next_dev_version} + backmerge main into dev (post-{release_version})"
git push -u origin chore/post-{release_version}-bump-and-backmerge
gh pr create --base dev --head chore/post-{release_version}-bump-and-backmerge \
  --title "chore: begin {next_dev_version} + backmerge main into dev (post-{release_version})" \
  --body "Combined post-release housekeeping: bump __version__ + skill metadata to {next_dev_version}, backmerge v{release_version} content from main, clear dev's stale [Unreleased] CHANGELOG carryover by taking main's post-release shape verbatim."
```

Ask the user to merge.

**Why combined, not separate:** the older separate-PR shape (one PR for the version bump, one PR for the backmerge) works when dev is NOT ahead of main, but loses signal in the common case where dev IS ahead — the two PRs touch the same files, the second's conflict resolution depends on the first being merged first, and the CHANGELOG cleanup naturally belongs with the backmerge. One PR is more honest about what's happening.

---

## Done

Report a summary including:

- Release type and version
- PyPI or TestPyPI URL
- For full releases: confirm the combined Steps 8 + 9 dev PR (next-dev bump + backmerge + CHANGELOG cleanup) is open or merged
- If a stash was created during pre-flight, `git stash pop` and confirm the file came back cleanly
