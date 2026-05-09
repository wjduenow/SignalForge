#!/usr/bin/env bash
# Regenerate tests/fixtures/dbt_project_austin/target/manifest.json by running
# `dbt parse` against the live `bigquery-public-data.austin_bikeshare` dataset
# via an ephemeral `uvx` install of dbt-bigquery. Strips the same five
# non-deterministic fields the sibling top-level regen script strips so the
# committed JSON diffs cleanly across regenerations.
#
#   1. cleans any prior `target/` artefacts so the run is hermetic
#   2. runs `uvx --from dbt-bigquery==1.8.* --with dbt-core==1.8.* dbt parse`
#      against tests/fixtures/dbt_project_austin/ with DBT_PROFILES_DIR set to
#      this directory (so the committed `profiles.yml` is picked up)
#   3. strips `metadata.{generated_at, invocation_id, user_id,
#      send_anonymous_usage_stats, adapter_type}` and zeroes `metadata.env`
#      via `jq` (mirrors tests/fixtures/regenerate.sh's scrub list)
#   4. moves the scrubbed file back to `target/manifest.json`
#
# Idempotent: safe to re-run. The committed JSON is intended to diff cleanly
# barring intentional schema changes in dbt.
#
# DEC-004 / DEC-019 / DEC-022 of plans/super/10-e2e-bigquery-smoke.md.
# DEC-019: dbt-bigquery is pinned at `1.8.*` floating; patch versions float,
# minor pin is firm. Bump the minor in lockstep with `dbt-core`.
# DEC-022: this script is a SIBLING of `tests/fixtures/regenerate.sh` (the
# DuckDB-fixture regen). It does NOT modify or replace that script — single
# source of truth per fixture, not per repo.
#
# Requirements:
#   * `uvx` (https://docs.astral.sh/uv/) and `jq` on PATH.
#   * `gcloud auth application-default login` run once (BigQuery uses ADC).
#   * `GOOGLE_CLOUD_PROJECT` environment variable set to the maintainer's
#     billing project (the `austin` profile in `profiles.yml` does not pin a
#     billing project; the BQ SDK reads it from the env at parse time).
#
# NOT invoked by CI. Maintainer-only step; the committed manifest is what CI
# and the in-process loads test (`tests/manifest/test_austin_fixture_loads.py`)
# consume.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"
TARGET_DIR="${PROJECT_DIR}/target"

if ! command -v uvx >/dev/null 2>&1; then
  echo "ERROR: uvx not found on PATH. Install via https://docs.astral.sh/uv/." >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq not found on PATH." >&2
  exit 1
fi

if [[ -z "${GOOGLE_CLOUD_PROJECT:-}" ]]; then
  echo "ERROR: GOOGLE_CLOUD_PROJECT is not set." >&2
  echo "       Set it to your billing project before running this script." >&2
  exit 1
fi

mkdir -p "${TARGET_DIR}"

clean_target() {
  rm -f \
    "${TARGET_DIR}/manifest.json" \
    "${TARGET_DIR}/partial_parse.msgpack" \
    "${TARGET_DIR}/perf_info.json" \
    "${TARGET_DIR}/semantic_manifest.json" \
    "${TARGET_DIR}/graph.gpickle" \
    "${TARGET_DIR}/graph_summary.json" \
    "${TARGET_DIR}/run_results.json"
}

# Strip the same five non-deterministic top-level metadata fields the sibling
# `tests/fixtures/regenerate.sh` strips, plus zero `metadata.env`. Keeps
# `dbt_schema_version` (load-bearing for version detection) and `dbt_version`
# (handy for debugging) intact.
scrub_manifest() {
  local in="$1"
  local out="$2"
  jq '
    .metadata.generated_at = null
    | .metadata.invocation_id = null
    | .metadata.user_id = null
    | .metadata.send_anonymous_usage_stats = null
    | .metadata.adapter_type = null
    | .metadata.env = {}
  ' "$in" >"$out.tmp"
  mv "$out.tmp" "$out"
}

echo "==> Generating Austin manifest.json with dbt-bigquery==1.8.*"
clean_target

(
  cd "${PROJECT_DIR}"
  DBT_PROFILES_DIR="${PROJECT_DIR}" uvx \
    --from "dbt-bigquery==1.8.*" \
    --with "dbt-core==1.8.*" \
    dbt parse --project-dir . --profiles-dir .
)

if [[ ! -f "${TARGET_DIR}/manifest.json" ]]; then
  echo "ERROR: dbt parse did not produce ${TARGET_DIR}/manifest.json" >&2
  exit 1
fi

scrub_manifest "${TARGET_DIR}/manifest.json" "${TARGET_DIR}/manifest.json"

# Final clean of stragglers (perf_info etc. — we never commit them).
rm -f \
  "${TARGET_DIR}/partial_parse.msgpack" \
  "${TARGET_DIR}/perf_info.json" \
  "${TARGET_DIR}/semantic_manifest.json" \
  "${TARGET_DIR}/graph.gpickle" \
  "${TARGET_DIR}/graph_summary.json" \
  "${TARGET_DIR}/run_results.json"

echo "==> Done. Committed manifest:"
ls -1 "${TARGET_DIR}"
