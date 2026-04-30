"""Centralised Anthropic SDK seam — :func:`call_anthropic` (US-006).

Implements the single entry point through which every Anthropic
``messages.create`` call in SignalForge flows. Owns:

- **Pre-send token-count check (DEC-024).** Calls
  :meth:`client.messages.count_tokens` against the cached block before the
  ``messages.create`` so a sub-minimum or oversize cached block fails loud
  rather than silently no-opping the cache marker (which would cost the
  user the input-token premium with none of the cache discount).
- **Retry policy (DEC-004).** Exponential backoff with bounded jitter:
  ``delay = 2**i * _rand_uniform(0.75, 1.25)``. 429s retry up to
  ``max_retries_429`` times, 5xx up to ``max_retries_5xx``, connection
  errors up to ``max_retries_conn``. 4xx (other than 401/403) and
  401/403 short-circuit — no retry. Each attempt emits a WARNING.
- **Cache-anomaly logging.** If the response's ``cache_creation_input_tokens``
  is 0 despite the request carrying a ``cache_control`` marker, surface a
  WARNING — this can happen on load-balancer rerouting or partial cache miss
  even when the pre-send size check passed.
- **Module-level aliases ``_sleep`` and ``_rand_uniform`` (DEC-004).** Tests
  reassign these to deterministic stand-ins so retry-branch coverage runs
  instantly without timing flake.

Observability discipline:

- Lazy-format JSON for every log call (``.claude/rules/safety-layer.md``
  DEC-022). Never f-string user-controlled values; the grep gate in the
  test suite asserts zero ``_LOGGER\\.\\w+\\(f"`` hits in this file.
- The shim itself does not import the ``anthropic`` SDK — exception
  classes are caught by name via :func:`_load_anthropic_exceptions`,
  which lazy-imports only when the retry loop is entered.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Final, Literal

from signalforge.llm._client import (
    _AnthropicClientProtocol,
    _load_anthropic_exception_classes,
)
from signalforge.llm.errors import (
    LLMAuthError,
    LLMCacheTooLargeError,
    LLMCacheTooSmallError,
    LLMConnectionError,
    LLMHelperError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMServerError,
)
from signalforge.llm.models import LLMResult

# Module-level aliases — tests reassign for deterministic backoff (DEC-004).
_sleep = time.sleep
_rand_uniform = random.uniform

_LOGGER = logging.getLogger(__name__)

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


def call_anthropic(
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
    client: _AnthropicClientProtocol | None = None,
) -> LLMResult:
    """Issue one Anthropic ``messages.create`` with retry + audit guard.

    Two user-message blocks are sent:

    1. ``cached_block`` carrying ``{"cache_control": {"type": "ephemeral",
       "ttl": cache_ttl}}`` — the manifest-summary + few-shot block that
       Anthropic's prompt-cache should reuse across sibling-model drafts.
    2. ``dynamic_block`` (no cache marker) — the per-call payload (model
       SQL, sampled rows or aggregates).

    The pre-send token-count check is issued against ``system + block 1``
    only: that's the surface the cache marker actually covers.

    The ``client`` argument is the dependency-injection seam used by tests
    (the hand-rolled :class:`FakeAnthropicClient` in ``tests/llm/_fake.py``
    satisfies :class:`_AnthropicClientProtocol`); production callers leave
    it ``None`` and let :func:`_make_anthropic_client` lazy-construct the
    real SDK client. ``client=None`` here is documented for completeness;
    the live drafter in US-013 will always thread an explicit client
    through so the audit layer can assert the same client object was used
    for the whole pipeline.
    """
    if client is None:
        # Lazy-construct via the shim so test environments that inject a
        # fake never pay the SDK import cost. Production callers from
        # US-013 will pass a client explicitly.
        from signalforge.llm._client import _make_anthropic_client

        client = _make_anthropic_client()

    # Lazy-import the SDK exception classes via the shim (every Anthropic
    # SDK type-checker suppression lives in ``_client.py`` per DEC-012).
    # Tests that don't reach the retry branch don't pay the import cost.
    exc_classes = _load_anthropic_exception_classes()
    rate_limit_cls = exc_classes.rate_limit
    api_status_cls = exc_classes.api_status
    auth_cls = exc_classes.auth
    connection_cls = exc_classes.connection

    # Build messages array. Block 1 carries the cache marker; block 2
    # does not (DEC-009: only the manifest-summary block is cached).
    block_1: dict[str, Any] = {
        "type": "text",
        "text": cached_block,
        "cache_control": {"type": "ephemeral", "ttl": cache_ttl},
    }
    block_2: dict[str, Any] = {"type": "text", "text": dynamic_block}
    messages = [{"role": "user", "content": [block_1, block_2]}]

    # Beta header is only required for the 1h TTL extension; sending it
    # for 5m is at best ignored, at worst a deprecation flag.
    extra_headers: dict[str, str] = (
        {"anthropic-beta": "extended-cache-ttl-2025-04-11"} if cache_ttl == "1h" else {}
    )

    # Pre-send token-count check (DEC-024). Issue against system + the
    # cached block only — that's the surface the cache marker covers.
    count_response = client.messages.count_tokens(
        model=model,
        system=system,
        messages=[{"role": "user", "content": [block_1]}],
    )
    cached_block_tokens = getattr(count_response, "input_tokens", None)
    if not isinstance(cached_block_tokens, int):
        raise LLMResponseFormatError(
            "count_tokens response is missing the `input_tokens` field.",
        )
    min_required = _min_cacheable_tokens(model)
    if cached_block_tokens < min_required:
        raise LLMCacheTooSmallError(
            cached_block_tokens=cached_block_tokens,
            min_tokens=min_required,
            model=model,
        )
    if cached_block_tokens > _CACHED_BLOCK_CAP_TOKENS:
        raise LLMCacheTooLargeError(
            cached_block_tokens=cached_block_tokens,
            cap=_CACHED_BLOCK_CAP_TOKENS,
        )

    # Retry loop — clauditor pattern.
    attempt = 0
    while True:
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                extra_headers=extra_headers,
            )
            break
        except auth_cls as exc:
            # 401 / 403 — retrying won't fix a missing/invalid API key.
            raise LLMAuthError(
                "Anthropic API rejected the request with an auth error.",
                cause=exc,
            ) from exc
        except rate_limit_cls as exc:
            if attempt >= max_retries_429:
                raise LLMRateLimitError(
                    f"Rate-limit retry budget exhausted after {attempt} retries.",
                    attempts=attempt,
                    cause=exc,
                ) from exc
            delay = (2**attempt) * _rand_uniform(0.75, 1.25)
            _LOGGER.warning(
                "retry attempt: %s",
                json.dumps(
                    {
                        "attempt": attempt,
                        "delay": delay,
                        "error_class": exc.__class__.__name__,
                        "model": model,
                    }
                ),
            )
            _sleep(delay)
            attempt += 1
            continue
        except connection_cls as exc:
            if attempt >= max_retries_conn:
                raise LLMConnectionError(
                    f"Connection retry budget exhausted after {attempt} retries.",
                    cause=exc,
                ) from exc
            delay = (2**attempt) * _rand_uniform(0.75, 1.25)
            _LOGGER.warning(
                "retry attempt: %s",
                json.dumps(
                    {
                        "attempt": attempt,
                        "delay": delay,
                        "error_class": exc.__class__.__name__,
                        "model": model,
                    }
                ),
            )
            _sleep(delay)
            attempt += 1
            continue
        except api_status_cls as exc:
            # 5xx: retry. 4xx (non-auth, non-429): no retry.
            if _is_5xx(exc):
                if attempt >= max_retries_5xx:
                    raise LLMServerError(
                        f"Server-error retry budget exhausted after {attempt} retries.",
                        cause=exc,
                    ) from exc
                delay = (2**attempt) * _rand_uniform(0.75, 1.25)
                _LOGGER.warning(
                    "retry attempt: %s",
                    json.dumps(
                        {
                            "attempt": attempt,
                            "delay": delay,
                            "error_class": exc.__class__.__name__,
                            "model": model,
                        }
                    ),
                )
                _sleep(delay)
                attempt += 1
                continue
            if _is_4xx_non_auth(exc):
                # 400 / 404 / 422 etc. — request is malformed in some way
                # the SDK didn't reject locally. Retrying won't fix it.
                raise LLMHelperError(
                    "Anthropic API rejected the request with a 4xx error.",
                    cause=exc,
                ) from exc
            # Status code we don't recognise — surface as helper error
            # rather than silently retry-loop.
            raise LLMHelperError(
                "Anthropic API returned an unexpected status.",
                cause=exc,
            ) from exc

    # Build the typed result from the SDK response.
    text_blocks = _extract_text_blocks(response)
    usage = getattr(response, "usage", None)
    if usage is None:
        raise LLMResponseFormatError(
            "Response is missing the `usage` attribute.",
        )
    input_tokens = _extract_usage_field(usage, "input_tokens")
    output_tokens = _extract_usage_field(usage, "output_tokens")
    cache_creation = _extract_usage_field(usage, "cache_creation_input_tokens", default=0)
    cache_read = _extract_usage_field(usage, "cache_read_input_tokens", default=0)

    # Cache-anomaly WARNING: the cached block had a marker AND was above
    # the model minimum (the pre-send check would have raised otherwise),
    # yet the response reports zero cache-creation tokens. This can happen
    # on load-balancer rerouting or partial cache miss; surface it so the
    # operator knows the cache discount didn't land.
    if cache_creation == 0:
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
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        model=model,
        prompt_version=prompt_version,
        raw_message=response,
    )


__all__ = ("call_anthropic",)
