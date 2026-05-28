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
