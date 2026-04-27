---
name: release-manager
description: Cut a clauditor-eval release. Test releases run from dev (→ TestPyPI); full releases run from main (→ PyPI).
compatibility: "Requires: uv, gh CLI, git. Must be run from the clauditor repo root."
metadata:
  clauditor-version: "0.1.0"
disable-model-invocation: true
allowed-tools: Bash(git *), Bash(gh *), Bash(uv *), Bash(uvx *), Bash(grep *), Bash(cat *), Bash(sleep *), Bash(pip *), Bash(curl *), Bash(awk *), Bash(rm *), Bash(date *), Read, Edit, Write
---

# /release-manager — Cut a clauditor-eval release

You help the maintainer cut a release of `clauditor-eval`.
- **Test releases** run from the `dev` branch and publish to TestPyPI.
- **Full releases** run from the `main` branch and publish to PyPI. The merge from `dev` → `main` is already done before a full release.

## Step 0 — Choose release type

Ask the user:
> **Release type?**
> - `test` — publish to TestPyPI as a pre-release (version must have a dev/alpha/rc suffix)
> - `full` — publish to real PyPI as a stable release (clean version, e.g. `0.1.0`)

Record the choice and follow the matching workflow below.

---

## Pre-flight

Run these checks and STOP if any fail — report the problem clearly and do not proceed.

**Branch check (differs by release type):**
- **Test release**: `git branch --show-current` must return `dev`
- **Full release**: `git branch --show-current` must return `main`

**Checks for both modes:**
1. **Clean working tree**: `git status --porcelain` must be empty.
   - If the output contains modified/staged entries (`M`, `A`, `D`, `R`), STOP and report — these are real changes the user must resolve.
   - If the output contains ONLY untracked entries (`??` prefix), do NOT auto-stop. List them and ask the user how to proceed: stash (`git stash push -u`), add to `.gitignore`, commit, or inspect first. After the release completes, pop the stash if one was created.
2. **Up to date with origin**:
   - Test: `git fetch origin dev && git status` must show "up to date"
   - Full: `git fetch origin main && git status` must show "up to date"
3. **Tests pass**: `uv run pytest --cov=clauditor --cov-report=term-missing -q`
4. **CHANGELOG `[Unreleased]` is current**. Read `CHANGELOG.md` and show the user the current `[Unreleased]` section. Ask: "Does this cover everything shipping in this release?" Pause for confirmation before continuing — `[Unreleased]` becomes the GitHub Release body via `--notes-file` (full release Step 5), so empty / stale content there means an empty / stale release page. If the user wants to update it, stop here, let them edit, then re-run pre-flight.

**Report a pre-flight summary** — always, even when every check passes. Render it as:

```
Pre-flight checks:
- Branch (dev|main): PASS|FAIL
- Clean working tree: PASS|FAIL
- Up to date with origin: PASS|FAIL
- Tests pass: PASS|FAIL
- CHANGELOG [Unreleased] reviewed: PASS|FAIL
```

Then continue to "Determine version" (on all-PASS) or STOP with the failing check highlighted.

## Determine version

Read `pyproject.toml` and extract the current version.

**For a test release:** version must have a pre-release suffix (`.devN`, `aN`, `bN`, `rcN`).
If the current version is already a pre-release (e.g. `0.1.0.dev3`), use it as the candidate.
If it is a clean version (e.g. `0.1.0`), stop and tell the user to bump to a dev version first.

Then check TestPyPI to make sure the candidate has not already been published — TestPyPI rejects re-uploads of the same version, and discovering this after the GitHub release tag is pushed is painful:

```bash
candidate=$(grep '^version' pyproject.toml | cut -d'"' -f2)
if curl -sf "https://test.pypi.org/pypi/clauditor-eval/${candidate}/json" >/dev/null; then
  echo "Version ${candidate} already on TestPyPI — bump required"
fi
```

If the candidate already exists on TestPyPI, propose the next `.devN` bump (e.g. `0.1.0.dev5` → `0.1.0.dev6`) and ask the user to confirm. On confirmation, edit `pyproject.toml` to set `version = "{next_dev_version}"` and re-run the TestPyPI check against the new candidate (in case that one was also published). If the candidate does **not** exist on TestPyPI, use it as-is — no edit needed.

**For a full release:** strip the pre-release suffix from the current version.
If the current version is already clean (e.g. `0.1.0`), use it as-is.

Show the user:
```
Current version : {current}
Release version : {release}
Next dev version: {next}   ← only shown for full release; ask user to confirm
```

Ask the user to confirm before proceeding.

---

## Test release workflow

### Step 1 — Build and verify
```bash
rm -rf dist/
uv build
uvx twine check dist/*
```
Both artifacts must show `PASSED`. Stop and report if either fails.

### Step 2 — Commit if version changed
If the version in `pyproject.toml` was not changed (used as-is), skip this step.
Otherwise:
```bash
git add pyproject.toml
git commit -m "chore: bump to {release_version} for test release"
git push origin dev
```

### Step 3 — Tag and create GitHub pre-release
```bash
git tag v{release_version}
git push origin v{release_version}
gh release create v{release_version} \
  --title "v{release_version} (pre-release)" \
  --generate-notes \
  --prerelease \
  --repo wjduenow/clauditor
```
The `--prerelease` flag routes the publish workflow to TestPyPI.

