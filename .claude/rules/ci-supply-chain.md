# CI supply-chain conventions

Established by issue #1 scaffolding (DEC-003, DEC-009). Apply to every GitHub Actions workflow in this repo.

## Pin third-party actions to commit SHA, not tag

```yaml
- uses: actions/checkout@<40-char-sha>  # v4.3.1
- uses: actions/setup-python@<40-char-sha>  # v5.6.0
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

## Single Python version for early milestones

DEC-003: lock to one Python version (currently `3.11`) for v0.1 CI. Widen the matrix when the package has real users running on multiple versions.

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
3. **No `if:` gate** — single-Python CI (DEC-003) means no matrix, so no `if: matrix.python-version == '3.13'`-style gate is needed. The upload step runs unconditionally after the pytest step.

The step must land AFTER the pytest step. `coverage.xml` is produced by `--cov-report=xml` in `pyproject.toml` `addopts` — no workflow-level `--cov` flag is needed.

## Reference

`plans/super/1-project-scaffolding.md` — DEC-003, DEC-009. `plans/super/27-codecov-coverage.md` — DEC-006, DEC-007. Issue #57 — `release/*` push trigger. `.github/workflows/ci.yml` — current implementation.
