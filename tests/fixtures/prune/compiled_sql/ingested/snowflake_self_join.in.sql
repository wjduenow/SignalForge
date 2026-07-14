select a.id
from "ANALYTICS"."PUBLIC"."ORDERS" as a
join "ANALYTICS"."PUBLIC"."ORDERS" as b on a.customer_id = b.customer_id
where a.id <> b.id
