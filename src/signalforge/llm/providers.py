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

    @abc.abstractmethod
    def estimate_input_tokens(
        self,
        model: str,
        text: str,
        *,
        client: object | None = None,
    ) -> int:
        """Return the input-token count ``text`` would consume on ``model``.

        Used by the ``signalforge generate --estimate`` cost-preview path
        (issue #36 / #136 US-005 — DEC-003). The Anthropic implementation
        delegates to the SDK's ``messages.count_tokens`` (a server-side
        count) so the figure matches what the runtime path would bill;
        the OpenAI implementation delegates to ``tiktoken`` (a local BPE
        count) because OpenAI has no equivalent pre-send count API.

        Capability flags (DEC-008 of #135) do NOT gate this method —
        every provider must answer "how many tokens is this text?" even
        if it lacks Anthropic-style prompt caching. The runtime retry
        loop's pre-send count gate (which IS gated by
        :attr:`supports_token_count`) is a different surface; this is
        the estimate path's calibration seam.

        ``client`` is an optional pre-constructed SDK client (e.g. a
        test fake satisfying the vendor's client protocol). Providers
        that build a transient client per call (e.g. Anthropic) MAY
        accept and reuse it to avoid the construction cost; providers
        whose implementation is local (e.g. OpenAI's ``tiktoken``) MAY
        ignore it. The orchestrator passes whatever client it already
        has in scope (or ``None``); the provider decides.
        """


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

    def estimate_input_tokens(
        self,
        model: str,
        text: str,
        *,
        client: object | None = None,
    ) -> int:
        """Count tokens via the Anthropic SDK's ``messages.count_tokens``.

        Preserves byte-identity with the pre-#136-US-005 inline
        ``client.messages.count_tokens(...)`` calls in
        :mod:`signalforge.cli._estimate` (DEC-013 of #136): the count
        is issued with a single user-message text block whose
        ``content`` is ``text``. Tests inject a queued-response
        ``FakeAnthropicClient`` via ``client``; production callers
        either pass a pre-constructed client (from the
        ``--estimate`` CLI prelude) or rely on the lazy fallback
        below.

        When ``client`` is ``None``, the provider builds a transient
        SDK client via
        :func:`signalforge.llm._anthropic_client._make_anthropic_client`.
        This path is reachable only when a caller invokes the engine
        without threading a client through (no v0.x caller does so on
        the happy path); the cost of one SDK construction per call is
        acceptable for that fallback case.
        """
        from typing import cast

        from signalforge.llm._anthropic_client import (
            AnthropicClientProtocol,
            _make_anthropic_client,
        )
        from signalforge.llm.errors import LLMResponseFormatError

        # Cast the optional ``client`` to the public protocol so pyright
        # sees the ``messages.count_tokens`` surface without a type-checker
        # suppression here — the DEC-012 confinement rule keeps every
        # SDK suppression inside ``_anthropic_client.py``. Both the real
        # ``anthropic.Anthropic`` (via the shim's ``_make_anthropic_client``)
        # and the ``FakeAnthropicClient`` test fake satisfy the protocol
        # structurally.
        resolved: AnthropicClientProtocol = (
            _make_anthropic_client() if client is None else cast(AnthropicClientProtocol, client)
        )
        response = resolved.messages.count_tokens(
            model=model,
            messages=[{"role": "user", "content": text}],
        )
        input_tokens = getattr(response, "input_tokens", None)
        if not isinstance(input_tokens, int):
            raise LLMResponseFormatError(
                "Anthropic count_tokens response is missing the `input_tokens` field.",
            )
        return input_tokens


# Register the Anthropic strategy at import time so ``provider_for("anthropic")``
# resolves it. The registry is a plugin point designed to grow — #136/#137
# register their providers the same way (DEC-003).
register_provider(AnthropicProvider())


