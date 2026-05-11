"""Model-price table for SignalForge's ``--estimate`` cost preview (US-001
of issue #36).

This module is the single source of truth for per-model USD math used by
``signalforge generate --estimate`` (and reusable by v0.2 cost-projection
callers). It ships:

* :data:`PRICE_TABLE_VERSION` — sourcing-date stamp for the table. Bump
  whenever Anthropic publishes a price change; reproducibility callers
  read this off the rendered estimate to confirm "what prices were in
  effect when this report was generated."
* :class:`ModelPricing` — ``@dataclass(frozen=True, slots=True)`` carrying
  four ``float`` per-MTok USD fields. ``frozen=True`` is load-bearing:
  same input → same USD math, every time. Field assignment raises
  :class:`dataclasses.FrozenInstanceError`.
* :data:`PRICES` — immutable mapping (``types.MappingProxyType``) from SKU
  string to :class:`ModelPricing`. v0.1 covers the three Anthropic SKUs
  this project supports.
* :func:`lookup` — the public access seam; raises
  :class:`signalforge.llm.errors.EstimateUnknownModelError` (CLI tier 2 —
  input-validation) on miss rather than returning ``None``, so downstream
  callers don't need to defensively branch.

Note on prices. The four fields encode Anthropic's public per-million-token
pricing for the message-batches / standard channel as of
:data:`PRICE_TABLE_VERSION`:

* ``input_per_mtok``        — non-cached input tokens.
* ``output_per_mtok``       — output (assistant) tokens.
* ``cache_write_5m_per_mtok`` — write to the 5-minute cache tier (the only
  tier SignalForge uses; v0.2 may add the 1-hour tier).
* ``cache_read_per_mtok``    — read a previously-cached block.

The table is a snapshot, not a live feed. Bumping
:data:`PRICE_TABLE_VERSION` is a deliberate refresh that should land in a
single commit alongside the numeric edits so audit JSONLs link back to a
specific table version.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from signalforge.llm.errors import EstimateUnknownModelError

__all__ = [
    "PRICE_TABLE_VERSION",
    "PRICES",
    "ModelPricing",
    "lookup",
]


PRICE_TABLE_VERSION: str = "2026-05-11"
"""Sourcing-date stamp for :data:`PRICES`. Bump alongside any numeric edit."""


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Per-million-token USD prices for one Anthropic SKU.

    ``frozen=True`` is the reproducibility invariant — once constructed,
    a :class:`ModelPricing` instance cannot mutate. ``slots=True`` keeps
    the instance memory footprint minimal (no ``__dict__``) and trips
    accidental attribute assignment loudly.

    All four fields are USD per million tokens. v0.1 supports only the
    5-minute cache tier; v0.2 may add a ``cache_write_1h_per_mtok`` field
    when the longer TTL becomes part of the SignalForge cache strategy.
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_write_5m_per_mtok: float
    cache_read_per_mtok: float


# TODO: verify v0.1 ships with current pricing (refresh PRICE_TABLE_VERSION
# above when these numbers change). Values below mirror Anthropic's
# publicly published per-MTok pricing for the standard channel as of the
# table version stamp; the Sonnet-family numbers are the canonical
# reference point. Apply a deliberate refresh commit per the
# "5-surface parity" rule in prune-engine.md when bumping any field.
_PRICES_MUTABLE: dict[str, ModelPricing] = {
    "claude-sonnet-4-6": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_write_5m_per_mtok=3.75,
        cache_read_per_mtok=0.30,
    ),
    "claude-opus-4-7": ModelPricing(
        input_per_mtok=15.00,
        output_per_mtok=75.00,
        cache_write_5m_per_mtok=18.75,
        cache_read_per_mtok=1.50,
    ),
    "claude-haiku-4-5": ModelPricing(
        input_per_mtok=0.80,
        output_per_mtok=4.00,
        cache_write_5m_per_mtok=1.00,
        cache_read_per_mtok=0.08,
    ),
}

PRICES: Mapping[str, ModelPricing] = MappingProxyType(_PRICES_MUTABLE)
"""Read-only mapping from Anthropic SKU to :class:`ModelPricing`.

``MappingProxyType`` is the standard-library immutable-mapping wrapper:
callers can iterate and look up entries, but cannot mutate the table
(``PRICES["x"] = ...`` raises :class:`TypeError`). The reproducibility
contract is "same SKU → same prices every time the table version stamp
is unchanged."
"""


def lookup(model: str) -> ModelPricing:
    """Return the :class:`ModelPricing` entry for ``model``.

    Raises :class:`EstimateUnknownModelError` (CLI tier 2) when ``model``
    is not in :data:`PRICES`. The remediation locked on
    :class:`EstimateUnknownModelError.default_remediation` points the
    operator at either adding the SKU or picking one of the three v0.1
    supported models.

    Returning the typed exception (rather than ``None`` or a sentinel)
    keeps downstream callers free of defensive branching — the
    ``--estimate`` engine assumes a successful ``lookup`` and lets the
    CLI's top-level ``try / except`` ladder render the typed error if
    the table doesn't know the SKU.
    """
    try:
        return PRICES[model]
    except KeyError as exc:
        raise EstimateUnknownModelError(model=model) from exc
