# Snowflake EXPLAIN USING JSON fixtures (issue #130)

These fixtures are **hand-crafted**. Ralph workers and CI cannot reach a live
Snowflake account, so the shape of an `EXPLAIN USING JSON <sql>` result cell is
captured here by hand and the pure parser (`signalforge.warehouse.adapters.snowflake
._parse_explain_json_bytes`) is pinned against it (engineered determinism — the
parsed `int` equals the fixture's known `bytesAssigned`, never a live planner value).

| File | Purpose |
|---|---|
| `explain_using_json_sample.json` | A realistic full plan with `GlobalStats.bytesAssigned = 104857600` (100 MiB) — the happy path. |
| `explain_using_json_no_stats.json` | Same document shape with `GlobalStats` absent — exercises the `EstimateUnavailableError` degrade path. |

## Maintainer regeneration (live Snowflake)

`EXPLAIN USING JSON <sql>` returns one row / one cell carrying the JSON plan.
With a live Snowflake connection a maintainer can recapture the shape:

```sql
EXPLAIN USING JSON SELECT customer_id, order_total FROM analytics.public.orders;
```

The single cell of the single returned row is the JSON document. Pretty-print it
(`json.dumps(..., indent=2)`) and drop it in here, keeping `bytesAssigned` a round
number so the determinism assertion stays readable. The gated
`@pytest.mark.snowflake` live test (issue #130 US-005) certifies the shape against
a real EXPLAIN; these committed fixtures pin the parse offline.
