"""Typed exception hierarchy for the CLI layer (US-003 / DEC-008).

Mirrors the layer-base pattern established by :mod:`signalforge.safety.errors`
and the other v0.1 stage error modules: every class carries an optional
``remediation`` field; ``__str__`` renders ``message`` plus a
``↳ Remediation: <text>`` line when remediation is set.

The CLI only ever raises these for failures the CLI itself produces (path
canonicalisation, input validation). Stage exceptions from upstream modules
flow up through :func:`signalforge.cli._helpers.format_error_to_stderr`
unchanged — the CLI is the sink for formatting, not a re-wrapper.
"""

from __future__ import annotations


class CliError(Exception):
    """Base class for CLI-layer errors.

    Subclasses do not redefine the rendering — the base ``__str__`` handles
    the optional remediation footer uniformly. Instances may pass
    ``remediation=`` to override the (otherwise absent) footer.
    """

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation

    def __str__(self) -> str:
        if self.remediation is None:
            return self.message
        return f"{self.message}\n  ↳ Remediation: {self.remediation}"


class CliPathError(CliError):
    """Raised by :func:`canonicalise_user_path` when a user-supplied path
    fails the symlink-hardened containment gate.
    """


class CliInputError(CliError):
    """Raised when the CLI itself rejects an input value (e.g., an
    out-of-range numeric flag) before dispatching to a subcommand handler.

    Argparse-native rejections (unknown flag, missing required arg) raise
    :class:`SystemExit` directly via the parser; this class is for the
    cases the CLI validates after argparse has parsed.
    """
