-- Hand-crafted nyctaxi seed model (issue #226, US-001). The model's `alias`
-- is overridden to `trips` so its relation resolves directly to the
-- read-only Unity Catalog sample samples.nyctaxi.trips (SignalForge
-- runs against the materialised relation; no `dbt run` needed). Declares
-- only REAL nyctaxi source columns — the `always-passes` drop signal for the
-- full-pipeline e2e relies on a NATURAL NOT NULL column
-- (`tpep_pickup_datetime`, the pickup timestamp) because under `oneshot` prune
-- queries the source table directly (mirrors the Austin bikeshare
-- natural-NOT-NULL pattern).
SELECT
    tpep_pickup_datetime,
    tpep_dropoff_datetime,
    trip_distance,
    fare_amount,
    pickup_zip,
    dropoff_zip
FROM {{ source('nyctaxi', 'trips') }}
