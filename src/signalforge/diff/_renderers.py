"""Renderer ABC + AnsiRenderer for the diff layer (US-008 of issue #8).

Implements the public-facing terminal renderer for a :class:`DiffReport`.
Two concrete sinks ship in v0.1: :class:`AnsiRenderer` (this module,
US-008) and :class:`MarkdownRenderer` (extended into this module by
US-009). The orchestrator (US-008's sibling story US-010) selects the
concrete via :attr:`signalforge.diff.config.DiffConfig.render_kind`.

This module is structured so US-009 can append :class:`MarkdownRenderer`
without restructuring: the :class:`Renderer` ABC sits at the top, the
ANSI concrete follows, and the Markdown concrete will be appended by
its own ticket. Per DEC-004 the concretes themselves are private
(``_renderers``); the public surface in :mod:`signalforge.diff` is
:func:`render_diff` (US-010), not the concrete classes.

Design commitments operationalised here (``plans/super/8-diff-renderer.md``):

* **DEC-007 — strip_ansi_escapes runs UNCONDITIONALLY.**
  ``signalforge.diff._ansi_safety.strip_ansi_escapes`` is invoked on
  every user-content field (``why``, the ``drop_reason`` literal, the
  ``artifact_id``) BEFORE the renderer's own colour codes are emitted.
  The ANSI strip is the security boundary; the colour-precedence
  decision (DEC-021) only governs whether the renderer's own SGR
  sequences are emitted. A malicious manifest field carrying
  ``\\x1b[31mEVIL\\x1b[0m`` renders as the literal text ``EVIL`` even
  when colour is forced ON — defence-in-depth against adversarial
  upstream content.

* **DEC-013 — Narrow-TTY compact mode.** When the effective terminal
  width (the test-injected ``terminal_width`` if present, else
  :func:`shutil.get_terminal_size`) is strictly less than
  :attr:`DiffConfig.narrow_terminal_threshold`, the kept/dropped/flagged
  table drops the ``why`` column from the row layout and emits each
  row's ``why`` as an indented follow-up line below its row. The
  threshold defaults to 60 columns; the snapshot fixture covers a
  40-column case.

* **DEC-021 — Colour-precedence chain (highest first).** The renderer
  emits ANSI SGR codes only when the precedence chain resolves to
  "colour ON". Six signals decide:

  1. ``respect_no_color_env=False`` (constructor or
     :attr:`DiffConfig.respect_no_color_env`) — forces colour ON
     regardless of every other signal. Useful for tests / non-tty
     pipelines that want ANSI output anyway.
  2. ``force_color=False`` (constructor) — forces colour OFF
     regardless of env / TTY. Maps to the CLI's ``--no-color`` flag
     wired by US-009 (#9 CLI ticket).
  3. ``force_color=True`` (constructor) — forces colour ON.
     Distinguishes the two ``force_color`` overrides from the
     ``respect_no_color_env=False`` override above; the former is
     the operator's per-invocation kill-switch, the latter is a
     config-file-level "always colour".
  4. ``FORCE_COLOR`` env var (any non-empty value) — forces colour ON.
  5. ``NO_COLOR`` env var (any value, per https://no-color.org/) —
     forces colour OFF.
  6. ``sys.stdout.isatty()`` — colour ON iff stdout is a terminal.

  The chain is documented on the
  :meth:`AnsiRenderer._should_emit_color` private helper as a sequence
  of early-returns; the order is asymmetric (respect_no_color_env
  beats every other signal because the CLI's documented seam is
  ``--no-color`` mapping to ``force_color=False``, while a YAML config
  knob ``respect_no_color_env: false`` is a more-considered choice
  that should beat both env and TTY).

The renderer does no I/O — it returns the rendered string. The
orchestrator (US-010) writes to stdout; this module does not import
``sys.stdout.write``.

This module also carries no ``_LOGGER`` calls in v0.1. The renderer's
output is the operator's primary signal; observability about the
renderer's own behaviour (cache hits, retries, ...) is reserved for
the orchestrator's WARNING channel.
"""

from __future__ import annotations

import abc
import os
import shutil
import sys
from typing import TYPE_CHECKING

from signalforge.diff._ansi_safety import strip_ansi_escapes
from signalforge.diff._markdown_safety import escape_markdown_scalar
from signalforge.diff.config import DiffConfig

if TYPE_CHECKING:
    from signalforge.diff.models import DiffEntry, DiffReport


# ---------------------------------------------------------------------------
# ANSI SGR sequences — internal-only constants used by AnsiRenderer.
# ---------------------------------------------------------------------------

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_CYAN = "\x1b[36m"
_DIM = "\x1b[2m"


# ---------------------------------------------------------------------------
# Renderer ABC.
# ---------------------------------------------------------------------------


