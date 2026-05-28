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

US-002 implements :class:`AnthropicProvider` (moving the Anthropic-specific
request-build, text/usage extraction, and exception classification behind the
ABC methods) and registers it at import time, so ``provider_for("anthropic")``
returns it. The Anthropic SDK noise stays confined to
:mod:`signalforge.llm._anthropic_client` — this module reaches it only through
that shim's typed surface plus the pure helpers in
:mod:`signalforge.llm.client`.
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


class AnthropicProvider(LLMProvider):
    """Anthropic strategy behind the generic LLM orchestrator (DEC-002/003/004).

    Moves the Anthropic-specific request-build, text/usage extraction, and
    exception classification — historically inline in
    :func:`signalforge.llm.client.call_llm` — behind the
    :class:`LLMProvider` ABC. Anthropic supports both prompt caching and
    pre-send token counting, so both capability flags are ``True`` and the
    orchestrator's Anthropic control flow + emitted bytes are unchanged
    (DEC-008).

    The Anthropic SDK client is constructed only via
    :func:`signalforge.llm._anthropic_client._make_anthropic_client`, keeping
    the DEC-012 SDK-ignore confinement intact. Exception classification reads
    the SDK exception classes through
    :func:`signalforge.llm._anthropic_client._load_anthropic_exception_classes`
    and reuses the pure ``_is_5xx`` / ``_is_4xx_non_auth`` helpers from
    :mod:`signalforge.llm.client`.

    .. note::
       The generic orchestrator :func:`signalforge.llm.client.call_llm` drives
       this strategy (US-003); the Anthropic path stays byte-identical to the
       pre-#135 ``call_llm`` it replaced. A new vendor ships its own
       :class:`LLMProvider` subclass + ``_<vendor>_client.py`` shim.
    """

    name = "anthropic"
    supports_prompt_caching = True
    supports_token_count = True

    def make_client(self) -> object:
        """Construct the real ``anthropic.Anthropic`` client via the shim."""
        from signalforge.llm._anthropic_client import _make_anthropic_client

        return _make_anthropic_client()

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
        """Build the kwargs for ``client.messages.create``.

        Reproduces byte-for-byte the request shape historically built inline in
        :func:`signalforge.llm.client.call_llm`: two user-message text
        blocks, with the ``cache_control`` ephemeral marker on block-1 ONLY when
        ``cache_marker_active``, and the extended-cache beta header on
        ``extra_headers`` only when ``cache_ttl == "1h"``.
        """
        block_1: dict[str, Any] = {"type": "text", "text": cached_block}
        if cache_marker_active:
            block_1["cache_control"] = {"type": "ephemeral", "ttl": cache_ttl}
        block_2: dict[str, Any] = {"type": "text", "text": dynamic_block}
        messages = [{"role": "user", "content": [block_1, block_2]}]
        extra_headers: dict[str, str] = (
            {"anthropic-beta": "extended-cache-ttl-2025-04-11"} if cache_ttl == "1h" else {}
        )
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "extra_headers": extra_headers,
        }

    def build_count_tokens_kwargs(
        self,
        *,
        system: str,
        cached_block: str,
        model: str,
    ) -> dict[str, Any]:
        """Build the kwargs for the pre-send ``client.messages.count_tokens``.

        The count is issued against ``system`` + the cached block only — that's
        the surface the cache marker covers. Block-1 here carries NO
        ``cache_control`` marker: the marker is irrelevant to the returned
        ``input_tokens`` count, so it is intentionally omitted from the probe.
        """
        return {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": [{"type": "text", "text": cached_block}]}],
        }

    def extract_text_blocks(self, response: object) -> tuple[str, ...]:
        """Extract text content blocks from an Anthropic response."""
        from signalforge.llm.client import _extract_text_blocks

        return _extract_text_blocks(response)

    def extract_usage(self, response: object) -> UsageMetrics:
        """Extract token economics from an Anthropic response.

        Mirrors the ``call_llm`` reads: ``input_tokens`` / ``output_tokens``
        are required; ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
        default to 0 when absent.
        """
        from signalforge.llm.client import _extract_usage_field
        from signalforge.llm.errors import LLMResponseFormatError

        usage = getattr(response, "usage", None)
        if usage is None:
            raise LLMResponseFormatError(
                "Response is missing the `usage` attribute.",
            )
        return UsageMetrics(
            input_tokens=_extract_usage_field(usage, "input_tokens"),
            output_tokens=_extract_usage_field(usage, "output_tokens"),
            cache_creation_input_tokens=_extract_usage_field(
                usage, "cache_creation_input_tokens", default=0
            ),
            cache_read_input_tokens=_extract_usage_field(
                usage, "cache_read_input_tokens", default=0
            ),
        )

    def classify_exception(self, exc: BaseException) -> ExceptionCategory:
        """Map a raised Anthropic SDK exception to a neutral category.

        Dispatch order mirrors the ``call_llm`` retry loop: auth →
        rate-limit → connection → API-status (5xx → SERVER_ERROR; 4xx-non-auth →
        NO_RETRY; any other status → NO_RETRY). Anything unrecognised maps to
        :attr:`ExceptionCategory.NO_RETRY` so the orchestrator surfaces it
        without retrying.
        """
        from signalforge.llm._anthropic_client import _load_anthropic_exception_classes
        from signalforge.llm.client import _is_4xx_non_auth, _is_5xx

        exc_classes = _load_anthropic_exception_classes()
        if isinstance(exc, exc_classes.auth):
            return ExceptionCategory.AUTH
        if isinstance(exc, exc_classes.rate_limit):
            return ExceptionCategory.RATE_LIMIT
        if isinstance(exc, exc_classes.connection):
            return ExceptionCategory.CONNECTION
        if isinstance(exc, exc_classes.api_status):
            if _is_5xx(exc):
                return ExceptionCategory.SERVER_ERROR
            if _is_4xx_non_auth(exc):
                return ExceptionCategory.NO_RETRY
            return ExceptionCategory.NO_RETRY
        return ExceptionCategory.NO_RETRY


# Register the Anthropic strategy at import time so ``provider_for("anthropic")``
# resolves it. The registry is a plugin point designed to grow — #136/#137
# register their providers the same way (DEC-003).
register_provider(AnthropicProvider())


__all__ = (
    "AnthropicProvider",
    "ExceptionCategory",
    "LLMProvider",
    "UsageMetrics",
    "provider_for",
    "register_provider",
)
