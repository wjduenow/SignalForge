"""Unit tests for the provider-neutral LLM seam (US-001 / US-002 of issue #135).

Covers the foundation types: the :class:`ExceptionCategory` enum, the
:class:`UsageMetrics` value object, the :class:`LLMProvider` ABC, the
process-level registry (:func:`register_provider` / :func:`provider_for`), and
the :class:`AnthropicProvider` strategy registered by US-002.

Every test is capable of failing: no ``assert True``-shaped placeholders
(``testing-signal.md``).
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx
import pytest

from signalforge.llm.errors import UnknownProviderError
from signalforge.llm.providers import (
    AnthropicProvider,
    ExceptionCategory,
    LLMProvider,
    OpenAIProvider,
    UsageMetrics,
    provider_for,
    register_provider,
)

from ._fake import FakeMessage, FakeTextBlock, FakeUsage


class _DummyProvider(LLMProvider):
    """Minimal concrete provider for registry tests — no real SDK behaviour."""

    name = "dummy"
    supports_prompt_caching = False
    supports_token_count = False

    def make_client(self) -> object:
        return object()

    def build_create_kwargs(
        self,
        *,
        system: str,
        cached_block: str,
        dynamic_block: str,
        model: str,
        max_tokens: int,
        cache_ttl: str,
        cache_marker_active: bool,
    ) -> dict[str, Any]:
        return {"model": model, "max_tokens": max_tokens}

    def build_count_tokens_kwargs(
        self,
        *,
        system: str,
        cached_block: str,
        model: str,
    ) -> dict[str, Any]:
        return {"model": model}

    def extract_text_blocks(self, response: object) -> tuple[str, ...]:
        return ()

    def extract_usage(self, response: object) -> UsageMetrics:
        return UsageMetrics(input_tokens=0, output_tokens=0)

    def classify_exception(self, exc: BaseException) -> ExceptionCategory:
        return ExceptionCategory.NO_RETRY

    def estimate_input_tokens(
        self,
        model: str,
        text: str,
        *,
        system: str = "",
        client: object | None = None,
    ) -> int:
        # Trivial deterministic stub (#136 US-005) — the registry tests
        # don't exercise the count, only the ABC instantiation path.
        return 0


@pytest.fixture
def _isolate_registry() -> Any:
    """Snapshot + restore the process-level registry so registering a dummy
    provider in one test doesn't leak into another."""
    from signalforge.llm import providers as providers_module

    saved = dict(providers_module._REGISTRY)
    try:
        yield
    finally:
        providers_module._REGISTRY.clear()
        providers_module._REGISTRY.update(saved)


@pytest.mark.unit
@pytest.mark.llm
def test_exception_category_has_exactly_five_members() -> None:
    """The retry-taxonomy enum has exactly the five DEC-002 members — adding
    or dropping one is a contract change the orchestrator dispatch depends on."""
    members = {m.name for m in ExceptionCategory}
    assert members == {
        "AUTH",
        "RATE_LIMIT",
        "SERVER_ERROR",
        "CONNECTION",
        "NO_RETRY",
    }


@pytest.mark.unit
@pytest.mark.llm
def test_usage_metrics_defaults_cache_fields_to_zero() -> None:
    """``UsageMetrics`` defaults both cache-token fields to 0 (DEC-002), so a
    provider without prompt caching reports 0 rather than requiring the caller
    to pass them."""
    usage = UsageMetrics(input_tokens=120, output_tokens=45)
    assert usage.input_tokens == 120
    assert usage.output_tokens == 45
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 0


@pytest.mark.unit
@pytest.mark.llm
def test_usage_metrics_is_frozen() -> None:
    """The value object is immutable post-construction (mirrors ``LLMResult``)."""
    from pydantic import ValidationError

    usage = UsageMetrics(input_tokens=1, output_tokens=1)
    with pytest.raises(ValidationError):
        usage.input_tokens = 999  # type: ignore[misc]