class Renderer(abc.ABC):
    """Abstract base for diff renderers.

    Two concretes ship in v0.1: :class:`AnsiRenderer` (this module,
    US-008) and :class:`MarkdownRenderer` (US-009; appended into this
    module). A third (``JsonRenderer``) lands with US-010 alongside the
    orchestrator. New renderers in v0.2 (HTML, plain-text, ...) subclass
    this ABC.

    The contract is intentionally minimal: one method,
    :meth:`render`, takes a :class:`signalforge.diff.models.DiffReport`
    and returns the rendered text. Renderers do no I/O — the
    orchestrator owns stdout; the sidecar writer owns disk. Returning
    a string keeps the renderer testable as a pure function.
    """

    @abc.abstractmethod
    def render(self, report: DiffReport) -> str:
        """Render ``report`` to renderer-specific text.

        Subclasses must produce a string suitable for the target sink
        (terminal, Markdown viewer, JSON consumer). The string is the
        whole rendered output — no trailing newline normalisation; the
        orchestrator decides whether to add one when piping to stdout.
        """
        raise NotImplementedError  # pragma: no cover — ABC contract


# ---------------------------------------------------------------------------
# AnsiRenderer — terminal-targeted concrete (US-008).
# ---------------------------------------------------------------------------


# Column widths for the wide-TTY 6-column table (header + per-row).
# Tuned so a 80-col terminal leaves ~26 cols for the "why" cell, which
# fits the default `DiffConfig.max_why_chars=80` truncation cap with a
# small visual margin. Narrow mode drops the why cell and re-flows the
# remaining 5 columns into the available width.
#
# Issue #50: ``_COL_TIER`` widened from 8 → 14 so the new
# ``kept-uncertain`` literal (14 chars) fits without truncation. The
# existing labels (``kept`` / ``dropped`` / ``flagged``) fit
# comfortably inside the wider cell.
_COL_TIER = 14
_COL_ARTIFACT = 28
_COL_TEST_TYPE = 14
_COL_DROP_REASON = 22
_COL_SCORE = 7
# `why` column width derived from terminal width at render time.


