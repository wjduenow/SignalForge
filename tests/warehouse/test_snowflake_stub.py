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

``column_stats`` still raises :class:`NotImplementedError` naming the epic
(#118) — ``sample_rows`` (#122 US-003), ``materialise_sample`` / ``run_test_sql``
(#122 US-004) are now implemented and exercised in the sampling / materialise
suites. ``estimate_query_bytes`` inherits the ABC default (raising the typed
:class:`EstimateNotSupportedError`) pending issue #123.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from signalforge.warehouse.adapters.snowflake import SnowflakeAdapter
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import EstimateNotSupportedError
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
    / ``database`` / ``schema`` — nor the key-pair / SSO auth fields
    ``private_key_path`` / ``private_key_passphrase`` / ``authenticator``
    (DEC-003 / DEC-008)."""
    adapter = SnowflakeAdapter(
        account="ac123",
        user="bob",
        password="s3cret",
        role="r",
        warehouse="WH",
        database="db",
        schema="sch",
        private_key_path="/keys/rsa_key.p8",
        private_key_passphrase="topsecret",
        authenticator="externalbrowser",
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

    # Key-pair / SSO auth fields must not leak — neither values nor labels.
    assert "topsecret" not in rendered
    assert "/keys/rsa_key.p8" not in rendered
    assert "private_key" not in rendered
    assert "passphrase" not in rendered
    assert "authenticator" not in rendered


def test_init_stores_key_pair_and_sso_auth_fields() -> None:
    """The constructor captures the three forward-compat auth params on
    ``self._private_key_path`` / ``self._private_key_passphrase`` /
    ``self._authenticator`` so #122 can open a real connection (DEC-008)."""
    adapter = SnowflakeAdapter(
        private_key_path="/keys/rsa_key.p8",
        private_key_passphrase="topsecret",
        authenticator="externalbrowser",
    )

    assert adapter._private_key_path == "/keys/rsa_key.p8"
    assert adapter._private_key_passphrase == "topsecret"
    assert adapter._authenticator == "externalbrowser"


# ---------------------------------------------------------------------------
# Stub methods raise NotImplementedError naming the epic (#118)
# ---------------------------------------------------------------------------


def test_column_stats_raises_not_implemented() -> None:
    """:meth:`column_stats` is part of the v0.2 skeleton surface."""
    adapter = SnowflakeAdapter()
    table = TableRef(project=None, dataset="public", name="t")

    with pytest.raises(NotImplementedError) as exc_info:
        adapter.column_stats(table, "id")

    assert "issue #118" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ABC-default graceful-degrade methods (NOT overridden)
# ---------------------------------------------------------------------------


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
    raise :class:`UnsupportedProfileTypeError`) and wires EVERY parsed field
    through (#120, US-005). Snowflake's ``schema:`` key hydrates
    ``profile.dataset`` via the alias, which the factory passes as the
    adapter's ``schema`` kwarg."""
    profile = DbtProfileTarget.model_validate(
        {
            "type": "snowflake",
            "account": "xy12345.us-east-1",
            "user": "svc",
            "role": "TRANSFORMER",
            "warehouse": "WH",
            "database": "DB",
            "schema": "sch",
            "password": "s3cret",
        }
    )

    adapter = WarehouseAdapter.from_profile(profile)

    assert isinstance(adapter, SnowflakeAdapter)
    # Full wiring: every parsed field reaches the adapter.
    assert adapter._account == "xy12345.us-east-1"
    assert adapter._user == "svc"
    assert adapter._role == "TRANSFORMER"
    assert adapter._warehouse == "WH"
    assert adapter._database == "DB"
    assert adapter._schema == "sch"
    assert adapter._password == "s3cret"


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
    {
        "type": "snowflake",
        "account": "xy12345.us-east-1",
        "user": "svc",
        "warehouse": "WH",
        "database": "DB",
        "schema": "sch",
    }
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
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode}):\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Context-manager parity with the BigQuery adapter
# ---------------------------------------------------------------------------


def test_context_manager_with_no_opened_connection_is_a_clean_no_op() -> None:
    """The adapter honours the ABC's ``with adapter:`` contract so callers can
    swap a Snowflake profile in without conditional ``with`` logic. When no
    connection was ever opened (``_active_session is None``), ``__exit__``'s
    fail-soft cleanup (#122 US-002) returns immediately — a clean no-op (no
    connection build, no close call)."""
    with SnowflakeAdapter() as adapter:
        assert isinstance(adapter, SnowflakeAdapter)
        assert adapter.dialect() is SNOWFLAKE_DIALECT
    # No connection was opened, so cleanup left state untouched.
    assert adapter._connection is None
    assert adapter._active_session is None
