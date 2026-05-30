"""5-surface parity test for the issue #141 / US-005 ``install-skill`` subcommand.

DEC-024 of ``plans/super/141-claude-skill-install.md`` plus the
``cli-layer.md`` 5-surface parity rule require that the
``install-skill`` subcommand name appears consistently across:

1. **argparse help** — the ``install-skill`` subparser's ``--help``
   output. Source of truth lives in
   :func:`signalforge.cli.install_skill.add_parser` (US-003 wired it).
2. **Handler docstring** — :mod:`signalforge.cli.install_skill`'s module
   docstring plus :func:`signalforge.cli.install_skill.cmd_install_skill`'s
   docstring. Both reference the subcommand by name.
3. **docs/cli-ops.md § Subcommands** — the ``signalforge install-skill``
   subsection ships with US-006.
4. **plans/super/141-claude-skill-install.md** — DEC-024 names the
   canonical token; the user-story section also names the subcommand.
5. **The test file itself** — implicitly satisfied (this file).

The test reads bytes from each external surface at runtime and asserts
the canonical token (``"install-skill"``) appears in each. Bespoke per
``cli-layer.md`` 5-surface parity rule — future flags get their own
parity test (or extend this one).

This test is **orthogonal** to
:mod:`tests.cli.test_skill_cli_parity` (US-004): that gate scans the
*full* CLI subparser registry against the bundled ``SKILL.md`` body;
this gate pins *one* subcommand across *five* surfaces.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import signalforge.cli.install_skill as install_skill_module
from signalforge.cli import main
from signalforge.cli.install_skill import add_parser, cmd_install_skill

# ---------------------------------------------------------------------------
# Surface locations
# ---------------------------------------------------------------------------

# The plan + ops doc live at the repository root; ``__file__`` is at
# ``tests/cli/test_5_surface_parity_install_skill.py``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PLAN_FILE = _REPO_ROOT / "plans" / "super" / "141-claude-skill-install.md"
_OPS_DOC = _REPO_ROOT / "docs" / "cli-ops.md"

# Canonical tokens the four external surfaces must all carry. Sourced
# from DEC-024. v0.1 has no flags on ``install-skill`` (DEC-003 — no
# ``--force``), so the subcommand name is the only token; a future flag
# extends this tuple in the same commit that adds the flag.
_CANONICAL_TOKENS = ("install-skill",)


def _install_skill_help_text() -> str:
    """Render the full ``signalforge install-skill --help`` output.

    Builds a fresh top-level parser and registers the subcommand via
    :func:`signalforge.cli.install_skill.add_parser`, then asks the
    subparser for its formatted help — mirrors
    :func:`tests.cli.test_5_surface_parity_init_demo._init_demo_help_text`
    so a reviewer sees the same shape across both parity tests.
    """
    parser = argparse.ArgumentParser(prog="signalforge")
    subparsers = parser.add_subparsers(dest="command")
    add_parser(subparsers)
    sub = subparsers.choices["install-skill"]
    return sub.format_help()


# ---------------------------------------------------------------------------
# Surface 1: argparse help
# ---------------------------------------------------------------------------


def test_install_skill_in_argparse_help() -> None:
    """Each canonical token appears in the rendered ``install-skill --help``
    output (surface 1 of 5).
    """
    help_text = _install_skill_help_text()
    for token in _CANONICAL_TOKENS:
        assert token in help_text, (
            f"install-skill --help missing canonical token {token!r}; got:\n{help_text}"
        )


def test_install_skill_help_via_main_entrypoint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end variant of surface 1: drive
    ``main(["install-skill", "--help"])`` so argparse's ``--help`` action
    prints to stdout.

    Belt-and-braces against a refactor that moves the subparser
    registration out of :func:`add_parser` — only the full ``main``
    dispatch path catches that drift. argparse's ``--help`` action
    raises :class:`SystemExit(0)`; :func:`signalforge.cli.main` catches
    it and returns ``0`` so the ``-> int`` contract holds (see
    ``.claude/rules/cli-layer.md`` § "No traceback ever leaks").
    """
    rc = main(["install-skill", "--help"])
    # argparse --help exits 0; main() returns it as an int.
    assert rc == 0
    captured = capsys.readouterr()
    for token in _CANONICAL_TOKENS:
        assert token in captured.out, (
            f"main(['install-skill', '--help']) stdout missing token "
            f"{token!r}; got:\n{captured.out}"
        )


