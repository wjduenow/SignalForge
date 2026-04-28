-- Mart: a synthetic fact built from a CTE so it can compile even though the
-- staging order model is disabled. Refs dim_users to keep the ref graph
-- non-trivial.
with synthetic_orders as (
    select
        1 as order_id,
        user_id,
        cast('2025-01-01' as date) as ordered_at,
        100.00 as amount
    from {{ ref('dim_users') }}
)

select
    order_id,
    user_id,
    ordered_at,
    amount
from synthetic_orders
