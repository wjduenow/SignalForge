"""Tests for :func:`signalforge.grade.parser.parse_grade_response` (US-006).

Covers the locked nine-value :data:`GradeOutputViolationType` literal
taxonomy at the parser surface: every malformed-LLM-response shape
routes to a typed :class:`GradeOutputError` with a ``violation_type``
from the finite Literal. Per ``.claude/rules/testing-signal.md`` every
test is capable of failing if its target is broken.
"""

from __future__ import annotations

import json
from typing import get_args

import pytest

from signalforge.grade.errors import GradeOutputError, GradeOutputViolationType
from signalforge.grade.models import GradingResult
from signalforge.grade.parser import parse_grade_response
from signalforge.grade.rubric import Criterion

_ARTIFACT_ID = "column.email.description"
_CRITERION = Criterion(id="clarity", criterion="Is the description clear and unambiguous?")


def _payload(**overrides: object) -> dict[str, object]:
    """Return a well-formed payload, overrideable per-test."""
    base: dict[str, object] = {
        "criterion_id": _CRITERION.id,
        "score": 0.8,
        "passed": True,
        "evidence": "ev",
        "reasoning": "First sentence. Second sentence.",
    }
    base.update(overrides)
    return base


def test_parse_grade_response_matching_criterion_id_succeeds() -> None:
    result = parse_grade_response(
        json.dumps(_payload()),
        artifact_id=_ARTIFACT_ID,
        criterion=_CRITERION,
    )
    assert isinstance(result, GradingResult)
    assert result.artifact_id == _ARTIFACT_ID
    assert result.criterion_id == _CRITERION.id
    assert result.score == 0.8
    assert result.passed is True
    assert result.evidence == "ev"
    assert result.reasoning == "First sentence. Second sentence."


def test_parse_grade_response_strips_json_code_fence() -> None:
    raw = "```json\n" + json.dumps(_payload()) + "\n```"
    result = parse_grade_response(raw, artifact_id=_ARTIFACT_ID, criterion=_CRITERION)
    assert result.criterion_id == _CRITERION.id


def test_parse_grade_response_strips_unfenced_code_fence() -> None:
    raw = "```\n" + json.dumps(_payload()) + "\n```"
    result = parse_grade_response(raw, artifact_id=_ARTIFACT_ID, criterion=_CRITERION)
    assert result.criterion_id == _CRITERION.id


def test_parse_grade_response_tolerates_prose_preamble() -> None:
    """Issue #144: the judge can narrate before the `{` and the model
    rejects an assistant prefill, so the parser strips the preamble."""
    raw = "Let me think about this. The description is clear, so:\n\n" + json.dumps(_payload())
    result = parse_grade_response(raw, artifact_id=_ARTIFACT_ID, criterion=_CRITERION)
    assert result.criterion_id == _CRITERION.id
    assert result.score == 0.8


def test_parse_grade_response_strips_surrounding_whitespace() -> None:
    raw = "   \n\t" + json.dumps(_payload()) + "\n  "
    result = parse_grade_response(raw, artifact_id=_ARTIFACT_ID, criterion=_CRITERION)
    assert result.criterion_id == _CRITERION.id


def test_parse_grade_response_invalid_json_raises_with_json_parse_violation() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response("{not valid json", artifact_id=_ARTIFACT_ID, criterion=_CRITERION)
    assert excinfo.value.violation_type == "json_parse"
    # The underlying JSONDecodeError is preserved as __cause__ so
    # callers can recover positional context if they need it.
    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


def test_parse_grade_response_top_level_list_raises_json_parse() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response("[1, 2, 3]", artifact_id=_ARTIFACT_ID, criterion=_CRITERION)
    assert excinfo.value.violation_type == "json_parse"


def test_parse_grade_response_top_level_scalar_raises_json_parse() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response("42", artifact_id=_ARTIFACT_ID, criterion=_CRITERION)
    assert excinfo.value.violation_type == "json_parse"


def test_parse_grade_response_missing_criterion_id_raises_with_missing_criterion_id() -> None:
    payload = _payload()
    del payload["criterion_id"]
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(payload),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "missing_criterion_id"


def test_parse_grade_response_missing_score_raises_with_missing_required_field() -> None:
    payload = _payload()
    del payload["score"]
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(payload),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "missing_required_field"


def test_parse_grade_response_missing_passed_raises_with_missing_required_field() -> None:
    payload = _payload()
    del payload["passed"]
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(payload),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "missing_required_field"


def test_parse_grade_response_mismatched_criterion_id_raises_with_criterion_id_mismatch() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(_payload(criterion_id="some-other-criterion")),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "criterion_id_mismatch"


def test_parse_grade_response_score_above_one_raises_score_out_of_range() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(_payload(score=1.5)),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "score_out_of_range"


def test_parse_grade_response_score_below_zero_raises_score_out_of_range() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(_payload(score=-0.1)),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "score_out_of_range"


def test_parse_grade_response_score_nan_raises_score_out_of_range() -> None:
    # JSON does not have a literal NaN; emit it as the JS-like token
    # that ``json.loads`` accepts via its default lenient float parser.
    raw = (
        '{"criterion_id": "clarity", "score": NaN, "passed": true, "evidence": "", "reasoning": ""}'
    )
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(raw, artifact_id=_ARTIFACT_ID, criterion=_CRITERION)
    assert excinfo.value.violation_type == "score_out_of_range"


