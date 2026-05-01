"""Rubric data model + locked default rubric + canonical-hash helper (US-003).

Three exported objects + one internal helper:

* :class:`Criterion` — frozen Pydantic v2 model with ``extra="forbid"``;
  ``id`` and ``criterion`` are required, non-empty strings (DEC-017). No
  ``weight`` / ``threshold`` field; v0.1 keeps the rubric shape minimal.
* :class:`GradeThresholds` — frozen Pydantic v2 model with
  ``extra="forbid"``; ``min_pass_rate`` / ``min_mean_score`` are bounded
  ``[0.0, 1.0]`` floats. Defaults match the locked Phase-2 numbers.
* :data:`Rubric` — :class:`typing.TypeAlias` over ``tuple[Criterion, ...]``
  (DEC-011). Deliberately NOT a wrapper class; keeps ergonomics in line
  with :class:`signalforge.prune.PruneResult.decisions` (also a tuple).
* :data:`DEFAULT_RUBRIC` — :class:`typing.Final` ``Rubric`` carrying the
  four locked criteria from DEC-016 verbatim. Their IDs and exact text
  are load-bearing for ``rubric_hash`` reproducibility across runs.
* :func:`_canonical_rubric_hash` — internal seam returning a 16-hex-char
  blake2b-8 digest (DEC-010); deterministic + order-invariant by
  construction. Mirrors the pattern in
  :func:`signalforge.safety.policy._compute_policy_hash`.
* :func:`validate_rubric` — rejects duplicate ``id`` values across the
  rubric and rejects the degenerate empty rubric. Per-criterion shape
  validation lives on :class:`Criterion` itself; this helper guards the
  whole-rubric structural invariants Pydantic cannot express on a tuple.

The hash helper is ``_``-prefixed: it is an internal seam that the
audit module (US-007) will call to populate
:class:`signalforge.grade.models.GradeEvent.rubric_hash`. It is not part
of the public package surface in v0.1.

See ``plans/super/7-quality-grader.md`` (DEC-007, DEC-010, DEC-011,
DEC-016, DEC-017) for the full design.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Final, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator

from signalforge.grade.errors import GradeRubricError


class Criterion(BaseModel):
    """One rubric entry: a stable identifier paired with the prompt text
    sent to the LLM-judge (DEC-017).

    ``id`` and ``criterion`` are both required, non-empty strings. The
    model is ``frozen`` (immutable post-construction; matches every
    other rubric-adjacent shape in :mod:`signalforge.grade`) and uses
    ``extra="forbid"`` per ``safety-layer.md`` DEC-015 — a typo like
    ``weight: 1.0`` in the rubric YAML must fail loud rather than be
    silently ignored, which is the failure mode this layer exists to
    prevent. ``populate_by_name=True`` keeps the constructor ergonomic
    when a future story aliases either field.

    No ``weight`` / ``threshold`` fields in v0.1: the four default
    criteria are equally weighted, and the per-criterion pass/fail line
    is encoded in :class:`GradeThresholds` rather than per-criterion to
    keep ``rubric_hash`` (DEC-010) stable across threshold tweaks.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    id: str
    criterion: str

    @field_validator("id", "criterion")
    @classmethod
    def _reject_empty_or_whitespace_only(cls, v: str) -> str:
        # Pydantic v2 already enforces ``str`` typing; the field validator
        # exists to reject the silent-no-op cases: empty string, or a
        # whitespace-only string (which a YAML author might land on after
        # editing a multi-line block scalar). Both are rejected with a
        # message specific enough to localise the bad field.
        if not v or not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class GradeThresholds(BaseModel):
    """Per-rubric pass/fail thresholds for the aggregate report.

    ``min_pass_rate`` is the fraction of (artifact, criterion) pairs
    that must score ``passed=True`` for the rubric to count as passed
    overall; ``min_mean_score`` is the floor on the mean numeric score
    across non-null verdicts. Both default to the locked Phase-2 numbers
    from DEC-016 (0.7 and 0.5 respectively).

    Both must lie in the closed interval ``[0.0, 1.0]``. Out-of-range
    values raise a Pydantic ``ValidationError`` at construction time —
    ``GradeConfig`` (US-004) wraps this as ``GradeConfigError`` so the
    operator gets a remediation line instead of a raw Pydantic trace.

    **v0.1 NOTE: This typed object is exported for forward-compat but
    is not currently consumed by production code.** ``GradeConfig``
    carries the same defaults as flat ``min_pass_rate`` /
    ``min_mean_score`` fields, and ``GradingReport.thresholds`` is a
    bare ``tuple[float, float]``. v0.2 will wire ``GradeThresholds`` as
    the canonical container so callers can pass one in instead of two
    flat scalars; the class ships now so the type-alias surface is
    stable across the v0.1 → v0.2 migration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    min_pass_rate: float = 0.7
    min_mean_score: float = 0.5

    @field_validator("min_pass_rate", "min_mean_score")
    @classmethod
    def _reject_out_of_range(cls, v: float) -> float:
        # The aggregate scoring lives on a [0.0, 1.0] scale (see
        # GradingReport's pass_rate / mean_score computed fields). A
        # threshold outside that range is a configuration error, not a
        # surprising-but-valid request.
        if v < 0.0 or v > 1.0:
            raise ValueError("must be in the closed interval [0.0, 1.0]")
        return v


# ``Rubric`` is a type alias, not a wrapper class (DEC-011). Mirrors
# ``PruneResult.decisions: tuple[PruneDecision, ...]``: Python's tuple is
# already immutable, hashable (when its members are), and trivially
# iterable — wrapping it in a Pydantic model would only buy validation
# that ``validate_rubric`` performs explicitly at the orchestrator entry.
Rubric: TypeAlias = tuple[Criterion, ...]


# DEC-016: the four locked default criteria. The IDs and the exact
# criterion text are load-bearing — every change here is a rubric_hash
# change, which means every audit row written under v0.1 is no longer
# reproducible. Bump ``audit_schema_version`` (DEC-014 of #4) before
# changing any of this in v0.2; the verbatim-match test in
# ``tests/grade/test_rubric.py`` is the regression guard.
DEFAULT_RUBRIC: Final[Rubric] = (
    Criterion(
        id="clarity",
        criterion=(
            "Is the column description clear, specific, and actionable? "
            "Does it unambiguously explain the column's purpose and "
            "business meaning without jargon or vagueness?"
        ),
    ),
    Criterion(
        id="consistency",
        criterion=(
            "Are column names and descriptions consistent in terminology? "
            "Do related concepts use the same term throughout, and do "
            "synonyms or conflicting terminology appear?"
        ),
    ),
    Criterion(
        id="rationale",
        criterion=(
            "Does every test have a clear rationale explaining why it is "
            "needed? Are vague or missing rationales present?"
        ),
    ),
    Criterion(
        id="no-redundant",
        criterion=(
            "Are any tests redundant — semantically identical to another "
            "test, or already dropped by the prune layer as always-passing?"
        ),
    ),
)


def _canonical_rubric_hash(rubric: Rubric) -> str:
    """Return a 16-hex-char blake2b-8 digest of the rubric's canonical
    form (DEC-010).

    Canonical form: a list of ``{"id": ..., "criterion": ...}`` mappings,
    sorted by ``id`` (so the hash is order-invariant by construction —
    swapping two criteria in the rubric tuple does not change the
    digest), then JSON-dumped with ``sort_keys=True`` and the compact
    ``separators=(",", ":")`` so whitespace differences cannot drift the
    hash either. blake2b digest_size=8 yields a 16-hex-character hex
    string; the compact form keeps the audit JSONL records short.

    Mirrors :func:`signalforge.safety.policy._compute_policy_hash`
    (DEC-014 of #4): both helpers exist so a reviewer can verify that
    two audit rows came from the same rubric / policy without re-loading
    ``signalforge.yml``. The grader's audit module (US-007) calls this
    helper once per run and writes the result to every
    :class:`GradeEvent` and to the sidecar :class:`GradingReport`.

    The function does NOT call :func:`validate_rubric` — duplicate IDs
    do not break the hash (the canonical form would still serialise
    deterministically), but they are an upstream programming error the
    orchestrator has already rejected. Keeping the helper structural
    avoids importing the validator in callers that only need a hash.
    """
    canonical_entries = sorted(
        ({"id": c.id, "criterion": c.criterion} for c in rubric),
        key=lambda entry: entry["id"],
    )
    canonical = json.dumps(canonical_entries, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()


def validate_rubric(rubric: Rubric) -> None:
    """Validate the structural invariants Pydantic cannot express on a
    tuple of :class:`Criterion` (DEC-017).

    Raises :class:`signalforge.grade.errors.GradeRubricError` on:

    * **Empty rubric.** A zero-length rubric grades nothing — every
      ``GradingReport`` would carry an empty ``results`` tuple and the
      run would be a silent no-op. Operators land on this case via a
      typo in the YAML (e.g. ``rubric: []``); failing loud is the
      correct behaviour for the same reason ``extra="forbid"`` rejects
      typos elsewhere.
    * **Duplicate ``id``.** Two criteria with the same ``id`` would
      produce two ``GradingResult`` rows the diff renderer (#8) cannot
      disambiguate, and the parser's anchor contract (US-006) leans on
      ``id`` being unique to route the judge response to the right
      criterion. ``Counter``-based detection surfaces every offending
      ``id`` (not just the first) so the operator fixes them all in one
      edit.

    Per-criterion shape validation (non-empty fields, ``extra="forbid"``,
    correct types) is already enforced by :class:`Criterion` itself; this
    helper deliberately does not re-walk per-criterion fields.
    """
    if not rubric:
        raise GradeRubricError(
            "Rubric is empty; the grader has no criteria to score against.",
            remediation=(
                "Provide at least one criterion in the rubric YAML, or omit "
                "the `grade.rubric` key entirely to inherit DEFAULT_RUBRIC. "
                "An empty rubric would make every grade run a silent no-op."
            ),
        )

    counts = Counter(c.id for c in rubric)
    duplicates = sorted(cid for cid, n in counts.items() if n > 1)
    if duplicates:
        raise GradeRubricError(
            f"Rubric contains duplicate criterion id(s): {duplicates!r}.",
            remediation=(
                "Each criterion must have a unique `id`. Rename or remove "
                "the duplicates so the parser's anchor contract can route "
                "the judge response to the correct criterion."
            ),
        )


__all__ = [
    "DEFAULT_RUBRIC",
    "Criterion",
    "GradeThresholds",
    "Rubric",
    "validate_rubric",
]
