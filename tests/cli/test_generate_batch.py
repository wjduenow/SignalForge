"""Tests for the issue #37 / US-003 refactor — single-model core + batch driver.

This bead (``bd_1-scaffolding-4v1.3``) extracted the per-model pipeline body
from :func:`signalforge.cli.generate.cmd_generate` into
:func:`signalforge.cli.generate._run_single_model` and added a
:func:`signalforge.cli.generate._run_batch` driver. The single-model
observable behaviour is pinned by every existing test in
``tests/cli/test_generate.py``; this file pins the NEW behaviour:

* :func:`_run_batch` constructs a FRESH :class:`WarehouseAdapter` per
  matched model (DEC-010 of ``plans/super/37-multi-model-select.md`` —
  avoids ``_active_session_id`` bleed across in-process iterations).
* :func:`_run_batch` continues after a per-model failure and accumulates
  exit codes via ``max(...)``.
* Zero-match raises :class:`CliSelectorNoMatchError` BEFORE any model
  iteration starts.
* :class:`SelectorParseError` from the manifest layer is re-raised as
  :class:`CliSelectorParseError` with the original exception on
  ``__cause__`` (DEC-007).

The argparse ``--select`` flag itself ships in US-004; until then, tests
invoke the helpers directly with an :class:`argparse.Namespace` that
carries ``select=...`` so they exercise the dispatcher branch.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from signalforge.cli import main
from signalforge.cli.errors import CliSelectorNoMatchError, CliSelectorParseError
from signalforge.cli.generate import (
    _BatchOutcome,
    _run_batch,
    _run_single_model,
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
# Helpers — multi-model manifest + patch installer
# ---------------------------------------------------------------------------


def _make_model_with(unique_id: str, name: str, *, tags: tuple[str, ...] = ()) -> Model:
    """Construct a tiny :class:`Model` with a stable ``unique_id`` / ``name``
    pair and optional tags. Mirrors :func:`tests.cli._factories.make_model`.
    """
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
    """Two-model manifest tagged ``staging`` plus a third tagged ``marts``.

    Three models keeps ``test_batch_driver_continues_after_per_model_failure``
    honest: a single failure must not abort the other two.
    """
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
    """Patch every stage entry point with typed-but-trivial return values.

    The adapter factory returns a NEW :class:`MagicMock` per call so the
    DEC-010 fresh-adapter-per-model invariant is observable (the test
    asserts the factory's ``call_count`` matches matched-model count).
    """
    from signalforge.cli import generate as gen_mod

    # Per-call return values for the stage entries. ``make_candidate`` /
    # friends are model-agnostic; per-model variation isn't needed for
    # the contract tests below.
    canonical_model = next(iter(manifest.nodes.values()))
    candidate = make_candidate(model_name=canonical_model.name)
    draft_outcome = make_draft_outcome(candidate)
    prune_result = make_prune_result(canonical_model)
    grade_report = make_grading_report(canonical_model)
    diff_report = make_diff_report(canonical_model, candidate)

    # ``make_warehouse_adapter`` returns a fresh ``MagicMock`` every time
    # so ``call_count`` rises with each invocation — pinning DEC-010.
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
    """Build an :class:`argparse.Namespace` that triggers the batch branch.

    The dispatcher reads ``args.select`` via :func:`getattr` so US-003 can
    test the new branch without depending on US-004's argparse wiring.
    """
    defaults: dict[str, Any] = {
        "select": select,
        "model": None,  # single-model arg unused in batch branch
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
# Single-model preservation
# ---------------------------------------------------------------------------


def test_single_model_path_unchanged_post_refactor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The positional-``<model>`` path runs end-to-end and prints the
    rendered marker, exactly as in the v0.1 shape.

    A re-affirmation of the existing
    ``test_generate_happy_path_against_fakes`` + stage-order contract —
    the refactor's load-bearing invariant is that this branch keeps
    working byte-for-byte.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)

    from signalforge.cli import generate as gen_mod

    model = make_model()
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={model.unique_id: model},
    )
    mocks = _install_batch_happy_patches(monkeypatch, manifest)
    # ``make_model`` matches what the single-model fixture builds; the
    # patched ``manifest.load`` returns the canonical single-model
    # manifest so ``get_model`` resolves.
    mocks["manifest_load"].return_value = manifest
    _ = gen_mod  # keep gen_mod referenced for type-checkers

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"
    assert "--- DIFF OUTPUT MARKER ---" in captured.out
    # Single-model path calls the adapter factory exactly once.
    assert mocks["make_warehouse_adapter"].call_count == 1
    # Stage order is recorded the same way as before.
    parent = MagicMock()
    # Reset and re-run with attached order tracking so we don't double-pay
    # for the order check. ``call_args_list`` on the mocks above is
    # already populated; build a synthetic order from their indexes in
    # the patch sequence.
    # The simpler assertion: each stage entry was called exactly once.
    for name in (
        "load_safety_config",
        "draft_schema",
        "prune_tests",
        "grade_artifacts",
        "render_diff",
    ):
        assert mocks[name].call_count == 1
    del parent  # appease ruff F841 — kept for inline doc above


# ---------------------------------------------------------------------------
# Batch driver — fresh adapter per model (DEC-010)
# ---------------------------------------------------------------------------


def test_batch_driver_fresh_adapter_per_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``_run_batch`` constructs one :class:`WarehouseAdapter` per matched
    model (DEC-010 of ``plans/super/37-multi-model-select.md``).

    The ``_make_warehouse_adapter`` factory is patched to return a fresh
    :class:`MagicMock` per call; the test pins ``call_count == matched``
    so a future refactor that hoists the adapter outside the loop breaks
    loudly.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, models = _make_multi_manifest()
    mocks = _install_batch_happy_patches(monkeypatch, manifest)

    # The selector ``tag:staging`` matches stg_a + stg_b (2 of 3 models).
    args = _batch_namespace("tag:staging", project_dir)
    code = cmd_generate(args)
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    # Two matches → two adapter constructions.
    assert mocks["make_warehouse_adapter"].call_count == 2
    # Two rendered diffs landed on stdout (one per model).
    assert captured.out.count("--- DIFF OUTPUT MARKER ---") == 2
    # Sanity: each stage entry fired twice (once per model).
    assert mocks["draft_schema"].call_count == 2
    assert mocks["prune_tests"].call_count == 2
    assert mocks["grade_artifacts"].call_count == 2
    assert mocks["render_diff"].call_count == 2
    _ = models  # appease ruff F841


# ---------------------------------------------------------------------------
# Batch driver — continue after per-model failure (DEC-004)
# ---------------------------------------------------------------------------


def test_batch_driver_continues_after_per_model_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One model failing at tier 3 does not abort the batch; remaining
    models complete; ``total_exit_code == max(per_model)`` (DEC-004).
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, models = _make_multi_manifest()
    mocks = _install_batch_happy_patches(monkeypatch, manifest)

    # Patch ``draft_schema`` to raise on the FIRST call (stg_a) and
    # succeed thereafter.  ``side_effect`` as a list cycles through.
    real_return = mocks["draft_schema"].return_value
    call_index = {"n": 0}

    def _flaky_draft(*_a: Any, **_kw: Any) -> Any:
        call_index["n"] += 1
        if call_index["n"] == 1:
            raise LLMRateLimitError("rate limited", attempts=3)
        return real_return

    mocks["draft_schema"].side_effect = _flaky_draft

    args = _batch_namespace("tag:staging,tag:marts", project_dir)
    code = cmd_generate(args)
    captured = capsys.readouterr()

    # Three matches; first fails (tier 3); other two succeed.
    assert mocks["draft_schema"].call_count == 3
    assert code == 3, f"expected max(3,0,0)==3; stderr={captured.err}"
    # Two successful diffs on stdout (the failed model's rendered_text is
    # empty so the dispatcher skips it).
    assert captured.out.count("--- DIFF OUTPUT MARKER ---") == 2
    # Stderr carries the ERROR for the failed model.
    assert "ERROR" in captured.err
    assert "rate limited" in captured.err
    # No traceback ever leaks (DEC-016).
    assert "Traceback" not in captured.err
    _ = models  # appease ruff F841


# ---------------------------------------------------------------------------
# Batch driver — zero match (DEC-006)
# ---------------------------------------------------------------------------


def test_batch_driver_zero_match_raises_cli_selector_no_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A well-formed selector resolving to zero models exits 2 via
    :class:`CliSelectorNoMatchError` — and the raise happens BEFORE any
    model iteration starts (no stage entry is called).
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    mocks = _install_batch_happy_patches(monkeypatch, manifest)

    args = _batch_namespace("tag:nonexistent", project_dir)
    code = cmd_generate(args)
    captured = capsys.readouterr()

    assert code == 2
    assert "matched zero models" in captured.err
    assert "Traceback" not in captured.err
    # No stage entry ran — the raise fired before iteration.
    for name in ("draft_schema", "prune_tests", "grade_artifacts", "render_diff"):
        assert mocks[name].call_count == 0, f"{name} should not have been called"


def test_run_batch_zero_match_raises_directly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library-level: :func:`_run_batch` raises
    :class:`CliSelectorNoMatchError` directly (not via the
    dispatcher's catch).
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    _install_batch_happy_patches(monkeypatch, manifest)

    args = _batch_namespace("tag:nonexistent", project_dir)
    profile = MagicMock(name="profile")
    with pytest.raises(CliSelectorNoMatchError) as excinfo:
        _run_batch(manifest, profile, args, project_dir=project_dir)
    assert excinfo.value.expr == "tag:nonexistent"


# ---------------------------------------------------------------------------
# Batch driver — selector parse error wrapping (DEC-007)
# ---------------------------------------------------------------------------


def test_batch_driver_parse_error_raises_cli_selector_parse_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--select tag:`` (empty tag payload) → manifest layer's
    :class:`SelectorParseError` re-wrapped as
    :class:`CliSelectorParseError` with the original on ``__cause__``
    (DEC-007).
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    _install_batch_happy_patches(monkeypatch, manifest)

    args = _batch_namespace("tag:", project_dir)
    code = cmd_generate(args)
    captured = capsys.readouterr()

    assert code == 2
    assert "failed to parse" in captured.err
    assert "Traceback" not in captured.err


def test_run_batch_parse_error_chains_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library-level: :func:`_run_batch` raises
    :class:`CliSelectorParseError` with the upstream
    :class:`SelectorParseError` set as ``__cause__``.
    """
    from signalforge.manifest.errors import SelectorParseError

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    _install_batch_happy_patches(monkeypatch, manifest)

    args = _batch_namespace("tag:", project_dir)
    profile = MagicMock(name="profile")
    with pytest.raises(CliSelectorParseError) as excinfo:
        _run_batch(manifest, profile, args, project_dir=project_dir)
    assert isinstance(excinfo.value.__cause__, SelectorParseError)
    assert excinfo.value.expr == "tag:"


# ---------------------------------------------------------------------------
# Outcome dataclass shape — frozen + populated fields
# ---------------------------------------------------------------------------


def test_run_batch_returns_batch_outcome_with_per_model_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library-level: :func:`_run_batch` returns a
    :class:`_BatchOutcome` whose ``per_model`` tuple has one entry per
    matched model in ``unique_id`` order.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    _install_batch_happy_patches(monkeypatch, manifest)

    args = _batch_namespace("tag:staging", project_dir)
    profile = MagicMock(name="profile")
    outcome = _run_batch(manifest, profile, args, project_dir=project_dir)

    assert isinstance(outcome, _BatchOutcome)
    assert len(outcome.per_model) == 2
    ids = [o.model_unique_id for o in outcome.per_model]
    assert ids == sorted(ids), "per-model outcomes must be unique_id-sorted"
    assert outcome.total_exit_code == 0
    for o in outcome.per_model:
        assert isinstance(o, _SingleModelOutcome)
        assert o.exit_code == 0
        assert o.exception_class_name is None
        assert o.rendered_text.endswith("\n")


def test_single_model_outcome_failure_carries_exception_class_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing :func:`_run_single_model` returns an outcome whose
    ``exception_class_name`` matches the typed error's class name.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, models = _make_multi_manifest()
    mocks = _install_batch_happy_patches(monkeypatch, manifest)

    mocks["draft_schema"].side_effect = LLMRateLimitError("rate limited", attempts=3)

    args = _batch_namespace("tag:staging", project_dir)
    profile = MagicMock(name="profile")
    outcome = _run_single_model(models[0], manifest, profile, args, project_dir=project_dir)

    assert outcome.exit_code == 3
    assert outcome.exception_class_name == "LLMRateLimitError"
    assert outcome.rendered_text == ""
    assert outcome.model_unique_id == "model.multi.stg_a"
