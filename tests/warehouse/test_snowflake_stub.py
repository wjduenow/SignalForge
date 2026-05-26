"""Tests for the SnowflakeAdapter v0.2 skeleton (issue #119, US-003; epic #118).

The skeleton exists to validate Architectural Commitment #3 — "warehouse-agnostic
by design" — by forcing the ``WarehouseAdapter`` ABC + ``from_profile`` factory
through a THIRD concrete code path (after BigQuery and the Postgres stub). Tests
pin the four issue ACs:

1. ``from_profile`` routes ``type: snowflake`` to the skeleton WITHOUT importing
   the google-cloud-bigquery SDK (DEC-001 / DEC-007).
2. :meth:`dialect` returns :data:`SNOWFLAKE_DIALECT` by identity (DEC-004).
3. :meth:`__repr__` shows only safe fields (``account`` / ``warehouse``), never
   credentials (DEC-003).
4. SDK type-ignores are confined to ``_snowflake_client.py`` — pinned by
   ``tests/warehouse/test_snowflake_client_confinement.py``, not here.

The three warehouse-operation methods raise :class:`NotImplementedError` naming
the epic (#118); ``__enter__`` / ``__exit__`` are no-ops so the ``with adapter:``
contract works without conditional logic at the call site;
``materialise_sample`` / ``estimate_query_bytes`` inherit the ABC defaults
(raising the typed not-supported errors) — they are deliberately NOT overridden.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from signalforge.warehouse.adapters.snowflake import SnowflakeAdapter
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import (
    EstimateNotSupportedError,
    MaterialisationNotSupportedError,
)
from signalforge.warehouse.models import SNOWFLAKE_DIALECT, Dialect, TableRef
from signalforge.warehouse.profiles import DbtProfileTarget

# ---------------------------------------------------------------------------
# Dialect contract (AC-2)
# ---------------------------------------------------------------------------


def test_snowflake_dialect_values() -> None:
    """The Snowflake :class:`Dialect` carries the values the prune compiler
    (issue #121) keys on. ``identifier_case='upper'`` is the load-bearing
    Snowflake-vs-Postgres difference."""
    assert isinstance(SNOWFLAKE_DIALECT, Dialect)
    assert SNOWFLAKE_DIALECT.name == "snowflake"
    assert SNOWFLAKE_DIALECT.quote_char == '"'
    assert SNOWFLAKE_DIALECT.identifier_case == "upper"
    assert SNOWFLAKE_DIALECT.supports_qualify is True


def test_dialect_method_returns_snowflake_dialect_by_identity() -> None:
    """:meth:`SnowflakeAdapter.dialect` returns the module-level constant by
    identity, not a freshly-constructed equivalent — callers may key on
    identity for cheap dispatch (DEC-004)."""
    adapter = SnowflakeAdapter()
    assert adapter.dialect() is SNOWFLAKE_DIALECT


# ---------------------------------------------------------------------------
# __repr__ credential redaction (AC-3, DEC-003)
# ---------------------------------------------------------------------------


def test_repr_shows_only_safe_fields_never_credentials() -> None:
    """:meth:`__repr__` renders ONLY ``account`` + ``warehouse``. A
    debug-print / log line must never leak ``user`` / ``password`` / ``role``
    / ``database`` / ``schema`` (DEC-003)."""
    adapter = SnowflakeAdapter(
        account="ac123",
        user="bob",
        password="s3cret",
        role="r",
        warehouse="WH",
        database="db",
        schema="sch",
    )
    rendered = repr(adapter)

    # Safe identifying fields appear.
    assert "ac123" in rendered
    assert "WH" in rendered

    # Credentials / data-location fields must NOT leak. (Assert the distinctive
    # substrings — single-char fields like role="r" alias to common letters,
    # so the load-bearing assertions are the password + user.)
    assert "s3cret" not in rendered
    assert "bob" not in rendered
    # The field-name labels for credentials must not appear either.
    assert "password" not in rendered
    assert "role" not in rendered
    assert "database" not in rendered
    assert "schema" not in rendered


# ---------------------------------------------------------------------------
# Stub methods raise NotImplementedError naming the epic (#118)
# ---------------------------------------------------------------------------


def test_sample_rows_raises_not_implemented() -> None:
    """The v0.2 skeleton raises :class:`NotImplementedError` on
    :meth:`sample_rows` naming the epic so the implementation work has a
    single grep target (DEC-008)."""
    adapter = SnowflakeAdapter()
    table = TableRef(project=None, dataset="public", name="t")

    with pytest.raises(NotImplementedError) as exc_info:
        adapter.sample_rows(table, 100)

    assert "issue #118" in str(exc_info.value)


def test_column_stats_raises_not_implemented() -> None:
    """:meth:`column_stats` is part of the v0.2 skeleton surface."""
    adapter = SnowflakeAdapter()
    table = TableRef(project=None, dataset="public", name="t")

    with pytest.raises(NotImplementedError) as exc_info:
        adapter.column_stats(table, "id")

    assert "issue #118" in str(exc_info.value)


def test_run_test_sql_raises_not_implemented() -> None:
    """:meth:`run_test_sql` is part of the v0.2 skeleton surface."""
    adapter = SnowflakeAdapter()

    with pytest.raises(NotImplementedError) as exc_info:
        adapter.run_test_sql("SELECT 1")

    assert "issue #118" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ABC-default graceful-degrade methods (NOT overridden)
# ---------------------------------------------------------------------------


def test_materialise_sample_raises_not_supported() -> None:
    """``materialise_sample`` is deliberately NOT overridden — the ABC default
    (raising :class:`MaterialisationNotSupportedError`) is the correct v0.2
    behaviour for a warehouse without a materialisation primitive (DEC-008)."""
    adapter = SnowflakeAdapter()
    table = TableRef(project=None, dataset="public", name="t")

    with pytest.raises(MaterialisationNotSupportedError):
        adapter.materialise_sample(table, 100)


def test_estimate_query_bytes_raises_not_supported() -> None:
    """``estimate_query_bytes`` inherits the ABC default raising
    :class:`EstimateNotSupportedError` (DEC-008)."""
    adapter = SnowflakeAdapter()

    with pytest.raises(EstimateNotSupportedError):
        adapter.estimate_query_bytes("SELECT 1")


# ---------------------------------------------------------------------------
# from_profile dispatch (AC-1)
# ---------------------------------------------------------------------------


def test_from_profile_dispatches_snowflake_to_skeleton() -> None:
    """The factory routes ``type: snowflake`` to the skeleton adapter (NOT
    raise :class:`UnsupportedProfileTypeError`), wiring the BigQuery-shaped
    profile's project/schema to database/schema (DEC-001). #120 will grow the
    profile to carry account/user/role/warehouse."""
    profile = DbtProfileTarget.model_validate(
        {
            "type": "snowflake",
            "project": "db",
            "schema": "sch",
        }
    )

    adapter = WarehouseAdapter.from_profile(profile)

    assert isinstance(adapter, SnowflakeAdapter)
    assert adapter._database == "db"
    assert adapter._schema == "sch"


# ---------------------------------------------------------------------------
# from_profile snowflake dispatch does NOT import the BigQuery SDK (AC-1, DEC-007)
# ---------------------------------------------------------------------------


# Driver program for the no-SDK-import assertion. Run in a fresh subprocess so
# the assertion is robust regardless of whether an earlier test in the parent
# process already imported "google.cloud.bigquery" (the warehouse package
# __init__ eagerly imports the BigQuery *adapter module*, but the google SDK
# import is lazy inside _client.make_real_client — so we assert against the SDK
# module name, NOT the adapter module name).
_NO_BQ_SDK_DRIVER = """
import sys

import signalforge.warehouse  # noqa: F401  (eager adapter-module import, lazy SDK)

assert "google.cloud.bigquery" not in sys.modules, (
    "google.cloud.bigquery should not be imported merely by importing the "
    "warehouse package"
)

from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.profiles import DbtProfileTarget

profile = DbtProfileTarget.model_validate(
    {"type": "snowflake", "project": "db", "schema": "sch"}
)
adapter = WarehouseAdapter.from_profile(profile)

from signalforge.warehouse.adapters.snowflake import SnowflakeAdapter

assert isinstance(adapter, SnowflakeAdapter)
assert "google.cloud.bigquery" not in sys.modules, (
    "snowflake dispatch must not import the google-cloud-bigquery SDK"
)
print("OK")
"""


def test_snowflake_dispatch_does_not_import_bigquery_sdk() -> None:
    """The snowflake branch of :meth:`from_profile` must not pull in the
    google-cloud-bigquery SDK (DEC-007). Run in a fresh subprocess so the
    ``sys.modules`` assertion is robust even when an earlier in-process test
    already imported the BigQuery SDK."""
    result = subprocess.run(
        [sys.executable, "-c", _NO_BQ_SDK_DRIVER],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode}):\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Context-manager parity with the BigQuery adapter
# ---------------------------------------------------------------------------


def test_context_manager_is_a_no_op() -> None:
    """The skeleton honours the ABC's ``with adapter:`` contract so callers can
    swap a Snowflake profile in without conditional ``with`` logic. The
    ``__exit__`` is a no-op; cleanup work lands when the v0.x implementation
    does."""
    with SnowflakeAdapter() as adapter:
        assert isinstance(adapter, SnowflakeAdapter)
        assert adapter.dialect() is SNOWFLAKE_DIALECT
