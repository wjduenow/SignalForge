"""Fail-closed JSONL audit-log writer for the safety layer (US-007).

This module is the safety layer's single observability seam. Every LLM call
the request builder makes lands here as exactly one JSONL line; any I/O
failure aborts the call (DEC-011 fail-closed) so the system never proceeds
without an audit trail.

Three load-bearing properties:

* **Atomic concurrent appends** (DEC-005). The writer uses ``os.open`` with
  ``O_APPEND`` and writes the full record in a single ``os.write`` call. POSIX
  guarantees ``write(2)`` is atomic up to ``PIPE_BUF`` (4 KiB on Linux); the
  module-level :data:`_AUDIT_RECORD_LIMIT_BYTES` enforces a 4000-byte cap with
  a 96-byte margin so concurrent writers cannot interleave partial records.
* **Fail-closed on every error** (DEC-011). ``write`` catches NO exceptions
  internally — serialisation errors, ``mkdir`` failures, ``open`` failures,
  ``write`` / ``fsync`` failures all propagate raw to the caller. The
  orchestrator (:func:`signalforge.safety.request.build_llm_request`) wraps
  non-typed propagations as :class:`AuditWriteError`. Oversize records raise
  :class:`AuditRecordTooLargeError` BEFORE any file is opened, so the
  serialised line never lands on disk. The propagation IS the defence —
  don't add try/except around the writes "to be defensive". This mirrors
  the writer shape in prune / grade / diff / draft.
* **ANSI-safe lazy-format logger** (DEC-022). The summary line is logged via
  ``%s`` lazy-format with ``json.dumps`` of the user-controlled fields. f-string
  interpolation here would let a crafted ``model_unique_id`` containing raw
  ANSI escapes pollute the log viewer; routing through ``json.dumps`` escapes
  control characters as ``\\uXXXX`` and quotes the strings, closing the
  log-injection seam.

The "no logging in stage-0 modules" rule from ``manifest-readers.md`` does NOT
apply here — this module *is* the observability stage. INFO-level logging is
its job, not noise.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from signalforge.safety.models import AuditEvent

_LOGGER: Final = logging.getLogger("signalforge.safety")

# POSIX guarantees ``write(2)`` is atomic only up to ``PIPE_BUF`` bytes
# (typically 4096 on Linux). The 96-byte margin leaves room for trailing
# newline plus any line-buffering / kernel overhead so the module's atomic-
# concurrent-append contract holds even at the size cap.
_AUDIT_RECORD_LIMIT_BYTES: Final[int] = 4000


def write(event: AuditEvent, audit_path: Path) -> None:
    """Append a single JSONL record to ``audit_path``. Fail-closed.

    Mirrors :func:`signalforge.prune.audit._write_prune_event`,
    :func:`signalforge.draft.audit.write_response_event`,
    :func:`signalforge.grade.audit.write_grade_event`, and
    :func:`signalforge.diff._sidecar.write_sidecar` semantics exactly:
    serialise → size-check (BEFORE any file open) → ``mkdir -p`` parent →
    ``os.open(O_APPEND | O_CREAT | O_WRONLY, 0o600)`` → looped ``os.write``
    → ``os.fsync`` → close. Catches NO exceptions internally; the
    ``try / finally`` around ``os.close`` only guarantees the descriptor is
    released, it does NOT swallow ``write`` / ``fsync`` failures.

    Args:
        event: the :class:`~signalforge.safety.models.AuditEvent` to persist.
        audit_path: absolute or project-relative path; the parent directory
            is created with mode ``0o700`` if missing, and the audit file
            itself is created with mode ``0o600`` on first call.

    Raises:
        AuditRecordTooLargeError: the serialised line exceeds the
            POSIX-atomic-append size cap (DEC-022); reduce ``columns_sent``
            or ``redactions`` count. Raised BEFORE any file is opened —
            an oversize record leaves no on-disk artefact.
        OSError: any underlying I/O failure (``PermissionError``,
            ``FileNotFoundError``, etc.) and any JSON-encoding failure
            (raw :class:`TypeError` / :class:`ValueError` from
            :func:`json.dumps`) propagates raw. The caller
            (:func:`signalforge.safety.request.build_llm_request`) wraps
            non-typed propagations as
            :class:`signalforge.safety.errors.AuditWriteError`.
    """
    # Local import keeps ``audit`` importable without forcing the errors module
    # at module-eval time and matches the style used elsewhere in the package.
    from signalforge.safety.errors import AuditRecordTooLargeError

    # Serialise. Any encoding error (e.g. an unserialisable smuggled type)
    # propagates raw; the orchestrator wraps as ``AuditWriteError``.
    payload = event.model_dump(mode="json")
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    encoded = line.encode("utf-8")

    # Size check BEFORE any file open so an oversize record leaves no
    # on-disk artefact. Mirrors prune/draft/grade/diff.
    if len(encoded) > _AUDIT_RECORD_LIMIT_BYTES:
        raise AuditRecordTooLargeError(size=len(encoded), limit=_AUDIT_RECORD_LIMIT_BYTES)

    # Ensure parent dir exists with private permissions. ``mode=0o700`` is the
    # umask-respecting permission used at *creation* time; an existing dir is
    # left alone (``exist_ok=True``). Failures (PermissionError, etc.)
    # propagate per fail-closed contract.
    audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # ``O_APPEND`` gives atomic concurrent appends; ``O_CREAT`` handles the
    # first call; ``0o600`` keeps the file owner-only. No try/except — any
    # ``OSError`` from ``os.write`` / ``os.fsync`` propagates so the caller
    # drops the partial record. The ``try / finally`` only guarantees the
    # descriptor is released; it does NOT silence the syscall failures.
    fd = os.open(str(audit_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
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
        # (or raised), so a close failure here would only mask the
        # real outcome. Mirrors the sibling writers.
        with contextlib.suppress(OSError):
            os.close(fd)

    # ANSI-safe lazy-format summary. The summary fields are user-controlled
    # (``model_unique_id`` ultimately comes from a dbt manifest) so they
    # MUST go through ``json.dumps`` rather than f-string interpolation —
    # ``json.dumps`` escapes ANSI / control bytes as ``\uXXXX`` so a crafted
    # value cannot smuggle terminal escape sequences into a log viewer.
    _LOGGER.info(
        "audit event: %s",
        json.dumps(
            {
                "unique_id": event.model_unique_id,
                "mode": event.mode.value,
                "columns_sent": len(event.columns_sent),
                "redacted": len(event.redactions),
                "audit_schema_version": event.audit_schema_version,
            }
        ),
    )


__all__ = ["write", "_AUDIT_RECORD_LIMIT_BYTES"]
