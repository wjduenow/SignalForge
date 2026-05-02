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

from signalforge.diff._renderers import AnsiRenderer, Renderer
from signalforge.diff.config import DiffConfig
from signalforge.diff.models import DiffEntry, DiffReport

# ---------------------------------------------------------------------------
# Test fixtures.
# ---------------------------------------------------------------------------


def _make_report(
    *,
    entries: tuple[DiffEntry, ...] = (),
    unified_diff: str = "",
    model_unique_id: str = "model.proj.customers",
) -> DiffReport:
    """Construct a minimal :class:`DiffReport` for renderer tests."""
    kept = sum(1 for e in entries if e.tier == "kept")
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
        kept_count=kept,
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
