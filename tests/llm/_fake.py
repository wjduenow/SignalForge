"""Hand-rolled fake for ``anthropic.Anthropic`` (US-006, DEC-002 / DEC-028).

Tests register expectations via :meth:`expect_count_tokens` and
:meth:`expect_messages_create`. The fake's ``messages.count_tokens`` and
``messages.create`` consume one matching expectation per call (FIFO);
unexpected calls raise loudly.

Mirrors the precedent set by ``tests/warehouse/_fake.py::FakeBigQueryClient``
(see ``docs/rules/warehouse-adapters.md`` — "Test fakes use an
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

from signalforge.llm._client import _AnthropicMessagesProtocol


@dataclass
class FakeUsage:
    """Stand-in for ``anthropic.types.Usage``.

    Only the fields :func:`signalforge.llm.client.call_anthropic` reads
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
    """Implements the ``messages`` namespace on the fake client."""

    _count_queue: list[_CountTokensExpectation] = field(default_factory=list)
    _create_queue: list[_MessagesCreateExpectation] = field(default_factory=list)
    _create_calls: list[dict[str, Any]] = field(default_factory=list)
    _count_calls: list[dict[str, Any]] = field(default_factory=list)

    def count_tokens(self, **kwargs: Any) -> Any:
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

    def create(self, **kwargs: Any) -> Any:
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
    "FakeTextBlock",
    "FakeUsage",
    "_StubAnthropicClient",
    "_StubMessages",
]
