#!/usr/bin/env bash
# Regenerate audit_events_sample.jsonl deterministically.
# Hand-authored schema; mirrors the documented shape in safety-layer.md and
# plans/super/4-pii-safety.md (DEC-005 + DEC-014). Issue #54 bumped
# audit_schema_version 1 → 2 and added the draft_skip_* RedactionReason
# values; the fixture exercises both an existing PII pattern_match record
# and one draft_skip_column_meta record so consumers gating on
# audit_schema_version >= 2 can verify their parser.
set -euo pipefail
cd "$(dirname "$0")"

python - <<'PY' > audit_events_sample.jsonl
import json
records = [
    {
        "timestamp": "2026-04-28T22:30:00+00:00",
        "model_unique_id": "model.sf_demo.customers",
        "mode": "schema-only",
        "columns_sent": ["id", "col_a3f29c61"],
        "redactions": [
            {
                "column_name": "email",
                "hashed_name": "col_a3f29c61",
                "redacted": True,
                "reason": "pattern_match",
            }
        ],
        "row_count": None,
        "signalforge_version": "0.1.0",
        "policy_hash": "abc123def456789a",
        "audit_schema_version": 2,
        "policy_flags": [],
    },
    {
        "timestamp": "2026-05-11T18:00:00+00:00",
        "model_unique_id": "model.sf_demo.orders",
        "mode": "schema-only",
        "columns_sent": ["id", "amount"],
        "redactions": [
            {
                "column_name": "internal_token",
                "hashed_name": "col_92aa17bd",
                "redacted": True,
                "reason": "draft_skip_column_meta",
            }
        ],
        "row_count": None,
        "signalforge_version": "0.1.0",
        "policy_hash": "def456abc78901bc",
        "audit_schema_version": 2,
        "policy_flags": [],
    },
]
for record in records:
    print(json.dumps(record, separators=(",", ":")))
PY
