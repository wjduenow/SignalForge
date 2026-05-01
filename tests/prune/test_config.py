"""Tests for :mod:`signalforge.prune.config` (US-005).

Covers the resolution + validation contract of :func:`load_prune_config`:

* ``path=None`` or missing path → :class:`PruneConfig` defaults silently
  (DEC-009).
* Empty / minimal ``prune:`` blocks → defaults.
* Full round-trip with non-default values for every field.
* Typo'd inner field (``scop:``) → :class:`PruneConfigError` (DEC-015,
  ``extra="forbid"`` on the inner config).
* Sibling top-level keys (``safety:``, ``llm:``) → tolerated by the
  outer wrapper (``extra="ignore"`` per DEC-020 namespace reservation).
* ``partition_filter`` mapping → typed
  :class:`signalforge.warehouse.PartitionFilter`.
* ``yaml.safe_load`` rejects classic Python-object-construction gadgets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.prune.config import PruneConfig, load_prune_config
from signalforge.prune.errors import PruneConfigError
from signalforge.warehouse.models import PartitionFilter

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "prune"


def test_load_prune_config_path_none_returns_defaults() -> None:
    """``load_prune_config(None)`` returns :class:`PruneConfig` defaults
    silently — no log, no error, every field at its DEC-009 default."""
    result = load_prune_config(None)

    assert result == PruneConfig()
    assert result.scope == "sample"
    assert result.sample_size == 100_000
    assert result.test_timeout_seconds == 30
    assert result.total_budget_seconds == 600
    assert result.capture_failure_rows == 3
    assert result.trusted_models == ()
    assert result.partition_filter is None


def test_load_prune_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    """A path that does not exist returns defaults without raising —
    parity with :func:`load_draft_config` and
    :func:`load_safety_config` for the implicit-config-file case."""
    missing = tmp_path / "missing.yml"
    assert not missing.exists()

    result = load_prune_config(missing)

    assert result == PruneConfig()


def test_load_prune_config_full_round_trip() -> None:
    """A populated ``prune:`` block round-trips into a
    :class:`PruneConfig` whose every field matches the YAML."""
    result = load_prune_config(_FIXTURES / "signalforge_full.yml")

    assert result.scope == "full"
    assert result.sample_size == 50_000
    assert result.test_timeout_seconds == 60
    assert result.total_budget_seconds == 1200
    assert result.capture_failure_rows == 5
    assert result.trusted_models == (
        "model.shop.dim_customers",
        "model.shop.fact_orders",
    )
    assert result.partition_filter is None


def test_load_prune_config_minimal_block_returns_defaults() -> None:
    """An empty ``prune: {}`` mapping validates as defaults — every
    field is optional with a default per DEC-009."""
    result = load_prune_config(_FIXTURES / "signalforge_minimal.yml")

    assert result == PruneConfig()


def test_load_prune_config_typo_raises() -> None:
    """A typo'd inner field (``scop:`` instead of ``scope:``) raises
    :class:`PruneConfigError`. ``PruneConfig`` uses ``extra="forbid"``
    per DEC-015 so silent no-op is impossible."""
    with pytest.raises(PruneConfigError) as excinfo:
        load_prune_config(_FIXTURES / "signalforge_typo.yml")

    # The wrapped pydantic ValidationError surfaces the offending key
    # name in the error message — the orchestrator / CLI uses this
    # message to point the operator at the typo.
    assert "scop" in str(excinfo.value)


def test_load_prune_config_tolerates_sibling_blocks() -> None:
    """Sibling top-level keys (``safety:``, ``llm:``) are reserved for
    other stages per DEC-020 and silently ignored by the prune
    loader."""
    result = load_prune_config(_FIXTURES / "signalforge_with_siblings.yml")

    assert result.scope == "sample"
    assert result.sample_size == 25_000
    # Other fields fall back to defaults:
    assert result.test_timeout_seconds == 30
    assert result.total_budget_seconds == 600


def test_load_prune_config_parses_partition_filter() -> None:
    """``prune.partition_filter`` (a YAML mapping) is recursively
    validated into a typed :class:`PartitionFilter` ADT — Pydantic
    handles the dict→dataclass conversion."""
    result = load_prune_config(_FIXTURES / "signalforge_partition.yml")

    assert isinstance(result.partition_filter, PartitionFilter)
    assert result.partition_filter.column == "event_dt"
    assert result.partition_filter.op == ">="
    assert result.partition_filter.value == "2026-01-01"


def test_load_prune_config_yaml_safe_load_rejects_python_objects() -> None:
    """A fixture containing a ``!!python/object/apply:os.system [...]``
    gadget raises rather than executing it — confirms the loader uses
    :func:`yaml.safe_load`, not :func:`yaml.load` (which honours
    arbitrary Python object construction tags)."""
    with pytest.raises(PruneConfigError):
        load_prune_config(_FIXTURES / "signalforge_unsafe.yml")
