"""Candidate-test SQL compiler.

Translates each variant of the drafter's :class:`CandidateTest` discriminated
union (``not_null``, ``unique``, ``accepted_values``, ``relationships``) into
a failing-rows SELECT statement using :class:`Dialect.quote_char` for
identifier quoting. Matches dbt-core's NULL-exclusion conventions verbatim
so prune verdicts agree with ``dbt test`` runtime verdicts. Quote-escapes
user-controlled values (notably ``accepted_values.values``) before SQL
interpolation; trusts adapter-validated identifiers on :class:`TableRef`.

Design commitments operationalised here:

* **DEC-023** — Every failing-rows SELECT excludes ``NULL`` from the
  candidate set the way dbt-core does (``unique`` and ``accepted_values``
  both filter ``IS NOT NULL`` before the violation predicate; ``not_null``
  is the inverse, selecting only ``IS NULL`` rows). Diverging from
  dbt-core's conventions would cause prune verdicts to disagree with
  ``dbt test`` verdicts on the same model — a UX-breaking inconsistency.
* **DEC-024** — :func:`signalforge.warehouse._sql_safety.escape_bq_string_literal`
  is the single string-literal escape seam shared between the partition
  filter renderer (US-004) and the ``accepted_values`` compiler. Reusing it
  keeps the escape rules in lockstep across the warehouse and prune
  layers; a divergence would surface as a SQL-injection seam in either
  direction.
* **DEC-025** — :func:`_compile_test` dispatches on
  :attr:`Dialect.quote_char` rather than on dialect ``name``. v0.2 ports
  (Snowflake's double-quoted identifiers; Postgres') drop in by adding a
  sibling :class:`Dialect` constant in
  :mod:`signalforge.warehouse.models` — the compiler does not need to
  branch on warehouse name.
* **DEC-026** — A ``relationships`` test whose ``to`` parent model is not
  present in the loaded manifest returns a :class:`_RequiresFutureData`
  sentinel. The orchestrator routes the sentinel to the
  ``requires-future-data`` drop reason without issuing a warehouse call.
  Returning a sentinel rather than raising keeps compilation total: every
  candidate produces either compiled SQL or a structured no-op, and the
  caller distinguishes via :func:`isinstance`.
* **DEC-005** — :func:`_compute_compiled_sql_hash` mirrors the
  blake2b-8 / 16-hex-char convention from
  :mod:`signalforge.draft.audit` so prune-audit and response-audit
  records use the same hash domain.

The module is a pure transform: no warehouse calls, no logging, no I/O.
Compilation never raises (DEC-006). The returned strings are passed to
:meth:`signalforge.warehouse.WarehouseAdapter.run_test_sql` by the
orchestrator (US-008+).

See ``plans/super/6-prune-engine.md`` for the full design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import blake2b
from typing import TYPE_CHECKING

from signalforge.draft.models import (
    CandidateTest,
    CandidateTestAcceptedValues,
    CandidateTestCustomSQL,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestRowCountAnomalyByPeriod,
    CandidateTestRowCountBetween,
    CandidateTestUnique,
    CandidateTestUniqueCombination,
)
from signalforge.manifest.errors import (
    AmbiguousRefError,
    RefNotFoundError,
    SourceNotFoundError,
    TemplateResolutionError,
)
from signalforge.manifest.template import resolve_template_refs
from signalforge.warehouse._sample_sql import render_sample_select
from signalforge.warehouse._sql_safety import (
    escape_bq_string_literal,
    validate_identifier,
    validate_test_sql,
)
from signalforge.warehouse.errors import InvalidIdentifierError, QuerySyntaxError
from signalforge.warehouse.models import PartitionFilter, TableRef

if TYPE_CHECKING:
    from signalforge.manifest.models import Manifest, Model
    from signalforge.prune.models import Scope
    from signalforge.warehouse.models import Dialect


# Word-boundary, case-insensitive ``JOIN`` detector used as the cheap
# multi-table heuristic for ``custom_sql`` tests (DEC-006). A test whose
# resolved SQL contains a ``JOIN`` references more than one table; sampling
# only one side of a join produces false negatives (an orphan-detection
# join against a sampled child would miss parents absent from the sample),
# so multi-table tests run unsampled (full-scan) bounded by the adapter's
# ``maximum_bytes_billed`` cap.
_JOIN_RE = re.compile(r"\bjoin\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _RequiresFutureData:
    """Sentinel returned by :func:`_compile_test` when a ``relationships``
    test references a manifest-absent parent model.

    The orchestrator routes the sentinel to ``drop_reason=requires-future-data``
    without issuing a warehouse call. The :attr:`reason` field carries the
    human-readable why-line that surfaces in the prune diff (DEC-026).
    """

    reason: str


@dataclass(frozen=True, slots=True)
class _InvalidIdentifier:
    """Sentinel returned by :func:`_compile_test` when a candidate test's
    identifier (``column``, ``field``) fails the DEC-013 SQL-identifier shape.

    Defence-in-depth: ``CandidateTest.column`` / ``.field`` arrive from
    the LLM drafter via the anchor-contract validator (which checks the
    name exists in the manifest model) but are NOT shape-validated against
    the SQL-identifier regex. A malformed identifier would be backtick-
    quoted into the failing-rows SELECT and could break out of the
    quoting (e.g. an embedded backtick or whitespace).

    The orchestrator routes this sentinel to ``kept-without-evidence``
    (decision="kept") with the ``reason`` text in ``why`` so a reviewer
    sees the malformed identifier and can fix the prompt or model
    upstream. Treating it as "could not evaluate" rather than "drop" is
    conservative — a malformed test MAY still be signal-bearing once
    fixed.
    """

    reason: str


def _fold_identifier(identifier: str, dialect: Dialect) -> str:
    """Case-fold ``identifier`` per :attr:`Dialect.identifier_case` (DEC-003).

    ``"upper"`` → ``.upper()`` (Snowflake folds unquoted identifiers to
    UPPERCASE, so a conventional ``CREATE TABLE(customer_id …)`` stores
    ``CUSTOMER_ID``); ``"lower"`` → ``.lower()`` (Postgres); ``"preserve"``
    → unchanged (BigQuery — keeps the 11 BigQuery snapshots byte-identical).

    Folding runs BEFORE quoting so the always-quote injection-safe posture
    is preserved while the quoted name still resolves against conventional
    Snowflake/dbt tables.
    """
    if dialect.identifier_case == "upper":
        return identifier.upper()
    if dialect.identifier_case == "lower":
        return identifier.lower()
    return identifier


def _render_partition_filter(pf: PartitionFilter, dialect: Dialect) -> str:
    """Render a :class:`PartitionFilter` to a SQL fragment for the WHERE clause.

    Mirrors :meth:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter._render_partition_filter`
    — the prune compiler reuses the same render rules so the partition
    predicate the engine threads through the deterministic sample CTE
    matches what the warehouse adapter would emit on its own
    ``sample_rows`` path.

    ``datetime`` renders via :attr:`Dialect.timestamp_literal_template`
    (BigQuery ``TIMESTAMP('…')`` / Snowflake ``'…'::TIMESTAMP``); ``date``
    via :attr:`Dialect.date_literal_template`; ``str`` is escaped via
    :func:`escape_bq_string_literal` for safe inclusion inside a
    single-quoted string literal. The column name is already
    DEC-013-validated by :class:`PartitionFilter`'s ``__post_init__`` and
    is folded+quoted per the dialect (DEC-003).
    """
    # ``datetime`` is a subclass of ``date``, so check it first.
    if isinstance(pf.value, datetime):
        rendered = dialect.timestamp_literal_template.format(value=pf.value.isoformat())
    elif isinstance(pf.value, date):
        rendered = dialect.date_literal_template.format(value=pf.value.isoformat())
    else:
        rendered = f"'{escape_bq_string_literal(str(pf.value))}'"
    return f"{_quote(pf.column, dialect)} {pf.op} {rendered}"


def _render_sample_cte(
    table: str,
    *,
    sample_size: int,
    sample_bucket: int,
    partition_filter: PartitionFilter | None,
    dialect: Dialect,
) -> str:
    """Render a deterministic-sample CTE matching the adapter's
    :meth:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter.sample_rows`
    SQL shape.

    Every sample-mode failing-rows query is wrapped as::

        WITH sample AS (
            SELECT * FROM <table> AS t
            WHERE MOD(<dialect.sample_row_hash_expr>, <bucket>) < 1
              [AND <partition_filter>]
            LIMIT <sample_size>
        )
        <test SQL targeting sample>

    The hash-mod predicate is rendered from
    :attr:`Dialect.sample_row_hash_expr` (BigQuery
    ``ABS(FARM_FINGERPRINT(TO_JSON_STRING(t)))`` / Snowflake
    ``ABS(HASH(*))``) so sampling decisions stay consistent between the
    adapter's internal samples and the prune engine's wrapped tests
    (DEC-006 of issue #3; DEC-002 of issue #121).

    The CTE body is built by the shared
    :func:`signalforge.warehouse._sample_sql.render_sample_select` so the
    compiler's sample CTE and the warehouse adapter's sample ``SELECT``
    stay byte-consistent (issue #139, DEC-002). The compiler CTE carries
    **no** ``ORDER BY`` (``order_by_hash=False``); the partition predicate
    is rendered by the compiler's own dialect-correct
    :func:`_render_partition_filter` and passed as ``extra_where``.
    """
    extra_where = (
        _render_partition_filter(partition_filter, dialect)
        if partition_filter is not None
        else None
    )
    body = render_sample_select(
        table,
        dialect=dialect,
        sample_bucket=sample_bucket,
        sample_size=sample_size,
        extra_where=extra_where,
        order_by_hash=False,
    )
    return f"WITH {dialect.sample_cte_alias} AS ({body})"


def _quote(identifier: str, dialect: Dialect) -> str:
    """Fold (per :attr:`Dialect.identifier_case`) then wrap ``identifier`` in
    :attr:`Dialect.quote_char` for SQL embedding (DEC-003).

    The compiler trusts adapter-validated identifiers on
    :class:`TableRef` — :func:`signalforge.warehouse._sql_safety.validate_identifier`
    runs at :class:`TableRef` construction time and rejects anything
    outside ``[A-Za-z_][A-Za-z0-9_]*``, so we do not re-validate here.
    Column names from :class:`signalforge.draft.models.CandidateTest`
    pass through the drafter's anchor-contract validator before reaching
    the compiler; the orchestrator's :class:`TableRef` construction is
    the gate, not this function.

    Case folding runs on the already-validated token, so it cannot
    introduce a quote-breaking character. BigQuery's ``"preserve"`` makes
    folding a no-op — the existing snapshots stay byte-identical.
    """
    folded = _fold_identifier(identifier, dialect)
    return f"{dialect.quote_char}{folded}{dialect.quote_char}"


def _qualified_table_name(table_ref: TableRef, dialect: Dialect) -> str:
    """Render a fully-qualified ``project.dataset.name`` table identifier.

    Branches on :attr:`Dialect.quote_qualified_per_component` (DEC-002):

    * ``False`` (BigQuery) — the entire dotted path is wrapped in one pair
      of quote characters (``\\`project.dataset.table\\```). No case folding
      (BigQuery's ``identifier_case="preserve"``) so the 11 BigQuery
      snapshots stay byte-identical.
    * ``True`` (Snowflake) — each component is folded+quoted separately and
      joined with ``.`` (``"DB"."SCH"."T"``), because a single quoted string
      spanning dots reads as one literal identifier named ``db.schema.table``.
    """
    if dialect.quote_qualified_per_component:
        components = (
            [table_ref.dataset, table_ref.name]
            if table_ref.project is None
            else [table_ref.project, table_ref.dataset, table_ref.name]
        )
        return ".".join(_quote(component, dialect) for component in components)
    qc = dialect.quote_char
    if table_ref.project is None:
        return f"{qc}{table_ref.dataset}.{table_ref.name}{qc}"
    return f"{qc}{table_ref.project}.{table_ref.dataset}.{table_ref.name}{qc}"


def _wrap_with_sample_or_partition(
    *,
    test_sql: str,
    table_sql: str,
    table_alias_sql: str,
    scope: Scope,
    sample_size: int | None,
    sample_bucket: int | None,
    partition_filter: PartitionFilter | None,
    dialect: Dialect,
) -> str:
    """Apply scope/sample/partition_filter wrapping to a per-test SELECT.

    ``test_sql`` is the failing-rows SELECT rendered against
    ``table_alias_sql`` (typically the same string as ``table_sql`` for
    the unwrapped path; substituted to ``sample`` when wrapping). The
    function returns:

    * ``scope == "sample"`` — a ``WITH sample AS (...) <test_sql>``
      compound where the test targets the CTE rather than the raw table.
      The deterministic-sample predicate matches the adapter's
      :meth:`sample_rows` shape (DEC-006 of issue #3).
    * ``scope == "full"`` AND ``partition_filter is not None`` — the
      partition predicate is appended to the test's existing WHERE clause
      (or added as a new WHERE when the test has none). The caller is
      responsible for emitting a test SQL that already targets the raw
      table with a ``WHERE`` clause; partition-only injection assumes the
      caller never emits ``WHERE`` for ``unique`` (it does emit one for
      ``not_null`` / ``accepted_values`` / ``relationships``). To avoid
      that fragility this helper composes the predicate via subquery: the
      caller's ``test_sql`` runs against a derived ``(SELECT * FROM
      <table> WHERE <partition_filter>) AS t`` rather than the raw table.
    * ``scope == "full"`` AND ``partition_filter is None`` — returns
      ``test_sql`` unchanged.

    The partition-via-subquery composition is uniform across all four
    test shapes (no per-test ``WHERE``-clause surgery), which keeps the
    helper a true wrapper.
    """
    del table_alias_sql  # currently unused — kept in signature for symmetry / future use
    if scope == "sample":
        if sample_size is None or sample_bucket is None:
            # Defensive: the orchestrator must supply both when scope=sample.
            # Falling back silently would defeat US-003's cost model.
            raise ValueError(
                "scope='sample' requires both sample_size and sample_bucket; "
                "the orchestrator should have computed these before calling _compile_test."
            )
        cte = _render_sample_cte(
            table_sql,
            sample_size=sample_size,
            sample_bucket=sample_bucket,
            partition_filter=partition_filter,
            dialect=dialect,
        )
        return f"{cte} {test_sql}"
    # scope == "full"
    if partition_filter is not None:
        partition_sql = _render_partition_filter(partition_filter, dialect)
        # Compose via derived table so per-test WHERE-clause shapes don't
        # have to be edited. Every per-test compiler emits exactly one
        # ``FROM <table_sql>`` fragment for the primary (child) table.
        # ``relationships`` emits a second ``LEFT JOIN <parent_table>``
        # which is intentionally NOT rewritten: the partition filter
        # applies to the model under prune (the child), not its
        # referenced parent.
        needle = f"FROM {table_sql}"
        replacement = f"FROM (SELECT * FROM {table_sql} WHERE {partition_sql})"
        if needle in test_sql:
            return test_sql.replace(needle, replacement, 1)
    return test_sql


def _compile_not_null(
    test: CandidateTestNotNull,
    table_ref: TableRef,
    dialect: Dialect,
    *,
    scope: Scope,
    sample_size: int | None,
    sample_bucket: int | None,
    partition_filter: PartitionFilter | None,
) -> str | _InvalidIdentifier:
    """Compile ``not_null(col)`` to ``SELECT col FROM t WHERE col IS NULL``."""
    try:
        validate_identifier("CandidateTestNotNull.column", test.column)
    except InvalidIdentifierError:
        return _InvalidIdentifier(
            reason=(
                f"candidate test references an invalid identifier shape: column={test.column!r}"
            )
        )
    col = _quote(test.column, dialect)
    table = _qualified_table_name(table_ref, dialect)
    target = dialect.sample_cte_alias if scope == "sample" else table
    test_sql = f"SELECT {col} FROM {target} WHERE {col} IS NULL"
    return _wrap_with_sample_or_partition(
        test_sql=test_sql,
        table_sql=table,
        table_alias_sql=target,
        scope=scope,
        sample_size=sample_size,
        sample_bucket=sample_bucket,
        partition_filter=partition_filter,
        dialect=dialect,
    )


def _compile_unique(
    test: CandidateTestUnique,
    table_ref: TableRef,
    dialect: Dialect,
    *,
    scope: Scope,
    sample_size: int | None,
    sample_bucket: int | None,
    partition_filter: PartitionFilter | None,
) -> str | _InvalidIdentifier:
    """Compile ``unique(col)`` to a GROUP BY ... HAVING COUNT(*) > 1.

    DEC-023 NULL-exclusion: ``IS NOT NULL`` filters NULL rows out of the
    grouped set, matching dbt-core (multiple NULLs in a column do not
    violate uniqueness in dbt's convention).
    """
    try:
        validate_identifier("CandidateTestUnique.column", test.column)
    except InvalidIdentifierError:
        return _InvalidIdentifier(
            reason=(
                f"candidate test references an invalid identifier shape: column={test.column!r}"
            )
        )
    col = _quote(test.column, dialect)
    table = _qualified_table_name(table_ref, dialect)
    target = dialect.sample_cte_alias if scope == "sample" else table
    test_sql = (
        f"SELECT {col} FROM {target} WHERE {col} IS NOT NULL GROUP BY {col} HAVING COUNT(*) > 1"
    )
    return _wrap_with_sample_or_partition(
        test_sql=test_sql,
        table_sql=table,
        table_alias_sql=target,
        scope=scope,
        sample_size=sample_size,
        sample_bucket=sample_bucket,
        partition_filter=partition_filter,
        dialect=dialect,
    )


def _compile_accepted_values(
    test: CandidateTestAcceptedValues,
    table_ref: TableRef,
    dialect: Dialect,
    *,
    scope: Scope,
    sample_size: int | None,
    sample_bucket: int | None,
    partition_filter: PartitionFilter | None,
) -> str | _InvalidIdentifier:
    """Compile ``accepted_values(col, values)`` to a ``NOT IN`` predicate.

    Each value goes through
    :func:`signalforge.warehouse._sql_safety.escape_bq_string_literal`
    (DEC-024) so embedded quotes, backslashes, newlines, and ANSI escapes
    cannot break out of the literal. The escaped value is wrapped in
    single quotes; the resulting SQL passes
    :func:`signalforge.warehouse._sql_safety.validate_test_sql` even for
    adversarial inputs (the entire injection attempt stays inside the
    quoted string).
    """
    try:
        validate_identifier("CandidateTestAcceptedValues.column", test.column)
    except InvalidIdentifierError:
        return _InvalidIdentifier(
            reason=(
                f"candidate test references an invalid identifier shape: column={test.column!r}"
            )
        )
    col = _quote(test.column, dialect)
    table = _qualified_table_name(table_ref, dialect)
    target = dialect.sample_cte_alias if scope == "sample" else table
    rendered_values = ", ".join(f"'{escape_bq_string_literal(v)}'" for v in test.values)
    test_sql = (
        f"SELECT {col} FROM {target} WHERE {col} IS NOT NULL AND {col} NOT IN ({rendered_values})"
    )
    return _wrap_with_sample_or_partition(
        test_sql=test_sql,
        table_sql=table,
        table_alias_sql=target,
        scope=scope,
        sample_size=sample_size,
        sample_bucket=sample_bucket,
        partition_filter=partition_filter,
        dialect=dialect,
    )


def _resolve_parent_table_ref(
    parent_name: str,
    manifest: Manifest,
) -> TableRef | _RequiresFutureData:
    """Resolve a ``relationships(to=parent_name)`` to its parent TableRef.

    The drafter's :class:`CandidateTestRelationships.to` field carries
    only the parent model's :attr:`Model.name` (not a full ``unique_id``);
    :class:`Manifest` indexes by ``unique_id``, so the lookup scans
    :attr:`Manifest.nodes` for every model whose :attr:`Model.name`
    matches.

    Returns a :class:`_RequiresFutureData` sentinel when:

    * No match is found (parent is absent from the manifest — DEC-026).
    * Two or more models in the manifest share ``parent_name`` (e.g.
      multiple packages with a ``customers`` model). The compiler does
      not have enough information to disambiguate; routing to
      ``requires-future-data`` ships the test to the operator with a
      precise diagnostic rather than silently picking a parent.

    When exactly one match is found, returns the parent's
    :class:`TableRef` via :meth:`TableRef.from_model`; that call may
    raise :class:`ManifestProjectNotFoundError` or
    :class:`ManifestSchemaNotFoundError` if the parent model lacks
    ``database`` / ``schema`` — those are manifest-shape problems and
    propagate, not prune problems to swallow.
    """
    matches = [m for m in manifest.nodes.values() if m.name == parent_name]
    if not matches:
        return _RequiresFutureData(reason=f"relationships parent {parent_name!r} not in manifest")
    if len(matches) > 1:
        return _RequiresFutureData(
            reason=(
                f"relationships parent {parent_name!r} ambiguous: "
                f"matched {len(matches)} models in manifest"
            )
        )
    return TableRef.from_model(matches[0])


def _compile_relationships(
    test: CandidateTestRelationships,
    table_ref: TableRef,
    dialect: Dialect,
    manifest: Manifest,
    *,
    scope: Scope,
    sample_size: int | None,
    sample_bucket: int | None,
    partition_filter: PartitionFilter | None,
) -> str | _RequiresFutureData | _InvalidIdentifier:
    """Compile ``relationships(child_col, to=parent, field=parent_col)``.

    Renders a LEFT JOIN orphan-detection SELECT: rows in the child where
    the foreign key is non-null but the parent has no matching row.

    Returns a :class:`_RequiresFutureData` sentinel when the parent
    model is not in the manifest (DEC-026); the orchestrator routes
    that to the ``requires-future-data`` drop reason without issuing a
    warehouse call.

    Returns an :class:`_InvalidIdentifier` sentinel when ``column`` or
    ``field`` fails the SQL-identifier shape check; the orchestrator
    routes that to ``kept-without-evidence``. ``to`` is NOT shape-checked
    here — it's a model name resolved via :func:`_resolve_parent_table_ref`
    and a missing parent yields the ``_RequiresFutureData`` branch.

    Sample-mode asymmetry: when ``scope == "sample"``, only the CHILD
    table is sampled. The parent stays at full so an orphan detected in
    the child sample is not a false positive caused by the parent's
    missing-from-sample row. ``partition_filter`` likewise applies only
    to the child (the model under prune).
    """
    try:
        validate_identifier("CandidateTestRelationships.column", test.column)
        validate_identifier("CandidateTestRelationships.field", test.field)
    except InvalidIdentifierError as exc:
        return _InvalidIdentifier(
            reason=(
                f"candidate test references an invalid identifier shape: {exc.field}={exc.value!r}"
            )
        )

    parent_table_ref = _resolve_parent_table_ref(test.to, manifest)
    if isinstance(parent_table_ref, _RequiresFutureData):
        return parent_table_ref

    child_col = _quote(test.column, dialect)
    parent_col = _quote(test.field, dialect)
    child_table = _qualified_table_name(table_ref, dialect)
    parent_table = _qualified_table_name(parent_table_ref, dialect)
    child_target = dialect.sample_cte_alias if scope == "sample" else child_table
    test_sql = (
        f"SELECT child.{child_col} "
        f"FROM {child_target} AS child "
        f"LEFT JOIN {parent_table} AS parent "
        f"ON child.{child_col} = parent.{parent_col} "
        f"WHERE child.{child_col} IS NOT NULL AND parent.{parent_col} IS NULL"
    )
    return _wrap_with_sample_or_partition(
        test_sql=test_sql,
        table_sql=child_table,
        table_alias_sql=child_target,
        scope=scope,
        sample_size=sample_size,
        sample_bucket=sample_bucket,
        partition_filter=partition_filter,
        dialect=dialect,
    )


def _is_multi_table(resolved_sql: str) -> bool:
    """Cheap multi-table heuristic for a resolved ``custom_sql`` body (DEC-006).

    Returns ``True`` when the resolved SQL contains a word-boundary
    ``JOIN`` keyword (case-insensitive). String literals are stripped
    first via :func:`signalforge.warehouse._sql_safety._strip_string_literals`
    so a ``JOIN`` appearing inside a quoted value (``WHERE label = 'pre-join'``)
    does not flip the heuristic to full-scan.

    A multi-table test runs unsampled (full-scan) because sampling only
    one table of a join is semantically wrong — an orphan-detection join
    against a sampled child would report false orphans for parents that
    are simply absent from the sample. Full-scan is bounded later by the
    adapter's ``maximum_bytes_billed`` cap; over-cap is the engine's
    concern (US-008), not the compiler's.
    """
    from signalforge.warehouse._sql_safety import _strip_string_literals

    return _JOIN_RE.search(_strip_string_literals(resolved_sql)) is not None


def _compile_custom_sql(
    test: CandidateTestCustomSQL,
    table_ref: TableRef,
    dialect: Dialect,
    manifest: Manifest,
    model: Model | None,
    *,
    scope: Scope,
    sample_size: int | None,
    sample_bucket: int | None,
    partition_filter: PartitionFilter | None,
) -> str | _RequiresFutureData | _InvalidIdentifier:
    """Compile a ``custom_sql`` singular test to a failing-rows SELECT.

    Per dbt's singular-test contract (DEC-003), ``test.sql`` is itself a
    full SELECT that returns the *failing* rows: zero rows means the test
    passes. The compiler:

    1. **Resolves dbt-Jinja refs** via
       :func:`signalforge.manifest.template.resolve_template_refs` —
       ``{{ this }}`` → the model's qualified name, ``{{ ref(...) }}`` /
       ``{{ source(...) }}`` → the referenced table's qualified name.
       Control-flow Jinja, ``var()`` / ``env_var()``, and macro calls are
       unsupported and surface as :class:`TemplateResolutionError`
       (DEC-004).
    2. **Runs SQL-safety pre-flight** (:func:`validate_test_sql`) on the
       *resolved* SQL (DEC-008). Stray ``;`` / ``--`` / ``/* */`` / unbalanced
       parens are rejected.
    3. **Returns the resolved failing-rows SELECT** for the adapter to wrap
       with ``SELECT COUNT(*) AS failures FROM (<sql>) AS t`` — identical
       to how the four built-in variants return their inner SELECT. The
       compiler does NOT pre-wrap the ``count(*)`` itself: the adapter's
       :meth:`run_test_sql` owns that envelope, and pre-wrapping here would
       double-count.

    Conservative-bias routing (DEC-006, DEC-008): Jinja-resolution failure
    (:class:`TemplateResolutionError` / :class:`UnsupportedJinjaError`),
    :class:`AmbiguousRefError`, and SQL-safety rejection return an
    :class:`_InvalidIdentifier` sentinel rather than raising — the
    orchestrator routes the sentinel to ``kept-without-evidence``
    (decision="kept") so a test SignalForge cannot evaluate is shipped,
    not silently dropped. An unresolvable ``{{ ref(...) }}`` /
    ``{{ source(...) }}`` whose target is absent from the manifest
    (:class:`RefNotFoundError` / :class:`SourceNotFoundError`) returns a
    :class:`_RequiresFutureData` sentinel instead — the referenced
    model/source isn't built yet, mirroring the ``relationships``
    missing-target precedent (DEC-026), so it routes to
    ``requires-future-data``. None of these errors ever propagate out of
    the compiler. Compilation stays total (DEC-006): every candidate
    yields compiled SQL or a structured sentinel.

    Single-table vs. multi-table (DEC-006, DEC-009):

    * **Single-table** (no ``JOIN`` after resolution) — the resolved SQL
      references only the model's own table. In ``scope="sample"`` the
      model's own qualified table name is substituted with the deterministic
      ``sample`` CTE alias and the CTE is prepended (mirrors the built-ins).
      In ``scope="full"`` with a ``partition_filter``, the model's table is
      replaced with a partition-filtered derived table.
    * **Multi-table** (a ``JOIN`` keyword survives literal-stripping) — runs
      full-scan (unsampled). A partition filter is still applied to the
      model's own table when one is available.

    ``model`` carries the :class:`Model` under prune so the Jinja resolver
    can map ``{{ this }}`` and so single-table substitution knows the
    model's own qualified name. When ``model is None`` (no model threaded
    through), the test cannot be resolved and routes to the sentinel.
    """
    if model is None:
        # The orchestrator must thread ``model`` for custom_sql resolution.
        # Absent it, conservatively route to kept-without-evidence rather
        # than raising — the LLM proposed the test; absent a way to resolve
        # its refs we ship it for the operator to decide.
        return _InvalidIdentifier(
            reason="custom_sql test cannot be resolved without the model under prune"
        )

    try:
        resolved_sql = resolve_template_refs(test.sql, model, manifest)
    except (RefNotFoundError, SourceNotFoundError) as exc:
        # The ref()/source() target is not in the manifest yet — the
        # referenced model/source simply isn't built. Mirror the
        # relationships missing-target precedent (DEC-026): route to
        # requires-future-data so the operator revisits when the
        # dependency lands. NEVER raise — these are ManifestError
        # siblings of TemplateResolutionError, not subclasses, so the
        # broader handler below would not catch them.
        return _RequiresFutureData(
            reason=f"custom_sql references a manifest-absent target: {type(exc).__name__}"
        )
    except AmbiguousRefError as exc:
        # Genuine user ambiguity (the ref() name matches multiple
        # packages), not future data. Route to kept-without-evidence so a
        # reviewer disambiguates with the two-arg ref('pkg','name') form.
        return _InvalidIdentifier(reason=f"custom_sql ref() is ambiguous: {type(exc).__name__}")
    except TemplateResolutionError as exc:
        # Covers both UnsupportedJinjaError and the residual-{{ }} case.
        return _InvalidIdentifier(
            reason=f"custom_sql Jinja could not be resolved: {type(exc).__name__}"
        )

    try:
        validate_test_sql(resolved_sql)
    except QuerySyntaxError:
        return _InvalidIdentifier(
            reason="custom_sql rejected by SQL safety pre-flight on resolved SQL"
        )

    # The model's own qualified table name, as the Jinja resolver emits it
    # (dialect-neutral ``[project.]dataset.name``) — this is the substring
    # we look for when sampling / partition-filtering the single-table case.
    own_qualified = model.resolve_this().qualified_name
    own_table_quoted = _qualified_table_name(table_ref, dialect)

    if _is_multi_table(resolved_sql):
        # Multi-table: full-scan. Apply a partition filter to the model's
        # own table when available; otherwise return the resolved SQL
        # unchanged. Sampling a join is semantically wrong (DEC-006).
        if partition_filter is not None:
            partition_sql = _render_partition_filter(partition_filter, dialect)
            replacement = f"(SELECT * FROM {own_qualified} WHERE {partition_sql})"
            if own_qualified in resolved_sql:
                return resolved_sql.replace(own_qualified, replacement, 1)
        return resolved_sql

    # Single-table.
    if scope == "sample":
        if sample_size is None or sample_bucket is None:
            raise ValueError(
                "scope='sample' requires both sample_size and sample_bucket; "
                "the orchestrator should have computed these before calling _compile_test."
            )
        # Fail closed when the resolved SQL never references the model's own
        # qualified name: there is nothing to substitute with the ``sample``
        # CTE alias, so running the SQL as-is would read the full source
        # table instead of the sample. Route to kept-without-evidence rather
        # than silently sampling the wrong (unsampled) table.
        if own_qualified not in resolved_sql:
            return _InvalidIdentifier(
                reason=(
                    "custom_sql does not reference the model's own table "
                    "({{ this }}); cannot bind the deterministic sample"
                )
            )
        # Substitute the model's own table with the ``sample`` CTE alias,
        # then prepend the deterministic-sample CTE bound to the real table.
        # Replace ALL occurrences (P2 fix): a single-table custom_sql that
        # references its own table more than once (correlated subquery /
        # self-UNION without a JOIN) would otherwise leave later occurrences
        # reading the full source table.
        sampled_sql = resolved_sql.replace(own_qualified, dialect.sample_cte_alias)
        cte = _render_sample_cte(
            own_table_quoted,
            sample_size=sample_size,
            sample_bucket=sample_bucket,
            partition_filter=partition_filter,
            dialect=dialect,
        )
        return f"{cte} {sampled_sql}"

    # scope == "full" single-table.
    #
    # P0 fix: when the orchestrator substituted a DIFFERENT physical table
    # for ``table_ref`` than the model's own source (i.e. the materialised
    # temp table under ``sample_strategy="materialised"`` + ``scope="sample"``,
    # which the engine compiles as effective ``scope="full"`` against the
    # temp table), rewrite ALL occurrences of the model's own qualified name
    # to the quoted ``table_ref`` so the test reads the sample rather than
    # full-scanning the production source. This mirrors the built-in
    # compilers, which always FROM ``table_ref``.
    if table_ref.qualified_name != own_qualified:
        # The effective table is NOT the model's own source (materialised
        # sample). Fail closed when the resolved SQL never names the model's
        # own table: there is nothing to rewrite to the sample table, so
        # running it as-is would read the wrong/source table rather than the
        # materialised sample. Route to kept-without-evidence.
        if own_qualified not in resolved_sql:
            return _InvalidIdentifier(
                reason=(
                    "custom_sql does not reference the model's own table "
                    "({{ this }}); cannot bind the materialised sample"
                )
            )
        return resolved_sql.replace(own_qualified, own_table_quoted)

    # No substitution (``table_ref`` IS the model's own table — the oneshot /
    # full-strategy path): compose a partition filter via derived table when
    # one is available (uniform with the built-ins).
    if partition_filter is not None:
        partition_sql = _render_partition_filter(partition_filter, dialect)
        replacement = f"(SELECT * FROM {own_qualified} WHERE {partition_sql})"
        if own_qualified in resolved_sql:
            return resolved_sql.replace(own_qualified, replacement)
    return resolved_sql


def _compile_row_count_between(
    test: CandidateTestRowCountBetween,
    table_ref: TableRef,
    dialect: Dialect,
) -> str | _InvalidIdentifier:
    """Compile ``row_count_between(minimum, maximum, where?)`` to a
    failing-rows SELECT (#169 DEC-003, corrected by US-007a / tt8.15).

    Emits a CTE-wrapped failing-rows SELECT of the form::

        SELECT n
        FROM (SELECT COUNT(*) AS n FROM <table> [WHERE <where>]) AS rc
        WHERE <bound-violation-predicate>

    The bound-violation predicate is one of:

    * ``n < <minimum>`` — only ``minimum`` is set.
    * ``n > <maximum>`` — only ``maximum`` is set.
    * ``n < <minimum> OR n > <maximum>`` — both set.

    This is the **failing-rows contract the other 4 built-in tests follow**.
    The adapter wraps every compiler output as
    ``SELECT COUNT(*) AS failures FROM (<sql>) AS t`` (BigQueryAdapter /
    SnowflakeAdapter): zero rows from the inner SELECT → ``failures=0`` →
    engine routes ``always-passes``; one row → ``failures=1`` → engine
    routes ``kept`` (or ``failed-on-known-clean-data`` on a trusted model).

    **The previous shape (``SELECT COUNT(*) FROM <table> [WHERE <where>]``)
    was a bug** (US-007a). Wrapped, that became
    ``SELECT COUNT(*) AS failures FROM (SELECT COUNT(*) FROM <table>) AS t``
    — the inner returns 1 row (the count), the outer ``COUNT(*)`` is
    always 1, so ``failures`` was always 1 regardless of bounds. The engine
    routed every real ``row_count_between`` to ``kept`` (or
    ``failed-on-known-clean-data`` if trusted) without ever checking the
    bounds. The CTE+WHERE shape pushes the bound check into the inner
    SELECT so the outer COUNT(*) reflects the real verdict.

    **Sample-mode is deliberately bypassed (DEC-003, corrected post-QG).**
    The compiled SQL is identical regardless of ``prune.scope`` — a sampled
    ``COUNT(*)`` is semantically wrong (a bucket-mod'd subset cannot be
    compared against the full-table bounds). **The engine routes
    ``row_count_between`` past the materialised-sample substitution
    entirely** — ``prune_tests`` overrides ``table_ref`` to the SOURCE
    table for this variant in every scope/strategy combination because a
    COUNT(*) against a materialised sample returns the SAMPLE SIZE
    (typically 100K rows), not the model's true row count, and bounds
    checked against sample size are meaningless. The COUNT(*) against the
    source is a single aggregate scan — cheap even on petabyte tables —
    so there's no cost argument for routing through the temp table. The
    materialised-sample contract from #116 still applies to the other
    five test types (``not_null`` / ``unique`` / ``accepted_values`` /
    ``relationships`` / ``custom_sql``) which read row-level data the
    sample faithfully represents.

    **DEC-005 — compose-then-validate.** ``where`` is freeform LLM- or
    operator-supplied SQL (e.g. ``"event_date >= '2024-01-01'"``). We
    compose the full failing-rows SELECT THEN call the existing
    :func:`signalforge.warehouse._sql_safety.validate_test_sql` on it. The
    composed-then-validated path catches every shape ``validate_test_sql``
    catches (stray ``;`` / ``--`` / ``/* */`` / unbalanced parens) without
    rolling a separate ``validate_where_fragment`` helper — reusing the
    existing surface keeps the cheap-rejects rules in lockstep across
    ``custom_sql`` and ``row_count_between``.

    A safety-rejected composed SQL routes via :class:`_InvalidIdentifier` to
    ``kept-without-evidence`` (DEC-011): the LLM proposed the test; absent
    a clean ``where`` we cannot evaluate it, but we ship it so the operator
    can fix the prompt or hand-edit the rule. Mirrors the ``custom_sql``
    conservative-bias routing precedent.
    """
    table = _qualified_table_name(table_ref, dialect)
    if test.where is None:
        inner = f"SELECT COUNT(*) AS n FROM {table}"
    else:
        inner = f"SELECT COUNT(*) AS n FROM {table} WHERE {test.where}"
    # Three-clause bound-violation predicate: at least one of (minimum,
    # maximum) is set (CandidateTestRowCountBetween validates this at
    # construction time).
    if test.minimum is not None and test.maximum is not None:
        predicate = f"n < {test.minimum} OR n > {test.maximum}"
    elif test.minimum is not None:
        predicate = f"n < {test.minimum}"
    else:
        # test.maximum is not None — guaranteed by the model's
        # _bounds_consistent validator.
        predicate = f"n > {test.maximum}"
    sql = f"SELECT n FROM ({inner}) AS rc WHERE {predicate}"
    try:
        validate_test_sql(sql)
    except QuerySyntaxError:
        return _InvalidIdentifier(
            reason="row_count_between rejected by SQL safety check on composed SQL"
        )
    return sql


def _compile_unique_combination(
    test: CandidateTestUniqueCombination,
    table_ref: TableRef,
    dialect: Dialect,
) -> str | _InvalidIdentifier:
    """Compile ``unique_combination(columns, where?)`` to a composite-grain
    GROUP BY ... HAVING COUNT(*) > 1 (#170, DEC-014 / DEC-015).

    Emits::

        SELECT <quoted_cols> FROM <table_ref> [WHERE <where>]
            GROUP BY <quoted_cols> HAVING COUNT(*) > 1

    Each ``columns[i]`` is shape-validated via
    :func:`signalforge.warehouse._sql_safety.validate_identifier`
    (DEC-014 defence-in-depth — the anchor-contract arm already checks
    column existence against the manifest model, but identifier shape is
    a separate guard against backtick/whitespace/quote break-out) and
    then folded + quoted per :attr:`Dialect.identifier_case` and
    :attr:`Dialect.quote_char`. Any malformed column identifier routes
    via :class:`_InvalidIdentifier` to ``kept-without-evidence`` —
    the LLM may have proposed a useful tuple with a typo'd identifier
    that the operator can repair, so we ship rather than drop (DEC-011 of
    issue #6).

    **DEC-015 compose-then-validate.** ``where`` is freeform LLM- or
    operator-supplied SQL. The compiler composes the full SELECT THEN
    routes the WHOLE statement through
    :func:`signalforge.warehouse._sql_safety.validate_test_sql` — the
    same reuse pattern :func:`_compile_row_count_between` follows (#169
    DEC-005). A hostile ``where`` containing ``;`` / ``--`` / ``/* */``
    / unbalanced parens fails the cheap-rejects scan on the composed SQL
    and routes via :class:`_InvalidIdentifier` to
    ``kept-without-evidence`` (#170 DEC-015).

    **No automatic NULL-exclusion.** Unlike the single-column ``unique``
    variant (DEC-023 — dbt-core convention), composite uniqueness does
    NOT inject an ``IS NOT NULL`` filter. This matches the
    ``dbt_utils.unique_combination_of_columns`` macro's default
    behaviour — operators that want NULL filtering supply it via the
    ``where`` field.

    **Sample-mode is out of scope for the compiler.** The engine
    (US-005b) routes ``unique_combination`` to the source table under
    both ``materialised`` and ``oneshot`` sample strategies (composite
    uniqueness on a bucket-mod'd subset has false-negative risk because
    a duplicate pair may straddle the sampled and unsampled rows). The
    compiler just consumes ``table_ref`` as-is; whether it resolves to
    the source or to a materialised sample is the engine's call.
    """
    # DEC-014 — per-column identifier shape gate, defence-in-depth on top
    # of the anchor-contract arm in ``signalforge.draft.parser``.
    for col in test.columns:
        try:
            validate_identifier("CandidateTestUniqueCombination.columns", col)
        except InvalidIdentifierError:
            return _InvalidIdentifier(
                reason=(f"candidate test references an invalid identifier shape: column={col!r}")
            )
    cols_sql = ", ".join(_quote(col, dialect) for col in test.columns)
    table = _qualified_table_name(table_ref, dialect)
    if test.where is None:
        sql = f"SELECT {cols_sql} FROM {table} GROUP BY {cols_sql} HAVING COUNT(*) > 1"
    else:
        sql = (
            f"SELECT {cols_sql} FROM {table} WHERE {test.where} "
            f"GROUP BY {cols_sql} HAVING COUNT(*) > 1"
        )
    # DEC-015 — compose-then-validate. The composed statement (not just
    # the ``where`` fragment) is what reaches the warehouse, so the safety
    # check fires on the assembled SQL. Mirrors ``_compile_row_count_between``.
    try:
        validate_test_sql(sql)
    except QuerySyntaxError:
        return _InvalidIdentifier(
            reason="unique_combination rejected by SQL safety check on composed SQL"
        )
    return sql


# ---------------------------------------------------------------------------
# Issue #171 — row_count_anomaly_by_period (#171 US-008, DEC-008 + DEC-011 +
# DEC-012). The 8th first-class variant compiles into TWO SQL strings per
# DEC-008: a per-method stats query (one row of stats; per-DOW when
# ``seasonality="dow"``) AND a violation query (rows in today's period — the
# adapter's COUNT(*) wrap yields today's row count, which the engine then
# compares against the band derived from the stats query in US-011). Every
# emitted query carries the DEC-012 partition-pruning WHERE clause so a
# 28-period lookback on a 1B-row daily-partitioned table scans only the
# touched partitions (~30× scan reduction) rather than the full table.
#
# Dialect-driven (DEC-011): every date-arithmetic / percentile SQL fragment
# is read from :class:`Dialect` (``date_trunc_expr_template``,
# ``interval_expr_template``, ``extract_dow_expr_template``,
# ``dow_sunday_index``, ``percentile_cont_expr_template``). The compiler
# NEVER branches on ``dialect.name`` — the
# :mod:`tests.prune.test_compiler_import_guard` AST gate is the regression
# fence for any future arm that reaches for a vendor SDK.
#
# Conservative-bias routing (DEC-005 of #169 generalised): a ``where`` clause
# composed into the full SELECT that trips
# :func:`signalforge.warehouse._sql_safety.validate_test_sql` (stray ``;`` /
# ``--`` / ``/* */`` / unbalanced parens) returns ``_InvalidIdentifier`` →
# engine routes to ``kept-without-evidence`` (mirrors ``custom_sql`` /
# ``row_count_between`` / ``unique_combination``).
# ---------------------------------------------------------------------------


def _period_unit_keyword(period: str) -> str:
    """Translate a ``CandidateTestRowCountAnomalyByPeriod.period`` value to
    the SQL keyword both dialects accept inside ``DATE_TRUNC`` / ``INTERVAL``.

    All three valid values (``hour`` / ``day`` / ``week``) map to their
    uppercase form (``HOUR`` / ``DAY`` / ``WEEK``) — these are the standard
    SQL keywords BigQuery, Snowflake, and Postgres all accept. The translation
    is dialect-neutral; per-dialect framing (e.g. Snowflake's single-quoting
    of the unit inside ``DATE_TRUNC``) happens at the
    :attr:`Dialect.date_trunc_expr_template` substitution site.
    """
    return period.upper()


def _date_value_literal(value_iso: str, column_type: str | None, dialect: Dialect) -> str:
    """Render a date/timestamp literal whose TYPE matches the date column.

    The DEC-012 partition-pruning predicate compares the bare (un-CAST) date
    column against this literal, so the literal's type MUST match the column's
    type or a strict-typed warehouse (BigQuery) rejects the comparison
    (``No matching signature for >=: TIMESTAMP, DATE``). CASTing the *column*
    would fix the type mismatch but disable partition pruning — exactly the
    cost regression DEC-012 exists to prevent — so we type-match the *literal*
    instead, leaving the column bare.

    ``column_type`` is the date column's ``data_type`` (from the manifest /
    catalog merge, issue #159). When it is unknown (no ``data_type``) we fall
    back to a DATE literal — the historical behaviour — which a non-DATE column
    will reject at query time, routing the test to ``kept-without-evidence``
    (graceful degrade, unchanged from today).
    """
    normalised = (column_type or "").strip().upper()
    if normalised.startswith("TIMESTAMP"):
        template = dialect.timestamp_literal_template
    elif normalised.startswith("DATETIME"):
        template = dialect.datetime_literal_template
    else:  # "DATE", unknown, or any other type → DATE-literal fallback
        template = dialect.date_literal_template
    return template.format(value=value_iso)


def _render_as_of_literal(
    as_of: date, period: str, dialect: Dialect, *, column_type: str | None = None
) -> str:
    """Render the ``as_of`` literal, period-aligned for ``week`` / ``hour``.

    Per #171 CodeRabbit finding #4: when ``period`` is ``week`` or ``hour``,
    a raw ``as_of`` value (e.g. ``2026-05-15``, a Thursday) gives a
    semantically muddled "current period" window: the violation query covers
    ``[2026-05-15, 2026-05-22)``, which is neither a calendar week nor a
    natural Mon–Sun span. Wrapping the literal in ``DATE_TRUNC(<lit>, <unit>)``
    aligns it to the natural period boundary so the stats and violation
    windows are consistent.

    For ``period="day"`` the truncation is a no-op (a ``date`` literal is
    already at day-boundary 00:00:00); we skip the wrap so existing snapshots
    stay byte-equal. For ``period="hour"`` the truncation runs but **note**:
    ``as_of`` is a ``date`` (not a ``datetime``), so ``DATE_TRUNC(<date>,
    HOUR)`` is dialect-divergent (BigQuery rejects, Snowflake returns a
    timestamp at midnight). Hour-period anomaly tests are a v0.x limitation
    — document; the workaround for hourly cadence is `period="day"` with the
    operator's choice of `as_of` reflecting their preferred hour boundary.
    """
    bare = _date_value_literal(as_of.isoformat(), column_type, dialect)
    if period == "day":
        return bare
    unit = _period_unit_keyword(period)
    return dialect.date_trunc_expr_template.format(date=bare, unit=unit)


def _render_anomaly_stats_partition_filter(
    *,
    date_column_quoted: str,
    as_of: date,
    lookback_periods: int,
    period: str,
    dialect: Dialect,
    column_type: str | None = None,
) -> str:
    """Render the load-bearing DEC-012 partition-pruning WHERE fragment
    for the stats (history) query.

    Returns a SQL fragment of the shape::

        <date_column> >= <as_of_literal> - INTERVAL <lookback> <unit>
        AND <date_column> < <as_of_literal>

    The stats query is history-only — it excludes the current period
    (the violation query covers today separately).

    The ``<as_of_literal>`` is rendered via
    :attr:`Dialect.date_literal_template` so BigQuery emits ``DATE('…')`` and
    Snowflake emits ``'…'::DATE``. The interval is rendered via
    :attr:`Dialect.interval_expr_template` so BigQuery emits bare
    ``INTERVAL 28 DAY`` and Snowflake emits the quoted ``INTERVAL '28 DAY'``
    form. The compiler NEVER branches on ``dialect.name`` — both surfaces
    are dialect templates.
    """
    as_of_literal = _render_as_of_literal(as_of, period, dialect, column_type=column_type)
    unit = _period_unit_keyword(period)
    lookback_interval = dialect.interval_expr_template.format(n=lookback_periods, unit=unit)
    return (
        f"{date_column_quoted} >= {as_of_literal} - {lookback_interval} "
        f"AND {date_column_quoted} < {as_of_literal}"
    )


def _render_anomaly_history_cte(
    *,
    date_column_quoted: str,
    table_sql: str,
    as_of: date,
    lookback_periods: int,
    period: str,
    seasonality: str,
    where: str | None,
    dialect: Dialect,
    column_type: str | None = None,
) -> str:
    """Render the ``history`` CTE common to all four methods.

    Two shapes, switched by ``seasonality``:

    * ``seasonality="none"`` — ``SELECT DATE_TRUNC(<date>, <unit>) AS period,
      COUNT(*) AS cnt FROM <table> WHERE <partition_filter> [AND <where>]
      GROUP BY period``.
    * ``seasonality="dow"`` — additionally projects + groups by
      ``EXTRACT(DAYOFWEEK FROM <date>) AS dow``.

    ``date_column_quoted`` is already dialect-folded + quoted; ``table_sql``
    is the dialect-correct qualified table expression.
    """
    trunc_expr = dialect.date_trunc_expr_template.format(
        date=date_column_quoted, unit=_period_unit_keyword(period)
    )
    partition_pred = _render_anomaly_stats_partition_filter(
        date_column_quoted=date_column_quoted,
        as_of=as_of,
        lookback_periods=lookback_periods,
        period=period,
        dialect=dialect,
        column_type=column_type,
    )
    where_clause = partition_pred if where is None else f"{partition_pred} AND {where}"

    if seasonality == "dow":
        dow_expr = dialect.extract_dow_expr_template.format(date=date_column_quoted)
        select_list = f"{trunc_expr} AS period, {dow_expr} AS dow, COUNT(*) AS cnt"
        group_by = "period, dow"
    else:
        select_list = f"{trunc_expr} AS period, COUNT(*) AS cnt"
        group_by = "period"

    return (
        f"history AS (SELECT {select_list} FROM {table_sql} "
        f"WHERE {where_clause} GROUP BY {group_by})"
    )


def _percentile_expr(p: float, order_expr: str, dialect: Dialect) -> str:
    """Render a GROUP-BY percentile expression from the dialect template.

    BigQuery has no ordered-set-aggregate ``PERCENTILE_CONT`` (it is
    window-only and cannot reduce rows in a ``GROUP BY``), so its template is
    ``APPROX_QUANTILES(expr, 100)[OFFSET(<offset>)]`` where ``<offset>`` is the
    bucket index for percentile ``p`` into the 101-element quantile array;
    Snowflake / Postgres use the standard-SQL ``PERCENTILE_CONT(p) WITHIN GROUP
    (ORDER BY expr)`` ordered-set form. The compiler never branches on
    ``dialect.name`` — both surfaces are dialect templates; ``{offset}`` is
    ignored by the WITHIN GROUP form and ``{p}`` is ignored by the
    APPROX_QUANTILES form.

    The offset is ``round-half-up`` (``int(p * 100 + 0.5)``), NOT Python's
    built-in ``round`` (banker's rounding, ties-to-even): a half-integer bucket
    such as ``p=0.125`` → ``12.5`` resolves to ``OFFSET(13)`` (away from zero),
    matching the conventional percentile-rounding expectation rather than
    silently rounding to the even ``12``. All realistic inputs (``p=0.5`` for
    the median; integer ``threshold`` percentiles) land on exact integers, so
    this only affects fractional thresholds.
    """
    offset = int(p * 100 + 0.5)
    return dialect.percentile_cont_expr_template.format(p=p, expr=order_expr, offset=offset)


def _compile_anomaly_stats_query(
    test: CandidateTestRowCountAnomalyByPeriod,
    table_ref: TableRef,
    dialect: Dialect,
    *,
    as_of: date,
    date_column_type: str | None = None,
) -> str:
    """Render the stats query for a ``row_count_anomaly_by_period`` test.

    Per-method per-seasonality output shapes (one CTE structure per method,
    shared ``history`` CTE):

    * ``mad``    → SELECT median, mad, n
    * ``zscore`` → SELECT mean, stddev, n
    * ``percentile`` → SELECT p_lo, p_hi, n (``p_lo = threshold / 100``;
      ``p_hi = 1 - p_lo``)
    * ``min_max`` → SELECT min_cnt, max_cnt, n

    Under ``seasonality="dow"`` the SELECT carries an additional ``dow``
    column and the per-method aggregates run per ``dow`` (the engine matches
    today's DOW against the historical per-DOW band in US-011).

    The compiler emits the SQL dictated by :class:`Dialect` (DEC-011) —
    NEVER branches on ``dialect.name``.
    """
    date_column_quoted = _quote(test.date_column, dialect)
    table_sql = _qualified_table_name(table_ref, dialect)
    history_cte = _render_anomaly_history_cte(
        date_column_quoted=date_column_quoted,
        table_sql=table_sql,
        as_of=as_of,
        lookback_periods=test.lookback_periods,
        period=test.period,
        seasonality=test.seasonality,
        where=test.where,
        dialect=dialect,
        column_type=date_column_type,
    )

    seasonality_dow = test.seasonality == "dow"
    dow_select = "dow, " if seasonality_dow else ""
    dow_group_suffix = " GROUP BY dow" if seasonality_dow else ""

    if test.method == "mad":
        median_expr = _percentile_expr(0.5, "cnt", dialect)
        if seasonality_dow:
            # Per-DOW: medians (per dow), mads (per dow), counts (per dow).
            medians_cte = (
                f"medians AS (SELECT dow, {median_expr} AS median FROM history GROUP BY dow)"
            )
            abs_dev_expr = "ABS(history.cnt - medians.median)"
            mad_expr = _percentile_expr(0.5, abs_dev_expr, dialect)
            mads_cte = (
                "mads AS (SELECT history.dow AS dow, "
                f"{mad_expr} AS mad "
                "FROM history JOIN medians ON history.dow = medians.dow "
                "GROUP BY history.dow)"
            )
            counts_cte = "counts AS (SELECT dow, COUNT(*) AS n FROM history GROUP BY dow)"
            final = (
                "SELECT medians.dow AS dow, medians.median AS median, mads.mad AS mad, "
                "counts.n AS n FROM medians "
                "JOIN mads ON medians.dow = mads.dow "
                "JOIN counts ON medians.dow = counts.dow"
            )
            return f"WITH {history_cte}, {medians_cte}, {mads_cte}, {counts_cte} {final}"
        # seasonality="none"
        medians_cte = f"medians AS (SELECT {median_expr} AS median FROM history)"
        abs_dev_expr = "ABS(cnt - (SELECT median FROM medians))"
        mad_expr = _percentile_expr(0.5, abs_dev_expr, dialect)
        final = (
            f"SELECT (SELECT median FROM medians) AS median, "
            f"{mad_expr} AS mad, "
            f"COUNT(*) AS n FROM history"
        )
        return f"WITH {history_cte}, {medians_cte} {final}"

    if test.method == "zscore":
        if seasonality_dow:
            final = (
                "SELECT dow, AVG(cnt) AS mean, STDDEV(cnt) AS stddev, COUNT(*) AS n "
                "FROM history GROUP BY dow"
            )
            return f"WITH {history_cte} {final}"
        final = "SELECT AVG(cnt) AS mean, STDDEV(cnt) AS stddev, COUNT(*) AS n FROM history"
        return f"WITH {history_cte} {final}"

    if test.method == "percentile":
        p_lo = test.threshold / 100.0
        p_hi = 1.0 - p_lo
        p_lo_expr = _percentile_expr(p_lo, "cnt", dialect)
        p_hi_expr = _percentile_expr(p_hi, "cnt", dialect)
        final = (
            f"SELECT {dow_select}{p_lo_expr} AS p_lo, {p_hi_expr} AS p_hi, COUNT(*) AS n "
            f"FROM history{dow_group_suffix}"
        )
        return f"WITH {history_cte} {final}"

    # test.method == "min_max" (the discriminated literal is closed at the
    # variant level — Pydantic rejects any other value at construction time).
    final = (
        f"SELECT {dow_select}MIN(cnt) AS min_cnt, MAX(cnt) AS max_cnt, COUNT(*) AS n "
        f"FROM history{dow_group_suffix}"
    )
    return f"WITH {history_cte} {final}"


def _compile_anomaly_violation_query(
    test: CandidateTestRowCountAnomalyByPeriod,
    table_ref: TableRef,
    dialect: Dialect,
    *,
    as_of: date,
    date_column_type: str | None = None,
) -> str:
    """Render the violation query for a ``row_count_anomaly_by_period`` test.

    Returns ``SELECT 1 FROM <table> WHERE <date> >= <as_of> AND <date> <
    <as_of> + INTERVAL 1 <period> [AND <where>]``. The adapter wraps it as
    ``SELECT COUNT(*) AS failures FROM (<query>) AS t``, which yields the
    count of rows in TODAY's period — the engine (US-011) compares that
    count against the band derived from the stats query.

    The ``SELECT 1`` projection (instead of ``SELECT *``) is deliberate:
    the outer ``COUNT(*)`` only needs row existence, and ``1`` keeps the
    inner shape trivial (no column-name escaping required, no risk of
    duplicate-column-alias errors on Snowflake).
    """
    date_column_quoted = _quote(test.date_column, dialect)
    table_sql = _qualified_table_name(table_ref, dialect)
    # The violation query is bounded to today's period only. The bound
    # ``[as_of, as_of + INTERVAL 1 <unit>)`` is the period that contains
    # ``as_of`` (assuming ``as_of`` lands on a period boundary — daily
    # ``as_of`` is a date at 00:00, which is the boundary by construction).
    # The DEC-012 partition-pruning shape inverts the upper bound vs. the
    # stats query: stats excludes today, violation IS today.
    as_of_literal = _render_as_of_literal(as_of, test.period, dialect, column_type=date_column_type)
    unit = _period_unit_keyword(test.period)
    today_interval = dialect.interval_expr_template.format(n=1, unit=unit)
    today_only = (
        f"{date_column_quoted} >= {as_of_literal} "
        f"AND {date_column_quoted} < {as_of_literal} + {today_interval}"
    )
    where_clause = today_only if test.where is None else f"{today_only} AND {test.where}"
    return f"SELECT 1 FROM {table_sql} WHERE {where_clause}"


def _render_anomaly_today_cte(
    *,
    date_column_quoted: str,
    table_sql: str,
    as_of: date,
    period: str,
    where: str | None,
    dialect: Dialect,
    include_dow: bool,
    column_type: str | None = None,
) -> str:
    """Render the ``today`` CTE used by the singular-test SQL emitter.

    Returns one row containing the count (and, when ``include_dow`` is True,
    today's DOW) of the period that contains ``as_of`` (the upper bound is
    ``as_of + INTERVAL 1 <unit>`` so the window is half-open ``[as_of, …)``,
    aligned to the natural period boundary by :func:`_render_as_of_literal`).

    When ``where`` is non-None, the predicate appends so the today count
    reflects the same filtered universe as the history CTE.
    """
    as_of_literal = _render_as_of_literal(as_of, period, dialect, column_type=column_type)
    unit = _period_unit_keyword(period)
    today_interval = dialect.interval_expr_template.format(n=1, unit=unit)
    today_pred = (
        f"{date_column_quoted} >= {as_of_literal} "
        f"AND {date_column_quoted} < {as_of_literal} + {today_interval}"
    )
    where_pred = today_pred if where is None else f"{today_pred} AND {where}"
    if include_dow:
        # #171 CodeRabbit finding #13 (zero-row seasonal bug): derive ``dow``
        # from the anchored ``as_of`` LITERAL — NOT from the filtered table
        # rows. Why: when today's period is empty, ``COUNT(*)`` is ``0`` but
        # ``MAX(EXTRACT(DOW FROM <col>))`` is ``NULL`` (no rows to extract
        # from). The downstream ``stats.dow = today.dow`` JOIN then drops
        # every stats row (NULL-comparison) and the test silently passes —
        # even though a zero-count period IS itself a meaningful anomaly to
        # surface (catastrophic load failure). Anchoring the DOW computation
        # on the ``as_of`` literal makes it a compile-time constant; the
        # band check fires correctly when the period contains zero rows.
        dow_expr = dialect.extract_dow_expr_template.format(date=as_of_literal)
        select_list = f"COUNT(*) AS cnt, {dow_expr} AS dow"
    else:
        select_list = "COUNT(*) AS cnt"
    return f"today AS (SELECT {select_list} FROM {table_sql} WHERE {where_pred})"


def _render_anomaly_band_violation_predicate(
    *,
    method: str,
    threshold: float,
    seasonality: str,
) -> str:
    """Render the WHERE predicate that selects "today's count is outside the
    historical band derived by the chosen method." Returns the SQL fragment
    that follows ``stats.n >= <min_samples> AND ``.

    Per-method predicates (the stats CTE exposes the relevant aggregates;
    the today CTE exposes ``today.cnt``):

    * ``mad`` — Iglewicz & Hoaglin modified z-score:
      ``ABS(0.6745 * (today.cnt - stats.median)) > threshold * NULLIF(stats.mad, 0)``.
      The ``NULLIF`` guards against the degenerate ``MAD=0`` case (every
      historical period had identical count) — NULL-typed comparisons
      yield NULL → predicate false → no anomaly row → test passes silently,
      matching the conservative-bias contract.
    * ``zscore`` — ``ABS(today.cnt - stats.mean) > threshold * NULLIF(stats.stddev, 0)``.
    * ``percentile`` — ``today.cnt < stats.p_lo OR today.cnt > stats.p_hi``.
      ``threshold`` is the percentile half-band width in points (see
      ``CandidateTestRowCountAnomalyByPeriod`` docstring).
    * ``min_max`` — ``today.cnt < stats.min_cnt OR today.cnt > stats.max_cnt``.
      ``threshold`` is ignored for ``min_max``.
    """
    if method == "mad":
        return f"ABS(0.6745 * (today.cnt - stats.median)) > {threshold} * NULLIF(stats.mad, 0)"
    if method == "zscore":
        return f"ABS(today.cnt - stats.mean) > {threshold} * NULLIF(stats.stddev, 0)"
    if method == "percentile":
        return "today.cnt < stats.p_lo OR today.cnt > stats.p_hi"
    # min_max — threshold is ignored per the variant docstring.
    return "today.cnt < stats.min_cnt OR today.cnt > stats.max_cnt"


def _compile_anomaly_singular_test_sql(
    test: CandidateTestRowCountAnomalyByPeriod,
    table_ref: TableRef,
    dialect: Dialect,
    *,
    as_of: date,
    date_column_type: str | None = None,
) -> str:
    """Render a STANDALONE dbt-singular-test SQL for the variant.

    Distinct from :func:`_compile_anomaly_violation_query` (which is the
    engine-side "rows in today's period" query, paired with the stats query
    for the two-query split): this emits a SINGLE self-contained SQL that
    returns ``0`` rows when today's count is within the historical band and
    ``>= 1`` row when out-of-band — the dbt singular-test contract (#171
    Copilot findings #8 + #9).

    The shape combines the history CTE + the same per-method stats CTEs as
    the engine's stats query + a fresh ``today`` CTE + a final SELECT
    predicated on the band-violation check::

        WITH history AS (...),
             <per-method stats CTEs>,
             today AS (SELECT COUNT(*) FROM <table> WHERE today-only)
        SELECT 'row_count_anomaly_by_period' AS signalforge_test,
               today.cnt AS today_cnt, <stats fields>
        FROM today CROSS JOIN stats
        WHERE stats.n >= <min_samples_per_bucket>
          AND <method-specific band-violation predicate>

    The ``signalforge_test`` literal projection makes the failing-rows
    output legible in dbt's test-failures viewer.

    When ``stats.n < min_samples_per_bucket`` (cold-start), the WHERE
    short-circuits to 0 rows → the test passes silently. This matches the
    engine-side conservative-bias contract (cold-start is "no signal,"
    NOT "always-passes"); the operator sees a passing test until enough
    history accumulates.

    Seasonal (``seasonality="dow"``) variants project today's DOW from the
    today CTE and JOIN against the per-DOW stats; the band-violation
    predicate fires only for today's DOW row.

    The hardcoded ``as_of`` value is baked into the emitted SQL — the test
    answers "was the period containing ``as_of`` anomalous given the history
    before ``as_of``?" This matches #171's reproducibility carve-out at
    ``(model, as_of)`` granularity. Re-running ``signalforge generate
    --as-of <date>`` with a different date emits a different test file.
    """
    date_column_quoted = _quote(test.date_column, dialect)
    table_sql = _qualified_table_name(table_ref, dialect)
    seasonal = test.seasonality == "dow"

    history_cte = _render_anomaly_history_cte(
        date_column_quoted=date_column_quoted,
        table_sql=table_sql,
        as_of=as_of,
        lookback_periods=test.lookback_periods,
        period=test.period,
        seasonality=test.seasonality,
        where=test.where,
        dialect=dialect,
        column_type=date_column_type,
    )
    today_cte = _render_anomaly_today_cte(
        date_column_quoted=date_column_quoted,
        table_sql=table_sql,
        as_of=as_of,
        period=test.period,
        where=test.where,
        dialect=dialect,
        include_dow=seasonal,
        column_type=date_column_type,
    )
    band_predicate = _render_anomaly_band_violation_predicate(
        method=test.method,
        threshold=test.threshold,
        seasonality=test.seasonality,
    )

    # Per-method stats CTE + final SELECT projection of the stats fields
    # the band-predicate references. Reuses the same percentile/mean/stddev/
    # min/max SQL as ``_compile_anomaly_stats_query`` but condensed to a
    # single ``stats`` CTE (no per-DOW JOIN gymnastics — seasonal singular
    # tests filter ``stats`` by ``today.dow``).
    dow_select_prefix = "stats.dow AS dow, " if seasonal else ""
    dow_group_suffix = " GROUP BY dow" if seasonal else ""
    dow_select = "dow, " if seasonal else ""
    dow_join_pred = " AND stats.dow = today.dow" if seasonal else ""

    if test.method == "mad":
        median_expr = _percentile_expr(0.5, "cnt", dialect)
        if seasonal:
            medians_cte = (
                f"medians AS (SELECT dow, {median_expr} AS median FROM history GROUP BY dow)"
            )
            mad_expr = _percentile_expr(0.5, "ABS(history.cnt - medians.median)", dialect)
            mads_cte = (
                "mads AS (SELECT history.dow AS dow, "
                f"{mad_expr} AS mad "
                "FROM history JOIN medians ON history.dow = medians.dow "
                "GROUP BY history.dow)"
            )
            counts_cte = "counts AS (SELECT dow, COUNT(*) AS n FROM history GROUP BY dow)"
            stats_cte = (
                "stats AS (SELECT medians.dow AS dow, medians.median AS median, "
                "mads.mad AS mad, counts.n AS n "
                "FROM medians JOIN mads ON medians.dow = mads.dow "
                "JOIN counts ON medians.dow = counts.dow)"
            )
            ctes = (
                f"{history_cte}, {medians_cte}, {mads_cte}, {counts_cte}, {stats_cte}, {today_cte}"
            )
        else:
            medians_cte = f"medians AS (SELECT {median_expr} AS median FROM history)"
            mad_expr = _percentile_expr(0.5, "ABS(cnt - (SELECT median FROM medians))", dialect)
            stats_cte = (
                f"stats AS (SELECT (SELECT median FROM medians) AS median, "
                f"{mad_expr} AS mad, COUNT(*) AS n FROM history)"
            )
            ctes = f"{history_cte}, {medians_cte}, {stats_cte}, {today_cte}"
        select_fields = (
            f"{dow_select_prefix}today.cnt AS today_cnt, "
            "stats.median AS median, stats.mad AS mad, stats.n AS n"
        )
    elif test.method == "zscore":
        stats_cte = (
            f"stats AS (SELECT {dow_select}AVG(cnt) AS mean, "
            f"STDDEV(cnt) AS stddev, COUNT(*) AS n FROM history{dow_group_suffix})"
        )
        ctes = f"{history_cte}, {stats_cte}, {today_cte}"
        select_fields = (
            f"{dow_select_prefix}today.cnt AS today_cnt, "
            "stats.mean AS mean, stats.stddev AS stddev, stats.n AS n"
        )
    elif test.method == "percentile":
        p_lo = test.threshold / 100.0
        p_hi = 1.0 - p_lo
        p_lo_expr = _percentile_expr(p_lo, "cnt", dialect)
        p_hi_expr = _percentile_expr(p_hi, "cnt", dialect)
        stats_cte = (
            f"stats AS (SELECT {dow_select}{p_lo_expr} AS p_lo, "
            f"{p_hi_expr} AS p_hi, COUNT(*) AS n FROM history{dow_group_suffix})"
        )
        ctes = f"{history_cte}, {stats_cte}, {today_cte}"
        select_fields = (
            f"{dow_select_prefix}today.cnt AS today_cnt, "
            "stats.p_lo AS p_lo, stats.p_hi AS p_hi, stats.n AS n"
        )
    else:  # min_max
        stats_cte = (
            f"stats AS (SELECT {dow_select}MIN(cnt) AS min_cnt, "
            f"MAX(cnt) AS max_cnt, COUNT(*) AS n FROM history{dow_group_suffix})"
        )
        ctes = f"{history_cte}, {stats_cte}, {today_cte}"
        select_fields = (
            f"{dow_select_prefix}today.cnt AS today_cnt, "
            "stats.min_cnt AS min_cnt, stats.max_cnt AS max_cnt, stats.n AS n"
        )

    return (
        f"WITH {ctes} "
        f"SELECT 'row_count_anomaly_by_period' AS signalforge_test, "
        f"{select_fields} "
        f"FROM today CROSS JOIN stats "
        f"WHERE stats.n >= {test.min_samples_per_bucket}{dow_join_pred} "
        f"AND ({band_predicate})"
    )


def _compile_row_count_anomaly_by_period(
    test: CandidateTestRowCountAnomalyByPeriod,
    table_ref: TableRef,
    dialect: Dialect,
    *,
    as_of: date,
    date_column_type: str | None = None,
) -> tuple[str, str] | _InvalidIdentifier:
    """Compile a ``row_count_anomaly_by_period`` test to the
    ``(stats_sql, violation_sql)`` tuple per DEC-008.

    Returns the tuple on success; returns :class:`_InvalidIdentifier` when:

    * ``test.date_column`` fails the SQL-identifier shape check (DEC-013
      defence-in-depth — the anchor-contract arm validates membership but
      not regex shape).
    * The composed stats or violation SQL trips
      :func:`signalforge.warehouse._sql_safety.validate_test_sql` on a hostile
      ``where`` (stray ``;`` / ``--`` / unbalanced parens).

    The dispatcher treats the tuple as a positive compile result; the
    engine (US-011) handles the two-query split (cold-start gate on
    ``stats.n_periods``, then violation-query run).
    """
    try:
        validate_identifier("CandidateTestRowCountAnomalyByPeriod.date_column", test.date_column)
    except InvalidIdentifierError:
        return _InvalidIdentifier(
            reason=(
                "candidate test references an invalid identifier shape: "
                f"date_column={test.date_column!r}"
            )
        )

    # #171 CodeRabbit finding #12: ``period="hour"`` with a ``date``-typed
    # ``as_of`` emits ``DATE_TRUNC(DATE '<...>', HOUR)`` which is INVALID
    # on BigQuery (DATE_TRUNC of DATE only accepts year/month/week/day
    # granularity; HOUR requires DATETIME/TIMESTAMP). Snowflake accepts but
    # returns TIMESTAMP semantics that diverge from the day-anchored as_of.
    # Route to ``kept-without-evidence`` per the conservative-bias contract
    # until a future ticket lets ``as_of`` be a ``datetime``.
    if test.period == "hour":
        return _InvalidIdentifier(
            reason=(
                "row_count_anomaly_by_period with period='hour' requires a "
                "datetime-typed as_of which is not yet supported "
                "(v0.x ships day/week only — see docs/prune-ops.md)"
            )
        )

    # ``date_column_type`` (the date column's warehouse ``data_type``, resolved
    # by ``_compile_test`` from the manifest / catalog merge of issue #159)
    # TYPE-MATCHES the partition-pruning bound literal to the column — a
    # TIMESTAMP column compared against a DATE literal is rejected by BigQuery,
    # and CASTing the column to DATE would disable partition pruning (DEC-012).
    # Unknown type → DATE-literal fallback (the historical behaviour), which
    # degrades gracefully if the column is not a DATE.
    stats_sql = _compile_anomaly_stats_query(
        test, table_ref, dialect, as_of=as_of, date_column_type=date_column_type
    )
    violation_sql = _compile_anomaly_violation_query(
        test, table_ref, dialect, as_of=as_of, date_column_type=date_column_type
    )

    # Compose-then-validate (DEC-005 of #169 generalised): a hostile ``where``
    # surfaces on the assembled SQL via the cheap-rejects scan. Re-using
    # ``validate_test_sql`` keeps the surface in lockstep with the other
    # variants' compose-then-validate seams. Either query's rejection routes
    # the whole test to ``kept-without-evidence``.
    try:
        validate_test_sql(stats_sql)
        validate_test_sql(violation_sql)
    except QuerySyntaxError:
        return _InvalidIdentifier(
            reason=("row_count_anomaly_by_period rejected by SQL safety check on composed SQL")
        )
    return (stats_sql, violation_sql)


def _compile_test(
    test: CandidateTest,
    table_ref: TableRef,
    dialect: Dialect,
    manifest: Manifest,
    *,
    model: Model | None = None,
    scope: Scope = "full",
    sample_size: int | None = None,
    sample_bucket: int | None = None,
    partition_filter: PartitionFilter | None = None,
    as_of: date | None = None,
) -> str | _RequiresFutureData | _InvalidIdentifier | tuple[str, str]:
    """Render a candidate test as a failing-rows SELECT.

    The returned string is a SELECT whose rows are violations: zero rows
    means the test passes; one or more rows mean it fails. The adapter's
    :meth:`signalforge.warehouse.WarehouseAdapter.run_test_sql` wraps the
    returned string with ``SELECT COUNT(*) AS failures FROM (...) AS t``
    (plus an optional ``ARRAY_AGG`` for sample-failure capture).

    Dispatch is on :attr:`Dialect.quote_char` (DEC-025) so v0.2
    Snowflake / Postgres adapters drop in by adding a sibling
    :class:`Dialect` constant — no compiler changes required.

    A ``relationships`` test whose parent is not in the manifest returns
    :class:`_RequiresFutureData` rather than raising (DEC-006, DEC-026);
    a test whose ``column`` / ``field`` fails the SQL-identifier shape
    check returns :class:`_InvalidIdentifier`; the orchestrator
    distinguishes the three return shapes via :func:`isinstance`.

    A ``custom_sql`` (singular) test resolves its dbt-Jinja refs
    (``{{ this }}`` / ``{{ ref() }}`` / ``{{ source() }}``) via
    :func:`signalforge.manifest.template.resolve_template_refs` then runs a
    :func:`validate_test_sql` pre-flight on the resolved SQL; both
    Jinja-resolution failure and SQL-safety rejection return an
    :class:`_InvalidIdentifier` sentinel (DEC-006 / DEC-008). The
    ``model`` keyword carries the :class:`Model` under prune for ``{{ this }}``
    resolution and single-table substitution — the four built-in variants
    ignore it. Single-table custom tests are sample-wrapped like the
    built-ins; multi-table tests (a ``JOIN`` survives literal-stripping)
    run full-scan (DEC-006 / DEC-009).

    Sampling and partition-filter wiring (post-PR-#20 review fix):

    * ``scope="sample"`` — wraps the test in a deterministic-sample CTE
      (``WITH sample AS (SELECT * FROM <table> AS t WHERE
      MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), <bucket>) < 1
      [AND <partition>] LIMIT <size>) <test_sql>``). The orchestrator
      derives ``sample_bucket`` from ``num_rows / sample_size`` and
      passes both kwargs in.
    * ``scope="full"`` — emits the test SQL against the raw table; when
      ``partition_filter`` is supplied the predicate is composed via
      derived table (``FROM (SELECT * FROM <table> WHERE
      <partition>)``).
    * ``relationships`` in sample mode samples the CHILD table only;
      the parent stays at full so an orphan detected in the child
      sample is not a false positive of the parent's missing-from-sample
      row. ``partition_filter`` likewise applies to the child only.
    """
    if isinstance(test, CandidateTestNotNull):
        return _compile_not_null(
            test,
            table_ref,
            dialect,
            scope=scope,
            sample_size=sample_size,
            sample_bucket=sample_bucket,
            partition_filter=partition_filter,
        )
    if isinstance(test, CandidateTestUnique):
        return _compile_unique(
            test,
            table_ref,
            dialect,
            scope=scope,
            sample_size=sample_size,
            sample_bucket=sample_bucket,
            partition_filter=partition_filter,
        )
    if isinstance(test, CandidateTestAcceptedValues):
        return _compile_accepted_values(
            test,
            table_ref,
            dialect,
            scope=scope,
            sample_size=sample_size,
            sample_bucket=sample_bucket,
            partition_filter=partition_filter,
        )
    if isinstance(test, CandidateTestRelationships):
        return _compile_relationships(
            test,
            table_ref,
            dialect,
            manifest,
            scope=scope,
            sample_size=sample_size,
            sample_bucket=sample_bucket,
            partition_filter=partition_filter,
        )
    if isinstance(test, CandidateTestCustomSQL):
        return _compile_custom_sql(
            test,
            table_ref,
            dialect,
            manifest,
            model,
            scope=scope,
            sample_size=sample_size,
            sample_bucket=sample_bucket,
            partition_filter=partition_filter,
        )
    if isinstance(test, CandidateTestRowCountBetween):
        # row_count_between bypasses scope / sample_size / sample_bucket /
        # partition_filter by design (DEC-003): the compiled SQL is the
        # COUNT(*) check itself, not a failing-rows SELECT, and a sampled
        # COUNT(*) cannot be compared against full-table bounds. Under
        # materialised-sample the orchestrator passes ``table_ref=<temp
        # table>`` so the count still lands on a cheap sample without
        # double-sampling.
        return _compile_row_count_between(test, table_ref, dialect)
    if isinstance(test, CandidateTestUniqueCombination):
        # unique_combination consumes ``table_ref`` as-is — sample-mode
        # routing (engine-level source override under ``materialised`` /
        # ``oneshot``) is the engine's responsibility (#170 US-005b).
        # Composite uniqueness on a bucket-mod'd subset has false-negative
        # risk because a duplicate pair may straddle the sampled and
        # unsampled rows, so the engine always routes this variant to the
        # source table. The compiler-level pin in
        # ``test_compile_unique_combination_*`` snapshots the SQL shape;
        # the engine-level pin in
        # ``test_prune_tests_unique_combination_under_*`` is the load-bearing
        # routing guarantee.
        return _compile_unique_combination(test, table_ref, dialect)
    if isinstance(test, CandidateTestRowCountAnomalyByPeriod):
        # row_count_anomaly_by_period compiles into TWO SQL strings (stats +
        # violation) per DEC-008 (#171 US-008). The engine (US-011) handles
        # the two-query split: run the stats query first, gate on
        # ``stats.n_periods >= min_samples_per_bucket``, then run the
        # violation query. ``as_of`` defaults to ``date.today()`` at the
        # engine-orchestrator boundary (US-009) when the caller omits it;
        # for compiler-level callers (tests) ``as_of`` is required. Like
        # ``row_count_between`` / ``unique_combination``, this variant
        # bypasses sample-mode routing — a sampled history window would
        # under-count periods and produce false bounds.
        if as_of is None:
            return _InvalidIdentifier(
                reason=(
                    "row_count_anomaly_by_period requires as_of; the engine "
                    "must resolve as_of to date.today() (or operator-supplied) "
                    "before calling _compile_test"
                )
            )
        # Resolve the date column's warehouse type from the manifest model so
        # the anomaly compiler can TYPE-MATCH the partition-pruning bound
        # literal (issue #159 data_type). ``model`` is None in unit snapshots
        # (column type unknown → DATE-literal fallback).
        anomaly_date_column_type: str | None = None
        if model is not None:
            anomaly_date_col = model.columns.get(test.date_column)
            if anomaly_date_col is not None:
                anomaly_date_column_type = anomaly_date_col.data_type
        return _compile_row_count_anomaly_by_period(
            test,
            table_ref,
            dialect,
            as_of=as_of,
            date_column_type=anomaly_date_column_type,
        )
    # The discriminated union is closed over the eight variants above; an
    # unreachable arm here means a ninth variant was added without a
    # compiler branch.
    raise NotImplementedError(  # pragma: no cover
        f"no compiler branch for candidate test variant {type(test).__name__}"
    )


def _compute_compiled_sql_hash(sql: str) -> str:
    """Compute the 16-hex-char blake2b-8 hash of a compiled SQL string.

    Mirrors the hash convention in :mod:`signalforge.draft.audit`
    (DEC-005). The prune-audit writer (US-009) records this hash on
    every :class:`signalforge.prune.models.PruneDecision` so a reviewer
    can correlate decisions across the prune-audit JSONL and the
    response-audit JSONL by hash.
    """
    return blake2b(sql.encode("utf-8"), digest_size=8).hexdigest()
