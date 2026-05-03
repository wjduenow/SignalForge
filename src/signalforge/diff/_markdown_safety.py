"""Markdown scalar escaper for diff-renderer output (DEC-008).

User-controlled scalars (column names, descriptions, prune ``why``
reasons, grade ``reasoning``) land verbatim in the rendered Markdown
diff. Three characters break Markdown structure when interpolated
naively:

- Backtick (`` ` ``): opens an inline-code span — a string containing
  one stray backtick "leaks" the rest of the line into a code span
  until the next backtick (or end of line).
- Backslash (``\\``): Markdown's own escape character. Without escaping
  it first, our subsequent escapes can be undone by a crafted input
  ending in ``\\``.
- Pipe (``|``): the cell separator inside GFM tables. A scalar
  containing ``|`` rendered into a table cell terminates the cell
  early and shifts every subsequent column.

Backslash is escaped first so the rest of the pass cannot be unwound.
Inside a table cell, pipe is HTML-entity-encoded (``&#124;``) rather
than backslash-escaped, because GFM's table parser tokenises pipes
*before* applying inline-escape rules — backslash-pipe still breaks
the column count in some renderers. Entity encoding sidesteps the
parser entirely and renders identically to a literal pipe in the
operator's eyes.

Mirrors the project's broader "user-controlled string at a sink
boundary needs an explicit escaping pass" pattern: ``safety.audit``
JSON-encodes for the JSONL sink, ``warehouse._sql_safety`` escapes
for the BQ literal sink, this module escapes for the Markdown sink.
"""

from __future__ import annotations

# Control chars that would break a single-row table cell when rendered.
# Newline / carriage return / tab inside a GFM table cell either
# terminates the row early or merges adjacent cells visually depending
# on the renderer. HTML-entity-encode them inside table cells so the
# row geometry survives.
_TABLE_CELL_CONTROL_CHARS = {
    "\n": "&#10;",
    "\r": "&#13;",
    "\t": "&#9;",
}


def escape_markdown_scalar(text: str, in_table_cell: bool = False) -> str:
    """Escape a scalar string for safe interpolation into Markdown output.

    Always escapes ``\\``, `` ` ``, and ``|`` with a backslash. When
    ``in_table_cell=True``, additionally HTML-entity-encodes ``|``
    (``&#124;``) and the row-breaking control characters
    (``\\n``/``\\r``/``\\t``) so the rendered table row stays
    rectangular.

    Idempotent on plain text (no escapeable characters → unchanged).
    Empty string returns empty string.

    Backslash is processed first so subsequent escape passes cannot
    be undone by a crafted trailing-``\\``. The function does not
    coerce its input; callers that hold ``bytes`` must decode first,
    and callers that worry about ANSI escapes should run
    :func:`signalforge.diff._ansi_safety.strip_ansi_escapes` *before*
    escaping for Markdown.
    """
    # Backslash first — escapes that follow must be irreversible.
    out = text.replace("\\", "\\\\")
    out = out.replace("`", "\\`")

    if in_table_cell:
        # GFM table parser tokenises `|` before inline-escape rules
        # apply, so a backslash-escaped pipe still breaks column
        # counts in some renderers. HTML entity sidesteps the
        # parser entirely.
        out = out.replace("|", "&#124;")
        for ch, entity in _TABLE_CELL_CONTROL_CHARS.items():
            out = out.replace(ch, entity)
    else:
        out = out.replace("|", "\\|")

    return out
