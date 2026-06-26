# Databricks EXPLAIN COST fixtures (issue #225)

These fixtures are **documented-format placeholders**, NOT real captures. Ralph
workers and CI cannot reach a live Databricks SQL warehouse, so the shape of an
`EXPLAIN COST <sql>` plan-text cell is reproduced here by hand and the pure
parser (`signalforge.warehouse.adapters.databricks._parse_explain_cost_bytes`)
is pinned against it (engineered determinism — the parsed `int` equals the
fixture's known leaf `sizeInBytes`, never a live planner value).

> ⚠ **TO BE REPLACED by a real Free-Edition capture.** A maintainer captures the
> real `EXPLAIN COST` plan text from Databricks Free Edition (command below) and
> swaps it into `explain_cost_sample.txt`, keeping the leaf-scan `sizeInBytes` a
> round value so the determinism assertion in
> `tests/warehouse/test_databricks_estimate.py` stays readable (update the
> asserted byte count in lockstep if the round value changes). **Live
> end-to-end validity is certified by issue #226** (the gated `databricks`
> pytest marker + `SF_RUN_DATABRICKS` env gate); issue #225 ships NO gated live
> test — these committed fixtures pin the parse offline.

| File | Purpose |
|---|---|
| `explain_cost_sample.txt` | A faithful `EXPLAIN COST` optimized-logical-plan + physical-plan whose leaf table scan carries `Statistics(sizeInBytes=128.0 MiB, rowCount=...)` — the MAX node, so the parser returns exactly `int(128.0 * 1024**2)` (134217728). The happy path. |
| `explain_cost_no_stats.txt` | The same plan shape where every node shows Spark's `Statistics(sizeInBytes=8.0 EiB)` no-CBO-statistics sentinel — exercises the `EstimateUnavailableError` degrade path (the operator must run `ANALYZE TABLE`). |

## Maintainer capture / regeneration (live Databricks Free Edition)

`EXPLAIN COST <sql>` returns one row whose single cell carries the multi-line
plan text. With a Databricks Free-Edition SQL warehouse and a personal access
token (PAT), a maintainer recaptures the shape via the `databricks-sql-connector`:

```bash
export DATABRICKS_SERVER_HOSTNAME=<workspace>.cloud.databricks.com
export DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/<warehouse-id>
export DATABRICKS_TOKEN=<personal-access-token>

uv run python - <<'PY'
import os
from databricks import sql

with sql.connect(
    server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
    http_path=os.environ["DATABRICKS_HTTP_PATH"],
    access_token=os.environ["DATABRICKS_TOKEN"],
) as conn:
    with conn.cursor() as cur:
        # Pick a Delta table that has had ANALYZE TABLE ... COMPUTE STATISTICS run
        # against it so the leaf scan carries a real (non-8.0-EiB) sizeInBytes.
        cur.execute("EXPLAIN COST SELECT * FROM workspace.default.trips")
        (plan,) = cur.fetchone()
        with open(
            "tests/fixtures/warehouse/databricks/explain_cost_sample.txt", "w"
        ) as fh:
            fh.write(plan)
            if not plan.endswith("\n"):
                fh.write("\n")
PY
```

For the no-stats variant, capture a plan whose leaf shows
`Statistics(sizeInBytes=8.0 EiB)` and write it to `explain_cost_no_stats.txt`.
Note: an un-`ANALYZE`d **Delta** table usually will NOT reproduce this — Delta
carries leaf size in its transaction log regardless of `ANALYZE TABLE`, so its
leaf scan shows a real size. The all-`8.0 EiB` (Spark `defaultSizeInBytes` =
`Long.MaxValue`) shape genuinely arises for a **non-Delta / external table** or
certain **views / federated sources** that have no stats — use one of those to
reproduce. (`ANALYZE TABLE … COMPUTE STATISTICS` is the right operator fix for
the non-Delta statless case, which is what the degrade `detail` hints at.)

After swapping in a real capture, update the asserted byte count in
`tests/warehouse/test_databricks_estimate.py` to the leaf scan's real
`sizeInBytes` value.

The gated live certification (issue #226, maintainer-only — needs real creds)
will run with:

```bash
export SF_RUN_DATABRICKS=1
export DATABRICKS_SERVER_HOSTNAME=<workspace>.cloud.databricks.com
export DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/<warehouse-id>
export DATABRICKS_TOKEN=<personal-access-token>
uv run pytest -m databricks --no-cov
```
