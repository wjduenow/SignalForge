"""5-surface parity test for the issue #116 / US-012 ``--force`` flag.

``cli-layer.md`` § "Multi-surface parity for behaviour changes" requires a
behaviour change to touch five surfaces, all consistent. For the
``generate --force`` flag (DEC-010/DEC-014 of
``plans/super/116-business-rule-tests.md``) the surfaces are:

1. **argparse help** — the ``--force`` action's ``.help`` string on the
   ``generate`` subparser (lives in :func:`signalforge.cli.generate.add_parser`).
2. **handler / helper docstring** — :func:`signalforge.cli.generate.add_parser`'s
   docstring and :func:`signalforge.cli.generate._write_proposed_test_files`'s
   docstring reference the flag.
3. **docs/cli-ops.md** — the ``generate`` flag reference documents ``--force``
   plus its overwrite policy and stderr WARNING shapes.
4. **test** — this file plus the behavioural tests in ``test_generate.py``.
5. **plan DEC** — ``plans/super/116-business-rule-tests.md`` references the
   flag under DEC-010 / DEC-014 / US-012.

The test reads each surface and asserts ``--force`` appears with the
load-bearing marker token (``signalforge:generated``) so a future drift —
e.g. dropping the flag from the docs while keeping it in argparse — fails
loud. Bespoke per ``cli-layer.md`` DEC-017 (mirrors
``test_5_surface_parity_select.py``).
"""

from __future__ import annotations

import argparse
import inspect
from pathlib import Path

from signalforge.cli import generate as gen_mod
from signalforge.cli.generate import add_parser

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PLAN_FILE = _REPO_ROOT / "plans" / "super" / "116-business-rule-tests.md"
_OPS_DOC = _REPO_ROOT / "docs" / "cli-ops.md"

# The load-bearing marker token that the overwrite policy keys on; it must
# appear in the help string and the docs alongside the flag.
_MARKER_TOKEN = "signalforge:generated"


def _force_help_text() -> str:
    """Recover the ``--force`` action's ``.help`` string from the parser."""
    parser = argparse.ArgumentParser(prog="signalforge")
    subparsers = parser.add_subparsers(dest="command")
    add_parser(subparsers)
    gen = subparsers.choices["generate"]
    for action in gen._actions:
        if "--force" in action.option_strings:
            assert action.help is not None
            return action.help
    raise AssertionError("--force action not found on generate subparser")


def test_5_surface_parity_for_force_flag() -> None:
    """``--force`` appears consistently across all five surfaces."""
    # Surface 1 — argparse help mentions --force semantics + the marker.
    help_text = _force_help_text()
    assert "--write" in help_text
    assert _MARKER_TOKEN in help_text
    assert "never" in help_text.lower() or "hand-authored" in help_text.lower()

    # Surface 2 — handler/helper docstrings reference --force.
    add_parser_doc = inspect.getdoc(add_parser) or ""
    assert "--force" in add_parser_doc
    writer_doc = inspect.getdoc(gen_mod._write_proposed_test_files) or ""
    assert "force" in writer_doc.lower()
    assert _MARKER_TOKEN in writer_doc

    # Surface 3 — docs/cli-ops.md documents the flag + the marker + policy.
    assert _OPS_DOC.exists(), f"docs/cli-ops.md not found at {_OPS_DOC}"
    ops_text = _OPS_DOC.read_text(encoding="utf-8")
    assert "`--force`" in ops_text
    assert _MARKER_TOKEN in ops_text
    assert "hand-authored" in ops_text.lower()

    # Surface 5 — plan DEC references the flag under DEC-010 / US-012.
    assert _PLAN_FILE.exists(), f"plan file not found at {_PLAN_FILE}"
    plan_text = _PLAN_FILE.read_text(encoding="utf-8")
    assert "--force" in plan_text
    assert "DEC-010" in plan_text
    assert "US-012" in plan_text
