"""Typed error hierarchy for ``signalforge.demo``.

Mirrors the layer-base pattern in every other ``signalforge.*.errors``
module (manifest, warehouse, safety, llm, draft, prune, grade, diff,
cli). The ``DemoError`` base carries an optional ``remediation`` field;
``__str__`` renders ``message`` plus a ``↳ Remediation: <text>`` line
when remediation is set. Subclasses define a ``default_remediation``
class attribute used when no explicit ``remediation`` is provided.

The CLI subcommand ``signalforge init-demo`` (see
``signalforge.cli.init_demo``) catches each concrete subclass and
re-raises it as the matching ``CliInitDemo*Error`` so the CLI
exit-code taxonomy stays homogeneous (DEC-012 of
``plans/super/47-init-demo.md``). The 7th AST scan in
``tests/test_audit_completeness.py`` walks every ``errors.py`` under
``src/signalforge/*/`` (including this one) and gates that every
concrete leaf appears in
``signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE``. The four
concretes below are mapped there at the same tiers as their CLI
wrappers (defence-in-depth — a future ``Demo*Error`` subclass that
escapes the CLI's try/except ladder will still get a sensible exit
code via ``map_exception_to_exit_code``'s MRO walk).
"""

from __future__ import annotations

__all__ = [
    "DemoDestExistsError",
    "DemoDestUnsafeError",
    "DemoError",
    "DemoFixtureMissingError",
    "DemoPathError",
]


class DemoError(Exception):
    """Abstract base for ``signalforge.demo`` errors.

    Listed in ``_EXCEPTION_MAPPING_EXCLUDED_BASES`` — every concrete
    leaf below must appear in the exit-code mapping, but the base is
    excluded (the MRO walk in ``map_exception_to_exit_code`` resolves
    forward-compat subclasses to their parent's tier).
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


class DemoPathError(DemoError):
    """Raised when the destination path cannot be canonicalised.

    Currently fires on symlink-cycle detection. The triggering error
    rides on the ``cause`` kwarg: ``RuntimeError`` on Python <= 3.12,
    ``OSError(errno.ELOOP)`` on >= 3.13 (gh-108958 changed
    ``Path.resolve()``'s cycle signal). The CLI wraps this as
    ``CliPathError`` (tier 1).
    """

    default_remediation = "Remove the symlink cycle at the destination or pick a different path."


class DemoDestExistsError(DemoError):
    """Raised when ``dest`` exists and is non-empty and ``force=False``.

    Empty existing directories proceed without ``--force`` — the gate
    only fires when there is content that ``copy_demo`` would
    otherwise have to either merge into (unsafe) or replace (requires
    explicit opt-in). The CLI wraps this as
    ``CliInitDemoDestExistsError`` (tier 2).
    """

    default_remediation = (
        "Remove the destination or pass force=True (CLI: --force) to replace it "
        "(refuses '/', $HOME, or the current working directory)."
    )


class DemoDestUnsafeError(DemoError):
    """Raised when ``force=True`` would target a catastrophic path.

    Refuses ``/``, ``Path.home()``, and ``Path.cwd()`` per DEC-001 —
    the blast-radius guard for ``signalforge init-demo --force``.
    Without the refusal, ``--force ~`` would ``rmtree($HOME)``. The
    CLI wraps this as ``CliInitDemoDestUnsafeError`` (tier 2).
    """

    default_remediation = (
        "Pick a fresh subdirectory rather than '/', $HOME, or the current working directory."
    )


class DemoFixtureMissingError(DemoError):
    """Raised when ``importlib.resources`` cannot locate the bundled
    ``_demo/`` tree.

    Indicates a broken install — the wheel target packaging should
    always ship ``src/signalforge/_demo/`` (``python-build.md`` DEC-011
    + plan DEC-002). The CLI wraps this as
    ``CliInitDemoFixtureMissingError`` (tier 1).
    """

    default_remediation = (
        "Reinstall signalforge-dbt — the bundled demo tree is missing from your install."
    )
