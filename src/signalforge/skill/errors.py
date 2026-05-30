"""Typed error hierarchy for ``signalforge.skill``.

Mirrors the layer-base pattern in every other ``signalforge.*.errors``
module (manifest, warehouse, safety, llm, draft, prune, grade, diff,
cli, demo, ingest, llm.cost — twelve before this module landed). The
:class:`SkillError` base carries an optional ``remediation`` field;
``__str__`` renders ``message`` plus a ``↳ Remediation: <text>`` line
when remediation is set. Subclasses define a ``default_remediation``
class attribute used when no explicit ``remediation`` is provided.

The CLI subcommand ``signalforge install-skill`` (issue #141 / US-003)
will catch each concrete subclass and re-raise it as the matching
``CliInstallSkill*Error`` so the CLI exit-code taxonomy stays
homogeneous (DEC-008 of ``plans/super/141-claude-skill-install.md``).
The 7th AST scan in ``tests/test_audit_completeness.py`` walks every
``errors.py`` under ``src/signalforge/*/`` (including this one — the
13th per-stage ``errors.py``) and gates that every concrete leaf
appears in ``signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE``. The
three concretes below are mapped there at the same tiers as their CLI
wrappers (defence-in-depth — a forward-compat ``Skill*Error`` subclass
that escapes the CLI's try/except ladder still gets a sensible exit
code via ``map_exception_to_exit_code``'s MRO walk).

Like :class:`signalforge.demo.errors.DemoError`, the three concretes
span tiers 1 and 2, so :class:`SkillError` itself is listed only in
``_EXCEPTION_MAPPING_EXCLUDED_BASES`` — there is no single fallback tier
that fits both classes (DEC-009).
"""

from __future__ import annotations

__all__ = [
    "SkillDestPathError",
    "SkillDestUnsafeError",
    "SkillError",
    "SkillPackageDataMissingError",
]


class SkillError(Exception):
    """Abstract base for ``signalforge.skill`` errors.

    Listed in ``_EXCEPTION_MAPPING_EXCLUDED_BASES`` — every concrete
    leaf below must appear in the exit-code mapping, but the base is
    excluded (the MRO walk in ``map_exception_to_exit_code`` resolves
    forward-compat subclasses to their parent's tier; the bases span
    tiers 1 and 2 so no single fallback tier fits — see DEC-009).
    """

    default_remediation: str | None = None

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation if remediation is not None else self.default_remediation
        self.cause = cause

    def __str__(self) -> str:
        if self.remediation is None:
            return self.message
        return f"{self.message}\n  ↳ Remediation: {self.remediation}"


class SkillDestPathError(SkillError):
    """Raised when the destination path cannot be canonicalised.

    Currently fires on symlink-cycle detection. The triggering error
    rides on the ``cause`` kwarg: ``RuntimeError`` on Python <= 3.12,
    ``OSError(errno.ELOOP)`` on >= 3.13 (gh-108958 changed
    ``Path.resolve()``'s cycle signal). The CLI wraps this as
    ``CliInstallSkillPathError`` (tier 1).
    """

    default_remediation = "Remove the symlink cycle at the destination or pick a different path."


class SkillDestUnsafeError(SkillError):
    """Raised when ``dest`` is in a shape we refuse to write under.

    Two surfaces fire this:

    * ``dest`` exists and is a regular file (not a directory) — we
      cannot create ``<file>/.claude/skills/...`` underneath it.
    * ``<dest>/.claude/skills/signalforge/SKILL.md`` exists and is a
      symlink — writing would follow the link and clobber an arbitrary
      destination, mirroring ``copy_demo``'s symlink-dest refusal.

    The CLI wraps this as ``CliInstallSkillDestUnsafeError`` (tier 2).
    """

    default_remediation = (
        "Pick an existing directory as the destination, or remove the symlinked SKILL.md first."
    )


class SkillPackageDataMissingError(SkillError):
    """Raised when ``importlib.resources`` cannot locate the bundled
    ``skills/signalforge/`` tree.

    Indicates a broken install — the wheel target packaging should
    always ship ``src/signalforge/skills/`` (``python-build.md``
    DEC-011 + plan DEC-010 of #141). The CLI wraps this as
    ``CliInstallSkillPackageDataMissingError`` (tier 1).
    """

    default_remediation = (
        "Reinstall signalforge-dbt — the bundled Claude Code skill tree is missing "
        "from your install."
    )