@pytest.mark.unit
@pytest.mark.llm
def test_registry_hit_returns_registered_provider(_isolate_registry: None) -> None:
    """A registered provider is retrievable by name (DEC-003)."""
    provider = _DummyProvider()
    register_provider(provider)
    assert provider_for("dummy") is provider


@pytest.mark.unit
@pytest.mark.llm
def test_registry_miss_raises_unknown_provider_error(_isolate_registry: None) -> None:
    """An unregistered name raises ``UnknownProviderError`` listing the
    available registered names (DEC-003)."""
    register_provider(_DummyProvider())
    with pytest.raises(UnknownProviderError) as excinfo:
        provider_for("nope")
    err = excinfo.value
    assert err.name == "nope"
    # The available-keys list names the one registered provider.
    assert "dummy" in err.available
    rendered = str(err)
    assert "nope" in rendered
    assert "dummy" in rendered
    assert "↳ Remediation:" in rendered


@pytest.mark.unit
@pytest.mark.llm
def test_anthropic_provider_is_registered() -> None:
    """US-002 registers ``AnthropicProvider`` at import time, so
    ``provider_for("anthropic")`` returns it (DEC-003)."""
    provider = provider_for("anthropic")
    assert isinstance(provider, AnthropicProvider)


@pytest.mark.unit
@pytest.mark.llm
def test_register_provider_last_writer_wins(_isolate_registry: None) -> None:
    """Re-registering under the same name replaces the prior entry (DEC-003)."""
    first = _DummyProvider()
    second = _DummyProvider()
    register_provider(first)
    register_provider(second)
    assert provider_for("dummy") is second


@pytest.mark.unit
@pytest.mark.llm
def test_llm_provider_is_abstract() -> None:
    """``LLMProvider`` cannot be instantiated directly — it is an ABC with
    unimplemented abstract methods."""
    with pytest.raises(TypeError):
        LLMProvider()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# US-002 — AnthropicProvider strategy
# ---------------------------------------------------------------------------


_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.unit
@pytest.mark.llm
def test_anthropic_provider_capability_flags() -> None:
    """Anthropic supports both prompt caching and pre-send token counting, so
    both capability flags are ``True`` (DEC-008) — keeping the orchestrator's
    Anthropic control flow unchanged."""
    provider = AnthropicProvider()
    assert provider.name == "anthropic"
    assert provider.supports_prompt_caching is True
    assert provider.supports_token_count is True


@pytest.mark.unit
@pytest.mark.llm
def test_build_create_kwargs_attaches_cache_marker_only_when_active() -> None:
    """The ``cache_control`` ephemeral marker rides on block-1 ONLY when
    ``cache_marker_active`` (mirrors the inline ``call_anthropic`` shape)."""
    provider = AnthropicProvider()
    with_marker = provider.build_create_kwargs(
        system="sys",
        cached_block="CACHED",
        dynamic_block="DYN",
        model="claude-sonnet-4",
        max_tokens=1024,
        cache_ttl="5m",
        cache_marker_active=True,
    )
    blocks = with_marker["messages"][0]["content"]
    assert blocks[0]["text"] == "CACHED"
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert blocks[1] == {"type": "text", "text": "DYN"}
    assert "cache_control" not in blocks[1]
    assert with_marker["model"] == "claude-sonnet-4"
    assert with_marker["max_tokens"] == 1024
    assert with_marker["system"] == "sys"

    without_marker = provider.build_create_kwargs(
        system="sys",
        cached_block="CACHED",
        dynamic_block="DYN",
        model="claude-sonnet-4",
        max_tokens=1024,
        cache_ttl="5m",
        cache_marker_active=False,
    )
    assert "cache_control" not in without_marker["messages"][0]["content"][0]


