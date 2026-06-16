"""Schema-stability drift detector for the Airflow drift core (US-002, DEC-017 of #235).

Pairs the production ``extra="ignore"`` read-back models from
:mod:`signalforge.airflow.drift` with ``extra="forbid"`` ``Strict<X>``
mirrors validated against the committed fixture
:file:`tests/fixtures/airflow/drift_report_v1.json`. Adding a field to a
production model without updating the strict mirror OR the fixture breaks
this test loudly.

**Ungated** (NO ``@pytest.mark.airflow``): :class:`DriftReport` and its
sub-models are airflow-free (DEC-002 — the pure core imports no airflow), so
the drift detector runs in the default pytest suite without an Apache Airflow
install.

Mirrors :mod:`tests.grade.test_drift_detector` and
:mod:`tests.diff.test_drift_detector` shape verbatim. The four
airflow-drift read-back models covered here:

* :class:`signalforge.airflow.drift.DriftArtifact`
* :class:`signalforge.airflow.drift.GradeRegression`
* :class:`signalforge.airflow.drift.SchemaShapeDelta`
* :class:`signalforge.airflow.drift.DriftReport`

Reference: ``.claude/rules/manifest-readers.md`` (drift detectors
mandatory for ``extra="ignore"`` reader-shaped models),
``.claude/rules/diff-renderer.md`` DEC-003 (pair every read-back model with
a one-off ``extra="forbid"`` mirror + committed fixture),
``.claude/rules/testing-signal.md`` (the drift-detector mandate),
``plans/super/235-drift-detection.md`` DEC-004 (the field set) + DEC-017
(this task).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from signalforge.airflow.drift import (
    DriftArtifact,
    DriftReport,
    GradeRegression,
    SchemaShapeDelta,
)

_STRICT = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "airflow"


# ---------------------------------------------------------------------------
# Strict drift mirrors.
# ---------------------------------------------------------------------------


class StrictDriftArtifact(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`DriftArtifact`.

    If you add a field to :class:`DriftArtifact`, you MUST:

    1. Add it here, and
    2. Update :file:`tests/fixtures/airflow/drift_report_v1.json` (each of
       the three transition lists carries a populated artifact).
    """

    model_config = _STRICT

    artifact_id: str
    previous_tier: str | None
    current_tier: str | None
    previous_drop_reason: str | None
    current_drop_reason: str | None
    why: str = ""


