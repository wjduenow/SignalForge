"""#136 US-001 DEC-010 — OpenAI SDK type-ignore confinement.

Every ``# type: ignore`` / ``# pyright: ignore`` line in the
``signalforge.llm`` tree that ALSO mentions "openai" must live ONLY in
``_openai_client.py`` — the one-shim-per-vendor SDK seam. Mirrors the
spirit of the Anthropic-SDK confinement scan (Scan 3 in
``tests/test_audit_completeness.py``) and the Snowflake-shaped per-file
line scan in ``tests/warehouse/test_snowflake_client_confinement.py``; a
simple file/line scan suffices here.

The companion Scan 9 in ``tests/test_audit_completeness.py`` enforces the
AST-level construction-call confinement (``openai.OpenAI(...)`` only in
the shim). This line-based scan is the cheap floor; Scan 9 is the
load-bearing AST one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_LLM_DIR = Path(__file__).resolve().parents[2] / "src" / "signalforge" / "llm"
_SHIM_FILENAME = "_openai_client.py"


def _openai_type_ignore_lines(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, text) for lines carrying an openai-mentioning
    type/pyright ignore directive.
    """
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        lowered = line.lower()
        has_ignore = "type: ignore" in lowered or "pyright: ignore" in lowered
        if has_ignore and "openai" in lowered:
            hits.append((lineno, line.strip()))
    return hits


def test_openai_type_ignores_only_in_shim() -> None:
    """No ``.py`` under ``signalforge/llm/`` other than
    ``_openai_client.py`` may carry an openai-mentioning type-ignore.
    """
    offenders: list[str] = []
    # ``rglob`` (not ``glob``) so nested modules under signalforge/llm/
    # are also scanned — PR #152 CodeRabbit catch: top-level-only glob
    # let openai-mentioning ignore directives in subpackages bypass the
    # guard (the package is flat today but a future subpackage would
    # silently un-confine the scan).
    for py in sorted(_LLM_DIR.rglob("*.py")):
        if py.name == _SHIM_FILENAME:
            continue
        for lineno, text in _openai_type_ignore_lines(py):
            offenders.append(f"{py.relative_to(_LLM_DIR)}:{lineno}: {text}")

    assert not offenders, (
        "openai SDK type-ignore must live only in "
        f"{_SHIM_FILENAME}, but found:\n" + "\n".join(offenders)
    )


def test_shim_actually_carries_openai_type_ignore() -> None:
    """Sanity: the shim itself DOES carry at least one openai-mentioning
    type-ignore. Without this, the confinement scan above could pass
    vacuously after a refactor that dropped the seam.
    """
    shim = _LLM_DIR / _SHIM_FILENAME
    assert _openai_type_ignore_lines(shim), (
        f"{_SHIM_FILENAME} should confine the openai SDK type-ignore; "
        "the confinement scan is only meaningful if the seam exists"
    )


# ---------------------------------------------------------------------------
# Coverage-closing tests for PR #152 codecov gaps on the shim internals.
# Confinement-test file is the natural home — these tests pin the per-shim
# behaviours that the production import surface depends on (the adapter
# façade + the tiktoken fallback) but that no production caller currently
# exercises in the default test set (the orchestrator drives via the
# FakeOpenAIClient, never through _OpenAIClientAdapter; tiktoken's fallback
# fires only on an unknown model id).
# ---------------------------------------------------------------------------


