"""Tests for the issue #37 / US-005 batch-summary + progress-prefix wiring.

Two helpers, two emission paths:

* :func:`signalforge.cli._helpers.format_batch_summary` — pure formatter
  for the DEC-005 stderr summary. Headline always; failure block when
  any per-model exit_code != 0; failure list capped at 50 with
  ``  ... and <K> more`` overflow line (DEC-009).
* :func:`signalforge.cli._helpers.emit_batch_progress_entry` —
  TTY-gated ``[i/N] <model_unique_id>`` stderr line emitted at the head
  of each :func:`_run_single_model` invocation INSIDE :func:`_run_batch`.
  DEC-014: ``--quiet`` suppresses, ``--verbose`` forces, single-model
  positional path emits NEITHER.

This file's tests complement ``tests/cli/test_generate_batch.py`` (which
pins the dispatcher/driver shape from US-003); here we pin the
*emission contracts* only. Integration with the multi-model fixture is
US-007's job.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from signalforge.cli._helpers import (
    emit_batch_progress_entry,
    format_batch_summary,
)
from signalforge.cli.generate import (
    _BatchOutcome,
    _run_batch,
    _SingleModelOutcome,
    cmd_generate,
)
from signalforge.llm.errors import LLMRateLimitError
from signalforge.manifest.models import Column, Manifest, Model
from tests.cli._factories import (
    make_candidate,
    make_diff_report,
    make_draft_outcome,
    make_fake_dbt_project,
    make_grading_report,
    make_model,
    make_prune_result,
)

# ---------------------------------------------------------------------------
# Helpers shared with test_generate_batch (kept local to avoid a cross-test
# import seam)
# ---------------------------------------------------------------------------


def _make_outcome(
    unique_id: str,
    *,
    exit_code: int = 0,
    kept: int = 1,
    dropped: int = 0,
    flagged: int = 0,
    exc_class: str | None = None,
    duration: float = 0.5,
) -> _SingleModelOutcome:
    return _SingleModelOutcome(
        model_unique_id=unique_id,
        exit_code=exit_code,
        kept_count=kept,
        dropped_count=dropped,
        flagged_count=flagged,
        rendered_text="rendered\n" if exit_code == 0 else "",
        duration_seconds=duration,
        exception_class_name=exc_class,
    )


def _make_batch(
    outcomes: tuple[_SingleModelOutcome, ...],
    *,
    duration: float = 1.5,
) -> _BatchOutcome:
    total = max((o.exit_code for o in outcomes), default=0)
    return _BatchOutcome(
        per_model=outcomes,
        total_exit_code=total,
        duration_seconds=duration,
    )


def _make_model_with(unique_id: str, name: str, *, tags: tuple[str, ...] = ()) -> Model:
    return Model(
        unique_id=unique_id,
        name=name,
        resource_type="model",
        package_name="multi",
        original_file_path=f"models/{name}.sql",
        path=f"{name}.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        tags=list(tags),
        columns={"id": Column(name="id")},
        raw_code="select 1 as id",
    )


def _make_multi_manifest() -> tuple[Manifest, tuple[Model, ...]]:
    m_a = _make_model_with("model.multi.stg_a", "stg_a", tags=("staging",))
    m_b = _make_model_with("model.multi.stg_b", "stg_b", tags=("staging",))
    m_c = _make_model_with("model.multi.fct_x", "fct_x", tags=("marts",))
    nodes = {m.unique_id: m for m in (m_a, m_b, m_c)}
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes=nodes,
    )
    return manifest, (m_a, m_b, m_c)


def _install_batch_happy_patches(
    monkeypatch: pytest.MonkeyPatch, manifest: Manifest
) -> dict[str, MagicMock]:
    """Mirror of ``test_generate_batch._install_batch_happy_patches``."""
    from signalforge.cli import generate as gen_mod

    canonical_model = next(iter(manifest.nodes.values()))
    candidate = make_candidate(model_name=canonical_model.name)
    draft_outcome = make_draft_outcome(candidate)
    prune_result = make_prune_result(canonical_model)
    grade_report = make_grading_report(canonical_model)
    diff_report = make_diff_report(canonical_model, candidate)

    def _fresh_adapter(*_a: Any, **_kw: Any) -> Any:
        return MagicMock(name="adapter")

    mocks: dict[str, MagicMock] = {
        "manifest_load": MagicMock(return_value=manifest),
        "load_profile": MagicMock(return_value=MagicMock(name="profile")),
        "make_warehouse_adapter": MagicMock(side_effect=_fresh_adapter),
        "load_safety_config": MagicMock(return_value=MagicMock(name="policy")),
        "load_draft_config": MagicMock(return_value=MagicMock(model="claude-fake")),
        "draft_schema": MagicMock(return_value=draft_outcome),
        "load_prune_config": MagicMock(return_value=MagicMock(enabled=True)),
        "prune_tests": MagicMock(return_value=prune_result),
        "load_grade_config": MagicMock(return_value=MagicMock(rubric=None)),
        "grade_artifacts": MagicMock(return_value=grade_report),
        "load_diff_config": MagicMock(return_value=MagicMock(render_kind="ansi")),
        "render_diff": MagicMock(return_value=diff_report),
        "render_to_text": MagicMock(return_value="--- DIFF OUTPUT MARKER ---"),
        "make_anthropic_client": MagicMock(return_value=None),
    }

    monkeypatch.setattr(gen_mod.manifest_module, "load", mocks["manifest_load"])
    monkeypatch.setattr(gen_mod.warehouse_module, "load_profile", mocks["load_profile"])
    monkeypatch.setattr(gen_mod, "_make_warehouse_adapter", mocks["make_warehouse_adapter"])
    monkeypatch.setattr(gen_mod, "_make_anthropic_client", mocks["make_anthropic_client"])
    monkeypatch.setattr(gen_mod.safety_module, "load_safety_config", mocks["load_safety_config"])
    monkeypatch.setattr(gen_mod.draft_module, "load_draft_config", mocks["load_draft_config"])
    monkeypatch.setattr(gen_mod.draft_module, "draft_schema", mocks["draft_schema"])
    monkeypatch.setattr(gen_mod.prune_module, "load_prune_config", mocks["load_prune_config"])
    monkeypatch.setattr(gen_mod.prune_module, "prune_tests", mocks["prune_tests"])
    monkeypatch.setattr(gen_mod.grade_module, "load_grade_config", mocks["load_grade_config"])
    monkeypatch.setattr(gen_mod.grade_module, "grade_artifacts", mocks["grade_artifacts"])
    monkeypatch.setattr(gen_mod.diff_module, "load_diff_config", mocks["load_diff_config"])
    monkeypatch.setattr(gen_mod.diff_module, "render_diff", mocks["render_diff"])
    monkeypatch.setattr(gen_mod.diff_module, "render_to_text", mocks["render_to_text"])

    return mocks


def _batch_namespace(select: str, project_dir: Path, **overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "select": select,
        "model": None,
        "project_dir": str(project_dir),
        "manifest": None,
        "profiles_dir": None,
        "mode": None,
        "min_score": None,
        "write": False,
        "dry_run": False,
        "format": "ansi",
        "scope": None,
        "sample_strategy": None,
        "quiet": False,
        "verbose": False,
        "no_color": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# format_batch_summary — pure formatter shape
# ---------------------------------------------------------------------------


def test_format_batch_summary_headline_shape() -> None:
    """The headline line exactly matches DEC-005's locked wording."""
    outcome = _make_batch(
        (
            _make_outcome("model.x.a", kept=3, dropped=1, flagged=0),
            _make_outcome("model.x.b", kept=2, dropped=4, flagged=1),
        ),
        duration=2.7,
    )
    rendered = format_batch_summary(outcome)
    first_line = rendered.splitlines()[0]
    assert first_line == "Generated 5 kept / 5 dropped / 1 flagged across 2 models in 2.7s"


