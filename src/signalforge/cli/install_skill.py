"""``signalforge install-skill`` subcommand (US-003 — issue #141).

Drops the bundled SignalForge Claude Code skill (the
``src/signalforge/skills/signalforge/`` tree) into
``<dest>/.claude/skills/signalforge/`` so a user can pair their Claude
Code session with SignalForge in one command. Wraps the
:func:`signalforge.skill.install_skill` library entry point (US-002) and
re-raises the three :class:`signalforge.skill.SkillError` subclasses at
the handler boundary as ``CliInstallSkill*Error`` wrappers so the CLI's
four-tier exit-code taxonomy stays homogeneous (DEC-008).

Path-handling note
==================

``install-skill`` is the second CLI subcommand that *creates* the
project context rather than operating *inside* one (the first is
``init-demo``), so it deliberately does **not** route ``dest`` through
:func:`signalforge.cli._helpers.canonicalise_user_path` — that helper
enforces a ``project_dir`` containment boundary appropriate for paths
the CLI consumes inside an existing project (DEC-006 of
``plans/super/141-claude-skill-install.md``). Symlink-cycle defence
still applies: :func:`signalforge.skill.install_skill` resolves ``dest``
via ``.resolve(strict=True)`` first (falling back to ``strict=False``
on ``FileNotFoundError`` / ``NotADirectoryError``) and raises
:class:`signalforge.skill.SkillDestPathError` on a cycle on every
supported Python version (gh-108958).

Default-dest is CWD
===================

The positional ``<dest>`` defaults to ``"."`` (current working
directory) per DEC-004. An operator running from the dbt project root
gets ``<CWD>/.claude/skills/signalforge/SKILL.md`` with no flag tuning
needed. Mirrors :mod:`signalforge.cli.init_demo`'s
default-to-CWD-friendly ergonomics.

Overwrite UX (DEC-017)
======================

On success the handler prints a single INFO line to stdout:

    ``Installed SignalForge skill to <abs path>``

If a SKILL.md already existed at the install path (detected BEFORE the
copy via :func:`Path.exists`), the line appends
``(replaced existing SKILL.md)``. The lib seam's overwrite policy is
upgrade-in-place friendly (DEC-003 — overwrites every file SignalForge
ships; preserves every other file in the destination tree); the CLI
surfaces just this one delta so operators know their hand-edited
SKILL.md was replaced. No ``--force`` flag, no ``.bak`` file, no diff
output — the operator can ``git diff`` if they had the file under
version control.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from signalforge.cli._helpers import (
    format_error_to_stderr,
    map_exception_to_exit_code,
    print_stderr,
)
from signalforge.cli.errors import (
    CliInstallSkillDestUnsafeError,
    CliInstallSkillPackageDataMissingError,
    CliInstallSkillPathError,
)
from signalforge.skill import (
    SkillDestPathError,
    SkillDestUnsafeError,
    SkillPackageDataMissingError,
    install_skill,
)

__all__ = ["add_parser", "cmd_install_skill"]


# Path components for the SKILL.md install location relative to
# ``<dest>``. Mirrors ``signalforge.skill``'s private constants — kept
# here for the pre-write existence probe that drives the DEC-017
# ``(replaced existing SKILL.md)`` suffix decision.
_INSTALLED_SKILL_REL: Path = Path(".claude") / "skills" / "signalforge" / "SKILL.md"


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``install-skill`` subcommand on the top-level parser.

    Mirrors the registration shape of :mod:`signalforge.cli.init_demo`
    (DEC-009 of ``.claude/rules/cli-layer.md`` — one flat module per
    subcommand). One surface:

    * Positional ``dest`` — optional (``nargs="?"``) with a string
      default of ``"."`` (current working directory) per DEC-004. String
      (not :class:`pathlib.Path`) so argparse's default stringification
      is predictable across Python versions and platforms;
      :func:`signalforge.skill.install_skill` itself runs ``Path(dest)``
      so callers can pass either form.

    Per DEC-003 there is no ``--force`` flag in v0.1 — the lib seam
    always overwrites the bundled-skill files in place and never
    touches any other file in the destination tree, so the
    ``--force``-against-symlink-dest hazard ``copy_demo`` defends
    against does not apply here.
    """
    parser = subparsers.add_parser(
        "install-skill",
        help=(
            "Install the bundled SignalForge Claude Code skill into "
            "<dest>/.claude/skills/signalforge/."
        ),
        description=(
            "Drop the bundled SignalForge Claude Code skill (SKILL.md "
            "+ assets) into <dest>/.claude/skills/signalforge/ so a "
            "Claude Code session in <dest> picks up the skill. Default "
            "<dest> is the current working directory. Overwrites the "
            "files SignalForge ships; preserves every other file in "
            "the destination tree (no --force flag, no backup file)."
        ),
    )
    parser.add_argument(
        "dest",
        nargs="?",
        default=".",
        metavar="DEST",
        help=(
            "Destination directory. Default: current working "
            "directory. The skill lands at "
            "<DEST>/.claude/skills/signalforge/SKILL.md."
        ),
    )
    parser.set_defaults(func=cmd_install_skill)


