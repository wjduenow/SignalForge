"""Centralised Anthropic SDK seam (DEC-012 confinement).

US-005 establishes the single shim where every ``# pyright: ignore[...]`` and
``# type: ignore[...]`` provoked by ``anthropic`` SDK type-stub gaps is allowed
to live. The rest of :mod:`signalforge.llm` and all of :mod:`signalforge.draft`
import the typed surface this module exposes and stay pyright-clean.

Mirrors the precedent established by
:mod:`signalforge.warehouse.adapters._client` for the BigQuery SDK (see
``.claude/rules/warehouse-adapters.md`` — "_client.py contains every #
pyright: ignore"). When a future v0.2 LLM provider is added, it should get its
own ``_<vendor>_client.py`` shim under ``signalforge.llm`` for the same reason;
do not pool SDK ignores into a generic util module.

Two responsibilities:

* :class:`AnthropicClientProtocol` — duck-typed surface common to
  ``anthropic.Anthropic`` and ``tests/llm/_fake.py::FakeAnthropicClient``
  (lands in US-006). Narrow on purpose — only the methods
  :func:`signalforge.llm.client.call_llm` actually consumes.
  Re-exported as ``signalforge.llm.AnthropicClientProtocol`` so the
  ``client`` kwarg on ``draft_schema`` / ``grade_artifacts`` and
  downstream library callers can type-annotate against the public name
  (issue #44).
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

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class _AnthropicMessagesProtocol(Protocol):
    """Duck-typed surface of the ``messages`` namespace on the SDK client.

    The Anthropic Python SDK exposes :meth:`create` and :meth:`count_tokens`
    directly on ``client.messages`` (verified against the installed SDK at
    US-005 time). The signatures are intentionally permissive — the real SDK
    accepts a large kwargs surface and the fake (US-006) only cares about
    the subset :func:`signalforge.llm.client.call_llm` passes.
    """

    def create(self, **kwargs: Any) -> Any: ...

    def count_tokens(self, **kwargs: Any) -> Any: ...


@runtime_checkable
class AnthropicClientProtocol(Protocol):
    """Duck-typed surface common to ``anthropic.Anthropic`` and the test fake.

    Both production (``anthropic.Anthropic``) and test
    (``tests/llm/_fake.py::FakeAnthropicClient``, US-006) clients satisfy
    this protocol, so :func:`signalforge.llm.client.call_llm` calls the
    same method signatures regardless of which client was injected. The
    protocol is intentionally narrow — only the surface
    :func:`call_llm` actually consumes (``messages.create``,
    ``messages.count_tokens``).

    Re-exported as ``signalforge.llm.AnthropicClientProtocol`` (issue #44)
    so the ``client`` kwarg on :func:`signalforge.draft.draft_schema` and
    :func:`signalforge.grade.grade_artifacts` can be type-annotated without
    importing a private underscore-prefixed name.
    """

    messages: _AnthropicMessagesProtocol


def _make_anthropic_client(
    api_key: str | None = None,
) -> AnthropicClientProtocol:  # pragma: no cover - exercised by integration tests only
    """Construct a real ``anthropic.Anthropic`` client.

    ``api_key=None`` lets the SDK consume the ``ANTHROPIC_API_KEY``
    environment variable (standard SDK behaviour); explicit values are
    preserved for callers that thread credentials through configuration.

    The ``anthropic`` import is lazy so test environments that inject a
    fake never pay the SDK import cost.
    """
    import anthropic  # type: ignore[import-not-found]

    return anthropic.Anthropic(api_key=api_key)  # type: ignore[no-any-return]


@dataclass(frozen=True)
class _AnthropicExceptionClasses:
    """Bundle of SDK exception classes used by the retry loop in
    :func:`signalforge.llm.client.call_llm`.

    Each tuple is the ``except`` clause's catch surface for one branch
    of the retry taxonomy (DEC-004). Wrapping them in a frozen dataclass
    keeps the seam's import surface narrow and confines every Anthropic-
    SDK ``# type: ignore`` to this module (DEC-012).
    """

    rate_limit: tuple[type[BaseException], ...]
    api_status: tuple[type[BaseException], ...]
    auth: tuple[type[BaseException], ...]
    connection: tuple[type[BaseException], ...]


def _load_anthropic_exception_classes() -> _AnthropicExceptionClasses:
    """Lazy-import the SDK exception classes the retry loop catches.

    Returning empty tuples on ``ImportError`` is a defensive fallback
    only; ``anthropic`` is a hard dependency of the package. Lazy import
    keeps tests that never reach the retry branch from paying the SDK
    import cost.
    """
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - the SDK is a hard dep
        empty: tuple[type[BaseException], ...] = ()
        return _AnthropicExceptionClasses(
            rate_limit=empty, api_status=empty, auth=empty, connection=empty
        )
    return _AnthropicExceptionClasses(
        rate_limit=(anthropic.RateLimitError,),
        api_status=(anthropic.APIStatusError,),
        auth=(anthropic.AuthenticationError, anthropic.PermissionDeniedError),
        connection=(anthropic.APIConnectionError,),
    )


__all__ = [
    "AnthropicClientProtocol",
    "_AnthropicExceptionClasses",
    "_AnthropicMessagesProtocol",
    "_load_anthropic_exception_classes",
    "_make_anthropic_client",
]
