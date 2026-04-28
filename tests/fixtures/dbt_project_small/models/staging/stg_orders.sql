-- Staging model: synthetic seed-shaped query, no upstream ref/source.
-- Disabled deliberately so the loader sees a model in the `disabled` parallel
-- dict (one of US-002's acceptance criteria: at least one disabled model).
{{ config(enabled=false) }}

select
    1 as order_id,
    1 as user_id,
    cast('2025-01-01' as date) as ordered_at,
    100.00 as amount
