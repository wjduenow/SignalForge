"""Provider-neutral rate-limit value object + header-parse helper (#202 US-002).

DEC-205. This module ships ONLY the neutral :class:`RateLimitBudget` value
object plus a vendor-neutral parse helper. The (later) shared rate limiter
(US-003 / US-004) and the wiring into ``call_llm`` / ``call_llm_async`` are
deliberately out of scope here — this is the foundation the limiter consumes.

The object is the rate-limit analogue of :class:`signalforge.llm.providers.UsageMetrics`:
a frozen, provider-neutral receipt that a provider's
:meth:`signalforge.llm.providers.LLMProvider.extract_rate_limit_info` populates
from a vendor's response / exception headers. Providers whose SDK does not
expose these signals (OpenAI / Gemini) return an EMPTY budget (all fields
``None``) — graceful degradation, never a raise.

Every field is optional (``None`` when the provider didn't surface it or the
header was absent / malformed). The neutral shape lets the orchestrator reason
about "how many requests / tokens are left and when does the window reset"
without touching a vendor SDK type — keeping the DEC-012 SDK-confinement
boundary intact (no ``anthropic.*`` / ``httpx.*`` type leaks past the provider
seam; the provider hands back only this object).

The parse helpers are intentionally tolerant: a missing header yields ``None``;
a malformed numeric header (non-int / non-float / empty) yields ``None`` for
that field rather than raising. This mirrors the "fail-soft on absent signal"
posture the orchestrator needs — a garbled ``retry-after`` must not crash the
retry loop; it just means "no hint, fall back to the backoff math".
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

#: Anthropic rate-limit response/exception header names (#202 DEC-205). The
#: vendor surfaces these on ``RateLimitError.response.headers`` (a 429) and on
#: a successful ``messages.create`` response's headers. ``retry-after`` is the
#: standard HTTP header (seconds until the window reopens); the
#: ``anthropic-ratelimit-*`` family carries the remaining-count / reset-time
#: signals. Kept here (not in ``providers.py``) so the parse helper and the
#: provider override read one shared source of truth.
_HEADER_RETRY_AFTER = "retry-after"
_HEADER_REQUESTS_REMAINING = "anthropic-ratelimit-requests-remaining"
_HEADER_TOKENS_REMAINING = "anthropic-ratelimit-tokens-remaining"
_HEADER_REQUESTS_RESET = "anthropic-ratelimit-requests-reset"
_HEADER_TOKENS_RESET = "anthropic-ratelimit-tokens-reset"


class RateLimitBudget(BaseModel):
    """Neutral rate-limit snapshot a provider extracts from response / exception
    headers (#202 DEC-205).

    The rate-limit analogue of :class:`signalforge.llm.providers.UsageMetrics`:
    frozen, provider-neutral, assembled in-process and handed to the (later)
    shared rate limiter — never deserialised from disk. ``frozen=True`` +
    ``extra="ignore"`` matches the neighbouring value-object convention.

    Every field is optional and defaults to ``None`` so an EMPTY budget (the
    OpenAI / Gemini case, and the "no headers present" Anthropic case) is the
    zero-argument constructor. ``None`` means "the provider did not surface this
    signal" — distinct from a present-but-zero value (e.g.
    ``requests_remaining == 0`` means the request window is exhausted).

    Fields:

    * :attr:`requests_remaining` — requests left in the current window
      (``anthropic-ratelimit-requests-remaining``).
    * :attr:`tokens_remaining` — tokens left in the current window
      (``anthropic-ratelimit-tokens-remaining``).
    * :attr:`requests_reset` — RFC-3339 timestamp string for when the request
      window refills (``anthropic-ratelimit-requests-reset``). Kept as the raw
      vendor string (not parsed to ``datetime``) so this object stays free of
      timezone-parsing edge cases at the value-object boundary; a consumer that
      needs an instant parses it.
    * :attr:`tokens_reset` — RFC-3339 timestamp string for the token window
      (``anthropic-ratelimit-tokens-reset``).
    * :attr:`retry_after` — seconds to wait before retrying, from the standard
      HTTP ``retry-after`` header. ``float`` to tolerate fractional-second
      values some gateways emit; whole-second values parse cleanly too.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    requests_remaining: int | None = None
    tokens_remaining: int | None = None
    requests_reset: str | None = None
    tokens_reset: str | None = None
    retry_after: float | None = None


