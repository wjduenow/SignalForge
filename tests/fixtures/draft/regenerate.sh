#!/usr/bin/env bash
# Regenerate draft-pipeline fixtures.
# TODO: filled in by US-014 — runs the `pytest -m anthropic` smoke test against
# claude-haiku-4-5-20251001 and captures the parsed CandidateSchema.model_dump_json
# output into candidate_schema_v1.json. Until then the fixture is hand-authored.
set -euo pipefail
cd "$(dirname "$0")"
