-- Synthetic two-row orders fact (fixture only). Literal SELECT so `dbt compile`
-- needs no seed and the column values are known, which is what lets the
-- dbt-expectations tests exercise each #154 prune outcome deterministically:
--   * amount is 100 / 200  -> impossible-bounds test returns failing rows (KEPT)
--                           -> vacuous-bounds test returns none    (ALWAYS-PASSES)
--   * order_id is 1 / 2     -> never null                          (ALWAYS-PASSES)
select 1 as order_id, 100.00 as amount, cast('2020-01-01' as timestamp) as ordered_at
union all
select 2 as order_id, 200.00 as amount, cast('2020-06-01' as timestamp) as ordered_at
