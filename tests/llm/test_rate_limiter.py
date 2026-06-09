"""Tests for the rate-limit value object + provider extract seam (#202 US-002).

DEC-205. Covers:

* :class:`signalforge.llm._rate_limiter.RateLimitBudget` — frozen, all-optional
  value object; the empty (zero-arg) constructor; field population.
* :meth:`AnthropicProvider.extract_rate_limit_info` — populated from
  ``retry-after`` + ``anthropic-ratelimit-*`` headers on a 429 exception AND on
  a success response; ``response=`` precedence over ``exc``.
* :meth:`OpenAIProvider` / :meth:`GeminiProvider` inherit the base default —
  an EMPTY budget (graceful degradation, they don't expose these headers).
* present / absent / malformed headers handled without raising.
* no vendor SDK type leaks the provider boundary — the return is always a bare
  :class:`RateLimitBudget`.

Every test is capable of failing — no ``assert True`` placeholders
(``testing-signal.md``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from signalforge.llm._rate_limiter import RateLimitBudget
from signalforge.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    OpenAIProvider,
)

from ._fake import FakeRateLimitError, FakeResponseWithHeaders

# A fully-populated, well-formed Anthropic rate-limit header set.
_FULL_HEADERS = {
    "retry-after": "42",
    "anthropic-ratelimit-requests-remaining": "5",
    "anthropic-ratelimit-tokens-remaining": "12000",
    "anthropic-ratelimit-requests-reset": "2026-06-04T12:00:00Z",
    "anthropic-ratelimit-tokens-reset": "2026-06-04T12:00:30Z",
}


# ---------------------------------------------------------------------------
# RateLimitBudget value object
# ---------------------------------------------------------------------------


def test_rate_limit_budget_empty_is_all_none() -> None:
    """The zero-argument constructor yields an EMPTY budget — every field
    ``None`` (the OpenAI / Gemini and no-headers cases)."""
    budget = RateLimitBudget()
    assert budget.requests_remaining is None
    assert budget.tokens_remaining is None
    assert budget.requests_reset is None
    assert budget.tokens_reset is None
    assert budget.retry_after is None


def test_rate_limit_budget_populated_fields_round_trip() -> None:
    """All five fields populate independently and keep their types."""
    budget = RateLimitBudget(
        requests_remaining=5,
        tokens_remaining=12000,
        requests_reset="2026-06-04T12:00:00Z",
        tokens_reset="2026-06-04T12:00:30Z",
        retry_after=42.0,
    )
    assert budget.requests_remaining == 5
    assert budget.tokens_remaining == 12000
    assert budget.requests_reset == "2026-06-04T12:00:00Z"
    assert budget.tokens_reset == "2026-06-04T12:00:30Z"
    assert budget.retry_after == 42.0


def test_rate_limit_budget_is_frozen() -> None:
    """The value object is frozen — assignment after construction raises."""
    budget = RateLimitBudget(requests_remaining=1)
    with pytest.raises(ValidationError):
        budget.requests_remaining = 2  # type: ignore[misc]


def test_rate_limit_budget_ignores_extra_fields() -> None:
    """``extra="ignore"`` — an unknown kwarg is dropped, not an error
    (matches the neighbouring ``UsageMetrics`` convention)."""
    budget = RateLimitBudget(requests_remaining=3, bogus="x")  # type: ignore[call-arg]
    assert budget.requests_remaining == 3
    assert not hasattr(budget, "bogus")


# ---------------------------------------------------------------------------
# AnthropicProvider.extract_rate_limit_info — populated from headers
# ---------------------------------------------------------------------------


def test_anthropic_populated_from_exception_headers() -> None:
    """A 429 whose ``response.headers`` carry the full set populates every
    field of the budget."""
    exc = FakeRateLimitError(headers=_FULL_HEADERS)
    budget = AnthropicProvider().extract_rate_limit_info(exc)
    assert budget.retry_after == 42.0
    assert budget.requests_remaining == 5
    assert budget.tokens_remaining == 12000
    assert budget.requests_reset == "2026-06-04T12:00:00Z"
    assert budget.tokens_reset == "2026-06-04T12:00:30Z"


def test_anthropic_populated_from_success_response_headers() -> None:
    """A success ``response`` (direct ``.headers``) populates the budget — the
    limiter can learn the live window without waiting for a 429."""
    response = FakeResponseWithHeaders(headers=_FULL_HEADERS)
    exc = FakeRateLimitError(headers={})
    budget = AnthropicProvider().extract_rate_limit_info(exc, response=response)
    assert budget.requests_remaining == 5
    assert budget.tokens_remaining == 12000
    assert budget.retry_after == 42.0


def test_anthropic_success_response_takes_precedence_over_exception() -> None:
    """When BOTH a success response and an exception carry headers, the
    response (the live 200 window) wins."""
    response = FakeResponseWithHeaders(headers={"anthropic-ratelimit-requests-remaining": "99"})
    exc = FakeRateLimitError(headers={"anthropic-ratelimit-requests-remaining": "1"})
    budget = AnthropicProvider().extract_rate_limit_info(exc, response=response)
    assert budget.requests_remaining == 99


def test_anthropic_zero_remaining_is_not_none() -> None:
    """A present ``0`` remaining is distinct from absent (``None``) — the
    window is exhausted, not unknown."""
    exc = FakeRateLimitError(headers={"anthropic-ratelimit-requests-remaining": "0"})
    budget = AnthropicProvider().extract_rate_limit_info(exc)
    assert budget.requests_remaining == 0


# ---------------------------------------------------------------------------
# Present / absent / malformed headers — never raises
# ---------------------------------------------------------------------------


def test_anthropic_absent_headers_yield_empty_budget() -> None:
    """A 429 with NO headers at all degrades to an EMPTY budget — no raise."""
    exc = FakeRateLimitError(headers={})
    budget = AnthropicProvider().extract_rate_limit_info(exc)
    assert budget == RateLimitBudget()


def test_anthropic_no_response_attribute_yields_empty_budget() -> None:
    """An exception lacking a ``response`` attribute entirely (a non-SDK
    exception) degrades to EMPTY — the getattr walk never raises."""
    budget = AnthropicProvider().extract_rate_limit_info(ValueError("boom"))
    assert budget == RateLimitBudget()


def test_anthropic_partial_headers_populate_only_present_fields() -> None:
    """A subset of headers populates only those fields; the rest stay
    ``None`` (each field parsed independently)."""
    exc = FakeRateLimitError(headers={"anthropic-ratelimit-requests-remaining": "7"})
    budget = AnthropicProvider().extract_rate_limit_info(exc)
    assert budget.requests_remaining == 7
    assert budget.tokens_remaining is None
    assert budget.retry_after is None
    assert budget.requests_reset is None


@pytest.mark.parametrize(
    "bad_value",
    ["", "  ", "not-a-number", "5.5", "1e3x", "NaNny"],
)
def test_anthropic_malformed_numeric_header_stays_none(bad_value: str) -> None:
    """A malformed numeric header leaves THAT field ``None`` without raising —
    and never poisons the other fields."""
    exc = FakeRateLimitError(
        headers={
            "anthropic-ratelimit-requests-remaining": bad_value,
            "anthropic-ratelimit-tokens-remaining": "500",
        }
    )
    budget = AnthropicProvider().extract_rate_limit_info(exc)
    assert budget.requests_remaining is None
    assert budget.tokens_remaining == 500


@pytest.mark.parametrize("bad_value", ["", "abc", "  "])
def test_anthropic_malformed_retry_after_stays_none(bad_value: str) -> None:
    """A malformed ``retry-after`` leaves ``retry_after`` ``None``."""
    exc = FakeRateLimitError(headers={"retry-after": bad_value})
    budget = AnthropicProvider().extract_rate_limit_info(exc)
    assert budget.retry_after is None


@pytest.mark.parametrize("bad_value", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_anthropic_non_finite_retry_after_stays_none(bad_value: str) -> None:
    """A non-finite ``retry-after`` (``nan`` / ``inf``) is treated as malformed —
    ``float()`` would parse it, but a non-finite wait would poison the backoff
    arithmetic, so the finite-guard in ``_parse_float`` returns ``None``."""
    exc = FakeRateLimitError(headers={"retry-after": bad_value})
    budget = AnthropicProvider().extract_rate_limit_info(exc)
    assert budget.retry_after is None


def test_anthropic_fractional_retry_after_parses_as_float() -> None:
    """``retry-after`` tolerates a fractional-second value some gateways emit."""
    exc = FakeRateLimitError(headers={"retry-after": "1.5"})
    budget = AnthropicProvider().extract_rate_limit_info(exc)
    assert budget.retry_after == 1.5


def test_anthropic_empty_reset_string_stays_none() -> None:
    """A whitespace-only reset header is treated as absent, not as a real
    (empty) reset time."""
    exc = FakeRateLimitError(headers={"anthropic-ratelimit-requests-reset": "   "})
    budget = AnthropicProvider().extract_rate_limit_info(exc)
    assert budget.requests_reset is None


# ---------------------------------------------------------------------------
# OpenAI / Gemini — inherit the base default (EMPTY budget)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", [OpenAIProvider(), GeminiProvider()])
def test_non_anthropic_providers_return_empty_budget(provider: object) -> None:
    """OpenAI / Gemini don't expose these headers — they return an EMPTY
    budget even when handed an exception that DOES carry Anthropic-shaped
    headers (they inherit the base default, which ignores the source)."""
    exc = FakeRateLimitError(headers=_FULL_HEADERS)
    budget = provider.extract_rate_limit_info(exc)  # type: ignore[attr-defined]
    assert budget == RateLimitBudget()


@pytest.mark.parametrize("provider", [OpenAIProvider(), GeminiProvider()])
def test_non_anthropic_providers_empty_even_with_response(provider: object) -> None:
    """The base default ignores a success ``response`` too."""
    response = FakeResponseWithHeaders(headers=_FULL_HEADERS)
    budget = provider.extract_rate_limit_info(  # type: ignore[attr-defined]
        ValueError("x"), response=response
    )
    assert budget == RateLimitBudget()


# ---------------------------------------------------------------------------
# No vendor type leaks the provider boundary
# ---------------------------------------------------------------------------


def test_extract_returns_neutral_type_only() -> None:
    """The provider hands back a bare :class:`RateLimitBudget` — never an
    ``httpx.Headers`` / ``anthropic.*`` value. The neutral type is the seam
    boundary (DEC-012 / DEC-205)."""
    exc = FakeRateLimitError(headers=_FULL_HEADERS)
    budget = AnthropicProvider().extract_rate_limit_info(exc)
    assert type(budget) is RateLimitBudget


def test_providers_module_never_imports_anthropic_sdk() -> None:
    """The Anthropic rate-limit extractor reaches headers purely by duck-typed
    ``getattr`` — ``providers.py`` issues no ``import``/``from`` of the
    ``anthropic`` SDK at ANY scope (an AST scan over every import node, so a
    docstring mention like ``import anthropic`` doesn't false-positive). The
    ``anthropic`` SDK is the one whose construction + type-ignores DEC-012
    confines to ``_anthropic_client.py``; keeping it out of ``providers.py``
    entirely is what proves no vendor type is in scope at the rate-limit
    boundary — the extractor hands back only the neutral budget."""
    import ast

    import signalforge.llm.providers as providers_mod

    source = providers_mod.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.split(".")[0])

    assert "anthropic" not in imported_roots, (
        "providers.py must not import the anthropic SDK at any scope; "
        "the rate-limit extractor reaches headers via getattr only."
    )
