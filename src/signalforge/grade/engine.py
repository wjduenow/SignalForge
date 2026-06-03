"""Grader orchestrator (US-008) — wires every prior story into one entry point.

:func:`grade_artifacts` is the public seam: given a model + drafted
candidate + prune verdict + (optional) rubric + config, it iterates
every ``(criterion, artifact)`` pair, issues one
:func:`signalforge.llm.client.call_llm` call per pair, parses the
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

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

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
from signalforge.grade.cache import (
    CacheRecord,
    compute_cache_key,
    lookup_cache,
    write_cache,
)
from signalforge.grade.config import GradeConfig
from signalforge.grade.errors import (
    GradeAuditRecordTooLargeError,
    GradeAuditWriteError,
    GradeBelowThresholdError,
    GradeCachePathError,
    GradeError,
    GradeLLMError,
    GradeNestedEventLoopError,
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
from signalforge.llm.client import call_llm_async
from signalforge.llm.errors import (
    LLMError,
    LLMProviderAsyncUnsupportedError,
    LLMResponseFormatError,
)
from signalforge.llm.providers import provider_for
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

# Async sibling alias (issue #186, US-009 / DEC-011). Mirrors
# :data:`signalforge.llm.client._async_sleep`. Tests reassign this to a
# fast-forwarding stand-in to drive the budget-cancellation path under
# :func:`asyncio.timeout`. The orchestrator does NOT call ``_async_sleep``
# on the happy path either; the alias is reserved for tests AND for
# possible v0.2 inter-call pacing on the async core.
_async_sleep = asyncio.sleep


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


def _resolve_async_client(client: AnthropicClientProtocol | None) -> object | None:
    """Translate a test-injected sync-shaped client to its async surface.

    The public :func:`grade_artifacts` signature accepts ``client:
    AnthropicClientProtocol | None`` for back-compat — every existing
    test passes a :class:`FakeAnthropicClient` (sync) and the production
    contract pre-#186 only knew sync clients. The async refactor (US-009)
    routes through :func:`signalforge.llm.client.call_llm_async`, which
    expects an **async-shaped client** (its ``messages.create`` must be
    awaitable).

    The fakes from US-007 expose both surfaces on the same instance:
    ``client.messages`` is sync; ``client.aio.messages`` is async; both
    drain the same expectation queue (DEC-012 of #186). Tests therefore
    pass the sync FakeAnthropicClient via ``client=fake`` and expect the
    engine to route to ``fake.aio`` under the hood. Production callers
    pass ``client=None`` and let :func:`call_llm_async` construct via
    ``provider.make_async_client()``.

    The detection is duck-typed on ``client.aio.messages``: a real
    ``anthropic.Anthropic`` does NOT have ``.aio``, but it also never
    flows through here because production callers don't inject a client.
    A future async-shaped client passed directly is detected as
    "already async" (no ``.aio`` namespace) and forwarded as-is.
    """
    if client is None:
        return None
    aio = getattr(client, "aio", None)
    if aio is None:
        # Already an async-shaped client (or a fake whose top-level
        # ``messages`` is already async). Forward as-is.
        return client
    return aio


async def _grade_one_async(
    *,
    artifact_id: str,
    artifact_text: str,
    criterion: Criterion,
    config: GradeConfig,
    rubric_block: str,
    rubric_hash: str,
    template_hash: str,
    crit_hash: str,
    client: object | None,
    run_id: str,
    timestamp: datetime,
    model_unique_id: str,
) -> tuple[GradingResult, GradeEvent]:
    """Issue one ``(artifact, criterion)`` LLM-judge call (async).

    Async sibling of :func:`_grade_one` (issue #186, US-009 / DEC-004).
    The body is byte-equivalent except for ``await call_llm_async(...)``
    instead of ``call_llm(...)``.

    Returns ``(result, event)`` on the happy path.
    ``GradePromptEnvelopeBreachError`` / :class:`LLMError` /
    :class:`GradeOutputError` propagate to the caller —
    :func:`_grade_artifacts_async_core` converts them into degraded
    results.

    The ``client`` parameter is the **already-translated** async client
    surface (see :func:`_resolve_async_client`).
    """
    # 1. Render the per-pair dynamic block. Raises
    #    GradePromptEnvelopeBreachError if the payload contains
    #    `</ARTIFACT>` — defence-in-depth past the whole-run scan.
    dynamic_block = render_dynamic_block(artifact_id, artifact_text, criterion)

    # 2. Issue the LLM call. Wrap LLMError -> GradeLLMError once at
    #    the seam (DEC-015 of #5 mirror: one-level adapter).
    # ``config.model`` is invariantly a concrete string post-construction
    # (#187 US-002: the sentinel ``None`` is resolved to the provider's fast
    # model at config-load; an unknown provider raises before any consumer
    # reads it). Narrow the static ``str | None`` once for this function.
    assert config.model is not None
    try:
        result = await call_llm_async(
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
            provider=config.provider,
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


def _format_degrade_reasoning(exc: BaseException) -> str:
    """Render the ``GradingResult.reasoning`` string for a degraded pair.

    Resolves issue #158: the bare ``f"call failed: {type(exc).__name__}"``
    shape loses the vendor ``finish_reason`` value when a provider's
    response-shape gate fires (Gemini ``MAX_TOKENS`` vs ``SAFETY`` vs
    ``RECITATION`` all collapse to ``"call failed: GradeLLMError"``).
    When the wrapped cause is :class:`LLMResponseFormatError`, surface its
    bare ``message`` field (which names the vendor field + value per
    :meth:`LLMProvider.unclean_finish_reason_message`) so operators can
    diagnose from the audit JSONL / sidecar alone without re-reading
    stderr.

    For every other cause (auth, rate-limit, parser failure, budget
    exhausted) the existing ``"call failed: <ClassName>"`` shape is
    preserved verbatim — the audit corpus stays diff-clean for the 90%
    case, and only the response-shape branch grows the diagnostic.
    """
    base = f"call failed: {type(exc).__name__}"
    if isinstance(exc, GradeLLMError) and isinstance(exc.cause, LLMResponseFormatError):
        return f"{base}: {exc.cause.message}"
    return base


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
    # ``config.model`` is invariantly concrete post-construction (#187 US-002).
    assert config.model is not None
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
# Async core (issue #186, US-009 — TaskGroup + Semaphore + budget timeout)
# ---------------------------------------------------------------------------


async def _grade_artifacts_async_core(
    *,
    candidate: CandidateSchema,
    resolved_rubric: Rubric,
    resolved_config: GradeConfig,
    resolved_audit_path: Path,
    client: object | None,
    run_id: str,
    rubric_hash: str,
    template_hash: str,
    rubric_block: str,
    crit_hash_by_id: dict[str, str],
    model_unique_id: str,
    prefilled_results: list[GradingResult | None] | None = None,
    cache_dir: Path | None = None,
    artifact_text_hash_by_index: dict[int, str] | None = None,
) -> list[GradingResult]:
    """Concurrent dispatch of every ``(criterion, artifact)`` pair (DEC-002).

    Replaces the sequential ``while iter_index < len(iterator)`` loop
    from the v0.1 ``grade_artifacts``. Orchestrates concurrent dispatch
    via :class:`asyncio.TaskGroup` throttled by an
    :class:`asyncio.Semaphore(max_concurrent_calls)`, bounded by
    :func:`asyncio.timeout(total_budget_seconds)`. Each pair runs as a
    coroutine; per-coroutine ``try/except`` isolates LLM-layer failures
    so one bad pair doesn't abort siblings (DEC-004 retry isolation).

    Cancellation attribution (DEC-008): when the budget trips, the
    enclosing ``asyncio.timeout`` cancels every in-flight task. Each
    coroutine catches :class:`asyncio.CancelledError` and consults the
    orchestrator-scope ``_budget_exceeded`` flag — if set, the pair
    degrades with the locked ``"grade budget exceeded ({N}s) before
    evaluation"`` reasoning (preserves the existing audit-corpus shape
    from v0.1); if unset, the cancellation is re-raised so an unrelated
    parent-task death (``KeyboardInterrupt``, ``SystemExit``) propagates
    intact to the CLI boundary.

    Audit writes happen inside each coroutine via
    ``await loop.run_in_executor(None, _write_event_or_abort, ...)``
    (DEC-017): ``os.fsync`` still serialises at the OS level (no
    wall-time win), but the event loop is free to schedule the next
    coroutine's prompt-build during the fsync.

    Returns the list of per-pair :class:`GradingResult`s in **iteration
    order** (NOT arrival order) — caller-visible result ordering is
    preserved across the refactor by storing each task's result at the
    iterator-index slot. On-disk JSONL ordering becomes arrival-order
    (per DEC-015); this is the deliberate concurrent-dispatch trade.
    """
    # Resolve the async-shaped client surface (US-009 docstring on
    # :func:`_resolve_async_client`). Production callers pass
    # ``client=None``; ``call_llm_async`` builds via
    # ``provider.make_async_client()``. Tests pass a sync
    # FakeAnthropicClient whose ``.aio`` namespace carries the async
    # surface; we translate once here so the per-coroutine forwarding
    # is a no-op.
    async_client = _resolve_async_client(cast(AnthropicClientProtocol | None, client))

    pairs = list(_iterate_artifacts(candidate, resolved_rubric))
    total_pairs = len(pairs)
    # Results land at their iterator-index slot so caller-visible
    # ``report.results`` stays in (criterion-outer, artifact-inner)
    # order even though tasks complete in arrival order on disk.
    #
    # ``prefilled_results`` carries cache-hit slots resolved by the sync
    # prefix (#189 / DEC-013 of #189). Cache hits never enter the
    # ``TaskGroup`` — they were already audit-written by the sync prefix
    # and pre-populated here so the synthesis pass below correctly skips
    # them. ``cache_dir`` is ``None`` when ``config.cache_enabled=False``
    # OR the caller hasn't computed it; either way the post-grade
    # cache-write path is short-circuited.
    if prefilled_results is not None:
        # Shallow copy so we own the list and can mutate freely.
        results_by_index: list[GradingResult | None] = list(prefilled_results)
        if len(results_by_index) != total_pairs:
            # Defensive guard against caller misuse — the prefilled list
            # MUST line up with the iterator order.
            raise GradeError(
                f"prefilled_results length {len(results_by_index)} "
                f"does not match total_pairs {total_pairs}.",
            )
    else:
        results_by_index = [None] * total_pairs

    semaphore = asyncio.Semaphore(resolved_config.max_concurrent_calls)
    # ``_budget_exceeded`` is the orchestrator-scope flag the per-task
    # ``except CancelledError`` arm reads (DEC-008). Closure capture
    # via a list mutable cell so the flag survives across nested
    # coroutines without resorting to ``nonlocal`` from a sibling.
    budget_state: dict[str, bool] = {"exceeded": False}

    # Per-pair category counters; populated as tasks finish.
    # ``completed`` covers both happy-path scores AND LLM-layer per-pair
    # degrades (DEC-015) — anything that ran to completion inside the
    # coroutine, regardless of verdict. ``degraded`` is the synthesis-pass
    # count of pairs that did NOT complete by budget-trip (whether they
    # were in-flight or never started — the asyncio cancellation contract
    # doesn't let us distinguish, per #186 QG Pass 1+3 triangulation).
    counters: dict[str, int] = {
        "completed": 0,  # scored (or LLM-layer-degraded — non-budget)
        "degraded": 0,  # synthesis pass: un-completed at trip time → budget degrade
    }
    # Cache-hit slots resolved by the sync prefix (#189 / DEC-013) count
    # toward the ``completed`` total so the budget-trip WARNING faithfully
    # reflects the run's actual progress (a 100%-cache-hit run that
    # never starts the async core should not WARN with completed=0).
    if prefilled_results is not None:
        counters["completed"] = sum(1 for r in prefilled_results if r is not None)

    total_budget_seconds = resolved_config.total_budget_seconds

    async def _one(index: int, artifact_id: str, artifact_text: str, criterion: Criterion) -> None:
        async with semaphore:
            # Each call gets its own ``timestamp`` so a forensic query
            # can distinguish per-call latency. The sidecar carries
            # ``started_at`` separately.
            crit_hash = crit_hash_by_id[criterion.id]
            per_call_ts = datetime.now(UTC)
            try:
                grading_result, event = await _grade_one_async(
                    artifact_id=artifact_id,
                    artifact_text=artifact_text,
                    criterion=criterion,
                    config=resolved_config,
                    rubric_block=rubric_block,
                    rubric_hash=rubric_hash,
                    template_hash=template_hash,
                    crit_hash=crit_hash,
                    client=async_client,
                    run_id=run_id,
                    timestamp=per_call_ts,
                    model_unique_id=model_unique_id,
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
                    reasoning=_format_degrade_reasoning(exc),
                    config=resolved_config,
                    rubric_hash=rubric_hash,
                    template_hash=template_hash,
                    crit_hash=crit_hash,
                    run_id=run_id,
                    timestamp=per_call_ts,
                    model_unique_id=model_unique_id,
                )
            # NOTE: deliberately NO ``except asyncio.CancelledError`` arm
            # here. Pre-#186-QG the orchestrator's ``budget_state["exceeded"]``
            # flag was set in ``except TimeoutError`` AFTER ``TaskGroup``'s
            # ``__aexit__`` returned — but cancelled children's
            # ``except CancelledError`` runs BEFORE that, so the flag was
            # always ``False`` when checked here. The branch was dead code
            # (QG Pass 1+3 triangulated). The synthesis pass below is now
            # the single source of truth for "this pair was un-completed
            # at trip time" — accurate by construction.

            # In-memory slot assignment FIRST. Under the worst-case
            # cancellation timing (timeout fires while the
            # ``run_in_executor`` audit await is suspended), the executor
            # thread keeps running and durably writes the audit record;
            # without setting the slot first the coroutine raises
            # ``CancelledError`` before line 691 runs, leaving the slot
            # ``None`` — and the synthesis pass then writes a SECOND audit
            # record for the same pair (QG Pass 1 BLOCKER). Setting the
            # slot before the await closes the race: even if the await
            # raises, the in-memory state is consistent with disk.
            results_by_index[index] = grading_result
            counters["completed"] += 1

            # Audit-write per pair (DEC-006 fail-closed; DEC-017
            # executor-wrap so the fsync doesn't block the loop). On
            # GradeAuditRecordTooLargeError / GradeAuditWriteError we
            # propagate; the run aborts.
            #
            # ``asyncio.shield`` ensures the audit-write completes even
            # when the outer task is cancelled — the executor work is
            # sync (cannot be killed mid-fsync); shielding lets it finish
            # before the cancellation propagates back. The slot was set
            # above so the synthesis pass will correctly skip this index.
            #
            # PR #190 review (CodeRabbit) refinement: capture the
            # executor future explicitly so a cancellation arriving
            # mid-shield doesn't silently swallow a downstream
            # ``GradeAuditWriteError`` / ``GradeAuditRecordTooLargeError``.
            # If the outer ``shield`` await raises ``CancelledError``,
            # await the future directly so the writer's exception (if
            # any) propagates to the TaskGroup as the run's abort signal
            # — preserves the fail-closed contract under concurrent
            # cancellation. On the happy path this is a no-op
            # (``audit_future.done()`` is already True when the shield
            # returns).
            loop = asyncio.get_running_loop()
            audit_future = loop.run_in_executor(
                None, _write_event_or_abort_kw, event, resolved_audit_path
            )
            try:
                await asyncio.shield(audit_future)
            except asyncio.CancelledError:
                await audit_future
                raise

            # Cache-write post-audit (#189 / DEC-005 / DEC-007). Fail-soft:
            # write_cache catches OSError / oversize / EEXIST internally
            # and surfaces them as WARNING lines — never raises. Skipped
            # when (a) cache is disabled (cache_dir is None) OR (b) the
            # result degraded (score is None — DEC-007 forbids caching
            # transient LLM failures). ``CacheRecord`` is built from the
            # GradeEvent's reproducibility fields so cache-hit re-runs
            # rehydrate a byte-identical audit corpus (modulo timestamp
            # + cache_hit=True).
            if (
                cache_dir is not None
                and grading_result.score is not None
                and artifact_text_hash_by_index is not None
            ):
                artifact_text_hash = artifact_text_hash_by_index.get(index)
                if artifact_text_hash is not None:
                    cache_record = CacheRecord(
                        artifact_id=artifact_id,
                        criterion_id=criterion.id,
                        score=grading_result.score,
                        passed=grading_result.passed,
                        evidence=grading_result.evidence,
                        reasoning=grading_result.reasoning,
                        criterion_prompt_hash=event.criterion_prompt_hash,
                        artifact_text_hash=artifact_text_hash,
                        provider=resolved_config.provider,
                        model=event.model,
                        prompt_version_template=event.prompt_version_template,
                        response_text_hash=event.response_text_hash,
                        rubric_hash=event.rubric_hash,
                        original_timestamp=per_call_ts,
                    )
                    cache_key = compute_cache_key(
                        criterion_prompt_hash=event.criterion_prompt_hash,
                        artifact_text_hash=artifact_text_hash,
                        provider=resolved_config.provider,
                        model=event.model,
                        prompt_version_template=event.prompt_version_template,
                    )
                    # Fail-soft per DEC-005 — :func:`write_cache`
                    # swallows OSError / oversize / EEXIST internally
                    # and surfaces them as WARNING lines. The defensive
                    # outer guard here is defence-in-depth: a future
                    # refactor that violates the fail-soft contract, OR
                    # a test stub that deliberately raises (see
                    # ``test_grade_engine_cache_write_failure_is_fail_soft``)
                    # MUST NOT abort the live grade. One lazy-format
                    # JSON WARNING; the live run continues.
                    try:
                        write_cache(cache_dir, cache_key, cache_record)
                    except Exception as cache_exc:
                        _LOGGER.warning(
                            "grade cache write failed (engine guard): %s",
                            json.dumps(
                                {
                                    "key": cache_key,
                                    "error_class": type(cache_exc).__name__,
                                }
                            ),
                        )

    try:
        async with asyncio.timeout(total_budget_seconds):
            async with asyncio.TaskGroup() as tg:
                for index, (artifact_id, artifact_text, criterion) in enumerate(pairs):
                    # Skip cache-hit slots already populated by the sync
                    # prefix (#189 / DEC-013). The synthesis pass below
                    # treats a non-None slot as "do not re-grade".
                    if results_by_index[index] is not None:
                        continue
                    tg.create_task(_one(index, artifact_id, artifact_text, criterion))
    except TimeoutError:
        # The timeout fired. Tasks not yet completed at this point
        # received CancelledError and routed through the budget-degrade
        # branch above (which populated ``results_by_index`` and bumped
        # ``counters["cancelled"]``). The TaskGroup's __aexit__ awaited
        # every cancelled task to finish before re-raising, so by the
        # time we land here every slot is populated UNLESS the task
        # body had not yet entered (``async with semaphore`` was still
        # pending) — in that case the slot is still ``None`` and we
        # synthesise a degraded result below.
        budget_state["exceeded"] = True
    except BaseExceptionGroup as group:
        # DEC-007 defence in depth — every per-coroutine grade-typed
        # exception is caught inside the coroutine, so a
        # ``BaseExceptionGroup`` here means a fail-closed audit-write
        # error (``GradeAuditRecordTooLargeError`` /
        # ``GradeAuditWriteError``) — or, more rarely, a non-grade
        # exception slipping through. The audit error MUST abort the
        # run AS THE ORIGINAL ERROR TYPE so the CLI / library callers
        # can pattern-match on the typed exception they already know
        # (mirrors the v0.1 sequential-loop ``raise`` semantics — the
        # only fail-closed boundary in the engine). Unwrap a single
        # representative exception via ``group.exceptions[0]`` (the
        # canonical pattern for re-raising-after-TaskGroup).
        # Multi-exception ExceptionGroups bubble unchanged to US-010's
        # CLI-level renderer.
        if len(group.exceptions) == 1:
            raise group.exceptions[0] from group
        raise

    # Fill any un-started slots with the budget-degrade shape. A slot is
    # ``None`` iff the task was cancelled BEFORE its body executed
    # (i.e. while it was still awaiting the semaphore). Mirrors the v0.1
    # "iter_index past the trip" semantics.
    for index, (artifact_id, _artifact_text, criterion) in enumerate(pairs):
        if results_by_index[index] is not None:
            continue
        crit_hash = crit_hash_by_id[criterion.id]
        per_call_ts = datetime.now(UTC)
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
            model_unique_id=model_unique_id,
        )
        _write_event_or_abort_kw(event, resolved_audit_path)
        results_by_index[index] = grading_result
        counters["degraded"] += 1

    # Emit ONE WARNING on budget trip, field set locked per DEC-018.
    # NB: ``cancelled_count`` was dropped in #186 QG (Pass 1 + Pass 3
    # triangulation) — the asyncio cancellation contract doesn't let
    # the engine distinguish "in-flight at trip" from "un-started at
    # trip", so the field was always 0 in practice and ``degraded_count``
    # carries everything-not-completed. The simpler invariant
    # ``completed_count + degraded_count == total_pairs`` holds.
    if budget_state["exceeded"]:
        _LOGGER.warning(
            "grade budget exceeded: %s",
            json.dumps(
                {
                    "run_id": run_id,
                    "model_unique_id": model_unique_id,
                    "completed_count": counters["completed"],
                    "degraded_count": counters["degraded"],
                    "total_budget_seconds": total_budget_seconds,
                }
            ),
        )

    # By construction every slot is populated; the type-narrowing cast
    # is safe.
    return [cast(GradingResult, r) for r in results_by_index]


def _write_event_or_abort_kw(event: GradeEvent, audit_path: Path) -> None:
    """Positional-arg shim so ``loop.run_in_executor`` can call the
    keyword-only writer.

    :func:`_write_event_or_abort` declares ``audit_path`` keyword-only
    for symmetry with :func:`signalforge.grade.audit.write_grade_event`,
    but :meth:`asyncio.AbstractEventLoop.run_in_executor` cannot bind
    keyword arguments. The shim closes the impedance mismatch in one
    line; no behavioural divergence.
    """
    _write_event_or_abort(event, audit_path=audit_path)


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
            :func:`signalforge.llm.client.call_llm` lazy-construct
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

    # 3a. Async-pre-flight guards (issue #186, DEC-006 / DEC-009). These
    #     fire BEFORE the iterator is materialised and BEFORE any future
    #     ``asyncio.run(...)`` call lands (US-009 wires the async core).
    #     They reject two operator-environment misconfigurations:
    #       1. Nested event loop — calling ``grade_artifacts`` from
    #          inside an already-running asyncio loop would cause the
    #          forthcoming ``asyncio.run`` to raise ``RuntimeError``;
    #          we surface a typed ``GradeNestedEventLoopError`` (CLI
    #          tier 1) with a remediation pointing at the v0.4 follow-up.
    #       2. Sync-only provider — when the configured provider has
    #          ``supports_async=False`` we raise the typed
    #          ``LLMProviderAsyncUnsupportedError`` (CLI tier 3) regardless
    #          of ``max_concurrent_calls``. The async sibling
    #          ``call_llm_async`` is the only LLM seam the engine consumes
    #          post-#186, so even ``max_concurrent_calls=1`` would fail
    #          per-pair with ``GradeLLMError`` and silently degrade every
    #          pair (QG Pass 1 Concern #2: ``cap=1`` is NOT an escape hatch
    #          on a sync-only provider — there's no path forward without
    #          async). Fail loud at entry. Mirrors the project's
    #          ``extra="forbid"`` posture.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise GradeNestedEventLoopError(model_unique_id=model.unique_id)

    provider_strategy = provider_for(resolved_config.provider)
    if not provider_strategy.supports_async:
        raise LLMProviderAsyncUnsupportedError(
            f"Provider {resolved_config.provider!r} does not support async dispatch, "
            f"required by the grade engine since #186. "
            f"Pick an async-capable provider for grading."
        )

    # 4. Run-wide derived values.
    run_id = uuid.uuid4().hex
    started_at = datetime.now(UTC)
    rubric_hash = _canonical_rubric_hash(resolved_rubric)
    template_hash = prompt_version_template(resolved_rubric)
    rubric_block = render_rubric_block(resolved_rubric)
    crit_hash_by_id: dict[str, str] = {c.id: criterion_prompt_hash(c) for c in resolved_rubric}

    # 4a. Resolve the grade cache (#189 / DEC-013, DEC-016). When
    #     ``config.cache_enabled`` is True, canonicalise the cache
    #     directory under ``<project>/.signalforge/grade-cache/`` and
    #     iterate every ``(artifact, criterion)`` pair in the sync
    #     prefix BEFORE the asyncio.TaskGroup. Cache hits resolve
    #     immediately: the GradingResult is reconstructed from the
    #     :class:`CacheRecord`, a ``cache_hit=True`` GradeEvent flows
    #     through the SOLE construction seam
    #     :func:`signalforge.grade.audit._build_grade_event`, and the
    #     audit record lands via the existing fail-closed writer with
    #     zero token counts. Cache hits NEVER acquire the async
    #     semaphore — they're a sync resolution.
    #
    #     Cache misses fall through to the async core which dispatches
    #     a live LLM call and (post-grade) writes a
    #     :class:`CacheRecord` via :func:`write_cache` (fail-soft per
    #     DEC-005).
    cache_dir: Path | None = None
    prefilled_results: list[GradingResult | None] | None = None
    artifact_text_hash_by_index: dict[int, str] | None = None
    if resolved_config.cache_enabled:
        raw_cache_dir = resolved_project_dir / ".signalforge" / "grade-cache"
        try:
            cache_dir = canonicalise_path(raw_cache_dir, resolved_project_dir)
        except PathContainmentError as exc:
            raise GradeCachePathError(
                f"Grade cache path {raw_cache_dir!r} failed symlink/containment validation.",
            ) from exc

        # Build the pairs list once (re-used inside the async core via
        # ``_iterate_artifacts``). Computing the hash map here keeps the
        # sync-prefix lookup AND the post-grade write paths aligned on a
        # single source of truth.
        pairs = list(_iterate_artifacts(candidate, resolved_rubric))
        artifact_text_hash_by_index = {}
        for index, (_aid, atext, _crit) in enumerate(pairs):
            artifact_text_hash_by_index[index] = hashlib.blake2b(
                atext.encode("utf-8"), digest_size=8
            ).hexdigest()

        prefilled_results = cast(list[GradingResult | None], [None] * len(pairs))
        # ``config.model`` is invariantly concrete post-construction
        # (#187 US-002).
        assert resolved_config.model is not None
        for index, (artifact_id, _atext, criterion) in enumerate(pairs):
            artifact_text_hash = artifact_text_hash_by_index[index]
            key = compute_cache_key(
                criterion_prompt_hash=crit_hash_by_id[criterion.id],
                artifact_text_hash=artifact_text_hash,
                provider=resolved_config.provider,
                model=resolved_config.model,
                prompt_version_template=template_hash,
            )
            record = lookup_cache(cache_dir, key)
            if record is None:
                continue
            # ``lookup_cache`` rejects ``score=None`` records via the
            # :class:`CacheRecord` model validator (DEC-007), so any
            # value here is a non-None finite float in [0.0, 1.0].
            per_call_ts = datetime.now(UTC)
            grading_result = GradingResult(
                artifact_id=artifact_id,
                criterion_id=criterion.id,
                score=record.score,
                passed=record.passed,
                evidence=record.evidence,
                reasoning=record.reasoning,
            )
            event = _build_grade_event(
                run_id=run_id,
                timestamp=per_call_ts,
                model_unique_id=model.unique_id,
                artifact_id=artifact_id,
                criterion_id=criterion.id,
                score=record.score,
                passed=record.passed,
                evidence=record.evidence,
                reasoning=record.reasoning,
                rubric_hash=record.rubric_hash,
                prompt_version_template=record.prompt_version_template,
                criterion_prompt_hash=record.criterion_prompt_hash,
                response_text_hash=record.response_text_hash,
                model=record.model,
                input_tokens=0,
                output_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                cache_hit=True,
            )
            _write_event_or_abort(event, audit_path=resolved_audit_path)
            prefilled_results[index] = grading_result

    # 5. Iterate ``(criterion, artifact)`` pairs via the async core
    #    (issue #186, US-009 / DEC-002 + DEC-004). The async core wraps
    #    a ``TaskGroup`` in ``asyncio.timeout(total_budget_seconds)``
    #    and dispatches up to ``max_concurrent_calls`` coroutines via a
    #    ``Semaphore``. Per-coroutine ``try/except`` handles LLM-layer
    #    failures and budget-cancellation; the public sync entry-point
    #    is preserved by wrapping in ``asyncio.run(...)``. The
    #    nested-event-loop guard (3a above) ensured this ``asyncio.run``
    #    call is safe.
    start_monotonic = time.monotonic()
    results = asyncio.run(
        _grade_artifacts_async_core(
            candidate=candidate,
            resolved_rubric=resolved_rubric,
            resolved_config=resolved_config,
            resolved_audit_path=resolved_audit_path,
            client=client,
            run_id=run_id,
            rubric_hash=rubric_hash,
            template_hash=template_hash,
            rubric_block=rubric_block,
            crit_hash_by_id=crit_hash_by_id,
            model_unique_id=model.unique_id,
            prefilled_results=prefilled_results,
            cache_dir=cache_dir,
            artifact_text_hash_by_index=artifact_text_hash_by_index,
        )
    )

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
