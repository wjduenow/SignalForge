"""Grader orchestrator (US-008) — wires every prior story into one entry point.

:func:`grade_artifacts` is the public seam: given a model + drafted
candidate + prune verdict + (optional) rubric + config, it iterates
every ``(criterion, artifact)`` pair, issues one
:func:`signalforge.llm.client.call_anthropic` call per pair, parses the
response, writes a fail-closed JSONL audit record, and at end-of-run
writes a sidecar JSON :class:`GradingReport`.

Design commitments operationalised here (``plans/super/7-quality-grader.md``):

* **DEC-001** — Public API surface; re-exported by
  :mod:`signalforge.grade.__init__`.
* **DEC-004** — One LLM call per ``(artifact, criterion)`` pair. Cached
  block is the rubric block (constant across the run); dynamic block is
  the per-pair ``<ARTIFACT>...</ARTIFACT>`` envelope.
* **DEC-006 / DEC-012** — Two fail-closed audit seams, mirroring the
  safety/draft/prune precedent: per-call JSONL via
  :func:`signalforge.grade.audit.write_grade_event` + end-of-run sidecar
  via :func:`signalforge.grade.audit.write_grading_report`.
* **DEC-009** — Canonical ``artifact_id`` dotted-path formatter
  :func:`_artifact_id_for`. Six shapes the formatter emits; identical
  shape vocabulary the resolver in
  :mod:`signalforge.grade.prompts.extract_artifact_text` consumes.
* **DEC-013** — Whole-run pre-flight envelope-breach scan: every
  artifact payload that would land inside ``<ARTIFACT>...</ARTIFACT>``
  is checked for the literal close tag BEFORE any LLM call. Mirrors the
  drafter's ``<MODEL_SQL>`` envelope (DEC-007 of #5).
* **DEC-014** — :class:`GradeEvent` carries ``rubric_hash`` +
  ``prompt_version_template`` + ``criterion_prompt_hash`` so a reviewer
  can correlate JSONL records back to (rubric, system prompt,
  per-criterion fragment).
* **DEC-015** — Graceful degrade for retry-exhausted /
  parser-failed / budget-exceeded pairs: ``GradingResult(score=None,
  passed=False, reasoning="...")`` plus a matching
  :class:`GradeEvent` with ``score=None`` and an empty
  ``response_text_hash``. The whole run only aborts when the audit
  itself fails to durably persist (DEC-006 fail-closed).
* **DEC-018** — Sequential criterion-outer / artifact-inner iteration
  for human-debug clarity: every JSONL group runs the same criterion
  consecutively, easier for grep/jq.
* **DEC-020** — ``run_id`` is a single :func:`uuid.uuid4` hex
  generated at orchestrator entry and stamped on every JSONL record AND
  the sidecar so JSONL → sidecar correlation never depends on
  timestamp ranges.
* **DEC-021** — Test-side ``expect_grade_responses`` helper lives in
  :file:`tests/grade/_fake.py`; the orchestrator only knows about the
  public :class:`signalforge.llm.AnthropicClientProtocol` contract.
* **DEC-022** — ``project_dir`` defaults to :func:`pathlib.Path.cwd`
  at orchestrator entry; ``audit_path`` and ``sidecar_path`` resolve
  relative to it.
* **DEC-023** — Module-level :data:`_sleep` alias mirrors
  :data:`signalforge.llm.client._sleep` and
  :data:`signalforge.prune.engine._sleep` (DEC-019 of #6, DEC-004 of
  #5). Tests reassign for deterministic budget exercise; the
  orchestrator does NOT call ``_sleep`` on the happy path.
* **DEC-027** — Single INFO log per invocation at the end, lazy-format
  ``json.dumps`` (mirroring ``llm-drafter.md`` DEC-011 / ``safety-layer.md``
  DEC-022 / ``prune-engine.md`` DEC-017). The grep-gate enforcement
  lands in US-009.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import signalforge as _sf
from signalforge._common.artifact_id import (
    artifact_id_for as _artifact_id_for,
)
from signalforge._common.artifact_id import (
    compute_args_hashes as _test_args_hashes,
)

# Re-exported for back-compat: `signalforge.grade.prompts` and
# `tests/diff/test_artifact_id.py` import the name from here. Issue #42
# hoisted the implementation to `signalforge._common.artifact_id`.
from signalforge._common.artifact_id import (  # noqa: F401
    model_test_args_hash as _model_test_args_hash,
)
from signalforge._common.path_safety import PathContainmentError, canonicalise_path
from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
)
from signalforge.grade.audit import (
    _build_grade_event,
    write_grade_event,
    write_grading_report,
)
from signalforge.grade.config import GradeConfig
from signalforge.grade.errors import (
    GradeAuditRecordTooLargeError,
    GradeAuditWriteError,
    GradeBelowThresholdError,
    GradeError,
    GradeLLMError,
    GradeOutputError,
    GradePromptEnvelopeBreachError,
)
from signalforge.grade.models import GradeEvent, GradingReport, GradingResult
from signalforge.grade.parser import parse_grade_response
from signalforge.grade.prompts import (
    _SYSTEM_PROMPT,
    criterion_prompt_hash,
    prompt_version_template,
    render_dynamic_block,
    render_rubric_block,
)
from signalforge.grade.rubric import (
    DEFAULT_RUBRIC,
    Criterion,
    Rubric,
    _canonical_rubric_hash,
    validate_rubric,
)
from signalforge.llm import AnthropicClientProtocol
from signalforge.llm.client import call_llm
from signalforge.llm.errors import LLMError
from signalforge.manifest.models import Model
from signalforge.prune.models import PruneResult

_LOGGER = logging.getLogger(__name__)

# Module-level alias for deterministic test override (DEC-023). Mirrors
# :data:`signalforge.llm.client._sleep` and
# :data:`signalforge.prune.engine._sleep`. The orchestrator does NOT
# call ``_sleep`` on the happy path; the alias is reserved for tests
# that monkey-patch a slow stand-in to exercise budget paths, and for a
# possible v0.2 inter-call pacing knob.
_sleep = time.sleep


# ---------------------------------------------------------------------------
# Canonical artifact_id formatter (DEC-009)
#
# Implementations live in :mod:`signalforge._common.artifact_id` after
# issue #42 hoisted the byte-equal copies under grade + diff into a
# single source of truth. The names ``_artifact_id_for`` /
# ``_model_test_args_hash`` / ``_test_args_hashes`` remain re-exported
# here so existing import paths keep working AND the cross-stage parity
# test asserts function-identity equality with
# :mod:`signalforge.diff._artifact_id`.
# ---------------------------------------------------------------------------


def _stable_artifact_pairs(
    candidate: CandidateSchema,
) -> list[tuple[str, str]]:
    """Return ``[(artifact_id, artifact_text), ...]`` in canonical order.

    Order (DEC-018, deterministic-by-construction):

    1. Each ``column.<col>.description`` in ``candidate.columns`` order.
    2. Each ``column.<col>.rationale`` in the same order.
    3. ``model.description``.
    4. ``model.rationale``.
    5. Each ``test.column.<col>.<type>`` in column order; tests within
       a column in declared order.
    6. Each ``test.model.<type>[.<args_hash>]`` in
       ``candidate.tests`` order.

    Empty rationales (``rationale is None`` or ``""``) are still
    iterated — the LLM judge handles "missing rationale" as part of
    the rubric. Only completely-empty descriptions skip the
    pre-flight envelope-breach scan because there is no payload to
    inspect.
    """
    pairs: list[tuple[str, str]] = []
    columns: tuple[CandidateColumn, ...] = candidate.columns

    for column in columns:
        artifact_id = _artifact_id_for(scope="column", column_name=column.name, field="description")
        pairs.append((artifact_id, column.description))
    for column in columns:
        artifact_id = _artifact_id_for(scope="column", column_name=column.name, field="rationale")
        pairs.append((artifact_id, column.rationale or ""))

    pairs.append((_artifact_id_for(scope="model", field="description"), candidate.description))
    pairs.append((_artifact_id_for(scope="model", field="rationale"), candidate.rationale or ""))

    args_map = _test_args_hashes(candidate)
    for column in columns:
        for test in column.tests:
            artifact_id = _artifact_id_for(
                scope="column",
                column_name=column.name,
                test=test,
                args_hash=args_map[id(test)],
            )
            pairs.append((artifact_id, test.rationale or ""))

    for test in candidate.tests:
        artifact_id = _artifact_id_for(scope="model", test=test, args_hash=args_map[id(test)])
        pairs.append((artifact_id, test.rationale or ""))

    return pairs


def _iterate_artifacts(
    candidate: CandidateSchema, rubric: Rubric
) -> Iterator[tuple[str, str, Criterion]]:
    """Yield ``(artifact_id, artifact_text, criterion)`` triples in
    canonical order (DEC-018).

    Outer loop: criteria (rubric tuple order). Inner loop: artifacts
    (the deterministic order computed by :func:`_stable_artifact_pairs`).
    Criterion-outer chosen so the per-criterion JSONL group is
    contiguous, which makes ``grep -F '"criterion_id":"clarity"'``
    return one cohesive block — the human-debug-clarity argument
    documented in DEC-018.

    The cached rubric block is invariant across iteration order, so
    Anthropic prompt-cache hits regardless of which axis is outer.
    """
    artifact_pairs = _stable_artifact_pairs(candidate)
    for criterion in rubric:
        for artifact_id, artifact_text in artifact_pairs:
            yield artifact_id, artifact_text, criterion


# ---------------------------------------------------------------------------
# Whole-run pre-flight envelope-breach scan (DEC-013)
# ---------------------------------------------------------------------------


def _scan_envelope_breach(candidate: CandidateSchema) -> None:
    """Raise :class:`GradePromptEnvelopeBreachError` if any artifact
    payload contains the literal ``</ARTIFACT>`` close tag.

    Whole-run pre-flight: scan every payload that would later be
    embedded in a ``<ARTIFACT>...</ARTIFACT>`` envelope. Failing fast
    here mirrors the drafter's ``<MODEL_SQL>`` precedent (DEC-007 of
    #5) — refuse to render rather than ship a degraded envelope on any
    of the dozens of judge calls one ``grade_artifacts`` run issues.

    The check is identical to :func:`render_dynamic_block`'s per-call
    guard; doing it whole-run-up-front means the operator sees one
    typed error pointing at the offending artifact rather than
    discovering the breach mid-iteration after several JSONL records
    have already landed.
    """
    for artifact_id, artifact_text in _stable_artifact_pairs(candidate):
        if "</ARTIFACT>" in artifact_text:
            raise GradePromptEnvelopeBreachError(artifact_id)


# ---------------------------------------------------------------------------
# Single-pair execution (DEC-004)
# ---------------------------------------------------------------------------


def _hash_response_text(response_text: str) -> str:
    """16-hex ``blake2b-8`` of the raw LLM response text.

    Mirrors :class:`signalforge.draft.audit.LLMResponseEvent.response_text_hash`
    so the cross-stage hash domain is consistent — a reviewer querying
    "what response text produced criterion X for artifact Y on date Z"
    can compare bytes verbatim across draft/grade JSONLs.
    """
    return hashlib.blake2b(response_text.encode("utf-8"), digest_size=8).hexdigest()


def _grade_one(
    *,
    artifact_id: str,
    artifact_text: str,
    criterion: Criterion,
    config: GradeConfig,
    rubric_block: str,
    rubric_hash: str,
    template_hash: str,
    crit_hash: str,
    client: AnthropicClientProtocol | None,
    run_id: str,
    timestamp: datetime,
    model_unique_id: str,
) -> tuple[GradingResult, GradeEvent]:
    """Issue one ``(artifact, criterion)`` LLM-judge call.

    Returns ``(result, event)`` on the happy path. On
    :class:`GradePromptEnvelopeBreachError` propagation lands here ONLY
    if the whole-run pre-flight scan was bypassed (e.g., a future
    caller used :func:`_grade_one` directly without
    :func:`grade_artifacts`); the orchestrator's normal flow has
    already checked. ``GradePromptEnvelopeBreachError`` /
    :class:`LLMError` / :class:`GradeOutputError` propagate to the
    caller — :func:`grade_artifacts` converts them into degraded
    results.
    """
    # 1. Render the per-pair dynamic block. Raises
    #    GradePromptEnvelopeBreachError if the payload contains
    #    `</ARTIFACT>` — defence-in-depth past the whole-run scan.
    dynamic_block = render_dynamic_block(artifact_id, artifact_text, criterion)

    # 2. Issue the LLM call. Wrap LLMError -> GradeLLMError once at
    #    the seam (DEC-015 of #5 mirror: one-level adapter).
    try:
        result = call_llm(
            system=_SYSTEM_PROMPT,
            cached_block=rubric_block,
            dynamic_block=dynamic_block,
            model=config.model,
            max_tokens=config.max_output_tokens,
            cache_ttl=config.cache_ttl,
            prompt_version=template_hash,
            max_retries_429=config.max_retries_429,
            max_retries_5xx=config.max_retries_5xx,
            max_retries_conn=config.max_retries_conn,
            client=client,
        )
    except LLMError as exc:
        raise GradeLLMError(
            f"LLM-judge call failed for artifact_id={artifact_id!r}, "
            f"criterion_id={criterion.id!r}.",
            cause=exc,
        ) from exc

    # 3. Parse + anchor-validate. Bad-response failures land BEFORE
    #    any audit write — the GradeAuditWriteError path is reserved
    #    for I/O failures, not for "the LLM returned junk".
    grading_result = parse_grade_response(
        result.response_text,
        artifact_id=artifact_id,
        criterion=criterion,
    )

    # 4. Build the audit event. Single construction seam (US-009 AST
    #    scan): every GradeEvent flows through _build_grade_event in
    #    signalforge.grade.audit.
    event = _build_grade_event(
        run_id=run_id,
        timestamp=timestamp,
        model_unique_id=model_unique_id,
        artifact_id=artifact_id,
        criterion_id=criterion.id,
        score=grading_result.score,
        passed=grading_result.passed,
        evidence=grading_result.evidence,
        reasoning=grading_result.reasoning,
        rubric_hash=rubric_hash,
        prompt_version_template=template_hash,
        criterion_prompt_hash=crit_hash,
        response_text_hash=_hash_response_text(result.response_text),
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_creation_input_tokens=result.cache_creation_input_tokens,
        cache_read_input_tokens=result.cache_read_input_tokens,
    )
    return grading_result, event


def _build_degraded(
    *,
    artifact_id: str,
    criterion: Criterion,
    reasoning: str,
    config: GradeConfig,
    rubric_hash: str,
    template_hash: str,
    crit_hash: str,
    run_id: str,
    timestamp: datetime,
    model_unique_id: str,
) -> tuple[GradingResult, GradeEvent]:
    """Construct the ``score=None`` degraded pair (DEC-015).

    Used for retry-exhausted / parser-failed / budget-exceeded pairs.
    The ``response_text_hash`` is the empty string (no response text
    to hash); ``input_tokens`` / ``output_tokens`` are 0. Both halves
    of the pair (the result returned to the caller AND the JSONL
    receipt) carry the same ``score=None`` / ``passed=False`` shape so
    a downstream replay round-trips cleanly.
    """
    grading_result = GradingResult(
        artifact_id=artifact_id,
        criterion_id=criterion.id,
        score=None,
        passed=False,
        evidence="",
        reasoning=reasoning,
    )
    event = _build_grade_event(
        run_id=run_id,
        timestamp=timestamp,
        model_unique_id=model_unique_id,
        artifact_id=artifact_id,
        criterion_id=criterion.id,
        score=None,
        passed=False,
        evidence="",
        reasoning=reasoning,
        rubric_hash=rubric_hash,
        prompt_version_template=template_hash,
        criterion_prompt_hash=crit_hash,
        response_text_hash="",
        model=config.model,
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return grading_result, event


# ---------------------------------------------------------------------------
# Audit-write seam (mirrors prune.engine._write_audit_or_abort)
# ---------------------------------------------------------------------------


def _write_event_or_abort(event: GradeEvent, *, audit_path: Path) -> None:
    """Write one :class:`GradeEvent`; raise :class:`GradeAuditWriteError`
    on I/O failure.

    Mirrors :func:`signalforge.prune.engine._write_audit_or_abort`:

    * :class:`GradeAuditRecordTooLargeError` propagates as-is.
    * :class:`KeyboardInterrupt` / :class:`SystemExit` propagate
      untouched (signal-shaped exits must not be demoted).
    * Every other ``BaseException`` wraps as
      :class:`GradeAuditWriteError(cause=...)`.
    """
    try:
        write_grade_event(event, audit_path=audit_path)
    except GradeAuditRecordTooLargeError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        raise GradeAuditWriteError(
            "Failed to durably persist a grade-decision audit record.",
            cause=exc,
        ) from exc


# ---------------------------------------------------------------------------
# Public API (DEC-001)
# ---------------------------------------------------------------------------


def grade_artifacts(
    model: Model,
    candidate: CandidateSchema,
    prune_result: PruneResult,
    *,
    rubric: Rubric | None = None,
    config: GradeConfig | None = None,
    audit_path: Path | None = None,
    sidecar_path: Path | None = None,
    client: AnthropicClientProtocol | None = None,
    project_dir: Path | None = None,
) -> GradingReport:
    """Grade every drafted artifact for ``model`` against ``rubric``.

    End-to-end orchestrator that wires every prior story
    (errors US-001, models US-002, rubric US-003, config US-004,
    prompts US-005, parser US-006, audit US-007) into one public
    seam. Mirrors :func:`signalforge.draft.draft_schema` and
    :func:`signalforge.prune.prune_tests` in calling-convention shape
    (DEC-022): keyword-only optionals, model-front-paired, sequential
    execution.

    Pipeline:

    1. Resolve config (``None`` → :class:`GradeConfig` defaults),
       rubric (explicit arg → ``config.rubric`` →
       :data:`DEFAULT_RUBRIC`), ``project_dir``, ``audit_path``,
       ``sidecar_path``.
    2. Validate the resolved rubric (no-empty / no-duplicate-id).
    3. Whole-run pre-flight envelope-breach scan (DEC-013): every
       artifact payload checked for ``</ARTIFACT>`` BEFORE any LLM
       call. Loud fail at this gate is the prompt-injection defence.
    4. Generate ``run_id`` (uuid4 hex, DEC-020). Compute the run's
       ``rubric_hash`` (DEC-014), ``prompt_version_template``,
       ``rubric_block`` (cached prefix for every call).
    5. Iterate every ``(criterion, artifact)`` pair. At the top of
       each loop iteration, check the wall-clock against
       ``config.total_budget_seconds``; once exceeded, every
       remaining pair lands as a degraded
       ``GradingResult(score=None, ...)`` plus matching
       :class:`GradeEvent` (DEC-015). Per-pair LLM failures
       (:class:`LLMError` retry-exhausted, :class:`GradeOutputError`
       parser failure) also degrade gracefully — only audit-write
       failures abort the run (DEC-006 fail-closed).
    6. Build the aggregate :class:`GradingReport`. Write the sidecar
       JSON via :func:`signalforge.grade.audit.write_grading_report`.
    7. Emit one INFO log with the run aggregate. Return the report.

    Args:
        model: the manifest :class:`Model` under grade.
        candidate: the :class:`CandidateSchema` from the LLM drafter
            (#5).
        prune_result: the :class:`PruneResult` from the prune layer
            (#6). Reserved for v0.2 — the v0.1 ``no-redundant``
            criterion does not yet consume the dropped-test set
            beyond what's already on ``candidate``; the parameter
            takes its place at the orchestrator entry to lock the
            calling convention.
        rubric: optional rubric override. Resolution order: explicit
            arg → ``config.rubric`` → :data:`DEFAULT_RUBRIC`.
        config: optional :class:`GradeConfig`. ``None`` resolves to
            defaults from DEC-023..DEC-027.
        audit_path: optional override for the JSONL audit path.
            ``None`` resolves to
            ``<project_dir>/.signalforge/grade.jsonl`` (DEC-006).
        sidecar_path: optional override for the sidecar JSON path.
            ``None`` resolves to
            ``<project_dir>/.signalforge/grade.json`` (DEC-012).
        client: optional dependency-injection seam for tests. Production
            callers leave this ``None`` and let
            :func:`signalforge.llm.client.call_anthropic` lazy-construct
            a real ``anthropic.Anthropic``.
        project_dir: optional project-root override used to resolve the
            default ``audit_path`` / ``sidecar_path``. ``None`` resolves
            to :func:`pathlib.Path.cwd`.

    Returns:
        A :class:`GradingReport` carrying every per-pair
        :class:`GradingResult`, the aggregate computed fields
        (``pass_rate``, ``mean_score``, ``passed``,
        ``aggregate_complete``), the run's ``rubric_hash`` /
        ``run_id`` / ``timestamp`` / ``duration_seconds``, and the
        signalforge version.

    Raises:
        GradePromptEnvelopeBreachError: any artifact payload contains
            the literal ``</ARTIFACT>`` close tag. Raised BEFORE any
            LLM call.
        GradeRubricError: the resolved rubric is empty or contains
            duplicate ids.
        GradeAuditRecordTooLargeError: a per-call audit record OR the
            sidecar exceeded the size cap. Aborts the run.
        GradeAuditWriteError: any other I/O / encoding failure in
            either audit writer. Aborts the run; wraps the underlying
            exception on ``cause``.
        GradeBelowThresholdError: raised when
            ``config.fail_on_below_threshold=True`` AND the aggregate
            ``GradingReport.passed`` is False (i.e. ``pass_rate``
            below ``min_pass_rate`` or ``mean_score`` below
            ``min_mean_score``). The exception carries ``pass_rate``,
            ``mean_score``, ``min_pass_rate``, ``min_mean_score``, and
            ``aggregate_complete``. Raised AFTER ``write_grading_report``
            returns so the sidecar JSON lands on disk first — operators
            need that durable hand-off to diagnose threshold failures
            (DEC-021 of the CLI ticket; graduated from the v0.2
            reservation in #7).
    """
    # 1. Resolve every optional argument.
    resolved_config: GradeConfig = config if config is not None else GradeConfig()
    if rubric is not None:
        resolved_rubric: Rubric = rubric
    elif resolved_config.rubric is not None:
        resolved_rubric = resolved_config.rubric
    else:
        resolved_rubric = DEFAULT_RUBRIC

    resolved_project_dir = project_dir if project_dir is not None else Path.cwd()
    raw_audit_path = (
        audit_path
        if audit_path is not None
        else resolved_project_dir / ".signalforge" / "grade.jsonl"
    )
    raw_sidecar_path = (
        sidecar_path
        if sidecar_path is not None
        else resolved_project_dir / ".signalforge" / "grade.json"
    )

    # Ensure project_dir exists for canonicalise_path's strict-resolve.
    resolved_project_dir.mkdir(parents=True, exist_ok=True)

    # Symlink-harden audit/sidecar paths against the orchestrator-resolved
    # project root (DEC-006/012; mirrors prune.engine). The writers also
    # canonicalise as defence-in-depth, but they derive project_dir from
    # the path itself — sufficient for the default <project>/.signalforge/
    # path but unsafe for caller-supplied paths that escape the tree. The
    # ENGINE is the place that knows the true project root; canonicalising
    # here is the load-bearing gate. Failures wrap as GradeAuditWriteError
    # before any I/O, so the writer never sees an escape-attempt path.
    try:
        resolved_audit_path = canonicalise_path(raw_audit_path, resolved_project_dir)
    except PathContainmentError as exc:
        raise GradeAuditWriteError(
            f"Grade audit path {raw_audit_path!r} failed symlink/containment validation.",
            cause=exc,
        ) from exc
    try:
        resolved_sidecar_path = canonicalise_path(raw_sidecar_path, resolved_project_dir)
    except PathContainmentError as exc:
        raise GradeAuditWriteError(
            f"Grade sidecar path {raw_sidecar_path!r} failed symlink/containment validation.",
            cause=exc,
        ) from exc

    # Bug 3 (QG pass 1): assert prune_result corresponds to the same model
    # to prevent stale-result-passed-to-grader. The parameter is reserved
    # for v0.2 (no-redundant criterion will consume dropped_decisions),
    # but the model-unique-id linkage is the boundary contract today.
    if prune_result.model_unique_id != model.unique_id:
        raise GradeError(
            f"prune_result.model_unique_id ({prune_result.model_unique_id!r}) does not "
            f"match model.unique_id ({model.unique_id!r}); refusing to grade with a "
            f"prune result that belongs to a different model.",
            remediation=(
                "Pass the PruneResult produced by prune_tests(model, ...) for the "
                "SAME model you are grading."
            ),
        )

    # 2. Validate the resolved rubric (no-empty / no-duplicate-id).
    #    config.rubric was already validated at config load; the
    #    explicit ``rubric=`` kwarg path may not have been, and
    #    DEFAULT_RUBRIC is a programming-error case if it ever fails.
    validate_rubric(resolved_rubric)

    # 3. Whole-run pre-flight envelope-breach scan (DEC-013).
    _scan_envelope_breach(candidate)

    # 4. Run-wide derived values.
    run_id = uuid.uuid4().hex
    started_at = datetime.now(UTC)
    rubric_hash = _canonical_rubric_hash(resolved_rubric)
    template_hash = prompt_version_template(resolved_rubric)
    rubric_block = render_rubric_block(resolved_rubric)
    crit_hash_by_id: dict[str, str] = {c.id: criterion_prompt_hash(c) for c in resolved_rubric}

    # 5. Iterate ``(criterion, artifact)`` pairs.
    start_monotonic = time.monotonic()
    total_budget_seconds = resolved_config.total_budget_seconds

    iterator = list(_iterate_artifacts(candidate, resolved_rubric))
    results: list[GradingResult] = []
    budget_exhausted = False
    iter_index = 0
    while iter_index < len(iterator):
        artifact_id, artifact_text, criterion = iterator[iter_index]

        if not budget_exhausted and (time.monotonic() - start_monotonic) >= total_budget_seconds:
            budget_exhausted = True
            _LOGGER.warning(
                "grade budget exceeded: %s",
                json.dumps(
                    {
                        "run_id": run_id,
                        "model_unique_id": model.unique_id,
                        "evaluated": len(results),
                        "remaining_pairs": len(iterator) - iter_index,
                        "total_budget_seconds": total_budget_seconds,
                    }
                ),
            )

        crit_hash = crit_hash_by_id[criterion.id]
        # Each call gets its own ``timestamp`` so a forensic query can
        # distinguish per-call latency. The sidecar carries
        # ``started_at`` separately.
        per_call_ts = datetime.now(UTC)

        if budget_exhausted:
            grading_result, event = _build_degraded(
                artifact_id=artifact_id,
                criterion=criterion,
                reasoning=(f"grade budget exceeded ({total_budget_seconds}s) before evaluation"),
                config=resolved_config,
                rubric_hash=rubric_hash,
                template_hash=template_hash,
                crit_hash=crit_hash,
                run_id=run_id,
                timestamp=per_call_ts,
                model_unique_id=model.unique_id,
            )
        else:
            try:
                grading_result, event = _grade_one(
                    artifact_id=artifact_id,
                    artifact_text=artifact_text,
                    criterion=criterion,
                    config=resolved_config,
                    rubric_block=rubric_block,
                    rubric_hash=rubric_hash,
                    template_hash=template_hash,
                    crit_hash=crit_hash,
                    client=client,
                    run_id=run_id,
                    timestamp=per_call_ts,
                    model_unique_id=model.unique_id,
                )
            except (
                GradeLLMError,
                GradeOutputError,
                GradePromptEnvelopeBreachError,
            ) as exc:
                # Per-pair degrade — DEC-015. Do NOT let one
                # criterion's failure abort the whole run.
                grading_result, event = _build_degraded(
                    artifact_id=artifact_id,
                    criterion=criterion,
                    reasoning=f"call failed: {type(exc).__name__}",
                    config=resolved_config,
                    rubric_hash=rubric_hash,
                    template_hash=template_hash,
                    crit_hash=crit_hash,
                    run_id=run_id,
                    timestamp=per_call_ts,
                    model_unique_id=model.unique_id,
                )

        # Audit-write per pair (DEC-006 fail-closed). On
        # GradeAuditRecordTooLargeError / GradeAuditWriteError we
        # propagate; the run aborts.
        _write_event_or_abort(event, audit_path=resolved_audit_path)
        results.append(grading_result)
        iter_index += 1

    # 6. Build the aggregate :class:`GradingReport` and write sidecar.
    elapsed_seconds = time.monotonic() - start_monotonic
    report = GradingReport(
        signalforge_version=_sf.__version__,
        run_id=run_id,
        timestamp=started_at,
        duration_seconds=elapsed_seconds,
        model_unique_id=model.unique_id,
        rubric_hash=rubric_hash,
        thresholds=(resolved_config.min_pass_rate, resolved_config.min_mean_score),
        results=tuple(results),
    )

    try:
        write_grading_report(report, sidecar_path=resolved_sidecar_path)
    except GradeAuditRecordTooLargeError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        raise GradeAuditWriteError(
            "Failed to durably persist the grade sidecar JSON.",
            cause=exc,
        ) from exc

    # 7. One INFO log per invocation. Lazy-format JSON per DEC-027.
    _LOGGER.info(
        "grade completed: %s",
        json.dumps(
            {
                "run_id": run_id,
                "model_unique_id": model.unique_id,
                "pass_rate": report.pass_rate,
                "mean_score": report.mean_score,
                "passed": report.passed,
                "aggregate_complete": report.aggregate_complete,
                "duration_seconds": elapsed_seconds,
                "results": len(report.results),
            }
        ),
    )

    # 8. Threshold-fail graduation (#9 US-002 / DEC-021). When the
    # operator opts into hard-fail behaviour AND the aggregate verdict
    # falls below threshold, raise AFTER the sidecar JSON is durably
    # persisted (step 6 above) and AFTER the INFO log fires. Order is
    # load-bearing: the operator gets a complete `grade.json` on disk
    # for diagnosis even on a threshold-fail run; the JSONL audit (step
    # 5) is also complete. Raising before the sidecar would defeat the
    # durable hand-off; pinned by
    # ``test_grade_below_threshold_writes_sidecar_before_raising``.
    if resolved_config.fail_on_below_threshold and not report.passed:
        min_pass_rate, min_mean_score = report.thresholds
        raise GradeBelowThresholdError(
            pass_rate=report.pass_rate,
            mean_score=report.mean_score,
            min_pass_rate=min_pass_rate,
            min_mean_score=min_mean_score,
            aggregate_complete=report.aggregate_complete,
        )

    return report


__all__ = ("grade_artifacts",)
