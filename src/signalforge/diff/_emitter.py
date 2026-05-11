"""Canonical YAML emitter for kept candidate artifacts (US-005).

Renders a :class:`signalforge.draft.CandidateSchema` filtered against a
:class:`signalforge.prune.PruneResult` into a deterministic dbt-style
``schema.yml`` document. Only tests whose decision is ``"kept"`` survive
the filter; column declaration order is preserved from the candidate;
tests inside each column are sorted by ``(test_type, args_hash)`` so the
emitted bytes are stable across runs with the same input.

This is a leaf module — it depends only on the production
:mod:`signalforge.draft` and :mod:`signalforge.prune` model types.
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

import hashlib
import json
from typing import Any

import yaml

from signalforge.draft import CandidateColumn, CandidateSchema, CandidateTest
from signalforge.draft.models import (
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestUnique,
)
from signalforge.prune import PruneResult

# ---------------------------------------------------------------------------
# args_hash — mirrors signalforge.grade.engine._model_test_args_hash
# ---------------------------------------------------------------------------


def _test_args_hash(test: CandidateTest) -> str:
    """Return the canonical 8-hex args fingerprint of a candidate test.

    Mirrors :func:`signalforge.grade.engine._model_test_args_hash` (DEC-009
    of the grade layer) so the emitter, the grader, and the diff renderer
    agree on test identity. The hash domain is the test's identifying
    args, sorted-key JSON-serialised so equivalent tests produce
    identical hashes regardless of field-construction order. For
    ``accepted_values``, the ``values`` tuple is sorted before hashing —
    a re-ordering of the literal list does not rotate the hash.
    """
    if isinstance(test, (CandidateTestNotNull, CandidateTestUnique)):
        payload: dict[str, object] = {"type": test.type, "column": test.column}
    elif isinstance(test, CandidateTestAcceptedValues):
        payload = {
            "type": test.type,
            "column": test.column,
            "values": sorted(test.values),
        }
    elif isinstance(test, CandidateTestRelationships):
        payload = {
            "type": test.type,
            "column": test.column,
            "to": test.to,
            "field": test.field,
        }
    else:  # pragma: no cover — exhaustive over the closed union
        raise ValueError(f"Unknown CandidateTest variant: {type(test).__name__}")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=4).hexdigest()


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
    """
    if isinstance(test, (CandidateTestNotNull, CandidateTestUnique)):
        return test.type
    if isinstance(test, CandidateTestAcceptedValues):
        return {test.type: {"values": list(test.values)}}
    if isinstance(test, CandidateTestRelationships):
        return {test.type: {"to": test.to, "field": test.field}}
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
        out["tests"] = [_render_test(t) for t in _sort_tests(tuple(surviving))]
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
        model_doc["tests"] = [_render_test(t) for t in _sort_tests(tuple(surviving_model_tests))]

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


__all__ = ("emit_proposed_yaml",)
