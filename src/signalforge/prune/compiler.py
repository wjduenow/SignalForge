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
    CandidateTestUnique,
)
from signalforge.manifest.errors import (
    AmbiguousRefError,
    RefNotFoundError,
    SourceNotFoundError,
    TemplateResolutionError,
)
from signalforge.manifest.template import resolve_template_refs
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


def _render_partition_filter(pf: PartitionFilter, quote_char: str) -> str:
    """Render a :class:`PartitionFilter` to a SQL fragment for the WHERE clause.

    Mirrors :meth:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter._render_partition_filter`
    — the prune compiler reuses the same render rules so the partition
    predicate the engine threads through the deterministic sample CTE
    matches what the warehouse adapter would emit on its own
    ``sample_rows`` path.

    ``datetime`` → ``TIMESTAMP('…')``; ``date`` → ``DATE('…')``;
    ``str`` is escaped via :func:`escape_bq_string_literal` for safe
    inclusion inside a single-quoted BigQuery string literal. The column
    name is already DEC-013-validated by :class:`PartitionFilter`'s
    ``__post_init__``.
    """
    # ``datetime`` is a subclass of ``date``, so check it first.
    if isinstance(pf.value, datetime):
        rendered = f"TIMESTAMP('{pf.value.isoformat()}')"
    elif isinstance(pf.value, date):
        rendered = f"DATE('{pf.value.isoformat()}')"
    else:
        rendered = f"'{escape_bq_string_literal(str(pf.value))}'"
    return f"{quote_char}{pf.column}{quote_char} {pf.op} {rendered}"


def _render_sample_cte(
    table: str,
    *,
    sample_size: int,
    sample_bucket: int,
    partition_filter: PartitionFilter | None,
    quote_char: str,
) -> str:
    """Render a deterministic-sample CTE matching the adapter's
    :meth:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter.sample_rows`
    SQL shape.

    Every sample-mode failing-rows query is wrapped as::

        WITH sample AS (
            SELECT * FROM <table> AS t
            WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), <bucket>) < 1
              [AND <partition_filter>]
            LIMIT <sample_size>
        )
        <test SQL targeting sample>

    The hash-mod predicate is identical to the adapter's
    ``sample_rows`` deterministic-sample shape (DEC-006 of issue #3) so
    sampling decisions stay consistent between the adapter's internal
    samples and the prune engine's wrapped tests.
    """
    where_clauses = [
        f"MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), {sample_bucket}) < 1",
    ]
    if partition_filter is not None:
        where_clauses.append(_render_partition_filter(partition_filter, quote_char))
    where_sql = " AND ".join(where_clauses)
    return f"WITH sample AS (SELECT * FROM {table} AS t WHERE {where_sql} LIMIT {sample_size})"


def _quote(identifier: str, quote_char: str) -> str:
    """Wrap ``identifier`` in ``quote_char`` for SQL embedding.

    The compiler trusts adapter-validated identifiers on
    :class:`TableRef` — :func:`signalforge.warehouse._sql_safety.validate_identifier`
    runs at :class:`TableRef` construction time and rejects anything
    outside ``[A-Za-z_][A-Za-z0-9_]*``, so we do not re-validate here.
    Column names from :class:`signalforge.draft.models.CandidateTest`
    pass through the drafter's anchor-contract validator before reaching
    the compiler; the orchestrator's :class:`TableRef` construction is
    the gate, not this function.
    """
    return f"{quote_char}{identifier}{quote_char}"


def _qualified_table_name(table_ref: TableRef, quote_char: str) -> str:
    """Render a fully-qualified ``project.dataset.name`` table identifier.

    Matches the BigQuery convention ``\\`project.dataset.table\\``` (entire
    qualified path inside one pair of backticks). Dialects with a different
    quote_char get the same shape with their own quote character so v0.2
    ports drop in without compiler changes (DEC-025).
    """
    if table_ref.project is None:
        return f"{quote_char}{table_ref.dataset}.{table_ref.name}{quote_char}"
    return f"{quote_char}{table_ref.project}.{table_ref.dataset}.{table_ref.name}{quote_char}"


