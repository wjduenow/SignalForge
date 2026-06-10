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


#: The shared burst input for the FIX-4 limiter-vs-baseline pair: the SAME
#: sequence of three ``retry-after: 30`` 429s then a success, replayed against
#: ``call_llm`` once WITH a limiter and once WITHOUT. Using one input is the
#: load-bearing change (#202 QG-FIX-4) — the previous pair compared a 3×429
#: limiter run against a DIFFERENT 4×429 baseline, so the budget-count alone
#: drove the outcome and the test passed even without the limiter. With the
#: same input, the limiter's effect is the only variable: it PACES the retry at
#: the header value (30s) instead of the blind exponential ``2**attempt``.
_BURST_RETRY_AFTER = "30"
_BURST_429_COUNT = 3


def _queue_burst(fake: FakeAnthropicClient) -> None:
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    for _ in range(_BURST_429_COUNT):
        fake.expect_messages_create(
            matching={}, returns=_rate_limit_error(retry_after=_BURST_RETRY_AFTER)
        )
    fake.expect_messages_create(matching={}, returns=_ok_message())


def test_burst_429_with_limiter_paces_at_header_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STAGE-1 ACCEPTANCE (#202 US-003 / QG-FIX-4).

    The SAME burst input as the no-limiter baseline below (three
    ``retry-after: 30`` 429s then a success). WITH a :class:`SyncRateLimiter`
    wired, every retry sleeps AT the header value (30s) — pacing at the limit
    instead of bursting back on the blind exponential backoff. The call
    recovers within budget and the chosen sleeps are EXACTLY the header value,
    which is the limiter's load-bearing effect (not merely the pass/fail
    outcome, which the shared budget count drives identically on both arms).
    """
    sleeps: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", sleeps.append)

    sync_limiter, _ = make_rate_limiters(max_concurrent_calls=10)

    fake = FakeAnthropicClient()
    _queue_burst(fake)

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
    # The limiter paced EVERY retry at the header value — NOT the blind
    # exponential ``[1, 2, 4]`` the baseline below produces from the SAME input.
    assert sleeps == [30.0, 30.0, 30.0]
    # Three 429s drove the AIMD decrease 10 → 5 → 2 → 1.
    assert sync_limiter.effective_concurrency == 1


def test_burst_429_baseline_without_limiter_blind_backs_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-limiter baseline for the SAME burst input (#202 QG-FIX-4).

    With NO limiter wired, the IDENTICAL three-429s-then-success sequence
    ignores the ``retry-after: 30`` header entirely and blind-backs-off at the
    historical ``2**attempt * jitter`` cadence (``[1, 2, 4]`` with the pinned
    jitter). Same input, different pacing — proving the limiter is the only
    thing that changes the retry timing. (Both arms recover, because the budget
    count is what drives pass/fail; the limiter's contribution is the pacing,
    which under real concurrency is what stops the thundering herd.)
    """
    sleeps: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", sleeps.append)

    fake = FakeAnthropicClient()
    _queue_burst(fake)

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
    # No limiter → header ignored → blind exponential backoff (pinned jitter
    # 1.0): 2**0, 2**1, 2**2.
    assert sleeps == [1.0, 2.0, 4.0]


def test_burst_429_over_budget_still_exhausts_without_header_pacing() -> None:
    """A burst that EXCEEDS the retry budget exhausts regardless of pacing.

    The budget count — not the delay value — is what raises
    :class:`LLMRateLimitError`. Four 429s against ``max_retries_429=3`` exhausts
    even WITH a limiter wired, because header pacing changes the *wait*, not the
    *count*. This pins the honest scope of the limiter: it paces the retries (the
    thundering-herd fix under concurrency), it does not enlarge the per-call
    budget.
    """
    sync_limiter, _ = make_rate_limiters(max_concurrent_calls=10)
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    for _ in range(4):
        fake.expect_messages_create(
            matching={}, returns=_rate_limit_error(retry_after=_BURST_RETRY_AFTER)
        )

    with _sync_limiter_set(sync_limiter), pytest.raises(LLMRateLimitError) as exc_info:
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
    # 6 → 3 on the one 429, then +1 headroom on the clean completion (#202
    # QG-FIX-1: a non-429 return now probes the AIMD concurrency back up).
    assert async_limiter.effective_concurrency == 4


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
