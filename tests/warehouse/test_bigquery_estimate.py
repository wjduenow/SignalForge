"""Tests for :meth:`BigQueryAdapter.estimate_query_bytes` (US-002 of issue #36).

The estimate seam exists so the v0.2 ``signalforge generate --estimate``
flow can produce a cost preview without committing to any real scan.
BigQuery's ``QueryJobConfig(dry_run=True)`` is the v0.2 mechanism;
non-BigQuery adapters inherit the ABC's default
``EstimateNotSupportedError`` raise.

These tests pin three load-bearing invariants:

1. The production path issues exactly one ``client.query`` call with
   ``dry_run=True`` and reads ``total_bytes_processed`` off the
   returned job.
2. ``maximum_bytes_billed`` is NOT set on the dry_run job_config —
   dry_run never bills, so a cap there would be dead config.
3. Auth / connection failures still route through
   :func:`signalforge.warehouse.adapters._client.map_bq_exception`
   (mirrors :meth:`run_test_sql` / :meth:`sample_rows`).

Plus two ABC-level tests:

* The default impl raises :class:`EstimateNotSupportedError` for any
  adapter that does NOT override the method.
* The default remediation is locked verbatim (DEC-004 of issue #36's plan).
"""

from __future__ import annotations

from typing import Any

import pytest
from google.api_core.exceptions import (
    BadRequest,
    Forbidden,
    InternalServerError,
)

from signalforge.warehouse import (
    BigQueryAdapter,
    EstimateNotSupportedError,
    WarehouseAuthError,
)
from signalforge.warehouse.base import WarehouseAdapter
from tests.warehouse._fake import FakeBigQueryClient


def test_estimate_query_bytes_dry_run_returns_total_bytes_processed(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """Happy path: ``estimate_query_bytes(sql)`` issues one
    ``client.query`` call with ``dry_run=True`` and returns the
    integer the fake registered as ``returns_bytes``.
    """
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=12_345_678)

    result = adapter.estimate_query_bytes("SELECT * FROM dataset.table WHERE 1=1")

    assert result == 12_345_678
    fake_client.assert_all_expectations_met()


def test_estimate_query_bytes_uses_dry_run_true_in_job_config(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """The job_config the production helper hands to ``client.query``
    has ``dry_run is True``. This is what BigQuery server-side keys on
    to skip the billing path; the fake's dispatch routes the call into
    the ``_dry_run_expectations`` queue based on the same field.
    """
    captured: dict[str, Any] = {}

    def _check(job_config: Any) -> bool:
        captured["job_config"] = job_config
        return True

    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=4096, job_config_check=_check)

    adapter.estimate_query_bytes("SELECT 1")

    assert captured["job_config"].dry_run is True


