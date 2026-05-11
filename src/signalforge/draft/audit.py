"""Fail-closed JSONL response-audit writer for the draft layer (US-012).

Mirrors :mod:`signalforge.safety.audit` exactly — same fail-closed contract
(DEC-011 of ``.claude/rules/safety-layer.md``), same POSIX-atomic-append size
cap, same ``O_APPEND | O_CREAT | 0o600`` open + ``os.fsync`` + close shape.
This is the *second* instance of the convention; reach for it (not ad-hoc
``Path.write_text`` calls) any time a new pipeline stage needs a durable,
fail-closed JSONL receipt.

Three load-bearing properties, all inherited verbatim from the safety layer:

* **Atomic concurrent appends.** ``os.open`` with ``O_APPEND`` + a single
  ``os.write`` call. POSIX guarantees ``write(2)`` is atomic up to
  ``PIPE_BUF`` (typically 4 KiB on Linux); the module-level
  :data:`_RESPONSE_AUDIT_RECORD_LIMIT_BYTES` enforces a 4000-byte cap with a
  96-byte margin so concurrent writers cannot interleave partial records.
* **Fail-closed on every error.** Serialisation errors, ``mkdir`` failures,
  ``open`` failures, ``write`` / ``fsync`` failures all propagate. Oversize
  records propagate as :class:`LLMResponseAuditRecordTooLargeError`. The
  caller (``draft_from_request`` in US-013) wraps any non-typed propagation
  as :class:`LLMResponseAuditWriteError`.
* **No internal exception handling.** ``write_response_event`` catches NO
  exceptions. An unaudited LLM response is, by definition, output leaving
  without a receipt — exactly the failure mode this layer exists to
  prevent. Don't add try/except around the writes "to be defensive"; the
  propagation IS the defence.

The three hash fields on :class:`LLMResponseEvent` (``response_text_hash``,
``parsed_schema_hash``, ``sent_sql_hash``) are deterministic 16-hex-char
``blake2b`` digests. Reviewers can correlate audit records with raw
responses / SQL / parsed schemas by re-computing the hash; the audit log
itself never carries the cleartext (which keeps records small and avoids
re-emitting whatever PII the LLM may have echoed back from the prompt).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

from signalforge.draft.models import CandidateSchema

if TYPE_CHECKING:
    from signalforge.llm.models import LLMResult

# POSIX guarantees ``write(2)`` is atomic only up to ``PIPE_BUF`` bytes
# (typically 4096 on Linux). The 96-byte margin leaves room for trailing
# newline plus any line-buffering / kernel overhead so the module's atomic-
# concurrent-append contract holds even at the size cap. Identical to
# ``signalforge.safety.audit._AUDIT_RECORD_LIMIT_BYTES`` by design.
_RESPONSE_AUDIT_RECORD_LIMIT_BYTES: Final[int] = 4000


class LLMResponseEvent(BaseModel):
    """One JSONL audit record per LLM response.

    Read-back-stable shape (``extra="ignore"`` per ``manifest-readers.md``
    DEC-008) — older readers tolerate forward-compat field additions, while
    a one-off ``extra="forbid"`` drift detector (US-014) catches silent
    schema expansion before a live audit log does.

    The three ``*_hash`` fields are 16-hex-char ``blake2b`` digests of the
    raw response text, the canonicalised parsed :class:`CandidateSchema`,
    and the rendered SQL respectively. Storing hashes (not cleartext) keeps
    individual records under the POSIX-atomic-append size cap and avoids
    re-emitting whatever PII the LLM may have echoed back from the prompt.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    timestamp: datetime
    model_unique_id: str
    prompt_version: str
    response_text_hash: str
    parsed_schema_hash: str
    sent_sql_hash: str
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    input_tokens: int
    output_tokens: int
    model: str
    signalforge_version: str
    audit_schema_version: int = 1


def _compute_response_text_hash(text: str) -> str:
    """Return a deterministic 16-hex-char ``blake2b`` digest of ``text``.

    The audit log records this hash rather than the raw response text, so
    individual JSONL records stay under the POSIX-atomic-append cap and the
    log doesn't re-emit any PII the LLM may have echoed from the prompt.
    Reviewers correlate records to raw responses by re-hashing the
    cleartext.
    """
    return hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()


def _compute_parsed_schema_hash(candidate: CandidateSchema) -> str:
    """Return a deterministic 16-hex-char digest of ``candidate``.

    Pydantic's emit order for ``model_dump_json`` is *not* a public-API
    guarantee across versions, so we re-serialise through ``json.dumps``
    with ``sort_keys=True`` to canonicalise the bytes. Two
    :class:`CandidateSchema` instances with the same content but
    constructed in different field orders therefore hash identically.
    """
    canonical = candidate.model_dump_json(by_alias=True)
    canonical_sorted = json.dumps(json.loads(canonical), sort_keys=True, ensure_ascii=False)
    return hashlib.blake2b(canonical_sorted.encode("utf-8"), digest_size=8).hexdigest()