def test_format_batch_summary_no_failures_omits_failure_block() -> None:
    """When every per_model exit_code is 0, the failure block is absent."""
    outcome = _make_batch(
        (
            _make_outcome("model.x.a"),
            _make_outcome("model.x.b"),
        ),
    )
    rendered = format_batch_summary(outcome)
    assert "models failed" not in rendered
    assert "  -" not in rendered  # no bullet lines


def test_format_batch_summary_names_failures_with_tier_and_class() -> None:
    """Failed-model lines render as ``  - <id>        exit <code>  (<Class>)``.

    Pins DEC-005's failure block.
    """
    outcome = _make_batch(
        (
            _make_outcome("model.x.a"),  # success
            _make_outcome(
                "model.x.bad",
                exit_code=3,
                kept=0,
                exc_class="LLMRateLimitError",
            ),
        ),
    )
    rendered = format_batch_summary(outcome)
    assert "1 models failed:" in rendered
    # The exact per-line shape is locked: two-space indent, dash, space,
    # id, whitespace, exit <code>, two spaces, ``(<Class>)``.
    assert "  - model.x.bad" in rendered
    assert "exit 3" in rendered
    assert "(LLMRateLimitError)" in rendered


def test_format_batch_summary_truncates_failure_list_at_50() -> None:
    """100 failures → first 50 named + ``  ... and 50 more`` (DEC-009)."""
    outcomes = tuple(
        _make_outcome(
            f"model.x.fail_{i:03d}",
            exit_code=2,
            kept=0,
            exc_class="ModelNotFoundError",
        )
        for i in range(100)
    )
    batch = _make_batch(outcomes)
    rendered = format_batch_summary(batch)
    # Exactly 50 named-failure bullet lines.
    bullet_lines = [line for line in rendered.splitlines() if line.startswith("  - ")]
    assert len(bullet_lines) == 50
    # The 51st-100th land in the overflow line.
    assert "  ... and 50 more" in rendered
    # The 100-failure count appears in the header.
    assert "100 models failed:" in rendered


