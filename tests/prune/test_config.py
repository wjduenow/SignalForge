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


def test_prune_config_accepts_oneshot_and_materialised_literals() -> None:
    """``sample_strategy`` accepts the two locked Literal values
    (``"oneshot"`` and ``"materialised"``) — Q7 / DEC-006 of #22.

    This is the foundation field for issue #22's temp-table sampling:
    ``"materialised"`` (the default) routes the engine through a single
    per-run `materialise_sample` call; ``"oneshot"`` falls back to the
    v0.1 per-test `sample_rows` path. Adding a third literal value is a
    contract break — the strict mirror in
    ``tests/prune/test_drift_detector.py`` will fail loud."""
    materialised = PruneConfig(sample_strategy="materialised")
    assert materialised.sample_strategy == "materialised"

    oneshot = PruneConfig(sample_strategy="oneshot")
    assert oneshot.sample_strategy == "oneshot"


def test_prune_config_rejects_typo_in_sample_strategy() -> None:
    """``sample_strategy`` is a ``Literal["oneshot", "materialised"]`` —
    a US-spelling typo (``"materialized"`` with a ``z``) MUST fail loud
    per ``safety-layer.md`` DEC-015 (``extra="forbid"`` is the loader's
    contract; the Literal validator is the field's contract).

    Silently accepting ``"materialized"`` would route through the
    fallback ``"oneshot"`` path under any future `if strategy ==
    "materialised":` dispatch — exactly the silent-no-op failure mode
    DEC-015 exists to prevent."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        PruneConfig(sample_strategy="materialized")  # type: ignore[arg-type]
    # The Literal validator surfaces the offending value.
    assert "materialized" in str(excinfo.value)


def test_prune_config_default_sample_strategy_is_materialised() -> None:
    """``PruneConfig().sample_strategy`` defaults to ``"materialised"``
    per Q7 of plans/super/22-temp-table-sample.md — the issue's stated
    default. Pinning the default in a test guards against an accidental
    flip when a future reviewer "tidies" the field declaration."""
    assert PruneConfig().sample_strategy == "materialised"


def test_load_prune_config_handles_v01_yaml_without_sample_strategy_field(
    tmp_path: Path,
) -> None:
    """A v0.1-shaped ``signalforge.yml`` (no ``sample_strategy:`` key
    under ``prune:``) loads cleanly with ``sample_strategy`` defaulting
    to ``"materialised"`` — backward-compat with every operator
    pre-issue-#22.

    The ``signalforge_full.yml`` fixture predates this story and
    therefore omits the new field; loading it MUST not raise and MUST
    surface the DEC-006 default."""
    result = load_prune_config(tmp_path, _FIXTURES / "signalforge_full.yml")

    assert result.sample_strategy == "materialised"
    # Sanity: the rest of the fixture still round-trips.
    assert result.scope == "full"
    assert result.sample_size == 50_000


def test_load_prune_config_enabled_defaults_to_true_when_field_absent(
    tmp_path: Path,
) -> None:
    """A v0.1-shaped ``signalforge.yml`` (no ``enabled:`` key under
    ``prune:``) loads cleanly with ``enabled`` defaulting to ``True``
    — issue #35 / US-001, DEC-005.

    Mirrors the backward-compat invariant from
    :func:`test_load_prune_config_handles_v01_yaml_without_sample_strategy_field`
    for the v0.2 ``sample_strategy`` graduation: every operator's
    existing config keeps the load-bearing default posture
    (signal-over-volume — Architectural Commitment #1) when the new
    field is absent."""
    config_yml = tmp_path / "signalforge.yml"
    config_yml.write_text("prune:\n  scope: sample\n")

    result = load_prune_config(tmp_path)

    assert result.enabled is True
    # Sanity: the rest of the config still round-trips cleanly.
    assert result.scope == "sample"


def test_load_prune_config_enabled_false_when_explicitly_set(tmp_path: Path) -> None:
    """``prune.enabled: false`` in ``signalforge.yml`` round-trips as
    ``PruneConfig().enabled is False`` — issue #35 / US-001, DEC-005.

    This is the escape-hatch path: the operator explicitly opts out of
    the prune layer's warehouse calls. The orchestrator's short-circuit
    (US-002) keys on this value; pinning the YAML→bool round-trip here
    is the contract surface that lets US-002 trust the loader."""
    config_yml = tmp_path / "signalforge.yml"
    config_yml.write_text("prune:\n  enabled: false\n")

    result = load_prune_config(tmp_path)

    assert result.enabled is False


def test_load_prune_config_enabled_typo_raises_config_error(tmp_path: Path) -> None:
    """A typo'd ``enabld:`` (vs ``enabled:``) raises
    :class:`PruneConfigError` per the existing ``extra="forbid"``
    contract on :class:`PruneConfig` (DEC-015).

    Silent no-op (the typo is ignored, the field falls back to the
    default ``True``) is exactly the failure mode DEC-015 exists to
    prevent: the operator believes they disabled prune, then sees
    warehouse bytes-billed and reasonably distrusts the tool.
    ``extra="forbid"`` on the inner :class:`PruneConfig` block makes
    this loud rather than silent — issue #35 / US-001."""
    config_yml = tmp_path / "signalforge.yml"
    config_yml.write_text("prune:\n  enabld: false\n")

    with pytest.raises(PruneConfigError) as excinfo:
        load_prune_config(tmp_path)

    # The wrapped pydantic ValidationError surfaces the offending key
    # name in the error message — same shape as the existing `scop:`
    # typo regression test.
    assert "enabld" in str(excinfo.value)


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
