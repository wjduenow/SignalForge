"""End-to-end provider tests driving :class:`GeminiProvider` through the
hand-rolled :class:`FakeGeminiClient` (#137 US-004, DEC-011).

These tests prove the integration shape — :meth:`GeminiProvider.make_client`
delegates to the shim's ``_make_gemini_client`` then wraps the bare SDK
client in the ``.messages`` façade adapter (DEC-001/004); and
:func:`signalforge.llm.client.call_llm` with ``provider="gemini"`` plus an
injected :class:`FakeGeminiClient` routes a full
``messages.create``-and-response round-trip through the fake. The
per-method unit tests for :class:`GeminiProvider` (build kwargs, extract
text/usage, classify exceptions) live in
:mod:`tests.llm.test_providers`; the AC #2 neutrality end-to-end across
``grade_artifacts`` / ``draft_schema`` is US-005's territory.

Pattern mirrors the Anthropic retry suite (:mod:`tests.llm.test_client_retries`),
including the ``_sleep`` / ``_rand_uniform`` reassignment for deterministic
backoff. SDK exception classes (``google.genai.errors.*``) are lazy-imported
inside each test that needs them (mirrors Snowflake's ``_sfe()`` pattern in
``warehouse-adapters.md``).
"""

from __future__ import annotations

from typing import Any

import pytest

import signalforge.llm.client as client_module
from signalforge.llm.client import call_llm
from signalforge.llm.errors import LLMRateLimitError, LLMResponseFormatError
from tests.llm._fake_gemini import (
    FakeGeminiCandidate,
    FakeGeminiClient,
    FakeGeminiContent,
    FakeGeminiPart,
    FakeGeminiResponse,
    FakeGeminiUsageMetadata,
)


def _ok_response(text: str = '{"score": 1.0}') -> FakeGeminiResponse:
    """Build a happy-path Gemini response carrying one JSON-shaped text part.

    Default payload is a one-line JSON literal because the grader/drafter
    parse the response as JSON; tests asserting on text-block extraction
    can override.
    """
    return FakeGeminiResponse(
        candidates=[
            FakeGeminiCandidate(
                content=FakeGeminiContent(parts=[FakeGeminiPart(text=text)]),
                finish_reason="STOP",
            )
        ],
        usage_metadata=FakeGeminiUsageMetadata(prompt_token_count=120, candidates_token_count=45),
    )


def _safety_blocked_response() -> FakeGeminiResponse:
    """Build a Gemini response that is safety-blocked: a candidate with no
    content and ``finish_reason='SAFETY'``. The orchestrator's
    :meth:`GeminiProvider.extract_text_blocks` raises
    :class:`LLMResponseFormatError` whose message names ``SAFETY``."""
    return FakeGeminiResponse(
        candidates=[FakeGeminiCandidate(content=None, finish_reason="SAFETY")],
        usage_metadata=FakeGeminiUsageMetadata(prompt_token_count=120, candidates_token_count=0),
    )


# ---------------------------------------------------------------------------
# make_client — shim delegation + façade wrap
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_provider_make_client_calls_shim_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """:meth:`GeminiProvider.make_client` delegates to the shim's
    ``_make_gemini_client`` (DEC-001), then wraps the bare SDK client in the
    ``.messages`` façade adapter (DEC-004). The shim factory is the single
    SDK construction seam; tests confirm it is reached and its return value
    is what the adapter holds."""
    import signalforge.llm._gemini_client as shim
    from signalforge.llm.providers import GeminiProvider

    call_counter = {"n": 0}

    class _RawClient:
        class _Models:
            def generate_content(self, **kwargs: Any) -> str:
                return f"forwarded:{kwargs.get('model')}"

            def count_tokens(self, **kwargs: Any) -> str:
                return f"count:{kwargs.get('model')}"

        models = _Models()

    raw_instance = _RawClient()

    def _factory() -> Any:
        call_counter["n"] += 1
        return raw_instance

    monkeypatch.setattr(shim, "_make_gemini_client", _factory)
    client: Any = GeminiProvider().make_client()

    # The shim factory was called exactly once.
    assert call_counter["n"] == 1
    # The adapter exposes the .messages façade...
    assert hasattr(client, "messages")
    # ...routes .messages.create through the SDK's models.generate_content...
    assert client.messages.create(model="gemini-2.5-flash") == "forwarded:gemini-2.5-flash"
    # ...and preserves the native .models surface for the US-007 estimator.
    assert client.models is raw_instance.models


