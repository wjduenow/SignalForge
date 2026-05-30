"""Mechanical SKILL.md ↔ CLI parity gate (issue #141 / US-004 / DEC-015, 016, 019).

The bundled Claude Code skill at ``src/signalforge/skills/signalforge/SKILL.md``
teaches Claude to drive the ``signalforge`` CLI, which makes it a *parity
surface*: every behaviour change to the CLI subcommand surface (add / rename /
remove a subcommand, or shift the canonical demo commands the skill names) MUST
update ``SKILL.md`` in the same change.

This test is the **gate** that enforces the parity (per ``.claude/rules/
skill-parity.md`` § "Enforcement is a gate, not a prompt"). Because it runs
inside the canonical ``VALIDATE_CMD`` (``uv run pytest``), a change that drifts
the CLI from the skill fails validation until ``SKILL.md`` is updated — the
skill stays current automatically without relying on the model remembering
during a ``/ralph-run`` session.

Three token categories must appear verbatim in ``SKILL.md`` (plain substring
match — no regex, no whitespace / case normalisation; matches the
"boring substring match" defence philosophy used by every other prompt /
content gate in the project):

1. **Every subcommand name from the LIVE argparse parser** — sourced by
   walking ``signalforge.cli._build_parser()``'s sole
   :class:`argparse._SubParsersAction` and reading ``.choices.keys()``. Today's
   v0.2 set is ``version``, ``lint``, ``generate``, ``init-demo``,
   ``install-skill``, ``prune-existing``; the iteration auto-grows as new
   subcommands land.
2. **Four canonical demo command lines** (DEC-015 hardcoded list):
   ``signalforge init-demo``, ``signalforge generate <model> --write``,
   ``signalforge prune-existing <model> --schema <path>``,
   ``signalforge install-skill``. Pinned as :data:`_CANONICAL_DEMO_COMMANDS`.
3. **The ``signalforge install-skill`` bootstrap line** — already covered by
   category 2's fourth entry, but documented separately per DEC-015 so a
   future refactor that drops the install-skill demo from category 2 still
   surfaces the missing-bootstrap intent.

The third test in this module plants a synthetic SKILL.md with one subcommand
missing and asserts the scan helper raises :class:`AssertionError`. Per
``.claude/rules/testing-signal.md`` § "AST source-scan gates must catch all
three bypass patterns", the planted-violation self-check is mandatory — without
it, a refactor that broke the scan visitor would silently disable the gate at
the precise moment a real violation needed catching. The check here is
substring rather than AST, but the philosophy is identical.

This test is NOT an extension of ``test_5_surface_parity_init_demo.py`` (per
US-004 plan): that file covers ONE subcommand across five surfaces; this file
covers the WHOLE CLI surface against one external file. Different shape,
different responsibilities.

The test file lives under ``tests/`` (not ``.claude/``) so Ralph workers can
update it — see :mod:`signalforge.skills` module docs and the ``ralph-worker-
claude-dir-perms`` memory: workers cannot write to ``.claude/`` in worktrees,
so the gate and the gated artefact both live in worker-writable trees.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from signalforge.cli import _build_parser

# ---------------------------------------------------------------------------
# Surface locations
# ---------------------------------------------------------------------------

# The shipped SKILL.md lives under ``src/signalforge/skills/signalforge/``;
# ``__file__`` is at ``tests/cli/test_skill_cli_parity.py``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILL_MD = _REPO_ROOT / "src" / "signalforge" / "skills" / "signalforge" / "SKILL.md"

# Hardcoded per DEC-015. Substring-matched verbatim — do NOT normalise
# whitespace, case, or angle brackets here or in the helper below.
_CANONICAL_DEMO_COMMANDS: tuple[str, ...] = (
    "signalforge init-demo",
    "signalforge generate <model> --write",
    "signalforge prune-existing <model> --schema <path>",
    "signalforge install-skill",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _live_subcommand_names() -> tuple[str, ...]:
    """Return every subcommand registered on the live argparse parser.

    Walks :func:`signalforge.cli._build_parser`'s sole
    :class:`argparse._SubParsersAction` rather than hardcoding the set —
    so a new subcommand landing in ``signalforge.cli`` automatically grows
    the parity contract.
    """
    parser = _build_parser()
    subparser_actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(subparser_actions) == 1, (
        f"expected exactly one subparser action on the top-level parser; got "
        f"{len(subparser_actions)} — has the CLI grown a second subparsers "
        "tree?"
    )
    return tuple(subparser_actions[0].choices.keys())


def _assert_skill_md_names_every_subcommand(skill_md_path: Path) -> None:
    """Assert every live subcommand name appears as a substring of
    ``skill_md_path``.

    Factored out of :func:`test_skill_md_names_every_registered_subcommand`
    so :func:`test_parity_gate_catches_missing_subcommand_planted_violation`
    can drive the same check against a synthetic SKILL.md under
    :class:`pytest.TempPathFactory`'s ``tmp_path``.

    Raises :class:`AssertionError` naming the missing subcommand on
    failure — the message MUST surface the missing token verbatim so the
    operator can fix the drift without re-reading the test.
    """
    assert skill_md_path.exists(), f"SKILL.md not found at {skill_md_path}"
    body = skill_md_path.read_text(encoding="utf-8")
    subcommand_names = _live_subcommand_names()
    missing = [name for name in subcommand_names if name not in body]
    assert not missing, (
        f"SKILL.md at {skill_md_path} is missing registered CLI subcommand(s): "
        f"{missing!r}. The bundled Claude Code skill must name every "
        "subcommand the live argparse parser exposes — see "
        ".claude/rules/skill-parity.md."
    )


# ---------------------------------------------------------------------------
# Category 1: every live subcommand name appears in SKILL.md
# ---------------------------------------------------------------------------


def test_skill_md_names_every_registered_subcommand() -> None:
    """Every subcommand registered on the live CLI argparse parser appears
    verbatim in ``SKILL.md`` (category 1 of 3 per DEC-015).

    Iterates :func:`_live_subcommand_names` so a new subcommand grown into
    ``signalforge.cli._build_parser`` automatically extends the contract.
    On failure, the assertion message names exactly which subcommand(s)
    drifted.
    """
    _assert_skill_md_names_every_subcommand(_SKILL_MD)


# ---------------------------------------------------------------------------
# Category 2: the four canonical demo command lines appear in SKILL.md
# ---------------------------------------------------------------------------


def test_skill_md_contains_canonical_demo_command_lines() -> None:
    """Each canonical demo command line from DEC-015 appears verbatim in
    ``SKILL.md`` (category 2 of 3).

    The four demo commands are the operator-facing flow the skill teaches
    Claude to walk through: ``init-demo`` → ``generate <model> --write``
    → ``prune-existing <model> --schema <path>`` → ``install-skill``. A
    drift here means the skill is teaching a flow that no longer matches
    the CLI surface — exactly the failure mode the gate exists to
    prevent.

    Substring-matched (not regex / case-folded) so angle-bracket
    placeholders like ``<model>`` and ``<path>`` survive verbatim.
    """
    assert _SKILL_MD.exists(), f"SKILL.md not found at {_SKILL_MD}"
    body = _SKILL_MD.read_text(encoding="utf-8")
    missing = [cmd for cmd in _CANONICAL_DEMO_COMMANDS if cmd not in body]
    assert not missing, (
        f"SKILL.md is missing canonical demo command line(s): {missing!r}. "
        "These are the four operator-facing commands the bundled Claude "
        "Code skill teaches — see plans/super/141-claude-skill-install.md "
        "DEC-015."
    )


# ---------------------------------------------------------------------------
# Category 3 (planted violation): the gate can fail loud
# ---------------------------------------------------------------------------


def test_parity_gate_catches_missing_subcommand_planted_violation(
    tmp_path: Path,
) -> None:
    """Planted-violation self-check: a synthetic SKILL.md missing one
    subcommand MUST trip the gate (DEC-016 + ``testing-signal.md`` § "AST
    source-scan gates").

    Without this test, a refactor that broke
    :func:`_assert_skill_md_names_every_subcommand` (e.g. a typo flipping
    the ``not in`` check, or an inadvertent ``return`` short-circuit)
    would silently disable the gate at the precise moment a real
    violation needed catching. The check here is substring rather than
    AST, but the philosophy from ``testing-signal.md`` applies verbatim.

    We craft a synthetic SKILL.md that names every subcommand EXCEPT one,
    point the helper at it, and assert :class:`AssertionError` raises
    with the missing subcommand named in the message. The synthetic body
    avoids touching the real shipped SKILL.md so the planted violation
    cannot accidentally leak into the gated artefact.
    """
    subcommand_names = _live_subcommand_names()
    # Choose the subcommand to omit deterministically — the first one in
    # the live order. The gate doesn't care which we drop; the contract
    # is "any missing subcommand trips the assert".
    omitted = subcommand_names[0]
    kept = tuple(n for n in subcommand_names if n != omitted)
    synthetic_body = (
        "# Synthetic SKILL.md for planted-violation self-check.\n\n"
        "This file names every CLI subcommand EXCEPT one, so the parity "
        "gate must raise AssertionError naming the omitted subcommand:\n\n"
        + "\n".join(f"- `signalforge {name}`" for name in kept)
        + "\n"
    )
    # Sanity: confirm the synthetic body actually omits the chosen
    # subcommand verbatim (defends against a future change to the
    # synthetic body that accidentally includes the omitted name as a
    # substring of something else).
    assert omitted not in synthetic_body, (
        f"synthetic SKILL.md inadvertently contains the omitted "
        f"subcommand {omitted!r}; rewrite the synthetic body so the "
        "planted violation is real"
    )

    synthetic_skill_md = tmp_path / "SKILL.md"
    synthetic_skill_md.write_text(synthetic_body, encoding="utf-8")

    with pytest.raises(AssertionError) as exc_info:
        _assert_skill_md_names_every_subcommand(synthetic_skill_md)

    # The assertion message must surface the omitted subcommand verbatim
    # so an operator running the gate locally fixes the right token.
    assert omitted in str(exc_info.value), (
        f"AssertionError message did not name the missing subcommand "
        f"{omitted!r}; got: {exc_info.value!r}"
    )


# ---------------------------------------------------------------------------
# Category 4 (QG extension): flags the skill claims must exist on the live CLI
# ---------------------------------------------------------------------------


# Match ``signalforge <subcommand> --<flag>`` occurrences inside SKILL.md so a
# typo / stale-rebase that teaches a non-existent flag (the US-008-era
# ``signalforge install-skill --force`` finding) trips the gate. Captures the
# subcommand AND the flag separately so we can dispatch the validity check to
# the right subparser. Plain ASCII flag chars only — the regex deliberately
# does NOT match exotic shells (`/`, `=`, etc.) because skill prose only ever
# teaches the conventional long-flag form.
_SKILL_FLAG_USAGE_RE = re.compile(r"signalforge ([a-z][a-z0-9-]*)\s+(--[a-z][a-z0-9-]*)")


def _subparser_flags(subcommand: str) -> frozenset[str]:
    """Return every long-form flag (``--foo``) registered on ``subcommand``.

    Walks the live argparse subparser for the named subcommand and harvests
    every ``option_string`` that starts with ``--`` from every action.
    Returns an empty frozenset if the subcommand isn't registered (caller
    is responsible for distinguishing that case).
    """
    parser = _build_parser()
    subparser_actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(subparser_actions) == 1
    choices = subparser_actions[0].choices
    if subcommand not in choices:
        return frozenset()
    sub = choices[subcommand]
    flags: set[str] = set()
    for action in sub._actions:
        for opt in action.option_strings:
            if opt.startswith("--"):
                flags.add(opt)
    return frozenset(flags)


def test_skill_md_only_teaches_flags_that_exist_on_the_live_cli() -> None:
    """Every ``signalforge <subcommand> --<flag>`` occurrence in SKILL.md
    names a flag that actually exists on the live subparser (QG-extended
    category 4 of 3, added after the US-008 review found SKILL.md teaching
    ``signalforge install-skill --force`` — a flag DEC-003 explicitly
    forbids).

    The substring match in category 1/2 catches missing subcommands and
    drifted demo commands but NOT a typo flag the skill claims to exist.
    This test closes that gap by scanning SKILL.md for any
    ``signalforge X --flag`` pattern and asserting ``--flag`` is in the
    live subparser's option strings.

    The skill-parity rule explicitly acknowledges the gate is "necessary
    not sufficient" (per ``.claude/rules/skill-parity.md``) — semantic
    prose freshness still rides reviewer attention + the clauditor self-
    grade. This category 4 narrows that gap by promoting one specific
    "prose lies about the CLI surface" failure mode into a mechanical
    gate.
    """
    assert _SKILL_MD.exists(), f"SKILL.md not found at {_SKILL_MD}"
    body = _SKILL_MD.read_text(encoding="utf-8")

    bogus: list[tuple[str, str]] = []
    for match in _SKILL_FLAG_USAGE_RE.finditer(body):
        subcommand, flag = match.group(1), match.group(2)
        registered = _subparser_flags(subcommand)
        if not registered:
            # Subcommand itself doesn't exist on the live CLI — caught by
            # category 1's gate, not this one. Skip to avoid double-counting.
            continue
        if flag not in registered:
            bogus.append((subcommand, flag))

    assert not bogus, (
        f"SKILL.md teaches flag(s) that do not exist on the live CLI: "
        f"{bogus!r}. Either the flag was renamed / removed (update SKILL.md) "
        "or the skill prose is wrong (e.g. teaching ``signalforge install-skill "
        "--force`` when DEC-003 explicitly forbids ``--force``). Run "
        "``signalforge <subcommand> --help`` to see the real flag set."
    )
