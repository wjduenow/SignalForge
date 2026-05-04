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


def test_generate_min_score_drives_flagged_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--min-score`` overrides ``grade.min_mean_score`` on the
    GradeConfig that reaches ``grade_artifacts`` (DEC-004).

    Reporting-only: the override changes the threshold the diff renderer
    reads to classify the ``flagged`` tier; it does NOT flip
    ``fail_on_below_threshold`` (which lives on signalforge.yml only).
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
