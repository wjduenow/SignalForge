"""sqlglot-AST analysis helpers for dbt-rendered (manifest ``compiled_code``) SQL.

Pure, string-in / verdict-out functions used to decide whether a dbt-compiled
test body can be pruned like a ``custom_sql`` candidate — and, since #268 US-002,
whether and where it can be relation-rewritten so it can be *sampled*:

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
* :func:`plan_relation_rewrite` — locate every reference to the model's OWN
  relation in a foreign-rendered body, as **character spans** the prune compiler
  can splice to a ``_SESSION._sf_sample_*`` temp table (#268 DEC-001/005/006/015).
* :func:`verify_relation_rewrite` — the DEC-004 post-condition on the *rewritten*
  SQL. **A count is not an integrity proof**; see the function's docstring.

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

Totality over hostile input (#268 DEC-012(1)): the parse guard catches
:data:`_PARSE_FAILURES` — ``sqlglot.errors.SqlglotError`` **plus** ``RecursionError``
and ``ValueError``, neither of which is a ``SqlglotError``. A deeply-nested body
(~2000 parens) blows sqlglot's recursive-descent parser with ``RecursionError``
and an unknown ``dialect=`` name raises ``ValueError``; pre-#268 both escaped the
``except`` and **aborted the whole prune run with no audit rows written** —
fail-OPEN on the fail-closed audit contract. Every helper must stay total: parse
failure of any shape → the helper's own conservative verdict, never a raise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

import sqlglot
import sqlglot.errors
from sqlglot import exp
from sqlglot.expressions.core import Expr
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.tokenizer_core import Token, TokenType

__all__ = [
    "RELATION_REWRITE_REASONS",
    "RelationRewriteReason",
    "RewritePlan",
    "is_deterministic_sql",
    "is_prunable_count_scalar",
    "is_row_returning",
    "plan_relation_rewrite",
    "validate_ingested_sql",
    "verify_relation_rewrite",
]


# Every exception shape a ``sqlglot.parse_one`` call can raise on hostile /
# malformed input (#268 DEC-012(1)). ``RecursionError`` and ``ValueError`` are
# NOT ``SqlglotError`` subclasses:
#
# * ``RecursionError`` — sqlglot's parser is recursive descent, so a body with
#   ~2000-deep paren nesting exhausts the Python stack.
# * ``ValueError`` — an unregistered ``dialect=`` name ("Unknown dialect 'x'.").
#
# Both escaped the pre-#268 ``except sqlglot.errors.SqlglotError`` and aborted
# the whole prune run mid-flight with no audit rows written. Catch all three and
# return the helper's conservative verdict so these functions are TOTAL.
_PARSE_FAILURES: Final[tuple[type[BaseException], ...]] = (
    sqlglot.errors.SqlglotError,
    RecursionError,
    ValueError,
)


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

    On a sqlglot parse failure of ANY shape (:data:`_PARSE_FAILURES` — a
    ``SqlglotError``, a ``RecursionError`` from a deeply-nested body, or a
    ``ValueError`` from an unknown ``dialect=``), returns ``True``
    (skip-when-uncertain): the function makes the *positive* claim "this body is
    non-deterministic", and an unparseable body affords no such claim; malformed
    SQL is caught elsewhere.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except _PARSE_FAILURES:
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

    On a sqlglot parse failure of ANY shape (:data:`_PARSE_FAILURES`) or a
    non-``SELECT`` root (such as a ``UNION``), returns ``True``
    (skip-when-uncertain): "this body is scalar" is the positive claim, and an
    unparseable / non-single-SELECT body affords no such claim.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except _PARSE_FAILURES:
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


def is_prunable_count_scalar(sql: str, *, dialect: str = "bigquery") -> bool:
    """Return ``True`` when ``sql`` is a single top-level ``COUNT`` scalar body.

    Of the scalar bodies that :func:`is_row_returning` rejects (one-row,
    aggregate-shaped), this is the NARROWER positive gate: it graduates only the
    *count-of-rows* idiom — a ``SELECT`` with **no ``GROUP BY`` / ``HAVING``** whose **sole**
    top-level projection is a bare ``exp.Count`` (``COUNT(*)``, ``COUNT(col)``,
    and ``COUNT(DISTINCT col)`` all graduate; #267 DEC-011 — a ``COUNT(DISTINCT)``
    is still a count that is ``0`` iff no matching rows, so the ``0 = pass``
    interpretation holds). Those bodies are soundly re-interpretable as a
    failing-rows count and so can be pruned rather than skip-recorded (#267
    DEC-001).

    Returns ``False`` for everything else:

    * a non-count aggregate (``AVG`` / ``SUM`` / ``MIN`` / ``MAX``);
    * arithmetic-on-count (``COUNT(*) + 1`` — the projection is an ``exp.Add``
      *containing* a ``Count``, not a bare ``Count``);
    * a multi-projection scalar (``SELECT COUNT(*), MAX(x) …``);
    * a ``GROUP BY`` or ``HAVING`` body (``HAVING`` filters the aggregate on a
      condition unrelated to the failing-row count, breaking ``0 = pass``);
    * a non-``SELECT`` root or an unparseable body.

    On a sqlglot parse failure of ANY shape (:data:`_PARSE_FAILURES`) or a
    non-single-``SELECT`` root (such as a ``UNION``), returns ``False``
    (skip-when-uncertain): "this is a prunable count scalar" is the *positive*
    claim, and an unparseable / non-single-SELECT body affords no such claim.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except _PARSE_FAILURES:
        return False
    if tree is None:
        return False

    root: object = tree
    while isinstance(root, (exp.Subquery, exp.Paren)):
        root = root.this

    if not isinstance(root, exp.Select):
        return False
    if root.args.get("group") is not None:
        return False
    # A HAVING clause (even without GROUP BY) filters the aggregate result, so a
    # ``SELECT COUNT(*) … HAVING …`` body returns zero-or-one rows on a condition
    # unrelated to "how many failing rows" — the ``0 = pass`` reinterpretation no
    # longer holds. Reject it (it stays skip-recorded) rather than risk a silent
    # wrong verdict.
    if root.args.get("having") is not None:
        return False

    projections = root.expressions
    if len(projections) != 1:
        return False

    proj: object = projections[0]
    if isinstance(proj, exp.Alias):
        proj = proj.this
    return isinstance(proj, exp.Count)


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