def _wrap_with_sample_or_partition(
    *,
    test_sql: str,
    table_sql: str,
    table_alias_sql: str,
    scope: Scope,
    sample_size: int | None,
    sample_bucket: int | None,
    partition_filter: PartitionFilter | None,
    quote_char: str,
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
            quote_char=quote_char,
        )
        return f"{cte} {test_sql}"
    # scope == "full"
    if partition_filter is not None:
        partition_sql = _render_partition_filter(partition_filter, quote_char)
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
    quote_char: str,
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
    col = _quote(test.column, quote_char)
    table = _qualified_table_name(table_ref, quote_char)
    target = "sample" if scope == "sample" else table
    test_sql = f"SELECT {col} FROM {target} WHERE {col} IS NULL"
    return _wrap_with_sample_or_partition(
        test_sql=test_sql,
        table_sql=table,
        table_alias_sql=target,
        scope=scope,
        sample_size=sample_size,
        sample_bucket=sample_bucket,
        partition_filter=partition_filter,
        quote_char=quote_char,
    )


def _compile_unique(
    test: CandidateTestUnique,
    table_ref: TableRef,
    quote_char: str,
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
    col = _quote(test.column, quote_char)
    table = _qualified_table_name(table_ref, quote_char)
    target = "sample" if scope == "sample" else table
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
        quote_char=quote_char,
    )


def _compile_accepted_values(
    test: CandidateTestAcceptedValues,
    table_ref: TableRef,
    quote_char: str,
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
    col = _quote(test.column, quote_char)
    table = _qualified_table_name(table_ref, quote_char)
    target = "sample" if scope == "sample" else table
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
        quote_char=quote_char,
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
    quote_char: str,
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

    child_col = _quote(test.column, quote_char)
    parent_col = _quote(test.field, quote_char)
    child_table = _qualified_table_name(table_ref, quote_char)
    parent_table = _qualified_table_name(parent_table_ref, quote_char)
    child_target = "sample" if scope == "sample" else child_table
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
        quote_char=quote_char,
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
    quote_char: str,
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
    own_table_quoted = _qualified_table_name(table_ref, quote_char)

    if _is_multi_table(resolved_sql):
        # Multi-table: full-scan. Apply a partition filter to the model's
        # own table when available; otherwise return the resolved SQL
        # unchanged. Sampling a join is semantically wrong (DEC-006).
        if partition_filter is not None:
            partition_sql = _render_partition_filter(partition_filter, quote_char)
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
        sampled_sql = resolved_sql.replace(own_qualified, "sample")
        cte = _render_sample_cte(
            own_table_quoted,
            sample_size=sample_size,
            sample_bucket=sample_bucket,
            partition_filter=partition_filter,
            quote_char=quote_char,
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
        partition_sql = _render_partition_filter(partition_filter, quote_char)
        replacement = f"(SELECT * FROM {own_qualified} WHERE {partition_sql})"
        if own_qualified in resolved_sql:
            return resolved_sql.replace(own_qualified, replacement)
    return resolved_sql


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
) -> str | _RequiresFutureData | _InvalidIdentifier:
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
    quote_char = dialect.quote_char
    if isinstance(test, CandidateTestNotNull):
        return _compile_not_null(
            test,
            table_ref,
            quote_char,
            scope=scope,
            sample_size=sample_size,
            sample_bucket=sample_bucket,
            partition_filter=partition_filter,
        )
    if isinstance(test, CandidateTestUnique):
        return _compile_unique(
            test,
            table_ref,
            quote_char,
            scope=scope,
            sample_size=sample_size,
            sample_bucket=sample_bucket,
            partition_filter=partition_filter,
        )
    if isinstance(test, CandidateTestAcceptedValues):
        return _compile_accepted_values(
            test,
            table_ref,
            quote_char,
            scope=scope,
            sample_size=sample_size,
            sample_bucket=sample_bucket,
            partition_filter=partition_filter,
        )
    if isinstance(test, CandidateTestRelationships):
        return _compile_relationships(
            test,
            table_ref,
            quote_char,
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
            quote_char,
            manifest,
            model,
            scope=scope,
            sample_size=sample_size,
            sample_bucket=sample_bucket,
            partition_filter=partition_filter,
        )
    # The discriminated union is closed over the five variants above; an
    # unreachable arm here means a sixth variant was added without a
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
