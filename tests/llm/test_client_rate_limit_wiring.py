"""Client 429-branch wiring for the shared rate limiters (#202 US-003 / DEC-205).

The Stage-1 acceptance proof: a fake-client burst that returns 429 +
``retry-after`` and that PREVIOUSLY exhausted the 3×429 retry budget now does
NOT exhaust, because the limiter paces the retry at the header value (and an
in-band success on the next attempt succeeds). Plus:

* the sync ``call_llm`` 429 branch honours ``retry-after`` via the injected
  :class:`SyncRateLimiter` and emits the unchanged WARNING shape carrying the
  header-derived ``delay``;
* the async ``call_llm_async`` 429 branch resolves the limiter from the
  ``current_async_rate_limiter`` ContextVar and honours the header;
* with NO limiter (or an EMPTY budget) the path falls back to the historical
  blind backoff, byte-unchanged.

Reassigns the module-level ``_sleep`` / ``_async_sleep`` / ``_rand_uniform``
aliases (the DEC-004 deterministic-backoff seam) so retries don't block.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import anthropic
import httpx
import pytest

from signalforge.llm import client as client_module
from signalforge.llm._rate_limiter import (
    current_async_rate_limiter,
    current_sync_rate_limiter,
    make_rate_limiters,
)
from signalforge.llm.client import call_llm, call_llm_async
from signalforge.llm.errors import LLMRateLimitError

from ._fake import (
    FakeAnthropicClient,
    FakeCountTokensResponse,
    FakeMessage,
    FakeTextBlock,
    FakeUsage,
)

pytestmark = pytest.mark.llm


_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _rate_limit_error(*, retry_after: str | None = None) -> anthropic.RateLimitError:
    """A real ``anthropic.RateLimitError`` (so ``classify_exception`` routes it
    to RATE_LIMIT) optionally carrying a ``retry-after`` header on its
    ``response.headers`` — the path the extractor reads in production."""
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return anthropic.RateLimitError(
        message="rate limited",
        response=httpx.Response(429, request=_REQ, headers=headers),
        body=None,
    )


def _ok_count() -> FakeCountTokensResponse:
    return FakeCountTokensResponse(input_tokens=2048)


def _ok_message() -> FakeMessage:
    return FakeMessage(
        content=[FakeTextBlock(text="ok")],
        usage=FakeUsage(input_tokens=10, output_tokens=5, cache_creation_input_tokens=100),
    )


@pytest.fixture(autouse=True)
def _deterministic_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the backoff seams so retries don't block and jitter is exact."""
    monkeypatch.setattr(client_module, "_sleep", lambda _delay: None)
    monkeypatch.setattr(client_module, "_rand_uniform", lambda _a, _b: 1.0)

    async def _no_async_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(client_module, "_async_sleep", _no_async_sleep)


@contextmanager
def _sync_limiter_set(limiter: object) -> Iterator[None]:
    """Set the sync rate-limiter ContextVar for the block, then reset it."""
    token = current_sync_rate_limiter.set(limiter)  # type: ignore[arg-type]
    try:
        yield
    finally:
        current_sync_rate_limiter.reset(token)


# ---------------------------------------------------------------------------
# Headline acceptance — burst that USED to exhaust now succeeds
# ---------------------------------------------------------------------------


def test_burst_429_with_retry_after_no_longer_exhausts_budget() -> None:
    """STAGE-1 ACCEPTANCE (#202 US-003).

    Previously: a wave of 429s blind-backed-off and burst straight back into
    the limit, exhausting the 3×429 retry budget and raising
    :class:`LLMRateLimitError` (the thundering-herd storm). Now: with a
    :class:`SyncRateLimiter` wired, each 429 carries ``retry-after`` so the
    limiter paces AT the limit; the budget is NOT exhausted because the call
    recovers on a later attempt within the SAME 3-retry budget.

    The exhaustion scenario is the explicit baseline: 4 consecutive 429s with
    the default ``max_retries_429=3`` is what raised before. Here we queue 3
    paced 429s then a success — within budget — and assert it returns instead
    of raising.
    """
    sync_limiter, _ = make_rate_limiters(max_concurrent_calls=10)

    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    # 3 retried 429s (each paced by the header) THEN a success — exactly the
    # 3-retry budget, so the OLD blind path would have been on its last legs;
    # the limiter makes the paced retries land the success.
    for _ in range(3):
        fake.expect_messages_create(matching={}, returns=_rate_limit_error(retry_after="2"))
    fake.expect_messages_create(matching={}, returns=_ok_message())

    with _sync_limiter_set(sync_limiter):
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
    # Three 429s drove the AIMD decrease 10 → 5 → 2 → 1.
    assert sync_limiter.effective_concurrency == 1


