"""Provider tests driving :class:`OpenAIProvider` through the hand-rolled
:class:`FakeOpenAIClient` (#155 US-001).

Mirrors the shape of :mod:`tests.llm.test_gemini_provider_via_fake`. Pins
the :meth:`LLMProvider.is_clean_completion` contract for OpenAI per
#155 DEC-005: the clean-stop-reason set is exactly ``{stop}``. OpenAI's
``length`` (truncated at max_tokens) and other non-``stop`` reasons are
UNCLEAN — that's the load-bearing #155 fix (a truncated response would
otherwise produce a partial-JSON parse failure downstream).
"""

from __future__ import annotations

import pytest

from signalforge.llm import call_llm
from signalforge.llm.errors import LLMResponseFormatError
from signalforge.llm.providers import OpenAIProvider
from tests.llm._fake_openai import (
    FakeOpenAIChoice,
    FakeOpenAIClient,
    FakeOpenAICompletion,
    FakeOpenAIMessage,
    FakeOpenAIUsage,
)


def _ok_response(finish_reason: str = "stop") -> FakeOpenAICompletion:
    """Build a happy-path OpenAI response carrying one choice with the given
    ``finish_reason``. Default is ``stop`` (the canonical clean completion).
    Mirrors the convenience helper in
    :mod:`tests.llm.test_gemini_provider_via_fake`.
    """
    return FakeOpenAICompletion(
        choices=[
            FakeOpenAIChoice(
                message=FakeOpenAIMessage(content='{"score": 1.0}'),
                finish_reason=finish_reason,
            )
        ],
        usage=FakeOpenAIUsage(prompt_tokens=120, completion_tokens=45),
    )


# ---------------------------------------------------------------------------
# is_clean_completion — happy-path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_is_clean_completion_true_for_stop() -> None:
    """``finish_reason='stop'`` is the canonical OpenAI clean completion.

    The orchestrator's gate at ``call_llm`` (immediately before
    :meth:`OpenAIProvider.extract_text_blocks`) must let this response
    through to the text-extraction path; this happy-path pin asserts the
    gate evaluates to ``True``.
    """
    response = _ok_response(finish_reason="stop")
    assert OpenAIProvider().is_clean_completion(response) is True


# ---------------------------------------------------------------------------
# is_clean_completion — unclean paths (#155 US-002)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_is_clean_completion_false_for_length_with_partial_text() -> None:
    """``finish_reason='length'`` is UNCLEAN even when partial text is
    present (#155 DEC-001/DEC-002, OpenAI-side analogue of the Gemini
    Finding-1 regression).

    A truncated-at-max_tokens response from OpenAI carries a non-empty
    ``message.content`` string that is mid-string. Before #155, the only
    post-call gate was :meth:`OpenAIProvider.extract_text_blocks`, which
    only raised when the message had no content — so truncation with
    partial text silently slipped through, reached the JSON parser, and
    surfaced as the wrong typed degrade (``GradeOutputError`` instead
    of ``GradeLLMError``). The :meth:`is_clean_completion` gate raises
    the floor: any non-clean ``finish_reason`` routes to
    :class:`LLMResponseFormatError` regardless of whether partial text
    was emitted.
    """
    response = FakeOpenAICompletion(
        choices=[
            FakeOpenAIChoice(
                message=FakeOpenAIMessage(
                    content='{"score": 0.9, "reasoning": "partial truncated',
                ),
                finish_reason="length",
            )
        ],
        usage=FakeOpenAIUsage(prompt_tokens=120, completion_tokens=45),
    )
    assert OpenAIProvider().is_clean_completion(response) is False


@pytest.mark.unit
@pytest.mark.llm
def test_is_clean_completion_false_for_content_filter() -> None:
    """``finish_reason='content_filter'`` is UNCLEAN (#155 DEC-002).

    OpenAI surfaces a moderation-blocked generation via
    ``finish_reason='content_filter'``; the response is structurally
    incomplete (the model was prevented from finishing) and routes
    through the typed degrade.
    """
    response = _ok_response(finish_reason="content_filter")
    assert OpenAIProvider().is_clean_completion(response) is False


@pytest.mark.unit
@pytest.mark.llm
def test_is_clean_completion_false_for_tool_calls() -> None:
    """``finish_reason='tool_calls'`` is UNCLEAN in v0.3 (#155 DEC-002,
    mirrors Anthropic's ``tool_use`` exclusion per DEC-006).

    The clean set is exactly ``{stop}``. ``tool_calls`` is deliberately
    excluded: the codebase doesn't use tools today, so a ``tool_calls``
    response would signal system-prompt drift or unexpected LLM behaviour.
    """
    response = _ok_response(finish_reason="tool_calls")
    assert OpenAIProvider().is_clean_completion(response) is False


