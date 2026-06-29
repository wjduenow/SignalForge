"""One-shot generator for the hand-crafted nyctaxi manifest seed.

Not part of the test suite. Run once to (re)emit ``manifest.json`` from the
declarative dict below, then commit the JSON. Kept alongside the fixture so a
future maintainer can regenerate the deterministic seed without re-deriving the
node shape from a live ``dbt parse``. See ``README.md`` in this directory for
the maintainer-only live-Databricks reproduction note.

    python tests/fixtures/databricks/_gen_manifest.py
"""

from __future__ import annotations

import json
from pathlib import Path

_HERE: Path = Path(__file__).resolve().parent

_PROJECT: str = "signalforge_test_nyctaxi"
_MODEL_UID: str = f"model.{_PROJECT}.stg_nyctaxi_trips"
_SOURCE_UID: str = f"source.{_PROJECT}.nyctaxi.trips"

# Raw SQL: a curated subset of REAL, UNRENAMED samples.nyctaxi.trips columns.
# The model's ``alias`` is overridden to ``trips`` so its relation resolves
# directly to the read-only Unity Catalog sample ``samples.nyctaxi.trips`` —
# under ``oneshot`` sampling SignalForge prunes against that SOURCE table, so
# every declared column MUST exist on it (a renamed/engineered column would
# compile to an "invalid identifier" and route to kept-without-evidence, never
# always-passes). The ``always-passes`` drop signal for the full-pipeline e2e
# therefore relies on a NATURAL NOT NULL source column — ``tpep_pickup_datetime``,
# the trip pickup timestamp — rather than engineered literals (mirrors the
# Austin bikeshare natural-NOT-NULL pattern; see tests/fixtures/dbt_project_austin).
_RAW_CODE: str = (
    "-- Hand-crafted nyctaxi seed model (issue #226, US-001). The model's `alias`\n"
    "-- is overridden to `trips` so its relation resolves directly to the\n"
    "-- read-only Unity Catalog sample `samples.nyctaxi.trips` (SignalForge\n"
    "-- runs against the materialised relation; no `dbt run` needed). Declares\n"
    "-- only REAL nyctaxi source columns — the `always-passes` drop signal for\n"
    "-- the full-pipeline e2e relies on a NATURAL NOT NULL column\n"
    "-- (`tpep_pickup_datetime`, the pickup timestamp) because under `oneshot`\n"
    "-- prune queries the source table directly (mirrors the Austin bikeshare\n"
    "-- natural-NOT-NULL pattern).\n"
    "SELECT\n"
    "    tpep_pickup_datetime,\n"
    "    tpep_dropoff_datetime,\n"
    "    trip_distance,\n"
    "    fare_amount,\n"
    "    pickup_zip,\n"
    "    dropoff_zip\n"
    "FROM {{ source('nyctaxi', 'trips') }}\n"
)


def _col(name: str, description: str) -> dict[str, object]:
    return {
        "name": name,
        "description": description,
        "meta": {},
        "data_type": None,
        "constraints": [],
        "quote": None,
        "tags": [],
    }


_MODEL_COLUMNS: dict[str, dict[str, object]] = {
    "tpep_pickup_datetime": _col(
        "tpep_pickup_datetime",
        "Trip pickup timestamp. NATURAL NOT NULL: every source row has a value, "
        "so a drafted `not_null` on it returns zero failing rows and prunes as "
        "always-passes (the full-pipeline e2e drop signal).",
    ),
    "tpep_dropoff_datetime": _col(
        "tpep_dropoff_datetime",
        "Trip dropoff timestamp. TIMESTAMP; non-null in the nyctaxi sample.",
    ),
    "trip_distance": _col(
        "trip_distance",
        "Trip distance in miles. DOUBLE; non-null.",
    ),
    "fare_amount": _col(
        "fare_amount",
        "Fare amount in USD. DOUBLE; may be small but is non-null.",
    ),
    "pickup_zip": _col(
        "pickup_zip",
        "ZIP code of the pickup location. INT; non-null.",
    ),
    "dropoff_zip": _col(
        "dropoff_zip",
        "ZIP code of the dropoff location. INT; non-null.",
    ),
}

_SOURCE_COLUMNS: dict[str, dict[str, object]] = {
    "tpep_pickup_datetime": _col("tpep_pickup_datetime", "Trip pickup timestamp."),
    "tpep_dropoff_datetime": _col("tpep_dropoff_datetime", "Trip dropoff timestamp."),
    "trip_distance": _col("trip_distance", "Trip distance in miles."),
    "fare_amount": _col("fare_amount", "Fare amount in USD."),
    "pickup_zip": _col("pickup_zip", "ZIP code of the pickup location."),
    "dropoff_zip": _col("dropoff_zip", "ZIP code of the dropoff location."),
}

_RELATION_NAME: str = "samples.nyctaxi.trips"

