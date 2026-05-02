"""SignalForge diff renderer — kept/dropped table + unified schema.yml diff.

This subpackage implements the README's "explainable diffs" architectural
commitment at the post-grade boundary: every kept and dropped artefact
ships with a one-line "why", and the operator gets a unified diff between
the existing committed ``schema.yml`` and the candidate output.

The full design is in ``plans/super/8-diff-renderer.md``. US-001 ships only
the typed exception hierarchy; the result models, config, renderers,
sidecar writer, and orchestrator land in subsequent stories.
"""

from __future__ import annotations

from signalforge.diff.errors import (
    DiffCandidateModelMismatchError,
    DiffError,
    DiffGradingReportModelMismatchError,
    DiffInputTooLargeError,
    DiffPruneResultModelMismatchError,
    DiffSidecarRecordTooLargeError,
    DiffSidecarWriteError,
)

__all__ = [
    "DiffCandidateModelMismatchError",
    "DiffError",
    "DiffGradingReportModelMismatchError",
    "DiffInputTooLargeError",
    "DiffPruneResultModelMismatchError",
    "DiffSidecarRecordTooLargeError",
    "DiffSidecarWriteError",
]
