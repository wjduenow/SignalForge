"""Drift detector for AuditEvent.

Production AuditEvent uses extra='ignore' for forward-compat (DEC-015). Pair it
with a one-off StrictAuditEvent (extra='forbid') validated against a committed
JSONL fixture. Adding a field to production AuditEvent without updating the
fixture or this strict model breaks the test loudly.

Issue #185 (US-005) — the v4 shape replaces v3's
``redactions: tuple[RedactionRecord, ...]`` with a symbol-table-by-reason dict
(``redactions_by_reason``) plus a (hashed → real) sibling mapback
(``column_name_map``); adds the chunk-correlation triple (``audit_id``,
``chunk_index``, ``chunk_count``); and graduates every metadata field to
``| None`` so a chunk-continuation row can carry only the redaction slice.
The shape-validator below mirrors production's three-branch
``@model_validator(mode="after")`` (non-chunked / chunk-header /
chunk-continuation) so the strict-mirror failure modes track production's.

Reference: .claude/rules/testing-signal.md (drift detection pattern).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from signalforge.safety.models import RedactionReason, SamplingMode


class StrictAuditEvent(BaseModel):
    """Mirror of production AuditEvent with extra='forbid' (v4 shape).

    If you add a field to AuditEvent (signalforge.safety.models), you MUST:
    1. Add it here, and
    2. Update tests/fixtures/safety/audit_events_sample.jsonl via
       tests/fixtures/safety/regenerate.sh.

    The shape validator mirrors production's three valid combinations
    (non-chunked / chunk-header / chunk-continuation); see the class
    docstring on :class:`signalforge.safety.models.AuditEvent` for the
    full contract.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    # Metadata fields — non-None on non-chunked + chunk-header rows, None on
    # chunk-continuation rows (the validator enforces this).
    timestamp: datetime | None = None
    model_unique_id: str | None = None
    mode: SamplingMode | None = None
    columns_sent: tuple[str, ...] | None = None
    row_count: int | None = None
    signalforge_version: str | None = None
    policy_hash: str | None = None
    policy_flags: tuple[str, ...] | None = None

    # v4 redaction representation (symbol-table-by-reason + mapback).
    redactions_by_reason: dict[RedactionReason, tuple[str, ...]] | None = None
    column_name_map: dict[str, str] | None = None

    # Chunk-correlation triple. All three ``None`` means non-chunked.
    audit_id: str | None = None
    chunk_index: int | None = None
    chunk_count: int | None = None

    audit_schema_version: int

    @model_validator(mode="after")
    def _enforce_chunk_shape(self) -> StrictAuditEvent:
        """Mirror production's three-branch shape validator verbatim.

        Three valid shapes:
        - Non-chunked: triple all None, metadata required, both redaction
          maps present (may be empty dicts).
        - Chunk header: triple all set, ``chunk_index == 0``,
          ``chunk_count >= 2``, metadata required, both redaction maps
          MUST be empty dicts.
        - Chunk continuation: triple all set,
          ``1 <= chunk_index < chunk_count``, metadata MUST be None,
          redaction maps carry the per-chunk slice.
        """
        triple_all_none = (
            self.audit_id is None and self.chunk_index is None and self.chunk_count is None
        )
        triple_all_set = (
            self.audit_id is not None
            and self.chunk_index is not None
            and self.chunk_count is not None
        )
        if not (triple_all_none or triple_all_set):
            raise ValueError(
                "audit_id, chunk_index, and chunk_count must all be set together or all be None"
            )

        if triple_all_none:
            missing_metadata = [
                name
                for name, value in (
                    ("timestamp", self.timestamp),
                    ("model_unique_id", self.model_unique_id),
                    ("mode", self.mode),
                    ("columns_sent", self.columns_sent),
                    ("signalforge_version", self.signalforge_version),
                    ("policy_hash", self.policy_hash),
                    ("policy_flags", self.policy_flags),
                )
                if value is None
            ]
            if missing_metadata:
                raise ValueError(
                    "non-chunked AuditEvent requires metadata fields: "
                    f"{', '.join(missing_metadata)}"
                )
            if self.redactions_by_reason is None or self.column_name_map is None:
                raise ValueError(
                    "non-chunked AuditEvent requires both redactions_by_reason "
                    "and column_name_map (may be empty dicts)"
                )
            return self

        # Chunked path. Narrowing for static checkers.
        assert self.chunk_index is not None  # noqa: S101
        assert self.chunk_count is not None  # noqa: S101

        if self.chunk_count < 2:
            raise ValueError(
                f"chunk_count must be >= 2 on a chunked event (got {self.chunk_count})"
            )
        if self.chunk_index < 0:
            raise ValueError(
                f"chunk_index must be >= 0 on a chunked event (got {self.chunk_index})"
            )
        if self.chunk_index >= self.chunk_count:
            raise ValueError(
                f"chunk_index ({self.chunk_index}) must be < chunk_count ({self.chunk_count})"
            )

        if self.chunk_index == 0:
            missing_metadata = [
                name
                for name, value in (
                    ("timestamp", self.timestamp),
                    ("model_unique_id", self.model_unique_id),
                    ("mode", self.mode),
                    ("columns_sent", self.columns_sent),
                    ("signalforge_version", self.signalforge_version),
                    ("policy_hash", self.policy_hash),
                    ("policy_flags", self.policy_flags),
                )
                if value is None
            ]
            if missing_metadata:
                raise ValueError(
                    "chunk-header AuditEvent (chunk_index=0) requires metadata "
                    f"fields: {', '.join(missing_metadata)}"
                )
            if self.redactions_by_reason is None or self.column_name_map is None:
                raise ValueError(
                    "chunk-header AuditEvent requires redactions_by_reason and "
                    "column_name_map to be empty dicts (not None)"
                )
            if self.redactions_by_reason or self.column_name_map:
                raise ValueError(
                    "chunk-header AuditEvent (chunk_index=0) requires "
                    "redactions_by_reason and column_name_map to be EMPTY dicts; "
                    "the redaction body rides on continuation rows"
                )
            return self

        # Continuation.
        present_metadata = [
            name
            for name, value in (
                ("timestamp", self.timestamp),
                ("model_unique_id", self.model_unique_id),
                ("mode", self.mode),
                ("columns_sent", self.columns_sent),
                ("row_count", self.row_count),
                ("signalforge_version", self.signalforge_version),
                ("policy_hash", self.policy_hash),
                ("policy_flags", self.policy_flags),
            )
            if value is not None
        ]
        if present_metadata:
            raise ValueError(
                "chunk-continuation AuditEvent (chunk_index >= 1) must omit "
                f"metadata fields: {', '.join(present_metadata)} present"
            )
        if self.redactions_by_reason is None or self.column_name_map is None:
            raise ValueError(
                "chunk-continuation AuditEvent requires redactions_by_reason "
                "and column_name_map (carrying the per-chunk slice)"
            )
        return self


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
    """Issue #54 bumped audit_schema_version 1 → 2; issue #55 bumped 2 → 3
    when ``policy_hash`` migrated from ``SHA-256[:16]`` to
    ``blake2b(digest_size=8)``; issue #185 bumped 3 → 4 when the v3
    ``redactions`` field was replaced by ``redactions_by_reason`` +
    ``column_name_map`` and the chunk-correlation triple was added.
    Pin the fixture so a future bump without updating the sample line
    breaks the test loudly.
    """
    from signalforge.safety.request import _AUDIT_SCHEMA_VERSION

    for raw in _fixture_lines():
        payload = json.loads(raw)
        assert payload["audit_schema_version"] == _AUDIT_SCHEMA_VERSION