# ---------------------------------------------------------------------------
# #268 US-002 — relation locate (plan) + rewrite verification.
#
# DEC-001: these are PURE ANALYSIS helpers. They parse, decide, and return
# character spans; they NEVER emit SQL. The prune compiler does the string
# splice, so ``import sqlglot`` stays out of ``signalforge.prune`` (no 4th
# sqlglot importer → no promoted confinement scan is owed). Mirrors #267's
# classify-at-ingest / restructure-at-compiler split exactly.
# ---------------------------------------------------------------------------


RelationRewriteReason = Literal[
    "unparseable",
    "zero-match",
    "multi-relation",
    "cte-alias-collision",
    "span-mismatch",
]

#: The closed set of machine-readable refusal reasons. A later stage builds a
#: ``{reason: count}`` histogram for one aggregate INFO line (#268 DEC-014), so
#: these strings are stable constants — never free-form prose.
RELATION_REWRITE_REASONS: Final[frozenset[str]] = frozenset(
    {
        "unparseable",
        "zero-match",
        "multi-relation",
        "cte-alias-collision",
        "span-mismatch",
    }
)


@dataclass(frozen=True, slots=True)
class RewritePlan:
    """The verdict of :func:`plan_relation_rewrite` for ONE compiled test body.

    Deliberately a single always-returned result type rather than the
    ``RewritePlan | None`` sketched in the #268 plan: a bare ``None`` cannot
    carry the machine-readable refusal reason the DEC-014 histogram needs.
    ``samplable`` is the boolean the routing layer branches on; ``reason`` is
    populated iff the body was refused.

    * ``spans`` — inclusive-``end`` **character** offsets (DEC-015) of each token
      run that resolves to the model's own physical relation, in source order.
      Empty iff the body was refused. Splice **back-to-front** against the
      IDENTICAL ``str`` object that was passed in — offsets computed on one
      string and applied to another (a comment-stripped or normalised copy) is
      exactly how this becomes an injection.
    * ``relation`` — the dialect-normalised ``(catalog, db, name)`` triple the
      spans resolve to (``""`` for absent leading parts).
    * ``reason`` — ``None`` on success; otherwise one of
      :data:`RELATION_REWRITE_REASONS`.
    """

    spans: tuple[tuple[int, int], ...]
    relation: tuple[str, str, str]
    reason: RelationRewriteReason | None

    @property
    def samplable(self) -> bool:
        """``True`` iff the body can be safely relation-rewritten."""
        return self.reason is None and bool(self.spans)


def _reject(reason: RelationRewriteReason) -> RewritePlan:
    return RewritePlan(spans=(), relation=("", "", ""), reason=reason)


