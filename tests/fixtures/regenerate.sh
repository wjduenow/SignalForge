#!/usr/bin/env bash
# Regenerate the four small dbt project manifests, one per supported schema
# version (v9..v12), using ephemeral dbt-core installs via `uvx`.
#
# Each invocation:
#   1. cleans any prior `target/manifest.json` so the run is hermetic
#   2. runs `dbt parse` against tests/fixtures/dbt_project_small/
#   3. strips non-deterministic timestamp / invocation_id fields with `jq`
#   4. moves target/manifest.json -> target/manifest_v<N>.json
#
# Idempotent: safe to re-run. The committed JSON files are deterministic —
# diffs between runs should be empty barring intentional changes.
#
# DEC-009 / DEC-012: dbt-core==1.5.x (v9), 1.6.x (v10), 1.7.x (v11), 1.8.x (v12).
# 1.5 / 1.6 require Python <= 3.11 (no `distutils` in 3.12+); we pin via --python.
#
# Requirements: `uvx` (https://docs.astral.sh/uv/) and `jq` on PATH.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}/dbt_project_small"
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

# Strip non-deterministic top-level metadata fields so the committed JSON
# diffs cleanly across regenerations. Keep `dbt_schema_version` (load-bearing
# for version detection) and `dbt_version` (handy for debugging).
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

run_version() {
  local schema_version="$1"   # e.g. v12
  local dbt_pin="$2"          # e.g. 1.8.*
  local python_pin="$3"       # e.g. 3.11 (pass empty string for default)

  echo "==> Generating manifest_${schema_version}.json with dbt-core==${dbt_pin}"
  clean_target

  local uvx_args=(--from "dbt-duckdb==${dbt_pin}" --with "dbt-core==${dbt_pin}")
  if [[ -n "${python_pin}" ]]; then
    uvx_args=(--python "${python_pin}" "${uvx_args[@]}")
  fi

  (
    cd "${PROJECT_DIR}"
    DBT_PROFILES_DIR="${PROJECT_DIR}" uvx "${uvx_args[@]}" dbt parse
  )

  if [[ ! -f "${TARGET_DIR}/manifest.json" ]]; then
    echo "ERROR: dbt parse did not produce ${TARGET_DIR}/manifest.json" >&2
    exit 1
  fi

  scrub_manifest "${TARGET_DIR}/manifest.json" "${TARGET_DIR}/manifest_${schema_version}.json"
  rm -f "${TARGET_DIR}/manifest.json"
}

run_version v9  "1.5.*" "3.11"
run_version v10 "1.6.*" "3.11"
run_version v11 "1.7.*" ""
run_version v12 "1.8.*" ""

# Final clean of stragglers (perf_info etc. — we never commit them).
clean_target

echo "==> Done. Committed manifests:"
ls -1 "${TARGET_DIR}"
