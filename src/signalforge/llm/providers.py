"""Provider-neutral LLM seam — value objects, the ``LLMProvider`` ABC, registry.

US-001 of issue #135 (provider-neutral LLM seam). Establishes the abstraction
that lets an LLM vendor plug in behind a thin, provider-neutral interface — the
prerequisite for OpenAI/Gemini grading (#136/#137). Mirrors the warehouse-adapter
seam (ABC/strategy + a registry in place of a factory ``if``-ladder).

Design commitments operationalised here:

* **DEC-001** — the generic orchestrator (``call_llm``, lands in US-003) owns the
  retry loop, backoff math, logging, and ``LLMResult`` assembly. A provider
  strategy owns only: build create-kwargs, build count-tokens-kwargs, extract
  text blocks, extract usage, classify exception → category, and capability
  flags. The orchestrator dispatches on :class:`ExceptionCategory`, never on a
  vendor SDK exception class, and never touches a vendor-shaped request dict.
* **DEC-002** — neutral value objects. :class:`UsageMetrics` (token economics)
  and :class:`ExceptionCategory` (the five retry-taxonomy branches) decouple the
  orchestrator from any vendor's response/exception shapes.
* **DEC-003** — :class:`LLMProvider` ABC + a process-level registry
  (:func:`register_provider` / :func:`provider_for`). Unknown name raises a typed
  :class:`signalforge.llm.errors.UnknownProviderError` listing the available
  registered providers. The registry is a plugin point designed to grow — a new
  provider registers itself rather than editing a factory ``if``-ladder.

No vendor behaviour is wired here. US-002 implements ``AnthropicProvider`` and
registers it; until then the registry starts empty and ``provider_for("anthropic")``
raises :class:`UnknownProviderError`.
"""

from __future__ import annotations

import abc
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

from signalforge.llm.errors import UnknownProviderError


class ExceptionCategory(Enum):
    """Neutral retry-taxonomy category an :class:`LLMProvider` maps a raised
    SDK exception to (DEC-002).

    The orchestrator (``call_llm``, US-003) dispatches its retry loop on these
    five categories instead of inspecting vendor exception classes directly, so
    the loop stays vendor-agnostic. The members mirror the existing Anthropic
    retry branches (``llm-drafter.md`` DEC-004):

    * :attr:`AUTH` — 401 / 403; short-circuit (retrying won't fix a credential).
    * :attr:`RATE_LIMIT` — 429; retried with backoff up to the per-call budget.
    * :attr:`SERVER_ERROR` — 5xx; retried up to the (smaller) per-call budget.
    * :attr:`CONNECTION` — network-level failure; retried up to its budget.
    * :attr:`NO_RETRY` — any other failure (e.g. a 4xx that isn't auth); the
      orchestrator surfaces it without retrying.
    """

    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    CONNECTION = "connection"
    NO_RETRY = "no_retry"


