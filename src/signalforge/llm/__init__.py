"""SignalForge LLM seam — provider-neutral call_llm orchestrator + retry taxonomy.

A provider registry (default ``anthropic``) plugs vendors in behind a thin
``LLMProvider`` strategy; see :mod:`signalforge.llm.providers`."""

from signalforge.llm._anthropic_client import AnthropicClientProtocol
from signalforge.llm.client import call_llm
from signalforge.llm.errors import (
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
from signalforge.llm.models import LLMResult
from signalforge.llm.pricing import (
    PRICE_TABLE_VERSION,
    PRICES,
    ModelPricing,
    lookup,
)
from signalforge.llm.providers import (
    AnthropicProvider,
    ExceptionCategory,
    LLMProvider,
    UsageMetrics,
    provider_for,
    register_provider,
)

__all__ = (
    "PRICES",
    "PRICE_TABLE_VERSION",
    "AnthropicClientProtocol",
    "AnthropicProvider",
    "EstimateUnknownModelError",
    "ExceptionCategory",
    "LLMAuthError",
    "LLMCacheTooLargeError",
    "LLMConnectionError",
    "LLMError",
    "LLMHelperError",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMResponseFormatError",
    "LLMResult",
    "LLMServerError",
    "ModelPricing",
    "UnknownProviderError",
    "UsageMetrics",
    "call_llm",
    "lookup",
    "provider_for",
    "register_provider",
)
