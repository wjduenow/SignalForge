"""Tests for ``signalforge.diff._renderers`` (US-008 of issue #8).

Covers the :class:`Renderer` ABC + the :class:`AnsiRenderer` concrete:

1. **Renderer ABC** — :class:`AnsiRenderer` is a subclass; the abstract
   :meth:`Renderer.render` cannot be instantiated without override.
2. **Wide-TTY 6-column table** — header, 5 row cells, ``why`` cell;
   ANSI escapes from user content stripped (DEC-007).
3. **Narrow-TTY compact mode (DEC-013)** — when terminal width below
   :attr:`DiffConfig.narrow_terminal_threshold`, the ``why`` column is
   dropped and a follow-up indented line carries the prose.
4. **Colour-precedence (DEC-021)** — six unit tests, one per chain
   position. Each higher-priority signal must beat every lower one.
5. **Unconditional ANSI stripping (DEC-007)** — a malicious ``why``
   field carrying ``\\x1b[31mEVIL\\x1b[0m`` renders the literal text
   ``EVIL`` and never ships the smuggled escape codes, in BOTH the
   coloured and the non-coloured output.
"""

from __future__ import annotations

import abc

import pytest

from signalforge.diff import render_to_text
from signalforge.diff._renderers import AnsiRenderer, MarkdownRenderer, Renderer
from signalforge.diff.config import DiffConfig
from signalforge.diff.models import DiffEntry, DiffReport, ProposedTestFile

# ---------------------------------------------------------------------------
# Test fixtures.
# ---------------------------------------------------------------------------


def _make_report(
    *,
    entries: tuple[DiffEntry, ...] = (),
    unified_diff: str = "",
    model_unique_id: str = "model.proj.customers",
    proposed_test_files: tuple[ProposedTestFile, ...] = (),
) -> DiffReport:
    """Construct a minimal :class:`DiffReport` for renderer tests."""
    kept = sum(1 for e in entries if e.tier == "kept")
    kept_uncertain = sum(1 for e in entries if e.tier == "kept-uncertain")
    dropped = sum(1 for e in entries if e.tier == "dropped")
    flagged = sum(1 for e in entries if e.tier == "flagged")
    return DiffReport(
        signalforge_version="0.0.0-test",
        model_unique_id=model_unique_id,
        run_id="r" * 32,
        duration_seconds=0.0,
        proposed_yaml="",
        existing_yaml=None,
        unified_diff=unified_diff,
        entries=entries,
        proposed_test_files=proposed_test_files,
        kept_count=kept,
        kept_uncertain_count=kept_uncertain,
        dropped_count=dropped,
        flagged_count=flagged,
        has_existing_schema=False,
        candidate_hash="0" * 16,
        prune_result_hash="0" * 16,
        grading_report_hash=None,
    )


def _kept_entry(
    *,
    artifact_id: str = "column.customer_id.description",
    why: str = "kept by default",
    score: float | None = 0.95,
) -> DiffEntry:
    return DiffEntry(
        artifact_id=artifact_id,
        test_type=None,
        tier="kept",
        drop_reason=None,
        why=why,
        score=score,
        passed=True if score is not None else None,
    )


def _dropped_entry(
    *,
    artifact_id: str = "test.column.email.not_null",
    drop_reason: str = "always-passes",
    why: str = "ran on 1k sample, 0 failing rows",
) -> DiffEntry:
    return DiffEntry(
        artifact_id=artifact_id,
        test_type="not_null",
        tier="dropped",
        drop_reason=drop_reason,  # type: ignore[arg-type]
        why=why,
        score=None,
        passed=None,
    )


@pytest.fixture
def default_config() -> DiffConfig:
    return DiffConfig()


# ---------------------------------------------------------------------------
# 1. Renderer ABC contract.
# ---------------------------------------------------------------------------


def test_renderer_is_abc() -> None:
    """The :class:`Renderer` base is an ABC (DEC-004)."""
    assert isinstance(Renderer, type)
    assert issubclass(Renderer, abc.ABC)


def test_ansi_renderer_subclass_of_renderer(default_config: DiffConfig) -> None:
    """AnsiRenderer derives from the Renderer ABC."""
    renderer = AnsiRenderer(config=default_config)
    assert isinstance(renderer, Renderer)


def test_renderer_abstract_method_raises_when_unimplemented(
    default_config: DiffConfig,
) -> None:
    """A subclass that doesn't override :meth:`Renderer.render` cannot
    be instantiated — Python's ABC machinery raises ``TypeError``.
    """

    class IncompleteRenderer(Renderer):
        # No render() override — should be flagged abstract.
        pass

    with pytest.raises(TypeError, match=r"abstract"):
        IncompleteRenderer()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# 2. Wide-TTY 6-column table.
# ---------------------------------------------------------------------------


def test_wide_tty_table_renders_six_columns(default_config: DiffConfig) -> None:
    """At wide terminal width the table header carries 6 column labels."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=200)
    report = _make_report(
        entries=(
            _kept_entry(),
            _dropped_entry(),
        )
    )
    output = renderer.render(report)
    # The header row carries the WHY label only in wide mode.
    assert "WHY" in output
    # Five fixed labels — TIER, ARTIFACT, TEST, REASON, SCORE.
    for label in ("TIER", "ARTIFACT", "TEST", "REASON", "SCORE"):
        assert label in output


def test_wide_tty_table_emits_each_entry_row(default_config: DiffConfig) -> None:
    """Each row's artifact_id appears in the rendered output."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=200)
    entries = (
        _kept_entry(artifact_id="column.id.description"),
        _dropped_entry(artifact_id="test.column.email.not_null"),
    )
    output = renderer.render(report=_make_report(entries=entries))
    assert "column.id.description" in output
    assert "test.column.email.not_null" in output