class UsageMetrics(BaseModel):
    """Neutral token-economics value object an :class:`LLMProvider` extracts
    from a vendor response (DEC-002).

    Mirrors the cache-token fields on
    :class:`signalforge.llm.models.LLMResult`: ``cache_creation_input_tokens``
    and ``cache_read_input_tokens`` default to 0 because providers without
    prompt caching omit them, and the orchestrator reports 0 in that case
    (DEC-008). Frozen + ``extra="ignore"`` matches the produced-in-process
    value-object convention of the neighbouring ``LLMResult`` — this object is
    assembled in-process and handed to the orchestrator, never deserialised
    from disk.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class LLMProvider(abc.ABC):
    """Provider strategy behind the generic LLM orchestrator (DEC-001, DEC-003).

    A concrete provider supplies the vendor-specific pieces the orchestrator
    (``call_llm``, US-003) needs: how to build the SDK client, how to shape the
    request kwargs, how to read text + usage off a response, and how to classify
    a raised exception into a neutral :class:`ExceptionCategory`. The orchestrator
    owns everything else (retry loop, backoff, logging, ``LLMResult`` assembly)
    so the vendor surface stays thin.

    Three capability descriptors gate orchestrator behaviour (DEC-008):

    * :attr:`name` — the registry key (e.g. ``"anthropic"``).
    * :attr:`supports_prompt_caching` — when ``False`` the orchestrator emits no
      ``cache_control`` marker, no extended-cache beta header, reports 0 cache
      tokens, and skips the dual-zero cache-anomaly WARNING.
    * :attr:`supports_token_count` — when ``False`` the orchestrator skips the
      pre-send count-tokens gate (a provider without token-counting cannot
      enforce the cap up front).

    Subclasses declare ``name`` / ``supports_prompt_caching`` /
    ``supports_token_count`` as class attributes (or override the property).

    The abstract method signatures are designed to fit the orchestrator/strategy
    split described in DEC-001; US-002/US-003 may refine them as the Anthropic
    strategy and ``call_llm`` land.
    """

    #: Registry key for this provider (e.g. ``"anthropic"``).
    name: str
    #: Whether the provider supports Anthropic-style prompt caching (DEC-008).
    supports_prompt_caching: bool
    #: Whether the provider can count input tokens before sending (DEC-008).
    supports_token_count: bool

    @abc.abstractmethod
    def make_client(self) -> object:
        """Build and return the real vendor SDK client.

        Called by the orchestrator when no client was injected for test use.
        """

    @abc.abstractmethod
    def build_create_kwargs(
        self,
        *,
        system: str,
        cached_block: str,
        dynamic_block: str,
        model: str,
        max_tokens: int,
        cache_ttl: str,
        cache_marker_active: bool,
    ) -> dict[str, Any]:
        """Build the kwargs for the vendor's message-create call.

        ``cache_marker_active`` is the orchestrator's resolved decision about
        whether a prompt-cache marker should be attached for this call; a
        provider that does not support caching ignores it.
        """

    @abc.abstractmethod
    def build_count_tokens_kwargs(
        self,
        *,
        system: str,
        cached_block: str,
        model: str,
    ) -> dict[str, Any]:
        """Build the kwargs for the vendor's pre-send token-count call.

        Only invoked by the orchestrator when :attr:`supports_token_count`.
        """

    @abc.abstractmethod
    def extract_text_blocks(self, response: object) -> tuple[str, ...]:
        """Extract the text content blocks from a vendor response."""

    @abc.abstractmethod
    def extract_usage(self, response: object) -> UsageMetrics:
        """Extract token-economics from a vendor response as :class:`UsageMetrics`."""

    @abc.abstractmethod
    def classify_exception(self, exc: BaseException) -> ExceptionCategory:
        """Map a raised vendor exception to a neutral :class:`ExceptionCategory`."""


# Process-level provider registry, keyed by ``provider.name`` (DEC-003). Module
# scope makes it a single registry per process; US-002 registers
# ``AnthropicProvider`` at import time.
_REGISTRY: dict[str, LLMProvider] = {}


def register_provider(provider: LLMProvider) -> None:
    """Register ``provider`` in the process-level registry, keyed by its
    ``name`` (DEC-003).

    Last-writer-wins: registering a provider under an already-registered name
    replaces the prior entry. The registry is a plugin point designed to grow —
    a new provider registers itself (typically at import time) rather than
    editing a factory ``if``-ladder.
    """
    _REGISTRY[provider.name] = provider


def provider_for(name: str) -> LLMProvider:
    """Return the registered provider for ``name`` (DEC-003).

    Raises :class:`signalforge.llm.errors.UnknownProviderError` — listing the
    available registered provider names — when ``name`` is not registered.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownProviderError(name, available=tuple(_REGISTRY)) from None


__all__ = (
    "ExceptionCategory",
    "LLMProvider",
    "UsageMetrics",
    "provider_for",
    "register_provider",
)
