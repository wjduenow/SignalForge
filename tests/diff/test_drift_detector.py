"""Schema-drift detection for the diff renderer (US-011, DEC-003 of #8).

Pairs production ``extra="ignore"`` models with ``extra="forbid"``
``Strict<X>`` mirrors validated against committed JSON fixtures.
Adding a field to a production model without updating the strict
mirror OR the fixture breaks the test loudly.

Mirrors :mod:`tests.grade.test_drift_detector` and
:mod:`tests.prune.test_drift_detector` shape verbatim. The two
diff-layer read-back models covered here:

* :class:`signalforge.diff.models.DiffEntry`
* :class:`signalforge.diff.models.DiffReport`

Unlike the prune / grade layers, the diff renderer has no fail-closed
audit JSONL — DEC-018 of the plan documents the explicit decision NOT
to add a 7th AST scan in v0.1 (the diff renderer projects already-
audited decisions; there's no new audit-event class to gate). The
sidecar :class:`DiffReport` is read-back, gated by these mirrors.

This module is paired with :mod:`tests.diff.test_models` (US-002),
which carries an equivalent set of strict-mirror tests inline. The
duplication is intentional: US-011 explicitly calls for a separate
``test_drift_detector.py`` so the mirror lives next to the fixture
matrix it gates, mirroring the prune / grade precedent. A future
consolidation can drop the duplicate; for v0.1 the explicit pairing
keeps the rules-doc reference (DEC-003) directly traceable to a file
named ``test_drift_detector.py``.

Reference: ``.claude/rules/manifest-readers.md`` (drift detectors
mandatory for ``extra="ignore"`` reader-shaped models),
``.claude/rules/grade-layer.md`` (drift detectors are mandatory for
read-back models),
``.claude/rules/prune-engine.md`` DEC-010 (production change == strict
change == fixture refresh, in the same commit),
``plans/super/8-diff-renderer.md`` US-011 + DEC-003.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from signalforge.diff.models import DiffEntry, DiffReport, ProposedTestFile, Tier
from signalforge.prune.models import DropReason

_STRICT = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "diff"


# ---------------------------------------------------------------------------
# Strict drift mirrors.
# ---------------------------------------------------------------------------


class StrictDiffEntry(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`DiffEntry`.

    If you add a field to :class:`DiffEntry`, you MUST:

    1. Add it here.
    2. Update :file:`tests/fixtures/diff/diff_entry_v1.json`
       AND :file:`tests/fixtures/diff/diff_report_v1.json`
       ``entries[*]`` to carry the new field.

    The field-set parity test below catches additions that arrive via
    one side but not the other.
    """

    model_config = _STRICT

    artifact_id: str
    test_type: str | None = None
    tier: Tier
    drop_reason: DropReason | None = None
    why: str = ""
    score: float | None = None
    passed: bool | None = None


