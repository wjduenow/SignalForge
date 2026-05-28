"""Centralised Google Gemini SDK seam (#137 US-001 / DEC-001).

US-001 establishes the single shim where every ``# pyright: ignore[...]`` and
``# type: ignore[...]`` provoked by the ``google-genai`` SDK is allowed to
live. Mirrors the precedent set by :mod:`signalforge.llm._anthropic_client`
(Anthropic) and :mod:`signalforge.warehouse.adapters._snowflake_client`
(Snowflake) — one shim per vendor, no SDK ignores leaking into sibling
modules.

The rule is encoded in two gates:

* **AST gate.** The
  ``test_gemini_client_construction_only_in_llm_client_shim`` scan in
  :mod:`tests.test_audit_completeness` rejects any ``genai.Client(...)``
  constructed outside ``src/signalforge/llm/_gemini_client.py``.
* **Line gate.** :mod:`tests.llm.test_gemini_client_confinement` rejects any
  ``# type: ignore`` / ``# pyright: ignore`` that mentions ``google.genai`` /
  ``genai`` in any module under ``src/signalforge/llm/`` other than this one.

Three responsibilities (mirrors the Anthropic shim's three):

* :class:`GeminiClientProtocol` — duck-typed surface common to a real
  ``google.genai.Client`` and the hand-rolled test fake (US-004). Narrow on
  purpose — only the methods :func:`signalforge.llm.client.call_llm` actually
  consumes. The protocol exposes ``.messages.create`` as the orchestrator-side
  façade; the real SDK's native surface is ``.models.generate_content``, so the
  shim adapts the call shape in US-002 (where :class:`GeminiProvider` lives).
  For US-001 the protocol declares the façade attribute only — the adapter
  body lands with the provider.
* :func:`_make_gemini_client` — factory that returns ``genai.Client(api_key=...)``.
  Lazy-imports the SDK so test environments that inject a fake never pay the
  import cost, AND so a base install without the ``[gemini]`` extra still
  imports this module cleanly.
* :func:`_load_gemini_exception_classes` — bundles the SDK exception classes
  the :func:`signalforge.llm.client.call_llm` retry loop catches. Empty-tuple
  fallback when ``google.genai`` is not installed (DEC-015) so a base install
  routes every exception to :attr:`ExceptionCategory.NO_RETRY` cleanly.

The shim deliberately does NOT define ``__repr__`` on the protocols — same
reason the Anthropic shim doesn't: avoids accidentally rendering client state
(API key, internal HTTP session, etc.) in tracebacks or logs. Observability
discipline (no logger calls) also mirrors the Anthropic and Snowflake shims;
logging lives in the seam where the stage label is known.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class _GeminiMessagesProtocol(Protocol):
    """Duck-typed surface of the ``.messages`` façade on the shim's client.

    The Google ``google-genai`` SDK exposes generation natively as
    ``client.models.generate_content(model=..., contents=..., config=...)``,
    but :func:`signalforge.llm.client.call_llm` is provider-neutral and calls
    ``client.messages.create(**kwargs)`` regardless of vendor. The shim
    (US-002, :class:`signalforge.llm.providers.GeminiProvider`) adapts the
    one-shape-to-the-other internally; this protocol pins the orchestrator-
    facing façade.

    The signature is intentionally permissive — the real SDK accepts a large
    kwargs surface and the fake (US-004) only cares about the subset
    :func:`call_llm` passes through :meth:`GeminiProvider.build_create_kwargs`.
    """

    def create(self, **kwargs: Any) -> Any: ...


@runtime_checkable
class _GeminiModelsProtocol(Protocol):
    """Duck-typed surface of the SDK's native ``.models`` namespace.

    ``client.models.count_tokens(model=..., contents=...)`` is the path
    :meth:`signalforge.llm.providers.GeminiProvider.estimate_input_tokens`
    (US-007) calls via the shim helper. Declared on the protocol for
    completeness — the orchestrator does NOT call this directly; only the
    estimate path threads through.

    The native ``models.generate_content(...)`` is intentionally absent: the
    orchestrator calls ``client.messages.create``, and the shim is the
    only piece that ever touches ``models.generate_content``. Keeping it off
    the protocol stops a future caller from reaching past the façade.
    """

    def count_tokens(self, **kwargs: Any) -> Any: ...


@runtime_checkable
class GeminiClientProtocol(Protocol):
    """Duck-typed surface common to a real ``genai.Client`` and the test fake.

    Both production (``google.genai.Client``) and test
    (``tests/llm/_fake_gemini.py::FakeGeminiClient``, US-004) clients satisfy
    this protocol, so :func:`signalforge.llm.client.call_llm` calls the same
    method signatures regardless of which client was injected. The protocol
    is intentionally narrow — only the surface :func:`call_llm` and the
    ``--estimate`` token-counter (US-007) actually consume.

    Re-exported as :data:`signalforge.llm.GeminiClientProtocol` so the
    ``client`` kwarg on :func:`signalforge.draft.draft_schema` and
    :func:`signalforge.grade.grade_artifacts` can be type-annotated without
    importing a private underscore-prefixed name — same convention as
    :data:`signalforge.llm.AnthropicClientProtocol` (#5 issue #44).
    """

    messages: _GeminiMessagesProtocol
    models: _GeminiModelsProtocol


def _make_gemini_client(
    api_key: str | None = None,
) -> GeminiClientProtocol:  # pragma: no cover - exercised by live tests only
    """Construct a real ``google.genai.Client``.

    ``api_key=None`` lets the SDK consume the standard ``GOOGLE_API_KEY``
    (or ``GEMINI_API_KEY``, depending on SDK version) environment variable;
    explicit values are preserved for callers that thread credentials through
    configuration (DEC-008).

    The ``google.genai`` import is lazy so test environments that inject a
    fake never pay the SDK import cost AND so importing this module does not
    require the ``[gemini]`` optional extra to be installed (DEC-015) — a
    base install can construct :class:`signalforge.llm.providers.GeminiProvider`
    objects via the registry but :func:`_make_gemini_client` will raise
    ``ImportError`` if invoked without the extra, which surfaces to the
    operator as a clear setup error.

    The returned object satisfies :class:`GeminiClientProtocol` structurally;
    the cast through ``Any`` is the one place ``google.genai``-typed values
    enter the package, confined here per DEC-001.

    NOTE: this returns the real SDK ``Client`` directly. The
    ``.messages.create`` façade lives on the orchestrator path via the
    provider's :meth:`build_create_kwargs` + the shim adapter that lands
    with US-002 — the bare ``genai.Client`` does not expose ``.messages`` at
    all. US-002 is responsible for wiring the façade (either via a thin
    wrapper class here or via the provider's request-shape adaptation).
    """
    from google import genai  # type: ignore[import-not-found]

    return genai.Client(api_key=api_key)  # type: ignore[no-any-return]


@dataclass(frozen=True)
class _GeminiExceptionClasses:
    """Bundle of SDK exception classes used by the retry loop in
    :func:`signalforge.llm.client.call_llm`.

    Each tuple is the ``except`` clause's catch surface for one branch of
    the retry taxonomy (DEC-006). Wrapping them in a frozen dataclass keeps
    the seam's import surface narrow and confines every Gemini-SDK
    ``# type: ignore`` to this module (DEC-001).

    The four-bucket shape mirrors :class:`_AnthropicExceptionClasses`
    verbatim so :meth:`signalforge.llm.providers.GeminiProvider.classify_exception`
    can structurally match the Anthropic provider's classification shape;
    the orchestrator's per-class budgets (``max_retries_429`` /
    ``max_retries_5xx`` / ``max_retries_conn``) apply unchanged.

    The actual SDK class identities live in :func:`_load_gemini_exception_classes`
    — DEC-006 names ``google.genai.errors.ClientError`` (HTTP 401/403 → AUTH;
    429 → RATE_LIMIT) and ``google.genai.errors.ServerError`` (5xx) as the
    base classes. The precise SDK class names and status-code attribute name
    (``code`` vs ``status_code``) are verified against the installed
    ``google-genai`` SDK at US-002 implementation time, when
    :meth:`GeminiProvider.classify_exception` lands and the offline
    exception-mapper tests pin the shape.
    """

    rate_limit: tuple[type[BaseException], ...]
    api_status: tuple[type[BaseException], ...]
    auth: tuple[type[BaseException], ...]
    connection: tuple[type[BaseException], ...]


def _load_gemini_exception_classes() -> _GeminiExceptionClasses:
    """Lazy-import the SDK exception classes the retry loop catches.

    Returning empty tuples on ``ImportError`` is DEC-015 — a base install
    without the ``[gemini]`` optional extra still imports this module
    cleanly; :meth:`GeminiProvider.classify_exception` then routes every
    exception to :attr:`ExceptionCategory.NO_RETRY` rather than crashing
    at module import.

    The classification surface in DEC-006 is:

    * ``google.genai.errors.ClientError`` carrying HTTP 401 / 403 → AUTH.
    * ``google.genai.errors.ClientError`` carrying HTTP 429 → RATE_LIMIT.
    * ``google.genai.errors.ServerError`` (5xx family) → SERVER_ERROR.
    * Connection-flavoured (``httpx.ConnectError`` / ``httpx.TimeoutException``
      or the SDK-wrapped equivalent — verified at US-002) → CONNECTION.
    * Anything else → NO_RETRY.

    The split between ``ClientError`` (4xx) and ``ServerError`` (5xx) is the
    same shape Anthropic uses (``RateLimitError`` / ``APIStatusError``); the
    HTTP-status disambiguation (401/403 vs 429 inside ``ClientError``) lives
    in :meth:`GeminiProvider.classify_exception` (US-002), not here.
    """
    try:
        from google.genai import errors as genai_errors  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - exercised only when the [gemini] extra is absent
        empty: tuple[type[BaseException], ...] = ()
        return _GeminiExceptionClasses(
            rate_limit=empty, api_status=empty, auth=empty, connection=empty
        )
    # DEC-006: ``ClientError`` covers both AUTH (401/403) and RATE_LIMIT
    # (429); the HTTP-status disambiguation happens in
    # ``GeminiProvider.classify_exception`` (US-002). Listing the same class
    # under two buckets is intentional — the orchestrator's branching is
    # driven by category, not by ``isinstance`` of a single bucket.
    client_error: tuple[type[BaseException], ...] = (genai_errors.ClientError,)
    server_error: tuple[type[BaseException], ...] = (genai_errors.ServerError,)
    # Connection-flavoured exceptions are not part of the SDK's typed surface;
    # they leak through from the underlying HTTP stack. The orchestrator's
    # connection-error retry budget catches them via category routing in
    # ``GeminiProvider.classify_exception`` (US-002), where the precise
    # ``httpx`` / SDK-wrapped class names are pinned against the installed
    # SDK. Leaving the bucket empty here keeps the shim free of an extra
    # transitive dependency surface.
    connection: tuple[type[BaseException], ...] = ()
    return _GeminiExceptionClasses(
        rate_limit=client_error,
        api_status=server_error,
        auth=client_error,
        connection=connection,
    )


__all__ = [
    "GeminiClientProtocol",
    "_GeminiExceptionClasses",
    "_GeminiMessagesProtocol",
    "_GeminiModelsProtocol",
    "_load_gemini_exception_classes",
    "_make_gemini_client",
]
