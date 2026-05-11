"""Tests for ``signalforge.safety.audit`` (US-007).

The audit module is the safety layer's single observability seam (DEC-005,
DEC-011, DEC-022): it appends one JSONL record per LLM call, fail-closed, with
a POSIX-atomic-append size cap and an ANSI-safe lazy-format logger. These
tests exercise real I/O on ``tmp_path`` because, per the testing-strategy
review, mocks of ``open`` hide buffering bugs that the real syscall surface
exposes.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from signalforge.safety.audit import write
from signalforge.safety.errors import AuditRecordTooLargeError
from signalforge.safety.models import AuditEvent, SamplingMode

pytestmark = pytest.mark.safety


def _make_event(**overrides: Any) -> AuditEvent:
    base: dict[str, Any] = dict(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        model_unique_id="model.test.x",
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=("id", "name"),
        redactions=(),
        row_count=None,
        signalforge_version="0.1.0",
        policy_hash="abc123def456789a",
        audit_schema_version=1,
        policy_flags=(),
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
    assert payload["mode"] == SamplingMode.SCHEMA_ONLY.value
    assert payload["columns_sent"] == ["id", "name"]
    assert payload["redactions"] == []
    assert payload["audit_schema_version"] == 1
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
    assert f'"mode": "{SamplingMode.SCHEMA_ONLY.value}"' in msg
    assert '"columns_sent": 2' in msg
    assert '"redacted": 0' in msg
    assert '"audit_schema_version": 1' in msg


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
    assert '"audit_schema_version": 1' in records[0].getMessage()


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
