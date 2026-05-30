"""Tests for ``signalforge install-skill`` (US-003 — issue #141).

In-process e2e via :func:`signalforge.cli.main` + ``capsys``. Covers the
US-003 acceptance criteria from
``plans/super/141-claude-skill-install.md``:

* happy path (fresh dest) → exit 0, INFO line on stdout per DEC-017
* pre-existing SKILL.md → exit 0, stdout appends ``(replaced existing
  SKILL.md)`` per DEC-017
* ``<dest>`` is a regular file → exit 2 (input-validation —
  :class:`CliInstallSkillDestUnsafeError`), no traceback
* ``<dest>`` resolves through a symlink cycle → exit 1 (load —
  :class:`CliInstallSkillPathError`), no traceback
* bundled package data missing (broken install) → exit 1
  (:class:`CliInstallSkillPackageDataMissingError`), no traceback
* no positional ``<dest>`` → default to current working directory per
  DEC-004; file lands at ``$CWD/.claude/skills/signalforge/SKILL.md``

Every test asserts the DEC-016 no-traceback floor on stderr (the
``cli-layer.md`` § "No traceback ever leaks" contract).
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from signalforge.cli import main


def _capture(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    return captured.out, captured.err


_INSTALLED_REL = Path(".claude") / "skills" / "signalforge" / "SKILL.md"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_install_skill_success_returns_zero_writes_file_prints_info(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fresh dest → exit 0; SKILL.md materialises under
    ``<dest>/.claude/skills/signalforge/`` and the DEC-017 stdout INFO
    line names the absolute install path.
    """
    ret = main(["install-skill", str(tmp_path)])
    out, err = _capture(capsys)
    assert ret == 0, f"stdout: {out}\nstderr: {err}"

    installed = tmp_path / _INSTALLED_REL
    assert installed.is_file(), f"expected {installed} to exist"

    # DEC-017 — single INFO line, names the absolute path.
    assert out.startswith("Installed SignalForge skill to ")
    assert str(installed.resolve()) in out
    # Fresh dest → no ``(replaced existing SKILL.md)`` suffix.
    assert "(replaced existing SKILL.md)" not in out
    # No-traceback floor.
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Overwrite path
# ---------------------------------------------------------------------------


