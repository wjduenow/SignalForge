"""Warehouse adapter abstract base class + factory (DEC-019).

The ABC contract is warehouse-agnostic; per-warehouse specifics live in
each concrete adapter under :mod:`signalforge.warehouse.adapters`.

The :meth:`WarehouseAdapter.from_profile` classmethod is the single
dispatch point for v0.1: ``profile.type == "bigquery"`` lazy-imports
:class:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter`; anything
else raises :class:`signalforge.warehouse.errors.UnsupportedProfileTypeError`.
The lazy import keeps ``google-cloud-bigquery`` off the import path of
callers that never invoke ``from_profile`` (e.g. tests injecting a fake
client directly).
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from signalforge.warehouse.models import (
        ColumnStats,
        Dialect,
        PartitionFilter,
        TableRef,
        TestResult,
    )
    from signalforge.warehouse.profiles import DbtProfileTarget


class WarehouseAdapter(abc.ABC):
    """Abstract sampler/profiler/test-runner for a SQL warehouse.

    Implementations live under :mod:`signalforge.warehouse.adapters`.
    The contract is warehouse-agnostic — every method either returns a
    typed value from :mod:`signalforge.warehouse.models` or raises a
    typed exception from :mod:`signalforge.warehouse.errors`.
    """

    @abc.abstractmethod
    def __enter__(self) -> WarehouseAdapter: ...

    @abc.abstractmethod
    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...

    @abc.abstractmethod
    def dialect(self) -> Dialect:
        """Return the :class:`Dialect` ADT for this warehouse."""

    @abc.abstractmethod
    def sample_rows(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
    ) -> list[dict]:
        """Return up to ``n`` rows sampled from ``table``.

        v0.1 contract: deterministic across runs (DEC-006). Implementations
        should fail loud (:class:`UnknownTableSizeError`,
        :class:`SamplingRequiresPartitionFilterError`) rather than silently
        over-spend on unscoped scans of large tables.
        """

    @abc.abstractmethod
    def column_stats(self, table: TableRef, column: str) -> ColumnStats:
        """Return aggregate stats for one column.

        Must be called inside an active context manager (DEC-025); raises
        :class:`RuntimeError` otherwise. Stats for multiple columns of the
        same table accumulate and flush as a single batched query at first
        read (DEC-008).
        """

    @abc.abstractmethod
    def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> TestResult:
        """Run candidate test SQL; return a typed :class:`TestResult`.

        SQL contract: a single ``SELECT`` returning rows (DEC-013). The
        test fails if any row returns; ``capture_failures>0`` carries up
        to N rows in :attr:`TestResult.sample_failures`.
        """

    def materialise_sample(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
        ttl_seconds: int = 3600,
    ) -> TableRef:
        """Materialise a deterministic sample of ``table`` into a temp/session
        table and return a :class:`TableRef` pointing at it (DEC-004 of issue #22).

        v0.2 contract: the default impl raises
        :class:`MaterialisationNotSupportedError`. Concrete adapters override
        the method when their warehouse provides a primitive that lets the
        prune layer materialise once and run every per-test failing-rows
        query against the materialised handle (BigQuery sessions + temp
        tables in v0.2; Snowflake / Postgres in v0.3+).

        Deliberately NOT decorated with ``@abstractmethod``: the default
        raise IS the v0.2 behaviour for non-BigQuery adapters. Forcing every
        new adapter to override would make the ABC harder to subclass for a
        warehouse that has not grown a materialisation primitive yet —
        :class:`MaterialisationNotSupportedError` is the correct, typed
        signal that the orchestrator routes via the conservative-bias
        ``kept-without-evidence`` taxonomy (DEC-009 of #22).

        Args:
            table: Source table to sample.
            n: Target sample size (passed through to the implementation's
                deterministic hash-mod sizing).
            partition_filter: Optional :class:`PartitionFilter` applied
                ONCE inside the materialisation WHERE clause (DEC-004; Q5
                of #22). Mirrors :meth:`sample_rows` parity.
            ttl_seconds: Hint to OUR cleanup-WARNING text only — NOT a
                value passed to the underlying warehouse SDK (DEC-013 of
                #22). BigQuery enforces its own server-side session
                lifetime; this kwarg lets the WARNING template say
                ``auto-expire in <N>s`` without an SDK round-trip.

        Returns:
            A :class:`TableRef` pointing at the materialised sample.

        Raises:
            MaterialisationNotSupportedError: Always, in the default impl.
                Concrete adapters override.
        """
        from signalforge.warehouse.errors import MaterialisationNotSupportedError

        raise MaterialisationNotSupportedError(adapter_name=type(self).__name__)

    @classmethod
    def from_profile(cls, profile: DbtProfileTarget) -> WarehouseAdapter:
        """Dispatch on ``profile.type`` and instantiate the matching adapter.

        v0.1 supports only ``profile.type == "bigquery"``; anything else
        raises :class:`UnsupportedProfileTypeError`. Direct instantiation
        of a concrete adapter remains supported for tests and explicit-config
        use; this factory is the single dispatch point for the CLI / prune
        layer so v0.2 can add a Snowflake case in one place rather than
        threading dispatch through every caller.
        """
        from signalforge.warehouse.errors import UnsupportedProfileTypeError

        if profile.type == "bigquery":
            # Lazy import: keeps google-cloud-bigquery off the import path
            # for users who never call from_profile (e.g. test paths
            # injecting a fake client directly into a concrete adapter).
            from signalforge.warehouse.adapters.bigquery import BigQueryAdapter

            # Explicit ``is None`` so a profile that pins
            # ``maximum_bytes_billed: 0`` (or any other falsy int) is honoured
            # rather than silently swapped for the default cap.
            max_bytes_billed = (
                100_000_000
                if profile.maximum_bytes_billed is None
                else profile.maximum_bytes_billed
            )
            return BigQueryAdapter(
                project=profile.project,
                location=profile.location,
                max_bytes_billed=max_bytes_billed,
            )
        raise UnsupportedProfileTypeError(profile_type=profile.type)


__all__ = ["WarehouseAdapter"]