@pytest.mark.unit
@pytest.mark.llm
def test_build_create_kwargs_beta_header_only_at_1h() -> None:
    """The ``extended-cache-ttl`` beta header is attached only when
    ``cache_ttl == "1h"`` (sending it for 5m is at best ignored)."""
    provider = AnthropicProvider()
    one_h = provider.build_create_kwargs(
        system="s",
        cached_block="c",
        dynamic_block="d",
        model="claude-opus-4",
        max_tokens=10,
        cache_ttl="1h",
        cache_marker_active=True,
    )
    assert one_h["extra_headers"] == {"anthropic-beta": "extended-cache-ttl-2025-04-11"}

    five_m = provider.build_create_kwargs(
        system="s",
        cached_block="c",
        dynamic_block="d",
        model="claude-opus-4",
        max_tokens=10,
        cache_ttl="5m",
        cache_marker_active=True,
    )
    assert five_m["extra_headers"] == {}


@pytest.mark.unit
@pytest.mark.llm
def test_build_count_tokens_kwargs_sends_cached_block_only_no_marker() -> None:
    """The pre-send count probe carries ``system`` + the cached block, with NO
    ``cache_control`` marker (matches the inline ``call_anthropic`` probe)."""
    provider = AnthropicProvider()
    kwargs = provider.build_count_tokens_kwargs(
        system="SYS",
        cached_block="CACHED",
        model="claude-sonnet-4",
    )
    assert kwargs["model"] == "claude-sonnet-4"
    assert kwargs["system"] == "SYS"
    block = kwargs["messages"][0]["content"][0]
    assert block == {"type": "text", "text": "CACHED"}
    assert "cache_control" not in block


@pytest.mark.unit
@pytest.mark.llm
def test_extract_text_blocks_parity() -> None:
    """``extract_text_blocks`` pulls every ``type == "text"`` block, matching
    the inline ``_extract_text_blocks`` byte-for-byte."""
    response = FakeMessage(
        content=[FakeTextBlock(text="hello"), FakeTextBlock(text="world")],
        usage=FakeUsage(input_tokens=1, output_tokens=1),
    )
    assert AnthropicProvider().extract_text_blocks(response) == ("hello", "world")


@pytest.mark.unit
@pytest.mark.llm
def test_extract_usage_returns_usage_metrics() -> None:
    """``extract_usage`` builds a :class:`UsageMetrics` from the response usage,
    defaulting the cache fields to 0 when present-as-zero."""
    response = FakeMessage(
        content=[FakeTextBlock(text="x")],
        usage=FakeUsage(
            input_tokens=120,
            output_tokens=45,
            cache_creation_input_tokens=10,
            cache_read_input_tokens=5,
        ),
    )
    usage = AnthropicProvider().extract_usage(response)
    assert isinstance(usage, UsageMetrics)
    assert usage.input_tokens == 120
    assert usage.output_tokens == 45
    assert usage.cache_creation_input_tokens == 10
    assert usage.cache_read_input_tokens == 5


@pytest.mark.unit
@pytest.mark.llm
def test_extract_usage_missing_usage_raises() -> None:
    """A response missing ``usage`` surfaces a typed format error (mirrors the
    inline ``call_anthropic`` guard)."""
    from signalforge.llm.errors import LLMResponseFormatError

    class _NoUsage:
        content: list[Any] = []

    with pytest.raises(LLMResponseFormatError):
        AnthropicProvider().extract_usage(_NoUsage())


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            anthropic.AuthenticationError(
                message="auth", response=httpx.Response(401, request=_REQ), body=None
            ),
            ExceptionCategory.AUTH,
        ),
        (
            anthropic.PermissionDeniedError(
                message="perm", response=httpx.Response(403, request=_REQ), body=None
            ),
            ExceptionCategory.AUTH,
        ),
        (
            anthropic.RateLimitError(
                message="rl", response=httpx.Response(429, request=_REQ), body=None
            ),
            ExceptionCategory.RATE_LIMIT,
        ),
        (
            anthropic.APIStatusError(
                message="5xx", response=httpx.Response(503, request=_REQ), body=None
            ),
            ExceptionCategory.SERVER_ERROR,
        ),
        (
            anthropic.APIStatusError(
                message="4xx", response=httpx.Response(422, request=_REQ), body=None
            ),
            ExceptionCategory.NO_RETRY,
        ),
        (
            # APIStatusError that is neither 5xx nor 4xx-non-auth (a 3xx) hits
            # the defensive fallthrough → NO_RETRY.
            anthropic.APIStatusError(
                message="3xx", response=httpx.Response(302, request=_REQ), body=None
            ),
            ExceptionCategory.NO_RETRY,
        ),
        (anthropic.APIConnectionError(request=_REQ), ExceptionCategory.CONNECTION),
        (ValueError("unrecognised"), ExceptionCategory.NO_RETRY),
    ],
)
def test_classify_exception_maps_each_category(
    exc: BaseException, expected: ExceptionCategory
) -> None:
    """Each Anthropic exception type maps to the correct neutral category,
    and anything unrecognised maps to NO_RETRY (DEC-002)."""
    assert AnthropicProvider().classify_exception(exc) is expected