def test_format_batch_summary_failure_count_in_header_uses_actual_count() -> None:
    """When 5 of 6 models fail, the header reads ``5 models failed:``."""
    outcomes = (
        _make_outcome("model.x.ok"),
        *tuple(
            _make_outcome(
                f"model.x.fail_{i}",
                exit_code=1,
                kept=0,
                exc_class="ManifestNotFoundError",
            )
            for i in range(5)
        ),
    )
    batch = _make_batch(outcomes)
    rendered = format_batch_summary(batch)
    assert "5 models failed:" in rendered


# ---------------------------------------------------------------------------
# Summary emission — end-of-batch stderr writes
# ---------------------------------------------------------------------------


def test_batch_summary_emits_to_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``_run_batch`` writes the formatted summary to stderr at end-of-run."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    _install_batch_happy_patches(monkeypatch, manifest)

    args = _batch_namespace("tag:staging", project_dir)
    code = cmd_generate(args)
    captured = capsys.readouterr()

    assert code == 0
    # Headline lands on stderr; two-of-three matched (stg_a, stg_b).
    assert "Generated " in captured.err
    assert "across 2 models" in captured.err
    # No failure block; both succeeded.
    assert "models failed" not in captured.err
    assert "Traceback" not in captured.err


def test_batch_summary_emits_when_any_model_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Summary fires when ≥1 model failed even if total matched models is 1."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    mocks = _install_batch_happy_patches(monkeypatch, manifest)
    mocks["draft_schema"].side_effect = LLMRateLimitError("rate limited", attempts=3)

    # ``tag:marts`` matches exactly one model (fct_x); that model fails
    # → summary fires because failed_count ≥ 1.
    args = _batch_namespace("tag:marts", project_dir)
    code = cmd_generate(args)
    captured = capsys.readouterr()

    assert code == 3
    assert "Generated " in captured.err
    assert "1 models failed:" in captured.err
    assert "(LLMRateLimitError)" in captured.err


def test_batch_summary_suppressed_for_single_matched_zero_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--select`` resolving to ONE model with success: no summary.

    DEC-005: summary suppressed when (single match) AND (zero failures)
    so the UX matches the single-model positional path in that degenerate
    case.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    _install_batch_happy_patches(monkeypatch, manifest)

    args = _batch_namespace("tag:marts", project_dir)
    code = cmd_generate(args)
    captured = capsys.readouterr()

    assert code == 0
    assert "Generated " not in captured.err
    assert "models failed" not in captured.err


