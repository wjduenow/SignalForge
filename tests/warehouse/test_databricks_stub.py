"""Tests for the DatabricksAdapter v0.x skeleton (issue #221; epic #219).

The skeleton exists to validate Architectural Commitment #3 — "warehouse-agnostic
by design" — by forcing the ``WarehouseAdapter`` ABC + ``from_profile`` factory
through a FOURTH concrete code path (after BigQuery, the Postgres stub, and
Snowflake). Tests pin the issue ACs:

1. ``from_profile`` routes ``type: databricks`` to the skeleton WITHOUT importing
   the google-cloud-bigquery SDK.
2. :meth:`dialect` returns :data:`DATABRICKS_DIALECT` by identity.
3. :meth:`__repr__` shows only safe fields (``host`` / ``http_path`` /
   ``catalog``), never ``token`` / ``client_secret`` / ``schema``.
4. ``column_stats`` / ``run_test_sql`` raise :class:`NotImplementedError`
   naming the epic (#219). (``sample_rows`` + ``get_row_count`` graduated to
   real implementations in #224 US-003 — covered in
   ``tests/warehouse/test_databricks_adapter.py``.)
5. ``materialise_sample`` / ``estimate_query_bytes`` / ``run_stats_query``
   inherit the ABC typed degrade.
6. SDK type-ignores are confined to ``_databricks_client.py`` — pinned by
   ``tests/warehouse/test_databricks_client_confinement.py``, not here.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from signalforge.warehouse import (
    DatabricksAdapter,
    EstimateNotSupportedError,
    StatsQueryNotSupportedError,
)
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.models import DATABRICKS_DIALECT, Dialect, TableRef
from signalforge.warehouse.profiles import DbtProfileTarget

# ---------------------------------------------------------------------------
# Dialect contract (AC-2)
# ---------------------------------------------------------------------------


def test_databricks_dialect_values() -> None:
    """The Databricks :class:`Dialect` carries the values the prune compiler
    (#223) will key on. ``quote_char='`'`` (backtick) and
    ``identifier_case='lower'`` (Unity Catalog lower-folds) are the load-bearing
    Databricks-vs-Snowflake differences."""
    assert isinstance(DATABRICKS_DIALECT, Dialect)
    assert DATABRICKS_DIALECT.name == "databricks"
    assert DATABRICKS_DIALECT.quote_char == "`"
    assert DATABRICKS_DIALECT.identifier_case == "lower"
    assert DATABRICKS_DIALECT.supports_qualify is True
    # 64-bit hash for sampling stability (Spark's bare hash(*) is Murmur3-32).
    assert "xxhash64" in DATABRICKS_DIALECT.sample_row_hash_expr
    # Sign-bit MASK, never ABS: Spark's ABS(Long.MIN_VALUE) stays negative in
    # non-ANSI mode and would skew MOD(<expr>, bucket) < 1 (CodeRabbit, PR #254).
    assert "ABS(" not in DATABRICKS_DIALECT.sample_row_hash_expr
    assert "9223372036854775807" in DATABRICKS_DIALECT.sample_row_hash_expr
    # Unity Catalog three-part names quote per component.
    assert DATABRICKS_DIALECT.quote_qualified_per_component is True


def test_dialect_method_returns_databricks_dialect_by_identity() -> None:
    """:meth:`DatabricksAdapter.dialect` returns the module-level constant by
    identity, not a freshly-constructed equivalent — callers may key on
    identity for cheap dispatch."""
    adapter = DatabricksAdapter()
    assert adapter.dialect() is DATABRICKS_DIALECT


# ---------------------------------------------------------------------------
# __repr__ credential redaction (AC-3)
# ---------------------------------------------------------------------------


def test_repr_shows_only_safe_fields_never_credentials() -> None:
    """:meth:`__repr__` renders ONLY ``host`` + ``http_path`` + ``catalog``. A
    debug-print / log line must never leak ``token``, ``schema``, or the
    OAuth-M2M ``client_id`` / ``client_secret``."""
    adapter = DatabricksAdapter(
        host="dbc-abc123.cloud.databricks.com",
        http_path="/sql/1.0/warehouses/abc123",
        token="dapideadbeefcafe",
        catalog="main",
        schema="analytics",
        auth_type="databricks-oauth",
        client_id="svc-client",
        client_secret="s3cret-oauth",
    )
    rendered = repr(adapter)

    # Safe identifying fields appear.
    assert "dbc-abc123.cloud.databricks.com" in rendered
    assert "/sql/1.0/warehouses/abc123" in rendered
    assert "main" in rendered

    # Credentials / data-location fields must NOT leak — neither values nor labels.
    assert "dapideadbeefcafe" not in rendered
    assert "token" not in rendered
    assert "s3cret-oauth" not in rendered
    assert "client_secret" not in rendered
    assert "client_id" not in rendered
    assert "svc-client" not in rendered
    assert "analytics" not in rendered
    assert "schema" not in rendered


def test_init_stores_forward_compat_oauth_fields() -> None:
    """The constructor captures the three forward-compat OAuth-M2M params on
    ``self._auth_type`` / ``self._client_id`` / ``self._client_secret`` so a
    later child can open an M2M connection without a signature change (epic
    open-decision #3 — PAT only for v0.x)."""
    adapter = DatabricksAdapter(
        auth_type="databricks-oauth",
        client_id="svc-client",
        client_secret="s3cret-oauth",
    )

    assert adapter._auth_type == "databricks-oauth"
    assert adapter._client_id == "svc-client"
    assert adapter._client_secret == "s3cret-oauth"


# ---------------------------------------------------------------------------
# column_stats still raises NotImplementedError (AC-4)
# (sample_rows graduated in #224 US-003; materialise_sample + run_test_sql
# graduated in #224 US-004 — see tests/warehouse/test_databricks_adapter.py)
# ---------------------------------------------------------------------------


def test_column_stats_raises_not_implemented() -> None:
    adapter = DatabricksAdapter()
    table = TableRef(project=None, dataset="analytics", name="t")
    with pytest.raises(NotImplementedError) as exc_info:
        adapter.column_stats(table, "id")
    assert "issue #219" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Degrade-default ABC methods inherit their typed *NotSupportedError (AC-5)
# (materialise_sample graduated to a real impl in #224 US-004)
# ---------------------------------------------------------------------------


def test_estimate_query_bytes_inherits_typed_degrade() -> None:
    adapter = DatabricksAdapter()
    with pytest.raises(EstimateNotSupportedError):
        adapter.estimate_query_bytes("SELECT 1")


def test_run_stats_query_inherits_typed_degrade() -> None:
    adapter = DatabricksAdapter()
    with pytest.raises(StatsQueryNotSupportedError):
        adapter.run_stats_query("SELECT 1")


# ---------------------------------------------------------------------------
# from_profile dispatch (AC-1)
# ---------------------------------------------------------------------------


def test_from_profile_dispatches_databricks_to_skeleton() -> None:
    """The factory routes ``type: databricks`` to the skeleton adapter (NOT
    raise :class:`UnsupportedProfileTypeError`). Since #222 the profile model
    parses the real Databricks connection fields (``host`` / ``http_path`` /
    ``token`` / ``catalog``), and US-003 rewired ``base.py``'s ``from_profile``
    databricks branch to wire every parsed field through to the adapter
    (replacing the #221 placeholder ``catalog<-profile.project`` mapping). So
    ``_host`` / ``_http_path`` / ``_token`` / ``_catalog`` now reflect the real
    target, and ``schema:`` still hydrates ``_schema`` via the ``dataset``
    alias."""
    profile = DbtProfileTarget.model_validate(
        {
            "type": "databricks",
            "host": "dbc-ab12cd34.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/abc123",
            "token": "dapi-token",
            "catalog": "main",
            "schema": "analytics",
        }
    )

    adapter = WarehouseAdapter.from_profile(profile)

    assert isinstance(adapter, DatabricksAdapter)
    # US-003 wires the real parsed connection fields through from_profile.
    assert adapter._host == "dbc-ab12cd34.cloud.databricks.com"
    assert adapter._http_path == "/sql/1.0/warehouses/abc123"
    assert adapter._token == "dapi-token"
    assert adapter._catalog == "main"
    # `schema:` hydrates profile.dataset, which base.py maps to _schema.
    assert adapter._schema == "analytics"


# ---------------------------------------------------------------------------
# from_profile databricks dispatch does NOT import the BigQuery SDK (AC-1)
# ---------------------------------------------------------------------------


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
        "type": "databricks",
        "host": "dbc-ab12cd34.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc123",
        "token": "dapi-token",
        "catalog": "main",
        "schema": "analytics",
    }
)
adapter = WarehouseAdapter.from_profile(profile)

