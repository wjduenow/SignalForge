-- Mart model: refs the staging models so the parent_map/child_map is
-- non-trivial. Tagged `marts` (set in dbt_project.yml).
with combined as (
    select
        user_id,
        cast(null as numeric) as amount
    from {{ ref('stg_a') }}
    union all
    select
        user_id,
        amount
    from {{ ref('stg_b') }}
)

select
    user_id,
    sum(amount) as total_amount
from combined
group by user_id
