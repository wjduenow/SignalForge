"""Connection lifecycle + fail-soft cleanup for SnowflakeAdapter (#122 US-002).

Pins the four #122 Phase-3 decisions this story implements:

* DEC-001 — injectable ``connection=`` seam + lazy build; ``_get_connection``
  returns the injected fake without building a real connection and records it
  as ``_active_session``.
* DEC-002 — connection-bound session state (the connection IS the session).
* DEC-003 — fail-soft ``__exit__`` cleanup: closes the connection (reaping
  session-scoped temp tables), swallows failure, resets state; success → INFO
  with a HASHED session id; failure → ONE operator-actionable WARNING naming
  the RAW session id + the temp-table auto-reap remediation (DEC-014 narrow
  exception).
* DEC-010 regression — ``__repr__`` still shows only ``account`` + ``warehouse``
  (#119 credential redaction).

Uses the hand-rolled :class:`FakeSnowflakeConnection` (NO MagicMock —
``testing-signal.md``).
"""

from __future__ import annotations

import logging

import pytest

from signalforge.warehouse._sample_id import _hash_session_id
from signalforge.warehouse.adapters.snowflake import SnowflakeAdapter

from ._fake_snowflake import FakeSnowflakeConnection

# ---------------------------------------------------------------------------
# DEC-001 — injectable connection seam + lazy build
# ---------------------------------------------------------------------------


def test_get_connection_returns_injected_fake_without_building_real() -> None:
    """``connection=`` injects the fake; :meth:`_get_connection` returns it by
    identity and never builds a real connection (DEC-001)."""
    conn = FakeSnowflakeConnection()
    adapter = SnowflakeAdapter(connection=conn)

    assert adapter._get_connection() is conn


def test_get_connection_records_active_session_on_first_open() -> None:
    """First :meth:`_get_connection` records the connection as
    ``_active_session`` so the ``__exit__`` cleanup boundary has a session to
    tear down (DEC-002)."""
    conn = FakeSnowflakeConnection()
    adapter = SnowflakeAdapter(connection=conn)
    assert adapter._active_session is None

    adapter._get_connection()

    assert adapter._active_session is conn


# ---------------------------------------------------------------------------
# DEC-003 (#119 regression) — __repr__ credential redaction still holds
# ---------------------------------------------------------------------------


def test_repr_still_shows_only_account_and_warehouse() -> None:
    """Adding the connection seam must not leak credentials through
    ``__repr__`` (regression of #119 DEC-003)."""
    adapter = SnowflakeAdapter(
        connection=FakeSnowflakeConnection(),
        account="ac123",
        user="bob",
        password="s3cret",
        role="r",
        warehouse="WH",
        database="db",
        schema="sch",
    )
    rendered = repr(adapter)

    assert "ac123" in rendered
    assert "WH" in rendered
    assert "s3cret" not in rendered
    assert "bob" not in rendered
    assert "password" not in rendered
    assert "database" not in rendered
    assert "schema" not in rendered
    # The fake connection object must not leak into the repr either.
    assert "FakeSnowflakeConnection" not in rendered


# ---------------------------------------------------------------------------
# DEC-003 — __exit__ fail-soft cleanup: happy path
# ---------------------------------------------------------------------------


def test_exit_closes_connection_once_and_resets_session_state() -> None:
    """
    Ensure adapter context exit closes the active connection once and resets session state.
    
    Asserts that exiting the adapter context calls the injected connection's `close()` exactly once, sets `adapter._active_session` to `None`, and preserves the injected `adapter._connection` reference (does not null it).
    """
    conn = FakeSnowflakeConnection()
    adapter = SnowflakeAdapter(connection=conn)

    with adapter:
        adapter._get_connection()  # open the session

    assert conn.close_call_count == 1
    assert adapter._active_session is None
    # The injected connection reference is retained (not discarded on cleanup).
    assert adapter._connection is conn


def test_second_exit_is_a_no_op() -> None:
    """A SECOND ``__exit__`` (or ``_cleanup_active_session``) is a no-op —
    ``close`` is not called again because state already reset (DEC-003
    idempotence)."""
    conn = FakeSnowflakeConnection()
    adapter = SnowflakeAdapter(connection=conn)
    adapter._get_connection()

    adapter._cleanup_active_session()
    assert conn.close_call_count == 1

    adapter._cleanup_active_session()
    assert conn.close_call_count == 1  # not called again


def test_success_path_info_uses_hashed_session_id_not_raw(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The happy-path INFO log uses the HASHED session id; the RAW session id
    string never appears in the INFO record (DEC-003 redaction)."""
    raw_session_id = "raw-session-abc-123"
    conn = FakeSnowflakeConnection(session_id=raw_session_id)
    adapter = SnowflakeAdapter(connection=conn)
    adapter._get_connection()

    with caplog.at_level(logging.INFO, logger="signalforge.warehouse"):
        adapter._cleanup_active_session()

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    message = info_records[0].getMessage()
    assert _hash_session_id(raw_session_id) in message
    assert raw_session_id not in message


# ---------------------------------------------------------------------------
# DEC-003 / DEC-014 — __exit__ fail-soft cleanup: failure path
# ---------------------------------------------------------------------------


def test_cleanup_failure_warns_with_raw_session_id_and_resets_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``close()`` raises, the failure is SWALLOWED, exactly one WARNING
    fires naming the RAW session id (DEC-014 narrow exception) + the
    temp-table-auto-reap remediation + the reason, and state still resets
    (DEC-003)."""
    raw_session_id = "raw-session-xyz-999"
    conn = FakeSnowflakeConnection(
        session_id=raw_session_id,
        close_raises=RuntimeError("connection reset by peer"),
    )
    adapter = SnowflakeAdapter(connection=conn)
    adapter._get_connection()

    with caplog.at_level(logging.WARNING, logger="signalforge.warehouse"):
        # Must NOT propagate — cleanup-boundary fail-soft.
        adapter._cleanup_active_session()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    record = warnings[0]
    # Asserted at WARNING level (the --quiet-non-suppression contract is about
    # level, not text).
    assert record.levelno == logging.WARNING

    message = record.getMessage()
    assert raw_session_id in message
    # Temp-table auto-reap remediation: the durable fallback.
    assert "reaps the idle session" in message
    # No manual drop command is offered (a temp table is unreachable outside
    # its owning session).
    assert "No manual drop command" in message
    # The reason carries the exception class name.
    assert "RuntimeError" in message

    # Session state still reset despite the failure; the connection reference
    # is retained (not nulled on cleanup — see the close-once test).
    assert adapter._active_session is None
    assert adapter._connection is conn


def test_cleanup_failure_does_not_raise() -> None:
    """The cleanup-boundary swallows the close failure entirely — exiting the
    ``with`` block raises nothing (DEC-003 fail-soft)."""
    conn = FakeSnowflakeConnection(close_raises=OSError("boom"))
    adapter = SnowflakeAdapter(connection=conn)

    with adapter:
        adapter._get_connection()
    # No exception propagated out of the ``with`` block.
