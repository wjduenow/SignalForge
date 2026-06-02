"""Tests for ``signalforge.safety.audit`` (US-007 + US-003 of #185).

The audit module is the safety layer's single observability seam (DEC-005,
DEC-011, DEC-022): it appends one JSONL record per LLM call, fail-closed, with
a POSIX-atomic-append size cap and an ANSI-safe lazy-format logger. These
tests exercise real I/O on ``tmp_path`` because, per the testing-strategy
review, mocks of ``open`` hide buffering bugs that the real syscall surface
exposes.

US-003 of #185 adds v4 chunking: the writer now emits N≥1 lines per logical
event (header + continuations) when the serialised form exceeds the
POSIX-atomic-append cap. New tests at the bottom of the module pin the
chunker, the pre-open size check, per-chunk fsync, and the reader-helper
reassembly path.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from signalforge.safety.audit import _chunk_event, read_audit_events, write
from signalforge.safety.errors import AuditRecordTooLargeError
from signalforge.safety.models import AuditEvent, SamplingMode

pytestmark = pytest.mark.safety


def _make_event(**overrides: Any) -> AuditEvent:
    """Build a small non-chunked v4 AuditEvent (issue #185).

    The v3 ``redactions: tuple[RedactionRecord, ...]`` field was replaced
    by the v4 symbol-table-by-reason maps; this default has empty
    redaction maps so the event fits trivially in a single chunk. Tests
    that need a wide-table event (multi-chunk emission) use
    :func:`_wide_event` further down the module.
    """
    base: dict[str, Any] = dict(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        model_unique_id="model.test.x",
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=("id", "name"),
        row_count=None,
        signalforge_version="0.1.0",
        policy_hash="abc123def456789a",
        policy_flags=(),
        redactions_by_reason={},
        column_name_map={},
        audit_id=None,
        chunk_index=None,
        chunk_count=None,
        audit_schema_version=4,
    )
    base.update(overrides)
    return AuditEvent(**base)


def test_audit_write_appends_one_jsonl_line(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    write(_make_event(), audit_path)
    assert audit_path.exists()
    contents = audit_path.read_text(encoding="utf-8")
    assert contents.endswith("\n")
    lines = contents.splitlines()
    assert len(lines) == 1
    json.loads(lines[0])  # parses


def test_audit_write_round_trips_through_json_loads(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    write(_make_event(), audit_path)
    payload = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["model_unique_id"] == "model.test.x"
    assert payload["mode"] == SamplingMode.SCHEMA_ONLY
    assert payload["columns_sent"] == ["id", "name"]
    # v4: ``redactions_by_reason`` + ``column_name_map`` replaced the v3
    # ``redactions: tuple[RedactionRecord, ...]`` field (issue #185).
    assert payload["redactions_by_reason"] == {}
    assert payload["column_name_map"] == {}
    assert "redactions" not in payload
    assert payload["audit_schema_version"] == 4
    assert payload["signalforge_version"] == "0.1.0"
    assert payload["policy_hash"] == "abc123def456789a"


def test_audit_write_creates_parent_dir_with_mode_0o700(tmp_path: Path) -> None:
    audit_path = tmp_path / ".signalforge" / "audit.jsonl"
    assert not audit_path.parent.exists()
    write(_make_event(), audit_path)
    assert audit_path.parent.is_dir()
    mode = audit_path.parent.stat().st_mode & 0o777
    # Be lenient: assert group/other bits are zero.
    assert mode & 0o077 == 0
    # And owner has read/write/exec at minimum.
    assert mode & 0o700 == 0o700


def test_audit_write_two_calls_two_lines(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    write(_make_event(model_unique_id="model.test.a"), audit_path)
    write(_make_event(model_unique_id="model.test.b"), audit_path)
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert [p["model_unique_id"] for p in parsed] == ["model.test.a", "model.test.b"]


def test_audit_write_emits_logger_info_line(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    with caplog.at_level(logging.INFO, logger="signalforge.safety"):
        write(_make_event(), audit_path)

    records = [r for r in caplog.records if r.name == "signalforge.safety"]
    assert len(records) == 1
    msg = records[0].getMessage()
    # Summary JSON should embed the key fields. The summary uses ``unique_id``
    # rather than the full ``model_unique_id`` field name to keep the line
    # short — both name and value are present.
    assert "model.test.x" in msg
    assert f'"mode": "{SamplingMode.SCHEMA_ONLY}"' in msg
    assert '"columns_sent": 2' in msg
    assert '"redacted": 0' in msg
    assert '"audit_schema_version": 4' in msg


def test_audit_write_logger_message_escapes_ansi_in_user_input(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    nasty = "\x1b[31mFAKE\x1b[0m"
    with caplog.at_level(logging.INFO, logger="signalforge.safety"):
        write(_make_event(model_unique_id=nasty), audit_path)

    records = [r for r in caplog.records if r.name == "signalforge.safety"]
    assert len(records) == 1
    raw_msg = records[0].getMessage()
    # The raw ANSI escape byte (ESC, 0x1b) must NOT appear in the rendered
    # log message — json.dumps escapes it as .
    assert "\x1b" not in raw_msg
    assert "\\u001b" in raw_msg


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only permission semantics")
def test_audit_write_failure_on_unwritable_parent_propagates_raw(
    tmp_path: Path,
) -> None:
    """A ``PermissionError`` on the parent dir propagates raw (fail-closed).

    Mirrors :func:`tests.draft.test_audit.test_write_response_event_permission_denied_propagates`:
    ``write`` catches NO exceptions internally, so the underlying
    ``OSError`` / ``PermissionError`` propagates to the caller
    (``build_llm_request`` in US-010) which wraps it as
    :class:`AuditWriteError`.
    """
    # Skip when running as root because root bypasses permission checks.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses POSIX permission checks")

    locked = tmp_path / "locked"
    locked.mkdir()
    audit_path = locked / "denied" / "audit.jsonl"
    locked.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            write(_make_event(), audit_path)
    finally:
        # Restore so tmp_path cleanup can succeed.
        locked.chmod(0o700)


def test_audit_write_oversize_record_raises_too_large(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("signalforge.safety.audit._AUDIT_RECORD_LIMIT_BYTES", 50)
    audit_path = tmp_path / "audit.jsonl"
    with pytest.raises(AuditRecordTooLargeError) as excinfo:
        write(_make_event(), audit_path)
    assert excinfo.value.limit == 50
    assert excinfo.value.size > 50


def test_audit_write_oversize_does_not_create_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("signalforge.safety.audit._AUDIT_RECORD_LIMIT_BYTES", 50)
    audit_path = tmp_path / "audit.jsonl"
    with pytest.raises(AuditRecordTooLargeError):
        write(_make_event(), audit_path)
    assert not audit_path.exists()


def test_audit_write_concurrent_threads_no_interleave(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"

    def writer(thread_idx: int) -> None:
        for i in range(50):
            write(
                _make_event(model_unique_id=f"thread.{thread_idx}.row.{i}"),
                audit_path,
            )

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(writer, range(10)))

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 500
    parsed = [json.loads(line) for line in lines]
    unique_ids = {p["model_unique_id"] for p in parsed}
    assert len(unique_ids) == 500


def test_audit_write_does_not_swallow_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``write`` propagates the raw ``OSError`` from ``os.write`` — the
    fail-closed contract is that the writer never wraps. The orchestrator
    (``build_llm_request``) owns the typed wrap.
    """
    audit_path = tmp_path / "audit.jsonl"

    def boom(fd: int, data: bytes) -> int:  # pragma: no cover - patched out
        raise OSError("simulated write failure")

    monkeypatch.setattr("signalforge.safety.audit.os.write", boom)
    with pytest.raises(OSError, match="simulated write failure"):
        write(_make_event(), audit_path)


def test_audit_write_serialisation_failure_propagates_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``json.dumps`` failure propagates raw — same fail-closed
    contract as the I/O syscalls. The orchestrator wraps; the writer
    does not.
    """
    audit_path = tmp_path / "audit.jsonl"

    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise TypeError("simulated json failure")

    # Force the json.dumps call inside audit.write to fail.
    monkeypatch.setattr("signalforge.safety.audit.json.dumps", boom)
    with pytest.raises(TypeError, match="simulated json failure"):
        write(_make_event(), audit_path)


def test_audit_write_zero_bytes_raises_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-byte return from ``os.write`` indicates an unrecoverable
    I/O failure (disk full, etc.) — the writer raises ``OSError``
    rather than spinning forever. Mirrors the diff sidecar's
    ``test_write_sidecar_short_write_zero_bytes_raises``.
    """
    audit_path = tmp_path / "audit.jsonl"

    def zero_write(fd: int, data: bytes) -> int:
        return 0

    monkeypatch.setattr("signalforge.safety.audit.os.write", zero_write)
    with pytest.raises(OSError, match="os.write returned 0"):
        write(_make_event(), audit_path)


def test_audit_write_fsyncs_before_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    calls: list[int] = []
    real_fsync = os.fsync

    def record_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("signalforge.safety.audit.os.fsync", record_fsync)
    write(_make_event(), audit_path)
    assert len(calls) == 1
    assert calls[0] >= 0


def test_audit_write_logger_includes_audit_schema_version(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    with caplog.at_level(logging.INFO, logger="signalforge.safety"):
        write(_make_event(), audit_path)
    records = [r for r in caplog.records if r.name == "signalforge.safety"]
    assert len(records) == 1
    assert '"audit_schema_version": 4' in records[0].getMessage()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only permission semantics")
def test_audit_write_file_perms_0o600_when_newly_created(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    write(_make_event(), audit_path)
    mode = audit_path.stat().st_mode
    # Lenient assertion (umask interactions): no group/other bits.
    assert mode & 0o077 == 0
    # And it is a regular file with owner read/write.
    assert stat.S_ISREG(mode)
    assert mode & 0o600 == 0o600


# ---------------------------------------------------------------------------
# US-003 of #185 — v4 chunker + multi-line writer + reader helper
# ---------------------------------------------------------------------------


def _make_v4_event(
    *,
    redactions_by_reason: dict[str, tuple[str, ...]] | None = None,
    column_name_map: dict[str, str] | None = None,
    **overrides: Any,
) -> AuditEvent:
    """Build a non-chunked v4 ``AuditEvent`` for the chunker/writer/reader tests.

    Defaults to small empty redaction maps; pass ``redactions_by_reason``
    / ``column_name_map`` to drive the chunker into the multi-chunk path.
    """
    base: dict[str, Any] = dict(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        model_unique_id="model.test.x",
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=("id", "name"),
        row_count=None,
        signalforge_version="0.1.0",
        policy_hash="abc123def456789a",
        policy_flags=(),
        redactions_by_reason=redactions_by_reason if redactions_by_reason is not None else {},
        column_name_map=column_name_map if column_name_map is not None else {},
        audit_id=None,
        chunk_index=None,
        chunk_count=None,
        audit_schema_version=4,
    )
    base.update(overrides)
    return AuditEvent(**base)


def _wide_event(col_count: int = 500) -> AuditEvent:
    """Build an event with ``col_count`` redacted columns so the v4 serialised
    form exceeds the POSIX-atomic-append cap and forces multi-chunk emission.

    Keeps ``columns_sent`` short (the LLM-bound payload column list is a few
    schema-visible columns; PII-redacted columns drop OUT of ``columns_sent``
    per the safety layer's redaction semantics). The bloat lives in
    ``redactions_by_reason`` + ``column_name_map`` — exactly the shape the
    chunker exists to handle.
    """
    hashed_names = tuple(f"col_{i:08x}" for i in range(col_count))
    real_names = {
        h: f"real_column_name_long_enough_to_inflate_{i:04d}" for i, h in enumerate(hashed_names)
    }
    return _make_v4_event(
        columns_sent=("id", "name"),
        redactions_by_reason={"pattern_match": hashed_names},
        column_name_map=real_names,
    )


def test_chunk_event_small_record_returns_single_chunk() -> None:
    """A small (non-wide) event fits in one ≤limit line — the chunker
    returns a single-element tuple."""
    chunks = _chunk_event(_make_v4_event(), limit=4000)
    assert len(chunks) == 1
    assert chunks[0].endswith(b"\n")
    assert len(chunks[0]) <= 4000


def test_chunk_event_wide_record_splits_into_multiple_chunks_each_under_cap() -> None:
    """A wide event (500 columns) exceeds the cap → the chunker emits a
    header + N-1 continuations, each within the cap."""
    chunks = _chunk_event(_wide_event(500), limit=4000)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 4000, f"chunk len {len(chunk)} > 4000"
        assert chunk.endswith(b"\n")

    # Validate shape: header (chunk_index=0) carries metadata + empty maps;
    # continuations (chunk_index >= 1) carry the redaction slice.
    payloads = [json.loads(c) for c in chunks]
    assert payloads[0]["chunk_index"] == 0
    chunk_count = payloads[0]["chunk_count"]
    assert chunk_count == len(chunks)
    assert payloads[0]["redactions_by_reason"] == {}
    assert payloads[0]["column_name_map"] == {}
    assert payloads[0]["model_unique_id"] == "model.test.x"

    audit_id = payloads[0]["audit_id"]
    assert audit_id is not None
    for i, payload in enumerate(payloads[1:], start=1):
        assert payload["chunk_index"] == i
        assert payload["chunk_count"] == chunk_count
        assert payload["audit_id"] == audit_id
        # Continuation: metadata fields are None / absent (model_dump emits None)
        # and the slice is non-empty.
        assert payload.get("model_unique_id") is None
        assert payload["redactions_by_reason"]


def test_chunk_event_deterministic_audit_id() -> None:
    """Two calls with the same event produce the same ``audit_id`` — the
    correlation key is deterministic over (model_unique_id, timestamp,
    signalforge_version) so reassembly works across runs."""
    event = _wide_event(500)
    chunks_a = _chunk_event(event, limit=4000)
    chunks_b = _chunk_event(event, limit=4000)
    a_header = json.loads(chunks_a[0])
    b_header = json.loads(chunks_b[0])
    assert a_header["audit_id"] is not None
    assert a_header["audit_id"] == b_header["audit_id"]


def test_write_pre_open_size_check_rejects_pathological_chunk_no_artifact(
    tmp_path: Path,
) -> None:
    """A single hashed column name way oversized (cannot fit in any single
    chunk) → ``AuditRecordTooLargeError`` BEFORE any file open; no artifact."""
    audit_path = tmp_path / ".signalforge" / "audit.jsonl"
    # A single hashed-name string larger than the chunk cap itself —
    # even one entry cannot fit into a single chunk envelope.
    pathological = "x" * 6000  # well over the 4000-byte cap
    event = _make_v4_event(
        redactions_by_reason={"pattern_match": (pathological,)},
        column_name_map={pathological: "real"},
    )
    with pytest.raises(AuditRecordTooLargeError):
        write(event, audit_path)
    # No on-disk artefact — neither the file nor (importantly) any line in it.
    assert not audit_path.exists()


def test_write_per_chunk_fsync_called_n_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a chunked write (N≥2 lines), ``os.fsync`` is called once per
    chunk (per-line durability)."""
    audit_path = tmp_path / "audit.jsonl"
    calls: list[int] = []
    real_fsync = os.fsync

    def record_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("signalforge.safety.audit.os.fsync", record_fsync)
    event = _wide_event(500)
    write(event, audit_path)
    chunks = _chunk_event(event, limit=4000)
    assert len(calls) == len(chunks), f"expected {len(chunks)} fsync call(s), got {len(calls)}"


def test_write_header_first_then_continuations_in_order(tmp_path: Path) -> None:
    """The header (chunk_index=0) lands as the FIRST line; continuations
    follow in chunk_index order. Mid-write crash leaves "header + < N
    chunks" — operator-visible incompleteness signal (DEC-006)."""
    audit_path = tmp_path / "audit.jsonl"
    event = _wide_event(500)
    write(event, audit_path)
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    payloads = [json.loads(line) for line in lines]
    assert payloads[0]["chunk_index"] == 0
    expected_count = payloads[0]["chunk_count"]
    assert len(lines) == expected_count
    for i, payload in enumerate(payloads):
        assert payload["chunk_index"] == i, f"line {i} has chunk_index {payload['chunk_index']}"


def test_read_audit_events_reassembles_chunked_record(tmp_path: Path) -> None:
    """Write a chunked event; ``read_audit_events`` reassembles it
    byte-for-byte equal to the original (via ``model_dump_json`` round-trip
    with sorted keys + excluded None)."""
    audit_path = tmp_path / "audit.jsonl"
    original = _wide_event(500)
    write(original, audit_path)

    events = list(read_audit_events(audit_path))
    assert len(events) == 1
    reassembled = events[0]

    # The reassembled event has audit_id/chunk_index/chunk_count cleared so
    # it round-trips through the non-chunked validator branch and
    # byte-compares against the original.
    original_dump = original.model_dump_json(by_alias=True, exclude_none=True)
    reassembled_dump = reassembled.model_dump_json(by_alias=True, exclude_none=True)
    # Both should be in the non-chunked shape; compare canonicalised content.
    assert json.loads(reassembled_dump) == json.loads(original_dump)


def test_read_audit_events_passes_non_chunked_through(tmp_path: Path) -> None:
    """A single-line v4 record (non-chunked) reads back unchanged."""
    audit_path = tmp_path / "audit.jsonl"
    event = _make_v4_event(model_unique_id="model.test.single")
    write(event, audit_path)
    events = list(read_audit_events(audit_path))
    assert len(events) == 1
    assert events[0].model_unique_id == "model.test.single"
    assert events[0].audit_id is None
    assert events[0].chunk_index is None
    assert events[0].chunk_count is None


def test_read_audit_events_warns_on_partial_group(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Drop one continuation from a chunked group → the reader logs a
    WARNING and skips the incomplete group (does NOT raise)."""
    audit_path = tmp_path / "audit.jsonl"
    write(_wide_event(500), audit_path)

    # Drop the LAST line so the group is incomplete (header + N-1 of N
    # required continuations present).
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 3, "expected ≥3 chunks for the partial-group test"
    truncated = "\n".join(lines[:-1]) + "\n"
    audit_path.write_text(truncated, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="signalforge.safety"):
        events = list(read_audit_events(audit_path))

    # Incomplete group is skipped (yields nothing).
    assert events == []
    # And a WARNING was surfaced.
    warnings_emitted = [
        r for r in caplog.records if r.name == "signalforge.safety" and r.levelno >= logging.WARNING
    ]
    assert len(warnings_emitted) >= 1, "expected a partial-group WARNING"


# ---------------------------------------------------------------------------
# US-004 of #185 — column_count propagation through the writer raise site
# ---------------------------------------------------------------------------


def test_audit_write_too_large_propagates_column_count_in_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEC-007 (#185 US-004): when ``write()`` raises ``AuditRecordTooLargeError``
    because a chunk exceeds the cap, the error carries a non-None
    ``column_count`` (derived from ``column_name_map`` size + summed
    ``redactions_by_reason`` value-list lengths) and the rendered remediation
    text includes the "Model has N columns" prefix.

    Drives the writer with a tiny artificial cap so any non-trivial event
    over-caps a chunk; asserts the propagation path runs.
    """
    # Force a tiny cap so even a small event over-caps a chunk → triggers
    # the pre-open size check + the typed raise with column_count.
    monkeypatch.setattr("signalforge.safety.audit._AUDIT_RECORD_LIMIT_BYTES", 200)
    audit_path = tmp_path / "audit.jsonl"

    # An event with a handful of redacted columns; under a 200-byte cap the
    # serialised single-chunk form well exceeds the limit, and even after
    # chunking individual chunks remain oversized — exactly the
    # pre-open-raise path US-004 targets.
    hashed_names = tuple(f"col_{i:08x}" for i in range(8))
    real_names = {h: f"real_column_name_{i:04d}" for i, h in enumerate(hashed_names)}
    event = _make_v4_event(
        redactions_by_reason={"pattern_match": hashed_names},
        column_name_map=real_names,
    )

    with pytest.raises(AuditRecordTooLargeError) as excinfo:
        write(event, audit_path)

    err = excinfo.value
    # Column count is the best-available estimate: column_name_map size +
    # sum of redaction-list lengths. The fake event uses identical hashed
    # names in both, so the estimate is len(map) + len(redaction list) ==
    # 8 + 8 == 16.
    assert err.column_count is not None
    assert err.column_count > 0
    assert err.column_count == 16

    # The rendered remediation includes the "Model has N columns" prefix,
    # the skip_draft workaround, the explicit aggregate-only NOT-a-workaround
    # clarification, and the follow-up issue pointer — the three-sentence
    # operator script.
    rendered = err.remediation
    assert f"Model has {err.column_count} columns" in rendered
    assert "meta.signalforge.skip_draft: true" in rendered
    assert "safety.mode: aggregate-only does NOT shrink" in rendered
    assert "issue #185 follow-up" in rendered

    # And the writer fail-closed contract held — no on-disk artefact.
    assert not audit_path.exists()
