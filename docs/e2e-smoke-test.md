# End-to-end smoke test

## What this test proves

SignalForge has a lot of moving parts: it reads dbt project metadata,
talks to your data warehouse, asks an LLM to draft `schema.yml`
entries, runs each drafted test against real warehouse data to
discard the no-signal ones, asks the LLM to grade the drafted prose
against a quality rubric, and writes a unified diff so a reviewer
can see what would land. Most of those parts are covered by unit
tests with simulated dependencies — fast, deterministic, no money
or accounts required to run them.

But unit tests with simulated dependencies can't catch every class
of bug. Real BigQuery rejects column names with subtle typos.
Real Anthropic occasionally returns JSON in a slightly different
shape than the parser expects. Real prompt-cache thresholds
silently no-op below a model-specific minimum. **The end-to-end
smoke test exists to catch the bugs that only surface when the
whole pipeline runs against real services.**

It runs `signalforge generate` against
[`bigquery-public-data.austin_bikeshare`](https://console.cloud.google.com/marketplace/product/austin-bikeshare/austin-bikeshare-trips)
— a small, stable, public dataset of bike-share trips in Austin,
Texas. The test exercises every stage of the pipeline (manifest
load → safety → draft → prune → grade → diff) and asserts that
the run completes cleanly, that at least one drafted test gets
dropped because warehouse data shows it always passes, and that
the diff sidecar lands on disk in the expected shape.

If this test passes, you have evidence that the pipeline composes
correctly against real Anthropic and real BigQuery. If it fails,
you have a tight, reproducible signal that something in the
integration is wrong before users notice.

## Who runs it

Maintainers, locally, before tagging a release. **Not** CI.

CI doesn't have the credentials this test needs (an Anthropic API
key, a Google Cloud billing project), and forked-PR CI runs strip
secrets by design. So the test is gated by three environment
variables and skipped by default; a missing variable produces a
clear skip reason rather than a confusing failure.

## What to expect when it passes

A typical successful run takes about 5–6 minutes wall-clock and
costs about **\$0.13** in Anthropic API spend plus **under
500 MB** of BigQuery data scanned (well within the free tier most
projects have available). The test asserts seven invariants:

1. The CLI exits cleanly (return code 0, no Python traceback in stderr).
2. A diff sidecar (`diff.json`) lands at the expected path under
   the project's `.signalforge/` directory.
3. The diff produced **at least one** entry — kept, dropped, or
   flagged. The pipeline isn't a no-op.
4. **At least one drafted test was dropped** with the reason
   `always-passes`. This is the load-bearing claim of the project:
   tests that pass for every row in real warehouse data are noise,
   and SignalForge filters them out before they reach a reviewer.
5. **At least one artifact was flagged** as below the quality
   threshold. The fixture pins very strict thresholds
   (`min_pass_rate: 0.95`, `min_mean_score: 0.95`) so this branch
   is exercised deterministically.
6. **Every grade call completed cleanly** (`aggregate_complete:
   true` in the grading report). No grade pair degraded due to
   network errors or timeouts.
7. **No Python traceback escaped to stderr** — the CLI's typed
   error machinery handled every layer's failure modes (per
   `cli-layer.md` DEC-016).

## Prerequisites

Three things have to be true before the test will run:

1. **Application Default Credentials for Google Cloud** are
   configured locally. Run once:

   ```bash
   gcloud auth application-default login
   ```

   This opens a browser window, asks you to sign in, and stores a
   refresh token at `~/.config/gcloud/application_default_credentials.json`.

2. **A Google Cloud project that you can bill BigQuery jobs to.**
   `bigquery-public-data.austin_bikeshare` is publicly readable,
   but BigQuery still has to charge *someone* for the query — and
   it can't be `bigquery-public-data` itself. Your billing project
   needs the `BigQuery Job User` IAM role (or equivalent). The
   test scans roughly 200–500 MB per run, well below the 1 TB
   monthly free tier that personal Google Cloud accounts get.

3. **An Anthropic API key** with budget for ~\$0.20 of spend. If
   you don't have one, sign up at <https://console.anthropic.com/>
   and create a key.

You'll wire these into your shell session via three environment
variables:

| Variable                  | Purpose                                                          |
|---------------------------|------------------------------------------------------------------|
| `SF_RUN_BQ=1`             | Opt-in to the BigQuery-backed branch (mirrors the existing `tests/warehouse/test_bigquery_integration.py` precedent). |
| `GOOGLE_CLOUD_PROJECT`    | The billing project ID (e.g. `my-personal-project-123456`).      |
| `ANTHROPIC_API_KEY`       | Your Anthropic API key (starts with `sk-ant-...`).               |

## Running the test

From the repository root, in a freshly authenticated shell:

```bash
# 1. Authenticate to Google Cloud (first run only).
gcloud auth application-default login

# 2. Set the three required env vars.
export SF_RUN_BQ=1
export GOOGLE_CLOUD_PROJECT=<your-billing-project>
export ANTHROPIC_API_KEY=sk-ant-...

# 3. Run the gated test.
pytest -m e2e --no-cov
```

The `--no-cov` flag is required because pytest's coverage gate
is set to 80% over the default test set; the e2e test on its own
exercises only a fraction of the codebase, so coverage would fail
spuriously without the flag.

