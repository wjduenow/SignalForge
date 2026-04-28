-- Mart: refs the staging user model (proves ref(...) edge in the manifest).
select
    user_id,
    email,
    created_at
from {{ ref('stg_users') }}