def _parse_int(value: object) -> int | None:
    """Coerce a header value to ``int``, returning ``None`` when absent or
    malformed.

    Tolerant by design (#202 DEC-205): a missing header (``None``), an empty
    string, or a non-numeric string yields ``None`` rather than raising, so a
    garbled vendor header degrades to "no signal" instead of crashing the
    retry loop. A float-shaped string (``"5.0"``) is rejected (``None``) — the
    remaining-count headers are integers; a fractional value is malformed.
    """
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_float(value: object) -> float | None:
    """Coerce a header value to ``float``, returning ``None`` when absent or
    malformed.

    Same tolerant posture as :func:`_parse_int` (#202 DEC-205); used for
    ``retry-after``, which may be fractional. A missing header, empty string,
    or non-numeric string yields ``None``.
    """
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_str(value: object) -> str | None:
    """Coerce a header value to a non-empty ``str``, returning ``None`` when
    absent or empty.

    The reset headers are opaque RFC-3339 timestamp strings — kept verbatim
    (no datetime parse here). An absent header (``None``) or a whitespace-only
    value degrades to ``None`` so an EMPTY-string header isn't mistaken for a
    real reset time.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _budget_from_headers(headers: Mapping[str, object] | None) -> RateLimitBudget:
    """Build a :class:`RateLimitBudget` from a header mapping (#202 DEC-205).

    Vendor-neutral on purpose: any provider whose SDK uses the
    ``retry-after`` + ``anthropic-ratelimit-*`` header names can reuse this
    helper. ``headers`` is any read-only mapping (e.g. an ``httpx.Headers``
    instance — case-insensitive — or a plain ``dict``); ``None`` (no headers
    available) yields an EMPTY budget.

    Each field is parsed independently and tolerantly: a present/absent/
    malformed value for one header never affects the others, and no malformed
    value raises — it simply leaves that field ``None``.
    """
    if headers is None:
        return RateLimitBudget()
    return RateLimitBudget(
        requests_remaining=_parse_int(_get(headers, _HEADER_REQUESTS_REMAINING)),
        tokens_remaining=_parse_int(_get(headers, _HEADER_TOKENS_REMAINING)),
        requests_reset=_parse_str(_get(headers, _HEADER_REQUESTS_RESET)),
        tokens_reset=_parse_str(_get(headers, _HEADER_TOKENS_RESET)),
        retry_after=_parse_float(_get(headers, _HEADER_RETRY_AFTER)),
    )


def _get(headers: Mapping[str, object], key: str) -> object:
    """Read ``key`` from ``headers``, returning ``None`` when absent.

    ``httpx.Headers`` is case-insensitive and supports ``.get``; a plain
    ``dict`` does too. Wrapped so a mapping whose ``__getitem__`` raises on a
    missing key never propagates — absence is always ``None``.
    """
    try:
        return headers.get(key)
    except (AttributeError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Shared AIMD rate-limiter state + sync/async limiters (#202 US-003 / DEC-205)
# ---------------------------------------------------------------------------
#
# The thundering-herd 429 storm this fixes: under concurrency, a wave of
# ``call_llm_async`` coroutines all hit a 429, then each independently sleeps a
# *blind* exponential backoff and all wake at ~the same time, bursting back
# past the rate limit and provoking the next 429 — repeat until the per-class
# retry budget exhausts and grades degrade. Two coupled mechanisms tame it:
#
# 1. Header-honouring wait. When the vendor surfaces a ``retry-after`` (or a
#    reset hint), the limiter waits AT the rate limit instead of a blind
#    ``2**attempt`` guess. The wait is the value object the provider already
#    extracts — no vendor type crosses the seam.
# 2. AIMD adaptive concurrency. A 429 multiplicatively DECREASES the effective
#    concurrency (halve, floor 1); observed headroom additively INCREASES it
#    (+1, capped at ``max_concurrent_calls``). Classic TCP-congestion shape:
#    back off hard on congestion, probe back up gently. The effective
#    concurrency is always clamped to ``[1, max_concurrent_calls]``.
#
# The sync (``call_llm``) and async (``call_llm_async``) paths SHARE one
# :class:`_RateLimiterState` cell so a 429 seen on either path tightens the
# budget the other path reads. The cell is guarded by a plain
# :class:`threading.Lock` — the AIMD bookkeeping is a few-microsecond critical
# section with no ``await`` inside it, so a threading lock is safe to take from
# an async coroutine (it never yields the event loop while held). The two
# limiter classes differ ONLY in how they SLEEP: threading vs. asyncio.

# Backoff math constant — mirrors the blind-backoff jitter window used by
# ``signalforge.llm.client._backoff_warn`` so the fallback path (no headers)
# stays byte-compatible with the historical retry delay.
_JITTER_LOW = 0.75
_JITTER_HIGH = 1.25


@dataclass
class _RateLimiterState:
    """Mutable AIMD state shared by the sync + async limiters (#202 DEC-205).

    Holds the latest :class:`RateLimitBudget` snapshot plus the AIMD effective
    concurrency, guarded by ``_lock``. A single instance is shared by a
    :class:`SyncRateLimiter` and an :class:`AsyncRateLimiter` so a 429 observed
    on one path tightens the concurrency the other path probes from — the
    cooperation the thundering-herd fix requires.

    The lock is a plain :class:`threading.Lock`: every method holds it for only
    the few non-blocking arithmetic statements of the AIMD update (no ``await``,
    no I/O inside the critical section), so taking it from an async coroutine
    never stalls the event loop.
    """

    max_concurrent_calls: int
    #: Current AIMD effective concurrency, always clamped to
    #: ``[1, max_concurrent_calls]``. Starts wide-open at the cap and decreases
    #: under congestion.
    effective_concurrency: int = field(default=0)
    #: The most recent budget snapshot extracted from a 429 / success response.
    budget: RateLimitBudget = field(default_factory=RateLimitBudget)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        # ``max_concurrent_calls`` is the ceiling AND the starting point — a
        # fresh run probes from wide-open and only narrows on observed
        # congestion. Clamp to >= 1 so a degenerate config can't deadlock.
        if self.max_concurrent_calls < 1:
            self.max_concurrent_calls = 1
        self.effective_concurrency = self.max_concurrent_calls

    def record_budget(self, budget: RateLimitBudget) -> None:
        """Store the latest budget snapshot (from a 429 or success response)."""
        with self._lock:
            self.budget = budget

    def on_rate_limited(self, budget: RateLimitBudget) -> int:
        """AIMD multiplicative-decrease on a 429; returns the new concurrency.

        Halves the effective concurrency (floored at 1) and records the budget
        in one atomic step so a concurrent reader never sees a half-applied
        update. The decrease is what stops the next wave from bursting straight
        back into the limit.
        """
        with self._lock:
            self.budget = budget
            self.effective_concurrency = max(1, self.effective_concurrency // 2)
            return self.effective_concurrency

    def on_headroom(self) -> int:
        """AIMD additive-increase when there is headroom; returns the new value.

        Adds one to the effective concurrency, capped at
        ``max_concurrent_calls``. Called after a clean (non-429) outcome — the
        gentle probe back up toward the configured ceiling.
        """
        with self._lock:
            self.effective_concurrency = min(
                self.max_concurrent_calls, self.effective_concurrency + 1
            )
            return self.effective_concurrency

    def snapshot(self) -> tuple[int, RateLimitBudget]:
        """Atomically read ``(effective_concurrency, budget)``."""
        with self._lock:
            return self.effective_concurrency, self.budget


def _wait_from_budget(budget: RateLimitBudget) -> float | None:
    """Compute a header-honouring wait (seconds) from ``budget``, or ``None``.

    Returns the ``retry-after`` value when the provider surfaced one — that is
    the vendor's explicit "wait this long" instruction and the most reliable
    pace-at-the-limit signal. An EMPTY budget (no headers — OpenAI / Gemini, or
    an Anthropic 429 that carried none) returns ``None`` so the caller falls
    back to the blind exponential backoff. A present-but-zero ``retry-after``
    (``0.0``) is honoured as "retry immediately" rather than treated as absent.
    """
    if budget.retry_after is not None:
        # Clamp negative gateway values to 0 — a negative wait is nonsensical.
        return max(0.0, budget.retry_after)
    return None


def _blind_backoff(attempt: int, rand_uniform: Callable[[float, float], float]) -> float:
    """The historical blind exponential backoff with bounded jitter.

    Byte-compatible with ``signalforge.llm.client._backoff_warn``'s delay:
    ``(2**attempt) * uniform(0.75, 1.25)``. Used as the fallback when the budget
    carries no header hint, so the no-limiter / no-header path is unchanged.
    """
    return (2**attempt) * rand_uniform(_JITTER_LOW, _JITTER_HIGH)


class _BaseRateLimiter:
    """Shared AIMD + wait-computation logic for the sync/async limiters.

    Subclasses supply only the SLEEP primitive (threading vs. asyncio); every
    other decision — what wait to use, how to mutate the AIMD state — lives here
    so the two paths can never drift. Both subclasses are constructed around the
    same :class:`_RateLimiterState`, which is how they cooperate.
    """

    def __init__(self, state: _RateLimiterState) -> None:
        self._state = state

    @property
    def state(self) -> _RateLimiterState:
        """The shared AIMD state cell (inspected by tests)."""
        return self._state

    @property
    def effective_concurrency(self) -> int:
        """Current AIMD effective concurrency (clamped to the configured cap)."""
        return self._state.snapshot()[0]

    def compute_rate_limit_wait(
        self,
        budget: RateLimitBudget,
        *,
        attempt: int,
        rand_uniform: Callable[[float, float], float],
    ) -> float:
        """Decide the wait for a 429 and apply the AIMD multiplicative-decrease.

        Computes the header-honouring wait from ``budget`` when present, else
        the blind backoff; records the 429 against the shared state (halving the
        effective concurrency). Returns the wait in seconds WITHOUT sleeping —
        the client owns the actual sleep via its ``_sleep`` / ``_async_sleep``
        seam (so the existing WARNING-then-sleep ordering + deterministic
        test-override are preserved unchanged).
        """
        self._state.on_rate_limited(budget)
        header_wait = _wait_from_budget(budget)
        if header_wait is not None:
            return header_wait
        return _blind_backoff(attempt, rand_uniform)

    def record_rate_limited(self, budget: RateLimitBudget) -> float | None:
        """Apply the AIMD decrease for a 429 and return the header wait, if any.

        Lighter sibling of :meth:`compute_rate_limit_wait` for the client's
        retry branch: it ALWAYS tightens the shared concurrency (the 429
        happened regardless of headers), then returns the header-honouring wait
        ONLY when the provider surfaced one (``retry-after`` / reset). On an
        EMPTY budget it returns ``None`` so the client falls back to its own
        ``total_attempts``-based blind backoff WITHOUT consuming a
        ``rand_uniform`` draw here (keeping the no-header path byte-identical to
        the no-limiter path).
        """
        self._state.on_rate_limited(budget)
        return _wait_from_budget(budget)

    def record_headroom(self) -> None:
        """Signal a clean outcome — AIMD additive-increase toward the cap."""
        self._state.on_headroom()


class SyncRateLimiter(_BaseRateLimiter):
    """Threading-primitive limiter for the sync :func:`call_llm` path (US-003).

    Shares its :class:`_RateLimiterState` with a sibling
    :class:`AsyncRateLimiter` (when one is paired) so a 429 on either path
    narrows the concurrency both probe from. Sleeps via an injected ``sleep``
    callable (defaults to the client's ``_sleep`` alias) so retry-branch tests
    run instantly.
    """

    def wait_after_rate_limit(
        self,
        budget: RateLimitBudget,
        *,
        attempt: int,
        sleep: Callable[[float], None],
        rand_uniform: Callable[[float, float], float],
    ) -> float:
        """Apply AIMD decrease, sleep the computed wait, return the wait.

        ``sleep`` / ``rand_uniform`` are injected (the module-level
        ``_sleep`` / ``_rand_uniform`` client aliases) so a test can pin them
        and assert the chosen wait without real-time blocking.
        """
        wait = self.compute_rate_limit_wait(budget, attempt=attempt, rand_uniform=rand_uniform)
        sleep(wait)
        return wait


class AsyncRateLimiter(_BaseRateLimiter):
    """Asyncio-primitive limiter for the async :func:`call_llm_async` path.

    Async sibling of :class:`SyncRateLimiter`; identical AIMD semantics, the
    only difference is ``await``-ing the sleep. Reachable by every concurrent
    ``call_llm_async`` coroutine that shares one run via the
    :data:`current_async_rate_limiter` ContextVar (US-004 sets it across the
    grade TaskGroup; here it defaults to ``None`` so the path works limiter-free).
    """

    async def wait_after_rate_limit(
        self,
        budget: RateLimitBudget,
        *,
        attempt: int,
        sleep: Callable[[float], Awaitable[object]],
        rand_uniform: Callable[[float, float], float],
    ) -> float:
        """Apply AIMD decrease, ``await`` the computed wait, return the wait.

        ``sleep`` is the awaitable ``_async_sleep`` client alias; ``rand_uniform``
        is the sync ``_rand_uniform`` alias (deterministic, no await needed).
        """
        wait = self.compute_rate_limit_wait(budget, attempt=attempt, rand_uniform=rand_uniform)
        await sleep(wait)
        return wait


def make_rate_limiters(
    max_concurrent_calls: int,
) -> tuple[SyncRateLimiter, AsyncRateLimiter]:
    """Build a cooperating sync + async limiter pair over one shared state.

    Both returned limiters reference the SAME :class:`_RateLimiterState`, so a
    429 observed on the sync path tightens the concurrency the async path probes
    from (and vice-versa). The caller (US-004) sets the async limiter into
    :data:`current_async_rate_limiter` for the run; the sync limiter is passed
    explicitly where needed.
    """
    state = _RateLimiterState(max_concurrent_calls=max_concurrent_calls)
    return SyncRateLimiter(state), AsyncRateLimiter(state)


#: ContextVar seam threading an :class:`AsyncRateLimiter` to every concurrent
#: ``call_llm_async`` coroutine in a run (#202 US-003 / DEC-205). Defaults to
#: ``None`` so the limiter is OPTIONAL — ``call_llm_async`` with no limiter set
#: falls back to the historical blind-backoff retry, unchanged. US-004 sets this
#: across the grade TaskGroup so one shared limiter paces the whole wave; this
#: bead provides only the seam + per-call wiring + unit proof.
current_async_rate_limiter: contextvars.ContextVar[AsyncRateLimiter | None] = (
    contextvars.ContextVar("current_async_rate_limiter", default=None)
)

#: Sync sibling of :data:`current_async_rate_limiter` (#202 US-003 / DEC-205).
#: Threads a :class:`SyncRateLimiter` to the sync ``call_llm`` 429 branch via a
#: ContextVar rather than a new keyword argument, so the sync / async
#: orchestrators keep their 1:1 signature parity (DEC-002 of #186) — the limiter
#: is an ambient run-scoped resource, not a per-call argument. Defaults to
#: ``None`` (optional limiter → blind-backoff fallback). ``make_rate_limiters``
#: builds the cooperating pair; the caller (US-004) sets both ContextVars for
#: the run.
current_sync_rate_limiter: contextvars.ContextVar[SyncRateLimiter | None] = contextvars.ContextVar(
    "current_sync_rate_limiter", default=None
)


__all__ = [
    "AsyncRateLimiter",
    "RateLimitBudget",
    "SyncRateLimiter",
    "current_async_rate_limiter",
    "current_sync_rate_limiter",
    "make_rate_limiters",
]