def _compute_sent_sql_hash(raw_code: str) -> str:
    """Return a deterministic 16-hex-char digest of the SQL sent to the LLM.

    No normalisation is applied: comments, whitespace, and trailing
    newlines all participate in the hash. Reviewers comparing two audit
    records will see different ``sent_sql_hash`` values whenever the
    cleartext differs by even a byte, which is the desired property — we
    want to detect prompt drift, not paper over it.
    """
    return hashlib.blake2b(raw_code.encode("utf-8"), digest_size=8).hexdigest()


def _build_response_event(
    *,
    timestamp: datetime,
    model_unique_id: str,
    candidate: CandidateSchema,
    raw_text: str,
    sent_sql: str,
    result: LLMResult,
    prompt_version: str,
    signalforge_version: str,
) -> LLMResponseEvent:
    """Internal seam: construct an :class:`LLMResponseEvent` from the inputs.

    DEC-013 (US-014) reserves direct ``LLMResponseEvent(...)`` construction
    to this module — the AST-completeness scan rejects calls anywhere else
    in :mod:`signalforge.draft`. The integration layer
    (:mod:`signalforge.draft.schema`) calls this helper rather than
    constructing the event itself, so the audit-write seam stays the only
    code path that produces a record.

    Hashes are computed via :func:`_compute_response_text_hash`,
    :func:`_compute_parsed_schema_hash`, and :func:`_compute_sent_sql_hash`
    so the audit log records 16-hex digests rather than the raw cleartext
    (keeps records under the POSIX-atomic-append cap; avoids re-emitting
    PII the LLM may have echoed from the prompt).
    """
    return LLMResponseEvent(
        timestamp=timestamp,
        model_unique_id=model_unique_id,
        prompt_version=prompt_version,
        response_text_hash=_compute_response_text_hash(raw_text),
        parsed_schema_hash=_compute_parsed_schema_hash(candidate),
        sent_sql_hash=_compute_sent_sql_hash(sent_sql),
        cache_creation_input_tokens=result.cache_creation_input_tokens,
        cache_read_input_tokens=result.cache_read_input_tokens,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        model=result.model,
        signalforge_version=signalforge_version,
    )


def write_response_event(event: LLMResponseEvent, *, audit_path: Path) -> None:
    """Append a single JSONL record to ``audit_path``. Fail-closed.

    Mirrors :func:`signalforge.safety.audit.write` semantics exactly:
    serialise → size-check (BEFORE any file open) → ``mkdir -p`` parent →
    ``os.open(O_APPEND | O_CREAT, 0o600)`` → single ``os.write`` →
    ``os.fsync`` → close. Catches NO exceptions internally.

    Args:
        event: the :class:`LLMResponseEvent` to persist.
        audit_path: absolute or project-relative path; the parent directory
            is created with mode ``0o700`` if missing, and the audit file
            itself is created with mode ``0o600`` on first call.

    Raises:
        LLMResponseAuditRecordTooLargeError: the serialised line exceeds
            :data:`_RESPONSE_AUDIT_RECORD_LIMIT_BYTES`. Raised BEFORE any
            file is opened — an oversize record leaves no on-disk artefact.
        OSError: any underlying I/O failure (``PermissionError`` on the
            parent directory, ``OSError`` on the ``open`` / ``write`` /
            ``fsync`` syscalls). The caller (``draft_from_request`` in
            US-013) wraps these as
            :class:`signalforge.draft.errors.LLMResponseAuditWriteError`.
    """
    # Local import keeps ``audit`` importable without forcing the errors
    # module at module-eval time, matching :mod:`signalforge.safety.audit`'s
    # style.
    from signalforge.draft.errors import LLMResponseAuditRecordTooLargeError

    # Serialise. ``model_dump_json`` is the canonical pydantic serialiser;
    # any encoding failure (e.g. an unserialisable smuggled type) propagates
    # to the caller for fail-closed handling.
    serialised = event.model_dump_json(by_alias=True) + "\n"
    payload = serialised.encode("utf-8")

    # Size check BEFORE any file open so an oversize record leaves no
    # on-disk artefact. Mirrors ``signalforge.safety.audit`` DEC-022.
    if len(payload) > _RESPONSE_AUDIT_RECORD_LIMIT_BYTES:
        raise LLMResponseAuditRecordTooLargeError(
            size=len(payload),
            limit=_RESPONSE_AUDIT_RECORD_LIMIT_BYTES,
        )

    # Ensure parent dir exists with private permissions. ``mode=0o700`` is
    # the umask-respecting permission used at *creation* time; an existing
    # dir is left alone (``exist_ok=True``). Failures (PermissionError on
    # the parent of the parent, etc.) propagate per fail-closed contract.
    audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # ``O_APPEND`` gives atomic concurrent appends; ``O_CREAT`` handles the
    # first call; ``0o600`` keeps the file owner-only. No try/except — any
    # ``OSError`` propagates so the caller drops the partial response.
    fd = os.open(str(audit_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        # Best-effort close; the write/fsync above already succeeded
        # (or raised), so a close failure here would only mask the
        # real outcome. Mirrors ``signalforge.safety.audit.write``.
        with contextlib.suppress(OSError):
            os.close(fd)


# Sorted alphabetically (mirrors the convention enforced by tests/draft/test_errors.py).
__all__ = ("LLMResponseEvent", "write_response_event")
