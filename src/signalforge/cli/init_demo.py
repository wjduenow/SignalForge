"""``signalforge init-demo`` subcommand (US-004 — issue #47).

Copies the bundled ``signalforge._demo/`` tree into a destination directory
so a first-run operator can ``cd`` into a working dbt project and run
``signalforge lint`` / ``signalforge generate --dry-run`` against
``bigquery-public-data.austin_bikeshare.bikeshare_trips`` without first
authoring their own project. Wraps :func:`signalforge.demo.copy_demo` (the
public library entry point, US-003) and re-raises the four
:class:`signalforge.demo.DemoError` subclasses at the handler boundary as
``CliInitDemo*Error`` wrappers so the CLI's four-tier exit-code taxonomy
stays homogeneous (DEC-012).

Path-handling note
==================

``init-demo`` is the one subcommand that *creates* a project rather than
operating *inside* one, so it deliberately does **not** route ``dest``
through :func:`signalforge.cli._helpers.canonicalise_user_path` — that
helper enforces a ``project_dir`` containment boundary appropriate for
paths consumed inside an existing project (DEC-004 of
``plans/super/47-init-demo.md``). Symlink-cycle defence still applies:
:func:`signalforge.demo.copy_demo` resolves ``dest`` via
``Path(dest).expanduser().resolve(strict=False)`` and raises
:class:`signalforge.demo.DemoPathError` on a cycle; the handler wraps
that into :class:`signalforge.cli.errors.CliPathError`.

Next-steps message
==================

On success, the handler prints :data:`_NEXT_STEPS_MESSAGE` to stdout per
DEC-014: plain text (no ANSI, no markdown), names the two env vars
operators must export (``GOOGLE_CLOUD_PROJECT`` and
``ANTHROPIC_API_KEY``), and lists the three first-run commands so an
operator can copy-paste their way to a working pipeline. The message
survives ``--no-color`` because it carries no colour codes.
"""

from __future__ import annotations

import argparse
import shlex
import sys

from signalforge.cli._helpers import (
    format_error_to_stderr,
    map_exception_to_exit_code,
)
from signalforge.cli.errors import (
    CliInitDemoCopyError,
    CliInitDemoDestExistsError,
    CliInitDemoDestUnsafeError,
    CliInitDemoFixtureMissingError,
    CliPathError,
)
from signalforge.demo import (
    DemoDestExistsError,
    DemoDestUnsafeError,
    DemoFixtureMissingError,
    DemoPathError,
    copy_demo,
)

__all__ = ["add_parser", "cmd_init_demo"]


