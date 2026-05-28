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


# ---------------------------------------------------------------------------
# Coverage-closing tests for #136 US-008 QG / PR #152 codecov gaps
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_anthropic_provider_estimate_input_tokens_skips_system_kwarg_when_empty() -> None:
    """``AnthropicProvider.estimate_input_tokens`` MUST omit the ``system=``
    kwarg entirely when ``system`` is the empty default — otherwise the SDK
    would carry a spurious ``system=""`` block in the count, inflating the
    figure by Anthropic's empty-system envelope size.

    Closes PR #152 codecov gap on the no-system branch
    (``providers.py``: ``response = resolved.messages.count_tokens(...)``
    inside the ``else:`` arm).
    """
    from tests.llm._fake import FakeAnthropicClient, FakeCountTokensResponse

    fake = FakeAnthropicClient(project="fake")
    captured: dict[str, Any] = {}

    def _capture(kwargs: dict[str, Any]) -> bool:
        captured.update(kwargs)
        return True

    fake.expect_count_tokens(matching=_capture, returns=FakeCountTokensResponse(input_tokens=42))

    tokens = AnthropicProvider().estimate_input_tokens(
        "claude-sonnet-4-6", "hello world", system="", client=fake
    )

    assert tokens == 42
    assert "system" not in captured, (
        f"empty system MUST be omitted from count_tokens kwargs; got {captured.keys()}"
    )
    fake.assert_all_expectations_met()


@pytest.mark.unit
@pytest.mark.llm
def test_anthropic_provider_estimate_input_tokens_raises_on_missing_input_tokens() -> None:
    """A count_tokens response with a missing/non-int ``input_tokens`` field
    raises :class:`LLMResponseFormatError` (defensive guard against an SDK
    response-shape regression).

    Closes PR #152 codecov gap on the ``raise LLMResponseFormatError(...)``
    arm of ``AnthropicProvider.estimate_input_tokens``.
    """
    from signalforge.llm.errors import LLMResponseFormatError
    from tests.llm._fake import FakeAnthropicClient

    fake = FakeAnthropicClient(project="fake")

    # Queue a response object whose ``input_tokens`` attr is missing
    # (a real SDK response always carries it as an int; a None return
    # surfaces the guard).
    class _NoInputTokens:
        pass

    fake.expect_count_tokens(matching=lambda kwargs: True, returns=_NoInputTokens())

    with pytest.raises(LLMResponseFormatError, match="input_tokens"):
        AnthropicProvider().estimate_input_tokens(
            "claude-sonnet-4-6", "hello", system="sys", client=fake
        )


@pytest.mark.unit
@pytest.mark.llm
def test_openai_provider_extract_text_blocks_missing_message_attr_raises() -> None:
    """A choice object missing the ``message`` attribute (or with
    ``message=None``) raises :class:`LLMResponseFormatError`.

    Distinct from the existing ``content=None`` / empty-choices cases
    (those exercise different arms of ``extract_text_blocks``). Closes
    PR #152 codecov gap on the ``raise LLMResponseFormatError(...)`` arm
    that fires when ``getattr(first, "message", None) is None``.
    """
    from types import SimpleNamespace

    from signalforge.llm.errors import LLMResponseFormatError

    # A choice with message=None (real SDK never produces this, but the
    # guard is the contract — surface a typed error rather than crash
    # later on the content attribute access).
    response = SimpleNamespace(choices=[SimpleNamespace(message=None)])

    with pytest.raises(LLMResponseFormatError, match="message"):
        OpenAIProvider().extract_text_blocks(response)


# ---------------------------------------------------------------------------
# US-002 of #137 — GeminiProvider strategy
# ---------------------------------------------------------------------------


def _genai_errors() -> Any:
    """Lazy-import ``google.genai.errors`` per-test (mirrors the Snowflake
    ``_sfe()`` helper in ``warehouse-adapters.md``).

    A module-level ``from google.genai import errors`` would go stale under
    any test that mutates ``sys.modules`` for the SDK, and lazy-importing
    keeps the assertions honest about which class identity the mapper sees.
    """
    from google.genai import errors as genai_errors  # noqa: PLC0415

    return genai_errors


