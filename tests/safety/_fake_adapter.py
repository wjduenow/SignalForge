"""Hand-rolled fake for :class:`signalforge.warehouse.base.WarehouseAdapter`.

Tests register expectations via ``expect_column_stats`` / ``expect_sample_rows``;
the adapter methods consume one matching expectation per call. Unexpected calls
raise ``AssertionError`` so silent mismatches surface loudly.

Mirrors the ``expect_*`` style of ``tests/warehouse/_fake.py``'s
``FakeBigQueryClient`` — never ``MagicMock``. Lives under ``tests/safety/`` and
is never imported by production code.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.models import (
    ColumnStats,
    Dialect,
    PartitionFilter,
    TableRef,
    TestResult,
)

_FAKE_DIALECT = Dialect(
    name="fake",
    supports_tablesample=False,
    supports_qualify=False,
    quote_char="`",
    identifier_case="preserve",
)


class FakeAdapter(WarehouseAdapter):
    """Explicit fake adapter for the safety-layer aggregate / sample tests.

    Calls to ``column_stats`` / ``sample_rows`` consume the head of the
    matching FIFO queue; the expected ``(table, column)`` (or ``(table, n)``)
    pair must match the call exactly. ``column_stats`` additionally enforces
    the DEC-025 contract that callers must be inside an active context
    manager.

    The fake also records ``enter_count`` / ``exit_count`` so tests can
    verify that aggregator code opens the context exactly once.
    """

    def __init__(self) -> None:
        self._column_stats_queue: deque[tuple[TableRef, str, ColumnStats | BaseException]] = deque()
        self._sample_rows_queue: deque[
            tuple[TableRef, int, list[dict[str, Any]] | BaseException]
        ] = deque()
        self._entered: bool = False
        self.enter_count: int = 0
        self.exit_count: int = 0

    # ---- context-manager surface -----------------------------------------

    def __enter__(self) -> FakeAdapter:
        self._entered = True
        self.enter_count += 1
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._entered = False
        self.exit_count += 1
        return None

    # ---- expectation API --------------------------------------------------

    def expect_column_stats(
        self,
        *,
        table: TableRef,
        column: str,
        returns: ColumnStats | BaseException,
    ) -> None:
        self._column_stats_queue.append((table, column, returns))

    def expect_sample_rows(
        self,
        *,
        table: TableRef,
        n: int,
        returns: list[dict[str, Any]] | BaseException,
    ) -> None:
        self._sample_rows_queue.append((table, n, returns))

    def assert_all_expectations_met(self) -> None:
        unmet: list[str] = []
        if self._column_stats_queue:
            unmet.append(f"column_stats: {list(self._column_stats_queue)!r}")
        if self._sample_rows_queue:
            unmet.append(f"sample_rows: {list(self._sample_rows_queue)!r}")
        if unmet:
            raise AssertionError("unmet expectations: " + "; ".join(unmet))

    # ---- WarehouseAdapter surface ----------------------------------------

    def dialect(self) -> Dialect:
        return _FAKE_DIALECT

    def column_stats(self, table: TableRef, column: str) -> ColumnStats:
        if not self._entered:
            raise AssertionError(
                f"column_stats called outside `with adapter:` context "
                f"(table={table!r}, column={column!r})"
            )
        if not self._column_stats_queue:
            raise AssertionError(
                f"unexpected column_stats call: table={table!r}, column={column!r}"
            )
        expected_table, expected_column, returns = self._column_stats_queue.popleft()
        if (expected_table, expected_column) != (table, column):
            raise AssertionError(
                f"column_stats mismatch: expected "
                f"(table={expected_table!r}, column={expected_column!r}); "
                f"got (table={table!r}, column={column!r})"
            )
        if isinstance(returns, BaseException):
            raise returns
        return returns

    def sample_rows(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
    ) -> list[dict[str, Any]]:
        if not self._sample_rows_queue:
            raise AssertionError(f"unexpected sample_rows call: table={table!r}, n={n}")
        expected_table, expected_n, returns = self._sample_rows_queue.popleft()
        if (expected_table, expected_n) != (table, n):
            raise AssertionError(
                f"sample_rows mismatch: expected "
                f"(table={expected_table!r}, n={expected_n}); "
                f"got (table={table!r}, n={n})"
            )
        if isinstance(returns, BaseException):
            raise returns
        return returns

    def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> TestResult:
        raise AssertionError(
            f"FakeAdapter does not support run_test_sql; got sql={sql!r}, "
            f"capture_failures={capture_failures}"
        )


__all__ = ["FakeAdapter"]
