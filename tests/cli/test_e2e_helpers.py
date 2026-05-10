"""Unit tests for the e2e helper module (issue #10 / US-004).

These tests exercise the typed helpers in :mod:`tests.cli._e2e_helpers`
against synthetic fixtures committed under
``tests/fixtures/e2e_helpers/``. They are deliberately NOT marked
``@pytest.mark.e2e`` — they validate the helper plumbing the real e2e
smoke (US-005) depends on, and must run as part of the default test
suite so a regression in fixture isolation or sidecar deserialisation
trips immediately.
"""

from __future__ import annotations

from pathlib import Path

from signalforge.diff import DiffReport
from signalforge.prune import PruneDecision
from tests.cli._e2e_helpers import (
    copy_fixture_to_tmp,
    read_diff_report,
    read_prune_decisions,
)

_HAPPY_FIXTURE = Path(__file__).parent.parent / "fixtures" / "e2e_helpers" / "happy"


def test_copy_fixture_to_tmp_creates_project_dir(tmp_path: Path) -> None:
    """Fixture is copied verbatim under ``tmp_path / "project"``.

    DEC-008 — every e2e run lands its audit JSONLs under a fresh tmp
    project_dir so the source fixture stays read-only.
    """
    project_dir = copy_fixture_to_tmp(_HAPPY_FIXTURE, tmp_path)
    assert project_dir == tmp_path / "project"
    assert project_dir.is_dir()
    # Every file under the source fixture is mirrored.
    src_files = sorted(
        p.relative_to(_HAPPY_FIXTURE) for p in _HAPPY_FIXTURE.rglob("*") if p.is_file()
    )
    dst_files = sorted(p.relative_to(project_dir) for p in project_dir.rglob("*") if p.is_file())
    assert src_files == dst_files
    # Sanity: the audit JSONL and diff sidecar exist under the copy.
    assert (project_dir / ".signalforge" / "prune.jsonl").is_file()
    assert (project_dir / ".signalforge" / "diff.json").is_file()


def test_read_prune_decisions_returns_typed_tuple() -> None:
    """Synthetic prune.jsonl deserialises into typed PruneDecisions.

    DEC-023 — the e2e smoke asserts at least one drop_reason ==
    "always-passes" lands in the audit log; this fixture exercises that
    shape and the helper's tuple-typed return.
    """
    decisions = read_prune_decisions(_HAPPY_FIXTURE)
    assert isinstance(decisions, tuple)
    assert len(decisions) >= 1
    assert all(isinstance(d, PruneDecision) for d in decisions)
    dropped_always_passes = [
        d for d in decisions if d.decision == "dropped" and d.reason == "always-passes"
    ]
    assert len(dropped_always_passes) >= 1


def test_read_diff_report_returns_typed_diff_report() -> None:
    """Synthetic diff.json deserialises into a typed DiffReport.

    DEC-023 — the e2e smoke asserts at least one kept artifact survives
    the pipeline; the synthetic fixture pins ``kept_count >= 1`` so the
    helper itself can be exercised without a real warehouse.
    """
    report = read_diff_report(_HAPPY_FIXTURE)
    assert isinstance(report, DiffReport)
    assert report.kept_count >= 1
