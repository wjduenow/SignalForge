"""Tests for ``signalforge generate <model>`` (US-005).

Pins the load-bearing properties of the CLI's pipeline orchestration:

* Project-root resolution (DEC-001 walk-up + DEC-027 absolute-assertion).
* Pipeline-stage ordering (DEC-025 — ``safety → draft → prune → grade →
  diff``).
* Exit-code mapping for the representative scenarios from the plan
  (happy path, unknown model, threshold-fail, rate-limit, anchor-contract
  violations, panic).
* No traceback ever leaks to stderr (DEC-016).

Heavy use of ``unittest.mock.patch`` against the stage entry-point
references attached to :mod:`signalforge.cli.generate`. The CLI imports
each stage as ``<stage>_module`` at module scope, so patches like
``signalforge.cli.generate.manifest_module.load`` swap the function
without disturbing the underlying package's own import graph.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from signalforge.cli import main
from signalforge.diff import DiffConfig
from signalforge.draft.errors import LLMOutputAnchorContractError
from signalforge.grade import GradeConfig
from signalforge.grade.errors import GradeBelowThresholdError
from signalforge.llm.errors import LLMRateLimitError
from signalforge.manifest.errors import ModelNotFoundError
from signalforge.prune import PruneConfig
from signalforge.safety import SafetyPolicy, SamplingMode
from tests.cli._factories import (
    make_candidate,
    make_diff_report,
    make_draft_outcome,
    make_fake_dbt_project,
    make_grading_report,
    make_manifest,
    make_model,
    make_prune_result,
)

# ---------------------------------------------------------------------------
# Helpers — install all stage-entry patches at once
# ---------------------------------------------------------------------------


def _install_happy_patches(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Patch every stage entry point on :mod:`signalforge.cli.generate`
    with a :class:`MagicMock` returning typed-but-trivial values.

    Returns the dict of ``{name: mock}`` so individual tests can reach
    in and override one mock's ``side_effect`` (e.g. to raise a typed
    exception).
    """
    from signalforge.cli import generate as gen_mod

    model = make_model()
    manifest = make_manifest(model)
    candidate = make_candidate(model_name=model.name)
    draft_outcome = make_draft_outcome(candidate)
    prune_result = make_prune_result(model)
    grade_report = make_grading_report(model)
    diff_report = make_diff_report(model, candidate)

    mocks: dict[str, MagicMock] = {
        "manifest_load": MagicMock(return_value=manifest),
        "load_profile": MagicMock(return_value=MagicMock()),
        "make_warehouse_adapter": MagicMock(return_value=MagicMock()),
        "load_safety_config": MagicMock(return_value=MagicMock()),
        "load_draft_config": MagicMock(return_value=MagicMock()),
        "draft_schema": MagicMock(return_value=draft_outcome),
        "load_prune_config": MagicMock(return_value=MagicMock()),
        "prune_tests": MagicMock(return_value=prune_result),
        "load_grade_config": MagicMock(return_value=MagicMock()),
        "grade_artifacts": MagicMock(return_value=grade_report),
        "load_diff_config": MagicMock(return_value=MagicMock()),
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


# ---------------------------------------------------------------------------
# Help / smoke
# ---------------------------------------------------------------------------


def test_generate_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``signalforge generate --help`` prints help and exits 0."""
    code = main(["generate", "--help"])
    captured = capsys.readouterr()
    assert code == 0
    assert "generate" in captured.out
    assert "model" in captured.out


# ---------------------------------------------------------------------------
# Project-root resolution (DEC-001 + DEC-027)
# ---------------------------------------------------------------------------


def test_generate_no_dbt_project_yml_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running outside any dbt project (no walk-up hit) exits 1."""
    monkeypatch.chdir(tmp_path)
    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 1
    assert "ERROR" in captured.err
    assert "dbt_project.yml" in captured.err
    assert "Traceback" not in captured.err


def test_generate_project_dir_override_missing_dbt_project_yml_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--project-dir <empty>`` is an absolute assertion (DEC-027) — no walk-up."""
    empty = tmp_path / "empty"
    empty.mkdir()
    code = main(["generate", "model.shop.customers", "--project-dir", str(empty)])
    captured = capsys.readouterr()
    assert code == 1
    assert "dbt_project.yml" in captured.err
    assert "Traceback" not in captured.err


def test_generate_no_project_dir_walks_up_from_subdirectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Walk-up from a subdirectory finds ``dbt_project.yml`` higher up."""
    project_dir = make_fake_dbt_project(tmp_path)
    deep = project_dir / "models" / "marts" / "deep"
    deep.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(deep)

    mocks = _install_happy_patches(monkeypatch)

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"
    # The manifest loader was called with a project_dir somewhere up the
    # walk-up chain; we just need it to have happened.
    assert mocks["manifest_load"].call_count == 1


# ---------------------------------------------------------------------------
# Happy path (US-005 acceptance: full pipeline against fakes)
# ---------------------------------------------------------------------------


def test_generate_happy_path_against_fakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end happy path: every stage patched; exit 0; stdout marker."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    assert "--- DIFF OUTPUT MARKER ---" in captured.out

    # Every stage entry point fired exactly once.
    for name in (
        "manifest_load",
        "load_profile",
        "make_warehouse_adapter",
        "load_safety_config",
        "draft_schema",
        "prune_tests",
        "grade_artifacts",
        "render_diff",
        "render_to_text",
    ):
        assert mocks[name].call_count == 1, f"{name} called {mocks[name].call_count} times"


# ---------------------------------------------------------------------------
# Stage-order test (DEC-025) — load-bearing
# ---------------------------------------------------------------------------


def test_generate_calls_stages_in_documented_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the documented ``safety → draft → prune → grade → diff``
    pipeline order (DEC-025 / CLAUDE.md "Pipeline shape").

    A future refactor that reorders these stages fails this test loudly.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    parent = MagicMock()
    parent.attach_mock(mocks["load_safety_config"], "safety")
    parent.attach_mock(mocks["draft_schema"], "draft")
    parent.attach_mock(mocks["prune_tests"], "prune")
    parent.attach_mock(mocks["grade_artifacts"], "grade")
    parent.attach_mock(mocks["render_diff"], "diff")

    code = main(["generate", "model.shop.customers"])
    assert code == 0

    # Each attached mock fires once; the recorded order of names on
    # ``parent.mock_calls`` must match the documented pipeline.
    call_order = [c[0] for c in parent.mock_calls]
    assert call_order == ["safety", "draft", "prune", "grade", "diff"], call_order


# ---------------------------------------------------------------------------
# Exit-code scenarios
# ---------------------------------------------------------------------------


def test_generate_unknown_model_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A model that doesn't exist in the manifest → exit 2."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    # Override: manifest.get_model raises ModelNotFoundError. We make the
    # manifest's get_model attribute raise.
    bad_manifest = MagicMock()
    bad_manifest.get_model.side_effect = ModelNotFoundError(
        "model 'model.shop.does_not_exist' is not present in the manifest"
    )
    mocks["manifest_load"].return_value = bad_manifest

    code = main(["generate", "model.shop.does_not_exist"])
    captured = capsys.readouterr()
    assert code == 2, f"stderr={captured.err}"
    assert "ERROR" in captured.err
    assert "Traceback" not in captured.err


def test_generate_grade_below_threshold_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``GradeBelowThresholdError`` from the grader → exit 2 (DEC-011)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["grade_artifacts"].side_effect = GradeBelowThresholdError(
        pass_rate=0.4,
        mean_score=0.55,
        min_pass_rate=0.6,
        min_mean_score=0.6,
        aggregate_complete=True,
    )

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 2, f"stderr={captured.err}"
    assert "below threshold" in captured.err.lower()
    # The error names the failing thresholds (DEC-011).
    assert "pass_rate" in captured.err or "mean_score" in captured.err
    assert "Traceback" not in captured.err


def test_generate_llm_rate_limit_exits_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``LLMRateLimitError`` from the draft seam → exit 3 (API tier)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["draft_schema"].side_effect = LLMRateLimitError(
        "rate limited: 429 after 3 attempts",
        attempts=3,
    )

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 3, f"stderr={captured.err}"
    assert "ERROR" in captured.err
    assert "Traceback" not in captured.err


def test_generate_anchor_contract_violations_format_to_bullets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``LLMOutputAnchorContractError`` renders as header + per-violation bullets (DEC-008/017)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["draft_schema"].side_effect = LLMOutputAnchorContractError(
        "draft response violated anchor contract",
        violations=(
            "column 'phantom' not in model columns",
            "test references missing column 'ghost'",
            "duplicate not_null on 'id'",
        ),
        raw_text="{}",
        prompt_version="abcdef",
        model="claude-fake",
        cache_hit=False,
        input_tokens=100,
        output_tokens=10,
    )

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 2, f"stderr={captured.err}"
    # Header + 3 bullets.
    assert captured.err.startswith("ERROR:")
    bullet_lines = [line for line in captured.err.splitlines() if line.startswith("  - ")]
    assert len(bullet_lines) == 3
    assert "Traceback" not in captured.err


def test_generate_no_traceback_on_panic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Untyped ``RuntimeError`` from a stage → exit 1, no traceback (DEC-016)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["draft_schema"].side_effect = RuntimeError("kaboom")

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 1, f"stderr={captured.err}"
    assert "Traceback" not in captured.err
    assert "kaboom" in captured.err


# ---------------------------------------------------------------------------
# --manifest / --profiles-dir flag plumbing
# ---------------------------------------------------------------------------


def test_generate_manifest_override_flows_through_canonicalise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--manifest`` is canonicalised against the resolved ``project_dir``.

    The resolved path is forwarded as the ``manifest_path`` kwarg to
    :func:`signalforge.manifest.load`.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    custom = project_dir / "target" / "manifest_custom.json"
    custom.write_text("{}", encoding="utf-8")

    mocks = _install_happy_patches(monkeypatch)
    code = main(
        [
            "generate",
            "model.shop.customers",
            "--manifest",
            str(custom),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"
    # manifest.load was called with manifest_path resolving to custom.
    call = mocks["manifest_load"].call_args
    forwarded: Any = call.kwargs.get("manifest_path")
    assert forwarded is not None
    assert Path(forwarded) == custom.resolve()


def test_generate_profiles_dir_sets_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--profiles-dir`` sets ``DBT_PROFILES_DIR`` for the run."""
    import os

    project_dir = make_fake_dbt_project(tmp_path)
    profiles_dir = project_dir / "profiles"
    profiles_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)

    _install_happy_patches(monkeypatch)
    code = main(
        [
            "generate",
            "model.shop.customers",
            "--profiles-dir",
            str(profiles_dir),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"
    assert os.environ.get("DBT_PROFILES_DIR") == str(profiles_dir.resolve())


def test_generate_profiles_dir_accepts_out_of_tree_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--profiles-dir`` may live outside the project tree (e.g. ``~/.dbt``).

    Regression: pass-1 quality gate caught that a containment-gated
    ``canonicalise_user_path`` call rejected every realistic profiles
    location (the dbt convention places ``profiles.yml`` at ``~/.dbt/``,
    which is intentionally outside the project tree). The flag now
    bypasses the project-dir containment gate; it still applies
    ``expanduser`` + ``resolve`` for symlink-loop safety, and the
    warehouse loader retains its own existence/shape gate on the
    resolved file.
    """
    import os

    project_dir = make_fake_dbt_project(tmp_path)
    # Sibling directory of project_dir — definitely outside the tree.
    out_of_tree = tmp_path / "external_profiles"
    out_of_tree.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)

    _install_happy_patches(monkeypatch)
    code = main(
        [
            "generate",
            "model.shop.customers",
            "--profiles-dir",
            str(out_of_tree),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"
    assert os.environ.get("DBT_PROFILES_DIR") == str(out_of_tree.resolve())


# ---------------------------------------------------------------------------
# US-006 — Generate flags (--mode, --min-score, --write/--dry-run, --format)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode_str,enum_value",
    [
        ("schema-only", SamplingMode.SCHEMA_ONLY),
        ("aggregate-only", SamplingMode.AGGREGATE_ONLY),
        ("sample", SamplingMode.SAMPLE),
    ],
)
def test_generate_mode_overrides_safety_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode_str: str,
    enum_value: SamplingMode,
) -> None:
    """``--mode`` flag flows through SafetyPolicy.with_mode (DEC-002).

    The override applies AFTER ``load_safety_config`` so the precedence
    chain is flag > signalforge.yml > library default. Asserts the
    policy reaching ``draft_schema`` carries the overridden mode.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    # The default SafetyPolicy(mode=SCHEMA_ONLY) is what
    # load_safety_config returns when no signalforge.yml is present.
    mocks["load_safety_config"].return_value = SafetyPolicy()

    code = main(["generate", "model.shop.customers", "--mode", mode_str])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    # The policy that reached ``draft_schema`` is the override-applied one.
    draft_call = mocks["draft_schema"].call_args
    forwarded_policy = draft_call.args[2]  # signature: (model, adapter, policy, manifest)
    assert isinstance(forwarded_policy, SafetyPolicy)
    assert forwarded_policy.mode is enum_value


def test_generate_mode_invalid_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Argparse rejects ``--mode bogus`` → exit 2 with usage error."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)

    code = main(["generate", "model.shop.customers", "--mode", "bogus"])
    captured = capsys.readouterr()
    assert code == 2
    # argparse usage error references the rejected choice.
    assert "bogus" in captured.err or "invalid choice" in captured.err.lower()


def test_generate_min_score_overrides_aggregate_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--min-score`` overrides ``grade.min_mean_score`` on the
    GradeConfig that reaches ``grade_artifacts`` (DEC-004).

    Reporting-only: the override changes the **aggregate-verdict**
    threshold consumed by ``GradingReport.passed`` (and, when
    ``grade.fail_on_below_threshold=true`` in ``signalforge.yml``,
    by ``GradeBelowThresholdError``). It does NOT flip
    ``fail_on_below_threshold`` itself, and it does NOT drive the diff
    renderer's ``flagged`` tier — that's a per-criterion signal set by
    the LLM judge, separate from the aggregate threshold.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    # Start from a real, default GradeConfig so the override path
    # exercises ``model_validate`` rather than touching a MagicMock.
    mocks["load_grade_config"].return_value = GradeConfig()

    code = main(["generate", "model.shop.customers", "--min-score", "0.95"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    grade_call = mocks["grade_artifacts"].call_args
    forwarded_config = grade_call.kwargs["config"]
    assert isinstance(forwarded_config, GradeConfig)
    assert forwarded_config.min_mean_score == pytest.approx(0.95)
    # Reporting-only: ``fail_on_below_threshold`` stays at its default
    # (``False``) — the flag must NOT enable it.
    assert forwarded_config.fail_on_below_threshold is False


def test_generate_min_score_out_of_range_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--min-score 1.5`` → exit 2 (CliInputError tier) with remediation."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    _install_happy_patches(monkeypatch)

    code = main(["generate", "model.shop.customers", "--min-score", "1.5"])
    captured = capsys.readouterr()
    assert code == 2, f"stderr={captured.err}"
    assert "0.0" in captured.err and "1.0" in captured.err
    assert "Traceback" not in captured.err


def test_generate_write_writes_schema_yml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--write`` passes ``output_path = <project>/<model_dir>/schema.yml``
    to ``render_diff`` and keeps the sidecar enabled (DEC-002 default-on)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    code = main(["generate", "model.shop.customers", "--write"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    render_call = mocks["render_diff"].call_args
    output_path = render_call.kwargs["output_path"]
    assert output_path is not None
    # ``make_model`` sets original_file_path = "models/customers.sql" so
    # the schema.yml lands beside it.
    assert Path(output_path) == project_dir / "models" / "schema.yml"
    # ``--write`` keeps the default-on sidecar.
    assert render_call.kwargs["write_sidecar"] is True


def test_generate_default_no_write_passes_no_output_path_keeps_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Neither flag (default): ``output_path=None`` and
    ``write_sidecar=True`` (default-on per DEC-002)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    render_call = mocks["render_diff"].call_args
    assert render_call.kwargs["output_path"] is None
    assert render_call.kwargs["write_sidecar"] is True


def test_generate_dry_run_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` runs the full pipeline but writes nothing —
    neither schema.yml nor the sidecar (DEC-010)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    code = main(["generate", "model.shop.customers", "--dry-run"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    render_call = mocks["render_diff"].call_args
    assert render_call.kwargs["output_path"] is None
    assert render_call.kwargs["write_sidecar"] is False
    # Pipeline still ran end-to-end → diff still on stdout.
    assert "--- DIFF OUTPUT MARKER ---" in captured.out


def test_generate_write_and_dry_run_mutex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--write --dry-run`` is rejected by argparse mutex group → exit 2."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    _install_happy_patches(monkeypatch)

    code = main(["generate", "model.shop.customers", "--write", "--dry-run"])
    captured = capsys.readouterr()
    assert code == 2
    # argparse usage error mentions one of the conflicting options.
    err_low = captured.err.lower()
    assert "--write" in err_low or "--dry-run" in err_low or "not allowed" in err_low


# ---------------------------------------------------------------------------
# US-012 of #116 — generate --write writes proposed .sql + --force policy
# ---------------------------------------------------------------------------


def _diff_report_with_test_files(model: Any, candidate: Any) -> Any:
    """Build a :class:`DiffReport` whose ``proposed_test_files`` carries one
    marked singular ``.sql`` proposal, mirroring what the diff emitter would
    produce (``_with_marker``-prefixed ``sql`` + an ``anchor_to_filename`` path).
    """
    from signalforge.diff._test_file_writer import _with_marker
    from signalforge.diff.models import DiffReport, ProposedTestFile

    base = make_diff_report(model, candidate)
    proposed = ProposedTestFile(
        path="tests/customers__amount_custom_sql_deadbeef.sql",
        sql=_with_marker(
            "SELECT * FROM {{ ref('customers') }} WHERE amount < 0",
            args_hash="deadbeef",
        ),
    )
    return DiffReport.model_validate({**base.model_dump(), "proposed_test_files": (proposed,)})


def test_generate_write_writes_proposed_sql_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--write`` materialises every proposed singular ``.sql`` test to its
    ``tests/`` path with the ``-- signalforge:generated`` marker (DEC-010)."""
    from signalforge.diff._test_file_writer import _GENERATED_MARKER_PREFIX

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)
    model = make_model()
    candidate = make_candidate(model_name=model.name)
    mocks["render_diff"].return_value = _diff_report_with_test_files(model, candidate)

    code = main(["generate", "model.shop.customers", "--write"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    written = project_dir / "tests" / "customers__amount_custom_sql_deadbeef.sql"
    assert written.is_file(), "proposed .sql test should be written under tests/"
    content = written.read_text(encoding="utf-8")
    assert content.startswith(f"{_GENERATED_MARKER_PREFIX} deadbeef")
    assert "WHERE amount < 0" in content
    assert "Traceback" not in captured.err


def test_generate_dry_run_writes_no_sql_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` writes nothing — including no proposed ``.sql`` files."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)
    model = make_model()
    candidate = make_candidate(model_name=model.name)
    mocks["render_diff"].return_value = _diff_report_with_test_files(model, candidate)

    code = main(["generate", "model.shop.customers", "--dry-run"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    written = project_dir / "tests" / "customers__amount_custom_sql_deadbeef.sql"
    assert not written.exists(), "--dry-run must not write proposed .sql files"


def test_generate_default_no_write_writes_no_sql_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default (neither --write nor --dry-run): no proposed ``.sql`` files."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)
    model = make_model()
    candidate = make_candidate(model_name=model.name)
    mocks["render_diff"].return_value = _diff_report_with_test_files(model, candidate)

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    written = project_dir / "tests" / "customers__amount_custom_sql_deadbeef.sql"
    assert not written.exists()


def test_generate_write_force_overwrites_marked_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An existing ``.sql`` carrying our marker is overwritten with --force
    (DEC-010)."""
    from signalforge.diff._test_file_writer import _GENERATED_MARKER_PREFIX

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)
    model = make_model()
    candidate = make_candidate(model_name=model.name)
    mocks["render_diff"].return_value = _diff_report_with_test_files(model, candidate)

    target = project_dir / "tests" / "customers__amount_custom_sql_deadbeef.sql"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"{_GENERATED_MARKER_PREFIX} stale\n\nSELECT 1 AS old_body\n", encoding="utf-8"
    )

    code = main(["generate", "model.shop.customers", "--write", "--force"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    content = target.read_text(encoding="utf-8")
    assert "WHERE amount < 0" in content, "marked file should be overwritten with --force"
    assert "old_body" not in content


def test_generate_write_marked_file_no_force_skips_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An existing marked ``.sql`` is skipped WITHOUT --force; stderr WARNING
    names the file; the file is left untouched (DEC-010)."""
    from signalforge.diff._test_file_writer import _GENERATED_MARKER_PREFIX

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)
    model = make_model()
    candidate = make_candidate(model_name=model.name)
    mocks["render_diff"].return_value = _diff_report_with_test_files(model, candidate)

    target = project_dir / "tests" / "customers__amount_custom_sql_deadbeef.sql"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = f"{_GENERATED_MARKER_PREFIX} stale\n\nSELECT 1 AS old_body\n"
    target.write_text(original, encoding="utf-8")

    code = main(["generate", "model.shop.customers", "--write"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    # Untouched.
    assert target.read_text(encoding="utf-8") == original
    # WARNING names the file and mentions --force.
    assert "customers__amount_custom_sql_deadbeef.sql" in captured.err
    assert "--force" in captured.err
    assert "Traceback" not in captured.err


def test_generate_write_force_never_overwrites_hand_authored_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hand-authored ``.sql`` (no marker) is NEVER overwritten, even with
    --force; skipped with a clear stderr WARNING (DEC-010)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)
    model = make_model()
    candidate = make_candidate(model_name=model.name)
    mocks["render_diff"].return_value = _diff_report_with_test_files(model, candidate)

    target = project_dir / "tests" / "customers__amount_custom_sql_deadbeef.sql"
    target.parent.mkdir(parents=True, exist_ok=True)
    hand_authored = "SELECT * FROM customers WHERE amount IS NULL\n"
    target.write_text(hand_authored, encoding="utf-8")

    code = main(["generate", "model.shop.customers", "--write", "--force"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    # Hand-authored content untouched even with --force.
    assert target.read_text(encoding="utf-8") == hand_authored
    assert "hand-authored" in captured.err.lower()
    assert "customers__amount_custom_sql_deadbeef.sql" in captured.err
    assert "Traceback" not in captured.err


def test_generate_format_default_ansi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ``--format`` flag: ``DiffConfig.render_kind`` stays ``"ansi"`` (the
    default), and the config that reaches ``render_diff`` matches."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["load_diff_config"].return_value = DiffConfig()

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    diff_config_used = mocks["render_diff"].call_args.kwargs["config"]
    assert isinstance(diff_config_used, DiffConfig)
    assert diff_config_used.render_kind == "ansi"


def test_generate_format_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--format markdown`` re-validates the frozen DiffConfig with
    ``render_kind="markdown"``."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["load_diff_config"].return_value = DiffConfig()

    code = main(["generate", "model.shop.customers", "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    diff_config_used = mocks["render_diff"].call_args.kwargs["config"]
    assert isinstance(diff_config_used, DiffConfig)
    assert diff_config_used.render_kind == "markdown"
    # ``render_to_text`` saw the same config (so stdout dispatched
    # through MarkdownRenderer).
    rtt_config = mocks["render_to_text"].call_args.kwargs["config"]
    assert rtt_config.render_kind == "markdown"


def test_generate_format_json_routes_through_render_to_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--format json`` flips ``DiffConfig.render_kind`` to ``"json"``;
    ``render_to_text`` then dispatches to JsonRenderer for stdout."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["load_diff_config"].return_value = DiffConfig()

    code = main(["generate", "model.shop.customers", "--format", "json"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    diff_config_used = mocks["render_diff"].call_args.kwargs["config"]
    assert diff_config_used.render_kind == "json"
    rtt_config = mocks["render_to_text"].call_args.kwargs["config"]
    assert rtt_config.render_kind == "json"


# ---------------------------------------------------------------------------
# US-007 — Generate observability (--quiet, --verbose, --no-color, progress)
# ---------------------------------------------------------------------------


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``sys.stderr.isatty`` and ``sys.stdout.isatty`` to return True
    so the TTY-gated progress lines fire under ``capsys``.
    """
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)


def test_generate_emits_progress_to_stderr_in_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default TTY: five `[N/5] <stage>: ...` entry lines and five
    paired `done in <X>` lines on stderr (DEC-014 / DEC-026)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    _install_happy_patches(monkeypatch)
    _force_tty(monkeypatch)

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    # One entry line per stage (5) + one done line per stage (5).
    _stage_prefixes = ("[1/5]", "[2/5]", "[3/5]", "[4/5]", "[5/5]")
    entry_lines = [line for line in captured.err.splitlines() if line.startswith(_stage_prefixes)]
    done_lines = [line for line in entry_lines if "done in" in line]
    body_lines = [line for line in entry_lines if "done in" not in line]
    assert len(body_lines) == 5, f"expected 5 entry lines, got {body_lines}"
    assert len(done_lines) == 5, f"expected 5 done lines, got {done_lines}"

    # Stage names appear in documented order.
    assert "safety:" in body_lines[0]
    assert "draft:" in body_lines[1]
    assert "prune:" in body_lines[2]
    assert "grade:" in body_lines[3]
    assert "diff:" in body_lines[4]
    # Live values plumbed through.
    joined = "\n".join(body_lines)
    assert "criteria" in joined
    assert "calls" in joined


def test_generate_no_progress_in_non_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default capsys (non-TTY): no `[N/5]` progress lines on stderr."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    _install_happy_patches(monkeypatch)
    # Do NOT force TTY — capsys produces non-tty streams.

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"
    progress_lines = [
        line
        for line in captured.err.splitlines()
        if line.startswith(("[1/5]", "[2/5]", "[3/5]", "[4/5]", "[5/5]"))
    ]
    assert progress_lines == [], f"unexpected progress lines: {progress_lines}"


def test_generate_quiet_suppresses_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--quiet`` + TTY → no progress lines emitted."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    _install_happy_patches(monkeypatch)
    _force_tty(monkeypatch)

    code = main(["generate", "model.shop.customers", "--quiet"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"
    assert "[1/5]" not in captured.err
    assert "[5/5]" not in captured.err


def test_generate_verbose_shows_progress_and_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--verbose`` forces progress on regardless of TTY AND skips
    installing ``_safe_excepthook`` so an unexpected raise leaves a
    traceback path available (DEC-016)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)
    # Capture sys.excepthook so we can assert verbose did NOT replace it.
    import sys as _sys

    original_hook = _sys.excepthook

    code = main(["generate", "model.shop.customers", "--verbose"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    # Progress lines appeared even though capsys streams are not TTYs.
    assert "[1/5]" in captured.err
    assert "[5/5]" in captured.err

    # ``--verbose`` left the default excepthook untouched (the CLI's
    # entry point only installs the strip when verbose is False).
    assert _sys.excepthook is original_hook
    # Sanity — happy path still ran.
    assert mocks["render_to_text"].call_count == 1


def test_generate_default_run_installs_safe_excepthook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without ``--verbose`` the CLI installs ``_safe_excepthook`` so
    Python's panic path strips tracebacks (DEC-016)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    _install_happy_patches(monkeypatch)
    import sys as _sys

    from signalforge.cli._helpers import _safe_excepthook

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"
    assert _sys.excepthook is _safe_excepthook
    # Restore so the rest of the run isn't affected.
    _sys.excepthook = _sys.__excepthook__


def test_generate_quiet_and_verbose_mutex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--quiet --verbose`` is rejected by argparse mutex group → exit 2."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    _install_happy_patches(monkeypatch)

    code = main(["generate", "model.shop.customers", "--quiet", "--verbose"])
    captured = capsys.readouterr()
    assert code == 2, f"stderr={captured.err}"
    err_low = captured.err.lower()
    assert "--quiet" in err_low or "--verbose" in err_low or "not allowed" in err_low


def test_generate_no_color_sets_NO_COLOR_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--no-color`` sets ``NO_COLOR=1`` in the environment by the time
    ``render_diff`` runs (DEC-023)."""
    import os as _os

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    mocks = _install_happy_patches(monkeypatch)

    captured_env: dict[str, str | None] = {}

    def _record_env(*_args: Any, **_kwargs: Any) -> Any:
        captured_env["NO_COLOR"] = _os.environ.get("NO_COLOR")
        captured_env["FORCE_COLOR"] = _os.environ.get("FORCE_COLOR")
        return mocks["render_diff"].return_value

    mocks["render_diff"].side_effect = _record_env

    code = main(["generate", "model.shop.customers", "--no-color"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"
    assert captured_env["NO_COLOR"] == "1"
    # Belt-and-braces: FORCE_COLOR was cleared so it cannot defeat the
    # operator's explicit opt-out.
    assert captured_env["FORCE_COLOR"] is None


def test_generate_NO_COLOR_env_strips_ansi_without_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``NO_COLOR=1`` in the environment alone (no flag) keeps the
    AnsiRenderer's precedence chain in plain-text mode."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)

    # Use the real renderer dispatch by going through render_to_text;
    # the patched render_diff still returns a typed DiffReport so we
    # can call render_to_text against it.
    mocks = _install_happy_patches(monkeypatch)

    # Restore the real render_to_text so the AnsiRenderer's precedence
    # chain actually fires against the diff_report fixture.
    from signalforge.cli import generate as gen_mod
    from signalforge.diff import render_to_text as real_render_to_text

    monkeypatch.setattr(gen_mod.diff_module, "render_to_text", real_render_to_text)
    # Use a real, default DiffConfig so render_to_text dispatches to AnsiRenderer.
    mocks["load_diff_config"].return_value = DiffConfig()

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"
    # No raw ANSI escape bytes leaked into stdout.
    assert "\x1b[" not in captured.out


def test_generate_progress_uses_live_values_not_hardcoded_hints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DEC-026: progress lines must NOT carry hardcoded duration hints
    like ``"30s"`` or ``"few minutes"`` or ``"this can take"``."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    _install_happy_patches(monkeypatch)
    _force_tty(monkeypatch)

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    forbidden = ("30s", "few minutes", "this can take", "may take", "could take")
    body_lines = [
        line
        for line in captured.err.splitlines()
        if line.startswith(("[1/5]", "[2/5]", "[3/5]", "[4/5]", "[5/5]")) and "done in" not in line
    ]
    for line in body_lines:
        for needle in forbidden:
            assert needle not in line, f"forbidden hint {needle!r} in {line!r}"


# ---------------------------------------------------------------------------
# US-006 of #22 — --scope and --sample-strategy flags (DEC-011, DEC-012)
# ---------------------------------------------------------------------------


def test_generate_scope_flag_overrides_config_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--scope full`` overrides ``prune.scope=sample`` from the config
    (DEC-011). The override is applied via
    :meth:`PruneConfig.model_validate` so validators re-run (DEC-012).
    The PruneConfig forwarded to ``prune_tests`` carries
    ``scope="full"``.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    # Config-file value: scope="sample" (the library default; an
    # explicit construction makes the contract obvious).
    mocks["load_prune_config"].return_value = PruneConfig(scope="sample")

    code = main(["generate", "model.shop.customers", "--scope", "full"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    forwarded = mocks["prune_tests"].call_args.kwargs["config"]
    assert isinstance(forwarded, PruneConfig)
    assert forwarded.scope == "full"
    # The unset axis falls through unchanged.
    assert forwarded.sample_strategy == "materialised"


def test_generate_sample_strategy_flag_overrides_config_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--sample-strategy oneshot`` overrides
    ``prune.sample_strategy=materialised`` from the config (DEC-011).
    Override applied via :meth:`PruneConfig.model_validate` (DEC-012).
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["load_prune_config"].return_value = PruneConfig(sample_strategy="materialised")

    code = main(["generate", "model.shop.customers", "--sample-strategy", "oneshot"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    forwarded = mocks["prune_tests"].call_args.kwargs["config"]
    assert isinstance(forwarded, PruneConfig)
    assert forwarded.sample_strategy == "oneshot"
    # The unset axis falls through unchanged.
    assert forwarded.scope == "sample"


def test_generate_both_flags_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both flags are independent — set both at once and the orchestrator
    sees both overrides applied (DEC-011)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["load_prune_config"].return_value = PruneConfig(
        scope="sample", sample_strategy="materialised"
    )

    code = main(
        [
            "generate",
            "model.shop.customers",
            "--scope",
            "full",
            "--sample-strategy",
            "oneshot",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    forwarded = mocks["prune_tests"].call_args.kwargs["config"]
    assert isinstance(forwarded, PruneConfig)
    assert forwarded.scope == "full"
    assert forwarded.sample_strategy == "oneshot"


def test_generate_no_flag_uses_config_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Neither flag set: the PruneConfig from the config-file passes
    through unchanged. The CLI does NOT re-validate when there is no
    override (so the config-file value is preserved verbatim, including
    any future fields not yet known to the CLI)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    config_obj = PruneConfig(scope="full", sample_strategy="oneshot")
    mocks["load_prune_config"].return_value = config_obj

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    forwarded = mocks["prune_tests"].call_args.kwargs["config"]
    assert isinstance(forwarded, PruneConfig)
    assert forwarded.scope == "full"
    assert forwarded.sample_strategy == "oneshot"
    # Identity check: with no override the loaded config flows through
    # without an extra ``model_validate`` round-trip.
    assert forwarded is config_obj


def test_generate_invalid_scope_returns_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--scope invalid`` → argparse rejection → tier-2 exit, no
    traceback (cli-layer.md DEC-016)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    _install_happy_patches(monkeypatch)

    code = main(["generate", "model.shop.customers", "--scope", "invalid"])
    captured = capsys.readouterr()
    assert code == 2
    err_low = captured.err.lower()
    assert "invalid" in err_low or "invalid choice" in err_low
    assert "Traceback" not in captured.err


def test_generate_invalid_sample_strategy_returns_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--sample-strategy materialized`` (US spelling) → argparse
    rejection → tier-2 exit, no traceback. Per DEC-015 of
    ``prune-engine.md``, a typo MUST fail loud."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    _install_happy_patches(monkeypatch)

    code = main(["generate", "model.shop.customers", "--sample-strategy", "materialized"])
    captured = capsys.readouterr()
    assert code == 2
    err_low = captured.err.lower()
    assert "materialized" in err_low or "invalid choice" in err_low
    assert "Traceback" not in captured.err


def test_generate_help_text_lists_new_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge generate --help`` mentions both flag names + their
    value lists. Multi-surface parity (cli-layer.md): the argparse help
    string is surface 1 of the 5-surface contract."""
    code = main(["generate", "--help"])
    captured = capsys.readouterr()
    assert code == 0

    out = captured.out
    # Both flag names appear in the help block.
    assert "--scope" in out
    assert "--sample-strategy" in out
    # Both value lists are surfaced (argparse renders ``choices`` as
    # ``{a,b}`` in the usage line and / or the help body).
    assert "sample" in out and "full" in out
    assert "oneshot" in out and "materialised" in out


def test_generate_override_re_runs_pydantic_validators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pin DEC-012: the CLI override path uses
    :meth:`PruneConfig.model_validate` (NOT
    ``model_copy(update=...)``), so every Pydantic validator re-runs.

    Mirrors the ``safety-layer.md`` DEC-018 pin
    (:meth:`SafetyPolicy.with_mode` re-runs validators). Asserts the
    validator-bearing ``model_validate`` classmethod is reached when the
    operator supplies a flag, AND that the resulting config carries the
    override — collectively proving the override path is *not* the
    silent ``model_copy`` path.
    """
    from signalforge import prune as prune_module
    from signalforge.cli import generate as gen_mod

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["load_prune_config"].return_value = PruneConfig(scope="sample")

    real_model_validate = PruneConfig.model_validate
    seen: list[dict[str, object]] = []

    def _tracking_model_validate(payload: object, *args: object, **kwargs: object) -> PruneConfig:
        # Record the payload so we can prove it carried the override
        # (and prove model_validate was the seam that built the result).
        if isinstance(payload, dict):
            seen.append(dict(payload))
        return real_model_validate(payload, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        gen_mod.prune_module.PruneConfig,
        "model_validate",
        classmethod(lambda cls, payload, *a, **kw: _tracking_model_validate(payload, *a, **kw)),
    )

    code = main(["generate", "model.shop.customers", "--scope", "full"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    # ``model_validate`` was reached with our override payload.
    assert any(p.get("scope") == "full" for p in seen), (
        f"override payload not seen via model_validate; seen={seen}"
    )
    # And the resulting config that reached ``prune_tests`` has the
    # override applied — proving model_validate built the result.
    forwarded = mocks["prune_tests"].call_args.kwargs["config"]
    assert isinstance(forwarded, prune_module.PruneConfig)
    assert forwarded.scope == "full"


# ---------------------------------------------------------------------------
# US-003 of #35 — INFO emission when prune.enabled=false (DEC-004)
# ---------------------------------------------------------------------------


def test_generate_emits_info_when_prune_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ``prune.enabled=false`` in the config, ``cmd_generate`` emits
    exactly one INFO line surfacing the short-circuit (DEC-004 of #35).

    Lazy-format JSON per ``prune-engine.md`` DEC-017 (the grep gate at
    ``tests/llm/test_logger_grep_gate.py`` rejects f-strings in
    ``_LOGGER`` calls).

    Verified by capturing the logger's records directly via a handler
    attached AFTER ``setup_logging`` runs — ``setup_logging`` uses
    ``logging.basicConfig(force=True)`` which would strip a handler
    attached at module-import time (e.g. pytest's ``caplog``). Attaching
    inside the test (after ``main`` runs ``setup_logging``) is not an
    option because we need to capture the call that happens during
    ``main``. The fix: install the handler via ``monkeypatch`` on the
    module's ``_LOGGER`` directly so it survives the ``basicConfig``
    reset (basicConfig only touches the root logger).
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["load_prune_config"].return_value = PruneConfig(enabled=False)

    # Attach a record-collecting handler directly to the CLI logger.
    # ``setup_logging``'s ``basicConfig(force=True)`` only resets the
    # root logger's handlers, not handlers attached to named loggers,
    # so this handler survives the reset and captures the INFO emitted
    # during ``cmd_generate``.
    from signalforge.cli import generate as gen_mod

    records: list[logging.LogRecord] = []

    class _RecordCollector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _RecordCollector(level=logging.INFO)
    gen_mod._LOGGER.addHandler(handler)
    gen_mod._LOGGER.setLevel(logging.INFO)
    try:
        code = main(["generate", "model.shop.customers"])
    finally:
        gen_mod._LOGGER.removeHandler(handler)
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    matching = [
        rec
        for rec in records
        if rec.levelno == logging.INFO and "prune disabled in signalforge.yml" in rec.getMessage()
    ]
    assert len(matching) == 1, (
        f"expected exactly one prune-disabled INFO line, got {len(matching)}: "
        f"{[rec.getMessage() for rec in matching]}"
    )
    msg = matching[0].getMessage()
    assert "routing all candidates to kept-without-evidence" in msg
    # The lazy-format JSON payload carries the model unique_id and the
    # candidate_count fact (the default fake candidate ships 1 column
    # with no tests, so candidate_count == 0).
    assert "model.shop.customers" in msg
    assert '"candidate_count": 0' in msg


def test_generate_no_info_when_prune_enabled_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default ``enabled=True`` path emits no prune-disabled INFO
    line (DEC-004 of #35). Pinned so a future refactor can't silently
    flip the gate.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    # Explicit construction of the default — enabled=True is the v0.2
    # contract per US-001 of #35.
    mocks["load_prune_config"].return_value = PruneConfig(enabled=True)

    from signalforge.cli import generate as gen_mod

    records: list[logging.LogRecord] = []

    class _RecordCollector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _RecordCollector(level=logging.INFO)
    gen_mod._LOGGER.addHandler(handler)
    gen_mod._LOGGER.setLevel(logging.INFO)
    try:
        code = main(["generate", "model.shop.customers"])
    finally:
        gen_mod._LOGGER.removeHandler(handler)
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    matching = [rec for rec in records if "prune disabled in signalforge.yml" in rec.getMessage()]
    assert matching == [], (
        f"expected no prune-disabled INFO when enabled=True, got "
        f"{[rec.getMessage() for rec in matching]}"
    )
