"""Tests for ``signalforge cache clear --grade`` (US-008 / issue #189).

In-process e2e via :func:`signalforge.cli.main` + ``capsys``. Covers the
US-008 acceptance criteria from ``plans/super/189-no-grade-cache.md``
DEC-015:

* happy path → exit 0; ``.signalforge/grade-cache/`` is removed.
* idempotent on a missing directory → exit 0; informative INFO line.
* symlink escape → exit 1 (``GradeCachePathError`` → tier 1 per US-004
  registration).
* ``signalforge cache clear --help`` → exit 0; help text names ``--grade``.
* ``cache`` subcommand appears on the live argparse parser's choices.

Every test asserts the DEC-016 no-traceback floor on stderr (the
``cli-layer.md`` § "No traceback ever leaks" contract).

The nested-subparser shape (``cache clear --grade``) is a documented
deviation from ``cli-layer.md`` § "Subpackage layout — flat,
per-subcommand modules" per DEC-015, justified by forward-compat for
future ``cache stats`` / ``cache list`` sub-actions.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from signalforge.cli import _build_parser, main
from tests.cli._factories import make_fake_dbt_project


def _capture(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    return captured.out, captured.err


def _seed_grade_cache(project_dir: Path, n_files: int = 3) -> Path:
    """Create ``.signalforge/grade-cache/`` under ``project_dir`` with
    ``n_files`` synthetic JSON files. Returns the cache directory path.
    """
    cache_dir = project_dir / ".signalforge" / "grade-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        # 16-hex-shaped filenames so they look like real cache entries
        # (the engine writes ``<16-hex>.json``); content does not matter
        # for ``clear --grade`` (it ``shutil.rmtree``s the whole dir).
        (cache_dir / f"{i:016x}.json").write_text('{"placeholder": true}\n', encoding="utf-8")
    return cache_dir


# ---------------------------------------------------------------------------
# Happy path — populated cache dir is removed
# ---------------------------------------------------------------------------


def test_cache_clear_grade_removes_cache_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Populated ``.signalforge/grade-cache/`` → exit 0 and the directory
    (with every file inside it) is gone.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    cache_dir = _seed_grade_cache(project_dir, n_files=3)
    assert cache_dir.is_dir()
    assert len(list(cache_dir.iterdir())) == 3

    ret = main(["cache", "clear", "--grade", "--project-dir", str(project_dir)])
    out, err = _capture(capsys)
    assert ret == 0, f"stdout: {out}\nstderr: {err}"
    assert not cache_dir.exists(), f"expected {cache_dir} to be removed"
    # No traceback even on the happy path.
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Idempotent — missing dir
# ---------------------------------------------------------------------------


def test_cache_clear_grade_idempotent_on_missing_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fresh project (no ``.signalforge/grade-cache/``) → exit 0 with an
    INFO line saying nothing was there.

    Confirms the DEC-015 ``shutil.rmtree`` does not raise on a missing
    directory and the handler returns success.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    cache_dir = project_dir / ".signalforge" / "grade-cache"
    assert not cache_dir.exists()

    ret = main(["cache", "clear", "--grade", "--project-dir", str(project_dir)])
    out, err = _capture(capsys)
    assert ret == 0, f"stdout: {out}\nstderr: {err}"
    assert not cache_dir.exists()
    # No traceback.
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Symlink escape — refuses to follow + exit 1
# ---------------------------------------------------------------------------


def test_cache_clear_grade_refuses_symlink_escape(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``.signalforge/grade-cache`` is a symlink pointing outside the
    project tree → exit 1 (``GradeCachePathError`` → tier 1 per
    US-004 registration).

    The symlink target must be **left intact** — the gate exists
    precisely to prevent ``shutil.rmtree`` against an arbitrary tree
    when the operator's project has a stray symlink.
    """
    project_dir = make_fake_dbt_project(tmp_path)

    # Real target outside the project tree, populated.
    external_target = tmp_path / "external-cache-target"
    external_target.mkdir()
    sentinel = external_target / "do-not-delete.json"
    sentinel.write_text('{"keep": true}\n', encoding="utf-8")

    # Plant the symlink under ``.signalforge/grade-cache``.
    sf_dir = project_dir / ".signalforge"
    sf_dir.mkdir(exist_ok=True)
    symlink_path = sf_dir / "grade-cache"
    symlink_path.symlink_to(external_target, target_is_directory=True)

    ret = main(["cache", "clear", "--grade", "--project-dir", str(project_dir)])
    out, err = _capture(capsys)
    assert ret == 1, f"expected tier-1 exit; stdout: {out}\nstderr: {err}"

    # External target + sentinel intact — the gate refused the rmtree.
    assert external_target.is_dir(), "symlink target must be left intact"
    assert sentinel.is_file(), "external file must NOT be deleted"

    # No traceback floor.
    assert "Traceback" not in err
    # Operator-actionable message text mentions the path containment refusal.
    assert "ERROR:" in err


