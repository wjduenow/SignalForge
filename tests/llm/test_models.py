"""Tests for :class:`signalforge.llm.models.LLMResult` (US-004)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from signalforge.llm.models import LLMResult


def _make_result(**overrides: object) -> LLMResult:
    base: dict[str, object] = {
        "text_blocks": ("hello", "world"),
        "response_text": "hello world",
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "model": "claude-sonnet-4-5",
        "prompt_version": "v1",
        "raw_message": object(),
    }
    base.update(overrides)
    return LLMResult(**base)  # type: ignore[arg-type]


def test_llm_result_constructs_with_required_fields() -> None:
    result = _make_result()
    assert result.text_blocks == ("hello", "world")
    assert result.response_text == "hello world"
    assert result.input_tokens == 10
    assert result.output_tokens == 20
    assert result.model == "claude-sonnet-4-5"
    assert result.prompt_version == "v1"


def test_llm_result_text_blocks_immutable_tuple() -> None:
    result = _make_result()
    assert result.text_blocks.__class__ is tuple
    # Pydantic v2 frozen models raise ValidationError on attribute assignment;
    # the underlying tuple itself raises TypeError on item assignment. Either
    # is sufficient evidence that the sequence cannot be mutated post-construct.
    with pytest.raises(ValidationError):
        result.text_blocks = ("mutated",)  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.text_blocks[0] = "mutated"  # type: ignore[index]


def test_llm_result_extra_ignore_drops_unknown_field() -> None:
    result = LLMResult.model_validate(
        {
            "text_blocks": ("hi",),
            "response_text": "hi",
            "input_tokens": 1,
            "output_tokens": 2,
            "model": "claude-sonnet-4-5",
            "prompt_version": "v1",
            "raw_message": None,
            "unknown_field": "x",
        }
    )
    assert not hasattr(result, "unknown_field")


def test_llm_result_raw_message_accepts_arbitrary_type() -> None:
    @dataclass
    class _CustomRaw:
        id: str

    raw = _CustomRaw(id="msg_123")
    result = _make_result(raw_message=raw)
    assert result.raw_message is raw


def test_llm_result_cache_tokens_default_to_zero() -> None:
    result = LLMResult(
        text_blocks=("hi",),
        response_text="hi",
        input_tokens=1,
        output_tokens=2,
        model="claude-sonnet-4-5",
        prompt_version="v1",
        raw_message=None,
    )
    assert result.cache_creation_input_tokens == 0
    assert result.cache_read_input_tokens == 0
