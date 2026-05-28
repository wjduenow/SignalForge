"""Typed exception hierarchy for the LLM-call layer.

Implements US-003 (DEC-004 retry taxonomy + DEC-009 / DEC-024 cache-size
checks). Mirrors the style established by :mod:`signalforge.safety.errors`
and :mod:`signalforge.warehouse.errors`: every error carries a class-level
``default_remediation`` that the base ``__str__`` renders on a separate
``↳ Remediation:`` line, and every user-supplied string is rendered through
:func:`_format_value` (i.e. ``repr()``) so adversarial input — embedded
quotes, control chars, ANSI escapes — cannot smuggle special characters
into log viewers or error messages.

The remediation pattern operationalises the README's "explainable diffs"
commitment at the LLM layer's failure surface; every distinct failure mode
the SDK seam can produce gets a typed exception so the drafter / CLI / audit
layers can pattern-match without sniffing message text.

The hierarchy is two-tiered:

- :class:`LLMError` — base for everything in this layer.
- :class:`LLMHelperError` — umbrella for SDK-call failures (DEC-004).
  Subclasses cover the retry-taxonomy branches (auth / rate-limit /
  server / connection / response-format).
- :class:`LLMCacheTooLargeError` — pre-send token-count failure
  (DEC-009 / DEC-024) when the cached block exceeds SignalForge's
  8000-token cap. Direct ``LLMError`` subclass, NOT a helper-error
  subclass, because it fires
  before any SDK call is issued.
"""

from __future__ import annotations

from typing import ClassVar


def _format_value(v: object) -> str:
    """Quote a user-supplied value via ``repr()`` for safe inclusion in
    error messages (DEC-022).

    Embedding raw user input in error strings is a log-injection seam: a
    crafted value like ``"foo'\\nINFO: spoofed log line"`` (or an ANSI
    escape such as ``"\\x1b[31m"``) could pollute log viewers or stack
    traces. Routing every user-controlled value through ``repr()`` quotes
    the string, escapes control characters, and makes whitespace visible.
    """
    return repr(v)


class LLMError(Exception):
    """Base class for all LLM-layer errors.

    Subclasses set a class-level ``default_remediation`` string; instances
    may override it via the ``remediation=`` keyword argument. ``__str__``
    renders the message and the remediation on separate lines so log output
    and CLI output both read cleanly.
    """

    default_remediation: ClassVar[str] = "(no remediation set — this is the base class)"

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = (
            remediation if remediation is not None else type(self).default_remediation
        )

    def __str__(self) -> str:
        return f"{self.message}\n  ↳ Remediation: {self.remediation}"


class LLMHelperError(LLMError):
    """Umbrella for SDK-call failures (DEC-004).

    The ``cause`` kwarg preserves the underlying exception (e.g. an
    ``anthropic.APIStatusError``) so callers can chain via ``raise ... from
    cause`` without losing the SDK detail. Subclasses cover the retry-taxonomy
    branches: ``except LLMHelperError`` catches every SDK-call failure with
    one clause.
    """

    default_remediation: ClassVar[str] = (
        "Inspect the specific subclass's remediation; the SDK call failed "
        "after the retry policy was exhausted (or short-circuited)."
    )

    def __init__(
        self,
        message: str,
        *,
        cause: BaseException | None = None,
        remediation: str | None = None,
    ) -> None:
        self.cause = cause
        super().__init__(message, remediation=remediation)


class LLMAuthError(LLMHelperError):
    """The Anthropic API rejected the request with 401 / 403.

    DEC-004 short-circuits retries on auth failures: a missing or invalid
    API key won't fix itself by retrying. The remediation points the
    operator at the documented environment variable.
    """

    default_remediation: ClassVar[str] = (
        "Set the ANTHROPIC_API_KEY environment variable to a valid Anthropic "
        "API key. The retry policy does NOT retry 401/403 — fix the credential "
        "and re-run."
    )


class LLMRateLimitError(LLMHelperError):
    """The Anthropic API returned 429 and the retry budget was exhausted
    (DEC-004 caps 429s at three attempts by default).

    The ``attempts`` field carries the number of attempts made so the
    drafter / CLI can report "we tried N times" without sniffing message
    text.
    """

    default_remediation: ClassVar[str] = (
        "Reduce concurrency, raise `max_retries_429` in DraftConfig, or wait "
        "for the rate-limit window to clear before re-running."
    )

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        cause: BaseException | None = None,
        remediation: str | None = None,
    ) -> None:
        self.attempts = attempts
        super().__init__(message, cause=cause, remediation=remediation)


class LLMServerError(LLMHelperError):
    """The Anthropic API returned 5xx and the retry budget was exhausted.

    DEC-004 caps 5xx retries at one attempt by default; servers usually
    recover quickly or stay broken — long retry chains hide the latter.
    """

    default_remediation: ClassVar[str] = (
        "Retry the call manually after a few minutes, or check Anthropic's "
        "status page if the failure persists."
    )


