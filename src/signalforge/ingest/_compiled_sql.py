"""sqlglot-AST analysis helpers for dbt-rendered (manifest ``compiled_code``) SQL.

Three pure, string-in / verdict-out functions used to decide whether a dbt-compiled
test body can be pruned like a ``custom_sql`` candidate:

* :func:`is_deterministic_sql` — reject bodies whose result depends on wall-clock
  time, randomness, or ``TABLESAMPLE`` (a non-deterministic body would make the
  prune verdict irreproducible, violating Architectural Commitment #5).
* :func:`is_row_returning` — reject scalar/aggregate-shaped bodies (a
  ``SELECT COUNT(*) …`` collapses to one row, so wrapping it in
  ``SELECT COUNT(*) AS failures FROM (<sql>) AS t`` yields ``failures=1`` **always**
  → a silent wrong ``kept`` verdict; #154 DEC-004 / AR row 10).
* :func:`validate_ingested_sql` — the comment-tolerant sibling of
  :func:`signalforge.warehouse._sql_safety.validate_test_sql`: dbt-compiled SQL
  routinely carries ``--`` and ``/* */`` comments that the ``#116`` validator
  rejects wholesale, so we strip comments (string-literal-aware) before the
  ``;`` / unbalanced-paren injection scan (#154 DEC-013 / AR row 11).

Placement (documented per US-002): this module lives under ``signalforge.ingest``
— the layer that is the *primary* consumer (the manifest-test ingest bridge, #154
US-003, runs the determinism + aggregate skip-record) — and is imported as a
belt-and-braces analysis seam by the ``signalforge.prune`` compiler (#154 US-004).
It carries **no ingest domain types** (no ``CandidateSchema`` / ``IngestResult``):
it is a pure ``str`` → verdict helper, so a ``from signalforge.ingest._compiled_sql
import …`` in the prune compiler creates no coupling to ingest's models. Keeping it
here (rather than under ``prune/``) keeps it in the stage-0-pure, warehouse-agnostic
zone alongside the reader it primarily serves.

sqlglot confinement (#159 DEC-008, extended by #154 DEC-006): ``sqlglot`` was
previously imported only by ``signalforge.draft.parser``. This module extends that
confinement — it is the ONLY new sqlglot importer added by #154, and sqlglot
imports must stay confined to it within the ingest layer. The parsing is
dialect-neutral (sqlglot dialects only affect a few edge tokens); the default
``dialect="bigquery"`` mirrors the drafter parser's default.

Conservative-bias / skip-when-uncertain: every analysis function returns the
*permissive* verdict on a sqlglot parse failure (``is_deterministic_sql`` → ``True``,
``is_row_returning`` → ``True``) — the layer cannot positively reject what it cannot
parse, and a genuinely malformed body is caught downstream (comment-tolerant
validation here, or the warehouse adapter's ``kept-without-evidence`` routing).
"""

from __future__ import annotations

import sqlglot
import sqlglot.errors
from sqlglot import exp

__all__ = [
    "is_deterministic_sql",
    "is_row_returning",
    "validate_ingested_sql",
]


# Non-deterministic SQL function names (upper-cased). Membership is checked
# against AST *function nodes* only, so a column literally named ``random_id``
# or ``current_timestamp_col`` (an ``exp.Column``) and the same tokens inside a
# string literal (an ``exp.Literal``) never match — that is the whole reason
# this uses sqlglot AST inspection rather than a regex/substring scan
# (#154 AR row 9).
_NONDETERMINISTIC_FUNCS: frozenset[str] = frozenset(
    {
        # randomness
        "RAND",
        "RANDOM",
        "RND",
        # wall-clock time
        "CURRENT_TIMESTAMP",
        "CURRENT_DATE",
        "CURRENT_DATETIME",
        "CURRENT_TIME",
        "NOW",
        "GETDATE",
        "SYSDATE",
        # unique-id generators
        "UUID",
        "GENERATE_UUID",
        "GEN_RANDOM_UUID",
    }
)


def _func_name(node: exp.Func) -> str | None:
    """Return the upper-cased SQL function name for a sqlglot ``exp.Func`` node.

    ``exp.Anonymous`` (a function sqlglot does not model with a dedicated class,
    e.g. ``NOW()`` / ``GETDATE()`` on some dialects) carries the real name on
    ``.name``; every other ``exp.Func`` subclass reports its canonical name via
    ``sql_name()`` (``exp.Rand`` → ``"RAND"``, ``exp.CurrentTimestamp`` →
    ``"CURRENT_TIMESTAMP"``, ``exp.Uuid`` → ``"UUID"``).
    """
    if isinstance(node, exp.Anonymous):
        this = node.name
        return this.upper() if isinstance(this, str) and this else None
    try:
        return node.sql_name().upper()
    except Exception:  # noqa: BLE001 — sql_name() surface is version-loose; skip.
        return None