The test itself handles the profile-rewrite friction (see "Why the
profile rewrite matters" below) — it copies the fixture into a
per-run `tmp_path`, substitutes `GOOGLE_CLOUD_PROJECT` into
`profiles.yml`, and runs `signalforge generate` against the temp
project. If you want to run the same flow as a one-off CLI
invocation (rather than through pytest), follow the README's
[Trying it out](../README.md#trying-it-out) walkthrough — it
shows the manual copy + profile-rewrite incantation.

### Why the profile rewrite matters

The committed `profiles.yml` in `tests/fixtures/dbt_project_austin/`
pins `project: bigquery-public-data`. That's correct for `dbt parse`
(which only reads schema metadata about the source dataset and
doesn't bill anything substantive). But at *query* time the BigQuery
SDK uses `profile.project` as the **billing** project, and you
can't bill `bigquery-public-data` itself (you don't own it).

The test sidesteps this by writing a per-run `profiles.yml` into
`tmp_path` that substitutes your `GOOGLE_CLOUD_PROJECT` and adds
`maximum_bytes_billed: 1000000000` (the default 100 MB cap blocks
the materialised-sample full-table scan over the ~2.27M-row source).
This is the v0.1 workaround; a future ticket may teach the profile
loader to render `${ENV_VAR}` references the way dbt does natively.

A successful run prints `1 passed` and exits 0. A failure prints
the assertion that fired plus the captured stderr from the CLI
run; the test framework also preserves the test's `tmp_path`
artifacts (manifest, profile, audit JSONLs, diff sidecar) under
`/tmp/pytest-of-<user>/` for post-mortem inspection.

## Security hygiene

- Use a fresh shell session, or `unset ANTHROPIC_API_KEY` after
  the run, so your API key doesn't sit in `~/.bash_history`.
- The test never writes the API key to disk — it goes from your
  environment straight into the in-memory Anthropic SDK client.
  The audit JSONLs SignalForge writes (under
  `.signalforge/`) only contain blake2b-8 hashes of inputs, never
  raw secrets.
- The committed fixture (`tests/fixtures/dbt_project_austin/`)
  contains zero secrets. The `profiles.yml` checked into git
  references `bigquery-public-data` as the *source* project (the
  read target); the *billing* project comes exclusively from your
  `GOOGLE_CLOUD_PROJECT` env var via a per-run profile rewrite
  inside the test.

## Cost ceiling

The test pins `maximum_bytes_billed: 1_000_000_000` (1 GB) at the
BigQuery client layer, so a runaway query physically cannot exceed
that. Anthropic spend is bounded by the rubric size (4 default
criteria × ~21 artifacts ≈ 84 calls × ~600 input tokens ≈
\$0.13 per run on Sonnet 4.6 prices as of 2026-05). If either of
those numbers ever drifts substantially, investigate before
re-running.

## What this test does **not** prove

- It runs against a single, small public dataset. Real-world dbt
  projects with hundreds of models, partitioned tables, or
  long-running materializations will exercise code paths the
  smoke test doesn't touch. Use it as a release smoke, not as a
  load test.
- It doesn't test the `--write` mode (writing the proposed
  `schema.yml` back to disk) — only the default `--dry-run`
  print-the-diff path. `--write` has its own unit-level coverage
  but no e2e gate.
- It uses the v0.1 default models (Sonnet 4.6 for both draft and
  grade). If you switch models in `signalforge.yml`, the smoke
  test won't validate the new model — re-run it against the new
  config to confirm.

## Troubleshooting

| Symptom                                                                                | Likely cause                                          | Fix                                                                                                                           |
|----------------------------------------------------------------------------------------|-------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------|
| `1 skipped` with reason `SF_RUN_BQ=1 required ...`                                     | One of the three env vars isn't set                   | Re-export the missing variable; check `env \| grep -E '(SF_RUN_BQ\|GOOGLE_CLOUD_PROJECT\|ANTHROPIC_API_KEY)'`                  |
| `User does not have bigquery.jobs.create permission in project bigquery-public-data`   | Billing project not set; SDK fell back to the source  | Set `GOOGLE_CLOUD_PROJECT` to a project where you have the `BigQuery Job User` role                                           |
| `Query exceeded max_bytes_billed (limit=100000000, ...)`                               | Old `maximum_bytes_billed` cap kicked in              | Refresh from `main` — issue #10 raised the cap to 1 GB in the per-run profile rewrite                                         |
| `LLM response did not match the CandidateSchema shape`                                 | Anthropic's response shape drifted vs. the parser     | Inspect `~/.anthropic-debug/` (set `ANTHROPIC_LOG=info`) for the raw response; file an issue                                  |
| `aggregate_complete=False` in `grade.json`                                             | Network blip during a grade call exhausted retries    | Re-run; if it persists, raise `grade.total_budget_seconds` in `signalforge.yml`                                               |
| `1 passed` but the wall-clock was >10 minutes                                          | Anthropic API was slow; not a real failure            | Acceptable; the test isn't time-bounded                                                                                       |

## Cross-references

- The walk-through quickstart for end users: `README.md`
  § "Trying it out".
- The fixture itself:
  `tests/fixtures/dbt_project_austin/`. Includes its own
  `regenerate.sh` for refreshing the committed `target/manifest.json`
  against a newer dbt-bigquery release.
- The full design and decisions log:
  `plans/super/10-e2e-bigquery-smoke.md`.
- The CLI's full flag reference: `docs/cli-ops.md`.
