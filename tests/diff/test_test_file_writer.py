"""Tests for the injection-safe filename builder + fail-closed ``.sql`` writer.

US-011 of #116 (DEC-010, DEC-014). Covers:

* :func:`anchor_to_filename` — exhaustive adversarial inputs (path-escape,
  separators, control chars, NUL, absolute paths, empty components).
* :func:`write_test_file` — 0o600 perms, parent ``mkdir``, header marker,
  ``O_TRUNC`` overwrite, size cap (before any file open), symlink /
  containment rejection, and the module-local AST no-except-around-write
  shape (mirrors ``tests/diff/test_sidecar.py``).
* Exit-code registration for the two new typed errors.
"""

from __future__ import annotations

import ast
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from signalforge.cli._helpers import map_exception_to_exit_code
from signalforge.diff._test_file_writer import (
    _DIFF_TEST_FILE_RECORD_LIMIT_BYTES,
    _GENERATED_MARKER_PREFIX,
    anchor_to_filename,
    write_test_file,
)
from signalforge.diff.errors import (
    DiffTestFileRecordTooLargeError,
    DiffTestFileWriteError,
)

# ---------------------------------------------------------------------------
# anchor_to_filename — happy path
# ---------------------------------------------------------------------------


def test_anchor_to_filename_happy_path() -> None:
    """A clean anchor produces ``tests/<model>__<descriptor>_<hash>.sql``."""
    name = anchor_to_filename(
        model_name="stg_orders",
        descriptor="total_amount_positive",
        args_hash="a1b2c3d4",
    )
    assert name == "tests/stg_orders__total_amount_positive_a1b2c3d4.sql"


def test_anchor_to_filename_always_relative_under_tests() -> None:
    """The result is always a relative path rooted at ``tests/`` — never absolute."""
    name = anchor_to_filename(model_name="m", descriptor="d", args_hash="h")
    assert not name.startswith("/")
    assert name.startswith("tests/")
    assert name.endswith(".sql")


# ---------------------------------------------------------------------------
# anchor_to_filename — adversarial inputs (path-escape / injection)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "..",
        "../sibling",
        "/absolute/path",
        "a/b/c",
        "back\\slash",
        "with space",
        "semi;colon",
        "tab\tchar",
        "new\nline",
        "carriage\rreturn",
        "nul\x00byte",
        "ansi\x1b[31mred",
        "dot.dot.dot",
        "héllo",
        "emoji😀here",
        "pipe|char",
        "$(whoami)",
        "`backtick`",
        "quote'd",
        'double"quote',
    ],
)
def test_anchor_to_filename_neutralises_hostile_model_name(hostile: str) -> None:
    """A hostile model_name cannot inject a separator or traversal token.

    The slug collapses every non-alphanumeric run to ``_`` so the result
    has exactly the ``tests/<model>__<descriptor>_<hash>.sql`` shape: one
    ``tests/`` prefix and one component below it. No ``..``, no extra ``/``,
    no ``\\``, no control chars, no NUL.
    """
    name = anchor_to_filename(model_name=hostile, descriptor="desc", args_hash="hash")
    # Exactly one slash separator (the ``tests/`` prefix).
    assert name.count("/") == 1
    assert name.startswith("tests/")
    assert name.endswith(".sql")
    # No traversal token survives.
    assert ".." not in name
    # No backslash, control char, or NUL.
    assert "\\" not in name
    for ch in name:
        assert ch == "/" or ch == "." or ch.isalnum() or ch == "_", repr(ch)
    assert "\x00" not in name
    assert "\x1b" not in name


def test_anchor_to_filename_neutralises_hostile_descriptor() -> None:
    """A hostile descriptor (LLM-emitted) is slugged the same way."""
    name = anchor_to_filename(
        model_name="m",
        descriptor="../../escape/attempt",
        args_hash="h",
    )
    assert name.count("/") == 1
    assert ".." not in name
    assert name.startswith("tests/")


def test_anchor_to_filename_neutralises_hostile_hash() -> None:
    """A hostile args_hash component is slugged too (belt-and-braces)."""
    name = anchor_to_filename(
        model_name="m",
        descriptor="d",
        args_hash="../h/../x",
    )
    assert name.count("/") == 1
    assert ".." not in name


