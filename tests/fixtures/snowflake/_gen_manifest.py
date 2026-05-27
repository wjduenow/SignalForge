"""One-shot generator for the hand-crafted TPCH manifest seed.

Not part of the test suite. Run once to (re)emit ``manifest.json`` from the
declarative dict below, then commit the JSON. Kept alongside the fixture so a
future maintainer can regenerate the deterministic seed without re-deriving the
node shape from a live ``dbt parse``. See ``README.md`` in this directory for
the maintainer-only live-Snowflake reproduction note.

    python tests/fixtures/snowflake/_gen_manifest.py
"""

from __future__ import annotations

import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent

_PROJECT = "signalforge_test_tpch"
_MODEL_UID = f"model.{_PROJECT}.stg_tpch_customers"
_SOURCE_UID = f"source.{_PROJECT}.tpch_sf1.customer"

# Raw SQL: a curated subset of TPCH_SF1.CUSTOMER columns plus TWO engineered
# always-pass columns:
#   * ``'us' AS region`` — a string literal; a drafted not_null on it can
#     never produce a failing row (mathematically always-pass).
#   * ``COALESCE(c_acctbal, 0) AS acctbal_safe`` — a NULL-guarded column; a
#     drafted not_null on it is likewise guaranteed to always-pass.
# Mirrors the Austin bikeshare engineered-determinism pattern (issue #10).
_RAW_CODE = (
    "-- Hand-crafted TPCH seed model (issue #124, US-003). Targets the\n"
    "-- Snowflake sample dataset SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER.\n"
    "-- Two engineered columns guarantee an always-pass drafted not_null so\n"
    "-- the full generate-pipeline live e2e (US-005) has a deterministic\n"
    "-- drop signal (mirrors the Austin bikeshare 'region' literal trick).\n"
    "SELECT\n"
    "    c_custkey AS customer_id,\n"
    "    c_name AS customer_name,\n"
    "    c_nationkey AS nation_id,\n"
    "    c_acctbal AS account_balance,\n"
    "    'us' AS region,\n"
    "    COALESCE(c_acctbal, 0) AS acctbal_safe\n"
    "FROM {{ source('tpch_sf1', 'customer') }}\n"
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


_MODEL_COLUMNS = {
    "customer_id": _col(
        "customer_id",
        "Unique key for each customer (source column `c_custkey`). Natural "
        "primary key; no two source rows share a value.",
    ),
    "customer_name": _col(
        "customer_name",
        "Customer display name (source column `c_name`), e.g. "
        "`Customer#000000001`. Free-form STRING.",
    ),
    "nation_id": _col(
        "nation_id",
        "Foreign key into the TPCH `NATION` table (source column "
        "`c_nationkey`). Resolves the customer's nation.",
    ),
    "account_balance": _col(
        "account_balance",
        "Customer account balance (source column `c_acctbal`). NUMBER; may be "
        "negative in the TPCH dataset.",
    ),
    "region": _col(
        "region",
        "Engineered literal `'us' AS region` — a constant STRING. A drafted "
        "not_null on this column is mathematically always-pass (the literal "
        "is never NULL), so it exercises the prune always-pass drop path.",
    ),
    "acctbal_safe": _col(
        "acctbal_safe",
        "Engineered NULL-guarded balance `COALESCE(c_acctbal, 0)`. A drafted "
        "not_null on this column is always-pass because COALESCE removes the "
        "only NULL source.",
    ),
}

_SOURCE_COLUMNS = {
    "c_custkey": _col("c_custkey", "Unique customer key."),
    "c_name": _col("c_name", "Customer name."),
    "c_address": _col("c_address", "Customer street address."),
    "c_nationkey": _col("c_nationkey", "Foreign key into NATION."),
    "c_phone": _col("c_phone", "Customer phone number."),
    "c_acctbal": _col("c_acctbal", "Customer account balance."),
    "c_mktsegment": _col("c_mktsegment", "Market segment."),
    "c_comment": _col("c_comment", "Free-form comment."),
}

_RELATION_NAME = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER"

_MANIFEST: dict[str, object] = {
    "metadata": {
        "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
        "dbt_version": "1.8.9",
        "generated_at": None,
        "invocation_id": None,
        "env": {},
        "project_name": _PROJECT,
        "project_id": "signalforgetpch00000000000000000",
        "user_id": None,
        "send_anonymous_usage_stats": None,
        "adapter_type": None,
    },
    "nodes": {
        _MODEL_UID: {
            "database": "SNOWFLAKE_SAMPLE_DATA",
            "schema": "TPCH_SF1",
            "name": "stg_tpch_customers",
            "resource_type": "model",
            "package_name": _PROJECT,
            "path": "staging/stg_tpch_customers.sql",
            "original_file_path": "models/staging/stg_tpch_customers.sql",
            "unique_id": _MODEL_UID,
            "fqn": [_PROJECT, "staging", "stg_tpch_customers"],
            "alias": "customer",
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
                "Source-as-model passthrough over the Snowflake sample "
                "dataset's `TPCH_SF1.CUSTOMER` table. Each row is one TPCH "
                "customer. Exposes a curated subset of source columns plus two "
                "engineered always-pass columns (`region`, `acctbal_safe`) so "
                "the SignalForge generate-pipeline live e2e (US-005) has a "
                "deterministic prune drop signal. The model's `alias` is "
                "overridden to `customer` so `relation_name` resolves directly "
                "to `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER`, sidestepping a "
                "`dbt run` materialisation step."
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
            "sources": [["tpch_sf1", "customer"]],
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
            "database": "SNOWFLAKE_SAMPLE_DATA",
            "schema": "TPCH_SF1",
            "name": "customer",
            "resource_type": "source",
            "package_name": _PROJECT,
            "path": "models/staging/sources.yml",
            "original_file_path": "models/staging/sources.yml",
            "unique_id": _SOURCE_UID,
            "fqn": [_PROJECT, "tpch_sf1", "customer"],
            "source_name": "tpch_sf1",
            "source_description": "Snowflake sample TPCH_SF1 dataset.",
            "loader": "",
            "identifier": "CUSTOMER",
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
            "description": "One row per TPCH customer.",
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
