"""Tests for ``signalforge.diff._sidecar`` (US-007 of #8).

Mirrors :mod:`tests.grade.test_audit`'s sidecar coverage exactly — same
fail-closed contract (DEC-009 of #8, DEC-006/012 of grade-layer.md), same
``O_WRONLY | O_CREAT | O_TRUNC | 0o600`` open + single ``os.write``
(looped) + ``os.fsync`` + close shape, same symlink-hardened
canonicalisation gate at writer entry.

Each test asserts a load-bearing property of the writer:

* happy path: writes a single JSON document, file mode is ``0o600``,
  content round-trips through :func:`json.loads`
* oversize record raises :class:`DiffSidecarRecordTooLargeError` BEFORE
  any file open (no on-disk artefact)
* a ``sidecar_path`` that escapes ``project_dir`` via symlink raises
  :class:`DiffSidecarWriteError` (the path-safety gate runs against the
  caller-supplied ``project_dir``, not a derivation of ``sidecar_path``)
* a symlink cycle on the resolution path wraps as
  :class:`DiffSidecarWriteError`
* ``O_TRUNC`` semantics: writing twice to the same path overwrites
  cleanly with the second payload's bytes
* AST defence: the ``try / finally`` for the ``os.close`` descriptor
  release is the only ``Try`` node whose body contains ``os.write`` /
  ``os.fsync`` syscalls — the writer has **no** ``except`` handler
  around the write/fsync block (the propagation IS the defence,
  mirrors ``grade-layer.md`` DEC-006).

Real I/O on ``tmp_path`` because mocks of ``open`` hide buffering bugs
that the real syscall surface exposes.
"""

from __future__ import annotations

import ast
import json
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import signalforge
from signalforge.diff._sidecar import _DIFF_SIDECAR_RECORD_LIMIT_BYTES, write_sidecar
from signalforge.diff.errors import DiffSidecarRecordTooLargeError, DiffSidecarWriteError
from signalforge.diff.models import DiffReport


def _make_report(**overrides: object) -> DiffReport:
    base: dict[str, object] = dict(
        signalforge_version=signalforge.__version__,
        model_unique_id="model.test.x",
        run_id="0123456789abcdef0123456789abcdef",
        duration_seconds=1.25,
        proposed_yaml="version: 2\nmodels: []\n",
        existing_yaml=None,
        unified_diff="--- a\n+++ b\n",
        entries=(),
        kept_count=0,
        kept_uncertain_count=0,
        dropped_count=0,
        flagged_count=0,
        has_existing_schema=False,
        candidate_hash="abcdef0123456789",
        prune_result_hash="0123456789abcdef",
        grading_report_hash=None,
    )
    base.update(overrides)
    return DiffReport(**base)  # type: ignore[arg-type]


