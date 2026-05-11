"""Unit tests for :mod:`signalforge._common.path_safety` (issue #43).

The helper is consumed by every pipeline stage (cli/diff/grade/prune) plus
the warehouse/safety layer wrappers. The cross-package consumers catch the
layer-neutral :class:`PathContainmentError` directly; the warehouse/safety
wrappers re-raise as their own typed error. This file pins the four
failure modes at the source so a regression surfaces here even if every
layer wrapper still translates correctly.

The three traps from ``.claude/rules/manifest-readers.md`` are exercised:

1. Resolve symlinks BEFORE checking containment.
2. Catch :class:`RuntimeError` from :meth:`pathlib.Path.resolve` on cycles.
3. Apply the gate to the *default* path the caller chooses, not just to
   user-supplied overrides — exercised via the relative-path branch.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from signalforge._common.path_safety import PathContainmentError, canonicalise_path


@pytest.mark.unit
def test_relative_path_resolves_under_project(tmp_path: Path) -> None:
    """A relative input resolves under the project_dir's canonical tree."""
    project = tmp_path / "project"
    project.mkdir()
    target = project / "subdir" / "file.txt"
    target.parent.mkdir()
    target.write_text("x")

    result = canonicalise_path("subdir/file.txt", project)

    assert result == target.resolve()


@pytest.mark.unit
def test_absolute_path_inside_project_resolves(tmp_path: Path) -> None:
    """An absolute input pointing inside project_dir is accepted."""
    project = tmp_path / "project"
    project.mkdir()
    target = project / ".signalforge" / "audit.jsonl"

    result = canonicalise_path(target, project)

    assert result == target.resolve()


@pytest.mark.unit
def test_absolute_path_outside_project_raises(tmp_path: Path) -> None:
    """An absolute path escaping project_dir raises :class:`PathContainmentError`."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside" / "etc.txt"
    outside.parent.mkdir()

    with pytest.raises(PathContainmentError, match="escapes project_dir"):
        canonicalise_path(outside, project)


@pytest.mark.unit
def test_symlink_pointing_outside_project_raises(tmp_path: Path) -> None:
    """A symlink resolved to an out-of-tree target raises :class:`PathContainmentError`."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside_file.txt"
    outside.write_text("evil")

    link = project / "linked.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlinks unsupported on this filesystem")

    with pytest.raises(PathContainmentError, match="escapes project_dir"):
        canonicalise_path("linked.txt", project)


@pytest.mark.unit
def test_input_symlink_loop_raises(tmp_path: Path) -> None:
    """A symlink loop on the input path raises :class:`PathContainmentError`."""
    project = tmp_path / "project"
    project.mkdir()
    loop_a = project / "a"
    loop_b = project / "b"
    try:
        os.symlink(loop_b, loop_a)
        os.symlink(loop_a, loop_b)
    except OSError:
        pytest.skip("symlinks unsupported on this filesystem")

    with pytest.raises(PathContainmentError, match="symlink loop"):
        canonicalise_path("a", project)


@pytest.mark.unit
def test_missing_project_dir_raises(tmp_path: Path) -> None:
    """A non-existent ``project_dir`` raises :class:`PathContainmentError`."""
    missing = tmp_path / "does-not-exist"

    with pytest.raises(PathContainmentError, match="does not exist"):
        canonicalise_path("any.txt", missing)


@pytest.mark.unit
def test_project_dir_is_a_regular_file_raises(tmp_path: Path) -> None:
    """A bare-file ``project_dir`` raises :class:`PathContainmentError`.

    ``Path.resolve(strict=True)`` succeeds on an existing regular file
    (it only raises on missing paths), so without an explicit
    :meth:`Path.is_dir` guard the helper would silently accept a file
    as ``project_dir``. Pinned per Copilot review on PR #72.
    """
    not_a_dir = tmp_path / "regular_file"
    not_a_dir.write_text("x")

    with pytest.raises(PathContainmentError, match="does not exist or is not a directory"):
        canonicalise_path("any.txt", not_a_dir)


@pytest.mark.unit
def test_project_dir_with_file_in_path_raises(tmp_path: Path) -> None:
    """A ``project_dir`` whose path traverses a regular file raises
    :class:`PathContainmentError` (the ``NotADirectoryError`` branch
    of :meth:`pathlib.Path.resolve`).
    """
    regular_file = tmp_path / "regular_file"
    regular_file.write_text("x")
    bogus_project = regular_file / "subdir"  # nested under a non-dir

    with pytest.raises(PathContainmentError, match="does not exist or is not a directory"):
        canonicalise_path("any.txt", bogus_project)


@pytest.mark.unit
def test_project_dir_symlink_loop_raises(tmp_path: Path) -> None:
    """A symlink loop on ``project_dir`` itself raises :class:`PathContainmentError`."""
    loop_a = tmp_path / "loop_a"
    loop_b = tmp_path / "loop_b"
    try:
        os.symlink(loop_b, loop_a)
        os.symlink(loop_a, loop_b)
    except OSError:
        pytest.skip("symlinks unsupported on this filesystem")

    with pytest.raises(PathContainmentError, match="symlink loop"):
        canonicalise_path("any.txt", loop_a)


@pytest.mark.unit
def test_path_containment_error_subclasses_exception() -> None:
    """``PathContainmentError`` is a plain :class:`Exception` subclass.

    Issue #43 — load-bearing invariant. The class deliberately does NOT
    subclass a layer-specific error hierarchy (e.g. ``WarehouseError``).
    """
    assert issubclass(PathContainmentError, Exception)
    # No layer hierarchy in the MRO above ``Exception``.
    bases = [cls.__name__ for cls in PathContainmentError.__mro__]
    assert "WarehouseError" not in bases
    assert "SafetyError" not in bases
