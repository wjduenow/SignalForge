"""Hand-rolled fake for a ``snowflake.connector`` connection (US-002, #122).

Mirrors the structure / quality bar of ``tests/warehouse/_fake.py`` (the
BigQuery fake) — explicit expectations, NO ``MagicMock`` (a MagicMock-style
fake auto-passes everything, violating ``testing-signal.md``).

The fake satisfies the duck-typed protocols the (future) SnowflakeAdapter
consumes:

* :class:`FakeSnowflakeConnection` satisfies
  :class:`signalforge.warehouse.adapters._snowflake_client._SnowflakeClientProtocol`
  (``cursor()`` / ``close()``).
* :class:`_FakeSnowflakeCursor` satisfies
  :class:`signalforge.warehouse.adapters._snowflake_client._SnowflakeCursorProtocol`
  (``execute()`` / ``fetchall()`` / ``close()`` / ``description``).

Tests register query round-trips via
:meth:`FakeSnowflakeConnection.expect_execute`; each ``cursor().execute(sql)``
consumes one matching expectation. Calls outside the canned set raise
``AssertionError("unexpected query: ...")`` (mirrors the BigQuery fake's loud
posture). Connection cleanup is observable via :attr:`close_call_count`, and a
``close_raises`` kwarg drives the :meth:`SnowflakeAdapter._cleanup_active_session`
swallow-and-warn path.

Lives in tests/warehouse/ (not the package proper) — never imported by
production code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class _ExecuteExpectation:
    """One queued ``cursor().execute(sql)`` round-trip.

    ``matching`` is a compiled regex matched via :meth:`re.Pattern.search`
    against the executed SQL. ``returns`` is either a list of rows (stashed for
    the next :meth:`_FakeSnowflakeCursor.fetchall`) or an :class:`Exception`
    instance (raised when the expectation is consumed). ``description`` is the
    optional DB-API column-descriptor sequence exposed on the cursor's
    ``description`` property after the matching ``execute``.
    """

    matching: re.Pattern[str]
    returns: list[Any] | Exception
    description: list[Any] | None = None


class _FakeSnowflakeCursor:
    """Stand-in for a ``snowflake.connector`` cursor.

    Bound to its parent :class:`FakeSnowflakeConnection` so a single
    expectation queue is shared across every cursor the connection vends —
    each ``execute`` consumes one matching entry from the connection's queue.
    """

    def __init__(self, connection: FakeSnowflakeConnection) -> None:
        self._connection = connection
        self._fetch_rows: list[Any] = []
        self._description: list[Any] | None = None
        self._closed = False

    @property
    def description(self) -> list[Any] | None:
        return self._description

    def execute(self, command: str, *args: Any, **kwargs: Any) -> _FakeSnowflakeCursor:
        rows, description = self._connection._consume_execute(command)
        self._fetch_rows = rows
        self._description = description
        return self

    def fetchall(self) -> list[Any]:
        return self._fetch_rows

    def close(self) -> None:
        self._closed = True


class FakeSnowflakeConnection:
    """Explicit fake connection; calls outside expectations raise loudly.

    Args:
        session_id: Opaque session-id string the adapter reads via
            ``conn.session_id`` for the cleanup log (the real connector exposes
            a session id on the connection). Defaults to a fixed fake id.
        close_raises: When supplied, :meth:`close` raises this exception — this
            drives :meth:`SnowflakeAdapter._cleanup_active_session`'s
            swallow-and-warn path. Defaults to ``None`` (clean close).
    """

    def __init__(
        self,
        *,
        session_id: str = "fake-session-0001",
        close_raises: Exception | None = None,
    ) -> None:
        self.session_id = session_id
        self._close_raises = close_raises
        self.close_call_count = 0
        self._execute_expectations: list[_ExecuteExpectation] = []

    # ---- expectation API --------------------------------------------------

    def expect_execute(
        self,
        *,
        matching: re.Pattern[str] | str,
        returns: list[Any] | Exception,
        description: list[Any] | None = None,
    ) -> None:
        """Queue one expected ``cursor().execute(sql)`` round-trip.

        Args:
            matching: A regex string or compiled pattern; matched via
                :meth:`re.Pattern.search` against the executed SQL.
            returns: A list of rows (stashed for the next ``fetchall()``) or an
                :class:`Exception` instance (raised on consumption).
            description: Optional DB-API column-descriptor sequence exposed on
                the cursor's ``description`` after the matching ``execute``.
        """
        pattern = matching if isinstance(matching, re.Pattern) else re.compile(matching)
        self._execute_expectations.append(
            _ExecuteExpectation(matching=pattern, returns=returns, description=description)
        )

    def assert_all_expectations_met(self) -> None:
        if self._execute_expectations:
            raise AssertionError(
                f"Unconsumed expectations: {len(self._execute_expectations)} execute expectations"
            )

    # ---- snowflake.connector connection surface ---------------------------

    def cursor(self) -> _FakeSnowflakeCursor:
        return _FakeSnowflakeCursor(self)

    def close(self) -> None:
        self.close_call_count += 1
        if self._close_raises is not None:
            raise self._close_raises

    # ---- internal ---------------------------------------------------------

    def _consume_execute(self, sql: str) -> tuple[list[Any], list[Any] | None]:
        """Walk the expectation queue for a match against ``sql``; consume one
        matching entry. Returns ``(rows, description)`` on success; raises the
        registered exception when ``returns`` is an :class:`Exception`.
        """
        for i, exp in enumerate(self._execute_expectations):
            if exp.matching.search(sql):
                self._execute_expectations.pop(i)
                if isinstance(exp.returns, Exception):
                    raise exp.returns
                return exp.returns, exp.description
        raise AssertionError(f"unexpected query: {sql!r}")
