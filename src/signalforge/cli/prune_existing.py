"""``signalforge prune-existing`` subcommand (US-002 — issue #105).

Prunes an externally-authored dbt ``schema.yml`` against real warehouse
data: it runs **ingest -> prune -> diff** (no draft, no grade, **no LLM
call**) and reports which of the operator's existing tests add signal and
which always pass / fail on known-clean data. The product story: *point
SignalForge at your existing dbt tests and let the warehouse tell you
which ones add no signal* — extending Architectural Commitment #1
("signal over volume") to any generator's tests (hand-written,
dbt-codegen, dbt Copilot, DinoAI, datapilot).

This module is the **contract surface** (US-002): it defines
:func:`add_parser` registering the full flag set and a **stub**
:func:`cmd_prune_existing` returning ``0``. The full ingest -> prune ->
diff orchestrator body lands in US-003.

Read-only by design (DEC-003)
=============================

``prune-existing`` deliberately ships **no** ``--write`` flag. The
``--schema`` file is hand-authored, so silently overwriting it would be
surprising and destructive. v1 prints the rendered diff to stdout and
writes the ``.signalforge/diff.json`` sidecar by default; ``--dry-run``
suppresses the sidecar for a pure-stdout, zero-disk run. The unified diff
is framed *against the operator's actual file* — the external
``schema.yml`` is fed to the diff renderer as ``existing_schema`` so the
diff shows exactly what to remove (DEC-004).

Flag set (DEC-002)
==================

The safety ``--mode`` knob that ``generate`` carries is deliberately
**dropped** here: ``prune_tests`` never consumes a ``SafetyPolicy`` and
this path makes no LLM call, so ``--mode`` would be a dead flag. The
genuinely-relevant warehouse knobs ``--scope`` / ``--sample-strategy``
take its place. ``--min-score``, ``--estimate``, ``--select``, and
``--write`` are likewise dropped (DEC-002 / DEC-003).
"""

from __future__ import annotations

import argparse

