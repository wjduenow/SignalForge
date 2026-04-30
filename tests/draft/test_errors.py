"""Unit tests for the draft errors module (US-007, DEC-003 / DEC-006).

Mirrors :mod:`tests.llm.test_errors` and :mod:`tests.safety.test_errors`.
Every test is capable of failing: no ``assert True``-shaped placeholders
(``testing-signal.md``).

Note: this directory deliberately has NO ``__init__.py``. Pytest's rootdir
+ ``--import-mode=importlib`` discovers the file via basename namespacing
without polluting the import graph.
"""

from __future__ import annotations

import json
from typing import TypedDict

import pytest
from pydantic import BaseModel, ValidationError

from signalforge.draft import errors as errors_module
from signalforge.draft.errors import (
    DraftError,
    LLMOutputAnchorContractError,
    LLMOutputError,
    LLMOutputJSONError,
    LLMOutputValidationError,
    LLMResponseAuditWriteError,
    _format_value,
)


class _Envelope(TypedDict):
    prompt_version: str
    model: str
    cache_hit: bool
    input_tokens: int
    output_tokens: int


# Common envelope kwargs shared across LLMOutputError-family constructions.
_ENVELOPE: _Envelope = {
    "prompt_version": "0123456789abcdef",
    "model": "claude-sonnet-4-6",
    "cache_hit": True,
    "input_tokens": 1234,
    "output_tokens": 567,
}


@pytest.mark.unit
@pytest.mark.draft
def test_draft_error_renders_remediation() -> None:
    """The base ``__str__`` includes both the message and the
    ``↳ Remediation:`` marker line."""
    rendered = str(DraftError("boom", remediation="fix it"))
    assert "boom" in rendered
    assert "↳ Remediation: fix it" in rendered


@pytest.mark.unit
@pytest.mark.draft
def test_all_is_sorted_and_complete() -> None:
    """``__all__`` is alphabetically sorted and lists 6 classes total."""
    assert errors_module.__all__ == sorted(errors_module.__all__)
    # 1 base (DraftError) + 1 LLM-output base + 3 LLM-output subclasses
    # (JSON / Validation / AnchorContract) + 1 audit-write = 6.
    assert len(errors_module.__all__) == 6, (
        "US-007 enumerates 5 typed subclasses + 1 base; update tests "
        "and __all__ together if this changes."
    )


@pytest.mark.unit
@pytest.mark.draft
def test_format_value_quotes_via_repr() -> None:
    """``_format_value`` quotes adversarial input safely (DEC-022)."""
    rendered = _format_value("foo'\nINFO: spoofed")
    # repr() escapes the embedded newline and adds quotes
    assert rendered.startswith("'") or rendered.startswith('"')
    assert "\\n" in rendered  # newline escaped, not literal
    assert "\n" not in rendered  # no raw newline can sneak through


@pytest.mark.unit
@pytest.mark.draft
def test_llm_output_error_excerpt_centred_on_parse_position() -> None:
    """The excerpt is a ±80-char window around the parse position."""
    # Sentinels at chars 0..2 and 200..202 let us verify the window is
    # bounded — repeating-char fillers would let `in` matches succeed
    # accidentally, so use a unique head/tail.
    raw_text = "HEAD" + ("a" * 96) + "BAD" + ("b" * 97) + "TAIL"
    bad_offset = raw_text.index("BAD")
    err = LLMOutputError(
        "parse failed",
        raw_text=raw_text,
        parse_position=(1, bad_offset + 1),  # 1-indexed col
        **_ENVELOPE,
    )
    # The window covers offset±80 and therefore contains "BAD".
    assert "BAD" in err.excerpt
    # The excerpt MUST NOT reach the unique head/tail sentinels — they're
    # more than 80 chars away from "BAD".
    assert "HEAD" not in err.excerpt
    assert "TAIL" not in err.excerpt
    # The excerpt is bounded by 2*radius + the sentinel marker length.
    assert len(err.excerpt) <= 2 * 80 + len(" ⟨HERE⟩ ")