# DEC-014: plain text, stdout, names both env vars + the three first-run
# commands. ``{dest}`` is the resolved-on-disk path returned by
# :func:`copy_demo` so the operator's copy-paste ``cd`` command lands on
# the actual directory rather than the (possibly relative) string they
# typed. ``{dest_quoted}`` shell-quotes the path so an operator whose home
# directory contains spaces (``/Users/Wes Duenow/...``) still gets a valid
# copy-pasteable ``cd`` line.
_NEXT_STEPS_MESSAGE: str = """\
Demo copied to {dest}

Next steps:
  1. export GOOGLE_CLOUD_PROJECT=<your-billing-project>
  2. export ANTHROPIC_API_KEY=<your-anthropic-api-key>
  3. cd {dest_quoted}
  4. signalforge lint
  5. signalforge generate models/staging/stg_bikeshare_trips.sql --dry-run

The demo uses bigquery-public-data.austin_bikeshare.bikeshare_trips. The
bundled profiles.yml reads GOOGLE_CLOUD_PROJECT from your environment, so
no profile editing is required.
"""


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``init-demo`` subcommand on the top-level parser.

    Mirrors the registration shape of :mod:`signalforge.cli.lint` and
    :mod:`signalforge.cli.version` (DEC-009 of ``.claude/rules/cli-layer.md``
    — one flat module per subcommand). Two surfaces:

    * Positional ``dest`` — optional (``nargs="?"``) with a string
      default of ``./signalforge-demo/``. String (not :class:`pathlib.Path`)
      so argparse's default stringification is predictable across
      Python versions and platforms; :func:`copy_demo` itself runs
      ``Path(dest)`` so callers can pass either form.
    * ``--force`` — boolean flag (default ``False``). Triggers
      :func:`copy_demo`'s atomic-replace path (``rmtree`` then
      ``copytree``); refuses non-empty dest unless ``--force`` is
      supplied (DEC-001).
    """
    parser = subparsers.add_parser(
        "init-demo",
        help="Copy the bundled demo dbt project into a fresh directory.",
        description=(
            "Copy the bundled signalforge demo project (Austin "
            "bikeshare staging model against bigquery-public-data) into "
            "<dest> so you can run 'signalforge lint' and 'signalforge "
            "generate --dry-run' against a known-good fixture. The "
            "subcommand refuses non-empty dest unless --force is "
            "supplied; --force will not clobber '/', $HOME, or the "
            "current working directory."
        ),
    )
    parser.add_argument(
        "dest",
        nargs="?",
        default="./signalforge-demo/",
        metavar="DEST",
        help=(
            "Destination directory for the demo project. Default: "
            "./signalforge-demo/. Refuses non-empty dest unless --force "
            "is supplied."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "Atomically replace dest if it exists and is non-empty. "
            "Refuses '/', $HOME, and the current working directory as "
            "a blast-radius guard."
        ),
    )
    parser.set_defaults(func=cmd_init_demo)


def cmd_init_demo(args: argparse.Namespace) -> int:
    """Copy the bundled demo project to ``args.dest`` and print next steps.

    Returns the integer exit code per the four-tier CLI taxonomy
    (DEC-008 of ``.claude/rules/cli-layer.md``):

    * ``0`` — copy succeeded; next-steps message printed to stdout.
    * ``1`` — broken install (:class:`CliInitDemoFixtureMissingError`),
      symlink cycle (:class:`CliPathError`), or generic filesystem
      failure (:class:`CliInitDemoCopyError`).
    * ``2`` — operator-side dest mistakes:
      :class:`CliInitDemoDestExistsError` (non-empty dest without
      ``--force``) or :class:`CliInitDemoDestUnsafeError` (``--force``
      against ``/``, ``Path.home()``, or cwd).

    The single ``try / except Exception`` boundary matches DEC-016 (no
    traceback ever leaks); failures route through
    :func:`format_error_to_stderr` so the canonical ``ERROR: <message>``
    + ``↳ Remediation: <text>`` shape applies uniformly with the rest
    of the CLI.
    """
    try:
        resolved_dest = copy_demo(args.dest, force=args.force)
    except DemoDestExistsError as exc:
        wrapped: Exception = CliInitDemoDestExistsError(dest=str(args.dest), cause=exc)
        print(format_error_to_stderr(wrapped), file=sys.stderr)
        return map_exception_to_exit_code(wrapped)
    except DemoDestUnsafeError as exc:
        wrapped = CliInitDemoDestUnsafeError(dest=str(args.dest), cause=exc)
        print(format_error_to_stderr(wrapped), file=sys.stderr)
        return map_exception_to_exit_code(wrapped)
    except DemoFixtureMissingError as exc:
        wrapped = CliInitDemoFixtureMissingError(cause=exc)
        print(format_error_to_stderr(wrapped), file=sys.stderr)
        return map_exception_to_exit_code(wrapped)
    except DemoPathError as exc:
        # Symlink-cycle resolve failure — re-use the existing CliPathError
        # so every CLI-originated path-safety failure produces one error
        # type (DEC-012 — re-use rather than add a new wrapper for the
        # path case).
        wrapped = CliPathError(
            f"failed to resolve dest path {str(args.dest)!r}: {exc}",
            remediation=("Remove the symlink cycle at the destination or pick a different path."),
        )
        print(format_error_to_stderr(wrapped), file=sys.stderr)
        return map_exception_to_exit_code(wrapped)
    except (KeyboardInterrupt, SystemExit):
        # Preserve Python's default semantics for operator Ctrl-C and
        # any clean SystemExit raised from within copy_demo (none today,
        # but defensive parity with the rest of the CLI).
        raise
    except OSError as exc:
        # Generic filesystem failure from shutil.copytree / rmtree:
        # ENOSPC, EACCES on the parent, EROFS, etc. Tier 1 per DEC-012.
        wrapped = CliInitDemoCopyError(dest=str(args.dest), cause=exc)
        print(format_error_to_stderr(wrapped), file=sys.stderr)
        return map_exception_to_exit_code(wrapped)
    except Exception as exc:  # noqa: BLE001 — uniform CLI boundary catch (DEC-016)
        # Belt-and-braces — any forward-compat exception added to the
        # demo helper's raise surface routes through the canonical
        # formatter + mapper rather than leaking a traceback.
        print(format_error_to_stderr(exc), file=sys.stderr)
        return map_exception_to_exit_code(exc)

    print(
        _NEXT_STEPS_MESSAGE.format(
            dest=resolved_dest,
            dest_quoted=shlex.quote(str(resolved_dest)),
        )
    )
    return 0
