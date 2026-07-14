select a.id
from "ANALYTICS"."PUBLIC"."_SF_SAMPLE_0F1E2D3C4B5A6978" as a
join "ANALYTICS"."PUBLIC"."_SF_SAMPLE_0F1E2D3C4B5A6978" as b on a.customer_id = b.customer_id
where a.id <> b.id
