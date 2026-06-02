"""Async sibling tests for :func:`signalforge.llm.client.call_llm_async`
(US-006 of issue #186).

Mirrors the structure of ``test_client.py`` + ``test_client_retries.py``
but drives the async orchestrator. Each test reassigns the module-level
``_async_sleep`` / ``_rand_uniform`` aliases (DEC-011 of #186) to
deterministic stand-ins so retry tests run instantly without timing
flake — same load-bearing test-injection seam as the sync sibling.

The :class:`tests.llm._fake.FakeAnthropicClient` is driven via its
``.aio`` namespace per US-007 (DEC-012 of #186): both sync and async
surfaces share the SAME ``_create_queue`` / ``_count_queue``, so the
``expect_*`` helpers populate the queue and the async ``call_llm_async``
drains it via ``await client.messages.create(...)``.

Traces to DEC-001, DEC-002, DEC-011 of
``plans/super/186-grade-asyncio-parallel.md``.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any, cast

import anthropic
import httpx
import pytest

from signalforge.llm import client as client_module
from signalforge.llm.client import call_llm, call_llm_async
from signalforge.llm.errors import (
    LLMConnectionError,
    LLMProviderAsyncUnsupportedError,
    LLMRateLimitError,
    LLMServerError,
)
from signalforge.llm.models import LLMResult
from signalforge.llm.providers import provider_for, register_provider

from ._fake import (
    FakeAnthropicClient,
    FakeCountTokensResponse,
    FakeMessage,
    FakeTextBlock,
    FakeUsage,
)
from ._fake_provider import (
    FAKE_NOCACHE_PROVIDER_NAME,
    FakeNoCacheProvider,
    FakeSyncOnlyNoCacheProvider,
)

pytestmark = pytest.mark.llm


# ---- helpers --------------------------------------------------------------

_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _rate_limit_error() -> anthropic.RateLimitError:
    return anthropic.RateLimitError(
        message="rate limited",
        response=httpx.Response(429, request=_REQ),
        body=None,
    )


def _status_error(code: int) -> anthropic.APIStatusError:
    return anthropic.APIStatusError(
        message=f"status {code}",
        response=httpx.Response(code, request=_REQ),
        body=None,
    )


def _connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=_REQ)


def _ok_count() -> FakeCountTokensResponse:
    # Pick a value >= every model minimum (Haiku is 2048).
    return FakeCountTokensResponse(input_tokens=2048)


def _ok_message() -> FakeMessage:
    return FakeMessage(
        content=[FakeTextBlock(text="ok")],
        usage=FakeUsage(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=100,
            cache_read_input_tokens=0,
        ),
    )


def _aio(fake: FakeAnthropicClient) -> Any:
    """Return the fake's async-namespace client surface.

    Per US-007 the :class:`FakeAnthropicClient` exposes an ``.aio``
    namespace whose ``.messages.create`` / ``.messages.count_tokens`` are
    awaitable; the async orchestrator structurally consumes a client whose
    ``.messages.*`` are async, so we hand ``fake.aio`` (rather than
    ``fake`` itself) to ``call_llm_async``. Both surfaces drain the same
    expectation queue.
    """
    return fake.aio


@pytest.fixture(autouse=True)
def _deterministic_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``_async_sleep`` and ``_rand_uniform`` so retries run instantly
    and jitter is fully deterministic. Mirrors the sync sibling's fixture
    in ``test_client_retries.py``.
    """

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(client_module, "_async_sleep", _no_sleep)
    monkeypatch.setattr(client_module, "_rand_uniform", lambda _a, _b: 1.0)


# ---- Signature parity -----------------------------------------------------


