"""Subprocess-gated smoke for the ``signalforge`` console script.

US-009 — verifies ``pyproject [project.scripts]`` wiring + the
console-script wrapper that ``pip install -e ".[dev]"`` generates on
PATH. Caught by ``pytest -m cli_subprocess`` only; default runs skip
this test (mirrors the ``bigquery`` and ``anthropic`` integration-test
gates per ``warehouse-adapters.md`` / DEC-018).

Belt-and-braces against drift the in-process ``main(argv)`` smoke tests
in ``test_smoke.py`` cannot catch:

* ``[project.scripts]`` table getting deleted or typoed.
* A wheel rebuild changing the console-script wrapper shape.
* ``which signalforge`` returning nothing because the entry point
  failed to install.
"""

from __future__ import annotations

import subprocess

import pytest


@pytest.mark.cli_subprocess
def test_signalforge_version_via_subprocess() -> None:
    """``signalforge --version`` exits 0 with the PEP 440 stdout shape.

    The 10s timeout is conservative; ``--version`` should complete in
    well under one second. A timeout signals a far worse regression
    than a wrong exit code (e.g. an import-time deadlock) and we want
    pytest to fail loudly with the timeout traceback rather than hang
    the suite.
    """
    result = subprocess.run(
        ["signalforge", "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    # ``argparse`` writes the version string to stdout.
    assert result.stdout.startswith("signalforge ")
    # Stderr is allowed to carry library-level warnings (e.g. pydantic
    # field-shadow notices) but must NEVER carry a Python traceback —
    # ``signalforge.cli._helpers._safe_excepthook`` exists to prevent
    # exactly that. Mirrors the in-process smoke assertion in
    # ``tests/cli/test_smoke.py``.
    assert "Traceback" not in result.stderr


@pytest.mark.cli_subprocess
def test_signalforge_generate_help_via_subprocess() -> None:
    """``signalforge generate --help`` exits 0 with the subcommand's help.

    Issue #58 — extends the subprocess-gated smoke beyond ``--version`` to
    a subparser that the top-level parser does NOT expose. ``--version``
    only proves ``[project.scripts]`` wiring; a regression in
    ``signalforge.cli.generate.add_parser`` (subparser deletion, an import
    failure inside the module, an ``add_argument`` that raises during
    registration) leaves ``--version`` working but breaks every real
    invocation. Asserting ``generate --help`` exits 0 closes that gap.
    """
    result = subprocess.run(
        ["signalforge", "generate", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    # The presence of the subcommand name plus a ``generate``-specific
    # flag (``--mode`` is unique to this subparser; absent from
    # ``lint`` / ``init-demo`` / ``version``) jointly guarantees argparse
    # is rendering the right subcommand's help, not the top-level usage.
    assert "generate" in result.stdout
    assert "--mode" in result.stdout
    # No-traceback floor — see the ``--version`` test above.
    assert "Traceback" not in result.stderr


@pytest.mark.cli_subprocess
def test_signalforge_lint_help_via_subprocess() -> None:
    """``signalforge lint --help`` exits 0 with the subcommand's help.

    Issue #58 — paired with ``generate --help`` above. Same rationale:
    a subparser-registration regression in ``signalforge.cli.lint`` would
    pass the ``--version`` smoke but break the real subcommand. The
    ``--model`` flag is unique to ``lint`` (issue #49) and pins the
    subcommand identity in the rendered help.
    """
    result = subprocess.run(
        ["signalforge", "lint", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "lint" in result.stdout
    assert "--model" in result.stdout
    # No-traceback floor — see the ``--version`` test above.
    assert "Traceback" not in result.stderr


@pytest.mark.cli_subprocess
def test_signalforge_version_help_via_subprocess() -> None:
    """``signalforge version --help`` exits 0 with the subcommand's help.

    Issue #58 — the existing ``--version`` test (top-level
    ``action="version"``) does NOT exercise ``signalforge.cli.version``'s
    ``add_parser`` registration; the ``version`` subparser could be
    deleted or break at import time without that test noticing. The
    subparser carries no flags of its own (``cmd_version`` takes no
    args) and ``add_parser(help=...)`` strings only render on the parent
    parser, so the assertion pins the argparse-emitted usage line
    ``usage: signalforge version`` — the dispatched-program name plus
    the subcommand name jointly prove argparse rendered the right
    subparser's help, not the top-level usage.
    """
    result = subprocess.run(
        ["signalforge", "version", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "usage: signalforge version" in result.stdout
    # No-traceback floor — see the ``--version`` test above.
    assert "Traceback" not in result.stderr


@pytest.mark.cli_subprocess
def test_signalforge_init_demo_help_via_subprocess() -> None:
    """``signalforge init-demo --help`` exits 0 with the subcommand's help.

    US-006 of ``plans/super/47-init-demo.md`` (DEC-010 / AC-5) — extends
    the subprocess-gated smoke to the new ``init-demo`` subcommand so a
    ``[project.scripts]`` regression specific to its argparse wiring
    (subparser deletion, ``add_parser`` typo, console-script wrapper
    losing the dispatch entry) is caught by ``pytest -m cli_subprocess``.
    The in-process ``main(argv)`` smoke tests in ``tests/cli/`` cannot
    catch this class of regression — they bypass the
    ``[project.scripts]`` table entirely.
    """
    result = subprocess.run(
        ["signalforge", "init-demo", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    # The presence of the subcommand name, the ``--force`` flag, and the
    # ``DEST`` positional (rendered in argparse's help as the metavar
    # ``DEST`` AND inside the description prose as ``dest``) jointly
    # guarantee argparse is rendering the new subcommand's help, not
    # falling back to top-level usage.
    assert "init-demo" in result.stdout
    assert "--force" in result.stdout
    assert "dest" in result.stdout.lower()
    # No-traceback floor — see the ``--version`` test above.
    assert "Traceback" not in result.stderr


@pytest.mark.cli_subprocess
def test_signalforge_prune_existing_help_via_subprocess() -> None:
    """``signalforge prune-existing --help`` exits 0 with the subcommand's help.

    US-005 of ``plans/super/105-prune-existing-cli.md`` (#105) — extends
    the subprocess-gated smoke to the new ``prune-existing`` subcommand so a
    ``[project.scripts]`` regression specific to its argparse wiring
    (subparser deletion, ``add_parser`` typo, console-script wrapper losing
    the dispatch entry) is caught by ``pytest -m cli_subprocess``. The
    in-process ``main(argv)`` smoke tests in ``tests/cli/`` cannot catch this
    class of regression — they bypass the ``[project.scripts]`` table
    entirely.
    """
    result = subprocess.run(
        ["signalforge", "prune-existing", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    # The presence of the subcommand name plus the ``--schema`` flag (unique
    # to ``prune-existing``; absent from ``generate`` / ``lint`` /
    # ``init-demo`` / ``version``) jointly guarantees argparse is rendering
    # the right subcommand's help, not the top-level usage.
    assert "prune-existing" in result.stdout
    assert "--schema" in result.stdout
    # No-traceback floor — see the ``--version`` test above.
    assert "Traceback" not in result.stderr