@pytest.mark.unit
@pytest.mark.llm
def test_unclean_finish_reason_message_names_finish_reason_field() -> None:
    """:meth:`unclean_finish_reason_message` renders an operator-facing
    diagnostic that names the vendor-native ``finish_reason`` field and
    quotes the actual unclean value (#155 DEC-007).

    Vendor-accurate naming (``finish_reason`` for OpenAI vs
    ``stop_reason`` for Anthropic) is why DEC-007 made this a
    provider-override rather than a shared default.
    """
    response = _ok_response(finish_reason="length")
    message = OpenAIProvider().unclean_finish_reason_message(response)
    assert "finish_reason" in message
    assert "'length'" in message


# ---------------------------------------------------------------------------
# is_clean_completion — orchestrator wire-in (#155 US-001 / DEC-005)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_call_llm_openai_length_with_partial_text_raises_at_is_clean_gate() -> None:
    """A ``finish_reason='length'`` response that CARRIES partial text must
    surface :class:`LLMResponseFormatError` from the
    :meth:`OpenAIProvider.is_clean_completion` gate inside ``call_llm``
    (orchestrator wire-in pin for OpenAI — analogous to the Gemini
    MAX_TOKENS pin in ``test_gemini_provider_via_fake.py``).

    Pre-#155 there was no per-provider check beyond ``extract_text_blocks``,
    which happily returns partial truncated text for OpenAI ``length`` (the
    ``message.content`` is just a non-empty string). Post-fix the wire-in
    runs ``is_clean_completion`` AHEAD of ``extract_text_blocks``, so
    ``length`` routes to ``LLMResponseFormatError`` here (and via the
    grade-engine wrap → ``GradeLLMError``).

    Companion pin: the equivalent regression for Anthropic (``stop_reason
    = "max_tokens"``) is at
    ``tests/llm/test_client.py::test_call_llm_raises_llmresponseformaterror_on_unclean_stop_reason``;
    for Gemini at
    ``tests/llm/test_gemini_provider_via_fake.py::test_call_llm_gemini_max_tokens_with_partial_text_raises_at_is_clean_gate``.
    A regression that re-ordered the gate after ``extract_text_blocks``
    would let truncated OpenAI responses slip through — this test catches
    that at unit cost rather than at live e2e cost.
    """
    fake = FakeOpenAIClient()
    truncated = FakeOpenAICompletion(
        choices=[
            FakeOpenAIChoice(
                message=FakeOpenAIMessage(content='{"score": 0.9, "reasoning": "partial trunc'),
                finish_reason="length",
            )
        ],
        usage=FakeOpenAIUsage(prompt_tokens=120, completion_tokens=512),
    )
    fake.expect_messages_create(matching={}, returns=truncated)

    with pytest.raises(LLMResponseFormatError) as excinfo:
        call_llm(
            system="SYS",
            cached_block="CACHED",
            dynamic_block="DYN",
            model="gpt-4o",
            max_tokens=512,
            prompt_version="v1",
            provider="openai",
            client=fake,
        )

    assert "finish_reason" in str(excinfo.value)
    assert "'length'" in str(excinfo.value)
    # Exactly one create call — no retry, no leak through to extract_text_blocks.
    assert len(fake.create_calls) == 1
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# is_clean_completion — defensive raise on malformed SDK response (#155 QG / codecov)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_is_clean_completion_raises_on_missing_or_empty_choices() -> None:
    """A response object lacking ``choices`` (or with an empty list)
    raises :class:`LLMResponseFormatError` naming the missing field.

    Guards against an SDK shape regression. The conservative-degrade path
    would otherwise silently swallow a structural surprise rather than
    raising loudly. Pinned at unit cost.
    """
    from types import SimpleNamespace

    # Missing attribute entirely.
    no_attr = SimpleNamespace()
    with pytest.raises(LLMResponseFormatError, match="choices"):
        OpenAIProvider().is_clean_completion(no_attr)

    # Present but empty list.
    empty = SimpleNamespace(choices=[])
    with pytest.raises(LLMResponseFormatError, match="choices"):
        OpenAIProvider().is_clean_completion(empty)


@pytest.mark.unit
@pytest.mark.llm
def test_is_clean_completion_raises_on_missing_finish_reason_on_first_choice() -> None:
    """A response with ``choices[0]`` present but missing ``finish_reason``
    raises :class:`LLMResponseFormatError` naming the missing field.

    Guards against an SDK shape regression where the choice object exists
    but the finish-reason field disappears (e.g. a vendor adds a new
    response variant where the field is conditional). Pinned at unit cost.
    """
    from types import SimpleNamespace

    response = SimpleNamespace(choices=[SimpleNamespace()])  # no finish_reason
    with pytest.raises(LLMResponseFormatError, match="finish_reason"):
        OpenAIProvider().is_clean_completion(response)
