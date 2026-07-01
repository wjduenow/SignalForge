# nyctaxi manifest seed (Databricks live e2e fixture)

Hand-crafted dbt project + `manifest.json` describing one model over the
Databricks Unity Catalog sample table `samples.nyctaxi.trips`. Consumed by the
Databricks test harness (issue #226): the loads-only test (added by US-002, runs
in the default suite) and the gated full-pipeline live e2e.

## Why the manifest is hand-crafted, not generated

`target/manifest.json` is **hand-crafted**, NOT produced by a live `dbt parse`.
Ralph workers and CI cannot reach a live Databricks workspace, so the seed is
committed verbatim and validated in-process by
`signalforge.manifest.load(<fixture_dir>)` — see
`.claude/rules/testing-signal.md` § "Hand-crafted manifest seed when workers
can't run live tooling". The generator that emits the JSON from a declarative
dict lives at `_gen_manifest.py` in this directory:

```bash
python tests/fixtures/databricks/_gen_manifest.py   # rewrites target/manifest.json
```

Edit the model shape there, not the JSON by hand.

## The model

`stg_nyctaxi_trips` (unique_id `model.signalforge_test_nyctaxi.stg_nyctaxi_trips`)
is a source-as-model passthrough over `samples.nyctaxi.trips`. Its `alias` is
overridden to `trips` so `relation_name` resolves directly to
`samples.nyctaxi.trips` (no `dbt run` materialisation needed).

It exposes a curated subset of **real, unrenamed** nyctaxi source columns
(`tpep_pickup_datetime`, `tpep_dropoff_datetime`, `trip_distance`,
`fare_amount`, `pickup_zip`, `dropoff_zip`). Under `oneshot` sampling the prune
stage queries the read-only source table directly, so every declared column
**must exist on the source** — a renamed or engineered (`'us' AS region`)
column would compile to an "invalid identifier" and route to
`kept-without-evidence`, never `always-passes`.

The deterministic prune drop signal therefore relies on a **natural NOT NULL**
column rather than engineered literals (mirroring the Austin bikeshare
fixture's natural-NOT-NULL pattern, issue #10): `tpep_pickup_datetime` is the
trip pickup timestamp — every source row has a value, so a drafted `not_null`
on it returns zero failing rows → mathematically always-pass → dropped by
prune. (The exact always-pass column is confirmed during the maintainer live
pass; all six real columns are declared now.)

## Maintainer-only live regeneration

To reproduce the manifest from a genuine `dbt parse` against live Databricks
(verification only — the committed seed is the source of truth):

1. Fill in real connection fields in `profiles.yml` (`host` / `http_path` /
   `token`; `catalog: samples`, `schema: nyctaxi`). `samples` is a read-only
   shared catalog present in every Databricks workspace.
2. Run (pinned, ephemeral, mirrors `tests/fixtures/regenerate.sh`):

   ```bash
   DBT_PROFILES_DIR="$(pwd)/tests/fixtures/databricks" \
     uvx --python 3.11 \
       --from "dbt-databricks==1.8.*" --with "dbt-core==1.8.*" \
       dbt parse --project-dir tests/fixtures/databricks
   ```

3. Strip non-deterministic fields (`generated_at`, `invocation_id`,
   `user_id`, ...) with `jq` before committing, then diff against the
   hand-crafted seed to confirm the shape still matches.

The committed seed sets those non-deterministic fields to `null` / `0` and uses
an all-zero checksum so the JSON is byte-stable across regenerations.