__all__ = ["add_parser", "cmd_prune_existing"]


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``prune-existing`` subcommand on the top-level parser.

    Mirrors the registration shape of :mod:`signalforge.cli.generate` and
    :mod:`signalforge.cli.lint` (DEC-009 of ``.claude/rules/cli-layer.md``
    — one flat module per subcommand). The full flag set (DEC-002):

    * positional ``<model>`` — accepts a bare model name, a dbt
      unique_id, or a file path.
    * ``--schema`` (**required**) — the externally-authored dbt
      ``schema.yml`` to prune.
    * ``--project-dir`` / ``--manifest`` / ``--profiles-dir`` — the
      shared project-locating flags (same semantics as ``generate``).
    * ``--scope {sample,full}`` — overrides ``PruneConfig.scope``.
    * ``--sample-strategy {oneshot,materialised}`` — overrides
      ``PruneConfig.sample_strategy``.
    * ``--format {ansi,markdown,json}`` (default ``ansi``) — selects the
      diff renderer.
    * ``--dry-run`` — suppresses the sidecar; there is **no** ``--write``
      (read-only w.r.t. the operator's ``schema.yml`` — DEC-003).
    * ``--quiet`` / ``--verbose`` / ``--no-color`` — observability flags.

    The choice flags (``--scope`` / ``--sample-strategy`` / ``--format``)
    reject typos at the argparse level (``choices=``), producing exit 2.
    """
    parser = subparsers.add_parser(
        "prune-existing",
        help="Prune an existing dbt schema.yml's tests against warehouse data.",
        description=(
            "Prune the tests in an externally-authored dbt schema.yml "
            "against real warehouse data (ingest -> prune -> diff; no LLM "
            "call). Reports which of your existing tests add signal "
            "(kept), which could not be evaluated (kept-uncertain), and "
            "which always pass or fail on known-clean data (dropped). "
            "Read-only: prints a unified diff against your schema.yml "
            "showing what to remove and writes a .signalforge/diff.json "
            "sidecar by default; there is no --write flag. Use --dry-run "
            "to suppress the sidecar."
        ),
    )
    parser.add_argument(
        "model",
        metavar="<model>",
        help=(
            "Model whose existing tests to prune. Accepts a bare model "
            "name (e.g. 'customers'), a dbt unique_id "
            "(e.g. 'model.proj.customers'), or a file path "
            "(e.g. 'models/marts/customers.sql')."
        ),
    )
    parser.add_argument(
        "--schema",
        metavar="PATH",
        required=True,
        help=(
            "Required. Path to the externally-authored dbt schema.yml "
            "whose tests to prune. The file is read-only — its content "
            "is fed to the diff renderer as the existing schema so the "
            "unified diff shows what to remove from this file."
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
    parser.add_argument(
        "--manifest",
        metavar="PATH",
        default=None,
        help=(
            "Override the default <project_dir>/target/manifest.json. "
            "Path is canonicalised against the resolved project_dir."
        ),
    )
    parser.add_argument(
        "--profiles-dir",
        metavar="PATH",
        default=None,
        help=(
            "Override the default profiles.yml search location. Mirrors "
            "dbt-core's --profiles-dir flag. Sets DBT_PROFILES_DIR in "
            "the current process environment."
        ),
    )
    parser.add_argument(
        "--scope",
        choices=("sample", "full"),
        default=None,
        help=(
            "Override prune.scope (default: from config). Precedence: "
            "flag > prune.scope in signalforge.yml > library default "
            "('sample'). Applied via PruneConfig.model_validate so "
            "validators re-run."
        ),
    )
    parser.add_argument(
        "--sample-strategy",
        dest="sample_strategy",
        choices=("oneshot", "materialised"),
        default=None,
        help=(
            "Override prune.sample_strategy (default: from config). "
            "Precedence: flag > prune.sample_strategy in signalforge.yml "
            "> library default ('materialised'). Applied via "
            "PruneConfig.model_validate so validators re-run."
        ),
    )
    parser.add_argument(
        "--format",
        dest="format",
        choices=("ansi", "markdown", "json"),
        default="ansi",
        help=(
            "Select the diff renderer. ANSI: coloured terminal output "
            "(default). Markdown: GitHub-friendly report. JSON: stdout "
            "receives the JSON sidecar's contents."
        ),
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "Run ingest -> prune -> diff and print the diff to stdout, "
            "but write nothing — suppresses the default-on "
            ".signalforge/diff.json sidecar. There is no --write flag "
            "(read-only w.r.t. your schema.yml)."
        ),
    )
    # Observability flags. ``--quiet`` and ``--verbose`` are mutually
    # exclusive at argparse time (combining them is a usage error ->
    # exit 2). ``--no-color`` flips the ``NO_COLOR`` env var so the
    # AnsiRenderer's existing precedence chain emits plain text.
    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Suppress per-stage stderr progress lines and the "
            "skipped-test report, and raise the log level to WARNING. "
            "Mutually exclusive with --verbose."
        ),
    )
    verbosity_group.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Raise the log level to DEBUG, list each skipped test in "
            "detail, and surface panic-path tracebacks for unexpected "
            "errors. Mutually exclusive with --quiet."
        ),
    )
    parser.add_argument(
        "--no-color",
        dest="no_color",
        action="store_true",
        help=(
            "Strip ANSI colour codes from stdout. Sets NO_COLOR=1 in the "
            "current process environment so the AnsiRenderer's existing "
            "precedence chain emits plain text."
        ),
    )
    parser.set_defaults(func=cmd_prune_existing)


def cmd_prune_existing(args: argparse.Namespace) -> int:
    """Prune an existing dbt schema.yml's tests against warehouse data.

    **Stub (US-002).** Returns ``0`` unconditionally. The full
    ingest -> prune -> diff orchestrator body — single ``try/except
    Exception`` boundary, model resolution, ``read_schema``,
    skipped-test report, ``PruneConfig`` overrides, ``prune_tests``,
    ``render_diff`` against the external ``schema.yml`` as
    ``existing_schema``, and ``render_to_text`` to stdout — lands in
    US-003.
    """
    return 0
