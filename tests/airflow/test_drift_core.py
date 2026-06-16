"""Ungated tests for the airflow-free drift core (US-001 of #235).

These tests run in the DEFAULT pytest suite — NO ``@pytest.mark.airflow``
marker — because :mod:`signalforge.airflow.drift` carries no ``from airflow``
import (DEC-002). They pin the transition classification (DEC-005), the
degrade taxonomy (DEC-013, compute side), the schema-shape derivation,
determinism (DEC-016), the ``alarming`` truth table, and the ``to_xcom`` shape
(DEC-015).

Each transition is *engineered* so the expected outcome is mathematically
guaranteed (``.claude/rules/testing-signal.md``): the two :class:`DiffReport`
inputs are built explicitly with the exact ``(prev_tier, curr_tier,
drop_reason)`` triples each classification arm keys on.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import signalforge
from signalforge.airflow import (
    DriftArtifact,
    GradeRegression,
    SchemaShapeDelta,
    compute_drift,
)
from signalforge.diff.models import DiffEntry, Tier
from signalforge.diff.models import DiffReport as SfDiffReport
from signalforge.grade.models import GradingReport, GradingResult

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "airflow" / "drift_pairs"


# ---------------------------------------------------------------------------
# Engineered builders — explicit DiffReport / GradingReport construction.
# ---------------------------------------------------------------------------


def _entry(
    artifact_id: str,
    tier: Tier,
    *,
    test_type: str | None = None,
    drop_reason: str | None = None,
    why: str = "",
) -> DiffEntry:
    return DiffEntry(
        artifact_id=artifact_id,
        test_type=test_type,
        tier=tier,
        drop_reason=drop_reason,  # type: ignore[arg-type]
        why=why,
    )


def _diff(
    *,
    model_unique_id: str = "model.shop.fct_orders",
    entries: tuple[DiffEntry, ...],
) -> SfDiffReport:
    """Build a minimal valid :class:`DiffReport` for drift comparison.

    The count fields / hashes are not consumed by :func:`compute_drift`; they
    are filled with consistent placeholders so the model validates.
    """
    kept = sum(1 for e in entries if e.tier in ("kept", "kept-uncertain", "flagged"))
    dropped = sum(1 for e in entries if e.tier == "dropped")
    return SfDiffReport(
        signalforge_version=signalforge.__version__,
        model_unique_id=model_unique_id,
        run_id="r" * 32,
        duration_seconds=1.0,
        proposed_yaml="version: 2\n",
        existing_yaml=None,
        unified_diff="",
        entries=entries,
        kept_count=kept,
        kept_uncertain_count=sum(1 for e in entries if e.tier == "kept-uncertain"),
        dropped_count=dropped,
        flagged_count=sum(1 for e in entries if e.tier == "flagged"),
        has_existing_schema=False,
        candidate_hash="0" * 16,
        prune_result_hash="0" * 16,
        grading_report_hash=None,
    )


def _grade(mean: float, *, model_unique_id: str = "model.shop.fct_orders") -> GradingReport:
    """Build a :class:`GradingReport` whose ``mean_score`` equals ``mean``.

    A single scored result with ``score=mean`` makes the computed
    ``mean_score`` exactly ``mean``.
    """
    return GradingReport(
        signalforge_version=signalforge.__version__,
        run_id="g" * 32,
        timestamp="2026-06-16T00:00:00.000000Z",  # type: ignore[arg-type]
        duration_seconds=2.0,
        model_unique_id=model_unique_id,
        rubric_hash="1" * 16,
        thresholds=(0.7, 0.7),
        results=(
            GradingResult(
                artifact_id="column.amount.description",
                criterion_id="clarity",
                score=mean,
                passed=True,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Transition classification (DEC-005).
# ---------------------------------------------------------------------------


def test_newly_always_passes_is_the_signal_rot_alarm() -> None:
    """kept → dropped/always-passes is the headline signal-rot transition."""
    prev = _diff(entries=(_entry("test.column.amount.not_null", "kept", why="caught rows"),))
    curr = _diff(
        entries=(
            _entry(
                "test.column.amount.not_null",
                "dropped",
                drop_reason="always-passes",
                why="always passes on the sample",
            ),
        )
    )
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert len(report.newly_always_passes) == 1
    artifact = report.newly_always_passes[0]
    assert artifact.artifact_id == "test.column.amount.not_null"
    assert artifact.previous_tier == "kept"
    assert artifact.current_tier == "dropped"
    assert artifact.current_drop_reason == "always-passes"
    assert report.newly_dropped == ()
    assert report.alarming is True


def test_flagged_to_always_passes_also_counts_as_signal_rot() -> None:
    """flagged is a kept-ish (shipped) tier — its rot to always-passes alarms."""
    prev = _diff(entries=(_entry("test.column.amount.not_null", "flagged"),))
    curr = _diff(
        entries=(_entry("test.column.amount.not_null", "dropped", drop_reason="always-passes"),)
    )
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert len(report.newly_always_passes) == 1
    assert report.newly_always_passes[0].previous_tier == "flagged"


def test_newly_dropped_non_always_passes_is_disjoint_from_signal_rot() -> None:
    """kept → dropped with a non-always-passes reason is informational only."""
    prev = _diff(entries=(_entry("test.column.user_id.relationships", "kept"),))
    curr = _diff(
        entries=(
            _entry(
                "test.column.user_id.relationships",
                "dropped",
                drop_reason="requires-future-data",
                why="ref target absent",
            ),
        )
    )
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert len(report.newly_dropped) == 1
    assert report.newly_dropped[0].current_drop_reason == "requires-future-data"
    assert report.newly_always_passes == ()
    assert report.alarming is False


def test_newly_kept_dropped_to_kept_ish() -> None:
    """dropped → kept (test started catching rows again) is informational."""
    prev = _diff(
        entries=(_entry("test.column.amount.unique", "dropped", drop_reason="always-passes"),)
    )
    curr = _diff(entries=(_entry("test.column.amount.unique", "kept", why="caught dupes"),))
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert len(report.newly_kept) == 1
    assert report.newly_kept[0].previous_tier == "dropped"
    assert report.newly_kept[0].current_tier == "kept"
    assert report.alarming is False


def test_dropped_to_kept_uncertain_is_newly_kept() -> None:
    """kept-uncertain is a kept-ish tier — dropped → kept-uncertain is newly_kept."""
    prev = _diff(entries=(_entry("test.model.row_count_between", "dropped"),))
    curr = _diff(entries=(_entry("test.model.row_count_between", "kept-uncertain"),))
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert len(report.newly_kept) == 1


def test_added_and_removed_artifacts() -> None:
    """artifact_ids present in only one report are added / removed."""
    prev = _diff(entries=(_entry("test.column.amount.not_null", "kept"),))
    curr = _diff(entries=(_entry("test.column.region.not_null", "kept"),))
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert report.added_artifacts == ("test.column.region.not_null",)
    assert report.removed_artifacts == ("test.column.amount.not_null",)
    # Neither is a tier transition.
    assert report.newly_always_passes == ()
    assert report.newly_dropped == ()
    assert report.newly_kept == ()


def test_same_tier_pair_is_a_noop() -> None:
    """An artifact whose tier is unchanged produces no transition entry."""
    prev = _diff(entries=(_entry("test.column.amount.not_null", "kept"),))
    curr = _diff(entries=(_entry("test.column.amount.not_null", "kept"),))
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert report.newly_always_passes == ()
    assert report.newly_dropped == ()
    assert report.newly_kept == ()
    assert report.added_artifacts == ()
    assert report.removed_artifacts == ()
    assert report.alarming is False


def test_kept_ish_to_kept_ish_transition_is_not_flagged_as_drift() -> None:
    """kept → flagged (graded below threshold) is NOT a drift transition.

    Both are kept-ish (shipped) tiers, so the artifact stays out of the
    three transition lists — grade movement is captured by grade_regressions,
    not by tier transitions.
    """
    prev = _diff(entries=(_entry("test.column.amount.not_null", "kept"),))
    curr = _diff(entries=(_entry("test.column.amount.not_null", "flagged"),))
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert report.newly_always_passes == ()
    assert report.newly_dropped == ()
    assert report.newly_kept == ()


# ---------------------------------------------------------------------------
# Schema-shape derivation from artifact_id prefixes.
# ---------------------------------------------------------------------------


def test_schema_shape_add_and_remove_from_artifact_ids() -> None:
    """Column SET add/remove derives from column.* and test.column.* prefixes."""
    prev = _diff(
        entries=(
            _entry("column.amount.description", "kept"),
            _entry("test.column.amount.not_null", "kept"),
        )
    )
    curr = _diff(
        entries=(
            _entry("column.region.description", "kept"),
            _entry("test.column.region.not_null", "kept"),
        )
    )
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert report.schema_shape_changes.columns_added == ("region",)
    assert report.schema_shape_changes.columns_removed == ("amount",)


def test_schema_shape_ignores_model_level_artifacts() -> None:
    """model.* and test.model.* carry no column → no shape change from them."""
    prev = _diff(
        entries=(
            _entry("model.description", "kept"),
            _entry("test.model.row_count_between", "kept"),
        )
    )
    curr = _diff(
        entries=(
            _entry("model.description", "kept"),
            _entry("test.model.unique_combination", "kept"),
        )
    )
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert report.schema_shape_changes == SchemaShapeDelta()


def test_schema_shape_test_with_args_hash_suffix_parses_column() -> None:
    """A test.column.<col>.<type>.<hash> 5-part form still yields the column."""
    prev = _diff(entries=())
    curr = _diff(entries=(_entry("test.column.amount.accepted_values.abcd1234", "kept"),))
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert report.schema_shape_changes.columns_added == ("amount",)


def test_schema_shape_ignores_too_short_malformed_artifact_ids() -> None:
    """Short/malformed dotted forms below the spec arity contribute no column.

    The canonical shapes are ``column.<col>.<field>`` (3 parts) and
    ``test.column.<col>.<type>`` (4 parts). A 2-part ``column.amount`` or a
    3-part ``test.column.amount`` is malformed and must NOT be mined as a
    column — otherwise a corrupt sidecar would pollute schema_shape_changes.
    """
    prev = _diff(entries=())
    curr = _diff(
        entries=(
            _entry("column.amount", "kept"),
            _entry("test.column.region", "kept"),
        )
    )
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert report.schema_shape_changes == SchemaShapeDelta()


# ---------------------------------------------------------------------------
# Grade regression (DEC-005) at / above / below threshold.
# ---------------------------------------------------------------------------


def test_grade_regression_at_exactly_threshold_trips() -> None:
    """current_mean <= previous_mean - threshold; 0.85 <= 0.90 - 0.05 is True."""
    diff = _diff(entries=(_entry("column.amount.description", "kept"),))
    report = compute_drift(
        previous_diff=diff,
        current_diff=diff,
        previous_grade=_grade(0.90),
        current_grade=_grade(0.85),
        grade_regression_threshold=0.05,
    )
    assert len(report.grade_regressions) == 1
    reg = report.grade_regressions[0]
    assert reg.previous_mean == 0.90
    assert reg.current_mean == 0.85
    assert abs(reg.delta - 0.05) < 1e-9
    assert report.alarming is True


def test_grade_regression_above_threshold_trips() -> None:
    diff = _diff(entries=(_entry("column.amount.description", "kept"),))
    report = compute_drift(
        previous_diff=diff,
        current_diff=diff,
        previous_grade=_grade(0.90),
        current_grade=_grade(0.70),
    )
    assert len(report.grade_regressions) == 1


def test_grade_regression_below_threshold_does_not_trip() -> None:
    """A drop smaller than the threshold (0.04 < 0.05) is not a regression."""
    diff = _diff(entries=(_entry("column.amount.description", "kept"),))
    report = compute_drift(
        previous_diff=diff,
        current_diff=diff,
        previous_grade=_grade(0.90),
        current_grade=_grade(0.86),
    )
    assert report.grade_regressions == ()
    assert report.alarming is False


def test_grade_improvement_is_not_a_regression() -> None:
    diff = _diff(entries=(_entry("column.amount.description", "kept"),))
    report = compute_drift(
        previous_diff=diff,
        current_diff=diff,
        previous_grade=_grade(0.70),
        current_grade=_grade(0.95),
    )
    assert report.grade_regressions == ()


def test_no_grade_means_no_regression_and_no_failure() -> None:
    """--no-grade (grades None) → grade_regressions empty, never raises."""
    diff = _diff(entries=(_entry("column.amount.description", "kept"),))
    # Both None.
    assert compute_drift(previous_diff=diff, current_diff=diff).grade_regressions == ()
    # Only one present → still empty (need both to compare).
    assert (
        compute_drift(
            previous_diff=diff, current_diff=diff, current_grade=_grade(0.5)
        ).grade_regressions
        == ()
    )
    assert (
        compute_drift(
            previous_diff=diff, current_diff=diff, previous_grade=_grade(0.5)
        ).grade_regressions
        == ()
    )


# ---------------------------------------------------------------------------
# Degrade: model mismatch (DEC-013).
# ---------------------------------------------------------------------------


def test_model_mismatch_degrades_and_is_never_alarming() -> None:
    prev = _diff(
        model_unique_id="model.shop.fct_orders",
        entries=(_entry("test.column.amount.not_null", "dropped", drop_reason="always-passes"),),
    )
    curr = _diff(
        model_unique_id="model.shop.dim_users",
        entries=(_entry("test.column.amount.not_null", "dropped", drop_reason="always-passes"),),
    )
    report = compute_drift(previous_diff=prev, current_diff=curr)
    assert report.degrade_reason is not None
    assert "model mismatch" in report.degrade_reason
    assert "model.shop.fct_orders" in report.degrade_reason
    assert "model.shop.dim_users" in report.degrade_reason
    # Empty transition lists by construction → never alarming.
    assert report.newly_always_passes == ()
    assert report.newly_dropped == ()
    assert report.newly_kept == ()
    assert report.grade_regressions == ()
    assert report.schema_shape_changes == SchemaShapeDelta()
    assert report.alarming is False
    # Model id reported is the current run's.
    assert report.model_unique_id == "model.shop.dim_users"


# ---------------------------------------------------------------------------
# Determinism (DEC-016) + report metadata.
# ---------------------------------------------------------------------------


def test_compute_drift_is_deterministic() -> None:
    """Same inputs → byte-identical model_dump_json on re-run."""
    prev = _diff(
        entries=(
            _entry("test.column.amount.not_null", "kept"),
            _entry("test.column.region.not_null", "kept"),
            _entry("column.amount.description", "kept"),
        )
    )
    curr = _diff(
        entries=(
            _entry("test.column.amount.not_null", "dropped", drop_reason="always-passes"),
            _entry(
                "test.column.region.not_null",
                "dropped",
                drop_reason="failed-on-known-clean-data",
            ),
            _entry("column.region.description", "kept"),
        )
    )
    a = compute_drift(previous_diff=prev, current_diff=curr, as_of=date(2026, 6, 16))
    b = compute_drift(previous_diff=prev, current_diff=curr, as_of=date(2026, 6, 16))
    assert a.model_dump_json() == b.model_dump_json()


def test_transition_lists_and_added_removed_are_sorted() -> None:
    """Iteration over sorted artifact_ids → sorted output tuples."""
    prev = _diff(
        entries=(
            _entry("test.column.zeta.not_null", "kept"),
            _entry("test.column.alpha.not_null", "kept"),
        )
    )
    curr = _diff(
        entries=(
            _entry("test.column.zeta.not_null", "dropped", drop_reason="always-passes"),
            _entry("test.column.alpha.not_null", "dropped", drop_reason="always-passes"),
        )
    )
    report = compute_drift(previous_diff=prev, current_diff=curr)
    ids = [a.artifact_id for a in report.newly_always_passes]
    assert ids == sorted(ids)


def test_report_carries_input_hashes_and_metadata() -> None:
    diff = _diff(entries=(_entry("column.amount.description", "kept"),))
    report = compute_drift(
        previous_diff=diff,
        current_diff=diff,
        as_of=date(2026, 6, 16),
        grade_regression_threshold=0.1,
    )
    assert report.signalforge_version == signalforge.__version__
    assert report.model_unique_id == "model.shop.fct_orders"
    assert report.as_of == date(2026, 6, 16)
    assert report.grade_regression_threshold == 0.1
    assert report.baseline is False
    assert len(report.previous_diff_hash) == 16
    assert len(report.current_diff_hash) == 16
    assert report.degrade_reason is None


def test_as_of_serializes_to_bare_iso_date_or_null() -> None:
    diff = _diff(entries=())
    with_date = compute_drift(previous_diff=diff, current_diff=diff, as_of=date(2026, 6, 16))
    assert json.loads(with_date.model_dump_json())["as_of"] == "2026-06-16"
    without = compute_drift(previous_diff=diff, current_diff=diff)
    assert json.loads(without.model_dump_json())["as_of"] is None


# ---------------------------------------------------------------------------
# alarming truth table.
# ---------------------------------------------------------------------------


def test_alarming_truth_table() -> None:
    base = _diff(entries=(_entry("column.amount.description", "kept"),))

    # No transitions, no regressions → not alarming.
    assert compute_drift(previous_diff=base, current_diff=base).alarming is False

    # Signal rot only → alarming.
    rot_prev = _diff(entries=(_entry("test.column.amount.not_null", "kept"),))
    rot_curr = _diff(
        entries=(_entry("test.column.amount.not_null", "dropped", drop_reason="always-passes"),)
    )
    assert compute_drift(previous_diff=rot_prev, current_diff=rot_curr).alarming is True

    # Grade regression only → alarming.
    assert (
        compute_drift(
            previous_diff=base,
            current_diff=base,
            previous_grade=_grade(0.9),
            current_grade=_grade(0.5),
        ).alarming
        is True
    )

    # newly_dropped / newly_kept / schema changes only → NOT alarming.
    drop_prev = _diff(entries=(_entry("test.column.amount.unique", "kept"),))
    drop_curr = _diff(
        entries=(
            _entry("test.column.amount.unique", "dropped", drop_reason="requires-future-data"),
        )
    )
    assert compute_drift(previous_diff=drop_prev, current_diff=drop_curr).alarming is False


# ---------------------------------------------------------------------------
# to_xcom shape (DEC-015) + no bulk text.
# ---------------------------------------------------------------------------


def test_to_xcom_shape_and_round_trips_through_json() -> None:
    # Engineered so every category is populated: the model-level test rots to
    # always-passes (signal rot), the amount column's doc is removed, the
    # region column's doc is added, and the grade drops 0.9 → 0.5.
    prev = _diff(
        entries=(
            _entry("test.model.row_count_between", "kept", why="caught row-count drift"),
            _entry("column.amount.description", "kept"),
        )
    )
    curr = _diff(
        entries=(
            _entry(
                "test.model.row_count_between",
                "dropped",
                drop_reason="always-passes",
                why="always passes",
            ),
            _entry("column.region.description", "kept"),
        )
    )
    report = compute_drift(
        previous_diff=prev,
        current_diff=curr,
        previous_grade=_grade(0.9),
        current_grade=_grade(0.5),
        as_of=date(2026, 6, 16),
    )
    xcom = report.to_xcom()
    # JSON-serialisable.
    round_tripped = json.loads(json.dumps(xcom))
    assert round_tripped == xcom

    assert xcom["alarming"] is True
    assert xcom["as_of"] == "2026-06-16"
    assert xcom["baseline"] is False
    assert xcom["model_unique_id"] == "model.shop.fct_orders"
    assert xcom["degrade_reason"] is None
    assert isinstance(xcom["previous_diff_hash"], str)
    assert isinstance(xcom["current_diff_hash"], str)

    counts = xcom["counts"]
    assert isinstance(counts, dict)
    assert counts["newly_always_passes"] == 1
    assert counts["grade_regressions"] == 1
    assert counts["columns_added"] == 1
    assert counts["columns_removed"] == 1
    assert counts["added_artifacts"] == 1
    assert counts["removed_artifacts"] == 1

    assert len(xcom["newly_always_passes"]) == 1
    assert xcom["newly_always_passes"][0]["artifact_id"] == "test.model.row_count_between"
    assert len(xcom["grade_regressions"]) == 1
    assert xcom["schema_shape_changes"]["columns_added"] == ["region"]
    assert xcom["schema_shape_changes"]["columns_removed"] == ["amount"]


def test_to_xcom_carries_no_bulk_sidecar_text() -> None:
    """to_xcom keys never include raw YAML / unified diff / stdout."""
    diff = _diff(entries=(_entry("column.amount.description", "kept"),))
    xcom = compute_drift(previous_diff=diff, current_diff=diff).to_xcom()
    forbidden = {"proposed_yaml", "existing_yaml", "unified_diff", "stdout", "stderr"}
    assert forbidden.isdisjoint(xcom.keys())


# ---------------------------------------------------------------------------
# DriftArtifact why truncation.
# ---------------------------------------------------------------------------


def test_drift_artifact_truncates_long_why() -> None:
    long_why = "x" * 500
    artifact = DriftArtifact(
        artifact_id="test.column.amount.not_null",
        previous_tier="kept",
        current_tier="dropped",
        previous_drop_reason=None,
        current_drop_reason="always-passes",
        why=long_why,
    )
    assert len(artifact.why) <= 200
    assert artifact.why.endswith("…")


def test_truncate_why_helper_edge_cases() -> None:
    """Direct coverage of the shared truncation helper's branches."""
    from signalforge.airflow.drift import _truncate_why

    assert _truncate_why("") == ""
    assert _truncate_why("   ") == ""
    # Non-positive budget has no room even for the ellipsis.
    assert _truncate_why("anything", 0) == ""
    assert _truncate_why("anything", -5) == ""
    # At/below budget → rstrip only, no ellipsis.
    assert _truncate_why("short  ") == "short"
    # Over budget → hard cut + ellipsis.
    truncated = _truncate_why("y" * 10, 4)
    assert truncated == "yyy…"


