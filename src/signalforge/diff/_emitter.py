"""Canonical YAML emitter for kept candidate artifacts (US-005).

Renders a :class:`signalforge.draft.CandidateSchema` filtered against a
:class:`signalforge.prune.PruneResult` into a deterministic dbt-style
``schema.yml`` document. Only tests whose decision is ``"kept"`` survive
the filter; column declaration order is preserved from the candidate;
tests inside each column are sorted by ``(test_type, args_hash)`` so the
emitted bytes are stable across runs with the same input.

Singular business-rule tests (DEC-002 of #116; DEC-007 of #171) are NOT
schema.yml blocks — dbt models them as standalone ``.sql`` files under
``tests/``. Two variants flow through this contract:

* ``custom_sql`` (#116) — operator-or-LLM-authored singular SQL.
* ``row_count_anomaly_by_period`` (#171, US-014) — the violation query
  compiled by
  :func:`signalforge.prune.compiler._compile_anomaly_violation_query`.

The YAML emitter (:func:`emit_proposed_yaml`) therefore **skips** both:
:func:`_render_test` returns the ``_SKIP`` sentinel and the column /
model renderers drop it. The companion :func:`emit_proposed_test_files`
surfaces every KEPT singular test as a
:class:`signalforge.diff.models.ProposedTestFile` carrying a safe
relative path (via
:func:`signalforge.diff._test_file_writer.anchor_to_filename`) and the
SQL body with the ``-- signalforge:generated <hash>`` header marker.

This is a leaf module — it depends only on the production
:mod:`signalforge.draft` and :mod:`signalforge.prune` model types
plus the shared args-hash seam and the in-layer filename builder.
``yaml.safe_dump`` is invoked with ``sort_keys=False`` so the
key ordering enforced here (top-level: ``version``, ``models``;
per-model: ``name``, ``description``, ``columns``, optional ``tests``;
per-column: ``name``, ``description``, ``tests``) is what hits the
output. ``allow_unicode=True`` preserves non-ASCII text in
descriptions; ``default_flow_style=False`` produces block style;
``width=4096`` avoids accidental line-wraps inside long descriptions.

Round-trip safety for edge-case descriptions is exercised in
``tests/diff/test_emitter.py``: literal ``---``, leading ``!tag``,
embedded triple-backticks, and embedded newlines all round-trip
through ``yaml.safe_load`` to identical strings (AR-9 acceptance test).
``yaml.safe_dump`` quotes those values with whichever style preserves
them; the load-back is the load-bearing assertion.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final

import yaml

from signalforge._common.artifact_id import model_test_args_hash as _shared_args_hash
from signalforge.diff._test_file_writer import _with_marker, anchor_to_filename
from signalforge.diff.models import ProposedTestFile
from signalforge.draft import CandidateColumn, CandidateSchema, CandidateTest
from signalforge.draft.models import (
    CandidateTestAcceptedValues,
    CandidateTestCustomSQL,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestRowCountAnomalyByPeriod,
    CandidateTestRowCountBetween,
    CandidateTestUnique,
    CandidateTestUniqueCombination,
)
from signalforge.manifest.models import Model
from signalforge.prune import PruneResult
from signalforge.prune.compiler import _compile_anomaly_violation_query
from signalforge.warehouse.models import BIGQUERY_DIALECT, Dialect, TableRef

# Sentinel returned by :func:`_render_test` for a ``custom_sql`` test —
# singular business-rule tests are NOT schema.yml blocks (DEC-002 of
# #116); they ship as standalone ``.sql`` files via
# :func:`emit_proposed_test_files`. The column / model renderers drop
# any test that renders to this sentinel.
_SKIP: Final[object] = object()

# ---------------------------------------------------------------------------
# args_hash — delegates to the shared seam (signalforge._common.artifact_id)
# ---------------------------------------------------------------------------


def _test_args_hash(test: CandidateTest) -> str:
    """Return the canonical 8-hex args fingerprint of a candidate test.

    Delegates to :func:`signalforge._common.artifact_id.model_test_args_hash`
    (the shared seam, issue #42) so the emitter, the grader, and the diff
    renderer agree on test identity across every ``CandidateTest`` variant
    — including the fifth ``custom_sql`` variant (issue #116), which the
    previous in-module copy did not handle and would have raised on. The
    shared seam is the single source of truth for the hash domain (sorted
    ``accepted_values``, raw SQL text for ``custom_sql``, etc.).
    """
    return _shared_args_hash(test)


# ---------------------------------------------------------------------------
# Kept-test fingerprint set
# ---------------------------------------------------------------------------


def _fingerprint(scope: str, column: str | None, test: CandidateTest) -> tuple[str, str, str, str]:
    """Return the (scope, column, type, args_hash) tuple identifying a test.

    ``scope`` is ``"column"`` or ``"model"``. ``column`` is the empty
    string for model-level tests; the per-test ``column`` field on a
    column-scoped test (which always equals the parent column on a
    well-formed candidate, enforced by the drafter's anchor contract).
    """
    return (scope, column or "", test.type, _test_args_hash(test))


def _kept_fingerprints(prune_result: PruneResult) -> set[tuple[str, str, str, str]]:
    """Materialise the (scope, column, type, args_hash) set of kept tests.

    A :class:`signalforge.prune.PruneDecision` carries its original
    :class:`signalforge.draft.CandidateTest` plus a ``test_anchor``
    string (``"column.<col>"`` or ``"model"``). We derive the scope and
    column from ``test_anchor`` rather than re-deriving from the
    discriminated union, so a future model-level test variant on a
    column-named field would still route correctly.
    """
    out: set[tuple[str, str, str, str]] = set()
    for decision in prune_result.kept_decisions:
        anchor = decision.test_anchor
        if anchor.startswith("column."):
            column = anchor[len("column.") :]
            out.add(_fingerprint("column", column, decision.test))
        elif anchor == "model":
            out.add(_fingerprint("model", None, decision.test))
        # Unknown anchor shapes simply don't match any candidate test;
        # the diff renderer's contract is "ship only kept", and a
        # mismatched anchor means the decision can't be paired with a
        # candidate, so it's elided.
    return out


# ---------------------------------------------------------------------------
# Test → dict rendering
# ---------------------------------------------------------------------------


def _render_test(test: CandidateTest) -> Any:
    """Render a :class:`CandidateTest` into the dbt schema.yml fragment.

    ``not_null`` / ``unique`` render as the bare type-name string. The
    parameterised tests render as a single-key dict mapping the type
    name to its args. ``rationale`` is intentionally NOT emitted — it
    is consumed by the grader and the diff "why" line, not by dbt.

    A ``custom_sql`` test (issue #116) returns the :data:`_SKIP` sentinel
    — singular business-rule tests are NOT schema.yml blocks; they ship
    as standalone ``.sql`` files via :func:`emit_proposed_test_files`.
    The column / model renderers drop any test that renders to
    :data:`_SKIP`, so ``custom_sql`` never lands in the proposed YAML and
    this function never crashes on the fifth variant.

    A ``row_count_anomaly_by_period`` test (issue #171, US-014) also
    returns :data:`_SKIP` — there is no dbt-macro YAML form for the
    statistical anomaly band (locked in Phase 1 B.7 of the plan), so it
    ships as a singular ``tests/*.sql`` file carrying the violation query
    compiled by
    :func:`signalforge.prune.compiler._compile_anomaly_violation_query`.
    The companion :func:`emit_proposed_test_files` surfaces the file.

    A ``row_count_between`` test (issue #169, DEC-002) renders as a
    ``dbt_expectations.expect_table_row_count_to_be_between`` block. The
    Python-side fields ``minimum`` / ``maximum`` are mapped OUTBOUND here
    to the dbt-expectations macro names ``min_value`` / ``max_value``
    (DEC-008). The optional ``where`` field appears verbatim under the
    ``where`` key only when set; ``None``-valued fields are omitted so
    the emitted YAML stays minimal (DEC-002). THIS FUNCTION is the
    outbound mapping seam — the ingest parser owns the inverse inbound
    mapping (``min_value``/``max_value`` → ``minimum``/``maximum``).

    A ``unique_combination`` test (issue #170, DEC-002) renders as a
    ``dbt_utils.unique_combination_of_columns`` block. The Python-side
    field ``columns`` maps OUTBOUND to the dbt-utils macro key
    ``combination_of_columns``; the prefix-free internal name mirrors
    the ``values`` / ``to`` / ``field`` precedent on the other variants
    while keeping the macro naming confined to the emitter (the ingest
    parser owns the inverse inbound mapping). Emission preserves the
    order Pydantic carries — sorting is for the canonical-hash domain
    only (DEC-011), NOT for YAML output, so the operator's review
    surface reflects the LLM's declared column order. The optional
    ``where`` field appears verbatim under the ``where`` key only when
    set; ``None`` is omitted.
    """
    if isinstance(test, (CandidateTestNotNull, CandidateTestUnique)):
        return test.type
    if isinstance(test, CandidateTestAcceptedValues):
        return {test.type: {"values": list(test.values)}}
    if isinstance(test, CandidateTestRelationships):
        return {test.type: {"to": test.to, "field": test.field}}
    if isinstance(test, CandidateTestCustomSQL):
        return _SKIP
    if isinstance(test, CandidateTestRowCountAnomalyByPeriod):
        # #171 US-014 — ``row_count_anomaly_by_period`` ships as a singular
        # ``tests/*.sql`` file (the violation query from
        # :func:`signalforge.prune.compiler._compile_anomaly_violation_query`),
        # NOT a YAML block. Mirrors the ``custom_sql`` arm above. The
        # companion :func:`emit_proposed_test_files` surfaces the SQL file;
        # the YAML emitter drops every test that renders to :data:`_SKIP`.
        return _SKIP
    if isinstance(test, CandidateTestRowCountBetween):
        body: dict[str, Any] = {}
        if test.minimum is not None:
            body["min_value"] = test.minimum
        if test.maximum is not None:
            body["max_value"] = test.maximum
        if test.where is not None:
            body["where"] = test.where
        return {"dbt_expectations.expect_table_row_count_to_be_between": body}
    if isinstance(test, CandidateTestUniqueCombination):
        uc_body: dict[str, Any] = {"combination_of_columns": list(test.columns)}
        if test.where is not None:
            uc_body["where"] = test.where
        return {"dbt_utils.unique_combination_of_columns": uc_body}
    raise ValueError(  # pragma: no cover — exhaustive over the closed union
        f"Unknown CandidateTest variant: {type(test).__name__}"
    )


def _sort_tests(tests: tuple[CandidateTest, ...]) -> list[CandidateTest]:
    """Return ``tests`` sorted by ``(type, args_hash)`` for determinism.

    Within a column (or at model-level), tests of distinct types sort
    alphabetically by type. Two tests of the same type — only possible
    for ``accepted_values`` and ``relationships`` per the drafter's
    anchor contract — sort by their args fingerprint, so the emitted
    YAML bytes are stable across runs with the same kept set.
    """
    return sorted(tests, key=lambda t: (t.type, _test_args_hash(t)))


def _render_column(
    column: CandidateColumn,
    kept: set[tuple[str, str, str, str]],
) -> dict[str, Any]:
    """Render one :class:`CandidateColumn` into its dbt schema.yml dict.

    Filters ``column.tests`` to those whose fingerprint is in ``kept``,
    then sorts the survivors by ``(type, args_hash)``. Emits keys in the
    fixed order ``name``, ``description``, ``tests`` — ``yaml.safe_dump``
    is invoked with ``sort_keys=False`` so this insertion order is
    preserved. ``tests`` is omitted when no tests survived the filter,
    which keeps the diff surface minimal for columns with no signal.
    """
    out: dict[str, Any] = {
        "name": column.name,
        "description": column.description,
    }
    surviving = [t for t in column.tests if _fingerprint("column", column.name, t) in kept]
    if surviving:
        # ``custom_sql`` tests render to the ``_SKIP`` sentinel — they
        # ship as standalone ``.sql`` files, not schema.yml blocks, so
        # they're dropped from the YAML here (issue #116).
        rendered = [_render_test(t) for t in _sort_tests(tuple(surviving))]
        rendered = [r for r in rendered if r is not _SKIP]
        if rendered:
            out["tests"] = rendered
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def emit_proposed_yaml(candidate: CandidateSchema, prune_result: PruneResult) -> str:
    """Render the kept slice of ``candidate`` as a dbt schema.yml string.

    Filters tests to those whose ``(scope, column, type, args_hash)``
    fingerprint matches a :class:`signalforge.prune.PruneDecision` with
    ``decision == "kept"``. Column declaration order is preserved from
    the candidate (NOT alphabetised — the LLM's ordering carries
    semantic intent). Tests within each column are sorted by
    ``(type, args_hash)`` so the bytes are deterministic.

    The emitted document is a YAML 1.1 stream (no leading ``---``
    marker; ``yaml.safe_dump`` with ``explicit_start=False``) suitable
    for direct write to ``models/<schema>.yml``.

    Round-trip safety: edge-case descriptions (``---``, ``!tag``,
    triple-backticks, embedded newlines) round-trip through
    :func:`yaml.safe_load` to identical strings — verified by AR-9 in
    ``tests/diff/test_emitter.py``.
    """
    kept = _kept_fingerprints(prune_result)

    columns_doc = [_render_column(col, kept) for col in candidate.columns]

    model_doc: dict[str, Any] = {
        "name": candidate.name,
        "description": candidate.description,
        "columns": columns_doc,
    }

    surviving_model_tests = [t for t in candidate.tests if _fingerprint("model", None, t) in kept]
    if surviving_model_tests:
        # Drop ``custom_sql`` (``_SKIP`` sentinel) — model-level
        # business-rule tests ship as standalone ``.sql`` files (#116).
        rendered_model_tests = [_render_test(t) for t in _sort_tests(tuple(surviving_model_tests))]
        rendered_model_tests = [r for r in rendered_model_tests if r is not _SKIP]
        if rendered_model_tests:
            model_doc["tests"] = rendered_model_tests

    document: dict[str, Any] = {
        "version": 2,
        "models": [model_doc],
    }

    return yaml.safe_dump(
        document,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
        allow_unicode=True,
    )


def emit_proposed_test_files(
    candidate: CandidateSchema,
    prune_result: PruneResult,
    *,
    model: Model | None = None,
    dialect: Dialect = BIGQUERY_DIALECT,
    as_of: date | None = None,
) -> tuple[ProposedTestFile, ...]:
    """Render the KEPT singular-SQL tests as standalone ``.sql`` proposals.

    Singular business-rule tests (DEC-002 of #116; DEC-007 of #171) are NOT
    schema.yml blocks — dbt models them as standalone ``.sql`` files under
    ``tests/``. Two variants flow through this function as of #171 US-014:

    * ``custom_sql`` (#116) — :attr:`~signalforge.draft.models.CandidateTestCustomSQL.sql`
      is operator-or-LLM-authored and ships verbatim (typically with
      ``{{ this }}`` / ``{{ ref() }}`` Jinja).
    * ``row_count_anomaly_by_period`` (#171, US-014) — the SQL is the
      violation query compiled by
      :func:`signalforge.prune.compiler._compile_anomaly_violation_query`
      against ``TableRef.from_model(model)`` and ``dialect``, bound to the
      ``as_of`` evaluation date (defaults to :func:`date.today`). The
      operator's dbt project runs this on every build; the stats query is
      computed at prune time only (the two-query split — see ``plans/super/
      171-row-count-anomaly.md`` § US-014 + Phase 1 B.7).

    Every proposed file carries:

    * :attr:`~signalforge.diff.models.ProposedTestFile.path` —
      ``tests/<model>__<descriptor>_<hash>.sql`` built via
      :func:`signalforge.diff._test_file_writer.anchor_to_filename`. The
      ``descriptor`` is ``<column>_<test_type>`` for a column-scoped test
      and ``<test_type>`` for a model-level one; the ``<hash>`` is the
      shared 8-hex args-hash (reuses
      :func:`signalforge._common.artifact_id.model_test_args_hash`, NOT a
      re-derivation) so two tests in the same scope with different args
      never collide on a filename.
    * :attr:`~signalforge.diff.models.ProposedTestFile.sql` — the SQL body
      with the ``-- signalforge:generated <hash>`` header marker prepended
      via :func:`signalforge.diff._test_file_writer._with_marker`, so the
      sidecar carries exactly the bytes a later
      :func:`signalforge.diff._test_file_writer.write_test_file` call
      would persist.

    Only KEPT decisions produce a proposal (mirrors the YAML emitter's
    "ship only kept" contract). The result is ordered by
    ``prune_result.kept_decisions`` order, then deduped by ``path`` so two
    decisions that resolve to the same filename collapse to one proposal
    (defensive — distinct args produce distinct hashes, so a collision
    means duplicate decisions).

    Args:
        candidate: the kept-test source from the LLM drafter.
        prune_result: the prune layer's decisions (only ``kept_decisions``
            are consumed).
        model: optional manifest :class:`Model` — required only to emit
            ``row_count_anomaly_by_period`` SQL (the violation query
            references ``TableRef.from_model(model)``). When ``None`` and
            an anomaly test is kept, the engine raises ``ValueError`` at
            emit time so the caller cannot silently ship a no-op file.
            ``custom_sql``-only call sites can omit it.
        dialect: optional :class:`Dialect` — drives quoting + date/interval
            templates for the anomaly violation query. Defaults to
            :data:`BIGQUERY_DIALECT` for v0.x (BigQuery is the v0.1
            warehouse). A v0.3 multi-warehouse callsite must source this
            from the adapter's :meth:`signalforge.warehouse.WarehouseAdapter.dialect`.
        as_of: optional evaluation date for the anomaly violation query.
            ``None`` defaults to :func:`date.today` — the operator can
            re-run ``signalforge generate --as-of <date>`` to bake in a
            different date. Threading the date through (rather than a
            templated ``{{ var('signalforge_as_of') }}`` placeholder) keeps
            the singular test file self-contained and matches the engine's
            ``as_of`` resolution shape (US-009).

    Raises:
        ValueError: a kept ``row_count_anomaly_by_period`` decision was
            found but ``model`` was ``None`` — the violation query cannot
            be compiled without :class:`TableRef.from_model`. Caller must
            pass ``model`` when anomaly tests can land on the candidate.
    """
    out: list[ProposedTestFile] = []
    seen_paths: set[str] = set()
    resolved_as_of: date | None = None
    for decision in prune_result.kept_decisions:
        test = decision.test
        descriptor: str
        sql_body: str
        if isinstance(test, CandidateTestCustomSQL):
            anchor = decision.test_anchor
            if anchor.startswith("column."):
                column = anchor[len("column.") :]
                descriptor = f"{column}_custom_sql"
            else:
                # Model-level (the literal "model" anchor, plus any
                # forward-compatible sentinel) — no column in the descriptor.
                descriptor = "custom_sql"
            sql_body = test.sql
        elif isinstance(test, CandidateTestRowCountAnomalyByPeriod):
            # #171 US-014. ``row_count_anomaly_by_period`` is model-level-only
            # (``column = None`` by the Pydantic schema), so the descriptor
            # is always the bare test type. The SQL is the violation query
            # compiled fresh — the prune engine's per-decision
            # ``compiled_sql`` currently routes the (stats, violation) tuple
            # to ``kept-without-evidence`` per US-008's temporary engine
            # routing and stores ``compiled_sql=""`` (US-011 will land the
            # two-query handling and populate the field). For now we
            # recompile to get the bytes the dbt singular test will run.
            if model is None:
                raise ValueError(
                    "emit_proposed_test_files cannot emit a "
                    "row_count_anomaly_by_period test without `model` — the "
                    "violation query references TableRef.from_model(model). "
                    "Pass `model=...` when anomaly tests can land on the "
                    "candidate."
                )
            descriptor = "row_count_anomaly_by_period"
            if resolved_as_of is None:
                # Resolve once per call. Prefer the decision's `as_of` (set
                # by US-009 of the engine) when available so the emitted
                # file matches the date the engine evaluated against;
                # otherwise resolve to today's date so the generated test
                # runs against the current period when the operator next
                # invokes dbt. The kwarg overrides both.
                resolved_as_of = (
                    as_of
                    if as_of is not None
                    else (decision.as_of if decision.as_of is not None else date.today())
                )
            table_ref = TableRef.from_model(model)
            sql_body = _compile_anomaly_violation_query(
                test, table_ref, dialect, as_of=resolved_as_of
            )
        else:
            continue
        args_hash = _shared_args_hash(test)
        path = anchor_to_filename(
            model_name=candidate.name,
            descriptor=descriptor,
            args_hash=args_hash,
        )
        if path in seen_paths:
            continue
        seen_paths.add(path)
        out.append(
            ProposedTestFile(
                path=path,
                sql=_with_marker(sql_body, args_hash=args_hash),
            )
        )
    return tuple(out)


__all__ = ("emit_proposed_test_files", "emit_proposed_yaml")
