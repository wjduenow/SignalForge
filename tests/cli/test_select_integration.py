"""End-to-end CLI integration tests for the issue #37 / US-007 ``--select`` flag.

This bead (``bd_1-scaffolding-4v1.7``) exercises the full batch driver
against the multi-model fixture at ``tests/fixtures/dbt_project_multi/``
via in-process :func:`signalforge.cli.main`. The LLM seam (Anthropic) and
the warehouse adapter are stubbed via monkeypatched stage entries
(mirrors :mod:`tests.cli.test_generate_batch` and
:mod:`tests.cli.test_batch_emission`) so no env vars, no live network,
and no real credentials are required — the test value is in pinning the
end-to-end *observable* shape: stdout, stderr, exit codes, and on-disk
sidecar bytes.

The fixture's ``signalforge.yml`` already ships
``safety.mode: aggregate-only`` and ``prune.enabled: false`` (US-006) so
the offline-safe contract holds even where the pipeline reaches the
prune stage.

Traces to DEC-001 (grammar), DEC-002 (mutex), DEC-004 (max() exit),
DEC-005 (summary shape), DEC-006 (zero-match tier 2), DEC-007 (parse
errors tier 2), DEC-009 (failure list shape), DEC-017 (5-surface parity
companion file).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from signalforge.cli import main
from signalforge.diff.models import DiffReport
from signalforge.draft.models import CandidateColumn, CandidateSchema
from signalforge.draft.schema import DraftOutcome
from signalforge.grade.models import GradingReport
from signalforge.llm.errors import LLMRateLimitError
from signalforge.manifest.models import Model
from signalforge.prune.models import PruneResult

# ---------------------------------------------------------------------------
# Fixture root + tmp_path copy helper
# ---------------------------------------------------------------------------

_FIXTURE_DIR = (Path(__file__).resolve().parent.parent / "fixtures" / "dbt_project_multi").resolve()


def _copy_fixture(tmp_path: Path) -> Path:
    """Copy the committed multi-model fixture into ``tmp_path / "project"``.

    Each test that invokes :func:`main` MUST own its own copy so
    ``.signalforge/`` artefacts (audit JSONLs + sidecars) land under
    ``tmp_path`` rather than polluting the committed fixture
    (``testing-signal.md`` DEC-008 of issue #10 — tmp_path isolation).

    The fixture ships a hand-crafted ``target/manifest.json`` (US-006);
    every stage in :func:`signalforge.cli.generate.cmd_generate` from
    ``manifest.load`` onward sees the copy, not the source.
    """
    project_dir = tmp_path / "project"
    shutil.copytree(_FIXTURE_DIR, project_dir)
    (project_dir / ".signalforge").mkdir(exist_ok=True)
    return project_dir


# ---------------------------------------------------------------------------
# Typed stub builders — per-model variation so sidecars carry the right id
# ---------------------------------------------------------------------------


def _candidate_for(model: Model) -> CandidateSchema:
    return CandidateSchema(
        name=model.name,
        description=f"Drafted description for {model.name}",
        columns=(CandidateColumn(name="id", description="primary key"),),
        tests=(),
    )


def _draft_outcome_for(model: Model) -> DraftOutcome:
    class _FakeOutcome:
        def __init__(self, candidate: CandidateSchema) -> None:
            self.candidate = candidate

    return _FakeOutcome(_candidate_for(model))  # type: ignore[return-value]


def _prune_result_for(model: Model) -> PruneResult:
    return PruneResult(
        model_unique_id=model.unique_id,
        decisions=(),
        elapsed_ms=0,
        signalforge_version="0.0.0-test",
    )


def _grading_report_for(model: Model) -> GradingReport:
    from datetime import datetime, timezone

    return GradingReport(
        signalforge_version="0.0.0-test",
        run_id=f"run-{model.unique_id}",
        timestamp=datetime(2026, 5, 11, tzinfo=timezone.utc),
        duration_seconds=0.0,
        model_unique_id=model.unique_id,
        rubric_hash="0" * 16,
        thresholds=(0.0, 0.0),
        results=(),
    )


def _diff_report_for(model: Model) -> DiffReport:
    return DiffReport(
        signalforge_version="0.0.0-test",
        model_unique_id=model.unique_id,
        run_id=f"run-{model.unique_id}",
        duration_seconds=0.0,
        proposed_yaml="version: 2\nmodels: []\n",
        existing_yaml=None,
        unified_diff="--- existing\n+++ proposed\n",
        entries=(),
        kept_count=0,
        dropped_count=0,
        flagged_count=0,
        has_existing_schema=False,
        candidate_hash="a" * 16,
        prune_result_hash="b" * 16,
        grading_report_hash="c" * 16,
    )


# ---------------------------------------------------------------------------
# Stage-entry patching — uses the real fixture's manifest but stubs every
# LLM / warehouse / sidecar-writing call
# ---------------------------------------------------------------------------


def _install_integration_patches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sidecars_to_disk: bool = False,
    draft_side_effect: Any = None,
) -> dict[str, MagicMock]:
    """Stub the stage entries the integration test crosses.

    Unlike :mod:`tests.cli.test_generate_batch`, we let the *real* manifest
    loader run against the fixture's hand-crafted ``target/manifest.json``
    so the selector grammar exercise is honest — the manifest layer
    classifies ``tag:`` / ``path:`` / bare against bytes the test author
    didn't synthesise.

    ``sidecars_to_disk=True`` makes the patched :func:`render_diff` /
    :func:`grade_artifacts` also write to the canonical sidecar paths so
    test #13 (``test_sidecar_last_writer_wins_across_batch``) can assert
    on the bytes left behind by the last model in the batch.

    ``draft_side_effect`` lets a test inject a per-call failure (e.g.,
    raise on the first invocation, succeed afterwards) for the
    partial-failure path (test #9).

    The factory for :class:`WarehouseAdapter` returns a fresh
    :class:`MagicMock` per call (DEC-010); :func:`load_profile` is
    monkeypatched so the fixture does not need a ``profiles.yml`` on
    disk.
    """
    from signalforge.cli import generate as gen_mod

    def _per_model_draft(model: Model, *_a: Any, **_kw: Any) -> DraftOutcome:
        return _draft_outcome_for(model)

    def _per_model_prune(model: Model, *_a: Any, **_kw: Any) -> PruneResult:
        return _prune_result_for(model)

    def _per_model_grade(
        model: Model, *_a: Any, project_dir: Path | None = None, **_kw: Any
    ) -> GradingReport:
        report = _grading_report_for(model)
        # Mirror the real grade engine's end-of-run sidecar write so
        # test #13 can observe last-writer-wins behaviour on
        # ``.signalforge/grade.json``.
        if sidecars_to_disk and project_dir is not None:
            sidecar_path = Path(project_dir) / ".signalforge" / "grade.json"
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            sidecar_path.write_text(report.model_dump_json(), encoding="utf-8")
        return report

    def _per_model_render_diff(
        model: Model,
        candidate: CandidateSchema,
        *_a: Any,
        project_dir: Path | None = None,
        write_sidecar: bool = True,
        **_kw: Any,
    ) -> DiffReport:
        report = _diff_report_for(model)
        # Mirror the real diff engine's default-on sidecar write so
        # test #13 can observe last-writer-wins behaviour on
        # ``.signalforge/diff.json``.
        if sidecars_to_disk and write_sidecar and project_dir is not None:
            sidecar_path = Path(project_dir) / ".signalforge" / "diff.json"
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            sidecar_path.write_text(report.model_dump_json(), encoding="utf-8")
        return report

    if draft_side_effect is not None:
        draft_mock = MagicMock(side_effect=draft_side_effect)
    else:
        draft_mock = MagicMock(side_effect=_per_model_draft)

    def _fresh_adapter(*_a: Any, **_kw: Any) -> Any:
        return MagicMock(name="adapter")

    mocks: dict[str, MagicMock] = {
        "load_profile": MagicMock(return_value=MagicMock(name="profile")),
        "make_warehouse_adapter": MagicMock(side_effect=_fresh_adapter),
        "make_anthropic_client": MagicMock(return_value=None),
        "load_safety_config": MagicMock(return_value=MagicMock(name="policy")),
        "load_draft_config": MagicMock(return_value=MagicMock(model="claude-fake")),
        "draft_schema": draft_mock,
        "load_prune_config": MagicMock(return_value=MagicMock(enabled=False)),
        "prune_tests": MagicMock(side_effect=_per_model_prune),
        "load_grade_config": MagicMock(return_value=MagicMock(rubric=None)),
        "grade_artifacts": MagicMock(side_effect=_per_model_grade),
        "load_diff_config": MagicMock(return_value=MagicMock(render_kind="ansi")),
        "render_diff": MagicMock(side_effect=_per_model_render_diff),
        "render_to_text": MagicMock(
            side_effect=lambda report, **_kw: f"--- DIFF: {report.model_unique_id} ---"
        ),
    }

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_select_tag_routes_to_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--select tag:staging`` matches the two staging-tagged models
    (``stg_a`` and ``stg_b``) and renders both diffs to stdout in
    ``unique_id`` lex order. ``main`` returns 0.
    """
    project_dir = _copy_fixture(tmp_path)
    _install_integration_patches(monkeypatch)

    code = main(["generate", "--select", "tag:staging", "--project-dir", str(project_dir)])
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    # Two diffs on stdout in unique_id order: stg_a < stg_b.
    out = captured.out
    assert "--- DIFF: model.dbt_project_multi.stg_a ---" in out
    assert "--- DIFF: model.dbt_project_multi.stg_b ---" in out
    # ``fct_x`` is marts-tagged; absent.
    assert "model.dbt_project_multi.fct_x" not in out
    # Order check: stg_a appears BEFORE stg_b on stdout (lex sort).
    assert out.index("stg_a") < out.index("stg_b")
    assert "Traceback" not in captured.err


def test_select_path_glob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--select path:models/staging/*`` matches the two staging-path
    models via :func:`fnmatch.fnmatchcase` against ``original_file_path``
    (DEC-016: shell-glob semantics, not regex / dbt path-prefix).
    """
    project_dir = _copy_fixture(tmp_path)
    _install_integration_patches(monkeypatch)

    code = main(
        ["generate", "--select", "path:models/staging/*", "--project-dir", str(project_dir)]
    )
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    out = captured.out
    assert "model.dbt_project_multi.stg_a" in out
    assert "model.dbt_project_multi.stg_b" in out
    assert "model.dbt_project_multi.fct_x" not in out


def test_select_multi_expression_union(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``tag:staging,tag:marts`` takes the UNION of both atoms — all three
    models render, deduplicated by ``unique_id``, ordered by
    ``unique_id`` (DEC-001).
    """
    project_dir = _copy_fixture(tmp_path)
    _install_integration_patches(monkeypatch)

    code = main(
        ["generate", "--select", "tag:staging,tag:marts", "--project-dir", str(project_dir)]
    )
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    out = captured.out
    # All three models present, exactly once each.
    assert out.count("model.dbt_project_multi.fct_x") == 1
    assert out.count("model.dbt_project_multi.stg_a") == 1
    assert out.count("model.dbt_project_multi.stg_b") == 1
    # unique_id-sorted: fct_x < stg_a < stg_b.
    assert out.index("fct_x") < out.index("stg_a") < out.index("stg_b")


def test_select_bare_unique_id_routes_to_batch_with_single_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare ``model.<...>`` selector resolves through the batch driver
    to exactly one model. Per DEC-005, the single-match + zero-failure
    case suppresses the aggregate summary so the UX matches the v0.1
    single-model path.
    """
    project_dir = _copy_fixture(tmp_path)
    _install_integration_patches(monkeypatch)

    code = main(
        [
            "generate",
            "--select",
            "model.dbt_project_multi.stg_a",
            "--project-dir",
            str(project_dir),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    assert "--- DIFF: model.dbt_project_multi.stg_a ---" in captured.out
    # Single match + zero failures → summary SUPPRESSED (DEC-005).
    assert "Generated " not in captured.err
    assert "models failed" not in captured.err


def test_positional_model_still_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Backward compat: positional ``<model>`` (no ``--select``) routes
    to the single-model path with the v0.1 output shape. No ``[i/N]``
    prefix; no batch summary.
    """
    project_dir = _copy_fixture(tmp_path)
    _install_integration_patches(monkeypatch)

    code = main(
        [
            "generate",
            "model.dbt_project_multi.stg_a",
            "--project-dir",
            str(project_dir),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    assert "--- DIFF: model.dbt_project_multi.stg_a ---" in captured.out
    # Single-model path: NO batch artefacts on stderr.
    assert "Generated " not in captured.err
    assert "[1/1]" not in captured.err
    assert "Traceback" not in captured.err


def test_positional_and_select_mutex_argparse_error_exits_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Supplying both positional ``<model>`` AND ``--select`` is an
    argparse mutex violation (DEC-002). Exit 2; stderr names the usage.
    """
    project_dir = _copy_fixture(tmp_path)
    _install_integration_patches(monkeypatch)

    code = main(
        [
            "generate",
            "model.dbt_project_multi.stg_a",
            "--select",
            "tag:staging",
            "--project-dir",
            str(project_dir),
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    # argparse's mutex error wording.
    assert "not allowed with" in captured.err
    assert "Traceback" not in captured.err


def test_select_zero_match_exits_2_with_cli_selector_no_match_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--select tag:nonexistent_xyz`` parses cleanly but matches zero
    models. :class:`CliSelectorNoMatchError` fires (tier 2). Exit 2;
    stderr carries the documented ``matched zero models`` shape.
    """
    project_dir = _copy_fixture(tmp_path)
    _install_integration_patches(monkeypatch)

    code = main(
        [
            "generate",
            "--select",
            "tag:nonexistent_xyz",
            "--project-dir",
            str(project_dir),
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    # DEC-006 stderr shape: ``ERROR: --select '<expr>' matched zero
    # models in this project``.
    assert "ERROR:" in captured.err
    assert "matched zero models" in captured.err
    assert "tag:nonexistent_xyz" in captured.err
    assert "Traceback" not in captured.err


def test_select_parse_failure_exits_2_with_cli_selector_parse_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--select tag:`` is well-formed argparse but a malformed selector
    payload (empty tag name). :class:`CliSelectorParseError` fires (tier 2).
    Exit 2; stderr names the offending selector.
    """
    project_dir = _copy_fixture(tmp_path)
    _install_integration_patches(monkeypatch)

    code = main(["generate", "--select", "tag:", "--project-dir", str(project_dir)])
    captured = capsys.readouterr()

    assert code == 2
    assert "ERROR:" in captured.err
    assert "failed to parse" in captured.err
    assert "'tag:'" in captured.err
    assert "Traceback" not in captured.err


def test_batch_partial_failure_collects_max_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One model failing at tier 3 (``LLMRateLimitError``) does not
    abort the batch (DEC-004). The remaining models complete; the run
    returns ``max(3, 0, 0) == 3``. The aggregate summary on stderr names
    the failed model with ``(LLMRateLimitError)``.
    """
    project_dir = _copy_fixture(tmp_path)

    # Fail on the first draft call (alphabetically first match — fct_x
    # under ``tag:staging,tag:marts``); succeed thereafter.
    call_count = {"n": 0}

    def _flaky_draft(model: Model, *_a: Any, **_kw: Any) -> DraftOutcome:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise LLMRateLimitError("rate limited", attempts=3)
        return _draft_outcome_for(model)

    _install_integration_patches(monkeypatch, draft_side_effect=_flaky_draft)

    code = main(
        [
            "generate",
            "--select",
            "tag:staging,tag:marts",
            "--project-dir",
            str(project_dir),
        ]
    )
    captured = capsys.readouterr()

    # 3 matched models; first fails (tier 3); remaining 2 succeed.
    assert code == 3, f"expected max(3, 0, 0)=3; stderr={captured.err}"
    # Two successful diffs on stdout.
    assert captured.out.count("--- DIFF:") == 2
    # Summary names the failed model.
    assert "1 models failed:" in captured.err
    assert "(LLMRateLimitError)" in captured.err
    # The first match in lex sort is fct_x.
    assert "model.dbt_project_multi.fct_x" in captured.err
    assert "Traceback" not in captured.err


def test_batch_summary_shape_to_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A multi-match all-success run emits the DEC-005 locked headline:
    ``Generated K kept / L dropped / J flagged across M models in T.Xs``.
    """
    project_dir = _copy_fixture(tmp_path)
    _install_integration_patches(monkeypatch)

    code = main(
        [
            "generate",
            "--select",
            "tag:staging,tag:marts",
            "--project-dir",
            str(project_dir),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    # The headline shape is locked verbatim by US-005's
    # ``test_format_batch_summary_headline_shape``; here we re-pin the
    # END-TO-END emission through ``main(...)`` against the real fixture.
    # Regex on the duration (variable across runs) + literal counts (0
    # everywhere because the stubbed reports carry ``kept_count=0`` etc.).
    pattern = re.compile(
        r"^Generated 0 kept / 0 dropped / 0 flagged across 3 models in \d+\.\d+s$",
        re.MULTILINE,
    )
    assert pattern.search(captured.err), (
        f"DEC-005 headline shape not found in stderr; got:\n{captured.err}"
    )
    assert "models failed" not in captured.err


def test_batch_progress_prefix_emits_under_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Under a fake TTY, each per-model iteration emits ``[i/N] <id>`` on
    stderr (DEC-014). Two matched models → ``[1/2]`` and ``[2/2]``.
    """
    project_dir = _copy_fixture(tmp_path)
    _install_integration_patches(monkeypatch)

    # Force both streams TTY so ``should_emit_progress`` returns True.
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)

    code = main(["generate", "--select", "tag:staging", "--project-dir", str(project_dir)])
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    assert "[1/2] model.dbt_project_multi.stg_a" in captured.err
    assert "[2/2] model.dbt_project_multi.stg_b" in captured.err


def test_batch_quiet_suppresses_progress_but_emits_summary_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--quiet`` suppresses the per-model ``[i/N]`` prefix.

    NOTE: the US-005 implementation also suppresses the batch summary
    under ``--quiet`` (``test_batch_summary_suppressed_under_quiet`` in
    :mod:`tests.cli.test_batch_emission`). So the asserted contract here
    is: no ``[i/N]`` prefix AND no batch summary, even when one model
    failed. The single per-model ``ERROR:`` line for the failed model
    still surfaces (it's printed by :func:`_run_single_model`'s boundary
    catch — independent of ``--quiet``).
    """
    project_dir = _copy_fixture(tmp_path)

    def _fail_once(model: Model, *_a: Any, **_kw: Any) -> DraftOutcome:
        raise LLMRateLimitError("rate limited", attempts=3)

    _install_integration_patches(monkeypatch, draft_side_effect=_fail_once)

    # TTY on — so without --quiet the prefix would normally fire.
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)

    code = main(
        [
            "generate",
            "--select",
            "tag:staging",
            "--project-dir",
            str(project_dir),
            "--quiet",
        ]
    )
    captured = capsys.readouterr()

    # Both models failed → max exit is 3.
    assert code == 3, f"stderr={captured.err}"
    # No per-model progress prefix under --quiet.
    assert "[1/2]" not in captured.err
    assert "[2/2]" not in captured.err
    # Per-model ERROR line for each failed model is still emitted (the
    # ``_run_single_model`` boundary-catch path is not gated by --quiet).
    assert captured.err.count("rate limited") == 2
    assert "Traceback" not in captured.err


def test_sidecar_last_writer_wins_across_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run a 3-model batch; assert only the LAST model's
    ``.signalforge/diff.json`` and ``.signalforge/grade.json`` persist
    (DEC-003 last-writer-wins).

    The batch runs in ``unique_id`` lex order; the last write wins. The
    three models are ``fct_x``, ``stg_a``, ``stg_b`` (lex-sorted) so the
    surviving sidecars must carry ``model_unique_id ==
    'model.dbt_project_multi.stg_b'``.
    """
    project_dir = _copy_fixture(tmp_path)
    _install_integration_patches(monkeypatch, sidecars_to_disk=True)

    code = main(
        [
            "generate",
            "--select",
            "tag:staging,tag:marts",
            "--project-dir",
            str(project_dir),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    diff_path = project_dir / ".signalforge" / "diff.json"
    grade_path = project_dir / ".signalforge" / "grade.json"
    assert diff_path.exists(), "diff sidecar not written"
    assert grade_path.exists(), "grade sidecar not written"

    diff_payload = json.loads(diff_path.read_text())
    grade_payload = json.loads(grade_path.read_text())
    # ``stg_b`` is the alphabetically last of the three matched models.
    assert diff_payload["model_unique_id"] == "model.dbt_project_multi.stg_b"
    assert grade_payload["model_unique_id"] == "model.dbt_project_multi.stg_b"
