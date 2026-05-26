"""Snowflake adapter — v0.2 skeleton (issue #119; epic #118).

The skeleton exists to validate the warehouse-agnostic seam — Architectural
Commitment #3 of ``CLAUDE.md`` — through a *third* concrete adapter code path
(after :class:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter` and the
:class:`signalforge.warehouse.adapters.postgres.PostgresAdapter` stub). Wiring
the ABC + factory seam through Snowflake right now surfaces any leak here
rather than during the real Snowflake implementation (issue #118).

Scope (deliberately minimal):

* :meth:`__init__` captures connection params (``account`` / ``user`` /
  ``password`` / ``role`` / ``warehouse`` / ``database`` / ``schema``) for
  forward-compat (DEC-002). No connection is opened.
* :meth:`__repr__` renders ONLY non-credential identifying fields — ``account``
  and ``warehouse`` — so a debug-print or log line never leaks ``user`` /
  ``password`` / ``role`` / ``database`` / ``schema`` (DEC-003).
* :meth:`__enter__` / :meth:`__exit__` are no-ops so the ``with adapter:``
  contract from :class:`signalforge.warehouse.base.WarehouseAdapter` works
  without conditional logic at the call site.
* :meth:`dialect` returns the :data:`SNOWFLAKE_DIALECT` constant from
  :mod:`signalforge.warehouse.models` (``quote_char='"'``,
  ``identifier_case='upper'``, ``supports_qualify=True``).
* The three warehouse-operation methods (:meth:`sample_rows`,
  :meth:`column_stats`, :meth:`run_test_sql`) raise
  :class:`NotImplementedError` naming the epic (#118) so the v0.2
  implementation work has a single grep target (DEC-008).
* :meth:`materialise_sample` / :meth:`estimate_query_bytes` are NOT overridden
  — the ABC defaults (raising :class:`MaterialisationNotSupportedError` /
  :class:`EstimateNotSupportedError`) are the correct v0.2 behaviour for a
  warehouse that hasn't grown those primitives yet (DEC-008).
* :meth:`WarehouseAdapter.from_profile` dispatches ``profile.type ==
  "snowflake"`` here so an operator with a Snowflake profile sees a
  ``NotImplementedError`` rather than the v0.1
  :class:`UnsupportedProfileTypeError`.

What this skeleton does NOT do:

* Real Snowflake connectivity. No ``snowflake.connector`` import — that is
  confined to :mod:`signalforge.warehouse.adapters._snowflake_client`, the
  one-shim-per-vendor SDK seam, when the full implementation lands.
* Extend :class:`DbtProfileTarget` to carry Snowflake-specific fields
  (``account`` / ``user`` / ``role`` / ``warehouse``). The current profile
  model is BigQuery-shaped; growing it to wire those fields into the factory
  is issue #120's work.

When the v0.2 implementation lands (issue #118), replace every
``NotImplementedError`` with the real adapter call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.models import SNOWFLAKE_DIALECT, ColumnStats, Dialect, TestResult

if TYPE_CHECKING:
    from signalforge.warehouse.models import PartitionFilter, TableRef


_V02_REMEDIATION = "SnowflakeAdapter is a v0.2 skeleton (issue #118) — full implementation pending."


class SnowflakeAdapter(WarehouseAdapter):
    """Skeleton :class:`WarehouseAdapter` for Snowflake profiles.

    Forward-compat only; every method other than :meth:`__init__`,
    :meth:`__repr__`, the context-manager no-ops, and :meth:`dialect` raises
    :class:`NotImplementedError`.
    """

    def __init__(
        self,
        *,
        account: str | None = None,
        user: str | None = None,
        password: str | None = None,
        role: str | None = None,
        warehouse: str | None = None,
        database: str | None = None,
        schema: str | None = None,
    ) -> None:
        self._account = account
        self._user = user
        self._password = password
        self._role = role
        self._warehouse = warehouse
        self._database = database
        self._schema = schema

    def __repr__(self) -> str:
        # DEC-003: render ONLY non-credential identifying fields. NEVER user,
        # password, role, database, or schema.
        return f"<SnowflakeAdapter account={self._account!r} warehouse={self._warehouse!r}>"

    def __enter__(self) -> WarehouseAdapter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def dialect(self) -> Dialect:
        return SNOWFLAKE_DIALECT

    def sample_rows(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
    ) -> list[dict]:
        raise NotImplementedError(f"sample_rows: {_V02_REMEDIATION}")

    def column_stats(self, table: TableRef, column: str) -> ColumnStats:
        raise NotImplementedError(f"column_stats: {_V02_REMEDIATION}")

    def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> TestResult:
        raise NotImplementedError(f"run_test_sql: {_V02_REMEDIATION}")


__all__ = ["SNOWFLAKE_DIALECT", "SnowflakeAdapter"]
