-- a self-join: the relation appears TWICE; both must land on the temp
select a.id
from `fake_project`.`dataset`.`orders` as a
join `fake_project`.`dataset`.`orders` as b
  on a.customer_id = b.customer_id and a.id <> b.id
where a.status = 'BAD'
