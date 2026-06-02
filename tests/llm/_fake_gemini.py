"""Hand-rolled fake for the Gemini client surface (#137 US-004, DEC-011).

Mirrors :mod:`tests.llm._fake` (``FakeAnthropicClient``) — tests register
expectations via :meth:`FakeGeminiClient.expect_messages_create` and the
fake's ``messages.create`` consumes one matching expectation per call
(FIFO); unexpected calls raise loudly.

Hand-rolled rather than ``MagicMock``-driven for the same reason as the
Anthropic fake: ``MagicMock`` auto-passes everything, which would silently
mask mismatches and violate ``testing-signal.md``. The fake satisfies the
``.messages.create(**kwargs)`` shape the orchestrator
(:func:`signalforge.llm.client.call_llm`) calls — the LOAD-BEARING entry
point per DEC-011. Production code reaches that shape via the
:class:`signalforge.llm.providers._GeminiClientAdapter` façade over the real
SDK's native ``client.models.generate_content``; tests inject this fake
directly via the ``client=`` kwarg, bypassing the adapter.

The response-shape dataclasses (``FakeGeminiPart`` /
``FakeGeminiContent`` / ``FakeGeminiCandidate`` / ``FakeGeminiUsageMetadata``
/ ``FakeGeminiResponse``) satisfy exactly the attribute surface
:meth:`GeminiProvider.extract_text_blocks` and
:meth:`GeminiProvider.extract_usage` read — no more, no less. Cache-token
fields are absent because the provider has
``supports_prompt_caching=False`` and reports zeros regardless (DEC-003).

Lives under ``tests/llm/`` and is never imported from production code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ---- Response value objects -----------------------------------------------


@dataclass
class FakeGeminiPart:
    """Stand-in for a Gemini content part (``response.candidates[i].content.parts[j]``).

    :meth:`signalforge.llm.providers.GeminiProvider.extract_text_blocks` reads
    only ``part.text``; non-text parts are filtered out.
    """

    text: str


@dataclass
class FakeGeminiContent:
    """Stand-in for ``candidate.content`` (the inner content namespace).

    ``role`` defaults to ``"model"`` because that's the role Gemini stamps on
    generation output; tests rarely care, but exposing the field keeps the
    shape honest.
    """

    parts: list[FakeGeminiPart]
    role: str = "model"


@dataclass
class _FakeFinishReason:
    """Stand-in for the SDK's ``FinishReason`` enum.

    The real SDK exposes ``finish_reason`` as an enum whose ``.name`` is the
    stable surface (``"STOP"`` / ``"SAFETY"`` / ``"MAX_TOKENS"`` / etc.).
    :meth:`GeminiProvider.extract_text_blocks` reads ``finish_reason.name``
    first, falling back to the value itself; this lightweight wrapper exposes
    only the ``.name`` attribute the production path inspects.
    """

    name: str


class FakeGeminiCandidate:
    """Stand-in for one entry in ``response.candidates``.

    Plain class (not a dataclass) so the constructor can accept ``finish_reason``
    as either a bare string (ergonomic for tests) or a pre-built
    :class:`_FakeFinishReason`, wrapping the string form before storage so the
    production extraction path — which reads ``finish_reason.name`` — sees the
    same shape it would from the real SDK's enum.
    """

    content: FakeGeminiContent | None
    finish_reason: _FakeFinishReason

    def __init__(
        self,
        content: FakeGeminiContent | None,
        finish_reason: str | _FakeFinishReason = "STOP",
    ) -> None:
        self.content = content
        if isinstance(finish_reason, str):
            self.finish_reason = _FakeFinishReason(name=finish_reason)
        else:
            self.finish_reason = finish_reason


@dataclass
class FakeGeminiUsageMetadata:
    """Stand-in for ``response.usage_metadata``.

    Exposes only the two fields :meth:`GeminiProvider.extract_usage` reads:
    ``prompt_token_count`` → ``input_tokens`` and ``candidates_token_count``
    → ``output_tokens``. ``cached_content_token_count`` is accepted on the
    constructor for tests that want to assert the provider still reports
    cache fields as 0 even when the SDK happens to expose one — but the
    provider never reads it (DEC-003: cache flags are False so the
    orchestrator reports 0 unconditionally).
    """

    prompt_token_count: int
    candidates_token_count: int
    cached_content_token_count: int = 0


@dataclass
class FakeGeminiResponse:
    """Stand-in for the ``messages.create`` response (which production code
    maps to the SDK's ``models.generate_content`` result).

    Carries only the attributes the provider extracts: ``candidates``
    (walked by ``extract_text_blocks``) and ``usage_metadata`` (read by
    ``extract_usage``).
    """

    candidates: list[FakeGeminiCandidate]
    usage_metadata: FakeGeminiUsageMetadata


@dataclass
class FakeGeminiCountTokensResponse:
    """Stand-in for ``models.count_tokens`` response (US-007).

    The real SDK's ``CountTokensResponse`` exposes ``total_tokens: int |
    None``; :meth:`GeminiProvider.estimate_input_tokens` reads only that
    field. Tests queue a fake returning the canned count via
    :meth:`FakeGeminiClient.expect_count_tokens`.
    """

    total_tokens: int | None


# ---- Expectation machinery -------------------------------------------------


# A "matching" predicate is either a dict (subset match against the kwargs
# dict the seam passes) or a callable returning bool. Mirrors the Anthropic
# fake's ``_Matcher`` type verbatim.
_Matcher = dict[str, Any] | Callable[[dict[str, Any]], bool]


@dataclass
class _CreateExpectation:
    matching: _Matcher
    returns: object | BaseException


def _matches(matcher: _Matcher, kwargs: dict[str, Any]) -> bool:
    """Apply a matcher to the kwargs of an SDK call.

    Copied from :mod:`tests.llm._fake` rather than imported to keep
    cross-test imports off the fake's surface. A dict matcher is a subset
    match: every key in the matcher must be present in ``kwargs`` with an
    equal value. A callable matcher receives the full kwargs dict and must
    return bool.
    """
    if callable(matcher):
        return bool(matcher(kwargs))
    for key, expected in matcher.items():
        if key not in kwargs:
            return False
        if kwargs[key] != expected:
            return False
    return True


@dataclass
class _FakeGeminiMessages:
    """Implements the ``messages`` namespace on the fake client.

    The orchestrator calls ``client.messages.create(**kwargs)``; this
    namespace consumes one queued expectation per call. ``count_tokens`` is
    NOT modelled — :class:`GeminiProvider` declares
    ``supports_token_count=False`` so the orchestrator never calls it, and
    a stray call should fail loudly via the missing-attribute path or via
    the test's own assertion (mirrors :class:`tests.llm._fake_provider.
    _FakeNoCacheMessages` where ``count_tokens`` raises).

    The async sibling :class:`_FakeGeminiAsyncMessages` (reachable via
    ``client.aio.messages``) shares the same ``_create_queue`` so tests can
    mix sync drains and async drains against one fake instance. Traces to
    DEC-012 of ``plans/super/186-grade-asyncio-parallel.md``.
    """

    _create_queue: list[_CreateExpectation] = field(default_factory=list)
    _create_calls: list[dict[str, Any]] = field(default_factory=list)

    def _pop_create(self, kwargs: dict[str, Any]) -> Any:
        """Shared queue-popping logic for sync + async ``create``."""
        self._create_calls.append(kwargs)
        if not self._create_queue:
            raise AssertionError(f"unexpected messages.create call: {kwargs!r}")
        expectation = self._create_queue[0]
        if not _matches(expectation.matching, kwargs):
            raise AssertionError(
                f"unexpected messages.create call: {kwargs!r} did not match expectation "
                f"{expectation.matching!r}"
            )
        self._create_queue.pop(0)
        if isinstance(expectation.returns, BaseException):
            raise expectation.returns
        return expectation.returns

    def create(self, **kwargs: Any) -> Any:
        return self._pop_create(kwargs)

    def count_tokens(self, **kwargs: Any) -> Any:
        # The Gemini provider declares ``supports_token_count=False`` so the
        # orchestrator must never call this. Mirroring
        # ``_FakeNoCacheMessages.count_tokens``, this raises loudly to turn a
        # silent gating regression into a hard test failure.
        raise AssertionError(
            "count_tokens must never be called for a provider with supports_token_count=False"
        )


@dataclass
class _FakeGeminiAsyncMessages:
    """Async sibling of :class:`_FakeGeminiMessages`, reachable via
    ``client.aio.messages``. ``async create`` delegates to the same
    ``_FakeGeminiMessages._pop_create`` helper as sync ``create``, so a single
    :class:`FakeGeminiClient` can drive both sync
    :func:`signalforge.llm.client.call_llm` and async
    :func:`signalforge.llm.client.call_llm_async` paths interchangeably.
    Traces to DEC-012.
    """

    _sync: _FakeGeminiMessages

    async def create(self, **kwargs: Any) -> Any:
        return self._sync._pop_create(kwargs)

    async def count_tokens(self, **kwargs: Any) -> Any:
        # Parity with sync: capability-flag-gate regression turns loud.
        raise AssertionError(
            "count_tokens must never be called for a provider with supports_token_count=False"
        )


@dataclass
class _CountTokensExpectation:
    matching: _Matcher
    returns: object | BaseException


@dataclass
class _FakeGeminiModels:
    """Implements the ``models`` namespace on the fake client (US-007).

    Only ``count_tokens(**kwargs)`` is modelled — the production path's
    ``models.generate_content`` is routed through ``.messages.create`` via
    :class:`signalforge.llm.providers._GeminiMessagesAdapter` rather than
    being called directly on the client. The US-007 estimate path bypasses
    the messages façade and reaches ``client.models.count_tokens`` natively.

    The async sibling :class:`_FakeGeminiAsyncModels` (reachable via
    ``client.aio.models``, mirroring the real SDK's namespace) shares the
    same ``_count_queue`` so tests can mix sync and async ``count_tokens``
    drains against one fake instance. Traces to DEC-012 of
    ``plans/super/186-grade-asyncio-parallel.md``.
    """

    _count_queue: list[_CountTokensExpectation] = field(default_factory=list)
    _count_calls: list[dict[str, Any]] = field(default_factory=list)

    def _pop_count_tokens(self, kwargs: dict[str, Any]) -> Any:
        """Shared queue-popping logic for sync + async ``count_tokens``."""
        self._count_calls.append(kwargs)
        if not self._count_queue:
            raise AssertionError(f"unexpected models.count_tokens call: {kwargs!r}")
        expectation = self._count_queue[0]
        if not _matches(expectation.matching, kwargs):
            raise AssertionError(
                f"unexpected models.count_tokens call: {kwargs!r} did not match expectation "
                f"{expectation.matching!r}"
            )
        self._count_queue.pop(0)
        if isinstance(expectation.returns, BaseException):
            raise expectation.returns
        return expectation.returns

    def count_tokens(self, **kwargs: Any) -> Any:
        return self._pop_count_tokens(kwargs)


@dataclass
class _FakeGeminiAsyncModels:
    """Async sibling of :class:`_FakeGeminiModels`, reachable via
    ``client.aio.models``. ``async count_tokens`` delegates to the same
    ``_FakeGeminiModels._pop_count_tokens`` helper. Traces to DEC-012.
    """

    _sync: _FakeGeminiModels

    async def count_tokens(self, **kwargs: Any) -> Any:
        return self._sync._pop_count_tokens(kwargs)


@dataclass
class _FakeGeminiAioNamespace:
    """The ``.aio`` namespace on :class:`FakeGeminiClient` — exposes both
    async ``messages`` and async ``models`` surfaces, mirroring the real
    Gemini SDK's ``client.aio.models.generate_content`` /
    ``client.aio.models.count_tokens`` shape. DEC-012.
    """

    messages: _FakeGeminiAsyncMessages
    models: _FakeGeminiAsyncModels


class FakeGeminiClient:
    """Explicit fake for the Gemini client surface; calls outside the queued
    expectations raise :class:`AssertionError`.

    Each ``expect_messages_create`` enqueues one expectation; calls consume
    them FIFO. Tests must call :meth:`assert_all_expectations_met` at the
    end — a non-empty queue at end-of-test is a leftover-expectation bug.

    Structurally satisfies the orchestrator's neutral ``.messages`` surface:
    ``client.messages.create(**kwargs)`` is exactly what
    :func:`signalforge.llm.client.call_llm` invokes. Inject via
    ``call_llm(..., client=<FakeGeminiClient>)`` to drive the Gemini path
    end-to-end without any real SDK on the path.

    Also exposes ``.models.count_tokens(**kwargs)`` for the US-007 estimate
    path — :meth:`GeminiProvider.estimate_input_tokens` calls the native
    SDK ``models.count_tokens`` surface, not the orchestrator's
    ``.messages.create`` façade.
    """

    def __init__(self) -> None:
        self._messages = _FakeGeminiMessages()
        self.messages = self._messages
        self._models = _FakeGeminiModels()
        self.models = self._models
        # The ``.aio`` namespace carries the async sibling surface; both
        # sync ``self.messages.create`` / ``self.models.count_tokens`` and
        # async ``self.aio.messages.create`` / ``self.aio.models.count_tokens``
        # drain the SAME queues. Mirrors the real Gemini SDK's
        # ``client.aio.models.*`` shape. DEC-012.
        self.aio = _FakeGeminiAioNamespace(
            messages=_FakeGeminiAsyncMessages(_sync=self._messages),
            models=_FakeGeminiAsyncModels(_sync=self._models),
        )

    def expect_messages_create(
        self,
        *,
        matching: _Matcher,
        returns: object | BaseException,
    ) -> None:
        """Queue one expectation for ``messages.create``.

        ``matching`` is a dict (subset match against the kwargs) or a
        predicate callable. ``returns`` is either the response object to
        return or a :class:`BaseException` instance to raise.
        """
        self._messages._create_queue.append(_CreateExpectation(matching=matching, returns=returns))

    def expect_count_tokens(
        self,
        *,
        matching: _Matcher,
        returns: object | BaseException,
    ) -> None:
        """Queue one expectation for ``models.count_tokens`` (US-007).

        ``matching`` is a dict (subset match against the kwargs) or a
        predicate callable. ``returns`` is either a
        :class:`FakeGeminiCountTokensResponse` (or any object with a
        ``total_tokens`` attribute) to return, or a :class:`BaseException`
        instance to raise.
        """
        self._models._count_queue.append(
            _CountTokensExpectation(matching=matching, returns=returns)
        )

    def assert_all_expectations_met(self) -> None:
        """Raise :class:`AssertionError` if any expectations remain unconsumed."""
        leftover: list[str] = []
        if self._messages._create_queue:
            leftover.append(f"{len(self._messages._create_queue)} messages.create expectation(s)")
        if self._models._count_queue:
            leftover.append(f"{len(self._models._count_queue)} models.count_tokens expectation(s)")
        if leftover:
            raise AssertionError("unconsumed expectations: " + ", ".join(leftover))

    @property
    def create_calls(self) -> list[dict[str, Any]]:
        """Inspector for tests that want to assert on the kwargs the seam
        passed to ``messages.create``."""
        return list(self._messages._create_calls)

    @property
    def count_tokens_calls(self) -> list[dict[str, Any]]:
        """Inspector for tests that want to assert on the kwargs the
        estimator passed to ``models.count_tokens``."""
        return list(self._models._count_calls)


__all__ = [
    "FakeGeminiCandidate",
    "FakeGeminiClient",
    "FakeGeminiContent",
    "FakeGeminiCountTokensResponse",
    "FakeGeminiPart",
    "FakeGeminiResponse",
    "FakeGeminiUsageMetadata",
]
