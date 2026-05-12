"""Public ``signalforge.demo`` module — programmatic access to the bundled
demo project.

Library callers (notebooks, scripts, CI bootstrap) can copy the bundled
``signalforge._demo/`` tree into a fresh directory via
:func:`copy_demo`. The CLI subcommand ``signalforge init-demo`` (US-004)
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

The function still defends against symlink cycles (``.resolve()`` raises
``RuntimeError`` on cycles regardless of ``strict=``) and refuses, when
``force=True``, to nuke any of ``/``, ``Path.home()``, or ``Path.cwd()``
(DEC-001 — the ``--force`` blast-radius guard).

See ``plans/super/47-init-demo.md`` § US-003 + DEC-001 / DEC-004 / DEC-005
/ DEC-011 / DEC-012 for the full contract.
"""

from __future__ import annotations

import shutil
from importlib.resources import as_file, files
from pathlib import Path

__all__ = [
    "DemoDestExistsError",
    "DemoDestUnsafeError",
    "DemoError",
    "DemoFixtureMissingError",
    "DemoPathError",
    "copy_demo",
]


# ---------------------------------------------------------------------------
# Error hierarchy — mirrors the layer-base pattern in ``signalforge.cli.errors``
# and the other v0.1 stage error modules: every class carries an optional
# ``remediation`` field; ``__str__`` renders ``message`` plus a
# ``↳ Remediation: <text>`` line when remediation is set. The CLI (US-004)
# wraps each subclass into a ``Cli*Error`` so the CLI exit-code taxonomy
# stays homogeneous (DEC-012).
# ---------------------------------------------------------------------------


class DemoError(Exception):
    """Base class for ``signalforge.demo`` errors.

    Instances may pass ``remediation=`` to override the (otherwise absent)
    footer; ``cause=`` is preserved for the CLI layer's traceback-free
    error formatter to surface if desired. Subclasses define a
    ``default_remediation`` class attribute which is used when no
    explicit ``remediation`` is provided.
    """

    default_remediation: str | None = None

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation if remediation is not None else self.default_remediation
        self.cause = cause

    def __str__(self) -> str:
        if self.remediation is None:
            return self.message
        return f"{self.message}\n  ↳ Remediation: {self.remediation}"


class DemoPathError(DemoError):
    """Raised when the destination path cannot be canonicalised.

    Currently fires on symlink-cycle detection (``Path.resolve()`` raises
    ``RuntimeError`` regardless of ``strict=`` on a cyclic chain). The
    triggering ``RuntimeError`` rides on the ``cause`` kwarg.
    """

    default_remediation = "Remove the symlink cycle at the destination or pick a different path."


class DemoDestExistsError(DemoError):
    """Raised when ``dest`` exists and is non-empty and ``force=False``.

    Empty existing directories proceed without ``--force`` — the gate
    only fires when there is content that ``copy_demo`` would otherwise
    have to either merge into (unsafe) or replace (requires explicit
    opt-in).
    """

    default_remediation = "Remove the destination or pass force=True (CLI: --force) to replace it."


class DemoDestUnsafeError(DemoError):
    """Raised when ``force=True`` would target a catastrophic path.

    Refuses ``/``, ``Path.home()``, and ``Path.cwd()`` per DEC-001 — the
    blast-radius guard for ``signalforge init-demo --force``. Without the
    refusal, ``--force ~`` would ``rmtree($HOME)``.
    """

    default_remediation = (
        "Pick a fresh subdirectory rather than '/', $HOME, or the current working directory."
    )


class DemoFixtureMissingError(DemoError):
    """Raised when ``importlib.resources`` cannot locate the bundled
    ``_demo/`` tree.

    Indicates a broken install — the wheel target packaging should
    always ship ``src/signalforge/_demo/`` (``python-build.md`` DEC-011
    + plan DEC-011). The CLI maps this to tier 1.
    """

    default_remediation = (
        "Reinstall signalforge-dbt — the bundled demo tree is missing from your install."
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def copy_demo(dest: Path | str, *, force: bool = False) -> Path:
    """Copy the bundled ``signalforge._demo/`` tree to ``dest``.

    Parameters
    ----------
    dest:
        Destination directory. Resolved via
        ``Path(dest).expanduser().resolve(strict=False)`` — a relative
        path resolves against the current working directory; ``~``
        expands; symlinks are followed; cycles raise
        :class:`DemoPathError`.
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
    try:
        resolved_dest = raw.expanduser().resolve(strict=False)
    except RuntimeError as exc:  # symlink cycle
        raise DemoPathError(
            f"failed to resolve destination path {str(raw)!r}: {exc}",
            cause=exc,
        ) from exc

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
    # an effective no-op.
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
