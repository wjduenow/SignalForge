"""Dual sync+async surface on the three vendor fakes (US-007, DEC-012).

Each of :class:`tests.llm._fake.FakeAnthropicClient`,
:class:`tests.llm._fake_openai.FakeOpenAIClient`, and
:class:`tests.llm._fake_gemini.FakeGeminiClient` carries both a sync
``.messages.create(**kw)`` surface (the v0.1 path consumed by
:func:`signalforge.llm.client.call_llm`) AND an async sibling
``.aio.messages.create(**kw)`` (the v0.2 path consumed by
``call_llm_async`` — US-006). Both surfaces drain the SAME
``_create_queue`` (DEC-012's load-bearing invariant: one queue per kind,
not two), so a single fake instance can drive interleaved sync + async
production paths without divergence.

Traces to DEC-012 of ``plans/super/186-grade-asyncio-parallel.md``:
> Dual sync+async methods on the existing fakes. Both surfaces drain the
> same ``expect_*`` queues — one queue per kind (create / count_tokens),
> not two. Simpler than separate ``FakeAsync<Vendor>Client`` classes;
> tests can mix sync + async drains against one fake instance.

The third vendor (Gemini) also exposes an async ``count_tokens`` via
``client.aio.models.count_tokens`` mirroring its real-SDK
``client.aio.models`` namespace shape (US-005); the parallel sync surface
is ``client.models.count_tokens`` and both drain the same ``_count_queue``.

Existing sync tests against the same fakes are untouched — the
``_pop_*`` queue-popping helpers are private; the public sync surface
preserves its existing call shape verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from tests.llm._fake import (
    FakeAnthropicClient,
    FakeCountTokensResponse,
    FakeMessage,
    FakeTextBlock,
    FakeUsage,
)
from tests.llm._fake_gemini import (
    FakeGeminiCandidate,
    FakeGeminiClient,
    FakeGeminiContent,
    FakeGeminiCountTokensResponse,
    FakeGeminiPart,
    FakeGeminiResponse,
    FakeGeminiUsageMetadata,
)
from tests.llm._fake_openai import (
    FakeOpenAIChoice,
    FakeOpenAIClient,
    FakeOpenAICompletion,
    FakeOpenAIMessage,
    FakeOpenAIUsage,
)

# ---- Per-vendor factories -------------------------------------------------


@dataclass
class _VendorFakeFactory:
    """Tiny per-vendor harness so the two AC tests can run identically across
    Anthropic / OpenAI / Gemini. ``make_client`` builds a fresh fake;
    ``make_response`` returns a canned response object the fake will return
    when its queue is drained; ``vendor_name`` is the parametrize id used
    for human-readable test IDs.
    """

    vendor_name: str
    make_client: Any
    make_response: Any


def _make_anthropic_response(text: str = "ok") -> FakeMessage:
    return FakeMessage(
        content=[FakeTextBlock(text=text)],
        usage=FakeUsage(input_tokens=10, output_tokens=5),
    )


def _make_openai_response(text: str = "ok") -> FakeOpenAICompletion:
    return FakeOpenAICompletion(
        choices=[FakeOpenAIChoice(message=FakeOpenAIMessage(content=text))],
        usage=FakeOpenAIUsage(),
    )


def _make_gemini_response(text: str = "ok") -> FakeGeminiResponse:
    return FakeGeminiResponse(
        candidates=[
            FakeGeminiCandidate(
                content=FakeGeminiContent(parts=[FakeGeminiPart(text=text)]),
                finish_reason="STOP",
            )
        ],
        usage_metadata=FakeGeminiUsageMetadata(prompt_token_count=10, candidates_token_count=5),
    )


_FACTORIES: tuple[_VendorFakeFactory, ...] = (
    _VendorFakeFactory(
        vendor_name="anthropic",
        make_client=FakeAnthropicClient,
        make_response=_make_anthropic_response,
    ),
    _VendorFakeFactory(
        vendor_name="openai",
        make_client=FakeOpenAIClient,
        make_response=_make_openai_response,
    ),
    _VendorFakeFactory(
        vendor_name="gemini",
        make_client=FakeGeminiClient,
        make_response=_make_gemini_response,
    ),
)


# ---- AC test 1: async drain consumes the same queue -----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", _FACTORIES, ids=lambda f: f.vendor_name)
async def test_async_drain_consumes_same_queue(factory: _VendorFakeFactory) -> None:
    """Queueing via the sync ``expect_messages_create`` helper and draining via
    the async ``aio.messages.create`` surface consumes the same expectation —
    proving DEC-012's "one queue per kind, not two" invariant.
    """

    fake = factory.make_client()
    response = factory.make_response("async-drain")
    fake.expect_messages_create(matching={"model": "test-model"}, returns=response)

    # Drain via the ASYNC surface; the expectation queued via the sync helper
    # must be consumed because both surfaces share the same ``_create_queue``.
    returned = await fake.aio.messages.create(model="test-model")
    assert returned is response

    # No leftover expectations → the async drain populated the same FIFO the
    # sync surface populates. If a separate async-side queue existed, the
    # sync-queued expectation would still be present and this would raise.
    fake.assert_all_expectations_met()


# ---- AC test 2: mixed sync + async drain ----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", _FACTORIES, ids=lambda f: f.vendor_name)
async def test_mixed_sync_async_drain(factory: _VendorFakeFactory) -> None:
    """Two expectations queued, one drained synchronously and one
    asynchronously; both surfaces consume from the SAME FIFO queue. Proves
    that production code mixing sync ``call_llm`` and async
    ``call_llm_async`` calls against one fake never observes queue
    duplication or skips. DEC-012.
    """

    fake = factory.make_client()
    response_first = factory.make_response("first")
    response_second = factory.make_response("second")

    # Queue 2 expectations; first to be drained sync, second to be drained
    # async. Both via the SAME ``expect_messages_create`` helper.
    fake.expect_messages_create(matching={"model": "m1"}, returns=response_first)
    fake.expect_messages_create(matching={"model": "m2"}, returns=response_second)

    # Sync drain pops the first FIFO entry.
    got_first = fake.messages.create(model="m1")
    assert got_first is response_first

    # Async drain pops the (now-first-in-queue) second entry — proves the
    # async surface is reading from the same FIFO the sync surface mutated.
    got_second = await fake.aio.messages.create(model="m2")
    assert got_second is response_second

    fake.assert_all_expectations_met()


# ---- Anthropic-specific: count_tokens dual surface ------------------------


@pytest.mark.asyncio
async def test_anthropic_count_tokens_dual_surface_shares_queue() -> None:
    """The Anthropic fake's ``count_tokens`` queue is exercised by BOTH sync
    (``fake.messages.count_tokens``) and async (``fake.aio.messages.count_tokens``)
    surfaces — a second per-vendor invariant beyond the ``create`` queue.
    OpenAI / Gemini ``count_tokens`` raises because their providers declare
    ``supports_token_count=False``; only Anthropic exercises this code path.
    DEC-012.
    """

    fake = FakeAnthropicClient()
    response_sync = FakeCountTokensResponse(input_tokens=123)
    response_async = FakeCountTokensResponse(input_tokens=456)

    fake.expect_count_tokens(matching={"model": "sync"}, returns=response_sync)
    fake.expect_count_tokens(matching={"model": "async"}, returns=response_async)

    # Sync drain pops first FIFO entry.
    got_sync = fake.messages.count_tokens(model="sync")
    assert got_sync is response_sync

    # Async drain pops the now-first entry.
    got_async = await fake.aio.messages.count_tokens(model="async")
    assert got_async is response_async

    fake.assert_all_expectations_met()


# ---- Gemini-specific: models.count_tokens dual surface --------------------


@pytest.mark.asyncio
async def test_gemini_count_tokens_dual_surface_shares_queue() -> None:
    """The Gemini fake's ``models.count_tokens`` queue (used by the US-007
    estimate path) is exercised by BOTH sync ``fake.models.count_tokens`` and
    async ``fake.aio.models.count_tokens`` surfaces — matching the real Gemini
    SDK's ``client.aio.models.*`` namespace shape (DEC-016 of #137). DEC-012.
    """

    fake = FakeGeminiClient()
    response_sync = FakeGeminiCountTokensResponse(total_tokens=789)
    response_async = FakeGeminiCountTokensResponse(total_tokens=1011)

    fake.expect_count_tokens(matching={"model": "sync"}, returns=response_sync)
    fake.expect_count_tokens(matching={"model": "async"}, returns=response_async)

    got_sync = fake.models.count_tokens(model="sync")
    assert got_sync is response_sync

    got_async = await fake.aio.models.count_tokens(model="async")
    assert got_async is response_async

    fake.assert_all_expectations_met()
