"""Test-only no-cache LLM provider — the AC #2 provider-neutrality proof.

US-005 of issue #135 (provider-neutral LLM seam). This module is the literal
demonstration of DEC-011: wiring a brand-new LLM provider takes only a small
:class:`signalforge.llm.providers.LLMProvider` subclass (its client shim, its
request-kwargs builder, text/usage extraction, and an exception → category map)
plus a :func:`signalforge.llm.providers.register_provider` call — nothing else.

The provider here declares ``supports_prompt_caching = False`` and
``supports_token_count = False`` so it exercises the orchestrator's capability
degrade paths (DEC-008):

* No ``count_tokens`` call is ever issued by ``call_llm`` (the fake client
  raises loudly if one is attempted — proving the gate holds).
* No ``cache_control`` marker and no extended-cache beta header is built (the
  orchestrator gates this, and the provider also never emits one).
* The reported cache-token counts are 0, and the dual-zero cache-anomaly
  WARNING is suppressed.

Lives under ``tests/`` and is NEVER imported from production code (mirrors
``tests/llm/_fake.py`` / ``tests/warehouse/_fake.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from signalforge.llm.providers import (
    ExceptionCategory,
    LLMProvider,
    UsageMetrics,
)

#: The registry key the neutrality test selects via ``GradeConfig(provider=...)``.
FAKE_NOCACHE_PROVIDER_NAME = "fake-nocache"


@dataclass
class FakeNoCacheUsage:
    """Usage object carrying NO cache fields (a no-cache provider omits them)."""

    input_tokens: int = 100
    output_tokens: int = 40


@dataclass
class FakeNoCacheResponse:
    """Canned ``messages.create`` response for the no-cache fake client.

    ``text`` is the single content payload; ``usage`` carries only the
    required input/output token counts and no cache accounting.
    """

    text: str
    usage: FakeNoCacheUsage = field(default_factory=FakeNoCacheUsage)
    model: str = "fake-nocache-judge"


@dataclass
class _FakeNoCacheMessages:
    """The ``.messages`` namespace on the no-cache fake client.

    ``create`` records every kwargs dict so the neutrality test can assert no
    ``cache_control`` marker / beta header was built. ``count_tokens`` raises:
    because the provider declares ``supports_token_count = False`` the
    orchestrator must never call it — if it does, this raise turns the silent
    gating regression into a loud failure.
    """

    response_text: str
    create_calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> FakeNoCacheResponse:
        self.create_calls.append(kwargs)
        return FakeNoCacheResponse(text=self.response_text)

    def count_tokens(self, **kwargs: Any) -> Any:
        raise AssertionError(
            "count_tokens must never be called for a provider with supports_token_count=False"
        )


class FakeNoCacheClient:
    """Minimal client returned by :meth:`FakeNoCacheProvider.make_client`.

    Structurally satisfies the orchestrator's neutral ``.messages`` surface
    (``create`` / ``count_tokens``). Tests can also construct one directly and
    inject it via ``call_llm(..., client=...)`` to inspect ``create_calls``.
    """

    def __init__(self, response_text: str = '{"ok": true}') -> None:
        self._messages = _FakeNoCacheMessages(response_text=response_text)
        self.messages = self._messages

    @property
    def create_calls(self) -> list[dict[str, Any]]:
        """The kwargs dicts passed to ``messages.create`` (inspector for tests)."""
        return list(self._messages.create_calls)


@dataclass
class _FakeNoCacheAsyncMessages:
    """Async ``.messages`` namespace on :class:`_FakeNoCacheAsyncClient`.

    Mirrors :class:`_FakeNoCacheMessages` byte-for-byte but with ``async``
    methods so :func:`signalforge.llm.client.call_llm_async` can ``await``
    each call. ``count_tokens`` still raises loudly because the provider
    declares ``supports_token_count=False`` — if the async orchestrator
    invokes it that's a silent gating regression and the raise turns it
    into a loud failure.
    """

    response_text: str
    create_calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> FakeNoCacheResponse:
        self.create_calls.append(kwargs)
        return FakeNoCacheResponse(text=self.response_text)

    async def count_tokens(self, **kwargs: Any) -> Any:
        raise AssertionError(
            "count_tokens must never be called for a provider with supports_token_count=False"
        )


class _FakeNoCacheAsyncClient:
    """Async sibling of :class:`FakeNoCacheClient` (US-006 of issue #186).

    Structurally satisfies the orchestrator's neutral async ``.messages``
    surface (awaitable ``create`` / ``count_tokens``). Constructed by
    :meth:`FakeNoCacheProvider.make_async_client` when the provider is
    declared async-capable; not exported because the call_llm_async tests
    drive it through :func:`signalforge.llm.providers.provider_for` rather
    than directly.
    """

    def __init__(self, response_text: str = '{"ok": true}') -> None:
        self._messages = _FakeNoCacheAsyncMessages(response_text=response_text)
        self.messages = self._messages


class FakeNoCacheProvider(LLMProvider):
    """A test-only :class:`LLMProvider` with neither caching nor token counting.

    This whole class IS the AC #2 wiring proof: register an instance and the
    seam accepts it everywhere — ``provider_for(name)`` resolves it,
    ``GradeConfig(provider=name)`` validates, and ``call_llm`` drives it with no
    other code change.

    The default ``response_text`` is a grade-judge JSON payload so the class can
    be registered and driven through ``grade_artifacts`` with no extra setup;
    callers wanting a per-(criterion, artifact) response can build their own
    :class:`FakeNoCacheClient` and inject it.
    """

    name = FAKE_NOCACHE_PROVIDER_NAME
    supports_prompt_caching = False
    supports_token_count = False
    # Inherits ``supports_async = True`` from the ABC (see the default in
    # :class:`signalforge.llm.providers.LLMProvider`). Sync-only fixtures
    # for the US-006 capability-gate test subclass :class:`FakeNoCacheProvider`
    # with ``supports_async = False`` rather than carrying an instance flag —
    # the ABC declares ``supports_async`` as a ``ClassVar`` so subclass
    # override is the type-checker-clean seam (see
    # :class:`FakeSyncOnlyNoCacheProvider` below).

    def __init__(self, response_text: str = '{"ok": true}') -> None:
        self._response_text = response_text

    def make_client(self) -> object:
        """Build the tiny canned-response client (no SDK, no network)."""
        return FakeNoCacheClient(response_text=self._response_text)

    def make_async_client(self) -> Any:
        """Build a tiny async-capable canned-response client (US-006 of #186).

        Returns an :class:`_FakeNoCacheAsyncClient` whose ``messages.create``
        / ``messages.count_tokens`` are awaitable wrappers around the same
        canned-response surface :meth:`make_client` exposes. ``Any`` return
        type sidesteps the strict ``_LLMAsyncClientProtocol`` override
        check at the ABC boundary (mirrors :class:`AnthropicProvider`'s
        explicit-protocol return for the same reason).

        A sync-only provider subclasses this class and sets
        ``supports_async = False``; the orchestrator
        (``call_llm_async``) raises
        :class:`signalforge.llm.errors.LLMProviderAsyncUnsupportedError`
        at entry BEFORE this method is reached, so this implementation
        is exercised only when the subclass leaves the flag at the
        inherited ``True`` (or doesn't subclass at all).
        """
        return _FakeNoCacheAsyncClient(response_text=self._response_text)

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
        """Build a minimal create-kwargs dict.

        Because the provider does NOT support prompt caching, it NEVER emits a
        ``cache_control`` marker or an extended-cache beta header regardless of
        ``cache_marker_active`` (the orchestrator already resolves that flag to
        ``False`` for a non-caching provider — this is belt-and-braces). The two
        blocks are concatenated into one plain message payload.
        """
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": cached_block},
                        {"type": "text", "text": dynamic_block},
                    ],
                }
            ],
        }

    def build_count_tokens_kwargs(
        self,
        *,
        system: str,
        cached_block: str,
        model: str,
    ) -> dict[str, Any]:
        """Never invoked — ``supports_token_count`` is ``False``.

        The orchestrator skips the pre-send count gate entirely for a provider
        that cannot count tokens (DEC-008), so this method is unreachable on the
        ``call_llm`` path. It raises to make any accidental call loud.
        """
        raise NotImplementedError(
            "build_count_tokens_kwargs is unreachable when supports_token_count=False"
        )

    def extract_text_blocks(self, response: object) -> tuple[str, ...]:
        """Pull the single text payload off the canned response."""
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            raise AssertionError("FakeNoCacheResponse is missing a string `text`.")
        return (text,)

    def extract_usage(self, response: object) -> UsageMetrics:
        """Return :class:`UsageMetrics` with both cache-token fields at 0.

        A no-cache provider has nothing to report for cache creation/read; the
        :class:`UsageMetrics` defaults already pin them to 0 (DEC-002), and the
        orchestrator reports 0 too (DEC-008).
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            raise AssertionError("FakeNoCacheResponse is missing `usage`.")
        return UsageMetrics(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )

    def is_clean_completion(self, response: object) -> bool:
        """Return ``True`` unconditionally — the canned response is always
        a fully-emitted body (#155 US-001).

        The fake's :class:`FakeNoCacheResponse` has no concept of a
        finish reason; the neutrality tests drive the happy path so the
        gate must let every canned response through. A test wanting to
        exercise the unclean-path contract uses one of the concrete
        provider tests (which speak their vendor's response shape) or
        a per-test subclass.
        """
        del response
        return True

    def classify_exception(self, exc: BaseException) -> ExceptionCategory:
        """Minimal exception → category map satisfying the ABC.

        Maps :class:`TimeoutError` to :attr:`ExceptionCategory.CONNECTION` (a
        plausible transient) and everything else to
        :attr:`ExceptionCategory.NO_RETRY`. The canned client never raises on
        the happy path, so this is exercised only if a future test injects a
        failing client.
        """
        if isinstance(exc, TimeoutError):
            return ExceptionCategory.CONNECTION
        return ExceptionCategory.NO_RETRY

    def estimate_input_tokens(
        self,
        model: str,
        text: str,
        *,
        system: str = "",
        client: object | None = None,
    ) -> int:
        """Return a trivial word-count proxy for ``system + text`` (#136 US-005).

        The neutrality test exercises orchestrator dispatch, not real
        token counting; ``len(text.split())`` is a deterministic
        non-zero positive answer that any rendered estimate will treat
        as valid. Mirrors :class:`FakeNoCacheProvider`'s overall posture
        of declaring the right capability flags and answering each ABC
        method with a minimal honest value.
        """
        del model, client  # neither is consulted on the proxy path
        # Join with a delimiter so the last word of ``system`` and the
        # first word of ``text`` don't merge into a single token under
        # ``.split()`` (boundary-word undercount; PR #152 CodeRabbit
        # catch).
        return len(f"{system} {text}".split())


class FakeSyncOnlyNoCacheProvider(FakeNoCacheProvider):
    """Sync-only sibling of :class:`FakeNoCacheProvider` (US-006 of issue #186).

    Declares ``supports_async = False`` so the
    :func:`signalforge.llm.client.call_llm_async` capability-gate test
    can exercise the
    :class:`signalforge.llm.errors.LLMProviderAsyncUnsupportedError`
    short-circuit at orchestrator entry. Subclass (not instance flag)
    because the ABC declares ``supports_async`` as a ``ClassVar`` —
    subclass override is the type-checker-clean seam (DEC-005 of #186
    establishes the capability flag; this is the sync-only variant
    that demonstrates the gate fires).

    :meth:`make_async_client` defensively raises so any accidental call
    past the orchestrator gate is loud rather than silent — the gate
    runs at ``call_llm_async`` entry BEFORE any client construction, so
    in correct use this raise is unreachable.
    """

    supports_async = False

    def make_async_client(self) -> Any:
        raise NotImplementedError(
            "FakeSyncOnlyNoCacheProvider.make_async_client: provider declares "
            "supports_async=False; the orchestrator must short-circuit with "
            "LLMProviderAsyncUnsupportedError before reaching this method.",
        )


__all__ = [
    "FAKE_NOCACHE_PROVIDER_NAME",
    "FakeNoCacheClient",
    "FakeNoCacheProvider",
    "FakeNoCacheResponse",
    "FakeNoCacheUsage",
    "FakeSyncOnlyNoCacheProvider",
]
