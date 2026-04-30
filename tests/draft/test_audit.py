"""Tests for ``signalforge.draft.audit`` (US-012).

The response-audit module is the draft layer's fail-closed observability
seam (DEC-006 / DEC-008 / DEC-013). It mirrors :mod:`signalforge.safety.audit`
exactly: append one JSONL record per LLM response, fail-closed, with a
POSIX-atomic-append size cap. These tests exercise real I/O on ``tmp_path``
because, per the testing-strategy review for the safety layer, mocks of
``open`` hide buffering bugs that the real syscall surface exposes.
"""

from __future__ import annotations

import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from signalforge.draft.audit import (
    LLMResponseEvent,
    _compute_parsed_schema_hash,
    _compute_response_text_hash,
    _compute_sent_sql_hash,
    write_response_event,
)
from signalforge.draft.errors import LLMResponseAuditRecordTooLargeError
from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTestNotNull,
    CandidateTestUnique,
)

pytestmark = pytest.mark.draft

_FIXTURE_PATH = (
    Path(__file__).parent.parent / "fixtures" / "draft" / "llm_response_audit_sample.jsonl"
)


def _make_event(**overrides: Any) -> LLMResponseEvent:
    base: dict[str, Any] = dict(
        timestamp=datetime(2026, 4, 29, 14, 22, 7, tzinfo=timezone.utc),
        model_unique_id="model.sf_demo.fct_orders",
        prompt_version="a1b2c3d4e5f60718",
        response_text_hash="9f8e7d6c5b4a3928",
        parsed_schema_hash="1122334455667788",
        sent_sql_hash="deadbeefcafef00d",
        cache_creation_input_tokens=4096,
        cache_read_input_tokens=0,
        input_tokens=512,
        output_tokens=1280,
        model="claude-haiku-4-5-20251001",
        signalforge_version="0.1.0.dev0",
        audit_schema_version=1,
    )
    base.update(overrides)
    return LLMResponseEvent(**base)


def _make_candidate_schema() -> CandidateSchema:
    return CandidateSchema(
        name="fct_orders",
        description="One row per order.",
        columns=(
            CandidateColumn(
                name="order_id",
                description="Primary key.",
                tests=(
                    CandidateTestNotNull(column="order_id"),
                    CandidateTestUnique(column="order_id"),
                ),
            ),
            CandidateColumn(name="customer_id", description="FK to customers."),
        ),
    )


# --------------------------------------------------------------------------- #
# LLMResponseEvent round-trip + defaults
# --------------------------------------------------------------------------- #


def test_llm_response_event_round_trip_via_fixture() -> None:
    """Parse the committed fixture, round-trip, and verify equality.

    The fixture (``tests/fixtures/draft/llm_response_audit_sample.jsonl``) is
    an immutable contract — adding a field to ``LLMResponseEvent`` without
    updating the fixture (or vice versa) will surface here, because
    round-trip is bit-stable when both sides agree.
    """
    line = _FIXTURE_PATH.read_text(encoding="utf-8").splitlines()[0]
    event = LLMResponseEvent.model_validate_json(line)
    re_serialised = event.model_dump_json(by_alias=True)
    re_parsed = LLMResponseEvent.model_validate_json(re_serialised)
    assert event == re_parsed
    # And the load-bearing fields survive intact.
    assert event.model_unique_id == "model.sf_demo.fct_orders"
    assert event.prompt_version == "a1b2c3d4e5f60718"
    assert event.audit_schema_version == 1
    assert event.signalforge_version == "0.1.0.dev0"


def test_llm_response_event_audit_schema_version_default_1() -> None:
    """``audit_schema_version`` defaults to 1 when omitted at construction."""
    event = LLMResponseEvent(
        timestamp=datetime(2026, 4, 29, tzinfo=timezone.utc),
        model_unique_id="model.x.y",
        prompt_version="v1",
        response_text_hash="0123456789abcdef",
        parsed_schema_hash="fedcba9876543210",
        sent_sql_hash="abcd1234efgh5678"[:16],
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        input_tokens=10,
        output_tokens=20,
        model="claude-haiku",
        signalforge_version="0.1.0.dev0",
    )
    assert event.audit_schema_version == 1


# --------------------------------------------------------------------------- #
# Hash helpers
# --------------------------------------------------------------------------- #


def test_compute_response_text_hash_deterministic() -> None:
    """Same input → same hash, identically reproducible."""
    text = "the quick brown fox jumps over the lazy dog"
    assert _compute_response_text_hash(text) == _compute_response_text_hash(text)


def test_compute_response_text_hash_16_hex_chars() -> None:
    """The hash is always exactly 16 hex chars from ``[0-9a-f]``."""
    h = _compute_response_text_hash("anything at all")
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_compute_parsed_schema_hash_uses_canonical_json() -> None:
    """Two equal :class:`CandidateSchema` instances hash identically.

    Pydantic's emit order for ``model_dump_json`` is *not* a stable cross-
    version contract, so the helper canonicalises via ``json.dumps`` with
    ``sort_keys=True``. Building two schemas with the same content (but
    reasonably different construction paths) must produce the same hash.
    """
    a = _make_candidate_schema()
    b = _make_candidate_schema()
    assert _compute_parsed_schema_hash(a) == _compute_parsed_schema_hash(b)
    # Sanity: a structurally-different schema produces a different hash.
    different = CandidateSchema(
        name="fct_orders_different",
        description="Different.",
        columns=(CandidateColumn(name="other_col", description="."),),
    )
    assert _compute_parsed_schema_hash(a) != _compute_parsed_schema_hash(different)


