"""Fail-closed JSONL audit-log writer for the safety layer (US-007 + US-003 of #185).

This module is the safety layer's single observability seam. Every LLM call
the request builder makes lands here as exactly one *logical* JSONL record;
any I/O failure aborts the call (DEC-011 fail-closed) so the system never
proceeds without an audit trail.

US-003 of #185 added **v4 chunking**: a wide-table event whose serialised
form exceeds the POSIX-atomic-append cap is split into a **header line**
(carrying all metadata + a chunk-correlation triple ``(audit_id,
chunk_index=0, chunk_count=N)``) followed by **N-1 continuation lines**
(each carrying only the chunk-correlation triple + a greedy-fit slice of
``redactions_by_reason`` + matching slice of ``column_name_map``). The
reader helper :func:`read_audit_events` reassembles chunked groups by
``audit_id``; partial groups on disk (mid-write crash, truncation) surface
as one WARNING and are skipped — they do NOT raise, because raising would
make the reader unusable in the exact scenario it was added to diagnose.

Four load-bearing properties:

* **Atomic concurrent appends per chunk** (DEC-005). Each chunk is one
  ``os.write`` call ≤ ``_AUDIT_RECORD_LIMIT_BYTES`` (4000 B with a 96-B
  margin under the 4 KiB POSIX ``PIPE_BUF`` floor). Sibling writers'
  chunks may *interleave* with this writer's chunks across the JSONL
  file, but the reader reassembles by ``audit_id`` so cross-event
  interleaving is correlatable. Single-thread per-event ordering is
  guaranteed because the chunk loop runs inside one ``Try / finally``
  with one open file descriptor.
* **Fail-closed on every error** (DEC-011). :func:`write` catches NO
  exceptions internally — serialisation errors, ``mkdir`` failures,
  ``open`` failures, ``write`` / ``fsync`` failures all propagate raw to
  the caller. The orchestrator
  (:func:`signalforge.safety.request.build_llm_request`) wraps non-typed
  propagations as :class:`AuditWriteError`. Oversize chunks raise
  :class:`AuditRecordTooLargeError` BEFORE any file is opened, so the
  serialised line(s) never land on disk. Pre-open size-check rejects
  pathological inputs (e.g. a hashed column name longer than the cap
  itself) and leaves no artefact. The propagation IS the defence — don't
  add try/except around the writes "to be defensive".
* **Per-chunk fsync** (DEC-006). Each chunk is independently durable
  before the next chunk is written. A mid-write crash leaves "header +
  K < N chunks" on disk — operator-visible incompleteness that
  :func:`read_audit_events` flags with a WARNING. The crash-safer
  shape than an ``is_final: bool`` flag (no "all chunks but header
  missing" failure mode).
* **ANSI-safe lazy-format logger** (DEC-022). Summary lines are logged
  via ``%s`` lazy-format with ``json.dumps`` of the user-controlled
  fields. f-string interpolation here would let a crafted
  ``model_unique_id`` containing raw ANSI escapes pollute log viewers;
  ``json.dumps`` escapes control characters as ``\\uXXXX``.

The "no logging in stage-0 modules" rule from ``manifest-readers.md`` does
NOT apply here — this module *is* the observability stage. INFO/WARNING-
level logging is its job, not noise.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Iterator
from hashlib import blake2b
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from signalforge.safety.models import AuditEvent

_LOGGER: Final = logging.getLogger("signalforge.safety")

# POSIX guarantees ``write(2)`` is atomic only up to ``PIPE_BUF`` bytes
# (typically 4096 on Linux). The 96-byte margin leaves room for trailing
# newline plus any line-buffering / kernel overhead so the module's atomic-
# concurrent-append contract holds even at the size cap.
_AUDIT_RECORD_LIMIT_BYTES: Final[int] = 4000


def _serialise_payload(payload: dict[str, Any]) -> bytes:
    """Canonical JSONL serialisation: compact separators + trailing newline."""
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    return line.encode("utf-8")


def _compute_audit_id(event: AuditEvent) -> str:
    """Deterministic 16-hex blake2b-8 over (model_unique_id, timestamp, version).

    Same event → same ``audit_id`` across runs, so re-running the same
    pipeline produces correlatable audit records. NUL-separator prevents
    field-concatenation collisions (e.g. a model id ending in the
    timestamp prefix).
    """
    # Non-chunked-shape inputs only; the v4 validator guarantees these are
    # non-None on the path that constructs the source event.
    assert event.model_unique_id is not None  # noqa: S101
    assert event.timestamp is not None  # noqa: S101
    assert event.signalforge_version is not None  # noqa: S101
    payload = (
        event.model_unique_id.encode("utf-8")
        + b"\x00"
        + event.timestamp.isoformat().encode("utf-8")
        + b"\x00"
        + event.signalforge_version.encode("utf-8")
    )
    return blake2b(payload, digest_size=8).hexdigest()


def _build_header_payload(
    event: AuditEvent,
    *,
    audit_id: str,
    chunk_count: int,
) -> dict[str, Any]:
    """Build the header-chunk payload (chunk_index=0, empty redaction maps).

    Carries every metadata field from the source event plus the
    chunk-correlation triple. ``redactions_by_reason`` / ``column_name_map``
    are emitted as empty dicts (the per-chunk validator requires both
    present but empty on the header).
    """
    base = event.model_dump(mode="json")
    base["audit_id"] = audit_id
    base["chunk_index"] = 0
    base["chunk_count"] = chunk_count
    base["redactions_by_reason"] = {}
    base["column_name_map"] = {}
    return base


def _build_continuation_payload(
    *,
    audit_id: str,
    chunk_index: int,
    chunk_count: int,
    redactions_slice: dict[str, tuple[str, ...]],
    column_name_map_slice: dict[str, str],
    audit_schema_version: int,
) -> dict[str, Any]:
    """Build a continuation-chunk payload — all metadata None, slice present.

    The per-chunk validator on :class:`AuditEvent` requires every metadata
    field to be ``None`` on a chunk-continuation row; ``redactions_by_reason``
    and ``column_name_map`` carry the slice for this chunk.
    """
    return {
        "timestamp": None,
        "model_unique_id": None,
        "mode": None,
        "columns_sent": None,
        "row_count": None,
        "signalforge_version": None,
        "policy_hash": None,
        "policy_flags": None,
        "redactions_by_reason": {k: list(v) for k, v in redactions_slice.items()},
        "column_name_map": dict(column_name_map_slice),
        "audit_id": audit_id,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "audit_schema_version": audit_schema_version,
    }


def _greedy_pack_continuations(
    *,
    audit_id: str,
    redactions_by_reason: dict[str, tuple[str, ...]],
    column_name_map: dict[str, str],
    audit_schema_version: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Greedy-pack redactions across continuation chunks (sorted-key order).

    Returns the list of continuation *payload dicts* in chunk-index order
    starting at ``chunk_index=1``. The caller stitches in the final
    ``chunk_count`` once N is known.

    Walks ``redactions_by_reason`` in sorted-key order; for each reason
    appends as many hashed names as fit in the current chunk envelope. A
    reason whose hashed-name list alone exceeds the chunk capacity is
    split across multiple chunks (so a single hashed name fitting on
    its own is the only thing that can cause a pathological
    over-cap chunk — that's what the writer's pre-open check catches).
    """
    continuations: list[dict[str, Any]] = []
    current_redactions: dict[str, list[str]] = {}
    current_map: dict[str, str] = {}

    def _current_payload(temp_index: int, temp_count: int) -> dict[str, Any]:
        # Snapshot the in-progress buffers into a fresh payload dict.
        return _build_continuation_payload(
            audit_id=audit_id,
            chunk_index=temp_index,
            chunk_count=temp_count,
            # Convert lists back to tuples for shape parity with the model.
            redactions_slice={k: tuple(v) for k, v in current_redactions.items()},
            column_name_map_slice=current_map,
            audit_schema_version=audit_schema_version,
        )

    def _flush() -> None:
        nonlocal current_redactions, current_map
        if current_redactions:
            # The chunk_index here is provisional — caller will rewrite once N
            # is known (we use chunk_count placeholder=2 to keep the dict
            # validator-shape-compatible mid-build; final stamp is done later).
            continuations.append(_current_payload(temp_index=len(continuations) + 1, temp_count=2))
            current_redactions = {}
            current_map = {}

    next_provisional_index = len(continuations) + 1

    for reason in sorted(redactions_by_reason.keys()):
        names = redactions_by_reason[reason]
        idx = 0
        while idx < len(names):
            # Try to fit the next name into the current chunk. Critical:
            # deep-copy the per-reason list so the tentative-append cannot
            # mutate ``current_redactions[reason]`` if the fit-check fails
            # (a shallow ``{**current_redactions}`` copies the dict's
            # references, NOT the contained lists — the bug shape that
            # caused chunk-boundary duplicates).
            tentative_redactions = {k: list(v) for k, v in current_redactions.items()}
            tentative_redactions.setdefault(reason, []).append(names[idx])
            tentative_map = {**current_map, names[idx]: column_name_map.get(names[idx], "")}

            # Build a tentative payload to measure its serialised size.
            tentative_payload = _build_continuation_payload(
                audit_id=audit_id,
                chunk_index=next_provisional_index,
                chunk_count=2,  # placeholder; cap-check is size-only
                redactions_slice={k: tuple(v) for k, v in tentative_redactions.items()},
                column_name_map_slice=tentative_map,
                audit_schema_version=audit_schema_version,
            )
            tentative_size = len(_serialise_payload(tentative_payload))

            if tentative_size <= limit:
                # Fits — commit the name into the current chunk.
                current_redactions = {k: list(v) for k, v in tentative_redactions.items()}
                current_map = tentative_map
                idx += 1
                continue

            # Doesn't fit. If current chunk has at least one entry, flush
            # and try again on a fresh chunk. If current is empty, this
            # single (reason, name) pair cannot fit at all — emit it as
            # its own (oversize) chunk; the writer's pre-open check will
            # raise.
            if current_redactions:
                _flush()
                next_provisional_index = len(continuations) + 1
                continue

            # Single-name-over-cap path: emit the lone entry as a chunk
            # so the writer's pre-open check has something concrete to
            # measure and reject.
            current_redactions = {reason: [names[idx]]}
            current_map = {names[idx]: column_name_map.get(names[idx], "")}
            _flush()
            next_provisional_index = len(continuations) + 1
            idx += 1

    # Final flush of any in-progress buffer.
    _flush()

    return continuations


