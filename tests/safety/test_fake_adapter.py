"""Self-tests for ``tests/safety/_fake_adapter.py``'s :class:`FakeAdapter`.

Mirrors ``tests/warehouse/test_fake.py`` for the warehouse fake: regressions
in the test fake itself must not masquerade as bugs in the safety-layer
aggregator. Each test is capable of failing on a real regression
(``testing-signal.md`` — no ``assert True``-shaped placeholders).
"""

from __future__ import annotations

import pytest

from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.models import ColumnStats, TableRef
from tests.safety._fake_adapter import FakeAdapter

pytestmark = pytest.mark.safety


def _make_table_ref(name: str = "tbl") -> TableRef:
    return TableRef(project="my-project", dataset="ds", name=name)


def _make_stats() -> ColumnStats:
    return ColumnStats(count=10, distinct=8, nulls=0, data_type="INT64")


def test_fake_adapter_satisfies_warehouse_adapter_abc() -> None:
    """Construction succeeds + ``isinstance`` check passes — proves every
    ``@abc.abstractmethod`` on ``WarehouseAdapter`` is implemented."""
    fake = FakeAdapter()
    assert isinstance(fake, WarehouseAdapter)


def test_fake_adapter_unexpected_column_stats_raises() -> None:
    fake = FakeAdapter()
    with fake, pytest.raises(AssertionError, match="unexpected column_stats"):
        fake.column_stats(_make_table_ref(), "col1")


def test_fake_adapter_column_stats_mismatch_raises() -> None:
    fake = FakeAdapter()
    table = _make_table_ref()
    fake.expect_column_stats(table=table, column="col1", returns=_make_stats())
    with fake, pytest.raises(AssertionError, match="column_stats mismatch"):
        fake.column_stats(table, "col2")


def test_fake_adapter_assert_all_expectations_met_succeeds_when_empty() -> None:
    fake = FakeAdapter()
    # Should not raise.
    fake.assert_all_expectations_met()


def test_fake_adapter_assert_all_expectations_met_raises_on_unmet() -> None:
    fake = FakeAdapter()
    fake.expect_column_stats(table=_make_table_ref(), column="c", returns=_make_stats())
    with pytest.raises(AssertionError, match="unmet expectations"):
        fake.assert_all_expectations_met()


def test_fake_adapter_outside_context_column_stats_raises_assertion() -> None:
    """DEC-025: column_stats must be called inside an active ``with``."""
    fake = FakeAdapter()
    fake.expect_column_stats(table=_make_table_ref(), column="c", returns=_make_stats())
    with pytest.raises(AssertionError, match="outside"):
        fake.column_stats(_make_table_ref(), "c")


def test_fake_adapter_returns_exception_raises_it() -> None:
    fake = FakeAdapter()
    fake.expect_column_stats(table=_make_table_ref(), column="c", returns=ValueError("boom"))
    with fake, pytest.raises(ValueError, match="boom"):
        fake.column_stats(_make_table_ref(), "c")


def test_fake_adapter_sample_rows_basic() -> None:
    fake = FakeAdapter()
    table = _make_table_ref()
    fake.expect_sample_rows(table=table, n=5, returns=[{"x": 1}])
    rows = fake.sample_rows(table, 5)
    assert rows == [{"x": 1}]
    fake.assert_all_expectations_met()
