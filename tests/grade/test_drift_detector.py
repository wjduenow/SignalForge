"""Schema-drift detection for the grader (US-002, DEC-010 of #6).

Pairs production ``extra="ignore"`` models with ``extra="forbid"``
``Strict<X>`` mirrors validated against committed JSON / JSONL fixtures.
Adding a field to a production model without updating the strict mirror
OR the fixture breaks the test loudly.

Mirrors :mod:`tests.prune.test_drift_detector` shape verbatim. The three
grader-layer read-back models covered here:

* :class:`signalforge.grade.models.GradingResult`
* :class:`signalforge.grade.models.GradingReport`
* :class:`signalforge.grade.models.GradeEvent`

Reference: ``.claude/rules/manifest-readers.md`` (drift detectors
mandatory for ``extra="ignore"`` reader-shaped models),
``.claude/rules/safety-layer.md`` DEC-014 / DEC-015 (pair every
read-back model with a one-off ``extra="forbid"`` mirror),
``.claude/rules/prune-engine.md`` DEC-010 (production change == strict
change == fixture refresh, in the same commit).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from signalforge.grade.models import GradeEvent, GradingReport, GradingResult

_STRICT = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "grade"


class StrictGradingResult(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`GradingResult`.

    If you add a field to :class:`GradingResult`, you MUST:

    1. Add it here, and
    2. Update every fixture that carries a ``GradingResult`` shape
       (``grade_report_v1.json`` ``results[*]``).

    Note: production :class:`GradingResult` exposes ``one_line_why`` as
    a :func:`pydantic.computed_field` property — it lives in
    ``model_computed_fields``, NOT in ``model_fields``, so the
    field-set parity test does not need to filter it out.
    """

    model_config = _STRICT

    artifact_id: str
    criterion_id: str
    score: float | None
    passed: bool
    evidence: str = ""
    reasoning: str = ""


