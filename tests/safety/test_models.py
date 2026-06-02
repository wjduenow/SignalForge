"""Tests for ``signalforge.safety.models`` (US-004, v4 shape under #185).

Covers the four typed shapes added by this story:

* :class:`SamplingMode` — :class:`enum.StrEnum` (see models.py DEC-024 note).
* :class:`RedactionRecord` — frozen Pydantic v2 model with ``Literal`` reason.
* :class:`AuditEvent` — frozen, reproducibility-carrying audit record (DEC-014),
  upgraded to the v4 shape by #185: symbol-table-by-reason redactions +
  chunk-correlation triple + chunk-shape ``@model_validator``.
* :class:`LLMRequest` — frozen, deep-immutable request payload (DEC-022).

The drift-detection ``extra="forbid"`` test lands separately in US-005; this
file only validates the production shapes' behaviour.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

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
# AuditEvent — v4 shape (issue #185)
# ---------------------------------------------------------------------------


def _valid_audit_event(**overrides: object) -> AuditEvent:
    """Build a valid **non-chunked** v4 :class:`AuditEvent`."""
    base: dict[str, object] = {
        "timestamp": datetime(2026, 4, 28, 22, 30, tzinfo=UTC),
        "model_unique_id": "model.sf_demo.customers",
        "mode": SamplingMode.SCHEMA_ONLY,
        "columns_sent": ("id", "col_a3f29c61"),
        "signalforge_version": "0.1.0",
        "policy_hash": "abc123def456789a",
        "policy_flags": (),
        "redactions_by_reason": {"pattern_match": ("col_a3f29c61",)},
        "column_name_map": {"col_a3f29c61": "email"},
    }
    base.update(overrides)
    return AuditEvent(**base)  # type: ignore[arg-type]


def _valid_chunk_header(**overrides: object) -> AuditEvent:
    """Build a valid **chunk-header** v4 :class:`AuditEvent` (chunk_index=0)."""
    base: dict[str, object] = {
        "timestamp": datetime(2026, 4, 28, 22, 30, tzinfo=UTC),
        "model_unique_id": "model.sf_demo.customers",
        "mode": SamplingMode.SCHEMA_ONLY,
        "columns_sent": ("id", "col_a3f29c61"),
        "signalforge_version": "0.1.0",
        "policy_hash": "abc123def456789a",
        "policy_flags": (),
        "redactions_by_reason": {},
        "column_name_map": {},
        "audit_id": "ad12cafe34beef56",
        "chunk_index": 0,
        "chunk_count": 2,
    }
    base.update(overrides)
    return AuditEvent(**base)  # type: ignore[arg-type]


def _valid_chunk_continuation(**overrides: object) -> AuditEvent:
    """Build a valid **chunk-continuation** v4 :class:`AuditEvent`."""
    base: dict[str, object] = {
        "redactions_by_reason": {"pattern_match": ("col_a3f29c61",)},
        "column_name_map": {"col_a3f29c61": "email"},
        "audit_id": "ad12cafe34beef56",
        "chunk_index": 1,
        "chunk_count": 2,
    }
    base.update(overrides)
    return AuditEvent(**base)  # type: ignore[arg-type]


def test_audit_event_schema_version_default_is_current() -> None:
    """Issue #185 bumped the default 3 → 4 when the v3 ``redactions`` field
    was replaced by ``redactions_by_reason`` + ``column_name_map`` and the
    chunk-correlation triple was added. The field stays ``int`` so future
    bumps round-trip cleanly."""
    event = _valid_audit_event()
    assert event.audit_schema_version == 4


def test_audit_event_accepts_legacy_schema_version_1() -> None:
    """Forward-compat: older v1 audit JSONLs must still parse the
    ``audit_schema_version`` field as an int (not a Literal)."""
    event = _valid_audit_event(audit_schema_version=1)
    assert event.audit_schema_version == 1


def test_audit_event_accepts_legacy_schema_version_2() -> None:
    """Forward-compat: v2 audit JSONLs must still parse on the int field."""
    event = _valid_audit_event(audit_schema_version=2)
    assert event.audit_schema_version == 2


def test_audit_event_extra_ignore_drops_unknown_field() -> None:
    event = _valid_audit_event(unknown_field="x")  # extra="ignore"
    dumped = event.model_dump()
    assert "unknown_field" not in dumped


def test_audit_event_v3_redactions_field_removed() -> None:
    """The v3 ``redactions: tuple[RedactionRecord, ...]`` field is replaced
    in the v4 shape (#185); ``AuditEvent.model_fields`` must no longer
    expose it. The :class:`RedactionRecord` class stays — it's still used
    as an internal value object on the build path in
    :mod:`signalforge.safety.request`."""
    assert "redactions" not in AuditEvent.model_fields
    # Sanity: the v4 fields ARE present.
    assert "redactions_by_reason" in AuditEvent.model_fields
    assert "column_name_map" in AuditEvent.model_fields
    assert "audit_id" in AuditEvent.model_fields
    assert "chunk_index" in AuditEvent.model_fields
    assert "chunk_count" in AuditEvent.model_fields


def test_audit_event_round_trips_through_json_dumps() -> None:
    """Build a v4 event, dump → reload, assert equality. Fixture-independent
    so it survives US-005's fixture regen."""
    original = _valid_audit_event()
    redumped = original.model_dump_json()
    reloaded = AuditEvent.model_validate_json(redumped)
    assert reloaded == original
    assert reloaded.audit_schema_version == 4
    assert reloaded.redactions_by_reason == {"pattern_match": ("col_a3f29c61",)}
    assert reloaded.column_name_map == {"col_a3f29c61": "email"}


def test_audit_event_columns_sent_immutable() -> None:
    event = _valid_audit_event()
    assert event.columns_sent is not None
    assert event.columns_sent.__class__ is tuple
    # Concatenation works (returns a new tuple); mutation is not available.
    assert event.columns_sent + ("x",) == ("id", "col_a3f29c61", "x")
    assert not hasattr(event.columns_sent, "append")


# ---------------------------------------------------------------------------
# v4 chunk-shape validator — happy paths
# ---------------------------------------------------------------------------


def test_audit_event_v4_non_chunked_shape_validates() -> None:
    """Non-chunked: all metadata required; both v4 maps present (may be
    empty); the chunk-correlation triple is all-None."""
    event = _valid_audit_event()
    assert event.audit_id is None
    assert event.chunk_index is None
    assert event.chunk_count is None
    assert event.redactions_by_reason == {"pattern_match": ("col_a3f29c61",)}
    assert event.column_name_map == {"col_a3f29c61": "email"}


def test_audit_event_v4_non_chunked_with_empty_redaction_maps_validates() -> None:
    """Non-chunked event with no redactions: maps are present but empty
    (NOT None — that's a different failure mode)."""
    event = _valid_audit_event(redactions_by_reason={}, column_name_map={})
    assert event.redactions_by_reason == {}
    assert event.column_name_map == {}


def test_audit_event_v4_chunk_header_with_empty_redactions_validates() -> None:
    """Chunk header: metadata present, ``redactions_by_reason`` and
    ``column_name_map`` MUST be empty dicts (the body rides on continuation
    rows)."""
    event = _valid_chunk_header()
    assert event.audit_id == "ad12cafe34beef56"
    assert event.chunk_index == 0
    assert event.chunk_count == 2
    assert event.redactions_by_reason == {}
    assert event.column_name_map == {}
    assert event.model_unique_id == "model.sf_demo.customers"


def test_audit_event_v4_chunk_continuation_with_none_metadata_validates() -> None:
    """Chunk continuation: all metadata fields MUST be None;
    ``redactions_by_reason`` and ``column_name_map`` carry the slice."""
    event = _valid_chunk_continuation()
    assert event.audit_id == "ad12cafe34beef56"
    assert event.chunk_index == 1
    assert event.chunk_count == 2
    assert event.timestamp is None
    assert event.model_unique_id is None
    assert event.mode is None
    assert event.columns_sent is None
    assert event.signalforge_version is None
    assert event.policy_hash is None
    assert event.policy_flags is None
    assert event.redactions_by_reason == {"pattern_match": ("col_a3f29c61",)}
    assert event.column_name_map == {"col_a3f29c61": "email"}


# ---------------------------------------------------------------------------
# v4 chunk-shape validator — rejection paths
# ---------------------------------------------------------------------------


def test_audit_event_chunked_missing_audit_id_raises() -> None:
    """A chunked event missing ``audit_id`` is a partially-set chunk triple
    (``audit_id is None`` while ``chunk_index`` / ``chunk_count`` are set)
    — must be rejected at construction time."""
    with pytest.raises(ValidationError):
        AuditEvent(
            timestamp=datetime(2026, 4, 28, 22, 30, tzinfo=UTC),
            model_unique_id="model.sf_demo.customers",
            mode=SamplingMode.SCHEMA_ONLY,
            columns_sent=("id",),
            signalforge_version="0.1.0",
            policy_hash="abc",
            policy_flags=(),
            redactions_by_reason={},
            column_name_map={},
            audit_id=None,
            chunk_index=0,
            chunk_count=2,
        )


def test_audit_event_chunk_index_at_or_above_count_raises() -> None:
    """``chunk_index >= chunk_count`` is a contract violation (index is
    0-based; on a 2-chunk event, valid indices are 0 and 1)."""
    with pytest.raises(ValidationError):
        _valid_chunk_continuation(chunk_index=2, chunk_count=2)
    with pytest.raises(ValidationError):
        _valid_chunk_continuation(chunk_index=5, chunk_count=2)


def test_audit_event_non_chunked_with_audit_id_raises() -> None:
    """Setting ``audit_id`` without ``chunk_index`` / ``chunk_count`` is a
    partially-set chunk triple; the validator rejects it."""
    with pytest.raises(ValidationError):
        _valid_audit_event(audit_id="ad12cafe34beef56")


def test_audit_event_chunk_header_with_nonempty_redactions_raises() -> None:
    """Chunk header (``chunk_index=0``) MUST carry empty
    ``redactions_by_reason`` and ``column_name_map`` — the redaction body
    rides on continuation rows."""
    with pytest.raises(ValidationError):
        _valid_chunk_header(redactions_by_reason={"pattern_match": ("col_a3f29c61",)})
    with pytest.raises(ValidationError):
        _valid_chunk_header(column_name_map={"col_a3f29c61": "email"})


def test_audit_event_chunk_continuation_with_metadata_raises() -> None:
    """Chunk continuation MUST omit every metadata field — having any
    non-None metadata on a continuation row is a contract violation."""
    with pytest.raises(ValidationError):
        _valid_chunk_continuation(model_unique_id="model.sf_demo.customers")
    with pytest.raises(ValidationError):
        _valid_chunk_continuation(timestamp=datetime(2026, 4, 28, 22, 30, tzinfo=UTC))
    with pytest.raises(ValidationError):
        _valid_chunk_continuation(signalforge_version="0.1.0")


def test_audit_event_non_chunked_with_none_redaction_maps_raises() -> None:
    """Non-chunked event with ``redactions_by_reason=None`` or
    ``column_name_map=None`` is invalid — the v4 shape requires both
    present (use empty dicts for an event with no redactions)."""
    with pytest.raises(ValidationError):
        _valid_audit_event(redactions_by_reason=None)
    with pytest.raises(ValidationError):
        _valid_audit_event(column_name_map=None)


def test_audit_event_chunk_count_lt_2_raises() -> None:
    """A chunked event with ``chunk_count == 1`` is degenerate (would be
    representable as non-chunked); the validator rejects it."""
    with pytest.raises(ValidationError):
        AuditEvent(
            redactions_by_reason={},
            column_name_map={},
            audit_id="ad12cafe34beef56",
            chunk_index=0,
            chunk_count=1,
        )


# ---------------------------------------------------------------------------
# QG Pass-3 patch-coverage backfill — uncovered rejection branches inside the
# #185 diff. These backfill the 5 untested ``raise ValueError(...)`` arms in
# ``AuditEvent``'s ``@model_validator`` so codecov patch coverage clears.
# ---------------------------------------------------------------------------


def test_audit_event_non_chunked_missing_metadata_raises() -> None:
    """The non-chunked branch requires every metadata field (timestamp,
    model_unique_id, mode, columns_sent, signalforge_version, policy_hash,
    policy_flags) to be populated. Dropping any one fires the ``raise
    ValueError(...)`` arm — pins the "metadata required" half of the
    non-chunked branch (the "redaction maps must be present" half is
    pinned separately by ``test_audit_event_non_chunked_with_none_redaction_maps_raises``)."""
    with pytest.raises(ValidationError):
        # timestamp absent — non-chunked branch requires it
        _valid_audit_event(timestamp=None)
    with pytest.raises(ValidationError):
        # model_unique_id absent
        _valid_audit_event(model_unique_id=None)
    with pytest.raises(ValidationError):
        # mode absent
        _valid_audit_event(mode=None)


def test_audit_event_chunk_index_negative_raises() -> None:
    """``chunk_index < 0`` is structurally invalid (the writer never emits
    negative indices). The validator branch fires only on
    ``model_validate_json`` replay of corrupt audit JSONL — without the
    test, a future "simplify the validator" refactor could silently
    remove the guard and let replay-corruption pass through as a typed
    event with negative chunk_index."""
    with pytest.raises(ValidationError):
        AuditEvent(
            redactions_by_reason={"pattern_match": ("col_abc12345",)},
            column_name_map={"col_abc12345": "real_col"},
            audit_id="ad12cafe34beef56",
            chunk_index=-1,
            chunk_count=2,
        )


def test_audit_event_chunk_header_missing_metadata_raises() -> None:
    """A chunk header (``chunk_index=0, chunk_count>=2``) must carry the
    full metadata set (same requirement as non-chunked events). Dropping a
    required metadata field on a header fires the validator's chunk-header
    metadata-required arm."""
    with pytest.raises(ValidationError):
        _valid_chunk_header(timestamp=None)
    with pytest.raises(ValidationError):
        _valid_chunk_header(model_unique_id=None)


def test_audit_event_chunk_header_with_none_redaction_maps_raises() -> None:
    """A chunk header MUST carry ``redactions_by_reason={}`` and
    ``column_name_map={}`` (empty dicts) — ``None`` is invalid (covered
    elsewhere for non-empty rejection; this pins the None case, which is a
    distinct validator arm)."""
    with pytest.raises(ValidationError):
        _valid_chunk_header(redactions_by_reason=None)
    with pytest.raises(ValidationError):
        _valid_chunk_header(column_name_map=None)


def test_audit_event_chunk_continuation_with_none_redaction_maps_raises() -> None:
    """A chunk continuation carries the slice of redactions for its chunk.
    Both ``redactions_by_reason`` and ``column_name_map`` must be present
    (may be empty dicts in the degenerate "fully-empty slice" case but not
    ``None``)."""
    with pytest.raises(ValidationError):
        _valid_chunk_continuation(redactions_by_reason=None)
    with pytest.raises(ValidationError):
        _valid_chunk_continuation(column_name_map=None)


# ---------------------------------------------------------------------------
# v4 __repr__ — PII redaction (safety-layer DEC-022)
# ---------------------------------------------------------------------------


def test_audit_event_repr_omits_column_name_map() -> None:
    """``column_name_map`` carries (hashed → real) and so its values are
    real column names — potentially PII-bearing. The custom ``__repr__``
    omits the dict's contents while still surfacing a count, matching the
    safety-layer DEC-022 redaction precedent."""
    event = _valid_audit_event(
        column_name_map={"col_a3f29c61": "patient_ssn", "col_92aa17bd": "diagnosis_code"},
        redactions_by_reason={"pattern_match": ("col_a3f29c61", "col_92aa17bd")},
    )
    rendered = repr(event)
    # Real column names must not leak.
    assert "patient_ssn" not in rendered
    assert "diagnosis_code" not in rendered
    # Counts and IDs survive.
    assert "model.sf_demo.customers" in rendered
    assert "column_name_map_count=2" in rendered
    assert "redactions_by_reason_count=2" in rendered
    assert "audit_schema_version=4" in rendered


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


def test_importing_safety_models_emits_no_userwarning() -> None:
    """Issue #93: a clean import must not emit the Pydantic ``schema``-shadow
    UserWarning. Run in a subprocess so the import is genuinely fresh — the
    parent process already imported the module via the test collector."""
    result = subprocess.run(
        [sys.executable, "-W", "error::UserWarning", "-c", "import signalforge.safety.models"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"importing signalforge.safety.models surfaced a UserWarning:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


def test_module_all_lists_documented_classes() -> None:
    from signalforge.safety import models as safety_models

    assert tuple(safety_models.__all__) == (
        "SamplingMode",
        "RedactionReason",
        "DRAFT_SKIP_REASONS",
        "RedactionRecord",
        "AuditEvent",
        "LLMRequest",
    )
