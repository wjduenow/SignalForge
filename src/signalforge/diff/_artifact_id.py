"""Diff-layer re-export of the shared ``artifact_id`` formatter (issue #42).

The implementation lives in :mod:`signalforge._common.artifact_id`. Before
#42 this module shipped a byte-equal copy of the grade-layer formatter;
the duplication is now hoisted to a single source of truth. The diff
renderer joins grade-sidecar JSON to its rendered diff via the
``artifact_id`` triple (``run_id``, ``artifact_id``, ``criterion_id``);
identity-equal function objects across the two consuming layers make a
silent drift impossible by construction. The load-bearing parity test
is :func:`tests.diff.test_artifact_id.test_cross_stage_parity_is_function_identity`
(``is`` equality across all three modules); the legacy byte-equal tests
(``test_cross_stage_parity_with_grade_engine`` and siblings) remain as
defence-in-depth. Those tests are the single allowed cross-stage import
seam between ``signalforge.diff`` and ``signalforge.grade``.

Programming errors raise :class:`ValueError` (re-exported neutral
behaviour from the shared seam). The diff orchestrator in
:func:`signalforge.diff.render_diff` is the only practical caller; valid
inputs cannot trip the failure modes.
"""

from __future__ import annotations

from signalforge._common.artifact_id import (
    artifact_id_for,
    compute_args_hashes,
)
from signalforge._common.artifact_id import (
    model_test_args_hash as _model_test_args_hash,
)

__all__ = ("_model_test_args_hash", "artifact_id_for", "compute_args_hashes")