@pytest.mark.unit
@pytest.mark.llm
def test_anthropic_provider_make_client_uses_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    """``make_client`` delegates to the shim's ``_make_anthropic_client`` so the
    DEC-012 SDK-construction confinement holds."""
    import signalforge.llm._anthropic_client as shim

    sentinel = object()
    monkeypatch.setattr(shim, "_make_anthropic_client", lambda: sentinel)
    assert AnthropicProvider().make_client() is sentinel


# ---------------------------------------------------------------------------
# US-002 of issue #136 — OpenAIProvider strategy
# ---------------------------------------------------------------------------


_OPENAI_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


class _FakeChoiceMessage:
    """Minimal stand-in for the OpenAI SDK's ``ChatCompletionMessage``."""

    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    """Minimal stand-in for an OpenAI ``ChatCompletion.Choice``."""

    def __init__(self, content: str | None) -> None:
        self.message = _FakeChoiceMessage(content)


class _FakeOpenAIUsage:
    """OpenAI Chat Completions usage shape (``prompt_tokens`` /
    ``completion_tokens``; no cache fields)."""

    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeOpenAIResponse:
    """Minimal stand-in for an OpenAI ``ChatCompletion`` response object."""

    def __init__(
        self,
        *,
        content: str | None = "ok",
        prompt_tokens: int = 120,
        completion_tokens: int = 45,
        choices: object | None = None,
    ) -> None:
        if choices is None:
            choices = [_FakeChoice(content)]
        self.choices = choices
        self.usage = _FakeOpenAIUsage(prompt_tokens, completion_tokens)


@pytest.mark.unit
@pytest.mark.llm
def test_openai_provider_is_registered() -> None:
    """US-002 of #136 registers ``OpenAIProvider`` at import time, so
    ``provider_for("openai")`` returns it (DEC-003 of #135)."""
    provider = provider_for("openai")
    assert isinstance(provider, OpenAIProvider)


@pytest.mark.unit
@pytest.mark.llm
def test_openai_provider_capability_flags() -> None:
    """OpenAI has no prompt-caching primitive and no server-side
    ``count_tokens`` API, so both capability flags are ``False``
    (DEC-008 of #135)."""
    provider = OpenAIProvider()
    assert provider.name == "openai"
    assert provider.supports_prompt_caching is False
    assert provider.supports_token_count is False


@pytest.mark.unit
@pytest.mark.llm
def test_openai_provider_build_create_kwargs_shape() -> None:
    """``build_create_kwargs`` returns the OpenAI-native Chat Completions
    kwargs shape: ``model``, ``max_tokens``, a system + user ``messages``
    pair (user content = cached + dynamic), and ``response_format``
    enforcing JSON server-side (DEC-006)."""
    kwargs = OpenAIProvider().build_create_kwargs(
        system="SYS",
        cached_block="CACHED",
        dynamic_block="DYN",
        model="gpt-4o",
        max_tokens=1024,
        cache_ttl="5m",
        cache_marker_active=False,
    )
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["max_tokens"] == 1024
    assert kwargs["response_format"] == {"type": "json_object"}
    messages = kwargs["messages"]
    assert len(messages) == 2
    assert messages[0] == {"role": "system", "content": "SYS"}
    assert messages[1] == {"role": "user", "content": "CACHEDDYN"}


