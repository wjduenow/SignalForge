"""``signalforge version`` subcommand (US-003).

Prints the same string as the top-level ``--version`` flag. Both
surfaces share one source of truth (the ``__version__`` constant in
:mod:`signalforge.__init__`); the flag uses argparse's
``action="version"`` while this subcommand calls :func:`cmd_version`
directly.
"""

from __future__ import annotations

import argparse

import signalforge


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``version`` subcommand on the top-level parser."""
    parser = subparsers.add_parser(
        "version",
        help="Print the SignalForge version and exit.",
    )
    parser.set_defaults(func=cmd_version)


def cmd_version(args: argparse.Namespace) -> int:
    """Print ``signalforge <version>`` to stdout and return 0."""
    del args  # unused; the subcommand has no flags
    print(f"signalforge {signalforge.__version__}")
    return 0
