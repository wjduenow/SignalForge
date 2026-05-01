"""Prune-layer config loader.

Loads the ``prune:`` top-level block from ``signalforge.yml`` into a typed
:class:`PruneConfig` (scope, sample_size, test_timeout_seconds,
total_budget_seconds, capture_failure_rows, trusted_models,
partition_filter). Outer file wrapper uses ``extra="ignore"`` so sibling
stage namespaces (``safety:``, ``llm:``, future ``grade:``) don't break
the loader; the inner :class:`PruneConfig` block uses ``extra="forbid"``
so a typo like ``scop:`` fails loud rather than silently no-op'ing.

Mirrors :mod:`signalforge.draft.config` and :mod:`signalforge.safety.config`.

Design commitments operationalised here:

* **DEC-009** — Defaults trace to plan Phase-1 housekeeping:
  ``scope="sample"``, ``sample_size=100_000``, ``test_timeout_seconds=30``,
  ``total_budget_seconds=600``, ``capture_failure_rows=3``,
  ``trusted_models=()``, ``partition_filter=None``.
* **DEC-015** — Config-shaped models use ``extra="forbid"`` so typos in
  ``signalforge.yml`` fail loud (mirrors ``safety-layer.md`` DEC-015 and
  ``llm-drafter.md`` DEC-027). Read-back / response-shaped models use
  ``extra="ignore"``.
* **DEC-020** — ``signalforge.yml`` top-level namespace key for this
  layer is ``prune:``. Other top-level keys (``safety:``, ``llm:``,
  future ``grade:``) are reserved for other stages and silently ignored
  by this loader.

Resolution:

* ``path=None`` or path does not exist → return :class:`PruneConfig`
  defaults silently.
* File present but ``prune:`` key absent or null → return defaults.
* ``prune:`` block well-formed → return the populated
  :class:`PruneConfig`.
* Unknown / typo'd inner field, non-mapping ``prune:`` block, YAML parse
  failure, or :class:`pydantic.ValidationError` from
  :class:`PruneConfig` → :class:`signalforge.prune.errors.PruneConfigError`
  with the underlying exception preserved on ``__cause__``.

Trusted-models validation against the manifest is NOT performed here
(DEC-008): the manifest isn't loaded yet at config-load time. That check
runs at :func:`signalforge.prune.prune_tests` entry — see US-009.

``yaml.safe_load`` only — ``yaml.load`` accepts arbitrary Python object
construction tags and is unsafe for any input we don't fully control.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from signalforge.prune.errors import PruneConfigError
from signalforge.warehouse.models import PartitionFilter


class PruneConfig(BaseModel):
    """User-facing knobs for the prune layer (DEC-009).

    Lives under the ``prune:`` top-level key in ``signalforge.yml``
    (DEC-020). Config-shaped per ``safety-layer.md`` DEC-015:
    ``extra="forbid"`` so typos like ``scop:`` instead of ``scope:`` fail
    loud rather than silently no-op'ing. The :class:`_PruneConfigFile`
    outer wrapper uses ``extra="ignore"`` so other top-level keys
    (``safety:``, ``llm:``, ...) reserved by DEC-020 don't trip the
    strict validator.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    scope: Literal["sample", "full"] = "sample"
    """Whether candidate tests run against a deterministic warehouse
    sample (``"sample"``, the default) or the full table (``"full"``)."""

    sample_size: int = 100_000
    """Target row count for ``"sample"`` scope. Passed to
    :meth:`signalforge.warehouse.WarehouseAdapter.sample_rows`."""

    test_timeout_seconds: int = 30
    """Per-test wall-clock budget. Adapter cancels the in-flight query
    when this elapses; the orchestrator routes the test to
    ``kept-without-evidence``."""

    total_budget_seconds: int = 600
    """Whole-run wall-clock budget. When exceeded the orchestrator drains
    every remaining test to ``kept-without-evidence`` (DEC-011)."""

    capture_failure_rows: int = 3
    """Number of failing rows to capture per failed test (matches the
    :class:`signalforge.warehouse.TestResult` cap)."""

    trusted_models: tuple[str, ...] = ()
    """Manifest ``unique_id``s whose data is treated as known-clean
    (Q1=B, opt-in only). A failure on a trusted model surfaces as a real
    failure rather than the "drop on clean-data failure" branch.
    Validated against the manifest at :func:`prune_tests` entry per
    DEC-008 — NOT here."""

    partition_filter: PartitionFilter | None = None
    """Optional :class:`PartitionFilter` scoping every sample query
    (DEC-009). Required by the warehouse adapter for tables ≥ 100M rows;
    otherwise optional. Pydantic recursively validates the YAML mapping
    into the typed ADT."""

    @field_validator("sample_size", "test_timeout_seconds", "total_budget_seconds")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator("capture_failure_rows")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be non-negative")
        return v