# ---------------------------------------------------------------------------
# call_llm round-trip through the fake
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_call_llm_routes_through_provider_into_fake_client() -> None:
    """End-to-end: :func:`call_llm` with ``provider='gemini'`` and an
    injected :class:`FakeGeminiClient` issues exactly one
    ``messages.create`` call (no ``count_tokens`` — the provider declares
    ``supports_token_count=False``), and the kwargs carry the Gemini shape:

    * ``response_mime_type='application/json'`` on ``config`` (DEC-018), and
    * no ``cache_control`` marker anywhere in the request body
      (capability flags both False ⇒ orchestrator suppresses caching).

    The resulting :class:`LLMResult` reports both cache-token fields as 0
    (DEC-008 ⇒ orchestrator zeroes them for a no-caching provider) and
    surfaces the response text the fake returned."""
    fake = FakeGeminiClient()
    fake.expect_messages_create(matching={}, returns=_ok_response(text='{"score": 0.9}'))

    result = call_llm(
        system="SYS",
        cached_block="CACHED",
        dynamic_block="DYN",
        model="gemini-2.5-flash",
        max_tokens=512,
        prompt_version="v1",
        provider="gemini",
        client=fake,
    )

    # Result shape: text, cache fields, token usage all carried through.
    assert result.response_text == '{"score": 0.9}'
    assert result.text_blocks == ('{"score": 0.9}',)
    assert result.cache_creation_input_tokens == 0
    assert result.cache_read_input_tokens == 0
    assert result.input_tokens == 120
    assert result.output_tokens == 45
    assert result.model == "gemini-2.5-flash"
    assert result.prompt_version == "v1"

    # Exactly one create call; no count_tokens (the fake's count_tokens
    # raises, so any orchestrator regression that called it would have
    # already failed the test).
    assert len(fake.create_calls) == 1
    create_kwargs = fake.create_calls[0]

    # Gemini-specific kwargs shape (DEC-004/DEC-018).
    assert create_kwargs["model"] == "gemini-2.5-flash"
    config = create_kwargs["config"]
    assert config["response_mime_type"] == "application/json"
    assert config["system_instruction"] == "SYS"
    assert config["max_output_tokens"] == 512

    # No cache marker or beta header anywhere — the orchestrator must NOT
    # build a caching shape for a provider whose capability flag is False.
    # Inspect every nested value, not just the top-level keys, because the
    # marker historically appeared inside a nested ``messages[0].content[0]``.
    assert "cache_control" not in repr(create_kwargs)
    assert "extra_headers" not in create_kwargs

    fake.assert_all_expectations_met()


@pytest.mark.unit
@pytest.mark.llm
def test_call_llm_gemini_safety_blocked_raises_llmresponseformaterror() -> None:
    """A safety-blocked response (no candidate yields any text part)
    surfaces :class:`LLMResponseFormatError` whose message names the
    ``finish_reason``. Drives the typed-error path through ``call_llm``
    rather than calling ``GeminiProvider.extract_text_blocks`` directly —
    proves the orchestrator does NOT swallow this error and that no retry
    is attempted (response-shape errors are not in the retry taxonomy)."""
    fake = FakeGeminiClient()
    fake.expect_messages_create(matching={}, returns=_safety_blocked_response())

    with pytest.raises(LLMResponseFormatError) as excinfo:
        call_llm(
            system="SYS",
            cached_block="CACHED",
            dynamic_block="DYN",
            model="gemini-2.5-flash",
            max_tokens=512,
            prompt_version="v1",
            provider="gemini",
            client=fake,
        )

    assert "SAFETY" in str(excinfo.value)
    # No retry was attempted — exactly one create call.
    assert len(fake.create_calls) == 1
    fake.assert_all_expectations_met()


