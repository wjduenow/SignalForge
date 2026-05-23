-- A singular test for the `orders` model: amounts must be positive.
select *
from {{ ref('orders') }}
where amount <= 0
