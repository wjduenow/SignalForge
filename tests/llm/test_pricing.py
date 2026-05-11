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
    # Locked remediation text — verbatim per US-001 AC.
    assert (
        "Add the model to signalforge.llm.pricing.PRICES or use a supported "
        "model: claude-sonnet-4-6, claude-opus-4-7, claude-haiku-4-5."
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
    a sourcing-date stamp; v0.1 ships ``"2026-05-11"`` but the assertion
    here is structural — that the constant exists and is non-empty —
    so a price-table refresh doesn't churn the test for cosmetic reasons.
    """
    assert isinstance(PRICE_TABLE_VERSION, str)
    assert PRICE_TABLE_VERSION


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


def test_prices_contains_all_v01_skus() -> None:
    """The v0.1 SKU set is locked: ``claude-sonnet-4-6``,
    ``claude-opus-4-7``, ``claude-haiku-4-5``. Adding a fourth SKU is a
    deliberate v0.2 expansion that should fail this test loudly until the
    AC is updated."""
    assert set(PRICES.keys()) == {
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-haiku-4-5",
    }


def test_estimateunknownmodelerror_is_in_exit_code_mapping_at_tier_2() -> None:
    """The 7th AST scan in ``tests/test_audit_completeness.py`` requires
    every concrete ``*Error`` to be registered; this test pins the
    specific tier (2 — input-validation) for ``EstimateUnknownModelError``.
    The operator picked a model the price table doesn't know — that's an
    input-shape error, not an external-dep failure.
    """
    from signalforge.cli._helpers import _EXCEPTION_TO_EXIT_CODE

    assert _EXCEPTION_TO_EXIT_CODE[EstimateUnknownModelError] == 2
