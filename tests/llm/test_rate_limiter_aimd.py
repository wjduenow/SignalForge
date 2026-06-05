"""AIMD limiter + wait-computation coverage for the shared rate limiters
(#202 US-003 / DEC-205).

Covers the limiter classes added on top of the US-002 value object:

* :class:`SyncRateLimiter` / :class:`AsyncRateLimiter` SHARE one
  :class:`_RateLimiterState` so a 429 on either path tightens the other's
  concurrency (the thundering-herd cooperation).
* AIMD multiplicative-decrease on a 429; additive-increase on headroom;
  effective concurrency always clamped to ``[1, max_concurrent_calls]``.
* Header-honouring wait from ``retry-after`` vs blind-backoff fallback on an
  EMPTY budget.
* The ``current_async_rate_limiter`` ContextVar defaults to ``None`` (optional
  limiter) and round-trips a set value.

Every test is capable of failing — no ``assert True`` placeholders
(``testing-signal.md``).
"""

from __future__ import annotations

import asyncio

import pytest

from signalforge.llm._rate_limiter import (
    AsyncRateLimiter,
    RateLimitBudget,
    SyncRateLimiter,
    current_async_rate_limiter,
    make_rate_limiters,
)

pytestmark = pytest.mark.llm


# A deterministic ``rand_uniform`` so the blind-backoff fallback is exact.
def _fixed_jitter(_a: float, _b: float) -> float:
    return 1.0


# ---------------------------------------------------------------------------
# Construction + clamping
# ---------------------------------------------------------------------------


def test_make_rate_limiters_starts_at_cap() -> None:
    """A fresh limiter pair probes from wide-open — effective concurrency
    starts at the configured ``max_concurrent_calls``."""
    sync, async_ = make_rate_limiters(max_concurrent_calls=10)
    assert sync.effective_concurrency == 10
    assert async_.effective_concurrency == 10


def test_make_rate_limiters_share_one_state() -> None:
    """The sync + async limiters reference the SAME state cell, so a 429 on
    one is visible to the other (the cooperation contract)."""
    sync, async_ = make_rate_limiters(max_concurrent_calls=8)
    assert sync.state is async_.state


def test_degenerate_cap_clamps_to_one() -> None:
    """A ``max_concurrent_calls < 1`` config can't deadlock — it clamps to 1."""
    sync, _ = make_rate_limiters(max_concurrent_calls=0)
    assert sync.effective_concurrency == 1
    assert sync.state.max_concurrent_calls == 1


# ---------------------------------------------------------------------------
# AIMD decrease / increase + clamping
# ---------------------------------------------------------------------------


def test_aimd_multiplicative_decrease_on_429() -> None:
    """Each 429 halves the effective concurrency (floored at 1)."""
    sync, _ = make_rate_limiters(max_concurrent_calls=16)
    empty = RateLimitBudget()
    assert sync.record_rate_limited(empty) is None  # no header → blind fallback
    assert sync.effective_concurrency == 8
    sync.record_rate_limited(empty)
    assert sync.effective_concurrency == 4
    sync.record_rate_limited(empty)
    assert sync.effective_concurrency == 2
    sync.record_rate_limited(empty)
    assert sync.effective_concurrency == 1


def test_aimd_decrease_floors_at_one() -> None:
    """Repeated 429s never drive concurrency below 1."""
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    empty = RateLimitBudget()
    for _ in range(10):
        sync.record_rate_limited(empty)
    assert sync.effective_concurrency == 1


def test_aimd_additive_increase_on_headroom() -> None:
    """Headroom adds one per call — the gentle probe back up."""
    sync, _ = make_rate_limiters(max_concurrent_calls=10)
    # Drop to 2 via two 429s (10 → 5 → 2).
    sync.record_rate_limited(RateLimitBudget())
    sync.record_rate_limited(RateLimitBudget())
    assert sync.effective_concurrency == 2
    sync.record_headroom()
    assert sync.effective_concurrency == 3
    sync.record_headroom()
    assert sync.effective_concurrency == 4


def test_aimd_increase_caps_at_max() -> None:
    """Additive-increase never exceeds the configured ceiling."""
    sync, _ = make_rate_limiters(max_concurrent_calls=3)
    for _ in range(20):
        sync.record_headroom()
    assert sync.effective_concurrency == 3


def test_aimd_stays_within_bounds_under_mixed_signal() -> None:
    """Interleaved 429s and headroom keep concurrency in ``[1, max]``."""
    sync, _ = make_rate_limiters(max_concurrent_calls=6)
    sequence = ["429", "ok", "429", "429", "ok", "ok", "429", "ok"]
    for signal in sequence:
        if signal == "429":
            sync.record_rate_limited(RateLimitBudget())
        else:
            sync.record_headroom()
        assert 1 <= sync.effective_concurrency <= 6


