"""``signalforge cache`` subcommand — grade-cache operations (US-008 / issue #189).

Top-level ``cache`` subcommand with a nested ``clear`` sub-action.
Today's only flag is ``--grade``, which removes the persistent grade
cache at ``<project_dir>/.signalforge/grade-cache/``.

Nested-subparser shape
======================

This is a **documented deviation** from
``.claude/rules/cli-layer.md`` § "Subpackage layout — flat,
per-subcommand modules" (DEC-009). The flat convention says one module
per top-level subcommand; ``cache`` IS one module but it carries a
nested :meth:`argparse.ArgumentParser.add_subparsers` for sub-actions.
Justified per DEC-015 of ``plans/super/189-no-grade-cache.md`` by
forward-compat for a future ``cache clear --drafter`` / ``cache stats``
/ ``cache list`` family — keeping the namespace claim avoids a
hyphenated ``cache-clear`` flat name or a verb-first ``clear-grade-cache``
that loses the ``git remote add / remove`` idiom.

Behaviour (DEC-015)
===================

* ``signalforge cache clear --grade [--project-dir PATH]`` →
  canonicalises ``<project_dir>/.signalforge/grade-cache/`` via
  :func:`signalforge.grade.cache.clear_cache` (which does its own
  symlink-hardening — refuses anything whose canonical form does not
  end with the conventional ``.signalforge/grade-cache`` suffix).
* On success: exit 0. INFO line via the lazy-format JSON logger naming
  the resolved path.
* On a missing cache directory: idempotent — exit 0 with the same
  shape (``clear_cache`` itself logs the no-op).
* On symlink escape: raises :class:`signalforge.grade.GradeCachePathError`
  → exit 1 (tier 1) via the registered mapping in
  :data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` (registered
  by US-004).

Per DEC-015 there is **no** ``--confirm`` flag — the destructive scope
is bounded by ``.signalforge/grade-cache/`` and the operator typed
``--grade`` explicitly. ``rm -rf .signalforge/grade-cache`` remains a
manual escape hatch.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from signalforge.cli._helpers import (
    format_error_to_stderr,
    map_exception_to_exit_code,
    print_stderr,
    setup_logging,
)
from signalforge.cli.errors import CliPathError
from signalforge.grade.cache import clear_cache

__all__ = ["add_parser", "cmd_cache"]


_LOGGER = logging.getLogger("signalforge.cli")


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``cache`` subcommand with a nested ``clear`` sub-action.

    DEC-015 — the nested ``add_subparsers()`` is a documented deviation
    from ``cli-layer.md`` § "Subpackage layout — flat, per-subcommand
    modules" (DEC-009). Forward-compat for a future ``cache clear
    --drafter`` / ``cache stats`` / ``cache list`` family.

    Surface today:

    * ``cache clear --grade`` — remove ``.signalforge/grade-cache/``.
      The ``--grade`` flag is ``required=True`` so a bare
      ``signalforge cache clear`` exits 2 via argparse rather than
      silently no-opping. (When a sibling flag like ``--drafter``
      lands, the ``required`` constraint will need to become a mutex
      group; for v0.6 single-flag the boolean ``required`` is the
      cleanest gate.)
    """
    cache_parser = subparsers.add_parser(
        "cache",
        help="Manage SignalForge caches.",
        description=(
            "Manage SignalForge's operator-side caches. Today's only "
            "sub-action is ``clear --grade``, which removes the "
            "persistent grade cache at "
            "``<project_dir>/.signalforge/grade-cache/``."
        ),
    )
    cache_subparsers = cache_parser.add_subparsers(
        dest="cache_subcommand",
        title="cache sub-actions",
        metavar="<sub-action>",
        required=True,
    )

    clear_parser = cache_subparsers.add_parser(
        "clear",
        help="Clear a SignalForge cache.",
        description=(
            "Remove ``<project_dir>/.signalforge/grade-cache/`` "
            "recursively. Idempotent on a missing directory. "
            "Symlink-hardened: refuses to remove anything whose "
            "canonical path does not end with the conventional "
            "``.signalforge/grade-cache`` suffix."
        ),
    )
    clear_parser.add_argument(
        "--grade",
        action="store_true",
        required=True,
        help=(
            "Clear the grade cache at "
            "<project_dir>/.signalforge/grade-cache/. Required — "
            "every ``cache clear`` invocation must name the cache to "
            "clear explicitly."
        ),
    )
    clear_parser.add_argument(
        "--project-dir",
        metavar="PATH",
        default=None,
        help=(
            "Absolute assertion: <PATH> must contain dbt_project.yml. "
            "When supplied, the CLI does NOT walk up from this path. "
            "Default: walk up from the current working directory."
        ),
    )

    # The dispatcher is wired at the ``cache`` parser level (NOT each
    # sub-action) so adding a future sub-action only requires
    # extending :func:`cmd_cache`'s dispatch.
    cache_parser.set_defaults(func=cmd_cache)


