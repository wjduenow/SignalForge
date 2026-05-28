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

from signalforge.llm.providers import OpenAIProvider
from tests.llm._fake_openai import (
    FakeOpenAIChoice,
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
