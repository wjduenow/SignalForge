"""Tests for :mod:`signalforge.prune.config` (US-005).

Covers the resolution + validation contract of :func:`load_prune_config`:

* ``path=None`` with no ``<project_dir>/signalforge.yml`` →
  :class:`PruneConfig` defaults silently (DEC-009).
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

import shutil
from pathlib import Path

import pytest

from signalforge.prune.config import PruneConfig, load_prune_config
from signalforge.prune.errors import PruneConfigError
from signalforge.warehouse.models import PartitionFilter

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "prune"


def _copy_fixture_as_default(fixture_name: str, project_dir: Path) -> None:
    """Copy ``_FIXTURES / fixture_name`` to ``project_dir/signalforge.yml``.

    Used by tests that exercise the default-resolution path
    (``load_prune_config(project_dir)``).
    """
    shutil.copy(_FIXTURES / fixture_name, project_dir / "signalforge.yml")


def test_load_prune_config_no_file_returns_defaults(tmp_path: Path) -> None:
    """``load_prune_config(project_dir)`` with no ``signalforge.yml`` in
    ``project_dir`` returns :class:`PruneConfig` defaults silently — no
    log, no error, every field at its DEC-009 default."""
    result = load_prune_config(tmp_path)

    assert result == PruneConfig()
    assert result.scope == "sample"
    assert result.sample_size == 100_000
    assert result.test_timeout_seconds == 30
    assert result.total_budget_seconds == 600
    assert result.capture_failure_rows == 3
    assert result.trusted_models == ()
    assert result.partition_filter is None


def test_load_prune_config_missing_explicit_path_returns_defaults(tmp_path: Path) -> None:
    """An explicit ``path=`` that does not exist returns defaults
    without raising — parity with the project-default branch."""
    missing = tmp_path / "missing.yml"
    assert not missing.exists()

    result = load_prune_config(tmp_path, missing)

    assert result == PruneConfig()


def test_load_prune_config_full_round_trip(tmp_path: Path) -> None:
    """A populated ``prune:`` block round-trips into a
    :class:`PruneConfig` whose every field matches the YAML."""
    result = load_prune_config(tmp_path, _FIXTURES / "signalforge_full.yml")

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


def test_load_prune_config_default_path_resolves_relative_to_project_dir(
    tmp_path: Path,
) -> None:
    """``path=None`` resolves to ``<project_dir>/signalforge.yml`` —
    parity with :func:`load_draft_config` and the safety-layer loader."""
    _copy_fixture_as_default("signalforge_full.yml", tmp_path)

    result = load_prune_config(tmp_path)

    assert result.scope == "full"
    assert result.sample_size == 50_000


def test_load_prune_config_minimal_block_returns_defaults(tmp_path: Path) -> None:
    """An empty ``prune: {}`` mapping validates as defaults — every
    field is optional with a default per DEC-009."""
    result = load_prune_config(tmp_path, _FIXTURES / "signalforge_minimal.yml")

    assert result == PruneConfig()


def test_load_prune_config_typo_raises(tmp_path: Path) -> None:
    """A typo'd inner field (``scop:`` instead of ``scope:``) raises
    :class:`PruneConfigError`. ``PruneConfig`` uses ``extra="forbid"``
    per DEC-015 so silent no-op is impossible."""
    with pytest.raises(PruneConfigError) as excinfo:
        load_prune_config(tmp_path, _FIXTURES / "signalforge_typo.yml")

    # The wrapped pydantic ValidationError surfaces the offending key
    # name in the error message — the orchestrator / CLI uses this
    # message to point the operator at the typo.
    assert "scop" in str(excinfo.value)


def test_load_prune_config_tolerates_sibling_blocks(tmp_path: Path) -> None:
    """Sibling top-level keys (``safety:``, ``llm:``) are reserved for
    other stages per DEC-020 and silently ignored by the prune
    loader."""
    result = load_prune_config(tmp_path, _FIXTURES / "signalforge_with_siblings.yml")

    assert result.scope == "sample"
    assert result.sample_size == 25_000
    # Other fields fall back to defaults:
    assert result.test_timeout_seconds == 30
    assert result.total_budget_seconds == 600


def test_load_prune_config_parses_partition_filter(tmp_path: Path) -> None:
    """``prune.partition_filter`` (a YAML mapping) is recursively
    validated into a typed :class:`PartitionFilter` ADT — Pydantic
    handles the dict→dataclass conversion."""
    result = load_prune_config(tmp_path, _FIXTURES / "signalforge_partition.yml")

    assert isinstance(result.partition_filter, PartitionFilter)
    assert result.partition_filter.column == "event_dt"
    assert result.partition_filter.op == ">="
    assert result.partition_filter.value == "2026-01-01"


def test_load_prune_config_yaml_safe_load_rejects_python_objects(tmp_path: Path) -> None:
    """Confirms :func:`yaml.safe_load` is used (not :func:`yaml.load`).

    Under unsafe ``yaml.load`` / ``yaml.unsafe_load``, the
    ``!!python/object/apply:os.system`` gadget invokes ``os.system``
    with a ``touch <marker>`` argument, creating an observable
    side-effect file before validation runs. ``yaml.safe_load``
    rejects the construction tag at parse time, so the marker file
    MUST NOT exist after the call. The test asserts both the typed
    error AND the absence of the marker so a future regression that
    swaps in unsafe ``yaml.load`` fails loud (the prior version of
    this test would have passed in either case because the validator
    rejected the resulting ``scope=0`` regardless of whether the
    gadget executed).
    """
    marker = tmp_path / "pwned"
    config_yml = tmp_path / "signalforge.yml"
    # `os.system("touch <marker>")` returns 0; under unsafe yaml.load
    # the marker file gets created as a side effect AND the parsed
    # scope becomes the int 0 (which then fails the Literal["sample",
    # "full"] validator → PruneConfigError). Under safe yaml.load,
    # the construction tag is rejected at parse time → still
    # PruneConfigError, BUT the marker is not created.
    config_yml.write_text(f'prune:\n  scope: !!python/object/apply:os.system ["touch {marker}"]\n')

    with pytest.raises(PruneConfigError):
        load_prune_config(tmp_path, config_yml)
    assert not marker.exists(), "yaml.load executed the gadget — safe_load is NOT in use"
