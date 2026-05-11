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


# ---------------------------------------------------------------------------
# canonicalise_user_path — symlink-hardened path-safety helper (issue #43)
# ---------------------------------------------------------------------------


def test_canonicalise_user_path_returns_none_for_none_input(tmp_path):
    """``None`` input passes through so callers can plumb optional flags
    without a per-call ``if`` ladder.
    """
    from signalforge.cli._helpers import canonicalise_user_path

    assert canonicalise_user_path(None, tmp_path) is None


def test_canonicalise_user_path_rejects_path_outside_project(tmp_path):
    """A path escaping ``project_dir`` raises :class:`CliPathError`
    (the cross-package consumer's typed-error wrap of the layer-neutral
    :class:`PathContainmentError` from
    :mod:`signalforge._common.path_safety`).
    """
    from signalforge.cli._helpers import canonicalise_user_path
    from signalforge.cli.errors import CliPathError

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    with pytest.raises(CliPathError) as excinfo:
        canonicalise_user_path(str(outside), project_dir)

    assert "failed safety check" in str(excinfo.value)
