{#
  Singular test whose compiled body is a bare scalar COUNT (issue 267 US-005):
  exercises the count-of-rows manifest-ingest prune path. A single ref() call
  and no GROUP BY, so it compiles to a bare top-level
  SELECT count(star) FROM "..."."..."."orders" WHERE ... that
  is_prunable_count_scalar graduates (US-001) and the ingest gate (US-002) plus
  compiler restructure (US-003) route to a from_manifest custom_sql candidate
  rather than skip-recording it.

  Determinism: amount is 100.00 / 200.00 in the two fixture rows, so this WHERE
  selects zero rows, count 0, and the 0-equals-pass convention makes it an
  always-passes drop. The value is known from the fixture, not the LLM.

  These are Jinja comments (deliberately NOT SQL line/block comments): dbt
  strips them at compile time, so the compiled body is comment-free and the
  warehouse adapter's validate_test_sql, which rejects SQL comment tokens,
  accepts the restructured body and lets the count-scalar reach a real prune
  verdict.
#}
select count(*) from {{ ref('orders') }} where amount > 1000
