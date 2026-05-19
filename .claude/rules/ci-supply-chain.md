# CI supply-chain conventions

Established by issue #1 scaffolding (DEC-003, DEC-009). Apply to every GitHub Actions workflow in this repo.

## Pin third-party actions to commit SHA, not tag

```yaml
- uses: actions/checkout@<40-char-sha>  # v4.3.1
- uses: astral-sh/setup-uv@<40-char-sha>  # v8.1.0
- uses: codecov/codecov-action@<40-char-sha>  # v5.5.4
- uses: pypa/gh-action-pypi-publish@<40-char-sha>  # v1.14.0
```

The trailing `# vX.Y.Z` comment is what reviewers and Dependabot read; the SHA is what GitHub Actions executes. Tags can be force-moved; SHAs cannot. Look up the SHA via:

```bash
gh api repos/actions/checkout/git/refs/tags/v4 --jq '.object.sha'
```

If the tag is annotated, dereference once more via `git/tags/<sha>`. Either way, what lands in the workflow must be 40 hex chars.

## Scope GITHUB_TOKEN at workflow level

```yaml
permissions:
  contents: read
```

Default-deny everything; expand per-job only when explicitly needed. Never use `pull_request_target` for fork-safe CI — it exposes write tokens.

## Concurrency: cancel superseded runs

```yaml
concurrency:
  group: <workflow>-${{ github.ref }}
  cancel-in-progress: true
```

Saves runner minutes and surfaces the latest result faster.

## Python matrix: 3.11 / 3.12 (uv migration)

Originally DEC-003 locked CI to a single Python version (3.11) for v0.1 — the matrix widening was deferred to "when the package has real users running on multiple versions." The uv migration graduated this: `astral-sh/setup-uv` fetches missing interpreters in seconds, so a multi-version matrix costs ~2x runner minutes for a cleanly worth-it signal (PEP 604 / match-statement / type-param syntax issues catch earlier).

The matrix runs ruff + pytest on every Python version. Two steps are gated to one iteration:

- **Pyright** runs only when `matrix.python-version == '3.11'` — pyright's own `pythonVersion = "3.11"` setting pins the type-check to the floor (`python-build.md` issue #46); running it on 3.12 adds no signal.
- **Codecov upload** runs only when `matrix.python-version == '3.12'` (the current matrix ceiling) — coverage is interpreter-invariant for this codebase, so the choice is conventional. When 3.13 returns to the matrix, flip the gate back to 3.13.

**3.13 is deferred.** Python 3.13 changed `Path.resolve()` to raise `OSError(errno.ELOOP)` instead of `RuntimeError` on cyclic symlinks; six tests under `tests/_common/`, `tests/diff/`, `tests/grade/`, `tests/manifest/` rely on the 3.11/3.12 shape. A follow-up issue tracks the catch-site updates in `signalforge/_common/path_safety.py` + `signalforge/manifest/loader.py`; once those land, 3.13 returns to the matrix.

If a future ticket bumps the matrix floor (e.g., to 3.12), update `requires-python` + `pyright.pythonVersion` + the matrix in lockstep per `python-build.md` issue #46.

## CI triggers cover every long-lived branch

Established by issue #57. `on.push.branches` includes `main`, `dev`, AND `release/*`. Release-prep commits (tag-prep, release-note fixes, hotfix backports) land directly on a `release/*` branch and must get CI feedback. Without the `release/*` glob, a direct push to a release branch bypasses CI entirely; PRs targeting `main` still trigger via the `pull_request` branch list, but ad-hoc commits on the release branch do not.

When a new long-lived branch shape lands (e.g., `hotfix/*` in v0.3), add it to BOTH `on.push.branches` AND `on.pull_request.branches` in lockstep. Short-lived feature branches don't need triggers — PRs targeting `main` / `dev` already cover them.

## Codecov coverage upload

Established by issue #27 (DEC-006, DEC-007). The upload step follows the same SHA-pinning rule as other third-party actions:

```yaml
- name: Upload coverage to Codecov
  uses: codecov/codecov-action@<40-char-sha>  # v5.X.Y
  with:
    files: coverage.xml
    fail_ci_if_error: false
  env:
    CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }}
```

Three load-bearing details:

1. **SHA-pin the action** — same `gh api repos/codecov/codecov-action/git/refs/tags/v5` lookup as other actions. Dereference annotated tags. The trailing `# v5.X.Y` comment is for reviewers; the SHA is what executes.
2. **`fail_ci_if_error: false`** — required for fork-safe CI. Fork PRs via `pull_request` do not receive `secrets.CODECOV_TOKEN` (GitHub strips repository secrets from fork-originated workflows). The upload silently fails; `fail_ci_if_error: false` prevents CI failure on the missing token. This is expected behaviour, not a bug.
3. **Gate on `matrix.python-version == '3.13'`** — the upload runs from the matrix ceiling iteration only, so coverage doesn't double-upload (codecov rejects duplicate uploads for the same commit). The choice of 3.13 over 3.11 is conventional; either matrix endpoint works.

The step must land AFTER the pytest step. `coverage.xml` is produced by `--cov-report=xml` in `pyproject.toml` `addopts` — no workflow-level `--cov` flag is needed.

## Reference

`plans/super/1-project-scaffolding.md` — DEC-003, DEC-009. `plans/super/27-codecov-coverage.md` — DEC-006, DEC-007. Issue #57 — `release/*` push trigger. `.github/workflows/ci.yml` — current implementation.
