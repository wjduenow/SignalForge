"""Tolerant JSON-payload extraction shared by the LLM-response parsers.

Issue #144: ``claude-sonnet-4-6`` reproducibly emitted a reasoning preamble
("I need to analyze the business rules carefully...") *before* the JSON
object on the business-rules drafting path, so the strict
``json.loads`` / ``model_validate_json`` parse failed at line 1. The
obvious guardrail — an assistant-turn prefill forcing JSON-only output —
is **not available**: the API rejects it with
``"This model does not support assistant message prefill. The
conversation must end with a user message."`` (HTTP 400). The parser is
therefore the only place a JSON-only guarantee can live, and prompts are
advisory at best.

:func:`extract_json_payload` locates the first complete JSON value
embedded in a response and returns just that substring, so the caller's
existing strict parser runs on clean JSON. Both the drafter
(:func:`signalforge.draft.parser.parse_draft_response`) and the grader
(:func:`signalforge.grade.parser.parse_grade_response`) route through it.
"""

from __future__ import annotations

import json

__all__ = ("extract_json_payload",)


def extract_json_payload(text: str) -> str:
    """Return the first complete JSON value embedded in ``text``.

    Tolerates a leading prose preamble (and any trailing content) around a
    single JSON object/array — the issue #144 failure mode, where the model
    narrates plain sentences before the ``{``. The text is
    whitespace-stripped, then decoding is attempted **at the first** ``{``
    or ``[`` via :meth:`json.JSONDecoder.raw_decode`; on success the
    substring spanning that value is returned (trailing content discarded).

    Decoding is attempted at the first structural character ONLY — never at
    later braces. Scanning deeper would let a *truncated* outer object
    (whose first ``{`` fails to decode) match a complete **inner** fragment,
    silently turning a "not valid JSON" failure into a wrong-shape parse.
    So if the first candidate fails to decode, the whitespace-stripped input
    is returned unchanged and the caller's strict parser raises its normal
    JSON error with the correct excerpt/position. This helper never raises
    and never mutates the JSON bytes — it only trims a leading preamble and
    trailing content around a cleanly-decodable value.
    """
    stripped = text.strip()
    first = next((i for i, ch in enumerate(stripped) if ch in "{["), -1)
    if first == -1:
        return stripped
    try:
        _value, end = json.JSONDecoder().raw_decode(stripped, first)
    except json.JSONDecodeError:
        return stripped
    return stripped[first:end]
