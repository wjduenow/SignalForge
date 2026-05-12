"""Drift detector for AuditEvent.

Production AuditEvent uses extra='ignore' for forward-compat (DEC-015). Pair it
with a one-off StrictAuditEvent (extra='forbid') validated against a committed
JSONL fixture. Adding a field to production AuditEvent without updating the
fixture or this strict model breaks the test loudly.

Reference: .claude/rules/testing-signal.md (drift detection pattern).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from signalforge.safety.models import RedactionRecord, SamplingMode


class StrictAuditEvent(BaseModel):
    """Mirror of production AuditEvent with extra='forbid'.

    If you add a field to AuditEvent (signalforge.safety.models), you MUST:
    1. Add it here, and
    2. Update tests/fixtures/safety/audit_events_sample.jsonl via
       tests/fixtures/safety/regenerate.sh.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    timestamp: datetime
    model_unique_id: str
    mode: SamplingMode
    columns_sent: tuple[str, ...]
    redactions: tuple[RedactionRecord, ...]
    row_count: int | None
    signalforge_version: str
    policy_hash: str
    audit_schema_version: int
    policy_flags: tuple[str, ...]


_FIXTURE = Path("tests/fixtures/safety/audit_events_sample.jsonl")


def _fixture_lines() -> list[str]:
    return [line for line in _FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_audit_event_drift_detector_validates_committed_fixture():
    lines = _fixture_lines()
    assert lines, f"expected ≥1 JSON line in {_FIXTURE}"
    for raw in lines:
        payload = json.loads(raw)
        # If this raises, an unknown field was introduced. Update production model
        # AND update both this StrictAuditEvent class AND the fixture's regenerate.sh.
        StrictAuditEvent.model_validate(payload)


def test_audit_event_drift_detector_rejects_unknown_field():
    lines = _fixture_lines()
    assert lines, f"expected ≥1 JSON line in {_FIXTURE}"
    payload = json.loads(lines[0])
    payload["phantom_field"] = "x"
    with pytest.raises(ValidationError):
        StrictAuditEvent.model_validate(payload)


def test_audit_event_fixture_audit_schema_version_is_current():
    """Issue #54 bumped audit_schema_version 1 → 2. Pin the fixture so a
    future bump without updating the sample line breaks the test loudly.
    """
    from signalforge.safety.request import _AUDIT_SCHEMA_VERSION

    for raw in _fixture_lines():
        payload = json.loads(raw)
        assert payload["audit_schema_version"] == _AUDIT_SCHEMA_VERSION


def test_audit_event_fixture_exercises_draft_skip_reason():
    """The fixture must include at least one draft_skip_* RedactionRecord
    so consumers gating on audit_schema_version >= 2 can verify their
    parser handles the new reason values (issue #54).
    """
    seen_reasons: set[str] = set()
    for raw in _fixture_lines():
        for rec in json.loads(raw)["redactions"]:
            seen_reasons.add(rec["reason"])
    assert {"draft_skip_column_meta"} <= seen_reasons, (
        f"audit_events_sample.jsonl should exercise draft_skip_column_meta; "
        f"saw reasons={sorted(seen_reasons)}"
    )


def test_audit_event_drift_detector_strict_model_field_set_matches_production():
    """Production AuditEvent and StrictAuditEvent must declare the same field set."""
    from signalforge.safety.models import AuditEvent

    prod_fields = set(AuditEvent.model_fields.keys())
    strict_fields = set(StrictAuditEvent.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictAuditEvent is missing fields present in AuditEvent: {missing_in_strict}. "
        "Update StrictAuditEvent to match."
    )
    assert not extra_in_strict, (
        f"StrictAuditEvent has fields absent from AuditEvent: {extra_in_strict}. "
        "Remove from StrictAuditEvent or add to AuditEvent."
    )
