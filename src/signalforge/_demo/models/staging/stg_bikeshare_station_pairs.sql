-- Source-as-model: the manifest aliases this model to `bikeshare_trips`
-- so its relation_name resolves directly to the public source table
-- (same trick as `stg_bikeshare_trips`). SignalForge runs queries against
-- the materialised relation; without `dbt run` against a writable billing
-- project this keeps the smoke test a single command (issue #10 Path A).
--
-- The natural multi-column GROUP BY pattern on
-- `(start_station_id, end_station_id, subscriber_type)` advertises a
-- composite natural-key shape to the drafter: each row in this model
-- represents one unique station-pair × subscriber-type combination from
-- the source. The pattern is engineered to steer `claude-sonnet-4-6`
-- toward proposing the structured `unique_combination` test (#170, AC-1)
-- rather than a freeform `custom_sql GROUP BY HAVING COUNT(*) > 1`.
--
-- Real columns from the source table only — no engineered literal/COALESCE
-- columns, because the source-as-model alias means the model's relation
-- resolves to the source table itself, not a materialised view of this
-- SELECT body. See `.claude/rules/testing-signal.md` §
-- "WHERE the always-pass column must live depends on whether the model
-- is materialised" for the gotcha that ruled out literal columns here.
SELECT
    start_station_id,
    end_station_id,
    subscriber_type,
    COUNT(*) AS trip_count,
    SUM(duration_minutes) AS total_duration_minutes
FROM {{ source('austin_bikeshare', 'bikeshare_trips') }}
GROUP BY
    start_station_id,
    end_station_id,
    subscriber_type
