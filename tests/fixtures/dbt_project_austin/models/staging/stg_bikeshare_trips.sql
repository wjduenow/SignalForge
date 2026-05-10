-- Source-as-model: the manifest aliases this model to `bikeshare_trips`
-- so its relation_name resolves directly to the public source table.
-- SignalForge runs queries against the materialised relation; without
-- `dbt run` against a writable billing project this keeps the smoke
-- test a single command (issue #10 Path A). The `always-passes` AC then
-- relies on natural NOT NULL columns (`trip_id`, `start_time`) rather
-- than engineered literal/COALESCE columns.
SELECT
    trip_id,
    subscriber_type,
    bike_id,
    start_time,
    start_station_id,
    end_station_id,
    duration_minutes
FROM {{ source('austin_bikeshare', 'bikeshare_trips') }}
