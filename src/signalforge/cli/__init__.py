"""SignalForge CLI entry point (US-003 — scaffolding).

Provides :func:`main` (callable both in-process for tests and via the
``signalforge`` console-script entry registered in ``pyproject.toml``)
and re-exports the CLI-layer error classes so consumers can pattern-match
without sneaking through the private module path.

Subsequent stories (US-004 through US-008) layer in the ``lint`` and
``generate`` subcommands plus their flag plumbing; this story just lays
the foundation: top-level argparse parser with ``--version`` and the
``version`` subcommand, dispatch shape, and the helpers / errors
subsequent commands rely on.
"""

from __future__ import annotations

import argparse
import sys

import signalforge
from signalforge.cli import generate as generate_cmd
from signalforge.cli import lint as lint_cmd
from signalforge.cli import version as version_cmd
from signalforge.cli._helpers import (
    _safe_excepthook,
    canonicalise_user_path,
    format_error_to_stderr,
    map_exception_to_exit_code,
    setup_logging,
)
from signalforge.cli.errors import CliError, CliInputError, CliPathError

__all__ = [
    "CliError",
    "CliInputError",
    "CliPathError",
    "canonicalise_user_path",
    "format_error_to_stderr",
    "main",
    "map_exception_to_exit_code",
    "setup_logging",
]


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser with ``--version`` and the
    ``version`` subcommand registered.

    Subsequent stories register additional subcommands by calling
    ``their_module.add_parser(subparsers)`` from :func:`main`.
    """
    parser = argparse.ArgumentParser(
        prog="signalforge",
        description=(
            "Draft dbt schema.yml, tests, and docs with an LLM, then prune "
            "candidates against real warehouse data so only signal-bearing "
            "artifacts ship."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"signalforge {signalforge.__version__}",
        help="Print the SignalForge version and exit.",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        title="subcommands",
        metavar="<command>",
    )
    version_cmd.add_parser(subparsers)
    lint_cmd.add_parser(subparsers)
    generate_cmd.add_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns an int exit code so in-process tests can call
    ``main(["--version"])`` and assert the return value directly without
    catching :class:`SystemExit`. The console-script wrapper registered
    by ``pyproject.toml`` calls :func:`sys.exit` with this return value.

    Argparse raises :class:`SystemExit` on ``--version`` (after printing
    via ``action="version"``), on unknown commands, and on missing
    required arguments. We catch that here and return its ``code`` so
    the contract is uniform whether the user typed ``--version`` or
    ``version``.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # ``code`` is ``int | str | None`` per the SystemExit signature;
        # argparse always passes an int. Defend against the str / None
        # cases so the function's ``-> int`` contract holds.
        code = exc.code
        if isinstance(code, int):
            return code
        if code is None:
            return 0
        return 1

    # No subcommand supplied — argparse does not exit on its own when
    # subparsers have ``dest="command"`` and no default. Print help to
    # stderr and exit 2 (argparse's standard for "missing argument").
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2

    func = getattr(args, "func", None)
    if func is None:  # pragma: no cover — defensive; every subcommand sets func
        parser.print_help(sys.stderr)
        return 2

    # US-007 / DEC-016 — install the no-traceback excepthook unless the
    # operator explicitly asked for ``--verbose``. ``--verbose`` is a
    # subcommand-level flag (not every subcommand has one); ``getattr``
    # with a ``False`` default makes the install path correct for the
    # ``version`` subcommand (which never raises and doesn't expose the
    # flag) AND for the ``generate`` subcommand (where the flag exists).
    if not getattr(args, "verbose", False):
        sys.excepthook = _safe_excepthook

    return func(args)
