"""Airflow-free run-over-run drift / signal-rot detection (pure core).

US-001 of issue #235 (epic #228, v0.7 Airflow). This module is the *pure
core* of the drift-detection feature: it compares a SignalForge run (N)
against its prior run (N-1) — two :class:`signalforge.diff.models.DiffReport`
sidecars, plus optional :class:`signalforge.grade.models.GradingReport`
sidecars — and emits a structured, JSON-serialisable :class:`DriftReport`
delta.

The headline signal is **signal rot** — a test that *used to* catch failing
rows (a ``kept`` / ``kept-uncertain`` / ``flagged`` tier) but now
**always-passes** (``dropped`` with ``drop_reason == "always-passes"``). That
transition is a schema-drift alarm worth paging on (Architectural Commitment
#1: an always-pass test is noise; a test that *rotted into* always-pass is a
drift signal).

**This module imports NO airflow.** That is load-bearing (DEC-002): it lets
``signalforge.airflow.__init__`` re-export :func:`compute_drift` and
:class:`DriftReport` **eagerly** (alongside ``result`` / ``runner`` — only the
operator/hook names stay lazy), and it keeps the comparison logic
unit-testable in the default pytest suite without the heavy, version-pinned
Apache Airflow dependency installed. Per the v0.8 note in
``.claude/rules/airflow-integration.md``, this pure core is a candidate to
hoist to a neutral ``signalforge.automation`` package later; out of scope here.

:func:`compute_drift` is pure, deterministic, and does NO I/O — it takes parsed
:class:`DiffReport` / :class:`GradingReport` objects (the loaders that read
them off disk / stdout land in US-003). The comparison **degrades, never
raises** (DEC-013): a model-set mismatch yields an empty report with a
``degrade_reason`` rather than an exception, and a missing grade sidecar simply
leaves ``grade_regressions`` empty.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

import signalforge
from signalforge.diff.models import DiffEntry, DiffReport
from signalforge.grade.models import GradingReport

_BASE_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

# Truncation budget for the per-artifact ``why`` string carried on a
# :class:`DriftArtifact` (mirrors the diff layer's ``_truncate_why`` idea — a
# one-line operator-readable explanation, not a multi-line dump).
_WHY_MAX_CHARS = 200

# Tiers that mean "the artifact ships in the proposed schema.yml". ``flagged``
# is kept-ish for transition purposes (DEC-005) — it still ships, it just
# graded below threshold. ``dropped`` is the only non-kept-ish tier.
_KEPT_ISH_TIERS: frozenset[str] = frozenset({"kept", "kept-uncertain", "flagged"})


def _truncate_why(text: str, max_chars: int = _WHY_MAX_CHARS) -> str:
    """Truncate ``text`` to ``max_chars`` with a U+2026 ellipsis tail.

    Mirrors :func:`signalforge.diff.engine._truncate_why` byte-for-byte:
    empty / whitespace-only input returns the empty string; a non-positive
    budget returns the empty string; an over-budget string is hard-cut at
    ``max_chars - 1`` (after :meth:`str.rstrip`) with ``"…"`` appended;
    otherwise the string is returned with a trailing :meth:`str.rstrip` only.
    """
    if not text or not text.strip():
        return ""
    if max_chars <= 0:
        return ""
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text.rstrip()


def _hash_report(report: BaseModel) -> str:
    """Return the project's canonical ``blake2b-8`` fingerprint of a model.

    DEC-016 recipe (inlined to keep this module airflow-free and free of a
    cross-stage import of the diff layer's private helper): serialise via
    ``model_dump_json(by_alias=True)`` and re-encode through
    :func:`json.dumps` with ``sort_keys=True`` + ``separators=(",", ":")`` so
    equivalent inputs produce identical 16-hex digests regardless of
    field-construction order.
    """
    raw_json = report.model_dump_json(by_alias=True)
    parsed = json.loads(raw_json)
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()


def _columns_from_artifact_ids(artifact_ids: Iterable[str]) -> set[str]:
    """Derive the column SET from ``artifact_id`` dotted-path prefixes.

    Column names are embedded in two of the six dotted-path shapes
    (``.claude/rules`` + :mod:`signalforge._common.artifact_id`):

    * ``column.<col>.<field>`` → column is the 2nd component.
    * ``test.column.<col>.<type>[.<hash>]`` → column is the 3rd component.

    ``model.<field>`` and ``test.model.<type>`` carry no column. Column names
    pass the strict ``^[A-Za-z_][A-Za-z0-9_]*$`` identifier regex upstream, so
    they never contain a dot — a plain ``str.split(".")`` is unambiguous.
    """
    columns: set[str] = set()
    for artifact_id in artifact_ids:
        parts = artifact_id.split(".")
        if len(parts) >= 2 and parts[0] == "column":
            columns.add(parts[1])
        elif len(parts) >= 3 and parts[0] == "test" and parts[1] == "column":
            columns.add(parts[2])
    return columns


class DriftArtifact(BaseModel):
    """One artifact whose tier transitioned between two runs.

    Carried on the three transition tuples of :class:`DriftReport`
    (``newly_always_passes`` / ``newly_dropped`` / ``newly_kept``). Records the
    artifact's identity plus its before/after tier and drop_reason, and a
    truncated one-line ``why`` (the current run's explanation for the new
    tier).

    Read-back-stable: ``frozen=True, extra="ignore"`` per the manifest-readers
    convention.
    """

    model_config = _BASE_CONFIG

    artifact_id: str
    previous_tier: str | None
    current_tier: str | None
    previous_drop_reason: str | None
    current_drop_reason: str | None
    why: str = ""

    @field_validator("why")
    @classmethod
    def _truncate_why_field(cls, value: str) -> str:
        """Cap ``why`` at :data:`_WHY_MAX_CHARS` regardless of caller."""
        return _truncate_why(value)


class GradeRegression(BaseModel):
    """A run-over-run grade regression for one model.

    Emitted when ``current_mean <= previous_mean - grade_regression_threshold``
    (DEC-005). ``delta = previous_mean - current_mean`` — positive means the
    grade fell (a regression).

    Read-back-stable: ``frozen=True, extra="ignore"``.
    """

    model_config = _BASE_CONFIG

    model_unique_id: str
    previous_mean: float
    current_mean: float
    delta: float


class SchemaShapeDelta(BaseModel):
    """Column SET add/remove between two runs (DEC-005, Q4).

    Derived from the union of ``artifact_id`` prefixes across the two reports.
    Column TYPE changes (retype) are out of scope — the sidecars carry no
    types. Both tuples are sorted for determinism.

    Read-back-stable: ``frozen=True, extra="ignore"``.
    """

    model_config = _BASE_CONFIG

    columns_added: tuple[str, ...] = ()
    columns_removed: tuple[str, ...] = ()


class DriftReport(BaseModel):
    """Run-over-run drift delta — the public output of :func:`compute_drift`.

    Pushed to XCom (via :meth:`to_xcom`) and optionally compared by the
    ``on_drift`` policy (US-003). Read-back-stable (``frozen=True,
    extra="ignore"``) and self-describing: it carries the two input
    ``blake2b-8`` hashes so the same two sidecars + same ``as_of`` reproduce a
    byte-identical report (DEC-014 / DEC-016).

    The headline :attr:`alarming` property is ``True`` iff there is signal rot
    (``newly_always_passes``) or a grade regression (``grade_regressions``) —
    these are the categories that page (DEC-005, Q3). ``newly_kept`` /
    ``newly_dropped`` / ``added_artifacts`` / ``removed_artifacts`` /
    ``schema_shape_changes`` are informational and never alarm. A degraded
    report (``degrade_reason`` set) is never alarming — the alarm lists are
    empty by construction (DEC-013).

    DEC-015 — minimal :meth:`__repr__` (and Pydantic-v2 :meth:`__repr_args__`)
    omit the long transition lists so an accidental ``_LOGGER.warning("drift:
    %s", report)`` doesn't dump every transitioned artifact's prose ``why``.
    """

    model_config = _BASE_CONFIG

    schema_version: Literal[1] = 1
    signalforge_version: str
    model_unique_id: str
    as_of: date | None
    grade_regression_threshold: float
    baseline: bool = False
    previous_diff_hash: str
    current_diff_hash: str
    newly_always_passes: tuple[DriftArtifact, ...] = ()
    newly_dropped: tuple[DriftArtifact, ...] = ()
    newly_kept: tuple[DriftArtifact, ...] = ()
    added_artifacts: tuple[str, ...] = ()
    removed_artifacts: tuple[str, ...] = ()
    grade_regressions: tuple[GradeRegression, ...] = ()
    schema_shape_changes: SchemaShapeDelta = SchemaShapeDelta()
    degrade_reason: str | None = None

    @field_serializer("as_of")
    def _serialize_as_of(self, value: date | None) -> str | None:
        """Render ``as_of`` as a bare ``YYYY-MM-DD`` ISO string or ``null``.

        Uses :meth:`date.isoformat` directly — NOT
        :func:`signalforge._common.timestamp.iso8601_z`, which is
        ``datetime``-only and would append a spurious ``T00:00:00Z`` suffix
        (mirrors :class:`signalforge.prune.audit.PruneEvent`'s ``as_of``
        serializer, issue #171).
        """
        return value.isoformat() if value is not None else None

    @property
    def alarming(self) -> bool:
        """Whether this drift trips the ``on_drift`` policy (DEC-005).

        ``True`` iff there is signal rot (:attr:`newly_always_passes`) or a
        grade regression (:attr:`grade_regressions`). A degraded report returns
        ``False`` because both lists are empty by construction (DEC-013).
        """
        return bool(self.newly_always_passes) or bool(self.grade_regressions)

    def to_xcom(self) -> dict[str, object]:
        """Return a JSON-serialisable summary for XCom (DEC-015).

        Per-category counts + the transition artifact lists (with the already
        truncated ``why``) + ``alarming`` + ``as_of`` (iso str or ``None``) +
        the two input hashes + ``grade_regressions`` / ``schema_shape_changes``
        as dicts + ``degrade_reason``. Carries NO bulk sidecar text and NO
        secrets — every value round-trips through ``json.dumps`` /
        ``json.loads``.
        """
        return {
            "schema_version": self.schema_version,
            "model_unique_id": self.model_unique_id,
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "baseline": self.baseline,
            "alarming": self.alarming,
            "grade_regression_threshold": self.grade_regression_threshold,
            "previous_diff_hash": self.previous_diff_hash,
            "current_diff_hash": self.current_diff_hash,
            "degrade_reason": self.degrade_reason,
            "counts": {
                "newly_always_passes": len(self.newly_always_passes),
                "newly_dropped": len(self.newly_dropped),
                "newly_kept": len(self.newly_kept),
                "added_artifacts": len(self.added_artifacts),
                "removed_artifacts": len(self.removed_artifacts),
                "grade_regressions": len(self.grade_regressions),
                "columns_added": len(self.schema_shape_changes.columns_added),
                "columns_removed": len(self.schema_shape_changes.columns_removed),
            },
            "newly_always_passes": [a.model_dump() for a in self.newly_always_passes],
            "newly_dropped": [a.model_dump() for a in self.newly_dropped],
            "newly_kept": [a.model_dump() for a in self.newly_kept],
            "added_artifacts": list(self.added_artifacts),
            "removed_artifacts": list(self.removed_artifacts),
            "grade_regressions": [g.model_dump() for g in self.grade_regressions],
            "schema_shape_changes": {
                "columns_added": list(self.schema_shape_changes.columns_added),
                "columns_removed": list(self.schema_shape_changes.columns_removed),
            },
        }

    def __repr__(self) -> str:
        """Minimal repr — omits the long transition lists (DEC-015)."""
        return (
            f"DriftReport(model_unique_id={self.model_unique_id!r}, "
            f"as_of={self.as_of!r}, "
            f"newly_always_passes={len(self.newly_always_passes)!r}, "
            f"newly_dropped={len(self.newly_dropped)!r}, "
            f"newly_kept={len(self.newly_kept)!r}, "
            f"grade_regressions={len(self.grade_regressions)!r}, "
            f"alarming={self.alarming!r}, "
            f"baseline={self.baseline!r})"
        )

    def __repr_args__(self) -> list[tuple[str | None, object]]:
        """Pydantic v2 structured-repr hook mirroring :meth:`__repr__`.

        Keeps ``rich.print()`` / ``devtools.pretty()`` / ``pprint`` from
        dumping the long transition lists too (see memory
        ``pydantic-v2-repr-args-redaction-required``).
        """
        return [
            ("model_unique_id", self.model_unique_id),
            ("as_of", self.as_of),
            ("newly_always_passes", len(self.newly_always_passes)),
            ("newly_dropped", len(self.newly_dropped)),
            ("newly_kept", len(self.newly_kept)),
            ("grade_regressions", len(self.grade_regressions)),
            ("alarming", self.alarming),
            ("baseline", self.baseline),
        ]


def _build_drift_artifact(
    artifact_id: str, previous: DiffEntry, current: DiffEntry
) -> DriftArtifact:
    """Build a :class:`DriftArtifact` from a matched (prev, curr) entry pair.

    The ``why`` is taken from the *current* entry (it describes the new tier)
    and is truncated by the :class:`DriftArtifact` field validator.
    """
    return DriftArtifact(
        artifact_id=artifact_id,
        previous_tier=previous.tier,
        current_tier=current.tier,
        previous_drop_reason=previous.drop_reason,
        current_drop_reason=current.drop_reason,
        why=current.why,
    )


def compute_drift(
    *,
    previous_diff: DiffReport,
    current_diff: DiffReport,
    previous_grade: GradingReport | None = None,
    current_grade: GradingReport | None = None,
    as_of: date | None = None,
    grade_regression_threshold: float = 0.05,
) -> DriftReport:
    """Compare two SignalForge runs and return a :class:`DriftReport` delta.

    Pure, deterministic, no I/O, no airflow (DEC-003). Iterates ``sorted``
    ``artifact_id``s so the output is reproducible: the same two sidecars +
    same ``as_of`` produce a byte-identical report (DEC-016).

    Transition classification (DEC-005), over the UNION of ``artifact_id``s,
    with ``flagged`` treated as a kept-ish (shipped) tier:

    * ``newly_always_passes`` — in BOTH; prior kept-ish; current ``dropped``
      with ``drop_reason == "always-passes"``. **The signal-rot alarm.**
    * ``newly_dropped`` — in BOTH; prior kept-ish; current ``dropped`` with a
      drop_reason ≠ ``always-passes``. (Disjoint from ``newly_always_passes``.)
    * ``newly_kept`` — in BOTH; prior ``dropped``; current kept-ish.
    * ``added_artifacts`` / ``removed_artifacts`` — present in only the current
      / only the prior report (informational).

    ``schema_shape_changes`` derives the column add/remove SET from
    ``artifact_id`` prefixes. ``grade_regressions`` is emitted only when BOTH
    grades are present and the mean fell beyond ``grade_regression_threshold``;
    a missing grade (``--no-grade``) leaves it empty (degrade, don't fail —
    DEC-013).

    Degrade (never raise — DEC-013): a ``model_unique_id`` mismatch between the
    two diffs returns an empty report with ``degrade_reason`` set and
    ``alarming`` ``False``. ``compute_drift`` does NOT handle the "no prior at
    all" baseline case — that is a loader concern (US-003); ``baseline``
    defaults to ``False`` here.
    """
    version = signalforge.__version__
    previous_diff_hash = _hash_report(previous_diff)
    current_diff_hash = _hash_report(current_diff)

    # Degrade: model-set mismatch. Empty report, never alarming (DEC-013).
    if previous_diff.model_unique_id != current_diff.model_unique_id:
        return DriftReport(
            signalforge_version=version,
            model_unique_id=current_diff.model_unique_id,
            as_of=as_of,
            grade_regression_threshold=grade_regression_threshold,
            previous_diff_hash=previous_diff_hash,
            current_diff_hash=current_diff_hash,
            degrade_reason=(
                f"model mismatch: prior={previous_diff.model_unique_id} "
                f"current={current_diff.model_unique_id}"
            ),
        )

    previous_by_id: dict[str, DiffEntry] = {e.artifact_id: e for e in previous_diff.entries}
    current_by_id: dict[str, DiffEntry] = {e.artifact_id: e for e in current_diff.entries}

    newly_always_passes: list[DriftArtifact] = []
    newly_dropped: list[DriftArtifact] = []
    newly_kept: list[DriftArtifact] = []
    added_artifacts: list[str] = []
    removed_artifacts: list[str] = []

    all_ids = sorted(set(previous_by_id) | set(current_by_id))
    for artifact_id in all_ids:
        prev = previous_by_id.get(artifact_id)
        curr = current_by_id.get(artifact_id)
        if prev is None:
            # Present only in the current run → an added artifact.
            added_artifacts.append(artifact_id)
            continue
        if curr is None:
            # Present only in the prior run → a removed artifact.
            removed_artifacts.append(artifact_id)
            continue
        prev_kept_ish = prev.tier in _KEPT_ISH_TIERS
        curr_kept_ish = curr.tier in _KEPT_ISH_TIERS
        if prev_kept_ish and curr.tier == "dropped":
            artifact = _build_drift_artifact(artifact_id, prev, curr)
            if curr.drop_reason == "always-passes":
                newly_always_passes.append(artifact)
            else:
                newly_dropped.append(artifact)
        elif prev.tier == "dropped" and curr_kept_ish:
            newly_kept.append(_build_drift_artifact(artifact_id, prev, curr))
        # Same-tier (or any other kept-ish ↔ kept-ish) pair: no-op.

    previous_columns = _columns_from_artifact_ids(previous_by_id)
    current_columns = _columns_from_artifact_ids(current_by_id)
    schema_shape_changes = SchemaShapeDelta(
        columns_added=tuple(sorted(current_columns - previous_columns)),
        columns_removed=tuple(sorted(previous_columns - current_columns)),
    )

    grade_regressions: list[GradeRegression] = []
    if previous_grade is not None and current_grade is not None:
        previous_mean = previous_grade.mean_score
        current_mean = current_grade.mean_score
        if current_mean <= previous_mean - grade_regression_threshold:
            grade_regressions.append(
                GradeRegression(
                    model_unique_id=current_diff.model_unique_id,
                    previous_mean=previous_mean,
                    current_mean=current_mean,
                    delta=previous_mean - current_mean,
                )
            )

    return DriftReport(
        signalforge_version=version,
        model_unique_id=current_diff.model_unique_id,
        as_of=as_of,
        grade_regression_threshold=grade_regression_threshold,
        previous_diff_hash=previous_diff_hash,
        current_diff_hash=current_diff_hash,
        newly_always_passes=tuple(newly_always_passes),
        newly_dropped=tuple(newly_dropped),
        newly_kept=tuple(newly_kept),
        added_artifacts=tuple(added_artifacts),
        removed_artifacts=tuple(removed_artifacts),
        grade_regressions=tuple(grade_regressions),
        schema_shape_changes=schema_shape_changes,
    )


__all__ = [
    "DriftArtifact",
    "DriftReport",
    "GradeRegression",
    "SchemaShapeDelta",
    "compute_drift",
]
