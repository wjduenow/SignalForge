"""Hand-rolled fake for the OpenAI client (#136 US-003).

Mirrors :mod:`tests.llm._fake`'s :class:`FakeAnthropicClient` shape verbatim
but speaks the OpenAI Chat Completions response surface instead of
Anthropic's typed-block array. Tests register expectations via
:meth:`FakeOpenAIClient.expect_messages_create`; the fake's
``.messages.create`` consumes one matching expectation per call (FIFO);
unexpected calls raise loudly. Unconsumed expectations at end of test are
caught by :meth:`assert_all_expectations_met`.

The ``.messages`` namespace mirrors the production
:class:`signalforge.llm._openai_client._OpenAIClientAdapter` shape, so the
generic orchestrator's ``llm_client.messages.create(**kwargs)`` call works
unchanged. ``count_tokens`` raises :class:`NotImplementedError` (defensive
parity with the adapter — orchestrator gates it off via
``supports_token_count=False``; if it ever drifts and gets called, the
raise turns the silent regression into a loud failure, mirroring
``tests/llm/_fake_provider.py::FakeNoCacheClient``).

Response dataclasses match the surface
:meth:`signalforge.llm.providers.OpenAIProvider.extract_text_blocks` and
:meth:`signalforge.llm.providers.OpenAIProvider.extract_usage` read:

* ``response.choices[0].message.content`` is the assistant text (single
  string, not a typed-block array).
* ``response.usage.prompt_tokens`` / ``response.usage.completion_tokens``
  are the token counts (no cache fields — OpenAI has no equivalent cache
  discount).

Hand-rolled rather than ``MagicMock``-driven because ``MagicMock``
auto-passes everything, which would silently mask mismatches and violate
``testing-signal.md``. Lives under ``tests/llm/`` and is never imported
from production code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeOpenAIUsage:
    """Stand-in for ``openai.types.CompletionUsage``.

    Only the fields :meth:`OpenAIProvider.extract_usage` reads are exposed.
    OpenAI has no cache-token fields — the provider unconditionally reports
    ``cache_creation_input_tokens=0`` / ``cache_read_input_tokens=0`` (matches
    ``supports_prompt_caching=False``).
    """

    prompt_tokens: int = 100
    completion_tokens: int = 40


@dataclass
class FakeOpenAIMessage:
    """Stand-in for ``openai.types.chat.ChatCompletionMessage``."""

    content: str
    role: str = "assistant"


@dataclass
class FakeOpenAIChoice:
    """Stand-in for ``openai.types.chat.Choice``."""

    message: FakeOpenAIMessage
    index: int = 0
    finish_reason: str = "stop"


@dataclass
class FakeOpenAICompletion:
    """Stand-in for ``openai.types.chat.ChatCompletion`` (the
    ``chat.completions.create`` response, which the adapter exposes through
    ``messages.create``).
    """

    choices: list[FakeOpenAIChoice]
    usage: FakeOpenAIUsage
    model: str = "openai-fake-1"
    id: str = "chatcmpl_fake_001"
    object: str = "chat.completion"


# A "matching" predicate is either a dict (subset match against the kwargs
# dict the seam passes) or a callable returning bool.
_Matcher = dict[str, Any] | Callable[[dict[str, Any]], bool]


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
class _FakeOpenAIMessages:
    """Implements the ``messages`` namespace on the fake client.

    Mirrors the production adapter's ``.messages`` surface: ``create``
    delegates here (in production it routes to
    ``chat.completions.create``); ``count_tokens`` raises (the orchestrator
    never calls it for a ``supports_token_count=False`` provider — a raise
    here turns a capability-flag-gate regression loud).
    """

    _create_queue: list[_MessagesCreateExpectation] = field(default_factory=list)
    _create_calls: list[dict[str, Any]] = field(default_factory=list)

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

    def count_tokens(self, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "OpenAI fake does not support pre-send count_tokens; "
            "OpenAIProvider.supports_token_count=False gates this call off "
            "in the orchestrator. If you see this, the capability-flag gate "
            "has drifted."
        )


class FakeOpenAIClient:
    """Explicit fake for the OpenAI client (the
    :class:`signalforge.llm._openai_client._OpenAIClientAdapter` shape);
    calls outside the queued expectations raise :class:`AssertionError`.

    Each :meth:`expect_messages_create` enqueues one expectation; calls
    consume them FIFO. Tests must call :meth:`assert_all_expectations_met`
    at the end; a non-empty queue at the end of a test is a
    leftover-expectation bug.

    Exceptions queued as ``returns`` (e.g. an ``openai.RateLimitError``
    instance) are raised when the expectation is consumed — supports the
    retry-loop test paths.
    """

    def __init__(self) -> None:
        self._messages = _FakeOpenAIMessages()
        self.messages = self._messages

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
        if self._messages._create_queue:
            raise AssertionError(
                f"unconsumed expectations: {len(self._messages._create_queue)} "
                "messages.create expectation(s)"
            )

    @property
    def create_calls(self) -> list[dict[str, Any]]:
        """Inspector for tests that want to assert on the kwargs the seam
        passed to ``messages.create``."""
        return list(self._messages._create_calls)


__all__ = [
    "FakeOpenAIChoice",
    "FakeOpenAIClient",
    "FakeOpenAICompletion",
    "FakeOpenAIMessage",
    "FakeOpenAIUsage",
]
