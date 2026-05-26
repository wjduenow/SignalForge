"""Tests for :mod:`signalforge.grade.models` (US-002).

Covers the three read-back-shaped Pydantic models:

* :class:`GradingResult` — score-range validation (DEC-015 degraded
  path: ``None`` allowed; non-finite + out-of-range rejected),
  ``one_line_why`` computed field semantics (DEC-018), minimal
  ``__repr__`` per DEC-022 of #6.
* :class:`GradingReport` — aggregate computed fields (``pass_rate``,
  ``mean_score``, ``aggregate_complete``, ``passed``), minimal
  ``__repr__``, ``passed`` requiring BOTH thresholds met.
* :class:`GradeEvent` — score-range validation, audit_schema_version
  pinned to 1.

Every test must be capable of failing if its target is broken
(:file:`docs/rules/testing-signal.md`); no ``assert True``-shaped
tests.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from signalforge.grade.models import GradeEvent, GradingReport, GradingResult


def _make_result(
    *,
    artifact_id: str = "column.email.description",
    criterion_id: str = "clarity",
    score: float | None = 0.8,
    passed: bool = True,
    evidence: str = "",
    reasoning: str = "",
) -> GradingResult:
    """Construct a :class:`GradingResult` with sensible defaults."""
    return GradingResult(
        artifact_id=artifact_id,
        criterion_id=criterion_id,
        score=score,
        passed=passed,
        evidence=evidence,
        reasoning=reasoning,
    )


def _make_report(
    *,
    results: tuple[GradingResult, ...],
    thresholds: tuple[float, float] = (0.7, 0.5),
) -> GradingReport:
    """Construct a :class:`GradingReport` with sensible defaults."""
    return GradingReport(
        signalforge_version="0.1.0.dev0",
        run_id="a1b2c3d4e5f6478890aabbccddeeff00",
        timestamp=datetime(2026, 5, 1, 17, 42, 13, tzinfo=UTC),
        duration_seconds=12.473,
        model_unique_id="model.shop.dim_customers",
        rubric_hash="0123456789abcdef",
        thresholds=thresholds,
        results=results,
    )


def _make_event(
    *,
    score: float | None = 0.8,
    passed: bool = True,
    response_text_hash: str = "5555666677778888",
) -> GradeEvent:
    """Construct a :class:`GradeEvent` with sensible defaults."""
    return GradeEvent(
        signalforge_version="0.1.0.dev0",
        run_id="a1b2c3d4e5f6478890aabbccddeeff00",
        timestamp=datetime(2026, 5, 1, 17, 42, 13, tzinfo=UTC),
        model_unique_id="model.shop.dim_customers",
        artifact_id="column.email.description",
        criterion_id="clarity",
        score=score,
        passed=passed,
        evidence="",
        reasoning="",
        rubric_hash="0123456789abcdef",
        prompt_version_template="fedcba9876543210",
        criterion_prompt_hash="1111222233334444",
        response_text_hash=response_text_hash,
        model="claude-sonnet-4-6",
        input_tokens=1820,
        output_tokens=140,
    )


# --- GradingResult: minimal construction & score validation ----------------


def test_grading_result_model_validates_minimal_input() -> None:
    """Minimal valid input round-trips and stores the supplied fields."""
    result = _make_result(score=0.5, passed=True)
    assert result.artifact_id == "column.email.description"
    assert result.criterion_id == "clarity"
    assert result.score == 0.5
    assert result.passed is True
    assert result.evidence == ""
    assert result.reasoning == ""


def test_grading_result_score_in_range_accepted() -> None:
    """Boundary values 0.0 and 1.0 are accepted (closed interval)."""
    low = _make_result(score=0.0, passed=False)
    high = _make_result(score=1.0, passed=True)
    assert low.score == 0.0
    assert high.score == 1.0


def test_grading_result_score_above_one_rejected() -> None:
    """Scores above 1.0 fail validation with a clear message."""
    with pytest.raises(ValidationError) as exc_info:
        _make_result(score=1.01, passed=True)
    assert "[0.0, 1.0]" in str(exc_info.value)


def test_grading_result_score_below_zero_rejected() -> None:
    """Scores below 0.0 fail validation."""
    with pytest.raises(ValidationError):
        _make_result(score=-0.01, passed=False)


def test_grading_result_score_nan_rejected() -> None:
    """NaN is not a valid score (it would poison aggregate means)."""
    with pytest.raises(ValidationError) as exc_info:
        _make_result(score=math.nan, passed=False)
    assert "finite" in str(exc_info.value)


def test_grading_result_score_inf_rejected() -> None:
    """Infinity is not a valid score."""
    with pytest.raises(ValidationError):
        _make_result(score=math.inf, passed=False)


def test_grading_result_score_none_accepted_degraded_path() -> None:
    """``None`` is the documented degraded-path sentinel (DEC-015)."""
    result = _make_result(
        score=None,
        passed=False,
        reasoning="LLM call retries exhausted: APITimeoutError",
    )
    assert result.score is None
    assert result.passed is False


# --- GradingResult.one_line_why -------------------------------------------


def test_grading_result_one_line_why_first_sentence_when_period_present() -> None:
    """Returns the first sentence (split on ``". "``) including the period."""
    result = _make_result(
        reasoning="The description is clear. Adding units would help.",
    )
    assert result.one_line_why == "The description is clear."


def test_grading_result_one_line_why_truncated_to_120_chars_when_no_period() -> None:
    """When no ``". "`` boundary exists, falls back to first 120 chars."""
    long_text = "x" * 200  # no period+space anywhere
    result = _make_result(reasoning=long_text)
    assert result.one_line_why == "x" * 120
    assert len(result.one_line_why) == 120


def test_grading_result_one_line_why_empty_when_reasoning_empty() -> None:
    """Empty reasoning -> empty one_line_why."""
    result = _make_result(reasoning="")
    assert result.one_line_why == ""


def test_grading_result_one_line_why_caps_long_first_sentence() -> None:
    """A 200-char first sentence is capped at 120 chars."""
    long_first = ("a" * 200) + ". more."
    result = _make_result(reasoning=long_first)
    assert len(result.one_line_why) == 120
    assert result.one_line_why == "a" * 120


# --- GradingResult.__repr__ minimisation ----------------------------------


def test_grading_result_repr_omits_evidence_and_reasoning() -> None:
    """Custom repr (DEC-022 of #6) excludes ``evidence`` / ``reasoning``."""
    sensitive_evidence = "user_email='alice@example.com' was sampled"
    sensitive_reasoning = "Quoted PII content from the warehouse sample"
    result = _make_result(
        evidence=sensitive_evidence,
        reasoning=sensitive_reasoning,
    )
    rendered = repr(result)
    assert "alice@example.com" not in rendered
    assert "PII content" not in rendered
    assert "evidence=" not in rendered
    assert "reasoning=" not in rendered
    # But identity + verdict should be present.
    assert "GradingResult(" in rendered
    assert "column.email.description" in rendered
    assert "clarity" in rendered


# --- GradingReport aggregates ---------------------------------------------


def test_grading_report_pass_rate_skips_null_scores() -> None:
    """``pass_rate`` skips degraded results (DEC-015)."""
    report = _make_report(
        results=(
            _make_result(criterion_id="clarity", score=0.9, passed=True),
            _make_result(criterion_id="rationale", score=0.4, passed=False),
            _make_result(criterion_id="consistency", score=None, passed=False),
        ),
    )
    # Two scored results: one passed, one failed -> 0.5 pass_rate.
    assert report.pass_rate == 0.5


def test_grading_report_mean_score_skips_null_scores() -> None:
    """``mean_score`` is averaged only over scored results."""
    report = _make_report(
        results=(
            _make_result(criterion_id="a", score=1.0, passed=True),
            _make_result(criterion_id="b", score=0.0, passed=False),
            _make_result(criterion_id="c", score=None, passed=False),
        ),
    )
    assert report.mean_score == pytest.approx(0.5)


def test_grading_report_pass_rate_zero_when_all_scores_null() -> None:
    """All-degraded report has pass_rate = 0.0 (no division-by-zero)."""
    report = _make_report(
        results=(
            _make_result(criterion_id="a", score=None, passed=False),
            _make_result(criterion_id="b", score=None, passed=False),
        ),
    )
    assert report.pass_rate == 0.0
    assert report.mean_score == 0.0


def test_grading_report_aggregate_complete_false_when_any_score_is_none() -> None:
    """One degraded result flips ``aggregate_complete`` to False."""
    report = _make_report(
        results=(
            _make_result(criterion_id="a", score=0.9, passed=True),
            _make_result(criterion_id="b", score=None, passed=False),
        ),
    )
    assert report.aggregate_complete is False


def test_grading_report_aggregate_complete_true_when_all_scores_present() -> None:
    """Every result scored -> ``aggregate_complete`` is True."""
    report = _make_report(
        results=(
            _make_result(criterion_id="a", score=0.9, passed=True),
            _make_result(criterion_id="b", score=0.7, passed=True),
        ),
    )
    assert report.aggregate_complete is True


def test_grading_report_passed_requires_both_thresholds_met() -> None:
    """``passed`` is True only when BOTH thresholds are met (DEC-002)."""
    # pass_rate=1.0, mean_score=0.9 -> well above (0.7, 0.5)
    high = _make_report(
        results=(
            _make_result(score=0.9, passed=True),
            _make_result(score=0.9, passed=True),
        ),
    )
    assert high.passed is True

    # pass_rate=1.0 but mean_score=0.4 -> mean below threshold
    mean_low = _make_report(
        results=(
            _make_result(score=0.4, passed=True),
            _make_result(score=0.4, passed=True),
        ),
    )
    assert mean_low.passed is False

    # pass_rate=0.5 but mean_score=0.7 -> rate below threshold
    rate_low = _make_report(
        results=(
            _make_result(score=0.9, passed=True),
            _make_result(score=0.5, passed=False),
        ),
    )
    assert rate_low.passed is False


def test_grading_report_passed_at_threshold_boundary_inclusive() -> None:
    """Threshold comparison is inclusive (``>=``)."""
    report = _make_report(
        thresholds=(0.5, 0.5),
        results=(
            _make_result(score=0.5, passed=True),
            _make_result(score=0.5, passed=False),
        ),
    )
    # pass_rate=0.5, mean_score=0.5; both at boundary.
    assert report.passed is True


# --- GradingReport.__repr__ minimisation ----------------------------------


def test_grading_report_repr_omits_results_payload() -> None:
    """Report repr collapses to identity + aggregates; no per-result body."""
    sensitive = "secret reasoning that should not surface in logs"
    report = _make_report(
        results=(
            _make_result(score=0.9, passed=True, reasoning=sensitive),
            _make_result(score=None, passed=False, reasoning="degraded"),
        ),
    )
    rendered = repr(report)
    assert sensitive not in rendered
    assert "secret reasoning" not in rendered
    # Aggregate counts and pass-flags ARE expected in the minimal repr.
    assert "GradingReport(" in rendered
    assert "results_count=2" in rendered
    assert "model.shop.dim_customers" in rendered


# --- GradeEvent ------------------------------------------------------------


def test_grade_event_rejects_score_above_one() -> None:
    """Same score-range contract as :class:`GradingResult`."""
    with pytest.raises(ValidationError):
        _make_event(score=1.5, passed=True)


def test_grade_event_rejects_score_nan() -> None:
    """NaN must not survive into the audit log."""
    with pytest.raises(ValidationError):
        _make_event(score=math.nan, passed=False)


def test_grade_event_score_none_accepted_degraded_path() -> None:
    """``None`` score + empty ``response_text_hash`` is the DEC-015 sentinel."""
    event = _make_event(score=None, passed=False, response_text_hash="")
    assert event.score is None
    assert event.response_text_hash == ""


def test_grade_event_audit_schema_version_locked_to_one() -> None:
    """``audit_schema_version`` is ``Literal[1]`` and defaults to 1."""
    event = _make_event()
    assert event.audit_schema_version == 1
    # Constructing with a different value should fail validation.
    with pytest.raises(ValidationError):
        GradeEvent(
            audit_schema_version=2,  # type: ignore[arg-type]
            signalforge_version="0.1.0.dev0",
            run_id="a1b2c3d4e5f6478890aabbccddeeff00",
            timestamp=datetime(2026, 5, 1, 17, 42, 13, tzinfo=UTC),
            model_unique_id="model.shop.dim_customers",
            artifact_id="column.email.description",
            criterion_id="clarity",
            score=0.8,
            passed=True,
            rubric_hash="0123456789abcdef",
            prompt_version_template="fedcba9876543210",
            criterion_prompt_hash="1111222233334444",
            response_text_hash="5555666677778888",
            model="claude-sonnet-4-6",
            input_tokens=1820,
            output_tokens=140,
        )


def test_grading_report_grade_schema_version_locked_to_one() -> None:
    """:attr:`GradingReport.grade_schema_version` is ``Literal[1]``."""
    report = _make_report(results=(_make_result(),))
    assert report.grade_schema_version == 1
    with pytest.raises(ValidationError):
        GradingReport(
            grade_schema_version=2,  # type: ignore[arg-type]
            signalforge_version="0.1.0.dev0",
            run_id="a1b2c3d4e5f6478890aabbccddeeff00",
            timestamp=datetime(2026, 5, 1, 17, 42, 13, tzinfo=UTC),
            duration_seconds=1.0,
            model_unique_id="model.shop.dim_customers",
            rubric_hash="0123456789abcdef",
            thresholds=(0.7, 0.5),
            results=(_make_result(),),
        )


# --- frozen + transitive immutability --------------------------------------


def test_grading_result_is_frozen() -> None:
    """Pydantic ``frozen=True`` blocks attribute reassignment."""
    result = _make_result()
    with pytest.raises(ValidationError):
        result.score = 0.1  # type: ignore[misc]


def test_grading_report_is_frozen() -> None:
    """:class:`GradingReport` is also frozen."""
    report = _make_report(results=(_make_result(),))
    with pytest.raises(ValidationError):
        report.duration_seconds = 99.0  # type: ignore[misc]


def test_grade_event_is_frozen() -> None:
    """:class:`GradeEvent` is also frozen."""
    event = _make_event()
    with pytest.raises(ValidationError):
        event.score = 0.1  # type: ignore[misc]
