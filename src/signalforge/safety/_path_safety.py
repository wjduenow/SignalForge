"""Symlink-hardened path canonicalisation for the safety subpackage.

Thin wrapper around :func:`signalforge._common.path_safety.canonicalise_path`
(issue #43) — the canonical home for the project's symlink / containment
defence is now layer-neutral. This shim catches the project-neutral
:class:`signalforge._common.path_safety.PathContainmentError` and re-raises
as :class:`signalforge.safety.errors.InvalidConfigError` so the safety
layer's catch surface stays homogeneous: every "we couldn't load the
safety config" condition raises a single typed error.

Per ``warehouse-adapters.md`` § "Path safety", layer-typed translation
happens at the helper level (here) so safety callers — ``safety/config.py``
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
from signalforge.safety.errors import InvalidConfigError


def canonicalise_path(input_path: Path | str, project_dir: Path) -> Path:
    """Resolve ``input_path`` to an absolute path inside ``project_dir``'s tree.

    Delegates to :func:`signalforge._common.path_safety.canonicalise_path`
    and translates :class:`PathContainmentError` into
    :class:`InvalidConfigError` so safety callers keep one typed-error
    catch surface.

    Raises:
        InvalidConfigError: when the resolved path escapes ``project_dir``,
            contains a symlink loop, or when ``project_dir`` itself does
            not exist or is not a directory.
    """
    try:
        return _common_canonicalise_path(input_path, project_dir)
    except PathContainmentError as exc:
        raise InvalidConfigError(
            message=str(exc),
            remediation=(
                f"{exc}. audit_path must point inside the project tree "
                "(default: .signalforge/audit.jsonl), must not traverse a "
                "symlink loop, and project_dir must exist as a directory."
            ),
        ) from exc
