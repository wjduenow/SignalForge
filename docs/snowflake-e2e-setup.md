# Snowflake end-to-end test setup

This guide walks a contributor with no prior Snowflake exposure
from zero to a green `pytest -m snowflake` run against a real
Snowflake account. It's the Snowflake analogue of the BigQuery
[End-to-end smoke test](e2e-smoke-test.md) guide.

> **Read the [Cost guardrails](#cost-guardrails-set-these-up-first)
> section first and set them up before you run anything.** A
> runaway query against an un-capped warehouse can bill real
> credits. The guardrails below make that physically impossible.

## What these tests prove

The Snowflake adapter has a lot of warehouse-specific surface that
unit tests with `fakesnow` can't fully certify: real Snowflake folds
unquoted identifiers to upper-case, rejects `HASH(*)` outside a
`SELECT` projection, enforces read-only shares on the sample
database, and reaps session-scoped temp tables on connection close.
The gated `@pytest.mark.snowflake` tests split into two tiers:

- **Offline (`fakesnow`)** — compiled-SQL validation and the
  `EXPLAIN`-estimate parsing path run through the in-process
  `fakesnow` engine. **No credentials, no account, no cost.**
- **Live** — the full `signalforge generate` pipeline (manifest →
  safety → draft → prune → grade → diff) runs against the read-only
  `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1` sample dataset, plus the live
  `EXPLAIN USING JSON` cost-estimate certification. These need a
  real account and a real Anthropic key.

The live e2e exercises the load-bearing claim of the project: the
prune step drops at least one `always-passes` test on a natural
NOT NULL source column (the TPCH primary key `c_custkey`). If it
passes, you have evidence the pipeline composes correctly against
real Snowflake and real Anthropic before users hit it.

## Who runs these

Maintainers and contributors, locally, before declaring a
Snowflake-touching PR ready — **not** CI. CI doesn't carry the
credentials these tests need, and forked-PR runs strip secrets by
design. The tests are gated by `SF_RUN_SNOWFLAKE` plus connection
env vars and self-skip with a clear reason when any are absent, so
a default `pytest` run never accidentally bills your account.

## Cost guardrails (set these up first)

Do this in the Snowflake UI **before** your first run. The whole
point is that a mistake can't cost more than a few cents.

1. **Create a resource monitor with a hard credit cap.** In
   Snowflake: *Admin → Cost Management → Resource Monitors → +
   Resource Monitor*. Set a small monthly credit quota (e.g. `1`
   credit) and a `SUSPEND` (or `SUSPEND_IMMEDIATE`) action at 100%.
   Assign it to the warehouse you'll use for testing. This is the
   backstop that makes a runaway query physically unable to bill
   unbounded credits.
2. **Use an XS warehouse.** TPCH_SF1 is tiny; a full-scope
   `COUNT(*)` over the ~150K-row `CUSTOMER` table is sub-second on
   the smallest compute. Create a dedicated `XSMALL` warehouse for
   testing rather than reusing a larger production warehouse:

   ```sql
   CREATE WAREHOUSE IF NOT EXISTS SIGNALFORGE_TEST_WH
     WAREHOUSE_SIZE = 'XSMALL'
     AUTO_SUSPEND = 60       -- park after 60s idle
     AUTO_RESUME = TRUE
     INITIALLY_SUSPENDED = TRUE;
   ```

3. **Set aggressive auto-suspend.** `AUTO_SUSPEND = 60` (seconds)
   parks the warehouse almost immediately after the test finishes
   so you don't pay for idle compute. The `CREATE WAREHOUSE` above
   already sets it; confirm it on any pre-existing warehouse you
   reuse.

With a resource monitor capped at 1 credit and an XS warehouse, a
full live run costs a fraction of a credit on the Snowflake side
(plus ~\$0.13–0.20 of Anthropic spend for the full-stack e2e — see
[Cost ceiling](#cost-ceiling)).

## Prerequisites

1. **A Snowflake account.** A
   [Snowflake trial](https://signup.snowflake.com/) works — trials
   ship with the `SNOWFLAKE_SAMPLE_DATA` shared database already
   mounted, which is all the live tests read.

2. **Access to `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`.** This is a
   read-only share Snowflake provides to every account. Confirm
   you can query it:

   ```sql
   SELECT COUNT(*) FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER;
   -- expect ~150000
   ```

   If the database isn't visible, an account admin can re-import it
   from *Data → Private Sharing → SNOWFLAKE_SAMPLE_DATA*.

3. **The `snowflake` dependencies installed.** Either install the
   published extra:

   ```bash
   pip install "signalforge-dbt[snowflake]"
   ```

   or, for development from source, `uv sync --dev` — the dev group
   already includes `snowflake-connector-python` **and** `fakesnow`
   (the offline harness), so a single sync gives you everything for
   both test tiers.

4. **An Anthropic API key** with budget for ~\$0.20 of spend — only
   needed for the **full-stack e2e** (`test_e2e_snowflake_smoke.py`),
   which drives a real LLM draft + grade. The offline fakesnow tests
   and the live `EXPLAIN`-estimate test don't call the LLM. Sign up
   at <https://console.anthropic.com/> if you don't have one.

## Local environment

Copy the committed template and fill in your values:

```bash
cp .env.example .env
$EDITOR .env
```

`.env` is gitignored (`.gitignore:14`) so your filled-in secrets
never get committed; only the placeholder `.env.example` is tracked.
SignalForge doesn't auto-load `.env` — `source` it into your shell
(or use [direnv](https://direnv.net/)) before running:

```bash
set -a; source .env; set +a   # export every var in .env
```

The required variables, mirroring the `SF_RUN_BQ` belt-and-suspenders
pattern (the marker deselects the test in CI; the runtime
`_skip_reason()` helper turns a missing var into an obvious
skip-with-reason rather than a confusing failure):

| Variable               | Required for      | Purpose                                                                 |
|------------------------|-------------------|-------------------------------------------------------------------------|
| `SF_RUN_SNOWFLAKE=1`   | every live test   | Opt-in to the Snowflake-backed branch (the Snowflake analogue of `SF_RUN_BQ=1`). |
| `SNOWFLAKE_ACCOUNT`    | every live test   | Your account identifier (e.g. `myorg-account1` or `xy12345.us-east-1`). |
| `SNOWFLAKE_USER`       | every live test   | The login user.                                                         |
| `SNOWFLAKE_PASSWORD`   | every live test   | The user's password (password auth is the v0.2 default).                |
| `SNOWFLAKE_WAREHOUSE`  | every live test   | The XS warehouse compute context for prune/sample queries.              |
| `ANTHROPIC_API_KEY`    | full-stack e2e    | The LLM seam (draft + grade); starts with `sk-ant-...`.                 |
| `SNOWFLAKE_ROLE`       | optional          | Override the user's default role if it lacks `SNOWFLAKE_SAMPLE_DATA` access. |
| `SNOWFLAKE_DATABASE`   | optional          | Defaults to `SNOWFLAKE_SAMPLE_DATA` in the fixture profile.             |
| `SNOWFLAKE_SCHEMA`     | optional          | Defaults to `TPCH_SF1` in the fixture profile.                          |

### dbt `profiles.yml` Snowflake target

For a one-off CLI invocation (rather than through pytest), a
Snowflake dbt profile target carries the same connection fields
(`DbtProfileTarget`, issue #120). A minimal password-auth target:

```yaml
signalforge_test_tpch:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: "{{ env_var('SNOWFLAKE_ACCOUNT') }}"
      user: "{{ env_var('SNOWFLAKE_USER') }}"
      password: "{{ env_var('SNOWFLAKE_PASSWORD') }}"
      role: "{{ env_var('SNOWFLAKE_ROLE', 'PUBLIC') }}"
      warehouse: "{{ env_var('SNOWFLAKE_WAREHOUSE') }}"
      database: SNOWFLAKE_SAMPLE_DATA
      schema: TPCH_SF1
```

The live e2e test handles this friction for you — it copies the
committed fixture into a per-run `tmp_path` and writes a profile
target wired to your env vars, so you don't edit any committed file.

## Running the tests

From the repository root, with `.env` sourced into your shell:

```bash
# Offline only — no credentials, no cost. Runs the fakesnow
# compiled-SQL + EXPLAIN-parse suites; the live tests self-skip.
uv run pytest -m snowflake --no-cov

# Full live run — needs every variable above set.
set -a; source .env; set +a
uv run pytest -m snowflake --no-cov
```

The same `-m snowflake` marker covers both tiers: with no
credentials set, the offline fakesnow tests run and the live tests
self-skip with a reason naming the missing variable; with everything
set, the live tests run too.

The `--no-cov` flag is **required** because `--cov-fail-under=80` in
`addopts` would fail any marker-specific run that exercises only a
fraction of the codebase (mirrors `pytest -m e2e --no-cov` for the
BigQuery suite).

A successful full run prints `N passed` and exits 0. A clean
credential-absent run prints the gated tests as `skipped` with the
missing-variable reason.

## Security hygiene

- Keep secrets in `.env` (gitignored), never inline in a committed
  file. `source` it into your shell; don't paste the password on the
  command line where it lands in `~/.bash_history`.
- Use a fresh shell session, or `unset SNOWFLAKE_PASSWORD
  ANTHROPIC_API_KEY` after the run.
- SignalForge never writes credentials to disk — they go from your
  environment straight into the in-memory Snowflake / Anthropic SDK
  clients. The audit JSONLs under `.signalforge/` carry only
  blake2b-8 hashes of inputs, never raw secrets. The `SnowflakeAdapter`
  `__repr__` deliberately shows only `account` + `warehouse`, never
  `user` / `password` / `role` / `database` / `schema`.
- The committed fixture (`tests/fixtures/snowflake/`) contains zero
  secrets — its `profiles.yml` carries placeholder credentials that
  the test overwrites per-run from your environment.

## Cost ceiling

- **Snowflake:** the resource monitor (capped at e.g. 1 credit) is
  the hard backstop. The actual work — a handful of full-scope
  `COUNT(*)` queries over the ~150K-row TPCH `CUSTOMER` table on an
  XS warehouse — costs a small fraction of a credit. Auto-suspend
  at 60s means you don't pay for idle compute after the run.
- **Anthropic:** bounded by the rubric size for the full-stack e2e
  (4 default criteria × the drafted artifact count), roughly
  \$0.13–0.20 per run on Sonnet 4.6 prices as of 2026-05. The
  offline fakesnow tests and the live `EXPLAIN`-estimate test make
  no LLM calls.

If either number drifts substantially, investigate before
re-running.

## Troubleshooting

| Symptom                                                                    | Likely cause                                          | Fix                                                                                                       |
|----------------------------------------------------------------------------|-------------------------------------------------------|-----------------------------------------------------------------------------------------------------------|
| Live tests `skipped` with reason `SF_RUN_SNOWFLAKE=1 required ...`         | The opt-in flag isn't set                             | `set -a; source .env; set +a`, then confirm with `env \| grep SF_RUN_SNOWFLAKE`                           |
| `skipped` with `<VAR> required (Snowflake connection parameter ...)`       | A connection env var is missing                       | Set the named variable in `.env` and re-source it                                                          |
| `250001 Could not connect to Snowflake backend`                            | Wrong `SNOWFLAKE_ACCOUNT` format                      | Use the org-account (`myorg-account1`) or legacy locator (`xy12345.us-east-1`) form, no `https://` prefix |
| `Object 'SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER' does not exist`          | The sample share isn't mounted, or role lacks access  | Re-import `SNOWFLAKE_SAMPLE_DATA` (Data → Private Sharing); set `SNOWFLAKE_ROLE` to one with access        |
| `No active warehouse selected in the current session`                      | `SNOWFLAKE_WAREHOUSE` unset or warehouse suspended    | Set `SNOWFLAKE_WAREHOUSE`; `AUTO_RESUME = TRUE` lets it wake on the first query                            |
| `Cannot create temporary table ... in a read-only share`                   | Ran with `prune.scope: sample` + `materialised`       | The e2e pins `prune.scope: full` for exactly this reason; don't override it against the sample dataset     |
| `LLM response did not match the CandidateSchema shape`                     | Anthropic's response shape drifted vs. the parser     | Set `ANTHROPIC_LOG=info`, inspect `~/.anthropic-debug/`, file an issue                                     |

## Cross-references

- The operator-facing Snowflake adapter reference (cost guidance,
  `EXPLAIN`-estimate semantics): [docs/warehouse-adapter-ops.md](warehouse-adapter-ops.md).
- The BigQuery analogue of this guide: [docs/e2e-smoke-test.md](e2e-smoke-test.md).
- The committed Snowflake fixture: `tests/fixtures/snowflake/`.
- Test-marker conventions and the pre-release coverage audit:
  `CONTRIBUTING.md` § "Test markers".
- The CLI's full flag reference: [docs/cli-ops.md](cli-ops.md).