#: Token types that can carry one component of a dotted relation path.
#: ``IDENTIFIER`` is the *quoted* form (`` `x` `` / ``"x"``); ``VAR`` is the bare
#: form. Anything else terminates the run — a relation part that tokenizes as a
#: keyword simply won't match, which fails closed.
_NAME_TOKENS: Final[frozenset[TokenType]] = frozenset({TokenType.IDENTIFIER, TokenType.VAR})


def _normalise_parts(parts: Sequence[tuple[str, bool]], dialect: str) -> tuple[str, str, str]:
    """Fold ``(text, quoted)`` components into a normalised ``(catalog, db, name)``.

    ``normalize_identifiers`` **is** the per-dialect fold rule (Snowflake
    upper-folds unquoted / preserves quoted; DuckDB + Databricks lower-fold;
    BigQuery preserves) — never hand-roll case folding. Both sides of every
    comparison in this module go through this one function, applied to a whole
    ``exp.Table`` node (normalising a *standalone* ``exp.Identifier`` does not
    reproduce the table-name rule), so the token side and the AST side can never
    disagree by construction.
    """
    identifiers = [exp.Identifier(this=text, quoted=quoted) for text, quoted in parts]
    kwargs: dict[str, exp.Identifier] = {}
    for key, identifier in zip(("this", "db", "catalog"), reversed(identifiers), strict=False):
        kwargs[key] = identifier
    table = normalize_identifiers(exp.Table(**kwargs), dialect=dialect)
    return (table.catalog, table.db, table.name)


def _table_tuple(table: exp.Table) -> tuple[str, str, str]:
    """Read the ``(catalog, db, name)`` triple off an ALREADY-normalised node."""
    return (table.catalog, table.db, table.name)


def _physical_tables(tree: Expr) -> list[exp.Table]:
    """Every ``exp.Table`` that is a genuine physical relation, CTE refs excluded.

    A CTE reference (``from grouped_expression``) parses as an ``exp.Table``
    structurally identical to a bare one-part relation, so it MUST be excluded
    or the splice could rewrite a CTE reference (DEC-005). Scope resolution —
    not a hand-rolled ``{c.alias_or_name for c in find_all(exp.CTE)}`` set — is
    what tells the two apart: ``Scope.sources`` maps a CTE reference's name to a
    child :class:`~sqlglot.optimizer.scope.Scope`, and a physical table's name to
    the ``exp.Table`` node itself.
    """
    cte_ref_ids: set[int] = set()
    for scope in traverse_scope(tree):
        for table in scope.tables:
            source = scope.sources.get(table.alias_or_name)
            if isinstance(source, Scope):
                cte_ref_ids.add(id(table))
    return [t for t in tree.find_all(exp.Table) if id(t) not in cte_ref_ids]


def _cte_alias_collides(tree: Expr, relation: tuple[str, str, str]) -> bool:
    """``True`` iff any CTE alias shadows the model relation (DEC-005 / AR-B2).

    Two collision shapes, both fatal:

    * the **dotted** alias — ``WITH `proj.ds.tbl` AS (…)`` parses the alias as a
      SINGLE ``Identifier`` whose text is ``proj.ds.tbl``, while the matching
      ``FROM `proj.ds.tbl` `` reference normalises to a ``Table`` tuple that
      exactly equals the model relation. A naive CTE-name exclusion set misses
      it entirely;
    * the **bare-name** alias — ``WITH orders AS (…)`` shadowing a model whose
      relation ends in ``orders``.

    Rewriting a CTE reference to the sample temp table plausibly yields zero rows
    → ``always-passes`` → **a real test is deleted**, the worst outcome the system
    can produce. So on ANY collision we refuse to sample rather than reason about
    which reference is which. The comparison is case-INSENSITIVE (strictly more
    conservative than the dialect fold — it can only refuse more, never less).
    """
    dotted = ".".join(p for p in relation if p).casefold()
    last = relation[2].casefold()
    for cte in tree.find_all(exp.CTE):
        folded = cte.alias_or_name.casefold()
        if folded in (dotted, last):
            return True
    return False