@pytest.mark.unit
@pytest.mark.draft
def test_llm_output_error_excerpt_handles_position_at_start_of_text() -> None:
    """Position near the start of raw_text doesn't crash; excerpt starts at 0."""
    raw_text = "STARTX" + ("y" * 200) + "ENDXX"
    err = LLMOutputError(
        "parse failed",
        raw_text=raw_text,
        parse_position=(1, 1),  # very first char
        **_ENVELOPE,
    )
    # Excerpt should include the start of the text — no negative-index
    # craziness — and the unique start sentinel proves we read from 0.
    assert "STARTX" in err.excerpt
    # The far end is more than 80 chars away; the unique end sentinel
    # MUST NOT appear.
    assert "ENDXX" not in err.excerpt


@pytest.mark.unit
@pytest.mark.draft
def test_llm_output_error_excerpt_handles_position_at_end_of_text() -> None:
    """Position near the end doesn't read past the buffer."""
    raw_text = ("y" * 200) + "Z"
    # Position past the end of text — should clamp without IndexError.
    err = LLMOutputError(
        "parse failed",
        raw_text=raw_text,
        parse_position=(1, len(raw_text) + 5),
        **_ENVELOPE,
    )
    assert "Z" in err.excerpt
    # The excerpt should be bounded; no crashes on overshoot.
    assert len(err.excerpt) <= len(raw_text) + len(" ⟨HERE⟩ ") + 1


@pytest.mark.unit
@pytest.mark.draft
def test_llm_output_error_excerpt_handles_none_parse_position() -> None:
    """``parse_position=None`` falls back to the first 160 chars of raw_text."""
    raw_text = "a" * 500
    err = LLMOutputError(
        "parse failed",
        raw_text=raw_text,
        parse_position=None,
        **_ENVELOPE,
    )
    # Falls back to first 160 chars (2 * _EXCERPT_RADIUS).
    assert err.excerpt == raw_text[:160]


@pytest.mark.unit
@pytest.mark.draft
def test_llm_output_error_str_truncates_raw_text_at_4kb() -> None:
    """``__str__`` does NOT include the full raw_text, even for huge inputs."""
    raw_text = "X" * 10_000
    err = LLMOutputError(
        "parse failed",
        raw_text=raw_text,
        parse_position=(1, 1),
        **_ENVELOPE,
    )
    rendered = str(err)
    # The full 10_000-char raw_text MUST NOT appear in the rendered string —
    # log lines would explode. The rendered string is bounded around the
    # excerpt (160 chars) + envelope metadata.
    assert raw_text not in rendered
    # Loose upper bound: the rendered string is much smaller than the
    # raw_text, even with metadata + remediation tail.
    assert len(rendered) < len(raw_text)
    # Rendered string should fit comfortably under raw_text rendering cap
    # plus a small overhead for metadata.
    assert len(rendered) < 4500


@pytest.mark.unit
@pytest.mark.draft
def test_llm_output_error_attribute_keeps_full_raw_text() -> None:
    """The full raw_text is preserved on the attribute (audit needs it)."""
    raw_text = "Z" * 10_000
    err = LLMOutputError(
        "parse failed",
        raw_text=raw_text,
        parse_position=(1, 1),
        **_ENVELOPE,
    )
    assert err.raw_text == raw_text
    assert len(err.raw_text) == 10_000


@pytest.mark.unit
@pytest.mark.draft
def test_llm_output_error_str_includes_position_and_metadata_tail() -> None:
    """``__str__`` carries a position + (model, prompt_version) tail."""
    err = LLMOutputError(
        "parse failed",
        raw_text="hello world",
        parse_position=(3, 17),
        **_ENVELOPE,
    )
    rendered = str(err)
    assert "line=3" in rendered
    assert "col=17" in rendered
    assert "claude-sonnet-4-6" in rendered
    assert "0123456789abcdef" in rendered


