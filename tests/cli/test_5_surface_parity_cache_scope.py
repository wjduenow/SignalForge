"""5-surface parity test for the issue #188 / US-007 ``--cache-scope`` flag.

Issue #188's DEC-003 codifies the ``cli-layer.md`` 5-surface parity rule
mechanically for ``--cache-scope``. The example tokens (``--cache-scope``,
``per-model``, ``project``) must appear consistently across:

1. **argparse help** — the ``--cache-scope`` action's ``.help`` string on
   the ``generate`` subparser. Source of truth lives in
   :func:`signalforge.cli.generate.add_parser` (US-005 wired it).
2. **docs/cli-ops.md** — the ``--cache-scope`` flag reference entry under
   ``signalforge generate`` plus the corrected Anthropic prompt-cache
   caveat in the ``## Running across many models`` section (US-007 / this
   bead lands this surface).
3. **plans/super/188-bulk-cache-prefix.md** — DEC-002 / DEC-003 plus the
   US-005 / US-007 TDD bullets reference the same tokens.

The test reads bytes from each surface and asserts the three example
tokens appear in each. Hard asserts — every surface exists by the time
this bead ships (no ``pytest.skip`` placeholders). Bespoke per DEC-003;
future flags get their own parity test. Mirrors
``tests/cli/test_5_surface_parity_select.py``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from signalforge.cli.generate import add_parser

# ---------------------------------------------------------------------------
# Surface locations
# ---------------------------------------------------------------------------

# The plan + ops doc live at the repository root; ``__file__`` is at
# ``tests/cli/test_5_surface_parity_cache_scope.py``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PLAN_FILE = _REPO_ROOT / "plans" / "super" / "188-bulk-cache-prefix.md"
_OPS_DOC = _REPO_ROOT / "docs" / "cli-ops.md"

# The cookbook section header where the corrected cache caveat lives. Used
# as a sentinel to assert the multi-model section is present.
_COOKBOOK_SENTINEL = "Running across many models"

# Three example tokens the plan + help string + docs all pin. Sourced from
# DEC-002 (auto-promote) and DEC-003 (the flag grammar).
_EXAMPLE_TOKENS = (
    "--cache-scope",
    "per-model",
    "project",
)


def _cache_scope_help_text() -> str:
    """Recover the ``--cache-scope`` action's ``.help`` string by walking
    :class:`argparse._SubParsersAction` after :func:`add_parser` populates
    it. Mirrors how ``cli-layer.md`` recommends introspecting the parser
    surface (DEC-003 of #188 — surface 1 of 5).
    """
    parser = argparse.ArgumentParser(prog="signalforge")
    subparsers = parser.add_subparsers(dest="command")
    add_parser(subparsers)
    gen = subparsers.choices["generate"]
    for action in gen._actions:
        if "--cache-scope" in action.option_strings:
            assert action.help is not None
            return action.help
    raise AssertionError("--cache-scope action not found on generate subparser")


def test_5_surface_parity_for_cache_scope_flag() -> None:
    """Each example token appears in every parity surface.

    Surfaces (all present by the time this bead ships — hard asserts):

    * 1 (argparse help) — wired by US-005.
    * 2 (docs/cli-ops.md) — flag reference + corrected cache caveat
      (US-007 / this bead).
    * 3 (plan DEC bullets) — DEC-002 / DEC-003 + US-005/US-007 bullets.

    The check is bespoke per DEC-003; the failure mode it catches is
    multi-surface drift on user-facing argv shapes (``cli-layer.md`` §
    Multi-surface parity).
    """
    # Surface 1 — argparse help.
    help_text = _cache_scope_help_text()
    for token in _EXAMPLE_TOKENS:
        assert token in help_text, (
            f"--cache-scope help string missing example token {token!r}; got:\n{help_text}"
        )

    # Surface 2 — docs/cli-ops.md flag reference + multi-model cache caveat.
    assert _OPS_DOC.exists(), f"docs/cli-ops.md not found at {_OPS_DOC}"
    ops_text = _OPS_DOC.read_text(encoding="utf-8")
    assert _COOKBOOK_SENTINEL in ops_text, (
        f"docs/cli-ops.md missing the {_COOKBOOK_SENTINEL!r} cookbook section "
        "— DEC-003 parity break (US-007 ships the corrected cache caveat here)"
    )
    for token in _EXAMPLE_TOKENS:
        assert token in ops_text, (
            f"docs/cli-ops.md missing example token {token!r} — DEC-003 parity break"
        )

    # Surface 3 — plan file (DEC-002, DEC-003, US-005/US-007 bullets all
    # reference the same tokens).
    assert _PLAN_FILE.exists(), f"plan file not found at {_PLAN_FILE}"
    plan_text = _PLAN_FILE.read_text(encoding="utf-8")
    for token in _EXAMPLE_TOKENS:
        assert token in plan_text, (
            f"plan file missing example token {token!r} — DEC-003 parity break"
        )
