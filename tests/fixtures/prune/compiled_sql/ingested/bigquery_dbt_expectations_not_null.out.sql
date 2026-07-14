




    with grouped_expression as (
    select
        
        
    
  order_id is not null as expression


    from `_SESSION._sf_sample_0f1e2d3c4b5a6978`
    

),
validation_errors as (

    select
        *
    from
        grouped_expression
    where
        not(expression = true)

)

select *
from validation_errors



