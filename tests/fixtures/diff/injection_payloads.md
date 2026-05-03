# Diff: model.shop.dim_customers

**kept=1**  **dropped=1**  **flagged=1**

| Tier | Artifact | Test | Reason | Score | Why |
| --- | --- | --- | --- | --- | --- |
| kept | column.customer_id.description |  |  | 0.85 | Has EVIL ANSI escape; \`\`\`triple\`\`\` backticks. |
| dropped | test.column.region.accepted_values | accepted_values | always-passes | — | Has &lt;/details&gt; HTML and col &#124; name pipe. |
| flagged | column.email.description |  |  | 0.45 | YAML edge content: --- and !tag stay inert. |

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