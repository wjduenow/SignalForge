"""Public ``signalforge.skill`` subpackage — programmatic install of the
bundled SignalForge Claude Code skill.

Library callers (notebooks, scripts, CI bootstrap) can drop the bundled
``signalforge/skills/signalforge/`` tree into a target project's
``.claude/skills/signalforge/`` via :func:`install_skill`. The CLI
subcommand ``signalforge install-skill`` (issue #141, US-003) will wrap
this function and re-raise the lower-level :class:`SkillError`
subclasses into ``CliInstallSkill*Error`` wrappers so the CLI exit-code
taxonomy stays homogeneous (DEC-008).

Two-name convention
===================

The runtime code lives at ``signalforge.skill`` (singular) — this
module — mirroring the existing ``signalforge.demo``. The package-data
tree lives at ``src/signalforge/skills/signalforge/`` (plural
``skills/``), matching the install destination shape
``.claude/skills/<name>/`` and anticipating future sibling skills
(DEC-001, DEC-002).

Path-handling note
==================

:func:`install_skill` does **not** route ``dest`` through the
project-wide ``canonicalise_user_path`` helper — that helper enforces a
``project_dir`` containment boundary appropriate for paths the CLI
consumes *inside* an existing project. ``install-skill`` is the second
entry point in the toolchain (alongside ``init-demo``) that operates
*before* a project context exists, so the containment gate doesn't
apply (DEC-006).

The function still defends against symlink cycles
(``.resolve(strict=True)`` raises ``RuntimeError`` on cycles on Python
<= 3.12 and ``OSError(ELOOP)`` on >= 3.13 — gh-108958), refuses to
overwrite a symlinked SKILL.md (writing would follow the link), and
refuses a regular-file ``dest`` (DEC-005, DEC-008).

Overwrite policy (DEC-003)
==========================

:func:`install_skill` always overwrites every file SignalForge ships
(SKILL.md + bundled assets) and never touches any other file in the
destination tree. There is no ``--force`` flag in v0.1 — the policy
is upgrade-in-place friendly ("re-run install-skill, get the new
SKILL.md") and the no-``rmtree`` discipline eliminates the
``--force``-against-symlink-dest hazard ``copy_demo`` has to defend
against.

See ``plans/super/141-claude-skill-install.md`` § US-002 + DEC-002,
DEC-003, DEC-005, DEC-006, DEC-007, DEC-008, DEC-009 for the full
contract.
"""

from __future__ import annotations

import errno
import shutil
from importlib.resources import as_file, files
from pathlib import Path

from signalforge.skill.errors import (
    SkillDestPathError,
    SkillDestUnsafeError,
    SkillError,
    SkillPackageDataMissingError,
)

__all__ = [
    "SkillDestPathError",
    "SkillDestUnsafeError",
    "SkillError",
    "SkillPackageDataMissingError",
    "install_skill",
]


# Path components for the destination tree under ``<dest>``. Mirrors
# the install destination shape ``.claude/skills/<skill-name>/`` that
# the Claude Code skill loader scans.
_CLAUDE_DIR = ".claude"
_SKILLS_DIR = "skills"
_SKILL_NAME = "signalforge"
_SKILL_MD = "SKILL.md"


