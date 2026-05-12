"""Tests for ``signalforge init-demo`` (US-004 — issue #47).

In-process e2e via :func:`signalforge.cli.main` + ``capsys``. Covers the
US-004 acceptance criteria:

* happy path (fresh dest) → exit 0, next-steps printed to stdout
* DEC-014 next-steps message names both env vars + the three first-run
  commands
* non-empty dest without ``--force`` → exit 2 with remediation
* non-empty dest with ``--force`` → exit 0 (atomic replace)
* ``--force`` against ``Path.home()`` → exit 2 (dest-unsafe)
* every test asserts no traceback leaks (DEC-016 floor)
* ``--help`` lists ``DEST`` positional + ``--force`` flag
* default dest is ``./signalforge-demo/``
* the four CLI wrapper errors are registered in the exit-code table at
  the right tiers (paired with the 7th AST scan in
  ``tests/test_audit_completeness.py``)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.cli import main
from signalforge.cli._helpers import _EXCEPTION_TO_EXIT_CODE
from signalforge.cli.errors import (
    CliInitDemoCopyError,
    CliInitDemoDestExistsError,
    CliInitDemoDestUnsafeError,
    CliInitDemoFixtureMissingError,
    CliInputError,
)


def _capture(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    return captured.out, captured.err


_EXPECTED_TOP_LEVEL = frozenset(
    {
        ".gitignore",
        "dbt_project.yml",
        "models",
        "profiles.yml",
        "signalforge.yml",
        "target",
    }
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_cmd_init_demo_to_fresh_path_returns_0_and_prints_next_steps(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dest = tmp_path / "demo"
    ret = main(["init-demo", str(dest)])
    out, err = _capture(capsys)
    assert ret == 0, f"stdout: {out}\nstderr: {err}"
    # Demo tree landed.
    assert dest.is_dir()
    entries = {p.name for p in dest.iterdir()}
    assert _EXPECTED_TOP_LEVEL.issubset(entries), entries
    # Next-steps message printed to stdout.
    assert "Demo copied to" in out
    assert str(dest.resolve()) in out
    # Floor: no traceback.
    assert "Traceback" not in err


def test_cmd_init_demo_emits_next_steps_naming_env_vars(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DEC-014: the next-steps stdout names both env vars + the three commands."""
    dest = tmp_path / "demo"
    ret = main(["init-demo", str(dest)])
    out, err = _capture(capsys)
    assert ret == 0
    # Both env vars must appear in stdout (DEC-014 contract).
    assert "GOOGLE_CLOUD_PROJECT" in out
    assert "ANTHROPIC_API_KEY" in out
    # The three first-run commands must appear (DEC-014).
    assert "signalforge lint" in out
    assert "signalforge generate" in out
    assert "--dry-run" in out
    # Floor: no traceback.
    assert "Traceback" not in err


