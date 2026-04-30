#!/usr/bin/env bash
# Regenerate draft-pipeline fixtures.
#
# Placeholder for v0.1 — the smoke test (US-015) that captures fresh
# CandidateSchema output from the real Anthropic API has not landed yet.
# Once it does, this script will run that test and rewrite
# candidate_schema_v1.json from its parsed output.
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

# Will run the real-API smoke test once US-015 lands. Today the file does
# not yet exist; this command will fail loudly so the maintainer notices.
pytest -m anthropic tests/draft/test_smoke_real_api.py -v

echo "TODO: Refreshing the fixture is a manual step in v0.1." >&2
echo "Add the smoke test (US-015) and reroute this script to capture its output." >&2
exit 1
