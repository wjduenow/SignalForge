"""Typed read-back shapes for the diff renderer (US-002 of #8).

Defines the two read-back-stable Pydantic v2 models the diff renderer
emits to its callers and to its on-disk sidecar:

* :class:`DiffEntry` — one row in the kept/dropped/flagged table
  (DEC-012, DEC-016, DEC-020).
* :class:`DiffReport` — the public sidecar shape, carrying the unified
  diff text, raw YAML payloads, the per-entry tuple, count aggregates,
  and reproducibility hashes (DEC-016, DEC-020).

Both models are ``frozen=True, extra="ignore"`` per the project's
manifest-readers convention (``.claude/rules/manifest-readers.md``):
``frozen=True`` makes them immutable post-construction, ``extra="ignore"``
survives forward-compat field additions in the persisted sidecar JSON.
The drift detector at :mod:`tests.diff.test_models` pairs each model
with a one-off ``extra="forbid"`` ``Strict<X>`` mirror validated against
the committed fixture so a silent schema expansion is loud at test time.

Custom ``__repr__`` (DEC-020 — mirrors prune DEC-022 / grade DEC-022):
both models override ``__repr__`` to surface only identity / aggregate
metadata. The unified diff body, raw YAML payloads, prose ``why`` and
the per-entry tuple stay accessible via field access; the redacted
repr keeps an accidental ``_LOGGER.warning("report: %s", report)`` from
dumping multi-megabyte diff content (and potentially PII inside quoted
artifact text) into log sinks.

DEC-012 — ``DiffEntry.tier: Literal["kept", "dropped", "flagged"]``.
``flagged`` is set only when a grading report was provided AND the
entry's grading is below threshold; the orchestrator (US-008) computes
the tier and never stores ``flagged`` when ``grading_report is None``.
This module treats ``tier`` as an opaque ``Literal`` — assignment of
the tier value is the orchestrator's contract (mirrors how
:class:`signalforge.grade.models.GradingResult` treats ``artifact_id``
as an opaque ``str`` produced by ``_artifact_id_for``).

DEC-016 reproducibility hashes — ``DiffReport`` carries three 16-hex
``blake2b-8`` fingerprints of the upstream inputs (``candidate_hash``,
``prune_result_hash``, ``grading_report_hash``). Mirrors the precedent
established by :class:`signalforge.safety.request.AuditEvent`'s
``policy_hash`` (DEC-014 of #4) and :class:`signalforge.grade.models.GradeEvent`'s
``rubric_hash`` (DEC-010 of #7). The hash computation itself is the
orchestrator's responsibility (US-008); this module stores them as
plain ``str`` fields.

This module declares only data shapes — it has no logging, no I/O, and
no LLM calls. Per ``.claude/rules/manifest-readers.md`` ("no logging /
metrics in stage-0 modules"), observability lives in the orchestrator
that consumes these shapes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from signalforge.prune.models import DropReason

_BASE_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

Tier = Literal["kept", "dropped", "flagged"]
"""Closed set of per-entry tiers emitted by the diff renderer (DEC-012).

* ``"kept"`` — artifact survived prune (and grade, if a report was
  provided) and ships in the proposed schema.yml.
* ``"dropped"`` — artifact was dropped by the prune engine; the
  matched :data:`signalforge.prune.models.DropReason` is carried on
  :attr:`DiffEntry.drop_reason`.
* ``"flagged"`` — artifact survived prune AND a grading report was
  provided AND its grading is below threshold (any criterion failed
  OR a graceful-degrade null score was recorded). The orchestrator
  (US-008) is responsible for assigning ``flagged``; an entry with
  ``tier="flagged"`` and ``grading_report_hash is None`` on the
  parent report is a contract violation (the renderer never produces
  one — defensive readers should treat it as drift).
