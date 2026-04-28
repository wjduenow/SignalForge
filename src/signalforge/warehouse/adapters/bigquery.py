"""BigQuery adapter — implementation lands in US-008.

US-006 ships only enough surface for
:meth:`signalforge.warehouse.base.WarehouseAdapter.from_profile` to construct
a :class:`BigQueryAdapter` instance: the constructor stores the three kwargs,
:meth:`__repr__` renders a credential-redacted (DEC-022) summary, and
:meth:`dialect` returns the live :data:`BIGQUERY_DIALECT` constant. Every
other abstract method raises :class:`NotImplementedError` until US-008 wires
in ``google-cloud-bigquery``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from signalforge.warehouse.base import WarehouseAdapter

if TYPE_CHECKING:
    from signalforge.warehouse.models import (
        ColumnStats,
        Dialect,
        PartitionFilter,
        TableRef,
        TestResult,
    )


class BigQueryAdapter(WarehouseAdapter):
    """v0.1 BigQuery implementation (skeleton; full impl in US-008)."""

    def __init__(
        self,
        *,
        project: str | None = None,
        location: str | None = None,
        max_bytes_billed: int = 100_000_000,
    ) -> None:
        self._project = project
        self._location = location
        self._max_bytes_billed = max_bytes_billed

    def __repr__(self) -> str:
        # DEC-022: never include credentials, the underlying bigquery.Client,
        # or any token. Project + location are the only safe fields.
        return f"<BigQueryAdapter project={self._project!r} location={self._location!r}>"

    # ------------------------------------------------------------------
    # Abstract methods — full bodies land in US-008.
    # ------------------------------------------------------------------

    def __enter__(self) -> WarehouseAdapter:
        raise NotImplementedError("BigQueryAdapter implementation pending US-008")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        raise NotImplementedError("BigQueryAdapter implementation pending US-008")

    def dialect(self) -> Dialect:
        # Returning the live constant is safe at this stage — the model
        # ships in US-004 and US-006's tests verify the wiring.
        from signalforge.warehouse.models import BIGQUERY_DIALECT

        return BIGQUERY_DIALECT

    def sample_rows(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
    ) -> list[dict]:
        raise NotImplementedError("BigQueryAdapter implementation pending US-008")

    def column_stats(self, table: TableRef, column: str) -> ColumnStats:
        raise NotImplementedError("BigQueryAdapter implementation pending US-008")

    def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> TestResult:
        raise NotImplementedError("BigQueryAdapter implementation pending US-008")


__all__ = ["BigQueryAdapter"]
