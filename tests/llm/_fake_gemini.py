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
    """

    _create_queue: list[_CreateExpectation] = field(default_factory=list)
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
        if isinstance(expectation.returns, BaseException):
            raise expectation.returns
        return expectation.returns

    def count_tokens(self, **kwargs: Any) -> Any:
        # The Gemini provider declares ``supports_token_count=False`` so the
        # orchestrator must never call this. Mirroring
        # ``_FakeNoCacheMessages.count_tokens``, this raises loudly to turn a
        # silent gating regression into a hard test failure.
        raise AssertionError(
            "count_tokens must never be called for a provider with supports_token_count=False"
        )


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
    """

    def __init__(self) -> None:
        self._messages = _FakeGeminiMessages()
        self.messages = self._messages

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

    def assert_all_expectations_met(self) -> None:
        """Raise :class:`AssertionError` if any expectations remain unconsumed."""
        leftover: list[str] = []
        if self._messages._create_queue:
            leftover.append(f"{len(self._messages._create_queue)} messages.create expectation(s)")
        if leftover:
            raise AssertionError("unconsumed expectations: " + ", ".join(leftover))

    @property
    def create_calls(self) -> list[dict[str, Any]]:
        """Inspector for tests that want to assert on the kwargs the seam
        passed to ``messages.create``."""
        return list(self._messages._create_calls)


__all__ = [
    "FakeGeminiCandidate",
    "FakeGeminiClient",
    "FakeGeminiContent",
    "FakeGeminiPart",
    "FakeGeminiResponse",
    "FakeGeminiUsageMetadata",
]