def test_drift_artifact_blank_why_is_empty() -> None:
    artifact = DriftArtifact(
        artifact_id="x",
        previous_tier="kept",
        current_tier="dropped",
        previous_drop_reason=None,
        current_drop_reason="always-passes",
        why="   ",
    )
    assert artifact.why == ""


def test_grade_regression_is_a_frozen_model() -> None:
    reg = GradeRegression(
        model_unique_id="model.shop.fct_orders",
        previous_mean=0.9,
        current_mean=0.5,
        delta=0.4,
    )
    assert reg.model_dump() == {
        "model_unique_id": "model.shop.fct_orders",
        "previous_mean": 0.9,
        "current_mean": 0.5,
        "delta": 0.4,
    }


def test_repr_omits_long_lists() -> None:
    prev = _diff(entries=(_entry("test.column.amount.not_null", "kept", why="x" * 300),))
    curr = _diff(
        entries=(
            _entry(
                "test.column.amount.not_null",
                "dropped",
                drop_reason="always-passes",
                why="x" * 300,
            ),
        )
    )
    report = compute_drift(previous_diff=prev, current_diff=curr)
    text = repr(report)
    assert "DriftReport(" in text
    assert "newly_always_passes=1" in text
    assert "alarming=True" in text
    # The long why is not dumped into the repr.
    assert "x" * 300 not in text
    # __repr_args__ mirrors the redacted field set.
    keys = [k for k, _ in report.__repr_args__()]
    assert "model_unique_id" in keys
    assert "alarming" in keys


