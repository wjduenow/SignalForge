"""Public ``signalforge.demo`` subpackage — programmatic access to the
bundled demo project.

Library callers (notebooks, scripts, CI bootstrap) can copy the bundled
``signalforge._demo/`` tree into a fresh directory via
:func:`copy_demo`. The CLI subcommand ``signalforge init-demo``
wraps this function and re-raises the lower-level :class:`DemoError`
subclasses into ``Cli*Error`` wrappers so the CLI exit-code taxonomy
stays homogeneous (DEC-012).

Path-handling note
==================

``copy_demo`` does **not** route ``dest`` through the project-wide
``canonicalise_user_path`` helper — that helper enforces a ``project_dir``
containment boundary appropriate for paths the CLI consumes *inside* an
existing project. ``init-demo`` is the one entry point in the toolchain
that *creates* a project, so the containment gate doesn't apply.

The function still defends against symlink cycles (``.resolve(strict=True)``
raises ``RuntimeError`` on cycles on Python <= 3.12 and ``OSError(ELOOP)`` on
>= 3.13) and refuses, when
``force=True``, to nuke any of ``/``, ``Path.home()``, or ``Path.cwd()``
(DEC-001 — the ``--force`` blast-radius guard).

See ``plans/super/47-init-demo.md`` § US-003 + DEC-001 / DEC-004 / DEC-005
/ DEC-011 / DEC-012 for the full contract.
"""

from __future__ import annotations

import errno
import shutil
from importlib.resources import as_file, files
from pathlib import Path

from signalforge.demo.errors import (
    DemoDestExistsError,
    DemoDestUnsafeError,
    DemoError,
    DemoFixtureMissingError,
    DemoPathError,
)

__all__ = [
    "DemoDestExistsError",
    "DemoDestUnsafeError",
    "DemoError",
    "DemoFixtureMissingError",
    "DemoPathError",
    "copy_demo",
]


