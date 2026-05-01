"""Fail-closed JSONL grade-audit writer (US-007).

Mirrors :mod:`signalforge.safety.audit` (DEC-011 of safety-layer.md),
:mod:`signalforge.draft.audit` (DEC-006/008/013 of llm-drafter.md), and
:mod:`signalforge.prune.audit` (DEC-016 of prune-engine.md) at the
LLM-judge boundary: opens with ``O_APPEND | O_CREAT | 0o600``, writes
one JSONL line per :class:`GradeEvent`, calls :func:`os.fsync`, closes.
This is the **fourth** instance of the convention; reach for it (not
ad-hoc ``Path.write_text`` calls) any time a new pipeline stage needs a
durable, fail-closed JSONL receipt.

Three load-bearing properties, all inherited verbatim from the safety,
draft, and prune layers:

* **Atomic concurrent appends.** ``os.open`` with ``O_APPEND`` + a single
  ``os.write`` (looped on short returns) call. POSIX guarantees
  ``write(2)`` is atomic up to ``PIPE_BUF`` (typically 4 KiB on Linux);
  the module-level :data:`_GRADE_AUDIT_RECORD_LIMIT_BYTES` enforces a
  4000-byte cap with a 96-byte margin so concurrent writers cannot
  interleave partial records.
* **Fail-closed on every error.** ``OSError`` / ``PermissionError`` /
  encoding failures all propagate raw. Oversize records propagate as
  :class:`GradeAuditRecordTooLargeError`. Path-safety failures (symlink
  escape / cycle) are wrapped as :class:`GradeAuditWriteError(cause=...)`
  at writer entry — the orchestrator branches on that one typed error.
  The non-path I/O failures stay raw so the orchestrator's exception
  ladder can wrap them in its own ``GradeAuditWriteError`` after deciding
  the contextual message. **Don't** add try/except inside
  :func:`write_grade_event` around the write/fsync — the propagation IS
  the contract; an unaudited grade decision is, by definition, a
  kept/rejected verdict without a receipt, exactly the failure mode the
  audit exists to prevent.
* **Single construction seam (DEC-018 of #6, sixth AST scan target in
  US-009).** :class:`GradeEvent` is constructed only inside this module —
  :func:`_build_grade_event` is the helper the orchestrator (US-008)
  calls. The AST audit-completeness scan in
  :file:`tests/test_audit_completeness.py` will reject ``GradeEvent(...)``
  calls anywhere else once US-009 lands.

The path-safety gate at writer entry routes ``audit_path`` through
:func:`signalforge.warehouse._path_safety.canonicalise_path`, the
project's standard symlink/containment helper. A symlink at
``<project>/.signalforge/grade.jsonl`` pointing outside the project
tree is rejected — the writer refuses rather than write outside the
project sandbox. Mirrors prune's post-QG fix.
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Final

from signalforge import __version__ as _SIGNALFORGE_VERSION
from signalforge.grade.errors import GradeAuditRecordTooLargeError, GradeAuditWriteError
from signalforge.grade.models import GradeEvent
from signalforge.warehouse._path_safety import canonicalise_path
from signalforge.warehouse.errors import ProfileNotFoundError

_LOGGER = logging.getLogger(__name__)

# POSIX guarantees ``write(2)`` is atomic only up to ``PIPE_BUF`` bytes
# (typically 4096 on Linux). The 96-byte margin leaves room for trailing
# newline plus any line-buffering / kernel overhead so the module's
# atomic-concurrent-append contract holds even at the size cap. Identical
# to ``signalforge.safety.audit._AUDIT_RECORD_LIMIT_BYTES``,
# ``signalforge.draft.audit._RESPONSE_AUDIT_RECORD_LIMIT_BYTES``, and
# ``signalforge.prune.audit._PRUNE_AUDIT_RECORD_LIMIT_BYTES`` by design.
_GRADE_AUDIT_RECORD_LIMIT_BYTES: Final[int] = 4000


def _build_grade_event(
    *,
    run_id: str,
    timestamp: datetime,
    model_unique_id: str,
    artifact_id: str,
    criterion_id: str,
    score: float | None,
    passed: bool,
    evidence: str,
    reasoning: str,
    rubric_hash: str,
    prompt_version_template: str,
    criterion_prompt_hash: str,
    response_text_hash: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> GradeEvent:
    """Construct a :class:`GradeEvent` — the **single** construction seam.

    DEC-018 of #6 / US-009 — every :class:`GradeEvent` in production code
    must come from this factory. The AST audit-completeness scan in
    :file:`tests/test_audit_completeness.py` will reject
    ``GradeEvent(...)`` calls anywhere outside this module once US-009
    lands; the orchestrator (US-008) calls this helper rather than
    constructing the event itself, so the audit-write seam stays the
    only code path that produces a record.

    Stamps :attr:`signalforge.grade.models.GradeEvent.signalforge_version`
    from :data:`signalforge.__version__` so a reviewer can identify which
    code shipped the receipt. ``audit_schema_version`` is frozen at the
    Literal default of ``1`` on the model.
    """
    return GradeEvent(
        signalforge_version=_SIGNALFORGE_VERSION,
        run_id=run_id,
        timestamp=timestamp,
        model_unique_id=model_unique_id,
        artifact_id=artifact_id,
        criterion_id=criterion_id,
        score=score,
        passed=passed,
        evidence=evidence,
        reasoning=reasoning,
        rubric_hash=rubric_hash,
        prompt_version_template=prompt_version_template,
        criterion_prompt_hash=criterion_prompt_hash,
        response_text_hash=response_text_hash,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )


def write_grade_event(event: GradeEvent, *, audit_path: Path) -> None:
    """Append one JSONL record for ``event`` to ``audit_path``. Fail-closed.

    Mirrors :func:`signalforge.safety.audit.write`,
    :func:`signalforge.draft.audit.write_response_event`, and
    :func:`signalforge.prune.audit._write_prune_event` semantics exactly.
    Steps:

    1. Serialise the event via Pydantic's JSON serialiser (``by_alias``
       and ``mode="json"`` so ``datetime`` lands as ISO-8601 and the
       discriminated-union/Literal fields render correctly). Append a
       trailing newline.
    2. UTF-8 encode the line.
    3. **Size check BEFORE any file open**: if the encoded byte length
       exceeds :data:`_GRADE_AUDIT_RECORD_LIMIT_BYTES`, raise
       :class:`GradeAuditRecordTooLargeError`. Leaves no on-disk
       artefact.
    4. **Path canonicalisation BEFORE any file open**: route
       ``audit_path`` through
       :func:`signalforge.warehouse._path_safety.canonicalise_path` with
       ``project_dir=audit_path.parent.parent`` (the audit log lives at
       ``<project>/.signalforge/grade.jsonl`` per DEC-006). A symlink
       escape or cycle is wrapped as :class:`GradeAuditWriteError` —
       this is the only place the writer wraps an exception, because the
       path-safety helper raises :class:`ProfileNotFoundError` (a
       warehouse-layer error name that would be confusing for grade
       callers).
    5. Ensure the parent directory exists (``mkdir(parents=True,
       exist_ok=True)``) BEFORE the ``os.open`` so the orchestrator can
       call us against a fresh ``.signalforge/`` directory.
    6. ``os.open`` with ``O_APPEND | O_CREAT | O_WRONLY`` and mode
       ``0o600``.
    7. Single ``os.write`` (looped on short returns), then ``os.fsync``.
       The ``try / finally`` only guarantees the descriptor is released;
       it does NOT swallow ``write`` / ``fsync`` failures.

    Args:
        event: the :class:`GradeEvent` to persist.
        audit_path: target audit log path. Conventionally
            ``<project>/.signalforge/grade.jsonl`` (DEC-006). Must reside
            inside the project tree (``audit_path.parent.parent``); the
            writer refuses paths that escape via symlink.

    Raises:
        GradeAuditRecordTooLargeError: the serialised line exceeds
            :data:`_GRADE_AUDIT_RECORD_LIMIT_BYTES`. Raised BEFORE any
            file is opened — an oversize record leaves no on-disk
            artefact.
        GradeAuditWriteError: ``audit_path`` escapes the project tree
            (symlink) or contains a symlink cycle. The original
            :class:`signalforge.warehouse.errors.ProfileNotFoundError`
            is preserved as ``__cause__`` / ``cause``.
        OSError: any underlying I/O failure on the open / write / fsync
            (``PermissionError``, ``FileNotFoundError``, etc.) propagates
            raw. The caller (the orchestrator in US-008) wraps these as
            :class:`GradeAuditWriteError` with the run-context-specific
            message; the writer itself stays minimal so the propagation
            IS the contract.
    """
    # Serialise via Pydantic's JSON serialiser so ``datetime`` lands as
    # ISO-8601 and ``Literal`` / ``score: float | None`` shapes render
    # correctly without a hand-rolled ``json.dumps(default=...)`` ladder.
    line = event.model_dump_json(by_alias=True) + "\n"
    encoded = line.encode("utf-8")

    # Size check BEFORE any file open so an oversize record leaves no
    # on-disk artefact. Mirrors safety / draft / prune.
    if len(encoded) > _GRADE_AUDIT_RECORD_LIMIT_BYTES:
        raise GradeAuditRecordTooLargeError(
            size=len(encoded),
            limit=_GRADE_AUDIT_RECORD_LIMIT_BYTES,
        )

    # Path canonicalisation BEFORE any file open. The audit log lives at
    # ``<project>/.signalforge/grade.jsonl`` per DEC-006, so the project
    # dir is ``audit_path.parent.parent``. A symlink escape or cycle in
    # the helper raises ``ProfileNotFoundError`` (the warehouse-layer
    # name); wrap into a ``GradeAuditWriteError`` so callers branch on
    # one grade-layer error class. This is the ONLY exception wrap the
    # writer performs — non-path I/O failures propagate raw.
    audit_path = Path(audit_path)
    project_dir = audit_path.parent.parent
    try:
        canonical_path = canonicalise_path(audit_path, project_dir=project_dir)
    except ProfileNotFoundError as exc:
        raise GradeAuditWriteError(
            (f"Grade audit path {audit_path!r} failed symlink/containment validation."),
            cause=exc,
        ) from exc

    # Ensure the parent directory exists. The orchestrator (US-008) may
    # call us against a fresh ``.signalforge/`` that the user hasn't
    # created. ``mkdir`` with ``exist_ok=True`` is idempotent; a
    # permission failure here propagates raw as ``OSError`` /
    # ``PermissionError`` per the contract.
    canonical_path.parent.mkdir(parents=True, exist_ok=True)

    # ``O_APPEND`` gives atomic concurrent appends; ``O_CREAT`` handles
    # the first call; ``0o600`` keeps the file owner-only. No try/except
    # — any ``OSError`` from ``os.open`` / ``os.write`` / ``os.fsync``
    # propagates so the caller drops the partial decision. The
    # ``try / finally`` only guarantees the descriptor is released; it
    # does NOT silence the syscall failures.
    fd = os.open(str(canonical_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        # ``os.write`` may return fewer bytes than requested (EINTR on a
        # signal-interrupted call, or short writes on certain
        # filesystems / kernels). Loop until the full payload lands;
        # raise on a zero-byte return (disk full / unrecoverable I/O
        # failure). POSIX atomicity for ``O_APPEND`` writes still holds
        # at the ``write(2)`` boundary up to ``PIPE_BUF``; the loop is
        # the documented short-write recovery, not a contract violation.
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
        # outcome. Mirrors safety / draft / prune.
        with contextlib.suppress(OSError):
            os.close(fd)


# Sorted alphabetically (mirrors the convention enforced by sibling modules).
__all__ = (
    "_GRADE_AUDIT_RECORD_LIMIT_BYTES",
    "_build_grade_event",
    "write_grade_event",
)
