-- a self-join: the relation appears TWICE; both must land on the temp
select a.id
from `_SESSION._sf_sample_0f1e2d3c4b5a6978` as a
join `_SESSION._sf_sample_0f1e2d3c4b5a6978` as b
  on a.customer_id = b.customer_id and a.id <> b.id
where a.status = 'BAD'
