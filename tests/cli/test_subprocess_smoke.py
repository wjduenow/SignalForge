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
