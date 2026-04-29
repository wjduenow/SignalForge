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
"""

from __future__ import annotations

import pytest

from signalforge.warehouse.adapters.bigquery import BigQueryAdapter
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import UnsupportedProfileTypeError
from signalforge.warehouse.profiles import DbtProfileTarget


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
    """Anything other than 'bigquery' must raise UnsupportedProfileTypeError.

    DbtProfileTarget.type is typed ``str`` (no Literal narrowing) precisely
    so this dispatch path can reject unsupported warehouses with a typed
    error rather than a Pydantic ValidationError.
    """
    profile = DbtProfileTarget.model_validate({"type": "snowflake"})

    with pytest.raises(UnsupportedProfileTypeError) as exc_info:
        WarehouseAdapter.from_profile(profile)

    assert exc_info.value.profile_type == "snowflake"


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
