-- A singular test that uses unsupported Jinja (a {% %} control block and a
-- macro call), which the bounded resolver cannot evaluate. References the
-- `orders` model, but must be skip-recorded as malformed-supported-test.
{% set threshold = 0 %}
select *
from {{ ref('orders') }}
where amount < {{ threshold }}
