"""Contract tests for :class:`tests.llm._fake_gemini.FakeGeminiClient` (#137 US-004).

The fake mirrors :class:`tests.llm._fake.FakeAnthropicClient`'s ``expect_*``
queue behaviour; the precedent is :mod:`tests.llm._fake`'s self-tests
embedded across the client-retry suite. These tests pin the fake's own
contract independently of any production code path, so a refactor of the
fake's queue/matcher machinery breaks here before it breaks the
integration tests in
:mod:`tests.llm.test_gemini_provider_via_fake`.
"""

from __future__ import annotations

import pytest

from tests.llm._fake_gemini import (
    FakeGeminiCandidate,
    FakeGeminiClient,
    FakeGeminiContent,
    FakeGeminiPart,
    FakeGeminiResponse,
    FakeGeminiUsageMetadata,
)


def _ok_response(text: str = "ok") -> FakeGeminiResponse:
    """Build a minimal happy-path response with one text part."""
    return FakeGeminiResponse(
        candidates=[
            FakeGeminiCandidate(
                content=FakeGeminiContent(parts=[FakeGeminiPart(text=text)]),
                finish_reason="STOP",
            )
        ],
        usage_metadata=FakeGeminiUsageMetadata(prompt_token_count=10, candidates_token_count=2),
    )


def test_expect_create_consumes_queue_in_fifo_order() -> None:
    """Two enqueued expectations are consumed strictly first-in-first-out
    (mirrors :class:`FakeAnthropicClient` semantics)."""
    fake = FakeGeminiClient()
    resp_a = _ok_response("first")
    resp_b = _ok_response("second")
    fake.expect_messages_create(matching={"model": "gemini-2.5-flash"}, returns=resp_a)
    fake.expect_messages_create(matching={"model": "gemini-2.5-pro"}, returns=resp_b)

    out_a = fake.messages.create(model="gemini-2.5-flash", contents=["one"])
    out_b = fake.messages.create(model="gemini-2.5-pro", contents=["two"])

    assert out_a is resp_a
    assert out_b is resp_b
    fake.assert_all_expectations_met()


def test_create_records_call_kwargs() -> None:
    """The ``create_calls`` inspector exposes every kwargs dict passed in,
    in order. Tests assert on this to confirm what the orchestrator sent."""
    fake = FakeGeminiClient()
    fake.expect_messages_create(matching={}, returns=_ok_response())

    fake.messages.create(
        model="gemini-2.5-flash",
        contents=["payload"],
        config={"response_mime_type": "application/json"},
    )

    calls = fake.create_calls
    assert len(calls) == 1
    assert calls[0]["model"] == "gemini-2.5-flash"
    assert calls[0]["contents"] == ["payload"]
    assert calls[0]["config"] == {"response_mime_type": "application/json"}


def test_unexpected_create_raises_assertion_error() -> None:
    """A ``create`` call with no enqueued expectation fails loud — the fake
    never silently auto-passes (unlike ``MagicMock``)."""
    fake = FakeGeminiClient()
    with pytest.raises(AssertionError, match="unexpected messages.create call"):
        fake.messages.create(model="gemini-2.5-flash")


def test_create_with_unmatched_dict_matcher_raises() -> None:
    """An enqueued dict matcher that doesn't subset-match the actual kwargs
    raises rather than consuming the expectation."""
    fake = FakeGeminiClient()
    fake.expect_messages_create(matching={"model": "gemini-2.5-pro"}, returns=_ok_response())
    with pytest.raises(AssertionError, match="did not match expectation"):
        fake.messages.create(model="gemini-2.5-flash")


def test_assert_all_expectations_met_passes_when_queue_empty() -> None:
    """No queued expectations and no calls — happy path; the assertion
    passes silently."""
    FakeGeminiClient().assert_all_expectations_met()


def test_assert_all_expectations_met_raises_when_queue_nonempty() -> None:
    """A leftover expectation at end-of-test is a bug; the assertion fires
    so it surfaces immediately, not as a downstream confusion."""
    fake = FakeGeminiClient()
    fake.expect_messages_create(matching={}, returns=_ok_response())
    with pytest.raises(AssertionError, match="unconsumed expectations"):
        fake.assert_all_expectations_met()


def test_returns_exception_raises_from_create() -> None:
    """Pass a :class:`BaseException` instance via ``returns=...`` and the
    fake raises it on the matching call instead of returning a response.
    Used by the retry tests to inject SDK-shaped error instances."""
    fake = FakeGeminiClient()
    fake.expect_messages_create(matching={}, returns=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        fake.messages.create(model="gemini-2.5-flash")


def test_count_tokens_raises_loudly() -> None:
    """The Gemini provider declares ``supports_token_count=False``; the
    orchestrator must never call ``count_tokens``. The fake raises rather
    than no-opping so a silent gating regression is loud (mirrors
    :class:`tests.llm._fake_provider._FakeNoCacheMessages.count_tokens`)."""
    fake = FakeGeminiClient()
    with pytest.raises(AssertionError, match="count_tokens must never be called"):
        fake.messages.count_tokens(model="gemini-2.5-flash")


def test_callable_matcher_receives_full_kwargs() -> None:
    """A predicate matcher gets the full kwargs dict and decides; used by
    integration tests that need to inspect the request body shape."""
    fake = FakeGeminiClient()

    def _matches(kwargs: dict[str, object]) -> bool:
        config = kwargs.get("config")
        return isinstance(config, dict) and config.get("response_mime_type") == "application/json"

    fake.expect_messages_create(matching=_matches, returns=_ok_response())

    fake.messages.create(
        model="gemini-2.5-flash",
        contents=["x"],
        config={"response_mime_type": "application/json"},
    )
    fake.assert_all_expectations_met()
