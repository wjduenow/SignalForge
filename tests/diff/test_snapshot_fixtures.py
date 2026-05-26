"""Snapshot fixture byte-equality tests for the diff renderer (US-011 of #8).

The 10 fixture cases enumerated in DEC-017 of
``plans/super/8-diff-renderer.md`` are committed under
:file:`tests/fixtures/diff/`. This module renders each case via the
exact same recipe used by :file:`tests/fixtures/diff/regenerate.sh` and
asserts byte-equality against the committed file.

If a snapshot diverges, the regression is either:

1. An intentional renderer change → run
   ``bash tests/fixtures/diff/regenerate.sh`` and review the diff.
2. An unintentional regression → fix the renderer.

The error message for a mismatch reminds the operator how to
regenerate. The drift detector at :mod:`tests.diff.test_drift_detector`
covers the schema-shape side of the same defence.

Reference: ``plans/super/8-diff-renderer.md`` US-011 + DEC-017,
``.claude/rules/testing-signal.md`` (no ``assert True``-shaped tests;
each snapshot byte-equality is capable of failing if its target
breaks).
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from tests.diff._snapshot_inputs import CASES, render_for_case

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "diff"


def _format_diff(expected: str, actual: str, fixture_path: Path) -> str:
    """Format a unified diff between expected and actual text.

    The reminder line steers the operator at the regenerate seam — same
    pattern as ``tests/fixtures/regenerate.sh`` for the manifest
    fixtures (issue #2).
    """
    diff = difflib.unified_diff(
        expected.splitlines(keepends=True),
        actual.splitlines(keepends=True),
        fromfile=str(fixture_path),
        tofile="<rendered>",
        lineterm="",
    )
    body = "".join(diff)
    return (
        f"snapshot mismatch for {fixture_path.name}:\n"
        f"{body}\n"
        f"\n"
        f"If the renderer change is intentional, regenerate via:\n"
        f"  bash tests/fixtures/diff/regenerate.sh\n"
    )


@pytest.mark.parametrize("case_name", list(CASES.keys()))
def test_snapshot_fixture_byte_equal(case_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each snapshot fixture matches the live renderer output byte-for-byte.

    The recipe specifies any environment manipulation (``NO_COLOR``,
    ``FORCE_COLOR``); :func:`render_for_case` applies it via
    ``os.environ`` mutation. We use ``monkeypatch.setattr`` style
    (via ``monkeypatch.setenv`` / ``delenv``) where the recipe needs
    env control, so the test isolates from the surrounding pytest
    process. The recipe runner itself is also resilient to a prior
    ``NO_COLOR`` value (see :func:`render_for_case`), but explicit
    monkeypatch keeps the test self-contained.
    """
    _builder, recipe = CASES[case_name]

    # Make the renderer's TTY-detection deterministic. Even when the
    # recipe pins `force_color`, the AnsiRenderer's chain falls back
    # to env / TTY in some branches; clearing both env vars makes the
    # outcome rest only on `force_color` + the recipe's `no_color_env`
    # toggle.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    if recipe.get("no_color_env"):
        monkeypatch.setenv("NO_COLOR", "1")

    rendered = render_for_case(case_name)
    fixture_path = _FIXTURES_DIR / recipe["filename"]
    expected = fixture_path.read_text(encoding="utf-8")

    assert rendered == expected, _format_diff(expected, rendered, fixture_path)


def test_every_dec017_case_has_a_committed_fixture() -> None:
    """Every entry in :data:`CASES` corresponds to a committed fixture file.

    The 10-case matrix (11 entries — case 10 has both ``.ansi`` and
    ``.md`` per DEC-017) must all exist on disk. A missing fixture is
    a regen-was-skipped or commit-was-incomplete failure.
    """
    missing = []
    for _name, (_builder, recipe) in CASES.items():
        fixture_path = _FIXTURES_DIR / recipe["filename"]
        if not fixture_path.exists():
            missing.append(recipe["filename"])
    assert not missing, (
        f"missing snapshot fixtures: {missing}. "
        f"Run `bash tests/fixtures/diff/regenerate.sh` to regenerate."
    )


def test_dec017_case_count() -> None:
    """The :data:`CASES` table holds exactly the DEC-017 matrix.

    Counts the unique fixture filenames so the AR-9 markdown-injection
    case (which contributes both ``.ansi`` and ``.md`` files) is
    counted as one logical case but two on-disk artefacts. This guards
    against an accidental drop / duplication of a case during a future
    refactor of :mod:`tests.diff._snapshot_inputs`.
    """
    filenames = {recipe["filename"] for _b, recipe in CASES.values()}
    # DEC-017 enumerates 10 cases; case 10 has both .ansi and .md
    # surfaces, giving 11 on-disk artefacts. Issue #116 adds the
    # proposed-test-files case in both .ansi and .md surfaces (2 more)
    # for 13 on-disk artefacts.
    assert len(filenames) == 13, (
        f"expected 13 snapshot fixture files (10 DEC-017 cases + the "
        f"injection_payloads markdown variant + the #116 proposed_test_files "
        f"ansi/markdown variants), got {len(filenames)}: {sorted(filenames)}"
    )


def test_plain_no_color_fixture_carries_no_ansi_escapes() -> None:
    """The ``plain_no_color.txt`` fixture has zero ``\\x1b`` bytes.

    Defence-in-depth: even if the recipe / renderer regress so colour
    leaks through despite ``NO_COLOR=1``, the static check on the
    committed fixture catches it. Mirrors DEC-007 ("strip
    UNCONDITIONALLY"): the user-content ANSI strip runs even on the
    plain surface; the renderer's own SGR codes are guarded by
    ``force_color=False`` + ``NO_COLOR=1`` per the recipe.
    """
    fixture_path = _FIXTURES_DIR / "plain_no_color.txt"
    text = fixture_path.read_text(encoding="utf-8")
    assert "\x1b" not in text, (
        "plain_no_color.txt fixture must contain no ANSI escapes (DEC-021 NO_COLOR + DEC-007 strip)"
    )


def test_injection_ansi_fixture_strips_user_content_escapes() -> None:
    """The injection-payloads ANSI fixture carries the literal text
    ``EVIL`` (the user-content payload, after strip) but NEVER the
    smuggled ``\\x1b[31m`` escape from inside the payload.

    The renderer's own SGR codes ARE present (they're how the ANSI
    surface colours its own table); the assertion checks the unique
    pattern that would only appear if the user-content strip failed.
    Mirrors DEC-007 (strip UNCONDITIONALLY).
    """
    fixture_path = _FIXTURES_DIR / "injection_payloads.ansi"
    text = fixture_path.read_text(encoding="utf-8")
    assert "EVIL" in text, "literal 'EVIL' text must survive the strip pass"
    # The original payload is `\x1b[31mEVIL\x1b[0m`. After strip, the
    # `\x1b[31m` and `\x1b[0m` are gone. The renderer's own header /
    # tier-cell colouring uses `\x1b[1m`, `\x1b[32m`, `\x1b[31m`, etc.,
    # so we cannot assert "no \x1b[31m anywhere"; we instead assert
    # that the smuggled-pair `\x1b[31mEVIL\x1b[0m` does not appear as
    # a contiguous run.
    assert "\x1b[31mEVIL\x1b[0m" not in text, (
        "smuggled `\\x1b[31mEVIL\\x1b[0m` payload must be stripped (DEC-007)"
    )
