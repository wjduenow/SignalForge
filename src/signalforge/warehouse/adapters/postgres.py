"""Postgres adapter — v0.2 stub (issue #53).

The stub exists to validate the warehouse-agnostic seam — Architectural
Commitment #3 of ``CLAUDE.md``. Pre-#53 the only concrete adapter was
:class:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter`; the
``WarehouseAdapter`` ABC and the ``Dialect`` value object's claim to be
"warehouse-agnostic by design" was *unverified* by a second adapter. This
module forces the ABC + factory seam through a second code path right
now so a v0.2 leak surfaces here instead of during a real Postgres /
Snowflake implementation later.

Scope (deliberately minimal):

* :meth:`__init__` captures connection params for forward-compat.
* :meth:`__enter__` / :meth:`__exit__` are implemented as no-ops so the
  ``with adapter:`` contract from
  :class:`signalforge.warehouse.base.WarehouseAdapter` works without
  conditional logic at the call site.
* :meth:`dialect` returns the :data:`POSTGRES_DIALECT` constant from
  :mod:`signalforge.warehouse.models` (``quote_char='"'``,
  ``identifier_case='lower'``, ``supports_qualify=False``).
* The three warehouse-operation methods (:meth:`sample_rows`,
  :meth:`column_stats`, :meth:`run_test_sql`) raise
  :class:`NotImplementedError` naming this ticket (#53) so the v0.2
  implementation work has a single grep target.
* :meth:`WarehouseAdapter.from_profile` dispatches ``profile.type ==
  "postgres"`` here so an operator with a Postgres profile sees a
  ``NotImplementedError`` rather than the v0.1
  :class:`UnsupportedProfileTypeError`.

What this stub does NOT do:

* Real Postgres connectivity. No psycopg / asyncpg / SQLAlchemy import.
* Extend :class:`DbtProfileTarget` to accept Postgres-specific fields
  (``host`` / ``port`` / ``user`` / ``password`` / ``dbname``). The
  current profile model is BigQuery-shaped (``extra="forbid"``); a real
  Postgres adapter will need to relax or split it. That work belongs to
  the v0.2 ticket.

When the v0.2 implementation lands, replace every ``NotImplementedError``
with the real adapter call and update ``adapters/_client.py`` (or its
Postgres sibling) for any pyright-ignore confinement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.models import POSTGRES_DIALECT, ColumnStats, Dialect, TestResult

if TYPE_CHECKING:
    from signalforge.warehouse.models import PartitionFilter, TableRef


_V02_REMEDIATION = "PostgresAdapter is a v0.2 stub (issue #53) — full implementation pending."


class PostgresAdapter(WarehouseAdapter):
    """Stub :class:`WarehouseAdapter` for Postgres profiles.

    Forward-compat only; every method other than :meth:`__init__` and
    :meth:`dialect` raises :class:`NotImplementedError`.
    """

    def __init__(
        self,
        *,
        dbname: str | None = None,
        schema: str | None = None,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self._dbname = dbname
        self._schema = schema
        self._host = host
        self._port = port
        self._user = user
        self._password = password

    def __repr__(self) -> str:
        return f"PostgresAdapter(dbname={self._dbname!r}, schema={self._schema!r})"

    def __enter__(self) -> WarehouseAdapter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def dialect(self) -> Dialect:
        return POSTGRES_DIALECT

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


__all__ = ["POSTGRES_DIALECT", "PostgresAdapter"]
