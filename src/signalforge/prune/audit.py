"""Fail-closed JSONL prune-audit writer (US-008).

Mirrors :mod:`signalforge.safety.audit` (DEC-011 of safety-layer.md) and
:mod:`signalforge.draft.audit` (DEC-006/008/013 of llm-drafter.md) at the
prune-decision boundary: opens with ``O_APPEND | O_CREAT | 0o600``, writes
one JSONL line per :class:`PruneEvent`, calls :func:`os.fsync`, closes.
This is the *third* instance of the convention; reach for it (not ad-hoc
``Path.write_text`` calls) any time a new pipeline stage needs a durable,
fail-closed JSONL receipt.

Three load-bearing properties, all inherited verbatim from the safety and
draft layers:

* **Atomic concurrent appends.** ``os.open`` with ``O_APPEND`` + a single
  ``os.write`` call. POSIX guarantees ``write(2)`` is atomic up to
  ``PIPE_BUF`` (typically 4 KiB on Linux); the module-level
  :data:`_PRUNE_AUDIT_RECORD_LIMIT_BYTES` enforces a 4000-byte cap with a
  96-byte margin so concurrent writers cannot interleave partial records.
* **Fail-closed on every error.** ``OSError`` / ``PermissionError`` /
  encoding failures all propagate. Oversize records propagate as
  :class:`PruneAuditRecordTooLargeError`. The caller (the engine in US-009)
  wraps any non-typed propagation as :class:`PruneAuditWriteError`. **Don't**
  add try/except inside :func:`_write_prune_event` — the propagation IS the
  contract; an unaudited prune decision is, by definition, a kept/dropped
  artefact without a receipt, exactly the failure mode the audit exists to
  prevent.
* **Single construction seam (DEC-018).** :class:`PruneEvent` is constructed
  only inside this module — :func:`_build_prune_event` is the helper the
  engine calls. The AST audit-completeness scan in
  :file:`tests/test_audit_completeness.py` rejects ``PruneEvent(...)`` calls
  anywhere else.

The :func:`_compute_config_hash` helper produces a 16-hex-char ``blake2b``
digest (``digest_size=8``) of the canonicalised
:mod:`signalforge.prune.config` block, matching the convention
:class:`signalforge.safety.models.AuditEvent` uses for ``policy_hash``
(DEC-005). Reviewers can verify all records in a run came from the same
prune config by checking the field across the JSONL. Issue #55 normalised
the hash family across every audit/sidecar writer (``blake2b-8`` over
canonical JSON) so a reviewer correlating ``safety.jsonl`` /
``llm_responses.jsonl`` / ``prune.jsonl`` / ``grade.jsonl`` / ``diff.json``
reads one recipe.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from datetime import UTC, date, datetime
from hashlib import blake2b
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, field_serializer

from signalforge import __version__ as _SIGNALFORGE_VERSION
from signalforge._common.timestamp import iso8601_z
from signalforge.draft.models import CandidateTest, CandidateTestCustomSQL
from signalforge.prune.errors import PruneAuditRecordTooLargeError
from signalforge.prune.models import DropReason, PruneDecision, Scope
from signalforge.prune.stats import AnomalyTestStats

_LOGGER = logging.getLogger(__name__)

# POSIX guarantees ``write(2)`` is atomic only up to ``PIPE_BUF`` bytes
# (typically 4096 on Linux). The 96-byte margin leaves room for trailing
# newline plus any line-buffering / kernel overhead so the module's atomic-
# concurrent-append contract holds even at the size cap. Identical to
# ``signalforge.safety.audit._AUDIT_RECORD_LIMIT_BYTES`` and
# ``signalforge.draft.audit._RESPONSE_AUDIT_RECORD_LIMIT_BYTES`` by design
# (DEC-016).
_PRUNE_AUDIT_RECORD_LIMIT_BYTES: Final[int] = 4000

# Frozen at the constant in production code. Bump when the JSONL schema
# evolves; v0.2 readers gate on this. Mirrors safety.AuditEvent.audit_schema_version
# and draft.LLMResponseEvent.audit_schema_version. Issue #55 bumped 1 → 2 when
# ``config_hash`` migrated from ``SHA-256[:16]`` to ``blake2b(digest_size=8)``
# so the audit corpus reads one hash recipe across every writer. Issue #171
# bumped 2 → 3 when ``as_of: date | None`` (the time-bound evaluation date
# threaded through for the ``row_count_anomaly_by_period`` variant) and
# ``stats: AnomalyTestStats | None`` (per-decision numerical state from the
# anomaly-stats query) landed on :class:`PruneEvent` per DEC-013 — both
# optional with ``None`` default, so v2 records still round-trip cleanly
# under the :class:`int` (not :class:`typing.Literal`) typing on
# :attr:`PruneEvent.audit_schema_version`. Issue #268 bumped 3 → 4 when
# ``bypassed_to_source: bool`` landed (DEC-011) — defaulted ``False``, so v3
# records still replay cleanly for the same reason.
_PRUNE_AUDIT_SCHEMA_VERSION: Final[int] = 4

# Character budget for each SQL-bearing field on a :class:`PruneEvent`
# (#268 DEC-012(3)). A :class:`PruneEvent` serialises the test's SQL body
# TWICE for a manifest-ingested ``custom_sql`` candidate — once as
# ``test.sql`` (which for a ``from_manifest`` candidate IS the whole dbt
# ``compiled_code``) and once as ``compiled_sql`` (the compiled / rewritten
# form). A real dbt-expectations ``compiled_code`` is routinely ~1–2 KB and
# the ingest reader caps a body at 256 KB, so an untruncated record blows the
# 4000-byte atomic-append cap and raises
# :class:`PruneAuditRecordTooLargeError` — which propagates out of
# ``prune_tests`` and **aborts the whole run mid-batch** with earlier
# decisions already fsync'd.
#
# The 4000-byte cap itself is load-bearing (``PIPE_BUF`` atomic concurrent
# appends — see ``.claude/rules/safety-layer.md``) and must NOT be raised.
# So the SQL bodies are truncated to a bounded, VISIBLY MARKED prefix
# instead. The forensic chain survives intact: ``compiled_sql_hash`` is
# computed by the engine over the FULL compiled SQL and stored separately,
# and the full ingested body remains in the operator's own ``manifest.json``
# / ``schema.yml`` / ``tests/*.sql`` source.
#
# 1000 chars per field keeps a worst-case double-SQL record (~2 KB of SQL +
# the ``why`` / ``stats`` / ``sample_failures`` fields) comfortably inside
# the 4000-byte line cap while still showing an operator enough SQL to
# recognise the statement. The writer's oversize check is UNCHANGED — it
# stays the fail-closed backstop for any other field that runs away
# (e.g. a large ``sample_failures`` capture).
_PRUNE_AUDIT_SQL_PREFIX_CHARS: Final[int] = 1000

#: Appended verbatim to any SQL body the audit truncates, so an operator
#: reading ``prune.jsonl`` is never misled into thinking they hold the full
#: statement. Rendered with ``.format(kept=..., total=...)``.
_PRUNE_AUDIT_SQL_TRUNCATION_MARKER: Final[str] = (
    "\n-- [signalforge: SQL truncated for the audit record — "
    "{kept} of {total} chars shown; the full statement is identified by "
    "compiled_sql_hash]"
)


def _truncate_sql_for_audit(sql: str) -> str:
    """Return ``sql`` bounded to :data:`_PRUNE_AUDIT_SQL_PREFIX_CHARS`.

    Under-cap input is returned unchanged (byte-identical to pre-#268 for
    every drafted built-in, whose compiled SQL is a couple hundred chars).
    Over-cap input is cut to the prefix and given the visible
    :data:`_PRUNE_AUDIT_SQL_TRUNCATION_MARKER` suffix — the truncation must
    never be silent, or an operator would read a syntactically-plausible
    fragment as the whole statement.
    """
    if len(sql) <= _PRUNE_AUDIT_SQL_PREFIX_CHARS:
        return sql
    return sql[:_PRUNE_AUDIT_SQL_PREFIX_CHARS] + _PRUNE_AUDIT_SQL_TRUNCATION_MARKER.format(
        kept=_PRUNE_AUDIT_SQL_PREFIX_CHARS, total=len(sql)
    )


def _truncate_test_for_audit(test: CandidateTest) -> CandidateTest:
    """Bound the SQL body carried on the audit copy of ``test``.

    Only :class:`~signalforge.draft.models.CandidateTestCustomSQL` carries a
    free-form SQL body; every other variant is a bounded args shape and is
    returned untouched. For a manifest-ingested candidate that body IS the
    dbt ``compiled_code`` (#154), so it is the OTHER half of the DEC-012(3)
    double-serialisation.

    Returns a ``model_copy`` — the in-memory :class:`PruneDecision` the
    engine returns to the diff / grade stages keeps the FULL SQL; only the
    audit record is truncated.
    """
    if not isinstance(test, CandidateTestCustomSQL):
        return test
    if len(test.sql) <= _PRUNE_AUDIT_SQL_PREFIX_CHARS:
        return test
    return test.model_copy(update={"sql": _truncate_sql_for_audit(test.sql)})


class PruneEvent(BaseModel):
    """One JSONL audit record per :class:`PruneDecision`.

    DEC-014 — flat shape (mirrors :class:`signalforge.safety.models.AuditEvent`
    and :class:`signalforge.draft.audit.LLMResponseEvent`). The decision's
    fields are flattened in rather than nested under a ``decision:`` key so
    a reviewer can ``jq`` over the JSONL without descending one level per
    field.

    DEC-018 — construction confined to :func:`_build_prune_event` in this
    module. The AST audit-completeness scan rejects direct ``PruneEvent(...)``
    construction anywhere else; the corresponding event would never reach
    disk and the prune decision would be unauditable.

    Read-back-stable (``extra="ignore"`` per ``manifest-readers.md`` DEC-008
    and ``safety-layer.md`` DEC-015) — older readers tolerate forward-compat
    field additions, while a one-off ``extra="forbid"`` drift detector (the
    standard pattern, lands in a future US) catches silent schema expansion
    before a live audit log does.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    audit_schema_version: int = _PRUNE_AUDIT_SCHEMA_VERSION
    """Frozen at :data:`_PRUNE_AUDIT_SCHEMA_VERSION`. Issue #55 bumped 1 → 2
    when ``config_hash`` migrated from ``SHA-256[:16]`` to
    ``blake2b(digest_size=8)``; issue #171 bumped 2 → 3 when ``as_of`` and
    ``stats`` landed (both optional); issue #268 bumped 3 → 4 when
    ``bypassed_to_source`` landed (defaulted ``False``). The field stays
    :class:`int` (not :class:`typing.Literal`) so older ``prune.jsonl``
    records with ``audit_schema_version: 1``, ``2``, or ``3`` still
    round-trip cleanly — audit replay across versions is a real
    requirement. Mirrors
    :attr:`signalforge.safety.models.AuditEvent.audit_schema_version`."""
    signalforge_version: str
    record_id: str
    timestamp: datetime
    config_hash: str
    model_unique_id: str
    test: CandidateTest
    test_anchor: str
    decision: Literal["kept", "dropped"]
    reason: DropReason
    failures: int
    sampled_rows: int | None
    scope: Scope
    elapsed_ms: int
    compiled_sql_hash: str
    """``blake2b-8`` digest of the **full** compiled SQL, computed by the
    engine BEFORE :func:`_truncate_sql_for_audit` bounds the ``compiled_sql``
    field below. The hash is therefore the forensic anchor that survives
    truncation (#268 DEC-012(3))."""
    compiled_sql: str
    """The compiled failing-rows SQL, truncated to a visibly-marked
    :data:`_PRUNE_AUDIT_SQL_PREFIX_CHARS` prefix when over-cap (#268
    DEC-012(3)). Use :attr:`compiled_sql_hash` to correlate the full
    statement; the in-memory
    :attr:`signalforge.prune.models.PruneDecision.compiled_sql` is
    untruncated."""
    why: str
    sample_failures: tuple[dict[str, Any], ...] | None = None
    bypassed_to_source: bool = False
    """Whether this test was routed PAST the sample to the source table
    (issue #268, DEC-011).

    :attr:`scope` is copied verbatim from ``prune.scope``, so a test that
    bypassed the sample is still recorded as ``scope="sample"`` (and its
    ``why`` may read "on 0 sample rows"). Without this field a genuinely
    **sampled** test and a **bypassed** one are indistinguishable in the
    audit — against Architectural Commitment #5 ("explainable diffs").

    ``True`` for the metadata-aggregate variants under a sample scope
    (``row_count_between`` / ``unique_combination`` /
    ``row_count_anomaly_by_period`` — which have silently carried this lie
    since #169) and for a manifest-ingested ``custom_sql`` whose body could
    not be safely rewritten onto the sample relation. ``False`` under
    ``scope="full"`` (there is no sample to bypass), for row-level tests
    that ran against the sample, and for decisions taken before any routing
    happened (prune disabled, budget exhausted, materialisation failed)."""
    as_of: date | None = None
    """Evaluation date for time-bound prune decisions (issue #171, DEC-006
    + DEC-013). Set for the ``row_count_anomaly_by_period`` variant — the
    first SignalForge primitive whose decision is inherently time-bound —
    and ``None`` for every other test variant. Reproducibility for anomaly
    variants is restored at the ``(model, as_of)`` granularity via this
    audit field; same input + same ``as_of`` = same decision. Serialised as
    ``YYYY-MM-DD`` ISO 8601 string via :meth:`_serialize_as_of`. NOT in
    the ``config_hash`` input set — ``config_hash`` answers "did config
    change," not "did time pass.\""""
    stats: AnomalyTestStats | None = None
    """Per-decision numerical state from the anomaly-stats query (issue
    #171, DEC-006 + DEC-013). Populated only for the
    ``row_count_anomaly_by_period`` variant; ``None`` for every other test
    type. The discriminated-union serialisation (the ``method`` field
    discriminates over ``mad`` / ``zscore`` / ``percentile`` / ``min_max``,
    DEC-005) is handled natively by Pydantic v2 — emits the discriminator
    field as part of the dict on ``model_dump`` / ``model_dump_json``."""

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return iso8601_z(value)

    @field_serializer("as_of")
    def _serialize_as_of(self, value: date | None) -> str | None:
        """Render ``as_of`` as ``YYYY-MM-DD`` ISO 8601 string.

        DEC-013 of #171: the canonical-timestamp helper
        :func:`signalforge._common.timestamp.iso8601_z` is deliberately
        :class:`datetime`-only per safety-layer.md issue #56 — :class:`date`
        has no time-of-day component and the ``...Z`` suffix shape does
        not apply. Pydantic v2's native :class:`date` JSON serialisation
        already emits ``YYYY-MM-DD``, but the explicit serializer documents
        the contract and keeps :class:`PruneEvent` rendering identically
        to :class:`signalforge.prune.models.PruneDecision`.
        """
        return value.isoformat() if value is not None else None


def _build_prune_event(
    *,
    decision: PruneDecision,
    model_unique_id: str,
    config_hash: str,
) -> PruneEvent:
    """Construct a :class:`PruneEvent` from one :class:`PruneDecision`.

    DEC-018 — the single construction seam. The AST audit-completeness scan
    in :file:`tests/test_audit_completeness.py` rejects ``PruneEvent(...)``
    calls anywhere outside this module; the engine (US-009) calls this
    helper rather than constructing the event itself, so the audit-write
    seam stays the only code path that produces a record.

    DEC-014 — flattens the decision's fields into the event rather than
    nesting under a ``decision:`` key.

    Stamps ``signalforge_version`` from :data:`signalforge.__version__`,
    generates a fresh ``uuid4`` ``record_id``, and stores a tz-aware UTC
    :class:`datetime`. The model's ``field_serializer`` renders it as
    ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (issue #56 — one canonical shape
    across every audit writer).

    DEC-012(3) of #268 — both SQL-bearing surfaces (``compiled_sql`` AND the
    ``sql`` body on a ``custom_sql`` ``test``) are bounded here via
    :func:`_truncate_sql_for_audit` / :func:`_truncate_test_for_audit`. A
    real dbt-expectations ``compiled_code`` is serialised TWICE on this
    record and would otherwise blow the 4000-byte atomic-append cap,
    aborting the whole prune run mid-batch. ``compiled_sql_hash`` is taken
    from the decision (computed over the FULL SQL) so the forensic chain
    survives truncation; the in-memory :class:`PruneDecision` is unchanged.
    """
    return PruneEvent(
        signalforge_version=_SIGNALFORGE_VERSION,
        record_id=uuid.uuid4().hex,
        timestamp=datetime.now(UTC),
        config_hash=config_hash,
        model_unique_id=model_unique_id,
        test=_truncate_test_for_audit(decision.test),
        test_anchor=decision.test_anchor,
        decision=decision.decision,
        reason=decision.reason,
        failures=decision.failures,
        sampled_rows=decision.sampled_rows,
        scope=decision.scope,
        elapsed_ms=decision.elapsed_ms,
        compiled_sql_hash=decision.compiled_sql_hash,
        compiled_sql=_truncate_sql_for_audit(decision.compiled_sql),
        why=decision.why,
        sample_failures=decision.sample_failures,
        bypassed_to_source=decision.bypassed_to_source,
        as_of=decision.as_of,
        stats=decision.stats,
    )


def _compute_config_hash(config_json: str) -> str:
    """Return a 16-hex-char ``blake2b`` digest of the canonicalised config.

    Matches the convention :class:`signalforge.safety.models.AuditEvent`
    uses for ``policy_hash`` (DEC-005): a reviewer can verify all records
    in a run came from the same prune config by checking this field across
    the JSONL. Issue #55 migrated from ``SHA-256[:16]`` to
    ``blake2b(digest_size=8)`` so every reproducibility hash in the audit /
    sidecar corpus reads one recipe — the v0.1 ``SHA-256[:16]`` form
    survives only in pre-v2 ``prune.jsonl`` records that consumers must
    gate on ``audit_schema_version >= 2`` to skip.

    Canonicalisation (``sort_keys=True``, no whitespace) is performed
    inside the helper so callers can't accidentally misuse it — Pydantic's
    ``model_dump_json`` does NOT contractually guarantee sorted keys
    across point releases, and a caller passing the bare dump would
    silently drift between runs. Mirrors
    :func:`signalforge.safety.policy._compute_policy_hash` verbatim
    (PR #84 review fix).
    """
    canonical = json.dumps(json.loads(config_json), sort_keys=True, separators=(",", ":"))
    return blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()


def _write_prune_event(event: PruneEvent, path: Path) -> None:
    """Append one JSONL record to ``path``. Fail-closed.

    Mirrors :func:`signalforge.safety.audit.write` and
    :func:`signalforge.draft.audit.write_response_event` semantics exactly:
    serialise → size-check (BEFORE any file open) → ``os.open(O_APPEND |
    O_CREAT | O_WRONLY, 0o600)`` → single ``os.write`` → ``os.fsync`` →
    close. Catches NO exceptions internally; the ``try / finally`` around
    ``os.close`` only guarantees the descriptor is released, it does NOT
    swallow ``write`` / ``fsync`` failures.

    Args:
        event: the :class:`PruneEvent` to persist.
        path: target audit log path. Caller is responsible for ensuring the
            parent directory exists (the engine in US-009 handles
            ``mkdir`` separately so it can wrap the failure mode
            appropriately).

    Raises:
        PruneAuditRecordTooLargeError: the serialised line exceeds
            :data:`_PRUNE_AUDIT_RECORD_LIMIT_BYTES`. Raised BEFORE any file
            is opened — an oversize record leaves no on-disk artefact.
        OSError: any underlying I/O failure (``PermissionError``,
            ``FileNotFoundError``, etc.) propagates raw. The caller (the
            engine in US-009) wraps these as
            :class:`signalforge.prune.errors.PruneAuditWriteError`.
    """
    payload = event.model_dump(mode="json")
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    encoded = line.encode("utf-8")

    # Size check BEFORE any file open so an oversize record leaves no
    # on-disk artefact. Mirrors safety.audit and draft.audit.
    if len(encoded) > _PRUNE_AUDIT_RECORD_LIMIT_BYTES:
        raise PruneAuditRecordTooLargeError(
            size=len(encoded),
            limit=_PRUNE_AUDIT_RECORD_LIMIT_BYTES,
        )

    # ``O_APPEND`` gives atomic concurrent appends; ``O_CREAT`` handles the
    # first call; ``0o600`` keeps the file owner-only. No try/except — any
    # ``OSError`` from ``os.write`` / ``os.fsync`` propagates so the caller
    # drops the partial decision. The ``try / finally`` only guarantees the
    # descriptor is released; it does NOT silence the syscall failures.
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        # ``os.write`` may return fewer bytes than requested (EINTR on a
        # signal-interrupted call, or short writes on certain filesystems
        # / kernels). Loop until the full payload lands; raise on a
        # zero-byte return (disk full / unrecoverable I/O failure).
        # POSIX atomicity for ``O_APPEND`` writes still holds at the
        # ``write(2)`` boundary up to ``PIPE_BUF``; the loop is the
        # documented short-write recovery, not a contract violation.
        written = 0
        while written < len(encoded):
            n = os.write(fd, encoded[written:])
            if n == 0:
                raise OSError("os.write returned 0 — disk full or other I/O failure")
            written += n
        os.fsync(fd)
    finally:
        # Best-effort close; the write/fsync above already succeeded
        # (or raised), so a close failure here would only mask the real
        # outcome. Mirrors safety.audit and draft.audit.
        with contextlib.suppress(OSError):
            os.close(fd)


# Sorted alphabetically (mirrors the convention enforced by sibling modules).
__all__ = (
    "PruneEvent",
    "_PRUNE_AUDIT_RECORD_LIMIT_BYTES",
    "_PRUNE_AUDIT_SCHEMA_VERSION",
    "_PRUNE_AUDIT_SQL_PREFIX_CHARS",
    "_PRUNE_AUDIT_SQL_TRUNCATION_MARKER",
    "_build_prune_event",
    "_compute_config_hash",
    "_truncate_sql_for_audit",
    "_truncate_test_for_audit",
    "_write_prune_event",
)