def _chunk_event(event: AuditEvent, limit: int = _AUDIT_RECORD_LIMIT_BYTES) -> tuple[bytes, ...]:
    """Serialise ``event``; if it fits in one ≤ ``limit`` line, return a
    single-element tuple. Otherwise emit a header line + N-1 continuation
    lines, each ≤ ``limit`` where possible.

    Returns the serialised chunk bytes in write order: header is always
    ``chunks[0]``; continuations follow in chunk_index order.

    Single-chunk path: serialise the source event verbatim; if the encoded
    line ≤ ``limit``, return ``(line,)``.

    Multi-chunk path: compute a deterministic ``audit_id`` from the source
    event, build a header (chunk_index=0, empty redaction maps), greedy-pack
    the redactions into continuations, recompute ``chunk_count = 1 +
    len(continuations)``, re-stamp every chunk's ``chunk_count``, and
    serialise each.

    The pre-open size check inside :func:`write` is the gate that catches
    a pathological "single hashed name longer than the cap" — this
    function may produce one chunk over ``limit`` in that case; the writer
    will raise :class:`AuditRecordTooLargeError` and never open the file.
    """
    # Single-chunk fast path: try serialising the event directly.
    single_payload = event.model_dump(mode="json")
    single_line = _serialise_payload(single_payload)
    if len(single_line) <= limit:
        return (single_line,)

    # Multi-chunk path. Compute the correlation id and pack continuations.
    audit_id = _compute_audit_id(event)

    redactions_by_reason: dict[str, tuple[str, ...]] = {
        # ``event.redactions_by_reason`` is keyed by ``RedactionReason``
        # (a Literal); cast to str-keyed for our internal slicing.
        str(k): v
        for k, v in (event.redactions_by_reason or {}).items()
    }
    column_name_map = dict(event.column_name_map or {})

    continuations_payloads = _greedy_pack_continuations(
        audit_id=audit_id,
        redactions_by_reason=redactions_by_reason,
        column_name_map=column_name_map,
        audit_schema_version=event.audit_schema_version,
        limit=limit,
    )

    chunk_count = 1 + len(continuations_payloads)

    # Re-stamp chunk_count + chunk_index on every continuation payload (the
    # greedy packer used a placeholder while building).
    for i, payload in enumerate(continuations_payloads, start=1):
        payload["chunk_index"] = i
        payload["chunk_count"] = chunk_count

    header_payload = _build_header_payload(event, audit_id=audit_id, chunk_count=chunk_count)

    chunks: list[bytes] = [_serialise_payload(header_payload)]
    for payload in continuations_payloads:
        chunks.append(_serialise_payload(payload))

    return tuple(chunks)


