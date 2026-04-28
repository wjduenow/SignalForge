-- Auto-generated mart that refs three staging models.
with a as (select * from {{ ref('stg_event_012') }}),
     b as (select * from {{ ref('stg_event_013') }}),
     c as (select * from {{ ref('stg_event_014') }})
select a.event_id, a.user_id, a.occurred_at,
       b.model_index as b_idx, c.model_index as c_idx
from a join b using (event_id) join c using (event_id)
