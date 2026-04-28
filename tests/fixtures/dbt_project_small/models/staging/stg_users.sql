-- Staging model: pulls from the raw users source (proves source(...) edge).
select
    id as user_id,
    email,
    created_at
from {{ source('raw', 'users') }}
