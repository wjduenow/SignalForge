"""Smoke tests for the CLI scaffold (US-003).

Exercises the four invocation shapes the v0.1 contract guarantees:

* ``signalforge --version`` — top-level argparse flag.
* ``signalforge --help`` — top-level help.
* ``signalforge version`` — subcommand sharing one source of truth with
  the flag.
* No traceback in stderr for any of the above.

All tests use in-process :func:`signalforge.cli.main` + ``capsys``. The
gated subprocess equivalent (``which signalforge`` / console-script
wiring) lands in US-009.
"""

from __future__ import annotations

import re

import pytest

import signalforge
from signalforge.cli import main


def _capture(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_version_flag_prints_pep440_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge --version`` returns 0 and prints the PEP 440 shape.

    Argparse's ``action="version"`` raises :class:`SystemExit(0)` after
    printing; :func:`main` catches that and returns its code so
    in-process callers see an int rather than catching the exit.
    """
    code = main(["--version"])
    out, err = _capture(capsys)

    assert code == 0
    # argparse writes the version string to stdout (Python 3.4+);
    # ensure both the PEP 440 shape and the project name appear.
    assert out.startswith("signalforge ")
    assert signalforge.__version__ in out
    # PEP 440 dev-release shape: ``MAJOR.MINOR.PATCH.devN`` is what
    # ``__version__`` currently carries; the assertion is loose enough
    # to survive a bump to ``0.1.0`` without churn.
    assert re.match(r"^signalforge \d+\.\d+\.\d+", out)
    assert "Traceback" not in err


def test_help_flag_prints_top_level_help_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge --help`` returns 0 and prints help.

    Argparse's ``--help`` raises :class:`SystemExit(0)`; :func:`main`
    catches and returns 0.
    """
    code = main(["--help"])
    out, err = _capture(capsys)

    assert code == 0
    # Help is on stdout (argparse's default for ``--help``).
    assert "signalforge" in out
    # The subcommand list must mention the version subcommand we
    # registered (catches a regression where ``add_parser`` wasn't
    # called).
    assert "version" in out
    assert "Traceback" not in err


def test_version_subcommand_prints_same_string_as_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge version`` returns 0 and prints the same string as
    ``signalforge --version``.

    Both surfaces share one implementation (the ``__version__``
    constant); this test pins the contract that they cannot drift.
    """
    code = main(["version"])
    out, err = _capture(capsys)

    assert code == 0
    assert out.strip() == f"signalforge {signalforge.__version__}"
    assert "Traceback" not in err


def test_version_flag_and_subcommand_produce_same_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Belt-and-braces: round-trip ``--version`` and ``version``
    through :func:`main` back-to-back and assert byte-equal stdout.
    """
    code_flag = main(["--version"])
    out_flag, _ = _capture(capsys)
    code_sub = main(["version"])
    out_sub, _ = _capture(capsys)

    assert code_flag == 0
    assert code_sub == 0
    assert out_flag.strip() == out_sub.strip()


def test_no_traceback_in_stderr_on_any_smoke_invocation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Belt-and-braces for DEC-016: no Python traceback ever leaks on
    the smoke invocations. A traceback in stderr is exactly the
    failure mode :func:`signalforge.cli._helpers._safe_excepthook`
    exists to prevent.
    """
    for argv in (["--version"], ["--help"], ["version"]):
        main(argv)
        _, err = _capture(capsys)
        assert "Traceback" not in err, f"Traceback leaked to stderr on argv={argv!r}: {err!r}"