def test_batch_summary_suppressed_under_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--quiet`` suppresses the end-of-batch summary even on multi-match."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    _install_batch_happy_patches(monkeypatch, manifest)

    args = _batch_namespace("tag:staging", project_dir, quiet=True)
    code = cmd_generate(args)
    captured = capsys.readouterr()

    assert code == 0
    assert "Generated " not in captured.err
    assert "models failed" not in captured.err


def test_batch_summary_absent_in_single_model_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Positional ``<model>`` (no ``--select``): no summary lands on stderr."""
    from signalforge.cli import main

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)

    model = make_model()
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={model.unique_id: model},
    )
    _install_batch_happy_patches(monkeypatch, manifest)

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 0
    assert "Generated " not in captured.err
    assert "across " not in captured.err
    assert "models failed" not in captured.err


# ---------------------------------------------------------------------------
# emit_batch_progress_entry — TTY gating + line shape
# ---------------------------------------------------------------------------


def test_emit_batch_progress_entry_writes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pure helper: when invoked, writes ``[i/N] <unique_id>`` to stderr."""
    emit_batch_progress_entry("model.x.a", 1, 3)
    captured = capsys.readouterr()
    assert captured.err == "[1/3] model.x.a\n"
    assert captured.out == ""


def test_batch_progress_prefix_emits_to_stderr_under_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Under a fake TTY, each per-model iteration emits ``[i/N] <id>`` on stderr."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    _install_batch_happy_patches(monkeypatch, manifest)

    # Fake TTY for both streams so ``should_emit_progress`` returns True.
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)

    args = _batch_namespace("tag:staging", project_dir)
    code = cmd_generate(args)
    captured = capsys.readouterr()

    assert code == 0
    # Two matched models → two ``[i/N]`` prefix lines on stderr.
    assert "[1/2] " in captured.err
    assert "[2/2] " in captured.err


def test_batch_progress_prefix_suppressed_under_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--quiet`` suppresses the per-model prefix even under TTY."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    _install_batch_happy_patches(monkeypatch, manifest)

    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)

    args = _batch_namespace("tag:staging", project_dir, quiet=True)
    code = cmd_generate(args)
    captured = capsys.readouterr()

    assert code == 0
    assert "[1/2]" not in captured.err
    assert "[2/2]" not in captured.err


def test_batch_progress_prefix_forced_under_verbose_non_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-TTY + ``--verbose`` still emits the per-model prefix (DEC-026)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    _install_batch_happy_patches(monkeypatch, manifest)

    # Non-TTY for both streams; capsys redirects them anyway, but be explicit.
    monkeypatch.setattr("sys.stderr.isatty", lambda: False, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)

    args = _batch_namespace("tag:staging", project_dir, verbose=True)
    code = cmd_generate(args)
    captured = capsys.readouterr()

    assert code == 0
    assert "[1/2] " in captured.err
    assert "[2/2] " in captured.err


def test_batch_progress_prefix_absent_in_single_model_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Positional single-model path emits NO ``[i/N]`` prefix even under TTY."""
    from signalforge.cli import main

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)

    model = make_model()
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={model.unique_id: model},
    )
    _install_batch_happy_patches(monkeypatch, manifest)

    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 0
    # The single-model path threads ``batch_index=None`` / ``batch_count=None``
    # so the batch-progress helper is never invoked. (Stage progress lines
    # like ``[1/5] safety: ...`` ARE emitted under verbose / TTY, but the
    # multi-model ``[i/N] <unique_id>`` prefix is not.)
    assert "[1/1] model.shop.customers" not in captured.err
    # And the summary is also absent on the single-model path.
    assert "Generated " not in captured.err


def test_run_batch_returns_outcome_unchanged_under_emission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Library-level: ``_run_batch`` still returns a ``_BatchOutcome`` with
    populated ``per_model``; emission does not mutate the outcome shape.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    _install_batch_happy_patches(monkeypatch, manifest)

    args = _batch_namespace("tag:staging", project_dir)
    profile = MagicMock(name="profile")
    outcome = _run_batch(manifest, profile, args, project_dir=project_dir)
    capsys.readouterr()  # drain

    assert isinstance(outcome, _BatchOutcome)
    assert len(outcome.per_model) == 2
    assert outcome.total_exit_code == 0
