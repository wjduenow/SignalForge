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
from datetime import UTC, datetime, timedelta

import pytest

from signalforge.llm import _rate_limiter as rl_module
from signalforge.llm._rate_limiter import (
    AsyncRateLimiter,
    RateLimitBudget,
    SyncRateLimiter,
    current_async_rate_limiter,
    make_async_gate,
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


# ---------------------------------------------------------------------------
# Adaptive concurrency gate (#202 QG-FIX-1)
# ---------------------------------------------------------------------------


def test_gate_shares_limiter_state() -> None:
    """The gate reads its cap from the SAME shared state cell as the limiter —
    one source of truth, no second concurrency value."""
    _, async_ = make_rate_limiters(max_concurrent_calls=5)
    gate = make_async_gate(async_)
    assert gate._state is async_.state


def test_gate_admits_up_to_effective_concurrency() -> None:
    """The gate admits at most ``effective_concurrency`` in-flight calls; the
    (N+1)th acquire BLOCKS until a release frees a slot."""
    _, async_ = make_rate_limiters(max_concurrent_calls=2)
    gate = make_async_gate(async_)

    async def _run() -> None:
        await gate.acquire()
        await gate.acquire()
        assert gate.in_flight == 2

        # A third acquire must block — the cap is 2.
        third = asyncio.ensure_future(gate.acquire())
        await asyncio.sleep(0)  # give it a chance to (not) complete
        assert not third.done(), "the gate must block the 3rd acquire at cap 2"
        assert gate.in_flight == 2

        # Releasing one frees the slot — the blocked acquire now completes.
        await gate.release()
        await asyncio.wait_for(third, timeout=1.0)
        assert gate.in_flight == 2

    asyncio.run(_run())


def test_gate_never_admits_below_one_when_fully_throttled() -> None:
    """Even at ``effective_concurrency == 1`` (fully throttled), the gate still
    admits ONE call so the run makes progress (never deadlocks)."""
    _, async_ = make_rate_limiters(max_concurrent_calls=8)
    # Drive concurrency to 1 via repeated 429s.
    for _ in range(5):
        async_.record_rate_limited(RateLimitBudget())
    assert async_.effective_concurrency == 1
    gate = make_async_gate(async_)

    async def _run() -> None:
        await gate.acquire()
        assert gate.in_flight == 1
        # A second acquire blocks at cap 1.
        second = asyncio.ensure_future(gate.acquire())
        await asyncio.sleep(0)
        assert not second.done()
        await gate.release()
        await asyncio.wait_for(second, timeout=1.0)

    asyncio.run(_run())


def test_gate_narrows_under_429_storm() -> None:
    """A 429 narrows the cap below ``max_concurrent_calls`` — a blocked acquire
    that would have been admitted at the old cap stays blocked."""
    _, async_ = make_rate_limiters(max_concurrent_calls=4)
    gate = make_async_gate(async_)

    async def _run() -> None:
        # Fill 2 slots (cap is still 4 here).
        await gate.acquire()
        await gate.acquire()
        # A 429 storm narrows the cap 4 → 2 → 1.
        async_.record_rate_limited(RateLimitBudget())
        async_.record_rate_limited(RateLimitBudget())
        assert async_.effective_concurrency == 1
        # Now in_flight (2) >= cap (1): a new acquire BLOCKS even though the
        # raw max_concurrent_calls would have allowed it.
        blocked = asyncio.ensure_future(gate.acquire())
        await asyncio.sleep(0)
        assert not blocked.done(), "the narrowed cap must block new admits"
        # Drain both in-flight; only when in_flight falls under the cap (to 0,
        # below cap 1) does the blocked acquire proceed.
        await gate.release()
        await asyncio.sleep(0)
        assert not blocked.done(), "in_flight=1 still >= cap=1 → still blocked"
        await gate.release()
        await asyncio.wait_for(blocked, timeout=1.0)

    asyncio.run(_run())


def test_gate_widens_back_on_headroom_wakes_waiter() -> None:
    """A headroom increase that widens the cap wakes a blocked acquirer (no
    lost wakeup) — concurrency probes back up toward ``max_concurrent_calls``."""
    _, async_ = make_rate_limiters(max_concurrent_calls=8)
    # Narrow to 1.
    for _ in range(5):
        async_.record_rate_limited(RateLimitBudget())
    assert async_.effective_concurrency == 1
    gate = make_async_gate(async_)

    async def _run() -> None:
        await gate.acquire()  # fills the only slot at cap 1
        waiter = asyncio.ensure_future(gate.acquire())
        await asyncio.sleep(0)
        assert not waiter.done(), "blocked at cap 1 with 1 in-flight"
        # Headroom widens the cap 1 → 2. The next release's notify wakes the
        # waiter against the larger cap. (Mirrors the engine: headroom fires on
        # a clean completion, just before the gate release.)
        async_.record_headroom()
        assert async_.effective_concurrency == 2
        await gate.release()
        await asyncio.wait_for(waiter, timeout=1.0)
        # in_flight back to 1 (released 1, admitted the waiter) under cap 2.
        assert gate.in_flight == 1

    asyncio.run(_run())


def test_gate_cap_stays_within_bounds_under_mixed_signal() -> None:
    """Across an interleaving of 429s and headroom, the gate's cap (the limiter
    effective concurrency, floored at 1) never exceeds max nor drops below 1."""
    _, async_ = make_rate_limiters(max_concurrent_calls=6)
    gate = make_async_gate(async_)
    for signal in ["429", "ok", "429", "429", "ok", "ok", "429", "ok", "ok"]:
        if signal == "429":
            async_.record_rate_limited(RateLimitBudget())
        else:
            async_.record_headroom()
        assert 1 <= gate._capacity() <= 6


def test_gate_release_in_finally_drains_on_exception() -> None:
    """``async with gate`` releases the slot even when the body raises — the
    release-in-finally correctness the cancellation path relies on."""
    _, async_ = make_rate_limiters(max_concurrent_calls=3)
    gate = make_async_gate(async_)

    async def _run() -> None:
        with pytest.raises(ValueError, match="boom"):
            async with gate:
                assert gate.in_flight == 1
                raise ValueError("boom")
        # The slot drained despite the exception.
        assert gate.in_flight == 0

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Reset-header fallback wait (#202 QG-FIX-3)
# ---------------------------------------------------------------------------


def test_reset_wait_used_when_no_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 with NO ``retry-after`` but a ``requests_reset`` instant derives a
    bounded wait until that reset (the QG-FIX-3 reset fallback)."""
    now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(rl_module, "_utcnow", lambda: now)
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    reset = (now + timedelta(seconds=15)).isoformat()
    wait = sync.record_rate_limited(RateLimitBudget(requests_reset=reset))
    assert wait == pytest.approx(15.0)


def test_reset_wait_picks_soonest_of_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both request + token resets are present, the SOONEST is used."""
    now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(rl_module, "_utcnow", lambda: now)
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    budget = RateLimitBudget(
        requests_reset=(now + timedelta(seconds=40)).isoformat(),
        tokens_reset=(now + timedelta(seconds=10)).isoformat(),
    )
    assert sync.record_rate_limited(budget) == pytest.approx(10.0)


def test_reset_wait_clamps_far_future_to_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pathological far-future reset clamps to the 60s cap (clock-skew guard)."""
    now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(rl_module, "_utcnow", lambda: now)
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    reset = (now + timedelta(hours=1)).isoformat()
    assert sync.record_rate_limited(RateLimitBudget(requests_reset=reset)) == 60.0


def test_reset_wait_past_instant_clamps_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reset already in the past means "retry now" → 0.0."""
    now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(rl_module, "_utcnow", lambda: now)
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    reset = (now - timedelta(seconds=5)).isoformat()
    assert sync.record_rate_limited(RateLimitBudget(requests_reset=reset)) == 0.0


def test_retry_after_takes_priority_over_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """``retry-after`` wins over a reset instant when both are present."""
    now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(rl_module, "_utcnow", lambda: now)
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    budget = RateLimitBudget(
        retry_after=3.0,
        requests_reset=(now + timedelta(seconds=15)).isoformat(),
    )
    assert sync.record_rate_limited(budget) == 3.0


def test_malformed_reset_falls_through_to_none() -> None:
    """A malformed reset string (no retry-after) → ``None`` (blind backoff)."""
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    assert sync.record_rate_limited(RateLimitBudget(requests_reset="not-a-date")) is None


def test_reset_wait_accepts_trailing_z(monkeypatch: pytest.MonkeyPatch) -> None:
    """An RFC-3339 ``Z``-suffixed reset parses (normalised to +00:00)."""
    now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(rl_module, "_utcnow", lambda: now)
    sync, _ = make_rate_limiters(max_concurrent_calls=4)
    # 20s ahead, expressed with a trailing Z.
    reset = "2026-06-05T12:00:20Z"
    assert sync.record_rate_limited(RateLimitBudget(requests_reset=reset)) == pytest.approx(20.0)
