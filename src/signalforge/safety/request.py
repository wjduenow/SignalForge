"""Single-entry request builder for the PII safety layer (US-010).

:func:`build_llm_request` is the only sanctioned constructor of an
:class:`signalforge.safety.models.LLMRequest`. It classifies every column on
the input :class:`~signalforge.manifest.models.Model`, dispatches to the
warehouse adapter according to the configured :class:`SamplingMode`, writes
exactly one :class:`~signalforge.safety.models.AuditEvent` to the JSONL log,
and returns the request only if the audit write succeeded.

Design commitments operationalised here:

* **DEC-009 — Single entry.** Direct construction of :class:`LLMRequest`
  bypasses the audit log; the AST scan in US-011 enforces this convention
  at lint time. This module is the human-readable companion: every code
  path that produces a request flows through one of three branches below.
* **DEC-010 — Column-name hashing.** Schema-only and aggregate-only modes
  redact column NAMES (not just values) by replacing each redacted column's
  identifier with its blake2b-derived ``col_<hash>`` placeholder. Sample
  mode does the same and additionally rewrites the keys of every sampled
  row so the LLM sees the same hashed identifiers it sees in
  :attr:`LLMRequest.schema`.
* **DEC-011 — Fail-closed audit.** Any exception from
  :func:`signalforge.safety.audit.write` propagates; the partial
  :class:`LLMRequest` is dropped on the floor. Callers never receive a
  request whose audit record didn't durably hit disk.
* **DEC-012(c) — Default mode is zero adapter calls.** Schema-only mode
  must not invoke ``adapter.column_stats`` or ``adapter.sample_rows`` —
  not even by opening the context manager. The companion regression test
  in :mod:`tests.safety.test_default_mode_regression` uses a
  :class:`FakeAdapter` with no expectations queued; any call would raise.
* **DEC-014 — Audit reproducibility.** Every emitted
  :class:`AuditEvent` carries ``signalforge_version``, ``policy_hash``,
  ``audit_schema_version``, and ``policy_flags``.
* **DEC-022 — Transitive immutability.** Sequences on the returned
  :class:`LLMRequest` are :class:`tuple`, never :class:`list`, so the
  payload cannot be mutated between audit-write time and LLM-call time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import signalforge as _sf
from signalforge.manifest.models import Model
from signalforge.safety import audit
from signalforge.safety.aggregate import aggregate_columns
from signalforge.safety.errors import (
    AuditRecordTooLargeError,
    AuditWriteError,
    InvalidSamplingModeError,
)
from signalforge.safety.models import (
    DRAFT_SKIP_REASONS,
    AuditEvent,
    LLMRequest,
    RedactionRecord,
    SamplingMode,
)
from signalforge.safety.policy import (
    DEFAULT_AUDIT_PATH,
    SafetyPolicy,
    _compute_policy_hash,
)
from signalforge.safety.redact import (
    _classify_column,
    redact_column_names,
    redact_rows,
)
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.models import TableRef

_AUDIT_SCHEMA_VERSION: Final[int] = 3
"""Bumped from 1 → 2 by issue #54: the :data:`RedactionReason` literal
gained ``draft_skip_column_meta`` and ``draft_skip_model_meta``, and
columns carrying those reasons are now omitted entirely from the
LLM-facing payload (``columns_sent`` / ``schema`` / ``aggregates`` /
``sampled_rows``). Audit consumers gating on ``audit_schema_version >= 2``
know the new reasons may appear in :attr:`AuditEvent.redactions`.

