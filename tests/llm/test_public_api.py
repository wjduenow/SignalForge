"""Public-API enforcement for ``signalforge.llm``.

DEC-013 of ``plans/super/9-cli-entrypoint.md``: the CLI tier-maps the
LLM-seam's typed exceptions to deterministic exit codes, so the CLI
cannot reach into private modules — every name the CLI imports must
be re-exported on :mod:`signalforge.llm`'s public surface.
:class:`signalforge.llm.LLMResponseFormatError` (US-001) is consumed
by the CLI's exception-mapper alongside the seven other
:class:`signalforge.llm.LLMError` subclasses.

Mirrors :mod:`tests.diff.test_public_api`,
:mod:`tests.safety.test_public_api`,
:mod:`tests.warehouse.test_public_api`, and
:mod:`tests.manifest.test_public_api` so ``__all__`` and the
documented surface cannot drift.
"""

from __future__ import annotations

import signalforge.llm as llm_pkg

_DOCUMENTED_PUBLIC = (
    # Function
    "call_llm",
    # Result model
    "LLMResult",
    # Client protocol (issue #44 — promoted from the private
    # ``_AnthropicClientProtocol`` so the ``client`` kwarg on
    # ``draft_schema`` / ``grade_artifacts`` and downstream library
    # callers can type-annotate against the public name).
    "AnthropicClientProtocol",
    # Errors — base + helper-call family
    "LLMError",
    "LLMHelperError",
    "LLMAuthError",
    "LLMConnectionError",
    "LLMRateLimitError",
    "LLMResponseFormatError",
    "LLMServerError",
    # Errors — cache sizing
    "LLMCacheTooLargeError",
    # Errors — estimate cost preview (US-001 of #36)
    "EstimateUnknownModelError",
    # Errors — provider registry (US-001 of #135)
    "UnknownProviderError",
    # Pricing surface (US-001 of #36)
    "PRICE_TABLE_VERSION",
    "PRICES",
    "ModelPricing",
    "lookup",
    # Provider seam (US-001 of #135) — neutral value objects + ABC + registry.
    "ExceptionCategory",
    "UsageMetrics",
    "LLMProvider",
    "register_provider",
    "provider_for",
    # Anthropic strategy (US-002 of #135) — registered at import time.
    "AnthropicProvider",
    # OpenAI strategy (US-002 of #136) — registered at import time.
    "OpenAIProvider",
)


def test_documented_surface_importable_from_package_root() -> None:
    """Every documented name resolves on ``signalforge.llm``."""
    for name in _DOCUMENTED_PUBLIC:
        assert hasattr(llm_pkg, name), f"signalforge.llm is missing {name!r}"


def test_all_lists_documented_surface() -> None:
    """``__all__`` matches the documented surface exactly."""
    assert sorted(llm_pkg.__all__) == sorted(_DOCUMENTED_PUBLIC), (
        "signalforge.llm.__all__ does not match the documented surface. "
        f"Missing from __all__: {sorted(set(_DOCUMENTED_PUBLIC) - set(llm_pkg.__all__))}; "
        f"unexpected in __all__: {sorted(set(llm_pkg.__all__) - set(_DOCUMENTED_PUBLIC))}."
    )


def test_each_public_name_is_importable_via_from_signalforge_llm() -> None:
    """Every documented public name is importable directly."""
    from signalforge.llm import (  # noqa: F401
        PRICE_TABLE_VERSION,
        PRICES,
        AnthropicClientProtocol,
        AnthropicProvider,
        EstimateUnknownModelError,
        ExceptionCategory,
        LLMAuthError,
        LLMCacheTooLargeError,
        LLMConnectionError,
        LLMError,
        LLMHelperError,
        LLMProvider,
        LLMRateLimitError,
        LLMResponseFormatError,
        LLMResult,
        LLMServerError,
        ModelPricing,
        OpenAIProvider,
        UnknownProviderError,
        UsageMetrics,
        call_llm,
        lookup,
        provider_for,
        register_provider,
    )


def test_typed_errors_subclass_llm_error() -> None:
    """All LLM-seam typed errors descend from ``LLMError``.

    Mirrors the equivalent assertion in :mod:`tests.draft.test_public_api`.
    """
    from signalforge.llm import (
        EstimateUnknownModelError,
        LLMAuthError,
        LLMCacheTooLargeError,
        LLMConnectionError,
        LLMError,
        LLMHelperError,
        LLMRateLimitError,
        LLMResponseFormatError,
        LLMServerError,
        UnknownProviderError,
    )

    for cls in (
        EstimateUnknownModelError,
        LLMAuthError,
        LLMCacheTooLargeError,
        LLMConnectionError,
        LLMHelperError,
        LLMRateLimitError,
        LLMResponseFormatError,
        LLMServerError,
        UnknownProviderError,
    ):
        assert issubclass(cls, LLMError), f"{cls.__name__} is not an LLMError subclass"
