"""Cursor-handle release tests for :class:`SnowflakeAdapter` (#258 US-001).

Traces to DEC-007 of ``plans/super/258-snowflake-column-stats.md``. Before this
fix only :meth:`SnowflakeAdapter._execute_scalar` closed its cursor in a
``try/finally``; :meth:`_execute`, :meth:`_execute_to_dicts`, and
:meth:`run_test_sql` opened a cursor on the long-lived connection and never
released it — leaking server-side cursor handles across repeated queries. This
mirrors the Databricks PR #257 fix (``tests/warehouse/test_databricks_adapter.py``
§ "Cursor-handle release").

Each test drives one of the three methods and asserts the handed-out cursor's
``closed`` is ``True`` afterwards — on the success path AND the failure path
(``expect_execute(returns=<Exception>)``), proving the ``finally`` arm fires.

Uses :class:`FakeSnowflakeConnection` (``tests/warehouse/_fake_snowflake.py``),
which tracks every cursor it vends via :attr:`cursors` and exposes ``closed``
per cursor — never a ``MagicMock`` (``testing-signal.md``).
"""

from __future__ import annotations

import pytest

from signalforge.warehouse import SnowflakeAdapter
from signalforge.warehouse.models import TableRef
from tests.warehouse._fake_snowflake import FakeSnowflakeConnection

# ``project`` follows :class:`TableRef`'s GCP-style project-id grammar
# (lowercase start, >= 6 chars); Snowflake quotes it verbatim per-component.
_TABLE = TableRef(project="mydatabase", dataset="SCH", name="ORDERS")

_SIZE_QUERY = r"INFORMATION_SCHEMA\.TABLES"
_SAMPLE_QUERY = r"_sf_sample_hash"
_COUNT_QUERY = r"SELECT COUNT\(\*\) AS failures"


def _make_adapter(conn: FakeSnowflakeConnection) -> SnowflakeAdapter:
    return SnowflakeAdapter(connection=conn)


# ---------------------------------------------------------------------------
# _execute — via get_row_count / _get_num_rows (the INFORMATION_SCHEMA lookup)
# ---------------------------------------------------------------------------


def test_execute_closes_cursor_on_success() -> None:
    """``_execute`` releases the cursor after a successful query so repeated
    queries on the long-lived connection don't leak server-side handles."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(10,)])
    adapter = _make_adapter(conn)

    adapter.get_row_count(_TABLE)

    assert conn.cursors, "expected the adapter to open at least one cursor"
    assert all(c.closed for c in conn.cursors)


def test_execute_closes_cursor_on_failure() -> None:
    """The cursor is released even when the query raises (the ``finally`` arm)."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=RuntimeError("boom"))
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError):
        adapter.get_row_count(_TABLE)

    assert conn.cursors and all(c.closed for c in conn.cursors)


# ---------------------------------------------------------------------------
# _execute_to_dicts — via sample_rows (the projection-subquery sample query)
# ---------------------------------------------------------------------------


def test_execute_to_dicts_closes_cursor_on_success() -> None:
    """``sample_rows`` (via ``_execute_to_dicts``) closes the cursor only AFTER
    ``cursor.description`` has been read to shape the rows (outer ``try/finally``
    wrapping the inner exception-mapping ``try/except``)."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(
        matching=_SAMPLE_QUERY,
        returns=[(1, 10)],
        description=[("ID",), ("AMOUNT",)],
    )
    adapter = _make_adapter(conn)

    rows = adapter.sample_rows(_TABLE, 100)

    assert rows == [{"ID": 1, "AMOUNT": 10}]
    assert conn.cursors and all(c.closed for c in conn.cursors)


def test_execute_to_dicts_closes_cursor_on_failure() -> None:
    """The cursor is released even when the sample query raises."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_SAMPLE_QUERY, returns=RuntimeError("boom"))
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError):
        adapter.sample_rows(_TABLE, 100)

    assert conn.cursors and all(c.closed for c in conn.cursors)


# ---------------------------------------------------------------------------
# run_test_sql — the COUNT(*) failing-rows wrap
# ---------------------------------------------------------------------------


def test_run_test_sql_closes_cursor_on_success() -> None:
    """``run_test_sql`` releases its cursor after reading ``description`` and
    the result rows (the ``finally`` arm added by #258 US-001)."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(0,)], description=[("FAILURES",)])
    adapter = _make_adapter(conn)

    adapter.run_test_sql('SELECT "ID" FROM "DB"."SCH"."T" WHERE "ID" IS NULL')

    assert conn.cursors and all(c.closed for c in conn.cursors)


def test_run_test_sql_closes_cursor_on_failure() -> None:
    """The cursor is released even when the wrapped query raises."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=RuntimeError("boom"))
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError):
        adapter.run_test_sql('SELECT "ID" FROM "DB"."SCH"."T" WHERE "ID" IS NULL')

    assert conn.cursors and all(c.closed for c in conn.cursors)
