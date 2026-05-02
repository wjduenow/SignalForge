"""Deterministic snapshot-fixture builders for the diff renderer (US-011).

Builds each of the 10 :class:`DiffReport` cases enumerated in DEC-017 of
``plans/super/8-diff-renderer.md`` plus the matching renderer invocation
recipe. The builders are pure functions: same inputs (which here are
hard-coded) → same :class:`DiffReport` → same rendered text. The
fixtures committed under :file:`tests/fixtures/diff/` are the byte-equal
output of feeding each report into the appropriate
:mod:`signalforge.diff._renderers` concrete with the controls listed
below.

Why build :class:`DiffReport` directly (vs. :func:`render_diff` end-to-end):

* The orchestrator stamps a fresh ``run_id`` (``uuid4``) and
  ``duration_seconds`` per call; both would drift the snapshot bytes
  every regeneration. Building the :class:`DiffReport` directly with
  pinned values keeps fixtures deterministic.
* The reproducibility hashes (``candidate_hash`` /
  ``prune_result_hash`` / ``grading_report_hash``) are stable here as
  literal hex strings rather than computed. A regression in the hash
  recipe surfaces in :mod:`tests.diff.test_engine` already; the
  snapshot fixtures cover the renderer surface.
* The renderer surface (text layout, colour-precedence, markdown
  structure, JSON serialisation) is what the snapshot test pins.

Each ``CASES[name]`` entry is a tuple of:

1. ``builder`` — zero-arg callable returning the
   :class:`DiffReport`.
2. ``recipe`` — dict of recipe knobs:

   * ``surface``: ``"ansi"`` / ``"markdown"`` / ``"json"`` — picks the
     renderer concrete.
   * ``filename``: snapshot fixture filename relative to
     :file:`tests/fixtures/diff/`.
   * ``terminal_width``: optional ``int`` for the AnsiRenderer's
     ``terminal_width`` kwarg (DEC-013 narrow-mode).
   * ``force_color``: optional ``bool | None`` for the AnsiRenderer's
     ``force_color`` kwarg.
   * ``no_color_env``: optional ``bool`` — when ``True``, the recipe
     runner sets ``NO_COLOR=1`` for the duration of the render.
   * ``markdown_project_dir``: optional ``str`` for the
     MarkdownRenderer's ``project_dir`` kwarg.

The recipe is consumed by :func:`render_for_case` (test side) and the
:file:`tests/fixtures/diff/regenerate.sh` companion script.

Reference: ``plans/super/8-diff-renderer.md`` US-011 + DEC-017.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TypedDict

from signalforge.diff._renderers import AnsiRenderer, JsonRenderer, MarkdownRenderer
from signalforge.diff.config import DiffConfig
from signalforge.diff.models import DiffEntry, DiffReport

# ---------------------------------------------------------------------------
# Stable, deterministic constants for every fixture.
# ---------------------------------------------------------------------------

_PINNED_VERSION = "0.0.0-snapshot"
_PINNED_RUN_ID = "0123456789abcdef0123456789abcdef"
_PINNED_DURATION = 0.0
_PINNED_CANDIDATE_HASH = "0123456789abcdef"
_PINNED_PRUNE_HASH = "fedcba9876543210"
_PINNED_GRADING_HASH = "1122334455667788"


# ---------------------------------------------------------------------------
# DiffEntry helpers.
# ---------------------------------------------------------------------------


def _kept_doc(
    *,
    artifact_id: str = "column.customer_id.description",
    why: str = "Description added; passed all grading criteria.",
    score: float | None = 0.85,
    passed: bool | None = True,
) -> DiffEntry:
    return DiffEntry(
        artifact_id=artifact_id,
        test_type=None,
        tier="kept",
        drop_reason=None,
        why=why,
        score=score,
        passed=passed,
    )


def _kept_test(
    *,
    artifact_id: str = "test.column.customer_id.not_null",
    test_type: str = "not_null",
    why: str = "Test returned non-zero failing rows on the warehouse sample.",
) -> DiffEntry:
    return DiffEntry(
        artifact_id=artifact_id,
        test_type=test_type,
        tier="kept",
        drop_reason=None,
        why=why,
        score=None,
        passed=None,
    )


def _dropped_test(
    *,
    artifact_id: str = "test.column.customer_id.unique",
    test_type: str = "unique",
    drop_reason: str = "always-passes",
    why: str = "Test returned zero failing rows on the representative sample.",
) -> DiffEntry:
    return DiffEntry(
        artifact_id=artifact_id,
        test_type=test_type,
        tier="dropped",
        drop_reason=drop_reason,  # type: ignore[arg-type]
        why=why,
        score=None,
        passed=None,
    )


def _flagged_doc(
    *,
    artifact_id: str = "column.email.description",
    why: str = "Grading score 0.45 below threshold 0.50.",
    score: float | None = 0.45,
    passed: bool | None = False,
) -> DiffEntry:
    return DiffEntry(
        artifact_id=artifact_id,
        test_type=None,
        tier="flagged",
        drop_reason=None,
        why=why,
        score=score,
        passed=passed,
    )


# ---------------------------------------------------------------------------
# Canonical YAML / unified-diff strings shared across cases.
# ---------------------------------------------------------------------------

_PROPOSED_YAML = (
    "version: 2\n"
    "models:\n"
    "  - name: dim_customers\n"
    "    columns:\n"
    "      - name: customer_id\n"
    "        description: Surrogate key.\n"
    "        tests:\n"
    "          - not_null\n"
)

_EXISTING_YAML = (
    "version: 2\nmodels:\n  - name: dim_customers\n    columns:\n      - name: customer_id\n"
)

_UNIFIED_DIFF = (
    "--- a/models/dim_customers.yml\n"
    "+++ b/models/dim_customers.yml\n"
    "@@ -1,5 +1,8 @@\n"
    " version: 2\n"
    " models:\n"
    "   - name: dim_customers\n"
    "     columns:\n"
    "       - name: customer_id\n"
    "+        description: Surrogate key.\n"
    "+        tests:\n"
    "+          - not_null\n"
)

_UNIFIED_DIFF_FROM_DEVNULL = (
    "--- /dev/null\n"
    "+++ b/models/dim_customers.yml\n"
    "@@ -0,0 +1,7 @@\n"
    "+version: 2\n"
    "+models:\n"
    "+  - name: dim_customers\n"
    "+    columns:\n"
    "+      - name: customer_id\n"
    "+        description: Surrogate key.\n"
    "+        tests:\n"
    "+          - not_null\n"
)


# ---------------------------------------------------------------------------
# Report builders — one per snapshot case.
# ---------------------------------------------------------------------------


def _full_with_grade_report() -> DiffReport:
    """Case 1/2/3 — happy path with kept + dropped + flagged + grading."""
    entries = (
        _kept_doc(),
        _kept_test(),
        _dropped_test(),
        _flagged_doc(),
    )
    return DiffReport(
        signalforge_version=_PINNED_VERSION,
        model_unique_id="model.shop.dim_customers",
        run_id=_PINNED_RUN_ID,
        duration_seconds=_PINNED_DURATION,
        proposed_yaml=_PROPOSED_YAML,
        existing_yaml=_EXISTING_YAML,
        unified_diff=_UNIFIED_DIFF,
        entries=entries,
        kept_count=2,
        dropped_count=1,
        flagged_count=1,
        has_existing_schema=True,
        candidate_hash=_PINNED_CANDIDATE_HASH,
        prune_result_hash=_PINNED_PRUNE_HASH,
        grading_report_hash=_PINNED_GRADING_HASH,
    )


def _no_existing_schema_report() -> DiffReport:
    """Case 4 — `existing_schema=None`; unified diff sources from /dev/null."""
    entries = (
        _kept_doc(),
        _kept_test(),
    )
    return DiffReport(
        signalforge_version=_PINNED_VERSION,
        model_unique_id="model.shop.dim_customers",
        run_id=_PINNED_RUN_ID,
        duration_seconds=_PINNED_DURATION,
        proposed_yaml=_PROPOSED_YAML,
        existing_yaml=None,
        unified_diff=_UNIFIED_DIFF_FROM_DEVNULL,
        entries=entries,
        kept_count=2,
        dropped_count=0,
        flagged_count=0,
        has_existing_schema=False,
        candidate_hash=_PINNED_CANDIDATE_HASH,
        prune_result_hash=_PINNED_PRUNE_HASH,
        grading_report_hash=None,
    )


def _kept_only_report() -> DiffReport:
    """Case 5 — every entry is `tier=kept`; empty dropped table cells."""
    entries = (
        _kept_doc(),
        _kept_test(),
        _kept_test(
            artifact_id="test.column.email.not_null",
            why="Test returned 12 failing rows on the warehouse sample.",
        ),
    )
    return DiffReport(
        signalforge_version=_PINNED_VERSION,
        model_unique_id="model.shop.dim_customers",
        run_id=_PINNED_RUN_ID,
        duration_seconds=_PINNED_DURATION,
        proposed_yaml=_PROPOSED_YAML,
        existing_yaml=_EXISTING_YAML,
        unified_diff=_UNIFIED_DIFF,
        entries=entries,
        kept_count=3,
        dropped_count=0,
        flagged_count=0,
        has_existing_schema=True,
        candidate_hash=_PINNED_CANDIDATE_HASH,
        prune_result_hash=_PINNED_PRUNE_HASH,
        grading_report_hash=None,
    )


def _dropped_only_report() -> DiffReport:
    """Case 6 — every entry is `tier=dropped`; empty kept table."""
    entries = (
        _dropped_test(),
        _dropped_test(
            artifact_id="test.column.email.unique",
            test_type="unique",
            drop_reason="always-passes",
            why="Test returned zero failing rows on the representative sample.",
        ),
        _dropped_test(
            artifact_id="test.column.region.relationships",
            test_type="relationships",
            drop_reason="requires-future-data",
            why="Target model 'ref(dim_region)' is absent from the manifest.",
        ),
    )
    return DiffReport(
        signalforge_version=_PINNED_VERSION,
        model_unique_id="model.shop.dim_customers",
        run_id=_PINNED_RUN_ID,
        duration_seconds=_PINNED_DURATION,
        proposed_yaml=_EXISTING_YAML,  # nothing kept; proposed == existing
        existing_yaml=_EXISTING_YAML,
        unified_diff="",  # identical inputs → no diff body
        entries=entries,
        kept_count=0,
        dropped_count=3,
        flagged_count=0,
        has_existing_schema=True,
        candidate_hash=_PINNED_CANDIDATE_HASH,
        prune_result_hash=_PINNED_PRUNE_HASH,
        grading_report_hash=None,
    )


def _no_grading_report_report() -> DiffReport:
    """Case 7 — `grading_report=None`; no flagged tier; score columns empty."""
    entries = (
        _kept_doc(score=None, passed=None, why="Description added."),
        _kept_test(),
        _dropped_test(),
    )
    return DiffReport(
        signalforge_version=_PINNED_VERSION,
        model_unique_id="model.shop.dim_customers",
        run_id=_PINNED_RUN_ID,
        duration_seconds=_PINNED_DURATION,
        proposed_yaml=_PROPOSED_YAML,
        existing_yaml=_EXISTING_YAML,
        unified_diff=_UNIFIED_DIFF,
        entries=entries,
        kept_count=2,
        dropped_count=1,
        flagged_count=0,
        has_existing_schema=True,
        candidate_hash=_PINNED_CANDIDATE_HASH,
        prune_result_hash=_PINNED_PRUNE_HASH,
        grading_report_hash=None,
    )


def _injection_payloads_report() -> DiffReport:
    """Case 10 — descriptions carry hostile content (DEC-007 + DEC-008 + AR-9).

    Payloads cover every escape vector on the security boundary:

    * ``\\x1b[31m...\\x1b[0m`` — ANSI SGR (smuggled colour).
    * ``\\`\\`\\`code\\`\\`\\``` — triple-backticks (markdown fence break).
    * ``</details>`` — raw HTML (markdown injection).
    * ``col | name`` — pipe (markdown table-cell break).
    * ``---`` / ``!tag`` — YAML-edge content (also benign in markdown).

    The renderer must show every payload as literal text — never
    execute the colour, never break the markdown fence, never break the
    table layout.
    """
    entries = (
        _kept_doc(
            artifact_id="column.customer_id.description",
            why="Has \x1b[31mEVIL\x1b[0m ANSI escape; ```triple``` backticks.",
        ),
        _dropped_test(
            artifact_id="test.column.region.accepted_values",
            test_type="accepted_values",
            drop_reason="always-passes",
            why="Has </details> HTML and col | name pipe.",
        ),
        _flagged_doc(
            artifact_id="column.email.description",
            why="YAML edge content: --- and !tag stay inert.",
        ),
    )
    return DiffReport(
        signalforge_version=_PINNED_VERSION,
        model_unique_id="model.shop.dim_customers",
        run_id=_PINNED_RUN_ID,
        duration_seconds=_PINNED_DURATION,
        proposed_yaml=_PROPOSED_YAML,
        existing_yaml=_EXISTING_YAML,
        unified_diff=_UNIFIED_DIFF,
        entries=entries,
        kept_count=1,
        dropped_count=1,
        flagged_count=1,
        has_existing_schema=True,
        candidate_hash=_PINNED_CANDIDATE_HASH,
        prune_result_hash=_PINNED_PRUNE_HASH,
        grading_report_hash=_PINNED_GRADING_HASH,
    )


# ---------------------------------------------------------------------------
# Recipe table — 10 cases per DEC-017.
# ---------------------------------------------------------------------------


class _RecipeRequired(TypedDict):
    """Always-present recipe keys (every snapshot case has these)."""

    surface: str  # "ansi" | "markdown" | "json"
    filename: str


class _Recipe(_RecipeRequired, total=False):
    """Per-case recipe dict — required keys + optional renderer knobs."""

    terminal_width: int
    force_color: bool | None
    no_color_env: bool
    markdown_project_dir: str


_Builder = Callable[[], DiffReport]

CASES: dict[str, tuple[_Builder, _Recipe]] = {
    # 1. full_with_grade — ANSI happy path (force_color=True for stable bytes).
    "full_with_grade.ansi": (
        _full_with_grade_report,
        {
            "surface": "ansi",
            "filename": "full_with_grade.ansi",
            "terminal_width": 120,
            "force_color": True,
        },
    ),
    # 2. full_with_grade.md — markdown surface, same inputs.
    "full_with_grade.md": (
        _full_with_grade_report,
        {
            "surface": "markdown",
            "filename": "full_with_grade.md",
            "markdown_project_dir": "<project_dir>",
        },
    ),
    # 3. full_with_grade.json — sidecar shape.
    "full_with_grade.json": (
        _full_with_grade_report,
        {
            "surface": "json",
            "filename": "full_with_grade.json",
        },
    ),
    # 4. no_existing_schema — unified diff sources from /dev/null.
    "no_existing_schema.ansi": (
        _no_existing_schema_report,
        {
            "surface": "ansi",
            "filename": "no_existing_schema.ansi",
            "terminal_width": 120,
            "force_color": True,
        },
    ),
    # 5. kept_only — every artifact `tier=kept`.
    "kept_only.ansi": (
        _kept_only_report,
        {
            "surface": "ansi",
            "filename": "kept_only.ansi",
            "terminal_width": 120,
            "force_color": True,
        },
    ),
    # 6. dropped_only — every artifact `tier=dropped`.
    "dropped_only.ansi": (
        _dropped_only_report,
        {
            "surface": "ansi",
            "filename": "dropped_only.ansi",
            "terminal_width": 120,
            "force_color": True,
        },
    ),
    # 7. no_grading_report — no flagged tier.
    "no_grading_report.ansi": (
        _no_grading_report_report,
        {
            "surface": "ansi",
            "filename": "no_grading_report.ansi",
            "terminal_width": 120,
            "force_color": True,
        },
    ),
    # 8. plain_no_color — ANSI surface with `NO_COLOR=1` env (no escapes).
    "plain_no_color.txt": (
        _full_with_grade_report,
        {
            "surface": "ansi",
            "filename": "plain_no_color.txt",
            "terminal_width": 120,
            "force_color": False,
            "no_color_env": True,
        },
    ),
    # 9. narrow_terminal — 40-col TTY (DEC-013 compact mode).
    "narrow_terminal.ansi": (
        _full_with_grade_report,
        {
            "surface": "ansi",
            "filename": "narrow_terminal.ansi",
            "terminal_width": 40,
            "force_color": True,
        },
    ),
    # 10. injection_payloads — adversarial content; ANSI surface.
    "injection_payloads.ansi": (
        _injection_payloads_report,
        {
            "surface": "ansi",
            "filename": "injection_payloads.ansi",
            "terminal_width": 120,
            "force_color": True,
        },
    ),
    # 10b. injection_payloads.md — markdown surface, same hostile inputs.
    "injection_payloads.md": (
        _injection_payloads_report,
        {
            "surface": "markdown",
            "filename": "injection_payloads.md",
            "markdown_project_dir": "<project_dir>",
        },
    ),
}


def render_for_case(
    name: str,
    *,
    config: DiffConfig | None = None,
) -> str:
    """Render the named case, applying the recipe.

    Mirrors what :file:`tests/fixtures/diff/regenerate.sh` does — pure
    Python; no subprocess. Tests call this with the same defaults so
    the snapshot file's bytes match the in-test render bytes.

    The ``no_color_env`` recipe knob mutates ``os.environ`` for the
    duration of the render. The mutation is intentionally local — the
    caller (the regenerate script or the test runner) is expected to
    isolate this from concurrent code; pytest's ``monkeypatch`` does
    that automatically.
    """
    builder, recipe = CASES[name]
    report = builder()
    cfg = config if config is not None else DiffConfig()
    surface = recipe["surface"]

    if surface == "json":
        return JsonRenderer().render(report)

    if surface == "markdown":
        renderer = MarkdownRenderer(
            config=cfg,
            project_dir=recipe.get("markdown_project_dir"),
        )
        return renderer.render(report)

    # ansi surface.
    if recipe.get("no_color_env"):
        # Defensive: if the caller already set NO_COLOR, leave it as is.
        had_prior = "NO_COLOR" in os.environ
        if not had_prior:
            os.environ["NO_COLOR"] = "1"
        try:
            ansi = AnsiRenderer(
                config=cfg,
                force_color=recipe.get("force_color"),
                terminal_width=recipe.get("terminal_width"),
            )
            return ansi.render(report)
        finally:
            if not had_prior:
                os.environ.pop("NO_COLOR", None)

    ansi = AnsiRenderer(
        config=cfg,
        force_color=recipe.get("force_color"),
        terminal_width=recipe.get("terminal_width"),
    )
    return ansi.render(report)


__all__ = ("CASES", "render_for_case")