class LLMConnectionError(LLMHelperError):
    """A network-level connection error reached the retry budget without
    succeeding (DEC-004 caps connection retries at one).

    Distinct from :class:`LLMServerError` because the server never
    responded — a flaky connection is operationally different from a 5xx.
    """

    default_remediation: ClassVar[str] = (
        "Check network connectivity to api.anthropic.com; verify there is no "
        "proxy or firewall blocking outbound HTTPS to that host."
    )


class LLMResponseFormatError(LLMHelperError):
    """The SDK returned a response with an unexpected shape.

    Raised when ``messages.create`` succeeds (HTTP 200) but the response
    object is missing an attribute the seam needs (no ``content`` blocks,
    no ``usage`` field, etc.). Distinct from a JSON-parse failure on the
    LLM's text content — that's the drafter's concern.
    """

    default_remediation: ClassVar[str] = (
        "Inspect the underlying SDK response (preserved on `cause`). The SDK "
        "version may have changed shape; pin or upgrade `anthropic` and re-run."
    )


class EstimateUnknownModelError(LLMError):
    """The operator requested a cost estimate for a model that does not
    appear in :data:`signalforge.llm.pricing.PRICES`.

    Raised by :func:`signalforge.llm.pricing.lookup` (and downstream
    callers in the v0.1 ``--estimate`` engine). The mapped CLI exit-code
    tier is 2 (input-validation — the operator picked a model we cannot
    price), not tier 3 (external-dep), because the failure mode is "the
    config / flag value names a SKU the table doesn't know," not "the
    Anthropic API is unreachable." Apply the same tier reasoning to any
    future "looked-up identifier not in a static table" error.
    """

    default_remediation: ClassVar[str] = (
        "Add the model to signalforge.llm.pricing.PRICES or use a "
        "supported model: claude-sonnet-4-6, claude-opus-4-7, "
        "claude-haiku-4-5."
    )

    def __init__(
        self,
        model: str,
        *,
        remediation: str | None = None,
    ) -> None:
        self.model = model
        # ``_format_value`` quotes via ``repr()`` so an adversarial
        # ``model`` value (control chars, ANSI escapes, embedded newlines)
        # cannot pollute log viewers / stack traces — see DEC-022 of the
        # warehouse adapter rules; same pattern applies layer-wide.
        message = f"unknown model for cost estimate: {_format_value(model)}"
        super().__init__(message, remediation=remediation)


class UnknownProviderError(LLMError):
    """The caller requested an LLM provider that is not in the registry.

    Raised by :func:`signalforge.llm.providers.provider_for` (and the
    registry-validated ``provider`` config fields that land in US-004) when a
    name does not match any registered provider. The message lists every
    currently-registered provider name so the operator can spot a typo or a
    not-yet-registered provider at a glance.

    Direct ``LLMError`` subclass (NOT an :class:`LLMHelperError`) because the
    failure is a registry lookup, not an SDK call — it fires before any vendor
    client is constructed.
    """

    default_remediation: ClassVar[str] = (
        "Set the provider to one of the registered names, or register the "
        "provider before selecting it. The default provider is 'anthropic'."
    )

    def __init__(
        self,
        name: str,
        *,
        available: tuple[str, ...] = (),
        remediation: str | None = None,
    ) -> None:
        self.name = name
        self.available = available
        # ``_format_value`` quotes via ``repr()`` so an adversarial provider
        # name (control chars, ANSI escapes, embedded newlines) cannot pollute
        # log viewers / stack traces — same pattern as EstimateUnknownModelError.
        available_str = ", ".join(_format_value(p) for p in available) if available else "(none)"
        message = (
            f"unknown LLM provider: {_format_value(name)}; available providers: {available_str}"
        )
        super().__init__(message, remediation=remediation)


class LLMCacheTooLargeError(LLMError):
    """Pre-send token-count check (DEC-024) reported the cached block is
    above the SignalForge cap (DEC-009 — 8000 input tokens).

    The cap is a SignalForge-imposed safeguard against accidental prompt
    bloat: the manifest summary should cover the model under draft + its
    direct neighbours only (DEC-009). A summary that exceeds the cap is a
    signal that the prompt builder has drifted (e.g. embedded the full
    project manifest) — failing loud surfaces the regression at the call
    site.
    """

    default_remediation: ClassVar[str] = (
        "Reduce the size of the cached block (manifest summary + few-shots) "
        "below the 8000-token cap. Per DEC-009 the summary should cover only "
        "the model under draft + its direct refs/depends_on neighbours."
    )

    def __init__(
        self,
        cached_block_tokens: int,
        cap: int = 8000,
        *,
        remediation: str | None = None,
    ) -> None:
        self.cached_block_tokens = cached_block_tokens
        self.cap = cap
        message = (
            f"Cached block is {cached_block_tokens} tokens; SignalForge caps "
            f"the cached block at {cap} tokens (DEC-009)."
        )
        super().__init__(message, remediation=remediation)


# Sorted alphabetically (verified by tests/llm/test_errors.py).
__all__ = [
    "EstimateUnknownModelError",
    "LLMAuthError",
    "LLMCacheTooLargeError",
    "LLMConnectionError",
    "LLMError",
    "LLMHelperError",
    "LLMRateLimitError",
    "LLMResponseFormatError",
    "LLMServerError",
    "UnknownProviderError",
]
