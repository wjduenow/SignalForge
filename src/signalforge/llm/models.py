"""Typed result shape for the LLM-drafting client (US-004).

Defines :class:`LLMResult` — the stable, read-back-tolerant value object
returned by :func:`signalforge.llm.client.call_anthropic` (lands in US-006)
and consumed by the parser/integration layers (US-009 onwards).

Design commitments operationalised here:

* **DEC-016** — :class:`LLMResult` carries the provenance fields a downstream
  ``DraftOutcome`` needs to explain itself: token usage (input/output plus
  cache creation/read), the resolved ``model`` string, and the
  ``prompt_version`` that was rendered into the request. Without these
  fields, a graded artefact cannot be traced back to the (model, prompt)
  pair that produced it.
* ``manifest-readers.md`` — Production read-back models are
  ``frozen=True, extra="ignore"``. The extras-ignore policy lets newer
  SignalForge versions add fields to the SDK seam's response shape without
  breaking older consumers; the matching ``extra="forbid"`` drift detector
  is the responsibility of US-011.
* ``raw_message`` is annotated :data:`typing.Any` rather than
  ``anthropic.types.Message`` so this module does not import the Anthropic
  SDK. The SDK's ``Message`` shape moves between minor versions; binding
  to it here would force every consumer of :class:`LLMResult` to pin the
  same SDK release. ``arbitrary_types_allowed=True`` lets callers pass the
  SDK's concrete object through untouched at runtime.
* ``text_blocks`` is :class:`tuple` (not :class:`list`) so ``frozen=True``
  actually prevents post-construction mutation. A consumer that holds an
  :class:`LLMResult` cannot rewrite the text blocks the parser will see.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class LLMResult(BaseModel):
    """Stable result shape returned by :func:`call_anthropic` (DEC-016).

    Frozen + ``extra="ignore"`` mirrors :class:`signalforge.safety.LLMRequest`:
    once the client has produced an :class:`LLMResult`, downstream stages
    (parser, grader, diff renderer) read it but never mutate it.

    The ``cache_creation_input_tokens`` and ``cache_read_input_tokens`` fields
    default to 0 because the Anthropic SDK omits them when prompt caching is
    not in play. Defaulting at the model layer keeps the client's response
    construction free of conditional ``hasattr`` checks.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    text_blocks: tuple[str, ...]
    response_text: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    model: str
    prompt_version: str
    raw_message: Any


__all__ = ("LLMResult",)
