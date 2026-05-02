"""Fail-closed JSON sidecar writer for the diff renderer (US-007 of #8).

Mirrors :func:`signalforge.grade.audit.write_grading_report` (DEC-006/012
of grade-layer.md) and the JSONL writers in :mod:`signalforge.safety.audit`,
:mod:`signalforge.draft.audit`, :mod:`signalforge.prune.audit`, and
:mod:`signalforge.grade.audit`. This is the **fifth** instance of the
project's fail-closed write convention; reach for it (not ad-hoc
``Path.write_text`` calls) any time a new pipeline stage needs a durable,
fail-closed sidecar receipt.

Three load-bearing properties, all inherited verbatim from the grade-layer
sidecar precedent:

* **Single-document overwrite, not append.** The sidecar is end-of-run
  only; every ``render_diff`` call replaces the prior sidecar atomically
  via ``O_WRONLY | O_CREAT | O_TRUNC``. There is no concurrent-append
  contract; concurrent runs against the same ``sidecar_path`` produce
  different ``run_id`` values and follow last-writer-wins (mirrors
  ``grade-layer.md`` DEC-006).
* **Fail-closed on every error.** Path-safety failures (symlink escape /
  cycle) are wrapped as :class:`DiffSidecarWriteError(cause=...)` at
  writer entry — the orchestrator branches on that one typed error.
  Oversize records propagate as :class:`DiffSidecarRecordTooLargeError`.
  ``OSError`` / ``PermissionError`` / encoding failures from the
  ``os.open`` / ``os.write`` / ``os.fsync`` syscalls all propagate raw
  so the orchestrator's exception ladder can wrap them in its own
  :class:`DiffSidecarWriteError` with the run-context-specific message.
  **No try/except** around the write/fsync — the propagation IS the
  contract. The only ``try / finally`` block guarantees the file
  descriptor is released without suppressing exceptions, mirroring the
  pattern enforced by US-007's AST defence test.
* **Size cap before any file open.** :data:`_DIFF_SIDECAR_RECORD_LIMIT_BYTES`
  is checked on the encoded byte length BEFORE any ``os.open`` call so
  an oversize payload leaves no on-disk artefact. The cap is 10 MB —
  an order of magnitude above the grade sidecar's 1 MB cap because
  diff text (unified diffs over thousands of lines) is naturally larger
  than evidence-only payloads (DEC-009 of #8).

The path-safety gate at writer entry routes ``sidecar_path`` through
:func:`signalforge.warehouse._path_safety.canonicalise_path`, the
project's standard symlink/containment helper. A symlink at
``<project>/.signalforge/diff.json`` pointing outside the project tree
is rejected — the writer refuses rather than write outside the project
sandbox. Mirrors prune's post-QG fix and grade's writer-entry
canonicalisation.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Final

from signalforge.diff.errors import DiffSidecarRecordTooLargeError, DiffSidecarWriteError
from signalforge.diff.models import DiffReport
from signalforge.warehouse._path_safety import canonicalise_path
from signalforge.warehouse.errors import ProfileNotFoundError

# DEC-009 of #8 — 10 MB cap, an order of magnitude above the grade
# sidecar's 1 MB cap because diff text (unified diffs across thousands of
# lines for a wide model) is naturally larger than the grade sidecar's
# evidence-only payloads. Pre-write size check raises
# :class:`DiffSidecarRecordTooLargeError` BEFORE any ``os.open`` so an
# oversize payload leaves no on-disk artefact.
_DIFF_SIDECAR_RECORD_LIMIT_BYTES: Final[int] = 10_000_000


def write_sidecar(
    report: DiffReport,
    *,
    sidecar_path: Path,
    project_dir: Path,
) -> None:
    """Write ``report`` to ``sidecar_path`` as a single JSON document. Fail-closed.

    Mirrors :func:`signalforge.grade.audit.write_grading_report` semantics
    exactly, with two differences specific to the diff renderer:

    * Larger size cap (:data:`_DIFF_SIDECAR_RECORD_LIMIT_BYTES` = 10 MB)
      because diff text is bigger than evidence-only payloads (DEC-009).
    * Symlink containment is gated against the **caller-supplied**
      ``project_dir``, not derived from ``sidecar_path.parent.parent``.
      The orchestrator (US-008) is the place that knows the true project
      directory; deriving it from the sidecar path would let a caller
      passing ``sidecar_path=/tmp/diff.json`` slip a containment check
      against ``/`` (mirrors the post-QG fix documented in
      ``grade-layer.md``'s "symlink-hardened path canonicalisation at
      the orchestrator" section).

    Steps:

    1. Serialise the report via :meth:`pydantic.BaseModel.model_dump_json`
       (``by_alias=True``, ``indent=2`` so the on-disk artefact is
       human-readable for review). Trailing newline keeps the document
       POSIX-newline-terminated for downstream tooling.
    2. UTF-8 encode the line.
    3. **Size check BEFORE any file open**: if the encoded byte length
       exceeds :data:`_DIFF_SIDECAR_RECORD_LIMIT_BYTES`, raise
       :class:`DiffSidecarRecordTooLargeError`. Leaves no on-disk
       artefact.
    4. **Path canonicalisation BEFORE any file open**: route
       ``sidecar_path`` through
       :func:`signalforge.warehouse._path_safety.canonicalise_path` with
       the caller-supplied ``project_dir``. A symlink escape or cycle is
       wrapped as :class:`DiffSidecarWriteError` — this is the only place
       the writer wraps an exception, because the path-safety helper
       raises :class:`ProfileNotFoundError` (a warehouse-layer error name
       that would be confusing for diff callers).
    5. Ensure the parent directory exists (``mkdir(parents=True,
       exist_ok=True)``) BEFORE the ``os.open`` so the orchestrator can
       call us against a fresh ``.signalforge/`` directory.
    6. ``os.open`` with ``O_WRONLY | O_CREAT | O_TRUNC`` and mode
       ``0o600``. ``O_TRUNC`` zero-lengths the file on open so a re-run
       replaces the prior sidecar atomically (subject to the platform's
       truncate semantics).
    7. Single ``os.write`` (looped on short returns), then ``os.fsync``.
       The ``try / finally`` only guarantees the descriptor is released;
       it does NOT swallow ``write`` / ``fsync`` failures.

    Args:
        report: the :class:`DiffReport` to persist.
        sidecar_path: target sidecar path. Conventionally
            ``<project>/.signalforge/diff.json``. Must reside inside
            ``project_dir``; the writer refuses paths that escape via
            symlink.
        project_dir: the project directory the orchestrator is operating
            against. The containment check is gated against this path,
            not against any derivation from ``sidecar_path``, so a
            caller passing an absolute ``sidecar_path`` outside the
            project tree fails loudly.

    Raises:
        DiffSidecarRecordTooLargeError: the serialised payload exceeds
            :data:`_DIFF_SIDECAR_RECORD_LIMIT_BYTES`. Raised BEFORE any
            file is opened — an oversize record leaves no on-disk
            artefact.
        DiffSidecarWriteError: ``sidecar_path`` escapes ``project_dir``
            (symlink) or contains a symlink cycle. The original
            :class:`signalforge.warehouse.errors.ProfileNotFoundError`
            is preserved as ``__cause__`` / ``cause``.
        OSError: any underlying I/O failure on the open / write / fsync
            (``PermissionError``, ``FileNotFoundError``, etc.) propagates
            raw. The caller (the orchestrator in US-008) wraps these as
            :class:`DiffSidecarWriteError` with the run-context-specific
            message; the writer itself stays minimal so the propagation
            IS the contract.
    """
    # Serialise via Pydantic's JSON serialiser. ``indent=2`` gives a
    # human-readable on-disk artefact for review (the v0.3 GH Action
    # consumer parses this directly); ``by_alias=True`` keeps field
    # names stable across any future alias rename. Trailing newline
    # keeps the document POSIX-newline-terminated.
    body = report.model_dump_json(by_alias=True, indent=2) + "\n"
    encoded = body.encode("utf-8")

    # Size check BEFORE any file open so an oversize record leaves no
    # on-disk artefact. Mirrors safety / draft / prune / grade.
    if len(encoded) > _DIFF_SIDECAR_RECORD_LIMIT_BYTES:
        raise DiffSidecarRecordTooLargeError(
            size=len(encoded),
            limit=_DIFF_SIDECAR_RECORD_LIMIT_BYTES,
        )

    # Path canonicalisation BEFORE any file open. The caller-supplied
    # ``project_dir`` is the load-bearing containment boundary (mirrors
    # grade's post-QG fix — the orchestrator is the place that knows the
    # true project root; deriving it from ``sidecar_path.parent.parent``
    # would let an absolute caller-supplied path slip the gate). A
    # symlink escape or cycle in the helper raises
    # ``ProfileNotFoundError`` (the warehouse-layer name); wrap into a
    # ``DiffSidecarWriteError`` so callers branch on one diff-layer
    # error class. This is the ONLY exception wrap the writer performs
    # — non-path I/O failures propagate raw.
    sidecar_path = Path(sidecar_path)
    project_dir = Path(project_dir)
    try:
        canonical_path = canonicalise_path(sidecar_path, project_dir=project_dir)
    except ProfileNotFoundError as exc:
        raise DiffSidecarWriteError(
            f"Diff sidecar path {sidecar_path!r} failed symlink/containment validation.",
            cause=exc,
        ) from exc

    # Ensure the parent directory exists. The orchestrator (US-008) may
    # call us against a fresh ``.signalforge/`` that the user hasn't
    # created. ``mkdir`` with ``exist_ok=True`` is idempotent; a
    # permission failure here propagates raw as ``OSError`` /
    # ``PermissionError`` per the contract.
    canonical_path.parent.mkdir(parents=True, exist_ok=True)

    # Single-document overwrite. ``O_TRUNC`` zero-lengths the file on
    # open; ``0o600`` keeps ownership owner-only. No try/except — any
    # syscall failure propagates so the caller wraps with run-context.
    # The single ``try / finally`` below only guarantees the descriptor
    # is released; the AST defence test in ``tests/diff/test_sidecar.py``
    # asserts this is the only ``Try`` node in the module (no
    # ``except`` handlers around write/fsync).
    fd = os.open(
        str(canonical_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        # ``os.write`` may return fewer bytes than requested (EINTR on a
        # signal-interrupted call, or short writes on certain
        # filesystems / kernels). Loop until the full payload lands;
        # raise on a zero-byte return (disk full / unrecoverable I/O
        # failure). Mirrors the grade / prune / draft / safety writers.
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
        # outcome. Mirrors safety / draft / prune / grade. The
        # ``contextlib.suppress`` here ONLY guards the close itself —
        # it does NOT wrap write/fsync (the AST defence test asserts
        # there are no ``except`` handlers in this module's flow other
        # than the path-canonicalisation wrap above and this single
        # ``contextlib.suppress`` for the descriptor close).
        with contextlib.suppress(OSError):
            os.close(fd)


# Sorted alphabetically (mirrors the convention enforced by sibling
# modules under the diff subpackage and the audit-write seams of
# safety / draft / prune / grade).
__all__ = (
    "_DIFF_SIDECAR_RECORD_LIMIT_BYTES",
    "write_sidecar",
)
