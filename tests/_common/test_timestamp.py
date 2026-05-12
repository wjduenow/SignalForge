"""Canonical ISO-8601 UTC timestamp helper + cross-writer parity (issue #56).

Pins:

* The helper renders the canonical
  ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` shape for a known fixed input.
* Every fail-closed audit writer that carries a ``timestamp`` field
  (``safety.AuditEvent``, ``draft.LLMResponseEvent``,
  ``prune.PruneEvent``, ``grade.GradeEvent``, plus the
  ``grade.GradingReport`` sidecar) emits byte-equal timestamp text for
  the same input. This is the load-bearing AC of issue #56.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from signalforge._common.timestamp import iso8601_z

_FIXED = datetime(2026, 5, 11, 12, 0, 0, 123456, tzinfo=UTC)
_EXPECTED = "2026-05-11T12:00:00.123456Z"


def test_iso8601_z_renders_canonical_shape() -> None:
    assert iso8601_z(_FIXED) == _EXPECTED


def test_iso8601_z_normalises_non_utc_to_utc() -> None:
    pacific = timezone(timedelta(hours=-8))
    same_instant_pacific = _FIXED.astimezone(pacific)
    assert iso8601_z(same_instant_pacific) == _EXPECTED


def test_iso8601_z_rejects_naive_datetime() -> None:
    naive = datetime(2026, 5, 11, 12, 0, 0, 123456)
    with pytest.raises(ValueError, match="tz-aware"):
        iso8601_z(naive)


def test_cross_writer_timestamp_byte_parity() -> None:
    """Every audit writer that serialises a ``timestamp`` field emits
    the canonical ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` shape for the same
    input — byte-equal across all five surfaces.
    """
    from signalforge.draft.audit import LLMResponseEvent
    from signalforge.grade.models import GradeEvent, GradingReport
    from signalforge.prune.audit import PruneEvent
    from signalforge.safety.models import AuditEvent

    audit = AuditEvent.model_validate(
        {
            "timestamp": _FIXED,
            "model_unique_id": "model.x.y",
            "mode": "schema-only",
            "columns_sent": [],
            "redactions": [],
            "signalforge_version": "0.0.0",
            "policy_hash": "0" * 16,
        }
    )
    response = LLMResponseEvent.model_validate(
        {
            "timestamp": _FIXED,
            "model_unique_id": "model.x.y",
            "prompt_version": "0" * 16,
            "response_text_hash": "0" * 16,
            "parsed_schema_hash": "0" * 16,
            "sent_sql_hash": "0" * 16,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": "claude-x",
            "signalforge_version": "0.0.0",
        }
    )
    prune = PruneEvent.model_validate(
        {
            "signalforge_version": "0.0.0",
            "record_id": "r" * 32,
            "timestamp": _FIXED,
            "config_hash": "0" * 16,
            "model_unique_id": "model.x.y",
            "test": {"type": "not_null", "column": "id"},
            "test_anchor": "x",
            "decision": "kept",
            "reason": "kept",
            "failures": 1,
            "sampled_rows": 10,
            "scope": "sample",
            "elapsed_ms": 1,
            "compiled_sql_hash": "0" * 16,
            "compiled_sql": "SELECT 1",
            "why": "ok",
        }
    )
    grade = GradeEvent.model_validate(
        {
            "signalforge_version": "0.0.0",
            "run_id": "r" * 32,
            "timestamp": _FIXED,
            "model_unique_id": "model.x.y",
            "artifact_id": "column.id.description",
            "criterion_id": "clarity",
            "score": 1.0,
            "passed": True,
            "evidence": "",
            "reasoning": "",
            "rubric_hash": "0" * 16,
            "prompt_version_template": "0" * 16,
            "criterion_prompt_hash": "0" * 16,
            "response_text_hash": "0" * 16,
            "model": "claude-x",
            "input_tokens": 0,
            "output_tokens": 0,
        }
    )
    report = GradingReport.model_validate(
        {
            "signalforge_version": "0.0.0",
            "run_id": "r" * 32,
            "timestamp": _FIXED,
            "duration_seconds": 0.0,
            "model_unique_id": "model.x.y",
            "rubric_hash": "0" * 16,
            "thresholds": (0.0, 0.0),
            "results": (),
        }
    )

    rendered = [
        m.model_dump(mode="json")["timestamp"] for m in (audit, response, prune, grade, report)
    ]
    assert rendered == [_EXPECTED] * 5
