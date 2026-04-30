"""Centralised Anthropic SDK seam (DEC-012 confinement).

US-005 establishes the single shim where every ``# pyright: ignore[...]`` and
``# type: ignore[...]`` provoked by ``anthropic`` SDK type-stub gaps is allowed
to live. The rest of :mod:`signalforge.llm` and all of :mod:`signalforge.draft`
import the typed surface this module exposes and stay pyright-clean.

Mirrors the precedent established by
:mod:`signalforge.warehouse.adapters._client` for the BigQuery SDK (see
``.claude/rules/warehouse-adapters.md`` — "_client.py contains every #
pyright: ignore"). When a future v0.2 LLM provider is added, it should get its
own ``_client.py`` shim under ``signalforge.llm`` for the same reason; do not
pool SDK ignores into a generic util module.

Two responsibilities:

* :class:`_AnthropicClientProtocol` — duck-typed surface common to
  ``anthropic.Anthropic`` and ``tests/llm/_fake.py::FakeAnthropicClient``
  (lands in US-006). Narrow on purpose — only the methods
  :func:`signalforge.llm.client.call_anthropic` actually consumes.
* :func:`_make_anthropic_client` — factory that returns
  ``anthropic.Anthropic(api_key=api_key)``. Lazy-imports the SDK so test
  environments that inject a fake never pay the import cost.

Observability discipline (mirroring DEC-027 from the warehouse layer): no
logger calls in this shim. Logging lives in the seam (US-006) where the
stage label is known. The shim itself is structural plumbing.

The protocol is structural; we deliberately do not define ``__repr__`` to
avoid accidentally rendering client state (auth headers, internal HTTP
session, etc.) in tracebacks or logs.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class _AnthropicMessagesProtocol(Protocol):
    """Duck-typed surface of the ``messages`` namespace on the SDK client.

    The Anthropic Python SDK exposes :meth:`create` and :meth:`count_tokens`
    directly on ``client.messages`` (verified against the installed SDK at
    US-005 time). The signatures are intentionally permissive — the real SDK
    accepts a large kwargs surface and the fake (US-006) only cares about
    the subset :func:`signalforge.llm.client.call_anthropic` passes.
    """

    def create(self, **kwargs: Any) -> Any: ...

    def count_tokens(self, **kwargs: Any) -> Any: ...


@runtime_checkable
class _AnthropicClientProtocol(Protocol):
    """Duck-typed surface common to ``anthropic.Anthropic`` and the test fake.

    Both production (``anthropic.Anthropic``) and test
    (``tests/llm/_fake.py::FakeAnthropicClient``, US-006) clients satisfy
    this protocol, so :func:`signalforge.llm.client.call_anthropic` calls the
    same method signatures regardless of which client was injected. The
    protocol is intentionally narrow — only the surface
    :func:`call_anthropic` actually consumes (``messages.create``,
    ``messages.count_tokens``).
    """

    messages: _AnthropicMessagesProtocol


def _make_anthropic_client(
    api_key: str | None = None,
) -> _AnthropicClientProtocol:  # pragma: no cover - exercised by integration tests only
    """Construct a real ``anthropic.Anthropic`` client.

    ``api_key=None`` lets the SDK consume the ``ANTHROPIC_API_KEY``
    environment variable (standard SDK behaviour); explicit values are
    preserved for callers that thread credentials through configuration.

    The ``anthropic`` import is lazy so test environments that inject a
    fake never pay the SDK import cost.
    """
    import anthropic  # type: ignore[import-not-found]

    return anthropic.Anthropic(api_key=api_key)  # type: ignore[no-any-return]


__all__ = [
    "_AnthropicClientProtocol",
    "_AnthropicMessagesProtocol",
    "_make_anthropic_client",
]