# ---------------------------------------------------------------------------
# Committed fixture pair (real serialized DiffReport / GradingReport JSON).
# ---------------------------------------------------------------------------


def test_signal_rot_from_committed_fixture_pair() -> None:
    """The engineered signal-rot fixture pair drives a real cross-run compare.

    Validates that genuine ``DiffReport.model_dump_json`` / ``GradingReport``
    JSON (round-tripped through ``model_validate_json``) flows through
    ``compute_drift`` and yields the headline signal-rot + grade-regression
    alarm.
    """
    prev_diff = SfDiffReport.model_validate_json(
        (_FIXTURE_DIR / "signal_rot_prev_diff.json").read_text()
    )
    curr_diff = SfDiffReport.model_validate_json(
        (_FIXTURE_DIR / "signal_rot_curr_diff.json").read_text()
    )
    prev_grade = GradingReport.model_validate_json(
        (_FIXTURE_DIR / "signal_rot_prev_grade.json").read_text()
    )
    curr_grade = GradingReport.model_validate_json(
        (_FIXTURE_DIR / "signal_rot_curr_grade.json").read_text()
    )
    report = compute_drift(
        previous_diff=prev_diff,
        current_diff=curr_diff,
        previous_grade=prev_grade,
        current_grade=curr_grade,
        as_of=date(2026, 6, 16),
    )
    assert report.alarming is True
    assert len(report.newly_always_passes) == 1
    assert report.newly_always_passes[0].artifact_id == "test.column.amount.not_null"
    # 0.9 → 0.8 is a 0.10 drop ≥ 0.05 threshold.
    assert len(report.grade_regressions) == 1
    assert report.grade_regressions[0].model_unique_id == "model.shop.fct_orders"
    # The kept doc column is unchanged → no transition for it.
    assert report.newly_dropped == ()
    assert report.newly_kept == ()


def test_eager_reexport_is_in_public_api() -> None:
    """compute_drift / DriftReport are eager-importable from the package root.

    Asserts the names are in ``signalforge.airflow.__all__`` and resolve to a
    usable callable / model — NOT object identity against this module's
    top-level binding, which a sibling test
    (``test_airflow_no_eager_import.py``) deliberately invalidates by purging
    ``signalforge.airflow.*`` from ``sys.modules``. (The no-airflow-import
    guarantee itself is pinned, with proper cleanup, in that sibling test.)
    """
    import signalforge.airflow as sf_airflow

    for name in (
        "compute_drift",
        "DriftReport",
        "DriftArtifact",
        "GradeRegression",
        "SchemaShapeDelta",
    ):
        assert name in sf_airflow.__all__

    diff = _diff(entries=(_entry("column.amount.description", "kept"),))
    report = sf_airflow.compute_drift(previous_diff=diff, current_diff=diff)
    assert isinstance(report, sf_airflow.DriftReport)