def test_wide_tty_header_carries_counts(default_config: DiffConfig) -> None:
    """The header line surfaces kept/dropped/flagged counts."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=200)
    report = _make_report(entries=(_kept_entry(), _dropped_entry()))
    output = renderer.render(report)
    assert "kept=1" in output
    assert "dropped=1" in output
    assert "flagged=0" in output


def test_no_entries_renders_placeholder(default_config: DiffConfig) -> None:
    """When the report has no entries, the table shows a placeholder."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=200)
    output = renderer.render(report=_make_report(entries=()))
    assert "no candidate artifacts" in output


# ---------------------------------------------------------------------------
# 3. Narrow-TTY compact mode (DEC-013).
# ---------------------------------------------------------------------------


def test_narrow_tty_drops_why_column(default_config: DiffConfig) -> None:
    """Below the threshold, the table header omits the WHY label."""
    # Default narrow_terminal_threshold = 60; 40 cols is well below.
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=40)
    report = _make_report(entries=(_kept_entry(why="prose explanation"),))
    output = renderer.render(report)
    # The table header in narrow mode omits the WHY label.
    header_line = next(
        line for line in output.splitlines() if "TIER" in line and "ARTIFACT" in line
    )
    assert "WHY" not in header_line


def test_narrow_tty_emits_follow_up_why_line(default_config: DiffConfig) -> None:
    """The dropped ``why`` re-appears below each row as an indented
    follow-up line in narrow mode (DEC-013)."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=40)
    why_text = "ran on 1k sample, 0 failing rows"
    report = _make_report(entries=(_dropped_entry(why=why_text),))
    output = renderer.render(report)
    assert "└─" in output
    assert why_text in output


def test_narrow_tty_skips_follow_up_for_empty_why(
    default_config: DiffConfig,
) -> None:
    """An entry with empty ``why`` does not get a follow-up line."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=40)
    report = _make_report(entries=(_kept_entry(why=""),))
    output = renderer.render(report)
    assert "└─" not in output


def test_wide_tty_does_not_emit_follow_up_lines(default_config: DiffConfig) -> None:
    """In wide mode the follow-up indented line is suppressed (the
    why is in the row's own cell)."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=200)
    report = _make_report(entries=(_kept_entry(why="hello prose"),))
    output = renderer.render(report)
    assert "└─" not in output


# ---------------------------------------------------------------------------
# 4. Colour-precedence chain (DEC-021).
# ---------------------------------------------------------------------------


def test_color_precedence_respect_no_color_env_false_forces_color_on(
    default_config: DiffConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Position 1: ``respect_no_color_env=False`` beats every other
    signal — colour ON even with NO_COLOR set + no TTY + force_color=False."""
    monkeypatch.setenv("NO_COLOR", "1")
    # `respect_no_color_env=False` MUST win over every lower signal.
    renderer = AnsiRenderer(
        config=default_config,
        respect_no_color_env=False,
        force_color=False,
        terminal_width=200,
    )
    report = _make_report(entries=(_kept_entry(),))
    output = renderer.render(report)
    # The renderer's own SGR codes (e.g. \x1b[32m for the green tier
    # cell) are present.
    assert "\x1b[" in output


def test_color_precedence_force_color_false_beats_force_color_env(
    default_config: DiffConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Position 2: ``force_color=False`` beats FORCE_COLOR env."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    renderer = AnsiRenderer(
        config=default_config,
        force_color=False,
        terminal_width=200,
    )
    output = renderer.render(_make_report(entries=(_kept_entry(),)))
    # No renderer-emitted SGR codes.
    assert "\x1b[" not in output