def test_compute_sent_sql_hash_deterministic() -> None:
    """Same SQL → same hash; SQL with comments → different hash than without."""
    sql = "select * from foo"
    assert _compute_sent_sql_hash(sql) == _compute_sent_sql_hash(sql)
    sql_with_comment = "select * from foo  -- a comment"
    assert _compute_sent_sql_hash(sql) != _compute_sent_sql_hash(sql_with_comment)


# --------------------------------------------------------------------------- #
# Writer: happy path
# --------------------------------------------------------------------------- #


def test_write_response_event_appends_jsonl(tmp_path: Path) -> None:
    """Writer appends exactly one JSONL line that round-trips via the model."""
    audit_path = tmp_path / "response_audit.jsonl"
    event = _make_event()
    write_response_event(event, audit_path=audit_path)

    contents = audit_path.read_text(encoding="utf-8")
    assert contents.endswith("\n")
    lines = contents.splitlines()
    assert len(lines) == 1

    re_parsed = LLMResponseEvent.model_validate_json(lines[0])
    assert re_parsed == event


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only permission semantics")
def test_write_response_event_creates_file_with_0600_mode(tmp_path: Path) -> None:
    """The audit file is created with mode ``0o600`` (owner-only).

    POSIX umask interactions can make this lenient — we assert at minimum
    that no group/other bits are set, and owner has read/write.
    """
    audit_path = tmp_path / "response_audit.jsonl"
    write_response_event(_make_event(), audit_path=audit_path)

    mode = audit_path.stat().st_mode
    assert stat.S_ISREG(mode)
    # No group/other bits set.
    assert mode & 0o077 == 0
    # Owner has read+write.
    assert mode & 0o600 == 0o600


def test_write_response_event_calls_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``os.fsync`` is invoked exactly once with a valid fd before close."""
    audit_path = tmp_path / "response_audit.jsonl"
    calls: list[int] = []
    real_fsync = os.fsync

    def record_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("signalforge.draft.audit.os.fsync", record_fsync)
    write_response_event(_make_event(), audit_path=audit_path)

    assert len(calls) == 1
    assert calls[0] >= 0


def test_write_response_event_creates_parent_dirs(tmp_path: Path) -> None:
    """Writer ``mkdir -p``s the parent dirs when they don't exist yet."""
    audit_path = tmp_path / "deep" / "nested" / "response_audit.jsonl"
    assert not audit_path.parent.exists()
    write_response_event(_make_event(), audit_path=audit_path)
    assert audit_path.parent.is_dir()
    assert audit_path.exists()


# --------------------------------------------------------------------------- #
# Writer: fail-closed semantics
# --------------------------------------------------------------------------- #


def test_write_response_event_oversize_raises_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversize records raise ``LLMResponseAuditRecordTooLargeError``.

    Crucially, the size check fires BEFORE any file open, so the audit
    file is NOT created — an oversize record leaves no on-disk artefact.
    Lower the limit so we can exercise this without crafting a 4 KB record.
    """
    monkeypatch.setattr(
        "signalforge.draft.audit._RESPONSE_AUDIT_RECORD_LIMIT_BYTES",
        50,
    )
    audit_path = tmp_path / "response_audit.jsonl"
    with pytest.raises(LLMResponseAuditRecordTooLargeError) as excinfo:
        write_response_event(_make_event(), audit_path=audit_path)

    assert excinfo.value.limit == 50
    assert excinfo.value.size > 50
    # No file artefact left behind.
    assert not audit_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only permission semantics")
def test_write_response_event_permission_denied_propagates(tmp_path: Path) -> None:
    """A ``PermissionError`` on the parent dir propagates raw (fail-closed).

    ``write_response_event`` catches NO exceptions internally, so the
    underlying ``OSError`` / ``PermissionError`` propagates to the caller
    (``draft_from_request`` in US-013) which wraps it as
    :class:`LLMResponseAuditWriteError`.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses POSIX permission checks")

    locked = tmp_path / "readonly_dir"
    locked.mkdir()
    audit_path = locked / "subdir" / "response_audit.jsonl"
    locked.chmod(0o500)  # read+execute only — cannot create subdir
    try:
        with pytest.raises(PermissionError):
            write_response_event(_make_event(), audit_path=audit_path)
    finally:
        # Restore so tmp_path cleanup can succeed.
        locked.chmod(0o700)


# --------------------------------------------------------------------------- #
# Two-call append (sanity that JSONL accumulates rather than overwrites)
# --------------------------------------------------------------------------- #


def test_write_response_event_two_calls_two_lines(tmp_path: Path) -> None:
    """Two writes leave exactly two JSONL lines in the same file."""
    audit_path = tmp_path / "response_audit.jsonl"
    write_response_event(_make_event(model_unique_id="model.a"), audit_path=audit_path)
    write_response_event(_make_event(model_unique_id="model.b"), audit_path=audit_path)

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [LLMResponseEvent.model_validate_json(line) for line in lines]
    assert [p.model_unique_id for p in parsed] == ["model.a", "model.b"]