### Step 4 — Monitor publish workflow
```bash
gh run watch --repo wjduenow/clauditor
```
Wait for the `publish-testpypi` job to complete. Report any failures.

### Step 5 — Verify on TestPyPI
```bash
sleep 15
pip index versions clauditor-eval --index-url https://test.pypi.org/simple/ 2>/dev/null | head -3
```
Confirm `{release_version}` appears.

Report: TestPyPI URL `https://test.pypi.org/project/clauditor-eval/{release_version}/`

---

## Full release workflow

> **Note:** `main` has GitHub branch protection — direct pushes are rejected. The release-version commit and the next-dev bump both ship via PR.

### Step 1 — Bump to release version
Edit `pyproject.toml`: set `version = "{release_version}"`. Then refresh the lock file so `uv.lock` matches:
```bash
uv sync
```

### Step 1b — Promote CHANGELOG `[Unreleased]` to `[{release_version}]`
Edit `CHANGELOG.md` so this release has its own dated section that the GitHub Release body can quote verbatim:

1. Insert a new dated header **directly after** the existing `## [Unreleased]` line:
   ```
   ## [Unreleased]

   ## [{release_version}] - {today_iso}
   ```
   where `{today_iso}` = `date +%Y-%m-%d`. Do **not** move the existing entries — leaving them under `[{release_version}]` is exactly the desired result; the new empty `[Unreleased]` above is what future entries land in.
2. Update the bottom reference link table:
   - Change the `[Unreleased]` link to `compare/v{release_version}...HEAD`.
   - Add `[{release_version}]: https://github.com/wjduenow/clauditor/releases/tag/v{release_version}` directly below it.

### Step 2 — Build and verify
```bash
rm -rf dist/
uv build
uvx twine check dist/*
```
Both artifacts must show `PASSED`. Stop and report if either fails.

### Step 3 — Open release PR
Push the version bump and the CHANGELOG promotion together on a release branch, then open a PR — direct push to `main` is blocked by branch protection.
```bash
git checkout -b release/{release_version}
git add pyproject.toml uv.lock CHANGELOG.md
git commit -m "chore: release {release_version}"
git push -u origin release/{release_version}
gh pr create --base main --head release/{release_version} \
  --title "chore: release {release_version}" \
  --body "Cuts v{release_version} to PyPI. Pre-flight tests pass; \`uv build\` + \`uvx twine check\` PASSED on both wheel and sdist. CHANGELOG promoted from \`[Unreleased]\`."
```
Stop and ask the user to merge the PR via GitHub. Once merged, continue.

### Step 4 — Pull main, tag, push tag
```bash
git checkout main
git pull origin main
git tag v{release_version}      # tags origin/main HEAD (the merge commit)
git push origin v{release_version}
```

### Step 5 — Create GitHub Release
Extract the just-promoted CHANGELOG section into a temp file and use it as the release body:
```bash
awk -v ver="{release_version}" '
  $0 ~ "^## \\["ver"\\] -" { capturing=1; next }
  capturing && /^## \[/ { exit }
  capturing { print }
' CHANGELOG.md > .release-notes.md

gh release create v{release_version} \
  --title "v{release_version}" \
  --notes-file .release-notes.md \
  --repo wjduenow/clauditor

rm .release-notes.md
```
No `--prerelease` flag — this routes to PyPI. `--notes-file` (not `--generate-notes`) uses the curated CHANGELOG section verbatim instead of an auto-generated PR list.

### Step 6 — Monitor publish workflow
```bash
gh run watch --repo wjduenow/clauditor
```
Wait for the `publish-pypi` job to complete. Report any failures.

### Step 7 — Verify on PyPI
```bash
curl -sf "https://pypi.org/pypi/clauditor-eval/{release_version}/json" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('version:', d['info']['version'])"
```
Confirm `{release_version}` appears.

### Step 8 — Open next-dev bump PR
Edit `pyproject.toml`: set `version = "{next_dev_version}"`. Refresh the lock, then push via PR:
```bash
uv sync
git checkout -b chore/begin-{next_dev_version}
git add pyproject.toml uv.lock
git commit -m "chore: begin {next_dev_version}"
git push -u origin chore/begin-{next_dev_version}
gh pr create --base main --head chore/begin-{next_dev_version} \
  --title "chore: begin {next_dev_version}" \
  --body "Bumps version to {next_dev_version} after the v{release_version} release."
```
Ask the user to merge.

### Step 9 — Backmerge main → dev
After both PRs are merged, sync `dev` with the new `main` so the next test release starts from the bumped version. Open a backmerge PR:
```bash
git checkout dev
git pull origin dev
git checkout -b chore/sync-main-into-dev
git merge origin/main
git push -u origin chore/sync-main-into-dev
gh pr create --base dev --head chore/sync-main-into-dev \
  --title "chore: sync main into dev after {release_version} release" \
  --body "Brings the release commits and the {next_dev_version} bump back to dev."
```
If `dev` allows direct push and the merge fast-forwards cleanly, you may instead `git push origin dev` after the merge — check repo rules first.

---

## Done

Report a summary including:
- Release type and version
- PyPI or TestPyPI URL
- For full releases: confirm the next-dev bump PR (Step 8) and the backmerge PR (Step 9) are open or merged
- If a stash was created during pre-flight, run `git stash pop` and confirm the file came back cleanly
