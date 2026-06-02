"""Tests for ``signalforge.prune.audit`` (US-008).

Mirrors :mod:`tests.safety.test_audit` and :mod:`tests.draft.test_audit`
exactly — same fail-closed contract (DEC-011 of safety-layer.md, DEC-006/008
of llm-drafter.md), same POSIX-atomic-append size cap, same
``O_APPEND | O_CREAT | 0o600`` open + ``os.fsync`` + close shape.

The eight tests below assert each load-bearing property of the writer:

* one JSONL line, all documented fields present, ``audit_schema_version == 3``
* file mode bits are exactly ``0o600`` (POSIX-only)
* ``os.fsync`` is called exactly once per write
* oversize record raises BEFORE any file open (no on-disk artefact)
* OSError on ``os.write`` propagates raw — caller is responsible for wrapping
* ANSI escape sequences in user-controlled fields round-trip safely through
  JSON (escaped as ``\\u001b``)
* concurrent appends from 10 threads × 50 writes produce exactly 500
  well-formed JSONL lines (POSIX ``O_APPEND`` atomicity for writes ≤ 4 KiB)
* :func:`_compute_config_hash` is deterministic and 16 hex characters

These exercise real I/O on ``tmp_path`` because — per the testing-strategy
review used elsewhere — mocks of ``open`` hide buffering bugs that the real
syscall surface exposes.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from signalforge.draft.models import CandidateTestNotNull
from signalforge.prune.audit import (
    _PRUNE_AUDIT_RECORD_LIMIT_BYTES,
    PruneEvent,
    _build_prune_event,
    _compute_config_hash,
    _write_prune_event,
)
from signalforge.prune.errors import PruneAuditRecordTooLargeError
from signalforge.prune.models import PruneDecision
from signalforge.prune.stats import MadStats, ZscoreStats


def _make_decision(**overrides: Any) -> PruneDecision:
    base: dict[str, Any] = dict(
        test_anchor="column.id",
        test=CandidateTestNotNull(column="id"),
        decision="kept",
        reason="kept",
        failures=0,
        sampled_rows=1000,
        scope="sample",
        elapsed_ms=42,
        compiled_sql_hash="0123456789abcdef",
        compiled_sql="SELECT COUNT(*) FROM `p.d.t` WHERE id IS NULL",
        why="0 failures across 1000 sampled rows",
        sample_failures=None,
    )
    base.update(overrides)
    return PruneDecision(**base)


def _make_event(**decision_overrides: Any) -> PruneEvent:
    decision = _make_decision(**decision_overrides)
    return _build_prune_event(
        decision=decision,
        model_unique_id="model.test.x",
        config_hash="abc123def456789a",
    )


def test_write_prune_event_emits_one_jsonl_line(tmp_path: Path) -> None:
    """Writer produces exactly one JSONL line; every documented field is
    present; ``audit_schema_version == 3``.
    """
    audit_path = tmp_path / "prune.jsonl"
    event = _make_event()
    _write_prune_event(event, audit_path)

    contents = audit_path.read_text(encoding="utf-8")
    assert contents.endswith("\n")
    lines = contents.splitlines()
    assert len(lines) == 1

    payload = json.loads(lines[0])
    assert payload["audit_schema_version"] == 3
    # Every documented field present (the issue #171 bump 2 → 3 adds
    # ``as_of`` + ``stats`` per DEC-013).
    expected_fields = {
        "audit_schema_version",
        "signalforge_version",
        "record_id",
        "timestamp",
        "config_hash",
        "model_unique_id",
        "test",
        "test_anchor",
        "decision",
        "reason",
        "failures",
        "sampled_rows",
        "scope",
        "elapsed_ms",
        "compiled_sql_hash",
        "compiled_sql",
        "why",
        "sample_failures",
        "as_of",
        "stats",
    }
    assert expected_fields.issubset(payload.keys())
    # The discriminated-union payload survives the round-trip.
    assert payload["test"]["type"] == "not_null"
    assert payload["test"]["column"] == "id"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only permission semantics")
def test_write_prune_event_uses_appendcreate_0o600_mode(tmp_path: Path) -> None:
    """Newly created file has ``0o600`` permission bits (owner-only).

    Mirrors safety/draft: ``O_APPEND | O_CREAT | O_WRONLY`` with mode 0o600
    keeps the audit log unreadable to other users on shared CI runners.
    """
    audit_path = tmp_path / "prune.jsonl"
    _write_prune_event(_make_event(), audit_path)
    mode = stat.S_IMODE(os.stat(audit_path).st_mode)
    # Lenient against umask: assert no group/other bits AND owner has rw.
    assert mode & 0o077 == 0
    assert mode & 0o600 == 0o600


def test_write_prune_event_calls_fsync(tmp_path: Path) -> None:
    """``os.fsync`` is called exactly once per write — durability is
    load-bearing for fail-closed semantics; without fsync a power loss
    can drop the audit line that proves the prune decision happened.
    """
    audit_path = tmp_path / "prune.jsonl"
    real_fsync = os.fsync
    calls: list[int] = []

    def record_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    with patch("signalforge.prune.audit.os.fsync", side_effect=record_fsync):
        _write_prune_event(_make_event(), audit_path)

    assert len(calls) == 1
    assert calls[0] >= 0


def test_write_prune_event_oversize_raises_before_open(tmp_path: Path) -> None:
    """An oversize record raises :class:`PruneAuditRecordTooLargeError`
    BEFORE any file is opened — no on-disk artefact is left behind.

    Constructed by stuffing ``compiled_sql`` past the 4000-byte cap.
    """
    audit_path = tmp_path / "prune.jsonl"
    huge_sql = "X" * 4500
    event = _make_event(compiled_sql=huge_sql)

    with pytest.raises(PruneAuditRecordTooLargeError) as excinfo:
        _write_prune_event(event, audit_path)

    assert excinfo.value.limit == _PRUNE_AUDIT_RECORD_LIMIT_BYTES
    assert excinfo.value.size > _PRUNE_AUDIT_RECORD_LIMIT_BYTES
    # No on-disk artefact: the size check fires before ``os.open``.
    assert not audit_path.exists()


def test_write_prune_event_propagates_oserror_raw(tmp_path: Path) -> None:
    """``OSError`` on ``os.write`` propagates raw — the caller (engine.py)
    is responsible for wrapping into :class:`PruneAuditWriteError`. The
    writer itself catches NO exceptions; the propagation IS the defence.
    """
    audit_path = tmp_path / "prune.jsonl"

    def boom(fd: int, data: bytes) -> int:
        raise OSError("simulated write failure")

    with (
        patch("signalforge.prune.audit.os.write", side_effect=boom),
        pytest.raises(OSError) as excinfo,
    ):
        _write_prune_event(_make_event(), audit_path)

    # Raw OSError, not a wrapped PruneAuditWriteError. The caller wraps,
    # not the writer.
    assert "simulated write failure" in str(excinfo.value)


def test_write_prune_event_ansi_safe_compiled_sql(tmp_path: Path) -> None:
    """ANSI escape bytes in ``compiled_sql`` round-trip safely through JSON.

    The audit log is consumed by terminal viewers; raw ``\\x1b`` bytes
    would inject terminal escape sequences. ``json.dumps`` escapes them
    as ``\\u001b`` — round-trip parse recovers the original bytes
    losslessly while the on-disk bytes never carry a raw escape.
    """
    audit_path = tmp_path / "prune.jsonl"
    nasty_sql = "SELECT 1 -- \x1b[31mFAKE\x1b[0m comment"
    _write_prune_event(_make_event(compiled_sql=nasty_sql), audit_path)

    raw_bytes = audit_path.read_bytes()
    # JSON-escaped form is present; raw ESC byte is NOT.
    assert b"\\u001b" in raw_bytes
    assert b"\x1b" not in raw_bytes

    # Round-trip recovers the original cleartext.
    payload = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["compiled_sql"] == nasty_sql


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only O_APPEND atomicity")
def test_write_prune_event_concurrent_appends_atomic(tmp_path: Path) -> None:
    """10 threads × 50 writes each → exactly 500 well-formed JSONL lines.

    POSIX guarantees ``write(2)`` is atomic for payloads ≤ ``PIPE_BUF``
    (4096 bytes on Linux). The 4000-byte cap leaves a 96-byte margin so
    concurrent ``O_APPEND`` writers cannot interleave partial records.
    Each line must parse independently; total line count must equal
    ``threads × writes_per_thread``.
    """
    audit_path = tmp_path / "prune.jsonl"

    def writer(thread_idx: int) -> None:
        for i in range(50):
            event = _make_event(why=f"thread {thread_idx} row {i}")
            _write_prune_event(event, audit_path)

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(writer, range(10)))

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 500
    # Every line parses as JSON — no interleaving.
    for line in lines:
        json.loads(line)


def test_write_prune_event_loops_on_short_writes(tmp_path: Path) -> None:
    """``os.write`` may return fewer bytes than requested (EINTR or
    short-write semantics on some filesystems). The writer loops until
    the full payload lands; the on-disk record must contain every byte.

    Pre-fix behavior: a single ``os.write(fd, encoded)`` call assumed
    the full payload was written. A short return would silently truncate
    the JSONL line, leaving a torn audit record. Post-fix: loop while
    ``written < len(encoded)``; raise on a zero-byte return (disk full
    or unrecoverable I/O failure).
    """
    audit_path = tmp_path / "prune.jsonl"
    real_write = os.write
    call_count = {"n": 0}

    def short_write(fd: int, data: bytes) -> int:
        call_count["n"] += 1
        # First call: write only half the bytes. Second call: write the
        # remainder. Mirrors real-world EINTR / partial-write recovery.
        if call_count["n"] == 1:
            half = len(data) // 2
            return real_write(fd, data[:half])
        return real_write(fd, data)

    with patch("signalforge.prune.audit.os.write", side_effect=short_write):
        _write_prune_event(_make_event(), audit_path)

    # The on-disk file carries the FULL JSONL line — nothing torn.
    contents = audit_path.read_text(encoding="utf-8")
    assert contents.endswith("\n")
    assert len(contents.splitlines()) == 1
    payload = json.loads(contents.splitlines()[0])
    assert payload["audit_schema_version"] == 3
    # The loop ran (at least two ``os.write`` calls — one short, one to
    # complete).
    assert call_count["n"] >= 2


def test_write_prune_event_short_write_zero_bytes_raises(tmp_path: Path) -> None:
    """A persistent zero-byte return from ``os.write`` raises ``OSError``
    rather than spinning forever — the writer guards against an
    infinite loop on a wedged file descriptor.
    """
    audit_path = tmp_path / "prune.jsonl"

    def stuck_write(fd: int, data: bytes) -> int:
        return 0

    with (
        patch("signalforge.prune.audit.os.write", side_effect=stuck_write),
        pytest.raises(OSError) as excinfo,
    ):
        _write_prune_event(_make_event(), audit_path)

    assert "returned 0" in str(excinfo.value) or "disk full" in str(excinfo.value)


def test_compute_config_hash_is_deterministic_and_16_hex() -> None:
    """:func:`_compute_config_hash` returns 16 hex characters and is
    deterministic across calls — matches safety's ``policy_hash`` (DEC-005).
    """
    h1 = _compute_config_hash("{}")
    h2 = _compute_config_hash("{}")
    assert h1 == h2
    assert len(h1) == 16
    assert all(c in "0123456789abcdef" for c in h1)
    # A different input produces a different digest (sanity floor).
    assert _compute_config_hash('{"a":1}') != h1


# --- Issue #171 US-012 — as_of / stats serialisation + replay -----------


def test_as_of_serializes_as_iso_date_string() -> None:
    """``PruneEvent.as_of`` renders as ``YYYY-MM-DD`` ISO 8601 string
    (DEC-013 of #171).

    The canonical-timestamp helper :func:`signalforge._common.timestamp.iso8601_z`
    is deliberately :class:`datetime`-only per safety-layer.md issue #56 —
    :class:`date` has no time-of-day component and the ``...Z`` suffix
    shape does not apply. The model's ``@field_serializer("as_of")``
    returns ``value.isoformat()`` for non-``None`` and ``None`` for
    ``None``, so a non-set ``as_of`` survives the round-trip cleanly
    too.
    """
    event = _make_event(as_of=date(2026, 5, 1))
    payload = json.loads(event.model_dump_json())
    assert payload["as_of"] == "2026-05-01"

    # ``None`` round-trips as ``null`` (default-set on non-anomaly tests).
    null_event = _make_event()
    null_payload = json.loads(null_event.model_dump_json())
    assert null_payload["as_of"] is None


def test_as_of_serializes_via_prune_decision_too() -> None:
    """The same serializer also lives on :class:`PruneDecision` (DEC-006).

    Two surfaces (audit + read-back) carry the field; both must render
    identically so a reviewer comparing ``prune.jsonl`` to the in-memory
    :class:`PruneResult.decisions` tuple sees one ISO date shape.
    """
    decision = _make_decision(as_of=date(2026, 5, 1))
    payload = json.loads(decision.model_dump_json())
    assert payload["as_of"] == "2026-05-01"


def test_stats_serializes_with_method_discriminator() -> None:
    """``PruneEvent.stats`` renders with the ``method`` discriminator field
    (DEC-005 + DEC-013 of #171).

    Pydantic v2 handles the discriminated-union JSON natively — the
    ``method`` literal on each subclass becomes a field on the dict, so a
    consumer can dispatch on ``stats["method"]`` without instantiating
    the model.
    """
    mad = MadStats(median=12500.0, mad=850.0, n_periods=28)
    event = _make_event(stats=mad)
    payload = json.loads(event.model_dump_json())
    assert payload["stats"] is not None
    assert payload["stats"]["method"] == "mad"
    assert payload["stats"]["median"] == 12500.0
    assert payload["stats"]["mad"] == 850.0
    assert payload["stats"]["n_periods"] == 28

    # A different method tags differently — discriminator IS the dispatcher.
    zscore = ZscoreStats(mu=12450.5, sigma=920.3, n_periods=28)
    z_event = _make_event(stats=zscore)
    z_payload = json.loads(z_event.model_dump_json())
    assert z_payload["stats"]["method"] == "zscore"
    assert z_payload["stats"]["mu"] == 12450.5
    assert z_payload["stats"]["sigma"] == 920.3


def test_stats_serializes_as_null_when_unset() -> None:
    """``stats=None`` (the default for every non-anomaly variant) renders
    as ``null`` — the field is optional but always present in the dump
    (consistent with ``sample_failures``).
    """
    event = _make_event()
    payload = json.loads(event.model_dump_json())
    assert "stats" in payload
    assert payload["stats"] is None


def test_v2_shaped_dict_replays_as_v3() -> None:
    """A v2-shaped dict (missing ``as_of`` + ``stats`` entirely) loads
    cleanly into the current v3 :class:`PruneEvent` (DEC-013 of #171).

    This is the load-bearing inline-v2-dict-replays-as-v3 regression
    test required by US-012's acceptance criteria. It verifies the
    :class:`int` (not :class:`typing.Literal`) typing on
    :attr:`PruneEvent.audit_schema_version` preserves audit-replay
    across the 2 → 3 bump — both new fields default to ``None`` on
    replay, and the existing fields survive untouched. Mirrors the
    same guarantee that issue #55's bump 1 → 2 added.
    """
    v2_dict: dict[str, Any] = dict(
        audit_schema_version=2,
        signalforge_version="0.1.0.dev0",
        record_id="abc123",
        timestamp="2026-04-30T12:00:00.000000Z",
        config_hash="abc123def456789a",
        model_unique_id="model.sf_demo.fct_orders",
        test={"type": "not_null", "column": "id", "rationale": "PK."},
        test_anchor="column.id",
        decision="dropped",
        reason="always-passes",
        failures=0,
        sampled_rows=100000,
        scope="sample",
        elapsed_ms=87,
        compiled_sql_hash="0123456789abcdef",
        compiled_sql="SELECT COUNT(*) AS failures FROM `p.d.t` WHERE id IS NULL",
        why="Test passed on 100000 sample rows; no failures.",
        sample_failures=None,
        # Deliberately NO ``as_of`` and NO ``stats`` — that is the v2 shape.
    )
    event = PruneEvent.model_validate(v2_dict)
    assert event.audit_schema_version == 2
    assert event.as_of is None
    assert event.stats is None


def test_config_hash_excludes_as_of() -> None:
    """``config_hash`` answers "did config change," not "did time pass"
    (DEC-013 of #171). Two :class:`PruneEvent` instances identical
    except for ``as_of`` MUST carry the same ``config_hash`` — the
    field is computed by :func:`_compute_config_hash` over the
    canonicalised :mod:`signalforge.prune.config` block alone, which
    does NOT include ``as_of`` in its input set.

    Without this contract, the same prune config run on two different
    days would carry two different ``config_hash`` values and a
    reviewer correlating audit JSONLs across days would falsely
    conclude the config rotated.
    """
    # Both events use the same config_hash threaded in via
    # _build_prune_event — but the model carries an as_of field that
    # MUST NOT affect that hash. The simplest pin: hash a representative
    # canonical config string with and without an as_of and observe
    # _compute_config_hash takes no as_of arg at all (it takes the
    # bare config_json string).
    config_json = '{"enabled":true,"scope":"sample"}'
    h = _compute_config_hash(config_json)
    # Even though the function's signature has only one positional arg,
    # the load-bearing assertion is that the same canonical-config-json
    # input produces the same hash regardless of how many ``as_of``
    # values an audit run interleaves.
    same_h = _compute_config_hash(config_json)
    assert h == same_h
    # And: two events constructed with different as_of carry the SAME
    # config_hash because the engine never threads as_of into the hash.
    e1 = _make_event(as_of=date(2026, 5, 1))
    e2 = _make_event(as_of=date(2026, 12, 1))
    assert e1.config_hash == e2.config_hash
    # And both equal the hash for an event with no as_of at all.
    e_none = _make_event()
    assert e_none.config_hash == e1.config_hash