class StrictProposedTestFile(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`ProposedTestFile` (#116)."""

    model_config = _STRICT

    path: str
    sql: str


class StrictDiffReport(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`DiffReport`.

    Stamps every field type ``DiffReport`` declares with the same
    typing — including the ``Literal[1]`` / ``Literal[3]`` schema
    sentinels and the nested ``StrictDiffEntry`` /
    ``StrictProposedTestFile`` tuples.
    """

    model_config = _STRICT

    schema_version: Literal[1] = 1
    audit_schema_version: Literal[3] = 3
    signalforge_version: str
    model_unique_id: str
    run_id: str
    duration_seconds: float
    proposed_yaml: str
    existing_yaml: str | None
    unified_diff: str
    entries: tuple[StrictDiffEntry, ...]
    proposed_test_files: tuple[StrictProposedTestFile, ...] = ()
    kept_count: int
    kept_uncertain_count: int
    dropped_count: int
    flagged_count: int
    has_existing_schema: bool
    candidate_hash: str
    prune_result_hash: str
    grading_report_hash: str | None


# ---------------------------------------------------------------------------
# Fixture validation.
# ---------------------------------------------------------------------------


def test_strict_diff_report_validates_fixture() -> None:
    """The :file:`diff_report_v1.json` fixture validates against
    :class:`StrictDiffReport`.

    If this raises, an unknown field was introduced in the fixture
    without being mirrored on :class:`StrictDiffReport` (or vice
    versa). Update production :class:`DiffReport`,
    :class:`StrictDiffReport`, and the fixture together.
    """
    fixture_path = _FIXTURES_DIR / "diff_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    StrictDiffReport.model_validate(payload)


def test_strict_diff_entry_validates_standalone_fixture() -> None:
    """The :file:`diff_entry_v1.json` fixture validates against
    :class:`StrictDiffEntry`.

    The standalone entry fixture exercises the dropped-tier path
    (``drop_reason`` populated, ``score``/``passed`` null) — a shape
    not exercised by the report-level fixture's first entry. Combined
    with :func:`test_strict_diff_entry_validates_each_report_entry`
    below, every :data:`Tier` literal lands in the strict-typing
    coverage.
    """
    fixture_path = _FIXTURES_DIR / "diff_entry_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    StrictDiffEntry.model_validate(payload)


def test_strict_diff_entry_validates_each_report_entry() -> None:
    """Each entry in :file:`diff_report_v1.json`'s ``entries`` validates
    against :class:`StrictDiffEntry`.

    The fixture is intentionally constructed to cover all four
    :data:`Tier` literals (``kept`` / ``kept-uncertain`` / ``dropped`` /
    ``flagged``); the assertion below pins that contract so a future
    fixture edit can't regress to a partial-tier shape and silently
    weaken the literal-typing coverage. ``kept-uncertain`` was added
    in issue #50 alongside the ``audit_schema_version: 2`` bump.
    """
    fixture_path = _FIXTURES_DIR / "diff_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    entries = payload["entries"]
    assert isinstance(entries, list) and entries, (
        f"expected non-empty 'entries' array in {fixture_path}"
    )
    seen_tiers: set[str] = set()
    for entry in entries:
        StrictDiffEntry.model_validate(entry)
        seen_tiers.add(entry["tier"])
    assert seen_tiers == {"kept", "kept-uncertain", "dropped", "flagged"}, (
        "diff_report_v1.json must exercise all four Tier values "
        "(kept, kept-uncertain, dropped, flagged) so the literal typing "
        "on StrictDiffEntry is tested end-to-end"
    )


# ---------------------------------------------------------------------------
# Field-set parity.
# ---------------------------------------------------------------------------


def test_diff_entry_field_set_parity() -> None:
    """:class:`StrictDiffEntry` ``model_fields`` exactly match
    :class:`DiffEntry` ``model_fields``.
    """
    strict_fields = set(StrictDiffEntry.model_fields.keys())
    prod_fields = set(DiffEntry.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictDiffEntry is missing fields present in DiffEntry: "
        f"{missing_in_strict}. Update StrictDiffEntry to match."
    )
    assert not extra_in_strict, (
        f"StrictDiffEntry has fields absent from DiffEntry: "
        f"{extra_in_strict}. Remove from StrictDiffEntry or add to DiffEntry."
    )


def test_proposed_test_file_field_set_parity() -> None:
    """:class:`StrictProposedTestFile` ``model_fields`` exactly match
    :class:`ProposedTestFile` ``model_fields`` (#116).
    """
    strict_fields = set(StrictProposedTestFile.model_fields.keys())
    prod_fields = set(ProposedTestFile.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictProposedTestFile is missing fields present in ProposedTestFile: "
        f"{missing_in_strict}. Update StrictProposedTestFile to match."
    )
    assert not extra_in_strict, (
        f"StrictProposedTestFile has fields absent from ProposedTestFile: "
        f"{extra_in_strict}. Remove from StrictProposedTestFile or add to ProposedTestFile."
    )


def test_diff_report_field_set_parity() -> None:
    """:class:`StrictDiffReport` ``model_fields`` exactly match
    :class:`DiffReport` ``model_fields``.
    """
    strict_fields = set(StrictDiffReport.model_fields.keys())
    prod_fields = set(DiffReport.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictDiffReport is missing fields present in DiffReport: "
        f"{missing_in_strict}. Update StrictDiffReport to match."
    )
    assert not extra_in_strict, (
        f"StrictDiffReport has fields absent from DiffReport: "
        f"{extra_in_strict}. Remove from StrictDiffReport or add to DiffReport."
    )


# ---------------------------------------------------------------------------
# Sanity floor — extra="forbid" actually fires.
# ---------------------------------------------------------------------------


def test_strict_diff_report_rejects_unknown_field() -> None:
    """Sanity floor: a fixture with an extra unknown field raises
    :class:`ValidationError`.

    Confirms ``extra="forbid"`` is wired up — a silently-accepted
    unknown field would defeat the entire drift gate. Mirrors
    ``test_strict_grade_event_rejects_unknown_field`` and
    ``test_strict_prune_event_rejects_unknown_field``.
    """
    fixture_path = _FIXTURES_DIR / "diff_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        StrictDiffReport.model_validate(payload)


def test_strict_diff_entry_rejects_unknown_field() -> None:
    """Same sanity floor for :class:`StrictDiffEntry`."""
    fixture_path = _FIXTURES_DIR / "diff_entry_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        StrictDiffEntry.model_validate(payload)
