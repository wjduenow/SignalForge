




    with grouped_expression as (
    select
        
        
    
  order_id is not null as expression


    from "ANALYTICS"."PUBLIC"."_SF_SAMPLE_0F1E2D3C4B5A6978"
    

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



