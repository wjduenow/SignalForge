"""Provider tests driving :class:`AnthropicProvider` through the hand-rolled
:class:`FakeAnthropicClient` (#155 US-001).

Mirrors the shape of :mod:`tests.llm.test_gemini_provider_via_fake`. Pins
the :meth:`LLMProvider.is_clean_completion` contract for Anthropic per
#155 DEC-005/DEC-006: the clean-stop-reason set is exactly
``{end_turn, stop_sequence}``. ``tool_use`` is deliberately UNCLEAN in
v0.3 — the codebase doesn't use tools today, so a ``tool_use`` response
would signal system-prompt drift; the clean set expands deliberately
when tool-use intentionally lands.
"""

from __future__ import annotations

import pytest

from signalforge.llm.providers import AnthropicProvider
from tests.llm._fake import FakeMessage, FakeTextBlock, FakeUsage


def _ok_response(stop_reason: str = "end_turn") -> FakeMessage:
    """Build a happy-path Anthropic response carrying one text block and the
    given ``stop_reason``. Default is ``end_turn`` (the most common clean
    completion). Mirrors the convenience helper in
    :mod:`tests.llm.test_gemini_provider_via_fake`.
    """
    return FakeMessage(
        content=[FakeTextBlock(text='{"score": 1.0}')],
        usage=FakeUsage(input_tokens=120, output_tokens=45),
        stop_reason=stop_reason,
    )


# ---------------------------------------------------------------------------
# is_clean_completion — happy-path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_is_clean_completion_true_for_end_turn() -> None:
    """``stop_reason='end_turn'`` is the canonical clean completion (DEC-006).

    The orchestrator's gate at ``call_llm`` (immediately before
    :meth:`AnthropicProvider.extract_text_blocks`) must let this response
    through to the text-extraction path; this happy-path pin asserts the
    gate evaluates to ``True``.
    """
    response = _ok_response(stop_reason="end_turn")
    assert AnthropicProvider().is_clean_completion(response) is True


@pytest.mark.unit
@pytest.mark.llm
def test_is_clean_completion_true_for_stop_sequence() -> None:
    """``stop_reason='stop_sequence'`` is also a clean completion (DEC-006).

    The clean set per #155 DEC-006 is ``{end_turn, stop_sequence}``.
    A stop-sequence terminated generation is a normal, fully-emitted
    response (the model hit a configured halt token) and the gate must
    let it through.
    """
    response = _ok_response(stop_reason="stop_sequence")
    assert AnthropicProvider().is_clean_completion(response) is True


# ---------------------------------------------------------------------------
# is_clean_completion — unclean paths (#155 US-002)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
def test_is_clean_completion_false_for_max_tokens_with_partial_text() -> None:
    """``stop_reason='max_tokens'`` is UNCLEAN even when partial text is
    present (#155 DEC-001/DEC-002, Anthropic-side analogue of the Gemini
    Finding-1 regression).

    A truncated-at-max_tokens response carries a non-empty ``content`` list
    whose final text block is mid-string. Before #155, the only post-call
    gate was :meth:`AnthropicProvider.extract_text_blocks`, which only
    raised when ZERO blocks were collected — so truncation with partial
    text silently slipped through, reached the JSON parser, and surfaced
    as the wrong typed degrade (``GradeOutputError`` instead of
    ``GradeLLMError``). The :meth:`is_clean_completion` gate raises the
    floor: any non-clean stop reason routes to
    :class:`LLMResponseFormatError` regardless of whether partial text was
    emitted.
    """
    response = FakeMessage(
        content=[FakeTextBlock(text='{"score": 0.9, "reasoning": "partial truncated')],
        usage=FakeUsage(input_tokens=120, output_tokens=45),
        stop_reason="max_tokens",
    )
    assert AnthropicProvider().is_clean_completion(response) is False


@pytest.mark.unit
@pytest.mark.llm
def test_is_clean_completion_false_for_tool_use() -> None:
    """``stop_reason='tool_use'`` is UNCLEAN in v0.3 (#155 DEC-006).

    The clean set is exactly ``{end_turn, stop_sequence}``. ``tool_use``
    is deliberately excluded: the codebase doesn't use tools today, so a
    ``tool_use`` response would signal system-prompt drift or unexpected
    LLM behaviour. When tool-use intentionally lands, the clean set
    expands deliberately.
    """
    response = _ok_response(stop_reason="tool_use")
    assert AnthropicProvider().is_clean_completion(response) is False


@pytest.mark.unit
@pytest.mark.llm
def test_unclean_finish_reason_message_names_stop_reason_field() -> None:
    """:meth:`unclean_finish_reason_message` renders an operator-facing
    diagnostic that names the vendor-native ``stop_reason`` field and
    quotes the actual unclean value (#155 DEC-007).

    Operators reading a CLI / log line should see the field name they
    can search Anthropic docs for ("stop_reason"), the offending value,
    and the most-likely cause categories. Vendor-accurate naming
    (``stop_reason`` for Anthropic vs ``finish_reason`` for OpenAI /
    Gemini) is why DEC-007 made this a provider-override rather than a
    shared default.
    """
    response = _ok_response(stop_reason="max_tokens")
    message = AnthropicProvider().unclean_finish_reason_message(response)
    assert "stop_reason" in message
    assert "'max_tokens'" in message
