"""Top-level dispatch tests for :func:`signalforge.cli.main` (US-003).

Covers the two argparse boundary conditions the scaffold has to handle
explicitly:

* No subcommand → exit 2 with help on stderr (argparse's standard for
  "missing argument").
* Unknown subcommand → exit 2 (argparse's standard for unrecognised
  args).

Every assertion uses in-process :func:`main` + ``capsys`` per the
testing-signal convention in this project.
"""

from __future__ import annotations

import pytest

from signalforge.cli import main


def test_no_args_prints_help_to_stderr_and_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge`` (no args) → exit 2.

    Argparse with ``dest="command"`` does not exit on its own when no
    subparser is selected; :func:`main` prints help to stderr and
    returns 2 to mirror argparse's "missing argument" semantics.
    """
    code = main([])
    captured = capsys.readouterr()

    assert code == 2
    # Help should mention the program name + at least one subcommand.
    # argparse routes ``print_help`` to whichever stream we passed (we
    # passed stderr); the body should be non-trivial.
    assert "signalforge" in captured.err
    assert "Traceback" not in captured.err


def test_unknown_command_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    """``signalforge nonexistent`` → exit 2 (argparse rejects).

    Argparse raises :class:`SystemExit(2)` on an unrecognised choice;
    :func:`main` catches that and returns the int code so the
    in-process contract holds.
    """
    code = main(["nonexistent"])
    captured = capsys.readouterr()

    assert code == 2
    # argparse's error message is on stderr; assert no traceback.
    assert "Traceback" not in captured.err


def test_unknown_command_no_traceback_in_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Belt-and-braces for DEC-016: even on the argparse error path,
    no Python traceback should leak to stderr.
    """
    main(["definitely-not-a-real-command"])
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
