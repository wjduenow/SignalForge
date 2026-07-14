select id
from `_SESSION._sf_sample_0f1e2d3c4b5a6978`  -- dbt's trailing comment
where amount > (
    select 0  /* a block comment naming orders */
)