def _make_requests_response(code: int, status_text: str, message: str) -> Any:
    """Construct a real ``requests.Response`` carrying the JSON body the
    ``google.genai.errors.APIError.__init__`` parses.

    The SDK's ``APIError`` constructor takes a ``requests.Response``-like
    object and pulls ``code`` / ``status`` / ``message`` out of its JSON
    body; the bare integer ``code`` is stored on ``exc.code``, which is what
    :meth:`GeminiProvider.classify_exception` reads.
    """
    import json

    import requests

    response = requests.Response()
    response.status_code = code
    response._content = json.dumps(  # type: ignore[attr-defined]
        {"error": {"code": code, "status": status_text, "message": message}}
    ).encode("utf-8")
    return response


@pytest.mark.unit
@pytest.mark.llm
def test_provider_for_gemini_returns_geminiprovider() -> None:
    """US-002 registers ``GeminiProvider`` at module import time, so
    ``provider_for("gemini")`` returns it. DEC-003: both capability flags
    are ``False``."""
    from signalforge.llm.providers import GeminiProvider

    provider = provider_for("gemini")
    assert isinstance(provider, GeminiProvider)
    assert provider.name == "gemini"
    assert provider.supports_prompt_caching is False
    assert provider.supports_token_count is False


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_build_create_kwargs_no_cache_marker() -> None:
    """DEC-003 / DEC-008 of #135: no ``cache_control`` block anywhere and
    no ``extra_headers`` key — the capability flags are ``False`` so the
    provider must emit a non-caching request shape.

    Mirrors the AC #135 pinned for the no-cache fake. ``cache_marker_active``
    is irrelevant: the provider must produce the same non-caching shape
    whether the flag arrives ``True`` (defensive) or ``False`` (the
    orchestrator's resolved value)."""
    from signalforge.llm.providers import GeminiProvider

    provider = GeminiProvider()
    for marker_active in (True, False):
        kwargs = provider.build_create_kwargs(
            system="SYS",
            cached_block="CACHED",
            dynamic_block="DYN",
            model="gemini-2.5-flash",
            max_tokens=1024,
            cache_ttl="5m",
            cache_marker_active=marker_active,
        )
        assert "cache_control" not in repr(kwargs)
        assert "extra_headers" not in kwargs


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_build_create_kwargs_sets_json_response_format() -> None:
    """DEC-018: server-side JSON enforcement via
    ``response_mime_type="application/json"`` on the ``GenerateContentConfig``.
    Belt-and-braces with the tolerant parser (issue #144) — server-side
    enforcement removes the prose-preamble drift class entirely."""
    from signalforge.llm.providers import GeminiProvider

    kwargs = GeminiProvider().build_create_kwargs(
        system="SYS",
        cached_block="C",
        dynamic_block="D",
        model="gemini-2.5-flash",
        max_tokens=128,
        cache_ttl="5m",
        cache_marker_active=False,
    )
    config = kwargs["config"]
    # The config can be a dict (GenerateContentConfigDict) or the typed
    # GenerateContentConfig; cover both shapes.
    if isinstance(config, dict):
        assert config["response_mime_type"] == "application/json"
    else:
        assert config.response_mime_type == "application/json"


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_build_create_kwargs_system_and_contents() -> None:
    """DEC-004: ``system`` → ``system_instruction``; ``cached_block`` +
    ``dynamic_block`` concatenated into a single user-role ``contents``
    entry. The provider has no caching so there is no value in keeping the
    two blocks separate."""
    from signalforge.llm.providers import GeminiProvider

    kwargs = GeminiProvider().build_create_kwargs(
        system="THE-SYSTEM",
        cached_block="cached-text",
        dynamic_block="dynamic-text",
        model="gemini-2.5-flash",
        max_tokens=42,
        cache_ttl="5m",
        cache_marker_active=False,
    )
    assert kwargs["model"] == "gemini-2.5-flash"
    config = kwargs["config"]
    system_instruction = (
        config["system_instruction"] if isinstance(config, dict) else config.system_instruction
    )
    assert system_instruction == "THE-SYSTEM"
    # Single user-role contents entry concatenating the two blocks.
    contents = kwargs["contents"]
    assert len(contents) == 1
    assert "cached-text" in contents[0]
    assert "dynamic-text" in contents[0]


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_build_count_tokens_kwargs_raises_notimplementederror() -> None:
    """DEC-003: ``supports_token_count = False`` means the orchestrator
    never calls this method — it raises ``NotImplementedError`` (mirrors
    :class:`tests.llm._fake_provider.FakeNoCacheProvider`) so any accidental
    call is loud."""
    from signalforge.llm.providers import GeminiProvider

    with pytest.raises(NotImplementedError) as excinfo:
        GeminiProvider().build_count_tokens_kwargs(
            system="s",
            cached_block="c",
            model="gemini-2.5-flash",
        )
    assert "supports_token_count=False" in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_extract_text_blocks_happy_path() -> None:
    """DEC-005: ``extract_text_blocks`` walks ``response.candidates``, then
    each candidate's ``content.parts``, collecting non-empty ``part.text``."""
    from types import SimpleNamespace as N

    from signalforge.llm.providers import GeminiProvider

    response = N(
        candidates=[
            N(
                content=N(parts=[N(text="hello"), N(text="world")]),
                finish_reason=N(name="STOP"),
            )
        ],
        usage_metadata=N(prompt_token_count=10, candidates_token_count=2),
    )
    assert GeminiProvider().extract_text_blocks(response) == ("hello", "world")


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_extract_text_blocks_safety_blocked_raises() -> None:
    """DEC-005: a safety-blocked response (no candidate yields any text)
    surfaces :class:`LLMResponseFormatError` whose message names the
    finish_reason. The OpenAI parser-degrade path can't catch a *structural*
    block (no candidate content at all), so this typed-error branch is
    load-bearing for Gemini."""
    from types import SimpleNamespace as N

    from signalforge.llm.errors import LLMResponseFormatError
    from signalforge.llm.providers import GeminiProvider

    response = N(
        candidates=[N(content=None, finish_reason=N(name="SAFETY"))],
        usage_metadata=N(prompt_token_count=10, candidates_token_count=0),
    )
    with pytest.raises(LLMResponseFormatError) as excinfo:
        GeminiProvider().extract_text_blocks(response)
    assert "SAFETY" in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_extract_text_blocks_no_candidates_raises() -> None:
    """No candidates at all (a defensively-shaped response) routes through
    the same typed error with ``finish_reason='unknown'``."""
    from types import SimpleNamespace as N

    from signalforge.llm.errors import LLMResponseFormatError
    from signalforge.llm.providers import GeminiProvider

    response = N(candidates=[], usage_metadata=N(prompt_token_count=0, candidates_token_count=0))
    with pytest.raises(LLMResponseFormatError) as excinfo:
        GeminiProvider().extract_text_blocks(response)
    assert "unknown" in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_extract_usage_zero_cache_fields() -> None:
    """DEC-003: the provider has no Anthropic-style prompt caching, so both
    cache-token fields are reported as 0 regardless of the response shape.

    Maps Gemini's ``prompt_token_count`` → ``input_tokens`` and
    ``candidates_token_count`` → ``output_tokens``."""
    from types import SimpleNamespace as N

    from signalforge.llm.providers import GeminiProvider

    response = N(
        candidates=[N(content=N(parts=[N(text="x")]), finish_reason=N(name="STOP"))],
        usage_metadata=N(prompt_token_count=120, candidates_token_count=45),
    )
    usage = GeminiProvider().extract_usage(response)
    assert usage.input_tokens == 120
    assert usage.output_tokens == 45
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 0


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_extract_usage_missing_metadata_raises() -> None:
    """A response missing ``usage_metadata`` entirely surfaces a typed
    :class:`LLMResponseFormatError` — the seam never silently fabricates 0s
    when the whole accounting block is gone."""
    from types import SimpleNamespace as N

    from signalforge.llm.errors import LLMResponseFormatError
    from signalforge.llm.providers import GeminiProvider

    response = N(candidates=[N(content=N(parts=[N(text="x")]), finish_reason=N(name="STOP"))])
    with pytest.raises(LLMResponseFormatError):
        GeminiProvider().extract_usage(response)


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_extract_usage_missing_inner_field_raises() -> None:
    """A ``usage_metadata`` block missing one of the per-field counts
    (or carrying a non-int value) surfaces :class:`LLMResponseFormatError`
    instead of silently defaulting to 0. Mirrors the Anthropic precedent
    via the shared ``_extract_usage_field`` helper — pinned in response
    to PR #151 review feedback (Copilot, line 920 of providers.py).
    """
    from types import SimpleNamespace as N

    from signalforge.llm.errors import LLMResponseFormatError
    from signalforge.llm.providers import GeminiProvider

    # Missing prompt_token_count: getattr would have returned 0 silently.
    no_prompt = N(
        candidates=[N(content=N(parts=[N(text="x")]), finish_reason=N(name="STOP"))],
        usage_metadata=N(candidates_token_count=45),
    )
    with pytest.raises(LLMResponseFormatError, match="prompt_token_count"):
        GeminiProvider().extract_usage(no_prompt)

    # Non-int candidates_token_count: also fails loud.
    bad_type = N(
        candidates=[N(content=N(parts=[N(text="x")]), finish_reason=N(name="STOP"))],
        usage_metadata=N(prompt_token_count=120, candidates_token_count="not-an-int"),
    )
    with pytest.raises(LLMResponseFormatError, match="candidates_token_count"):
        GeminiProvider().extract_usage(bad_type)


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_classify_exception_each_category() -> None:
    """DEC-006: each ``google.genai.errors`` shape maps to its neutral
    :class:`ExceptionCategory`. SDK classes lazy-imported per-test (mirrors
    Snowflake's ``_sfe()`` pattern)."""
    from signalforge.llm.providers import GeminiProvider

    genai_errors = _genai_errors()
    provider = GeminiProvider()

    # 401 / 403 → AUTH (ClientError carrying the HTTP code)
    err_401 = genai_errors.ClientError(401, _make_requests_response(401, "UNAUTHENTICATED", "x"))
    assert provider.classify_exception(err_401) is ExceptionCategory.AUTH
    err_403 = genai_errors.ClientError(403, _make_requests_response(403, "PERMISSION_DENIED", "x"))
    assert provider.classify_exception(err_403) is ExceptionCategory.AUTH

    # 429 → RATE_LIMIT
    err_429 = genai_errors.ClientError(429, _make_requests_response(429, "RESOURCE_EXHAUSTED", "x"))
    assert provider.classify_exception(err_429) is ExceptionCategory.RATE_LIMIT

    # 503 / 5xx → SERVER_ERROR
    err_503 = genai_errors.ServerError(503, _make_requests_response(503, "UNAVAILABLE", "x"))
    assert provider.classify_exception(err_503) is ExceptionCategory.SERVER_ERROR

    # Other 4xx → NO_RETRY (e.g. 418 teapot)
    err_other = genai_errors.ClientError(418, _make_requests_response(418, "TEAPOT", "x"))
    assert provider.classify_exception(err_other) is ExceptionCategory.NO_RETRY

    # CONNECTION — httpx.ConnectError leaks through on a hard network failure.
    import httpx

    assert provider.classify_exception(httpx.ConnectError("boom")) is ExceptionCategory.CONNECTION
    assert provider.classify_exception(httpx.ConnectTimeout("slow")) is ExceptionCategory.CONNECTION

    # Anything else → NO_RETRY (a plain ValueError as the unrecognised case).
    assert provider.classify_exception(ValueError("unrecognised")) is ExceptionCategory.NO_RETRY


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_make_client_uses_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    """``make_client`` delegates to the shim's ``_make_gemini_client`` (DEC-001),
    then wraps the bare client in the ``.messages`` façade adapter (DEC-004)
    so the orchestrator's vendor-neutral ``client.messages.create(**kwargs)``
    call shape works."""
    import signalforge.llm._gemini_client as shim
    from signalforge.llm.providers import GeminiProvider

    class _SentinelRawClient:
        class _Models:
            def generate_content(self, **kwargs: Any) -> str:
                return f"forwarded:{kwargs.get('model')}"

            def count_tokens(self, **kwargs: Any) -> str:
                return f"count:{kwargs.get('model')}"

        models = _Models()

    monkeypatch.setattr(shim, "_make_gemini_client", lambda: _SentinelRawClient())
    client: Any = GeminiProvider().make_client()
    # The adapter exposes the .messages façade...
    assert hasattr(client, "messages")
    assert client.messages.create(model="gemini-2.5-flash") == "forwarded:gemini-2.5-flash"
    # ...and forwards count_tokens for the US-007 estimate path.
    assert client.messages.count_tokens(model="gemini-2.5-flash") == "count:gemini-2.5-flash"