@pytest.mark.unit
@pytest.mark.llm
def test_openai_provider_build_create_kwargs_never_emits_cache_marker_or_extra_headers() -> None:
    """OpenAI has no caching primitive — there must be no ``cache_control``
    marker anywhere in the kwargs and no ``extra_headers`` field, regardless
    of ``cache_marker_active`` / ``cache_ttl`` (the orchestrator already
    resolves the flag to ``False`` for a non-caching provider; this is
    belt-and-braces)."""
    for cache_marker_active in (True, False):
        for cache_ttl in ("5m", "1h"):
            kwargs = OpenAIProvider().build_create_kwargs(
                system="s",
                cached_block="c",
                dynamic_block="d",
                model="gpt-4o",
                max_tokens=10,
                cache_ttl=cache_ttl,
                cache_marker_active=cache_marker_active,
            )
            assert "extra_headers" not in kwargs
            # The kwargs dict carries no cache_control marker at any depth.
            assert "cache_control" not in repr(kwargs)


@pytest.mark.unit
@pytest.mark.llm
def test_openai_provider_build_count_tokens_kwargs_raises() -> None:
    """``build_count_tokens_kwargs`` raises ``NotImplementedError`` because
    ``supports_token_count`` is ``False`` (DEC-011 of #136 — mirrors
    ``FakeNoCacheProvider`` precedent)."""
    with pytest.raises(NotImplementedError):
        OpenAIProvider().build_count_tokens_kwargs(system="s", cached_block="c", model="gpt-4o")


@pytest.mark.unit
@pytest.mark.llm
def test_openai_provider_extract_text_blocks_returns_single_element_tuple() -> None:
    """``extract_text_blocks`` pulls ``choices[0].message.content`` as a
    single string and wraps it in a one-element tuple so the orchestrator's
    downstream ``"".join(blocks)`` is provider-agnostic."""
    response = _FakeOpenAIResponse(content="hello world")
    assert OpenAIProvider().extract_text_blocks(response) == ("hello world",)


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.parametrize(
    "response",
    [
        _FakeOpenAIResponse(choices=[]),  # empty choices
        _FakeOpenAIResponse(content=None),  # message present but content None
    ],
)
def test_openai_provider_extract_text_blocks_missing_structure_raises(
    response: object,
) -> None:
    """A response missing the expected structure surfaces a typed format
    error (mirrors the ``AnthropicProvider`` guard)."""
    from signalforge.llm.errors import LLMResponseFormatError

    with pytest.raises(LLMResponseFormatError):
        OpenAIProvider().extract_text_blocks(response)


@pytest.mark.unit
@pytest.mark.llm
def test_openai_provider_extract_text_blocks_missing_choices_attr_raises() -> None:
    """A response object missing the ``choices`` attribute entirely raises
    :class:`LLMResponseFormatError`."""
    from signalforge.llm.errors import LLMResponseFormatError

    class _NoChoices:
        usage = _FakeOpenAIUsage(1, 1)

    with pytest.raises(LLMResponseFormatError):
        OpenAIProvider().extract_text_blocks(_NoChoices())


@pytest.mark.unit
@pytest.mark.llm
def test_openai_provider_extract_usage_returns_usage_metrics() -> None:
    """``extract_usage`` maps ``usage.prompt_tokens`` /
    ``usage.completion_tokens`` to :class:`UsageMetrics` with both cache
    fields fixed at 0 (OpenAI has no cache discount —
    ``supports_prompt_caching=False``)."""
    response = _FakeOpenAIResponse(prompt_tokens=120, completion_tokens=45)
    usage = OpenAIProvider().extract_usage(response)
    assert isinstance(usage, UsageMetrics)
    assert usage.input_tokens == 120
    assert usage.output_tokens == 45
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 0


