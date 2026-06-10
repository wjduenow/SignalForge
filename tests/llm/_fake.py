"""Hand-rolled fake for ``anthropic.Anthropic`` (US-006, DEC-002 / DEC-028).

Tests register expectations via :meth:`expect_count_tokens` and
:meth:`expect_messages_create`. The fake's ``messages.count_tokens`` and
``messages.create`` consume one matching expectation per call (FIFO);
unexpected calls raise loudly.

Mirrors the precedent set by ``tests/warehouse/_fake.py::FakeBigQueryClient``
(see ``.claude/rules/warehouse-adapters.md`` — "Test fakes use an
``expect_*`` helper API"). Hand-rolled rather than ``MagicMock``-driven
because ``MagicMock`` auto-passes everything, which would silently mask
mismatches and violate ``testing-signal.md``.

Lives under ``tests/llm/`` and is never imported from production code.

The placeholder shims (``_StubAnthropicClient`` / ``_StubMessages``) from
the US-005 stub are kept for the protocol-conformance tests in
``test_client_shim.py`` — they cover the structural shape independently
of the expectation-tracking fake.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from signalforge.llm._anthropic_client import _AnthropicMessagesProtocol


@dataclass
class FakeUsage:
    """Stand-in for ``anthropic.types.Usage``.

    Only the fields :func:`signalforge.llm.client.call_llm` reads
    are exposed. ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
    default to 0 so tests that don't care about cache accounting don't
    have to set them; the seam treats them as optional and defaults to 0
    when absent on the real SDK response too.
    """

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeTextBlock:
    """Stand-in for ``anthropic.types.TextBlock``."""

    text: str
    type: str = "text"


@dataclass
class FakeMessage:
    """Stand-in for ``anthropic.types.Message`` (the ``messages.create``
    response).
    """

    content: list[FakeTextBlock]
    usage: FakeUsage
    id: str = "msg_fake_001"
    model: str = "claude-fake-1"
    role: str = "assistant"
    stop_reason: str = "end_turn"
    type: str = "message"


@dataclass
class FakeCountTokensResponse:
    """Stand-in for the ``messages.count_tokens`` response."""

    input_tokens: int


@dataclass
class _FakeResponseWithHeaders:
    """Stand-in for the ``httpx.Response`` hung off an SDK exception /
    success response (#202 US-002).

    Carries only the ``headers`` attribute the rate-limit extractor reads —
    a case-insensitive ``dict`` is enough because
    :meth:`signalforge.llm.providers.AnthropicProvider._headers_from`
    probes for any :class:`collections.abc.Mapping` and the neutral
    parse helper does a plain ``.get(key)`` (the real ``httpx.Headers`` is
    case-insensitive, but tests pass exact lowercased header names, so a
    plain ``dict`` faithfully exercises the parse path).
    """

    headers: dict[str, str]


class FakeRateLimitError(Exception):
    """Test double for ``anthropic.RateLimitError`` (#202 US-002 / DEC-205).

    The real SDK's ``RateLimitError`` (a 429) hangs its
    ``retry-after`` + ``anthropic-ratelimit-*`` headers off
    ``error.response.headers`` (an ``httpx.Response``). This fake reproduces
    exactly that shape — ``self.response.headers`` is the supplied mapping —
    so :meth:`signalforge.llm.providers.AnthropicProvider.extract_rate_limit_info`
    walks the same ``exc.response.headers`` path it walks in production,
    WITHOUT pulling in the real ``anthropic`` / ``httpx`` types.

    Hand-rolled (not ``MagicMock``) for the same reason as
    :class:`FakeAnthropicClient`: an auto-passing mock would silently mask a
    wrong attribute path; an explicit double fails loud
    (``testing-signal.md``).
    """

    def __init__(self, *, headers: dict[str, str] | None = None) -> None:
        super().__init__("fake rate limit")
        # ``headers=None`` models a 429 whose response surfaced no headers at
        # all (the extractor degrades to an EMPTY budget). An empty dict models
        # "response present, but no rate-limit headers".
        self.response = _FakeResponseWithHeaders(headers=headers or {})


@dataclass
class FakeResponseWithHeaders:
    """Test double for a SUCCESS response carrying rate-limit headers
    (#202 US-002 / DEC-205).

    Mirrors the ``response.headers`` surface a 200 ``messages.create`` reply
    exposes (``self.headers`` directly, not nested under ``.response`` — the
    success path the extractor probes via the direct form). Lets a test drive
    :meth:`signalforge.llm.providers.AnthropicProvider.extract_rate_limit_info`'s
    ``response=`` argument without the real SDK.
    """

    headers: dict[str, str]


# A "matching" predicate is either a dict (subset match against the kwargs
# dict the seam passes) or a callable returning bool.
_Matcher = dict[str, Any] | Callable[[dict[str, Any]], bool]


@dataclass
class _CountTokensExpectation:
    matching: _Matcher
    returns: object | Exception


@dataclass
class _MessagesCreateExpectation:
    matching: _Matcher
    returns: object | Exception


def _matches(matcher: _Matcher, kwargs: dict[str, Any]) -> bool:
    """Apply a matcher to the kwargs of an SDK call.

    A dict matcher is a subset match: every key in the matcher must be
    present in ``kwargs`` with an equal value. A callable matcher is
    invoked with the full kwargs dict and must return bool.
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
class _FakeMessages:
    """Implements the ``messages`` namespace on the fake client.

    Both the sync ``count_tokens`` / ``create`` methods AND the async siblings
    on :class:`_FakeAsyncMessages` (reachable via ``client.aio.messages``)
    consume from the SAME ``_count_queue`` / ``_create_queue``. Tests can mix
    sync drains and async drains against one fake instance — the shared queue
    is the proof that production sync (``call_llm``) and async
    (``call_llm_async``) paths exercise identical expectation logic. Traces to
    DEC-012 of ``plans/super/186-grade-asyncio-parallel.md``.
    """

    _count_queue: list[_CountTokensExpectation] = field(default_factory=list)
    _create_queue: list[_MessagesCreateExpectation] = field(default_factory=list)
    _create_calls: list[dict[str, Any]] = field(default_factory=list)
    _count_calls: list[dict[str, Any]] = field(default_factory=list)

    def _pop_count_tokens(self, kwargs: dict[str, Any]) -> Any:
        """Shared queue-popping logic for sync + async ``count_tokens``."""
        self._count_calls.append(kwargs)
        if not self._count_queue:
            raise AssertionError(f"unexpected count_tokens call: {kwargs!r}")
        expectation = self._count_queue[0]
        if not _matches(expectation.matching, kwargs):
            raise AssertionError(
                f"unexpected count_tokens call: {kwargs!r} did not match expectation "
                f"{expectation.matching!r}"
            )
        self._count_queue.pop(0)
        if isinstance(expectation.returns, Exception):
            raise expectation.returns
        return expectation.returns

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
        if isinstance(expectation.returns, Exception):
            raise expectation.returns
        return expectation.returns

    def count_tokens(self, **kwargs: Any) -> Any:
        return self._pop_count_tokens(kwargs)

    def create(self, **kwargs: Any) -> Any:
        return self._pop_create(kwargs)


@dataclass
class _FakeAsyncMessages:
    """Async sibling of :class:`_FakeMessages`, reachable via ``client.aio.messages``.

    Both ``async create`` and ``async count_tokens`` delegate to the SAME
    ``_FakeMessages`` instance's queue-popping helpers, so a single
    :class:`FakeAnthropicClient` can be driven by the sync
    :func:`signalforge.llm.client.call_llm` AND the async
    :func:`signalforge.llm.client.call_llm_async` (US-006) interchangeably.
    Traces to DEC-012.
    """

    _sync: _FakeMessages

    async def count_tokens(self, **kwargs: Any) -> Any:
        return self._sync._pop_count_tokens(kwargs)

    async def create(self, **kwargs: Any) -> Any:
        return self._sync._pop_create(kwargs)


@dataclass
class _FakeAioNamespace:
    """The ``.aio`` namespace on :class:`FakeAnthropicClient` — exposes the
    async messages surface. Mirrors Gemini's ``client.aio.models.*`` pattern
    so all three fakes share one async-namespace shape (DEC-012).
    """

    messages: _FakeAsyncMessages


class FakeAnthropicClient:
    """Explicit fake for ``anthropic.Anthropic``; calls outside the queued
    expectations raise :class:`AssertionError`.

    Each ``expect_*`` enqueues one expectation; calls consume them FIFO.
    Tests must call :meth:`assert_all_expectations_met` at the end (or
    rely on the absence of leftover queue entries via inspection); a
    non-empty queue at the end of a test is a leftover-expectation bug.
    """

    messages: _AnthropicMessagesProtocol

    def __init__(self, project: str = "test") -> None:
        self.project = project
        # The class-level annotation types ``messages`` as the protocol so
        # the FakeAnthropicClient satisfies ``AnthropicClientProtocol``
        # under pyright's invariance rules; ``_messages`` is the concrete
        # backing object the ``expect_*`` helpers reach into.
        self._messages = _FakeMessages()
        self.messages = self._messages
        # The ``.aio`` namespace carries the async sibling surface; both
        # ``self.messages.create`` (sync) and ``self.aio.messages.create``
        # (async) drain the SAME ``_messages._create_queue``. DEC-012 of
        # ``plans/super/186-grade-asyncio-parallel.md``.
        self.aio = _FakeAioNamespace(messages=_FakeAsyncMessages(_sync=self._messages))

    def expect_count_tokens(
        self,
        *,
        matching: _Matcher,
        returns: object | Exception,
    ) -> None:
        self._messages._count_queue.append(
            _CountTokensExpectation(matching=matching, returns=returns)
        )

    def expect_messages_create(
        self,
        *,
        matching: _Matcher,
        returns: object | Exception,
    ) -> None:
        self._messages._create_queue.append(
            _MessagesCreateExpectation(matching=matching, returns=returns)
        )

    def assert_all_expectations_met(self) -> None:
        leftover: list[str] = []
        if self._messages._count_queue:
            leftover.append(f"{len(self._messages._count_queue)} count_tokens expectation(s)")
        if self._messages._create_queue:
            leftover.append(f"{len(self._messages._create_queue)} messages.create expectation(s)")
        if leftover:
            raise AssertionError("unconsumed expectations: " + ", ".join(leftover))

    @property
    def create_calls(self) -> list[dict[str, Any]]:
        """Inspector for tests that want to assert on the kwargs the seam
        passed to ``messages.create``."""
        return list(self._messages._create_calls)

    @property
    def count_calls(self) -> list[dict[str, Any]]:
        """Inspector for tests that want to assert on the kwargs the seam
        passed to ``messages.count_tokens``."""
        return list(self._messages._count_calls)


# ---- Placeholder shim from US-005 (kept for protocol-conformance tests) ----


class _StubMessages:
    """Stand-in for ``anthropic.Anthropic().messages`` (US-005 placeholder).

    Both methods raise :class:`NotImplementedError`. Kept alongside the
    full :class:`FakeAnthropicClient` because the protocol-conformance
    tests in ``test_client_shim.py`` hold its structural shape; replacing
    it would require touching that test file unnecessarily.
    """

    def create(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Use FakeAnthropicClient for full call coverage.")

    def count_tokens(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Use FakeAnthropicClient for full call coverage.")


class _StubAnthropicClient:
    """Minimal client that satisfies ``AnthropicClientProtocol`` without
    queueing any expectations (US-005 protocol-conformance harness)."""

    def __init__(self) -> None:
        self.messages = _StubMessages()


__all__ = [
    "FakeAnthropicClient",
    "FakeCountTokensResponse",
    "FakeMessage",
    "FakeRateLimitError",
    "FakeResponseWithHeaders",
    "FakeTextBlock",
    "FakeUsage",
    "_StubAnthropicClient",
    "_StubMessages",
]
