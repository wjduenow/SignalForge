#!/usr/bin/env bash
# Regenerate draft-pipeline fixtures.
#
# v0.1 — the smoke test (US-015) is a wire test that stops at
# LLMCacheTooSmallError before any messages.create call (the smoke
# manifest's cached block is ~106 tokens, far below Haiku's 2048-token
# minimum). So real-API CandidateSchema capture is still manual.
#
# When the smoke fixture grows past the cache minimums (~1024 tokens for
# Sonnet, ~2048 for Haiku) and the smoke test exercises the full
# round-trip, replace the exit-1 below with capture of the parsed
# CandidateSchema (e.g. via a pytest plugin that dumps DraftOutcome).
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
