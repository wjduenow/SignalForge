# Diff: model.shop.dim_customers

**kept=1**  **kept-uncertain=0**  **dropped=0**  **flagged=0**

| Tier | Artifact | Test | Reason | Score | Why |
| --- | --- | --- | --- | --- | --- |
| kept | test.column.total_amount.custom_sql | custom_sql |  | — | business rule: total_amount must be non-negative. |

```diff
--- a/models/dim_customers.yml
+++ b/models/dim_customers.yml
@@ -1,5 +1,8 @@
 version: 2
 models:
   - name: dim_customers
     columns:
       - name: customer_id
+        description: Surrogate key.
+        tests:
+          - not_null
```

## Proposed test files

### `tests/dim_customers__total_amount_custom_sql_a1b2c3d4.sql`

```sql
-- signalforge:generated a1b2c3d4

select *
from {{ ref('dim_customers') }}
where total_amount < 0
```

### `tests/dim_customers__custom_sql_deadbeef.sql`

```sql
-- signalforge:generated deadbeef

select count(*) as n
from {{ ref('dim_customers') }}
having n = 0
```