_MANIFEST: dict[str, object] = {
    "metadata": {
        "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
        "dbt_version": "1.8.9",
        "generated_at": None,
        "invocation_id": None,
        "env": {},
        "project_name": _PROJECT,
        "project_id": "signalforgenyctaxi00000000000000",
        "user_id": None,
        "send_anonymous_usage_stats": None,
        "adapter_type": None,
    },
    "nodes": {
        _MODEL_UID: {
            "database": "samples",
            "schema": "nyctaxi",
            "name": "stg_nyctaxi_trips",
            "resource_type": "model",
            "package_name": _PROJECT,
            "path": "staging/stg_nyctaxi_trips.sql",
            "original_file_path": "models/staging/stg_nyctaxi_trips.sql",
            "unique_id": _MODEL_UID,
            "fqn": [_PROJECT, "staging", "stg_nyctaxi_trips"],
            "alias": "trips",
            "checksum": {
                "name": "sha256",
                "checksum": "0" * 64,
            },
            "config": {
                "enabled": True,
                "alias": None,
                "schema": None,
                "database": None,
                "tags": [],
                "meta": {},
                "group": None,
                "materialized": "view",
                "incremental_strategy": None,
                "persist_docs": {},
                "post-hook": [],
                "pre-hook": [],
                "quoting": {},
                "column_types": {},
                "full_refresh": None,
                "unique_key": None,
                "on_schema_change": "ignore",
                "on_configuration_change": "apply",
                "grants": {},
                "packages": [],
                "docs": {"show": True, "node_color": None},
                "contract": {"enforced": False, "alias_types": True},
                "access": "protected",
            },
            "tags": [],
            "description": (
                "Source-as-model passthrough over the Databricks Unity Catalog "
                "sample table `samples.nyctaxi.trips`. Each row is one NYC taxi "
                "trip. Exposes a curated subset of REAL source columns so the "
                "SignalForge generate-pipeline live e2e has a deterministic "
                "prune drop signal — a drafted `not_null` on the natural NOT "
                "NULL pickup timestamp `tpep_pickup_datetime` prunes as "
                "always-passes. The model's `alias` is overridden to `trips` so "
                "`relation_name` resolves directly to `samples.nyctaxi.trips`, "
                "sidestepping a `dbt run` materialisation step."
            ),
            "columns": _MODEL_COLUMNS,
            "meta": {},
            "group": None,
            "docs": {"show": True, "node_color": None},
            "patch_path": None,
            "build_path": None,
            "unrendered_config": {"materialized": "view"},
            "created_at": 0,
            "relation_name": _RELATION_NAME,
            "raw_code": _RAW_CODE,
            "language": "sql",
            "refs": [],
            "sources": [["nyctaxi", "trips"]],
            "metrics": [],
            "depends_on": {"macros": [], "nodes": [_SOURCE_UID]},
            "compiled_path": None,
            "contract": {"enforced": False, "alias_types": True, "checksum": None},
            "access": "protected",
            "constraints": [],
            "version": None,
            "latest_version": None,
            "deprecation_date": None,
        }
    },
    "sources": {
        _SOURCE_UID: {
            "database": "samples",
            "schema": "nyctaxi",
            "name": "trips",
            "resource_type": "source",
            "package_name": _PROJECT,
            "path": "models/staging/sources.yml",
            "original_file_path": "models/staging/sources.yml",
            "unique_id": _SOURCE_UID,
            "fqn": [_PROJECT, "nyctaxi", "trips"],
            "source_name": "nyctaxi",
            "source_description": "Databricks Unity Catalog nyctaxi sample dataset.",
            "loader": "",
            "identifier": "trips",
            "quoting": {
                "database": None,
                "schema": None,
                "identifier": None,
                "column": None,
            },
            "loaded_at_field": None,
            "freshness": {
                "warn_after": {"count": None, "period": None},
                "error_after": {"count": None, "period": None},
                "filter": None,
            },
            "external": None,
            "description": "One row per NYC taxi trip.",
            "columns": _SOURCE_COLUMNS,
            "meta": {},
            "source_meta": {},
            "tags": [],
            "config": {"enabled": True},
            "patch_path": None,
            "unrendered_config": {},
            "relation_name": _RELATION_NAME,
            "created_at": 0,
        }
    },
    "macros": {},
    "docs": {},
    "exposures": {},
    "metrics": {},
    "groups": {},
    "selectors": {},
    "disabled": {},
    "parent_map": {
        _MODEL_UID: [_SOURCE_UID],
        _SOURCE_UID: [],
    },
    "child_map": {
        _MODEL_UID: [],
        _SOURCE_UID: [_MODEL_UID],
    },
    "group_map": {},
    "saved_queries": {},
    "semantic_models": {},
    "unit_tests": {},
}


def main() -> None:
    target_dir = _HERE / "target"
    target_dir.mkdir(exist_ok=True)
    out = target_dir / "manifest.json"
    out.write_text(json.dumps(_MANIFEST, indent=4) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