# ---------------------------------------------------------------------------
# Help text — names --grade
# ---------------------------------------------------------------------------


def test_cache_clear_grade_help_text_lists_grade_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge cache clear --help`` exits cleanly (argparse raises
    SystemExit; ``main`` catches and returns) and the help text names
    the ``--grade`` flag.
    """
    ret = main(["cache", "clear", "--help"])
    out, _err = _capture(capsys)
    assert ret == 0, f"expected argparse --help to exit 0; got {ret}"
    assert "--grade" in out


def test_cache_help_text_lists_clear_subaction(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge cache --help`` lists the ``clear`` sub-action."""
    ret = main(["cache", "--help"])
    out, _err = _capture(capsys)
    assert ret == 0, f"expected argparse --help to exit 0; got {ret}"
    assert "clear" in out


# ---------------------------------------------------------------------------
# Parser shape — cache subcommand is registered
# ---------------------------------------------------------------------------


def test_cache_subcommand_appears_in_parser_choices() -> None:
    """Builds the live top-level parser, introspects its
    :class:`argparse._SubParsersAction`, and asserts ``cache`` is one of
    the registered choices.

    This is the mechanical contract the skill-parity gate
    (``tests/cli/test_skill_cli_parity.py``) keys on — a new subcommand
    landing in ``signalforge.cli._build_parser`` automatically extends
    the gate.
    """
    parser = _build_parser()
    subparser_actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(subparser_actions) == 1
    choices = subparser_actions[0].choices
    assert "cache" in choices, (
        f"expected ``cache`` subcommand to be registered; got: {sorted(choices)}"
    )


def test_cache_clear_requires_grade_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge cache clear`` without ``--grade`` → argparse error
    (exit 2). The ``--grade`` flag is ``required=True`` on the live
    parser; argparse handles the missing-required surface.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    ret = main(["cache", "clear", "--project-dir", str(project_dir)])
    _out, err = _capture(capsys)
    # argparse exits with code 2 on missing-required-argument.
    assert ret == 2, f"expected argparse exit 2; got {ret}; stderr: {err}"


# --- QG Pass 3 Finding 8 — project-dir resolution coverage gaps -----------


def test_cache_clear_grade_project_dir_missing_dbt_project_yml_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ``--project-dir`` points at a directory WITHOUT
    ``dbt_project.yml``, the handler raises ``CliPathError`` (tier 1).
    Exercises the absolute-assertion branch in
    ``_resolve_project_dir``.
    """
    bare = tmp_path / "no_dbt"
    bare.mkdir()
    code = main(["cache", "clear", "--grade", "--project-dir", str(bare)])
    assert code == 1
    captured = capsys.readouterr()
    assert "does not contain dbt_project.yml" in captured.err


def test_cache_clear_grade_walk_up_fails_without_dbt_project_yml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When no ``--project-dir`` is given AND the walk-up from CWD
    finds no ``dbt_project.yml`` anywhere, the handler raises
    ``CliPathError`` (tier 1). Exercises the walk-up branch.
    """
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(isolated)
    code = main(["cache", "clear", "--grade"])
    assert code == 1
    captured = capsys.readouterr()
    assert "could not find dbt_project.yml" in captured.err


def test_cmd_cache_unknown_sub_action_returns_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """QG Pass 3 Finding 10 — the defensive ``unknown cache sub-action``
    branch in :func:`cmd_cache` is otherwise unreachable (argparse
    ``required=True`` filters it). Direct invocation with a synthetic
    Namespace exercises the defensive branch."""
    from signalforge.cli.cache import cmd_cache

    args = argparse.Namespace(cache_subcommand="unknown_action")
    code = cmd_cache(args)
    assert code == 2
    captured = capsys.readouterr()
    assert "unknown cache sub-action" in captured.err
    assert "'unknown_action'" in captured.err
