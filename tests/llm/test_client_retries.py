"""Retry-branch coverage for :func:`signalforge.llm.client.call_anthropic`
(US-006, DEC-004).

Reassigns the module-level ``_sleep`` and ``_rand_uniform`` aliases to
deterministic stand-ins so retry tests run instantly without timing
flake. The reassignment is the load-bearing reason those aliases exist
at module level (DEC-004).

Each retry branch (429 → max_retries_429, 5xx → max_retries_5xx,
connection error → max_retries_conn, 4xx-other → no retry, 401/403 →
no retry) gets its own test. Per-attempt WARNING shape is asserted.
"""

from __future__ import annotations

import json
import logging

import anthropic
import httpx
import pytest

from signalforge.llm import client as client_module
from signalforge.llm.client import call_anthropic
from signalforge.llm.errors import (
    LLMAuthError,
    LLMConnectionError,
    LLMHelperError,
    LLMRateLimitError,
    LLMServerError,
)

from ._fake import (
    FakeAnthropicClient,
    FakeCountTokensResponse,
    FakeMessage,
    FakeTextBlock,
    FakeUsage,
)

pytestmark = pytest.mark.llm


# ---- helpers ---------------------------------------------------------------


_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _rate_limit_error() -> anthropic.RateLimitError:
    return anthropic.RateLimitError(
        message="rate limited",
        response=httpx.Response(429, request=_REQ),
        body=None,
    )


def _status_error(code: int) -> anthropic.APIStatusError:
    return anthropic.APIStatusError(
        message=f"status {code}",
        response=httpx.Response(code, request=_REQ),
        body=None,
    )


def _auth_error(code: int = 401) -> anthropic.APIStatusError:
    if code == 401:
        return anthropic.AuthenticationError(
            message="auth failed",
            response=httpx.Response(401, request=_REQ),
            body=None,
        )
    return anthropic.PermissionDeniedError(
        message="permission denied",
        response=httpx.Response(403, request=_REQ),
        body=None,
    )


def _connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=_REQ)


def _ok_count() -> FakeCountTokensResponse:
    # Pick a value that's >= every model minimum (Haiku is 2048).
    return FakeCountTokensResponse(input_tokens=2048)


def _ok_message() -> FakeMessage:
    return FakeMessage(
        content=[FakeTextBlock(text="ok")],
        usage=FakeUsage(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=100,
            cache_read_input_tokens=0,
        ),
    )


@pytest.fixture(autouse=True)
def _deterministic_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``_sleep`` and ``_rand_uniform`` so retries don't actually
    block and jitter is fully deterministic. Tests that need to inspect
    arguments install their own spies on top of this fixture."""
    monkeypatch.setattr(client_module, "_sleep", lambda _delay: None)
    monkeypatch.setattr(client_module, "_rand_uniform", lambda _a, _b: 1.0)


# ---- 429 ------------------------------------------------------------------


def test_call_anthropic_429_retries_three_times_then_raises_rate_limit_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Default ``max_retries_429=3``: four total attempts → exhausted."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    # 1 initial + 3 retries = 4 failures.
    for _ in range(4):
        fake.expect_messages_create(matching={}, returns=_rate_limit_error())

    with (
        caplog.at_level(logging.WARNING, logger="signalforge.llm.client"),
        pytest.raises(LLMRateLimitError) as exc_info,
    ):
        call_anthropic(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )

    assert exc_info.value.attempts == 3
    assert isinstance(exc_info.value.cause, anthropic.RateLimitError)
    # Three retry warnings (one per retry, NOT one per failure).
    retry_warnings = [r for r in caplog.records if "retry attempt" in r.getMessage()]
    assert len(retry_warnings) == 3
    fake.assert_all_expectations_met()


# ---- 5xx ------------------------------------------------------------------


def test_call_anthropic_5xx_retries_once_then_raises_server_error() -> None:
    """Default ``max_retries_5xx=1``: two total attempts → exhausted."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_status_error(500))
    fake.expect_messages_create(matching={}, returns=_status_error(503))

    with pytest.raises(LLMServerError) as exc_info:
        call_anthropic(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )
    assert isinstance(exc_info.value.cause, anthropic.APIStatusError)
    fake.assert_all_expectations_met()


