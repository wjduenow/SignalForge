-- Hand-crafted TPCH seed model (issue #124, US-003). The model's `alias`
-- is overridden to `customer` so its relation resolves directly to the
-- read-only source SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER (SignalForge
-- runs against the materialised relation; no `dbt run` needed). Declares
-- only REAL TPCH source columns — the `always-passes` drop signal for the
-- full-pipeline e2e (US-005) relies on a NATURAL NOT NULL column
-- (`c_custkey`, the primary key) because under `oneshot` prune queries the
-- source table directly (mirrors the Austin bikeshare natural-NOT-NULL
-- pattern).
SELECT
    c_custkey,
    c_name,
    c_nationkey,
    c_phone,
    c_acctbal,
    c_mktsegment
FROM {{ source('tpch_sf1', 'customer') }}
