"""Test-side helper for the grader's LLM-judge fake (US-008, DEC-021).

Wraps :class:`tests.llm._fake.FakeAnthropicClient` with a single
helper, :func:`expect_grade_responses`, that enqueues the
``(count_tokens, messages.create)`` expectation pair for every
``(criterion, artifact)`` call :func:`signalforge.grade.grade_artifacts`
will issue. The helper mirrors the orchestrator's iteration order
(criterion-outer, artifact-inner per
:func:`signalforge.grade.engine._iterate_artifacts`).

Mirrors the precedent set by
:func:`tests.warehouse._fake.FakeBigQueryClient.expect_*` and the
LLM-layer fake's :meth:`expect_messages_create` — a hand-rolled
fake driven by a small, explicit ``expect_*`` API. Lives under
``tests/grade/`` and is never imported from production code.
"""

from __future__ import annotations

import json
from typing import Any

from signalforge.draft.models import CandidateSchema
from signalforge.grade.engine import _stable_artifact_pairs
from signalforge.grade.rubric import Rubric
from tests.llm._fake import (
    FakeAnthropicClient,
    FakeCountTokensResponse,
    FakeMessage,
    FakeTextBlock,
    FakeUsage,
)


def expect_grade_responses(
    fake_client: FakeAnthropicClient,
    *,
    rubric: Rubric,
    candidate: CandidateSchema,
    scores: dict[tuple[str, str], tuple[float | None, bool, str, str]] | None = None,
    cached_tokens: int = 1500,
    input_tokens: int = 1700,
    output_tokens: int = 140,
) -> None:
    """Enqueue ``(count_tokens, messages.create)`` expectation pairs for an
    entire :func:`signalforge.grade.grade_artifacts` run.

    For every ``(criterion, artifact)`` pair the orchestrator will
    iterate (criterion-outer, artifact-inner), enqueue:

    1. one ``count_tokens`` expectation returning a
       :class:`FakeCountTokensResponse(input_tokens=cached_tokens)`;
    2. one ``messages.create`` expectation returning a
       :class:`FakeMessage` whose ``content[0].text`` is the JSON
       ``{"criterion_id": ..., "score": ..., "passed": ...,
       "evidence": ..., "reasoning": ...}``.

    Args:
        fake_client: the :class:`FakeAnthropicClient` to drive.
        rubric: the rubric the orchestrator will iterate over.
        candidate: the :class:`CandidateSchema` whose artifacts will
            be iterated.
        scores: optional dict keyed by ``(artifact_id, criterion_id)``
            mapping to ``(score, passed, evidence, reasoning)``.
            Missing keys default to ``(0.5, True, "", "")``. Pass
            ``score=None`` to test a degraded path (the parser will
            accept it; the grader's audit / sidecar will record
            ``None``).
        cached_tokens: ``count_tokens.input_tokens`` returned by the
            fake; defaults to a value safely above the
            Sonnet/Opus 1024-token minimum.
        input_tokens / output_tokens: usage values stamped on the
            fake response object.
    """
    scores = scores or {}
    artifact_pairs = _stable_artifact_pairs(candidate)
    for criterion in rubric:
        for artifact_id, _artifact_text in artifact_pairs:
            score, passed, evidence, reasoning = scores.get(
                (artifact_id, criterion.id), (0.5, True, "", "")
            )
            fake_client.expect_count_tokens(
                matching=lambda _kw: True,
                returns=FakeCountTokensResponse(input_tokens=cached_tokens),
            )
            payload: dict[str, Any] = {
                "criterion_id": criterion.id,
                "score": score,
                "passed": passed,
                "evidence": evidence,
                "reasoning": reasoning,
            }
            fake_client.expect_messages_create(
                matching=lambda _kw: True,
                returns=FakeMessage(
                    content=[FakeTextBlock(text=json.dumps(payload))],
                    usage=FakeUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_creation_input_tokens=0,
                        cache_read_input_tokens=cached_tokens,
                    ),
                    model="claude-fake-grade-judge",
                ),
            )


__all__ = ["expect_grade_responses"]
