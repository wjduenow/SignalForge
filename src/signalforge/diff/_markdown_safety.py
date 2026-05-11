"""Markdown scalar escaper for diff-renderer output (DEC-008).

User-controlled scalars (column names, descriptions, prune ``why``
reasons, grade ``reasoning``) land verbatim in the rendered Markdown
diff. Several characters break Markdown / HTML structure when
interpolated naively:

- HTML metacharacters (``&``, ``<``, ``>``): GFM allows raw HTML and
  parses tags like ``<script>`` or ``</details>`` directly. Entity-
  encoding ``&``→``&amp;``, ``<``→``&lt;``, ``>``→``&gt;`` neutralises
  smuggled HTML / closing-tag-injection at the source. ``&`` is encoded
  first so subsequent ampersands inside our own escape entities aren't
  double-encoded.
- Backtick (`` ` ``): opens an inline-code span — a string containing
  one stray backtick "leaks" the rest of the line into a code span
  until the next backtick (or end of line).
- Backslash (``\\``): Markdown's own escape character. Without escaping
  it first, our subsequent escapes can be undone by a crafted input
  ending in ``\\``.
- Pipe (``|``): the cell separator inside GFM tables. A scalar
  containing ``|`` rendered into a table cell terminates the cell
  early and shifts every subsequent column.

Order matters. ``&`` is processed first so subsequent ampersands in
our own escape entities aren't double-encoded; ``<``/``>`` follow.
Backslash is escaped before backtick / pipe so a trailing ``\\``
cannot unwind the later escapes. Inside a table cell, pipe is
HTML-entity-encoded (``&#124;``) rather than backslash-escaped,
because GFM's table parser tokenises pipes *before* applying
inline-escape rules — backslash-pipe still breaks the column count
in some renderers. Entity encoding sidesteps the parser entirely
and renders identically to a literal pipe in the operator's eyes.

The HTML-metacharacter pass applies in BOTH ``in_table_cell=True``
and ``in_table_cell=False`` paths as defence-in-depth. Fenced code
blocks — the only place GFM ignores HTML — bypass this escaper
entirely (the renderer doesn't run the escape on diff-fence body
content); every code path that DOES run through this function is a
structural sink where smuggled HTML would render.

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

    HTML-entity-encodes ``&``/``<``/``>`` first (defence against raw
    HTML smuggling — GFM parses ``<script>``/``</details>`` directly),
    then escapes ``\\``, `` ` ``, and ``|`` with a backslash. When
    ``in_table_cell=True``, additionally HTML-entity-encodes ``|``
    (``&#124;``) and the row-breaking control characters
    (``\\n``/``\\r``/``\\t``) so the rendered table row stays
    rectangular.

    Idempotent on plain text (no escapeable characters → unchanged).
    Empty string returns empty string.

    Pass ordering: ``&`` first (so subsequent ampersands in our own
    escape entities aren't double-encoded), then ``<``/``>``, then
    backslash (so trailing ``\\`` can't unwind the later escapes),
    then backtick / pipe / control chars. The HTML escapes apply in
    both table-cell and non-table-cell modes — fenced code blocks
    (the only Markdown context where raw HTML stays inert) bypass
    this function entirely.

    The function does not coerce its input; callers that hold
    ``bytes`` must decode first, and callers that worry about ANSI
    escapes should run
    :func:`signalforge.diff._ansi_safety.strip_ansi_escapes` *before*
    escaping for Markdown.
    """
    # HTML metacharacters first. ``&`` MUST come before any escape
    # that emits ampersand-prefixed entities; otherwise ``&amp;``
    # below would itself be re-encoded to ``&amp;amp;``.
    out = text.replace("&", "&amp;")
    out = out.replace("<", "&lt;")
    out = out.replace(">", "&gt;")

    # Backslash next — Markdown's escape character. Subsequent escapes
    # rely on backslash being literal in the output stream.
    out = out.replace("\\", "\\\\")
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
