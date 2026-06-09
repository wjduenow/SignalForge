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

from signalforge.grade.cache import CacheRecord
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
    degrade_reason_type: Literal["transient", "budget", "ceiling"] | None = None


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
    """One-off ``extra="forbid"`` mirror of :class:`GradeEvent` (v1).

    Mirrors the flat shape — every reproducibility / token / hash
    field at the top level so a reviewer can ``jq`` over the JSONL
    without descending levels.

    Pins the **v1** shape against :file:`grade_event_v1.jsonl` — the
    replay anchor for pre-#189 audit corpora. The production
    :class:`GradeEvent` widened ``audit_schema_version`` to ``int`` and
    bumped its default to ``2``, but the v1 fixture's
    ``audit_schema_version: 1`` line MUST keep validating against this
    mirror so a future schema drift on the v1 line still fails loudly.
    The sibling :class:`StrictGradeEventV2` below pins the v2 line.
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


class StrictGradeEventV2(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`GradeEvent` (v2).

    Pins the v2 shape introduced by #189 (DEC-008 / DEC-009 / DEC-010):
    ``audit_schema_version: Literal[2]`` and a new ``cache_hit: bool``
    field placed between ``response_text_hash`` and ``model`` so the
    reproducibility hashes stay adjacent. The v1 mirror above stays as
    the replay anchor; the sibling :class:`StrictGradeEventV3` below
    pins the v3 line and is the field-set-current mirror.

    Do NOT grow this mirror — it is frozen as the v2 replay anchor. New
    fields land on :class:`StrictGradeEventV3` instead.
    """

    model_config = _STRICT

    audit_schema_version: Literal[2] = 2
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
    cache_hit: bool = False
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class StrictGradeEventV3(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`GradeEvent` (v3).

    Pins the v3 shape introduced by #202 US-001 (DEC-203):
    ``audit_schema_version: Literal[3]`` plus the new
    ``degrade_reason_type: Literal["transient","budget","ceiling"] | None``
    discriminator placed after ``reasoning`` so the prose + its
    structured classification stay adjacent. #202 US-005 (DEC-206) added
    the additive optional ``sweep_round: int | None`` field (after
    ``cache_hit``) WITHOUT an ``audit_schema_version`` bump — a new
    defaulted field on an ``extra="ignore"`` read-back model does not
    require a version bump. The v1 and v2 mirrors above stay as replay
    anchors.

    This is the field-set-current mirror — production :class:`GradeEvent`
    drift is gated against it. If you add a field to production
    :class:`GradeEvent`, mirror it here AND refresh
    :file:`grade_event_v3.jsonl` in lockstep.
    """

    model_config = _STRICT

    audit_schema_version: Literal[3] = 3
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
    degrade_reason_type: Literal["transient", "budget", "ceiling"] | None = None
    rubric_hash: str
    prompt_version_template: str
    criterion_prompt_hash: str
    response_text_hash: str
    cache_hit: bool = False
    sweep_round: int | None = None
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class StrictCacheRecord(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`CacheRecord` (v1).

    Pins the v1 cache-record shape introduced by #189 (DEC-011) against
    :file:`grade_cache_record_v1.json`. Adding a field to production
    :class:`CacheRecord` without mirroring it here OR refreshing the
    fixture breaks this test loudly.

    Production :class:`CacheRecord` uses ``extra="ignore"`` for
    forward-compat; this strict mirror is the regression gate.
    """

    model_config = _STRICT

    cache_schema_version: int = 1
    artifact_id: str
    criterion_id: str
    score: float
    passed: bool
    evidence: str = ""
    reasoning: str = ""
    criterion_prompt_hash: str
    artifact_text_hash: str
    provider: str
    model: str
    prompt_version_template: str
    response_text_hash: str
    rubric_hash: str
    original_timestamp: datetime


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
    :class:`StrictGradeEvent` (the v1 mirror).

    The v1 fixture is the replay anchor for pre-#189 audit corpora —
    production widened ``audit_schema_version`` to ``int`` (US-001) so
    the v1 line still round-trips through production :class:`GradeEvent`
    too (covered by :func:`test_v1_fixture_still_validates_against_production_grade_event`
    below).
    """
    fixture_path = _FIXTURES_DIR / "grade_event_v1.jsonl"
    text = fixture_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, f"expected one-or-more JSONL lines in {fixture_path}"
    for line in lines:
        StrictGradeEvent.model_validate_json(line)


def test_strict_grade_event_v2_validates_jsonl_fixture() -> None:
    """Each line of :file:`grade_event_v2.jsonl` validates against
    :class:`StrictGradeEventV2`.

    Pins the v2 shape introduced by #189 (DEC-009 / DEC-010):
    ``audit_schema_version: 2`` plus the new ``cache_hit: bool`` field.
    The fixture intentionally carries TWO lines — one ``cache_hit: true``
    and one ``cache_hit: false`` — so a regression that flipped the field
    type or moved its position would fail loudly on at least one shape.
    """
    fixture_path = _FIXTURES_DIR / "grade_event_v2.jsonl"
    text = fixture_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) >= 2, (
        f"expected ≥2 JSONL lines in {fixture_path} (one cache_hit=true, one cache_hit=false)"
    )
    seen_cache_hit_true = False
    seen_cache_hit_false = False
    for line in lines:
        event = StrictGradeEventV2.model_validate_json(line)
        if event.cache_hit:
            seen_cache_hit_true = True
        else:
            seen_cache_hit_false = True
    assert seen_cache_hit_true, f"{fixture_path} must include at least one cache_hit=true line"
    assert seen_cache_hit_false, f"{fixture_path} must include at least one cache_hit=false line"


def test_strict_grade_event_v3_validates_jsonl_fixture() -> None:
    """Each line of :file:`grade_event_v3.jsonl` validates against
    :class:`StrictGradeEventV3`.

    Pins the v3 shape introduced by #202 US-001 (DEC-203):
    ``audit_schema_version: 3`` plus the new ``degrade_reason_type``
    discriminator, AND the additive ``sweep_round`` field from #202 US-005
    (DEC-206). The fixture carries five lines — one scored
    (``degrade_reason_type: null``), one for each of the three degrade
    causes (``"transient"`` / ``"budget"`` / ``"ceiling"``), plus a
    sweep-recovery line (``sweep_round: 1``) — so a regression that
    dropped, retyped, or mis-classified either field fails loudly on at
    least one shape.
    """
    fixture_path = _FIXTURES_DIR / "grade_event_v3.jsonl"
    text = fixture_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) >= 5, (
        f"expected ≥5 JSONL lines in {fixture_path} "
        f"(one scored + three degrade causes + one sweep-recovery)"
    )
    seen: set[str | None] = set()
    seen_sweep_rounds: set[int | None] = set()
    for line in lines:
        event = StrictGradeEventV3.model_validate_json(line)
        seen.add(event.degrade_reason_type)
        seen_sweep_rounds.add(event.sweep_round)
    assert seen == {None, "transient", "budget", "ceiling"}, (
        f"{fixture_path} must cover scored (null) + all three degrade reason types; saw {seen}"
    )
    # #202 US-005: the fixture must carry both a main-pass record
    # (sweep_round: null) and a sweep-tagged record (sweep_round >= 1).
    assert None in seen_sweep_rounds, (
        f"{fixture_path} must include a main-pass (sweep_round=null) row"
    )
    assert any(sr is not None and sr >= 1 for sr in seen_sweep_rounds), (
        f"{fixture_path} must include a sweep-tagged (sweep_round>=1) row; saw {seen_sweep_rounds}"
    )


def test_v2_fixture_still_validates_against_production_grade_event() -> None:
    """Backward-compat: the v2 JSONL fixture (without
    ``degrade_reason_type``) round-trips through production
    :class:`GradeEvent` via ``extra="ignore"`` and the
    ``degrade_reason_type: ... | None = None`` default.

    Per #202 US-001: a v2 audit record from a pre-#202 corpus must still
    load through the current production model without a migration step —
    ``degrade_reason_type`` defaults to ``None`` (i.e. "this record
    predates the structured discriminator").
    """
    fixture_path = _FIXTURES_DIR / "grade_event_v2.jsonl"
    text = fixture_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, f"expected one-or-more JSONL lines in {fixture_path}"
    for line in lines:
        event = GradeEvent.model_validate_json(line)
        assert event.audit_schema_version == 2
        assert event.degrade_reason_type is None


def test_strict_grade_event_v3_rejects_unknown_field() -> None:
    """Sanity floor for :class:`StrictGradeEventV3` — an extra unknown
    field raises :class:`ValidationError` (confirms ``extra="forbid"``).
    """
    fixture_path = _FIXTURES_DIR / "grade_event_v3.jsonl"
    first_line = fixture_path.read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(first_line)
    payload["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        StrictGradeEventV3.model_validate(payload)


def test_v1_fixture_still_validates_against_production_grade_event() -> None:
    """Backward-compat: the v1 JSONL fixture (without ``cache_hit``)
    round-trips through production :class:`GradeEvent` via
    ``extra="ignore"`` and the ``cache_hit: bool = False`` default.

    Per DEC-009 of #189: a v1 audit record from a pre-#189 corpus must
    still load through the current production model without the
    operator running a migration step — ``cache_hit`` defaults to
    ``False`` (i.e. "this record predates cache support; treat as a
    live-grade record").
    """
    fixture_path = _FIXTURES_DIR / "grade_event_v1.jsonl"
    text = fixture_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, f"expected one-or-more JSONL lines in {fixture_path}"
    for line in lines:
        event = GradeEvent.model_validate_json(line)
        # The v1 line has audit_schema_version: 1 — production accepts
        # that because the field is typed ``int`` (DEC-008 of #189).
        assert event.audit_schema_version == 1
        # ``cache_hit`` defaults to False on a v1 record.
        assert event.cache_hit is False


def test_strict_cache_record_validates_committed_fixture() -> None:
    """The :file:`grade_cache_record_v1.json` fixture validates against
    :class:`StrictCacheRecord`.

    Production :class:`signalforge.grade.CacheRecord` uses
    ``extra="ignore"`` for forward-compat; this strict mirror catches
    silent schema drift before a future cache-corruption bug strikes
    in the field.
    """
    fixture_path = _FIXTURES_DIR / "grade_cache_record_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    StrictCacheRecord.model_validate(payload)


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


def test_cache_record_field_set_parity() -> None:
    """:class:`StrictCacheRecord` model_fields exactly match
    :class:`signalforge.grade.CacheRecord` model_fields.

    Adding a field to production :class:`CacheRecord` MUST land in
    :class:`StrictCacheRecord` AND refresh
    :file:`grade_cache_record_v1.json` in the same change.
    """
    strict_fields = set(StrictCacheRecord.model_fields.keys())
    prod_fields = set(CacheRecord.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictCacheRecord is missing fields present in CacheRecord: "
        f"{missing_in_strict}. Update StrictCacheRecord to match."
    )
    assert not extra_in_strict, (
        f"StrictCacheRecord has fields absent from CacheRecord: "
        f"{extra_in_strict}. Remove from StrictCacheRecord or add to "
        f"CacheRecord."
    )


def test_grade_event_field_set_parity() -> None:
    """:class:`StrictGradeEventV3` model_fields exactly match
    :class:`GradeEvent` model_fields.

    The v3 mirror is the field-set-current shape; the v1 / v2 mirrors
    intentionally lag (no ``cache_hit`` / no ``degrade_reason_type``)
    because they pin earlier fixtures' replay-compatibility surfaces.
    Production drift is gated against the v3 mirror — if you add a field
    to :class:`GradeEvent`, add it to :class:`StrictGradeEventV3` AND
    refresh :file:`grade_event_v3.jsonl` in the same change.
    """
    strict_fields = set(StrictGradeEventV3.model_fields.keys())
    prod_fields = set(GradeEvent.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictGradeEventV3 is missing fields present in GradeEvent: "
        f"{missing_in_strict}. Update StrictGradeEventV3 to match."
    )
    assert not extra_in_strict, (
        f"StrictGradeEventV3 has fields absent from GradeEvent: "
        f"{extra_in_strict}. Remove from StrictGradeEventV3 or add to "
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


def test_strict_grade_event_v2_rejects_unknown_field() -> None:
    """Same sanity floor for :class:`StrictGradeEventV2`."""
    fixture_path = _FIXTURES_DIR / "grade_event_v2.jsonl"
    first_line = fixture_path.read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(first_line)
    payload["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        StrictGradeEventV2.model_validate(payload)


def test_strict_grading_report_rejects_unknown_field() -> None:
    """Same sanity floor for :class:`StrictGradingReport`."""
    fixture_path = _FIXTURES_DIR / "grade_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        StrictGradingReport.model_validate(payload)