def test_install_skill_overwrite_appends_replaced_notice(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pre-existing SKILL.md → stdout appends
    ``(replaced existing SKILL.md)`` per DEC-017.
    """
    skill_dir = tmp_path / _INSTALLED_REL.parent
    skill_dir.mkdir(parents=True)
    pre = skill_dir / "SKILL.md"
    pre.write_text("# stale prior content\n")

    ret = main(["install-skill", str(tmp_path)])
    out, err = _capture(capsys)
    assert ret == 0, f"stdout: {out}\nstderr: {err}"

    assert pre.is_file()
    # The file was overwritten — its content no longer matches the seed.
    assert pre.read_text() != "# stale prior content\n"

    # DEC-017 contract: the replaced-existing notice appended to the
    # single INFO line.
    assert out.startswith("Installed SignalForge skill to ")
    assert "(replaced existing SKILL.md)" in out
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Dest-is-file → tier 2
# ---------------------------------------------------------------------------


def test_install_skill_dest_is_file_returns_two_no_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``<dest>`` is a regular file (not a directory) → exit 2
    (input-validation, :class:`CliInstallSkillDestUnsafeError`).
    """
    dest = tmp_path / "not-a-dir"
    dest.write_text("i am a regular file")

    ret = main(["install-skill", str(dest)])
    out, err = _capture(capsys)
    assert ret == 2, f"expected tier 2; got {ret}\nstdout: {out}\nstderr: {err}"
    assert err.startswith("ERROR: ")
    # The lib's ``SkillDestUnsafeError`` message names "not a directory".
    assert "not a directory" in err
    # Remediation footer surfaces.
    assert "↳ Remediation:" in err
    # The original file is untouched.
    assert dest.read_text() == "i am a regular file"
    # No-traceback floor.
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Symlink-cycle → tier 1
# ---------------------------------------------------------------------------


def test_install_skill_dest_with_symlink_cycle_returns_one_no_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink cycle at ``<dest>`` resolves to
    :class:`CliInstallSkillPathError` (tier 1), no traceback. WSL2's
    filesystem does not raise on cycles, and Python 3.13 changed the
    ``Path.resolve()`` signal from ``RuntimeError`` to
    ``OSError(ELOOP)`` (gh-108958), so we synthesise the error by
    monkeypatching the lib seam — exercises the CLI handler branch
    deterministically across every supported version.
    """
    from signalforge.cli import install_skill as install_skill_mod
    from signalforge.skill import SkillDestPathError

    def _raise(*_args: object, **_kwargs: object) -> Path:
        raise SkillDestPathError(
            "failed to resolve destination path 'fake': simulated cycle",
            cause=OSError(errno.ELOOP, "Too many levels of symbolic links"),
        )

    monkeypatch.setattr(install_skill_mod, "install_skill", _raise)
    ret = main(["install-skill", str(tmp_path)])
    out, err = _capture(capsys)
    assert ret == 1, f"expected tier 1; got {ret}\nstdout: {out}\nstderr: {err}"
    assert err.startswith("ERROR: ")
    # The wrapper carries the resolve / symlink language through.
    assert "resolve" in err.lower() or "symlink" in err.lower()
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Missing package data → tier 1
# ---------------------------------------------------------------------------


def test_install_skill_missing_package_data_returns_one_no_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulated broken install (bundled skill tree missing) → exit 1
    with the reinstall-signalforge-dbt remediation.
    """
    import signalforge.skill as skill_mod

    class _NotADir:
        def joinpath(self, _name: str) -> _NotADir:
            return self

        def is_dir(self) -> bool:
            return False

    monkeypatch.setattr(skill_mod, "files", lambda _pkg: _NotADir())

    ret = main(["install-skill", str(tmp_path)])
    out, err = _capture(capsys)
    assert ret == 1, f"stdout: {out}\nstderr: {err}"
    assert err.startswith("ERROR: ")
    # The lib's ``SkillPackageDataMissingError`` default remediation
    # points the operator at reinstalling the wheel.
    assert "Reinstall" in err or "reinstall" in err.lower()
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Default-dest is CWD (DEC-004)
# ---------------------------------------------------------------------------


def test_install_skill_default_dest_is_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No positional ``<dest>`` → default to current working directory
    per DEC-004; file materialises at
    ``$CWD/.claude/skills/signalforge/SKILL.md``.
    """
    monkeypatch.chdir(tmp_path)
    ret = main(["install-skill"])
    out, err = _capture(capsys)
    assert ret == 0, f"stdout: {out}\nstderr: {err}"

    installed = tmp_path / _INSTALLED_REL
    assert installed.is_file(), f"expected {installed} to exist after default-dest run"
    # The INFO line names the absolute resolved path, so it must contain
    # the resolved tmp_path location.
    assert str(installed.resolve()) in out
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Exit-code-table membership (paired with the 7th AST scan)
# ---------------------------------------------------------------------------


def test_cli_install_skill_wrappers_in_exit_code_table() -> None:
    """The three ``CliInstallSkill*Error`` wrappers are registered in
    :data:`_EXCEPTION_TO_EXIT_CODE` at the DEC-008 tiers.
    """
    from signalforge.cli._helpers import _EXCEPTION_TO_EXIT_CODE
    from signalforge.cli.errors import (
        CliInputError,
        CliInstallSkillDestUnsafeError,
        CliInstallSkillPackageDataMissingError,
        CliInstallSkillPathError,
    )

    # Path failure → tier 1 (load).
    assert CliInstallSkillPathError in _EXCEPTION_TO_EXIT_CODE
    assert _EXCEPTION_TO_EXIT_CODE[CliInstallSkillPathError] == 1

    # Dest-unsafe → tier 2 (input-validation); subclass of CliInputError
    # so callers can pattern-match on the base.
    assert CliInstallSkillDestUnsafeError in _EXCEPTION_TO_EXIT_CODE
    assert _EXCEPTION_TO_EXIT_CODE[CliInstallSkillDestUnsafeError] == 2
    assert issubclass(CliInstallSkillDestUnsafeError, CliInputError)

    # Package-data-missing → tier 1 (load — broken install).
    assert CliInstallSkillPackageDataMissingError in _EXCEPTION_TO_EXIT_CODE
    assert _EXCEPTION_TO_EXIT_CODE[CliInstallSkillPackageDataMissingError] == 1


# ---------------------------------------------------------------------------
# argparse help surface
# ---------------------------------------------------------------------------


def test_install_skill_help_lists_dest_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge install-skill --help`` mentions the ``DEST``
    positional (metavar) so operators know it accepts a path argument.
    """
    ret = main(["install-skill", "--help"])
    out, err = _capture(capsys)
    assert ret == 0
    assert "install-skill" in out
    assert "DEST" in out or "dest" in out.lower()
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Forward-compat belt-and-braces (DEC-016 panic-path coverage)
# ---------------------------------------------------------------------------


def test_install_skill_forward_compat_exception_belt_and_braces(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forward-compat exception type raised inside ``install_skill``
    still routes through the canonical formatter + mapper without
    leaking a traceback (the catch-all ``except Exception`` in
    ``cmd_install_skill`` per DEC-016).
    """
    from signalforge.cli import install_skill as install_skill_mod

    class _FutureSkillConcurrencyError(Exception):
        """Hypothetical v0.x error type."""

    def _raise(*_args: object, **_kwargs: object) -> Path:
        raise _FutureSkillConcurrencyError("skill busy in another process")

    monkeypatch.setattr(install_skill_mod, "install_skill", _raise)
    ret = main(["install-skill", str(tmp_path)])
    out, err = _capture(capsys)
    # Unmapped → tier 1 (panic-path default).
    assert ret == 1, f"stdout: {out}\nstderr: {err}"
    assert err.startswith("ERROR: ")
    assert "skill busy" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# KeyboardInterrupt propagation (DEC-016 carve-out)
# ---------------------------------------------------------------------------


def test_install_skill_keyboard_interrupt_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KeyboardInterrupt`` from inside ``install_skill`` propagates —
    operator Ctrl-C must reach Python's default handler for sane shell
    semantics. DEC-016 carve-out (same as ``init-demo``).
    """
    from signalforge.cli import install_skill as install_skill_mod

    def _raise(*_args: object, **_kwargs: object) -> Path:
        raise KeyboardInterrupt

    monkeypatch.setattr(install_skill_mod, "install_skill", _raise)
    with pytest.raises(KeyboardInterrupt):
        main(["install-skill", str(tmp_path)])


# ---------------------------------------------------------------------------
# Wrapper-constructor branch coverage
# ---------------------------------------------------------------------------


def test_cli_install_skill_error_constructors_render_without_cause() -> None:
    """Every ``CliInstallSkill*Error`` constructor accepts ``cause=None``
    and renders a sensible message + remediation. Exit-code-table
    assertions always pass a ``cause``; this exercises the
    ``cause is None`` branch in each wrapper.
    """
    from signalforge.cli.errors import (
        CliInstallSkillDestUnsafeError,
        CliInstallSkillPackageDataMissingError,
        CliInstallSkillPathError,
    )

    e1 = CliInstallSkillPathError(dest="/tmp/x")
    assert "resolve" in str(e1).lower()
    assert e1.cause is None
    assert "↳ Remediation:" in str(e1)

    e2 = CliInstallSkillDestUnsafeError(dest="/tmp/x")
    assert "unsafe" in str(e2).lower() or "refus" in str(e2).lower()
    assert e2.cause is None
    assert "↳ Remediation:" in str(e2)

    e3 = CliInstallSkillPackageDataMissingError()
    assert "missing" in str(e3).lower() or "skill" in str(e3).lower()
    assert e3.cause is None
    assert "↳ Remediation:" in str(e3)


# ---------------------------------------------------------------------------
# Symlink-cycle defensive path (don't follow the link)
# ---------------------------------------------------------------------------


def test_install_skill_with_symlinked_skill_md_returns_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Existing ``SKILL.md`` is a symlink → exit 2 per the lib's
    symlink-dest refusal (writing would follow the link).
    """
    skill_dir = tmp_path / _INSTALLED_REL.parent
    skill_dir.mkdir(parents=True)
    real_target = tmp_path / "elsewhere.md"
    real_target.write_text("victim file")
    skill_md = skill_dir / "SKILL.md"
    try:
        os.symlink(real_target, skill_md)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support symlinks")

    ret = main(["install-skill", str(tmp_path)])
    out, err = _capture(capsys)
    assert ret == 2, f"expected tier 2; got {ret}\nstdout: {out}\nstderr: {err}"
    assert err.startswith("ERROR: ")
    # The lib's symlink message is operator-actionable.
    assert "symlink" in err.lower()
    # Victim file untouched.
    assert real_target.read_text() == "victim file"
    assert "Traceback" not in err


def test_install_skill_handles_oserror_in_existed_before_probe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-install probe for ``existed_before`` swallows ``OSError``
    silently — a permission-denied or stat-failure on the dest tree
    must NOT abort the install (the lib seam's own failure surfaces
    through the typed except ladder below the probe instead).

    Pinned per the QG coverage gap: the ``except OSError`` branch around
    the probe was patch-coverage-dead even though it exists for a real
    reason (an unreadable parent dir during pre-probe should not punish
    the operator before the lib gets a chance to raise its own typed
    error).
    """
    import pathlib

    real_exists = pathlib.Path.exists

    def _exists_raises(self: pathlib.Path) -> bool:
        # Raise OSError only when the probe is inspecting SKILL.md.
        # Real-existence checks on tmp_path / other paths still work.
        if self.name == "SKILL.md":
            raise OSError(errno.EACCES, "Permission denied")
        return real_exists(self)

    monkeypatch.setattr(pathlib.Path, "exists", _exists_raises)

    ret = main(["install-skill", str(tmp_path)])
    out, err = _capture(capsys)
    # Probe-OSError silently downgrades → install proceeds → exit 0.
    assert ret == 0, f"expected install to proceed; got {ret}\nstdout: {out}\nstderr: {err}"
    # Probe failed → existed_before stays False → no `(replaced existing SKILL.md)` suffix.
    assert "(replaced existing SKILL.md)" not in out
    assert "Traceback" not in err