def test_anchor_to_filename_empty_components_get_placeholder() -> None:
    """All-non-alphanumeric (or empty) components become ``_``, never empty."""
    name = anchor_to_filename(model_name="...", descriptor="", args_hash="///")
    # Each component slugs to "" then is replaced with "_". The template
    # ``tests/{m}__{d}_{h}.sql`` with all three = "_" yields six underscores
    # (m=_, "__", d=_, "_", h=_).
    assert name == "tests/______.sql"
    # Result is still relative, under tests/, and carries no separator below.
    assert name.startswith("tests/")
    assert name.count("/") == 1


def test_anchor_to_filename_is_deterministic() -> None:
    """Same anchor → same filename (reproducibility — Architectural Commitment #5)."""
    a = anchor_to_filename(model_name="m", descriptor="d", args_hash="h")
    b = anchor_to_filename(model_name="m", descriptor="d", args_hash="h")
    assert a == b


# ---------------------------------------------------------------------------
# write_test_file — happy path, perms, mkdir, marker, overwrite
# ---------------------------------------------------------------------------


def test_write_test_file_writes_with_marker_and_body(tmp_path: Path) -> None:
    """The file carries the ``-- signalforge:generated <hash>`` header marker."""
    rel = anchor_to_filename(model_name="m", descriptor="d", args_hash="deadbeef")
    written = write_test_file(
        "SELECT 1 AS failures",
        relative_path=rel,
        args_hash="deadbeef",
        project_dir=tmp_path,
    )
    content = written.read_text(encoding="utf-8")
    assert content.startswith(f"{_GENERATED_MARKER_PREFIX} deadbeef\n")
    assert "SELECT 1 AS failures\n" in content


def test_write_test_file_returns_canonical_path_inside_project(tmp_path: Path) -> None:
    """The returned path is absolute, inside the project, and ends ``.sql``."""
    rel = anchor_to_filename(model_name="m", descriptor="d", args_hash="h")
    written = write_test_file("SELECT 1", relative_path=rel, args_hash="h", project_dir=tmp_path)
    assert written.is_absolute()
    assert written.resolve().is_relative_to(tmp_path.resolve())
    assert written.name.endswith(".sql")


def test_write_test_file_perms_are_owner_only(tmp_path: Path) -> None:
    """The file is created mode 0o600 (owner read/write only)."""
    rel = anchor_to_filename(model_name="m", descriptor="d", args_hash="h")
    written = write_test_file("SELECT 1", relative_path=rel, args_hash="h", project_dir=tmp_path)
    mode = stat.S_IMODE(os.stat(written).st_mode)
    assert mode == 0o600, oct(mode)


def test_write_test_file_creates_parent_tests_dir(tmp_path: Path) -> None:
    """The ``tests/`` directory is created if it does not exist (mkdir -p)."""
    assert not (tmp_path / "tests").exists()
    rel = anchor_to_filename(model_name="m", descriptor="d", args_hash="h")
    write_test_file("SELECT 1", relative_path=rel, args_hash="h", project_dir=tmp_path)
    assert (tmp_path / "tests").is_dir()


def test_write_test_file_appends_trailing_newline(tmp_path: Path) -> None:
    """SQL without a trailing newline gets one (POSIX-newline-terminated)."""
    rel = anchor_to_filename(model_name="m", descriptor="d", args_hash="h")
    written = write_test_file("SELECT 1", relative_path=rel, args_hash="h", project_dir=tmp_path)
    assert written.read_text(encoding="utf-8").endswith("\n")


def test_write_test_file_overwrites_via_o_trunc(tmp_path: Path) -> None:
    """A re-write replaces the prior content atomically (O_TRUNC)."""
    rel = anchor_to_filename(model_name="m", descriptor="d", args_hash="h")
    write_test_file(
        "SELECT 1 AS old_long_content_here",
        relative_path=rel,
        args_hash="h",
        project_dir=tmp_path,
    )
    written = write_test_file("SELECT 2", relative_path=rel, args_hash="h", project_dir=tmp_path)
    content = written.read_text(encoding="utf-8")
    assert "old_long_content_here" not in content
    assert "SELECT 2" in content


# ---------------------------------------------------------------------------
# write_test_file — size cap (before any file open)
# ---------------------------------------------------------------------------


