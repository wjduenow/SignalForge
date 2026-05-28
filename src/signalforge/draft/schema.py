"""Integration layer for the LLM drafter (US-013).

Wires together the prompt builder (US-010), the LLM seam (US-006), the
parser (US-011), and the response-audit writer (US-012) into the public
draft entry points :func:`draft_from_request` and :func:`draft_schema`.

Design commitments operationalised here:

* **DEC-001** — :class:`DraftOutcome` is the typed value object the CLI
  (#9), prune layer (#6), and grader (#7) consume. Frozen +
  ``extra="ignore"`` so downstream stages can hold an outcome without
  worrying about post-construction mutation.
* **DEC-002** — :func:`draft_schema` is the wrapper that takes a
  :class:`Model`, :class:`WarehouseAdapter`, and :class:`SafetyPolicy`
  and returns a :class:`DraftOutcome`. Threads through the safety
  layer's :func:`build_llm_request` (the only sanctioned constructor of
  an :class:`LLMRequest`).
* **DEC-006** — The response-audit JSONL lives at
  ``<safety.audit_path>.with_name("llm_responses.jsonl")``. Reusing the
  policy's audit-path parent directory keeps both audit streams under one
  privacy boundary.
* **DEC-011** — Fail-closed response audit. Any exception from
  :func:`signalforge.draft.audit.write_response_event` propagates as
  :class:`LLMResponseAuditWriteError`; the partial outcome is dropped on
  the floor. Exception:
  :class:`LLMResponseAuditRecordTooLargeError` is already a typed draft
  error and propagates as-is so downstream pattern-matching can branch
  on it.
* **DEC-013** — Direct :class:`LLMResponseEvent` construction is reserved
  to :mod:`signalforge.draft.audit` (the AST-completeness scan rejects
  any other location). This module calls
  :func:`signalforge.draft.audit._build_response_event` instead.
* **DEC-015** — Lazy-format JSON in every ``_LOGGER`` call (mirroring
  ``.claude/rules/safety-layer.md`` DEC-022). f-string interpolation on
  user-controlled values is a log-injection seam; ``json.dumps`` is the
  defence.
* **DEC-016** — :class:`DraftOutcome` carries the :class:`LLMRequest`
  and :class:`LLMResult` alongside the :class:`CandidateSchema` so a
  reviewer can correlate a graded artefact back to the (prompt, request,
  response) triple that produced it.
* **DEC-020** — Public API exported via :mod:`signalforge.draft.__init__`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

import signalforge as _sf
from signalforge.draft.audit import _build_response_event, write_response_event
from signalforge.draft.config import DraftConfig
from signalforge.draft.errors import (
    LLMResponseAuditRecordTooLargeError,
    LLMResponseAuditWriteError,
)
from signalforge.draft.models import CandidateSchema
from signalforge.draft.parser import _LLMResultMeta, parse_draft_response
from signalforge.draft.prompts import render_prompt
from signalforge.llm import AnthropicClientProtocol
from signalforge.llm.client import call_llm
from signalforge.llm.models import LLMResult
from signalforge.manifest.models import Manifest, Model
from signalforge.safety.models import LLMRequest
from signalforge.safety.policy import SafetyPolicy
from signalforge.safety.request import build_llm_request
from signalforge.warehouse.base import WarehouseAdapter

_LOGGER = logging.getLogger(__name__)


class DraftOutcome(BaseModel):
    """Typed value object returned by :func:`draft_schema` /
    :func:`draft_from_request` (DEC-001 / DEC-016).

    Carries the three artefacts a downstream stage needs to explain a
    graded draft back to its inputs:

    * ``candidate`` — the parsed :class:`CandidateSchema` ready for the
      prune step (#6).
    * ``request`` — the safety-layer :class:`LLMRequest` that was sent
      to the LLM. The audit log ties the request to a durable receipt;
      keeping the typed object here lets the prune layer cross-check
      ``columns_sent`` / ``redactions`` without re-running the safety
      layer.
    * ``result`` — the typed :class:`LLMResult` from the LLM seam, with
      token usage + ``prompt_version`` + ``raw_message`` for forensic
      replay.

    Frozen + ``extra="ignore"`` per ``manifest-readers.md`` —
    downstream stages can hold an outcome without worrying about
    post-construction mutation. ``arbitrary_types_allowed=True`` lets
    :class:`LLMResult` carry the SDK's raw message object through
    untouched.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    candidate: CandidateSchema
    request: LLMRequest
    result: LLMResult


def draft_from_request(
    request: LLMRequest,
    model: Model,
    manifest: Manifest,
    *,
    config: DraftConfig,
    audit_path: Path,
    _client: AnthropicClientProtocol | None = None,
) -> DraftOutcome:
    """Orchestrate one LLM draft call from a pre-built :class:`LLMRequest`.

    Steps (each owned by a separate US):

    1. Render the four-part prompt (US-010 / :func:`render_prompt`).
    2. Issue the LLM call via the seam (US-006 / :func:`call_llm`).
    3. Parse + anchor-validate the response (US-011 /
       :func:`parse_draft_response`). Parse errors propagate as
       :class:`LLMOutputJSONError` /
       :class:`LLMOutputValidationError` /
       :class:`LLMOutputAnchorContractError` BEFORE any audit write —
       a malformed response is recorded by the LLM provider's own logs;
       writing a half-truth to our audit JSONL would muddle the receipt.
    4. Write the response-audit JSONL (US-012). Fail-closed (DEC-011): a
       failed write drops the partial outcome.
    5. Return the typed :class:`DraftOutcome`.

    The ``audit_path`` argument is the *safety-layer* audit path; this
    function derives the response-audit path as
    ``audit_path.with_name("llm_responses.jsonl")`` (DEC-006). Both
    streams share a parent directory so the privacy boundary is uniform.

    Args:
        request: a typed :class:`LLMRequest` from
            :func:`signalforge.safety.request.build_llm_request`.
        model: the manifest :class:`Model` under draft. Threaded through
            the prompt builder + the parser's anchor contract
            (``model.columns`` is the source of truth for valid column
            names).
        manifest: the parent :class:`Manifest`. Used by the prompt
            builder to render direct neighbours into the cached block.
        config: the :class:`DraftConfig` controlling the LLM call (model
            id, max output tokens, cache TTL, retry budgets).
        audit_path: the safety-layer audit path. The response-audit
            JSONL sits next to it.
        _client: optional dependency-injection seam for tests. Production
            callers leave this ``None`` and let
            :func:`signalforge.llm.client.call_llm` lazy-construct
            a real ``anthropic.Anthropic``.

    Returns:
        A :class:`DraftOutcome` carrying the parsed candidate, the
        request, and the result.

    Raises:
        LLMOutputJSONError: the LLM's response was not valid JSON.
        LLMOutputValidationError: the response parsed but did not match
            the :class:`CandidateSchema` shape.
        LLMOutputAnchorContractError: the response cited columns that do
            not exist on ``model``.
        LLMResponseAuditRecordTooLargeError: the audit record exceeded
            the POSIX-atomic-append size cap. Propagates as-is.
        LLMResponseAuditWriteError: any other I/O / encoding failure in
            the audit writer. Wraps the underlying exception on
            ``cause``.
    """
    # 1. Render the prompt. Returns (system, cached, dynamic, prompt_version).
    # Issue #54: thread DraftConfig.exclude_tests through so the system
    # prompt's test catalogue is filtered AND the prompt-version hash
    # rotates per exclusion set (cache-invalidation contract).
    system, cached, dynamic, prompt_version = render_prompt(
        model, request, manifest, exclude_tests=config.exclude_tests
    )

    # 2. Issue the LLM call through the seam.
    result = call_llm(
        system=system,
        cached_block=cached,
        dynamic_block=dynamic,
        model=config.model,
        max_tokens=config.max_output_tokens,
        cache_ttl=config.cache_ttl,
        prompt_version=prompt_version,
        max_retries_429=config.max_retries_429,
        max_retries_5xx=config.max_retries_5xx,
        max_retries_conn=config.max_retries_conn,
        provider=config.provider,
        client=_client,
    )

    # 3. Parse + anchor-validate. Parse errors propagate BEFORE any
    #    audit write — a malformed response leaves no receipt.
    meta = _LLMResultMeta(
        prompt_version=prompt_version,
        model=config.model,
        cache_hit=(result.cache_read_input_tokens > 0),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    model_columns: frozenset[str] = frozenset(c.name for c in model.columns_list)
    candidate = parse_draft_response(
        result.response_text,
        model_columns,
        llm_result_meta=meta,
        exclude_tests=frozenset(config.exclude_tests),
    )

    # 4. Write the response-audit record. Fail-closed (DEC-011):
    #    LLMResponseAuditRecordTooLargeError propagates as-is (typed);
    #    every other exception wraps as LLMResponseAuditWriteError.
    response_audit_path = audit_path.with_name("llm_responses.jsonl")
    # `sent_sql_hash` records the SQL we sent — hash `Model.raw_code`,
    # NOT `dynamic` (which also contains the <MODEL_SQL> envelope and
    # the mode-specific data section). Hashing the full dynamic block
    # would drift on prompt-template changes and break correlation
    # with `Model.raw_code` for incident-response queries.
    event = _build_response_event(
        timestamp=datetime.now(UTC),
        model_unique_id=model.unique_id,
        candidate=candidate,
        raw_text=result.response_text,
        sent_sql=model.raw_code or "",
        result=result,
        prompt_version=prompt_version,
        signalforge_version=_sf.__version__,
    )
    try:
        write_response_event(event, audit_path=response_audit_path)
    except LLMResponseAuditRecordTooLargeError:
        # Already a typed draft error — propagate so downstream
        # pattern-matching can branch on it.
        raise
    except (KeyboardInterrupt, SystemExit):
        # Signal-shaped exits must propagate untouched — wrapping them
        # would silently demote a Ctrl-C into an audit error.
        raise
    except BaseException as exc:
        raise LLMResponseAuditWriteError(
            "Failed to durably persist the LLM response-audit record.",
            cause=exc,
        ) from exc

    # 5. Emit a one-line DEBUG record after a successful write. Lazy-format
    #    JSON per DEC-015 — never f-string interpolate user-controlled
    #    strings into a logger call.
    _LOGGER.debug(
        "draft prompt version: %s",
        json.dumps(
            {
                "prompt_version": prompt_version,
                "model": config.model,
                "model_unique_id": model.unique_id,
            }
        ),
    )

    return DraftOutcome(candidate=candidate, request=request, result=result)


def draft_schema(
    model: Model,
    adapter: WarehouseAdapter,
    policy: SafetyPolicy,
    manifest: Manifest,
    *,
    config: DraftConfig,
    _client: AnthropicClientProtocol | None = None,
) -> DraftOutcome:
    """End-to-end draft entry point: safety-layer + prompt + LLM + parse + audit.

    Wraps :func:`draft_from_request` with the safety-layer
    :func:`build_llm_request` call, which is the only sanctioned
    constructor of an :class:`LLMRequest` (DEC-009 of the safety layer).
    Errors from :func:`build_llm_request`
    (e.g. :class:`signalforge.safety.errors.AuditWriteError`) propagate
    UNCHANGED — they're safety-layer errors, not draft-layer errors, and
    re-wrapping would muddle the failure surface.

    Args:
        model: the manifest :class:`Model` under draft.
        adapter: the :class:`WarehouseAdapter` (BigQuery for v0.1). Used
            by the safety layer for aggregate / sample modes; unused for
            schema-only mode (DEC-012(c) of the safety layer).
        policy: the :class:`SafetyPolicy` controlling sampling-mode
            redaction + audit path.
        manifest: the parent :class:`Manifest` for the model under draft.
        config: the :class:`DraftConfig` controlling the LLM call.
        _client: optional DI seam for tests.

    Returns:
        A :class:`DraftOutcome` from :func:`draft_from_request`.
    """
    request = build_llm_request(model, adapter, policy)
    return draft_from_request(
        request,
        model,
        manifest,
        config=config,
        audit_path=policy.audit_path,
        _client=_client,
    )


__all__ = ("DraftOutcome", "draft_from_request", "draft_schema")