def write(event: AuditEvent, audit_path: Path) -> None:
    """Append one logical audit event as N≥1 JSONL chunk-lines. Fail-closed.

    Mirrors :func:`signalforge.prune.audit._write_prune_event`,
    :func:`signalforge.draft.audit.write_response_event`,
    :func:`signalforge.grade.audit.write_grade_event`, and
    :func:`signalforge.diff._sidecar.write_sidecar` semantics:
    serialise → chunk → size-check EVERY chunk (BEFORE any file open) →
    ``mkdir -p`` parent → ``os.open(O_APPEND | O_CREAT | O_WRONLY, 0o600)``
    → for each chunk: looped ``os.write`` → ``os.fsync`` → close. Catches
    NO exceptions internally; the ``try / finally`` around ``os.close``
    only guarantees the descriptor is released, it does NOT swallow
    ``write`` / ``fsync`` failures.

    Args:
        event: the :class:`~signalforge.safety.models.AuditEvent` to persist.
        audit_path: absolute or project-relative path; the parent directory
            is created with mode ``0o700`` if missing, and the audit file
            itself is created with mode ``0o600`` on first call.

    Raises:
        AuditRecordTooLargeError: any single chunk exceeds the POSIX-atomic-
            append size cap (e.g. a pathologically-long hashed column name
            that cannot fit even alone in a chunk envelope). Raised BEFORE
            any file is opened — no on-disk artefact.
        OSError: any underlying I/O failure (``PermissionError``,
            ``FileNotFoundError``, ``IsADirectoryError``, etc.) from
            ``mkdir`` / ``os.open`` / ``os.write`` / ``os.fsync`` propagates
            raw. The caller
            (:func:`signalforge.safety.request.build_llm_request`) wraps
            non-typed propagations as
            :class:`signalforge.safety.errors.AuditWriteError`.
        TypeError: a JSON-encoding failure from :func:`json.dumps` (e.g. an
            unserialisable smuggled type) propagates raw. Same orchestrator
            wrap as ``OSError``.
        ValueError: same as ``TypeError`` — any other
            :func:`json.dumps` failure mode propagates raw.
    """
    # Local import keeps ``audit`` importable without forcing the errors module
    # at module-eval time and matches the style used elsewhere in the package.
    from signalforge.safety.errors import AuditRecordTooLargeError

    # Serialise + chunk. Any encoding error (e.g. an unserialisable smuggled
    # type) propagates raw; the orchestrator wraps as ``AuditWriteError``.
    chunks = _chunk_event(event, limit=_AUDIT_RECORD_LIMIT_BYTES)

    # Pre-open size check on EVERY chunk so a single pathologically-large
    # chunk leaves no on-disk artefact. Mirrors prune/draft/grade/diff.
    # US-004 of #185 (DEC-007): pass ``column_count`` so the default
    # remediation builds the three-sentence operator script naming the
    # actual column count + byte overage. Best-available estimate: each
    # entry in ``column_name_map`` is a redacted column, and each entry
    # in any ``redactions_by_reason`` value list is also a redacted
    # column — combine for a useful upper bound.
    column_count = len(event.column_name_map or {}) + sum(
        len(v) for v in (event.redactions_by_reason or {}).values()
    )
    for chunk in chunks:
        if len(chunk) > _AUDIT_RECORD_LIMIT_BYTES:
            raise AuditRecordTooLargeError(
                size=len(chunk),
                limit=_AUDIT_RECORD_LIMIT_BYTES,
                column_count=column_count,
            )

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
    #
    # Scan 8 invariant: ONE Try block (no except handlers) wraps the entire
    # chunk loop; ``os.close`` is the only thing under finally. The chunk
    # loop is inside the try; the short-write While loop inside the chunk
    # loop satisfies Scan 8's "one While containing os.write per writer".
    fd = os.open(str(audit_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        for chunk in chunks:
            # ``os.write`` may return fewer bytes than requested (EINTR on a
            # signal-interrupted call, or short writes on certain filesystems
            # / kernels). Loop until the full payload lands; raise on a
            # zero-byte return (disk full / unrecoverable I/O failure).
            # POSIX atomicity for ``O_APPEND`` writes still holds at the
            # ``write(2)`` boundary up to ``PIPE_BUF``; the loop is the
            # documented short-write recovery, not a contract violation.
            written = 0
            while written < len(chunk):
                n = os.write(fd, chunk[written:])
                if n == 0:
                    raise OSError("os.write returned 0 — disk full or other I/O failure")
                written += n
            # Per-chunk fsync — each chunk is independently durable so a
            # mid-write crash leaves "header + K<N chunks" on disk rather
            # than a half-flushed final chunk.
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
    redacted_count = (
        sum(len(v) for v in event.redactions_by_reason.values())
        if event.redactions_by_reason is not None
        else 0
    )
    _LOGGER.info(
        "audit event: %s",
        json.dumps(
            {
                "unique_id": event.model_unique_id,
                "mode": event.mode.value if event.mode is not None else None,
                "columns_sent": len(event.columns_sent) if event.columns_sent is not None else 0,
                "redacted": redacted_count,
                "audit_schema_version": event.audit_schema_version,
                "chunk_count": len(chunks),
            }
        ),
    )


def read_audit_events(path: Path) -> Iterator[AuditEvent]:
    """Read a JSONL audit file; yield non-chunked rows verbatim and reassemble
    chunked groups by ``audit_id``.

    The reader buffers chunked rows in-memory keyed by ``audit_id`` until the
    full group is observed (``len(accumulated) == chunk_count``), at which
    point it merges every chunk's ``redactions_by_reason`` (key-wise concat,
    sorted) + ``column_name_map`` (dict update), takes metadata from the
    header (chunk_index=0), and yields an :class:`AuditEvent` with the
    chunk-correlation triple cleared so the reassembled value round-trips
    through the non-chunked validator branch.

    Partial groups (incomplete chunk sets at end-of-stream) surface as one
    WARNING per group via the standard ANSI-safe lazy-format JSON logger
    and are SKIPPED — the reader does NOT raise. Raising would make the
    helper unusable for the exact diagnostic scenario (mid-write crash,
    truncation) it was added to support.

    Args:
        path: the audit JSONL file to read.

    Yields:
        Reassembled :class:`AuditEvent` instances in observation order (the
        order their FINAL chunk landed in the file). Non-chunked rows are
        yielded in line order.
    """
    from signalforge.safety.models import AuditEvent as AuditEventCls

    # Accumulator: audit_id -> dict[chunk_index, payload_dict]
    accumulated: dict[str, dict[int, dict[str, Any]]] = {}
    expected_counts: dict[str, int] = {}

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)

            audit_id = payload.get("audit_id")
            chunk_index = payload.get("chunk_index")
            chunk_count = payload.get("chunk_count")

            # Non-chunked: every chunk-triple field is None.
            if audit_id is None and chunk_index is None and chunk_count is None:
                yield AuditEventCls.model_validate(payload)
                continue

            # Chunked row.
            if audit_id is None or chunk_index is None:
                # Shape violation but we don't raise. Skip.
                continue

            group = accumulated.setdefault(audit_id, {})
            group[int(chunk_index)] = payload
            if chunk_count is not None:
                expected_counts[audit_id] = int(chunk_count)

            expected = expected_counts.get(audit_id)
            if expected is None:
                continue

            if len(group) < expected:
                continue

            # Group complete — reassemble.
            yield _reassemble_chunked_group(group, expected)
            del accumulated[audit_id]
            del expected_counts[audit_id]

    # End-of-stream: anything left in ``accumulated`` is incomplete.
    for audit_id, group in accumulated.items():
        expected = expected_counts.get(audit_id, -1)
        _LOGGER.warning(
            "audit chunk group incomplete: %s",
            json.dumps(
                {
                    "audit_id": audit_id,
                    "chunks_received": len(group),
                    "chunks_expected": expected,
                }
            ),
        )


def _reassemble_chunked_group(group: dict[int, dict[str, Any]], expected_count: int) -> AuditEvent:
    """Merge a complete chunked group back into a non-chunked AuditEvent.

    Header (chunk_index=0) carries metadata; continuations carry redaction
    slices. The reassembled event has ``audit_id`` / ``chunk_index`` /
    ``chunk_count`` cleared so it round-trips through the non-chunked
    validator branch (which is what callers wanting to compare against the
    original event expect).
    """
    from signalforge.safety.models import AuditEvent as AuditEventCls

    header = group[0]

    # Merge redactions across every continuation.
    merged_redactions: dict[str, list[str]] = {}
    merged_map: dict[str, str] = {}
    for index in sorted(group.keys()):
        if index == 0:
            continue
        chunk_payload = group[index]
        for reason, names in (chunk_payload.get("redactions_by_reason") or {}).items():
            merged_redactions.setdefault(reason, []).extend(names)
        merged_map.update(chunk_payload.get("column_name_map") or {})

    # Sort each reason's name list so the reassembled event is canonical
    # regardless of which chunks landed in which order.
    for reason in merged_redactions:
        merged_redactions[reason] = sorted(merged_redactions[reason])

    # Build the reassembled payload: header metadata + merged redactions,
    # chunk-triple cleared so it routes through the non-chunked validator.
    reassembled_payload = dict(header)
    reassembled_payload["audit_id"] = None
    reassembled_payload["chunk_index"] = None
    reassembled_payload["chunk_count"] = None
    reassembled_payload["redactions_by_reason"] = {
        k: tuple(v) for k, v in merged_redactions.items()
    }
    reassembled_payload["column_name_map"] = merged_map

    # Silence unused-arg complaints in static checkers — ``expected_count`` is
    # informational (the caller already gates on length match).
    _ = expected_count

    return AuditEventCls.model_validate(reassembled_payload)


__all__ = [
    "write",
    "read_audit_events",
    "_chunk_event",
    "_AUDIT_RECORD_LIMIT_BYTES",
]
