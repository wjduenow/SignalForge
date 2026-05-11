-- Staging model A: engineered determinism for the always-passes drop path.
--
-- The `'austin' AS source` literal column is mathematically guaranteed to be
-- non-NULL, so any LLM-drafted `not_null` test on `source` will always pass
-- on a representative sample. The prune engine routes that decision to
-- DropReason="always-passes" (DEC-006 of plans/super/6-prune-engine.md).
-- This is the same trick used by tests/fixtures/dbt_project_austin/
-- (testing-signal.md, "Engineered determinism for LLM-driven assertions").
select
    1 as user_id,
    'austin' as source,
    cast('2025-01-01' as date) as ingested_at
