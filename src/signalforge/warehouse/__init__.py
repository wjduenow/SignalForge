"""Warehouse subpackage — sampling, profiling, and test-running for SQL warehouses.

Stage-2 of the SignalForge pipeline. Public surface (DEC-017, mirroring
:mod:`signalforge.manifest`):

- :func:`load_profile` — read a dbt ``profiles.yml`` and return a typed
  :class:`DbtProfileTarget`.
- :class:`WarehouseAdapter` — abstract sampler/profiler/test-runner; the
  factory :meth:`WarehouseAdapter.from_profile` dispatches on
  ``profile.type``.
- :class:`BigQueryAdapter` — v0.1's only concrete adapter.
- :class:`Dialect`, :class:`TableRef`, :class:`PartitionFilter`,
  :class:`ColumnStats`, :class:`TestResult`, :class:`DbtProfileTarget` —
  warehouse-agnostic value objects callers consume / construct.
- :data:`BIGQUERY_DIALECT` — the canonical :class:`Dialect` instance for
  BigQuery (DEC-003).
- The full :class:`WarehouseError` hierarchy, so callers can catch typed
  failures without reaching into private modules.

Anything not re-exported here is an implementation detail. Internal helpers
(``_sql_safety``, ``_path_safety``, ``_test_result_repr``) remain reachable
via their dotted module paths but are deliberately not promoted to the
package's top-level namespace.
"""

from signalforge.warehouse.adapters.bigquery import BigQueryAdapter
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import (
    BytesBilledExceededError,
    ColumnNotFoundError,
    EstimateNotSupportedError,
    InvalidIdentifierError,
    ManifestProjectNotFoundError,
    ManifestSchemaNotFoundError,
    MaterialisationFailedError,
    MaterialisationNotSupportedError,
    ProfileEnvVarUnsetError,
    ProfileNotFoundError,
    ProfileTargetNotFoundError,
    QuerySyntaxError,
    SamplingError,
    SamplingRequiresPartitionFilterError,
    TableNotFoundError,
    UnknownTableSizeError,
    UnsupportedAuthMethodError,
    UnsupportedProfileTypeError,
    WarehouseAuthError,
    WarehouseError,
)
from signalforge.warehouse.models import (
    BIGQUERY_DIALECT,
    ColumnStats,
    Dialect,
    PartitionFilter,
    TableRef,
    TestResult,
)
from signalforge.warehouse.profiles import DbtProfileTarget, load_profile

# Sorted alphabetically (Python default: capitals before lowercase).
# Hard-coded literal — pyright's reportUnsupportedDunderAll rejects a
# computed ``sorted([...])`` here. ``test_all_is_sorted`` guards against
# drift.
__all__ = [
    "BIGQUERY_DIALECT",
    "BigQueryAdapter",
    "BytesBilledExceededError",
    "ColumnNotFoundError",
    "ColumnStats",
    "DbtProfileTarget",
    "Dialect",
    "EstimateNotSupportedError",
    "InvalidIdentifierError",
    "ManifestProjectNotFoundError",
    "ManifestSchemaNotFoundError",
    "MaterialisationFailedError",
    "MaterialisationNotSupportedError",
    "PartitionFilter",
    "ProfileEnvVarUnsetError",
    "ProfileNotFoundError",
    "ProfileTargetNotFoundError",
    "QuerySyntaxError",
    "SamplingError",
    "SamplingRequiresPartitionFilterError",
    "TableNotFoundError",
    "TableRef",
    "TestResult",
    "UnknownTableSizeError",
    "UnsupportedAuthMethodError",
    "UnsupportedProfileTypeError",
    "WarehouseAdapter",
    "WarehouseAuthError",
    "WarehouseError",
    "load_profile",
]
