"""Tests for ``signalforge prune-existing`` parser surface (US-002 — issue #105).

US-002 is the **contract surface** story: it ships :func:`add_parser`
(the full flag set) and a **stub** :func:`cmd_prune_existing` returning
``0``. The full ingest -> prune -> diff orchestrator lands in US-003, so
these tests cover only the parser shape and dispatch:

* the subcommand is registered in ``_build_parser`` and dispatches to the
  stub handler;
* ``--schema`` is required (argparse exits 2 if omitted);
* ``--scope`` / ``--sample-strategy`` / ``--format`` reject typos via
  ``choices`` (exit 2);
* the dropped flags (``--mode`` / ``--write`` / ``--min-score`` /
  ``--select`` / ``--estimate``) are NOT present;
* ``--help`` exits 0 and lists every flag.
"""

from __future__ import annotations

import pytest

from signalforge.cli import main
from signalforge.cli import prune_existing as prune_existing_cmd
from signalforge.cli.prune_existing import cmd_prune_existing

# ---------------------------------------------------------------------------
# Registration + dispatch
# ---------------------------------------------------------------------------


def test_subcommand_registered_in_build_parser() -> None:
    """``prune-existing`` is a registered subcommand of the top-level parser."""
    from signalforge.cli import _build_parser

    parser = _build_parser()
    # Find the subparsers action and assert ``prune-existing`` is a choice.
    choices: set[str] = set()
    for action in parser._actions:  # noqa: SLF001 — argparse introspection in test
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            choices.update(action.choices)
    assert "prune-existing" in choices


def test_parser_sets_func_to_stub_handler() -> None:
    """Parsing a ``prune-existing`` invocation wires ``args.func`` to the stub."""
    from signalforge.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["prune-existing", "customers", "--schema", "schema.yml"])
    assert args.func is cmd_prune_existing


def test_main_dispatches_to_stub_returning_0() -> None:
    """``main(["prune-existing", ...])`` reaches the stub handler -> exit 0."""
    exit_code = main(["prune-existing", "customers", "--schema", "schema.yml"])
    assert exit_code == 0


def test_stub_handler_returns_0_directly() -> None:
    """The stub :func:`cmd_prune_existing` returns 0 regardless of args."""
    from argparse import Namespace

    assert cmd_prune_existing(Namespace()) == 0


def test_module_all_exports() -> None:
    """``__all__`` exports exactly ``add_parser`` and ``cmd_prune_existing``."""
    assert set(prune_existing_cmd.__all__) == {"add_parser", "cmd_prune_existing"}


# ---------------------------------------------------------------------------
# Required --schema
# ---------------------------------------------------------------------------


def test_schema_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    """Omitting ``--schema`` is an argparse usage error -> exit 2."""
    exit_code = main(["prune-existing", "customers"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--schema" in err


def test_schema_value_accepted() -> None:
    """``--schema <path>`` parses cleanly when supplied."""
    from signalforge.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["prune-existing", "customers", "--schema", "my_schema.yml"])
    assert args.schema == "my_schema.yml"


# ---------------------------------------------------------------------------
# choices rejection on the three choice flags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "good", "bad"),
    [
        ("--scope", "full", "bogus"),
        ("--sample-strategy", "oneshot", "bogus"),
        ("--format", "json", "bogus"),
    ],
)
def test_choice_flags_accept_valid_and_reject_invalid(
    flag: str,
    good: str,
    bad: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each choice flag accepts a valid value and rejects a typo via exit 2."""
    base = ["prune-existing", "customers", "--schema", "schema.yml"]

    # Valid value -> reaches stub -> exit 0.
    assert main([*base, flag, good]) == 0

    # Invalid value -> argparse usage error -> exit 2.
    capsys.readouterr()  # drain any prior output
    assert main([*base, flag, bad]) == 2


@pytest.mark.parametrize(
    ("dest", "default"),
    [
        ("scope", None),
        ("sample_strategy", None),
        ("format", "ansi"),
        ("dry_run", False),
        ("quiet", False),
        ("verbose", False),
        ("no_color", False),
    ],
)
def test_flag_defaults(dest: str, default: object) -> None:
    """Sentinel / boolean defaults match the documented contract."""
    from signalforge.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["prune-existing", "customers", "--schema", "schema.yml"])
    assert getattr(args, dest) == default


# ---------------------------------------------------------------------------
# Dropped flags must NOT be present (DEC-002 / DEC-003)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dropped", ["--mode", "--write", "--min-score", "--select", "--estimate"])
def test_dropped_flags_absent(dropped: str, capsys: pytest.CaptureFixture[str]) -> None:
    """The flags dropped vs. ``generate`` are rejected as unknown -> exit 2."""
    exit_code = main(["prune-existing", "customers", "--schema", "schema.yml", dropped])
    assert exit_code == 2


# ---------------------------------------------------------------------------
# --quiet / --verbose mutex
# ---------------------------------------------------------------------------


def test_quiet_verbose_mutually_exclusive() -> None:
    """Supplying both ``--quiet`` and ``--verbose`` is a usage error -> exit 2."""
    base = ["prune-existing", "customers", "--schema", "schema.yml"]
    assert main([*base, "--quiet", "--verbose"]) == 2


# ---------------------------------------------------------------------------
# --help lists every flag (surface 1 of 5-surface parity)
# ---------------------------------------------------------------------------


def test_help_lists_every_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """``prune-existing --help`` exits 0 and names every flag in the set."""
    exit_code = main(["prune-existing", "--help"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Traceback" not in out
    for token in (
        "<model>",
        "--schema",
        "--project-dir",
        "--manifest",
        "--profiles-dir",
        "--scope",
        "--sample-strategy",
        "--format",
        "--dry-run",
        "--quiet",
        "--verbose",
        "--no-color",
    ):
        assert token in out, f"{token!r} missing from --help output"