def is_deterministic_sql(sql: str, *, dialect: str = "bigquery") -> bool:
    """Return ``False`` when ``sql`` contains a non-deterministic construct.

    Flags, via sqlglot AST inspection (NOT regex/substring):

    * ``TABLESAMPLE`` (``exp.TableSample``);
    * a call to a non-deterministic function — ``RAND`` / ``RANDOM`` / ``RND``,
      ``CURRENT_TIMESTAMP`` / ``CURRENT_DATE`` / ``NOW`` / ``GETDATE`` (and
      siblings), ``UUID`` / ``GENERATE_UUID`` — anywhere in the tree. An
      ``ORDER BY RAND() … LIMIT`` body is caught by the ``RAND`` function-node
      match; the ``ORDER BY`` context needs no special handling.

    Does **not** false-positive on a column named ``random_id`` /
    ``current_timestamp_col`` (parsed as ``exp.Column``) nor on those tokens
    inside a string literal (parsed as ``exp.Literal``).

    On a sqlglot parse failure, returns ``True`` (skip-when-uncertain): the
    function makes the *positive* claim "this body is non-deterministic", and an
    unparseable body affords no such claim; malformed SQL is caught elsewhere.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.errors.SqlglotError:
        return True
    if tree is None:
        return True

    if tree.find(exp.TableSample) is not None:
        return False

    for func in tree.find_all(exp.Func):
        name = _func_name(func)
        if name is not None and name in _NONDETERMINISTIC_FUNCS:
            return False
    return True


def _projection_collapses(node: object) -> bool:
    """Return ``True`` iff ``node`` contains a *collapsing* aggregate.

    A collapsing aggregate is an ``exp.AggFunc`` (``COUNT`` / ``SUM`` / ``AVG`` /
    ``MIN`` / ``MAX`` / …) that, at the projection's OWN query scope, forces the
    whole SELECT to one row. Two exclusions keep the walk correct:

    * **Windowed aggregates** (``COUNT(*) OVER (…)`` — the agg lives under an
      ``exp.Window``) do NOT collapse; the descent stops at ``exp.Window``.
    * **Aggregates inside a nested subquery** (``(SELECT COUNT(*) FROM t2)`` in
      the projection) belong to a different scope; the descent stops at
      ``exp.Subquery`` / a nested ``exp.Select``.

    The traversal is a manual downward descent (via ``iter_expressions``) rather
    than ``find_all`` precisely so the two scope boundaries can prune it — a flat
    ``find_all(exp.AggFunc)`` would wrongly count windowed / subquery aggregates.

    ``node`` is typed ``object`` (not the unexported ``exp.Expression``, per the
    drafter-parser convention); children are reached via ``getattr`` so a
    non-node leaf terminates the recursion cleanly.
    """
    if isinstance(node, (exp.Subquery, exp.Select, exp.Window)):
        return False
    if isinstance(node, exp.AggFunc):
        return True
    iter_expressions = getattr(node, "iter_expressions", None)
    if iter_expressions is None:
        return False
    return any(_projection_collapses(child) for child in iter_expressions())


def is_row_returning(sql: str, *, dialect: str = "bigquery") -> bool:
    """Return ``False`` when ``sql`` is scalar/aggregate-shaped (one-row).

    A body is scalar (returns ``False``) when it is a ``SELECT`` with **no
    ``GROUP BY``** whose **every** top-level projection is a collapsing aggregate
    (see :func:`_projection_collapses`). Everything else — a genuine
    failing-rows ``SELECT … WHERE`` (``True``), a windowed aggregate (``True``),
    an aggregate confined to a subquery whose outer projection is a plain column
    or ``*`` (``True``), an aggregate with ``GROUP BY`` (``True``) — is
    row-returning.

    Wrapping a scalar body in ``SELECT COUNT(*) AS failures FROM (<sql>) AS t``
    yields ``failures=1`` unconditionally, silently producing a wrong ``kept``
    verdict — the exact ``row_count_between`` bug (#154 DEC-004 / AR row 10). The
    ingest bridge skip-records scalar bodies so they never reach that wrap.

    On a sqlglot parse failure (or a non-``SELECT`` root such as a ``UNION``),
    returns ``True`` (skip-when-uncertain): "this body is scalar" is the
    positive claim, and an unparseable / non-single-SELECT body affords no such
    claim.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.errors.SqlglotError:
        return True
    if tree is None:
        return True

    root: object = tree
    while isinstance(root, (exp.Subquery, exp.Paren)):
        root = root.this

    if not isinstance(root, exp.Select):
        return True
    if root.args.get("group") is not None:
        return True

    projections = root.expressions
    if not projections:
        return True

    return not all(_projection_collapses(proj) for proj in projections)


def _strip_sql_comments(sql: str) -> str:
    """Strip ``--`` line comments and ``/* */`` block comments, string-literal-aware.

    A ``--`` / ``/*`` inside a ``'…'`` / ``"…"`` / `` `…` `` quoted span is NOT a
    comment, so the scan tracks the active quote (honouring doubled-quote escapes
    ``''`` / ``""`` / `` `` `` AND backslash escapes ``\\'`` / ``\\"`` inside
    ``'…'`` / ``"…"`` spans, which BigQuery/standard string literals accept — a
    dbt-expectations regex arg like ``'it\\'s'`` must not exit the span early)
    and only recognises a comment when outside a quoted span. Comment bodies are
    dropped (block comments collapse to a single space); string/identifier spans
    pass through untouched so the downstream ``;`` / paren scan can blank them via
    :func:`signalforge.warehouse._sql_safety._strip_string_literals`.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if quote is not None:
            out.append(ch)
            if ch == "\\" and quote in ("'", '"') and nxt:
                # Backslash escape inside a string span: the next char is literal
                # and can never close the span (``'it\\'s -- x'`` stays one string).
                # Backtick-quoted identifiers do NOT honour backslash escapes.
                out.append(nxt)
                i += 2
                continue
            if ch == quote:
                if nxt == quote:  # doubled-quote escape stays inside the span
                    out.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch == "-" and nxt == "-":
            i += 2
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i < n and not (sql[i] == "*" and i + 1 < n and sql[i + 1] == "/"):
                i += 1
            i += 2  # consume the closing "*/" (or run off the end harmlessly)
            out.append(" ")
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _blank_sql_literals(sql: str) -> str:
    """Replace the *contents* of quoted string/identifier spans with spaces.

    Backslash-escape- and doubled-quote-aware (same quote tracking as
    :func:`_strip_sql_comments`), so a ``;`` or paren hidden inside a literal is
    neutralised while a genuine top-level one survives the scan. Self-contained
    on purpose — it does NOT import the warehouse ``_strip_string_literals``
    private helper, so a rename there can't silently break the ingested-SQL gate
    (and this stage-0 reader stays free of a cross-layer coupling to a
    ``_``-prefixed internal). The opening/closing quote delimiters are kept so
    paren-balance accounting is unaffected.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if quote is not None:
            if ch == "\\" and quote in ("'", '"') and nxt:
                out.append("  ")  # blank the backslash-escape pair
                i += 2
                continue
            if ch == quote:
                if nxt == quote:  # doubled-quote escape stays inside the span
                    out.append("  ")
                    i += 2
                    continue
                out.append(ch)  # keep the closing delimiter
                quote = None
                i += 1
                continue
            out.append(" ")  # blank the literal content
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)  # keep the opening delimiter
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def validate_ingested_sql(sql: str) -> None:
    """Comment-tolerant safety scan for dbt-compiled (manifest) test SQL.

    Same contract as
    :func:`signalforge.warehouse._sql_safety.validate_test_sql` — returns
    ``None`` on success, raises :class:`~signalforge.warehouse.errors.QuerySyntaxError`
    on a real injection signal — but tolerant of the ``--`` line comments and
    ``/* */`` block comments dbt routinely emits in ``compiled_code`` (which the
    ``#116`` validator rejects wholesale, mass-degrading real bodies; #154
    DEC-013 / AR row 11).

    Comments are stripped first (string-literal-aware), then the remaining body
    is scanned exactly like ``validate_test_sql``: a top-level ``;`` (multiple
    statements) and unbalanced parentheses still fail loud. This is deliberately
    NOT a reuse of the ``#116`` validator — it is a distinct comment-tolerant
    entry point for the ingested path.
    """
    from signalforge.warehouse.errors import QuerySyntaxError

    # Strip comments (string-aware) THEN blank string/identifier literals, so a
    # ``;`` or unbalanced paren hidden inside a comment or a string is ignored
    # while a genuine top-level one still trips the scan. Both passes share the
    # same backslash-/doubled-quote-aware quote tracking (no warehouse-private
    # import — see :func:`_blank_sql_literals`).
    body = _blank_sql_literals(_strip_sql_comments(sql))

    if ";" in body:
        raise QuerySyntaxError(detail="ingested SQL must be a single statement (no `;`)")

    depth = 0
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise QuerySyntaxError(detail="ingested SQL has unbalanced parentheses")
    if depth != 0:
        raise QuerySyntaxError(detail="ingested SQL has unbalanced parentheses")
