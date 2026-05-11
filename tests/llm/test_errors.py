"""Unit tests for the LLM errors module (US-003, DEC-004 / DEC-009 / DEC-024).

Mirrors :mod:`tests.warehouse.test_errors` and :mod:`tests.safety.test_errors`.
Every test is capable of failing: no ``assert True``-shaped placeholders
(``testing-signal.md``).

Note: this directory deliberately has NO ``__init__.py``. Pytest's rootdir
+ ``--import-mode=importlib`` discovers the file via basename namespacing
without polluting the import graph.
"""

from __future__ import annotations

import pytest

from signalforge.llm import errors as errors_module
from signalforge.llm.errors import (
    LLMAuthError,
    LLMCacheTooLargeError,
    LLMConnectionError,
    LLMError,
    LLMHelperError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMServerError,
    _format_value,
)

# Constructor kwargs for each subclass — keeps the parametrised "every
# subclass constructible" test honest as new subclasses get added (the
# missing entry breaks at collection rather than silently skipping).
_CONSTRUCT_KWARGS: dict[str, dict[str, object]] = {
    "LLMError": {"message": "generic LLM failure"},
    "LLMHelperError": {"message": "generic helper failure"},
    "LLMAuthError": {"message": "401 unauthorized"},
    "LLMRateLimitError": {"message": "429 after retries", "attempts": 3},
    "LLMServerError": {"message": "5xx after retries"},
    "LLMConnectionError": {"message": "connection reset"},
    "LLMResponseFormatError": {"message": "missing content block"},
    "LLMCacheTooLargeError": {"cached_block_tokens": 9000},
    "EstimateUnknownModelError": {"model": "fake-model-id"},
}


@pytest.mark.unit
@pytest.mark.llm
def test_llm_error_renders_remediation() -> None:
    """The base ``__str__`` includes both the message and the
    ``↳ Remediation:`` marker line."""
    rendered = str(LLMError("boom", remediation="fix it"))
    assert "boom" in rendered
    assert "↳ Remediation: fix it" in rendered


@pytest.mark.unit
@pytest.mark.llm
def test_all_is_sorted_and_complete() -> None:
    """``__all__`` is alphabetically sorted and lists 8 classes total
    (LLMError + 7 subclasses)."""
    assert errors_module.__all__ == sorted(errors_module.__all__)
    # 1 base (LLMError) + 1 umbrella (LLMHelperError) + 5 helper subclasses
    # (Auth/RateLimit/Server/Connection/ResponseFormat) + 1 cache-size
    # subclass (TooLarge) + 1 estimate subclass (EstimateUnknownModelError,
    # US-001 of #36) = 9 classes. (LLMCacheTooSmallError was dropped
    # in #10's follow-up — Anthropic silently no-ops a sub-minimum cache
    # marker, so the production code drops the marker and continues
    # rather than raising.)
    assert len(errors_module.__all__) == 9, (
        "US-003 + US-001 of #36 enumerate 8 typed subclasses + 1 base; "
        "update tests and __all__ together if this changes."
    )


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.parametrize("name", sorted(_CONSTRUCT_KWARGS))
def test_each_subclass_has_default_remediation(name: str) -> None:
    """Every class in ``__all__`` declares a non-empty ``default_remediation``
    and subclasses :class:`LLMError`. Iterating ``__all__`` (rather than a
    hand-curated tuple) is what catches a future contributor who adds a
    class but forgets the remediation."""
    assert name in errors_module.__all__, (
        f"{name} listed in _CONSTRUCT_KWARGS but missing from __all__"
    )
    cls = getattr(errors_module, name)
    assert issubclass(cls, LLMError), f"{name} must subclass LLMError"
    remediation = cls.default_remediation
    assert isinstance(remediation, str)
    assert remediation.strip(), f"{name}.default_remediation must be non-empty"


@pytest.mark.unit
@pytest.mark.llm
def test_every_subclass_constructible_and_renders() -> None:
    """Smoke-test the constructor signature and ``__str__`` for every class
    in ``__all__``. The ``_CONSTRUCT_KWARGS`` table is intentionally a tight
    coupling: adding a new subclass without an entry breaks this test."""
    missing = set(errors_module.__all__) - set(_CONSTRUCT_KWARGS)
    assert not missing, f"_CONSTRUCT_KWARGS missing entries: {sorted(missing)}"
    for name in errors_module.__all__:
        cls = getattr(errors_module, name)
        kwargs = _CONSTRUCT_KWARGS[name]
        instance = cls(**kwargs)
        rendered = str(instance)
        assert "↳ Remediation:" in rendered, f"{name} did not render remediation"


