"""Pin the Gemini async-adapter ``.aio`` forwarding shape (#186 US-005).

Gemini's async surface lives on the SAME ``google.genai.Client`` via the
``.aio`` namespace — there is no separate ``AsyncGenAI`` constructor. The
async adapter (:class:`signalforge.llm.providers._GeminiAsyncMessagesAdapter`)
forwards ``.messages.create(**kw)`` to ``await client.aio.models.generate_content(**kw)``
and ``.messages.count_tokens(**kw)`` to ``await client.aio.models.count_tokens(**kw)``.

These tests pin three load-bearing properties:

1. Each async adapter method IS a coroutine (returns an awaitable) — a
   regression replacing ``async def`` with ``def`` would silently break
   the async orchestrator's ``await client.messages.create(...)`` call.
2. The kwargs forwarded to ``.aio.models.*`` match the kwargs the
   orchestrator passes verbatim — no mid-flight transformation.
3. :meth:`GeminiProvider.make_async_client` reuses the existing bare-client
   factory (:func:`signalforge.llm._gemini_client._make_gemini_client`)
   rather than introducing a new constructor — keeps AST Scan 10
   (``genai.Client(...)`` only in ``_gemini_client.py``) green by
   construction. The DEC-014 of #186 explicitly notes Scan 10 stays
   unchanged for Gemini.

Mirrors the unit-test shape used elsewhere in the LLM test suite
(``tests/llm/test_providers.py`` for sync per-method tests); the
end-to-end ``call_llm_async`` integration via :class:`FakeGeminiClient`
is exercised separately in US-006 (orchestrator) + US-008 (fake async
extension).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# _GeminiAsyncMessagesAdapter — forwards ``.create`` / ``.count_tokens``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.llm
async def test_async_messages_adapter_create_awaits_aio_models_generate_content() -> None:
    """``_GeminiAsyncMessagesAdapter.create(**kw)`` awaits
    ``client.aio.models.generate_content(**kw)`` and returns its result.

    The fake ``aio.models.generate_content`` is itself an ``async def`` —
    a real ``google.genai.Client.aio.models.generate_content`` is an
    awaitable coroutine in ``google-genai >= 0.5``. Forwarding via
    ``await`` is the only correct shape; a regression that dropped the
    ``await`` would return a coroutine object instead of the response.
    """
    from signalforge.llm.providers import _GeminiAsyncMessagesAdapter

    captured_kwargs: dict[str, Any] = {}

    async def _fake_generate_content(**kwargs: Any) -> str:
        captured_kwargs.update(kwargs)
        return "ok-response"

    fake_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=_fake_generate_content)),
    )

    adapter = _GeminiAsyncMessagesAdapter(fake_client)

    # The adapter method itself must be a coroutine function — pin this
    # explicitly so a regression replacing ``async def`` with ``def`` fails
    # loudly here rather than producing a hard-to-debug "returns a coroutine
    # instead of the result" symptom further downstream.
    assert inspect.iscoroutinefunction(adapter.create)

    result = await adapter.create(model="gemini-2.5-flash", contents=["hi"], config={"x": 1})

    assert result == "ok-response"
    assert captured_kwargs == {
        "model": "gemini-2.5-flash",
        "contents": ["hi"],
        "config": {"x": 1},
    }


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.llm
async def test_async_messages_adapter_count_tokens_awaits_aio_models_count_tokens() -> None:
    """``_GeminiAsyncMessagesAdapter.count_tokens(**kw)`` awaits
    ``client.aio.models.count_tokens(**kw)`` and returns its result.

    ``GeminiProvider`` declares ``supports_token_count = False`` (#137
    DEC-003) so the async orchestrator skips the pre-send count gate on
    the happy path — but the surface is present on the adapter for
    protocol parity with the sync sibling. Pin the forwarding shape so a
    future capability-flag flip exposes a working method, not a stub.
    """
    from signalforge.llm.providers import _GeminiAsyncMessagesAdapter

    captured_kwargs: dict[str, Any] = {}

    async def _fake_count_tokens(**kwargs: Any) -> SimpleNamespace:
        captured_kwargs.update(kwargs)
        return SimpleNamespace(total_tokens=42)

    fake_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(count_tokens=_fake_count_tokens)),
    )

    adapter = _GeminiAsyncMessagesAdapter(fake_client)

    assert inspect.iscoroutinefunction(adapter.count_tokens)

    result = await adapter.count_tokens(model="gemini-2.5-flash", contents=["hi"])

    assert result.total_tokens == 42
    assert captured_kwargs == {"model": "gemini-2.5-flash", "contents": ["hi"]}


# ---------------------------------------------------------------------------
# _GeminiAsyncClientAdapter — exposes ``.messages`` over the bare client
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_async_client_adapter_exposes_async_messages_adapter() -> None:
    """``_GeminiAsyncClientAdapter(client).messages`` is a
    :class:`_GeminiAsyncMessagesAdapter` wrapping the same bare client.

    The orchestrator (``call_llm_async``, US-006) reads ``.messages`` once
    and calls ``await client.messages.create(...)``; the adapter must
    therefore expose a typed async-messages façade, not the raw SDK
    namespace. Pin this so a regression that aliased ``self.messages`` to
    e.g. ``client.aio.models`` directly would fail loud.
    """
    from signalforge.llm.providers import (
        _GeminiAsyncClientAdapter,
        _GeminiAsyncMessagesAdapter,
    )

    bare_client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace()))

    wrapped = _GeminiAsyncClientAdapter(bare_client)

    assert isinstance(wrapped.messages, _GeminiAsyncMessagesAdapter)
    # The async messages adapter must close over the SAME bare client so
    # forwarding reaches ``client.aio.models.*`` at call time.
    assert wrapped.messages._client is bare_client


# ---------------------------------------------------------------------------
# GeminiProvider.make_async_client — reuses _make_gemini_client (no new ctor)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_make_async_client_reuses_make_gemini_client_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """:meth:`GeminiProvider.make_async_client` calls the SAME
    :func:`signalforge.llm._gemini_client._make_gemini_client` factory the
    sync :meth:`make_client` uses, and wraps the result in
    :class:`_GeminiAsyncClientAdapter`.

    This is the load-bearing #186 DEC-014 invariant for Gemini: Scan 10
    pins ``genai.Client(...)`` construction to ``_gemini_client.py``, and
    the async path MUST NOT introduce a new vendor constructor (Gemini's
    ``.aio`` namespace lives on the existing ``genai.Client``, so no new
    AST scan is added — see Scan 3b / Scan 9b for the Anthropic / OpenAI
    contrast). Patching the factory and asserting it is invoked exactly
    once proves the async path goes through the existing seam.
    """
    from signalforge.llm import _gemini_client as gemini_shim
    from signalforge.llm.providers import (
        GeminiProvider,
        _GeminiAsyncClientAdapter,
        _GeminiAsyncMessagesAdapter,
    )

    call_counter = {"n": 0}
    sentinel_bare_client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace()))

    def _factory(api_key: str | None = None) -> Any:
        del api_key  # signature parity with the real factory; unused in this test
        call_counter["n"] += 1
        return sentinel_bare_client

    monkeypatch.setattr(gemini_shim, "_make_gemini_client", _factory)

    async_client = GeminiProvider().make_async_client()

    # Factory called exactly once — no second construction site.
    assert call_counter["n"] == 1
    # The returned object is the async adapter, NOT the bare client.
    assert isinstance(async_client, _GeminiAsyncClientAdapter)
    # The adapter wraps the SAME bare client the factory produced — no
    # extra layer of indirection that would defeat the ``.aio`` forwarding.
    assert async_client._client is sentinel_bare_client
    # And the ``.messages`` façade is an async adapter ready to ``await``.
    assert isinstance(async_client.messages, _GeminiAsyncMessagesAdapter)


@pytest.mark.unit
@pytest.mark.llm
def test_geminiprovider_supports_async_is_true() -> None:
    """:attr:`GeminiProvider.supports_async` is ``True`` so the grade
    engine's capability check (#186 DEC-006) permits ``max_concurrent_calls
    > 1`` against Gemini.

    Set on the class in #186 US-002 (LLMProvider ABC additions). Pinned
    here next to the US-005 async-adapter wiring so a regression flipping
    the flag while the adapter is in place fails loudly.
    """
    from signalforge.llm.providers import GeminiProvider

    assert GeminiProvider.supports_async is True