class StrictGradeRegression(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`GradeRegression`."""

    model_config = _STRICT

    model_unique_id: str
    previous_mean: float
    current_mean: float
    delta: float


class StrictSchemaShapeDelta(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`SchemaShapeDelta`."""

    model_config = _STRICT

    columns_added: tuple[str, ...] = ()
    columns_removed: tuple[str, ...] = ()


class StrictDriftReport(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`DriftReport`.

    Stamps every field type :class:`DriftReport` declares with the same
    typing — including the ``Literal[1]`` schema sentinel, the ``date | None``
    ``as_of``, and the nested ``StrictDriftArtifact`` / ``StrictGradeRegression``
    tuples + the nested ``StrictSchemaShapeDelta``.

    The computed ``alarming`` property lives on production :class:`DriftReport`
    as a plain ``@property`` (NOT a Pydantic ``computed_field``), so it is
    absent from ``model_fields`` and is not part of the field-set parity below.
    """

    model_config = _STRICT

    schema_version: Literal[1] = 1
    signalforge_version: str
    model_unique_id: str
    as_of: date | None
    grade_regression_threshold: float
    baseline: bool = False
    previous_diff_hash: str
    current_diff_hash: str
    newly_always_passes: tuple[StrictDriftArtifact, ...] = ()
    newly_dropped: tuple[StrictDriftArtifact, ...] = ()
    newly_kept: tuple[StrictDriftArtifact, ...] = ()
    added_artifacts: tuple[str, ...] = ()
    removed_artifacts: tuple[str, ...] = ()
    grade_regressions: tuple[StrictGradeRegression, ...] = ()
    schema_shape_changes: StrictSchemaShapeDelta = StrictSchemaShapeDelta()
    degrade_reason: str | None = None


# ---------------------------------------------------------------------------
# Fixture validation.
# ---------------------------------------------------------------------------


def test_strict_drift_report_validates_fixture() -> None:
    """The :file:`drift_report_v1.json` fixture validates against
    :class:`StrictDriftReport`.

    If this raises, an unknown field was introduced in the fixture without
    being mirrored on :class:`StrictDriftReport` (or vice versa). Update
    production :class:`DriftReport`, :class:`StrictDriftReport`, and the
    fixture together.
    """
    fixture_path = _FIXTURES_DIR / "drift_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    StrictDriftReport.model_validate(payload)


def test_fixture_exercises_every_drift_report_field() -> None:
    """The committed fixture populates EVERY :class:`DriftReport` field —
    including at least one entry in each transition list, one grade
    regression, and a non-empty schema-shape delta.

    Without this, a fixture edit could quietly drop a transition list to
    empty and the strict mirror would still validate, silently weakening the
    typing coverage on the nested ``Strict*`` tuples.
    """
    fixture_path = _FIXTURES_DIR / "drift_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))

    # Every top-level field name from production is present as a JSON key.
    prod_fields = set(DriftReport.model_fields.keys())
    fixture_keys = set(payload.keys())
    assert prod_fields <= fixture_keys, (
        f"drift_report_v1.json is missing DriftReport fields: {prod_fields - fixture_keys}"
    )

    # The three transition lists each carry at least one populated artifact.
    assert payload["newly_always_passes"], "fixture must populate newly_always_passes"
    assert payload["newly_dropped"], "fixture must populate newly_dropped"
    assert payload["newly_kept"], "fixture must populate newly_kept"
    # One grade regression + a non-empty schema-shape delta.
    assert payload["grade_regressions"], "fixture must populate grade_regressions"
    assert payload["schema_shape_changes"]["columns_added"], (
        "fixture must populate schema_shape_changes.columns_added"
    )
    assert payload["schema_shape_changes"]["columns_removed"], (
        "fixture must populate schema_shape_changes.columns_removed"
    )
    # Informational artifact lists are populated too.
    assert payload["added_artifacts"], "fixture must populate added_artifacts"
    assert payload["removed_artifacts"], "fixture must populate removed_artifacts"


def test_strict_drift_artifact_validates_each_transition_entry() -> None:
    """Each artifact in every transition list of :file:`drift_report_v1.json`
    validates against :class:`StrictDriftArtifact` (``extra="forbid"``).

    Exercises the ``previous_drop_reason``/``current_drop_reason``
    ``str | None`` typing end-to-end: across the three lists the fixture
    covers both a populated ``current_drop_reason`` (signal rot →
    ``always-passes``) and a null one (newly kept).
    """
    fixture_path = _FIXTURES_DIR / "drift_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    artifacts = (
        payload["newly_always_passes"] + payload["newly_dropped"] + payload["newly_kept"]
    )
    assert artifacts, "expected at least one transition artifact in the fixture"
    saw_drop_reason = False
    saw_null_drop_reason = False
    for entry in artifacts:
        StrictDriftArtifact.model_validate(entry)
        if entry["current_drop_reason"] is None:
            saw_null_drop_reason = True
        else:
            saw_drop_reason = True
    assert saw_drop_reason, (
        "fixture must include a transition with a populated current_drop_reason"
    )
    assert saw_null_drop_reason, (
        "fixture must include a transition with a null current_drop_reason"
    )


# ---------------------------------------------------------------------------
# Field-set parity.
# ---------------------------------------------------------------------------


def test_drift_artifact_field_set_parity() -> None:
    """:class:`StrictDriftArtifact` ``model_fields`` exactly match
    :class:`DriftArtifact` ``model_fields``.
    """
    strict_fields = set(StrictDriftArtifact.model_fields.keys())
    prod_fields = set(DriftArtifact.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictDriftArtifact is missing fields present in DriftArtifact: "
        f"{missing_in_strict}. Update StrictDriftArtifact to match."
    )
    assert not extra_in_strict, (
        f"StrictDriftArtifact has fields absent from DriftArtifact: "
        f"{extra_in_strict}. Remove from StrictDriftArtifact or add to DriftArtifact."
    )


def test_grade_regression_field_set_parity() -> None:
    """:class:`StrictGradeRegression` ``model_fields`` exactly match
    :class:`GradeRegression` ``model_fields``.
    """
    strict_fields = set(StrictGradeRegression.model_fields.keys())
    prod_fields = set(GradeRegression.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictGradeRegression is missing fields present in GradeRegression: "
        f"{missing_in_strict}. Update StrictGradeRegression to match."
    )
    assert not extra_in_strict, (
        f"StrictGradeRegression has fields absent from GradeRegression: "
        f"{extra_in_strict}. Remove from StrictGradeRegression or add to GradeRegression."
    )


def test_schema_shape_delta_field_set_parity() -> None:
    """:class:`StrictSchemaShapeDelta` ``model_fields`` exactly match
    :class:`SchemaShapeDelta` ``model_fields``.
    """
    strict_fields = set(StrictSchemaShapeDelta.model_fields.keys())
    prod_fields = set(SchemaShapeDelta.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictSchemaShapeDelta is missing fields present in SchemaShapeDelta: "
        f"{missing_in_strict}. Update StrictSchemaShapeDelta to match."
    )
    assert not extra_in_strict, (
        f"StrictSchemaShapeDelta has fields absent from SchemaShapeDelta: "
        f"{extra_in_strict}. Remove from StrictSchemaShapeDelta or add to SchemaShapeDelta."
    )


def test_drift_report_field_set_parity() -> None:
    """:class:`StrictDriftReport` ``model_fields`` exactly match
    :class:`DriftReport` ``model_fields``.

    ``alarming`` is a plain ``@property`` on production :class:`DriftReport`
    (NOT a Pydantic ``computed_field``), so it lives on neither
    ``model_fields`` nor ``model_computed_fields`` — it is not part of this
    comparison.
    """
    strict_fields = set(StrictDriftReport.model_fields.keys())
    prod_fields = set(DriftReport.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictDriftReport is missing fields present in DriftReport: "
        f"{missing_in_strict}. Update StrictDriftReport to match."
    )
    assert not extra_in_strict, (
        f"StrictDriftReport has fields absent from DriftReport: "
        f"{extra_in_strict}. Remove from StrictDriftReport or add to DriftReport."
    )


# ---------------------------------------------------------------------------
# The standard pair: production extra="ignore", strict mirror extra="forbid".
# ---------------------------------------------------------------------------


def test_production_models_are_extra_ignore_and_strict_mirrors_are_extra_forbid() -> None:
    """Production read-back models use ``extra="ignore"`` (forward-compat);
    their strict mirrors use ``extra="forbid"`` (the drift gate).

    This is the standard drift-detector pair (``.claude/rules/diff-renderer.md``
    DEC-003 / ``manifest-readers.md``): production tolerates an upstream field
    addition silently, while the strict mirror fails loudly so the addition is
    caught and mirrored in the same change.
    """
    production = (DriftArtifact, GradeRegression, SchemaShapeDelta, DriftReport)
    for model in production:
        assert model.model_config.get("extra") == "ignore", (
            f"{model.__name__} must use extra='ignore' for forward-compat"
        )
    strict = (
        StrictDriftArtifact,
        StrictGradeRegression,
        StrictSchemaShapeDelta,
        StrictDriftReport,
    )
    for model in strict:
        assert model.model_config.get("extra") == "forbid", (
            f"{model.__name__} must use extra='forbid' to gate schema drift"
        )


# ---------------------------------------------------------------------------
# Sanity floor — extra="forbid" actually fires.
# ---------------------------------------------------------------------------


def test_strict_drift_report_rejects_unknown_field() -> None:
    """Sanity floor: a fixture with an extra unknown field raises
    :class:`ValidationError`.

    Confirms ``extra="forbid"`` is wired up — a silently-accepted unknown
    field would defeat the entire drift gate. Mirrors
    ``test_strict_diff_report_rejects_unknown_field`` and
    ``test_strict_grade_event_rejects_unknown_field``.
    """
    fixture_path = _FIXTURES_DIR / "drift_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        StrictDriftReport.model_validate(payload)


def test_strict_drift_artifact_rejects_unknown_field() -> None:
    """Same sanity floor for :class:`StrictDriftArtifact`."""
    fixture_path = _FIXTURES_DIR / "drift_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    entry = dict(payload["newly_always_passes"][0])
    entry["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        StrictDriftArtifact.model_validate(entry)
