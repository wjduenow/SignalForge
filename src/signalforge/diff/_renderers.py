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
_COL_TIER = 8
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
        dropped = self._color(f"dropped={report.dropped_count}", _RED, emit_color=emit_color)
        flagged = self._color(f"flagged={report.flagged_count}", _YELLOW, emit_color=emit_color)
        return f"{title}  {kept}  {dropped}  {flagged}"

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
        tier_colour = {"kept": _GREEN, "dropped": _RED, "flagged": _YELLOW}.get(entry.tier, _RESET)
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


__all__ = ("AnsiRenderer", "Renderer")
