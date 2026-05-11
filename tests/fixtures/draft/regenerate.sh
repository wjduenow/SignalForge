#!/usr/bin/env bash
# Regenerate draft-pipeline fixtures.
#
# v0.1 — full real-API end-to-end coverage now lives in the issue #10
# smoke test (`tests/cli/test_e2e_bigquery_smoke.py`, gated by
# `@pytest.mark.e2e` + the SF_RUN_BQ=1 / ANTHROPIC_API_KEY /
# GOOGLE_CLOUD_PROJECT env-var triple). This script's draft-layer
# fixtures are still hand-authored: candidate_schema_v1.json is the
# golden CandidateSchema shape and is edited manually when the
# CandidateSchema model evolves. (The previous v0.1 wire-test
# `tests/draft/test_smoke_real_api.py` was retired in #10's follow-up
# — it relied on the now-removed LLMCacheTooSmallError hard-fail to
# short-circuit before `messages.create`.)
#
# Until then, candidate_schema_v1.json is hand-authored and edited
# manually when the CandidateSchema model shape changes (the
# tests/draft/test_drift_detector.py drift detector enforces this).

set -euo pipefail

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "error: ANTHROPIC_API_KEY is not set" >&2
  exit 2
fi

cd "$(dirname "$0")/../../.."

# Run the wire smoke test (US-015). Proves SDK auth + transport but
# does not yet capture a CandidateSchema — see header.
pytest -m anthropic tests/draft/test_smoke_real_api.py -v

echo "TODO: Refreshing candidate_schema_v1.json is still a manual step in v0.1." >&2
echo "When the smoke fixture grows past cache minimums, replace this exit with" >&2
echo "capture of the parsed CandidateSchema." >&2
exit 1