def _project(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(project_dir, sidecar_path)`` with an existing project tree.

    ``canonicalise_path`` requires ``project_dir`` to exist (the helper
    calls ``project_dir.resolve(strict=True)``). Set up the canonical
    layout the writer expects: ``<project>/.signalforge/diff.json``.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    sidecar_path = project_dir / ".signalforge" / "diff.json"
    return project_dir, sidecar_path


def test_write_sidecar_happy_path_roundtrips_json(tmp_path: Path) -> None:
    """A simple report serialises to a single JSON document; the file
    bytes parse back via :func:`json.loads` and carry the model's fields.
    """
    project_dir, sidecar_path = _project(tmp_path)
    report = _make_report()

    write_sidecar(report, sidecar_path=sidecar_path, project_dir=project_dir)

    assert sidecar_path.exists()
    raw = sidecar_path.read_bytes()
    # Trailing newline keeps the document POSIX-newline-terminated.
    assert raw.endswith(b"\n")
    decoded = json.loads(raw)
    assert decoded["model_unique_id"] == "model.test.x"
    assert decoded["run_id"] == "0123456789abcdef0123456789abcdef"
    assert decoded["kept_count"] == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only mode bits")
def test_write_sidecar_uses_safe_perms(tmp_path: Path) -> None:
    """The on-disk artefact has mode ``0o600`` so a casual ``ls`` on a
    shared CI runner does not expose the rendered diff to other users.
    Mirrors safety / draft / prune / grade.
    """
    project_dir, sidecar_path = _project(tmp_path)
    write_sidecar(_make_report(), sidecar_path=sidecar_path, project_dir=project_dir)

    mode = stat.S_IMODE(sidecar_path.stat().st_mode)
    assert mode == 0o600


def test_write_sidecar_creates_parent_directory(tmp_path: Path) -> None:
    """The orchestrator may call us against a fresh ``.signalforge/``
    that the user hasn't created. ``mkdir(parents=True, exist_ok=True)``
    is idempotent.
    """
    project_dir, sidecar_path = _project(tmp_path)
    # Confirm the parent does NOT exist before the call.
    assert not sidecar_path.parent.exists()

    write_sidecar(_make_report(), sidecar_path=sidecar_path, project_dir=project_dir)

    assert sidecar_path.parent.is_dir()
    assert sidecar_path.is_file()


def test_write_sidecar_o_trunc_overwrites_cleanly(tmp_path: Path) -> None:
    """A second call against the same path replaces the prior sidecar
    atomically — no leftover bytes from the first write linger past the
    second payload's length. Validates ``O_TRUNC`` semantics (DEC-009 /
    grade-layer.md DEC-006: last-writer-wins).
    """
    project_dir, sidecar_path = _project(tmp_path)
    long_report = _make_report(unified_diff="X" * 5000)

    write_sidecar(long_report, sidecar_path=sidecar_path, project_dir=project_dir)
    first_size = sidecar_path.stat().st_size
    assert first_size > 5000

    short_report = _make_report(unified_diff="short")
    write_sidecar(short_report, sidecar_path=sidecar_path, project_dir=project_dir)

    second_size = sidecar_path.stat().st_size
    assert second_size < first_size
    decoded = json.loads(sidecar_path.read_bytes())
    assert decoded["unified_diff"] == "short"


def test_write_sidecar_calls_fsync(tmp_path: Path) -> None:
    """``os.fsync`` is called exactly once per write so the kernel
    flushes the file's metadata + data to disk before the writer
    returns. Mirrors safety / draft / prune / grade.
    """
    project_dir, sidecar_path = _project(tmp_path)

    real_fsync = __import__("os").fsync
    with patch("signalforge.diff._sidecar.os.fsync", side_effect=real_fsync) as mock_fsync:
        write_sidecar(_make_report(), sidecar_path=sidecar_path, project_dir=project_dir)

    assert mock_fsync.call_count == 1


def test_write_sidecar_oversize_raises_too_large_before_open(tmp_path: Path) -> None:
    """A serialised payload larger than
    :data:`_DIFF_SIDECAR_RECORD_LIMIT_BYTES` raises
    :class:`DiffSidecarRecordTooLargeError` BEFORE any ``os.open`` call;
    no on-disk artefact remains. The size check stubs
    :meth:`DiffReport.model_dump_json` to return an oversize string —
    the cap is enforced on the encoded byte length, not on the model's
    field-level limits, so the boundary holds even when an upstream
    field accidentally grew past its expected size.
    """
    project_dir, sidecar_path = _project(tmp_path)
    report = _make_report()

    oversize = "x" * (_DIFF_SIDECAR_RECORD_LIMIT_BYTES + 1)
    with (
        patch.object(DiffReport, "model_dump_json", return_value=oversize),
        pytest.raises(DiffSidecarRecordTooLargeError) as excinfo,
    ):
        write_sidecar(report, sidecar_path=sidecar_path, project_dir=project_dir)

    # The error reports the actual encoded size and the limit.
    assert excinfo.value.size > _DIFF_SIDECAR_RECORD_LIMIT_BYTES
    assert excinfo.value.limit == _DIFF_SIDECAR_RECORD_LIMIT_BYTES
    # No on-disk artefact at the would-be canonical location.
    assert not sidecar_path.exists()
    # The parent directory should NOT have been created either — the
    # size check fires before ``mkdir`` and ``os.open``.
    assert not sidecar_path.parent.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only symlink semantics")
def test_write_sidecar_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink at ``<project>/.signalforge`` pointing outside the
    project tree is rejected; the writer raises
    :class:`DiffSidecarWriteError` rather than write outside the
    project sandbox. Mirrors prune's post-QG fix and grade's
    writer-entry canonicalisation.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    escape_dir = tmp_path / "escape"
    escape_dir.mkdir()
    # Symlink ``.signalforge`` to a location outside the project tree.
    sf_dir = project_dir / ".signalforge"
    sf_dir.symlink_to(escape_dir, target_is_directory=True)
    sidecar_path = project_dir / ".signalforge" / "diff.json"

    with pytest.raises(DiffSidecarWriteError) as excinfo:
        write_sidecar(
            _make_report(),
            sidecar_path=sidecar_path,
            project_dir=project_dir,
        )

    # The wrap preserves the underlying path-safety failure.
    assert excinfo.value.cause is not None
    # No on-disk artefact at the escape location.
    assert not (escape_dir / "diff.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only symlink semantics")
def test_write_sidecar_rejects_absolute_path_outside_project_dir(tmp_path: Path) -> None:
    """An absolute ``sidecar_path`` outside ``project_dir`` is rejected
    by the gate. This test asserts the load-bearing post-QG-fix
    semantic from ``grade-layer.md``: the orchestrator-supplied
    ``project_dir`` is the containment boundary, NOT a derivation of
    ``sidecar_path``. A caller passing
    ``sidecar_path=/tmp/elsewhere/diff.json, project_dir=<project>``
    must fail loudly.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sidecar_path = elsewhere / "diff.json"

    with pytest.raises(DiffSidecarWriteError):
        write_sidecar(
            _make_report(),
            sidecar_path=sidecar_path,
            project_dir=project_dir,
        )

    assert not sidecar_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only symlink semantics")
def test_write_sidecar_rejects_symlink_loop(tmp_path: Path) -> None:
    """A symlink cycle on the sidecar-path resolution wraps as
    :class:`DiffSidecarWriteError`.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    sf_dir = project_dir / ".signalforge"
    sf_dir.mkdir()
    # Self-referential symlink that ``Path.resolve`` cannot break out of.
    loop = sf_dir / "loop"
    loop.symlink_to(loop)
    sidecar_path = loop / "diff.json"

    with pytest.raises(DiffSidecarWriteError):
        write_sidecar(
            _make_report(),
            sidecar_path=sidecar_path,
            project_dir=project_dir,
        )


def test_write_sidecar_short_write_loops_until_complete(tmp_path: Path) -> None:
    """``os.write`` may return fewer bytes than requested (EINTR or
    short writes on certain filesystems / kernels). The writer loops
    until the full payload lands. Mirrors safety / draft / prune /
    grade.
    """
    project_dir, sidecar_path = _project(tmp_path)
    report = _make_report()
    expected_body = report.model_dump_json(by_alias=True, indent=2) + "\n"
    expected_bytes = expected_body.encode("utf-8")

    real_write = __import__("os").write
    write_calls: list[int] = []

    def short_write(fd: int, data: bytes) -> int:
        # Always write at most 16 bytes per call so the loop has to run
        # many iterations to land the full payload.
        chunk = data[:16]
        n = real_write(fd, chunk)
        write_calls.append(n)
        return n

    with patch("signalforge.diff._sidecar.os.write", side_effect=short_write):
        write_sidecar(report, sidecar_path=sidecar_path, project_dir=project_dir)

    # The loop ran multiple times to cover the full payload.
    assert len(write_calls) > 1
    assert sum(write_calls) == len(expected_bytes)
    # The on-disk content matches the expected payload exactly.
    assert sidecar_path.read_bytes() == expected_bytes


def test_write_sidecar_short_write_zero_bytes_raises(tmp_path: Path) -> None:
    """A zero-byte return from ``os.write`` indicates an unrecoverable
    I/O failure (disk full, etc.) — the writer raises ``OSError``
    rather than spinning forever.
    """
    project_dir, sidecar_path = _project(tmp_path)

    with (
        patch("signalforge.diff._sidecar.os.write", return_value=0),
        pytest.raises(OSError, match="os.write returned 0"),
    ):
        write_sidecar(
            _make_report(),
            sidecar_path=sidecar_path,
            project_dir=project_dir,
        )


def test_write_sidecar_propagates_oserror_raw(tmp_path: Path) -> None:
    """An ``OSError`` from ``os.write`` propagates raw — the writer
    catches **no** exceptions internally around write/fsync. The caller
    (the orchestrator in US-008) is responsible for wrapping with a
    run-context-specific :class:`DiffSidecarWriteError` (mirrors safety
    / draft / prune / grade).
    """
    project_dir, sidecar_path = _project(tmp_path)

    boom = OSError("simulated write failure")
    with (
        patch("signalforge.diff._sidecar.os.write", side_effect=boom),
        pytest.raises(OSError, match="simulated write failure"),
    ):
        write_sidecar(
            _make_report(),
            sidecar_path=sidecar_path,
            project_dir=project_dir,
        )


# ---------------------------------------------------------------------------
# AST defence test (US-007 acceptance criterion)
# ---------------------------------------------------------------------------


def test_sidecar_module_no_except_handler_around_write_fsync() -> None:
    """The writer must not wrap the write/fsync block in any ``except``
    handler — the propagation IS the defence (DEC-006 of grade-layer.md;
    DEC-016 of prune-engine.md; DEC-011 of safety-layer.md). Any
    ``ast.Try`` whose body issues an ``os.write`` or ``os.fsync`` call
    must have ``handlers == []`` (only ``finally`` is permitted, for
    the descriptor release).

    This is the AST defence the US-007 acceptance criteria require:
    "scan ``_sidecar.py`` source for ``try:`` / ``except`` patterns;
    assert only the ``try / finally`` for ``os.close`` exists." Codified
    here as: no ``except`` handler may guard the syscalls that persist
    the receipt; an accidental ``try / except OSError`` around
    ``os.write`` would silently swallow the exact failure mode the
    fail-closed pattern exists to surface.
    """
    sidecar_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "signalforge"
        / "diff"
        / "_sidecar.py"
    )
    tree = ast.parse(sidecar_path.read_text(encoding="utf-8"))

    def _body_calls_os_syscall(body: list[ast.stmt], names: tuple[str, ...]) -> bool:
        """Return True iff any node inside ``body`` issues a top-level
        ``os.<name>`` call (``os.write``, ``os.fsync``).
        """
        for node in body:
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "os"
                    and sub.func.attr in names
                ):
                    return True
        return False

    syscall_names = ("write", "fsync")
    offending: list[int] = []
    syscall_try_count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if _body_calls_os_syscall(node.body, syscall_names):
            syscall_try_count += 1
            if node.handlers:
                offending.append(node.lineno)

    # Exactly one ``Try`` block guards the write/fsync syscalls — the
    # ``try / finally`` around the descriptor close. The sanity check
    # guards against a future refactor that accidentally drops the
    # ``try / finally`` (which would leak the descriptor on a write
    # failure).
    assert syscall_try_count == 1, (
        f"Expected exactly one Try block guarding os.write/os.fsync; found {syscall_try_count}."
    )
    # No ``except`` handler may wrap the syscalls — a handler here
    # would silently swallow the very failure the fail-closed pattern
    # exists to surface.
    assert offending == [], (
        f"Found except-handler(s) around os.write/os.fsync at line(s) "
        f"{offending}. The fail-closed contract requires propagation, not "
        f"suppression — only `try / finally` for os.close is permitted "
        f"around the syscalls."
    )
