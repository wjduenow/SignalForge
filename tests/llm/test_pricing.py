"""Unit tests for the LLM pricing module (US-001 of issue #36).

Mirrors :mod:`tests.llm.test_errors` and :mod:`tests.llm.test_models`. Every
test is capable of failing (``testing-signal.md``); no ``assert True``-shaped
placeholders.

This module is TDD-first: tests written before the production module exists.
The price table is the single source of truth for per-model USD math used by
``--estimate`` (and reusable by v0.2 cost-projection callers).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from signalforge.llm import (
    PRICE_TABLE_VERSION,
    PRICES,
    EstimateUnknownModelError,
    ModelPricing,
    lookup,
)


def test_lookup_returns_modelpricing_for_known_model_claude_sonnet_4_6() -> None:
    """``lookup`` returns a ``ModelPricing`` instance with non-zero fields
    for every shipped SKU. v0.1 ships three; this test pins
    ``claude-sonnet-4-6`` as the default-model representative.
    """
    pricing = lookup("claude-sonnet-4-6")
    assert isinstance(pricing, ModelPricing)
    assert pricing.input_per_mtok > 0.0
    assert pricing.output_per_mtok > 0.0
    assert pricing.cache_write_5m_per_mtok > 0.0
    assert pricing.cache_read_per_mtok > 0.0


def test_lookup_raises_estimateunknownmodelerror_for_unknown_model() -> None:
    """``lookup`` raises ``EstimateUnknownModelError`` carrying the model id
    and the locked remediation when handed an unknown SKU."""
    with pytest.raises(EstimateUnknownModelError) as exc_info:
        lookup("not-a-real-model-9999")
    rendered = str(exc_info.value)
    assert "not-a-real-model-9999" in rendered
    # Locked remediation text — verbatim per US-001 AC, refreshed in
    # #136 US-008 (QG) to enumerate the four OpenAI SKUs added by US-004,
    # and again in #137 US-006 (DEC-017) for the three Gemini SKUs.
    assert (
        "Add the model to signalforge.llm.pricing.PRICES or use a supported "
        "model: claude-sonnet-4-6, claude-opus-4-7, claude-haiku-4-5, "
        "gpt-4o, gpt-4o-mini, gpt-4.1, gpt-4-turbo, "
        "gemini-2.5-pro, gemini-2.5-flash, gemini-2.0-flash."
    ) in rendered
    # Carries the model id as a structured field for typed handling.
    assert exc_info.value.model == "not-a-real-model-9999"


def test_modelpricing_is_frozen() -> None:
    """``ModelPricing`` is ``frozen=True``; field assignment raises
    ``FrozenInstanceError`` (config-shaped reproducibility invariant —
    same input → same USD math, always)."""
    pricing = lookup("claude-sonnet-4-6")
    with pytest.raises(FrozenInstanceError):
        pricing.input_per_mtok = 999.0  # type: ignore[misc]


def test_price_table_version_is_a_nonempty_string() -> None:
    """``PRICE_TABLE_VERSION`` is a non-empty string. The literal value is
    a sourcing-date stamp; the structural check here ensures the constant
    exists and is non-empty, so a price-table refresh doesn't churn the
    test for cosmetic reasons.
    """
    assert isinstance(PRICE_TABLE_VERSION, str)
    assert PRICE_TABLE_VERSION


def test_price_table_version_pinned_to_us_004_ship_date() -> None:
    """The ``#136 US-004`` ship-date stamp is pinned so a stealth bump
    to the table version without a paired numeric edit / commit fails
    loudly. Bump in lockstep with any future ``_PRICES_MUTABLE`` edit
    (paired commit, per the module docstring)."""
    assert PRICE_TABLE_VERSION == "2026-05-28"


def test_pricing_module_exports() -> None:
    """All five public symbols are importable from the package top-level
    (``signalforge.llm``) — not just from the private
    ``signalforge.llm.pricing`` submodule. Mirrors the re-export contract
    every stage's ``__init__`` follows.
    """
    from signalforge import llm as llm_pkg

    assert hasattr(llm_pkg, "PRICE_TABLE_VERSION")
    assert hasattr(llm_pkg, "PRICES")
    assert hasattr(llm_pkg, "ModelPricing")
    assert hasattr(llm_pkg, "lookup")
    assert hasattr(llm_pkg, "EstimateUnknownModelError")


def test_prices_contains_all_shipped_skus() -> None:
    """The shipped SKU set is locked: three Anthropic + four OpenAI as of
    ``#136 US-004`` (DEC-007). Adding a future SKU is a deliberate
    expansion that should fail this test loudly until the AC is updated.
    """
    assert set(PRICES.keys()) == {
        # Anthropic
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-haiku-4-5",
        # OpenAI (#136 US-004)
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4-turbo",
        # Gemini (#137 US-006, DEC-017)
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    }


@pytest.mark.parametrize(
    "sku",
    [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4-turbo",
    ],
)
def test_lookup_returns_modelpricing_for_each_openai_sku(sku: str) -> None:
    """Every OpenAI SKU added in #136 US-004 (DEC-007) ``lookup``s to a
    ``ModelPricing`` with strictly positive input/output rates and
    cache fields set to ``0.0`` — OpenAI has no Anthropic-equivalent
    ``cache_control`` discount tier, so the cache columns are a
    deliberate zero, not an unset placeholder.
    """
    pricing = lookup(sku)
    assert isinstance(pricing, ModelPricing)
    assert pricing.input_per_mtok > 0.0
    assert pricing.output_per_mtok > 0.0
    assert pricing.cache_write_5m_per_mtok == 0.0
    assert pricing.cache_read_per_mtok == 0.0


def test_lookup_raises_for_unknown_openai_flavoured_id() -> None:
    """A plausible-looking but unsupported OpenAI id still raises
    ``EstimateUnknownModelError`` — `lookup` does no provider sniffing
    or fuzzy matching, just a strict dict lookup."""
    with pytest.raises(EstimateUnknownModelError) as exc_info:
        lookup("gpt-9-unicorn")
    assert exc_info.value.model == "gpt-9-unicorn"


@pytest.mark.parametrize(
    "model",
    ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
)
def test_lookup_returns_modelpricing_for_gemini_skus(model: str) -> None:
    """Each Gemini SKU (#137 DEC-017) resolves via ``lookup`` with positive
    input/output rates and zero cache rates — v0.3 Gemini ships without an
    Anthropic-equivalent prompt-cache discount.
    """
    pricing = lookup(model)
    assert isinstance(pricing, ModelPricing)
    assert pricing.input_per_mtok > 0.0
    assert pricing.output_per_mtok > 0.0
    assert pricing.cache_write_5m_per_mtok == 0.0
    assert pricing.cache_read_per_mtok == 0.0


def test_lookup_raises_estimateunknownmodelerror_for_unknown_gemini_model() -> None:
    """An unknown Gemini-shaped SKU routes through the standard
    ``EstimateUnknownModelError`` path (no vendor-prefix fallback).

    Also pins the operator-facing remediation text: with Gemini SKUs now
    registered, stale Claude-only / OpenAI-only guidance would pass the
    ``.model`` field check unnoticed and mislead operators. The pin is
    structural ("the rendered exception names every shipped SKU
    including the Gemini ones") rather than a verbatim string match, so
    a future SKU addition only breaks this test if the remediation
    isn't updated in lockstep.
    """
    with pytest.raises(EstimateUnknownModelError) as exc_info:
        lookup("gemini-unknown")
    assert exc_info.value.model == "gemini-unknown"
    rendered = str(exc_info.value)
    for sku in ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"):
        assert sku in rendered, (
            f"Remediation text omits Gemini SKU {sku!r} — operator guidance "
            "drifted from the pricing table."
        )


def test_anthropic_skus_remain_byte_identical_after_us_004() -> None:
    """Anthropic estimate byte-identity floor (DEC-013 of #135 / preserved
    by #136): adding OpenAI SKUs must NOT perturb any field on the three
    Anthropic ``ModelPricing`` rows. Pinning every field by value (rather
    than asserting equality against a freshly-constructed dataclass) so a
    silent rate drift fails loud here, not only at the cost-projection
    seam.
    """
    sonnet = lookup("claude-sonnet-4-6")
    assert sonnet.input_per_mtok == 3.00
    assert sonnet.output_per_mtok == 15.00
    assert sonnet.cache_write_5m_per_mtok == 3.75
    assert sonnet.cache_read_per_mtok == 0.30

    opus = lookup("claude-opus-4-7")
    assert opus.input_per_mtok == 15.00
    assert opus.output_per_mtok == 75.00
    assert opus.cache_write_5m_per_mtok == 18.75
    assert opus.cache_read_per_mtok == 1.50

    haiku = lookup("claude-haiku-4-5")
    assert haiku.input_per_mtok == 0.80
    assert haiku.output_per_mtok == 4.00
    assert haiku.cache_write_5m_per_mtok == 1.00
    assert haiku.cache_read_per_mtok == 0.08


def test_estimateunknownmodelerror_is_in_exit_code_mapping_at_tier_2() -> None:
    """The 7th AST scan in ``tests/test_audit_completeness.py`` requires
    every concrete ``*Error`` to be registered; this test pins the
    specific tier (2 — input-validation) for ``EstimateUnknownModelError``.
    The operator picked a model the price table doesn't know — that's an
    input-shape error, not an external-dep failure.
    """
    from signalforge.cli._helpers import _EXCEPTION_TO_EXIT_CODE

    assert _EXCEPTION_TO_EXIT_CODE[EstimateUnknownModelError] == 2
