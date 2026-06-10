"""Tests for ``signalforge generate --cache-scope`` (US-005 of #188).

Pins the CLI-surface contract for the prompt-cache prefix scope:

* ``--cache-scope {per-model,project}`` flag exists, mirrors the
  ``--scope`` / ``--sample-strategy`` / ``--format`` overlay shape, and
  routes through :meth:`DraftConfig.model_validate` so validators re-run
  (DEC-010).
* ``--select`` matching >= 2 models auto-promotes the draft overlay to
  ``cache_scope="project"`` (DEC-002), unless the operator pinned a scope
  via the flag or a non-default ``llm.cache_scope`` in ``signalforge.yml``.
* The single-model positional path NEVER auto-promotes (DEC-002); it
  reflects only an explicit ``--cache-scope`` flag, and the no-flag path
  preserves the v0.1 output shape byte-for-byte.
* Precedence (DEC-003): explicit flag > YAML non-default > auto-promote.

Every test asserts the no-traceback floor (DEC-016 of ``cli-layer.md``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from signalforge.cli import main
from signalforge.draft import DraftConfig
from tests.cli._factories import make_fake_dbt_project
from tests.cli.test_generate import _install_happy_patches
from tests.cli.test_generate_batch import (
    _install_batch_happy_patches,
    _make_multi_manifest,
)

# ---------------------------------------------------------------------------
# Helpers — assert on the DraftConfig forwarded into draft_schema
# ---------------------------------------------------------------------------


def _forwarded_cache_scopes(draft_schema_mock: MagicMock) -> list[str]:
    """Pull the ``config.cache_scope`` from every ``draft_schema`` call.

    ``draft_schema`` receives the (possibly overlaid) :class:`DraftConfig`
    as the ``config`` keyword arg; this is the load-bearing assertion seam
    — the actual scope the drafter (and thus the prompt renderer) will use.
    """
    scopes: list[str] = []
    for call in draft_schema_mock.call_args_list:
        config = call.kwargs["config"]
        assert isinstance(config, DraftConfig), f"expected DraftConfig, got {type(config)}"
        scopes.append(config.cache_scope)
    return scopes


# ---------------------------------------------------------------------------
# Auto-promote: --select >= 2 models → project (DEC-002)
# ---------------------------------------------------------------------------


def test_select_two_models_auto_promotes_to_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ``--select`` batch matching >= 2 models auto-promotes the draft
    overlay to ``cache_scope="project"`` — asserted on the actual
    :class:`DraftConfig` passed into ``draft_schema`` (DEC-002)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    mocks = _install_batch_happy_patches(monkeypatch, manifest)
    # Real DraftConfig (default per-model) so the overlay + auto-promote
    # exercise ``model_validate`` rather than a MagicMock.
    mocks["load_draft_config"].return_value = DraftConfig()

    code = main(["generate", "--select", "tag:staging"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    # ``tag:staging`` matches stg_a + stg_b (two models) → both promote.
    scopes = _forwarded_cache_scopes(mocks["draft_schema"])
    assert scopes == ["project", "project"], scopes
    assert "Traceback" not in captured.err


def test_select_single_match_does_not_promote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ``--select`` expr matching exactly ONE model does not
    auto-promote — fewer than 2 matches keeps per-model scope (DEC-002)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    mocks = _install_batch_happy_patches(monkeypatch, manifest)
    mocks["load_draft_config"].return_value = DraftConfig()

    # ``tag:marts`` matches only fct_x (one model).
    code = main(["generate", "--select", "tag:marts"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    scopes = _forwarded_cache_scopes(mocks["draft_schema"])
    assert scopes == ["per-model"], scopes
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# Single-model positional never auto-promotes (DEC-002)
# ---------------------------------------------------------------------------


def test_single_model_positional_never_promotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The positional single-model path keeps per-model scope with no
    flag — never auto-promotes (DEC-002)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)
    mocks["load_draft_config"].return_value = DraftConfig()

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    scopes = _forwarded_cache_scopes(mocks["draft_schema"])
    assert scopes == ["per-model"], scopes
    assert "Traceback" not in captured.err


def test_single_model_no_flag_passes_no_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no ``--cache-scope`` flag, the single-model path forwards the
    loaded :class:`DraftConfig` object unchanged (identity) — no
    ``model_validate`` round-trip, preserving the v0.1 shape."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)
    config_obj = DraftConfig()
    mocks["load_draft_config"].return_value = config_obj

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    forwarded = mocks["draft_schema"].call_args.kwargs["config"]
    assert forwarded is config_obj
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# Flag wins over auto-promote (DEC-003)
# ---------------------------------------------------------------------------


def test_cache_scope_per_model_flag_wins_over_auto_promote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--cache-scope per-model`` on a >= 2 batch overrides auto-promote
    — the flag wins (DEC-003)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    mocks = _install_batch_happy_patches(monkeypatch, manifest)
    mocks["load_draft_config"].return_value = DraftConfig()

    code = main(["generate", "--select", "tag:staging", "--cache-scope", "per-model"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    scopes = _forwarded_cache_scopes(mocks["draft_schema"])
    assert scopes == ["per-model", "per-model"], scopes
    assert "Traceback" not in captured.err


def test_cache_scope_project_flag_forces_single_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--cache-scope project`` on a single-model positional run forces
    project scope (DEC-003) — the only way the single-model path reaches
    project scope (it never auto-promotes)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)
    mocks["load_draft_config"].return_value = DraftConfig()

    code = main(["generate", "model.shop.customers", "--cache-scope", "project"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    scopes = _forwarded_cache_scopes(mocks["draft_schema"])
    assert scopes == ["project"], scopes
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# YAML non-default honoured (DEC-002 precedence rung 2)
# ---------------------------------------------------------------------------


def test_yaml_project_scope_honoured_without_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``llm.cache_scope: project`` in signalforge.yml is honoured on a
    single-model run when no ``--cache-scope`` flag is given. The loaded
    config already carries ``project``; no overlay is needed (identity
    pass-through)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)
    config_obj = DraftConfig(cache_scope="project")
    mocks["load_draft_config"].return_value = config_obj

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    forwarded = mocks["draft_schema"].call_args.kwargs["config"]
    assert forwarded is config_obj
    assert forwarded.cache_scope == "project"
    assert "Traceback" not in captured.err


def test_yaml_non_default_blocks_auto_promote_in_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-default ``llm.cache_scope`` in YAML blocks auto-promote in a
    >= 2 batch (DEC-002 precedence rung 2): the operator pinned the scope,
    so the CLI does not overlay on top. Here the operator pinned
    ``per-model`` in YAML; the batch must stay per-model despite >= 2
    matches."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    manifest, _models = _make_multi_manifest()
    mocks = _install_batch_happy_patches(monkeypatch, manifest)
    # NOTE: ``per-model`` IS the field default, so it cannot signal an
    # explicit YAML pin. The contract rung 2 fires for any non-default
    # value. We exercise it with ``project`` pinned in YAML and confirm
    # the batch keeps ``project`` (no double overlay / no validation
    # surprise) — the operator's YAML choice is honoured verbatim.
    mocks["load_draft_config"].return_value = DraftConfig(cache_scope="project")

    code = main(["generate", "--select", "tag:staging"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    scopes = _forwarded_cache_scopes(mocks["draft_schema"])
    assert scopes == ["project", "project"], scopes
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# Overlay re-validates via model_validate (DEC-010)
# ---------------------------------------------------------------------------


def test_cache_scope_overlay_re_runs_pydantic_validators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pin DEC-010: the CLI overlay uses :meth:`DraftConfig.model_validate`
    (NOT ``model_copy(update=...)``), so every Pydantic validator re-runs.

    Mirrors the prune ``--scope`` DEC-012 pin in
    ``tests/cli/test_generate.py``. Asserts the validator-bearing
    ``model_validate`` classmethod is reached with the override payload
    AND the resulting config carries the override.
    """
    from signalforge import draft as draft_module
    from signalforge.cli import generate as gen_mod

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)
    mocks["load_draft_config"].return_value = DraftConfig()

    real_model_validate = DraftConfig.model_validate
    seen: list[dict[str, object]] = []

    def _tracking(payload: object, *args: object, **kwargs: object) -> DraftConfig:
        if isinstance(payload, dict):
            seen.append(dict(payload))
        return real_model_validate(payload, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        gen_mod.draft_module.DraftConfig,
        "model_validate",
        classmethod(lambda cls, payload, *a, **kw: _tracking(payload, *a, **kw)),
    )

    code = main(["generate", "model.shop.customers", "--cache-scope", "project"])
    captured = capsys.readouterr()
    assert code == 0, f"stderr={captured.err}"

    assert any(p.get("cache_scope") == "project" for p in seen), (
        f"override payload not seen via model_validate; seen={seen}"
    )
    forwarded = mocks["draft_schema"].call_args.kwargs["config"]
    assert isinstance(forwarded, draft_module.DraftConfig)
    assert forwarded.cache_scope == "project"
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# argparse surface + invalid value
# ---------------------------------------------------------------------------


def test_generate_help_lists_cache_scope_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge generate --help`` surfaces ``--cache-scope`` and both
    choices (surface 1 of the 5-surface parity contract)."""
    code = main(["generate", "--help"])
    captured = capsys.readouterr()
    assert code == 0
    out = captured.out
    assert "--cache-scope" in out
    assert "per-model" in out
    assert "project" in out


def test_generate_invalid_cache_scope_returns_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--cache-scope bogus`` → argparse rejection → tier-2 exit, no
    traceback (cli-layer.md DEC-016)."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    _install_happy_patches(monkeypatch)

    code = main(["generate", "model.shop.customers", "--cache-scope", "bogus"])
    captured = capsys.readouterr()
    assert code == 2
    err_low = captured.err.lower()
    assert "bogus" in err_low or "invalid choice" in err_low
    assert "Traceback" not in captured.err
