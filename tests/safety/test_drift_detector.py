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


def test_audit_event_drift_detector_validates_committed_fixture():
    line = _FIXTURE.read_text(encoding="utf-8").strip()
    assert line, f"expected one JSON line in {_FIXTURE}"
    payload = json.loads(line)
    # If this raises, an unknown field was introduced. Update production model
    # AND update both this StrictAuditEvent class AND the fixture's regenerate.sh.
    StrictAuditEvent.model_validate(payload)


def test_audit_event_drift_detector_rejects_unknown_field():
    line = _FIXTURE.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    payload["phantom_field"] = "x"
    with pytest.raises(ValidationError):
        StrictAuditEvent.model_validate(payload)


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
