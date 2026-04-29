#!/usr/bin/env bash
# Regenerate audit_events_sample.jsonl deterministically.
# Until US-004 lands AuditEvent, the schema is hand-authored here and must
# match the documented shape in plans/super/4-pii-safety.md (DEC-005 + DEC-014).
set -euo pipefail
cd "$(dirname "$0")"

python - <<'PY' > audit_events_sample.jsonl
import json
record = {
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
    "audit_schema_version": 1,
    "policy_flags": [],
}
print(json.dumps(record, separators=(",", ":")))
PY
