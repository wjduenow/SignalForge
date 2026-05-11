---
name: release-manager
description: Cut a signalforge-dbt release. Test releases publish to TestPyPI via a prerelease GitHub Release; full releases publish to PyPI via a non-prerelease GitHub Release.
compatibility: "Requires: gh CLI, git, pip, uvx (for twine). Must be run from the SignalForge repo root."
metadata:
  signalforge-version: "0.1.0"
disable-model-invocation: true
allowed-tools: Bash(git *), Bash(gh *), Bash(uvx *), Bash(grep *), Bash(cat *), Bash(sleep *), Bash(pip *), Bash(python *), Bash(curl *), Bash(awk *), Bash(rm *), Bash(date *), Read, Edit, Write
---

# /release-manager — Cut a signalforge-dbt release

You help the maintainer cut a release of `signalforge-dbt` (PyPI distribution name; the import package and CLI remain `signalforge`).

SignalForge's publish workflow (`.github/workflows/publish.yml`) routes on the GitHub Release `prerelease` flag, **not** on branch:

- **Prerelease** GitHub Release → `publish-testpypi` job → https://test.pypi.org/project/signalforge-dbt/
- **Non-prerelease** GitHub Release → `publish-pypi` job → https://pypi.org/project/signalforge-dbt/

Both routes use PyPI Trusted Publishers; each index has its own environment (`testpypi` / `pypi`) — these must be configured (or "pending") on the respective index before the first publish.

## Step 0 — Choose release type

Ask the user:
> **Release type?**
> - `test` — publish to TestPyPI as a pre-release (version must have a dev/alpha/beta/rc suffix, e.g. `0.1.0rc1`)
> - `full` — publish to real PyPI as a stable release (clean version, e.g. `0.1.0`)

Record the choice and follow the matching workflow below.

---

## Pre-flight

Run these checks and STOP if any fail — report the problem clearly and do not proceed.

**Branch check (differs by release type):**

- **Test release**: any branch is acceptable, but the tag will point at the current commit. Typically run from a `release/X.Y.Zrc1` branch that has been merged to `main`, or from `main` itself once the release PR is merged.
- **Full release**: `git branch --show-current` must return `main`. Full releases tag `main` HEAD.

**Checks for both modes:**

1. **Clean working tree**: `git status --porcelain` must be empty.
   - Modified/staged entries (`M`, `A`, `D`, `R`) → STOP and ask the user to resolve.
   - Only untracked entries (`??`) → list them and ask: stash (`git stash push -u`), add to `.gitignore`, commit, or inspect first. After the release completes, `git stash pop` if a stash was created.
2. **Up to date with origin**:
   - `git fetch origin {branch} && git status` must show "up to date".
3. **Canonical validation passes** (per `CLAUDE.md`):
   ```bash
   ruff check . && ruff format --check . && pyright && pytest
   ```
4. **CHANGELOG `[Unreleased]` is current**. Read `CHANGELOG.md` and show the user the current `[Unreleased]` section. Ask: "Does this cover everything shipping in this release?" Pause for confirmation — for **full** releases the `[Unreleased]` content gets promoted to `[X.Y.Z]` and used as the GitHub Release body. Empty / stale `[Unreleased]` means an empty / stale release page. If the user wants to edit it, stop here, let them edit, then re-run pre-flight.

**Report a pre-flight summary** — always, even when every check passes:

```
Pre-flight checks:
- Branch (any|main): PASS|FAIL
- Clean working tree: PASS|FAIL
- Up to date with origin: PASS|FAIL
- ruff check / format / pyright / pytest: PASS|FAIL
- CHANGELOG [Unreleased] reviewed: PASS|FAIL
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
python -m build
uvx twine check dist/*
```

Both artifacts must show `PASSED`. STOP and report if either fails.

### Step 2 — Commit if version changed

If the version in `src/signalforge/__init__.py` was not changed (used as-is), skip this step. Otherwise open a short PR — main is the release branch and the prerelease tag will point at `main` HEAD:

```bash
branch=$(git branch --show-current)
git add src/signalforge/__init__.py
git commit -m "chore: bump to {release_version} for test release"
git push origin "$branch"
```

If `$branch` is `main` and `main` has branch protection, instead open a release-bump PR (`release/{release_version}`) and stop here for the user to merge.

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

Confirm `{release_version}` appears. Then install in a clean venv to smoke-test:

```bash
python -m venv /tmp/sf-testpypi-check && \
  /tmp/sf-testpypi-check/bin/pip install \
    --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ \
    "signalforge-dbt=={release_version}" && \
  /tmp/sf-testpypi-check/bin/signalforge --version
```

Report: TestPyPI URL `https://test.pypi.org/project/signalforge-dbt/{release_version}/`.

---

## Full release workflow

> **Note:** `main` typically has branch protection — direct pushes are rejected. The release-version commit and the next-dev bump both ship via PR.

### Step 1 — Bump to release version

Edit `src/signalforge/__init__.py`: set `__version__ = "{release_version}"`.

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
python -m build
uvx twine check dist/*
```

Both artifacts must show `PASSED`. STOP and report if either fails.

### Step 3 — Open release PR

Push the version bump and the CHANGELOG promotion together on a release branch, then open a PR — direct push to `main` is blocked by branch protection.

```bash
git checkout -b release/{release_version}
git add src/signalforge/__init__.py CHANGELOG.md
git commit -m "chore: release {release_version}"
git push -u origin release/{release_version}
gh pr create --base main --head release/{release_version} \
  --title "chore: release {release_version}" \
  --body "Cuts v{release_version} to PyPI. Pre-flight passed (ruff/pyright/pytest); \`python -m build\` + \`uvx twine check\` PASSED on wheel and sdist. CHANGELOG promoted from \`[Unreleased]\`."
```

STOP and ask the user to merge the PR via GitHub. Once merged, continue.

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

### Step 8 — Open next-dev bump PR (target dev)

Edit `src/signalforge/__init__.py`: set `__version__ = "{next_dev_version}"`.

```bash
git checkout dev
git pull origin dev
git checkout -b chore/begin-{next_dev_version}
git add src/signalforge/__init__.py
git commit -m "chore: begin {next_dev_version}"
git push -u origin chore/begin-{next_dev_version}
gh pr create --base dev --head chore/begin-{next_dev_version} \
  --title "chore: begin {next_dev_version}" \
  --body "Bumps version to {next_dev_version} after the v{release_version} release."
```

Ask the user to merge.

### Step 9 — Backmerge main → dev

After both PRs are merged, sync `dev` with the new `main` so the next release starts from the bumped version:

```bash
git checkout dev
git pull origin dev
git checkout -b chore/sync-main-into-dev
git merge origin/main
git push -u origin chore/sync-main-into-dev
gh pr create --base dev --head chore/sync-main-into-dev \
  --title "chore: sync main into dev after {release_version} release" \
  --body "Brings the release commits back to dev."
```

If `dev` allows direct push and the merge fast-forwards cleanly, you may instead `git push origin dev` directly after the local merge — check repo rules first.

---

## Done

Report a summary including:

- Release type and version
- PyPI or TestPyPI URL
- For full releases: confirm the next-dev bump PR (Step 8) and the backmerge PR (Step 9) are open or merged
- If a stash was created during pre-flight, `git stash pop` and confirm the file came back cleanly