class OpenAIProvider(LLMProvider):
    """OpenAI strategy behind the generic LLM orchestrator (#136 DEC-001/005/006/009/011).

    OpenAI has no Anthropic-style prompt-caching primitive and no server-side
    pre-send ``count_tokens`` API; both capability flags are therefore
    ``False`` (DEC-008 of #135). The orchestrator consequently:

    * emits no ``cache_control`` marker and no extended-cache beta header,
    * reports ``cache_creation_input_tokens=0`` and ``cache_read_input_tokens=0``,
    * skips the pre-send count-tokens gate entirely (the ``--estimate`` path
      uses a local ``tiktoken`` count instead — US-005).

    The OpenAI SDK exposes ``client.chat.completions.create(...)`` rather than
    ``client.messages.create(...)``; the orchestrator hard-calls
    ``client.messages.create(**kwargs)``. The shim's
    :class:`signalforge.llm._openai_client._OpenAIClientAdapter` wraps the
    real OpenAI client so ``.messages.create`` delegates to the underlying
    ``chat.completions.create`` (DEC-009). Every OpenAI-SDK type-checker
    suppression lives in :mod:`signalforge.llm._openai_client` (DEC-010);
    this provider reaches the SDK only through that shim's typed surfaces.

    ``build_create_kwargs`` attaches ``response_format={"type": "json_object"}``
    to enforce JSON output server-side (DEC-006). This is belt-and-braces with
    the existing tolerant :func:`signalforge.llm.json_payload.extract_json_payload`
    parser; server-side enforcement eliminates the prose-preamble drift class
    (mirrors issue #144's fix for ``claude-sonnet-4-6``). The grade and drafter
    system prompts both already name "JSON" so OpenAI's prompt-requirement check
    passes.

    .. note::
       :meth:`build_count_tokens_kwargs` raises :class:`NotImplementedError`
       (DEC-011 of #136). The orchestrator gates the pre-send count call on
       :attr:`supports_token_count` and so never invokes this method —
       mirrors the ``FakeNoCacheProvider`` precedent.
    """

    name = "openai"
    supports_prompt_caching = False
    supports_token_count = False

    def make_client(self) -> object:
        """Construct the real OpenAI client (wrapped in the shim adapter)."""
        from signalforge.llm._openai_client import _make_openai_client

        return _make_openai_client()

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
        """Build the kwargs for ``client.chat.completions.create``.

        Returns the OpenAI-native Chat Completions kwargs shape: ``model``,
        ``max_tokens``, a two-message ``messages`` list (system + user where the
        user content is the cached block followed by the dynamic block), and
        ``response_format={"type": "json_object"}`` for server-side JSON
        enforcement (DEC-006).

        ``cache_ttl`` and ``cache_marker_active`` are ignored: OpenAI has no
        prompt-caching primitive, both capability flags are ``False`` (DEC-008
        of #135), and the orchestrator already resolves ``cache_marker_active``
        to ``False`` for a non-caching provider. There is no ``cache_control``
        marker and no ``extra_headers`` attached.
        """
        return {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": cached_block + dynamic_block},
            ],
            "response_format": {"type": "json_object"},
        }

    def build_count_tokens_kwargs(
        self,
        *,
        system: str,
        cached_block: str,
        model: str,
    ) -> dict[str, Any]:
        """Never invoked — ``supports_token_count`` is ``False`` (DEC-011).

        The orchestrator skips the pre-send count gate entirely for a provider
        that cannot count tokens server-side; the ``--estimate`` path uses
        :func:`signalforge.llm._openai_client._count_openai_tokens` (a local
        ``tiktoken`` count) instead. Raising here makes a future regression in
        the capability-flag gate loud rather than silent.
        """
        raise NotImplementedError(
            "build_count_tokens_kwargs is unreachable when supports_token_count=False"
        )

    def extract_text_blocks(self, response: object) -> tuple[str, ...]:
        """Extract the assistant text from an OpenAI Chat Completions response.

        OpenAI returns ``response.choices[0].message.content`` as a single
        string (no per-block typing — unlike Anthropic's typed-block array).
        Returns a single-element tuple so the orchestrator's downstream
        ``"".join(blocks)`` is the same shape across providers. Raises
        :class:`signalforge.llm.errors.LLMResponseFormatError` if the
        structure is missing or the content is ``None``.
        """
        from signalforge.llm.errors import LLMResponseFormatError

        choices = getattr(response, "choices", None)
        if not choices:
            raise LLMResponseFormatError(
                "OpenAI response is missing the `choices` attribute or it is empty.",
            )
        first = choices[0]
        message = getattr(first, "message", None)
        if message is None:
            raise LLMResponseFormatError(
                "OpenAI response choice is missing the `message` attribute.",
            )
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            raise LLMResponseFormatError(
                "OpenAI response message `content` is missing or not a string.",
            )
        return (content,)

    def extract_usage(self, response: object) -> UsageMetrics:
        """Extract token economics from an OpenAI Chat Completions response.

        OpenAI reports ``usage.prompt_tokens`` and ``usage.completion_tokens``
        (no cache fields — OpenAI has no equivalent cache discount). Returns
        :class:`UsageMetrics` with ``cache_creation_input_tokens=0`` and
        ``cache_read_input_tokens=0``; matches ``supports_prompt_caching=False``.
        """
        from signalforge.llm.client import _extract_usage_field
        from signalforge.llm.errors import LLMResponseFormatError

        usage = getattr(response, "usage", None)
        if usage is None:
            raise LLMResponseFormatError(
                "OpenAI response is missing the `usage` attribute.",
            )
        return UsageMetrics(
            input_tokens=_extract_usage_field(usage, "prompt_tokens"),
            output_tokens=_extract_usage_field(usage, "completion_tokens"),
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )

    def classify_exception(self, exc: BaseException) -> ExceptionCategory:
        """Map a raised OpenAI SDK exception to a neutral category (DEC-009 of #136).

        Dispatch order mirrors :meth:`AnthropicProvider.classify_exception`:
        auth (401 / 403) → rate-limit (429) → connection → API-status (5xx →
        SERVER_ERROR; 4xx-non-auth → NO_RETRY; any other status → NO_RETRY).
        Anything unrecognised maps to :attr:`ExceptionCategory.NO_RETRY` so the
        orchestrator surfaces it without retrying.

        Reads the SDK exception classes through the shim's
        :func:`signalforge.llm._openai_client._load_openai_exception_classes`
        so the DEC-010 SDK-ignore confinement holds.
        """
        from signalforge.llm._openai_client import _load_openai_exception_classes
        from signalforge.llm.client import _is_4xx_non_auth, _is_5xx

        exc_classes = _load_openai_exception_classes()
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

    def estimate_input_tokens(
        self,
        model: str,
        text: str,
        *,
        client: object | None = None,
    ) -> int:
        """Count tokens locally via ``tiktoken`` (DEC-003/DEC-012 of #136).

        OpenAI has no server-side ``count_tokens`` API (and the provider
        declares ``supports_token_count=False`` so the runtime retry loop
        skips its pre-send count gate entirely — DEC-008 of #135). The
        ``--estimate`` calibration path counts tokens locally instead by
        delegating to
        :func:`signalforge.llm._openai_client._count_openai_tokens`,
        which uses ``tiktoken.encoding_for_model(model)`` with a
        ``cl100k_base`` fallback for unknown model ids.

        ``client`` is ignored — the count is a pure local BPE pass with
        no SDK or network involvement. The kwarg is declared for
        protocol parity with :meth:`AnthropicProvider.estimate_input_tokens`
        so the orchestrator can call every provider the same way.
        """
        from signalforge.llm._openai_client import _count_openai_tokens

        del client  # tiktoken needs no SDK client
        return _count_openai_tokens(model, text)


# Register the OpenAI strategy at import time so ``provider_for("openai")``
# resolves it and both ``GradeConfig`` / ``DraftConfig`` validators accept
# ``provider="openai"`` (DEC-003 of #135; US-002 of #136).
register_provider(OpenAIProvider())


__all__ = (
    "AnthropicProvider",
    "ExceptionCategory",
    "LLMProvider",
    "OpenAIProvider",
    "UsageMetrics",
    "provider_for",
    "register_provider",
)
