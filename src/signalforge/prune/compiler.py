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

from dataclasses import dataclass
from hashlib import blake2b
from typing import TYPE_CHECKING

from signalforge.draft.models import (
    CandidateTest,
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestUnique,
)
from signalforge.warehouse._sql_safety import escape_bq_string_literal, validate_identifier
from signalforge.warehouse.errors import InvalidIdentifierError
from signalforge.warehouse.models import TableRef

if TYPE_CHECKING:
    from signalforge.manifest.models import Manifest
    from signalforge.warehouse.models import Dialect


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


def _compile_not_null(
    test: CandidateTestNotNull,
    table_ref: TableRef,
    quote_char: str,
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
    return f"SELECT {col} FROM {table} WHERE {col} IS NULL"


def _compile_unique(
    test: CandidateTestUnique,
    table_ref: TableRef,
    quote_char: str,
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
    return f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL GROUP BY {col} HAVING COUNT(*) > 1"


def _compile_accepted_values(
    test: CandidateTestAcceptedValues,
    table_ref: TableRef,
    quote_char: str,
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
    rendered_values = ", ".join(f"'{escape_bq_string_literal(v)}'" for v in test.values)
    return f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL AND {col} NOT IN ({rendered_values})"


def _resolve_parent_table_ref(
    parent_name: str,
    manifest: Manifest,
) -> TableRef | _RequiresFutureData:
    """Resolve a ``relationships(to=parent_name)`` to its parent TableRef.

    The drafter's :class:`CandidateTestRelationships.to` field carries
    only the parent model's :attr:`Model.name` (not a full ``unique_id``);
    :class:`Manifest` indexes by ``unique_id``, so the lookup scans
    :attr:`Manifest.nodes` for a model whose :attr:`Model.name` matches.

    Returns a :class:`_RequiresFutureData` sentinel when no match is
    found (DEC-026). When a match is found, returns the parent's
    :class:`TableRef` via :meth:`TableRef.from_model`; that call may
    raise :class:`ManifestProjectNotFoundError` or
    :class:`ManifestSchemaNotFoundError` if the parent model lacks
    ``database`` / ``schema`` — those are manifest-shape problems and
    propagate, not prune problems to swallow.
    """
    for parent_model in manifest.nodes.values():
        if parent_model.name == parent_name:
            return TableRef.from_model(parent_model)
    return _RequiresFutureData(reason=f"relationships parent {parent_name!r} not in manifest")


def _compile_relationships(
    test: CandidateTestRelationships,
    table_ref: TableRef,
    quote_char: str,
    manifest: Manifest,
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
    return (
        f"SELECT child.{child_col} "
        f"FROM {child_table} AS child "
        f"LEFT JOIN {parent_table} AS parent "
        f"ON child.{child_col} = parent.{parent_col} "
        f"WHERE child.{child_col} IS NOT NULL AND parent.{parent_col} IS NULL"
    )


def _compile_test(
    test: CandidateTest,
    table_ref: TableRef,
    dialect: Dialect,
    manifest: Manifest,
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
    """
    quote_char = dialect.quote_char
    if isinstance(test, CandidateTestNotNull):
        return _compile_not_null(test, table_ref, quote_char)
    if isinstance(test, CandidateTestUnique):
        return _compile_unique(test, table_ref, quote_char)
    if isinstance(test, CandidateTestAcceptedValues):
        return _compile_accepted_values(test, table_ref, quote_char)
    # Only CandidateTestRelationships remains in the discriminated union.
    return _compile_relationships(test, table_ref, quote_char, manifest)


def _compute_compiled_sql_hash(sql: str) -> str:
    """Compute the 16-hex-char blake2b-8 hash of a compiled SQL string.

    Mirrors the hash convention in :mod:`signalforge.draft.audit`
    (DEC-005). The prune-audit writer (US-009) records this hash on
    every :class:`signalforge.prune.models.PruneDecision` so a reviewer
    can correlate decisions across the prune-audit JSONL and the
    response-audit JSONL by hash.
    """
    return blake2b(sql.encode("utf-8"), digest_size=8).hexdigest()