def test_call_llm_gemini_max_tokens_with_partial_text_raises_at_is_clean_gate() -> None:
    """A ``finish_reason="MAX_TOKENS"`` response that CARRIES partial text
    must still surface :class:`LLMResponseFormatError` from the new
    :meth:`GeminiProvider.is_clean_completion` gate — the load-bearing
    Finding-1 orchestrator pin (#155 US-001/US-003).

    The pre-existing safety-blocked test above exercises the SAFETY path
    where no text is collected at all. THIS test exercises the
    MAX_TOKENS-with-partial-text path: the response has a non-empty
    ``parts[0].text`` (typical of mid-string truncation), and pre-fix
    ``extract_text_blocks`` would return that partial JSON happily,
    sending it downstream to ``parse_grade_response`` which would raise
    ``GradeOutputError(violation_type="json_parse")``. The post-fix
    orchestrator wire-in runs ``is_clean_completion`` AHEAD of
    ``extract_text_blocks``, so MAX_TOKENS routes to ``LLMResponseFormatError``
    here (and via the grade-engine wrap → ``GradeLLMError``).

    A regression that re-ordered the gate after ``extract_text_blocks``
    would silently let partial-text MAX_TOKENS responses slip back through
    — this test catches that at unit-test cost rather than at live-e2e
    cost (`test_e2e_gemini_smoke.py` would also fail invariant #6 in that
    case, but at $0.02/run instead of zero).
    """
    fake = FakeGeminiClient()
    truncated = FakeGeminiResponse(
        candidates=[
            FakeGeminiCandidate(
                content=FakeGeminiContent(
                    parts=[FakeGeminiPart(text='{"score": 0.9, "reasoning": "partial trunc')]
                ),
                finish_reason="MAX_TOKENS",
            )
        ],
        usage_metadata=FakeGeminiUsageMetadata(prompt_token_count=120, candidates_token_count=512),
    )
    fake.expect_messages_create(matching={}, returns=truncated)

    with pytest.raises(LLMResponseFormatError) as excinfo:
        call_llm(
            system="SYS",
            cached_block="CACHED",
            dynamic_block="DYN",
            model="gemini-2.5-flash",
            max_tokens=512,
            prompt_version="v1",
            provider="gemini",
            client=fake,
        )

    assert "MAX_TOKENS" in str(excinfo.value)
    # Exactly one create call — no retry, no leak through to extract_text_blocks.
    assert len(fake.create_calls) == 1
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# is_clean_completion — happy-path (#155 US-001)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_is_clean_completion_true_for_stop() -> None:
    """``finish_reason.name == 'STOP'`` is the canonical Gemini clean
    completion (#155 DEC-005/DEC-006).

    The orchestrator's gate at ``call_llm`` (immediately before
    :meth:`GeminiProvider.extract_text_blocks`) must let this response
    through to the text-extraction path; this happy-path pin asserts the
    gate evaluates to ``True``. Gemini's enum-typed ``finish_reason``
    surface — read via ``.name`` to dodge the enum value/identity question
    — is the load-bearing semantic that the #155 fix promotes from "only
    raise on zero text parts" to "raise on any non-clean stop reason."
    """
    from signalforge.llm.providers import GeminiProvider

    response = _ok_response(text='{"score": 1.0}')
    assert GeminiProvider().is_clean_completion(response) is True


# ---------------------------------------------------------------------------
# is_clean_completion — unclean paths (#155 US-002)
#
# The MAX_TOKENS-with-partial-text test below is the LOAD-BEARING #155
# Finding 1 regression. Before the fix, ``GeminiProvider.extract_text_blocks``
# only raised ``LLMResponseFormatError`` when ZERO text parts were collected
# (providers.py:867-897 pre-fix). A ``finish_reason='MAX_TOKENS'`` response
# that produced a partial (truncated mid-string) text part silently returned,
# the truncated JSON reached ``parse_grade_response``, and the grade engine
# wrapped the resulting ``GradeOutputError(violation_type="json_parse")`` as
# a degraded result with ``reasoning="call failed: GradeOutputError"`` —
# masking the actionable typed degrade (``"call failed: GradeLLMError"``)
# that llm-drafter.md § "Gemini provider shape" DEC-005 of #137 contracts.
# The #155 fix promotes the gate to ``is_clean_completion`` which fires on
# ANY non-clean ``finish_reason`` regardless of whether partial text was
# emitted, routing every truncation/safety/recitation case uniformly through
# the ``LLMResponseFormatError`` → ``GradeLLMError`` degrade path.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_is_clean_completion_false_for_max_tokens_with_partial_text() -> None:
    """``finish_reason.name == 'MAX_TOKENS'`` with a non-empty partial text
    part is UNCLEAN — the LOAD-BEARING #155 Finding 1 regression pin.

    See the section header above for the full bug history. The test
    constructs a Gemini response that carries a real (mid-string) text
    part AND ``finish_reason=MAX_TOKENS`` — exactly the shape that
    silently slipped through pre-fix. The fix routes it through
    :class:`LLMResponseFormatError`; this pin asserts the gate fires.
    """
    from signalforge.llm.providers import GeminiProvider

    response = FakeGeminiResponse(
        candidates=[
            FakeGeminiCandidate(
                content=FakeGeminiContent(
                    parts=[
                        FakeGeminiPart(
                            text='{"score": 0.9, "reasoning": "partial truncated',
                        )
                    ]
                ),
                finish_reason="MAX_TOKENS",
            )
        ],
        usage_metadata=FakeGeminiUsageMetadata(prompt_token_count=120, candidates_token_count=2048),
    )
    assert GeminiProvider().is_clean_completion(response) is False


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.parametrize("finish_reason", ["SAFETY", "RECITATION", "OTHER"])
def test_is_clean_completion_false_for_non_stop_reasons(finish_reason: str) -> None:
    """Non-``STOP`` finish reasons are UNCLEAN (#155 DEC-002).

    The clean set is exactly ``{STOP}``. Gemini's documented non-clean
    finish-reasons — ``SAFETY`` (content blocked by safety filter),
    ``RECITATION`` (model produced verbatim training-data snippet), and
    ``OTHER`` (catch-all bucket the SDK uses for filter mechanisms not
    enumerated above) — all route through the typed degrade. The
    ``MAX_TOKENS`` case has its own dedicated test above because it is
    the load-bearing #155 Finding 1 regression.
    """
    from signalforge.llm.providers import GeminiProvider

    # Build a response with the partial-text shape — proves the gate
    # raises regardless of whether content was emitted.
    response = FakeGeminiResponse(
        candidates=[
            FakeGeminiCandidate(
                content=FakeGeminiContent(parts=[FakeGeminiPart(text="partial")]),
                finish_reason=finish_reason,
            )
        ],
        usage_metadata=FakeGeminiUsageMetadata(prompt_token_count=120, candidates_token_count=10),
    )
    assert GeminiProvider().is_clean_completion(response) is False


