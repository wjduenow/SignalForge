#!/usr/bin/env bash
# Regenerate the small dbt project manifests (one per supported schema version
# v9..v12) AND the #154 dbt-expectations compiled fixture, using ephemeral
# dbt-core installs via `uvx`.
#
# The v9..v12 small-project runs:
#   1. clean any prior `target/manifest.json` so the run is hermetic
#   2. run `dbt parse` against tests/fixtures/dbt_project_small/
#   3. strip non-deterministic timestamp / invocation_id fields with `jq`
#   4. move target/manifest.json -> target/manifest_v<N>.json
#
# The #154 dbt-expectations run (tests/fixtures/dbt_project_expectations/):
#   1. clean any prior target artefacts
#   2. run `dbt deps` (installs dbt-expectations 0.10.4 + dbt_date) THEN
#      `dbt compile` — NOT `dbt parse`. `dbt parse` leaves `compiled_code`
#      null; only `dbt compile` / `dbt build` populates it on TEST nodes, and
#      #154 reads that Jinja-resolved SQL off `resource_type == "test"` nodes.
#   3. scrub the same top-level metadata fields PLUS null every per-node
#      `created_at` epoch (`dbt compile` stamps them; the parse fixtures leave
#      them, but the compiled fixture nulls them so the committed JSON diffs
#      cleanly across regens)
#   4. keep target/manifest.json in place (committed via the fixture-local
#      .gitignore negation, so `signalforge.manifest.load(fixture_dir)` finds
#      it at the default path)
#
# Idempotent: safe to re-run. The committed JSON files are deterministic —
# diffs between runs should be empty barring intentional changes.
#
# DEC-009 / DEC-012: dbt-core==1.5.x (v9), 1.6.x (v10), 1.7.x (v11), 1.8.x (v12).
# 1.5 / 1.6 require Python <= 3.11 (no `distutils` in 3.12+); we pin via --python.
# DEC-017 of plans/super/154-dbt-expectations-prune.md: the compiled fixture is
# built via real `dbt compile` on dbt-duckdb + dbt-expectations (credential-free,
# duckdb is embedded) — `dbt deps` needs network at regen time.
#
# Requirements: `uvx` (https://docs.astral.sh/uv/) and `jq` on PATH; network for
# `dbt deps` on the expectations run.

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
  mv -f "$out.tmp" "$out"
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

# ---------------------------------------------------------------------------
# #154 (US-006): the dbt-expectations compiled fixture.
#
# Distinct from the small-project runs above in three ways:
#   * `dbt deps && dbt compile` (NOT `dbt parse`) — populates `compiled_code`
#     on the `resource_type == "test"` nodes, which is what #154 reads.
#   * broader scrub — also nulls every per-node `created_at` epoch that
#     `dbt compile` stamps (`scrub_compiled_manifest`).
#   * commits `target/manifest.json` in place (fixture-local .gitignore
#     negation) so `signalforge.manifest.load(fixture_dir)` finds it directly.
# ---------------------------------------------------------------------------

EXP_DIR="${SCRIPT_DIR}/dbt_project_expectations"
EXP_TARGET="${EXP_DIR}/target"

# Same top-level metadata scrub as scrub_manifest, PLUS null every per-node
# `created_at` (a wall-clock epoch dbt stamps on every node/macro at parse
# time). `walk` recursively nulls the key wherever it appears.
scrub_compiled_manifest() {
  local in="$1"
  local out="$2"
  jq '
    .metadata.generated_at = null
    | .metadata.invocation_id = null
    | .metadata.user_id = null
    | .metadata.send_anonymous_usage_stats = null
    | .metadata.adapter_type = null
    | .metadata.env = {}
    | walk(if type == "object" and has("created_at") then .created_at = null else . end)
  ' "$in" >"$out.tmp"
  mv -f "$out.tmp" "$out"
}

echo "==> Generating dbt_project_expectations/target/manifest.json via dbt compile (dbt-core==1.8.*)"
rm -f \
  "${EXP_TARGET}/manifest.json" \
  "${EXP_TARGET}/partial_parse.msgpack" \
  "${EXP_TARGET}/perf_info.json" \
  "${EXP_TARGET}/semantic_manifest.json" \
  "${EXP_TARGET}/graph.gpickle" \
  "${EXP_TARGET}/graph_summary.json" \
  "${EXP_TARGET}/run_results.json"

(
  cd "${EXP_DIR}"
  DBT_PROFILES_DIR="${EXP_DIR}" uvx --python 3.11 \
    --from "dbt-duckdb==1.8.*" --with "dbt-core==1.8.*" dbt deps
  DBT_PROFILES_DIR="${EXP_DIR}" uvx --python 3.11 \
    --from "dbt-duckdb==1.8.*" --with "dbt-core==1.8.*" dbt compile
)

if [[ ! -f "${EXP_TARGET}/manifest.json" ]]; then
  echo "ERROR: dbt compile did not produce ${EXP_TARGET}/manifest.json" >&2
  exit 1
fi

scrub_compiled_manifest "${EXP_TARGET}/manifest.json" "${EXP_TARGET}/manifest.json"

# Never commit the compile stragglers / duckdb / installed packages.
rm -rf "${EXP_DIR}/dbt_packages" "${EXP_DIR}/logs" "${EXP_DIR}/dev.duckdb"
rm -f \
  "${EXP_TARGET}/partial_parse.msgpack" \
  "${EXP_TARGET}/perf_info.json" \
  "${EXP_TARGET}/semantic_manifest.json" \
  "${EXP_TARGET}/graph.gpickle" \
  "${EXP_TARGET}/graph_summary.json" \
  "${EXP_TARGET}/run_results.json"
rm -rf "${EXP_TARGET}/compiled" "${EXP_TARGET}/run"

echo "==> Done. Committed dbt-expectations fixture:"
ls -1 "${EXP_TARGET}"
