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


class _GeminiMessagesAdapter:
    """Façade exposing ``.create(**kwargs)`` over the SDK's native
    ``client.models.generate_content(...)`` surface.

    The provider-neutral orchestrator in :mod:`signalforge.llm.client` always
    calls ``client.messages.create(**kwargs)`` regardless of vendor. Google's
    ``google-genai`` SDK has no native ``.messages`` namespace — generation
    lives at ``client.models.generate_content(...)``. The shim in
    :mod:`signalforge.llm._gemini_client` declares the
    :class:`signalforge.llm._gemini_client.GeminiClientProtocol` façade but
    leaves the adapter body to US-002 (this module) so the shim stays free of
    behaviour, and every façade-to-SDK forwarding stays in one place.

    The kwargs dict produced by :meth:`GeminiProvider.build_create_kwargs`
    matches the native ``models.generate_content`` signature exactly, so this
    adapter forwards ``**kwargs`` straight through.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        """Forward to the SDK's native ``models.generate_content``."""
        return self._client.models.generate_content(**kwargs)

    def count_tokens(self, **kwargs: Any) -> Any:
        """Forward to the SDK's native ``models.count_tokens``.

        Unused on the ``call_llm`` happy path — :class:`GeminiProvider`
        declares ``supports_token_count = False`` (DEC-003) so the
        orchestrator skips the pre-send count gate. Kept on the adapter for
        the US-007 ``--estimate`` path and so the façade structurally
        satisfies the neutral client protocol.
        """
        return self._client.models.count_tokens(**kwargs)


