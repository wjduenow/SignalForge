"""Tests for the issue #37 / US-004 ``--select`` argparse wiring.

This bead (``bd_1-scaffolding-4v1.4``) wires ``--select <expr>`` onto the
``generate`` subparser as part of a ``mutually_exclusive_group(required=True)``
that ALSO contains the positional ``<model>``. The dispatcher in
:func:`cmd_generate` was already prepared by US-003 — this bead's tests
cover the argparse-level behaviour:

* Positional model alone → still works (backward compat).
* ``--select`` alone → routes to the batch driver (not an argparse usage error).
* Both positional + ``--select`` → argparse rejects with usage error (exit 2).
* Neither → argparse rejects with required-group error (exit 2).
* ``--select tag:`` (parse failure) → exit 2 with :class:`CliSelectorParseError`
  message shape.
* ``--select tag:nonexistent`` (zero match) → exit 2 with
  :class:`CliSelectorNoMatchError` message shape.

Traces to DEC-001 (grammar examples in help), DEC-002 (mutex), DEC-016
(``fnmatch`` documented).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from signalforge.cli import main
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
# Helpers — reuse the batch-driver test fixtures' style
# ---------------------------------------------------------------------------


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


def _make_multi_manifest() -> Manifest:
    """Two staging-tagged models + one marts-tagged model."""
    m_a = _make_model_with("model.multi.stg_a", "stg_a", tags=("staging",))
    m_b = _make_model_with("model.multi.stg_b", "stg_b", tags=("staging",))
    m_c = _make_model_with("model.multi.fct_x", "fct_x", tags=("marts",))
    return Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={m.unique_id: m for m in (m_a, m_b, m_c)},
    )


def _install_happy_patches(
    monkeypatch: pytest.MonkeyPatch, manifest: Manifest
) -> dict[str, MagicMock]:
    """Stub every stage entry so the pipeline runs without LLM or warehouse.

    Mirrors :func:`tests.cli.test_generate_batch._install_batch_happy_patches`
    but a slimmer copy — this test file does not exercise per-call
    variation (no flaky-draft, etc.).
    """
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
    }

    monkeypatch.setattr(gen_mod.manifest_module, "load", mocks["manifest_load"])
    monkeypatch.setattr(gen_mod.warehouse_module, "load_profile", mocks["load_profile"])
    monkeypatch.setattr(gen_mod, "_make_warehouse_adapter", mocks["make_warehouse_adapter"])
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
# Backward compat: positional <model> alone still works
# ---------------------------------------------------------------------------


def test_positional_model_alone_works_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Existing single-model invocation (positional ``<model>``, no
    ``--select``) succeeds. Validates the mutex group's ``nargs="?"`` on
    the positional did not break the v0.1 single-model path.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)

    model = make_model()
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={model.unique_id: model},
    )
    _install_happy_patches(monkeypatch, manifest)

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"
    assert "--- DIFF OUTPUT MARKER ---" in captured.out


# ---------------------------------------------------------------------------
# --select alone routes to batch driver (not argparse error)
# ---------------------------------------------------------------------------


def test_select_flag_alone_routes_to_batch_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--select tag:staging`` resolves through the parser into the
    handler (not an argparse usage error). The full pipeline runs against
    every match and exits 0.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest = _make_multi_manifest()
    mocks = _install_happy_patches(monkeypatch, manifest)

    code = main(["generate", "--select", "tag:staging"])
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    # Two staging-tagged models → adapter constructed twice (fresh per model).
    assert mocks["make_warehouse_adapter"].call_count == 2
    # Two rendered diffs on stdout.
    assert captured.out.count("--- DIFF OUTPUT MARKER ---") == 2


# ---------------------------------------------------------------------------
# Mutex: positional + --select → argparse usage error (exit 2)
# ---------------------------------------------------------------------------


def test_positional_and_select_mutex_argparse_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Supplying both positional ``<model>`` AND ``--select`` is an
    argparse mutex violation. Argparse prints a usage error and exits 2.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)

    # ``signalforge.cli.main`` catches argparse's ``SystemExit`` and
    # returns the code so the function's ``-> int`` contract holds (see
    # ``signalforge.cli.__init__.main``). Assert the returned int, not
    # ``pytest.raises``.
    code = main(["generate", "model.x.y", "--select", "tag:foo"])
    captured = capsys.readouterr()

    assert code == 2
    assert "not allowed with" in captured.err
    # No traceback ever leaks.
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# Required group: neither positional nor --select → argparse error
# ---------------------------------------------------------------------------


def test_neither_positional_nor_select_argparse_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Neither ``<model>`` nor ``--select`` supplied is an argparse
    required-group violation. Argparse prints a usage error and exits 2.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)

    # ``main`` catches argparse's ``SystemExit`` and returns the code
    # (see :func:`signalforge.cli.main`).
    code = main(["generate"])
    captured = capsys.readouterr()

    assert code == 2
    # argparse's "one of the arguments ... is required" wording.
    assert "required" in captured.err.lower()
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# --select parse failure → exit 2 with CliSelectorParseError stderr shape
# ---------------------------------------------------------------------------


def test_select_parse_failure_returns_exit_2_with_cli_selector_parse_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--select tag:`` (empty tag payload) is a well-formed argparse
    argument but a malformed selector grammar. The manifest layer's
    :class:`SelectorParseError` is re-wrapped as
    :class:`CliSelectorParseError` (tier 2). Exit code 2; stderr matches
    the documented ``selector 'tag:' failed to parse`` shape.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest = _make_multi_manifest()
    _install_happy_patches(monkeypatch, manifest)

    code = main(["generate", "--select", "tag:"])
    captured = capsys.readouterr()

    assert code == 2
    assert "failed to parse" in captured.err
    assert "'tag:'" in captured.err
    assert "Traceback" not in captured.err


def test_select_empty_string_routes_to_parse_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--select ""`` (empty string) is argparse-valid (mutex group
    accepts it as "provided") but a malformed selector. The dispatcher
    MUST route through ``_run_batch`` (not fall through to the
    single-model branch where ``args.model is None``); the parser raises
    :class:`SelectorParseError` which is re-wrapped as
    :class:`CliSelectorParseError`. Exit 2; stderr names the empty
    expression.

    Regression guard for QG pass-1 finding: truthiness check on
    ``select_expr`` previously fell through to single-model on empty
    string. Now uses ``is not None``.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest = _make_multi_manifest()
    _install_happy_patches(monkeypatch, manifest)

    code = main(["generate", "--select", ""])
    captured = capsys.readouterr()

    assert code == 2
    assert "failed to parse" in captured.err
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# --select zero-match → exit 2 with CliSelectorNoMatchError stderr shape
# ---------------------------------------------------------------------------


def test_select_zero_match_returns_exit_2_with_cli_selector_no_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--select tag:nonexistent_tag_xyz`` parses cleanly but matches
    zero models. :class:`CliSelectorNoMatchError` fires (tier 2). Exit
    code 2; stderr matches the ``matched zero models`` shape.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest = _make_multi_manifest()
    _install_happy_patches(monkeypatch, manifest)

    code = main(["generate", "--select", "tag:nonexistent_tag_xyz"])
    captured = capsys.readouterr()

    assert code == 2
    assert "matched zero models" in captured.err
    assert "Traceback" not in captured.err
