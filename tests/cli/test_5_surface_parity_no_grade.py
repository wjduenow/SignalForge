"""5-surface parity test for issue #189 — ``--no-grade``, ``--no-cache``, ``cache clear --grade``.

Issue #189 ships three new operator-facing surface tokens on the
``signalforge`` CLI: the ``--no-grade`` flag on ``signalforge
generate`` (DEC-001), the ``--no-cache`` flag on ``signalforge
generate`` (DEC-002), and the new top-level subcommand
``signalforge cache clear --grade`` (DEC-015). Every operator-facing
behaviour change touches the five-surface parity contract from
``cli-layer.md`` § "Multi-surface parity for behaviour changes" plus
``skill-parity.md`` (the bundled Claude Code skill is the sixth
surface, gated separately by
:mod:`tests.cli.test_skill_cli_parity`). This module is the bespoke
parity gate for #189 — DEC-019 mandates it and locks the literal
tokens that must appear verbatim across the surfaces.

The three literal tokens pinned by this module
==============================================

This module's own ``__doc__`` is one of the five surfaces (surface
4 — "test docstring", per the cli-layer.md parity rule). The
parity tests below substring-match against THIS docstring's bytes,
so the tokens must appear verbatim here in the prose:

* ``--no-grade``
* ``--no-cache``
* ``cache clear --grade``

Substring match, no whitespace / case normalisation — mirrors the
"boring substring match" philosophy of
:mod:`tests.cli.test_5_surface_parity_select` and the
envelope-breach guard in :mod:`signalforge.draft.prompts`
(``business-rule-tests.md`` § "Numbered envelope + parser cardinality
gate").

Surface inventory
=================

For ``--no-grade`` and ``--no-cache`` the five surfaces are:

1. **Argparse help** — the ``--no-grade`` / ``--no-cache``
   actions' ``.help`` strings on the ``generate`` subparser. Sourced
   by walking
   :func:`signalforge.cli.generate.add_parser`'s registered actions.
2. **Plan file** — ``plans/super/189-no-grade-cache.md`` carries
   DEC-001 and DEC-002 which lock the grammar.
3. **CLI ops doc** — ``docs/cli-ops.md`` ships cookbook sections for
   each flag.
4. **Test docstring** — THIS module's ``__doc__``. Substring-matched
   against this very text.
5. **Bundled skill** — ``src/signalforge/skills/signalforge/SKILL.md``
   carries an operator-facing paragraph naming the flag.

For ``cache clear --grade`` the surfaces are FOUR:

1. **Argparse help** — the ``clear`` sub-action's ``--grade`` action
   on the ``cache`` subparser.
2. **Plan file** — ``plans/super/189-no-grade-cache.md`` carries
   DEC-015 which locks the subcommand grammar.
3. **CLI ops doc** — cookbook section in ``docs/cli-ops.md``.
4. **Test docstring** — THIS module's ``__doc__``.
5. **Bundled skill** — ``SKILL.md`` carries the operator-facing
   paragraph naming the subcommand.

(The plan file IS one of the surfaces for ``cache clear --grade`` —
DEC-015 names the literal token, so the count is 5, not the
conservative 4 mentioned in the US-009 spec. The test below
verifies presence in every surface that DOES carry the literal
token; if the plan file ever drops the literal, the test would
need to be re-scoped, but today it asserts 5.)

The parity gate is bespoke per DEC-017 of #37 / DEC-019 of #189 —
future flags get their own parity test or extend this one.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from signalforge.cli.cache import add_parser as cache_add_parser
from signalforge.cli.generate import add_parser as generate_add_parser

# ---------------------------------------------------------------------------
# Surface locations
# ---------------------------------------------------------------------------

# ``__file__`` is at ``tests/cli/test_5_surface_parity_no_grade.py``; the
# repo root is two directories up.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PLAN_FILE = _REPO_ROOT / "plans" / "super" / "189-no-grade-cache.md"
_CLI_OPS_DOC = _REPO_ROOT / "docs" / "cli-ops.md"
_GRADE_OPS_DOC = _REPO_ROOT / "docs" / "grade-ops.md"
_SKILL_MD = _REPO_ROOT / "src" / "signalforge" / "skills" / "signalforge" / "SKILL.md"


def _generate_help_blob() -> str:
    """Return a concatenation of every action's option strings AND
    ``.help`` string on the ``signalforge generate`` subparser.

    Walks the parser registered by
    :func:`signalforge.cli.generate.add_parser` rather than capturing
    ``--help`` text via subprocess so the test stays in-process and
    survives changes to argparse's formatter. The option strings
    (e.g. ``--no-grade``) are included so the literal flag token
    counts as "present in the argparse surface" even when the help
    prose does not happen to repeat the flag's own name.
    """
    parser = argparse.ArgumentParser(prog="signalforge")
    subparsers = parser.add_subparsers(dest="command")
    generate_add_parser(subparsers)
    gen = subparsers.choices["generate"]
    parts: list[str] = []
    for action in gen._actions:
        for opt in action.option_strings:
            parts.append(opt)
        if action.help is not None:
            parts.append(action.help)
    return "\n".join(parts)


def _cache_help_blob() -> str:
    """Return a concatenation of every help string under the
    ``signalforge cache`` subparser tree (top-level + nested
    ``clear`` sub-action + every action on each).

    The ``--grade`` flag lives on the nested ``cache clear``
    sub-action; the literal token ``cache clear --grade`` appears
    verbatim in the description and help blobs that
    :func:`signalforge.cli.cache.add_parser` wires up.
    """
    parser = argparse.ArgumentParser(prog="signalforge")
    subparsers = parser.add_subparsers(dest="command")
    cache_add_parser(subparsers)
    cache_parser = subparsers.choices["cache"]
    parts: list[str] = []
    if cache_parser.description is not None:
        parts.append(cache_parser.description)
    for action in cache_parser._actions:
        if action.help is not None:
            parts.append(action.help)
        # The nested subparsers carry their own choices.
        if isinstance(action, argparse._SubParsersAction):
            for sub_name, sub_parser in action.choices.items():
                if sub_parser.description is not None:
                    parts.append(sub_parser.description)
                for sub_action in sub_parser._actions:
                    if sub_action.help is not None:
                        parts.append(sub_action.help)
                # Record the sub-action name itself so a literal like
                # ``cache clear --grade`` can be assembled by the
                # reader if needed.
                parts.append(f"cache {sub_name}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_grade_token_appears_in_all_5_surfaces() -> None:
    """The literal ``--no-grade`` token appears in all five DEC-019 surfaces.

    Surfaces:

    1. argparse help on the ``generate`` subparser
    2. plan file (``plans/super/189-no-grade-cache.md``)
    3. ``docs/cli-ops.md``
    4. this module's ``__doc__``
    5. ``src/signalforge/skills/signalforge/SKILL.md``

    Boring substring match per the "no whitespace / case
    normalisation" rule from
    :mod:`tests.cli.test_5_surface_parity_select` and the
    envelope-breach guard pattern.
    """
    token = "--no-grade"

    # Surface 1 — argparse help.
    help_text = _generate_help_blob()
    assert token in help_text, (
        f"argparse help for `signalforge generate` missing literal {token!r} — "
        f"DEC-019 parity break (surface 1)"
    )

    # Surface 2 — plan file (DEC-001 of #189).
    assert _PLAN_FILE.exists(), f"plan file not found at {_PLAN_FILE}"
    plan_text = _PLAN_FILE.read_text(encoding="utf-8")
    assert token in plan_text, (
        f"plan file missing literal {token!r} — DEC-019 parity break (surface 2)"
    )

    # Surface 3 — CLI ops doc.
    assert _CLI_OPS_DOC.exists(), f"cli-ops.md not found at {_CLI_OPS_DOC}"
    cli_ops_text = _CLI_OPS_DOC.read_text(encoding="utf-8")
    assert token in cli_ops_text, (
        f"docs/cli-ops.md missing literal {token!r} — DEC-019 parity break (surface 3)"
    )

    # Surface 4 — this module's docstring. The substring must appear
    # in the prose above (the test reads ``__doc__`` rather than the
    # file bytes so a future split into multiple test functions does
    # not silently drop the surface).
    assert __doc__ is not None, "module docstring missing"
    assert token in __doc__, (
        f"this module's docstring missing literal {token!r} — DEC-019 parity break (surface 4)"
    )

    # Surface 5 — SKILL.md.
    assert _SKILL_MD.exists(), f"SKILL.md not found at {_SKILL_MD}"
    skill_text = _SKILL_MD.read_text(encoding="utf-8")
    assert token in skill_text, (
        f"SKILL.md missing literal {token!r} — DEC-019 parity break (surface 5)"
    )


def test_no_cache_token_appears_in_all_5_surfaces() -> None:
    """The literal ``--no-cache`` token appears in all five DEC-019 surfaces.

    Mirrors :func:`test_no_grade_token_appears_in_all_5_surfaces`
    verbatim — different literal, same surfaces.
    """
    token = "--no-cache"

    # Surface 1 — argparse help.
    help_text = _generate_help_blob()
    assert token in help_text, (
        f"argparse help for `signalforge generate` missing literal {token!r} — "
        f"DEC-019 parity break (surface 1)"
    )

    # Surface 2 — plan file (DEC-002 of #189).
    plan_text = _PLAN_FILE.read_text(encoding="utf-8")
    assert token in plan_text, (
        f"plan file missing literal {token!r} — DEC-019 parity break (surface 2)"
    )

    # Surface 3 — CLI ops doc.
    cli_ops_text = _CLI_OPS_DOC.read_text(encoding="utf-8")
    assert token in cli_ops_text, (
        f"docs/cli-ops.md missing literal {token!r} — DEC-019 parity break (surface 3)"
    )

    # Surface 4 — this module's docstring.
    assert __doc__ is not None, "module docstring missing"
    assert token in __doc__, (
        f"this module's docstring missing literal {token!r} — DEC-019 parity break (surface 4)"
    )

    # Surface 5 — SKILL.md.
    skill_text = _SKILL_MD.read_text(encoding="utf-8")
    assert token in skill_text, (
        f"SKILL.md missing literal {token!r} — DEC-019 parity break (surface 5)"
    )


def test_cache_clear_grade_token_appears_in_all_surfaces() -> None:
    """The literal ``cache clear --grade`` token appears in every DEC-015 surface.

    Surfaces present at runtime (per the inventory in the module
    docstring above):

    1. argparse help on the ``cache`` subparser tree (top-level
       description, nested ``clear`` description, ``--grade``
       action's help string — the literal ``cache clear --grade``
       appears in the operator-facing prose on at least one of
       these).
    2. plan file — DEC-015 names the subcommand by its literal
       token.
    3. ``docs/cli-ops.md`` — cookbook section.
    4. this module's ``__doc__``.
    5. ``src/signalforge/skills/signalforge/SKILL.md`` — operator
       paragraph.

    Substring match. The test asserts presence in each of the FIVE
    surfaces individually; a missing surface is a DEC-019 parity
    break.
    """
    token = "cache clear --grade"

    # Surface 1 — argparse help blob across both ``cache`` and
    # ``generate`` subparsers. The literal "cache clear --grade"
    # appears in :mod:`signalforge.cli.generate`'s ``--no-cache``
    # help string (it points operators at the wipe command), AND in
    # :mod:`signalforge.cli.cache`'s docstrings / prose. Either
    # surface satisfies parity — we concat both blobs and substring-
    # match.
    help_text = _cache_help_blob() + "\n" + _generate_help_blob()
    assert token in help_text, (
        f"argparse help blob (cache + generate subparsers) missing literal "
        f"{token!r} — DEC-019 parity break (surface 1)"
    )

    # Surface 2 — plan file (DEC-015 of #189).
    plan_text = _PLAN_FILE.read_text(encoding="utf-8")
    assert token in plan_text, (
        f"plan file missing literal {token!r} — DEC-019 parity break (surface 2)"
    )

    # Surface 3 — CLI ops doc.
    cli_ops_text = _CLI_OPS_DOC.read_text(encoding="utf-8")
    assert token in cli_ops_text, (
        f"docs/cli-ops.md missing literal {token!r} — DEC-019 parity break (surface 3)"
    )

    # Surface 4 — this module's docstring.
    assert __doc__ is not None, "module docstring missing"
    assert token in __doc__, (
        f"this module's docstring missing literal {token!r} — DEC-019 parity break (surface 4)"
    )

    # Surface 5 — SKILL.md.
    skill_text = _SKILL_MD.read_text(encoding="utf-8")
    assert token in skill_text, (
        f"SKILL.md missing literal {token!r} — DEC-019 parity break (surface 5)"
    )


def test_grade_cache_section_present_in_grade_ops() -> None:
    """``docs/grade-ops.md`` carries the dedicated "Grade cache" section.

    Not a DEC-019 parity surface per se, but the US-009 spec ships
    grade-cache documentation in ``grade-ops.md`` so cross-linking
    from ``cli-ops.md`` stays meaningful. Pinning the section header
    here catches a refactor that drops the cross-link target.
    """
    assert _GRADE_OPS_DOC.exists(), f"grade-ops.md not found at {_GRADE_OPS_DOC}"
    grade_ops_text = _GRADE_OPS_DOC.read_text(encoding="utf-8")
    # H2 section header — DEC-016 docs the cache surface as a
    # standalone block under the grade-ops doc.
    assert "## Grade cache" in grade_ops_text, (
        "docs/grade-ops.md missing the '## Grade cache' section header — "
        "US-009 of #189 spec mandates this section as the cross-link target "
        "from docs/cli-ops.md § `signalforge cache clear --grade`"
    )
