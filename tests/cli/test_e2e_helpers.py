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

import json
from pathlib import Path

import pytest
import yaml

from signalforge.diff import DiffReport
from signalforge.manifest import load
from signalforge.prune import PruneDecision
from tests.cli._e2e_helpers import (
    apply_provider_override,
    copy_fixture_to_tmp,
    inject_model_business_rules,
    read_diff_report,
    read_prune_decisions,
)

_HAPPY_FIXTURE = Path(__file__).parent.parent / "fixtures" / "e2e_helpers" / "happy"
_AUSTIN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"
_AUSTIN_MODEL = "model.signalforge_test_austin.stg_bikeshare_trips"


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


def test_inject_model_business_rules_round_trips_through_loader(tmp_path: Path) -> None:
    """Injected rules land where ``draft._read_business_rules`` reads them.

    Issue #116 / US-015. The custom-business-rule e2e injects rules into
    the per-run copy of the committed Austin manifest. This default-suite
    test (no env vars, no network) pins that the injected
    ``config.meta.signalforge.business_rules`` survives a real
    :func:`signalforge.manifest.load` round-trip and shows up on
    ``Model.config.meta`` — the exact path the drafter reads.
    """
    project_dir = copy_fixture_to_tmp(_AUSTIN_FIXTURE, tmp_path)
    rules = [
        "duration_minutes must always be greater than or equal to itself",
        "every trip must start and end at the same station",
    ]
    inject_model_business_rules(project_dir, _AUSTIN_MODEL, rules)

    # Raw manifest carries the rules under config.meta.signalforge.
    raw = json.loads((project_dir / "target" / "manifest.json").read_text())
    assert raw["nodes"][_AUSTIN_MODEL]["config"]["meta"]["signalforge"]["business_rules"] == rules

    # The loaded Model exposes them on config.meta where the drafter looks.
    manifest = load(project_dir)
    model = manifest.get_model(_AUSTIN_MODEL)
    loaded = model.config.meta["signalforge"]["business_rules"]
    assert loaded == rules


def test_inject_model_business_rules_unknown_model_raises(tmp_path: Path) -> None:
    """A typo'd unique_id fails loud rather than silently injecting nothing."""
    project_dir = copy_fixture_to_tmp(_AUSTIN_FIXTURE, tmp_path)
    try:
        inject_model_business_rules(project_dir, "model.nope.does_not_exist", ["x"])
    except KeyError:
        return
    raise AssertionError("expected KeyError for an unknown model unique_id")


def test_apply_provider_override_creates_grade_block_when_empty(tmp_path: Path) -> None:
    """An empty ``signalforge.yml`` gains a ``grade:`` block populated with overlay keys.

    Issue #155 / US-004 / DEC-012 — per-test provider overlay must work
    against a minimal config without pre-existing ``grade:`` content.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "signalforge.yml").write_text("")

    apply_provider_override(
        project_dir,
        grade_provider="gemini",
        grade_model="gemini-2.5-flash",
        grade_max_output_tokens=2048,
    )

    data = yaml.safe_load((project_dir / "signalforge.yml").read_text())
    assert data["grade"] == {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "max_output_tokens": 2048,
    }


def test_apply_provider_override_preserves_other_blocks(tmp_path: Path) -> None:
    """Top-level sibling blocks (``llm:``, ``safety:``, ``prune:``) are untouched.

    DEC-012 — the overlay is surgical to the ``grade:`` block; other
    pipeline-stage configs (and any future top-level keys) must round-trip
    byte-equivalently.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "signalforge.yml").write_text(
        "llm:\n"
        "  model: claude-sonnet-4-6\n"
        "safety:\n"
        "  mode: aggregate-only\n"
        "prune:\n"
        "  sample_strategy: materialised\n"
    )

    apply_provider_override(project_dir, grade_provider="openai", grade_model="gpt-4o")

    data = yaml.safe_load((project_dir / "signalforge.yml").read_text())
    assert data["llm"] == {"model": "claude-sonnet-4-6"}
    assert data["safety"] == {"mode": "aggregate-only"}
    assert data["prune"] == {"sample_strategy": "materialised"}
    assert data["grade"] == {"provider": "openai", "model": "gpt-4o"}


def test_apply_provider_override_preserves_existing_grade_knobs(tmp_path: Path) -> None:
    """Existing ``grade:`` knobs (e.g. thresholds) survive the overlay.

    DEC-012 — the Austin fixture's ``grade:`` block carries
    ``min_pass_rate`` / ``min_mean_score`` / ``fail_on_below_threshold`` /
    ``total_budget_seconds``. A provider overlay must NOT clobber them.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "signalforge.yml").write_text(
        "grade:\n"
        "  min_pass_rate: 0.95\n"
        "  min_mean_score: 0.95\n"
        "  fail_on_below_threshold: false\n"
        "  total_budget_seconds: 600\n"
    )

    apply_provider_override(
        project_dir,
        grade_provider="openai",
        grade_model="gpt-4o",
        grade_max_output_tokens=4096,
    )

    data = yaml.safe_load((project_dir / "signalforge.yml").read_text())
    assert data["grade"] == {
        "min_pass_rate": 0.95,
        "min_mean_score": 0.95,
        "fail_on_below_threshold": False,
        "total_budget_seconds": 600,
        "provider": "openai",
        "model": "gpt-4o",
        "max_output_tokens": 4096,
    }


def test_apply_provider_override_none_knobs_are_no_op(tmp_path: Path) -> None:
    """Passing only one non-None knob leaves the other grade fields alone.

    DEC-012 — unset knobs (default ``None``) must not appear in the
    output, even as explicit ``null``.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "signalforge.yml").write_text(
        "grade:\n  model: claude-sonnet-4-6\n  min_pass_rate: 0.95\n"
    )

    apply_provider_override(project_dir, grade_provider="anthropic")

    data = yaml.safe_load((project_dir / "signalforge.yml").read_text())
    assert data["grade"] == {
        "model": "claude-sonnet-4-6",
        "min_pass_rate": 0.95,
        "provider": "anthropic",
    }
    # No spurious null entries for the unset knobs.
    assert "max_output_tokens" not in data["grade"]


def test_apply_provider_override_all_none_is_no_change_to_existing_grade(
    tmp_path: Path,
) -> None:
    """Passing every knob as ``None`` round-trips an existing ``grade:`` block.

    DEC-012 — a no-overlay call must be a structural no-op (canonical
    YAML round-trip; values unchanged).
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    original = "grade:\n  model: claude-sonnet-4-6\n  min_pass_rate: 0.95\n"
    (project_dir / "signalforge.yml").write_text(original)

    apply_provider_override(project_dir)

    data = yaml.safe_load((project_dir / "signalforge.yml").read_text())
    assert data == {"grade": {"model": "claude-sonnet-4-6", "min_pass_rate": 0.95}}


def test_apply_provider_override_missing_file_raises(tmp_path: Path) -> None:
    """A missing ``signalforge.yml`` raises ``FileNotFoundError``.

    DEC-012 — the overlay assumes the caller has already copied a real
    fixture into ``tmp_path``; silently creating one would mask
    misconfigured tests.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    # NO signalforge.yml here.

    with pytest.raises(FileNotFoundError):
        apply_provider_override(project_dir, grade_provider="openai")
