-- A singular test for a DIFFERENT model (`customers`): every row must have an
-- email. Ingesting `orders` must NOT pull this test in.
select *
from {{ ref('customers') }}
where email is null
