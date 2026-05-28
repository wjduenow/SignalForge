"""Centralised OpenAI SDK seam (#136 DEC-010 confinement).

US-001 of #136 establishes the single shim where every
``# pyright: ignore[...]`` / ``# type: ignore[...]`` provoked by the ``openai``
SDK is allowed to live. The rest of :mod:`signalforge.llm` and all of
:mod:`signalforge.draft` / :mod:`signalforge.grade` import the typed surface
this module exposes and stay pyright-clean.

Mirrors the precedent established by
:mod:`signalforge.llm._anthropic_client` for the Anthropic SDK (see
``.claude/rules/llm-drafter.md`` — "One SDK seam — `signalforge.llm._anthropic_client`
confines every ``# pyright: ignore`` (DEC-012)") and
:mod:`signalforge.warehouse.adapters._snowflake_client` for the
``snowflake-connector-python`` SDK. When a future v0.x LLM provider is added
(e.g. #137 Gemini), it should get its own ``_<vendor>_client.py`` shim under
:mod:`signalforge.llm` for the same reason; do not pool SDK ignores into a
generic util module.

Three responsibilities:

* :class:`OpenAIClientProtocol` — duck-typed surface common to the real
  OpenAI client (wrapped behind :class:`_OpenAIClientAdapter` to expose the
  ``.messages`` namespace the orchestrator hard-calls) and the test fake
  (lands in US-003). Narrow on purpose — only the methods
  :func:`signalforge.llm.client.call_llm` actually consumes.
* :func:`_make_openai_client` — factory that returns
  ``_OpenAIClientAdapter(openai.OpenAI(api_key=api_key))``. Lazy-imports the
  SDK so test environments that inject a fake never pay the import cost.
  The adapter wraps the underlying OpenAI client to expose ``.messages.create``
  (delegating to ``chat.completions.create``) — DEC-009.
* :func:`_count_openai_tokens` — local token counter via ``tiktoken``,
  with ``cl100k_base`` fallback for unknown model ids (DEC-012). The
  ``supports_token_count=False`` capability flag on ``OpenAIProvider``
  means the orchestrator skips its pre-send count gate; this helper is
  used by the ``--estimate`` path (US-005), not the runtime retry loop.

OpenAI Chat Completions API surface (DEC-001):

* ``client.chat.completions.create(model=..., max_tokens=..., messages=[...],
  response_format={"type": "json_object"})``
* Response: ``response.choices[0].message.content`` is the assistant text;
  ``response.usage.prompt_tokens`` / ``response.usage.completion_tokens``
  are the token counts (no cache fields — OpenAI has no equivalent cache
  discount, so ``UsageMetrics.cache_*_input_tokens`` is always ``0`` for
  this provider; matches ``supports_prompt_caching=False``).

Observability discipline (mirroring DEC-027 from the warehouse layer): no
logger calls in this shim. Logging lives in the seam
(:mod:`signalforge.llm.client`) where the stage label is known. The shim
itself is structural plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class _OpenAIMessagesProtocol(Protocol):
    """Duck-typed surface of the ``messages`` namespace on the adapter.

    The OpenAI Python SDK exposes ``client.chat.completions.create(...)``
    rather than ``client.messages.create(...)``; the
    :class:`_OpenAIClientAdapter` returned by :func:`_make_openai_client`
    wraps the real client to expose a :attr:`messages` namespace whose
    :meth:`create` delegates to ``chat.completions.create``. This protocol
    types that namespace so :func:`signalforge.llm.client.call_llm` calls
    the same method signatures regardless of which provider is wired.

    :meth:`count_tokens` raises :class:`NotImplementedError` because
    OpenAI has no equivalent of Anthropic's pre-send count API; the
    orchestrator gates the pre-send count call on
    ``supports_token_count=True`` and so never invokes this method for an
    ``OpenAIProvider`` (capability flag is ``False``). The method is
    declared on the protocol for structural parity with the Anthropic
    shim, and the adapter raises defensively if it is ever called.
    """

    def create(self, **kwargs: Any) -> Any: ...

    def count_tokens(self, **kwargs: Any) -> Any: ...


@runtime_checkable
class OpenAIClientProtocol(Protocol):
    """Duck-typed surface common to the OpenAI adapter and the test fake.

    Both production (the :class:`_OpenAIClientAdapter` wrapper returned by
    :func:`_make_openai_client`) and test
    (``tests/llm/_fake_openai.py::FakeOpenAIClient``, US-003) clients
    satisfy this protocol, so :func:`signalforge.llm.client.call_llm` can
    call the same method signatures regardless of which client was
    injected. The protocol is intentionally narrow — only the surface
    :func:`call_llm` actually consumes (``messages.create``;
    ``messages.count_tokens`` is gated off by
    ``supports_token_count=False`` but declared for protocol parity with
    the Anthropic shim).

    Re-exported as ``signalforge.llm.OpenAIClientProtocol`` in US-002 so
    the ``client`` kwarg on :func:`signalforge.draft.draft_schema` and
    :func:`signalforge.grade.grade_artifacts` can be type-annotated
    without importing a private underscore-prefixed name.
    """

    messages: _OpenAIMessagesProtocol


class _OpenAIClientAdapter:
    """Wrap an ``openai.OpenAI`` client to expose a ``.messages`` namespace.

    The orchestrator at :func:`signalforge.llm.client.call_llm` hard-calls
    ``llm_client.messages.create(**kwargs)``. The OpenAI SDK exposes
    ``client.chat.completions.create(...)`` instead, so the adapter
    rebinds the surface: :attr:`messages` is a
    :class:`types.SimpleNamespace` with a :meth:`create` callable that
    delegates to ``self._raw.chat.completions.create`` and a
    :meth:`count_tokens` callable that raises
    :class:`NotImplementedError` (orchestrator never calls it for a
    ``supports_token_count=False`` provider, per DEC-011 of #136 — but
    raising is the honest behaviour if the gate ever drifts).

    The ``_raw`` reference is kept on the adapter so future code paths
    (e.g. structured-outputs API, streaming) can reach through if needed
    without re-importing the SDK.

    Construction goes only through :func:`_make_openai_client` — this
    class is internal plumbing for the shim.
    """

    def __init__(self, raw_client: Any) -> None:
        self._raw = raw_client
        self.messages = SimpleNamespace(
            create=self._messages_create,
            count_tokens=self._messages_count_tokens,
        )

    def _messages_create(self, **kwargs: Any) -> Any:
        """Delegate to ``self._raw.chat.completions.create(**kwargs)``.

        The kwargs dict is OpenAI-native (``model``, ``max_tokens``,
        ``messages`` list of ``{role, content}`` dicts,
        ``response_format``); the caller in
        :meth:`OpenAIProvider.build_create_kwargs` (US-002) is
        responsible for shaping the payload.
        """
        return self._raw.chat.completions.create(**kwargs)

    def _messages_count_tokens(self, **kwargs: Any) -> Any:  # pragma: no cover - defensive
        """Defensive: orchestrator never calls this for OpenAI.

        ``OpenAIProvider.supports_token_count`` is ``False``, so
        :func:`signalforge.llm.client.call_llm` skips its pre-send
        count-tokens gate entirely for this provider (DEC-008 of #135).
        Raising here surfaces a regression where the gate drifts and the
        orchestrator starts calling this method against an OpenAI client
        — better a loud :class:`NotImplementedError` than a silent
        fabricated zero (DEC-011 of #136).
        """
        raise NotImplementedError(
            "OpenAI provider does not support pre-send count_tokens; "
            "supports_token_count=False gates this call off in the "
            "orchestrator. If you see this, the capability-flag gate has drifted."
        )


def _make_openai_client(
    api_key: str | None = None,
) -> OpenAIClientProtocol:  # pragma: no cover - exercised by integration tests only
    """Construct a real ``openai.OpenAI`` client wrapped in the adapter.

    ``api_key=None`` lets the SDK consume the ``OPENAI_API_KEY``
    environment variable (standard SDK behaviour); explicit values are
    preserved for callers that thread credentials through configuration.

    The ``openai`` import is lazy so test environments that inject a
    fake never pay the SDK import cost, and so a base install without
    the ``[openai]`` extra does not crash at module-import time
    (DEC-014 of #136 — :func:`_load_openai_exception_classes` returns
    empty tuples on ``ImportError``; this factory raises naturally on
    import-time failure because a caller who reached this path *did*
    ask for a real client).
    """
    import openai  # type: ignore[import-not-found]

    return _OpenAIClientAdapter(openai.OpenAI(api_key=api_key))  # type: ignore[no-any-return]


@dataclass(frozen=True)
class _OpenAIExceptionClasses:
    """Bundle of SDK exception classes used by the retry loop in
    :func:`signalforge.llm.client.call_llm`.

    Each tuple is the ``except`` clause's catch surface for one branch
    of the retry taxonomy (DEC-004 of #5, generalised by #135's
    provider-neutral seam). Wrapping them in a frozen dataclass keeps
    the seam's import surface narrow and confines every OpenAI-SDK
    ``# type: ignore`` to this module (DEC-010 of #136).

    The :class:`OpenAIProvider.classify_exception` impl (US-002) uses
    this bundle to route a caught SDK exception to the right
    :class:`signalforge.llm.providers.ExceptionCategory`.
    """

    rate_limit: tuple[type[BaseException], ...]
    api_status: tuple[type[BaseException], ...]
    auth: tuple[type[BaseException], ...]
    connection: tuple[type[BaseException], ...]


def _load_openai_exception_classes() -> _OpenAIExceptionClasses:
    """Lazy-import the SDK exception classes the retry loop catches.

    Returning empty tuples on ``ImportError`` is a defensive fallback
    for base installs that did NOT pull the ``[openai]`` extra (DEC-014
    of #136 — mirrors :func:`_load_anthropic_exception_classes`'s
    ``pragma: no cover`` branch). With empty tuples, every caught
    exception in the retry loop routes to ``NO_RETRY`` cleanly — the
    operator never reaches this path in practice because the provider
    registry validator at config load rejects ``provider: openai``
    when the registration also fails import-time, but graceful
    fallback at import-time is the contract.
    """
    try:
        import openai  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - the SDK is optional via [openai]
        empty: tuple[type[BaseException], ...] = ()
        return _OpenAIExceptionClasses(
            rate_limit=empty, api_status=empty, auth=empty, connection=empty
        )
    return _OpenAIExceptionClasses(
        rate_limit=(openai.RateLimitError,),
        api_status=(openai.APIStatusError,),
        auth=(openai.AuthenticationError, openai.PermissionDeniedError),
        connection=(openai.APIConnectionError,),
    )


def _count_openai_tokens(model: str, text: str) -> int:
    """Count tokens for ``text`` under ``model`` using ``tiktoken`` (DEC-012).

    Used by the ``--estimate`` path (US-005) to render a pre-send
    grader/drafter cost preview without an API call (mirrors the
    Anthropic ``messages.count_tokens`` path; OpenAI has no equivalent
    server-side pre-send count so we count locally).

    Falls back to the ``cl100k_base`` encoding if
    ``tiktoken.encoding_for_model(model)`` raises ``KeyError`` for an
    unknown model id — ``--estimate`` is a calibration signal, not a
    billing guarantee (mirrors the planner-estimate caveats in
    ``warehouse-adapters.md`` § "estimate_query_bytes graduation"). The
    fallback is loud at the logger seam (US-005), silent here in the
    shim per the no-logger discipline.

    The ``tiktoken`` import is lazy so a base install without the
    ``[openai]`` extra does not crash this module at import time
    (DEC-012 of #136 — ``tiktoken`` ships with ``openai`` in the
    ``[openai]`` extra).
    """
    import tiktoken  # type: ignore[import-not-found]

    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))


__all__ = [
    "OpenAIClientProtocol",
    "_OpenAIClientAdapter",
    "_OpenAIExceptionClasses",
    "_OpenAIMessagesProtocol",
    "_count_openai_tokens",
    "_load_openai_exception_classes",
    "_make_openai_client",
]
