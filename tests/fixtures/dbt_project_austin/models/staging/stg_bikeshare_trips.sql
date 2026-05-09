-- Literal/COALESCE'd columns deliberately included to give the LLM at least one
-- mathematically-guaranteed always-pass test to drop (issue #10 AC).
SELECT
    trip_id,
    subscriber_type,
    bikeid,
    start_time,
    start_station_id,
    end_station_id,
    duration_minutes,
    'austin' AS region,
    COALESCE(start_time, TIMESTAMP '1970-01-01 00:00:00 UTC') AS start_time_safe
FROM {{ source('austin_bikeshare', 'bikeshare_trips') }}
LIMIT 100000
