"""Concrete warehouse adapters (DEC-001).

v0.1 ships :mod:`signalforge.warehouse.adapters.bigquery` only; v0.2 adds
Snowflake/Postgres siblings. This package is intentionally imported lazily
from :meth:`signalforge.warehouse.base.WarehouseAdapter.from_profile` so
``google-cloud-bigquery`` does not land on the import path of callers that
never invoke the factory.
"""
