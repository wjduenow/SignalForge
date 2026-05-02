"""Diff orchestrator (US-010 of issue #8) — wires every prior story.

:func:`render_diff` is the public-facing entry point. Given a model + drafted
candidate + prune verdict (+ optional grading report + optional existing
schema text + config + I/O paths), it:

1. Validates the three boundary checks (DEC-002) BEFORE any other work.
2. Validates the ``existing_schema`` size cap (DEC-006) BEFORE any
   ``yaml.safe_load`` call; emits the soft-warn (DEC-014) when the
   payload exceeds the configured warn-at threshold.
3. Builds the canonical proposed YAML via
   :func:`signalforge.diff._emitter.emit_proposed_yaml`.
4. Computes the unified diff via :func:`difflib.unified_diff` against the
   raw existing schema text (or ``/dev/null`` when none was provided).
5. Walks ``prune_result.decisions`` and ``candidate``'s columns / tests /
   doc fields to assemble the per-row :class:`DiffEntry` tuple, joining
   :class:`signalforge.grade.models.GradingResult` rows by the canonical
   ``artifact_id`` produced by :func:`_artifact_id.artifact_id_for`.
6. Stamps three reproducibility hashes (DEC-016) — ``candidate_hash``,
   ``prune_result_hash``, and (when given) ``grading_report_hash``.
7. Constructs the :class:`DiffReport` and dispatches to the renderer
   selected via ``config.render_kind`` (``ansi`` / ``markdown`` / ``json``).
8. When ``output_path`` is supplied, writes the rendered text via the
   fail-closed atomic-write seam (mirrors the sidecar writer's contract).
9. When ``sidecar_path`` is supplied (or the default resolves), invokes
   :func:`signalforge.diff._sidecar.write_sidecar` for the durable JSON
   sidecar.
10. Emits one INFO log via lazy-format :func:`json.dumps` (DEC-015).

The orchestrator owns symlink-hardened path canonicalisation against the
caller-supplied ``project_dir`` for both ``output_path`` and
``sidecar_path`` BEFORE the writer ever sees them. Mirrors the post-QG
fix in :mod:`signalforge.grade.engine` verbatim — the writer's own
canonicalise stays as defence-in-depth, but the load-bearing gate is
here. Failures wrap as :class:`DiffSidecarWriteError`.

Hash recipe (DEC-016): each input is fed through
``blake2b(model_dump_json(..., by_alias=True), digest_size=8)`` with the
``model_dump_json`` carrying ``sort_keys=True`` so equivalent inputs
produce identical hashes regardless of construction order. The 8-byte
(16-hex-char) digest mirrors the precedent set by
:class:`signalforge.safety.request.AuditEvent`'s ``policy_hash``,
:class:`signalforge.draft.LLMResponseEvent`'s ``response_text_hash``,
and :class:`signalforge.grade.models.GradeEvent`'s ``rubric_hash``.

The :class:`JsonRenderer` concrete is small enough to fold into this
ticket alongside the orchestrator (per the US-010 task description). It
is appended to :mod:`signalforge.diff._renderers` rather than declared
here so the renderer ABC + every concrete live in one module.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path

import yaml

import signalforge as _sf
from signalforge.diff._artifact_id import artifact_id_for, compute_args_hashes
from signalforge.diff._emitter import emit_proposed_yaml
from signalforge.diff._renderers import (
    AnsiRenderer,
    JsonRenderer,
    MarkdownRenderer,
    Renderer,
)
from signalforge.diff._sidecar import write_sidecar
from signalforge.diff.config import DiffConfig
from signalforge.diff.errors import (
    DiffCandidateModelMismatchError,
    DiffGradingReportModelMismatchError,
    DiffInputTooLargeError,
    DiffPruneResultModelMismatchError,
    DiffSidecarRecordTooLargeError,
    DiffSidecarWriteError,
)
from signalforge.diff.models import DiffEntry, DiffReport, Tier
from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTest,
)
from signalforge.grade.models import GradingReport, GradingResult
from signalforge.manifest.models import Model
from signalforge.prune.models import DropReason, PruneDecision, PruneResult
from signalforge.warehouse._path_safety import canonicalise_path
from signalforge.warehouse.errors import ProfileNotFoundError as _PathContainmentError

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reproducibility hashes (DEC-016)
# ---------------------------------------------------------------------------


def _blake2b_8(payload: bytes) -> str:
    """Return a 16-hex-char ``blake2b-8`` digest of ``payload``.

    Mirrors :func:`signalforge.grade.engine._hash_response_text` and the
    safety / draft / prune precedent: 8-byte digest, hex-encoded, used
    as a stable fingerprint.
    """
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


def _hash_pydantic(obj: object) -> str:
    """Return the canonical-sort blake2b-8 hash of a Pydantic v2 model.

    DEC-016 hash recipe: serialise via ``model_dump_json(by_alias=True)``
    and re-encode through :func:`json.dumps` with ``sort_keys=True``
    + ``separators=(",", ":")`` so equivalent inputs produce identical
    digests regardless of field-construction order. The double-pass
    avoids relying on Pydantic's internal field ordering, which Pydantic
    v2 declares stable but is not contractually guaranteed to remain so
    across point releases.
    """
    raw_json = obj.model_dump_json(by_alias=True)  # type: ignore[attr-defined]
    parsed = json.loads(raw_json)
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return _blake2b_8(canonical.encode("utf-8"))


# ---------------------------------------------------------------------------
# DiffEntry assembly helpers
# ---------------------------------------------------------------------------


def _decision_to_drop_reason(decision: PruneDecision) -> DropReason | None:
    """Return the :data:`DropReason` for a dropped decision, else ``None``.

    Kept decisions carry a ``reason`` literal in the prune layer (e.g.
    ``"kept"``, ``"kept-without-evidence"``); only ``decision="dropped"``
    rows carry a meaningful drop-reason for the diff renderer's
    kept/dropped/flagged table. The renderer surfaces only the
    ``"dropped"`` reasons so the per-row "why" stays focused.
    """
    if decision.decision != "dropped":
        return None
    return decision.reason


def _resolve_test_artifact_id(
    decision: PruneDecision,
    args_hashes: dict[int, str | None],
) -> str:
    """Translate a :class:`PruneDecision` into a canonical ``artifact_id``.

    Uses :func:`signalforge.diff._artifact_id.artifact_id_for`, joining
    via the test's ``id()`` against the pre-computed ``args_hashes``
    table from :func:`compute_args_hashes`. Mirrors the join-by-anchor
    pattern in :mod:`signalforge.diff._emitter._kept_fingerprints`.
    """
    anchor = decision.test_anchor
    args_hash = args_hashes.get(id(decision.test))
    if anchor.startswith("column."):
        column_name = anchor[len("column.") :]
        return artifact_id_for(
            scope="column",
            column_name=column_name,
            test=decision.test,
            args_hash=args_hash,
        )
    # Treat any non-column anchor (the literal "model" plus any
    # forward-compatible sentinel) as model-scoped.
    return artifact_id_for(
        scope="model",
        test=decision.test,
        args_hash=args_hash,
    )


def _grading_index(
    grading_report: GradingReport | None,
) -> dict[str, list[GradingResult]]:
    """Bucket :class:`GradingResult` rows by ``artifact_id``.

    A single artifact may carry multiple grading results (one per
    rubric criterion); the diff renderer aggregates per-artifact for
    the kept/dropped/flagged table. Empty index when no grading report
    was supplied.
    """
    if grading_report is None:
        return {}
    out: dict[str, list[GradingResult]] = {}
    for result in grading_report.results:
        out.setdefault(result.artifact_id, []).append(result)
    return out


def _aggregate_grading(
    results: list[GradingResult],
) -> tuple[float | None, bool | None]:
    """Aggregate per-criterion :class:`GradingResult` into ``(score, passed)``.

    * ``score`` — mean of non-None scores, or ``None`` if every result
      was degraded.
    * ``passed`` — ``True`` iff every result is scored AND every
      result's ``passed`` is ``True``; ``False`` otherwise; ``None``
      when no grading was applied.

    Mirrors :class:`signalforge.grade.models.GradingReport`'s aggregate
    semantics (skip-null-scores) at the per-artifact level.
    """
    if not results:
        return None, None
    scored = [r for r in results if r.score is not None]
    if not scored:
        return None, False
    mean = sum(r.score for r in scored if r.score is not None) / len(scored)
    all_passed = all(r.passed for r in scored) and len(scored) == len(results)
    return mean, all_passed


def _tier_for_kept(score: float | None, passed: bool | None) -> Tier:
    """Return ``"flagged"`` or ``"kept"`` for a kept artifact.

    DEC-012: ``flagged`` is set only when a grading report was provided
    AND the entry's grading is below threshold (``passed=False`` for any
    criterion OR a graceful-degrade null score was recorded). When no
    grading was provided (``score=None`` AND ``passed=None``), the
    entry is plain ``"kept"``.
    """
    if score is None and passed is None:
        return "kept"
    if passed is False or score is None:
        return "flagged"
    return "kept"


def _entry_for_test(
    decision: PruneDecision,
    args_hashes: dict[int, str | None],
    grading_index: dict[str, list[GradingResult]],
) -> DiffEntry:
    """Build a :class:`DiffEntry` for one :class:`PruneDecision`."""
    artifact_id = _resolve_test_artifact_id(decision, args_hashes)
    if decision.decision == "dropped":
        return DiffEntry(
            artifact_id=artifact_id,
            test_type=decision.test.type,
            tier="dropped",
            drop_reason=_decision_to_drop_reason(decision),
            why=decision.why,
            score=None,
            passed=None,
        )

    # decision == "kept" — join grading aggregate (if any).
    grading_results = grading_index.get(artifact_id, [])
    score, passed = _aggregate_grading(grading_results)
    tier: Tier = _tier_for_kept(score, passed)
    return DiffEntry(
        artifact_id=artifact_id,
        test_type=decision.test.type,
        tier=tier,
        drop_reason=None,
        why=decision.why,
        score=score,
        passed=passed,
    )


def _entries_for_doc(
    *,
    artifact_id: str,
    description: str,
    grading_index: dict[str, list[GradingResult]],
) -> DiffEntry | None:
    """Build a :class:`DiffEntry` for a doc / rationale artifact, or
    ``None`` if there is no grading attached to the artifact (doc rows
    only surface when a grading report supplied per-criterion signal).

    The diff layer's kept/dropped/flagged table surfaces only artifacts
    with positive evidence — every prune decision (kept or dropped),
    plus every doc artifact a grading run scored. Doc artifacts without
    a grading report stay out of the table; their content is visible
    in the unified-diff section, which is the operator's primary signal
    for descriptions.
    """
    grading_results = grading_index.get(artifact_id, [])
    if not grading_results:
        return None
    score, passed = _aggregate_grading(grading_results)
    tier: Tier = _tier_for_kept(score, passed)
    # Use the first criterion's reasoning as the row "why"; the diff
    # layer's per-row why is intentionally one-line. The full
    # per-criterion record stays in the grade sidecar.
    why = grading_results[0].reasoning or description
    return DiffEntry(
        artifact_id=artifact_id,
        test_type=None,
        tier=tier,
        drop_reason=None,
        why=why,
        score=score,
        passed=passed,
    )


def _build_entries(
    candidate: CandidateSchema,
    prune_result: PruneResult,
    grading_report: GradingReport | None,
) -> tuple[DiffEntry, ...]:
    """Walk every artifact and return the per-row tuple.

    Order:

    1. Model-level doc / rationale (when graded).
    2. Per-column doc / rationale (when graded), in the candidate's
       column declaration order.
    3. All prune decisions (kept + dropped) in their decision order.

    Graded doc artifacts only land in the table when grading is
    present — otherwise the unified diff body carries the doc-text
    signal and we don't surface a redundant row.
    """
    args_hashes = compute_args_hashes(candidate)
    grading_index = _grading_index(grading_report)

    rows: list[DiffEntry] = []

    # Model-level doc artifacts.
    for field in ("description", "rationale"):
        artifact_id = artifact_id_for(scope="model", field=field)  # type: ignore[arg-type]
        text = getattr(candidate, field, None)
        if text is None:
            continue
        entry = _entries_for_doc(
            artifact_id=artifact_id,
            description=text,
            grading_index=grading_index,
        )
        if entry is not None:
            rows.append(entry)

    # Column-level doc artifacts.
    for column in candidate.columns:
        for field in ("description", "rationale"):
            artifact_id = artifact_id_for(
                scope="column",
                column_name=column.name,
                field=field,  # type: ignore[arg-type]
            )
            text = getattr(column, field, None)
            if text is None:
                continue
            entry = _entries_for_doc(
                artifact_id=artifact_id,
                description=text,
                grading_index=grading_index,
            )
            if entry is not None:
                rows.append(entry)

    # Prune decisions (kept + dropped).
    for decision in prune_result.decisions:
        rows.append(_entry_for_test(decision, args_hashes, grading_index))

    return tuple(rows)


# ---------------------------------------------------------------------------
# Renderer dispatch (DEC-004)
# ---------------------------------------------------------------------------


def _build_renderer(config: DiffConfig, *, project_dir: Path | None) -> Renderer:
    """Pick the renderer concrete based on ``config.render_kind``.

    Mirrors the dispatch table documented in DEC-004 of #8: ``ansi`` →
    :class:`AnsiRenderer`, ``markdown`` → :class:`MarkdownRenderer`,
    ``json`` → :class:`JsonRenderer`. The sidecar always uses the JSON
    renderer regardless of this setting (DEC-004); ``render_kind`` only
    governs the human-facing output.
    """
    if config.render_kind == "ansi":
        return AnsiRenderer(config=config)
    if config.render_kind == "markdown":
        project_dir_str = str(project_dir) if project_dir is not None else None
        return MarkdownRenderer(config=config, project_dir=project_dir_str)
    if config.render_kind == "json":
        return JsonRenderer()
    # Pydantic's ``Literal`` validator rejects every other value at
    # config-load time, but the static checker doesn't know that.
    raise ValueError(  # pragma: no cover — defensive
        f"unrecognised render_kind: {config.render_kind!r}"
    )


# ---------------------------------------------------------------------------
# output_path writer (mirrors sidecar writer fail-closed semantics)
# ---------------------------------------------------------------------------


def _write_rendered_text(text: str, *, output_path: Path) -> None:
    """Write rendered text to ``output_path`` durably.

    Mirrors :func:`signalforge.diff._sidecar.write_sidecar` semantics:
    ``O_WRONLY | O_CREAT | O_TRUNC | 0o600``, single ``write`` (looped
    on short returns), ``fsync``, close. **No try/except** around the
    write/fsync — propagation is the contract. The orchestrator catches
    the raw :class:`OSError` and wraps it as
    :class:`DiffSidecarWriteError` so callers branch on one diff-layer
    error class.
    """
    import contextlib
    import os

    encoded = text.encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        str(output_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        written = 0
        while written < len(encoded):
            n = os.write(fd, encoded[written:])
            if n == 0:
                raise OSError("os.write returned 0 — disk full or other I/O failure")
            written += n
        os.fsync(fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


# ---------------------------------------------------------------------------
# Public API (DEC-001)
# ---------------------------------------------------------------------------


def render_diff(
    model: Model,
    candidate: CandidateSchema,
    prune_result: PruneResult,
    *,
    grading_report: GradingReport | None = None,
    existing_schema: str | None = None,
    config: DiffConfig | None = None,
    output_path: Path | None = None,
    sidecar_path: Path | None = None,
    project_dir: Path | None = None,
) -> DiffReport:
    """Build and render a :class:`DiffReport` for ``model``.

    End-to-end orchestrator that wires every prior story (errors US-001,
    models US-002, config US-003, safety US-004, emitter US-005,
    artifact-id US-006, sidecar US-007, renderers US-008/009) into one
    public seam. Mirrors :func:`signalforge.grade.grade_artifacts` and
    :func:`signalforge.prune.prune_tests` in calling-convention shape:
    keyword-only optionals, model-front-paired, sequential execution.

    Pipeline:

    1. Resolve every optional argument (config defaults, ``project_dir``
       defaults to :func:`pathlib.Path.cwd`).
    2. Boundary checks (DEC-002): mismatched ids raise typed errors
       BEFORE any other work.
    3. ``existing_schema`` size check (DEC-006); soft-warn (DEC-014);
       parse-validate via :func:`yaml.safe_load` to confirm parseability
       — the parsed value is discarded; the diff is computed against
       the raw text.
    4. Build canonical proposed YAML via :func:`emit_proposed_yaml`.
    5. Compute the unified diff via :func:`difflib.unified_diff`.
    6. Build the per-row :class:`DiffEntry` tuple.
    7. Compute reproducibility hashes (DEC-016).
    8. Construct the :class:`DiffReport`.
    9. Dispatch to the selected renderer (DEC-004).
    10. (Optional) Write rendered text to ``output_path``.
    11. (Optional) Write the JSON sidecar via :func:`write_sidecar`.
    12. Emit one INFO log per DEC-015.

    Args:
        model: the manifest :class:`Model` under render.
        candidate: the :class:`CandidateSchema` from the LLM drafter (#5).
        prune_result: the :class:`PruneResult` from the prune layer (#6).
        grading_report: optional :class:`GradingReport` from the grader (#7).
        existing_schema: optional raw YAML text of the current
            ``schema.yml`` for ``model``. ``None`` produces a unified
            diff against ``/dev/null``.
        config: optional :class:`DiffConfig`. ``None`` resolves to
            defaults from DEC-010.
        output_path: optional path for the rendered text. ``None``
            means the orchestrator does not write to disk; the caller
            is expected to receive the rendered text via the
            :class:`DiffReport` (or to call the renderer themselves).
        sidecar_path: optional override for the sidecar JSON path.
            ``None`` means no sidecar is written. (Note: the diff
            sidecar default is opt-in, unlike the grade/prune
            audits — the diff layer does not always need a durable
            on-disk artefact.)
        project_dir: optional project-root override used to resolve
            symlink-hardened path canonicalisation for ``output_path``
            and ``sidecar_path``. ``None`` resolves to
            :func:`pathlib.Path.cwd`.

    Returns:
        A fully-populated :class:`DiffReport` carrying the rendered
        artefacts and the per-row table. The renderer's text output is
        captured on the report only via the JSON shape; ANSI / Markdown
        output is written to ``output_path`` when supplied or returned
        to the caller indirectly via ``report.unified_diff`` +
        ``report.entries``.

    Raises:
        DiffCandidateModelMismatchError: ``candidate.name`` does not
            match ``model.name``. DEC-002.
        DiffPruneResultModelMismatchError: ``prune_result.model_unique_id``
            does not match ``model.unique_id``. DEC-002.
        DiffGradingReportModelMismatchError: ``grading_report.model_unique_id``
            (when provided) does not match ``model.unique_id``. DEC-002.
        DiffInputTooLargeError: ``existing_schema`` exceeds the byte cap
            applied BEFORE any ``yaml.safe_load`` call. DEC-006.
        DiffSidecarRecordTooLargeError: the sidecar payload exceeds the
            10 MB cap. DEC-009.
        DiffSidecarWriteError: ``output_path`` or ``sidecar_path``
            failed symlink/containment validation, or any underlying
            I/O failure on the write path.
    """
    started_at = time.monotonic()

    # 1. Resolve every optional argument.
    resolved_config: DiffConfig = config if config is not None else DiffConfig()
    resolved_project_dir = project_dir if project_dir is not None else Path.cwd()

    # 2. Boundary checks (DEC-002) — fail BEFORE any other work.
    if candidate.name != model.name:
        raise DiffCandidateModelMismatchError(candidate.name, model.name)
    if prune_result.model_unique_id != model.unique_id:
        raise DiffPruneResultModelMismatchError(prune_result.model_unique_id, model.unique_id)
    if grading_report is not None and grading_report.model_unique_id != model.unique_id:
        raise DiffGradingReportModelMismatchError(grading_report.model_unique_id, model.unique_id)

    # 3. existing_schema size cap (DEC-006) + soft-warn (DEC-014).
    if existing_schema is not None:
        encoded_size = len(existing_schema.encode("utf-8"))
        if encoded_size > resolved_config.existing_schema_size_limit_bytes:
            raise DiffInputTooLargeError(
                size=encoded_size,
                limit=resolved_config.existing_schema_size_limit_bytes,
            )
        if encoded_size > resolved_config.existing_schema_warn_at_bytes:
            _LOGGER.warning(
                "large existing schema.yml: %s",
                json.dumps(
                    {
                        "bytes": encoded_size,
                        "model_unique_id": model.unique_id,
                        "warn_at": resolved_config.existing_schema_warn_at_bytes,
                    }
                ),
            )
        # Parse-validate (discard the value); the diff is computed against
        # the raw text. ``yaml.safe_load`` is safe against arbitrary code
        # execution; the byte cap above defends against pathological
        # nesting/anchor expansion. A parse failure here is a programmer
        # error in the caller — surface it to them rather than silently
        # diffing an invalid YAML against the proposed.
        yaml.safe_load(existing_schema)

    # 4. Build canonical proposed YAML.
    proposed_yaml = emit_proposed_yaml(candidate, prune_result)

    # 5. Compute unified diff.
    existing_text = existing_schema if existing_schema is not None else ""
    proposed_lines = proposed_yaml.splitlines(keepends=True)
    existing_lines = existing_text.splitlines(keepends=True) if existing_text else []
    fromfile = f"a/models/{model.name}.yml" if existing_schema is not None else "/dev/null"
    tofile = f"b/models/{model.name}.yml"
    unified_diff = "".join(
        difflib.unified_diff(
            existing_lines,
            proposed_lines,
            fromfile=fromfile,
            tofile=tofile,
            n=resolved_config.context_lines,
        )
    )

    # 6. Build per-row entries.
    entries = _build_entries(candidate, prune_result, grading_report)

    # 7. Reproducibility hashes (DEC-016).
    candidate_hash = _hash_pydantic(candidate)
    prune_result_hash = _hash_pydantic(prune_result)
    grading_report_hash = _hash_pydantic(grading_report) if grading_report is not None else None

    # 8. Aggregate counts and construct the report.
    kept_count = sum(1 for e in entries if e.tier == "kept")
    dropped_count = sum(1 for e in entries if e.tier == "dropped")
    flagged_count = sum(1 for e in entries if e.tier == "flagged")
    has_existing_schema = existing_schema is not None
    duration_seconds = time.monotonic() - started_at
    run_id = uuid.uuid4().hex

    report = DiffReport(
        signalforge_version=_sf.__version__,
        model_unique_id=model.unique_id,
        run_id=run_id,
        duration_seconds=duration_seconds,
        proposed_yaml=proposed_yaml,
        existing_yaml=existing_schema,
        unified_diff=unified_diff,
        entries=entries,
        kept_count=kept_count,
        dropped_count=dropped_count,
        flagged_count=flagged_count,
        has_existing_schema=has_existing_schema,
        candidate_hash=candidate_hash,
        prune_result_hash=prune_result_hash,
        grading_report_hash=grading_report_hash,
    )

    # 9. Dispatch to renderer.
    renderer = _build_renderer(resolved_config, project_dir=resolved_project_dir)
    rendered_text = renderer.render(report)

    # 10. Optional output_path write (symlink-hardened at the orchestrator).
    if output_path is not None:
        # Ensure project_dir exists for canonicalise_path's strict-resolve.
        resolved_project_dir.mkdir(parents=True, exist_ok=True)
        try:
            canonical_output_path = canonicalise_path(output_path, resolved_project_dir)
        except _PathContainmentError as exc:
            raise DiffSidecarWriteError(
                f"Diff output path {output_path!r} failed symlink/containment validation.",
                cause=exc,
            ) from exc
        try:
            _write_rendered_text(rendered_text, output_path=canonical_output_path)
        except (DiffSidecarRecordTooLargeError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            raise DiffSidecarWriteError(
                f"Failed to durably persist rendered diff text to {output_path!r}.",
                cause=exc,
            ) from exc

    # 11. Optional sidecar write (symlink-hardened at the orchestrator).
    if sidecar_path is not None:
        resolved_project_dir.mkdir(parents=True, exist_ok=True)
        try:
            canonical_sidecar_path = canonicalise_path(sidecar_path, resolved_project_dir)
        except _PathContainmentError as exc:
            raise DiffSidecarWriteError(
                f"Diff sidecar path {sidecar_path!r} failed symlink/containment validation.",
                cause=exc,
            ) from exc
        # The writer has its own canonicalise as defence-in-depth, but
        # the load-bearing gate is the orchestrator's. Pass the
        # already-canonicalised path through.
        try:
            write_sidecar(
                report,
                sidecar_path=canonical_sidecar_path,
                project_dir=resolved_project_dir,
            )
        except (DiffSidecarRecordTooLargeError, DiffSidecarWriteError):
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            raise DiffSidecarWriteError(
                "Failed to durably persist the diff sidecar JSON.",
                cause=exc,
            ) from exc

    # 12. One INFO log per invocation. Lazy-format JSON per DEC-015.
    _LOGGER.info(
        "rendered diff: %s",
        json.dumps(
            {
                "run_id": run_id,
                "model_unique_id": model.unique_id,
                "render_kind": resolved_config.render_kind,
                "kept": kept_count,
                "dropped": dropped_count,
                "flagged": flagged_count,
                "has_existing_schema": has_existing_schema,
                "duration_seconds": duration_seconds,
                "candidate_hash": candidate_hash,
                "prune_result_hash": prune_result_hash,
                "grading_report_hash": grading_report_hash,
            }
        ),
    )

    return report


# Suppress pyright's "unused import" warnings for symbols that exist
# purely as part of the type-checked surface — they're documented in
# the docstrings and consumed by the test layer.
_ = (CandidateColumn, CandidateTest, DiffEntry)


__all__ = ("render_diff",)
