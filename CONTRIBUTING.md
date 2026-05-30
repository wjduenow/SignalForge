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

Validate before pushing (CI runs the same four checks on a 3.11 / 3.12 / 3.13
matrix; pyright is gated to the matrix floor, codecov upload to the ceiling):

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
```

**Coverage:** see [`docs/codecov-ops.md`](docs/codecov-ops.md) for Codecov setup, badge interpretation, and threshold bumps.

**Docs:** the [published docs site](https://wjduenow.github.io/SignalForge/) is built by MkDocs Material on every push to `main`. Edits to `docs/*.md` and `README.md` land on `dev` like any other PR; the published site picks them up on the next `dev → main` merge. Local preview with `uv run mkdocs serve`. See [`.claude/rules/docs-publishing.md`](.claude/rules/docs-publishing.md) for the full deploy contract.

## Pre-release coverage audit

The default `pytest` run — and therefore the coverage badge — measures only the
default marker set. Tests gated behind `bigquery`, `anthropic`, `openai`,
`gemini`, `cli_subprocess`, `e2e`, `snowflake`, and `wheel_smoke` are filtered
out by `addopts` (see `.claude/rules/testing-signal.md` § "Known gap: excluded
markers"), so the real-network and packaging paths are not instrumented in the
badge number.

Run this audit against the **matrix ceiling** (currently Python 3.13 — the
highest version CI exercises) by prefixing `uv run --python 3.13`, so the
gated wheel-build (`wheel_smoke`) and console-script (`cli_subprocess`) paths
are checked on the newest supported interpreter. The gated markers carry no
version pin; they run on whatever interpreter `uv run` resolves.

Before cutting a release, run both suites and combine their coverage into one
total to catch regressions in the gated paths:

```bash
# 1. Default coverage (what the badge reports) — writes a fresh .coverage file:
uv run pytest

# 2. Append the gated-marker run to the SAME .coverage data file.
#    --cov-append combines with run 1 so the term report shows the COMBINED total.
#    --cov-fail-under=0 overrides the 80% gate inherited from addopts — gated
#    markers alone never clear it, and this is a measurement, not a gate.
#    (bigquery/anthropic/openai/gemini/snowflake/e2e need creds; cli_subprocess/wheel_smoke do not.)
SF_RUN_BQ=1 ANTHROPIC_API_KEY=sk-... OPENAI_API_KEY=sk-... \
  SF_RUN_OPENAI=1 SF_RUN_SNOWFLAKE=1 SF_RUN_GEMINI=1 GOOGLE_API_KEY=... \
  GOOGLE_CLOUD_PROJECT=<billing-project> \
  SNOWFLAKE_ACCOUNT=... SNOWFLAKE_USER=... SNOWFLAKE_PASSWORD=... SNOWFLAKE_WAREHOUSE=... \
  uv run pytest -m 'bigquery or anthropic or openai or gemini or snowflake or e2e or cli_subprocess or wheel_smoke' \
  --cov=signalforge --cov-append --cov-fail-under=0 --cov-report=term
```

## Gemini live smoke

Three tests gated by `@pytest.mark.gemini` exercise the Gemini provider
end-to-end against the real Google Gemini API:

- `tests/llm/test_gemini_live.py` — raw `call_llm(provider="gemini", ...)` round-trip.
- `tests/draft/test_gemini_draft_live.py` — `draft_schema` against a small in-test manifest fixture.
- `tests/grade/test_gemini_grade_live.py` — `grade_artifacts` 1-criterion × 1-artifact.

All three are deselected from default CI (`addopts -m 'not gemini'`) and
additionally self-skip via a runtime gate if either env var is missing:

```bash
SF_RUN_GEMINI=1 GOOGLE_API_KEY=... uv run pytest -m gemini --no-cov
```

Recommended SKU is `gemini-2.5-flash` (cheapest of the three registered
SKUs); per-call cost is dominated by the no-caching posture (DEC-013 of
#137) so each rubric criterion ships the full system + rubric prompt.

The combined total from step 2 minus the default badge number from step 1 is
the coverage the gated paths add — typically 5–10%. Interpreting the delta: if
the default badge number drops by M% but the combined total holds steady, that
is likely a redistribution (a code path moved behind a gated marker) rather than
a true regression. A drop in the *combined* total is a real regression worth
chasing before the release goes out.

## Live e2e suite (pre-release only)

The live e2e and live-API tests below hit real warehouses and real LLM
providers, so each invocation costs real money. They run on a
**pre-release cadence only** — NOT per-PR, NOT CI-gated. The
`addopts -m 'not …'` exclusion in `pyproject.toml` keeps every gated
marker out of default runs automatically; this section just documents how
a maintainer invokes the full suite when cutting a release (per DEC-010
of [`plans/super/155-gemini-truncation-e2e-gap.md`](plans/super/155-gemini-truncation-e2e-gap.md)).

**Cost ceiling:** ≈ **$1.38 per full-suite run** (measured 2026-05-29
against the Austin bikeshare fixture at pricing-table version `2026-05-28`;
~108 grade calls/test × 6 paid e2e tests ≈ 660 LLM calls/run across the
three providers). This is a **calibration signal, not a billing guarantee** —
vendor pricing rotates and the per-test artifact count is workload-specific.
See [`plans/super/157-e2e-cost-and-parallel.md`](plans/super/157-e2e-cost-and-parallel.md)
§ "Measured baseline (2026-05-29)" for the per-provider breakdown (Anthropic
~$0.87, OpenAI gpt-4o ~$0.42, Gemini 2.5-flash ~$0.087) and the per-test
wall-clock table.

At ~2–3 pre-release audits per month for a one-maintainer project, that
lands at roughly **$2.80–$4.20/month** — still small enough that a shell
wrapper around the invocation would add surface area without changing the
contract.

### Tests in the live e2e suite

Five paid e2e tests cover the full `signalforge generate` pipeline
(manifest → safety → draft → prune → grade → diff) against real
warehouses and real graders:

1. **`tests/cli/test_e2e_bigquery_smoke.py`** — `@pytest.mark.e2e`.
   Parametrized over `grade.provider ∈ {"anthropic", "openai", "gemini"}`
   (issue #155 US-007). The baseline gate is `SF_RUN_BQ=1` +
   `ANTHROPIC_API_KEY` + `GOOGLE_CLOUD_PROJECT` (every variant uses the
   Anthropic drafter); the `openai` variant additionally requires
   `SF_RUN_OPENAI=1` + `OPENAI_API_KEY`; the `gemini` variant
   additionally requires `SF_RUN_GEMINI=1` + `GOOGLE_API_KEY`. Variants
   missing their extra env vars skip cleanly; the baseline variant
   always runs when the BQ + Anthropic gate is satisfied.
2. **`tests/cli/test_e2e_business_rules.py`** — `@pytest.mark.e2e`.
   The `custom_sql` business-rule path (issue #116): Anthropic drafter
   + Anthropic grader against the Austin bikeshare BQ fixture with
   `meta.signalforge.business_rules` injected into the per-run
   manifest copy. Exercises ingest → draft → prune → grade → diff of
   a singular-test SELECT. Gate: `SF_RUN_BQ=1` + `ANTHROPIC_API_KEY` +
   `GOOGLE_CLOUD_PROJECT` (mirrors the BQ baseline variant).
3. **`tests/cli/test_e2e_openai_smoke.py`** — `@pytest.mark.e2e` +
   `@pytest.mark.openai`. Anthropic drafter, OpenAI `gpt-4o` grader.
   Five-env-var gate (mirrors the Gemini sibling and the parametrized
   BQ smoke — drafter stays Anthropic Sonnet per DEC-011, so the
   Anthropic auth + BigQuery opt-in are part of the contract): `SF_RUN_OPENAI=1`
   + `OPENAI_API_KEY` + `SF_RUN_BQ=1` + `ANTHROPIC_API_KEY` +
   `GOOGLE_CLOUD_PROJECT`.
4. **`tests/cli/test_e2e_gemini_smoke.py`** — `@pytest.mark.e2e` +
   `@pytest.mark.gemini`. Anthropic drafter, Gemini
   `gemini-2.5-flash` grader with `grade.max_output_tokens=4096` (the
   floor was originally 2048 per DEC-008 of #155 / verified-safe at
   the 5-pair in-isolation smoke scale; #158 raised it to 4096 after
   the full-Austin-fixture e2e run found 5-6/108 pairs still
   degrading at 2048 — 4096 is the current fixture-scale floor). Gate:
   `SF_RUN_GEMINI=1` + `GOOGLE_API_KEY` + `SF_RUN_BQ=1` +
   `ANTHROPIC_API_KEY` + `GOOGLE_CLOUD_PROJECT`.
5. **`tests/cli/test_e2e_snowflake_smoke.py`** — `@pytest.mark.snowflake`
   (NOT `@pytest.mark.e2e` — reached via the `snowflake` marker, see the
   `-m` note below). The other-warehouse path: Anthropic drafter,
   Anthropic grader, Snowflake adapter against read-only
   `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1` with `prune.sample_strategy: oneshot`.
   Gate: `SF_RUN_SNOWFLAKE=1` + `ANTHROPIC_API_KEY` + `SNOWFLAKE_ACCOUNT`
   + `SNOWFLAKE_USER` + `SNOWFLAKE_PASSWORD` + `SNOWFLAKE_WAREHOUSE`.
   See [`docs/snowflake-e2e-setup.md`](docs/snowflake-e2e-setup.md) for
   warehouse-side setup (resource monitor, XS warehouse, aggressive
   auto-suspend — guardrails against runaway cost).

Six **grade-only / draft-only live-API smokes** complement the e2e
suite. They exercise a single layer (no warehouse) against a real
provider and are gated by `@pytest.mark.anthropic` /
`@pytest.mark.openai` / `@pytest.mark.gemini` (NOT `e2e`):

- `tests/grade/test_smoke_real_api.py` (Anthropic),
  `tests/grade/test_smoke_real_api_openai.py` (OpenAI),
  `tests/grade/test_gemini_grade_live.py` (Gemini).
- `tests/draft/test_smoke_real_api_openai.py` (OpenAI),
  `tests/draft/test_gemini_draft_live.py` (Gemini).
- `tests/llm/test_gemini_live.py` (Gemini raw `call_llm` round-trip).

Per-marker drilldowns (env vars, cost-per-call, single-marker
invocations) live in the `## Gemini live smoke`,
`## OpenAI live-API smoke tests`, and `## BigQuery integration tests`
sections below. The block here documents how to run the **whole** suite
in one go.

### Parallel execution (recommended)

`pytest-xdist` is a dev dep (added by issue #157 US-004 — `uv sync --dev`
pulls it automatically). The recommended pre-release invocation runs the
`@pytest.mark.e2e` files in parallel across 3 workers:

```bash
uv run pytest -m e2e -n 3 --no-cov
```

`-n 3` is a deliberate choice, NOT `-n auto`. Default `addopts` stays
sequential — parallelism is opt-in only (per DEC-001 / DEC-003 of
[`plans/super/157-e2e-cost-and-parallel.md`](plans/super/157-e2e-cost-and-parallel.md)).
The `cli_subprocess` and `wheel_smoke` markers stay serial — do NOT add
`-n` to those invocations.

**Measured wall-clock (2026-05-29 baseline):** ~15 min at `-n 3`
(893s end-to-end) vs ~40 min serial (`-n 1`) → ~2.6× speedup against the
Austin bikeshare fixture. **Zero Anthropic rate-limit retries observed at
`-n 3`** in this baseline; the rate-limit caveat below is still load-bearing
guidance for larger fixtures or if the Anthropic 50-RPM tier changes.

**Anthropic 50 RPM rate-limit caveat.** Every paid e2e test uses Anthropic
as the drafter (the `[anthropic]` parametrize variant of the BQ smoke and
the business-rules test also use Anthropic as the grader). With `-n 3`
three parallel tests can collectively issue ~50 calls in a tight window
and trigger the `WARNING: rate limit` retry path (see
[`.claude/rules/llm-drafter.md`](.claude/rules/llm-drafter.md) §
"Module-level `_sleep` / `_rand_uniform` aliases" — retries are bounded by
`DraftConfig.max_retries_429` / `GradeConfig.max_retries_429`). The
retries succeed in practice but extend wall-time; monitor with:

```bash
uv run pytest -m e2e -n 3 --no-cov 2>&1 | tee pytest-stderr.log
grep -c "rate limit" pytest-stderr.log
```

or run with `--capture=no` for live visibility.

**Downgrade path.** If you see a burst of `rate limit` retries
(say, >10 across the run) or want to be more conservative on a paid-API
budget:

- `-n 2` — still ~2× speedup, half the Anthropic concurrency.
- `-n 1` (or simply omit `-n`) — fully serial, equivalent to the
  pre-#157 baseline.

**Cost rollup after the run.** Per-test `tmp_path` directories under
`/tmp/pytest-of-$USER/pytest-current/` carry each run's audit JSONLs and
`grade.json` sidecars. Use
[`scripts/measure_e2e_cost.py`](scripts/measure_e2e_cost.py) (added by
issue #157 US-003) to roll up the per-test cost into a single total.

### Full pre-release invocation

```bash
SF_RUN_BQ=1 \
SF_RUN_OPENAI=1 \
SF_RUN_GEMINI=1 \
SF_RUN_SNOWFLAKE=1 \
ANTHROPIC_API_KEY=sk-ant-... \
OPENAI_API_KEY=sk-proj-... \
GOOGLE_API_KEY=... \
GOOGLE_CLOUD_PROJECT=<billing-project> \
SNOWFLAKE_ACCOUNT=... SNOWFLAKE_USER=... SNOWFLAKE_PASSWORD=... \
SNOWFLAKE_WAREHOUSE=... SNOWFLAKE_DATABASE=... SNOWFLAKE_SCHEMA=... \
  uv run pytest -m 'e2e or anthropic or openai or gemini or snowflake' --no-cov
```

`--no-cov` is required per `.claude/rules/python-build.md` § "Python
version: advertised floor matches the tested floor" — the
`--cov-fail-under=80` gate inherited from `addopts` would fail any
marker-specific run that exercises only a fraction of the codebase
(mirrors `uv run pytest -m bigquery --no-cov` and
`uv run pytest -m cli_subprocess --no-cov`).

The `snowflake` marker spans two tiers: an **offline** `fakesnow` +
`sqlglot` validation suite (no env vars required — runs whenever the
marker is invoked) AND the **live** Snowflake-warehouse tests
(`test_e2e_snowflake_smoke.py`, `test_snowflake_estimate_live.py`,
`test_snowflake_prune_live.py`) gated by `SF_RUN_SNOWFLAKE=1` and the
six `SNOWFLAKE_*` connection env vars. The `-m` expression above
includes `snowflake` so the full pre-release sweep covers the live
Snowflake path; if you want to skip the live tier (e.g. no Snowflake
credentials handy), unset `SF_RUN_SNOWFLAKE` and the live tests skip
with their `_skip_reason()` while the offline tier still runs.

If you need to skip a specific provider for a given release (e.g. an
OpenAI quota hold), simply omit its `SF_RUN_<X>=1` env var — that
provider's tests self-skip with a named reason while the rest of the
suite proceeds.

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

## OpenAI live-API smoke tests

Three tests gated by `@pytest.mark.openai` exercise the OpenAI provider
end-to-end (issue #136):

- `tests/grade/test_smoke_real_api_openai.py` — `grade_artifacts` against
  a tiny in-test fixture, single criterion.
- `tests/draft/test_smoke_real_api_openai.py` — `draft_schema` against a
  small in-test manifest; honours DEC-005's "scope both stages" commitment.
- `tests/cli/test_e2e_estimate_openai.py` — `signalforge generate
  --estimate` with `llm.provider: openai` + `grade.provider: openai`.

All three are skipped by default (filtered out by `addopts = -m 'not
openai'`) and additionally self-skip via a runtime gate if either env var
is missing — the belt-and-suspenders pattern from
`.claude/rules/testing-signal.md` § "End-to-end gated tests".

Run with credentials:

```bash
SF_RUN_OPENAI=1 OPENAI_API_KEY=sk-... uv run pytest -m openai --no-cov
```

The `--estimate` test additionally honours `GOOGLE_CLOUD_PROJECT` when
present (lets the warehouse-bytes leg compute instead of degrading to
`<unavailable: ...>`); absent, the warehouse half degrades cleanly per
DEC-005 of #36 and the test still passes. They are maintainer-only; no
CI job runs them. Each run hits the real OpenAI API and incurs a small
cost (the grade smoke is 5 `gpt-4o` calls at ~$0.005 each; the draft
smoke is 1 call; `--estimate` is local tiktoken only, no API call).
