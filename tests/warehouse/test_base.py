"""Tests for the WarehouseAdapter ABC + factory (US-006, DEC-019).

Every test is capable of failing on a real regression (`testing-signal.md`):

* The ABC test fails if a future refactor accidentally drops an
  ``@abstractmethod`` decorator (Python's ABCMeta would let
  ``WarehouseAdapter()`` succeed).
* The ``from_profile`` dispatch tests fail if the factory either constructs
  the wrong adapter type or fails to thread profile fields through to the
  constructor.
* The default / explicit ``max_bytes_billed`` tests pin DEC-019's 100 MB
  fallback so a future refactor cannot silently change it.
* The ``materialise_sample`` ABC tests pin DEC-004 / DEC-008 of issue #22:
  the method exists with the published signature; the default impl raises
  :class:`MaterialisationNotSupportedError` carrying the DEC-006
  remediation text.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from signalforge.warehouse.adapters.bigquery import BigQueryAdapter
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import (
    MaterialisationNotSupportedError,
    UnsupportedProfileTypeError,
)
from signalforge.warehouse.models import (
    BIGQUERY_DIALECT,
    ColumnStats,
    Dialect,
    PartitionFilter,
    TableRef,
    TestResult,
)
from signalforge.warehouse.profiles import DbtProfileTarget


class _StubAdapter(WarehouseAdapter):
    """Minimal :class:`WarehouseAdapter` subclass for ABC default-impl tests.

    Overrides every ``@abstractmethod`` with a no-op stub so the class can
    be instantiated; deliberately does NOT override ``materialise_sample``
    so the ABC's default raise is what the test exercises (DEC-008 of #22).
    """

    def __enter__(self) -> WarehouseAdapter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def dialect(self) -> Dialect:
        return BIGQUERY_DIALECT

    def sample_rows(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
    ) -> list[dict[str, Any]]:
        return []

    def column_stats(self, table: TableRef, column: str) -> ColumnStats:
        raise NotImplementedError("test stub — not exercised here")

    def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> TestResult:
        raise NotImplementedError("test stub — not exercised here")


def test_warehouse_adapter_is_abstract() -> None:
    """ABCMeta should refuse to instantiate the bare ABC."""
    with pytest.raises(TypeError):
        WarehouseAdapter()  # type: ignore[abstract]


def test_from_profile_dispatches_bigquery() -> None:
    """profile.type == 'bigquery' should yield a BigQueryAdapter with all
    three constructor kwargs threaded through from the profile."""
    profile = DbtProfileTarget.model_validate(
        {
            "type": "bigquery",
            "project": "my-gcp-project",
            "location": "US",
            "maximum_bytes_billed": 200_000_000,
        }
    )

    adapter = WarehouseAdapter.from_profile(profile)

    assert isinstance(adapter, BigQueryAdapter)
    assert adapter._project == "my-gcp-project"
    assert adapter._location == "US"
    assert adapter._max_bytes_billed == 200_000_000


def test_from_profile_raises_for_unknown_type() -> None:
    """A warehouse the factory does not dispatch must raise
    UnsupportedProfileTypeError.

    DbtProfileTarget.type is typed ``str`` (no Literal narrowing) precisely
    so this dispatch path can reject unsupported warehouses with a typed
    error rather than a Pydantic ValidationError. ``"databricks"`` is used as
    the unsupported example because ``"bigquery"`` / ``"postgres"`` (issue #53)
    / ``"snowflake"`` (issue #119) all now dispatch to a concrete adapter.
    """
    profile = DbtProfileTarget.model_validate({"type": "databricks"})

    with pytest.raises(UnsupportedProfileTypeError) as exc_info:
        WarehouseAdapter.from_profile(profile)

    assert exc_info.value.profile_type == "databricks"


def test_from_profile_uses_default_max_bytes_when_unset() -> None:
    """maximum_bytes_billed=None should fall back to the 100 MB default
    pinned by DEC-019."""
    profile = DbtProfileTarget.model_validate(
        {
            "type": "bigquery",
            "project": "my-gcp-project",
            "maximum_bytes_billed": None,
        }
    )

    adapter = WarehouseAdapter.from_profile(profile)

    assert isinstance(adapter, BigQueryAdapter)
    assert adapter._max_bytes_billed == 100_000_000


def test_from_profile_honours_explicit_zero_max_bytes_billed() -> None:
    """Only ``None`` triggers the default — an explicit ``0`` (or any other
    falsy int) flows through verbatim. Regression for the ``or 100_000_000``
    fallback that overrode explicit zeros (Copilot review feedback)."""
    profile = DbtProfileTarget.model_validate(
        {
            "type": "bigquery",
            "project": "my-gcp-project",
            "maximum_bytes_billed": 0,
        }
    )

    adapter = WarehouseAdapter.from_profile(profile)

    assert isinstance(adapter, BigQueryAdapter)
    assert adapter._max_bytes_billed == 0


def test_from_profile_respects_profile_max_bytes_billed() -> None:
    """An explicit profile value must flow through verbatim — no clamping,
    no rounding, no silent override of user config."""
    profile = DbtProfileTarget.model_validate(
        {
            "type": "bigquery",
            "project": "my-gcp-project",
            "maximum_bytes_billed": 50_000_000,
        }
    )

    adapter = WarehouseAdapter.from_profile(profile)

    assert isinstance(adapter, BigQueryAdapter)
    assert adapter._max_bytes_billed == 50_000_000


# ---------------------------------------------------------------------------
# materialise_sample ABC default-impl contract (DEC-004 / DEC-008 of #22)
# ---------------------------------------------------------------------------


def test_materialise_sample_default_impl_raises_not_supported() -> None:
    """An adapter that does NOT override ``materialise_sample`` inherits
    the ABC default which raises :class:`MaterialisationNotSupportedError`.

    Pins DEC-008 of issue #22: the typed error is what the prune
    orchestrator pattern-matches on to route every candidate to
    ``kept-without-evidence`` (conservative-bias; DEC-009 of #22). The
    error must NOT be ``NotImplementedError`` — the four-tier exit-code
    taxonomy keys on the typed ``WarehouseError`` subclass to map this
    failure to CLI exit tier 3.
    """
    adapter = _StubAdapter()
    # GCP project IDs require 6-30 chars; ``my-project`` is the canonical
    # placeholder used elsewhere in the suite (see test_from_profile_*).
    table = TableRef(project="my-project", dataset="ds", name="t")

    with pytest.raises(MaterialisationNotSupportedError) as exc_info:
        adapter.materialise_sample(table, 1000)

    err = exc_info.value
    # DEC-006 of issue #22 — the remediation text is locked verbatim and
    # consumed by the CLI's stderr formatter; bumping it without bumping
    # this test would silently drift the operator-facing surface.
    assert err.default_remediation == (
        "Set 'prune.sample_strategy: oneshot' in signalforge.yml to fall "
        "back to per-test sampling, or wait for v0.3 multi-warehouse "
        "materialisation support."
    )
    rendered = str(err)
    assert "_StubAdapter" in rendered
    assert "↳ Remediation:" in rendered
    assert "prune.sample_strategy: oneshot" in rendered


def test_materialise_sample_signature_matches_dec_004() -> None:
    """The published signature is ``materialise_sample(table, n, *,
    partition_filter=None, ttl_seconds=3600) -> TableRef`` (DEC-004 of #22).

    Locks each load-bearing detail:
    * ``table`` and ``n`` are positional (POSITIONAL_OR_KEYWORD).
    * The ``*`` separator forces ``partition_filter`` and ``ttl_seconds``
      to be keyword-only — so a downstream caller cannot accidentally swap
      a positional ``n`` for a positional ``ttl_seconds``.
    * Defaults: ``partition_filter=None``, ``ttl_seconds=3600``.
    * Return annotation: :class:`TableRef`.

    Drift on any of these would silently change the public contract; the
    test catches it before a v0.2 BigQuery / v0.3 Snowflake override
    diverges from the ABC.
    """
    sig = inspect.signature(WarehouseAdapter.materialise_sample)
    params = sig.parameters

    assert list(params.keys()) == [
        "self",
        "table",
        "n",
        "partition_filter",
        "ttl_seconds",
    ]

    table_param = params["table"]
    n_param = params["n"]
    assert table_param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert table_param.default is inspect.Parameter.empty
    assert n_param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert n_param.default is inspect.Parameter.empty

    pf_param = params["partition_filter"]
    ttl_param = params["ttl_seconds"]
    assert pf_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert pf_param.default is None
    assert ttl_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert ttl_param.default == 3600

    # Return annotation must point at TableRef (DEC-004). Compare against
    # both the resolved class and the string form so the assertion holds
    # whether ``from __future__ import annotations`` deferred evaluation
    # or not.
    return_annotation = sig.return_annotation
    assert return_annotation is TableRef or return_annotation == "TableRef"
