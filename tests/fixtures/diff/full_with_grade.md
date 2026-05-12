# Diff: model.shop.dim_customers

**kept=2**  **kept-uncertain=1**  **dropped=1**  **flagged=1**

| Tier | Artifact | Test | Reason | Score | Why |
| --- | --- | --- | --- | --- | --- |
| kept | column.customer_id.description |  |  | 0.85 | Description added; passed all grading criteria. |
| kept | test.column.customer_id.not_null | not_null |  | — | Test returned non-zero failing rows on the warehouse sample. |
| kept-uncertain | test.column.email.unique | unique |  | — | total prune budget exceeded before evaluation |
| dropped | test.column.customer_id.unique | unique | always-passes | — | Test returned zero failing rows on the representative sample. |
| flagged | column.email.description |  |  | 0.45 | Grading score 0.45 below threshold 0.50. |

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