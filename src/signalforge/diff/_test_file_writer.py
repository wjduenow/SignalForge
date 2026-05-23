"""Injection-safe filename builder + fail-closed ``.sql`` writer (US-011 of #116).

Singular business-rule tests (DEC-010 of ``plans/super/116-business-rule-tests.md``)
are emitted as standalone ``.sql`` files under ``tests/`` when ``generate --write``
runs. This module ships the two primitives a later bead's CLI write-path calls:

1. :func:`anchor_to_filename` — turns a test anchor (model name, optional column
   descriptor, args-hash) into a safe **relative** filename of the shape
   ``tests/<model>__<descriptor>_<hash>.sql``. Every path-escape vector
   (``..``, ``/``, ``\\``, absolute paths, control chars, NUL bytes) is
   slugged to ``_`` so a crafted manifest model name or LLM-emitted column
   name can never escape the ``tests/`` directory.

2. :func:`write_test_file` — the project's **sixth** fail-closed writer
   (after ``safety.audit`` / ``draft.audit`` / ``prune.audit`` /
   ``grade.audit`` / ``diff._sidecar``). Mirrors the
   :func:`signalforge.diff._sidecar.write_sidecar` template **exactly**:
   size-check before open → symlink-hardened path canonicalisation →
   ``mkdir -p`` → ``os.open(O_WRONLY | O_CREAT | O_TRUNC, 0o600)`` →
   short-write ``while`` loop → ``os.fsync`` → close in ``try / finally``.
   **No ``except`` around the write/fsync** — the propagation IS the defence
   (``safety-layer.md`` §"Fail-closed writer shape — Scan 8"). A
   ``-- signalforge:generated <hash>`` header marker is prepended to the
   file content so a later overwrite-guard can recognise SignalForge's own
   output and refuse to clobber a hand-edited file (DEC-010).

Both primitives are silent (no logging) — the consuming CLI write-path owns
observability.
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path
from typing import Final

from signalforge._common.path_safety import PathContainmentError, canonicalise_path
from signalforge.diff.errors import (
    DiffTestFileRecordTooLargeError,
    DiffTestFileWriteError,
)

# DEC-010 of #116 — 1 MB cap on a single generated ``.sql`` test file. A
# singular business-rule test is a handful of SQL lines plus the header
# marker; 1 MB is two orders of magnitude of headroom and matches the
# grade sidecar's cap (a single small document, unlike the diff sidecar's
# 10 MB unified-diff payload). Checked on the encoded byte length BEFORE
# any ``os.open`` so an oversize payload leaves no on-disk artefact.
_DIFF_TEST_FILE_RECORD_LIMIT_BYTES: Final[int] = 1_000_000

# The directory every generated test file lands under (relative to the
# project root). dbt's convention; the builder hard-codes it so a crafted
# model/column name cannot redirect the write elsewhere.
_TESTS_DIRNAME: Final[str] = "tests"

# Header marker prepended to every generated ``.sql`` file. The trailing
# ``<hash>`` is the test's args-hash so a later overwrite-guard can both
# recognise SignalForge's own output AND detect a content change.
_GENERATED_MARKER_PREFIX: Final[str] = "-- signalforge:generated"

# Non-`[A-Za-z0-9]` runs collapse to a single ``_``. Applied to every
# user/LLM-controlled filename component (model name, descriptor, hash) so
# ``..`` / ``/`` / ``\\`` / control chars / NUL / whitespace all become
# ``_`` and cannot be path separators or traversal tokens.
_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9]+")


def _slug(component: str) -> str:
    """Slug a single filename component to ``[A-Za-z0-9_]`` only.

    Every run of one-or-more non-alphanumeric characters collapses to a
    single underscore; leading / trailing underscores are stripped. The
    result can contain NO path separator, NO traversal token (``..`` →
    ``_``), NO control char, NO NUL byte, NO whitespace — so it is safe to
    interpolate into a relative filename.

    An empty or all-non-alphanumeric input (e.g. ``"../"`` or ``""``)
    slugs to the empty string; callers substitute a placeholder so the
    final filename never carries an empty component.
    """
    return _SLUG_RE.sub("_", component).strip("_")


def anchor_to_filename(
    *,
    model_name: str,
    descriptor: str,
    args_hash: str,
) -> str:
    """Build a safe **relative** ``tests/<model>__<descriptor>_<hash>.sql`` filename.

    Every component is slugged to ``[A-Za-z0-9_]`` via :func:`_slug` so a
    crafted ``model_name`` like ``"../../etc/passwd"`` or an LLM-emitted
    ``descriptor`` like ``"foo/../../bar"`` cannot inject a path separator
    or a ``..`` traversal token — both collapse to ``_``. The returned
    string is ALWAYS a relative path rooted at the ``tests/`` directory
    with exactly one path component below it; the caller joins it onto the
    project root and routes the result through
    :func:`signalforge._common.path_safety.canonicalise_path` for a second,
    defence-in-depth containment check.

    A component that slugs to the empty string (all-non-alphanumeric input)
    is replaced with ``"_"`` so the filename never carries an empty segment
    (which would produce ``tests/__foo_<hash>.sql`` shapes that collide).

    Args:
        model_name: the model the test anchors to (manifest-supplied;
            potentially hostile).
        descriptor: a short human-readable descriptor of the test (e.g.
            the column name + test type; LLM-emitted; potentially hostile).
        args_hash: the test's args-hash (already hex from
            :func:`signalforge._common.artifact_id.model_test_args_hash`,
            but slugged anyway as belt-and-braces).

    Returns:
        A relative POSIX path string of the form
        ``tests/<model>__<descriptor>_<hash>.sql`` with every component
        slugged. Uses ``/`` as the separator (POSIX); the caller turns it
        into a :class:`~pathlib.Path` for joining.
    """
    model = _slug(model_name) or "_"
    desc = _slug(descriptor) or "_"
    digest = _slug(args_hash) or "_"
    # ``__`` between model and descriptor mirrors the dotted-path
    # artifact_id convention's scope boundary; ``_`` before the hash keeps
    # the hash visually attached to the descriptor it disambiguates.
    return f"{_TESTS_DIRNAME}/{model}__{desc}_{digest}.sql"


def _with_marker(sql: str, *, args_hash: str) -> str:
    """Prepend the ``-- signalforge:generated <hash>`` header marker to ``sql``.

    The marker is a SQL line comment so the resulting file is still valid
    dbt-runnable SQL. The trailing ``<hash>`` lets a later overwrite-guard
    recognise SignalForge's own output (refuse to clobber a hand-edited
    file) and detect a content change. A single blank line separates the
    marker from the body for readability; the body's trailing newline is
    preserved (or added) so the file is POSIX-newline-terminated.
    """
    body = sql if sql.endswith("\n") else sql + "\n"
    return f"{_GENERATED_MARKER_PREFIX} {args_hash}\n\n{body}"


def write_test_file(
    sql: str,
    *,
    relative_path: str,
    args_hash: str,
    project_dir: Path,
    size_limit_bytes: int | None = None,
) -> Path:
    """Write a generated singular-test ``.sql`` file. Fail-closed.

    The project's **sixth** fail-closed writer. Mirrors
    :func:`signalforge.diff._sidecar.write_sidecar` semantics verbatim;
    the only differences are the artefact (a ``.sql`` text file, not JSON)
    and the smaller size cap.

    Steps:

    1. Prepend the ``-- signalforge:generated <args_hash>`` header marker
       (DEC-010) to ``sql`` via :func:`_with_marker`.
    2. UTF-8 encode the marked content.
    3. **Size check BEFORE any file open**: if the encoded byte length
       exceeds :data:`_DIFF_TEST_FILE_RECORD_LIMIT_BYTES` (or the caller's
       ``size_limit_bytes`` override), raise
       :class:`DiffTestFileRecordTooLargeError`. Leaves no on-disk
       artefact.
    4. **Path canonicalisation BEFORE any file open**: join
       ``relative_path`` onto ``project_dir`` and route the result through
       :func:`signalforge._common.path_safety.canonicalise_path`. A symlink
       escape / cycle (or a ``relative_path`` that escapes the project
       tree) raises :class:`PathContainmentError`, wrapped as
       :class:`DiffTestFileWriteError` — the ONLY exception this writer
       wraps, because the path-safety helper raises a layer-neutral error
       and the diff layer surfaces its own typed error.
    5. ``mkdir -p`` the parent directory (the ``tests/`` directory may not
       exist yet).
    6. ``os.open`` with ``O_WRONLY | O_CREAT | O_TRUNC`` and mode
       ``0o600``. ``O_TRUNC`` zero-lengths the file on open so a re-run
       replaces the prior file atomically.
    7. Single ``os.write`` (looped on short returns), then ``os.fsync``,
       then close in a ``try / finally`` that only releases the descriptor
       — it does NOT swallow write/fsync failures. No ``except`` around the
       write/fsync (scan 8 asserts this).

    Args:
        sql: the test SQL body (the failing-rows query). The header marker
            is prepended automatically — callers pass the bare SQL.
        relative_path: the relative ``tests/<...>.sql`` path produced by
            :func:`anchor_to_filename`. Joined onto ``project_dir``.
        args_hash: the test's args-hash, embedded verbatim in the header
            marker.
        project_dir: the project root the orchestrator operates against.
            The containment check is gated against this path.
        size_limit_bytes: optional override for the byte cap; absent the
            module-level constant applies.

    Returns:
        The canonical absolute :class:`~pathlib.Path` the file was written
        to. Lets the caller report the exact on-disk location and feed it
        into the diff sidecar.

    Raises:
        DiffTestFileRecordTooLargeError: the marked payload exceeds the
            byte cap. Raised BEFORE any file is opened — leaves no on-disk
            artefact.
        DiffTestFileWriteError: ``relative_path`` escapes ``project_dir``
            (symlink / traversal) or contains a symlink cycle. The
            original :class:`PathContainmentError` is preserved as
            ``__cause__`` / ``cause``.
        OSError: any underlying I/O failure on the open / write / fsync
            propagates raw. The caller (the CLI write-path) wraps these as
            :class:`DiffTestFileWriteError` with run-context; the writer
            stays minimal so the propagation IS the contract.
    """
    body = _with_marker(sql, args_hash=args_hash)
    encoded = body.encode("utf-8")

    # Size check BEFORE any file open so an oversize record leaves no
    # on-disk artefact. Mirrors safety / draft / prune / grade / diff.
    effective_limit = (
        size_limit_bytes if size_limit_bytes is not None else _DIFF_TEST_FILE_RECORD_LIMIT_BYTES
    )
    if len(encoded) > effective_limit:
        raise DiffTestFileRecordTooLargeError(
            size=len(encoded),
            limit=effective_limit,
        )

    # Path canonicalisation BEFORE any file open. ``relative_path`` is
    # already builder-slugged, but the second containment check via the
    # project's standard helper is defence-in-depth: a symlink at the
    # target, or a caller passing a relative_path that escapes the tree,
    # is rejected. The PathContainmentError → DiffTestFileWriteError wrap
    # is the ONLY exception wrap the writer performs; non-path I/O
    # failures propagate raw.
    project_dir = Path(project_dir)
    target = project_dir / relative_path
    try:
        canonical_path = canonicalise_path(target, project_dir=project_dir)
    except PathContainmentError as exc:
        raise DiffTestFileWriteError(
            f"Generated test-file path {relative_path!r} failed symlink/containment "
            "validation against the project directory.",
            cause=exc,
        ) from exc

    # Ensure the parent (``tests/``) directory exists. ``exist_ok=True`` is
    # idempotent; a permission failure here propagates raw as OSError per
    # the contract.
    canonical_path.parent.mkdir(parents=True, exist_ok=True)

    # ``O_TRUNC`` zero-lengths the file on open; ``0o600`` keeps it
    # owner-only. No try/except — any syscall failure propagates so the
    # caller wraps with run-context. The single ``try / finally`` below
    # only releases the descriptor; scan 8 asserts it is the only ``Try``
    # node guarding os.write/os.fsync in this module (no ``except``).
    fd = os.open(
        str(canonical_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        # ``os.write`` may return fewer bytes than requested (EINTR on a
        # signal-interrupted call, or short writes on certain
        # filesystems / kernels). Loop until the full payload lands;
        # raise on a zero-byte return. Mirrors the diff sidecar writer.
        written = 0
        while written < len(encoded):
            n = os.write(fd, encoded[written:])
            if n == 0:
                raise OSError("os.write returned 0 — disk full or other I/O failure")
            written += n
        os.fsync(fd)
    finally:
        # Best-effort close; the write/fsync above already succeeded (or
        # raised). ``contextlib.suppress`` here ONLY guards the close — it
        # does NOT wrap write/fsync (scan 8 asserts no ``except`` handlers
        # around the syscalls).
        with contextlib.suppress(OSError):
            os.close(fd)

    return canonical_path


# Sorted alphabetically (mirrors sibling diff modules + the safety / draft /
# prune / grade audit-write seams).
__all__ = (
    "_DIFF_TEST_FILE_RECORD_LIMIT_BYTES",
    "_GENERATED_MARKER_PREFIX",
    "anchor_to_filename",
    "write_test_file",
)
