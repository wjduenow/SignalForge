# Contributing to SignalForge

SignalForge is pre-alpha and designing in the open. The differentiator is the prune step — generation that grades itself against real warehouse data — so the bar for new artifact classes and new code paths is "does this respect signal-over-volume?" Contributions that hold that line are welcome.

## Branching

- Feature branches are `feature/<n>-<short-name>` off `dev` (e.g., `feature/2-bigquery-adapter`).
- PRs land into `dev`. `main` is the released line — only `dev` → `main` merges.

## Local development

The repo is uv-managed. Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv sync --dev
```

uv reads `[dependency-groups].dev` in `pyproject.toml`, picks an interpreter
on the matrix floor (3.11) by default, and writes `uv.lock` (committed).
Contributors without uv can fall back to `pip install -e ".[dev]"` — the
`[project.optional-dependencies].dev` extra is kept in sync.

Validate before pushing (CI runs the same four checks on a 3.11 / 3.12
matrix; pyright is gated to the matrix floor, codecov upload to the ceiling.
3.13 is deferred — see the open Python-3.13 path-safety follow-up issue):

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
```

**Coverage:** see [`docs/codecov-ops.md`](docs/codecov-ops.md) for Codecov setup, badge interpretation, and threshold bumps.

**Docs:** the [published docs site](https://wjduenow.github.io/SignalForge/) is built by MkDocs Material on every push to `main`. Edits to `docs/*.md` and `README.md` land on `dev` like any other PR; the published site picks them up on the next `dev → main` merge. Local preview with `uv run mkdocs serve`. See [`.claude/rules/docs-publishing.md`](.claude/rules/docs-publishing.md) for the full deploy contract.

## Pre-release coverage audit

The default `pytest` run — and therefore the coverage badge — measures only the
default marker set. Tests gated behind `bigquery`, `anthropic`, `cli_subprocess`,
`e2e`, and `wheel_smoke` are filtered out by `addopts` (see
`.claude/rules/testing-signal.md` § "Known gap: excluded markers"), so the
real-network and packaging paths are not instrumented in the badge number.

Before cutting a release, run both suites and combine their coverage into one
total to catch regressions in the gated paths:

```bash
# 1. Default coverage (what the badge reports) — writes a fresh .coverage file:
uv run pytest

# 2. Append the gated-marker run to the SAME .coverage data file.
#    --cov-append combines with run 1 so the term report shows the COMBINED total.
#    --cov-fail-under=0 overrides the 80% gate inherited from addopts — gated
#    markers alone never clear it, and this is a measurement, not a gate.
#    (bigquery/anthropic/e2e need creds; cli_subprocess/wheel_smoke do not.)
SF_RUN_BQ=1 ANTHROPIC_API_KEY=sk-... GOOGLE_CLOUD_PROJECT=<billing-project> \
  uv run pytest -m 'bigquery or anthropic or e2e or cli_subprocess or wheel_smoke' \
  --cov=signalforge --cov-append --cov-fail-under=0 --cov-report=term
```

The combined total from step 2 minus the default badge number from step 1 is
the coverage the gated paths add — typically 5–10%. Interpreting the delta: if
the default badge number drops by M% but the combined total holds steady, that
is likely a redistribution (a code path moved behind a gated marker) rather than
a true regression. A drop in the *combined* total is a real regression worth
chasing before the release goes out.

## Test markers

Tests are tagged with `@pytest.mark.{unit, integration, error}` (declared in
`pyproject.toml`). Run a single category with `pytest -m unit`. New tests
SHOULD use a marker; bare tests are fine for true smoke checks.

## Regenerating fixtures

Fixture regen lives in [`tests/fixtures/README.md`](tests/fixtures/README.md).
v12 is a one-liner against the in-`[dev]` `dbt-core` install; older schemas
(v9 / v10 / v11) use ephemeral `uvx` invocations.

## License

Contributions are Apache-2.0. The repo-level [LICENSE](LICENSE) covers it — do not add per-file license preambles.

## Issues

File issues at https://github.com/wjduenow/SignalForge/issues. v0.1 is design-in-the-open on `dev`; expect the shape of things to move.

## Out of scope for this iteration

`bark`, `/super-plan`, and `bd` are internal tooling, not contributor expectations. Tracked under #13.

## CLI subprocess smoke

`tests/cli/test_subprocess_smoke.py` runs `signalforge --version` via
`subprocess.run` to catch console-script wiring drift that the
in-process `main(argv)` tests cannot. It is gated behind
`@pytest.mark.cli_subprocess` (filtered out by default `addopts`).
Maintainers should run it once before declaring a CLI PR ready
(mirrors the `bigquery` integration-test gate):

```bash
uv run pytest -m cli_subprocess --no-cov
```

## BigQuery integration tests

A small set of tests under `tests/warehouse/test_bigquery_integration.py`
exercises `BigQueryAdapter` against the real `bigquery-public-data`
dataset. They are skipped by default — both via `@pytest.mark.bigquery`
(filtered out by `addopts = -m 'not bigquery'`) and via `skipif(not
SF_RUN_BQ)`.

### Running them locally

1. Configure Application Default Credentials:
   ```bash
   gcloud auth application-default login
   ```

2. Run with the gate:
   ```bash
   SF_RUN_BQ=1 uv run pytest -m bigquery --no-cov
   ```

The tests query `bigquery-public-data.samples.shakespeare` (164K rows,
free under the 1 TB/month BigQuery tier). They are maintainer-only for
v0.1; no CI job runs them.
