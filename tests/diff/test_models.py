"""Tests for ``signalforge.diff.models`` (US-002 of #8; DEC-012, DEC-016, DEC-020).

Covers the two read-back-stable Pydantic shapes the diff renderer emits:
:class:`DiffEntry` (one row per artifact) and :class:`DiffReport` (the
sidecar shape carrying the unified diff + per-entry tuple +
reproducibility hashes).

Tests fall into four groups:

1. **Construction + shape gates** — :class:`DiffEntry` / :class:`DiffReport`
   accept their canonical field set; ``frozen=True`` blocks attribute
   reassignment; ``DiffEntry.tier`` rejects values outside the
   :data:`signalforge.diff.models.Tier` literal.
2. **Round-trip** — ``model_dump_json`` round-trips back via
   ``model_validate_json`` so the orchestrator can persist a sidecar
   and a downstream reader can rehydrate it without lossy coercion.
3. **Custom ``__repr__`` (DEC-020)** — neither model's repr exposes
   prose / large-text fields. Mirrors the assertions in
   :mod:`tests.prune.test_models` (PruneDecision/PruneResult repr
   discipline) and :mod:`tests.grade.test_models` (GradingResult/Report
   repr discipline).
4. **Drift detection (mirrors prune / grade)** — one-off ``Strict<X>``
   ``extra="forbid"`` mirrors validate the committed
   :file:`tests/fixtures/diff/diff_report_v1.json` fixture; field-set
   parity confirms the strict mirrors carry every field on the
   production models. The sanity-floor "rejects unknown field" test
   confirms ``extra="forbid"`` is wired up — a silently-accepted
   unknown field would defeat the entire drift gate.

Reference: ``docs/rules/manifest-readers.md`` (drift detectors
mandatory for ``extra="ignore"`` reader-shaped models),
``docs/rules/prune-engine.md`` DEC-010 (production change == strict
change == fixture refresh, in the same commit), and
``plans/super/8-diff-renderer.md`` US-002.
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


# --- Strict drift mirrors --------------------------------------------------


class StrictDiffEntry(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`DiffEntry`.

    If you add a field to :class:`DiffEntry`, you MUST:

    1. Add it here, and
    2. Update :file:`tests/fixtures/diff/diff_report_v1.json`
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
    """One-off ``extra="forbid"`` mirror of :class:`DiffReport`."""

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


# --- Construction + shape gates --------------------------------------------


def _sample_entry(**overrides: object) -> DiffEntry:
    """Build a canonical-shape :class:`DiffEntry` for tests, allowing overrides."""
    base: dict[str, object] = {
        "artifact_id": "column.customer_id.description",
        "test_type": None,
        "tier": "kept",
        "drop_reason": None,
        "why": "Description added; passed all grading criteria.",
        "score": 0.85,
        "passed": True,
    }
    base.update(overrides)
    return DiffEntry(**base)  # type: ignore[arg-type]


def _sample_report(**overrides: object) -> DiffReport:
    """Build a canonical-shape :class:`DiffReport` for tests, allowing overrides."""
    base: dict[str, object] = {
        "signalforge_version": "0.1.0.dev0",
        "model_unique_id": "model.shop.dim_customers",
        "run_id": "f0e1d2c3b4a5968778695a4b3c2d1e0f",
        "duration_seconds": 1.234,
        "proposed_yaml": "version: 2\n",
        "existing_yaml": "version: 2\n",
        "unified_diff": "--- a/x\n+++ b/x\n",
        "entries": (_sample_entry(),),
        "kept_count": 1,
        "kept_uncertain_count": 0,
        "dropped_count": 0,
        "flagged_count": 0,
        "has_existing_schema": True,
        "candidate_hash": "0123456789abcdef",
        "prune_result_hash": "fedcba9876543210",
        "grading_report_hash": None,
    }
    base.update(overrides)
    return DiffReport(**base)  # type: ignore[arg-type]


def test_diff_entry_constructs_with_canonical_field_set() -> None:
    """Happy-path: :class:`DiffEntry` accepts its documented field set."""
    entry = _sample_entry()
    assert entry.artifact_id == "column.customer_id.description"
    assert entry.tier == "kept"
    assert entry.score == pytest.approx(0.85)
    assert entry.passed is True


def test_diff_entry_is_frozen() -> None:
    """``frozen=True`` blocks attribute reassignment (DEC-020 + manifest-readers convention)."""
    entry = _sample_entry()
    with pytest.raises(ValidationError):
        entry.tier = "dropped"  # type: ignore[misc]


def test_diff_entry_rejects_invalid_tier_literal() -> None:
    """``DiffEntry.tier`` rejects a value outside the :data:`Tier` literal (DEC-012)."""
    with pytest.raises(ValidationError):
        _sample_entry(tier="quux")


def test_diff_entry_accepts_kept_uncertain_tier() -> None:
    """Issue #50: ``DiffEntry.tier`` accepts ``"kept-uncertain"``.

    Positive sanity test paired with the negative one above — confirms
    the fourth :data:`Tier` literal is wired through the Pydantic
    validator. A regression that drops the literal value from the
    closed set would break this test before any rendering happens.
    """
    entry = _sample_entry(
        tier="kept-uncertain",
        drop_reason=None,
        why="total prune budget exceeded before evaluation",
        score=None,
        passed=None,
    )
    assert entry.tier == "kept-uncertain"


def test_diff_entry_accepts_dropped_with_drop_reason() -> None:
    """A ``dropped`` entry carries a :data:`DropReason` literal."""
    entry = _sample_entry(
        tier="dropped",
        drop_reason="always-passes",
        why="Test returned zero failing rows on the representative sample.",
        score=None,
        passed=None,
    )
    assert entry.tier == "dropped"
    assert entry.drop_reason == "always-passes"


def test_diff_entry_rejects_invalid_drop_reason_literal() -> None:
    """``DiffEntry.drop_reason`` rejects a value outside :data:`DropReason`."""
    with pytest.raises(ValidationError):
        _sample_entry(tier="dropped", drop_reason="not-a-real-reason")


def test_diff_report_constructs_with_canonical_field_set() -> None:
    """Happy-path: :class:`DiffReport` accepts its documented field set."""
    report = _sample_report()
    assert report.schema_version == 1
    assert report.audit_schema_version == 3
    assert report.kept_count == 1
    assert report.kept_uncertain_count == 0
    assert report.has_existing_schema is True
    assert len(report.entries) == 1


def test_diff_report_is_frozen() -> None:
    """``frozen=True`` blocks attribute reassignment."""
    report = _sample_report()
    with pytest.raises(ValidationError):
        report.kept_count = 99  # type: ignore[misc]


def test_diff_report_existing_yaml_can_be_none() -> None:
    """``existing_yaml is None`` is the no-committed-schema branch."""
    report = _sample_report(existing_yaml=None, has_existing_schema=False)
    assert report.existing_yaml is None
    assert report.has_existing_schema is False


def test_diff_report_grading_report_hash_can_be_none() -> None:
    """``grading_report_hash is None`` when no grading report was provided."""
    report = _sample_report(grading_report_hash=None)
    assert report.grading_report_hash is None


def test_diff_report_proposed_test_files_defaults_empty() -> None:
    """``proposed_test_files`` defaults to an empty tuple (#116)."""
    report = _sample_report()
    assert report.proposed_test_files == ()


def test_proposed_test_file_constructs_and_is_frozen() -> None:
    """:class:`ProposedTestFile` accepts ``path`` + ``sql`` and is frozen (#116)."""
    proposed = ProposedTestFile(path="tests/m__custom_sql_deadbeef.sql", sql="select 1")
    assert proposed.path == "tests/m__custom_sql_deadbeef.sql"
    assert proposed.sql == "select 1"
    with pytest.raises(ValidationError):
        proposed.path = "x"  # type: ignore[misc]


def test_proposed_test_file_repr_omits_sql_body() -> None:
    """DEC-020-style minimal repr keeps the (large) SQL body out of logs (#116)."""
    proposed = ProposedTestFile(
        path="tests/m__custom_sql_deadbeef.sql",
        sql="select \x1b[31mevil\x1b[0m from x",
    )
    rendered = repr(proposed)
    assert "tests/m__custom_sql_deadbeef.sql" in rendered
    assert "evil" not in rendered


def test_diff_report_round_trips_proposed_test_files_through_json() -> None:
    """Sidecar round-trip: ``proposed_test_files`` survives model_dump_json (#116)."""
    proposed = ProposedTestFile(
        path="tests/customers__total_custom_sql_a1b2c3d4.sql",
        sql="-- signalforge:generated a1b2c3d4\n\nselect * from x where total < 0\n",
    )
    report = _sample_report(proposed_test_files=(proposed,))
    rehydrated = DiffReport.model_validate_json(report.model_dump_json(by_alias=True))
    assert rehydrated.proposed_test_files == (proposed,)


# --- Round-trip ------------------------------------------------------------


def test_diff_entry_round_trips_through_json() -> None:
    """``model_dump_json`` round-trips back via ``model_validate_json``."""
    original = _sample_entry()
    rehydrated = DiffEntry.model_validate_json(original.model_dump_json())
    assert rehydrated == original


def test_diff_report_round_trips_through_json() -> None:
    """:class:`DiffReport` round-trips through JSON, including nested entries."""
    original = _sample_report(
        entries=(
            _sample_entry(),
            _sample_entry(
                artifact_id="test.column.email.unique",
                test_type="unique",
                tier="dropped",
                drop_reason="always-passes",
                why="Zero failing rows on sample.",
                score=None,
                passed=None,
            ),
        ),
        kept_count=1,
        dropped_count=1,
        flagged_count=0,
    )
    rehydrated = DiffReport.model_validate_json(original.model_dump_json())
    assert rehydrated == original


# --- Custom __repr__ (DEC-020) --------------------------------------------


def test_diff_entry_repr_omits_prose_why() -> None:
    """:meth:`DiffEntry.__repr__` does NOT include the prose ``why`` (DEC-020)."""
    secret_marker = "PROSE-SHOULD-NOT-APPEAR-IN-REPR"
    entry = _sample_entry(why=f"This {secret_marker} should be redacted.")
    rendered = repr(entry)
    assert secret_marker not in rendered, (
        "DiffEntry.__repr__ must omit the prose `why` field (DEC-020)"
    )


def test_diff_entry_repr_includes_identifying_fields() -> None:
    """:meth:`DiffEntry.__repr__` exposes the identifying tuple per DEC-020."""
    entry = _sample_entry(
        artifact_id="column.customer_id.description",
        tier="dropped",
        drop_reason="always-passes",
        score=0.42,
    )
    rendered = repr(entry)
    assert "DiffEntry(" in rendered
    assert "column.customer_id.description" in rendered
    assert "'dropped'" in rendered or "dropped" in rendered
    assert "always-passes" in rendered
    assert "0.42" in rendered


def test_diff_report_repr_omits_unified_diff() -> None:
    """:meth:`DiffReport.__repr__` does NOT include the unified diff body (DEC-020)."""
    secret_marker = "DIFF-BODY-SHOULD-NOT-APPEAR-IN-REPR"
    report = _sample_report(unified_diff=f"--- a/x\n+++ b/x\n@@ {secret_marker} @@\n")
    rendered = repr(report)
    assert secret_marker not in rendered, (
        "DiffReport.__repr__ must omit the unified_diff field (DEC-020)"
    )


def test_diff_report_repr_omits_proposed_and_existing_yaml() -> None:
    """:meth:`DiffReport.__repr__` does NOT include the raw YAML payloads."""
    proposed_marker = "PROPOSED-YAML-SECRET-MARKER"
    existing_marker = "EXISTING-YAML-SECRET-MARKER"
    report = _sample_report(
        proposed_yaml=f"# {proposed_marker}\n",
        existing_yaml=f"# {existing_marker}\n",
    )
    rendered = repr(report)
    assert proposed_marker not in rendered
    assert existing_marker not in rendered


def test_diff_report_repr_omits_entries_payload() -> None:
    """:meth:`DiffReport.__repr__` does NOT serialise the per-entry tuple.

    A single entry's ``why`` field appearing in the parent repr would
    drag prose content out via the report-level log path even if the
    entry's own repr is correct.
    """
    secret_why = "ENTRY-WHY-SHOULD-NOT-APPEAR-IN-REPORT-REPR"
    report = _sample_report(entries=(_sample_entry(why=secret_why),))
    rendered = repr(report)
    assert secret_why not in rendered


def test_diff_report_repr_includes_summary_fields() -> None:
    """:meth:`DiffReport.__repr__` exposes summary aggregates per DEC-020."""
    report = _sample_report(
        kept_count=3,
        dropped_count=1,
        flagged_count=2,
        has_existing_schema=False,
        existing_yaml=None,
    )
    rendered = repr(report)
    assert "DiffReport(" in rendered
    assert "model.shop.dim_customers" in rendered
    assert "kept_count=3" in rendered
    assert "dropped_count=1" in rendered
    assert "flagged_count=2" in rendered
    assert "has_existing_schema=False" in rendered


# --- Drift detection (mirrors prune / grade) ------------------------------


def test_strict_diff_report_validates_fixture() -> None:
    """The :file:`diff_report_v1.json` fixture validates against
    :class:`StrictDiffReport`.

    If this raises, an unknown field was introduced in the fixture
    without being mirrored on :class:`StrictDiffReport` (or vice versa).
    Update production :class:`DiffReport`, :class:`StrictDiffReport`,
    and the fixture together.
    """
    fixture_path = _FIXTURES_DIR / "diff_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    StrictDiffReport.model_validate(payload)


def test_strict_diff_entry_validates_each_fixture_entry() -> None:
    """Each entry in ``diff_report_v1.json``'s ``entries`` validates
    against :class:`StrictDiffEntry` (``extra="forbid"``).

    The fixture intentionally exercises all four :data:`Tier` values
    (``kept`` / ``kept-uncertain`` / ``dropped`` / ``flagged``) so the
    literal-typing on the strict mirror is exercised end-to-end. The
    ``kept-uncertain`` literal was added in issue #50.
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
        "(kept, kept-uncertain, dropped, flagged) so the literal "
        "typing on StrictDiffEntry is tested end-to-end"
    )


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


# --- Sanity floor: extra="forbid" actually fires --------------------------


def test_strict_diff_report_rejects_unknown_field() -> None:
    """Sanity floor: a fixture with an extra unknown field raises
    :class:`ValidationError`. Confirms ``extra="forbid"`` is wired up —
    a silently-accepted unknown field would defeat the drift gate.
    """
    fixture_path = _FIXTURES_DIR / "diff_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        StrictDiffReport.model_validate(payload)


def test_strict_diff_entry_rejects_unknown_field() -> None:
    """Same sanity floor for :class:`StrictDiffEntry`."""
    fixture_path = _FIXTURES_DIR / "diff_report_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    first_entry = payload["entries"][0]
    first_entry["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        StrictDiffEntry.model_validate(first_entry)