@pytest.mark.unit
@pytest.mark.llm
def test_unknown_provider_lists_anthropic_and_gemini() -> None:
    """``UnknownProviderError`` for an unregistered name lists every
    currently registered provider — both ``anthropic`` (US-002 of #135) and
    ``gemini`` (US-002 of #137)."""
    with pytest.raises(UnknownProviderError) as excinfo:
        provider_for("xyz-definitely-not-registered")
    err = excinfo.value
    assert err.name == "xyz-definitely-not-registered"
    assert "anthropic" in err.available
    assert "gemini" in err.available
    rendered = str(err)
    assert "anthropic" in rendered
    assert "gemini" in rendered


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_in_signalforge_llm_public_surface() -> None:
    """``GeminiProvider`` is re-exported from :mod:`signalforge.llm` so
    downstream callers can ``from signalforge.llm import GeminiProvider``
    without reaching into the private :mod:`signalforge.llm.providers`."""
    import signalforge.llm as llm
    from signalforge.llm.providers import GeminiProvider

    assert llm.GeminiProvider is GeminiProvider
    assert "GeminiProvider" in llm.__all__


# ---------------------------------------------------------------------------
# US-007 — GeminiProvider.estimate_input_tokens
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_estimate_input_tokens_via_injected_client() -> None:
    """:meth:`GeminiProvider.estimate_input_tokens` delegates to the injected
    client's ``models.count_tokens`` and returns ``response.total_tokens``.

    Pins the load-bearing call shape: ``model=<arg>`` and
    ``contents=[system + text]``. A regression that re-shaped the call
    (e.g. splitting system and text into two ``contents`` entries) would
    silently shift the count by Gemini's per-content envelope tokens.
    """
    from signalforge.llm.providers import GeminiProvider
    from tests.llm._fake_gemini import FakeGeminiClient, FakeGeminiCountTokensResponse

    fake = FakeGeminiClient()
    captured: dict[str, Any] = {}

    def _capture(kwargs: dict[str, Any]) -> bool:
        captured.update(kwargs)
        return True

    fake.expect_count_tokens(
        matching=_capture,
        returns=FakeGeminiCountTokensResponse(total_tokens=42),
    )

    tokens = GeminiProvider().estimate_input_tokens(
        "gemini-2.5-flash", "hello world", system="sys-prefix ", client=fake
    )

    assert tokens == 42
    assert captured == {
        "model": "gemini-2.5-flash",
        "contents": ["sys-prefix hello world"],
    }
    fake.assert_all_expectations_met()


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_estimate_input_tokens_concatenates_system_and_text() -> None:
    """The ``contents`` arg is exactly ``[system + text]`` — Gemini's
    count_tokens endpoint does not distinguish a system envelope from
    user content, so the provider concatenates them into one entry.

    Asserting on the literal concatenated string (rather than only on
    membership) catches a regression that flipped the order to
    ``[text + system]`` or inserted a separator.
    """
    from signalforge.llm.providers import GeminiProvider
    from tests.llm._fake_gemini import FakeGeminiClient, FakeGeminiCountTokensResponse

    fake = FakeGeminiClient()
    fake.expect_count_tokens(
        matching=lambda kw: True,
        returns=FakeGeminiCountTokensResponse(total_tokens=7),
    )

    tokens = GeminiProvider().estimate_input_tokens(
        "gemini-2.5-flash", "BODY", system="SYSTEM ", client=fake
    )

    assert tokens == 7
    assert fake.count_tokens_calls == [
        {"model": "gemini-2.5-flash", "contents": ["SYSTEM BODY"]},
    ]
    fake.assert_all_expectations_met()


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_estimate_input_tokens_missing_total_tokens_raises() -> None:
    """A count_tokens response with a missing/non-int ``total_tokens``
    raises :class:`LLMResponseFormatError` (defensive guard against an
    SDK response-shape regression).

    Mirrors the Anthropic precedent
    (``test_anthropic_provider_estimate_input_tokens_raises_on_missing_input_tokens``).
    """
    from signalforge.llm.errors import LLMResponseFormatError
    from signalforge.llm.providers import GeminiProvider
    from tests.llm._fake_gemini import FakeGeminiClient, FakeGeminiCountTokensResponse

    fake = FakeGeminiClient()
    # ``total_tokens=None`` is a possible SDK return shape (the field is
    # ``int | None`` in the real CountTokensResponse); the guard surfaces
    # it as a typed error rather than letting the orchestrator pretend
    # the prompt costs zero tokens.
    fake.expect_count_tokens(
        matching=lambda kw: True,
        returns=FakeGeminiCountTokensResponse(total_tokens=None),
    )

    with pytest.raises(LLMResponseFormatError, match="total_tokens"):
        GeminiProvider().estimate_input_tokens("gemini-2.5-flash", "hello", system="", client=fake)


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_estimate_input_tokens_missing_models_surface_raises() -> None:
    """A client missing ``.models.count_tokens`` raises
    :class:`LLMResponseFormatError` rather than ``AttributeError``.

    The ``--estimate`` engine catches typed LLM errors as supplementary
    failures (cli-layer.md DEC-005 of #36) and renders
    ``<unavailable: <ErrorClass>>``; an untyped ``AttributeError`` would
    propagate through the panic boundary as exit 1 instead.
    """
    from signalforge.llm.errors import LLMResponseFormatError
    from signalforge.llm.providers import GeminiProvider

    # A "client" object with no ``.models`` attribute at all.
    class _BareClient:
        pass

    with pytest.raises(LLMResponseFormatError, match="models.count_tokens"):
        GeminiProvider().estimate_input_tokens("gemini-2.5-flash", "hello", client=_BareClient())


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_estimate_input_tokens_builds_client_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``client=None``, the provider builds a fresh SDK client via
    :func:`signalforge.llm._gemini_client._make_gemini_client` and wraps
    it in :class:`_GeminiClientAdapter`.

    Patches the shim factory to return a fake whose ``models.count_tokens``
    is queryable; asserts the factory was called exactly once. Without
    this lazy-build fallback, callers that don't thread a client through
    (no v0.x CLI path does on the Gemini estimate happy path right now)
    would get :class:`AttributeError` instead.
    """
    from signalforge.llm import _gemini_client as gemini_shim
    from signalforge.llm.providers import GeminiProvider
    from tests.llm._fake_gemini import FakeGeminiCountTokensResponse

    call_counter = {"n": 0}

    class _FakeRawClient:
        class _Models:
            def count_tokens(self, **kwargs: Any) -> Any:
                assert kwargs.get("model") == "gemini-2.5-flash"
                assert kwargs.get("contents") == ["sys + body"]
                return FakeGeminiCountTokensResponse(total_tokens=99)

        models = _Models()

    def _factory() -> Any:
        call_counter["n"] += 1
        return _FakeRawClient()

    monkeypatch.setattr(gemini_shim, "_make_gemini_client", _factory)

    tokens = GeminiProvider().estimate_input_tokens("gemini-2.5-flash", "body", system="sys + ")

    assert tokens == 99
    assert call_counter["n"] == 1