def test_cmd_init_demo_to_empty_existing_dir_returns_0(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty existing dir proceeds without --force per US-003's contract."""
    dest = tmp_path / "demo"
    dest.mkdir()
    ret = main(["init-demo", str(dest)])
    out, err = _capture(capsys)
    assert ret == 0, f"stdout: {out}\nstderr: {err}"
    assert dest.is_dir()
    assert (dest / "dbt_project.yml").is_file()
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Existence-gate
# ---------------------------------------------------------------------------


def test_cmd_init_demo_against_existing_nonempty_dir_returns_exit_2_with_remediation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dest = tmp_path / "demo"
    dest.mkdir()
    (dest / "stale.txt").write_text("don't clobber me")
    ret = main(["init-demo", str(dest)])
    out, err = _capture(capsys)
    assert ret == 2, f"stdout: {out}\nstderr: {err}"
    # Canonical CLI error shape + remediation footer.
    assert err.startswith("ERROR: ")
    assert "exists" in err
    assert "↳ Remediation:" in err
    assert "--force" in err
    # The preexisting file is untouched.
    assert (dest / "stale.txt").read_text() == "don't clobber me"
    # No traceback floor.
    assert "Traceback" not in err


def test_cmd_init_demo_force_against_existing_nonempty_dir_returns_0(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dest = tmp_path / "demo"
    dest.mkdir()
    (dest / "stale.txt").write_text("stale")
    (dest / "old_sub").mkdir()
    (dest / "old_sub" / "x.txt").write_text("also stale")
    ret = main(["init-demo", str(dest), "--force"])
    out, err = _capture(capsys)
    assert ret == 0, f"stdout: {out}\nstderr: {err}"
    # Stale content gone, demo content present.
    assert not (dest / "stale.txt").exists()
    assert not (dest / "old_sub").exists()
    assert (dest / "dbt_project.yml").is_file()
    assert "Traceback" not in err


def test_cmd_init_demo_dest_is_file_returns_exit_2_with_clear_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ``dest`` that exists as a regular file (not a directory) routes
    to tier 2 ``CliInitDemoDestExistsError`` with a "not a directory"
    message — NOT a tier-1 ``CliInitDemoCopyError`` wrap of a raw
    ``NotADirectoryError`` from ``iterdir()``. Pass-2 QG defence.
    """
    dest = tmp_path / "demo"
    dest.write_text("i am a regular file, not a directory")
    ret = main(["init-demo", str(dest)])
    out, err = _capture(capsys)
    assert ret == 2, f"expected tier 2; got {ret}\nstderr: {err}"
    assert err.startswith("ERROR: ")
    assert "not a directory" in err
    # The original file is untouched.
    assert dest.read_text() == "i am a regular file, not a directory"
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Force blast-radius guard
# ---------------------------------------------------------------------------


def test_cmd_init_demo_force_against_home_returns_exit_2_dest_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--force`` against ``Path.home()`` is refused — DEC-001."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    # Put something inside so the existence-gate would trigger if force
    # weren't refused first.
    (fake_home / "important_dotfile").write_text("don't nuke me")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    ret = main(["init-demo", str(fake_home), "--force"])
    out, err = _capture(capsys)
    assert ret == 2, f"stdout: {out}\nstderr: {err}"
    assert err.startswith("ERROR: ")
    # Message names the catastrophic-path rationale.
    assert "system or user" in err or "clobber" in err
    # Home directory + its contents survived.
    assert fake_home.is_dir()
    assert (fake_home / "important_dotfile").read_text() == "don't nuke me"
    assert "Traceback" not in err


def test_cmd_init_demo_force_against_cwd_returns_exit_2_dest_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--force`` against ``Path.cwd()`` is refused — DEC-001."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "important.txt").write_text("preserved")
    ret = main(["init-demo", str(tmp_path), "--force"])
    out, err = _capture(capsys)
    assert ret == 2, f"stdout: {out}\nstderr: {err}"
    assert err.startswith("ERROR: ")
    # cwd survived.
    assert (tmp_path / "important.txt").read_text() == "preserved"
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Floor: no traceback
# ---------------------------------------------------------------------------


def test_cmd_init_demo_never_leaks_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DEC-016 of cli-layer.md — floor-of-every-CLI-test assertion.

    Force the path-error branch by passing a destination whose parent is
    a regular file (creating it inside is impossible — ``copytree``
    raises ``OSError``). The CLI must wrap into ``CliInitDemoCopyError``
    and print ``ERROR: ...`` without leaking a traceback.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    dest = blocker / "sub"
    ret = main(["init-demo", str(dest)])
    out, err = _capture(capsys)
    assert "Traceback" not in err
    # Exit code is tier 1 — the copytree OSError is wrapped as
    # CliInitDemoCopyError (CliError → tier 1). A tier 2 outcome here
    # would mean the OSError was misrouted to an input-validation
    # wrapper; assert exact tier so a regression surfaces loudly.
    assert ret == 1, f"expected tier 1 (CliInitDemoCopyError); got {ret}\nstderr: {err}"
    assert err.startswith("ERROR: ")


# ---------------------------------------------------------------------------
# argparse help surface
# ---------------------------------------------------------------------------


def test_init_demo_help_lists_force_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge init-demo --help`` mentions ``--force``."""
    ret = main(["init-demo", "--help"])
    out, err = _capture(capsys)
    # argparse --help exits 0.
    assert ret == 0
    assert "--force" in out
    # The help text must explicitly note the non-empty-dest refusal so
    # the 5-surface parity test (US-005) can hard-assert this phrase.
    assert "non-empty" in out.lower() or "refuses" in out.lower()
    assert "Traceback" not in err


def test_init_demo_help_lists_dest_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--help`` shows the positional ``DEST`` arg."""
    ret = main(["init-demo", "--help"])
    out, _err = _capture(capsys)
    assert ret == 0
    assert "DEST" in out


def test_init_demo_default_dest_is_signalforge_demo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No positional arg → default dest ``./signalforge-demo/``."""
    monkeypatch.chdir(tmp_path)
    ret = main(["init-demo"])
    out, err = _capture(capsys)
    assert ret == 0, f"stdout: {out}\nstderr: {err}"
    default_dest = tmp_path / "signalforge-demo"
    assert default_dest.is_dir()
    assert (default_dest / "dbt_project.yml").is_file()
    assert "Traceback" not in err


def test_init_demo_help_default_string_visible(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The help output references the default-dest string ``signalforge-demo``.

    Pins DEC-001's default-name choice and pairs with the 5-surface
    parity test (US-005) for the default-dest name across the help /
    docstring / docs / DEC list.
    """
    ret = main(["init-demo", "--help"])
    out, _err = _capture(capsys)
    assert ret == 0
    assert "signalforge-demo" in out


# ---------------------------------------------------------------------------
# Exit-code table membership (paired with the 7th AST scan)
# ---------------------------------------------------------------------------


def test_cli_init_demo_dest_exists_error_in_exit_code_table() -> None:
    """The dest-exists wrapper is tier 2 — DEC-013 / 7th AST scan."""
    assert CliInitDemoDestExistsError in _EXCEPTION_TO_EXIT_CODE
    assert _EXCEPTION_TO_EXIT_CODE[CliInitDemoDestExistsError] == 2
    # Subclass relationship — every CLI-input-validation error is a
    # CliInputError so callers can pattern-match on the base.
    assert issubclass(CliInitDemoDestExistsError, CliInputError)


def test_cli_init_demo_dest_unsafe_error_in_exit_code_table() -> None:
    assert CliInitDemoDestUnsafeError in _EXCEPTION_TO_EXIT_CODE
    assert _EXCEPTION_TO_EXIT_CODE[CliInitDemoDestUnsafeError] == 2
    assert issubclass(CliInitDemoDestUnsafeError, CliInputError)


def test_cli_init_demo_fixture_missing_error_in_exit_code_table() -> None:
    """Broken-install wrapper is tier 1 — DEC-012."""
    assert CliInitDemoFixtureMissingError in _EXCEPTION_TO_EXIT_CODE
    assert _EXCEPTION_TO_EXIT_CODE[CliInitDemoFixtureMissingError] == 1


def test_cli_init_demo_copy_error_in_exit_code_table() -> None:
    """Generic copy-failure wrapper is tier 1 — DEC-012."""
    assert CliInitDemoCopyError in _EXCEPTION_TO_EXIT_CODE
    assert _EXCEPTION_TO_EXIT_CODE[CliInitDemoCopyError] == 1


# ---------------------------------------------------------------------------
# Fixture-missing path (broken-install simulation)
# ---------------------------------------------------------------------------


def test_cmd_init_demo_fixture_missing_returns_exit_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Simulated broken install → exit 1 with the broken-install remediation."""
    import signalforge.demo as demo_mod

    class _NotADir:
        def joinpath(self, name: str) -> _NotADir:
            return self

        def is_dir(self) -> bool:
            return False

    monkeypatch.setattr(demo_mod, "files", lambda pkg: _NotADir())
    ret = main(["init-demo", str(tmp_path / "demo")])
    out, err = _capture(capsys)
    assert ret == 1, f"stdout: {out}\nstderr: {err}"
    assert err.startswith("ERROR: ")
    assert "missing" in err.lower() or "fixture" in err.lower()
    assert "Reinstall" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Remediation footer surfaces in stderr (DEC-017 stderr shape)
# ---------------------------------------------------------------------------


def test_cmd_init_demo_dest_exists_remediation_mentions_force(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The remediation footer points operators at ``--force``."""
    dest = tmp_path / "demo"
    dest.mkdir()
    (dest / "stale.txt").write_text("stale")
    ret = main(["init-demo", str(dest)])
    _out, err = _capture(capsys)
    assert ret == 2
    # The remediation footer is part of the canonical CLI error shape.
    assert "↳ Remediation:" in err
    assert "--force" in err