@pytest.mark.unit
@pytest.mark.llm
def test_unclean_finish_reason_message_names_finish_reason_field() -> None:
    """:meth:`unclean_finish_reason_message` renders an operator-facing
    diagnostic that names the vendor-native ``finish_reason`` field and
    quotes the actual unclean value (#155 DEC-007).

    Vendor-accurate naming (``finish_reason`` for Gemini vs
    ``stop_reason`` for Anthropic) is why DEC-007 made this a
    provider-override rather than a shared default.
    """
    from signalforge.llm.providers import GeminiProvider

    response = FakeGeminiResponse(
        candidates=[
            FakeGeminiCandidate(
                content=FakeGeminiContent(parts=[FakeGeminiPart(text="partial")]),
                finish_reason="MAX_TOKENS",
            )
        ],
        usage_metadata=FakeGeminiUsageMetadata(prompt_token_count=120, candidates_token_count=10),
    )
    message = GeminiProvider().unclean_finish_reason_message(response)
    assert "finish_reason" in message
    assert "'MAX_TOKENS'" in message


@pytest.mark.unit
@pytest.mark.llm
def test_call_llm_gemini_retry_429_exhaustion_routes_to_llmratelimiterror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeating 429 (Gemini's ``ClientError(code=429)``) exhausts the
    orchestrator's retry budget and surfaces as
    :class:`LLMRateLimitError`. Proves the Gemini exception classifier is
    wired through :func:`call_llm`'s retry loop and that the per-class
    budget (``max_retries_429``) is honoured. Backoff is pinned via the
    module-level ``_sleep`` / ``_rand_uniform`` aliases (mirrors
    :mod:`tests.llm.test_client_retries` precedent)."""
    # Pin backoff to instant + deterministic delays (DEC-004).
    monkeypatch.setattr(client_module, "_sleep", lambda _delay: None)
    monkeypatch.setattr(client_module, "_rand_uniform", lambda _a, _b: 1.0)

    # Lazy-import the SDK error classes per-test so the assertions key on
    # the same class identity the mapper sees at call time (mirrors
    # warehouse-adapters.md's ``_sfe()`` pattern).
    from google.genai import errors as genai_errors  # noqa: PLC0415

    # Build a real ClientError carrying the 429 HTTP code via the SDK's
    # constructor — the same shape ``GeminiProvider.classify_exception``
    # reads on ``.code``. Mirrors ``_make_requests_response`` in
    # tests/llm/test_providers.py.
    def _err_429() -> BaseException:
        import json as _json  # noqa: PLC0415

        import requests  # noqa: PLC0415

        response = requests.Response()
        response.status_code = 429
        response._content = _json.dumps(  # type: ignore[attr-defined]
            {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "x"}}
        ).encode("utf-8")
        return genai_errors.ClientError(429, response)

    # ``max_retries_429=3`` ⇒ 1 initial + 3 retries = 4 total failures.
    fake = FakeGeminiClient()
    for _ in range(4):
        fake.expect_messages_create(matching={}, returns=_err_429())

    with pytest.raises(LLMRateLimitError) as excinfo:
        call_llm(
            system="SYS",
            cached_block="CACHED",
            dynamic_block="DYN",
            model="gemini-2.5-flash",
            max_tokens=512,
            prompt_version="v1",
            max_retries_429=3,
            provider="gemini",
            client=fake,
        )

    assert excinfo.value.attempts == 3
    assert isinstance(excinfo.value.cause, genai_errors.ClientError)
    fake.assert_all_expectations_met()
