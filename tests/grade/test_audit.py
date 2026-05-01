"""Tests for ``signalforge.grade.audit`` (US-007).

Mirrors :mod:`tests.prune.test_audit` exactly — same fail-closed
contract (DEC-011 of safety-layer.md, DEC-006/008 of llm-drafter.md,
DEC-016 of prune-engine.md), same POSIX-atomic-append size cap, same
``O_APPEND | O_CREAT | 0o600`` open + ``os.fsync`` + close shape.

Each test asserts a load-bearing property of the writer:

* one JSONL line per call; round-trips cleanly through
  :meth:`signalforge.grade.models.GradeEvent.model_validate_json`
* appends to a pre-existing file rather than truncating
* creates the parent ``.signalforge/`` directory when absent
* file mode bits are exactly ``0o600`` (POSIX-only)
* ``os.fsync`` is called exactly once per write
* oversize record raises :class:`GradeAuditRecordTooLargeError` BEFORE
  any file open (no on-disk artefact)
* ``OSError`` on ``os.write`` propagates raw (caller wraps, not writer)
* ``PermissionError`` on a chmod-protected file propagates raw
* the symlink-escape gate via
  :func:`signalforge.warehouse._path_safety.canonicalise_path` rejects
  audit paths that resolve outside the project tree, wrapped as
  :class:`GradeAuditWriteError`
* :func:`_build_grade_event` stamps ``signalforge_version`` and defaults
  the cache-token fields to ``0``

Real I/O on ``tmp_path`` because mocks of ``open`` hide buffering bugs
that the real syscall surface exposes.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import signalforge
from signalforge.grade.audit import (
    _GRADE_AUDIT_RECORD_LIMIT_BYTES,
    _build_grade_event,
    write_grade_event,
)
from signalforge.grade.errors import GradeAuditRecordTooLargeError, GradeAuditWriteError
from signalforge.grade.models import GradeEvent


def _make_event(**overrides: Any) -> GradeEvent:
    base: dict[str, Any] = dict(
        run_id="0123456789abcdef0123456789abcdef",
        timestamp=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
        model_unique_id="model.test.x",
        artifact_id="column.id.description",
        criterion_id="grounded_in_sql",
        score=0.9,
        passed=True,
        evidence="evidence body",
        reasoning="reasoning body",
        rubric_hash="abc1234567890def",
        prompt_version_template="def0987654321abc",
        criterion_prompt_hash="11112222aaaa3333",
        response_text_hash="ffff0000eeee1111",
        model="claude-sonnet-test",
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    base.update(overrides)
    return _build_grade_event(**base)


def _project(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(project_dir, audit_path)`` with an existing project tree.

    ``canonicalise_path`` requires ``project_dir`` to exist (the helper
    calls ``project_dir.resolve(strict=True)``). Set up the canonical
    layout the writer expects: ``<project>/.signalforge/grade.jsonl``.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    return project_dir, audit_path


def test_write_grade_event_appends_one_jsonl_line(tmp_path: Path) -> None:
    """Two calls produce two lines; each parses; each round-trips through
    :meth:`GradeEvent.model_validate_json` losslessly.
    """
    _, audit_path = _project(tmp_path)
    write_grade_event(_make_event(), audit_path=audit_path)
    write_grade_event(_make_event(criterion_id="alt_criterion"), audit_path=audit_path)

    contents = audit_path.read_text(encoding="utf-8")
    assert contents.endswith("\n")
    lines = contents.splitlines()
    assert len(lines) == 2

    payload = json.loads(lines[0])
    assert payload["audit_schema_version"] == 1
    assert payload["model_unique_id"] == "model.test.x"
    assert payload["criterion_id"] == "grounded_in_sql"

    # Round-trip through Pydantic recovers a usable model.
    reparsed = GradeEvent.model_validate_json(lines[0])
    assert reparsed.criterion_id == "grounded_in_sql"
    reparsed_2 = GradeEvent.model_validate_json(lines[1])
    assert reparsed_2.criterion_id == "alt_criterion"


def test_write_grade_event_appends_to_existing_file(tmp_path: Path) -> None:
    """Pre-existing audit log content is preserved; the writer appends."""
    project_dir, audit_path = _project(tmp_path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text('{"prior":"line"}\n', encoding="utf-8")

    write_grade_event(_make_event(), audit_path=audit_path)

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"prior": "line"}
    assert json.loads(lines[1])["model_unique_id"] == "model.test.x"
    # Untouched: ``project_dir`` itself.
    assert project_dir.is_dir()


def test_write_grade_event_creates_parent_directory(tmp_path: Path) -> None:
    """The writer creates the ``.signalforge/`` parent directory when
    absent. The orchestrator (US-008) calls us against a fresh project.
    """
    _, audit_path = _project(tmp_path)
    assert not audit_path.parent.exists()

    write_grade_event(_make_event(), audit_path=audit_path)

    assert audit_path.parent.is_dir()
    assert audit_path.is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only permission semantics")
def test_write_grade_event_uses_safe_perms(tmp_path: Path) -> None:
    """Newly created file has ``0o600`` permission bits (owner-only).

    Mirrors safety / draft / prune: ``O_APPEND | O_CREAT | O_WRONLY``
    with mode ``0o600`` keeps the audit log unreadable to other users on
    shared CI runners.
    """
    _, audit_path = _project(tmp_path)
    write_grade_event(_make_event(), audit_path=audit_path)
    mode = stat.S_IMODE(os.stat(audit_path).st_mode)
    assert mode & 0o077 == 0
    assert mode & 0o600 == 0o600


def test_write_grade_event_calls_fsync(tmp_path: Path) -> None:
    """``os.fsync`` is called exactly once per write — durability is
    load-bearing for fail-closed semantics; without ``fsync`` a power
    loss can drop the audit line that proves the grade decision happened.
    """
    _, audit_path = _project(tmp_path)
    real_fsync = os.fsync
    calls: list[int] = []

    def record_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    with patch("signalforge.grade.audit.os.fsync", side_effect=record_fsync):
        write_grade_event(_make_event(), audit_path=audit_path)

    assert len(calls) == 1
    assert calls[0] >= 0


def test_write_grade_event_oversize_record_raises_too_large_before_open(
    tmp_path: Path,
) -> None:
    """An oversize record raises :class:`GradeAuditRecordTooLargeError`
    BEFORE any file is opened — no on-disk artefact is left behind.

    Constructed by stuffing ``reasoning`` past the 4000-byte cap.
    """
    _, audit_path = _project(tmp_path)
    huge_reasoning = "X" * 4500
    event = _make_event(reasoning=huge_reasoning)

    with pytest.raises(GradeAuditRecordTooLargeError) as excinfo:
        write_grade_event(event, audit_path=audit_path)

    assert excinfo.value.limit == _GRADE_AUDIT_RECORD_LIMIT_BYTES
    assert excinfo.value.size > _GRADE_AUDIT_RECORD_LIMIT_BYTES
    # No on-disk artefact: the size check fires before ``os.open``.
    assert not audit_path.exists()
    # The path-canonicalisation step also did not run, so the parent
    # directory was not created either.
    assert not audit_path.parent.exists()


def test_write_grade_event_propagates_oserror_raw(tmp_path: Path) -> None:
    """``OSError`` on ``os.write`` propagates raw — the caller (the
    orchestrator in US-008) is responsible for wrapping into
    :class:`GradeAuditWriteError`. The writer itself catches NO
    exceptions on the I/O path; the propagation IS the defence.
    """
    _, audit_path = _project(tmp_path)

    def boom(fd: int, data: bytes) -> int:
        raise OSError("simulated write failure")

    with (
        patch("signalforge.grade.audit.os.write", side_effect=boom),
        pytest.raises(OSError) as excinfo,
    ):
        write_grade_event(_make_event(), audit_path=audit_path)

    # Raw OSError, not wrapped GradeAuditWriteError.
    assert "simulated write failure" in str(excinfo.value)
    assert not isinstance(excinfo.value, GradeAuditWriteError)


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="POSIX-only permission semantics; root bypasses chmod restrictions",
)
def test_write_grade_event_propagates_permission_error_when_file_unwritable(
    tmp_path: Path,
) -> None:
    """``PermissionError`` from ``os.open`` on a chmod-protected file
    propagates raw. Mirrors the OSError test — the writer does not wrap.
    """
    _, audit_path = _project(tmp_path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text('{"prior":"line"}\n', encoding="utf-8")
    audit_path.chmod(0o400)
    try:
        with pytest.raises(PermissionError):
            write_grade_event(_make_event(), audit_path=audit_path)
    finally:
        # Restore writable perms so pytest's tmpdir cleanup succeeds.
        audit_path.chmod(0o600)


def test_write_grade_event_short_write_loops_until_complete(tmp_path: Path) -> None:
    """``os.write`` may return fewer bytes than requested (EINTR or
    short-write semantics). The writer loops until the full payload
    lands; the on-disk record must contain every byte.
    """
    _, audit_path = _project(tmp_path)
    real_write = os.write
    call_count = {"n": 0}

    def short_write(fd: int, data: bytes) -> int:
        call_count["n"] += 1
        if call_count["n"] == 1:
            half = len(data) // 2
            return real_write(fd, data[:half])
        return real_write(fd, data)

    with patch("signalforge.grade.audit.os.write", side_effect=short_write):
        write_grade_event(_make_event(), audit_path=audit_path)

    contents = audit_path.read_text(encoding="utf-8")
    assert contents.endswith("\n")
    lines = contents.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["audit_schema_version"] == 1
    assert call_count["n"] >= 2


def test_write_grade_event_short_write_zero_bytes_raises(tmp_path: Path) -> None:
    """A persistent zero-byte return from ``os.write`` raises
    :class:`OSError` rather than spinning forever — the writer guards
    against an infinite loop on a wedged file descriptor.
    """
    _, audit_path = _project(tmp_path)

    def stuck_write(fd: int, data: bytes) -> int:
        return 0

    with (
        patch("signalforge.grade.audit.os.write", side_effect=stuck_write),
        pytest.raises(OSError) as excinfo,
    ):
        write_grade_event(_make_event(), audit_path=audit_path)

    assert "returned 0" in str(excinfo.value) or "disk full" in str(excinfo.value)


def test_write_grade_event_ansi_safe_reasoning(tmp_path: Path) -> None:
    """ANSI escape bytes in ``reasoning`` round-trip safely through JSON.

    The audit log is consumed by terminal viewers; raw ``\\x1b`` bytes
    would inject terminal escape sequences. ``json.dumps`` escapes them
    as ``\\u001b`` — round-trip parse recovers the original cleartext
    while the on-disk bytes never carry a raw escape.
    """
    _, audit_path = _project(tmp_path)
    nasty = "judge says \x1b[31mFAIL\x1b[0m"
    write_grade_event(_make_event(reasoning=nasty), audit_path=audit_path)

    raw_bytes = audit_path.read_bytes()
    assert b"\\u001b" in raw_bytes
    assert b"\x1b" not in raw_bytes

    payload = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["reasoning"] == nasty


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only symlink semantics")
def test_write_grade_event_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink at ``<project>/.signalforge`` pointing outside the
    project tree is rejected; the writer raises
    :class:`GradeAuditWriteError` rather than write outside the project
    sandbox. Mirrors prune's post-QG fix (DEC-016 of prune-engine.md).
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    escape_dir = tmp_path / "escape"
    escape_dir.mkdir()
    # Symlink ``.signalforge`` to a location outside the project tree.
    sf_dir = project_dir / ".signalforge"
    sf_dir.symlink_to(escape_dir, target_is_directory=True)
    audit_path = project_dir / ".signalforge" / "grade.jsonl"

    with pytest.raises(GradeAuditWriteError) as excinfo:
        write_grade_event(_make_event(), audit_path=audit_path)

    # The wrap preserves the underlying path-safety failure.
    assert excinfo.value.cause is not None
    # No on-disk artefact at the would-be canonical location.
    assert not (escape_dir / "grade.jsonl").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only symlink semantics")
def test_write_grade_event_rejects_symlink_loop(tmp_path: Path) -> None:
    """A symlink cycle on the audit-path resolution wraps as
    :class:`GradeAuditWriteError`.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    sf_dir = project_dir / ".signalforge"
    sf_dir.mkdir()
    # Create a self-referential symlink that ``Path.resolve`` cannot
    # break out of.
    loop = sf_dir / "loop"
    loop.symlink_to(loop)
    audit_path = loop / "grade.jsonl"

    with pytest.raises(GradeAuditWriteError):
        write_grade_event(_make_event(), audit_path=audit_path)