from signalforge.warehouse import DatabricksAdapter

assert isinstance(adapter, DatabricksAdapter)
assert "google.cloud.bigquery" not in sys.modules, (
    "databricks dispatch must not import the google-cloud-bigquery SDK"
)
assert "databricks" not in sys.modules, (
    "the skeleton must not import the databricks-sql-connector SDK (no eager connect)"
)
print("OK")
"""


def test_databricks_dispatch_does_not_import_bigquery_or_connector_sdk() -> None:
    """The databricks branch of :meth:`from_profile` must not pull in the
    google-cloud-bigquery SDK, nor the databricks-sql-connector (no eager
    connect). Run in a fresh subprocess so the ``sys.modules`` assertions are
    robust even when an earlier in-process test already imported a SDK."""
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
# Context-manager parity with the other adapters
# ---------------------------------------------------------------------------


def test_context_manager_with_no_opened_connection_is_a_clean_no_op() -> None:
    """The adapter honours the ABC's ``with adapter:`` contract so callers can
    swap a Databricks profile in without conditional ``with`` logic. When no
    connection was ever opened (``_active_session is None``), ``__exit__``'s
    fail-soft cleanup returns immediately — a clean no-op (no connection build,
    no close call)."""
    with DatabricksAdapter() as adapter:
        assert isinstance(adapter, DatabricksAdapter)
        assert adapter.dialect() is DATABRICKS_DIALECT
    # No connection was opened, so cleanup left state untouched.
    assert adapter._connection is None
    assert adapter._active_session is None
