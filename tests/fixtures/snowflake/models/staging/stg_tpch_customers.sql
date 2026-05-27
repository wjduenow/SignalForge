-- Hand-crafted TPCH seed model (issue #124, US-003). Targets the
-- Snowflake sample dataset SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER.
-- Two engineered columns guarantee an always-pass drafted not_null so
-- the full generate-pipeline live e2e (US-005) has a deterministic
-- drop signal (mirrors the Austin bikeshare 'region' literal trick).
SELECT
    c_custkey AS customer_id,
    c_name AS customer_name,
    c_nationkey AS nation_id,
    c_acctbal AS account_balance,
    'us' AS region,
    COALESCE(c_acctbal, 0) AS acctbal_safe
FROM {{ source('tpch_sf1', 'customer') }}
