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

## Reference

`plans/super/1-project-scaffolding.md` — DEC-003, DEC-009. `.github/workflows/ci.yml` — current implementation.