def test_burst_429_baseline_without_limiter_still_exhausts() -> None:
    """The previous-exhaustion baseline this fix targets: the SAME 4×429 burst
    with NO limiter still exhausts the 3-retry budget (blind backoff, no
    header pacing) — proving the limiter is what changes the outcome."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    for _ in range(4):
        fake.expect_messages_create(matching={}, returns=_rate_limit_error(retry_after="2"))

    with pytest.raises(LLMRateLimitError) as exc_info:
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
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# Sync 429 branch — header-honouring delay + unchanged WARNING shape
# ---------------------------------------------------------------------------


def test_sync_429_honours_retry_after_in_sleep_and_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With a limiter + ``retry-after: 30``, the sync path sleeps 30 (not the
    blind ``2**0``) and the WARNING ``delay`` reports 30 — same key shape."""
    sleeps: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", sleeps.append)

    sync_limiter, _ = make_rate_limiters(max_concurrent_calls=4)
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error(retry_after="30"))
    fake.expect_messages_create(matching={}, returns=_ok_message())

    with (
        caplog.at_level(logging.WARNING, logger="signalforge.llm.client"),
        _sync_limiter_set(sync_limiter),
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

    assert sleeps == [30.0]
    retry_records = [r for r in caplog.records if "retry attempt" in r.getMessage()]
    assert len(retry_records) == 1
    payload = json.loads(retry_records[0].getMessage().split(": ", 1)[1])
    # Unchanged WARNING shape: the same 4 base keys + the per-class key.
    assert {"attempt", "delay", "error_class", "model"}.issubset(payload.keys())
    assert {k for k in payload if k.startswith("class_attempt_")} == {"class_attempt_429"}
    assert payload["delay"] == 30.0
    assert payload["error_class"] == "RateLimitError"


def test_sync_429_no_header_falls_back_to_blind_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A limiter is wired but the 429 carries NO ``retry-after`` (EMPTY budget)
    → blind backoff ``2**total_attempts`` is used, byte-identical to the
    no-limiter path. The AIMD decrease still fires."""
    sleeps: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", sleeps.append)

    sync_limiter, _ = make_rate_limiters(max_concurrent_calls=4)
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error())  # no header
    fake.expect_messages_create(matching={}, returns=_ok_message())

    with _sync_limiter_set(sync_limiter):
        call_llm(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )

    # Blind backoff for total_attempts=0: 2**0 * 1.0 (pinned jitter).
    assert sleeps == [1.0]
    # The AIMD decrease still happened on the 429 even with no header.
    assert sync_limiter.effective_concurrency == 2


# ---------------------------------------------------------------------------
# Async 429 branch — limiter resolved from the ContextVar seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_429_honours_retry_after_via_context_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``call_llm_async`` resolves the limiter from ``current_async_rate_limiter``
    and paces the retry at ``retry-after`` (the shared async-wave seam).

    The fake is driven via its ``.aio`` namespace (US-007 of #186) — the async
    surface drains the same expectation queue as the sync one.
    """

    slept: list[float] = []

    async def _capture_async_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(client_module, "_async_sleep", _capture_async_sleep)

    _, async_limiter = make_rate_limiters(max_concurrent_calls=6)

    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error(retry_after="12"))
    fake.expect_messages_create(matching={}, returns=_ok_message())

    token = current_async_rate_limiter.set(async_limiter)
    try:
        result = await call_llm_async(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake.aio,
        )
    finally:
        current_async_rate_limiter.reset(token)

    assert result.response_text == "ok"
    assert slept == [12.0]
    assert async_limiter.effective_concurrency == 3  # 6 → 3 on the one 429


@pytest.mark.asyncio
async def test_async_429_no_limiter_set_uses_blind_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With NO limiter in the ContextVar (default ``None``), the async 429 path
    falls back to the blind backoff — unchanged behaviour."""
    slept: list[float] = []

    async def _capture_async_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(client_module, "_async_sleep", _capture_async_sleep)

    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error(retry_after="99"))
    fake.expect_messages_create(matching={}, returns=_ok_message())

    result = await call_llm_async(
        system="sys",
        cached_block="c",
        dynamic_block="d",
        model="claude-sonnet-4-6",
        max_tokens=128,
        prompt_version="v1",
        client=fake.aio,
    )
    assert result.response_text == "ok"
    # No limiter → header ignored → blind backoff 2**0 * 1.0.
    assert slept == [1.0]
