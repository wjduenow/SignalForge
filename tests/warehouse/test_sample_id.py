"""US-001 (#122) — shared cross-adapter deterministic sample-id seam.

Covers DEC-008 (helper relocation to :mod:`signalforge.warehouse._sample_id`),
DEC-009 (``map_snowflake_exception`` minimal taxonomy), and DEC-010
(``_SnowflakeCursorProtocol.description``). The relocation is a pure move — the
``run_id`` recipe bytes are unchanged — so determinism here plus the existing
``tests/warehouse/test_materialise_sample.py`` snapshots together pin
byte-parity.
"""

from __future__ import annotations

from datetime import date

from signalforge.warehouse._sample_id import (
    _canonical_partition_filter,
    _compute_run_id,
    _hash_session_id,
)
from signalforge.warehouse.adapters._snowflake_client import (
    _SnowflakeCursorProtocol,
    map_snowflake_exception,
)
from signalforge.warehouse.errors import QuerySyntaxError, WarehouseAuthError
from signalforge.warehouse.models import PartitionFilter, TableRef

_TABLE = TableRef(project="fake_project", dataset="ds", name="orders")


# ---------------------------------------------------------------------------
# DEC-008 — relocated helpers stay deterministic and are the SAME objects
# the BigQuery adapter imports (no duplicate local copy).
# ---------------------------------------------------------------------------


def test_compute_run_id_is_deterministic() -> None:
    """Same ``(table, n, partition_filter)`` → byte-equal 16-hex run_id."""
    a = _compute_run_id(table=_TABLE, n=100, partition_filter=None)
    b = _compute_run_id(table=_TABLE, n=100, partition_filter=None)
    assert a == b
    assert len(a) == 16
    assert all(c in "0123456789abcdef" for c in a)


def test_compute_run_id_varies_with_inputs() -> None:
    """Distinct ``n`` / partition_filter produce distinct run_ids."""
    base = _compute_run_id(table=_TABLE, n=100, partition_filter=None)
    assert base != _compute_run_id(table=_TABLE, n=200, partition_filter=None)
    pf = PartitionFilter(column="dt", op=">=", value=date(2024, 1, 1))
    assert base != _compute_run_id(table=_TABLE, n=100, partition_filter=pf)


def test_canonical_partition_filter_is_stable() -> None:
    """``None`` → ``"null"``; a filter renders canonical sorted JSON with
    ``isoformat()`` dates so two callers building the same filter agree."""
    assert _canonical_partition_filter(None) == "null"
    pf = PartitionFilter(column="dt", op=">=", value=date(2024, 1, 1))
    rendered = _canonical_partition_filter(pf)
    assert rendered == '{"column":"dt","op":">=","value":"2024-01-01"}'
    # Same logical filter → byte-equal canonical text.
    assert rendered == _canonical_partition_filter(
        PartitionFilter(column="dt", op=">=", value=date(2024, 1, 1))
    )


def test_hash_session_id_is_eight_hex() -> None:
    h = _hash_session_id("session-abc")
    assert len(h) == 8
    assert all(c in "0123456789abcdef" for c in h)
    assert h == _hash_session_id("session-abc")
    assert h != _hash_session_id("session-xyz")


def test_bigquery_imports_relocated_helpers_no_duplicate_copy() -> None:
    """The BigQuery adapter must IMPORT the helpers from ``_sample_id`` —
    not carry its own local copy. Identity equality proves there is exactly
    one definition shared across adapters (DEC-008)."""
    from signalforge.warehouse import _sample_id
    from signalforge.warehouse.adapters import bigquery

    assert bigquery._compute_run_id is _sample_id._compute_run_id
    assert bigquery._hash_session_id is _sample_id._hash_session_id


# ---------------------------------------------------------------------------
# DEC-010 — cursor protocol carries ``description`` and stays
# runtime_checkable-satisfied.
# ---------------------------------------------------------------------------


class _StandInCursor:
    """Minimal object exposing the DB-API cursor surface the protocol names."""

    description = None

    def execute(self, command: str, *args: object, **kwargs: object) -> object:
        """
        Execute a SQL command on the cursor.
        
        Parameters:
            command (str): SQL statement or command to execute.
            *args: Positional parameters for the command.
            **kwargs: Keyword parameters for the command.
        
        Returns:
            None: This implementation does not return a result.
        """
        return None

    def fetchall(self) -> object:
        """
        Return all rows from the last executed query.
        
        Returns:
            list: The fetched result rows; this implementation always returns an empty list.
        """
        return []

    def close(self) -> None:
        """
        Close the cursor and release any associated resources.
        """
        return None


def test_cursor_protocol_satisfied_by_object_with_description() -> None:
    cursor = _StandInCursor()
    assert isinstance(cursor, _SnowflakeCursorProtocol)
    # The runtime_checkable protocol keys on member presence; an object
    # missing ``description`` (and the rest) is NOT an instance.
    assert hasattr(cursor, "description")


# ---------------------------------------------------------------------------
# DEC-009 — map_snowflake_exception minimal taxonomy.
# ---------------------------------------------------------------------------


def test_map_snowflake_programming_error_to_query_syntax_error() -> None:
    """
    Verify that a Snowflake ProgrammingError is mapped to a QuerySyntaxError and that provided context is included in the mapped error detail.
    
    Asserts that map_snowflake_exception converts a snowflake.connector.errors.ProgrammingError indicative of a SQL syntax issue into a QuerySyntaxError and that the supplied context (e.g., "table") appears in the mapped error's detail.
    """
    from snowflake.connector import errors as sfe

    exc = sfe.ProgrammingError(msg="001003: SQL compilation error: syntax error", errno=1003)
    mapped = map_snowflake_exception(exc, context={"table": "fake_project.ds.orders"})
    assert isinstance(mapped, QuerySyntaxError)
    # ``context["table"]`` enriches the detail (mirrors map_bq_exception).
    assert "fake_project.ds.orders" in mapped.detail


def test_map_snowflake_auth_error_to_warehouse_auth_error() -> None:
    from snowflake.connector import errors as sfe

    exc = sfe.DatabaseError(
        msg="250001: Incorrect username or password was specified.", errno=250001
    )
    mapped = map_snowflake_exception(exc)
    assert isinstance(mapped, WarehouseAuthError)


def test_map_snowflake_forbidden_error_to_warehouse_auth_error() -> None:
    from snowflake.connector import errors as sfe

    exc = sfe.ForbiddenError(msg="403: access denied", errno=250001)
    mapped = map_snowflake_exception(exc)
    assert isinstance(mapped, WarehouseAuthError)


def test_map_snowflake_arbitrary_exception_returned_unchanged() -> None:
    exc = ValueError("not a snowflake error")
    mapped = map_snowflake_exception(exc)
    assert mapped is exc


def test_map_snowflake_generic_operational_error_passthrough() -> None:
    """A non-auth operational blip (timeout / network) has no auth marker, so
    it falls through to the unchanged-passthrough arm — the caller re-raises
    the original rather than mis-labelling it as auth."""
    from snowflake.connector import errors as sfe

    exc = sfe.OperationalError(msg="390114: request timed out", errno=390114)
    mapped = map_snowflake_exception(exc)
    assert mapped is exc
