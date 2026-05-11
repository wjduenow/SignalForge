"""Layer-neutral symlink-hardened path canonicalisation.

This module is the single canonical home for the project's symlink /
containment defence (the three traps from ``.claude/rules/manifest-readers.md``).
Layers (warehouse, safety, cli, diff, grade, prune) call into here directly
and either catch :class:`PathContainmentError` to re-raise as their own
layer-typed error, or let it propagate to a single boundary catch.

Before issue #43, every cross-package consumer imported
``signalforge.warehouse._path_safety.canonicalise_path`` and pattern-matched on
:class:`signalforge.warehouse.errors.ProfileNotFoundError`. The warehouse name
leaked into grade/diff/prune/cli audit-write paths — a stack trace from a
grade-layer audit write surfaced a warehouse-profile error class. This module
fixes that by exposing a project-neutral typed escape that no layer owns.

The three traps preserved here:

1. Resolve symlinks before checking containment
   (:meth:`pathlib.Path.relative_to` does **not** follow symlinks).
2. Catch :class:`RuntimeError` from :meth:`pathlib.Path.resolve` — it raises
   on symlink cycles regardless of the ``strict=`` flag.
3. Apply the same gate to the *default* path the caller chooses, not just to
   user-supplied overrides — convention is not a security boundary.
"""

from __future__ import annotations

from pathlib import Path


class PathContainmentError(Exception):
    """Raised when a path fails symlink-hardened canonicalisation.

    Layer-neutral typed escape so non-warehouse consumers (cli / diff /
    grade / prune) can catch this at their orchestrator boundary and
    re-raise as their own layer-typed error without importing a
    warehouse-layer name.

    The exception message identifies the specific failure mode (loop,
    missing directory, escape from ``project_dir``); callers that need to
    translate to a layer-typed remediation can use the message verbatim or
    paraphrase it.
    """


def canonicalise_path(input_path: Path | str, project_dir: Path) -> Path:
    """Resolve ``input_path`` to an absolute path inside ``project_dir``'s tree.

    Both the input path and ``project_dir`` are run through
    :meth:`pathlib.Path.resolve` so symlinks are followed before the
    containment check. The default-path argument the caller supplies must
    flow through this helper too — the hardening only holds when both
    user-supplied and default paths use the same gate.

    Raises:
        PathContainmentError: when the resolved path escapes
            ``project_dir`` (so a symlink pointing at ``/etc/passwd`` cannot
            be reached via a relative input), when either path traverses a
            symlink cycle (``Path.resolve`` raises :class:`RuntimeError` on
            cycles regardless of ``strict=``), or when ``project_dir``
            itself does not exist or is not a directory.
    """
    p = Path(input_path)
    try:
        project_resolved = project_dir.resolve(strict=True)
    except RuntimeError as exc:
        raise PathContainmentError(f"project_dir {project_dir} contains a symlink loop") from exc
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise PathContainmentError(
            f"project_dir {project_dir} does not exist or is not a directory"
        ) from exc
    # `Path.resolve(strict=True)` succeeds on an existing regular file, so the
    # "is not a directory" promise in the docstring would silently break for a
    # bare-file `project_dir` (caught by Copilot on PR #72). Explicit guard:
    if not project_resolved.is_dir():
        raise PathContainmentError(
            f"project_dir {project_dir} does not exist or is not a directory"
        )

    if not p.is_absolute():
        p = project_resolved / p
    try:
        resolved = p.resolve(strict=False)
    except RuntimeError as exc:
        raise PathContainmentError(f"path {p} contains a symlink loop") from exc
    if not resolved.is_relative_to(project_resolved):
        raise PathContainmentError(f"path {resolved} escapes project_dir {project_resolved}")
    return resolved