# ---------------------------------------------------------------------------
# Surface 2: handler docstring
# ---------------------------------------------------------------------------


def test_install_skill_in_handler_docstring() -> None:
    """Each canonical token appears in either the module docstring or the
    handler docstring (surface 2 of 5).

    The handler ships three docstring surfaces — the module-level one
    (describing what ``install-skill`` does and how it differs from
    ``init-demo``'s path handling), the per-function one on
    :func:`cmd_install_skill`, and the registration docstring on
    :func:`add_parser`. The parity check accepts a hit in any of them so
    a future refactor that consolidates the prose into one surface
    doesn't break the contract.
    """
    module_doc = install_skill_module.__doc__ or ""
    handler_doc = cmd_install_skill.__doc__ or ""
    add_parser_doc = add_parser.__doc__ or ""
    combined = "\n".join((module_doc, handler_doc, add_parser_doc))
    for token in _CANONICAL_TOKENS:
        assert token in combined, (
            f"signalforge.cli.install_skill docstrings missing canonical "
            f"token {token!r}; got module:\n{module_doc}\n\n"
            f"handler:\n{handler_doc}\n\nadd_parser:\n{add_parser_doc}"
        )


# ---------------------------------------------------------------------------
# Surface 3: docs/cli-ops.md § Subcommands
# ---------------------------------------------------------------------------


def test_install_skill_in_cli_ops_doc() -> None:
    """Each canonical token appears in ``docs/cli-ops.md`` (surface 3 of 5).

    The check is intentionally whole-file rather than scoped to the
    ``install-skill`` subsection — restricting to the subsection would
    couple the test to the doc's heading structure (brittle on a
    refactor that splits or merges sections).
    """
    assert _OPS_DOC.exists(), f"docs/cli-ops.md not found at {_OPS_DOC}"
    ops_text = _OPS_DOC.read_text(encoding="utf-8")
    for token in _CANONICAL_TOKENS:
        assert token in ops_text, (
            f"docs/cli-ops.md missing canonical token {token!r} — "
            "5-surface parity break (US-006 ships this surface)"
        )


# ---------------------------------------------------------------------------
# Surface 4: plans/super/141-claude-skill-install.md DEC list
# ---------------------------------------------------------------------------


def test_install_skill_in_plan_dec_list() -> None:
    """Each canonical token appears in
    ``plans/super/141-claude-skill-install.md`` (surface 4 of 5).

    The plan's DEC-024 names the canonical token; the user-story
    section names the subcommand. Whole-file check rather than
    DEC-scoped for the same reason as surface 3 — the contract is
    "the tokens appear somewhere in the plan," not "in a specific
    section."
    """
    assert _PLAN_FILE.exists(), f"plan file not found at {_PLAN_FILE}"
    plan_text = _PLAN_FILE.read_text(encoding="utf-8")
    for token in _CANONICAL_TOKENS:
        assert token in plan_text, (
            f"plans/super/141-claude-skill-install.md missing canonical "
            f"token {token!r} — 5-surface parity break"
        )


# ---------------------------------------------------------------------------
# Aggregate parity summary
# ---------------------------------------------------------------------------


def test_install_skill_consistent_across_surfaces(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Aggregate check: every canonical token appears in every external
    surface (1, 2, 3, 4 — the 5th is this test file).

    This is the single test a reviewer reads first to verify the
    contract; the per-surface tests above pinpoint exactly which
    surface drifted on failure.
    """
    surfaces: dict[str, str] = {
        "argparse_help": _install_skill_help_text(),
        "handler_docstring": "\n".join(
            (
                install_skill_module.__doc__ or "",
                cmd_install_skill.__doc__ or "",
                add_parser.__doc__ or "",
            )
        ),
        "cli_ops_doc": _OPS_DOC.read_text(encoding="utf-8"),
        "plan_dec_list": _PLAN_FILE.read_text(encoding="utf-8"),
    }
    missing: list[tuple[str, str]] = []
    for surface_name, surface_text in surfaces.items():
        for token in _CANONICAL_TOKENS:
            if token not in surface_text:
                missing.append((surface_name, token))
    assert not missing, (
        f"5-surface parity break — canonical tokens missing from one or more surfaces: {missing!r}"
    )
    # Drain any stdout the help-rendering helpers produced (argparse's
    # ``--help`` action prints when exercised through ``main(...)``; here
    # we used ``format_help`` so no stdout, but capsys is part of the
    # signature for parity with the surface-1 test).
    capsys.readouterr()
