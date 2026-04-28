"""Symlink-hardened path canonicalisation for the warehouse subpackage.

This is the warehouse-side counterpart to
:func:`signalforge.manifest.loader._canonicalise_path`. Per the
:doc:`/.claude/rules/manifest-readers` rule, every reader that derives a
filesystem path from user-supplied input must:

1. Resolve symlinks before checking containment
   (:meth:`pathlib.Path.relative_to` does **not** follow symlinks).
2. Catch :class:`RuntimeError` from :meth:`pathlib.Path.resolve` — it raises
   on symlink cycles regardless of the ``strict=`` flag.
3. Apply the same gate to the *default* path the loader chooses, not just to
   user-supplied overrides — convention is not a security boundary.

The function is a near-clone of the manifest loader's helper. DEC-017 in the
warehouse plan deliberately keeps the two copies decoupled rather than
hoisting a shared helper into a utility module — promotion is a US-014
follow-up. The escape / cycle exception is :class:`ProfileNotFoundError`
here (instead of ``ModelPathOutsideProjectError``) so the warehouse layer's
catch surface stays homogeneous: every "we couldn't load profiles.yml"
condition raises a single typed error.
"""

from __future__ import annotations

from pathlib import Path

from signalforge.warehouse.errors import ProfileNotFoundError


def canonicalise_path(input_path: Path | str, project_dir: Path) -> Path:
    """Resolve ``input_path`` to an absolute path inside ``project_dir``'s tree.

    Both the input path and ``project_dir`` are run through
    :meth:`pathlib.Path.resolve` so symlinks are followed before the
    containment check. The default-path argument the caller supplies must
    flow through this helper too — DEC-007's hardening only holds when both
    user-supplied and default paths use the same gate.

    Raises:
        ProfileNotFoundError: when the resolved path escapes
            ``project_dir`` (so a symlink pointing at ``/etc/passwd`` cannot
            be reached via a relative input), or when either path traverses
            a symlink cycle (``Path.resolve`` raises :class:`RuntimeError`
            on cycles regardless of ``strict=``).
    """
    p = Path(input_path)
    try:
        project_resolved = project_dir.resolve(strict=True)
    except RuntimeError as exc:
        raise ProfileNotFoundError(
            searched_paths=[project_dir],
            remediation=(
                f"project_dir {project_dir} contains a symlink loop; resolve the "
                "loop or pass a different project directory."
            ),
        ) from exc
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ProfileNotFoundError(
            searched_paths=[project_dir],
            remediation=(f"project_dir {project_dir} does not exist or is not a directory."),
        ) from exc

    if not p.is_absolute():
        p = project_resolved / p
    try:
        resolved = p.resolve(strict=False)
    except RuntimeError as exc:
        raise ProfileNotFoundError(
            searched_paths=[p],
            remediation=(
                f"Path {p} contains a symlink loop; resolve the loop or remove "
                "the offending symlink."
            ),
        ) from exc
    if not resolved.is_relative_to(project_resolved):
        raise ProfileNotFoundError(
            searched_paths=[resolved],
            remediation=(
                f"Path {resolved} escapes project_dir {project_resolved}; "
                "profiles.yml symlinks must point inside the project tree."
            ),
        )
    return resolved