def install_skill(dest: Path | str) -> Path:
    """Install the bundled SignalForge Claude Code skill under ``dest``.

    Drops the bundled ``signalforge/skills/signalforge/`` tree into
    ``<dest>/.claude/skills/signalforge/``. Overwrites every file
    SignalForge ships (SKILL.md + bundled assets); preserves every other
    file already in the destination tree (DEC-003 — friendly to
    upgrade-in-place workflows).

    Parameters
    ----------
    dest:
        Destination directory. Resolved via
        ``Path(dest).expanduser().resolve(strict=True)``, falling back to
        ``resolve(strict=False)`` only when the destination does not exist
        yet (``FileNotFoundError`` / ``NotADirectoryError``) — a relative
        path resolves against the current working directory; ``~``
        expands; symlinks are followed; cycles raise
        :class:`SkillDestPathError`. Mirrors :func:`signalforge.demo.copy_demo`
        verbatim (DEC-005, DEC-006).

    Returns
    -------
    Path
        The absolute path to the installed
        ``<dest>/.claude/skills/signalforge/SKILL.md`` file. Library
        callers and the CLI's next-steps message both consume this for
        downstream messaging.

    Raises
    ------
    SkillDestPathError
        Symlink cycle at ``dest``.
    SkillDestUnsafeError
        ``dest`` exists as a regular file, OR the existing
        ``<dest>/.claude/skills/signalforge/SKILL.md`` is a symlink (we
        would otherwise follow the link and write into the link target).
    SkillPackageDataMissingError
        The bundled ``signalforge/skills/signalforge/`` tree is missing
        from the installed package (broken install).
    """

    raw = Path(dest)
    expanded_dest = raw.expanduser()
    # Resolve strict=True first so a symlink cycle surfaces on every
    # supported Python: <= 3.12 raises RuntimeError, >= 3.13 raises
    # OSError(ELOOP) (gh-108958). A genuinely missing destination (the
    # common case — the dest dir need not exist yet) raises
    # FileNotFoundError / NotADirectoryError, where we fall back to
    # strict=False. (Under 3.13, strict=False stops at the loop silently
    # and the cycle guard would never fire.) Mirrors
    # ``signalforge.demo.copy_demo`` verbatim per DEC-005.
    try:
        resolved_dest = expanded_dest.resolve(strict=True)
    except RuntimeError as exc:  # pragma: no cover - <=3.12 cycle signal
        raise SkillDestPathError(
            f"failed to resolve destination path {str(raw)!r}: {exc}",
            cause=exc,
        ) from exc
    except (FileNotFoundError, NotADirectoryError):
        # Destination does not exist yet — fall back to best-effort
        # resolution. Narrow to these two so a PermissionError / other
        # OSError surfaces instead of being silently downgraded.
        resolved_dest = expanded_dest.resolve(strict=False)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # Python >= 3.13 symlink cycle (gh-108958)
            raise SkillDestPathError(
                f"failed to resolve destination path {str(raw)!r}: {exc}",
                cause=exc,
            ) from exc
        raise

    # Shape gate — ``dest`` must be a directory (or not exist yet, in
    # which case we create the chain). A regular file (or symlink to a
    # file, etc.) cannot serve as the project root we install under.
    # Without this, the ``mkdir(parents=True)`` below would raise
    # ``NotADirectoryError`` which surfaces through the CLI as a less
    # informative wrap.
    if resolved_dest.exists() and not resolved_dest.is_dir():
        raise SkillDestUnsafeError(
            f"destination {str(resolved_dest)!r} exists but is not a directory"
        )

    # Symlinked-target defence (DEC-005). ``copytree`` with
    # ``dirs_exist_ok=True`` will faithfully overwrite a regular file at
    # the same path, but on a symlink it would follow the link and write
    # into the link target — a destination the operator did not consent
    # to. The check covers:
    #   (a) every install-tree ancestor under ``<resolved_dest>`` back to
    #       ``.claude/`` (so a symlinked ``.claude/skills/signalforge/``
    #       dir cannot smuggle writes through), AND
    #   (b) every bundled file path we will overwrite — enumerated from
    #       the source tree below — so a symlinked
    #       ``assets/SKILL.eval.json`` (or symlinked ``assets/`` dir) is
    #       refused, not just SKILL.md.
    # Refuse loudly before any source materialisation.
    target_skill_dir = resolved_dest / _CLAUDE_DIR / _SKILLS_DIR / _SKILL_NAME
    for ancestor in (
        resolved_dest / _CLAUDE_DIR,
        resolved_dest / _CLAUDE_DIR / _SKILLS_DIR,
        target_skill_dir,
    ):
        if ancestor.is_symlink():
            raise SkillDestUnsafeError(
                f"refusing to install through symlinked ancestor {str(ancestor)!r}: "
                "would follow the link and write into the resolved target. Remove the "
                "symlink first or pick a different destination."
            )

    # Source lookup via importlib.resources — handles editable installs,
    # wheel installs, and zipapp/zipimport cases. ``as_file``
    # materialises zip-extracted resources to a real Path; for
    # filesystem installs it's an effective no-op. All file I/O is
    # performed inside the ``with`` block so the materialised path is
    # valid for the duration of the copy (DEC-007 — mirrors
    # ``copy_demo`` verbatim).
    source_ref = files("signalforge").joinpath(_SKILLS_DIR).joinpath(_SKILL_NAME)
    if not source_ref.is_dir():
        raise SkillPackageDataMissingError(
            "bundled signalforge/skills/signalforge/ tree not found in the installed package"
        )

    # Per-bundled-path symlink defence. We enumerate every path the
    # bundled source ships and refuse to overwrite any of them through
    # a symlink (file OR dir). This generalises the SKILL.md-only check
    # the original DEC-005 implementation carried — a symlinked
    # ``assets/SKILL.eval.json`` or symlinked ``assets/`` dir would
    # otherwise let copytree write into an arbitrary target.
    with as_file(source_ref) as _src_for_enumeration:
        bundled_rel_paths = tuple(
            sorted(p.relative_to(_src_for_enumeration) for p in _src_for_enumeration.rglob("*"))
        )
    for rel in bundled_rel_paths:
        target = target_skill_dir / rel
        if target.is_symlink():
            raise SkillDestUnsafeError(
                f"refusing to overwrite symlinked bundled path at {str(target)!r}: "
                "would follow the link and clobber the resolved target. Remove the "
                "symlink first or pick a different destination."
            )

    # Ensure the destination chain exists. ``exist_ok=True`` so an
    # already-present skill dir (the upgrade-in-place case) is fine.
    # A non-dir component along the chain (e.g. ``<dest>/.claude`` is a
    # regular file) raises ``NotADirectoryError`` from ``mkdir``; wrap
    # to :class:`SkillDestUnsafeError` so the operator sees a typed,
    # remediation-bearing message instead of a raw OSError.
    try:
        target_skill_dir.mkdir(parents=True, exist_ok=True)
    except NotADirectoryError as exc:
        raise SkillDestUnsafeError(
            f"cannot create install chain under {str(resolved_dest)!r}: a non-directory "
            "component blocks ``.claude/skills/signalforge/``. Remove the offending file "
            "or pick a different destination."
        ) from exc

    with as_file(source_ref) as source_path:
        # ``dirs_exist_ok=True`` enables the overwrite-files /
        # preserve-siblings policy (DEC-003): copytree walks the source
        # tree and overwrites every matching file in the destination
        # tree; any file in the destination tree without a counterpart
        # in the source tree is left untouched. ``symlinks=False``
        # follows source symlinks (the shipped tree carries none — the
        # wheel-smoke negative-assertion + the parity test pin that).
        shutil.copytree(
            source_path,
            target_skill_dir,
            symlinks=False,
            dirs_exist_ok=True,
        )

    # Return the canonical resolved path to the installed SKILL.md so
    # callers (and the CLI's next-steps message) get an absolute path
    # they can hand to the user.
    return (target_skill_dir / _SKILL_MD).resolve()
