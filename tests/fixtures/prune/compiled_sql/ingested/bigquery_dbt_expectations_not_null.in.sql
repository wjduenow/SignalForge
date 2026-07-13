




    with grouped_expression as (
    select
        
        
    
  order_id is not null as expression


    from `fake_project`.`dataset`.`orders`
    

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