def test_parse_grade_response_score_inf_raises_score_out_of_range() -> None:
    raw = (
        '{"criterion_id": "clarity", "score": Infinity, "passed": true, '
        '"evidence": "", "reasoning": ""}'
    )
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(raw, artifact_id=_ARTIFACT_ID, criterion=_CRITERION)
    assert excinfo.value.violation_type == "score_out_of_range"


def test_parse_grade_response_score_string_raises_score_not_a_number() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(_payload(score="0.8")),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "score_not_a_number"


def test_parse_grade_response_score_bool_true_raises_score_not_a_number() -> None:
    # ``True`` is a subclass of ``int`` in Python; reject explicitly so
    # it doesn't sneak past the numeric type check.
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(_payload(score=True)),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "score_not_a_number"


def test_parse_grade_response_score_bool_false_raises_score_not_a_number() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(_payload(score=False)),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "score_not_a_number"


def test_parse_grade_response_score_none_accepted_degraded_path() -> None:
    # DEC-015 — the degraded path: when the LLM judge exhausts retries
    # the orchestrator emits ``score=None``. The parser permits None
    # so a deserialised audit row round-trips.
    result = parse_grade_response(
        json.dumps(_payload(score=None)),
        artifact_id=_ARTIFACT_ID,
        criterion=_CRITERION,
    )
    assert result.score is None


def test_parse_grade_response_score_int_accepted() -> None:
    # An LLM might emit ``"score": 1`` instead of ``1.0``; coerce.
    result = parse_grade_response(
        json.dumps(_payload(score=1)),
        artifact_id=_ARTIFACT_ID,
        criterion=_CRITERION,
    )
    assert result.score == 1.0
    assert isinstance(result.score, float)


def test_parse_grade_response_passed_int_one_raises_passed_not_a_bool() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(_payload(passed=1)),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "passed_not_a_bool"


def test_parse_grade_response_passed_int_zero_raises_passed_not_a_bool() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(_payload(passed=0)),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "passed_not_a_bool"


def test_parse_grade_response_passed_string_raises_passed_not_a_bool() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(_payload(passed="true")),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "passed_not_a_bool"


def test_parse_grade_response_evidence_default_empty_string_when_absent() -> None:
    payload = _payload()
    del payload["evidence"]
    result = parse_grade_response(
        json.dumps(payload),
        artifact_id=_ARTIFACT_ID,
        criterion=_CRITERION,
    )
    assert result.evidence == ""


def test_parse_grade_response_reasoning_default_empty_string_when_absent() -> None:
    payload = _payload()
    del payload["reasoning"]
    result = parse_grade_response(
        json.dumps(payload),
        artifact_id=_ARTIFACT_ID,
        criterion=_CRITERION,
    )
    assert result.reasoning == ""


def test_parse_grade_response_evidence_non_string_raises_missing_required_field() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(_payload(evidence=123)),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "missing_required_field"


def test_parse_grade_response_reasoning_non_string_raises_missing_required_field() -> None:
    with pytest.raises(GradeOutputError) as excinfo:
        parse_grade_response(
            json.dumps(_payload(reasoning=["a", "b"])),
            artifact_id=_ARTIFACT_ID,
            criterion=_CRITERION,
        )
    assert excinfo.value.violation_type == "missing_required_field"


def test_parse_grade_response_extra_fields_tolerated() -> None:
    # ``GradingResult`` uses ``extra="ignore"`` (US-002) — the LLM may
    # emit extra keys; the parser drops them rather than failing.
    result = parse_grade_response(
        json.dumps(_payload(foo="bar", confidence=0.9)),
        artifact_id=_ARTIFACT_ID,
        criterion=_CRITERION,
    )
    assert result.criterion_id == _CRITERION.id
    # Confirm the extra field truly didn't slip into the model dump.
    assert "foo" not in result.model_dump()
    assert "confidence" not in result.model_dump()


def test_parse_grade_response_artifact_id_carried_verbatim() -> None:
    # The parser does not validate ``artifact_id`` shape; the
    # orchestrator owns format gates per DEC-009. The parser must
    # carry the supplied id verbatim onto the result.
    result = parse_grade_response(
        json.dumps(_payload()),
        artifact_id="test.column.user_id.not_null",
        criterion=_CRITERION,
    )
    assert result.artifact_id == "test.column.user_id.not_null"


def test_grade_output_violation_type_literal_taxonomy_locked() -> None:
    """The Literal taxonomy is the load-bearing surface of US-006.

    Adding or removing a literal here without updating the production
    parser, the drift-detector strict mirror, and docs/grade-ops.md is
    a regression. The test asserts the exact set so the failure mode
    is "this test is broken; you have additional checklist items"
    rather than "violation taxonomy silently expanded."
    """
    expected = {
        "json_parse",
        "missing_required_field",
        "missing_criterion_id",
        "criterion_id_mismatch",
        "score_out_of_range",
        "score_not_a_number",
        "passed_not_a_bool",
        "unknown_artifact_id",
        "ambiguous_artifact_id",
    }
    assert set(get_args(GradeOutputViolationType)) == expected
    assert len(get_args(GradeOutputViolationType)) == len(expected)