def test_call_llm_async_signature_parity_with_call_llm() -> None:
    """``call_llm_async`` carries the exact same parameter surface as
    ``call_llm`` (modulo being a coroutine function).

    DEC-002 of #186: sibling async surface, smallest blast radius. Pin
    the parity at the parameter level — if a future change to the sync
    orchestrator's signature lands without updating the async sibling
    (or vice versa) this fails loud.
    """
    sync_sig = inspect.signature(call_llm)
    async_sig = inspect.signature(call_llm_async)

    # Parameters: every name, kind, default, and annotation must match.
    assert list(sync_sig.parameters) == list(async_sig.parameters), (
        "parameter NAME order diverged between call_llm and call_llm_async; "
        "DEC-002 of #186 requires 1:1 parity."
    )
    for name in sync_sig.parameters:
        sync_p = sync_sig.parameters[name]
        async_p = async_sig.parameters[name]
        assert sync_p.kind == async_p.kind, f"parameter {name!r} kind diverged"
        assert sync_p.default == async_p.default, f"parameter {name!r} default diverged"
        assert sync_p.annotation == async_p.annotation, f"parameter {name!r} annotation diverged"

    # ``call_llm_async`` must be a coroutine function; ``call_llm`` is not.
    assert inspect.iscoroutinefunction(call_llm_async)
    assert not inspect.iscoroutinefunction(call_llm)


# ---- Happy path -----------------------------------------------------------


@pytest.mark.asyncio
async def test_call_llm_async_happy_path_returns_llm_result() -> None:
    """One successful call returns an :class:`LLMResult` with usage + content.

    Drives the fake via its ``.aio`` namespace; the expectation queue
    populated by the sync ``expect_*`` helpers is shared with the async
    surface (DEC-012 of #186).
    """
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={"model": "claude-sonnet-4-6"}, returns=_ok_count())
    fake.expect_messages_create(matching={"model": "claude-sonnet-4-6"}, returns=_ok_message())

    result = await call_llm_async(
        system="sys",
        cached_block="x" * 100,
        dynamic_block="y" * 50,
        model="claude-sonnet-4-6",
        max_tokens=1024,
        prompt_version="v1",
        client=_aio(fake),
    )

    assert isinstance(result, LLMResult)
    assert result.text_blocks == ("ok",)
    assert result.response_text == "ok"
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.cache_creation_input_tokens == 100
    assert result.cache_read_input_tokens == 0
    assert result.model == "claude-sonnet-4-6"
    assert result.prompt_version == "v1"
    fake.assert_all_expectations_met()


# ---- 429 retry-with-success -----------------------------------------------


@pytest.mark.asyncio
async def test_call_llm_async_429_retry_then_success() -> None:
    """A 429 then a 200 returns normally — one retry consumed, one
    WARNING emitted, no exception propagated."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error())
    fake.expect_messages_create(matching={}, returns=_ok_message())

    result = await call_llm_async(
        system="sys",
        cached_block="c",
        dynamic_block="d",
        model="claude-sonnet-4-6",
        max_tokens=128,
        prompt_version="v1",
        client=_aio(fake),
    )
    assert result.response_text == "ok"
    fake.assert_all_expectations_met()


# ---- 5xx retry-with-success -----------------------------------------------


@pytest.mark.asyncio
async def test_call_llm_async_5xx_retry_then_success() -> None:
    """A 5xx then a 200 returns normally — proves the 5xx retry branch
    awaits ``_async_sleep`` and continues to the next attempt."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_status_error(503))
    fake.expect_messages_create(matching={}, returns=_ok_message())

    result = await call_llm_async(
        system="sys",
        cached_block="c",
        dynamic_block="d",
        model="claude-sonnet-4-6",
        max_tokens=128,
        prompt_version="v1",
        client=_aio(fake),
    )
    assert result.response_text == "ok"
    fake.assert_all_expectations_met()


# ---- Connection-error retry-with-success ----------------------------------


