




    with grouped_expression as (
    select
        
        
    
  order_id is not null as expression


    from `main`.`default`.`orders`
    

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



