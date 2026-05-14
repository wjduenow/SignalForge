# Contributing to SignalForge

SignalForge is pre-alpha and designing in the open. The differentiator is the prune step — generation that grades itself against real warehouse data — so the bar for new artifact classes and new code paths is "does this respect signal-over-volume?" Contributions that hold that line are welcome.

## Branching

- Feature branches are `feature/<n>-<short-name>` off `dev` (e.g., `feature/2-bigquery-adapter`).
- PRs land into `dev`. `main` is the released line — only `dev` → `main` merges.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # quoted for zsh; bash also accepts this form
```

Validate before pushing:

```bash
ruff check . && ruff format --check . && pyright && pytest
```

**Coverage:** see [`docs/codecov-ops.md`](docs/codecov-ops.md) for Codecov setup, badge interpretation, and threshold bumps.

## Pre-release coverage audit

The default `pytest` run — and therefore the coverage badge — measures only the
default marker set. Tests gated behind `bigquery`, `anthropic`, `cli_subprocess`,
`e2e`, and `wheel_smoke` are filtered out by `addopts` (see
`.claude/rules/testing-signal.md` § "Known gap: excluded markers"), so the
real-network and packaging paths are not instrumented in the badge number.

Before cutting a release, run the gated-marker suite once to catch coverage
regressions in those paths:

```bash
# Default coverage (what the badge reports):
pytest

# Gated-marker coverage (bigquery/anthropic/e2e need creds; cli_subprocess/wheel_smoke do not):
SF_RUN_BQ=1 ANTHROPIC_API_KEY=sk-... GOOGLE_CLOUD_PROJECT=<billing-project> \
  pytest -m 'bigquery or anthropic or e2e or cli_subprocess or wheel_smoke' \
  --cov=signalforge --cov-report=term --no-cov-on-fail
```

Gated paths contribute roughly 5–10% additional coverage over the default run.
Interpreting the delta: if the default coverage drops by M% but the gated run
rises by M%, that is likely a redistribution (a code path moved behind a gated
marker) rather than a true regression. A drop in *both* numbers is a real
regression worth chasing before the release goes out.

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
pytest -m cli_subprocess --no-cov
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
   SF_RUN_BQ=1 pytest -m bigquery --no-cov
   ```

The tests query `bigquery-public-data.samples.shakespeare` (164K rows,
free under the 1 TB/month BigQuery tier). They are maintainer-only for
v0.1; no CI job runs them.