@pytest.mark.unit
@pytest.mark.llm
def test_llm_helper_error_carries_cause() -> None:
    """The umbrella exposes ``cause`` so SDK-call failures can be chained
    with the underlying exception preserved (DEC-004)."""
    underlying = RuntimeError("socket reset")
    err = LLMHelperError("call failed", cause=underlying)
    assert err.cause is underlying
    # Default cause is None.
    err_no_cause = LLMHelperError("call failed")
    assert err_no_cause.cause is None


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.error
def test_llm_helper_error_caught_by_llm_error_base() -> None:
    """``except LLMError`` catches helper-error subclasses — proves the
    inheritance chain is what US-003 specifies, not a sibling layout."""
    caught: LLMError | None = None
    try:
        raise LLMConnectionError("connection reset")
    except LLMError as exc:
        caught = exc
    assert isinstance(caught, LLMConnectionError)
    assert isinstance(caught, LLMHelperError)


@pytest.mark.unit
@pytest.mark.llm
def test_llm_rate_limit_error_includes_attempts() -> None:
    """The ``attempts`` field discriminates "tried N times" from a single
    429 response — DEC-004's retry policy makes this load-bearing for ops
    queries."""
    err = LLMRateLimitError("429 after retries", attempts=3)
    assert err.attempts == 3
    # Cause kwarg also flows through.
    underlying = RuntimeError("rate limited")
    err_with_cause = LLMRateLimitError("429 after retries", attempts=2, cause=underlying)
    assert err_with_cause.attempts == 2
    assert err_with_cause.cause is underlying


@pytest.mark.unit
@pytest.mark.llm
def test_llm_cache_too_large_error_carries_block_size_and_cap() -> None:
    """Two discriminating attributes: ``cached_block_tokens`` and ``cap``.
    The cap defaults to 8000 (DEC-009)."""
    err = LLMCacheTooLargeError(cached_block_tokens=9000)
    assert err.cached_block_tokens == 9000
    assert err.cap == 8000
    rendered = str(err)
    assert "9000" in rendered
    assert "8000" in rendered

    # Cap can be overridden (e.g. for test fixtures pinning a smaller cap).
    err_custom = LLMCacheTooLargeError(cached_block_tokens=200, cap=100)
    assert err_custom.cap == 100


@pytest.mark.unit
@pytest.mark.llm
def test_llm_auth_error_remediation_mentions_anthropic_api_key() -> None:
    """Failing this test means the remediation lost its actionable hint —
    a regression that erodes the explainable-diffs commitment. The
    remediation MUST point at ``ANTHROPIC_API_KEY`` so the operator knows
    which environment variable to set."""
    rendered = str(LLMAuthError("401 unauthorized"))
    assert "ANTHROPIC_API_KEY" in rendered


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.error
def test_user_input_repr_quoted_in_messages() -> None:
    """DEC-022: user-controlled values flow through ``_format_value`` (==
    ``repr()``) so adversarial input — embedded ANSI escapes, control
    characters — cannot pollute log viewers or stack traces."""
    adversarial = "claude\x1b[31mevil\x1b[0m"
    # Sanity: the helper IS repr().
    assert _format_value(adversarial) == repr(adversarial)
    # And the raw escape sequence should NOT appear in the repr (repr
    # escapes the ESC byte).
    assert "\x1b" not in repr(adversarial)

    # The helper itself is the load-bearing surface — guarded directly via
    # ``_format_value(adversarial) == repr(adversarial)`` above. New typed
    # errors that surface user-controlled values must route through it.
    assert "\x1b" not in repr(adversarial)


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.error
def test_remediation_override_per_instance() -> None:
    """Explicit ``remediation=`` overrides the class default, mirroring the
    safety/warehouse precedent."""
    err = LLMAuthError("401 unauthorized", remediation="custom hint")
    rendered = str(err)
    assert "custom hint" in rendered
    assert LLMAuthError.default_remediation not in rendered


@pytest.mark.unit
@pytest.mark.llm
def test_llm_helper_subclasses_caught_by_umbrella() -> None:
    """``except LLMHelperError`` catches every retry-taxonomy branch — keeps
    the "I just want to know the SDK call failed" caller path simple."""
    helper_subclasses = (
        LLMAuthError("401"),
        LLMRateLimitError("429", attempts=3),
        LLMServerError("500"),
        LLMConnectionError("conn"),
        LLMResponseFormatError("shape"),
    )
    for instance in helper_subclasses:
        caught: LLMHelperError | None = None
        try:
            raise instance
        except LLMHelperError as exc:
            caught = exc
        assert caught is instance


@pytest.mark.unit
@pytest.mark.llm
def test_cache_errors_not_helper_subclasses() -> None:
    """The cache-size error fires BEFORE any SDK call (DEC-024 pre-send
    check), so it intentionally lives outside the ``LLMHelperError``
    umbrella. Catching ``LLMHelperError`` must NOT swallow it."""
    assert not issubclass(LLMCacheTooLargeError, LLMHelperError)
    # Still an ``LLMError`` instance so a top-level catch-all works.
    assert issubclass(LLMCacheTooLargeError, LLMError)
