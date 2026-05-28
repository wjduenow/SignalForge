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
  string to :class:`ModelPricing`. Covers three Anthropic SKUs +
  four OpenAI SKUs (added in #136 US-004 per DEC-007 of the
  provider-neutral grading-provider plan).
* :func:`lookup` — the public access seam; raises
  :class:`signalforge.llm.errors.EstimateUnknownModelError` (CLI tier 2 —
  input-validation) on miss rather than returning ``None``, so downstream
  callers don't need to defensively branch.

Note on prices. The four fields encode each provider's public
per-million-token pricing for the message-batches / standard channel
(Anthropic) or the standard chat-completions channel (OpenAI) as of
:data:`PRICE_TABLE_VERSION`. OpenAI does not currently expose a
prompt-cache discount tier comparable to Anthropic's
``cache_control``, so the two cache fields on OpenAI SKUs are
``0.0`` (no discount, no premium):

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


PRICE_TABLE_VERSION: str = "2026-05-28"
"""Sourcing-date stamp for :data:`PRICES`. Bump alongside any numeric edit."""


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Per-million-token USD prices for one provider SKU (Anthropic or OpenAI).

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


# TODO: verify each row ships with current pricing (refresh
# PRICE_TABLE_VERSION above when these numbers change). Anthropic values
# mirror the publicly published per-MTok pricing for the standard channel
# as of the table version stamp; the Sonnet-family numbers are the
# canonical reference point. OpenAI values are calibration figures
# captured from OpenAI's public price page at PR-prep time (see
# #136 US-004); operators should treat them as a sanity-check baseline
# rather than a billing guarantee — bump PRICE_TABLE_VERSION any time
# they're refreshed. OpenAI does not currently expose a prompt-cache
# discount tier comparable to Anthropic's `cache_control`, so the two
# OpenAI cache fields are 0.0 (no discount, no premium). Apply a
# deliberate refresh commit per the "5-surface parity" rule in
# prune-engine.md when bumping any field.
_PRICES_MUTABLE: dict[str, ModelPricing] = {
    # -- Anthropic SKUs ------------------------------------------------
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
    # -- OpenAI SKUs (#136 US-004, DEC-007) ----------------------------
    # `gpt-4o` is the default judge per DEC-004; the other three are
    # supported back-/cross-compat options. Cache fields are 0.0 — OpenAI
    # has no Anthropic-equivalent `cache_control` discount tier.
    "gpt-4o": ModelPricing(
        input_per_mtok=2.50,
        output_per_mtok=10.00,
        cache_write_5m_per_mtok=0.0,
        cache_read_per_mtok=0.0,
    ),
    "gpt-4o-mini": ModelPricing(
        input_per_mtok=0.15,
        output_per_mtok=0.60,
        cache_write_5m_per_mtok=0.0,
        cache_read_per_mtok=0.0,
    ),
    "gpt-4.1": ModelPricing(
        input_per_mtok=2.00,
        output_per_mtok=8.00,
        cache_write_5m_per_mtok=0.0,
        cache_read_per_mtok=0.0,
    ),
    "gpt-4-turbo": ModelPricing(
        input_per_mtok=10.00,
        output_per_mtok=30.00,
        cache_write_5m_per_mtok=0.0,
        cache_read_per_mtok=0.0,
    ),
    # -- Gemini SKUs (#137 US-006, DEC-017) ----------------------------
    # `gemini-2.5-flash` is the documented mid-tier judge per DEC-004
    # of #137; `gemini-2.5-pro` is the flagship; `gemini-2.0-flash` is
    # the budget option. Cache fields are 0.0 — v0.3 Gemini ships
    # without prompt caching (DEC-003). Per-Mtok USD figures from
    # Google's public Gemini API pricing at PR-prep time
    # (2026-05-27); `gemini-2.5-pro` figures are the base ≤200K
    # context tier.
    "gemini-2.5-pro": ModelPricing(
        input_per_mtok=1.25,
        output_per_mtok=10.00,
        cache_write_5m_per_mtok=0.0,
        cache_read_per_mtok=0.0,
    ),
    "gemini-2.5-flash": ModelPricing(
        input_per_mtok=0.30,
        output_per_mtok=2.50,
        cache_write_5m_per_mtok=0.0,
        cache_read_per_mtok=0.0,
    ),
    "gemini-2.0-flash": ModelPricing(
        input_per_mtok=0.10,
        output_per_mtok=0.40,
        cache_write_5m_per_mtok=0.0,
        cache_read_per_mtok=0.0,
    ),
}

PRICES: Mapping[str, ModelPricing] = MappingProxyType(_PRICES_MUTABLE)
"""Read-only mapping from provider SKU (Anthropic or OpenAI) to :class:`ModelPricing`.

``MappingProxyType`` is the standard-library immutable-mapping wrapper:
callers can iterate and look up entries, but cannot mutate the table
(``PRICES["x"] = ...`` raises :class:`TypeError`). The reproducibility
contract is "same SKU → same prices every time the table version stamp
is unchanged."
"""

# QG pass 1 I-3 — drop the mutable binding once the read-only proxy is
# built. Without this, ``signalforge.llm.pricing._PRICES_MUTABLE["x"] = ...``
# would mutate the table through the proxy (the proxy is a view, not a
# copy). Deleting the name keeps ``MappingProxyType`` as the only handle.
del _PRICES_MUTABLE


def lookup(model: str) -> ModelPricing:
    """Return the :class:`ModelPricing` entry for ``model``.

    Raises :class:`EstimateUnknownModelError` (CLI tier 2) when ``model``
    is not in :data:`PRICES`. The remediation locked on
    :class:`EstimateUnknownModelError.default_remediation` points the
    operator at either adding the SKU or picking one of the supported
    models (three Anthropic SKUs + four OpenAI SKUs as of #136 US-004).

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