def _resolve_project_dir(args: argparse.Namespace) -> Path:
    """Resolve the dbt project root.

    Mirrors :func:`signalforge.cli.lint._resolve_project_dir` and
    :func:`signalforge.cli.generate._resolve_project_dir` (DEC-001
    walk-up; DEC-027 absolute-assertion under ``--project-dir``). The
    duplication is deliberate per the project's path-safety convention
    (each layer's containment gate stays homogeneous; promotion to a
    shared helper is a future v0.x refinement).
    """
    override = getattr(args, "project_dir", None)
    if override is not None:
        candidate = Path(override).resolve()
        if not candidate.is_dir() or not (candidate / "dbt_project.yml").is_file():
            raise CliPathError(
                f"--project-dir {override!r} does not contain dbt_project.yml",
                remediation=(
                    "Pass a path that points directly at a dbt project root "
                    "(the directory containing dbt_project.yml). The flag is "
                    "an absolute assertion; the CLI does not walk up from it."
                ),
            )
        _LOGGER.debug(
            "resolved project_dir: %s",
            json.dumps({"project_dir": str(candidate), "source": "flag"}),
        )
        return candidate

    cwd = Path.cwd().resolve()
    current: Path | None = cwd
    while current is not None:
        if (current / "dbt_project.yml").is_file():
            _LOGGER.debug(
                "resolved project_dir: %s",
                json.dumps({"project_dir": str(current), "source": "walk-up"}),
            )
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent

    raise CliPathError(
        f"could not find dbt_project.yml walking up from {cwd}",
        remediation=(
            "Run `signalforge cache` from inside a dbt project, or pass "
            "--project-dir <PATH> pointing at the directory that contains "
            "dbt_project.yml."
        ),
    )


def cmd_cache(args: argparse.Namespace) -> int:
    """Dispatch on ``args.cache_subcommand``.

    Today's only sub-action is ``clear``; the dispatcher will grow new
    branches as ``cache stats`` / ``cache list`` etc. land. The single
    ``try / except Exception`` boundary lives inside each sub-action
    handler so a future sibling can shape its own error wrapping
    without touching this dispatch table.

    Returns the integer exit code per the four-tier CLI taxonomy
    (DEC-008 of ``cli-layer.md``).
    """
    sub = getattr(args, "cache_subcommand", None)
    if sub == "clear":
        return _cmd_cache_clear(args)
    # ``required=True`` on the nested subparsers (DEC-015) means argparse
    # itself rejects a missing sub-action before reaching this branch,
    # but defend against a future change that relaxes the constraint.
    print_stderr(f"ERROR: unknown cache sub-action: {sub!r}")
    return 2


def _cmd_cache_clear(args: argparse.Namespace) -> int:
    """Handler for ``signalforge cache clear --grade``.

    Resolves the project root, computes
    ``<project_dir>/.signalforge/grade-cache/``, and delegates to
    :func:`signalforge.grade.cache.clear_cache` (which owns the
    symlink-hardening + idempotent-on-missing-dir semantics per
    DEC-015).

    Returns:

    * ``0`` — cache cleared OR was already absent (idempotent).
    * ``1`` — :class:`signalforge.grade.GradeCachePathError` (symlink
      escape) or :class:`signalforge.cli.errors.CliPathError`
      (``--project-dir`` mistake). Tier 1 (load-time / operator-config
      problem).
    * other — any escaping forward-compat exception routes through
      :func:`map_exception_to_exit_code` (defence in depth — the panic
      catch never returns 0).

    The single ``try / except Exception`` boundary matches DEC-016 (no
    traceback ever leaks); failures route through
    :func:`format_error_to_stderr` so the canonical
    ``ERROR: <message>`` + ``↳ Remediation: <text>`` shape applies
    uniformly with the rest of the CLI.
    """
    # Re-bind the root logger's handler to the current stderr (mirrors
    # ``cmd_generate`` / ``cmd_prune_existing``). The ``force=True`` flag
    # on ``logging.basicConfig`` inside ``setup_logging`` drops any stale
    # handlers left over from a prior run / a prior in-process test, so
    # ``signalforge.grade.cache`` 's INFO line lands on the live stderr
    # rather than a closed handle. Cache subcommand has no
    # ``--verbose`` / ``--quiet`` flags (DEC-015 keeps the surface small);
    # defaults to INFO.
    setup_logging(verbose=False, quiet=False)

    try:
        project_dir = _resolve_project_dir(args)
        cache_dir = project_dir / ".signalforge" / "grade-cache"
        # ``clear_cache`` does its own ``Path.resolve(strict=False)`` +
        # two-layer containment check (DEC-015 / DEC-017): suffix
        # containment AND project-anchor containment when ``project_dir``
        # is supplied. Passing ``project_dir`` here closes the
        # symlink-escape gap where ``<project>/.signalforge/grade-cache``
        # could be a symlink to ``/tmp/.signalforge/grade-cache``
        # (issue #189 QG Pass 1 Finding 2). We do NOT route through
        # ``canonicalise_user_path`` — the lib seam IS the containment
        # gate, and the cache dir may not exist yet (idempotent-missing
        # case), which ``canonicalise_user_path``'s ``strict=True``
        # resolve would mis-handle.
        clear_cache(cache_dir, project_dir=project_dir)
    except (KeyboardInterrupt, SystemExit):
        # Preserve Python's default semantics for operator Ctrl-C and
        # any clean SystemExit raised from within ``clear_cache``
        # (none today, but defensive parity with the rest of the CLI).
        raise
    except Exception as exc:  # noqa: BLE001 — uniform CLI boundary catch (DEC-016)
        print_stderr(format_error_to_stderr(exc))
        return map_exception_to_exit_code(exc)

    return 0