class _PruneConfigFile(BaseModel):
    """Outer wrapper for the ``signalforge.yml`` top-level mapping.

    ``extra="ignore"`` at this level — other top-level keys (``safety:``,
    ``llm:``, ``grade:``, ...) are reserved for other stages per DEC-020
    and must not trigger a prune-layer validation error. The strict
    ``extra="forbid"`` lives on :class:`PruneConfig` itself.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    prune: PruneConfig | None = None


def load_prune_config(path: Path | str | None = None) -> PruneConfig:
    """Load a :class:`PruneConfig` from ``signalforge.yml``.

    See module docstring for the full resolution order and error
    contract.

    Args:
        path: Optional explicit config-file path. ``None`` or a path that
            does not exist returns :class:`PruneConfig` defaults
            silently.

    Returns:
        A fully-validated :class:`PruneConfig`. When the file is absent,
        empty, or the ``prune:`` key is missing, the defaults from
        DEC-009 apply.

    Raises:
        PruneConfigError: The file is not valid YAML, its top level is
            not a mapping, the ``prune:`` block is not a mapping, or the
            contents fail :class:`PruneConfig` validation (typo, unknown
            ``scope`` value, non-positive ``test_timeout_seconds`` /
            ``total_budget_seconds`` / ``sample_size``, ...). The
            original :class:`pydantic.ValidationError` (if any) is
            preserved on ``__cause__``.
    """
    if path is None:
        return PruneConfig()

    config_file = Path(path)
    if not config_file.exists():
        return PruneConfig()

    raw_text = config_file.read_text(encoding="utf-8").strip()
    if not raw_text:
        return PruneConfig()

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise PruneConfigError(
            f"signalforge.yml is not valid YAML: {exc}",
        ) from exc

    if loaded is None:
        # File parses to None (e.g. only comments) — same as empty.
        return PruneConfig()

    if not isinstance(loaded, dict):
        raise PruneConfigError(
            f"signalforge.yml top level must be a mapping; got {type(loaded).__name__}",
        )

    if "prune" not in loaded or loaded["prune"] is None:
        # Missing `prune:` key (or `prune:` with null value) — other
        # top-level keys reserved per DEC-020 namespace.
        return PruneConfig()

    prune_block = loaded["prune"]
    if not isinstance(prune_block, dict):
        raise PruneConfigError(
            f"signalforge.yml: 'prune' must be a mapping; got {type(prune_block).__name__}",
        )

    try:
        wrapper = _PruneConfigFile.model_validate({"prune": prune_block})
    except ValidationError as exc:
        raise PruneConfigError(
            f"signalforge.yml: 'prune' block failed schema validation: {exc}",
        ) from exc

    # `wrapper.prune` is non-None here because we already filtered the
    # missing-key branch above; assert for the type checker.
    assert wrapper.prune is not None
    return wrapper.prune


__all__ = ["PruneConfig", "load_prune_config"]
