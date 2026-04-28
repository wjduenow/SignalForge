"""Smoke tests for BigQueryAdapter (US-008).

Comprehensive unit tests for the adapter live in US-009. This file holds
a *handful* of fast checks that pyright can't catch — exact SQL-token
rendering and the RuntimeError guard for ``column_stats`` outside a
context manager.

Each test is capable of failing on a real regression
(``testing-signal.md``): they pin specific SQL substrings and the
RuntimeError surface that the prune layer will key on.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from signalforge.warehouse.adapters.bigquery import BigQueryAdapter
from signalforge.warehouse.models import PartitionFilter, TableRef


def _make_adapter() -> BigQueryAdapter:
    """Construct an adapter without touching the real BigQuery SDK.

    The ``client`` kwarg is set to a tiny stand-in that only carries the
    ``project`` attribute the adapter needs for ``_quote`` resolution.
    """

    class _MiniClient:
        project = "default-billing-project"

        def query(self, sql: str, job_config: object = None) -> object:  # pragma: no cover
            raise AssertionError("smoke test should not issue queries")

        def get_table(self, ref: object) -> object:  # pragma: no cover
            raise AssertionError("smoke test should not call get_table")

        def list_rows(
            self, ref: object, max_results: int | None = None
        ) -> object:  # pragma: no cover
            raise AssertionError("smoke test should not call list_rows")

    return BigQueryAdapter(client=_MiniClient())


def test_quote_uses_explicit_project() -> None:
    """``TableRef.project`` set explicitly should appear verbatim in the
    backtick-quoted identifier (DEC-013, DEC-027)."""
    adapter = _make_adapter()
    ref = TableRef(project="myproj", dataset="ds", name="tbl")

    assert adapter._quote(ref) == "`myproj.ds.tbl`"


def test_quote_falls_back_to_client_project_when_none() -> None:
    """``TableRef.project=None`` should resolve to the underlying client's
    billing project (DEC-027)."""
    adapter = _make_adapter()
    ref = TableRef(project=None, dataset="ds", name="tbl")

    assert adapter._quote(ref) == "`default-billing-project.ds.tbl`"


def test_render_partition_filter_datetime_emits_timestamp() -> None:
    """``datetime`` partition values should render as ``TIMESTAMP('…')``
    (DEC-014); ``datetime`` is a subclass of ``date`` so order matters in
    the type check."""
    adapter = _make_adapter()
    pf = PartitionFilter(column="ts", op=">=", value=datetime(2025, 1, 2, 3, 4, 5))

    rendered = adapter._render_partition_filter(pf)

    assert "TIMESTAMP(" in rendered
    assert "2025-01-02T03:04:05" in rendered
    assert rendered.startswith("`ts` >= ")


def test_render_partition_filter_date_emits_date() -> None:
    """``date`` partition values should render as ``DATE('…')`` (DEC-014)."""
    adapter = _make_adapter()
    pf = PartitionFilter(column="d", op="=", value=date(2025, 1, 2))

    rendered = adapter._render_partition_filter(pf)

    assert "DATE('2025-01-02')" in rendered
    assert rendered.startswith("`d` = ")


def test_render_partition_filter_str_escapes_single_quotes() -> None:
    """``str`` partition values should be single-quote-escaped (``'``→``''``)
    so adversarial tenant ids cannot smuggle SQL fragments (DEC-014)."""
    adapter = _make_adapter()
    pf = PartitionFilter(column="tenant", op="=", value="o'brien")

    rendered = adapter._render_partition_filter(pf)

    assert rendered == "`tenant` = 'o''brien'"


def test_column_stats_outside_context_raises_runtime_error() -> None:
    """DEC-025: ``column_stats`` must be called inside ``with adapter:``."""
    adapter = _make_adapter()
    ref = TableRef(project="p", dataset="d", name="t")

    with pytest.raises(RuntimeError, match="column_stats must be called inside"):
        adapter.column_stats(ref, "x")


def test_repr_redacts_credentials() -> None:
    """DEC-022: ``__repr__`` carries project + location, never the client."""
    adapter = BigQueryAdapter(project="p", location="US")

    rendered = repr(adapter)

    assert "project='p'" in rendered
    assert "location='US'" in rendered
    # No leak of the client / credentials hooks.
    assert "client" not in rendered.lower()
    assert "credentials" not in rendered.lower()