def cmd_install_skill(args: argparse.Namespace) -> int:
    """Install the bundled SignalForge skill under ``args.dest`` and
    print the DEC-017 INFO line.

    Returns the integer exit code per the four-tier CLI taxonomy
    (DEC-008 of ``.claude/rules/cli-layer.md``):

    * ``0`` — install succeeded; INFO line printed to stdout.
    * ``1`` — broken install
      (:class:`CliInstallSkillPackageDataMissingError`), symlink cycle
      (:class:`CliInstallSkillPathError`), or an unexpected
      forward-compat exception caught at the
      ``except Exception`` belt-and-braces boundary.
    * ``2`` — operator-side dest mistakes
      (:class:`CliInstallSkillDestUnsafeError`): ``<dest>`` is a
      regular file, or the existing ``SKILL.md`` is a symlink (writing
      would follow the link).

    The single ``try / except Exception`` boundary matches DEC-016 (no
    traceback ever leaks); failures route through
    :func:`format_error_to_stderr` so the canonical
    ``ERROR: <message>`` + ``↳ Remediation: <text>`` shape applies
    uniformly with the rest of the CLI.

    DEC-017 — the success path prints a single INFO line to stdout
    naming the absolute install path. When a SKILL.md already existed
    at the install target (detected BEFORE the copy), the line appends
    ``(replaced existing SKILL.md)`` so the operator knows the lib's
    upgrade-in-place overwrite policy fired.
    """
    raw_dest = args.dest
    # Pre-probe for an existing SKILL.md so the DEC-017 suffix is
    # accurate. ``Path(...).expanduser()`` is enough — we do not need
    # full canonicalisation here; the lib seam does that. ``exists()``
    # ``.exists()`` returns True for regular files AND working symlinks
    # (it follows the link); ``.is_symlink()`` returns True for symlinks
    # regardless of whether the target is broken. We OR both so the
    # probe reports "replaced" for every shape an operator would call
    # an existing SKILL.md — including a broken symlink, which the lib
    # seam refuses with ``SkillDestUnsafeError`` (the suffix is moot for
    # that path but the semantics stay honest). If the parent dir is
    # unreadable the probe silently returns False and the suffix is
    # omitted — the lib seam's own failure surfaces in the except
    # ladder below.
    try:
        target_skill_md = Path(raw_dest).expanduser() / _INSTALLED_SKILL_REL
        existed_before = target_skill_md.exists() or target_skill_md.is_symlink()
    except OSError:
        existed_before = False

    try:
        installed_path = install_skill(raw_dest)
    except SkillDestPathError as exc:
        wrapped: Exception = CliInstallSkillPathError(dest=str(raw_dest), cause=exc)
        print_stderr(format_error_to_stderr(wrapped))
        return map_exception_to_exit_code(wrapped)
    except SkillDestUnsafeError as exc:
        wrapped = CliInstallSkillDestUnsafeError(dest=str(raw_dest), cause=exc)
        print_stderr(format_error_to_stderr(wrapped))
        return map_exception_to_exit_code(wrapped)
    except SkillPackageDataMissingError as exc:
        wrapped = CliInstallSkillPackageDataMissingError(cause=exc)
        print_stderr(format_error_to_stderr(wrapped))
        return map_exception_to_exit_code(wrapped)
    except (KeyboardInterrupt, SystemExit):
        # Preserve Python's default semantics for operator Ctrl-C and
        # any clean SystemExit raised from within ``install_skill``
        # (none today, but defensive parity with the rest of the CLI).
        raise
    except Exception as exc:  # noqa: BLE001 — uniform CLI boundary catch (DEC-016)
        # Belt-and-braces — any forward-compat exception added to the
        # install helper's raise surface routes through the canonical
        # formatter + mapper rather than leaking a traceback.
        print_stderr(format_error_to_stderr(exc))
        return map_exception_to_exit_code(exc)

    # DEC-017 — single INFO line, names the absolute install path.
    # ``installed_path`` is already an absolute :class:`Path` from the
    # lib seam (``target_skill_md.resolve()``); ``str(...)`` is what
    # operators copy-paste.
    line = f"Installed SignalForge skill to {installed_path}"
    if existed_before:
        line += " (replaced existing SKILL.md)"
    print(line)
    return 0