def test_429_on_sync_is_visible_to_async_sibling() -> None:
    """The shared state means a sync-path 429 tightens the async path's
    concurrency too — the core thundering-herd cooperation."""
    sync, async_ = make_rate_limiters(max_concurrent_calls=8)
    sync.record_rate_limited(RateLimitBudget())
    assert sync.effective_concurrency == 4
    assert async_.effective_concurrency == 4


# ---------------------------------------------------------------------------
# Wait computation — header-honouring vs blind-backoff fallback
# ---------------------------------------------------------------------------


def test_record_rate_limited_returns_header_wait_when_present() -> None:
    """A populated ``retry-after`` is the returned wait — pace AT the limit."""
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    wait = sync.record_rate_limited(RateLimitBudget(retry_after=42.0))
    assert wait == 42.0


def test_record_rate_limited_empty_budget_returns_none() -> None:
    """An EMPTY budget (no headers) returns ``None`` → caller blind-backs off."""
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    assert sync.record_rate_limited(RateLimitBudget()) is None


def test_record_rate_limited_zero_retry_after_is_honoured() -> None:
    """A present ``retry-after == 0`` is "retry now", NOT absent."""
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    assert sync.record_rate_limited(RateLimitBudget(retry_after=0.0)) == 0.0


def test_negative_retry_after_clamps_to_zero() -> None:
    """A nonsensical negative gateway value clamps to 0."""
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    assert sync.record_rate_limited(RateLimitBudget(retry_after=-3.0)) == 0.0


def test_compute_rate_limit_wait_blind_fallback_uses_jitter() -> None:
    """With no header, ``compute_rate_limit_wait`` returns the blind backoff
    ``(2**attempt) * jitter`` using the injected ``rand_uniform``."""
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    wait = sync.compute_rate_limit_wait(RateLimitBudget(), attempt=2, rand_uniform=_fixed_jitter)
    assert wait == (2**2) * 1.0


def test_compute_rate_limit_wait_header_overrides_blind() -> None:
    """With a header, ``compute_rate_limit_wait`` returns the header wait and
    does NOT consume the blind-backoff math."""
    sync, _ = make_rate_limiters(max_concurrent_calls=4)

    def _explode(_a: float, _b: float) -> float:  # pragma: no cover - must not run
        raise AssertionError("blind backoff must not be computed when a header wait exists")

    wait = sync.compute_rate_limit_wait(
        RateLimitBudget(retry_after=7.0), attempt=3, rand_uniform=_explode
    )
    assert wait == 7.0


# ---------------------------------------------------------------------------
# Sleep wrappers — sync threading + async asyncio
# ---------------------------------------------------------------------------


def test_sync_wait_after_rate_limit_sleeps_the_wait() -> None:
    """The sync wrapper applies the decrease AND sleeps the chosen wait."""
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    slept: list[float] = []
    wait = sync.wait_after_rate_limit(
        RateLimitBudget(retry_after=5.0),
        attempt=0,
        sleep=slept.append,
        rand_uniform=_fixed_jitter,
    )
    assert wait == 5.0
    assert slept == [5.0]
    assert sync.effective_concurrency == 2  # decreased from 4


def test_async_wait_after_rate_limit_awaits_the_wait() -> None:
    """The async wrapper applies the decrease AND awaits the chosen wait."""
    _, async_ = make_rate_limiters(max_concurrent_calls=4)
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async def _run() -> float:
        return await async_.wait_after_rate_limit(
            RateLimitBudget(retry_after=9.0),
            attempt=0,
            sleep=_fake_sleep,
            rand_uniform=_fixed_jitter,
        )

    wait = asyncio.run(_run())
    assert wait == 9.0
    assert slept == [9.0]
    assert async_.effective_concurrency == 2


# ---------------------------------------------------------------------------
# ContextVar seam
# ---------------------------------------------------------------------------


def test_context_var_defaults_to_none() -> None:
    """The async-limiter ContextVar is OPTIONAL — defaults to ``None`` so the
    async retry path works limiter-free."""
    assert current_async_rate_limiter.get() is None


def test_context_var_round_trips_a_limiter() -> None:
    """A set limiter is readable back, then resets cleanly via the token."""
    _, async_ = make_rate_limiters(max_concurrent_calls=4)
    token = current_async_rate_limiter.set(async_)
    try:
        got = current_async_rate_limiter.get()
        assert got is async_
        assert isinstance(got, AsyncRateLimiter)
    finally:
        current_async_rate_limiter.reset(token)
    assert current_async_rate_limiter.get() is None


def test_sync_limiter_is_distinct_class_from_async() -> None:
    """The pair are distinct concrete classes (threading vs asyncio sleep)."""
    sync, async_ = make_rate_limiters(max_concurrent_calls=4)
    assert isinstance(sync, SyncRateLimiter)
    assert isinstance(async_, AsyncRateLimiter)
