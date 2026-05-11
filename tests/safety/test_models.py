"""Tests for ``signalforge.safety.models`` (US-004).

Covers the four typed shapes added by this story:

* :class:`SamplingMode` — ``str + Enum`` mixin (Python 3.10-compatible).
* :class:`RedactionRecord` — frozen Pydantic v2 model with ``Literal`` reason.
* :class:`AuditEvent` — frozen, reproducibility-carrying audit record (DEC-014).
* :class:`LLMRequest` — frozen, deep-immutable request payload (DEC-022).

The drift-detection ``extra="forbid"`` test lands separately in US-011; this
file only validates the production shapes' behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from signalforge.safety.models import (
    AuditEvent,
    LLMRequest,
    RedactionRecord,
    SamplingMode,
)

pytestmark = pytest.mark.safety


# ---------------------------------------------------------------------------
# SamplingMode
# ---------------------------------------------------------------------------


def test_sampling_mode_enum_values_exact_strings() -> None:
    assert SamplingMode.SCHEMA_ONLY.value == "schema-only"
    assert SamplingMode.AGGREGATE_ONLY.value == "aggregate-only"
    assert SamplingMode.SAMPLE.value == "sample"
    assert len(SamplingMode) == 3


def test_sampling_mode_is_str_subclass() -> None:
    assert isinstance(SamplingMode.SCHEMA_ONLY, str)
    # str-equality works: critical for YAML round-trip compatibility.
    assert SamplingMode.SCHEMA_ONLY == "schema-only"
    assert SamplingMode.AGGREGATE_ONLY == "aggregate-only"
    assert SamplingMode.SAMPLE == "sample"


def test_sampling_mode_iteration() -> None:
    assert tuple(SamplingMode) == (
        SamplingMode.SCHEMA_ONLY,
        SamplingMode.AGGREGATE_ONLY,
        SamplingMode.SAMPLE,
    )


# ---------------------------------------------------------------------------
# RedactionRecord
# ---------------------------------------------------------------------------


def _valid_record() -> RedactionRecord:
    return RedactionRecord(
        column_name="email",
        hashed_name="col_a3f29c61",
        redacted=True,
        reason="pattern_match",
    )


def test_redaction_record_construction_happy_path() -> None:
    record = _valid_record()
    assert record.column_name == "email"
    assert record.hashed_name == "col_a3f29c61"
    assert record.redacted is True
    assert record.reason == "pattern_match"


def test_redaction_record_reason_literal_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        RedactionRecord(
            column_name="x",
            hashed_name="col_y",
            redacted=True,
            reason="phantom",  # type: ignore[arg-type]
        )


def test_redaction_record_is_frozen() -> None:
    record = _valid_record()
    with pytest.raises(ValidationError):
        record.column_name = "z"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AuditEvent
# ---------------------------------------------------------------------------


def _valid_audit_event(**overrides: object) -> AuditEvent:
    base: dict[str, object] = {
        "timestamp": datetime(2026, 4, 28, 22, 30, tzinfo=timezone.utc),
        "model_unique_id": "model.sf_demo.customers",
        "mode": SamplingMode.SCHEMA_ONLY,
        "columns_sent": ("id", "col_a3f29c61"),
        "redactions": (_valid_record(),),
        "signalforge_version": "0.1.0",
        "policy_hash": "abc123def456789a",
    }
    base.update(overrides)
    return AuditEvent(**base)  # type: ignore[arg-type]


def test_audit_event_schema_version_default_is_1() -> None:
    event = _valid_audit_event()
    assert event.audit_schema_version == 1


def test_audit_event_extra_ignore_drops_unknown_field() -> None:
    event = _valid_audit_event(unknown_field="x")  # extra="ignore"
    dumped = event.model_dump()
    assert "unknown_field" not in dumped


def test_audit_event_round_trips_through_json_dumps() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "safety" / "audit_events_sample.jsonl"
    )
    line = fixture_path.read_text(encoding="utf-8").splitlines()[0]
    event = AuditEvent.model_validate_json(line)
    assert event.model_unique_id == "model.sf_demo.customers"
    assert event.mode is SamplingMode.SCHEMA_ONLY
    assert event.columns_sent == ("id", "col_a3f29c61")
    assert event.row_count is None
    assert event.signalforge_version == "0.1.0"
    assert event.policy_hash == "abc123def456789a"
    assert event.audit_schema_version == 1
    assert event.policy_flags == ()
    assert len(event.redactions) == 1
    assert event.redactions[0].reason == "pattern_match"

    # Round-trip back through JSON and reconstruct an equal record.
    redumped = event.model_dump_json()
    event2 = AuditEvent.model_validate_json(redumped)
    assert event2 == event


def test_audit_event_columns_sent_immutable() -> None:
    event = _valid_audit_event()
    assert event.columns_sent.__class__ is tuple
    # Concatenation works (returns a new tuple); mutation is not available.
    assert event.columns_sent + ("x",) == ("id", "col_a3f29c61", "x")
    assert not hasattr(event.columns_sent, "append")


# ---------------------------------------------------------------------------
# LLMRequest
# ---------------------------------------------------------------------------


def _valid_llm_request(**overrides: object) -> LLMRequest:
    base: dict[str, object] = {
        "model_unique_id": "model.sf_demo.customers",
        "mode": SamplingMode.SCHEMA_ONLY,
        "columns_sent": ("id", "col_a3f29c61"),
        "redactions": (_valid_record(),),
        "schema": (("id", "INT64"), ("col_a3f29c61", "STRING")),
    }
    base.update(overrides)
    return LLMRequest(**base)  # type: ignore[arg-type]


def test_llm_request_columns_sent_is_tuple() -> None:
    request = _valid_llm_request()
    assert request.columns_sent.__class__ is tuple


def test_llm_request_redactions_is_tuple_of_records() -> None:
    request = _valid_llm_request()
    assert request.redactions.__class__ is tuple
    assert all(isinstance(r, RedactionRecord) for r in request.redactions)


def test_llm_request_sampled_rows_immutable_when_none() -> None:
    request = _valid_llm_request(sampled_rows=None)
    assert request.sampled_rows is None


def test_llm_request_sampled_rows_immutable_when_present() -> None:
    request = _valid_llm_request(
        sampled_rows=({"id": 1, "col_a3f29c61": "abc"},),
    )
    assert request.sampled_rows is not None
    assert request.sampled_rows.__class__ is tuple
    with pytest.raises(ValidationError):
        request.sampled_rows = None  # type: ignore[misc]


def test_llm_request_aggregates_is_tuple_of_tuples_when_present() -> None:
    """Regression: ``aggregates`` was ``dict`` (mutable post-frozen) — caught by
    Quality-Gate review. Now ``tuple[tuple[str, ColumnStats|None], ...]`` so
    downstream consumers (#5) cannot ``request.aggregates["x"] = ...`` after
    the audit log has been written (DEC-022 transitive immutability)."""
    from signalforge.warehouse.models import ColumnStats

    stats = ColumnStats(count=10, distinct=5, nulls=0, min=0, max=9, data_type="INT64")
    request = _valid_llm_request(aggregates=(("id", stats), ("col_a3f29c61", None)))
    assert request.aggregates is not None
    assert request.aggregates.__class__ is tuple
    for entry in request.aggregates:
        assert entry.__class__ is tuple
        assert len(entry) == 2
        assert isinstance(entry[0], str)
        assert entry[1] is None or isinstance(entry[1], ColumnStats)


def test_llm_request_aggregates_immutable_when_none() -> None:
    request = _valid_llm_request(aggregates=None)
    assert request.aggregates is None


def test_llm_request_aggregates_field_reassignment_blocked_by_frozen() -> None:
    request = _valid_llm_request(aggregates=(("id", None),))
    with pytest.raises(ValidationError):
        request.aggregates = None  # type: ignore[misc]


def test_llm_request_schema_field_is_tuple_of_tuples() -> None:
    request = _valid_llm_request()
    assert request.schema.__class__ is tuple
    for entry in request.schema:
        assert entry.__class__ is tuple
        assert len(entry) == 2
        assert isinstance(entry[0], str)
        assert isinstance(entry[1], str)


def test_llm_request_docstring_warns_about_direct_construction() -> None:
    assert LLMRequest.__doc__ is not None
    assert "build_llm_request" in LLMRequest.__doc__
    assert "audit log" in LLMRequest.__doc__


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_module_all_lists_documented_classes() -> None:
    from signalforge.safety import models as safety_models

    assert tuple(safety_models.__all__) == (
        "SamplingMode",
        "RedactionReason",
        "RedactionRecord",
        "AuditEvent",
        "LLMRequest",
    )
