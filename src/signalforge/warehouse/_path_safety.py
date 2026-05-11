"""Symlink-hardened path canonicalisation for the warehouse subpackage.

Thin wrapper around :func:`signalforge._common.path_safety.canonicalise_path`
(issue #43) — the canonical home for the project's symlink / containment
defence is now layer-neutral. This shim catches the project-neutral
:class:`signalforge._common.path_safety.PathContainmentError` and re-raises
as :class:`signalforge.warehouse.errors.ProfileNotFoundError` so the
warehouse layer's catch surface stays homogeneous: every "we couldn't load
profiles.yml" condition raises a single typed error.

Per ``warehouse-adapters.md`` § "Path safety", layer-typed translation
happens at the helper level (here) so warehouse callers — ``profiles.py``
in particular — don't carry the try/except at every call site.
"""

from __future__ import annotations

from pathlib import Path

from signalforge._common.path_safety import (
    PathContainmentError,
)
from signalforge._common.path_safety import (
    canonicalise_path as _common_canonicalise_path,
)
from signalforge.warehouse.errors import ProfileNotFoundError


def canonicalise_path(input_path: Path | str, project_dir: Path) -> Path:
    """Resolve ``input_path`` to an absolute path inside ``project_dir``'s tree.

    Delegates to :func:`signalforge._common.path_safety.canonicalise_path`
    and translates :class:`PathContainmentError` into
    :class:`ProfileNotFoundError` so warehouse callers keep one typed-error
    catch surface.

    Raises:
        ProfileNotFoundError: when the resolved path escapes
            ``project_dir``, contains a symlink loop, or when
            ``project_dir`` itself does not exist or is not a directory.
    """
    try:
        return _common_canonicalise_path(input_path, project_dir)
    except PathContainmentError as exc:
        # Record the project-scoped location actually validated so the
        # ``ProfileNotFoundError`` carries the right diagnostic (Copilot
        # caught the regression — a relative ``input_path`` like
        # ``"profiles.yml"`` would otherwise land as cwd-relative,
        # losing the project_dir context).
        raw = Path(input_path)
        searched = raw if raw.is_absolute() else (Path(project_dir) / raw)
        raise ProfileNotFoundError(
            searched_paths=[searched],
            remediation=(
                f"{exc}. profiles.yml must point inside the project tree, "
                "must not traverse a symlink loop, and project_dir must "
                "exist as a directory."
            ),
        ) from exc