class _GeminiClientAdapter:
    """Wraps a real ``google.genai.Client`` so it satisfies the orchestrator's
    neutral ``.messages.{create,count_tokens}`` surface.

    Constructed only inside :meth:`GeminiProvider.make_client` from the
    bare client returned by
    :func:`signalforge.llm._gemini_client._make_gemini_client`. Test
    environments inject :class:`tests.llm._fake_gemini.FakeGeminiClient`
    (US-004) directly via the ``client=`` kwarg on ``call_llm`` and never see
    this adapter — keeping the adapter logic narrowly on the production path.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.messages = _GeminiMessagesAdapter(client)

    @property
    def models(self) -> Any:
        """Expose the SDK's native ``.models`` namespace for any caller
        (e.g. the US-007 token-estimator) that bypasses the ``.messages``
        façade. The native surface is the only way to reach
        ``client.models.count_tokens`` for an estimate."""
        return self._client.models


class GeminiProvider(LLMProvider):
    """Google Gemini strategy behind the generic LLM orchestrator (#137).

    Per DEC-003, both capability flags are ``False``: the v0.3 Gemini wiring
    ships **without** Anthropic-style prompt caching and **without** a
    pre-send token-count gate. The orchestrator therefore:

    * builds no ``cache_control`` marker and no extended-cache beta header,
    * skips the pre-send :class:`signalforge.llm.errors.LLMCacheTooLargeError`
      gate (``messages.count_tokens`` is never called on the happy path),
    * reports ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
      as 0, and suppresses the dual-zero cache-anomaly WARNING.

    The Google ``google-genai`` SDK noise stays confined to
    :mod:`signalforge.llm._gemini_client` (DEC-001). This class never imports
    a ``google.genai`` symbol at module scope — every SDK touch is via the
    shim's helpers (:func:`_make_gemini_client`,
    :func:`_load_gemini_exception_classes`) so a base install without the
    ``[gemini]`` extra still imports this module cleanly (DEC-015).

    Server-side JSON enforcement (DEC-018). :meth:`build_create_kwargs`
    sets ``response_mime_type="application/json"`` on the
    ``GenerateContentConfig``. Belt-and-braces with the tolerant
    :func:`signalforge.llm._json.extract_json_payload` (issue #144) — the
    server-side flag eliminates the prose-preamble drift class; the
    tolerant parser remains the fallback if a future model strips the flag.
    """

    name = "gemini"
    supports_prompt_caching = False
    supports_token_count = False

    def make_client(self) -> object:
        """Build the real ``google.genai.Client`` via the shim, wrapped in
        the ``.messages`` façade adapter.

        The shim's :func:`signalforge.llm._gemini_client._make_gemini_client`
        returns the bare SDK client (which has no native ``.messages``); the
        adapter exposes ``.messages.create`` so the orchestrator calls a
        single, vendor-neutral surface. DEC-001 / DEC-004.
        """
        from signalforge.llm._gemini_client import _make_gemini_client

        return _GeminiClientAdapter(_make_gemini_client())

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
        """Build the kwargs for ``client.models.generate_content`` (DEC-004 +
        DEC-018).

        Three load-bearing shape choices:

        * ``system`` → ``GenerateContentConfig.system_instruction``. Gemini
          carries the system message on the request config, not as a
          conversation turn.
        * ``cached_block + "\\n\\n" + dynamic_block`` concatenated into a
          single user-role ``contents`` entry. The provider has no caching
          (DEC-003), so there is no value in keeping the two blocks separate.
        * ``response_mime_type="application/json"`` on the config (DEC-018).
          Server-side JSON enforcement removes the prose-preamble drift
          class entirely; the tolerant parser in
          :mod:`signalforge.llm._json` remains the fallback.

        ``cache_marker_active`` is intentionally ignored — both capability
        flags are ``False`` so the orchestrator already resolves it to
        ``False``; passing it through would still be a no-op, but skipping
        the read keeps the request shape obviously vendor-correct (no stray
        ``cache_control`` field anywhere; no ``extra_headers`` key on the
        returned dict).

        ``config`` is built as a plain ``dict`` (``GenerateContentConfigDict``
        in the SDK's type union) so this module never imports
        ``google.genai.types`` at any scope — keeping the
        :mod:`tests.llm.test_gemini_client_confinement` line gate green and
        a base install (no ``[gemini]`` extra) able to import this module
        cleanly.
        """
        contents = [cached_block + "\n\n" + dynamic_block]
        config: dict[str, Any] = {
            "system_instruction": system,
            "response_mime_type": "application/json",
            "max_output_tokens": max_tokens,
        }
        return {
            "model": model,
            "contents": contents,
            "config": config,
        }

    def build_count_tokens_kwargs(
        self,
        *,
        system: str,
        cached_block: str,
        model: str,
    ) -> dict[str, Any]:
        """Never invoked — ``supports_token_count`` is ``False`` (DEC-003).

        The orchestrator skips the pre-send count gate entirely for a
        provider that cannot count tokens, so this method is unreachable on
        the :func:`signalforge.llm.client.call_llm` path. Mirrors the
        :class:`tests.llm._fake_provider.FakeNoCacheProvider` precedent:
        raise rather than return a placeholder dict, so any accidental call
        is loud.
        """
        raise NotImplementedError(
            "build_count_tokens_kwargs is unreachable when supports_token_count=False"
        )

    def extract_text_blocks(self, response: object) -> tuple[str, ...]:
        """Pull text from each candidate's parts (DEC-005).

        Walks ``response.candidates``; for each candidate, walks
        ``candidate.content.parts`` and collects ``part.text`` where present
        and non-empty. When NO candidate yields any non-empty text part
        (safety-filtered, recitation, prohibited content, no candidates at
        all), raises
        :class:`signalforge.llm.errors.LLMResponseFormatError` whose message
        names the first candidate's ``finish_reason`` so the operator can
        diagnose. ``finish_reason`` is rendered via ``repr`` so a hostile
        value cannot inject control characters into logs.
        """
        from signalforge.llm.errors import LLMResponseFormatError

        candidates = getattr(response, "candidates", None) or ()
        blocks: list[str] = []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            if content is None:
                continue
            parts = getattr(content, "parts", None) or ()
            for part in parts:
                text = getattr(part, "text", None)
                if isinstance(text, str) and text:
                    blocks.append(text)
        if blocks:
            return tuple(blocks)
        # No text — surface the first candidate's finish_reason so the
        # operator knows whether the model was blocked, hit a length cap,
        # produced an empty/None content, etc.
        finish_reason: object = "unknown"
        if candidates:
            fr_attr = getattr(candidates[0], "finish_reason", None)
            if fr_attr is not None:
                # FinishReason is an enum; ``.name`` is the stable surface
                # but fall back to its repr if a future SDK changes shape.
                finish_reason = getattr(fr_attr, "name", fr_attr)
        raise LLMResponseFormatError(
            f"Gemini response produced no text (finish_reason={finish_reason!r}).",
        )

    def extract_usage(self, response: object) -> UsageMetrics:
        """Extract token economics from a Gemini response.

        Reads ``response.usage_metadata.{prompt_token_count,
        candidates_token_count}``; both cache fields default to 0 because
        the provider has no Anthropic-style prompt caching (DEC-003).
        Missing fields default to 0 via ``getattr``; the whole
        ``usage_metadata`` being absent surfaces an
        :class:`LLMResponseFormatError`.
        """
        from signalforge.llm.errors import LLMResponseFormatError

        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            raise LLMResponseFormatError(
                "Gemini response is missing the `usage_metadata` attribute.",
            )
        input_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0
        return UsageMetrics(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )

    def classify_exception(self, exc: BaseException) -> ExceptionCategory:
        """Map a raised ``google.genai.errors`` instance to a neutral
        category (DEC-006).

        Dispatch order:

        * ``ServerError`` (5xx family) → :attr:`SERVER_ERROR`.
        * ``ClientError`` with HTTP ``code in (401, 403)`` → :attr:`AUTH`.
        * ``ClientError`` with HTTP ``code == 429`` → :attr:`RATE_LIMIT`.
        * Connection-flavoured (``httpx.ConnectError`` /
          ``httpx.TimeoutException``) → :attr:`CONNECTION`. Listed for
          completeness; the SDK may wrap them. Anything else → NO_RETRY.

        ``ServerError`` is checked **before** ``ClientError`` because both
        derive from ``google.genai.errors.APIError``; ``ServerError`` is the
        narrower bucket and routing it first avoids a false-match on a
        future shared parent.

        SDK class identities are loaded via the shim's
        :func:`signalforge.llm._gemini_client._load_gemini_exception_classes`
        so this module never imports ``google.genai`` directly (DEC-001).
        A base install (no ``[gemini]`` extra) lands every exception on
        :attr:`NO_RETRY` cleanly via the empty-tuple fallback (DEC-015).
        """
        from signalforge.llm._gemini_client import _load_gemini_exception_classes

        exc_classes = _load_gemini_exception_classes()
        # Check ServerError before ClientError (both derive from APIError).
        if exc_classes.api_status and isinstance(exc, exc_classes.api_status):
            return ExceptionCategory.SERVER_ERROR
        if exc_classes.rate_limit and isinstance(exc, exc_classes.rate_limit):
            code = getattr(exc, "code", None)
            if code in (401, 403):
                return ExceptionCategory.AUTH
            if code == 429:
                return ExceptionCategory.RATE_LIMIT
            return ExceptionCategory.NO_RETRY
        # Connection-flavoured: httpx leaks through the SDK on a hard
        # network failure. Imported lazily so the SDK transitive isn't
        # required at module import. ImportError → fall through to NO_RETRY.
        try:
            import httpx
        except ImportError:  # pragma: no cover - httpx ships with google-genai
            return ExceptionCategory.NO_RETRY
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
            return ExceptionCategory.CONNECTION
        return ExceptionCategory.NO_RETRY


# Register the Gemini strategy at import time so ``provider_for("gemini")``
# resolves it. Mirrors the Anthropic registration above; the registry is a
# plugin point designed to grow (DEC-003).
register_provider(GeminiProvider())


__all__ = (
    "AnthropicProvider",
    "ExceptionCategory",
    "GeminiProvider",
    "LLMProvider",
    "UsageMetrics",
    "provider_for",
    "register_provider",
)
