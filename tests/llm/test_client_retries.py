"""Retry-branch coverage for :func:`signalforge.llm.client.call_llm`
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
from signalforge.llm.client import call_llm
from signalforge.llm.errors import (
    LLMAuthError,
    LLMConnectionError,
    LLMHelperError,
    LLMRateLimitError,
    LLMResponseFormatError,
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


def test_call_llm_429_retries_three_times_then_raises_rate_limit_error(
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
        call_llm(
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


def test_call_llm_5xx_retries_once_then_raises_server_error() -> None:
    """Default ``max_retries_5xx=1``: two total attempts → exhausted."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_status_error(500))
    fake.expect_messages_create(matching={}, returns=_status_error(503))

    with pytest.raises(LLMServerError) as exc_info:
        call_llm(
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


def test_call_llm_5xx_recovers_on_retry() -> None:
    """A 5xx then a 200 returns normally."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_status_error(503))
    fake.expect_messages_create(matching={}, returns=_ok_message())

    result = call_llm(
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


def test_call_llm_4xx_no_retry_raises_immediately() -> None:
    """4xx (non-401/403/429) does not retry."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_status_error(422))

    with pytest.raises(LLMHelperError) as exc_info:
        call_llm(
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


def test_call_llm_401_raises_auth_error_with_api_key_hint() -> None:
    """401 → :class:`LLMAuthError`, no retry, remediation mentions API key."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_auth_error(401))

    with pytest.raises(LLMAuthError) as exc_info:
        call_llm(
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


def test_call_llm_403_raises_auth_error() -> None:
    """403 → :class:`LLMAuthError`, no retry."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_auth_error(403))

    with pytest.raises(LLMAuthError) as exc_info:
        call_llm(
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


def test_call_llm_connection_error_retries_once() -> None:
    """Default ``max_retries_conn=1``: two total attempts → exhausted."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_connection_error())
    fake.expect_messages_create(matching={}, returns=_connection_error())

    with pytest.raises(LLMConnectionError) as exc_info:
        call_llm(
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


def test_call_llm_each_retry_emits_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each retry emits a WARNING with attempt/delay/error_class/model."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error())
    fake.expect_messages_create(matching={}, returns=_ok_message())

    with caplog.at_level(logging.WARNING, logger="signalforge.llm.client"):
        call_llm(
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
    # The 4 baseline keys are always present; per-class branches also
    # carry exactly one ``class_attempt_<429|5xx|conn>`` key (PR #19
    # quality-gate fix from CodeRabbit) so reviewers can see which retry
    # budget the failure consumed.
    base_keys = {"attempt", "delay", "error_class", "model"}
    assert base_keys.issubset(payload.keys())
    class_keys = {k for k in payload if k.startswith("class_attempt_")}
    assert class_keys == {"class_attempt_429"}
    assert payload["error_class"] == "RateLimitError"
    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["attempt"] == 0
    assert payload["class_attempt_429"] == 0
    assert payload["delay"] == 1.0  # 2**0 * 1.0 with the pinned _rand_uniform


# ---- Jitter ---------------------------------------------------------------


def test_call_llm_jitter_bounded_by_rand_uniform_aliases(
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

    call_llm(
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


def test_call_llm_per_class_budgets_do_not_cross_consume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quality-Gate fix (CodeRabbit on PR #19): one failure class must
    NOT consume another class's retry budget.

    Sequence: 1 connection error (consumes the conn budget of 1) THEN 3
    rate-limit errors (consumes the 429 budget of 3). Before the fix,
    the shared `attempt` counter would have raised LLMRateLimitError
    after only 2 retries because the connection retry already consumed
    one slot. After the fix, per-class counters allow the full 3+1
    retry budget.
    """
    monkeypatch.setattr(client_module, "_sleep", lambda _: None)
    monkeypatch.setattr(client_module, "_rand_uniform", lambda _a, _b: 1.0)

    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    # 1 conn failure (consumes conn budget)
    fake.expect_messages_create(matching={}, returns=_connection_error())
    # 3 rate-limit failures (each consumes a 429-budget slot; max 3)
    fake.expect_messages_create(matching={}, returns=_rate_limit_error())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error())
    # 4th 429 finally exhausts the 429 budget
    fake.expect_messages_create(matching={}, returns=_rate_limit_error())

    with pytest.raises(LLMRateLimitError) as exc_info:
        call_llm(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            max_retries_429=3,
            max_retries_5xx=1,
            max_retries_conn=1,
            client=fake,
        )
    # Five total create calls: 1 conn + 3 retried 429s + 1 final 429.
    assert len(fake.create_calls) == 5
    # The error reports 429-class attempts (3), NOT the total (which
    # would include the conn retry).
    assert exc_info.value.attempts == 3


# ---- count_tokens probe error mapping -------------------------------------
# The pre-send count_tokens probe maps a raised SDK exception to a typed
# LLMError via ``strategy.classify_exception`` and NEVER retries it (a probe
# failure must not consume the messages.create budget). One assertion per
# ExceptionCategory branch in ``call_llm`` (client.py count-gate block).


def _call_llm_probe_failure(fake: FakeAnthropicClient) -> None:
    """Invoke ``call_llm`` with a fake whose count_tokens is pre-queued to fail."""
    call_llm(
        system="sys",
        cached_block="c",
        dynamic_block="d",
        model="claude-sonnet-4-6",
        max_tokens=128,
        prompt_version="v1",
        client=fake,
    )


def test_count_tokens_auth_error_maps_to_auth_error_no_retry() -> None:
    """count_tokens 401 → LLMAuthError, no messages.create issued."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_auth_error(401))

    with pytest.raises(LLMAuthError) as exc_info:
        _call_llm_probe_failure(fake)

    assert isinstance(exc_info.value.cause, anthropic.AuthenticationError)
    assert len(fake.create_calls) == 0


def test_count_tokens_rate_limit_maps_to_rate_limit_error_attempts_zero() -> None:
    """count_tokens 429 → LLMRateLimitError(attempts=0), not retried."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_rate_limit_error())

    with pytest.raises(LLMRateLimitError) as exc_info:
        _call_llm_probe_failure(fake)

    assert exc_info.value.attempts == 0
    assert isinstance(exc_info.value.cause, anthropic.RateLimitError)
    assert len(fake.create_calls) == 0


def test_count_tokens_connection_error_maps_to_connection_error_no_retry() -> None:
    """count_tokens connection failure → LLMConnectionError, not retried."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_connection_error())

    with pytest.raises(LLMConnectionError) as exc_info:
        _call_llm_probe_failure(fake)

    assert isinstance(exc_info.value.cause, anthropic.APIConnectionError)
    assert len(fake.create_calls) == 0


def test_count_tokens_5xx_maps_to_server_error_no_retry() -> None:
    """count_tokens 503 → LLMServerError, not retried."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_status_error(503))

    with pytest.raises(LLMServerError) as exc_info:
        _call_llm_probe_failure(fake)

    assert isinstance(exc_info.value.cause, anthropic.APIStatusError)
    assert len(fake.create_calls) == 0


def test_count_tokens_4xx_non_auth_maps_to_helper_error_no_retry() -> None:
    """count_tokens non-5xx, non-auth (400) → LLMHelperError, not retried."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_status_error(400))

    with pytest.raises(LLMHelperError) as exc_info:
        _call_llm_probe_failure(fake)

    assert isinstance(exc_info.value.cause, anthropic.APIStatusError)
    assert len(fake.create_calls) == 0


def test_count_tokens_missing_input_tokens_field_raises_response_format_error() -> None:
    """A count_tokens response lacking ``input_tokens`` → LLMResponseFormatError."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=object())

    with pytest.raises(LLMResponseFormatError):
        _call_llm_probe_failure(fake)

    assert len(fake.create_calls) == 0
