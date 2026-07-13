select id
from `fake_project`.`dataset`.`orders`  -- dbt's trailing comment
where amount > (
    select 0  /* a block comment naming orders */
)