class StrictGradingReport(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`GradingReport`.

    Computed-field aggregates (:attr:`pass_rate`, :attr:`mean_score`,
    :attr:`aggregate_complete`, :attr:`passed`) live in
    ``model_computed_fields`` — the field-set parity test only compares
    stored-field sets.
    """

    model_config = _STRICT

    grade_schema_version: Literal[1] = 1
    signalforge_version: str
    run_id: str
    timestamp: datetime
    duration_seconds: float
    model_unique_id: str
    rubric_hash: str
    thresholds: tuple[float, float]
    results: tuple[StrictGradingResult, ...]


class StrictGradeEvent(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`GradeEvent`.

    Mirrors the flat shape — every reproducibility / token / hash
    field at the top level so a reviewer can ``jq`` over the JSONL
    without descending levels.
    """

    model_config = _STRICT

    audit_schema_version: Literal[1] = 1
    signalforge_version: str
    run_id: str
    timestamp: datetime
    model_unique_id: str
    artifact_id: str
    criterion_id: str
    score: float | None
    passed: bool
    evidence: str = ""
    reasoning: str = ""
    rubric_hash: str
    prompt_version_template: str
    criterion_prompt_hash: str
    response_text_hash: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


# --- Fixture validation ----------------------------------------------------


def test_strict_grading_report_validates_fixture() -> None:
    """The :file:`grade_report_v1.json` fixture validates against
    :class:`StrictGradingReport`.

    If this raises, an unknown field was introduced in the fixture
    without being mirrored on :class:`StrictGradingReport` (or vice
    versa). Update production :class:`GradingReport`,
    :class:`StrictGradingReport`, and the fixture together.
    """
    fixture_path = _FIXTURES_DIR / "grade_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    StrictGradingReport.model_validate(payload)


def test_strict_grading_result_validates_each_fixture_entry() -> None:
    """Each entry in ``grade_report_v1.json``'s ``results`` validates
    against :class:`StrictGradingResult` (``extra="forbid"``).

    The fixture intentionally includes a degraded-path row
    (``score: null``) so the strict mirror's ``score: float | None``
    typing is exercised end-to-end.
    """
    fixture_path = _FIXTURES_DIR / "grade_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    results = payload["results"]
    assert isinstance(results, list) and results, (
        f"expected non-empty 'results' array in {fixture_path}"
    )
    saw_null_score = False
    for entry in results:
        StrictGradingResult.model_validate(entry)
        if entry["score"] is None:
            saw_null_score = True
    assert saw_null_score, (
        "grade_report_v1.json must include at least one degraded-path "
        "row (score: null) to exercise DEC-015"
    )


def test_strict_grade_event_validates_jsonl_fixture() -> None:
    """Each line of :file:`grade_event_v1.jsonl` validates against
    :class:`StrictGradeEvent`.
    """
    fixture_path = _FIXTURES_DIR / "grade_event_v1.jsonl"
    text = fixture_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, f"expected one-or-more JSONL lines in {fixture_path}"
    for line in lines:
        StrictGradeEvent.model_validate_json(line)


# --- Field-set parity ------------------------------------------------------


def test_grading_result_field_set_parity() -> None:
    """:class:`StrictGradingResult` model_fields exactly match
    :class:`GradingResult` model_fields. ``one_line_why`` is a
    computed_field and lives in ``model_computed_fields``, so it is
    NOT part of this comparison.
    """
    strict_fields = set(StrictGradingResult.model_fields.keys())
    prod_fields = set(GradingResult.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictGradingResult is missing fields present in GradingResult: "
        f"{missing_in_strict}. Update StrictGradingResult to match."
    )
    assert not extra_in_strict, (
        f"StrictGradingResult has fields absent from GradingResult: "
        f"{extra_in_strict}. Remove from StrictGradingResult or add to "
        f"GradingResult."
    )


def test_grading_report_field_set_parity() -> None:
    """:class:`StrictGradingReport` model_fields exactly match
    :class:`GradingReport` model_fields.

    ``pass_rate`` / ``mean_score`` / ``aggregate_complete`` / ``passed``
    are computed_fields on production :class:`GradingReport` — they
    live in ``model_computed_fields``, NOT in ``model_fields``.
    """
    strict_fields = set(StrictGradingReport.model_fields.keys())
    prod_fields = set(GradingReport.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictGradingReport is missing fields present in GradingReport: "
        f"{missing_in_strict}. Update StrictGradingReport to match."
    )
    assert not extra_in_strict, (
        f"StrictGradingReport has fields absent from GradingReport: "
        f"{extra_in_strict}. Remove from StrictGradingReport or add to "
        f"GradingReport."
    )


def test_grade_event_field_set_parity() -> None:
    """:class:`StrictGradeEvent` model_fields exactly match
    :class:`GradeEvent` model_fields.
    """
    strict_fields = set(StrictGradeEvent.model_fields.keys())
    prod_fields = set(GradeEvent.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictGradeEvent is missing fields present in GradeEvent: "
        f"{missing_in_strict}. Update StrictGradeEvent to match."
    )
    assert not extra_in_strict, (
        f"StrictGradeEvent has fields absent from GradeEvent: "
        f"{extra_in_strict}. Remove from StrictGradeEvent or add to "
        f"GradeEvent."
    )


# --- Sanity floor: extra="forbid" actually fires ---------------------------


def test_strict_grade_event_rejects_unknown_field() -> None:
    """Sanity floor: a fixture line with an extra unknown field raises
    :class:`ValidationError`. Confirms ``extra="forbid"`` is wired up —
    a silently-accepted unknown field would defeat the entire drift gate.
    """
    fixture_path = _FIXTURES_DIR / "grade_event_v1.jsonl"
    first_line = fixture_path.read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(first_line)
    payload["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        StrictGradeEvent.model_validate(payload)


def test_strict_grading_report_rejects_unknown_field() -> None:
    """Same sanity floor for :class:`StrictGradingReport`."""
    fixture_path = _FIXTURES_DIR / "grade_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        StrictGradingReport.model_validate(payload)