def _relation_spans(
    tokens: Sequence[Token], relation: tuple[str, str, str], dialect: str, arity: int
) -> tuple[tuple[int, int], ...]:
    """Character spans of every MAXIMAL dotted token run equal to ``relation``.

    **Maximality on both sides is the AR-B1 defence.** In
    ``select proj.ds.tbl.c from `proj.ds.tbl` `` the column qualifier tokenizes as
    the 4-part run ``proj . ds . tbl . c`` and the ``FROM`` as the 3-part run
    ``proj . ds . tbl``. A span-finder that matched a *prefix* of a longer run
    would splice the COLUMN PATH — and because that body also yields exactly one
    matching ``exp.Table``, a naive ``ast_count == span_count`` cross-check still
    passes while the ``FROM`` stays pointed at **production**. Requiring the run
    to be maximal (not preceded by a ``.``, extended greedily to the right) and
    to have EXACTLY the relation's arity rejects it.

    A run immediately followed by ``(`` is a function / table-valued-function
    call, never a relation reference — skipped.
    """
    spans: list[tuple[int, int]] = []
    n = len(tokens)
    i = 0
    while i < n:
        token = tokens[i]
        if token.token_type not in _NAME_TOKENS:
            i += 1
            continue
        if i > 0 and tokens[i - 1].token_type is TokenType.DOT:
            # Not the head of the run — the greedy extension below already
            # consumed it (or it belongs to a longer, non-matching run).
            i += 1
            continue

        run = [token]
        j = i
        while (
            j + 2 < n
            and tokens[j + 1].token_type is TokenType.DOT
            and tokens[j + 2].token_type in _NAME_TOKENS
        ):
            run.append(tokens[j + 2])
            j += 2
        i = j + 1

        if j + 1 < n and tokens[j + 1].token_type is TokenType.L_PAREN:
            continue  # a function / TVF call, not a relation
        if len(run) != arity:
            continue

        parts = [(t.text, t.token_type is TokenType.IDENTIFIER) for t in run]
        if _normalise_parts(parts, dialect) != relation:
            continue
        spans.append((run[0].start, run[-1].end))
    return tuple(spans)


def plan_relation_rewrite(sql: str, *, relation: Sequence[str], dialect: str) -> RewritePlan:
    """Locate every reference to ``relation`` in a dbt-compiled test body.

    ``sql`` is FOREIGN-rendered SQL — dbt produced it, not SignalForge — so the
    relation carries dbt's own dialect-specific quoting (`` `proj`.`ds`.`tbl` ``
    on BigQuery, ``"dev"."main"."orders"`` on DuckDB/Snowflake), appears an
    arbitrary number of times, and can sit inside CTEs and subqueries. String
    substitution (the #116 drafted-``custom_sql`` path) cannot find it; only an
    AST parse can.

    ``relation`` is the model's own 1..3-part relation, unquoted, in the
    warehouse's canonical case (e.g. ``model.resolve_this()`` split on ``.``).
    Matching is **exact on the full tuple, never a suffix** — a bare ``orders``
    matching a 3-part model relation would false-positive against a *different*
    table in the session's default schema.

    Refuses (``plan.samplable is False``, ``plan.reason`` set) when:

    * the body does not parse in ``dialect`` (``"unparseable"`` — also covers a
      ``RecursionError`` from a ~2000-deep body and a ``ValueError`` from an
      unknown ``dialect=``; see :data:`_PARSE_FAILURES`);
    * a CTE alias collides with the relation (``"cte-alias-collision"``, DEC-005);
    * the body touches more than one distinct physical relation
      (``"multi-relation"``, DEC-006) — enforced on the AST, NOT on a ``\\bjoin\\b``
      regex, which misses comma-joins, correlated subqueries and ``NOT EXISTS``.
      Sampling one leg of a join can produce a **false pass → ``always-passes`` →
      a real test is dropped**;
    * the body never references the model's relation (``"zero-match"``);
    * the token spans and the AST matches disagree, or a span does not re-tokenize
      to the run it claims (``"span-mismatch"``) — fail closed.

    Emits no SQL (stage-0 rule): the caller splices, then MUST prove the
    post-condition with :func:`verify_relation_rewrite` (DEC-004 — a count is not
    an integrity proof).
    """
    if not 1 <= len(relation) <= 3:
        return _reject("zero-match")

    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except _PARSE_FAILURES:
        return _reject("unparseable")
    if tree is None:  # pragma: no cover — sqlglot raises rather than return None.
        return _reject("unparseable")

    try:
        target = _normalise_parts([(part, False) for part in relation], dialect)
        tree = normalize_identifiers(tree, dialect=dialect)
        physical = _physical_tables(tree)
        tokens = sqlglot.tokenize(sql, read=dialect)
    # pragma: no cover — belt-and-braces. Every failure shape we can trigger
    # (bad dialect, RecursionError, ParseError) is already raised by the
    # ``parse_one`` above; scope-resolution / tokenization of an
    # already-parsed tree has no reachable failure. Kept so a sqlglot upgrade
    # that moves a raise here degrades to a refusal instead of aborting the run.
    except _PARSE_FAILURES:  # pragma: no cover
        return _reject("unparseable")

    if _cte_alias_collides(tree, target):
        return _reject("cte-alias-collision")

    distinct = {_table_tuple(t) for t in physical}
    if target not in distinct:
        return _reject("zero-match")
    if len(distinct) > 1:
        return _reject("multi-relation")

    ast_matches = sum(1 for t in physical if _table_tuple(t) == target)
    spans = _relation_spans(tokens, target, dialect, arity=len(relation))

    if not spans:
        # The AST matched but the tokenizer found no run of the expected arity.
        # Reachable in practice: a single-backtick DOTTED relation
        # (`` `proj.ds.tbl` ``) parses as a 3-part table but tokenizes as ONE
        # arity-1 identifier, so no arity-3 run is found. dbt-bigquery emits the
        # per-component `` `proj`.`ds`.`tbl` `` form, so this is uncommon, but it
        # must fail closed (→ source bypass) rather than guess.
        return _reject("zero-match")
    if len(spans) != ast_matches:
        # The tokenizer and the parser disagree about how many times the relation
        # appears — never guess which is right.
        return _reject("span-mismatch")

    # DEC-015: prove each span re-tokenizes to the run it claims, against the
    # IDENTICAL ``str`` object, before anyone splices it. This is the last gate
    # before a caller is handed offsets it will splice production SQL with; the
    # arms below are unreachable while :func:`_relation_spans` is correct, and
    # exist precisely so a future refactor of it fails CLOSED (a refusal) rather
    # than open (a mis-aimed splice).
    for start, end in spans:
        try:
            slice_tokens = sqlglot.tokenize(sql[start : end + 1], read=dialect)
        except _PARSE_FAILURES:  # pragma: no cover
            return _reject("span-mismatch")
        names = [t for t in slice_tokens if t.token_type in _NAME_TOKENS]
        if len(names) != len(relation):  # pragma: no cover
            return _reject("span-mismatch")
        parts = [(t.text, t.token_type is TokenType.IDENTIFIER) for t in names]
        try:
            if _normalise_parts(parts, dialect) != target:  # pragma: no cover
                return _reject("span-mismatch")
        except _PARSE_FAILURES:  # pragma: no cover
            return _reject("span-mismatch")

    return RewritePlan(spans=spans, relation=target, reason=None)