def _truncate(text: str, max_chars: int) -> str:
    """Truncate ``text`` to ``max_chars`` with an ellipsis if longer.

    Keeps the table readable at any terminal width. Mirrors
    :attr:`DiffConfig.max_why_chars` (default 80) for the prose ``why``
    cell. Empty string and short strings pass through unchanged.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1] + "…"


def _pad(text: str, width: int) -> str:
    """Left-pad ``text`` with spaces to ``width`` characters.

    Truncates with an ellipsis if longer (so the table layout never
    breaks). Used for fixed-width column layout in the table body.
    """
    if width <= 0:
        return ""
    truncated = _truncate(text, width)
    if len(truncated) >= width:
        return truncated[:width]
    return truncated + " " * (width - len(truncated))


class AnsiRenderer(Renderer):
    """Render a :class:`DiffReport` to terminal-targeted text (US-008).

    Wide-TTY mode emits a 6-column kept/dropped/flagged table followed
    by the unified-diff body. Narrow-TTY mode (DEC-013) drops the
    ``why`` column from the table and emits each row's ``why`` as an
    indented follow-up line below its row.

    Colour-precedence (DEC-021, highest first):

    1. ``respect_no_color_env=False`` (constructor; defaults to the
       :attr:`DiffConfig.respect_no_color_env` value) — forces colour
       ON regardless of all other signals.
    2. ``force_color=False`` (constructor) — forces colour OFF.
    3. ``force_color=True`` (constructor) — forces colour ON.
    4. ``FORCE_COLOR`` env var (non-empty) — forces colour ON.
    5. ``NO_COLOR`` env var (any value) — forces colour OFF.
    6. ``sys.stdout.isatty()`` — colour ON iff stdout is a TTY.

    DEC-007 — :func:`signalforge.diff._ansi_safety.strip_ansi_escapes`
    runs UNCONDITIONALLY on every user-content field (``why``,
    ``artifact_id``, ``drop_reason`` text) BEFORE the renderer's own
    colour codes are emitted. The chain above only decides whether the
    renderer's own SGR sequences ship; user content is always
    sanitised.

    Args:
        config: The :class:`DiffConfig` knob block. Used for the
            ``narrow_terminal_threshold`` (DEC-013), ``max_why_chars``
            truncation cap, and the default ``respect_no_color_env``
            value (overridable via the constructor kwarg).
        force_color: Three-state colour override (DEC-021 chain
            positions 2 and 3). ``True`` forces ANSI codes on,
            ``False`` forces them off, ``None`` defers to env / TTY.
            The CLI (#9) maps ``--no-color`` to ``force_color=False``.
        respect_no_color_env: When ``False``, forces colour ON
            regardless of env / TTY (DEC-021 chain position 1). When
            ``None`` (the default), inherits from
            :attr:`DiffConfig.respect_no_color_env`. The kwarg exists
            so a caller (CLI / test) can override the config value
            without rebuilding a :class:`DiffConfig`.
        terminal_width: Optional terminal-width override for the
            narrow-TTY compact mode (DEC-013). When ``None`` (the
            default), the renderer reads :func:`shutil.get_terminal_size`
            at render time. Tests inject a fixed value to exercise the
            compact-mode branch deterministically.
    """

    def __init__(
        self,
        *,
        config: DiffConfig,
        force_color: bool | None = None,
        respect_no_color_env: bool | None = None,
        terminal_width: int | None = None,
    ) -> None:
        self._config = config
        self._force_color = force_color
        # The constructor kwarg overrides the config value if explicitly
        # provided; otherwise inherit from the config (DEC-021 chain
        # position 1).
        self._respect_no_color_env = (
            respect_no_color_env
            if respect_no_color_env is not None
            else config.respect_no_color_env
        )
        self._terminal_width = terminal_width

    def render(self, report: DiffReport) -> str:
        """Render ``report`` to terminal-targeted text.

        Output structure:

        1. Header line — model_unique_id + count summary.
        2. Kept/dropped/flagged table — wide-TTY 6 columns OR narrow-
           TTY 5 columns + indented follow-up ``why`` lines.
        3. Blank line.
        4. Unified-diff section — the report's ``unified_diff`` body
           rendered verbatim (DEC-007 strip on each line; the renderer
           does NOT colourise the diff body itself in v0.1).

        See the class docstring for colour-precedence (DEC-021) and
        narrow-TTY (DEC-013) semantics.
        """
        emit_color = self._should_emit_color()
        width = self._effective_width()
        narrow = width < self._config.narrow_terminal_threshold

        chunks: list[str] = []
        chunks.append(self._render_header(report, emit_color=emit_color))
        chunks.append("")
        chunks.append(self._render_table(report, narrow=narrow, emit_color=emit_color))
        chunks.append("")
        chunks.append(self._render_diff_section(report))
        return "\n".join(chunks)

    # ------------------------------------------------------------------
    # Colour-precedence chain (DEC-021).
    # ------------------------------------------------------------------

    def _should_emit_color(self) -> bool:
        """Resolve the DEC-021 colour-precedence chain.

        Six early-returns, highest-priority signal first. See the class
        docstring for the rationale on the asymmetric ordering between
        ``respect_no_color_env`` (highest) and the ``force_color``
        kwarg (second).
        """
        # Position 1 — config-level "always colour" override.
        if self._respect_no_color_env is False:
            return True

        # Positions 2 + 3 — explicit constructor kwarg.
        if self._force_color is True:
            return True
        if self._force_color is False:
            return False

        # Position 4 — FORCE_COLOR env (any non-empty value).
        if os.environ.get("FORCE_COLOR"):
            return True

        # Position 5 — NO_COLOR env (any value, per the spec).
        if "NO_COLOR" in os.environ:
            return False

        # Position 6 — TTY detection. Some environments stub stdout
        # without `isatty`; fall back to "no colour" rather than crash.
        try:
            return sys.stdout.isatty()
        except (AttributeError, ValueError):  # pragma: no cover — defensive
            return False

    # ------------------------------------------------------------------
    # Terminal-width detection (DEC-013).
    # ------------------------------------------------------------------

    def _effective_width(self) -> int:
        """Return the effective terminal width in columns.

        Test-injected ``terminal_width`` wins; otherwise read
        :func:`shutil.get_terminal_size`. The fallback width when the
        terminal-size lookup fails is ``shutil``'s own 80-column
        default — wide enough that the renderer's default behaviour is
        the wide-TTY layout.
        """
        if self._terminal_width is not None:
            return self._terminal_width
        return shutil.get_terminal_size().columns

    # ------------------------------------------------------------------
    # Rendering helpers.
    # ------------------------------------------------------------------

    def _color(self, text: str, code: str, *, emit_color: bool) -> str:
        """Wrap ``text`` in the renderer's own SGR ``code`` if colour
        is on; otherwise return ``text`` unchanged. The renderer's own
        codes are the only ANSI escapes that ship in the rendered
        output — user-content escapes are stripped at the input
        boundary by :func:`strip_ansi_escapes` (DEC-007).
        """
        if not emit_color:
            return text
        return f"{code}{text}{_RESET}"

    def _render_header(self, report: DiffReport, *, emit_color: bool) -> str:
        """Render the one-line header above the table.

        Surfaces the model under render and the three count aggregates.
        ``model_unique_id`` runs through :func:`strip_ansi_escapes`
        UNCONDITIONALLY (DEC-007) — even though manifest unique_ids
        rarely carry escapes, the input boundary is the same defence
        as the prose ``why`` cell.
        """
        clean_id = strip_ansi_escapes(report.model_unique_id)
        title = self._color(f"diff: {clean_id}", _BOLD, emit_color=emit_color)
        kept = self._color(f"kept={report.kept_count}", _GREEN, emit_color=emit_color)
        kept_uncertain = self._color(
            f"kept-uncertain={report.kept_uncertain_count}",
            _CYAN,
            emit_color=emit_color,
        )
        dropped = self._color(f"dropped={report.dropped_count}", _RED, emit_color=emit_color)
        flagged = self._color(f"flagged={report.flagged_count}", _YELLOW, emit_color=emit_color)
        return f"{title}  {kept}  {kept_uncertain}  {dropped}  {flagged}"

    def _render_table(self, report: DiffReport, *, narrow: bool, emit_color: bool) -> str:
        """Render the kept/dropped/flagged table.

        Wide mode: 6 columns (tier, artifact_id, test_type, drop_reason,
        score, why). Narrow mode (DEC-013): 5 columns (drops ``why``)
        with each ``why`` emitted as an indented follow-up line below
        its row.
        """
        if not report.entries:
            return self._color("(no candidate artifacts)", _DIM, emit_color=emit_color)

        lines: list[str] = []
        lines.append(self._render_table_header(narrow=narrow, emit_color=emit_color))
        for entry in report.entries:
            lines.append(self._render_table_row(entry, narrow=narrow, emit_color=emit_color))
            if narrow:
                follow = self._render_follow_up_why(entry)
                if follow:
                    lines.append(follow)
        return "\n".join(lines)

    def _render_table_header(self, *, narrow: bool, emit_color: bool) -> str:
        """Render the column-header row of the table.

        Static labels — no user-content interpolation, so no
        :func:`strip_ansi_escapes` needed here. The colour wrap on the
        whole line is purely cosmetic.
        """
        cells = [
            _pad("TIER", _COL_TIER),
            _pad("ARTIFACT", _COL_ARTIFACT),
            _pad("TEST", _COL_TEST_TYPE),
            _pad("REASON", _COL_DROP_REASON),
            _pad("SCORE", _COL_SCORE),
        ]
        if not narrow:
            cells.append("WHY")
        line = "  ".join(cells)
        return self._color(line, _BOLD, emit_color=emit_color)

    def _render_table_row(self, entry: DiffEntry, *, narrow: bool, emit_color: bool) -> str:
        """Render one row of the table.

        Every user-content cell — ``artifact_id``, the prose ``why``,
        the ``drop_reason`` literal — runs through
        :func:`strip_ansi_escapes` UNCONDITIONALLY (DEC-007) BEFORE the
        renderer's own colour codes are added. The strip is the
        security boundary; the colour is cosmetic.
        """
        # User-content fields — strip ANSI escapes UNCONDITIONALLY
        # (DEC-007). The strip is independent of `emit_color`.
        clean_artifact = strip_ansi_escapes(entry.artifact_id)
        clean_why = strip_ansi_escapes(entry.why)
        clean_drop_reason = strip_ansi_escapes(entry.drop_reason) if entry.drop_reason else ""
        clean_test_type = strip_ansi_escapes(entry.test_type or "")

        # Tier cell — coloured per-tier via the renderer's own codes.
        # Issue #50: ``kept-uncertain`` paints cyan to distinguish it
        # from the green ``kept`` tier (positive prune evidence).
        tier_colour = {
            "kept": _GREEN,
            "kept-uncertain": _CYAN,
            "dropped": _RED,
            "flagged": _YELLOW,
        }.get(entry.tier, _RESET)
        tier_cell = self._color(_pad(entry.tier, _COL_TIER), tier_colour, emit_color=emit_color)

        score_text = "—" if entry.score is None else f"{entry.score:.2f}"

        cells = [
            tier_cell,
            _pad(clean_artifact, _COL_ARTIFACT),
            _pad(clean_test_type, _COL_TEST_TYPE),
            _pad(clean_drop_reason, _COL_DROP_REASON),
            _pad(score_text, _COL_SCORE),
        ]
        if not narrow:
            why_cell = _truncate(clean_why, self._config.max_why_chars)
            cells.append(why_cell)
        return "  ".join(cells)

    def _render_follow_up_why(self, entry: DiffEntry) -> str:
        """Render the indented follow-up ``why`` line for narrow mode.

        Only emitted in narrow-TTY compact mode (DEC-013). The
        ``why`` is stripped via :func:`strip_ansi_escapes` and
        truncated at :attr:`DiffConfig.max_why_chars`. Empty
        ``why`` → empty string (the caller suppresses the blank line).
        """
        clean_why = strip_ansi_escapes(entry.why)
        if not clean_why:
            return ""
        truncated = _truncate(clean_why, self._config.max_why_chars)
        return f"    └─ {truncated}"

    def _render_diff_section(self, report: DiffReport) -> str:
        """Render the unified-diff body section.

        The diff body itself is upstream-controlled (it embeds YAML
        from manifest fields and LLM-drafted artifact text). Every
        line runs through :func:`strip_ansi_escapes` UNCONDITIONALLY
        (DEC-007). The renderer does NOT colourise the diff body in
        v0.1 — colourising unified-diff hunks is a v0.2 enhancement.
        """
        body = report.unified_diff
        if not body:
            return "(no diff — proposed and existing schemas are identical)"
        # Strip ANSI from every line (DEC-007). The body may be a
        # multi-line string; splitlines + rejoin preserves the
        # line-by-line structure without coercing line endings.
        clean_lines = [strip_ansi_escapes(line) for line in body.splitlines()]
        return "\n".join(clean_lines)


# ---------------------------------------------------------------------------
# MarkdownRenderer — GitHub-flavored Markdown concrete (US-009).
# ---------------------------------------------------------------------------


# DEC-005 fallback. The locked v0.1 default is also held on
# :attr:`DiffConfig.markdown_max_diff_chars` (US-003). We mirror it here
# as a fallback constant only because :class:`DiffConfig` is the source
# of truth — if a future refactor drops the field, the renderer should
# refuse to silently render an unbounded body.
_DEFAULT_MARKDOWN_MAX_DIFF_CHARS = 60_000


def _longest_backtick_run(text: str) -> int:
    """Return the length of the longest consecutive ````` run in ``text``.

    Used to size the dynamic fence (post-QG fix): a static
    triple-backtick fence can be terminated prematurely by an
    unchanged YAML line in the diff body that itself contains exactly
    three consecutive backticks. The fence emitted by the renderer
    must be longer than any backtick run inside the body.

    ``text`` containing no backticks → ``0`` (the renderer floors at
    a 3-char fence). Empty string → ``0``.
    """
    longest = 0
    current = 0
    for ch in text:
        if ch == "`":
            current += 1
            if current > longest:
                longest = current
        else:
            current = 0
    return longest


class MarkdownRenderer(Renderer):
    """Render a :class:`DiffReport` to GitHub-flavored Markdown (US-009).

    Output structure:

    1. ``# Diff: <model_unique_id>`` heading (escaped via
       :func:`escape_markdown_scalar`; ANSI escapes from upstream
       content stripped per DEC-007).
    2. Count summary line (``kept=… dropped=… flagged=…``).
    3. GFM pipe-table of kept/dropped/flagged entries. Every cell value
       runs through
       :func:`signalforge.diff._markdown_safety.escape_markdown_scalar`
       with ``in_table_cell=True`` (DEC-008): backticks /
       backslashes / pipes / row-breaking control characters are
       neutralised so a ``why`` containing ``|`` cannot terminate the
       cell early or shift subsequent columns.
    4. Fenced ``` ```diff ``` block carrying the unified diff verbatim.
       The diff body is **not** Markdown-escaped — the fence is the
       defence; backticks / pipes / HTML in the body are inert inside
       a fenced block.

    DEC-005 — The fenced block is hard-capped at
    ``config.markdown_max_diff_chars`` (default 60 000 — leaves
    head-room under GitHub's 65 536-char comment cap). When the body
    would exceed the cap, the renderer truncates at the **last complete
    hunk boundary** (a hunk starts with ``@@`` and runs until the next
    ``@@`` or EOF) and appends a footer of the form::

        ... (N more lines truncated — see <project_dir>/.signalforge/diff.json for full diff)

    The footer lives **inside** the fenced ``` ```diff ``` block so the
    whole rendered Markdown remains a single well-formed code-block
    boundary. ``N`` counts the dropped lines (not bytes) so a reader
    can immediately gauge the magnitude of what's missing.

    DEC-007 — :func:`signalforge.diff._ansi_safety.strip_ansi_escapes`
    runs UNCONDITIONALLY on every user-content field (``model_unique_id``,
    ``artifact_id``, ``why``, ``drop_reason``, ``test_type``, and the
    unified-diff body) BEFORE markdown-escaping or fence-wrapping.
    The two passes compose: strip first (defence against smuggled SGR),
    escape second (defence against smuggled markdown structure).

    Args:
        config: The :class:`DiffConfig` knob block. Used for
            :attr:`DiffConfig.markdown_max_diff_chars` (DEC-005) and
            :attr:`DiffConfig.max_why_chars` (truncation cap on the
            ``why`` cell).
        project_dir: Optional explicit project directory rendered into
            the truncation footer. The diff layer's
            :class:`signalforge.diff.models.DiffReport` does not carry
            ``project_dir`` (it's an orchestrator-level value), so the
            renderer accepts it here. When ``None`` (the default), the
            footer renders the literal placeholder
            ``<project_dir>/.signalforge/diff.json`` — the operator
            still understands the path, and the placeholder is stable
            across snapshots.
    """

    def __init__(
        self,
        *,
        config: DiffConfig,
        project_dir: str | None = None,
    ) -> None:
        self._config = config
        self._project_dir = project_dir

    def render(self, report: DiffReport) -> str:
        """Render ``report`` to a GitHub-flavored Markdown string.

        See the class docstring for the four-section structure
        (heading + summary + pipe-table + fenced diff) and the DEC-005
        truncation contract.
        """
        chunks: list[str] = []
        chunks.append(self._render_heading(report))
        chunks.append("")
        chunks.append(self._render_summary(report))
        chunks.append("")
        chunks.append(self._render_table(report))

        diff_block = self._render_diff_block(report)
        if diff_block:
            chunks.append("")
            chunks.append(diff_block)

        return "\n".join(chunks)

    # ------------------------------------------------------------------
    # Section helpers.
    # ------------------------------------------------------------------

    def _render_heading(self, report: DiffReport) -> str:
        """Render the ``# Diff: <model_unique_id>`` heading.

        ``model_unique_id`` is upstream-controlled (manifest field), so
        the strip-then-escape composition applies. Outside a table cell
        the escaper uses the non-table-cell mode so a manifest id
        containing ``|`` becomes ``\\|`` rather than the HTML entity.
        """
        clean_id = strip_ansi_escapes(report.model_unique_id)
        escaped_id = escape_markdown_scalar(clean_id, in_table_cell=False)
        return f"# Diff: {escaped_id}"

    def _render_summary(self, report: DiffReport) -> str:
        """Render the per-tier count summary line.

        Static integer counts — no user content, no escaping required.
        Bold via ``**…**`` so the counts stand out in a Markdown viewer.
        """
        return (
            f"**kept={report.kept_count}**  "
            f"**kept-uncertain={report.kept_uncertain_count}**  "
            f"**dropped={report.dropped_count}**  "
            f"**flagged={report.flagged_count}**"
        )

    def _render_table(self, report: DiffReport) -> str:
        """Render the kept/dropped/flagged entries as a GFM pipe-table.

        The header row, divider row, and every data cell follow the
        DEC-008 escaping contract (``escape_markdown_scalar`` with
        ``in_table_cell=True``). When the report has no entries the
        function emits an italic placeholder line instead of an empty
        table — an empty table renders as a syntax-error in some
        Markdown dialects.
        """
        if not report.entries:
            return "_(no candidate artifacts)_"

        header_cells = ("Tier", "Artifact", "Test", "Reason", "Score", "Why")
        divider_cells = ("---",) * len(header_cells)
        lines: list[str] = []
        lines.append("| " + " | ".join(header_cells) + " |")
        lines.append("| " + " | ".join(divider_cells) + " |")
        for entry in report.entries:
            lines.append(self._render_table_row(entry))
        return "\n".join(lines)

    def _render_table_row(self, entry: DiffEntry) -> str:
        """Render a single GFM table row for ``entry``.

        Strip-then-escape applies to every user-content cell. ``why``
        is truncated at :attr:`DiffConfig.max_why_chars` *before* the
        markdown escape so the per-row width stays bounded; the escape
        adds at most a small constant overhead (entity encodings) on
        top of the truncated form.
        """
        clean_artifact = strip_ansi_escapes(entry.artifact_id)
        clean_test_type = strip_ansi_escapes(entry.test_type or "")
        clean_drop_reason = strip_ansi_escapes(entry.drop_reason or "")
        clean_why_full = strip_ansi_escapes(entry.why)
        clean_why = _truncate(clean_why_full, self._config.max_why_chars)

        score_text = "—" if entry.score is None else f"{entry.score:.2f}"

        cells = (
            escape_markdown_scalar(entry.tier, in_table_cell=True),
            escape_markdown_scalar(clean_artifact, in_table_cell=True),
            escape_markdown_scalar(clean_test_type, in_table_cell=True),
            escape_markdown_scalar(clean_drop_reason, in_table_cell=True),
            escape_markdown_scalar(score_text, in_table_cell=True),
            escape_markdown_scalar(clean_why, in_table_cell=True),
        )
        return "| " + " | ".join(cells) + " |"

    # ------------------------------------------------------------------
    # Fenced diff block + DEC-005 truncation.
    # ------------------------------------------------------------------

    def _render_diff_block(self, report: DiffReport) -> str:
        """Render the fenced ``` ```diff ``` block, applying DEC-005
        truncation if the body exceeds
        :attr:`DiffConfig.markdown_max_diff_chars`.

        The diff body is upstream-controlled (it embeds YAML from the
        existing schema and LLM-drafted artifact text). DEC-007 strip
        applies line-by-line; the fence itself protects the body from
        Markdown structural injection (backticks / pipes inside a
        fenced block are inert).

        The fence length is chosen dynamically (post-QG fix). A static
        triple-backtick fence can be closed prematurely by an
        unchanged YAML line that itself contains exactly three
        consecutive backticks. The renderer scans the body for the
        longest run of consecutive backticks and emits an opening
        fence with one extra backtick (CommonMark allows fences of
        3+ backticks; the closing fence must be the same length or
        longer). The closing fence matches the opening length.

        Returns the empty string when the report's ``unified_diff`` is
        empty — the caller suppresses the leading blank line and the
        rendered output omits the diff block entirely.
        """
        body = report.unified_diff
        if not body:
            return ""

        # DEC-007 strip applied line-by-line, identical to AnsiRenderer.
        clean_lines = [strip_ansi_escapes(line) for line in body.splitlines()]
        clean_body = "\n".join(clean_lines)

        # Fence length must exceed the longest consecutive-backtick run
        # in the body so a YAML payload containing ``` cannot close the
        # outer fence early.
        fence = "`" * max(3, _longest_backtick_run(clean_body) + 1)
        opening = f"{fence}diff\n"
        closing = f"\n{fence}"

        limit = self._markdown_max_diff_chars()
        # DEC-005 cap applies to the WHOLE rendered diff block, not just
        # ``clean_body``. Subtract the opening fence + language tag,
        # the leading newline of the closing fence, and the closing
        # fence itself before deriving the body budget. Without this
        # subtraction the post-truncation block can exceed the cap by
        # the constant overhead of fence + footer.
        # ``opening`` already carries the trailing newline; ``closing``
        # carries its leading newline; the rendered shape is
        # ``opening + body + closing`` (no extra separators).
        envelope_overhead = len(opening) + len(closing)
        body_budget = limit - envelope_overhead
        if len(clean_body) <= body_budget:
            return f"{opening}{clean_body}{closing}"

        # The truncator also has to leave room for the footer line
        # itself plus a trailing newline that separates the footer
        # from the closing fence.
        footer_overhead = len(self._truncation_footer(0)) + 1  # +1 for "\n"
        truncated_body, dropped_line_count = self._truncate_at_last_hunk(
            clean_body, body_budget - footer_overhead
        )
        footer = self._truncation_footer(dropped_line_count)
        # The footer lives INSIDE the fenced block (DEC-005). One trailing
        # newline before the footer keeps the rendered Markdown tidy
        # regardless of whether the truncated body ends in a newline.
        if truncated_body and not truncated_body.endswith("\n"):
            truncated_body += "\n"
        return f"{opening}{truncated_body}{footer}{closing}"

    def _markdown_max_diff_chars(self) -> int:
        """Return the active markdown body cap (DEC-005).

        Reads :attr:`DiffConfig.markdown_max_diff_chars`. Falls back to
        the module-level :data:`_DEFAULT_MARKDOWN_MAX_DIFF_CHARS` when
        the config field is somehow missing — a defence against a
        future refactor accidentally dropping the field.
        """
        # TODO: drop the fallback once US-003's `markdown_max_diff_chars`
        # field is locked permanent on `DiffConfig`. Today (2026-05-02)
        # the field exists; the fallback is paranoia, not need.
        return getattr(self._config, "markdown_max_diff_chars", _DEFAULT_MARKDOWN_MAX_DIFF_CHARS)

    def _truncate_at_last_hunk(self, body: str, limit: int) -> tuple[str, int]:
        """Truncate ``body`` to the last complete hunk that fits in ``limit`` chars.

        A hunk starts with ``@@`` (typical unified-diff hunk header) and
        runs until the next ``@@`` or EOF. The truncation finds the
        latest hunk-start whose end position is ``<= limit``; everything
        beyond that point is dropped.

        Two edge cases the post-QG fix handles deliberately:

        * **Body ending with a trailing newline.** ``body.split("\\n")``
          on a string ending in ``\\n`` produces a trailing empty
          segment. The original line-count derivation included that
          empty segment, off-by-one'ing the ``dropped_line_count``
          rendered into the footer. The fix strips the trailing empty
          segment before counting.

        * **First hunk already exceeds ``limit``.** Falling back to a
          mid-hunk character cut produces a malformed unified-diff
          body (a half-emitted ``@@`` header or a torn context line)
          that breaks downstream tooling (Recce, GitHub's diff viewer).
          The fix emits ONLY the file-header lines (``---``/``+++``)
          followed by no hunk content; the truncation footer then
          carries the dropped-line count and the operator follows the
          footer link to the sidecar JSON for the full diff.

        Returns:
            ``(truncated_body, dropped_line_count)``. The truncated
            body never ends with a partial hunk header. The dropped
            line count is computed against the original line count of
            the input body, with trailing empty segments excluded.
        """
        original_lines = body.split("\n")
        # ``body.split("\n")`` produces a trailing empty segment when
        # ``body`` ends with ``"\n"``; strip it before counting so the
        # footer's ``N more lines truncated`` matches the operator's
        # mental model of "lines of content".
        if original_lines and original_lines[-1] == "":
            original_lines = original_lines[:-1]
        original_line_count = len(original_lines)

        # Find the byte-offset of every "@@" hunk header. We treat any
        # line that begins with `@@` as a hunk start (matches the
        # difflib output shape; `git`-style `@@ -1,4 +1,5 @@` and the
        # bare-`@@` markers difflib emits both qualify).
        hunk_starts: list[int] = []
        offset = 0
        for line in original_lines:
            if line.startswith("@@"):
                hunk_starts.append(offset)
            offset += len(line) + 1  # +1 for the rejoining '\n'.

        if not hunk_starts:
            # No hunks — line-boundary cut.
            return self._truncate_at_line_boundary(body, limit, original_line_count)

        # Find the latest hunk-start such that the slice [0:next_hunk_start)
        # (or [0:end) if it's the last hunk) is <= limit. We want the
        # previous hunk's end, which is `next_hunk_start_offset` for the
        # next hunk (or len(body) for the last one).
        boundaries = [*hunk_starts[1:], len(body)]
        chosen_end = -1
        for boundary in boundaries:
            if boundary <= limit:
                chosen_end = boundary
            else:
                break

        if chosen_end <= 0:
            # Even the first hunk is too big — emit ONLY the file
            # header lines (``---``, ``+++``) so the resulting body
            # remains a valid (albeit content-free) unified-diff
            # prefix. The line-boundary fallback would emit a
            # mid-hunk character cut and break downstream parsers.
            header_lines: list[str] = []
            for line in original_lines:
                if line.startswith("@@"):
                    break
                header_lines.append(line)
            truncated = "\n".join(header_lines)
            dropped = original_line_count - len(header_lines)
            return truncated, max(dropped, 0)

        truncated = body[:chosen_end].rstrip("\n")
        kept_line_count = truncated.count("\n") + 1 if truncated else 0
        dropped = original_line_count - kept_line_count
        return truncated, max(dropped, 0)

    def _truncate_at_line_boundary(
        self, body: str, limit: int, original_line_count: int
    ) -> tuple[str, int]:
        """Fallback truncation when the body has no hunk markers.

        Keeps as many complete leading lines as fit under ``limit``; a
        partial trailing line is dropped entirely so the truncated body
        ends cleanly before the footer is appended.
        """
        kept_lines: list[str] = []
        running_len = 0
        for line in body.split("\n"):
            # +1 for the joining newline; the very first line doesn't
            # cost a join newline but adding one anyway is a safe
            # over-estimate (we'd undercount one char, not overshoot).
            cost = len(line) + 1
            if running_len + cost > limit:
                break
            kept_lines.append(line)
            running_len += cost

        truncated = "\n".join(kept_lines)
        dropped = original_line_count - len(kept_lines)
        return truncated, max(dropped, 0)

    def _truncation_footer(self, dropped_line_count: int) -> str:
        """Render the DEC-005 truncation footer.

        ``project_dir`` is rendered as the literal placeholder
        ``<project_dir>`` when not provided to the constructor — the
        operator still understands the meaning, and the placeholder is
        stable across snapshot fixtures (no orchestrator-supplied path
        bleeds into golden output).
        """
        project_dir = self._project_dir if self._project_dir is not None else "<project_dir>"
        return (
            f"... ({dropped_line_count} more lines truncated — "
            f"see {project_dir}/.signalforge/diff.json for full diff)"
        )


# ---------------------------------------------------------------------------
# JsonRenderer — sidecar-shaped JSON concrete (US-010).
# ---------------------------------------------------------------------------


class JsonRenderer(Renderer):
    """Render a :class:`DiffReport` to indented JSON text (US-010).

    Output is exactly :meth:`pydantic.BaseModel.model_dump_json` with
    ``indent=2`` and ``by_alias=True`` — the same shape the sidecar
    writer (US-007) persists to ``<project>/.signalforge/diff.json``.
    The orchestrator (US-010) selects this concrete when
    :attr:`DiffConfig.render_kind == "json"`; the sidecar always uses
    this shape regardless of the configured render kind (DEC-004).

    This renderer does not call :func:`strip_ansi_escapes` on report
    fields. Defence-in-depth against ANSI smuggling for the JSON
    surface is unnecessary: :func:`json.dumps` (which Pydantic uses
    internally for ``model_dump_json``) escapes every byte outside the
    JSON string-literal grammar — ``\\x1b`` lands in the on-disk
    artefact as the four-byte literal ``\\u001b``, not as a smuggled
    SGR sequence.

    Args:
        none. The renderer is stateless; no constructor kwargs.
    """

    def render(self, report: DiffReport) -> str:
        """Render ``report`` to indented JSON text.

        Mirrors the sidecar writer's exact serialisation
        (``by_alias=True``, ``indent=2``) so the rendered text is
        byte-equal to the sidecar JSON when ``output_path`` and
        ``sidecar_path`` would point at the same file. The trailing
        newline is omitted here — the orchestrator decides whether
        to append one when piping to stdout, mirroring the contract
        documented on :meth:`Renderer.render`.
        """
        return report.model_dump_json(by_alias=True, indent=2)


__all__ = ("AnsiRenderer", "JsonRenderer", "MarkdownRenderer", "Renderer")