@pytest.mark.asyncio
async def test_call_llm_async_connection_error_retry_then_success() -> None:
    """A connection error then a 200 returns normally — proves the
    CONNECTION branch awaits ``_async_sleep`` and continues."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_connection_error())
    fake.expect_messages_create(matching={}, returns=_ok_message())

    result = await call_llm_async(
        system="sys",
        cached_block="c",
        dynamic_block="d",
        model="claude-sonnet-4-6",
        max_tokens=128,
        prompt_version="v1",
        client=_aio(fake),
    )
    assert result.response_text == "ok"
    fake.assert_all_expectations_met()


# ---- 429 exhausted --------------------------------------------------------


@pytest.mark.asyncio
async def test_call_llm_async_429_exhausted_raises_rate_limit_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Default ``max_retries_429=3``: four total attempts → exhausted.
    Three retry WARNINGs (not four — the budget-exceeded raise does not
    emit one)."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    for _ in range(4):
        fake.expect_messages_create(matching={}, returns=_rate_limit_error())

    with (
        caplog.at_level(logging.WARNING, logger="signalforge.llm.client"),
        pytest.raises(LLMRateLimitError) as exc_info,
    ):
        await call_llm_async(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=_aio(fake),
        )

    assert exc_info.value.attempts == 3
    assert isinstance(exc_info.value.cause, anthropic.RateLimitError)
    retry_warnings = [r for r in caplog.records if "retry attempt" in r.getMessage()]
    assert len(retry_warnings) == 3
    # The WARNING is lazy-format JSON — parse the body to assert the
    # per-class key is present, matching the sync sibling's contract.
    payload = json.loads(retry_warnings[0].getMessage().split(": ", 1)[1])
    assert set(payload).issuperset({"attempt", "delay", "error_class", "model"})
    assert {k for k in payload if k.startswith("class_attempt_")} == {"class_attempt_429"}
    fake.assert_all_expectations_met()


# ---- 5xx exhausted --------------------------------------------------------


@pytest.mark.asyncio
async def test_call_llm_async_5xx_exhausted_raises_server_error() -> None:
    """Default ``max_retries_5xx=1``: two total attempts → exhausted."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_status_error(500))
    fake.expect_messages_create(matching={}, returns=_status_error(503))

    with pytest.raises(LLMServerError) as exc_info:
        await call_llm_async(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=_aio(fake),
        )
    assert isinstance(exc_info.value.cause, anthropic.APIStatusError)
    fake.assert_all_expectations_met()


# ---- Connection exhausted -------------------------------------------------


@pytest.mark.asyncio
async def test_call_llm_async_connection_exhausted_raises_connection_error() -> None:
    """Default ``max_retries_conn=1``: two total attempts → exhausted."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_connection_error())
    fake.expect_messages_create(matching={}, returns=_connection_error())

    with pytest.raises(LLMConnectionError) as exc_info:
        await call_llm_async(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=_aio(fake),
        )
    assert isinstance(exc_info.value.cause, anthropic.APIConnectionError)
    fake.assert_all_expectations_met()


# ---- async backoff is actually awaited ------------------------------------


@pytest.mark.asyncio
async def test_call_llm_async_uses_async_sleep_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module-level ``_async_sleep`` alias is what the retry loop awaits
    (DEC-011 of #186 — load-bearing test-injection seam).

    Reassigns ``_async_sleep`` to a recording stub and asserts the delay
    values passed through match ``2**i * jitter``. Without this pin, a
    refactor that imported ``asyncio.sleep`` locally inside ``call_llm_async``
    would silently disable the alias and break parity with ``test_client_retries.py``'s
    ``_sleep`` injection pattern.
    """
    sleeps: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(client_module, "_async_sleep", _record_sleep)
    jitters = iter([0.75, 1.25])
    monkeypatch.setattr(
        client_module,
        "_rand_uniform",
        lambda _a, _b: next(jitters),
    )

    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error())
    fake.expect_messages_create(matching={}, returns=_rate_limit_error())
    fake.expect_messages_create(matching={}, returns=_ok_message())

    await call_llm_async(
        system="sys",
        cached_block="c",
        dynamic_block="d",
        model="claude-sonnet-4-6",
        max_tokens=128,
        prompt_version="v1",
        client=_aio(fake),
    )

    assert sleeps == [
        2**0 * 0.75,  # first retry: attempt index 0, jitter 0.75
        2**1 * 1.25,  # second retry: attempt index 1, jitter 1.25
    ]


# ---- Sync-only provider raises at orchestrator entry ----------------------


