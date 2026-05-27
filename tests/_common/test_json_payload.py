"""Tests for :func:`signalforge._common.json_payload.extract_json_payload`.

The helper is the load-bearing JSON-only guardrail for the LLM-response
parsers (issue #144): claude-sonnet-4-6 can narrate a prose preamble
before the JSON object and does NOT support an assistant-turn prefill to
force JSON-only output, so the parser must tolerate the preamble.
"""

from __future__ import annotations

import json

import pytest

from signalforge._common.json_payload import extract_json_payload

pytestmark = pytest.mark.unit


def test_plain_json_object_passthrough() -> None:
    text = '{"a": 1, "b": [2, 3]}'
    assert extract_json_payload(text) == text


def test_strips_leading_prose_preamble() -> None:
    """The issue #144 failure mode: prose before the object."""
    preamble = "I need to analyze the business rules carefully. The first rule is a tautology. "
    payload = '{"columns": [], "tests": []}'
    extracted = extract_json_payload(preamble + payload)
    assert extracted == payload
    assert json.loads(extracted) == {"columns": [], "tests": []}


def test_strips_trailing_content_after_object() -> None:
    payload = '{"score": 0.8, "passed": true}'
    assert extract_json_payload(payload + "\n\nHope that helps!") == payload


def test_strips_markdown_code_fence_via_first_brace() -> None:
    text = '```json\n{"ok": true}\n```'
    assert json.loads(extract_json_payload(text)) == {"ok": True}


def test_decodes_at_first_brace_when_it_is_valid_json() -> None:
    """When the first structural char begins a valid object, it is returned
    even if more prose/JSON follows."""
    text = 'Result: {"verdict": "yes"} and some trailing note {"ignored": 1}'
    assert json.loads(extract_json_payload(text)) == {"verdict": "yes"}


def test_truncated_outer_object_not_rescued_by_inner_fragment() -> None:
    """A truncated outer object must NOT be silently replaced by a complete
    inner fragment — decoding is attempted at the first brace only, so this
    returns unchanged and the caller's strict parser fails loud."""
    truncated = '{"columns": [{"name": "order_id"}], "tests": ['  # no closing braces
    assert extract_json_payload(truncated) == truncated
    with pytest.raises(json.JSONDecodeError):
        json.loads(extract_json_payload(truncated))


def test_extracts_leading_array() -> None:
    text = "Sure! [1, 2, 3]"
    assert extract_json_payload(text) == "[1, 2, 3]"


def test_no_json_returns_stripped_input_unchanged() -> None:
    """No decodable JSON → return the stripped text so the caller's strict
    parser raises its normal error with the right excerpt."""
    text = "  this is not json at all  "
    assert extract_json_payload(text) == "this is not json at all"


def test_empty_input_returns_empty() -> None:
    assert extract_json_payload("   ") == ""


def test_does_not_mutate_json_bytes() -> None:
    """Whitespace inside the object is preserved verbatim (no re-encoding)."""
    payload = '{"a":    1,\n  "b": 2}'
    assert extract_json_payload("preamble " + payload) == payload
