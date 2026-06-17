"""Tests for the fail-soft drift sidecar loaders (issue #235 / US-003).

These tests import ONLY the airflow-free drift core — never the real
``apache-airflow`` package — so they run in the **default** pytest suite (NO
``airflow`` marker). They pin the FAIL-SOFT contract of
:func:`signalforge.airflow.drift.load_diff_report` /
:func:`~signalforge.airflow.drift.load_grade_report` /
:func:`~signalforge.airflow.drift.parse_diff_report`:

* happy path returns the right type;
* absent / corrupt-JSON / wrong-shape / oversize input → ``None`` (never raise);
* a symlink cycle in the path → ``None`` (never raise) — DEC-007 / DEC-008.

The prior-read path is operator-TRUSTED and may live OUTSIDE any project dir, so
the loaders are symlink-loop-hardened but NOT project-contained (DEC-008).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import signalforge
from signalforge.airflow.drift import (
    _DRIFT_SIDECAR_SIZE_LIMIT_BYTES,
    load_diff_report,
    load_grade_report,
    parse_diff_report,
)
from signalforge.diff.models import DiffEntry, DiffReport
from signalforge.grade.models import GradingReport, GradingResult

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "airflow" / "drift_pairs"


# ---------------------------------------------------------------------------
# Engineered builders — minimal valid models serialised to disk for the loaders.
# ---------------------------------------------------------------------------


def _diff_report() -> DiffReport:
    """A minimal valid :class:`DiffReport` (one kept test)."""
    return DiffReport(
        signalforge_version=signalforge.__version__,
        model_unique_id="model.shop.fct_orders",
        run_id="r" * 32,
        duration_seconds=1.0,
        proposed_yaml="version: 2\n",
        existing_yaml=None,
        unified_diff="",
        entries=(DiffEntry(artifact_id="test.column.amount.not_null", tier="kept"),),
        kept_count=1,
        kept_uncertain_count=0,
        dropped_count=0,
        flagged_count=0,
        has_existing_schema=False,
        candidate_hash="0" * 16,
        prune_result_hash="0" * 16,
        grading_report_hash=None,
    )


def _grade_report() -> GradingReport:
    """A minimal valid :class:`GradingReport` (one scored result)."""
    return GradingReport(
        signalforge_version=signalforge.__version__,
        run_id="g" * 32,
        timestamp="2026-06-16T00:00:00.000000Z",  # type: ignore[arg-type]
        duration_seconds=2.0,
        model_unique_id="model.shop.fct_orders",
        rubric_hash="1" * 16,
        thresholds=(0.7, 0.7),
        results=(
            GradingResult(
                artifact_id="column.amount.description",
                criterion_id="clarity",
                score=0.9,
                passed=True,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# load_diff_report
# ---------------------------------------------------------------------------


def test_load_diff_report_happy_path(tmp_path: Path) -> None:
    """A round-tripped DiffReport JSON loads back into an equal DiffReport."""
    report = _diff_report()
    path = tmp_path / "diff.json"
    path.write_text(report.model_dump_json(by_alias=True), encoding="utf-8")

    loaded = load_diff_report(path)
    assert loaded is not None
    assert isinstance(loaded, DiffReport)
    assert loaded.model_unique_id == "model.shop.fct_orders"
    assert loaded.model_dump_json() == report.model_dump_json()


def test_load_diff_report_from_committed_fixture() -> None:
    """The committed signal-rot fixture loads as a DiffReport (str path form)."""
    loaded = load_diff_report(str(_FIXTURE_DIR / "signal_rot_curr_diff.json"))
    assert isinstance(loaded, DiffReport)
    assert loaded.model_unique_id == "model.shop.fct_orders"


def test_load_diff_report_absent_returns_none(tmp_path: Path) -> None:
    """A path that does not exist degrades to ``None`` (never raises)."""
    assert load_diff_report(tmp_path / "nope.json") is None


def test_load_diff_report_corrupt_json_returns_none(tmp_path: Path) -> None:
    """Non-JSON content degrades to ``None``."""
    path = tmp_path / "diff.json"
    path.write_text("this is not json {", encoding="utf-8")
    assert load_diff_report(path) is None


def test_load_diff_report_wrong_shape_returns_none(tmp_path: Path) -> None:
    """Valid JSON of the WRONG shape (missing required fields) → ``None``."""
    path = tmp_path / "diff.json"
    path.write_text('{"model_unique_id": "model.x.y"}', encoding="utf-8")
    assert load_diff_report(path) is None


def test_load_diff_report_grade_json_is_wrong_shape(tmp_path: Path) -> None:
    """A grade.json (missing DiffReport's required fields) → ``None``."""
    path = tmp_path / "grade.json"
    path.write_text(_grade_report().model_dump_json(by_alias=True), encoding="utf-8")
    assert load_diff_report(path) is None


def test_load_diff_report_oversize_returns_none(tmp_path: Path) -> None:
    """A file over the size cap degrades to ``None`` BEFORE any JSON parse."""
    path = tmp_path / "diff.json"
    # One byte over the cap — content need not be valid JSON; the cap fires first.
    path.write_text("x" * (_DRIFT_SIDECAR_SIZE_LIMIT_BYTES + 1), encoding="utf-8")
    assert load_diff_report(path) is None


def test_load_diff_report_at_size_cap_is_read(tmp_path: Path) -> None:
    """A file EXACTLY at the cap is read (the cap is a strict ``>`` over-limit)."""
    report = _diff_report()
    body = report.model_dump_json(by_alias=True)
    assert len(body.encode("utf-8")) <= _DRIFT_SIDECAR_SIZE_LIMIT_BYTES
    path = tmp_path / "diff.json"
    path.write_text(body, encoding="utf-8")
    loaded = load_diff_report(path)
    assert isinstance(loaded, DiffReport)


# ---------------------------------------------------------------------------
# load_grade_report
# ---------------------------------------------------------------------------


def test_load_grade_report_happy_path(tmp_path: Path) -> None:
    """A round-tripped GradingReport JSON loads back into an equal report."""
    report = _grade_report()
    path = tmp_path / "grade.json"
    path.write_text(report.model_dump_json(by_alias=True), encoding="utf-8")

    loaded = load_grade_report(path)
    assert isinstance(loaded, GradingReport)
    assert loaded.model_unique_id == "model.shop.fct_orders"
    assert loaded.mean_score == pytest.approx(0.9)


def test_load_grade_report_from_committed_fixture() -> None:
    """The committed grade fixture loads as a GradingReport."""
    loaded = load_grade_report(_FIXTURE_DIR / "signal_rot_curr_grade.json")
    assert isinstance(loaded, GradingReport)


def test_load_grade_report_absent_returns_none(tmp_path: Path) -> None:
    """An absent grade sidecar (e.g. ``--no-grade``) degrades to ``None``."""
    assert load_grade_report(tmp_path / "nope.json") is None


def test_load_grade_report_corrupt_returns_none(tmp_path: Path) -> None:
    """Malformed grade JSON degrades to ``None``."""
    path = tmp_path / "grade.json"
    path.write_text("{ broken", encoding="utf-8")
    assert load_grade_report(path) is None


def test_load_grade_report_diff_json_is_wrong_shape(tmp_path: Path) -> None:
    """A diff.json (missing GradingReport's required fields) → ``None``."""
    path = tmp_path / "diff.json"
    path.write_text(_diff_report().model_dump_json(by_alias=True), encoding="utf-8")
    assert load_grade_report(path) is None


# ---------------------------------------------------------------------------
# Symlink-loop hardening (DEC-007 / DEC-008) — fail-soft, never raise.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink-loop semantics required")
def test_load_diff_report_symlink_loop_returns_none(tmp_path: Path) -> None:
    """A symlink cycle in the path degrades to ``None`` rather than raising.

    Resolving ``strict=True`` surfaces the loop (``OSError(errno.ELOOP)`` on
    Python >= 3.13, ``RuntimeError`` on <= 3.12); both are caught and degraded.
    """
    link = tmp_path / "loop.json"
    link.symlink_to(link)  # self-referential symlink — a one-node cycle
    assert load_diff_report(link) is None


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink-loop semantics required")
def test_load_grade_report_symlink_loop_returns_none(tmp_path: Path) -> None:
    """The grade loader degrades symmetrically on a symlink cycle."""
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.symlink_to(b)
    b.symlink_to(a)  # two-node cycle
    assert load_grade_report(a) is None


# ---------------------------------------------------------------------------
# parse_diff_report (current report off run_signalforge stdout)
# ---------------------------------------------------------------------------


def test_parse_diff_report_happy_path() -> None:
    """Valid DiffReport JSON on stdout parses into a DiffReport."""
    report = _diff_report()
    stdout = report.model_dump_json(by_alias=True)
    parsed = parse_diff_report(stdout)
    assert isinstance(parsed, DiffReport)
    assert parsed.model_unique_id == "model.shop.fct_orders"


def test_parse_diff_report_strips_surrounding_whitespace() -> None:
    """Leading/trailing whitespace around the JSON is tolerated (stripped)."""
    report = _diff_report()
    stdout = "\n\n" + report.model_dump_json(by_alias=True) + "\n  "
    assert isinstance(parse_diff_report(stdout), DiffReport)


@pytest.mark.parametrize("stdout", ["", "   ", "\n\t "])
def test_parse_diff_report_empty_returns_none(stdout: str) -> None:
    """Empty / whitespace-only stdout (a non-zero exit produced no diff) → ``None``."""
    assert parse_diff_report(stdout) is None


def test_parse_diff_report_non_json_returns_none() -> None:
    """Non-JSON stdout (a non-json ``--format``) degrades to ``None``."""
    assert parse_diff_report("Rendered an ANSI table here, not JSON.") is None


def test_parse_diff_report_wrong_shape_returns_none() -> None:
    """Valid JSON of the wrong shape (missing required fields) → ``None``."""
    assert parse_diff_report('{"model_unique_id": "model.x.y"}') is None
