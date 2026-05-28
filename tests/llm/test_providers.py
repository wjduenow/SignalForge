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
