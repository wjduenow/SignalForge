"""Generic provider-neutral LLM seam — :func:`call_llm` (US-003).

Implements the single entry point through which every LLM
``messages.create``-equivalent call in SignalForge flows. The orchestrator
owns the vendor-agnostic machinery; a provider strategy (resolved from the
registry in :mod:`signalforge.llm.providers`) supplies the vendor-specific
bits (build request kwargs, extract text/usage, classify exceptions, and the
capability flags ``supports_prompt_caching`` / ``supports_token_count``).
The orchestrator owns:

- **Pre-send token-count gate (DEC-024, gated on ``supports_token_count``).**
  When the provider supports token counting, calls
  ``client.messages.count_tokens`` against the cached block before the
  ``messages.create`` so a sub-minimum or oversize cached block fails loud
  rather than silently no-opping the cache marker (which would cost the
  user the input-token premium with none of the cache discount). When the
  provider does not support token counting, the gate is skipped entirely
  (no count call, no :class:`LLMCacheTooLargeError` pre-send — DEC-008).
- **Retry policy (DEC-004).** Exponential backoff with bounded jitter:
  ``delay = 2**i * _rand_uniform(0.75, 1.25)``. 429s retry up to
  ``max_retries_429`` times, 5xx up to ``max_retries_5xx``, connection
  errors up to ``max_retries_conn``. 4xx (other than 401/403) and
  401/403 short-circuit — no retry. Each attempt emits a WARNING. The loop
  dispatches on :class:`ExceptionCategory` from ``strategy.classify_exception``
  rather than catching vendor SDK exception classes directly (DEC-001).
- **Cache-anomaly logging (gated on ``supports_prompt_caching``).** If the
  response's ``cache_creation_input_tokens`` is 0 *and*
  ``cache_read_input_tokens`` is 0 despite the request carrying a
  ``cache_control`` marker, surface a WARNING — this can happen on
  load-balancer rerouting or partial cache miss even when the pre-send size
  check passed.
- **Module-level aliases ``_sleep`` and ``_rand_uniform`` (DEC-004).** Tests
  reassign these to deterministic stand-ins so retry-branch coverage runs
  instantly without timing flake.

Observability discipline:

- Lazy-format JSON for every log call (``.claude/rules/safety-layer.md``
  DEC-022). Never f-string user-controlled values; the grep gate in the
  test suite asserts zero ``_LOGGER\\.\\w+\\(f"`` hits in this file.
- The orchestrator never imports a vendor SDK directly — the provider
  strategy confines that to its ``_<vendor>_client.py`` shim.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

from signalforge.llm.errors import (
    LLMAuthError,
    LLMCacheTooLargeError,
    LLMConnectionError,
    LLMHelperError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMServerError,
)
from signalforge.llm.models import LLMResult
from signalforge.llm.providers import ExceptionCategory, provider_for

# Module-level aliases — tests reassign for deterministic backoff (DEC-004).
_sleep = time.sleep
_rand_uniform = random.uniform

_LOGGER = logging.getLogger(__name__)


@runtime_checkable
class _LLMMessagesProtocol(Protocol):
    """The ``.messages`` surface the orchestrator consumes, vendor-neutral.

    Duck-typed at exactly ``count_tokens`` + ``create``. A vendor's real SDK
    client (or a test fake) satisfies it structurally; this keeps the
    orchestrator free of any vendor-SDK import or type-checker suppression —
    those stay confined to the per-vendor ``_<vendor>_client.py`` shim
    (DEC-012).
    """

    def count_tokens(self, **kwargs: Any) -> Any: ...

    def create(self, **kwargs: Any) -> Any: ...


@runtime_checkable
class _LLMClientProtocol(Protocol):
    """Vendor-neutral client surface: a ``.messages`` namespace.

    ``strategy.make_client()`` returns ``object``; the orchestrator narrows it
    to this protocol so the call sites type-check without leaking a vendor SDK
    type into ``signalforge.llm.client``.
    """

    @property
    def messages(self) -> _LLMMessagesProtocol: ...


# Anthropic prompt-cache minimum block sizes per model family (DEC-009 /
# DEC-024). Below these, a ``cache_control`` marker is silently a no-op:
# the request still succeeds but the cache entry is never created, so the
# user pays the input-token premium with none of the discount.
_MIN_CACHEABLE_TOKENS: dict[str, int] = {
    "claude-haiku": 2048,
    "claude-sonnet": 1024,
    "claude-opus": 1024,
}

# SignalForge-imposed cap on cached-block size (DEC-009). The manifest
# summary should cover the model under draft + its direct refs/depends_on
# neighbours only; a summary above the cap signals the prompt builder has
# drifted (e.g. embedded the full project manifest) and we'd rather fail
# loud than silently bloat every request.
_CACHED_BLOCK_CAP_TOKENS: Final[int] = 8000


def _min_cacheable_tokens(model: str) -> int:
    """Pick the minimum cacheable block size for ``model`` (DEC-009).

    Longest-prefix-match wins. Unknown model strings default to 1024 —
    chosen so a future Anthropic model name we haven't seen yet falls into
    the more permissive Sonnet/Opus bucket rather than the stricter Haiku
    one. The pre-send check then fails loud only if the block is below
    even the permissive minimum.
    """
    matches = [
        (prefix, minimum)
        for prefix, minimum in _MIN_CACHEABLE_TOKENS.items()
        if model.startswith(prefix)
    ]
    if not matches:
        return 1024
    matches.sort(key=lambda pair: len(pair[0]), reverse=True)
    return matches[0][1]


def _is_5xx(exc: BaseException) -> bool:
    """Return ``True`` if ``exc`` is an Anthropic ``APIStatusError`` with
    a 5xx status code (excluding 429, which is handled by the rate-limit
    branch above this in the dispatch order).
    """
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and 500 <= status < 600


def _is_4xx_non_auth(exc: BaseException) -> bool:
    """Return ``True`` if ``exc`` is a 4xx ``APIStatusError`` other than
    401 / 403 / 429 — those branches are handled separately.
    """
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status not in (401, 403, 429)


def _extract_text_blocks(response: Any) -> tuple[str, ...]:
    """Pull text from each ``content`` block of an Anthropic response.

    Anthropic responses carry a list of typed content blocks; we extract
    only the ones with ``type == "text"``. Non-text blocks (tool uses,
    images) are ignored — they're not produced by the schema-drafting
    prompt and would surface as an :class:`LLMResponseFormatError` if the
    drafter ever sees one.
    """
    content = getattr(response, "content", None)
    if content is None:
        raise LLMResponseFormatError(
            "Response is missing the `content` attribute.",
        )
    blocks: list[str] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                raise LLMResponseFormatError(
                    "Text block is missing a string `text` attribute.",
                )
            blocks.append(text)
    return tuple(blocks)


def _extract_usage_field(usage: Any, name: str, *, default: int | None = None) -> int:
    """Pull an integer usage field off ``response.usage`` with a default."""
    value = getattr(usage, name, default)
    if value is None:
        if default is None:
            raise LLMResponseFormatError(
                f"Response usage is missing required field {name!r}.",
            )
        return default
    if not isinstance(value, int):
        raise LLMResponseFormatError(
            f"Response usage field {name!r} is not an int.",
        )
    return value


def _backoff_warn(
    *,
    total_attempts: int,
    class_attempt_key: str,
    class_attempt_value: int,
    error_class: str,
    model: str,
) -> float:
    """Compute the backoff delay, emit the per-retry WARNING, return the delay.

    Mirrors the historical inline retry-branch logging byte-for-byte: a
    ``"retry attempt: <json>"`` WARNING carrying ``attempt``, the per-class
    counter (under its class-specific key), ``delay``, ``error_class``, and
    ``model``, in that key order.
    """
    delay = (2**total_attempts) * _rand_uniform(0.75, 1.25)
    _LOGGER.warning(
        "retry attempt: %s",
        json.dumps(
            {
                "attempt": total_attempts,
                class_attempt_key: class_attempt_value,
                "delay": delay,
                "error_class": error_class,
                "model": model,
            }
        ),
    )
    return delay


def call_llm(
    *,
    system: str,
    cached_block: str,
    dynamic_block: str,
    model: str,
    max_tokens: int,
    cache_ttl: Literal["5m", "1h"] = "5m",
    prompt_version: str,
    max_retries_429: int = 3,
    max_retries_5xx: int = 1,
    max_retries_conn: int = 1,
    provider: str = "anthropic",
    client: object | None = None,
) -> LLMResult:
    """Issue one provider-neutral LLM call with retry + cache gating.

    The orchestrator resolves the provider strategy from the registry
    (:func:`signalforge.llm.providers.provider_for`) and delegates every
    vendor-specific decision to it; it owns the retry loop, backoff math,
    logging, and :class:`LLMResult` assembly (DEC-001).

    Two user-message blocks are sent (for a caching provider like Anthropic):

    1. ``cached_block`` carrying ``{"cache_control": {"type": "ephemeral",
       "ttl": cache_ttl}}`` — the manifest-summary + few-shot block the
       prompt-cache should reuse across sibling-model drafts.
    2. ``dynamic_block`` (no cache marker) — the per-call payload (model
       SQL, sampled rows or aggregates).

    The pre-send token-count gate is issued against ``system + block 1``
    only (that's the surface the cache marker covers) and runs **only** when
    ``strategy.supports_token_count`` is ``True``. When ``False`` the gate is
    skipped entirely — no count call, no :class:`LLMCacheTooLargeError`
    pre-send (DEC-008).

    Cache-marker / beta-header attachment and the dual-zero cache-anomaly
    WARNING are gated on ``strategy.supports_prompt_caching`` (DEC-008). For
    ``provider="anthropic"`` (both flags ``True``) the control flow + emitted
    bytes are unchanged from the historical inline seam.

    The ``client`` argument is the dependency-injection seam used by tests
    (the hand-rolled :class:`FakeAnthropicClient` in ``tests/llm/_fake.py``
    satisfies the Anthropic client surface); production callers leave it
    ``None`` and let ``strategy.make_client()`` lazy-construct the real SDK
    client (DEC-006).
    """
    strategy = provider_for(provider)
    if client is None:
        # The strategy owns client construction so test environments that
        # inject a fake never pay the SDK import cost (DEC-006).
        client = strategy.make_client()
    # Narrow to the neutral client protocol (``.messages.{count_tokens,create}``)
    # so the call sites type-check without leaking a vendor SDK type here.
    llm_client = cast(_LLMClientProtocol, client)

    supports_caching = strategy.supports_prompt_caching

    # Resolve the cache-marker decision + (optionally) run the pre-send
    # count gate. ``cache_marker_active`` is the orchestrator's resolved
    # decision threaded into ``build_create_kwargs``; ``cached_block_tokens``
    # / ``min_required`` are only meaningful when the count gate ran.
    cache_marker_active = supports_caching
    cached_block_tokens: int | None = None
    min_required: int | None = None

    if strategy.supports_token_count:
        # Pre-send token-count gate (DEC-024). Issue against system + the
        # cached block only — that's the surface the cache marker covers.
        #
        # count_tokens errors are MAPPED to typed LLMError subclasses (so
        # raw vendor exceptions don't leak past the seam), but they are
        # NOT retried: count_tokens is a cheap probe; consuming the
        # messages.create retry budget on a probe failure would let one
        # transient blip exhaust the retry budget before the real call.
        count_kwargs = strategy.build_count_tokens_kwargs(
            system=system,
            cached_block=cached_block,
            model=model,
        )
        try:
            count_response = llm_client.messages.count_tokens(**count_kwargs)
        except Exception as exc:
            category = strategy.classify_exception(exc)
            if category is ExceptionCategory.AUTH:
                raise LLMAuthError(
                    "LLM count_tokens rejected the request with an auth error.",
                    cause=exc,
                ) from exc
            if category is ExceptionCategory.RATE_LIMIT:
                raise LLMRateLimitError(
                    "LLM count_tokens hit a rate limit (no retry on the probe call).",
                    attempts=0,
                    cause=exc,
                ) from exc
            if category is ExceptionCategory.CONNECTION:
                raise LLMConnectionError(
                    "LLM count_tokens connection failed (no retry on the probe call).",
                    cause=exc,
                ) from exc
            if category is ExceptionCategory.SERVER_ERROR:
                raise LLMServerError(
                    "LLM count_tokens 5xx (no retry on the probe call).",
                    cause=exc,
                ) from exc
            raise LLMHelperError(
                "LLM count_tokens returned a non-5xx error status.",
                cause=exc,
            ) from exc

        cached_block_tokens = getattr(count_response, "input_tokens", None)
        if not isinstance(cached_block_tokens, int):
            raise LLMResponseFormatError(
                "count_tokens response is missing the `input_tokens` field.",
            )
        min_required = _min_cacheable_tokens(model)
        if supports_caching and cached_block_tokens < min_required:
            # The vendor silently no-ops the cache marker below the minimum,
            # so leaving it set wastes the count_tokens call AND triggers our
            # own dual-zero cache-anomaly WARNING further down. Drop the
            # marker and log once: the call still succeeds, the caller just
            # doesn't get caching. Callers whose cached block is reliably
            # below the minimum (e.g. the grade layer's compact rubric) get a
            # clean run instead of a hard error. The ``cache_marker_active``
            # flag also gates the downstream dual-zero WARNING, which would
            # otherwise fire as a false alarm here — both ``cache_creation``
            # and ``cache_read`` are guaranteed to be 0 when no marker sent.
            _LOGGER.info(
                "cache marker dropped (block below cacheable minimum): %s",
                json.dumps(
                    {
                        "model": model,
                        "cached_block_size_tokens": cached_block_tokens,
                        "min_required": min_required,
                    }
                ),
            )
            cache_marker_active = False
        if cached_block_tokens > _CACHED_BLOCK_CAP_TOKENS:
            raise LLMCacheTooLargeError(
                cached_block_tokens=cached_block_tokens,
                cap=_CACHED_BLOCK_CAP_TOKENS,
            )

    create_kwargs = strategy.build_create_kwargs(
        system=system,
        cached_block=cached_block,
        dynamic_block=dynamic_block,
        model=model,
        max_tokens=max_tokens,
        cache_ttl=cache_ttl,
        cache_marker_active=cache_marker_active,
    )

    # Retry loop — clauditor pattern with per-class budgets, dispatching on
    # the neutral ExceptionCategory (DEC-001) rather than vendor classes.
    #
    # Each failure class (429 / 5xx / connection) carries its own
    # counter so one class can't consume another's budget. A single
    # `total_attempts` drives the backoff math + WARNING log so delays
    # remain monotonic across mixed failure types — but per-class
    # exhaustion is what raises the typed error.
    attempt_429 = 0
    attempt_5xx = 0
    attempt_conn = 0
    total_attempts = 0
    while True:
        try:
            response = llm_client.messages.create(**create_kwargs)
            break
        except Exception as exc:
            category = strategy.classify_exception(exc)
            if category is ExceptionCategory.AUTH:
                # 401 / 403 — retrying won't fix a missing/invalid API key.
                raise LLMAuthError(
                    "LLM API rejected the request with an auth error.",
                    cause=exc,
                ) from exc
            if category is ExceptionCategory.RATE_LIMIT:
                if attempt_429 >= max_retries_429:
                    raise LLMRateLimitError(
                        f"Rate-limit retry budget exhausted after {attempt_429} retries.",
                        attempts=attempt_429,
                        cause=exc,
                    ) from exc
                delay = _backoff_warn(
                    total_attempts=total_attempts,
                    class_attempt_key="class_attempt_429",
                    class_attempt_value=attempt_429,
                    error_class=exc.__class__.__name__,
                    model=model,
                )
                _sleep(delay)
                attempt_429 += 1
                total_attempts += 1
                continue
            if category is ExceptionCategory.CONNECTION:
                if attempt_conn >= max_retries_conn:
                    raise LLMConnectionError(
                        f"Connection retry budget exhausted after {attempt_conn} retries.",
                        cause=exc,
                    ) from exc
                delay = _backoff_warn(
                    total_attempts=total_attempts,
                    class_attempt_key="class_attempt_conn",
                    class_attempt_value=attempt_conn,
                    error_class=exc.__class__.__name__,
                    model=model,
                )
                _sleep(delay)
                attempt_conn += 1
                total_attempts += 1
                continue
            if category is ExceptionCategory.SERVER_ERROR:
                if attempt_5xx >= max_retries_5xx:
                    raise LLMServerError(
                        f"Server-error retry budget exhausted after {attempt_5xx} retries.",
                        cause=exc,
                    ) from exc
                delay = _backoff_warn(
                    total_attempts=total_attempts,
                    class_attempt_key="class_attempt_5xx",
                    class_attempt_value=attempt_5xx,
                    error_class=exc.__class__.__name__,
                    model=model,
                )
                _sleep(delay)
                attempt_5xx += 1
                total_attempts += 1
                continue
            # NO_RETRY — 4xx (non-auth) or any other status the strategy
            # couldn't classify into a retryable bucket. Retrying won't fix
            # a malformed request; surface as a helper error.
            raise LLMHelperError(
                "LLM API rejected the request with a non-retryable error.",
                cause=exc,
            ) from exc

    # Build the typed result from the response via the strategy.
    text_blocks = strategy.extract_text_blocks(response)
    usage = strategy.extract_usage(response)
    cache_creation = usage.cache_creation_input_tokens if supports_caching else 0
    cache_read = usage.cache_read_input_tokens if supports_caching else 0

    # Cache-anomaly WARNING (gated on supports_prompt_caching): the cached
    # block had a marker AND was above the model minimum (the pre-send check
    # would have dropped the marker otherwise), yet the response reports
    # neither a cache write nor a cache read. This can happen on load-
    # balancer rerouting or partial cache miss; surface it so the operator
    # knows the cache discount didn't land.
    # NB: ``cache_creation == 0`` alone is the *normal* cache-hit case
    # (creation already happened on a prior call); we only warn when both
    # creation AND read are zero — the genuine no-op signal.
    # NB: ``cache_marker_active`` gates the warning so we don't false-alarm
    # on calls where the pre-send check intentionally dropped the marker
    # (sub-minimum cached block).
    if supports_caching and cache_marker_active and cache_creation == 0 and cache_read == 0:
        _LOGGER.warning(
            "cache marker no-op: %s",
            json.dumps(
                {
                    "model": model,
                    "cached_block_size_tokens": cached_block_tokens,
                    "min_required": min_required,
                }
            ),
        )

    return LLMResult(
        text_blocks=text_blocks,
        response_text="".join(text_blocks),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        model=model,
        prompt_version=prompt_version,
        raw_message=response,
    )


__all__ = ("call_llm",)