def copy_demo(dest: Path | str, *, force: bool = False) -> Path:
    """Copy the bundled ``signalforge._demo/`` tree to ``dest``.

    Parameters
    ----------
    dest:
        Destination directory. Resolved via
        ``Path(dest).expanduser().resolve(strict=True)``, falling back to
        ``resolve(strict=False)`` only when the destination does not exist
        yet (``FileNotFoundError`` / ``NotADirectoryError``) — a relative
        path resolves against the current working directory; ``~`` expands;
        symlinks are followed; cycles raise :class:`DemoPathError`.
    force:
        If ``True``, a non-empty existing destination is replaced
        atomically (``shutil.rmtree`` then ``shutil.copytree``). The
        sanity gate refuses ``force=True`` against ``/``,
        ``Path.home()``, or ``Path.cwd()`` (DEC-001).

    Returns
    -------
    Path
        The resolved destination path (post ``.expanduser().resolve()``).
        Library callers and the CLI's next-steps message both consume
        this for downstream messaging.

    Raises
    ------
    DemoPathError
        Symlink cycle at ``dest``.
    DemoDestUnsafeError
        ``force=True`` against ``/``, ``Path.home()``, or ``Path.cwd()``.
    DemoDestExistsError
        ``dest`` exists, is non-empty, and ``force=False``.
    DemoFixtureMissingError
        The bundled ``_demo/`` tree is missing from the installed
        package (broken install).
    """

    raw = Path(dest)
    expanded_dest = raw.expanduser()
    # Resolve strict=True first so a symlink cycle surfaces on every supported
    # Python: <= 3.12 raises RuntimeError, >= 3.13 raises OSError(ELOOP)
    # (gh-108958). A genuinely missing destination (the common case — the dest
    # dir need not exist yet) raises FileNotFoundError / NotADirectoryError,
    # where we fall back to strict=False. (Under 3.13, strict=False stops at
    # the loop silently and the cycle guard would never fire.)
    try:
        resolved_dest = expanded_dest.resolve(strict=True)
    except RuntimeError as exc:  # Python <= 3.12 symlink cycle
        raise DemoPathError(
            f"failed to resolve destination path {str(raw)!r}: {exc}",
            cause=exc,
        ) from exc
    except (FileNotFoundError, NotADirectoryError):
        # Destination does not exist yet (the common case) — fall back to
        # best-effort resolution. Narrow to these two so a PermissionError /
        # other OSError surfaces instead of being silently downgraded.
        resolved_dest = expanded_dest.resolve(strict=False)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # Python >= 3.13 symlink cycle (gh-108958)
            raise DemoPathError(
                f"failed to resolve destination path {str(raw)!r}: {exc}",
                cause=exc,
            ) from exc
        raise

    # Symlink + --force blast-radius guard. `resolve()` above followed the
    # link, so if we proceeded with `force=True` the existence gate's
    # `shutil.rmtree(resolved_dest)` would delete the symlink TARGET, not
    # the link itself — an unintended external location. Refuse loudly.
    # Without --force the existing behaviour is preserved: a symlink-dest
    # copy follows the link and writes into the target directory (pinned
    # by ``test_copy_demo_with_symlink_dest_resolves_target``). DEC-001
    # extended by QG follow-up to cover the symlink case.
    if expanded_dest.is_symlink() and force:
        raise DemoDestUnsafeError(
            f"refusing to --force-replace symlink destination {str(expanded_dest)!r}: "
            "would follow the link and clobber the resolved target. Remove the "
            "symlink first or pick a different destination."
        )

    # DEC-001 blast-radius guard — only fires under force=True; without
    # force the existence gate below handles the same paths benignly
    # (a non-empty home / cwd / "/" raises DemoDestExistsError instead).
    if force:
        unsafe_targets = {
            Path("/").resolve(),
            Path.home().resolve(),
            Path.cwd().resolve(),
        }
        if resolved_dest in unsafe_targets:
            raise DemoDestUnsafeError(
                f"refusing to --force-replace {str(resolved_dest)!r}: "
                "would clobber a top-level system or user directory"
            )

    # Shape gate — if dest exists but isn't a directory (file, symlink to
    # a file, etc.), treat it as non-empty content. Without this
    # pre-check, the iterdir() below would raise NotADirectoryError
    # which surfaces through the CLI as a misleading
    # "failed to copy demo tree" wrap instead of the clear
    # DemoDestExistsError the operator should see.
    if resolved_dest.exists() and not resolved_dest.is_dir():
        if not force:
            raise DemoDestExistsError(
                f"destination {str(resolved_dest)!r} exists but is not a directory"
            )
        # force=True against a non-directory → remove the file/symlink
        # and fall through to the fresh-dest branch.
        resolved_dest.unlink()

    # Existence gate — non-empty + no force → loud refusal. Empty dirs
    # and non-existent dests both fall through to the copy. We track
    # whether the dest was already a (empty) directory so we can pass
    # ``dirs_exist_ok=True`` to ``shutil.copytree`` for that case.
    dest_existed_empty = False
    if resolved_dest.exists() and any(resolved_dest.iterdir()):
        if not force:
            raise DemoDestExistsError(f"destination {str(resolved_dest)!r} exists and is not empty")
        # force=True with non-empty dest → atomic replace.
        shutil.rmtree(resolved_dest)
    elif resolved_dest.is_dir():
        # Empty existing directory — copytree refuses to clobber the dir
        # by default, so we opt into ``dirs_exist_ok`` for this branch.
        dest_existed_empty = True

    # Source lookup via importlib.resources — handles editable installs,
    # wheel installs, and zipapp/zipimport cases. ``as_file`` materialises
    # zip-extracted resources to a real Path; for filesystem installs it's
    # an effective no-op. All file I/O is performed inside the ``with``
    # block so the materialised path is valid for the duration of the copy.
    source_ref = files("signalforge").joinpath("_demo")
    if not source_ref.is_dir():
        raise DemoFixtureMissingError(
            "bundled signalforge._demo/ tree not found in the installed package"
        )

    with as_file(source_ref) as source_path:
        # symlinks=False: follow symlinks during the copy (copy contents,
        # not the link itself). DEC-005 + the parity test pins zero
        # symlinks in the shipped tree, so this codifies the no-symlink
        # policy: if a symlink ever sneaks in, the consumer gets a real
        # file at the other end.
        shutil.copytree(
            source_path,
            resolved_dest,
            symlinks=False,
            dirs_exist_ok=dest_existed_empty,
        )

    return resolved_dest
