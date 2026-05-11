#!/usr/bin/env bash
# Regenerate the diff-renderer snapshot fixtures under
# tests/fixtures/diff/ from the in-tree signalforge.diff source.
#
# Mirrors tests/fixtures/regenerate.sh shape: idempotent; safe to re-run;
# diffs between consecutive runs should be empty barring intentional
# changes to the renderer or the snapshot inputs.
#
# US-011 of issue #8 (DEC-017): builds the 10-case fixture matrix
# enumerated in plans/super/8-diff-renderer.md.
#
# Why a Python helper:
#   - The fixtures' content is the rendered output of the diff
#     renderers. Running the renderers requires importing the in-tree
#     signalforge package — easier in Python than in shell.
#   - The recipe table (which renderer + which kwargs per case) lives
#     in tests/diff/_snapshot_inputs.py alongside the report builders.
#
# Requirements:
#   - signalforge installed in editable mode in the active venv:
#       pip install -e ".[dev]"
#   - python3 on PATH.
#
# Run from the repo root or anywhere — the script resolves its own
# path via $0 and runs the regenerator with the package importable.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

cd "$REPO_ROOT"

echo "regenerating diff snapshot fixtures..." >&2
python3 "$SCRIPT_DIR/_regenerate.py"
echo "done." >&2
