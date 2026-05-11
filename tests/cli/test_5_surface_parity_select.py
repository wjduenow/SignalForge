"""5-surface parity test for the issue #37 / US-007 ``--select`` flag.

This bead's DEC-017 codifies the ``cli-layer.md`` 5-surface parity rule
mechanically for ``--select``. The example selectors (``tag:staging``,
``path:models/marts/*``, ``tag:staging,path:models/marts/*``) must
appear consistently across:

1. **argparse help** — the ``--select`` action's ``.help`` string on the
   ``generate`` subparser. Source of truth lives in
   :func:`signalforge.cli.generate.add_parser` (US-004 wired it).
2. **docs/cli-ops.md** — the ``## Running across many models`` cookbook
   section. **US-008 lands this surface in a separate PR** running in
   parallel; until that PR merges, the section is absent. We
   ``pytest.skipif`` on a sentinel-string check so this test does NOT
   spuriously fail on a merge race. Once US-008 ships the section, the
   skip lifts automatically and the parity contract becomes active.
3. **plans/super/37-multi-model-select.md** — the DEC-001 / DEC-016
   grammar block plus US-007's TDD bullets reference the same atoms.

The test reads bytes from each surface present at runtime and asserts
the three example atoms appear in each. Bespoke per DEC-017 — future
flags get their own parity test (or extend this one).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from signalforge.cli.generate import add_parser

# ---------------------------------------------------------------------------
# Surface locations
# ---------------------------------------------------------------------------

# The plan + ops doc live at the repository root; ``__file__`` is at
# ``tests/cli/test_5_surface_parity_select.py``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PLAN_FILE = _REPO_ROOT / "plans" / "super" / "37-multi-model-select.md"
_OPS_DOC = _REPO_ROOT / "docs" / "cli-ops.md"

# The cookbook section header that US-008 will ship. Used as the sentinel
# that gates the optional surface-2 check below.
_COOKBOOK_SENTINEL = "Running across many models"

# Three example selectors the plan + help string both pin. Sourced from
# DEC-001 (grammar) and DEC-016 (fnmatch path semantics).
_EXAMPLE_SELECTORS = (
    "tag:staging",
    "path:models/marts/*",
    "tag:staging,path:models/marts/*",
)


def _select_help_text() -> str:
    """Recover the ``--select`` action's ``.help`` string by walking
    :class:`argparse._SubParsersAction` after :func:`add_parser` populates
    it. Mirrors how ``cli-layer.md`` recommends introspecting the parser
    surface (DEC-017 of #37 — surface 1 of 5).
    """
    parser = argparse.ArgumentParser(prog="signalforge")
    subparsers = parser.add_subparsers(dest="command")
    add_parser(subparsers)
    # Walk every action on the ``generate`` subparser looking for ``--select``.
    gen = subparsers.choices["generate"]
    for action in gen._actions:
        if "--select" in action.option_strings:
            assert action.help is not None
            return action.help
    raise AssertionError("--select action not found on generate subparser")


# ---------------------------------------------------------------------------
# Surface 1: argparse help — always present after US-004
# ---------------------------------------------------------------------------


def test_5_surface_parity_for_select_flag() -> None:
    """Each example selector appears in every surface present at runtime.

    Surfaces:

    * 1 (argparse help) — always present after US-004.
    * 3 (plan DEC bullets) — present in this repository; required.
    * 2 (cookbook) — optional; ``pytest.skipif`` when US-008 hasn't merged.

    The check is bespoke per DEC-017; the failure mode it catches is
    multi-surface drift on user-facing argv shapes (``testing-signal.md``
    end-to-end gated-tests section).
    """
    # Surface 1 — argparse help.
    help_text = _select_help_text()
    for selector in _EXAMPLE_SELECTORS:
        assert selector in help_text, (
            f"--select help string missing example selector {selector!r}; got:\n{help_text}"
        )

    # Surface 3 — plan file (DEC-001, DEC-016, US-007 bullets all
    # reference the same atoms). Required.
    assert _PLAN_FILE.exists(), f"plan file not found at {_PLAN_FILE}"
    plan_text = _PLAN_FILE.read_text(encoding="utf-8")
    for selector in _EXAMPLE_SELECTORS:
        assert selector in plan_text, (
            f"plan file missing example selector {selector!r} — DEC-017 parity break"
        )

    # Surface 2 — cookbook section. US-008 has merged; this is a hard
    # contract. Missing file or missing sentinel is a parity break.
    assert _OPS_DOC.exists(), f"docs/cli-ops.md not found at {_OPS_DOC}"
    ops_text = _OPS_DOC.read_text(encoding="utf-8")
    assert _COOKBOOK_SENTINEL in ops_text, (
        f"docs/cli-ops.md missing the {_COOKBOOK_SENTINEL!r} cookbook section "
        "— DEC-017 parity break (US-008 ships this section)"
    )
    for selector in _EXAMPLE_SELECTORS:
        assert selector in ops_text, (
            f"docs/cli-ops.md cookbook missing example selector {selector!r} — DEC-017 parity break"
        )