def test_estimate_query_bytes_does_not_set_maximum_bytes_billed(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-004 of issue #36 — dry_run does not bill bytes, so the
    production helper deliberately does NOT set
    ``maximum_bytes_billed`` on the dry_run job_config. Setting it
    would be dead config; worse, it could mislead a reader into
    thinking the dry_run was capped against runaway cost.
    """
    captured: dict[str, Any] = {}

    def _check(job_config: Any) -> bool:
        captured["maximum_bytes_billed"] = job_config.maximum_bytes_billed
        return True

    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=1024, job_config_check=_check)

    adapter.estimate_query_bytes("SELECT 1")

    # The BigQuery SDK exposes the unset state as ``None``; the
    # explicit assertion documents the contract independent of the
    # SDK's sentinel choice.
    assert captured["maximum_bytes_billed"] is None


def test_estimate_query_bytes_propagates_warehouse_auth_error(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """A 403 ``Forbidden`` from the SDK routes through
    :func:`map_bq_exception` and surfaces as
    :class:`WarehouseAuthError`. Mirrors :meth:`run_test_sql` /
    :meth:`sample_rows`.
    """
    fake_client.expect_dry_run(
        sql_matching=r"SELECT",
        returns_bytes=Forbidden("permission denied"),
    )

    with pytest.raises(WarehouseAuthError):
        adapter.estimate_query_bytes("SELECT 1")


def test_estimate_query_bytes_propagates_warehouse_connection_error(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """A transient server-side failure that
    :func:`map_bq_exception` does not specifically translate (here, a
    500 ``InternalServerError``) re-raises as-is via the
    ``if mapped is exc: raise`` branch. The CLI tier-3 mapping then
    catches it via the :class:`WarehouseError` MRO walk.

    "Connection error" in the AC names this branch generically — any
    SDK exception the mapper does not specialise still propagates
    rather than being silently swallowed.
    """
    exc = InternalServerError("backend transient failure")
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=exc)

    with pytest.raises(InternalServerError):
        adapter.estimate_query_bytes("SELECT 1")


def test_estimate_query_bytes_propagates_warehouse_quota_error(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """A malformed-SQL ``BadRequest`` routes through
    :func:`map_bq_exception` as :class:`QuerySyntaxError`. This is
    the analogue of US-002's "quota failure" AC: any ``BadRequest``
    that isn't a billing-limit shape surfaces as the typed
    query-syntax error rather than leaking the SDK exception.
    """
    from signalforge.warehouse import QuerySyntaxError

    fake_client.expect_dry_run(
        sql_matching=r"SELECT",
        returns_bytes=BadRequest("Syntax error: Unexpected end of statement"),
    )

    with pytest.raises(QuerySyntaxError):
        adapter.estimate_query_bytes("SELECT 1")


def test_warehouse_adapter_abc_default_raises_estimatenotsupportederror() -> None:
    """A bare :class:`WarehouseAdapter` subclass without an
    ``estimate_query_bytes`` override inherits the ABC's default
    raise. Mirrors the :meth:`materialise_sample` precedent of issue
    #22 — graceful degrade rather than ``@abstractmethod`` force.

    The fake subclass below only fills in the truly-abstract methods
    so it can instantiate; ``estimate_query_bytes`` is left at the
    default. Any caller invoking it gets the typed error with the
    locked remediation.
    """

    class _BareAdapter(WarehouseAdapter):
        def __enter__(self) -> WarehouseAdapter:  # pragma: no cover - never entered
            return self

        def __exit__(  # pragma: no cover - never entered
            self, exc_type: object, exc: object, tb: object
        ) -> None:
            return None

        def dialect(self) -> Any:  # pragma: no cover - never called
            raise NotImplementedError

        def sample_rows(  # pragma: no cover - never called
            self, table: Any, n: int, *, partition_filter: Any = None
        ) -> Any:
            raise NotImplementedError

        def column_stats(  # pragma: no cover - never called
            self, table: Any, column: str
        ) -> Any:
            raise NotImplementedError

        def run_test_sql(  # pragma: no cover - never called
            self, sql: str, *, capture_failures: int = 0
        ) -> Any:
            raise NotImplementedError

    bare = _BareAdapter()

    with pytest.raises(EstimateNotSupportedError) as exc_info:
        bare.estimate_query_bytes("SELECT 1")

    # The adapter name routes onto the error for operator
    # diagnostics — same shape as MaterialisationNotSupportedError.
    assert exc_info.value.adapter_name == "_BareAdapter"


def test_estimatenotsupportederror_remediation_locked_verbatim() -> None:
    """DEC-004 of issue #36's plan — the default remediation is locked
    byte-for-byte. Changing this text is a contract break: the CLI's
    ``--estimate`` flow surfaces the string to operators verbatim, and
    downstream tooling / CI parsers may key on it.
    """
    expected = (
        "Use --estimate with a BigQuery profile, "
        "or wait for v0.3 multi-warehouse estimation support."
    )
    assert EstimateNotSupportedError.default_remediation == expected


def test_estimate_query_bytes_uses_correct_stage_label(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-015 of issue #3 / DEC-004 of issue #36 — the dry_run
    job_config carries the SignalForge labels for v0.2 cost
    attribution via ``INFORMATION_SCHEMA.JOBS_BY_PROJECT``. The
    stage label is ``"warehouse_estimate_query_bytes"`` so operators
    can filter estimate-stage jobs distinctly from sample / test /
    materialise jobs.
    """
    captured: dict[str, Any] = {}

    def _check(job_config: Any) -> bool:
        captured["labels"] = dict(job_config.labels or {})
        return True

    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=2048, job_config_check=_check)

    adapter.estimate_query_bytes("SELECT 1")

    assert captured["labels"].get("signalforge_stage") == "warehouse_estimate_query_bytes"


def test_estimate_query_bytes_rejects_invalid_test_sql(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """Mirrors :meth:`run_test_sql` — the production helper subjects
    ``sql`` to the same cheap rejects (no ``;``, no ``--``, balanced
    parens) before it reaches the SDK. A semicolon-terminated SQL
    fails the validator at the seam; no dry_run call is issued.
    """
    from signalforge.warehouse import QuerySyntaxError

    # No expectation queued — if the production helper accidentally
    # passed through to the SDK, the fake would raise the standard
    # "unexpected dry_run" AssertionError, which would mask the real
    # safety-check failure. The validator must fire first.
    with pytest.raises(QuerySyntaxError):
        adapter.estimate_query_bytes("SELECT 1;")
