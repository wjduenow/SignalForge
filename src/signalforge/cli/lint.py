"""``signalforge lint`` subcommand (US-004 — config-only validator).

Validates the five existing ``signalforge.yml`` config blocks
(``safety:``, ``llm:``, ``prune:``, ``grade:``, ``diff:``) against their
per-stage loaders. No warehouse, no LLM, no network — sub-second target.

Multi-error reporting per DEC-008:

* Zero loaders raise → exit 0 with silent stdout (git-style).
* Exactly one loader raises → exit 1 with the canonical
  ``ERROR: <message>`` single-line stderr shape.
* Two or more loaders raise → exit 1 with a header + bullet list:

  .. code-block:: text

      ERROR: signalforge.yml has N validation errors:
        - safety: <error msg>
        - prune:  <error msg>

  This mirrors the DEC-008 tier-2 multi-violation shape used by the
  drafter's whole-draft anchor-contract error so CI parsers see one
  visual contract for any "list of problems" output the CLI emits.

Project-root resolution mirrors :mod:`signalforge.cli.generate`:

* ``--project-dir <PATH>`` is an absolute assertion (DEC-027) — the
  supplied path must directly contain ``dbt_project.yml``.
* No flag → walk up from cwd until ``dbt_project.yml`` is found
  (DEC-001).

The sub-second target is a smoke assertion in
``tests/cli/test_lint.py``. The five loaders are pure YAML parse +
Pydantic validation; nothing in this path opens a network connection or
touches the warehouse.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from signalforge.cli._helpers import (
    canonicalise_user_path,
    format_error_to_stderr,
    map_exception_to_exit_code,
)
from signalforge.cli.errors import CliPathError
from signalforge.diff import load_diff_config
from signalforge.draft import load_draft_config
from signalforge.grade import load_grade_config
from signalforge.prune import load_prune_config
from signalforge.safety import load_safety_config

__all__ = ["add_parser", "cmd_lint"]


_LOGGER = logging.getLogger("signalforge.cli")


# Five (block-label, loader) pairs in the documented signalforge.yml
# top-level order. The label drives the multi-error bullet output so the
# operator sees which block failed without having to grep the message.
# Keeping the loaders in a tuple (immutable, ordered) makes the iteration
# deterministic — every run of `signalforge lint` against the same input
# emits errors in the same order.
_BLOCK_LOADERS: tuple[tuple[str, object], ...] = (
    ("safety", load_safety_config),
    ("llm", load_draft_config),
    ("prune", load_prune_config),
    ("grade", load_grade_config),
    ("diff", load_diff_config),
)


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``lint`` subcommand on the top-level parser."""
    parser = subparsers.add_parser(
        "lint",
        help="Validate the signalforge.yml config blocks.",
        description=(
            "Validate the five signalforge.yml config blocks (safety, llm, "
            "prune, grade, diff). Exits 0 when every block parses cleanly "
            "(or is absent — each loader returns defaults silently per its "
            "own DEC). On error, lists every failing block in one run "
            "rather than short-circuiting on the first failure."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=(
            "Override the default <project_dir>/signalforge.yml. Path is "
            "canonicalised against the resolved project_dir."
        ),
    )
    parser.add_argument(
        "--project-dir",
        metavar="PATH",
        default=None,
        help=(
            "Absolute assertion: <PATH> must contain dbt_project.yml. "
            "When supplied, the CLI does NOT walk up from this path. "
            "Default: walk up from the current working directory."
        ),
    )
    parser.set_defaults(func=cmd_lint)


def _resolve_project_dir(args: argparse.Namespace) -> Path:
    """Resolve the dbt project root from the arguments.

    Mirrors :func:`signalforge.cli.generate._resolve_project_dir` (DEC-001
    walk-up; DEC-027 absolute-assertion under ``--project-dir``). The
    duplication is deliberate per the project's path-safety convention
    (each layer's containment gate stays homogeneous; promotion to a
    shared helper is a future v0.2 refinement).
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
            "Run `signalforge lint` from inside a dbt project, or pass "
            "--project-dir <PATH> pointing at the directory that contains "
            "dbt_project.yml."
        ),
    )


def cmd_lint(args: argparse.Namespace) -> int:
    """Validate every config block and report all failures in one run.

    Returns the integer exit code per the four-tier CLI taxonomy
    (DEC-008). Every loader failure routes through
    :func:`map_exception_to_exit_code` so the tier matches what the same
    exception would produce from ``cmd_generate``:

    * ``0`` — every loader returned cleanly (or returned defaults silently
      because the block / file was absent).
    * single failure → :func:`map_exception_to_exit_code` of the
      exception (typically tier 1 for ``*ConfigNotFoundError`` /
      ``*ConfigInvalidError``; tier 2 for ``CliInputError``).
    * multiple failures → ``max(...)`` of the per-failure tiers, rendered
      with the multi-error header + bullets shape (DEC-008).
    * project-root / path-canonicalisation failures from the CLI layer
      itself route through the same mapper (typically tier 1 for
      ``CliPathError``, tier 2 for ``CliInputError``).
    """
    try:
        project_dir = _resolve_project_dir(args)
        config_path = canonicalise_user_path(args.config, project_dir)
    except Exception as exc:  # noqa: BLE001 — uniform CLI boundary catch (DEC-016)
        message = format_error_to_stderr(exc)
        print(message, file=sys.stderr)
        return map_exception_to_exit_code(exc)

    # Collect every loader failure rather than short-circuiting on the
    # first — the operator sees every problem in one run (DEC-008
    # multi-error reporting).
    failures: list[tuple[str, Exception]] = []
    for block_name, loader in _BLOCK_LOADERS:
        try:
            # The loader signature is uniform across stages:
            # ``(project_dir, path=None) -> <Config>``.
            loader(project_dir, config_path)  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 — collect every typed error
            failures.append((block_name, exc))

    if not failures:
        return 0

    if len(failures) == 1:
        # Single-error shape: reuse the canonical CLI formatter so
        # remediation footers render uniformly with the rest of the CLI.
        # Route through ``map_exception_to_exit_code`` so the tier
        # matches what ``cmd_generate`` would produce for the same
        # exception (DEC-008 four-tier contract).
        _block_name, exc = failures[0]
        message = format_error_to_stderr(exc)
        print(message, file=sys.stderr)
        return map_exception_to_exit_code(exc)

    # Multi-error shape (DEC-008 header + bullets). Each bullet names
    # the failing block AND a one-line summary of the error message.
    # Pydantic-derived messages can carry embedded newlines (e.g.
    # ``"1 validation error for _PruneConfigFile\nprune.test_timeout_seconds\n
    # Value error, must be positive ..."``); we collapse those into a
    # single line so the bullet shape stays one ``  - <block>: <msg>``
    # row per failure — CI parsers key on the leading two-space dash.
    header = f"ERROR: signalforge.yml has {len(failures)} validation errors:"
    bullets: list[str] = []
    for block_name, exc in failures:
        # Use the typed error's ``message`` attribute if present (every
        # layer-base error class in the repo carries one); fall back to
        # ``str(exc)`` for forward-compat exceptions that don't.
        msg = getattr(exc, "message", None)
        if msg is None:
            msg = str(exc) or exc.__class__.__name__
        # Collapse internal whitespace runs (newlines, indentation) so
        # the bullet renders on one visual line.
        msg = " ".join(msg.split())
        bullets.append(f"  - {block_name}: {msg}")
    print(header + "\n" + "\n".join(bullets), file=sys.stderr)
    # Multi-error exit code = max of the per-failure tiers. Loader
    # failures are typically tier 1 (load), but a CliInputError that
    # bubbles up from a deeper validator surfaces as tier 2 — picking
    # the most-severe keeps the four-tier contract honest (DEC-008).
    return max(map_exception_to_exit_code(exc) for _, exc in failures)