"""


class DiffEntry(BaseModel):
    """One row in the rendered kept / dropped / flagged table.

    Six fields define the row's content per DEC-016:

    * :attr:`artifact_id` — canonical dotted-path identifier produced
      by the orchestrator's artifact-id formatter (US-006). Treated as
      an opaque ``str`` here so the formatter remains the single
      source of truth for the dotted-path grammar.
    * :attr:`test_type` — the dbt-style test type
      (``"not_null"``, ``"unique"``, ``"accepted_values"``,
      ``"relationships"``) when the artifact is a test; ``None``
      for column / model documentation artifacts.
    * :attr:`tier` — the :data:`Tier` literal documented above.
    * :attr:`drop_reason` — the prune layer's
      :data:`signalforge.prune.models.DropReason` literal when
      ``tier == "dropped"``; ``None`` otherwise (kept and flagged
      artifacts have no drop reason).
    * :attr:`why` — one-line operator-readable explanation. Upstream
      truncation (``DiffConfig.max_why_chars``, default 80 — DEC-010)
      keeps the line readable; this model imposes no shape on the
      string beyond ``str``.
    * :attr:`score` / :attr:`passed` — the
      :class:`signalforge.grade.models.GradingResult` aggregate for
      this artifact when a grading report was provided; ``None`` when
      no report was provided OR the artifact had no grading result
      (e.g. a dropped test never reaches the grader).

    DEC-020 — minimal :meth:`__repr__` exposes only
    ``artifact_id``, ``tier``, ``drop_reason``, ``score``. The prose
    :attr:`why` (potentially quoting artifact text) stays out of
    accidental log lines; full content remains accessible via field
    access or :meth:`pydantic.BaseModel.model_dump`.
    """

    model_config = _BASE_CONFIG

    artifact_id: str
    test_type: str | None = None
    tier: Tier
    drop_reason: DropReason | None = None
    why: str = ""
    score: float | None = None
    passed: bool | None = None

    def __repr__(self) -> str:
        """Minimal repr — omits the prose :attr:`why` (DEC-020).

        Mirrors :class:`signalforge.grade.models.GradingResult` and
        :class:`signalforge.prune.models.PruneDecision`: an accidental
        ``_LOGGER.warning("entry: %s", entry)`` would otherwise
        interpolate the full ``why`` string, which can quote artifact
        text generated upstream. The redacted repr collapses to the
        identifying tuple only; full body remains accessible via
        ``entry.model_dump()``.
        """
        return (
            f"DiffEntry(artifact_id={self.artifact_id!r}, "
            f"tier={self.tier!r}, "
            f"drop_reason={self.drop_reason!r}, "
            f"score={self.score!r})"
        )


class DiffReport(BaseModel):
    """Sidecar shape — the diff renderer's public output for one model.

    Carried verbatim into the on-disk JSON sidecar (DEC-009) and
    returned to the caller of ``render_diff`` (US-008). Mirrors the
    end-of-run sidecar pattern established by
    :class:`signalforge.grade.models.GradingReport` (DEC-012 of #7);
    the per-entry payload (kept/dropped/flagged rows) is the diff
    layer's analogue of grade's per-criterion results tuple.

    Field set per DEC-016:

    * :attr:`schema_version` — forward-compat sentinel pinned to ``1``;
      v0.2 readers gate on this.
    * :attr:`audit_schema_version` — mirrors prior stages (safety #4,
      grade #7); kept at ``1`` so a reviewer querying the sidecar can
      filter on the same field name across the pipeline.
    * :attr:`signalforge_version` — read from
      :data:`signalforge.__version__` at orchestrator entry.
    * :attr:`model_unique_id` — the model under render.
    * :attr:`run_id` — uuid4 hex generated at orchestrator entry,
      ties this sidecar to any companion JSONL artefacts in the same
      run (mirrors grade DEC-020).
    * :attr:`duration_seconds` — wall-clock duration of the
      ``render_diff`` call.
    * :attr:`proposed_yaml` / :attr:`existing_yaml` — raw YAML strings.
      ``existing_yaml is None`` when the operator has no committed
      schema.yml (the unified diff sources from ``/dev/null`` in that
      case).
    * :attr:`unified_diff` — the rendered unified diff body as a
      single string.
    * :attr:`entries` — the per-row tuple. Empty when no candidate
      artifacts existed (vacuous report).
    * :attr:`kept_count` / :attr:`dropped_count` /
      :attr:`flagged_count` — stored, not computed, so a reader of an
      external sidecar JSON gets the orchestrator's authoritative
      counts without re-deriving from :attr:`entries` (forward-compat
      shield against renaming :data:`Tier` literals in v0.2).
    * :attr:`has_existing_schema` — stored convenience boolean equal
      to ``existing_yaml is not None``; the orchestrator stamps it so
      sidecar consumers don't need to introspect ``existing_yaml``.
    * :attr:`candidate_hash` / :attr:`prune_result_hash` /
      :attr:`grading_report_hash` — DEC-016 reproducibility
      fingerprints. ``grading_report_hash is None`` when no grading
      report was provided. The hashes are computed by the
      orchestrator (US-008) over the canonical-sorted JSON of each
      input (mirrors safety's ``policy_hash`` and grade's
      ``rubric_hash``).

    DEC-020 — minimal :meth:`__repr__` exposes only the identifying
    tuple, the three count aggregates, ``has_existing_schema``, and
    ``duration_seconds``. The unified diff body, raw YAML, and per-entry
    tuple stay out of accidental log lines.
    """

    model_config = _BASE_CONFIG

    schema_version: Literal[1] = 1
    audit_schema_version: Literal[1] = 1
    signalforge_version: str
    model_unique_id: str
    run_id: str
    duration_seconds: float
    proposed_yaml: str
    existing_yaml: str | None
    unified_diff: str
    entries: tuple[DiffEntry, ...]
    kept_count: int
    dropped_count: int
    flagged_count: int
    has_existing_schema: bool
    candidate_hash: str
    prune_result_hash: str
    grading_report_hash: str | None

    def __repr__(self) -> str:
        """Minimal repr — omits raw YAML, unified diff, and per-entry payload (DEC-020).

        The full ``proposed_yaml`` / ``existing_yaml`` / ``unified_diff``
        and ``entries`` tuple remain accessible via field access /
        ``report.model_dump()`` — the custom repr only protects
        accidental ``_LOGGER`` interpolations from dumping multi-megabyte
        diff content into log sinks.
        """
        return (
            f"DiffReport(model_unique_id={self.model_unique_id!r}, "
            f"kept_count={self.kept_count!r}, "
            f"dropped_count={self.dropped_count!r}, "
            f"flagged_count={self.flagged_count!r}, "
            f"has_existing_schema={self.has_existing_schema!r}, "
            f"duration_seconds={self.duration_seconds!r})"
        )


__all__ = (
    "DiffEntry",
    "DiffReport",
    "Tier",
)
