"""Canonical ``artifact_id`` dotted-path formatter (US-006 of issue #8).

Mirrors :func:`signalforge.grade.engine._artifact_id_for` byte-for-byte.
The diff renderer joins grade-sidecar JSON to its rendered diff via the
``artifact_id`` triple (``run_id``, ``artifact_id``, ``criterion_id``);
if the diff layer's formatter and the grade layer's formatter ever
disagreed on a single dotted-path shape, every grade row whose
artifact_id depended on the disagreement would silently drop out of the
join. The cross-stage parity is therefore load-bearing — see
:file:`tests/diff/test_artifact_id.py::test_cross_stage_parity_with_grade_engine`,
the single allowed cross-stage import seam.

Six dotted-path shapes the formatter emits (per grade-layer.md DEC-009):

* ``column.<col>.description`` / ``column.<col>.rationale`` — column
  doc / rationale.
* ``model.description`` / ``model.rationale`` — model-level doc /
  rationale.
* ``test.column.<col>.<test.type>`` (or ``...<.args_hash>`` when two
  tests on the same column share a ``test.type``) — column-scoped test.
* ``test.model.<test.type>`` (or ``...<.args_hash>`` when two model-
  level tests share a ``test.type``) — model-level test.

Collision rule (post-QG fix, mirrored verbatim from the grade engine):
two tests in the SAME scope (model-level OR same-column) sharing a
``test.type`` get an 8-hex ``blake2b-4`` ``args_hash`` suffix to
disambiguate. Exact duplicates (same type, same args → identical
blake2b-4 hash) get an additional ``:<n>`` ordinal so artifact_ids stay
globally unique even when a candidate carries semantically identical
tests.

Programming errors (e.g., calling with ``scope='column'`` and no
``column_name``) raise :class:`ValueError`. The diff layer has no
typed error hierarchy at the leaf-module level — the orchestrator in
US-001 owns wrapping if a caller chooses to handle the failure mode
beyond fail-loud.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from signalforge.draft.models import (
    CandidateSchema,
    CandidateTest,
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestUnique,
)


def _model_test_args_hash(test: CandidateTest) -> str:
    """Return an 8-hex ``blake2b-4`` digest of a test's identifying args.

    Mirrors :func:`signalforge.grade.engine._model_test_args_hash` byte-
    for-byte. The hash domain is the test's identifying args (type +
    column + variant-specific fields), serialised as canonical JSON
    (``sort_keys=True, separators=(",", ":")``) so equivalent tests
    produce identical hashes regardless of construction order. For
    :class:`CandidateTestAcceptedValues`, the ``values`` tuple is sorted
    before hashing — a re-ordering of the literal list does not rotate
    the hash because the test is semantically identical.
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
    else:  # pragma: no cover - exhaustive dispatch over the closed union
        raise ValueError(
            f"Unknown CandidateTest variant: {type(test).__name__}. "
            "A new CandidateTest discriminated-union variant landed without "
            "updating signalforge.diff._artifact_id._model_test_args_hash."
        )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=4).hexdigest()


def artifact_id_for(
    *,
    scope: Literal["column", "model"],
    column_name: str | None = None,
    test: CandidateTest | None = None,
    field: Literal["description", "rationale"] | None = None,
    args_hash: str | None = None,
) -> str:
    """Return the canonical dotted-path ``artifact_id`` (DEC-009).

    Byte-equal mirror of
    :func:`signalforge.grade.engine._artifact_id_for`. See module
    docstring for the six emitted shapes and the args_hash collision
    rule. The cross-stage parity is exercised by
    :func:`tests.diff.test_artifact_id.test_cross_stage_parity_with_grade_engine`.
    """
    # test-shaped artifact_ids — column scope.
    if test is not None and scope == "column":
        if column_name is None:
            raise ValueError(
                "artifact_id_for: column-scope test artifact_id requires column_name. "
                "Pass column_name=... alongside scope='column' and a CandidateTest. "
                "This is a programming error in the caller."
            )
        if args_hash is not None:
            return f"test.column.{column_name}.{test.type}.{args_hash}"
        return f"test.column.{column_name}.{test.type}"

    # test-shaped artifact_ids — model scope.
    if test is not None and scope == "model":
        if args_hash is not None:
            return f"test.model.{test.type}.{args_hash}"
        return f"test.model.{test.type}"

    # description / rationale — column scope.
    if scope == "column":
        if column_name is None or field is None:
            raise ValueError(
                "artifact_id_for: column-scope text artifact_id requires "
                "column_name + field. Pass column_name=... and "
                "field='description'/'rationale' alongside scope='column'. "
                "This is a programming error in the caller."
            )
        return f"column.{column_name}.{field}"

    # description / rationale — model scope.
    if scope == "model":
        if field is None:
            raise ValueError(
                "artifact_id_for: model-scope text artifact_id requires field. "
                "Pass field='description'/'rationale' alongside scope='model'. "
                "This is a programming error in the caller."
            )
        return f"model.{field}"

    raise ValueError(
        f"artifact_id_for: unrecognised scope {scope!r}. scope must be 'column' or 'model'."
    )


def compute_args_hashes(candidate: CandidateSchema) -> dict[int, str | None]:
    """Pre-compute per-test args_hash values keyed by ``id(test)``.

    Byte-equal mirror of
    :func:`signalforge.grade.engine._test_args_hashes`. Returns a dict
    mapping each test's :func:`id` to either:

    * ``None`` — the test's ``test.type`` is unique within its scope
      (model-level OR same-column), so the bare 4-part artifact_id
      shape (``test.<scope>.<...>.<type>``) is collision-free.
    * an 8-hex ``args_hash`` string — the test's ``test.type`` collides
      with another test in the same scope. Same hash with a
      ``:<n>`` ordinal suffix when two tests have identical args (the
      first occurrence keeps the bare hash; second+ get
      ``<hash>:1``, ``<hash>:2``, ...).

    Collision rules (DEC-009 + post-QG fixes):

    * Two model-level tests with the same ``test.type`` collide
      regardless of args.
    * Two tests on the SAME column with the same ``test.type`` collide.
    * A model-level test does NOT collide with a column-scope test
      because the artifact_id prefix differs (``test.model.`` vs
      ``test.column.``).
    """
    out: dict[int, str | None] = {}

    def _assign(tests: tuple[CandidateTest, ...]) -> None:
        type_counts: dict[str, int] = {}
        for test in tests:
            type_counts[test.type] = type_counts.get(test.type, 0) + 1
        seen: dict[tuple[str, str], int] = {}
        for test in tests:
            if type_counts[test.type] <= 1:
                out[id(test)] = None
                continue
            base_hash = _model_test_args_hash(test)
            key = (test.type, base_hash)
            seen[key] = seen.get(key, 0) + 1
            ordinal = seen[key]
            out[id(test)] = base_hash if ordinal == 1 else f"{base_hash}:{ordinal - 1}"

    _assign(candidate.tests)
    for column in candidate.columns:
        _assign(column.tests)

    return out