def test_openai_client_adapter_messages_create_delegates_to_chat_completions() -> None:
    """``_OpenAIClientAdapter.messages.create(**kwargs)`` MUST delegate
    verbatim to ``self._raw.chat.completions.create(**kwargs)``.

    The orchestrator's ``call_llm`` hard-calls
    ``llm_client.messages.create(...)``; the adapter is the only thing
    that maps that into OpenAI's actual SDK call shape. A regression
    that breaks the delegation (e.g. a refactor that swaps the SDK call
    path) would surface here, not in the integration tests (those use
    ``FakeOpenAIClient`` which has its own ``.messages.create`` and
    never goes through the adapter).
    """
    from types import SimpleNamespace
    from typing import Any

    from signalforge.llm._openai_client import _OpenAIClientAdapter

    captured: dict[str, Any] = {}
    sentinel = object()

    def _create(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    raw = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    adapter = _OpenAIClientAdapter(raw)

    result = adapter.messages.create(
        model="gpt-4o",
        max_tokens=128,
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_object"},
    )

    assert result is sentinel
    assert captured == {
        "model": "gpt-4o",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_object"},
    }


# ---------------------------------------------------------------------------
# Issue #186 (US-004) — async shim coverage
# ---------------------------------------------------------------------------


def test_make_openai_async_client_returns_protocol_satisfying_object() -> None:
    """The async factory returns an adapter exposing the ``messages``
    namespace with awaitable ``create`` / ``count_tokens`` (issue #186,
    US-004).

    Verified by constructing through the factory with a fake api_key
    (the SDK does not actually issue a network call at construction
    time) and checking the structural surface.
    """
    from signalforge.llm._openai_client import _make_openai_async_client

    client = _make_openai_async_client(api_key="test-key-not-real")
    assert hasattr(client, "messages")
    assert hasattr(client.messages, "create")
    assert hasattr(client.messages, "count_tokens")


def test_fake_satisfies_async_openai_client_protocol() -> None:
    """The shared :class:`FakeOpenAIClient` (US-007) satisfies
    :class:`AsyncOpenAIClientProtocol` structurally via its ``.aio``
    namespace — one fake drives sync (``call_llm``) AND async
    (``call_llm_async``) paths interchangeably (DEC-012 of #186).

    ``AsyncOpenAIClientProtocol`` is ``@runtime_checkable``; the
    ``aio`` namespace exposes ``messages.create`` /
    ``messages.count_tokens`` as coroutines, satisfying the protocol's
    structural shape.
    """
    from signalforge.llm._openai_client import AsyncOpenAIClientProtocol

    from ._fake_openai import FakeOpenAIClient

    fake = FakeOpenAIClient()
    aio = fake.aio
    # The async surface lives at ``client.aio`` (mirrors Anthropic and
    # Gemini's ``client.aio.*`` namespaces — DEC-012). ``isinstance``
    # over the runtime-checkable protocol confirms structural
    # conformance.
    assert isinstance(aio, AsyncOpenAIClientProtocol)
    assert hasattr(aio.messages, "create")
    assert hasattr(aio.messages, "count_tokens")


def test_async_openai_provider_make_async_client_returns_async_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """:meth:`OpenAIProvider.make_async_client` delegates to
    :func:`_make_openai_async_client` and returns an object exposing
    the async ``messages.create`` / ``messages.count_tokens`` surface
    (issue #186, US-004).

    The orchestrator narrows the returned ``object`` to
    :class:`signalforge.llm.client._LLMAsyncClientProtocol`; the
    structural duck-typed match against ``messages.create`` /
    ``messages.count_tokens`` is what makes the call-site type-check
    without leaking a vendor SDK type into the seam.

    The OpenAI SDK enforces credentials at construction time, so the
    test monkeypatches ``OPENAI_API_KEY`` rather than passing
    ``api_key=`` (the production ``make_async_client`` calls
    ``_make_openai_async_client()`` with no arguments — the env var is
    the standard injection point per the docstring).
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    from signalforge.llm.providers import OpenAIProvider

    provider = OpenAIProvider()
    client = provider.make_async_client()
    assert hasattr(client, "messages")
    assert hasattr(client.messages, "create")
    assert hasattr(client.messages, "count_tokens")


def test_openai_async_client_adapter_messages_create_delegates_to_chat_completions() -> None:
    """``_OpenAIAsyncClientAdapter.messages.create(**kwargs)`` MUST
    delegate verbatim to ``await self._raw.chat.completions.create(**kwargs)``.

    Mirrors :func:`test_openai_client_adapter_messages_create_delegates_to_chat_completions`
    for the async path. The orchestrator's ``call_llm_async`` (US-006)
    hard-calls ``await llm_client.messages.create(...)``; the async
    adapter is the only thing that maps that into the SDK's actual
    async call shape. A regression breaking the delegation (e.g. a
    refactor that swaps the SDK call path) would surface here, not in
    the integration tests (those use ``FakeOpenAIClient`` which has
    its own ``.aio.messages.create`` and never goes through the
    production adapter).

    Preserves the JSON-mode ``response_format`` kwarg (DEC-006 of #136)
    end-to-end.
    """
    import asyncio
    from types import SimpleNamespace
    from typing import Any

    from signalforge.llm._openai_client import _OpenAIAsyncClientAdapter

    captured: dict[str, Any] = {}
    sentinel = object()

    async def _create(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    raw = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    adapter = _OpenAIAsyncClientAdapter(raw)

    result = asyncio.run(
        adapter.messages.create(
            model="gpt-4o",
            max_tokens=128,
            messages=[{"role": "user", "content": "hi"}],
            response_format={"type": "json_object"},
        )
    )

    assert result is sentinel
    assert captured == {
        "model": "gpt-4o",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_object"},
    }


def test_count_openai_tokens_falls_back_to_cl100k_base_for_unknown_model() -> None:
    """``_count_openai_tokens`` MUST NOT raise on an unknown model id —
    DEC-012 of #136 documents the ``cl100k_base`` fallback so a
    newer-than-tiktoken OpenAI SKU still produces a usable estimate
    rather than crashing the ``--estimate`` flow. ``--estimate`` is a
    calibration signal, not a billing guarantee (mirrors the
    planner-estimate caveat in ``warehouse-adapters.md``).
    """
    from signalforge.llm._openai_client import _count_openai_tokens

    # A model id ``tiktoken.encoding_for_model`` doesn't recognise.
    # Must return a positive int via the cl100k_base fallback, NOT raise.
    count = _count_openai_tokens("not-a-real-openai-model-xyz", "hello world")
    assert isinstance(count, int)
    assert count > 0
