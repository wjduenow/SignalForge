"""5-surface parity test for the issue #105 ``prune-existing`` flag set.

Mirrors :mod:`tests.cli.test_5_surface_parity_select` and codifies the
``cli-layer.md`` § "Multi-surface parity for behaviour changes" rule for
``signalforge prune-existing``. The distinctive flag set
(``--schema``, ``--scope``, ``--sample-strategy``) and the read-only /
no-``--write`` intent must appear consistently across:

1. **argparse help** — the ``prune-existing`` subparser's collected help
   text (description + every action's ``.help``). Source of truth lives
   in :func:`signalforge.cli.prune_existing.add_parser` (US-002 wired it).
2. **module help strings / docstrings** — ``prune_existing.py`` source
   (the ``cmd_prune_existing`` docstring + ``add_parser`` help strings).
3. **docs/cli-ops.md** — the ``signalforge prune-existing`` section
   (US-004 ships it).
4. **plans/super/105-prune-existing-cli.md** — the DEC list referencing
   the same flag names + key decisions.

The test reads bytes from each surface and asserts the shared tokens
appear in each. Bespoke per DEC-017 of #37 (the parity-test precedent);
future flags get their own parity test.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from signalforge.cli.prune_existing import add_parser

# ---------------------------------------------------------------------------
# Surface locations
# ---------------------------------------------------------------------------

# ``__file__`` is ``tests/cli/test_5_surface_parity_prune_existing.py``;
# the plan + ops doc + the prune_existing module live under the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PLAN_FILE = _REPO_ROOT / "plans" / "super" / "105-prune-existing-cli.md"
_OPS_DOC = _REPO_ROOT / "docs" / "cli-ops.md"
_MODULE_FILE = _REPO_ROOT / "src" / "signalforge" / "cli" / "prune_existing.py"

# The subcommand name + the flags that distinguish ``prune-existing`` from
# ``generate``. ``--schema`` is required; ``--scope`` / ``--sample-strategy``
# are the prune knobs that replace the (dropped) ``--mode`` flag.
_FLAG_TOKENS = (
    "prune-existing",
    "--schema",
    "--scope",
    "--sample-strategy",
    "--dry-run",
)

# Read-only intent token — the load-bearing DEC-003 decision that
# distinguishes this subcommand from ``generate``. Appears verbatim
# (case-insensitively) in every surface.
_READ_ONLY_TOKEN = "read-only"


def _mentions_no_write(text: str) -> bool:
    """True when ``text`` carries the no-``--write`` decision in either the
    backtick-fenced (``no `--write```) or plain (``no --write``) form —
    surfaces vary in Markdown vs. docstring rendering of the same intent.
    """
    return "no `--write`" in text or "no --write" in text


# Section anchor in the ops doc that US-004 ships.
_OPS_SECTION_SENTINEL = "signalforge prune-existing"


def _prune_existing_help_text() -> str:
    """Collect the ``prune-existing`` subparser's full help surface.

    Builds a throwaway top-level parser, registers the subcommand via
    :func:`add_parser`, then concatenates the subparser ``description``
    with every action's ``.help`` string. Mirrors how
    :mod:`tests.cli.test_5_surface_parity_select` introspects the parser
    (surface 1 of the 5-surface rule).
    """
    parser = argparse.ArgumentParser(prog="signalforge")
    subparsers = parser.add_subparsers(dest="command")
    add_parser(subparsers)
    # ``prune-existing`` is the registered subparser key (the literal
    # token a user types); include it so surface 1 carries the subcommand
    # name alongside its flag help.
    assert "prune-existing" in subparsers.choices, (
        "add_parser did not register the 'prune-existing' subcommand"
    )
    sub = subparsers.choices["prune-existing"]
    parts: list[str] = ["prune-existing", sub.description or ""]
    for action in sub._actions:
        if action.help:
            parts.append(action.help)
        parts.extend(action.option_strings)
    return "\n".join(parts)


def test_5_surface_parity_for_prune_existing_flags() -> None:
    """The flag set + read-only intent appear in every surface.

    Surfaces (all present in this repository after US-004):

    * 1 (argparse help) — the registered subparser's collected help.
    * 2 (module help strings / docstrings) — ``prune_existing.py``.
    * 3 (ops doc) — ``docs/cli-ops.md`` § ``signalforge prune-existing``.
    * 4 (plan DEC list) — ``plans/super/105-prune-existing-cli.md``.

    Catches multi-surface drift on the user-facing argv shape
    (``cli-layer.md`` § "Multi-surface parity for behaviour changes").
    """
    # Surface 1 — argparse help (the registered subparser).
    help_text = _prune_existing_help_text()
    for token in _FLAG_TOKENS:
        assert token in help_text, (
            f"prune-existing argparse help missing flag token {token!r}; got:\n{help_text}"
        )

    # Surface 2 — module source (add_parser help strings + cmd docstring).
    assert _MODULE_FILE.exists(), f"prune_existing.py not found at {_MODULE_FILE}"
    module_text = _MODULE_FILE.read_text(encoding="utf-8")
    for token in _FLAG_TOKENS:
        assert token in module_text, (
            f"prune_existing.py missing flag token {token!r} — parity break"
        )
    assert _READ_ONLY_TOKEN in module_text, (
        f"prune_existing.py missing read-only intent token {_READ_ONLY_TOKEN!r} — parity break"
    )
    assert _mentions_no_write(module_text), (
        "prune_existing.py missing the no-`--write` decision — parity break"
    )

    # Surface 3 — ops doc section.
    assert _OPS_DOC.exists(), f"docs/cli-ops.md not found at {_OPS_DOC}"
    ops_text = _OPS_DOC.read_text(encoding="utf-8")
    assert _OPS_SECTION_SENTINEL in ops_text, (
        f"docs/cli-ops.md missing the {_OPS_SECTION_SENTINEL!r} section — parity break"
    )
    for token in _FLAG_TOKENS:
        assert token in ops_text, f"docs/cli-ops.md missing flag token {token!r} — parity break"
    assert _READ_ONLY_TOKEN in ops_text, (
        f"docs/cli-ops.md missing read-only intent token {_READ_ONLY_TOKEN!r} — parity break"
    )
    assert _mentions_no_write(ops_text), (
        "docs/cli-ops.md missing the no-`--write` decision — parity break"
    )

    # Surface 4 — plan DEC list.
    assert _PLAN_FILE.exists(), f"plan file not found at {_PLAN_FILE}"
    plan_text = _PLAN_FILE.read_text(encoding="utf-8")
    for token in _FLAG_TOKENS:
        assert token in plan_text, f"plan file missing flag token {token!r} — parity break"
    # The plan's DEC-003 carries the no-``--write`` / read-only decision.
    assert _READ_ONLY_TOKEN in plan_text, (
        f"plan file missing read-only intent token {_READ_ONLY_TOKEN!r} — parity break"
    )
    assert _mentions_no_write(plan_text), (
        "plan file missing the no-`--write` decision — parity break"
    )
