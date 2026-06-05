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

from collections.abc import Mapping

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


__all__ = [
    "RateLimitBudget",
]
