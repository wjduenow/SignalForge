# Codecov coverage reporting — operations guide

Operational reference for Codecov integration in SignalForge CI.
Companion to the CI workflow at
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) and the design
record in [`plans/super/27-codecov-coverage.md`](../plans/super/27-codecov-coverage.md).

## Operator setup

To enable Codecov uploads for the repository:

1. Go to [codecov.io](https://codecov.io) and navigate to Settings for the
   `wjduenow/SignalForge` repository. Copy the **Repository Upload Token**.
2. In the GitHub repository, go to **Settings > Secrets and variables >
   Actions > New repository secret**.
3. Create a secret named `CODECOV_TOKEN` and paste the token value.

The CI workflow references `secrets.CODECOV_TOKEN` in the upload step.
Without it, coverage uploads silently fail (see "Fork-PR upload failures"
below).

## Reading the badge

The coverage badge in `README.md` reports the percentage for the `dev`
branch, not `main`. The `main` branch README will show a stale or unknown
badge until the next `dev` to `main` merge that includes a CI run with a
successful Codecov upload.

## Reading the per-PR comment

Codecov automatically posts a comment on pull requests showing the coverage
delta between the PR head and the base branch. Key elements:

- **Overall project coverage change** — the +/- percentage relative to the
  base branch.
- **File-level breakdown** — which files gained or lost coverage lines.
- **Patch coverage** — percentage of newly added or modified lines that are
  covered by tests.

A negative delta does not necessarily block merge; it signals that new or
changed code paths lack test coverage. Review the file-level annotations to
decide whether the gap is acceptable.

## Bumping the threshold

The coverage floor is enforced by `--cov-fail-under=<N>` in `pyproject.toml`
under `[tool.pytest.ini_options] addopts`. The canonical procedure:

1. Run `pytest --cov=signalforge --cov-report=term` twice (two independent
   runs guard against flaky ordering effects).
2. Pick `floor(min(run_1, run_2, 80))` as the new threshold.
3. Update `--cov-fail-under=<N>` in `pyproject.toml` `addopts`.
4. Revisit when actual coverage exceeds `<N> + 5` for two consecutive `dev`
   builds.

The floor of 80 is a practical ceiling for v0.1 — pushing higher risks
churn on test-only changes that don't improve signal.

## Known gap: excluded markers

Coverage measures only the default pytest set. Tests gated behind these
markers are excluded by `addopts`:

- `bigquery` — real BigQuery integration tests (`SF_RUN_BQ=1`).
- `anthropic` — real Anthropic API tests.
- `cli_subprocess` — console-script wiring smoke test.

Those code paths are exercised via fakes in unit tests, but the
real-network paths (authentication, retry against live services, console
script spawning) are not instrumented in the coverage report. v0.2 may add
a gated coverage-append job that merges marker-gated runs into the main
report.

Because `--cov-fail-under` is in `addopts`, marker-specific runs (e.g.,
`pytest -m cli_subprocess` or `pytest -m bigquery`) will fail the coverage
gate — only a small fraction of the codebase is exercised. Use `--no-cov`
to disable coverage instrumentation for these runs:

```bash
pytest -m cli_subprocess --no-cov
SF_RUN_BQ=1 pytest -m bigquery --no-cov
```

## Fork-PR upload failures

Fork PRs triggered via `pull_request` do not receive `secrets.CODECOV_TOKEN`
(GitHub does not expose repository secrets to fork-originated workflows).
The Codecov upload step silently fails in this case. The workflow sets
`fail_ci_if_error: false` so CI does not break on the missing token. This
is expected behaviour — fork contributors see a passing CI run without a
coverage comment; maintainers re-run the PR from the upstream branch if a
coverage report is needed.

## Local `--cov-fail-under` gating

The `--cov-fail-under` flag lives in `pyproject.toml` `addopts`, so it runs
locally too. Running `pytest` locally will fail if coverage drops below the
threshold. This matches the canonical validation command in `CLAUDE.md`:

```bash
pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest
```

To skip the coverage gate temporarily (e.g., during a focused TDD loop),
override addopts:

```bash
pytest -o "addopts=-ra --strict-markers --import-mode=importlib -m 'not bigquery and not anthropic and not cli_subprocess'"
```

This drops the `--cov*` flags while preserving markers and strict mode.