@pytest.mark.asyncio
async def test_call_llm_async_sync_only_provider_raises_capability_error() -> None:
    """A provider declaring ``supports_async=False`` raises
    :class:`LLMProviderAsyncUnsupportedError` at orchestrator entry —
    BEFORE any client construction or pre-send probe.

    Registers a one-off :class:`FakeNoCacheProvider` instance with
    ``supports_async=False`` under a dedicated registry key so the test
    doesn't poison the shared ``"fake-nocache"`` provider used by the
    neutrality tests; on teardown the key is removed from the registry
    by restoring the prior value (or popping if no prior). DEC-005 / DEC-006
    of #186.
    """
    sync_only_name = "fake-nocache-sync-only-v8j6"
    sync_only = FakeSyncOnlyNoCacheProvider()
    sync_only.name = sync_only_name
    register_provider(sync_only)
    try:
        # Double-check the registration actually carries the False flag —
        # guards against a future refactor that ignores the kwarg.
        assert provider_for(sync_only_name).supports_async is False

        with pytest.raises(LLMProviderAsyncUnsupportedError) as exc_info:
            await call_llm_async(
                system="sys",
                cached_block="c",
                dynamic_block="d",
                model="any",
                max_tokens=128,
                prompt_version="v1",
                provider=sync_only_name,
            )
        # The error message names the offending provider so the operator
        # can spot the misconfiguration without sniffing message text.
        assert sync_only_name in str(exc_info.value)
    finally:
        # Pop the test-only registration to keep cross-test state clean.
        # Cast to access the private _REGISTRY for cleanup; the registry
        # is module-level state by design (DEC-003 of #135).
        registry = cast(
            dict[str, Any],
            client_module.__dict__.get("_REGISTRY")
            or __import__("signalforge.llm.providers", fromlist=["_REGISTRY"])._REGISTRY,
        )
        registry.pop(sync_only_name, None)


# ---- supports_async=True with FakeNoCacheProvider drives the happy path ---


@pytest.mark.asyncio
async def test_call_llm_async_drives_fake_nocache_provider_async() -> None:
    """When ``supports_async=True`` (the FakeNoCacheProvider default) the
    orchestrator builds an async client via ``make_async_client()`` and
    awaits ``messages.create`` — proving the no-cache provider's async
    seam is reachable end-to-end without a registered SDK.

    This is the no-cache analogue of the happy-path test above: a
    minimal provider with neither prompt caching nor token counting
    exercises the orchestrator's degrade paths (DEC-008 of #135) on the
    async surface. Mirrors :mod:`tests.grade.test_provider_neutrality`
    intent for the call_llm_async path.
    """
    async_name = "fake-nocache-async-v8j6"
    async_provider = FakeNoCacheProvider(response_text="async-ok")
    async_provider.name = async_name
    register_provider(async_provider)
    try:
        result = await call_llm_async(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="any",
            max_tokens=128,
            prompt_version="v1",
            provider=async_name,
        )
        assert result.response_text == "async-ok"
        # No caching → both cache-token fields are 0 per DEC-008 of #135.
        assert result.cache_creation_input_tokens == 0
        assert result.cache_read_input_tokens == 0
    finally:
        registry = __import__("signalforge.llm.providers", fromlist=["_REGISTRY"])._REGISTRY
        registry.pop(async_name, None)


# ---- Module-level fixture cross-check -------------------------------------
# Belt-and-braces: prove the FAKE_NOCACHE_PROVIDER_NAME default registration
# (from import-time wiring elsewhere in the test suite) is NOT a sync-only
# provider; the capability-gate test above uses a one-off registration to
# avoid touching this default state.


def test_fake_nocache_provider_default_supports_async_true() -> None:
    """The pre-registered ``fake-nocache`` provider (registered by other
    test modules) inherits ``supports_async=True`` from the ABC default
    so it remains compatible with the async neutrality path. Pins the
    invariant so a refactor that flips the ABC default doesn't silently
    break neutrality tests; the sync-only sibling
    :class:`FakeSyncOnlyNoCacheProvider` is the explicit opt-out.
    """
    fresh = FakeNoCacheProvider()
    assert fresh.supports_async is True
    assert fresh.name == FAKE_NOCACHE_PROVIDER_NAME
    # The subclass demonstrates the opt-out shape; pin both sides so a
    # silent ABC-default flip is caught here.
    sync_only = FakeSyncOnlyNoCacheProvider()
    assert sync_only.supports_async is False