def test_write_test_file_size_cap_raises_before_open(tmp_path: Path) -> None:
    """An oversize payload raises before any file is created (leaves no artefact)."""
    rel = anchor_to_filename(model_name="m", descriptor="d", args_hash="h")
    huge = "X" * (_DIFF_TEST_FILE_RECORD_LIMIT_BYTES + 1)
    with pytest.raises(DiffTestFileRecordTooLargeError) as exc_info:
        write_test_file(huge, relative_path=rel, args_hash="h", project_dir=tmp_path)
    assert exc_info.value.limit == _DIFF_TEST_FILE_RECORD_LIMIT_BYTES
    # No on-disk artefact — neither the file nor the tests/ dir is created
    # (the size check precedes both mkdir and os.open).
    assert not (tmp_path / rel).exists()
    assert not (tmp_path / "tests").exists()


def test_write_test_file_size_cap_override(tmp_path: Path) -> None:
    """The ``size_limit_bytes`` override is honoured."""
    rel = anchor_to_filename(model_name="m", descriptor="d", args_hash="h")
    with pytest.raises(DiffTestFileRecordTooLargeError):
        write_test_file(
            "SELECT 1 FROM somewhere",
            relative_path=rel,
            args_hash="h",
            project_dir=tmp_path,
            size_limit_bytes=5,
        )


# ---------------------------------------------------------------------------
# write_test_file — symlink / containment rejection
# ---------------------------------------------------------------------------


def test_write_test_file_rejects_symlink_escape(tmp_path: Path) -> None:
    """A ``tests/`` symlink pointing outside the project is rejected, fail-closed."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # Symlink project/tests -> outside (escapes the project tree).
    (project / "tests").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DiffTestFileWriteError) as exc_info:
        write_test_file(
            "SELECT 1",
            relative_path="tests/m__d_h.sql",
            args_hash="h",
            project_dir=project,
        )
    # The original PathContainmentError is preserved as the cause.
    assert exc_info.value.cause is not None
    assert exc_info.value.__cause__ is exc_info.value.cause


def test_write_test_file_rejects_relative_path_escape(tmp_path: Path) -> None:
    """A relative_path that escapes the project tree is rejected."""
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(DiffTestFileWriteError):
        write_test_file(
            "SELECT 1",
            relative_path="../escape.sql",
            args_hash="h",
            project_dir=project,
        )


# ---------------------------------------------------------------------------
# write_test_file — short-write loop guard (mirrors sidecar)
# ---------------------------------------------------------------------------


def test_write_test_file_short_write_zero_bytes_raises(tmp_path: Path) -> None:
    """A zero-byte return from ``os.write`` indicates an unrecoverable I/O
    failure (disk full, etc.) — the writer raises ``OSError`` rather than
    spinning forever. Mirrors ``tests/diff/test_sidecar.py``."""
    rel = anchor_to_filename(model_name="m", descriptor="d", args_hash="h")
    with (
        patch("signalforge.diff._test_file_writer.os.write", return_value=0),
        pytest.raises(OSError, match="os.write returned 0"),
    ):
        write_test_file("SELECT 1", relative_path=rel, args_hash="h", project_dir=tmp_path)


# ---------------------------------------------------------------------------
# Exit-code registration (DEC-014, scan 7 backstop)
# ---------------------------------------------------------------------------


def test_new_errors_map_to_tier_3() -> None:
    """Both new write-path errors map to tier 3 (write-durability)."""
    assert map_exception_to_exit_code(DiffTestFileWriteError("boom", cause=OSError("io"))) == 3
    assert map_exception_to_exit_code(DiffTestFileRecordTooLargeError(size=2, limit=1)) == 3


# ---------------------------------------------------------------------------
# AST defence — no except around write/fsync in this module (mirrors sidecar)
# ---------------------------------------------------------------------------


def test_module_no_except_handler_around_write_fsync() -> None:
    """The writer module must propagate raw exceptions from os.write/os.fsync.

    Module-local mirror of the project-wide scan 8 in
    ``tests/test_audit_completeness.py``: any ``ast.Try`` whose body issues
    an ``os.write`` / ``os.fsync`` must have ``handlers == []`` (only a
    ``finally`` for the descriptor release is permitted).
    """
    module_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "signalforge"
        / "diff"
        / "_test_file_writer.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    def _calls_syscall(body: list[ast.stmt]) -> bool:
        for node in body:
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "os"
                    and sub.func.attr in ("write", "fsync")
                ):
                    return True
        return False

    syscall_try_count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and _calls_syscall(node.body):
            syscall_try_count += 1
            assert node.handlers == [], (
                f"except handler around os.write/os.fsync at line {node.lineno} — "
                "the fail-closed contract requires propagation, not suppression."
            )
    # Exactly one writer function → exactly one such Try block.
    assert syscall_try_count == 1