def test_audit_event_fixture_exercises_draft_skip_reason():
    """The fixture must include at least one draft_skip_* RedactionReason
    so consumers gating on audit_schema_version >= 2 can verify their
    parser handles the new reason values (issue #54). Carried through the
    issue-#55 bump 2 → 3 and the issue-#185 bump 3 → 4 (which moved the
    reason from a per-record ``reason`` field to the dict KEYS of
    ``redactions_by_reason``).
    """
    seen_reasons: set[str] = set()
    for raw in _fixture_lines():
        payload = json.loads(raw)
        # The chunk-header row's redactions_by_reason is empty; reasons live
        # on the non-chunked row and on chunk-continuation rows.
        for reason in payload.get("redactions_by_reason") or {}:
            seen_reasons.add(reason)
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


def test_audit_event_fixture_exercises_chunked_shape() -> None:
    """The fixture must cover all three v4 shapes (non-chunked / chunk
    header / chunk continuation) so a reader-helper regression on any
    shape branch breaks the test loudly. Issue #185.
    """
    have_non_chunked = False
    have_chunk_header = False
    have_chunk_cont = False
    audit_ids_seen: dict[str, list[int]] = {}
    for raw in _fixture_lines():
        payload = json.loads(raw)
        audit_id = payload.get("audit_id")
        chunk_index = payload.get("chunk_index")
        if audit_id is None and chunk_index is None:
            have_non_chunked = True
            continue
        # Chunked row: both audit_id and chunk_index must be present.
        assert isinstance(audit_id, str), f"chunked row missing audit_id: {payload}"
        assert isinstance(chunk_index, int), f"chunked row missing chunk_index: {payload}"
        if chunk_index == 0:
            have_chunk_header = True
        else:
            have_chunk_cont = True
        audit_ids_seen.setdefault(audit_id, []).append(chunk_index)
    assert have_non_chunked, "fixture must include one non-chunked event"
    assert have_chunk_header, "fixture must include one chunk-header line"
    assert have_chunk_cont, "fixture must include one chunk-continuation line"
    # The chunked group must be observable in full (header + every
    # continuation up to chunk_count - 1).
    for audit_id, indices in audit_ids_seen.items():
        expected_chunk_count = max(indices) + 1
        assert sorted(indices) == list(range(expected_chunk_count)), (
            f"chunked group {audit_id!r} has gaps: indices={indices}"
        )
