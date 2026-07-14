



    with grouped_expression as (
    select
        
        
    
  
( 1=1 and count(*) >= 1 and count(*) <= 100
)
 as expression


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





