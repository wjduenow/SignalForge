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
# Per DEC-015 of plans/super/47-init-demo.md: the final phase mirrors every
# file (except this script) into `src/signalforge/_demo/` then applies two
# demo-only rewrites (`profiles.yml` → env_var() macro per DEC-009;
# `.gitignore` slimmed per DEC-008). Keeping both trees aligned by
# construction means the parity gate at
# `tests/test_demo_fixture_parity.py` fires only on uncommanded drift.
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

# ---------------------------------------------------------------------------
# DEC-015 of plans/super/47-init-demo.md: keep src/signalforge/_demo/ in sync
# with this test fixture so the parity gate at
# tests/test_demo_fixture_parity.py fires only on uncommanded drift.
#
# 1. Mirror every file (except this script) verbatim into the shipped tree.
# 2. Overwrite profiles.yml with the env_var('GOOGLE_CLOUD_PROJECT') variant
#    (DEC-009) — the maintainer-only "DO NOT signalforge against this" header
#    is dropped because the env_var() lookup makes the shipped copy
#    safe-to-run as-is.
# 3. Overwrite .gitignore with the slimmed demo-audience copy (DEC-008) —
#    issue-#10 / DEC-021 internal references are removed; only the
#    `.signalforge/` exclusion (actually useful to a demo user) remains.
# ---------------------------------------------------------------------------

REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
DEMO_DIR="${REPO_ROOT}/src/signalforge/_demo"

echo "==> Mirroring fixture tree into ${DEMO_DIR}"
mkdir -p "${DEMO_DIR}"

# Use rsync if available (cleanly handles the regenerate.sh exclusion);
# fall back to a portable cp -R + explicit prune otherwise.
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude regenerate.sh \
    "${PROJECT_DIR}/" "${DEMO_DIR}/"
else
  rm -rf "${DEMO_DIR}"
  mkdir -p "${DEMO_DIR}"
  (cd "${PROJECT_DIR}" && find . -mindepth 1 -path ./regenerate.sh -prune -o -print \
    | while IFS= read -r rel; do
        rel="${rel#./}"
        src_path="${PROJECT_DIR}/${rel}"
        dst_path="${DEMO_DIR}/${rel}"
        if [[ -d "${src_path}" ]]; then
          mkdir -p "${dst_path}"
        else
          mkdir -p "$(dirname "${dst_path}")"
          cp -f "${src_path}" "${dst_path}"
        fi
      done)
fi

# Demo-only rewrite #1: profiles.yml uses env_var('GOOGLE_CLOUD_PROJECT')
# (DEC-009). Operator with that env var set runs the demo with zero file
# edits. Written verbatim so the file is fully reproducible from this
# script and the parity test's "rewrite must be different from source"
# clause is preserved.
cat >"${DEMO_DIR}/profiles.yml" <<'PROFILES_YML'
# Demo dbt profile shipped by `signalforge init-demo`.
#
# `method: oauth` falls through to Application Default Credentials — run
# `gcloud auth application-default login` once before invoking
# `signalforge generate` against this project.
#
# `project: "{{ env_var('GOOGLE_CLOUD_PROJECT') }}"` resolves at runtime from
# your billing project; export it before running the demo:
#
#     export GOOGLE_CLOUD_PROJECT=<your-gcp-project-id>
#
# The Austin bikeshare data is served from the public
# `bigquery-public-data.austin_bikeshare` dataset; your `GOOGLE_CLOUD_PROJECT`
# is the BILLING project the BigQuery SDK uses to issue the read.
austin:
  target: dev
  outputs:
    dev:
      type: bigquery
      method: oauth
      project: "{{ env_var('GOOGLE_CLOUD_PROJECT') }}"
      dataset: austin_bikeshare
      location: US
PROFILES_YML

# Demo-only rewrite #2: .gitignore slimmed (DEC-008). Drop issue-#10 /
# DEC-021 internal references; keep only the `.signalforge/` exclusion the
# demo audience actually needs.
cat >"${DEMO_DIR}/.gitignore" <<'GITIGNORE'
# SignalForge writes per-run audit logs and sidecar artefacts under .signalforge/.
# These are reproducible from a re-run; no value in committing them.
.signalforge/
GITIGNORE

echo "==> Done. Shipped demo files:"
find "${DEMO_DIR}" -type f | sort
