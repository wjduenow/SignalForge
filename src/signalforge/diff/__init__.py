"""SignalForge diff renderer — kept/dropped table + unified ``schema.yml`` diff.

This subpackage implements the README's "explainable diffs" architectural
commitment at the post-grade boundary: every kept and dropped artefact
ships with a one-line "why", and the operator gets a unified diff between
the existing committed ``schema.yml`` and the candidate output.

Public API surface (DEC-004 of ``plans/super/8-diff-renderer.md``):

* :func:`render_diff` — end-to-end orchestrator. Takes the upstream
  ``Model`` / ``CandidateSchema`` / ``PruneResult`` (and an optional
  ``GradingReport``), emits a :class:`DiffReport`, drives the selected
  renderer, and (optionally) writes the JSON sidecar.
* :func:`load_diff_config` — loads the ``diff:`` block from
  ``signalforge.yml`` and returns a typed :class:`DiffConfig`. Mirrors
  :func:`signalforge.grade.load_grade_config`,
  :func:`signalforge.prune.load_prune_config`, and
  :func:`signalforge.draft.load_draft_config`.
* :class:`DiffReport`, :class:`DiffEntry` — typed read-back result
  models (``frozen=True, extra="ignore"``).
* :class:`DiffConfig` — user-facing knob block (``frozen=True,
  extra="forbid"``; typos like ``contxt_lines:`` fail loud at load time).
* :data:`Tier` — the ``Literal["kept", "dropped", "flagged"]``
  enumeration of per-entry tiers.
* The seven-class :class:`DiffError` hierarchy: :class:`DiffError`,
  :class:`DiffCandidateModelMismatchError`,
  :class:`DiffPruneResultModelMismatchError`,
  :class:`DiffGradingReportModelMismatchError`,
  :class:`DiffInputTooLargeError`,
  :class:`DiffSidecarRecordTooLargeError`,
  :class:`DiffSidecarWriteError`.

Concrete renderers (``AnsiRenderer``, ``MarkdownRenderer``,
``JsonRenderer``) and internal helpers (``_emitter``, ``_sidecar``,
``_artifact_id``, ``_ansi_safety``, ``_markdown_safety``) are private
per DEC-004 — only the typed-result + orchestrator + config + errors
form the public contract. Concrete renderers stay reachable via dotted
import (``from signalforge.diff._renderers import AnsiRenderer``) for
internal use, but are absent from the package namespace.

See ``docs/diff-ops.md`` for the operational reference and
``plans/super/8-diff-renderer.md`` for the full design.
"""

from __future__ import annotations

from signalforge.diff.config import DiffConfig, load_diff_config
from signalforge.diff.engine import render_diff, render_to_text
from signalforge.diff.errors import (
    DiffCandidateModelMismatchError,
    DiffError,
    DiffGradingReportModelMismatchError,
    DiffInputTooLargeError,
    DiffPruneResultModelMismatchError,
    DiffSidecarRecordTooLargeError,
    DiffSidecarWriteError,
    DiffTestFileRecordTooLargeError,
    DiffTestFileWriteError,
)
from signalforge.diff.models import DiffEntry, DiffReport, Tier

__all__ = [
    # Orchestrator
    "render_diff",
    "render_to_text",
    # Config
    "DiffConfig",
    "load_diff_config",
    # Result models + literal
    "DiffEntry",
    "DiffReport",
    "Tier",
    # Errors (9)
    "DiffError",
    "DiffCandidateModelMismatchError",
    "DiffPruneResultModelMismatchError",
    "DiffGradingReportModelMismatchError",
    "DiffInputTooLargeError",
    "DiffSidecarRecordTooLargeError",
    "DiffSidecarWriteError",
    "DiffTestFileRecordTooLargeError",
    "DiffTestFileWriteError",
]
