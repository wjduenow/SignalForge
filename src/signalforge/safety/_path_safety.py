"""Symlink-hardened path canonicalisation for the safety subpackage.

This is the safety-side counterpart to
:func:`signalforge.warehouse._path_safety.canonicalise_path` and
:func:`signalforge.manifest.loader._canonicalise_path`. Per the
``warehouse-adapters.md`` rule "Path safety: duplicated, not extracted",
each layer keeps its own copy so the layer's catch surface stays
homogeneous: every "we couldn't load the safety config" condition raises
:class:`signalforge.safety.errors.InvalidConfigError`.

The three traps from ``manifest-readers.md`` apply:

1. Resolve symlinks before checking containment
   (:meth:`pathlib.Path.relative_to` does **not** follow symlinks).
2. Catch :class:`RuntimeError` from :meth:`pathlib.Path.resolve` — it raises
   on symlink cycles regardless of the ``strict=`` flag.
3. Apply the same gate to the *default* path the loader chooses, not just to
   user-supplied overrides — convention is not a security boundary.
"""

from __future__ import annotations

from pathlib import Path

from signalforge.safety.errors import InvalidConfigError


def canonicalise_path(input_path: Path | str, project_dir: Path) -> Path:
    """Resolve ``input_path`` to an absolute path inside ``project_dir``'s tree.

    Both the input path and ``project_dir`` are run through
    :meth:`pathlib.Path.resolve` so symlinks are followed before the
    containment check. The default-path argument the caller supplies must
    flow through this helper too — DEC-013's hardening only holds when both
    user-supplied and default paths use the same gate.

    Raises:
        InvalidConfigError: when the resolved path escapes ``project_dir``
            (so a symlink pointing at ``/etc/passwd`` cannot be reached via a
            relative input), when either path traverses a symlink cycle
            (``Path.resolve`` raises :class:`RuntimeError` on cycles
            regardless of ``strict=``), or when ``project_dir`` itself does
            not exist or is not a directory.
    """
    p = Path(input_path)
    try:
        project_resolved = project_dir.resolve(strict=True)
    except RuntimeError as exc:
        raise InvalidConfigError(
            message=f"project_dir {project_dir} contains a symlink loop",
            remediation=(
                f"project_dir {project_dir} contains a symlink loop; resolve the "
                "loop or pass a different project directory."
            ),
        ) from exc
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise InvalidConfigError(
            message=f"project_dir {project_dir} does not exist or is not a directory",
            remediation=(f"project_dir {project_dir} does not exist or is not a directory."),
        ) from exc

    if not p.is_absolute():
        p = project_resolved / p
    try:
        resolved = p.resolve(strict=False)
    except RuntimeError as exc:
        raise InvalidConfigError(
            message=f"Path {p} contains a symlink loop",
            remediation=(
                f"Path {p} contains a symlink loop; resolve the loop or remove "
                "the offending symlink."
            ),
        ) from exc
    if not resolved.is_relative_to(project_resolved):
        raise InvalidConfigError(
            message=f"Path {resolved} escapes project_dir {project_resolved}",
            remediation=(
                f"Path {resolved} escapes project_dir {project_resolved}; "
                "audit_path must point inside the project tree "
                "(default: .signalforge/audit.jsonl)."
            ),
        )
    return resolved