def test_call_anthropic_5xx_recovers_on_retry() -> None:
    """A 5xx then a 200 returns normally."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_status_error(503))
    fake.expect_messages_create(matching={}, returns=_ok_message())

    result = call_anthropic(
        system="sys",
        cached_block="c",
        dynamic_block="d",
        model="claude-sonnet-4-6",
        max_tokens=128,
        prompt_version="v1",
        client=fake,
    )
    assert result.response_text == "ok"
    fake.assert_all_expectations_met()


# ---- 4xx (non-auth) -------------------------------------------------------


def test_call_anthropic_4xx_no_retry_raises_immediately() -> None:
    """4xx (non-401/403/429) does not retry."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_status_error(422))

    with pytest.raises(LLMHelperError) as exc_info:
        call_anthropic(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )
    # Not a more-specific subclass — generic helper-error wrap.
    assert type(exc_info.value) is LLMHelperError
    assert isinstance(exc_info.value.cause, anthropic.APIStatusError)
    fake.assert_all_expectations_met()


# ---- 401 / 403 ------------------------------------------------------------


def test_call_anthropic_401_raises_auth_error_with_api_key_hint() -> None:
    """401 → :class:`LLMAuthError`, no retry, remediation mentions API key."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_auth_error(401))

    with pytest.raises(LLMAuthError) as exc_info:
        call_anthropic(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )
    assert "ANTHROPIC_API_KEY" in str(exc_info.value)
    assert isinstance(exc_info.value.cause, anthropic.AuthenticationError)


def test_call_anthropic_403_raises_auth_error() -> None:
    """403 → :class:`LLMAuthError`, no retry."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_auth_error(403))

    with pytest.raises(LLMAuthError) as exc_info:
        call_anthropic(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )
    assert isinstance(exc_info.value.cause, anthropic.PermissionDeniedError)


# ---- Connection -----------------------------------------------------------


def test_call_anthropic_connection_error_retries_once() -> None:
    """Default ``max_retries_conn=1``: two total attempts → exhausted."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_connection_error())
    fake.expect_messages_create(matching={}, returns=_connection_error())

    with pytest.raises(LLMConnectionError) as exc_info:
        call_anthropic(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )
    assert isinstance(exc_info.value.cause, anthropic.APIConnectionError)
    fake.assert_all_expectations_met()


# ---- WARNING shape --------------------------------------------------------


def test_call_anthropic_each_retry_emits_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each retry emits a WARNING with attempt/delay/error_class/model."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error())
    fake.expect_messages_create(matching={}, returns=_ok_message())

    with caplog.at_level(logging.WARNING, logger="signalforge.llm.client"):
        call_anthropic(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )

    retry_records = [r for r in caplog.records if "retry attempt" in r.getMessage()]
    assert len(retry_records) == 1
    record = retry_records[0]
    assert record.levelno == logging.WARNING
    payload = json.loads(record.getMessage().split(": ", 1)[1])
    assert set(payload.keys()) == {"attempt", "delay", "error_class", "model"}
    assert payload["error_class"] == "RateLimitError"
    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["attempt"] == 0
    assert payload["delay"] == 1.0  # 2**0 * 1.0 with the pinned _rand_uniform


# ---- Jitter ---------------------------------------------------------------


def test_call_anthropic_jitter_bounded_by_rand_uniform_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reassigning ``_rand_uniform`` deterministically controls ``_sleep``
    delays. Two retries → two ``_sleep`` calls with the expected
    ``2**i * jitter`` values."""
    sleeps: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", lambda d: sleeps.append(d))

    # Alternate min/max jitter so retry 0 uses 0.75, retry 1 uses 1.25.
    jitters = iter([0.75, 1.25])
    monkeypatch.setattr(
        client_module,
        "_rand_uniform",
        lambda _a, _b: next(jitters),
    )

    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error())
    fake.expect_messages_create(matching={}, returns=_ok_message())

    call_anthropic(
        system="sys",
        cached_block="c",
        dynamic_block="d",
        model="claude-sonnet-4-6",
        max_tokens=128,
        prompt_version="v1",
        client=fake,
    )

    assert sleeps == [
        2**0 * 0.75,  # first retry: attempt index 0
        2**1 * 1.25,  # second retry: attempt index 1
    ]
