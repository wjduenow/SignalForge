# TPCH manifest seed (Snowflake live e2e fixture)

Hand-crafted dbt project + `manifest.json` describing one model over the
Snowflake sample dataset `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER`. Consumed by
the Snowflake test harness (issue #124): the loads-only test
(`tests/warehouse/test_snowflake_seed_loads.py`, runs in the default suite) and
the gated full-pipeline live e2e (US-005).

## Why the manifest is hand-crafted, not generated

`target/manifest.json` is **hand-crafted**, NOT produced by a live `dbt parse`.
Ralph workers and CI cannot reach a live Snowflake account, so the seed is
committed verbatim and validated in-process by
`signalforge.manifest.load(<fixture_dir>)` — see
`.claude/rules/testing-signal.md` § "Hand-crafted manifest seed when workers
can't run live tooling". The generator that emits the JSON from a declarative
dict lives at `_gen_manifest.py` in this directory:

```bash
python tests/fixtures/snowflake/_gen_manifest.py   # rewrites target/manifest.json
```

Edit the model shape there, not the JSON by hand.

## The model

`stg_tpch_customers` (unique_id `model.signalforge_test_tpch.stg_tpch_customers`)
is a source-as-model passthrough over `TPCH_SF1.CUSTOMER`. Its `alias` is
overridden to `customer` so `relation_name` resolves directly to
`SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER` (no `dbt run` materialisation needed).

It exposes a curated subset of **real, unrenamed** TPCH source columns
(`c_custkey`, `c_name`, `c_nationkey`, `c_phone`, `c_acctbal`,
`c_mktsegment`). Under `oneshot` sampling the prune stage queries the read-only
source table directly, so every declared column **must exist on the source** —
a renamed or engineered (`'us' AS region`) column would compile to an
"invalid identifier" and route to `kept-without-evidence`, never
`always-passes`.

The deterministic prune drop signal therefore relies on a **natural NOT NULL**
column rather than engineered literals (mirroring the Austin bikeshare
fixture's natural-NOT-NULL pattern, issue #10): `c_custkey` is the TPCH
primary key — every source row has a value, so a drafted `not_null` on it
returns zero failing rows → mathematically always-pass → dropped by prune.

## Maintainer-only live regeneration

To reproduce the manifest from a genuine `dbt parse` against live Snowflake
(verification only — the committed seed is the source of truth):

1. Fill in real connection fields in `profiles.yml` (`account` / `user` /
   `role` / `warehouse`; `database: SNOWFLAKE_SAMPLE_DATA`, `schema: TPCH_SF1`).
   `SNOWFLAKE_SAMPLE_DATA` is a read-only shared database present in every
   Snowflake account.
2. Run (pinned, ephemeral, mirrors `tests/fixtures/regenerate.sh`):

   ```bash
   DBT_PROFILES_DIR="$(pwd)/tests/fixtures/snowflake" \
     uvx --python 3.11 \
       --from "dbt-snowflake==1.8.*" --with "dbt-core==1.8.*" \
       dbt parse --project-dir tests/fixtures/snowflake
   ```

3. Strip non-deterministic fields (`generated_at`, `invocation_id`,
   `user_id`, ...) with `jq` before committing, then diff against the
   hand-crafted seed to confirm the shape still matches.

The committed seed sets those non-deterministic fields to `null` / `0` and uses
an all-zero checksum so the JSON is byte-stable across regenerations.