def test_build_grade_event_attaches_signalforge_version() -> None:
    """The factory stamps ``signalforge_version`` from
    :data:`signalforge.__version__` so reviewers can identify which
    code shipped the receipt.
    """
    event = _make_event()
    assert event.signalforge_version == signalforge.__version__


def test_build_grade_event_default_cache_token_fields_zero() -> None:
    """When ``cache_creation_input_tokens`` /
    ``cache_read_input_tokens`` are omitted, both default to ``0`` —
    matches the model defaults so unstamped fields don't carry stale
    values from a previous call.
    """
    event = _build_grade_event(
        run_id="r" * 32,
        timestamp=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
        model_unique_id="model.test.x",
        artifact_id="column.id.description",
        criterion_id="grounded_in_sql",
        score=0.5,
        passed=True,
        evidence="",
        reasoning="",
        rubric_hash="abc1234567890def",
        prompt_version_template="def0987654321abc",
        criterion_prompt_hash="11112222aaaa3333",
        response_text_hash="ffff0000eeee1111",
        model="claude-sonnet-test",
        input_tokens=10,
        output_tokens=5,
    )
    assert event.cache_creation_input_tokens == 0
    assert event.cache_read_input_tokens == 0


def test_build_grade_event_preserves_none_score() -> None:
    """``score=None`` is the documented degraded-path sentinel
    (DEC-015) — the factory passes it through unchanged.
    """
    event = _make_event(score=None, passed=False)
    assert event.score is None
    assert event.passed is False