def test_color_precedence_force_color_true_beats_no_color_env(
    default_config: DiffConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Position 3: ``force_color=True`` beats NO_COLOR env."""
    monkeypatch.setenv("NO_COLOR", "1")
    renderer = AnsiRenderer(
        config=default_config,
        force_color=True,
        terminal_width=200,
    )
    output = renderer.render(_make_report(entries=(_kept_entry(),)))
    assert "\x1b[" in output


def test_color_precedence_force_color_env_beats_no_color_env(
    default_config: DiffConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Position 4: FORCE_COLOR env beats NO_COLOR env (when no
    constructor override pins the answer)."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    renderer = AnsiRenderer(
        config=default_config,
        force_color=None,
        terminal_width=200,
    )
    output = renderer.render(_make_report(entries=(_kept_entry(),)))
    assert "\x1b[" in output


def test_color_precedence_no_color_env_beats_isatty(
    default_config: DiffConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Position 5: NO_COLOR env beats isatty() — colour OFF even when
    stdout claims to be a terminal."""
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")

    # Force isatty() → True via a stub.
    class _ForceTTY:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr("sys.stdout", _ForceTTY())
    renderer = AnsiRenderer(config=default_config, force_color=None, terminal_width=200)
    output = renderer.render(_make_report(entries=(_kept_entry(),)))
    assert "\x1b[" not in output


def test_color_precedence_isatty_is_terminal_signal(
    default_config: DiffConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Position 6: with no env / no kwarg, the renderer follows
    :meth:`sys.stdout.isatty`. ``isatty()=False`` → colour OFF."""
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)

    class _NotTTY:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr("sys.stdout", _NotTTY())
    renderer = AnsiRenderer(config=default_config, force_color=None, terminal_width=200)
    output = renderer.render(_make_report(entries=(_kept_entry(),)))
    assert "\x1b[" not in output


# ---------------------------------------------------------------------------
# 5. Unconditional ANSI stripping (DEC-007).
# ---------------------------------------------------------------------------


def test_strip_ansi_runs_when_color_disabled(default_config: DiffConfig) -> None:
    """A malicious ``why`` carrying SGR codes renders as plain text
    when colour is OFF — strip is unconditional."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=200)
    evil_why = "\x1b[31mGOTCHA\x1b[0m"
    report = _make_report(entries=(_dropped_entry(why=evil_why),))
    output = renderer.render(report)
    assert "GOTCHA" in output
    # The smuggled red SGR is gone, and since color is OFF the
    # renderer adds none of its own — output carries no ESC at all.
    assert "\x1b[" not in output


def test_strip_ansi_runs_when_color_forced_on(default_config: DiffConfig) -> None:
    """The DEC-007 strip runs even when the renderer itself is
    emitting colour codes — the only ESC sequences in the output come
    from the renderer's own code, never from user content."""
    renderer = AnsiRenderer(config=default_config, force_color=True, terminal_width=200)
    evil_why = "\x1b[31mGOTCHA\x1b[0m"
    report = _make_report(entries=(_dropped_entry(why=evil_why),))
    output = renderer.render(report)
    assert "GOTCHA" in output
    # The user's smuggled "\x1b[31m...GOTCHA...\x1b[0m" sequence is
    # gone — verify the literal escape that wraps GOTCHA is absent.
    assert "\x1b[31mGOTCHA" not in output


def test_strip_ansi_runs_on_artifact_id(default_config: DiffConfig) -> None:
    """The ``artifact_id`` user-content field also runs through
    :func:`strip_ansi_escapes` (DEC-007). Adversarial manifest content
    can't smuggle escapes via the dotted-path identifier."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=200)
    evil_id = "column.\x1b[31mevil\x1b[0m.description"
    report = _make_report(entries=(_kept_entry(artifact_id=evil_id),))
    output = renderer.render(report)
    assert "column.evil.description" in output
    assert "\x1b[" not in output


def test_strip_ansi_runs_on_unified_diff_body(default_config: DiffConfig) -> None:
    """The ``unified_diff`` body also runs through ``strip_ansi_escapes``
    line-by-line — manifest YAML + LLM-drafted artifact text are the
    upstream sources, both adversarial-content-bearing."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=200)
    diff_body = "+ \x1b[31mevil\x1b[0m: nope\n- safe: yes"
    report = _make_report(unified_diff=diff_body)
    output = renderer.render(report)
    assert "+ evil: nope" in output
    assert "- safe: yes" in output
    assert "\x1b[" not in output


def test_strip_ansi_runs_on_narrow_follow_up_why(
    default_config: DiffConfig,
) -> None:
    """The narrow-mode follow-up line also strips user-content
    escapes — same DEC-007 strip applies there as in the wide-mode
    cell."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=40)
    evil_why = "\x1b[31mEVIL_FOLLOW_UP\x1b[0m"
    report = _make_report(entries=(_dropped_entry(why=evil_why),))
    output = renderer.render(report)
    assert "EVIL_FOLLOW_UP" in output
    assert "\x1b[" not in output


# ---------------------------------------------------------------------------
# 6. Smaller polish — diff section, score formatting, model id strip.
# ---------------------------------------------------------------------------


def test_empty_diff_emits_identical_marker(default_config: DiffConfig) -> None:
    """When ``unified_diff`` is empty, the diff section emits a
    placeholder explaining the schemas are identical."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=200)
    output = renderer.render(_make_report(unified_diff=""))
    assert "identical" in output


def test_score_none_renders_em_dash(default_config: DiffConfig) -> None:
    """An entry with ``score=None`` renders an em-dash in the SCORE
    column rather than a numeric formatter — the column should never
    fail when the grade layer reported a graceful-degrade null."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=200)
    entry = _kept_entry(score=None)
    output = renderer.render(_make_report(entries=(entry,)))
    assert "—" in output


def test_model_unique_id_stripped_in_header(default_config: DiffConfig) -> None:
    """``model_unique_id`` runs through ``strip_ansi_escapes`` in
    the header (DEC-007 — every user-content field, not only the
    table cells)."""
    renderer = AnsiRenderer(config=default_config, force_color=False, terminal_width=200)
    evil_model_id = "model.\x1b[31mproj\x1b[0m.customers"
    report = _make_report(model_unique_id=evil_model_id)
    output = renderer.render(report)
    assert "model.proj.customers" in output
    assert "\x1b[31m" not in output


# ---------------------------------------------------------------------------
# MarkdownRenderer tests (US-009).
# ---------------------------------------------------------------------------


def test_markdown_renderer_subclass_of_renderer(default_config: DiffConfig) -> None:
    """MarkdownRenderer derives from the Renderer ABC and ``render``
    returns ``str``."""
    renderer = MarkdownRenderer(config=default_config)
    assert isinstance(renderer, Renderer)
    output = renderer.render(_make_report(entries=(_kept_entry(),)))
    assert isinstance(output, str)


def test_markdown_renderer_emits_pipe_table(default_config: DiffConfig) -> None:
    """The kept/dropped/flagged table is rendered as a GFM pipe-table:
    header + divider + one row per entry."""
    renderer = MarkdownRenderer(config=default_config)
    report = _make_report(entries=(_kept_entry(), _dropped_entry()))
    output = renderer.render(report)
    # Header row.
    assert "| Tier | Artifact | Test | Reason | Score | Why |" in output
    # Divider row — exactly six segments.
    assert "| --- | --- | --- | --- | --- | --- |" in output
    # Each entry's artifact_id appears in a cell.
    assert "column.customer_id.description" in output
    assert "test.column.email.not_null" in output


def test_markdown_renderer_emits_fenced_diff_block(default_config: DiffConfig) -> None:
    """The unified-diff body is wrapped in a ```diff fenced block."""
    renderer = MarkdownRenderer(config=default_config)
    diff_body = "@@ -1,2 +1,2 @@\n-old: value\n+new: value\n"
    report = _make_report(unified_diff=diff_body)
    output = renderer.render(report)
    assert "```diff\n" in output
    assert "@@ -1,2 +1,2 @@" in output
    assert "+new: value" in output
    # The fence closes.
    assert output.rstrip().endswith("```")


def test_markdown_renderer_omits_diff_block_when_empty(default_config: DiffConfig) -> None:
    """An empty unified_diff suppresses the entire ```diff block."""
    renderer = MarkdownRenderer(config=default_config)
    # Provide at least one entry so the table renders for verification
    # alongside the omitted diff block.
    report = _make_report(unified_diff="", entries=(_kept_entry(),))
    output = renderer.render(report)
    assert "```diff" not in output
    # The table still renders.
    assert "| Tier | Artifact" in output


def test_markdown_renderer_escapes_pipe_in_cell(default_config: DiffConfig) -> None:
    """A ``why`` containing a literal pipe is HTML-entity encoded so
    the column count survives (DEC-008 in_table_cell mode)."""
    renderer = MarkdownRenderer(config=default_config)
    why = "left | right"
    entry = _dropped_entry(why=why)
    output = renderer.render(_make_report(entries=(entry,)))
    # The literal pipe is encoded; it must not appear raw in the cell.
    assert "&#124;" in output
    # The table layout was preserved — the row still has six cells
    # (5 separators + 2 outer pipes = 7 `|` characters before the
    # encoded pipe within the cell). Verify by checking the row line.
    row_line = next(line for line in output.splitlines() if "left" in line and "right" in line)
    # Row begins and ends with '|' and has the cell count separator.
    assert row_line.startswith("| ")
    assert row_line.endswith(" |")
    # Six cells = 7 unencoded '|' characters in the row.
    assert row_line.count("|") == 7


def test_markdown_renderer_escapes_backtick_in_cell(default_config: DiffConfig) -> None:
    """A ``why`` containing a backtick is backslash-escaped in the cell
    so it cannot open an inline-code span (DEC-008)."""
    renderer = MarkdownRenderer(config=default_config)
    why = "uses `metric` column"
    entry = _kept_entry(why=why)
    output = renderer.render(_make_report(entries=(entry,)))
    # Backslash-escaped backtick.
    assert "\\`metric\\`" in output


def test_markdown_renderer_fence_passes_through_raw_diff_content(
    default_config: DiffConfig,
) -> None:
    """A diff body containing what would look like a Markdown header
    or a pipe character passes through inside the fenced ```diff block
    untouched. The fence is the defence; markdown-escaping inside the
    fence would ship literal backslashes to the operator."""
    renderer = MarkdownRenderer(config=default_config)
    # Diff content that includes pipes, backticks, and a #-header.
    diff_body = "@@ -1,2 +1,2 @@\n-# old: a | b `c`\n+# new: x | y `z`\n"
    report = _make_report(unified_diff=diff_body)
    output = renderer.render(report)
    # The diff body is rendered raw inside the fence.
    assert "-# old: a | b `c`" in output
    assert "+# new: x | y `z`" in output
    # No HTML entity for pipe inside the fenced block.
    fence_start = output.index("```diff")
    fence_end = output.rindex("```")
    fenced_section = output[fence_start:fence_end]
    assert "&#124;" not in fenced_section


def test_markdown_renderer_table_escapes_diff_like_content(
    default_config: DiffConfig,
) -> None:
    """The same pipe / header content that passes through inside the
    fence is escaped when it appears in a table cell. The table cell
    is the regulated sink; the fence is the unregulated sink."""
    renderer = MarkdownRenderer(config=default_config)
    why = "# heading-shape | with pipe"
    entry = _dropped_entry(why=why)
    output = renderer.render(_make_report(entries=(entry,)))
    # Table cell — pipe encoded.
    assert "&#124;" in output


def test_markdown_renderer_truncates_at_last_complete_hunk(
    default_config: DiffConfig,
) -> None:
    """Body exceeding ``markdown_max_diff_chars`` is truncated at the
    last complete hunk boundary (DEC-005)."""
    # Use a small cap so we can drive the truncation deterministically.
    cfg = DiffConfig(markdown_max_diff_chars=200)
    renderer = MarkdownRenderer(config=cfg)

    # Three hunks of distinct sizes. Hunks 1 + 2 fit inside 200 chars;
    # adding hunk 3 overflows. The truncator should keep hunks 1 + 2
    # only.
    hunk1 = "@@ -1,2 +1,2 @@\n-a: 1\n+a: 2"
    hunk2 = "@@ -10,2 +10,2 @@\n-b: 1\n+b: 2"
    hunk3 = "@@ -20,2 +20,2 @@\n" + ("+long " * 50)
    body = "\n".join([hunk1, hunk2, hunk3])
    assert len(body) > 200  # sanity
    report = _make_report(unified_diff=body)
    output = renderer.render(report)

    # Hunks 1 and 2 should survive.
    assert "+a: 2" in output
    assert "+b: 2" in output
    # Hunk 3 content (the runaway "+long " repetition) should be gone.
    assert "+long +long" not in output
    # Truncation footer present, with a non-zero dropped-line count
    # and the project_dir placeholder reference.
    assert "more lines truncated" in output
    assert "<project_dir>/.signalforge/diff.json" in output


def test_markdown_renderer_truncation_footer_inside_fence(
    default_config: DiffConfig,
) -> None:
    """The truncation footer appears INSIDE the ```diff fenced block."""
    cfg = DiffConfig(markdown_max_diff_chars=120)
    renderer = MarkdownRenderer(config=cfg)
    hunk1 = "@@ -1,2 +1,2 @@\n-a: 1\n+a: 2"
    hunk2 = "@@ -10,2 +10,2 @@\n" + ("+long " * 50)
    body = "\n".join([hunk1, hunk2])
    report = _make_report(unified_diff=body)
    output = renderer.render(report)

    # The closing ``` must come AFTER the footer.
    assert "more lines truncated" in output
    footer_idx = output.index("more lines truncated")
    closing_fence_idx = output.rindex("```")
    assert closing_fence_idx > footer_idx


def test_markdown_renderer_truncation_dropped_line_count(
    default_config: DiffConfig,
) -> None:
    """The footer reports the number of *lines* dropped, not bytes."""
    cfg = DiffConfig(markdown_max_diff_chars=40)
    renderer = MarkdownRenderer(config=cfg)
    # Hunk 1 fits (~25 chars); hunk 2 has 5 lines that get dropped.
    hunk1 = "@@ -1,2 +1,2 @@\n-a: 1\n+a: 2"
    # 5 dropped lines: header + 4 content lines.
    hunk2 = "@@ -10,4 +10,4 @@\n-b: 1\n-c: 1\n+b: 2\n+c: 2"
    body = "\n".join([hunk1, hunk2])
    report = _make_report(unified_diff=body)
    output = renderer.render(report)
    # Look for the bracket "(N more lines truncated" — N must be > 0.
    import re

    match = re.search(r"\((\d+) more lines truncated", output)
    assert match is not None
    dropped = int(match.group(1))
    assert dropped > 0


def test_markdown_renderer_no_truncation_when_under_cap(
    default_config: DiffConfig,
) -> None:
    """A small unified_diff renders without a truncation footer."""
    renderer = MarkdownRenderer(config=default_config)
    body = "@@ -1,2 +1,2 @@\n-old: 1\n+new: 2"
    report = _make_report(unified_diff=body)
    output = renderer.render(report)
    assert "more lines truncated" not in output
    assert "+new: 2" in output


def test_markdown_renderer_project_dir_renders_in_footer(
    default_config: DiffConfig,
) -> None:
    """An explicit ``project_dir`` constructor arg renders into the
    truncation footer in place of the placeholder."""
    cfg = DiffConfig(markdown_max_diff_chars=120)
    renderer = MarkdownRenderer(config=cfg, project_dir="/repo/myproj")
    hunk1 = "@@ -1,2 +1,2 @@\n-a: 1\n+a: 2"
    hunk2 = "@@ -10,2 +10,2 @@\n" + ("+long " * 50)
    body = "\n".join([hunk1, hunk2])
    report = _make_report(unified_diff=body)
    output = renderer.render(report)
    assert "/repo/myproj/.signalforge/diff.json" in output
    assert "<project_dir>" not in output


def test_markdown_renderer_strips_ansi_in_table_cell(
    default_config: DiffConfig,
) -> None:
    """User-content escapes are stripped before markdown escaping
    (DEC-007 + DEC-008 compose)."""
    renderer = MarkdownRenderer(config=default_config)
    evil_why = "\x1b[31mEVIL\x1b[0m"
    entry = _dropped_entry(why=evil_why)
    output = renderer.render(_make_report(entries=(entry,)))
    assert "EVIL" in output
    assert "\x1b[31m" not in output


def test_markdown_renderer_strips_ansi_in_diff_body(
    default_config: DiffConfig,
) -> None:
    """ANSI escapes in the unified-diff body are stripped before the
    body is wrapped in the fenced block."""
    renderer = MarkdownRenderer(config=default_config)
    diff_body = "@@ -1,1 +1,1 @@\n-\x1b[31mevil\x1b[0m: 1\n+safe: 1"
    report = _make_report(unified_diff=diff_body)
    output = renderer.render(report)
    assert "-evil: 1" in output
    assert "\x1b[31m" not in output


def test_markdown_renderer_no_entries_emits_placeholder(
    default_config: DiffConfig,
) -> None:
    """When there are no entries, the table renders an italic placeholder
    rather than a malformed empty pipe-table."""
    renderer = MarkdownRenderer(config=default_config)
    output = renderer.render(_make_report(entries=()))
    assert "_(no candidate artifacts)_" in output
    # No table header in the empty-entries case.
    assert "| Tier |" not in output


# ---------------------------------------------------------------------------
# MarkdownRenderer post-QG fixes (4 separate bugs).
# ---------------------------------------------------------------------------


def test_markdown_renderer_dropped_line_count_excludes_trailing_newline() -> None:
    """A unified_diff body ending in ``\\n`` produces a trailing empty
    segment under ``split("\\n")``; the dropped-line count must not
    include it (post-QG fix 3a)."""
    cfg = DiffConfig(markdown_max_diff_chars=200)
    renderer = MarkdownRenderer(config=cfg)
    hunk1 = "@@ -1,2 +1,2 @@\n-a: 1\n+a: 2"
    hunk2 = "@@ -10,2 +10,2 @@\n" + ("+long " * 50)
    # Important: trailing newline mirrors what difflib.unified_diff
    # produces.
    body = "\n".join([hunk1, hunk2]) + "\n"
    report = _make_report(unified_diff=body)
    output = renderer.render(report)

    import re

    match = re.search(r"\((\d+) more lines truncated", output)
    assert match is not None
    dropped = int(match.group(1))
    # The body has hunk1 (3 lines) + hunk2 (1 line; the hunk2 content is
    # all on one line because it's not split by '\n'). Only the kept
    # part of the body is hunk1 (3 lines); dropped = body lines - 3.
    body_lines = body.split("\n")
    if body_lines and body_lines[-1] == "":
        body_lines = body_lines[:-1]
    expected_total = len(body_lines)
    assert dropped == expected_total - 3, (
        f"trailing-newline bug: dropped={dropped}, expected={expected_total - 3}"
    )


def test_markdown_renderer_full_block_within_cap() -> None:
    """The WHOLE rendered diff block (fence + body + footer + closing
    fence) must fit under ``markdown_max_diff_chars`` (post-QG fix 3b).
    Originally only ``clean_body`` was checked, so the post-truncation
    block could exceed the cap by the constant fence + footer overhead.
    """
    cfg = DiffConfig(markdown_max_diff_chars=200)
    renderer = MarkdownRenderer(config=cfg)
    hunk1 = "@@ -1,2 +1,2 @@\n-a: 1\n+a: 2"
    hunk2 = "@@ -10,2 +10,2 @@\n" + ("+long " * 50)
    body = "\n".join([hunk1, hunk2])
    report = _make_report(unified_diff=body)
    output = renderer.render(report)

    # Extract just the fenced block from the rendered output. The
    # block starts at the first ``` and ends at the last ```.
    first_fence = output.index("```")
    last_fence = output.rindex("```")
    rendered_block = output[first_fence : last_fence + 3]
    assert len(rendered_block) <= cfg.markdown_max_diff_chars, (
        f"diff block size {len(rendered_block)} exceeds cap {cfg.markdown_max_diff_chars}"
    )


def test_markdown_renderer_first_hunk_too_big_emits_only_file_headers(
    default_config: DiffConfig,
) -> None:
    """When the first hunk alone exceeds the cap, the renderer must NOT
    emit a mid-hunk character cut (which produces a malformed
    unified-diff body). It emits ONLY the ``---``/``+++`` file-header
    lines followed by the truncation footer (post-QG fix 3c)."""
    # Pick a cap small enough that even the first hunk overflows but
    # the file-header lines + footer fit comfortably.
    cfg = DiffConfig(markdown_max_diff_chars=400)
    renderer = MarkdownRenderer(config=cfg)
    body = (
        "--- a/models/m.yml\n"
        "+++ b/models/m.yml\n"
        "@@ -1,2 +1,2 @@\n" + ("+long content line here\n" * 200)  # huge first hunk
    )
    report = _make_report(unified_diff=body)
    output = renderer.render(report)

    # The truncation footer must be present.
    assert "more lines truncated" in output
    # File-header lines survive.
    assert "--- a/models/m.yml" in output
    assert "+++ b/models/m.yml" in output
    # The hunk header itself was either kept on its own (if it fits) or
    # dropped — but a partial mid-hunk cut would manifest as a
    # truncated content line. Verify no half-emitted hunk content
    # appears: the body between the file-header lines and the footer
    # has no ``+long content`` text.
    fence_open = output.index("```diff")
    footer_idx = output.index("more lines truncated")
    block_pre_footer = output[fence_open:footer_idx]
    assert "+long content" not in block_pre_footer, (
        "first-hunk-too-big fallback emitted a mid-hunk content cut"
    )


def test_markdown_renderer_dynamic_fence_when_body_contains_triple_backticks(
    default_config: DiffConfig,
) -> None:
    """An unchanged YAML line containing ``` (a triple-backtick run)
    must NOT close the outer fenced block prematurely (post-QG fix 3d).
    The renderer dynamically sizes the fence to one more backtick than
    the longest run inside the body."""
    renderer = MarkdownRenderer(config=default_config)
    # Body contains a line that is exactly ``` (CommonMark would treat
    # this as a closing fence inside a 3-backtick fence).
    body = (
        "@@ -1,3 +1,3 @@\n"
        " line 1\n"
        " ```\n"  # this line would close a 3-backtick fence
        " line 3\n"
    )
    report = _make_report(unified_diff=body)
    output = renderer.render(report)

    # The outer fence must be at least 4 backticks (longest run in body
    # is 3, so fence is 4). Look for the first occurrence of a fence
    # followed by ``diff``.
    import re

    fence_match = re.search(r"^(`{4,})diff$", output, re.MULTILINE)
    assert fence_match is not None, "expected a 4+-backtick opening fence; got:\n" + output
    fence = fence_match.group(1)
    # The closing fence must be the same length.
    closing_count = output.count(f"\n{fence}\n") + (1 if output.endswith(fence) else 0)
    assert closing_count >= 1
    # Confirm the body's literal ``` line is preserved verbatim
    # (not consumed by an early fence-close).
    assert " ```" in output


def test_markdown_renderer_dynamic_fence_floor_at_three_backticks(
    default_config: DiffConfig,
) -> None:
    """Body without any backticks → fence floors at 3 backticks (the
    standard ``` ```diff ``` shape). Regression guard against the
    dynamic-fence helper accidentally emitting a 4-fence on
    backtick-free input."""
    renderer = MarkdownRenderer(config=default_config)
    body = "@@ -1,1 +1,1 @@\n-a: 1\n+a: 2"
    report = _make_report(unified_diff=body)
    output = renderer.render(report)
    assert "```diff\n" in output
    # No 4+-backtick fence present.
    assert "````diff" not in output


# ---------------------------------------------------------------------------
# Issue #41 — hostile rationale threaded into DiffEntry.why escapes through
# both DEC-007 (ANSI strip) and DEC-008 (Markdown HTML-entity escape) at
# the table-cell sink.
# ---------------------------------------------------------------------------


def test_kept_row_rationale_with_hostile_content_is_escaped_in_markdown(
    default_config: DiffConfig,
) -> None:
    """Issue #41 acceptance #3 — bead 1 now threads candidate ``rationale``
    into :attr:`DiffEntry.why` for kept rows. A hostile rationale carrying
    HTML close tags, triple-backticks, pipes, and ANSI control sequences
    must still flow through the existing DEC-007 strip + DEC-008 markdown
    escape sinks at the table-cell boundary. Exercised end-to-end via
    :func:`render_to_text` so the production code path is the one under
    test (not a unit-level call to ``escape_markdown_scalar``).
    """
    # Hostile rationale combining all four threat shapes:
    #   * ``</details>`` — HTML close-tag injection (DEC-008 ``<``/``>``)
    #   * triple-backtick — would open a code span / close the outer
    #     fence if it leaked unescaped (DEC-008 backtick escape)
    #   * pipe — terminates a GFM table cell early (DEC-008 in_table_cell
    #     ``&#124;`` encoding)
    #   * ``\x1b[31m...\x1b[0m`` — SGR colour, must be stripped at the
    #     input boundary by DEC-007's unconditional ANSI strip
    hostile_rationale = "</details> ```triple``` | pipe \x1b[31mred\x1b[0m sneak"
    entry = _kept_entry(why=hostile_rationale)
    report = _make_report(entries=(entry,))

    cfg = DiffConfig(render_kind="markdown")
    output = render_to_text(report, config=cfg)

    # DEC-008 — HTML metacharacters in the close-tag are entity-encoded.
    assert "&lt;/details&gt;" in output
    # And the raw close-tag must NOT appear anywhere in the rendered
    # output. (The encoded form contains neither '<' nor '>' so the
    # check is direct.)
    assert "</details>" not in output

    # DEC-008 — pipe inside a table cell is HTML-entity-encoded so the
    # GFM column count survives. The raw pipe still appears as the
    # row's column-separator characters, but the cell content's pipe
    # must be encoded.
    assert "&#124;" in output

    # DEC-008 — triple-backtick is backslash-escaped (each backtick
    # individually), so the rendered cell carries ``\`\`\``` instead
    # of a raw ``` run that could open a code span / close the outer
    # fence. The raw triple-backtick must NOT appear anywhere in the
    # cell. (It could appear inside the fenced diff block, but the
    # report here has no unified_diff, so the only place ``` could
    # appear is the table cell.)
    assert "\\`\\`\\`" in output
    assert "```triple```" not in output

    # DEC-007 — the ANSI SGR sequence is stripped UNCONDITIONALLY
    # before the markdown-escape pass. The plain "red" text survives;
    # the raw ESC bytes do not.
    assert "red" in output
    assert "\x1b[" not in output
    assert "\x1b[31m" not in output

    # Structural invariant — the table row stays rectangular: 6 cells
    # means 7 unencoded ``|`` characters in the row. A leaked raw pipe
    # in the why cell would push the count to 8 and shift columns.
    row_line = next(
        line
        for line in output.splitlines()
        if "kept" in line and "column.customer_id.description" in line
    )
    assert row_line.startswith("| ")
    assert row_line.endswith(" |")
    assert row_line.count("|") == 7


# ---------------------------------------------------------------------------
# Issue #50 — kept-uncertain tier surfaces in every renderer with a
# distinct visual / structural signal.
# ---------------------------------------------------------------------------


def _kept_uncertain_entry(
    *,
    artifact_id: str = "test.column.email.unique",
    why: str = "total prune budget exceeded before evaluation",
) -> DiffEntry:
    return DiffEntry(
        artifact_id=artifact_id,
        test_type="unique",
        tier="kept-uncertain",
        drop_reason=None,
        why=why,
        score=None,
        passed=None,
    )


def test_ansi_renderer_kept_uncertain_uses_cyan_colour(
    default_config: DiffConfig,
) -> None:
    """Issue #50: the ANSI renderer paints kept-uncertain rows cyan
    (``\\x1b[36m``) to distinguish them from the green ``kept`` tier
    (positive prune evidence). Yellow is reserved for ``flagged``
    rows; the cyan choice avoids the palette collision.
    """
    renderer = AnsiRenderer(config=default_config, force_color=True, terminal_width=200)
    output = renderer.render(_make_report(entries=(_kept_uncertain_entry(),)))

    # Header carries the kept-uncertain count painted cyan.
    assert "\x1b[36mkept-uncertain=1\x1b[0m" in output
    # Tier cell is cyan-coloured.
    assert "\x1b[36mkept-uncertain" in output


def test_markdown_renderer_kept_uncertain_renders_distinct_row(
    default_config: DiffConfig,
) -> None:
    """Issue #50: the Markdown renderer surfaces the kept-uncertain
    tier in the summary line AND as a distinct tier-cell value in the
    table row. Markdown carries no colour — the structural distinction
    is the literal tier-cell text plus the per-tier count in the bold
    summary.
    """
    cfg = DiffConfig(render_kind="markdown")
    output = render_to_text(_make_report(entries=(_kept_uncertain_entry(),)), config=cfg)

    assert "**kept-uncertain=1**" in output
    # The tier cell value is the literal Tier literal.
    row_line = next(line for line in output.splitlines() if "test.column.email.unique" in line)
    assert "| kept-uncertain |" in row_line


def test_kept_uncertain_with_hostile_why_is_ansi_stripped(
    default_config: DiffConfig,
) -> None:
    """Issue #50 + DEC-007: even on the new kept-uncertain code path,
    the unconditional ANSI strip runs on the ``why`` field. The strip
    is the security boundary; the colour-precedence chain only governs
    the renderer's own SGR codes.

    A hostile prune ``why`` carrying smuggled SGR bytes cannot survive
    to the rendered output regardless of how the tier dispatches.
    """
    hostile_why = "budget exceeded \x1b[31mEVIL\x1b[0m sneak"
    renderer = AnsiRenderer(config=default_config, force_color=True, terminal_width=200)
    output = renderer.render(_make_report(entries=(_kept_uncertain_entry(why=hostile_why),)))

    # The literal text "EVIL" survives the strip.
    assert "EVIL" in output
    # The smuggled SGR pair does NOT appear as a contiguous run.
    assert "\x1b[31mEVIL\x1b[0m" not in output


def test_json_renderer_serialises_kept_uncertain_tier_literally(
    default_config: DiffConfig,
) -> None:
    """Issue #50: the JSON renderer round-trips ``"kept-uncertain"`` as
    a plain string. The sidecar consumer (a v0.3 GitHub Action) reads
    this literal to gate on the four-tier taxonomy.
    """
    import json as _json

    cfg = DiffConfig(render_kind="json")
    output = render_to_text(_make_report(entries=(_kept_uncertain_entry(),)), config=cfg)

    parsed = _json.loads(output)
    assert parsed["audit_schema_version"] == 3
    assert parsed["kept_uncertain_count"] == 1
    assert parsed["entries"][0]["tier"] == "kept-uncertain"


# ---------------------------------------------------------------------------
# Proposed test files section (issue #116).
# ---------------------------------------------------------------------------


def test_ansi_renderer_omits_test_files_section_when_empty(
    default_config: DiffConfig,
) -> None:
    """No ``proposed test files`` heading when the report carries none."""
    report = _make_report(entries=(_kept_entry(),))
    out = AnsiRenderer(config=default_config, force_color=False).render(report)
    assert "proposed test files" not in out


def test_ansi_renderer_shows_proposed_test_file(default_config: DiffConfig) -> None:
    """The ANSI renderer shows the proposed ``.sql`` path + SQL body."""
    proposed = ProposedTestFile(
        path="tests/customers__total_custom_sql_a1b2c3d4.sql",
        sql="-- signalforge:generated a1b2c3d4\n\nselect * from x where total < 0\n",
    )
    report = _make_report(entries=(_kept_entry(),), proposed_test_files=(proposed,))
    out = AnsiRenderer(config=default_config, force_color=False).render(report)
    assert "proposed test files" in out
    assert "+++ tests/customers__total_custom_sql_a1b2c3d4.sql" in out
    assert "select * from x where total < 0" in out
    assert "-- signalforge:generated a1b2c3d4" in out


def test_ansi_renderer_strips_ansi_from_hostile_sql(default_config: DiffConfig) -> None:
    """DEC-007: SQL body ANSI escapes are stripped unconditionally.

    Render with colour OFF so the renderer emits ZERO SGR codes of its
    own — then any surviving ``\\x1b`` would be a smuggled escape from the
    SQL body. The strip is independent of colour state (the security
    boundary), so the hostile escape is removed but the literal ``EVIL``
    text survives.
    """
    proposed = ProposedTestFile(
        path="tests/m__custom_sql_deadbeef.sql",
        sql="select \x1b[31mEVIL\x1b[0m from x",
    )
    report = _make_report(proposed_test_files=(proposed,))
    out = AnsiRenderer(config=default_config, force_color=False).render(report)
    assert "\x1b" not in out
    assert "select EVIL from x" in out


def test_markdown_renderer_omits_test_files_section_when_empty(
    default_config: DiffConfig,
) -> None:
    report = _make_report(entries=(_kept_entry(),))
    out = MarkdownRenderer(config=default_config).render(report)
    assert "Proposed test files" not in out


def test_markdown_renderer_shows_proposed_test_file(default_config: DiffConfig) -> None:
    """Markdown renderer shows a heading, inline-code path, and fenced sql block."""
    proposed = ProposedTestFile(
        path="tests/customers__total_custom_sql_a1b2c3d4.sql",
        sql="select * from x where total < 0",
    )
    report = _make_report(proposed_test_files=(proposed,))
    out = MarkdownRenderer(config=default_config).render(report)
    assert "## Proposed test files" in out
    assert "### `tests/customers__total_custom_sql_a1b2c3d4.sql`" in out
    assert "```sql" in out
    assert "select * from x where total < 0" in out


def test_markdown_renderer_dynamic_fence_for_backtick_sql(
    default_config: DiffConfig,
) -> None:
    """A SQL body containing a triple-backtick run gets a longer fence."""
    proposed = ProposedTestFile(
        path="tests/m__custom_sql_deadbeef.sql",
        sql="select '```' as marker",
    )
    report = _make_report(proposed_test_files=(proposed,))
    out = MarkdownRenderer(config=default_config).render(report)
    # The opening fence must be longer than the 3-backtick run in the body.
    assert "````sql" in out
    # The body's backticks survive verbatim inside the longer fence.
    assert "select '```' as marker" in out


def test_markdown_renderer_strips_ansi_from_hostile_sql(default_config: DiffConfig) -> None:
    proposed = ProposedTestFile(
        path="tests/m__custom_sql_deadbeef.sql",
        sql="select \x1b[31mEVIL\x1b[0m from x",
    )
    report = _make_report(proposed_test_files=(proposed,))
    out = MarkdownRenderer(config=default_config).render(report)
    assert "\x1b[31m" not in out
    assert "EVIL" in out
