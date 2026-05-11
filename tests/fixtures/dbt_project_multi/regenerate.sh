#!/usr/bin/env bash
# Regenerate tests/fixtures/dbt_project_multi/target/manifest.json by running
# `dbt parse` against this offline fixture via an ephemeral `uvx` install of
# dbt-core. Mirrors tests/fixtures/dbt_project_austin/regenerate.sh; this
# fixture does NOT touch BigQuery (no source(), no real warehouse), so the
# regen only needs dbt-core + a no-op adapter (dbt-duckdb is fine — the
# manifest is fully resolvable from the model SQL and dbt_project.yml).
#
# Strips the same five non-deterministic top-level metadata fields the sibling
# scripts strip so the committed JSON diffs cleanly across regenerations.
#
#   1. cleans any prior `target/` artefacts so the run is hermetic
#   2. runs `uvx --from dbt-duckdb==1.8.* --with dbt-core==1.8.* dbt parse`
#      against tests/fixtures/dbt_project_multi/ with DBT_PROFILES_DIR set to
#      this directory (a sibling profiles.yml is NOT required because `dbt
#      parse` does not connect to the warehouse — but if dbt complains, drop
#      a minimal duckdb profile next to this script)
#   3. strips `metadata.{generated_at, invocation_id, user_id,
#      send_anonymous_usage_stats, adapter_type}` and zeroes `metadata.env`
#      via `jq` (mirrors tests/fixtures/regenerate.sh's scrub list)
#   4. moves the scrubbed file back to `target/manifest.json`
#
# Idempotent: safe to re-run.
#
# DEC-013 of plans/super/37-multi-model-select.md.
#
# Requirements:
#   * `uvx` (https://docs.astral.sh/uv/) and `jq` on PATH.
#
# NOT invoked by CI. Maintainer-only step; the committed manifest is what CI
# and the in-process loads test (`tests/manifest/test_multi_fixture_loads.py`)
# consume. The committed manifest was hand-crafted in US-006; this script is
# the documented regeneration path for when dbt's manifest schema evolves.

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

echo "==> Generating multi-model manifest.json with dbt-duckdb==1.8.*"
clean_target

(
  cd "${PROJECT_DIR}"
  DBT_PROFILES_DIR="${PROJECT_DIR}" uvx \
    --from "dbt-duckdb==1.8.*" \
    --with "dbt-core==1.8.*" \
    dbt parse --project-dir . --profiles-dir .
)

if [[ ! -f "${TARGET_DIR}/manifest.json" ]]; then
  echo "ERROR: dbt parse did not produce ${TARGET_DIR}/manifest.json" >&2
  exit 1
fi

scrub_manifest "${TARGET_DIR}/manifest.json" "${TARGET_DIR}/manifest.json"

rm -f \
  "${TARGET_DIR}/partial_parse.msgpack" \
  "${TARGET_DIR}/perf_info.json" \
  "${TARGET_DIR}/semantic_manifest.json" \
  "${TARGET_DIR}/graph.gpickle" \
  "${TARGET_DIR}/graph_summary.json" \
  "${TARGET_DIR}/run_results.json"

echo "==> Done. Committed manifest:"
ls -1 "${TARGET_DIR}"