@pytest.mark.unit
@pytest.mark.llm
def test_openai_provider_extract_usage_missing_usage_raises() -> None:
    """A response missing ``usage`` surfaces a typed format error."""
    from signalforge.llm.errors import LLMResponseFormatError

    class _NoUsage:
        choices = [_FakeChoice("x")]

    with pytest.raises(LLMResponseFormatError):
        OpenAIProvider().extract_usage(_NoUsage())


def _openai_api_status(message: str, status: int) -> BaseException:
    """Construct an ``openai.APIStatusError`` with the given status code."""
    import openai

    return openai.APIStatusError(
        message=message, response=httpx.Response(status, request=_OPENAI_REQ), body=None
    )


@pytest.mark.unit
@pytest.mark.llm
def test_openai_provider_classify_exception_maps_each_category() -> None:
    """Each OpenAI SDK exception type maps to the correct neutral category
    (DEC-009 of #136); unrecognised exceptions map to NO_RETRY."""
    import openai

    provider = OpenAIProvider()
    assert (
        provider.classify_exception(
            openai.AuthenticationError(
                message="auth",
                response=httpx.Response(401, request=_OPENAI_REQ),
                body=None,
            )
        )
        is ExceptionCategory.AUTH
    )
    assert (
        provider.classify_exception(
            openai.PermissionDeniedError(
                message="perm",
                response=httpx.Response(403, request=_OPENAI_REQ),
                body=None,
            )
        )
        is ExceptionCategory.AUTH
    )
    assert (
        provider.classify_exception(
            openai.RateLimitError(
                message="rl",
                response=httpx.Response(429, request=_OPENAI_REQ),
                body=None,
            )
        )
        is ExceptionCategory.RATE_LIMIT
    )
    assert (
        provider.classify_exception(openai.APIConnectionError(request=_OPENAI_REQ))
        is ExceptionCategory.CONNECTION
    )
    # 5xx APIStatusError → SERVER_ERROR.
    assert (
        provider.classify_exception(_openai_api_status("5xx", 503))
        is ExceptionCategory.SERVER_ERROR
    )
    # 4xx-non-auth APIStatusError → NO_RETRY.
    assert provider.classify_exception(_openai_api_status("4xx", 422)) is ExceptionCategory.NO_RETRY
    # APIStatusError that is neither 5xx nor 4xx-non-auth (a 3xx) hits the
    # defensive fallthrough → NO_RETRY.
    assert provider.classify_exception(_openai_api_status("3xx", 302)) is ExceptionCategory.NO_RETRY
    # Anything unrecognised maps to NO_RETRY.
    assert provider.classify_exception(ValueError("unrecognised")) is ExceptionCategory.NO_RETRY


@pytest.mark.unit
@pytest.mark.llm
def test_openai_provider_make_client_uses_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    """``make_client`` delegates to the shim's ``_make_openai_client`` so the
    DEC-010 SDK-construction confinement holds."""
    import signalforge.llm._openai_client as shim

    sentinel = object()
    monkeypatch.setattr(shim, "_make_openai_client", lambda: sentinel)
    assert OpenAIProvider().make_client() is sentinel


@pytest.mark.unit
@pytest.mark.llm
def test_unknown_provider_error_lists_both_anthropic_and_openai() -> None:
    """After US-002 registers ``OpenAIProvider``, an unknown name raises
    :class:`UnknownProviderError` listing BOTH ``"anthropic"`` and
    ``"openai"`` in its ``available`` tuple / message (DEC-003 of #135)."""
    with pytest.raises(UnknownProviderError) as excinfo:
        provider_for("xyz")
    err = excinfo.value
    assert err.name == "xyz"
    assert "anthropic" in err.available
    assert "openai" in err.available
    rendered = str(err)
    assert "anthropic" in rendered
    assert "openai" in rendered