Bumped from 2 → 3 by issue #55: :func:`_compute_policy_hash` migrated
from ``SHA-256[:16]`` to ``blake2b(digest_size=8)`` so every
reproducibility hash in the audit / sidecar corpus reads one recipe.
Consumers correlating ``policy_hash`` across audit JSONLs must gate on
``audit_schema_version >= 3`` to skip records produced by the
pre-migration writer."""


def build_llm_request(
    model: Model,
    adapter: WarehouseAdapter,
    policy: SafetyPolicy,
) -> LLMRequest:
    """Produce a typed :class:`LLMRequest` and durably audit it (DEC-009).

    Per-mode behaviour:

    * :attr:`SamplingMode.SCHEMA_ONLY` — no adapter calls (DEC-012(c)).
      ``sampled_rows=None``, ``aggregates=None``. ``schema`` carries
      ``(hashed_name, type)`` for redacted columns and ``(real_name, type)``
      for everyone else.
    * :attr:`SamplingMode.AGGREGATE_ONLY` — delegates to
      :func:`signalforge.safety.aggregate.aggregate_columns`.
      ``sampled_rows=None``; ``aggregates`` is a tuple of (hashed-name,
      (redacted columns, value=``None``) or real name (otherwise, value=
      :class:`ColumnStats`).
    * :attr:`SamplingMode.SAMPLE` — calls ``adapter.sample_rows`` inside the
      adapter context, redacts values for redacted columns to
      ``"<REDACTED>"``, and rewrites the keys of every row so the LLM sees
      the same hashed identifiers it sees in :attr:`LLMRequest.schema`.

    Audit semantics (DEC-011 fail-closed): an exception from
    :func:`signalforge.safety.audit.write` propagates; the partial request
    is dropped on the floor.

    Args:
        model: a manifest :class:`Model` whose columns will be classified.
        adapter: a :class:`WarehouseAdapter`. Must be unused for
            schema-only mode; opened-and-closed once for aggregate-only
            and sample modes.
        policy: a :class:`SafetyPolicy` selecting the sampling mode and
            redaction patterns.

    Returns:
        A :class:`LLMRequest` ready to hand to the LLM-drafting layer
        (issue #5).

    Raises:
        AuditWriteError: ``audit.write`` failed for any reason. The partial
            request is not returned.
        AuditRecordTooLargeError: The serialised audit line exceeded the
            POSIX-atomic-append size cap.
        InvalidSamplingModeError: defensive — should be unreachable given
            :class:`SamplingMode`'s closed enum.
    """
    # ---- 1. Classify every column up front ----------------------------------
    # Doing this once gives us the redaction set for downstream branches
    # (schema rewrite, row-key rewrite, value redaction) without re-walking
    # ``policy.redact_patterns`` per branch.
    classifications: dict[str, RedactionRecord | None] = {
        column.name: _classify_column(column, model, policy) for column in model.columns_list
    }
    redactions: tuple[RedactionRecord, ...] = tuple(
        rec for rec in classifications.values() if rec is not None and rec.redacted
    )

    # Issue #54: ``draft_skip_*`` redaction reasons mean "omit this column
    # entirely from the LLM payload" — distinct from the seven PII reasons
    # which mean "send a hashed placeholder". Their RedactionRecord still
    # rides on the audit event (operator-visible record of the omission),
    # but they are filtered out of ``schema`` / ``columns_sent`` /
    # ``aggregates`` / ``sampled_rows`` so the LLM never sees them.
    skipped_columns: frozenset[str] = frozenset(
        rec.column_name for rec in redactions if rec.reason in DRAFT_SKIP_REASONS
    )

    # ``schema`` is what the LLM sees: (display_name, type_str) for every
    # column. Display name is the hashed placeholder for PII-redacted
    # columns; draft-skip columns are dropped entirely.
    raw_schema: tuple[tuple[str, str], ...] = tuple(
        (column.name, column.data_type or "")
        for column in model.columns_list
        if column.name not in skipped_columns
    )
    schema = redact_column_names(raw_schema, redactions)

    # ---- 2. Per-mode dispatch ----------------------------------------------
    sampled_rows: tuple[dict[str, object], ...] | None = None
    aggregates = None

    if policy.mode is SamplingMode.SCHEMA_ONLY:
        # DEC-012(c): zero adapter calls. Even opening the context manager
        # is forbidden — schema-only mode must be a pure transform of the
        # already-loaded manifest.
        pass
    elif policy.mode is SamplingMode.AGGREGATE_ONLY:
        # ``aggregate_columns`` re-runs ``_classify_column`` internally;
        # since ``_classify_column`` is a pure function of ``column``,
        # ``model``, and ``policy.redact_patterns``, the redaction set it
        # produces matches ours up-front. We pass ``redactions`` from our
        # classification as the source of truth on the audit/request side.
        #
        # Issue #54: draft-skip columns are excluded from the aggregate
        # request entirely — we never even query their stats.
        aggregates_dict, _ = aggregate_columns(
            adapter,
            model,
            [c.name for c in model.columns_list if c.name not in skipped_columns],
            policy,
        )
        # Convert to tuple-of-tuples so the LLMRequest.aggregates field is
        # transitively immutable (DEC-022). A bare dict on a frozen Pydantic
        # model is still mutable in its contents, which would let a downstream
        # consumer rewrite values after the audit log has been written.
        aggregates = tuple(aggregates_dict.items())
    elif policy.mode is SamplingMode.SAMPLE:
        table = TableRef.from_model(model)
        with adapter:
            raw_rows = adapter.sample_rows(table, policy.sample_size)
        # Issue #54: drop skip_draft columns from every row before any
        # further processing — they must not surface in the LLM-bound
        # payload even with a redacted value.
        if skipped_columns:
            raw_rows = tuple(
                {k: v for k, v in row.items() if k not in skipped_columns} for row in raw_rows
            )
        # Redact values for PII-redacted columns (keys still on real names).
        redacted_real_names = frozenset(
            rec.column_name for rec in redactions if rec.reason not in DRAFT_SKIP_REASONS
        )
        value_redacted = redact_rows(raw_rows, redacted_real_names)
        # Rewrite each redacted column's key from real -> hashed so the row
        # keys match the identifiers the LLM sees in ``schema`` /
        # ``columns_sent``. Non-redacted columns keep their real names.
        real_to_hashed = {
            rec.column_name: rec.hashed_name
            for rec in redactions
            if rec.reason not in DRAFT_SKIP_REASONS
        }
        sampled_rows = tuple(
            {real_to_hashed.get(k, k): v for k, v in row.items()} for row in value_redacted
        )
    else:
        # Defensive: ``SamplingMode`` is a closed enum, so this branch is
        # unreachable in practice. Surfacing it as a typed error rather
        # than a bare ``RuntimeError`` keeps the safety layer's failure
        # surface uniform.
        raise InvalidSamplingModeError(
            value=policy.mode,
            allowed=tuple(m.value for m in SamplingMode),
        )

    # ---- 3. Assemble columns_sent + audit fields ----------------------------
    columns_sent: tuple[str, ...] = tuple(name for name, _ in schema)
    row_count = len(sampled_rows) if sampled_rows is not None else None

    policy_flags: list[str] = []
    if policy.mode is SamplingMode.SAMPLE:
        policy_flags.append("sample_mode_enabled")
    if len(policy.redact_patterns) == 0:
        policy_flags.append("redaction_disabled")
    if policy.audit_path != DEFAULT_AUDIT_PATH:
        policy_flags.append("audit_path_overridden")

    event = AuditEvent(
        timestamp=datetime.now(UTC),
        model_unique_id=model.unique_id,
        mode=policy.mode,
        columns_sent=columns_sent,
        redactions=redactions,
        row_count=row_count,
        signalforge_version=_sf.__version__,
        policy_hash=_compute_policy_hash(policy),
        audit_schema_version=_AUDIT_SCHEMA_VERSION,
        policy_flags=tuple(policy_flags),
    )

    # ---- 4. Build the request, then audit, then return ----------------------
    # DEC-011 fail-closed: ``audit.write`` runs BEFORE we hand back the
    # request. ``write`` catches NO exceptions internally (mirrors the
    # writer shape in prune/draft/grade/diff); the orchestrator owns the
    # typed wrap. ``AuditRecordTooLargeError`` propagates as-is (already
    # typed); every other exception wraps as ``AuditWriteError``. The
    # partial request never escapes on either path.
    request = LLMRequest(
        model_unique_id=model.unique_id,
        mode=policy.mode,
        columns_sent=columns_sent,
        redactions=redactions,
        sampled_rows=sampled_rows,
        aggregates=aggregates,
        schema=schema,
    )
    try:
        audit.write(event, policy.audit_path)
    except AuditRecordTooLargeError:
        # Already a typed safety error — propagate so downstream
        # pattern-matching can branch on it.
        raise
    except (KeyboardInterrupt, SystemExit):
        # Signal-shaped exits must propagate untouched — wrapping them
        # would silently demote a Ctrl-C into an audit error.
        raise
    except BaseException as exc:
        raise AuditWriteError(path=policy.audit_path, cause=exc) from exc
    return request


__all__ = ["build_llm_request"]
