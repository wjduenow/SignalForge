-- Staging model B: varied column shape (no literal-source column).
-- Used alongside stg_a.sql to exercise multi-model selection across the
-- `staging` tag.
select
    1 as order_id,
    1 as user_id,
    100.00 as amount,
    cast('2025-01-01' as timestamp) as ordered_at
