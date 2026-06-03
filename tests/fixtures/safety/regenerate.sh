#!/usr/bin/env bash
# Regenerate audit_events_sample.jsonl deterministically.
# Hand-authored schema; mirrors the documented shape in safety-layer.md and
# plans/super/4-pii-safety.md (DEC-005 + DEC-014).
#
# Issue #54 bumped audit_schema_version 1 → 2 and added the draft_skip_*
# RedactionReason values; the fixture exercises both an existing PII
# pattern_match record and one draft_skip_column_meta record so consumers
# gating on audit_schema_version >= 2 can verify their parser.
#
# Issue #55 bumped 2 → 3 when _compute_policy_hash migrated from SHA-256[:16]
# to blake2b(digest_size=8) so the audit corpus reads one hash recipe across
# every writer. The placeholder policy_hash values below remain 16-hex
# opaque strings (drift detector exercises shape, not provenance).
#
# Issue #185 bumped 3 → 4 when the v3 ``redactions:
# tuple[RedactionRecord, ...]`` field was replaced by
# ``redactions_by_reason: dict[reason, tuple[hashed_name, ...]]`` plus a
# sibling ``column_name_map: dict[hashed_name, real_name]``, and the
# chunk-correlation triple (``audit_id``, ``chunk_index``, ``chunk_count``)
# was added. The fixture now ships THREE lines covering all three v4 shapes:
#   line 1: non-chunked v4 record (carries the pattern_match redaction).
#   line 2: chunked header — chunk_index=0, chunk_count=2, EMPTY redaction
#           maps + full metadata + an audit_id correlation key.
#   line 3: chunked continuation — chunk_index=1, chunk_count=2, every
#           metadata field null, audit_id matches line 2, redactions_by_reason
#           + column_name_map carry the per-chunk slice (the
#           draft_skip_column_meta record).
set -euo pipefail
cd "$(dirname "$0")"

python - <<'PY' > audit_events_sample.jsonl
import json

records = [
    # Line 1: non-chunked v4 record (small enough to fit in one line).
    # Carries one PII pattern_match redaction (email -> col_a3f29c61).
    {
        "timestamp": "2026-04-28T22:30:00.000000Z",
        "model_unique_id": "model.sf_demo.customers",
        "mode": "schema-only",
        "columns_sent": ["id", "col_a3f29c61"],
        "row_count": None,
        "signalforge_version": "0.1.0",
        "policy_hash": "abc123def456789a",
        "policy_flags": [],
        "redactions_by_reason": {"pattern_match": ["col_a3f29c61"]},
        "column_name_map": {"col_a3f29c61": "email"},
        "audit_id": None,
        "chunk_index": None,
        "chunk_count": None,
        "audit_schema_version": 4,
    },
    # Line 2: chunked header — metadata + chunk-correlation triple + EMPTY
    # redaction maps (the per-chunk validator requires both present but
    # empty on a header row; the redaction body rides on continuation
    # lines).
    {
        "timestamp": "2026-05-11T18:00:00.000000Z",
        "model_unique_id": "model.sf_demo.orders",
        "mode": "schema-only",
        "columns_sent": ["id", "amount"],
        "row_count": None,
        "signalforge_version": "0.1.0",
        "policy_hash": "def456abc78901bc",
        "policy_flags": [],
        "redactions_by_reason": {},
        "column_name_map": {},
        "audit_id": "0123456789abcdef",
        "chunk_index": 0,
        "chunk_count": 2,
        "audit_schema_version": 4,
    },
    # Line 3: chunked continuation — every metadata field null, redaction
    # maps carry the per-chunk slice. Carries the draft_skip_column_meta
    # record so consumers can pattern-match on the new (issue-#54) reason.
    {
        "timestamp": None,
        "model_unique_id": None,
        "mode": None,
        "columns_sent": None,
        "row_count": None,
        "signalforge_version": None,
        "policy_hash": None,
        "policy_flags": None,
        "redactions_by_reason": {
            "draft_skip_column_meta": ["col_92aa17bd"],
        },
        "column_name_map": {"col_92aa17bd": "internal_token"},
        "audit_id": "0123456789abcdef",
        "chunk_index": 1,
        "chunk_count": 2,
        "audit_schema_version": 4,
    },
]
for record in records:
    print(json.dumps(record, separators=(",", ":")))
PY