@pytest.mark.unit
@pytest.mark.draft
def test_llm_output_json_error_carries_json_decode_cause() -> None:
    """``LLMOutputJSONError`` derives ``parse_position`` from the cause."""
    raw_text = '{\n  "foo": ,\n}'
    try:
        json.loads(raw_text)
    except json.JSONDecodeError as exc:
        cause = exc
    else:
        pytest.fail("expected json.loads to raise on malformed JSON")

    err = LLMOutputJSONError(
        "JSON parse failed",
        cause=cause,
        raw_text=raw_text,
        **_ENVELOPE,
    )
    assert err.cause is cause
    assert err.parse_position == (cause.lineno, cause.colno)
    # And the envelope round-trips.
    assert err.raw_text == raw_text
    assert err.model == "claude-sonnet-4-6"


@pytest.mark.unit
@pytest.mark.draft
def test_llm_output_validation_error_carries_pydantic_cause() -> None:
    """``LLMOutputValidationError`` preserves the Pydantic ValidationError."""

    class _M(BaseModel):
        x: int

    try:
        _M.model_validate({"x": "nope"})
    except ValidationError as exc:
        cause = exc
    else:
        pytest.fail("expected ValidationError on bad type")

    err = LLMOutputValidationError(
        "schema validation failed",
        cause=cause,
        raw_text='{"x": "nope"}',
        **_ENVELOPE,
    )
    assert err.cause is cause
    # parse_position defaults to None for structural validation failures.
    assert err.parse_position is None
    # Excerpt falls back to first 160 chars.
    assert err.excerpt == '{"x": "nope"}'


@pytest.mark.unit
@pytest.mark.draft
def test_llm_output_anchor_contract_error_carries_violations_list() -> None:
    """``LLMOutputAnchorContractError`` preserves the violations tuple."""
    violations = ("missing_col_a", "missing_col_b")
    err = LLMOutputAnchorContractError(
        "anchor contract violated",
        violations=violations,
        raw_text='{"columns": []}',
        **_ENVELOPE,
    )
    assert err.violations == violations
    # The tuple is preserved as a tuple — tests downstream pattern-match
    # on type rather than coercing.
    assert isinstance(err.violations, tuple)
    assert len(err.violations) == 2


@pytest.mark.unit
@pytest.mark.draft
def test_llm_response_audit_write_error_carries_cause() -> None:
    """``LLMResponseAuditWriteError`` preserves the underlying cause."""
    cause = OSError("permission denied")
    err = LLMResponseAuditWriteError(
        "response audit write failed",
        cause=cause,
    )
    assert err.cause is cause
    assert isinstance(err, DraftError)
    # The remediation surfaces in __str__.
    assert "↳ Remediation:" in str(err)


@pytest.mark.unit
@pytest.mark.draft
def test_excerpt_marks_offending_position_with_sentinel() -> None:
    """The ⟨HERE⟩ sentinel lands at the parse-position offset."""
    raw_text = "before|after"
    err = LLMOutputError(
        "parse failed",
        raw_text=raw_text,
        parse_position=(1, 7),  # "|" is at 0-indexed offset 6, col 7
        **_ENVELOPE,
    )
    # The sentinel sits between "before" and "|after".
    assert "before ⟨HERE⟩ |after" in err.excerpt


@pytest.mark.unit
@pytest.mark.draft
def test_subclasses_are_draft_errors() -> None:
    """Every public subclass inherits from :class:`DraftError`."""
    for name in errors_module.__all__:
        cls = getattr(errors_module, name)
        assert issubclass(cls, DraftError), f"{name} must subclass DraftError"


@pytest.mark.unit
@pytest.mark.draft
def test_default_remediations_are_set() -> None:
    """Every class in ``__all__`` declares a non-empty ``default_remediation``."""
    for name in errors_module.__all__:
        cls = getattr(errors_module, name)
        rem = cls.default_remediation
        assert isinstance(rem, str) and rem, (
            f"{name}.default_remediation must be a non-empty string; got {rem!r}"
        )