def verify_relation_rewrite(
    rewritten_sql: str,
    *,
    source: Sequence[str],
    temp: Sequence[str],
    expected_n: int,
    dialect: str,
) -> bool:
    """Prove the DEC-004 post-condition on the REWRITTEN SQL. **A count is not a proof.**

    The obvious invariant — ``ast_match_count == span_count`` on the *original*
    body — is defeatable: ``select proj.ds.tbl.c from `proj.ds.tbl` `` yields
    exactly one matching ``exp.Table`` *and* one dotted token run, but a
    span-finder that recognised the column qualifier would rewrite the COLUMN PATH
    while the ``FROM`` stayed on **production** — and the count check would pass.
    A silent full-scan of production, recorded as an evidence-backed verdict at
    ``scope="sample"``, is the worst thing this system can do.

    So the gate is a post-condition proved on the rewritten text:

    a. it parses clean in ``dialect``;
    b. **ZERO** residual ``exp.Table`` normalising to ``source``;
    c. exactly ``expected_n`` ``exp.Table`` equal to ``temp``.

    Anything else → ``False`` (the caller routes that to ``kept-without-evidence``).

    Post-splice :func:`validate_ingested_sql` is **not** a substitute: it only
    checks for a top-level ``;`` and paren balance on a literal-blanked copy, and
    cannot detect a partial rewrite, a residual production reference, or a splice
    that landed inside a string literal.
    """
    if not 1 <= len(source) <= 3 or not 1 <= len(temp) <= 3 or expected_n < 1:
        return False

    try:
        tree = sqlglot.parse_one(rewritten_sql, dialect=dialect)
        if tree is None:  # pragma: no cover — sqlglot raises rather than return None.
            return False
        source_tuple = _normalise_parts([(p, False) for p in source], dialect)
        temp_tuple = _normalise_parts([(p, False) for p in temp], dialect)
        tree = normalize_identifiers(tree, dialect=dialect)
        tuples = [_table_tuple(t) for t in tree.find_all(exp.Table)]
    except _PARSE_FAILURES:
        return False

    if any(t == source_tuple for t in tuples):
        return False
    return sum(1 for t in tuples if t == temp_tuple) == expected_n
