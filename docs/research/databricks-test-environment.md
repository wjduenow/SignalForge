# Databricks test-environment spike (#220)

> Status: **decided** · Epic [#219](https://github.com/wjduenow/SignalForge/issues/219) · First child.
> This doc is the contract every later Databricks child (#D4/#D5/#D6) references for the
> test stack, the env-var gate, and the live certification target — so no sibling
> re-litigates them.

Databricks has **no `fakesnow` equivalent**, and we are building without a paid account.
This spike decides the concrete free/low-cost build+test stack, confirms a live connection
works on the free tier, and writes both down. It does **not** ship adapter code (that is
#D4/#D5/#D6) — it ships the test target everything else certifies against.

The load-bearing unknown the issue flagged — *can a personal access token even authenticate
the `databricks-sql-connector` against a Free-Edition SQL warehouse?* — is **confirmed
working** (see [Certification results](#certification-results)).

---

## Decisions

### DEC-1 — Two-tier test stack (Tier 2 local-Spark skipped)

The Snowflake epic validated in tiers: offline shape (`sqlglot` parse + hand fakes) →
offline execution (`fakesnow` DuckDB) → gated live workspace. Databricks keeps the first
and third tiers and **drops the middle one**:

- **Tier 1 — offline, zero account (build + iterate; ~90% of validation).**
  - `sqlglot.parse_one(sql, dialect="databricks")` as the compiler parse-guard (the #121
    lesson: snapshot equality certifies *shape*, not *validity* — keep a parser in the loop).
  - A hand-rolled `FakeDatabricksConnection` (`expect_execute` / `assert_all_expectations_met`
    / `close_raises`) for behaviour assertions, error-mapping, and cleanup fail-soft — the
    `FakeSnowflakeConnection` analogue.
- **Tier 3 — Databricks Free Edition, free real workspace (gated-live certification).**
  Gated behind `@pytest.mark.databricks` + `SF_RUN_DATABRICKS=1`. The serverless `2X-Small`
  SQL warehouse + bundled `samples` catalog is the live target.

**Tier 2 (local `pyspark` + `delta-spark`) is skipped.** Databricks SQL is a Spark-SQL
superset, so a local `SparkSession` *could* execute non-Databricks-specific SQL — but it
drags a JVM and a heavy dev-dep into the tree for a marginal middle tier, and Tier 3 is now
confirmed cheap and working. `fakesnow` earned its place because it is a lightweight
in-process DuckDB; `pyspark` is not the same trade. #D6 builds the **Tier-1 harness only**;
if a future need for offline execution appears (e.g. Tier 3 quota pressure during heavy
iteration), Tier 2 can be reconsidered as its own ticket.

### DEC-2 — The live target is Databricks Free Edition

**Free Edition** (serverless-only; replaced Community Edition) ships:

- one SQL warehouse limited to **`2X-Small`** (serverless; auto-stops),
- Unity Catalog with one metastore,
- the bundled **`samples`** catalog (`samples.tpch`, `samples.nyctaxi`, …) for read-only
  source-as-model fixtures,
- a writable default catalog, **`workspace`** — the natural target for `materialise_sample`
  temp tables if/when that lands (#D5).

No Tier-4 cloud trial (Azure/AWS/GCP Databricks) is needed: PAT auth — the documented risk —
works on Free Edition. The cloud-trial fallback stays documented in the issue as an escape
hatch only.

### DEC-3 — The `databricks` pytest marker + env-var gate (mirrors `snowflake`)

Live tests carry the belt-and-suspenders gate from `testing-signal.md`: a
`@pytest.mark.databricks` marker (deselected by default `addopts`) **plus** a runtime
`_skip_reason()` that skips with a clear message when an env var is missing. Run with
`uv run pytest -m databricks --no-cov` (the `--no-cov` is required — `--cov-fail-under` in
`addopts` would fail a marker-only run, mirroring the `snowflake` / `bigquery` precedent).

**Env-var contract** — the canonical reference #D4/#D5/#D6 cite (don't re-derive):

| Env var | Where it comes from |
|---|---|
| `SF_RUN_DATABRICKS=1` | opt into the live leg (belt-and-suspenders with the marker) |
| `DATABRICKS_SERVER_HOSTNAME` | SQL warehouse → **Connection details** → *Server hostname* (e.g. `dbc-xxxxxxxx-xxxx.cloud.databricks.com`) |
| `DATABRICKS_HTTP_PATH` | SQL warehouse → **Connection details** → *HTTP path* (e.g. `/sql/1.0/warehouses/xxxxxxxxxxxxxxxx`) |
| `DATABRICKS_TOKEN` | User Settings → **Developer** → **Access tokens** → *Generate new token* (a `dapi…` PAT) |

A read-only `samples` table (`samples.tpch.region`, 5 rows) is the source-as-model fixture;
a writable schema under the `workspace` catalog is only needed if `materialise_sample` lands.

### DEC-4 — Packaging: `databricks-sql-connector` ships behind a `[databricks]` extra

The core install (`pip install signalforge-dbt`) **never** gains an unconditional Databricks
dependency (Architectural Commitment #4, mirroring the `[snowflake]` / `[airflow]` extras).
`databricks-sql-connector` ships behind a `[databricks]` optional extra defined by the
adapter child (#D4); the lazy SDK import stays confined to the adapter's `_databricks_client.py`
shim per the one-shim-per-vendor rule. The extra is mirrored into the dev group so the gated
suite resolves it (contrast the `[airflow]` deliberate exception — Databricks is not heavy or
constraints-pinned, so the normal mirror applies).

---

## Databricks-specific notes for the adapter author (#D4)

Pin these before the adapter rediscovers them:

- **PATs have no per-API scopes.** Unlike a GCP service-account key, a Databricks PAT carries
  no scope picker — it inherits the creating user's full workspace permissions. The
  prerequisites are workspace-level, not token-level: the **Personal Access Tokens** setting
  must be enabled (Settings → Advanced; on by default for the Free-Edition admin), and the
  user needs `CAN USE` on the warehouse + `SELECT` on `samples`.
- **Unity Catalog three-part naming.** Identifiers are `catalog.schema.table` (e.g.
  `samples.tpch.region`, `workspace.<schema>.<tbl>`) — distinct from BigQuery's
  `project.dataset.table` and Snowflake's `database.schema.table`. The `Dialect`/`TableRef`
  seam handles this without name-branching; backtick (`` ` ``) is the Databricks identifier
  quote.
- **Serverless cold-start latency.** A stopped `2X-Small` warehouse cold-starts in ~5–30s
  (measured ~15s, see below); the connector waits through it. Budget for it in live-test
  timeouts — don't treat the first query's latency as a hang.
- **`samples` is read-only.** Source-as-model fixtures read from `samples`; anything that
  writes (materialise temp tables) must target the writable `workspace` catalog.

⚠️ **Fair-use quota.** Exceeding the Free-Edition daily/monthly quota shuts the warehouse down
for the rest of the period. Keep live tests **minimal and idempotent** (the probe below reads
a 5-row table); the `2X-Small` warehouse + auto-stop keeps cost at zero. This mirrors the
Snowflake ops guidance ("resource monitor first, smallest warehouse, aggressive auto-suspend").

---

## Reproduce a live connection (the acceptance signal)

A maintainer can reproduce the confirmed connection from scratch:

1. **Sign up** for Databricks Free Edition (`docs.databricks.com/aws/en/getting-started/free-edition`).
   Account-level auth is email-OTP / Google / Microsoft.
2. **SQL warehouse** — Free Edition provisions a serverless `2X-Small` automatically. Open it →
   **Connection details** and copy *Server hostname* + *HTTP path*.
3. **Enable PATs** — Settings → Advanced → **Personal Access Tokens** (on by default as admin).
4. **Generate a token** — User Settings → Developer → **Access tokens** → *Generate new token*.
   Set any comment + a sensible expiry; there is no scope step.
5. **Set the four env vars** (DEC-3) — locally via repo-root `.env`, sourced with
   `set -a && . ./.env && set +a`.
6. **Probe** — a one-off PEP 723 script (`uv run` resolves `databricks-sql-connector` without
   touching `pyproject.toml`) that opens a connection and runs, in order: `SELECT 1` (auth +
   connectivity floor), `current_user()` / `current_catalog()` (identity + default catalog),
   `SELECT COUNT(*) FROM samples.tpch.region` (read-only samples access). A green run is the
   Tier-3 confirmation; #D6 grows this into the real `@pytest.mark.databricks` suite.

The spike script itself is throwaway and not committed — the [results below](#certification-results)
are the durable deliverable. Re-create a one-off `uv run` probe if a re-verify is ever needed.

---

## Certification results

**2026-06-24 · Databricks Free Edition · `databricks-sql-connector>=3,<4`**

```
[1/3] SELECT 1            -> ok (15.4s incl. warehouse start)
[2/3] current_user()      -> <maintainer email>  (default catalog: workspace)
[3/3] samples.tpch.region -> 5 rows readable
SUCCESS: PAT + databricks-sql-connector works against the Free-Edition 2X-Small warehouse.
```

Confirmed:

- **PAT auth works on Free Edition** — the load-bearing unknown; no Tier-4 fallback needed.
- **Default catalog `workspace` is writable** → the `materialise_sample` target (#D5).
- **`samples` reads** → source-as-model fixtures (#D4 prune/grade e2e).
- **`2X-Small` cold-start ~15s** → live-test timeout budget.
- **Env-var contract (DEC-3) holds verbatim** from repo-root `.env`.

---

## What this spike ships

- This doc: the decided two-tier stack (DEC-1), the live target (DEC-2), the marker + env-var
  contract (DEC-3), the packaging posture (DEC-4), the adapter-author notes, the reproduction
  steps, and the certification results.

## What it does NOT ship (later children)

- The `Dialect` value object + `sqlglot` `databricks` parse-guard + `[databricks]` extra (#D4).
- `sample_rows` / `materialise_sample` / `run_test_sql` + connection-bound session (#D5).
- The `FakeDatabricksConnection` + the gated `@pytest.mark.databricks` live suite + the full
  error taxonomy + the consolidated ops-doc section (#D6).

## Sources

- [Databricks Free Edition limitations](https://docs.databricks.com/aws/en/getting-started/free-edition-limitations)
- [Sign up for Databricks Free Edition](https://docs.databricks.com/aws/en/getting-started/free-edition)
- [Databricks SQL Connector for Python](https://docs.databricks.com/aws/en/dev-tools/python-sql-connector)
