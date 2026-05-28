"""Test-only no-cache LLM provider — the AC #2 provider-neutrality proof.

US-005 of issue #135 (provider-neutral LLM seam). This module is the literal
demonstration of DEC-011: wiring a brand-new LLM provider takes only a small
:class:`signalforge.llm.providers.LLMProvider` subclass (its client shim, its
request-kwargs builder, text/usage extraction, and an exception → category map)
plus a :func:`signalforge.llm.providers.register_provider` call — nothing else.

The provider here declares ``supports_prompt_caching = False`` and
``supports_token_count = False`` so it exercises the orchestrator's capability
degrade paths (DEC-008):

* No ``count_tokens`` call is ever issued by ``call_llm`` (the fake client
  raises loudly if one is attempted — proving the gate holds).
* No ``cache_control`` marker and no extended-cache beta header is built (the
  orchestrator gates this, and the provider also never emits one).
* The reported cache-token counts are 0, and the dual-zero cache-anomaly
  WARNING is suppressed.

Lives under ``tests/`` and is NEVER imported from production code (mirrors
``tests/llm/_fake.py`` / ``tests/warehouse/_fake.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from signalforge.llm.providers import (
    ExceptionCategory,
    LLMProvider,
    UsageMetrics,
)

#: The registry key the neutrality test selects via ``GradeConfig(provider=...)``.
FAKE_NOCACHE_PROVIDER_NAME = "fake-nocache"


@dataclass
class FakeNoCacheUsage:
    """Usage object carrying NO cache fields (a no-cache provider omits them)."""

    input_tokens: int = 100
    output_tokens: int = 40


@dataclass
class FakeNoCacheResponse:
    """Canned ``messages.create`` response for the no-cache fake client.

    ``text`` is the single content payload; ``usage`` carries only the
    required input/output token counts and no cache accounting.
    """

    text: str
    usage: FakeNoCacheUsage = field(default_factory=FakeNoCacheUsage)
    model: str = "fake-nocache-judge"


@dataclass
class _FakeNoCacheMessages:
    """The ``.messages`` namespace on the no-cache fake client.

    ``create`` records every kwargs dict so the neutrality test can assert no
    ``cache_control`` marker / beta header was built. ``count_tokens`` raises:
    because the provider declares ``supports_token_count = False`` the
    orchestrator must never call it — if it does, this raise turns the silent
    gating regression into a loud failure.
    """

    response_text: str
    create_calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> FakeNoCacheResponse:
        self.create_calls.append(kwargs)
        return FakeNoCacheResponse(text=self.response_text)

    def count_tokens(self, **kwargs: Any) -> Any:
        raise AssertionError(
            "count_tokens must never be called for a provider with supports_token_count=False"
        )


class FakeNoCacheClient:
    """Minimal client returned by :meth:`FakeNoCacheProvider.make_client`.

    Structurally satisfies the orchestrator's neutral ``.messages`` surface
    (``create`` / ``count_tokens``). Tests can also construct one directly and
    inject it via ``call_llm(..., client=...)`` to inspect ``create_calls``.
    """

    def __init__(self, response_text: str = '{"ok": true}') -> None:
        self._messages = _FakeNoCacheMessages(response_text=response_text)
        self.messages = self._messages

    @property
    def create_calls(self) -> list[dict[str, Any]]:
        """The kwargs dicts passed to ``messages.create`` (inspector for tests)."""
        return list(self._messages.create_calls)


class FakeNoCacheProvider(LLMProvider):
    """A test-only :class:`LLMProvider` with neither caching nor token counting.

    This whole class IS the AC #2 wiring proof: register an instance and the
    seam accepts it everywhere — ``provider_for(name)`` resolves it,
    ``GradeConfig(provider=name)`` validates, and ``call_llm`` drives it with no
    other code change.

    The default ``response_text`` is a grade-judge JSON payload so the class can
    be registered and driven through ``grade_artifacts`` with no extra setup;
    callers wanting a per-(criterion, artifact) response can build their own
    :class:`FakeNoCacheClient` and inject it.
    """

    name = FAKE_NOCACHE_PROVIDER_NAME
    supports_prompt_caching = False
    supports_token_count = False

    def __init__(self, response_text: str = '{"ok": true}') -> None:
        self._response_text = response_text

    def make_client(self) -> object:
        """Build the tiny canned-response client (no SDK, no network)."""
        return FakeNoCacheClient(response_text=self._response_text)

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
        """Build a minimal create-kwargs dict.

        Because the provider does NOT support prompt caching, it NEVER emits a
        ``cache_control`` marker or an extended-cache beta header regardless of
        ``cache_marker_active`` (the orchestrator already resolves that flag to
        ``False`` for a non-caching provider — this is belt-and-braces). The two
        blocks are concatenated into one plain message payload.
        """
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": cached_block},
                        {"type": "text", "text": dynamic_block},
                    ],
                }
            ],
        }

    def build_count_tokens_kwargs(
        self,
        *,
        system: str,
        cached_block: str,
        model: str,
    ) -> dict[str, Any]:
        """Never invoked — ``supports_token_count`` is ``False``.

        The orchestrator skips the pre-send count gate entirely for a provider
        that cannot count tokens (DEC-008), so this method is unreachable on the
        ``call_llm`` path. It raises to make any accidental call loud.
        """
        raise NotImplementedError(
            "build_count_tokens_kwargs is unreachable when supports_token_count=False"
        )

    def extract_text_blocks(self, response: object) -> tuple[str, ...]:
        """Pull the single text payload off the canned response."""
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            raise AssertionError("FakeNoCacheResponse is missing a string `text`.")
        return (text,)

    def extract_usage(self, response: object) -> UsageMetrics:
        """Return :class:`UsageMetrics` with both cache-token fields at 0.

        A no-cache provider has nothing to report for cache creation/read; the
        :class:`UsageMetrics` defaults already pin them to 0 (DEC-002), and the
        orchestrator reports 0 too (DEC-008).
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            raise AssertionError("FakeNoCacheResponse is missing `usage`.")
        return UsageMetrics(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )

    def classify_exception(self, exc: BaseException) -> ExceptionCategory:
        """Minimal exception → category map satisfying the ABC.

        Maps :class:`TimeoutError` to :attr:`ExceptionCategory.CONNECTION` (a
        plausible transient) and everything else to
        :attr:`ExceptionCategory.NO_RETRY`. The canned client never raises on
        the happy path, so this is exercised only if a future test injects a
        failing client.
        """
        if isinstance(exc, TimeoutError):
            return ExceptionCategory.CONNECTION
        return ExceptionCategory.NO_RETRY

    def estimate_input_tokens(
        self,
        model: str,
        text: str,
        *,
        system: str = "",
        client: object | None = None,
    ) -> int:
        """Return a trivial word-count proxy for ``system + text`` (#136 US-005).

        The neutrality test exercises orchestrator dispatch, not real
        token counting; ``len(text.split())`` is a deterministic
        non-zero positive answer that any rendered estimate will treat
        as valid. Mirrors :class:`FakeNoCacheProvider`'s overall posture
        of declaring the right capability flags and answering each ABC
        method with a minimal honest value.
        """
        del model, client  # neither is consulted on the proxy path
        # Join with a delimiter so the last word of ``system`` and the
        # first word of ``text`` don't merge into a single token under
        # ``.split()`` (boundary-word undercount; PR #152 CodeRabbit
        # catch).
        return len(f"{system} {text}".split())


__all__ = [
    "FAKE_NOCACHE_PROVIDER_NAME",
    "FakeNoCacheClient",
    "FakeNoCacheProvider",
    "FakeNoCacheResponse",
    "FakeNoCacheUsage",
]
