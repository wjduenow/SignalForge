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
import collections
import os
import time
from pathlib import Path

from signalforge import diff as diff_module
from signalforge import ingest as ingest_module
from signalforge import manifest as manifest_module
from signalforge import prune as prune_module
from signalforge import warehouse as warehouse_module
from signalforge.cli._helpers import (
    _resolve_model_by_key,
    canonicalise_user_path,
    emit_progress_done,
    emit_progress_entry,
    format_error_to_stderr,
    map_exception_to_exit_code,
    print_stderr,
    setup_logging,
    should_emit_progress,
)
from signalforge.cli.errors import CliPathError
from signalforge.warehouse.base import WarehouseAdapter

__all__ = ["add_parser", "cmd_prune_existing"]


# Three pipeline stages drive the progress UX (DEC-010): ingest → prune →
# diff. Passed to ``emit_progress_*`` as ``total=3`` so the lines read
# ``[1/3] ingest`` … ``[3/3] diff`` rather than the ``generate`` pipeline's
# ``/5``.
_TOTAL_STAGES = 3


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


# ---------------------------------------------------------------------------
# Test-injection seam (DEC-009)
# ---------------------------------------------------------------------------


def _make_warehouse_adapter(profile: warehouse_module.DbtProfileTarget) -> WarehouseAdapter:
    """Construct the :class:`WarehouseAdapter` for the resolved profile.

    A thin module-level seam (DEC-009 of
    ``plans/super/105-prune-existing-cli.md``) mirroring
    :func:`signalforge.cli.generate._make_warehouse_adapter`. Tests patch
    ``signalforge.cli.prune_existing._make_warehouse_adapter`` (patch-where-
    used) with a :class:`tests.warehouse._fake.FakeBigQueryClient`-backed
    :class:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter`.

    The returned adapter is **un-entered** — :func:`prune_tests` owns the
    ``with adapter:`` block (DEC-013 of #22), so the handler must not call
    ``__enter__`` itself.
    """
    return WarehouseAdapter.from_profile(profile)


# ---------------------------------------------------------------------------
# Project-root resolution (mirrors generate.py — DEC-001 + DEC-027)
# ---------------------------------------------------------------------------


def _resolve_project_dir(args: argparse.Namespace) -> Path:
    """Resolve the dbt project root from the arguments.

    Mirrors :func:`signalforge.cli.generate._resolve_project_dir`:
    ``--project-dir`` is an *absolute assertion* — the supplied path must
    directly contain ``dbt_project.yml`` (the CLI does NOT walk up from it,
    DEC-027). With no flag, walk up from :func:`Path.cwd` to the nearest
    directory containing ``dbt_project.yml`` (DEC-001).
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
        return candidate

    cwd = Path.cwd().resolve()
    current: Path | None = cwd
    while current is not None:
        if (current / "dbt_project.yml").is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent

    raise CliPathError(
        f"could not find dbt_project.yml walking up from {cwd}",
        remediation=(
            "Run `signalforge prune-existing` from inside a dbt project, or "
            "pass --project-dir <PATH> pointing at the directory that "
            "contains dbt_project.yml."
        ),
    )


# ---------------------------------------------------------------------------
# Skipped-test report (DEC-007)
# ---------------------------------------------------------------------------


def _emit_skipped_report(skipped: tuple[ingest_module.SkippedTest, ...], *, verbose: bool) -> None:
    """Emit the operator-facing skipped-test report to stderr (DEC-007).

    One summary line grouped by :data:`signalforge.ingest.SkipReason`,
    e.g.::

        Skipped 3 unsupported tests: custom-or-generic-test×2, unsupported-test-type×1

    Under ``--verbose``, follow with one indented line per
    :class:`SkippedTest` (test_name, column, reason, detail). Uses
    :func:`print_stderr` (the ANSI-safe sink), NOT ``_LOGGER`` — this is
    operator-facing info, not a log event, so it stays off the lazy-format
    grep gate. The caller gates on ``not quiet`` before invoking.
    """
    if not skipped:
        return
    counts: collections.Counter[str] = collections.Counter(s.reason for s in skipped)
    # Group counts in encounter order of first appearance for a stable,
    # human-readable summary (Counter preserves first-seen insertion order).
    breakdown = ", ".join(f"{reason}×{count}" for reason, count in counts.items())
    noun = "test" if len(skipped) == 1 else "tests"
    print_stderr(f"Skipped {len(skipped)} unsupported {noun}: {breakdown}")
    if verbose:
        # The verbose per-item fields (test_name / column / detail) come
        # straight from the operator's YAML, so a crafted value carrying a
        # raw newline could otherwise inject a fake "  - ..." bullet into the
        # report. print_stderr already strips ANSI CSI escapes; we additionally
        # scrub \n / \r / \t so a value cannot break the one-line-per-test
        # geometry — mirrors the control-char scrub in format_batch_summary.
        for s in skipped:
            test_name = _scrub_control_chars(s.test_name)
            column = _scrub_control_chars(s.column) if s.column is not None else "<model-level>"
            detail = f" — {_scrub_control_chars(s.detail)}" if s.detail else ""
            print_stderr(f"  - {test_name} (column={column}, reason={s.reason}){detail}")


def _scrub_control_chars(value: str) -> str:
    """Replace newline / carriage-return / tab with a single space.

    Defence-in-depth for operator-supplied strings rendered into a
    one-line-per-item stderr report — mirrors the scrub in
    :func:`signalforge.cli._helpers.format_batch_summary`.
    """
    return value.replace("\n", " ").replace("\r", " ").replace("\t", " ")


# ---------------------------------------------------------------------------
# Subcommand entry point
# ---------------------------------------------------------------------------


def cmd_prune_existing(args: argparse.Namespace) -> int:
    """Prune an existing dbt schema.yml's tests against warehouse data.

    Runs **ingest → prune → diff** (no draft, no grade, **no LLM call**)
    for ``args.model`` against the externally-authored ``--schema`` file,
    and prints a unified diff against that file to stdout. Returns the
    integer exit code per the four-tier taxonomy in
    :data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`.

    Every step runs inside one ``try/except Exception`` boundary
    (DEC-016 of ``cli-layer.md``): no traceback ever leaks. The five
    :class:`signalforge.ingest.IngestError` concretes are already
    registered in the exit-code table (DEC-006 — no bespoke
    ``CliPruneExisting*`` wrappers), so they route to the correct tier via
    the MRO walk in :func:`map_exception_to_exit_code`.

    Pipeline (DEC-002 … DEC-010):

    1. Set process env: ``--no-color`` → ``NO_COLOR=1``; ``--profiles-dir``
       → ``DBT_PROFILES_DIR`` (mirrors ``generate`` — DEC-023 of
       ``cli-layer.md``).
    2. Resolve ``project_dir`` (``--project-dir`` absolute-assertion;
       walk-up default).
    3. Load the manifest (``--manifest`` canonicalised against
       ``project_dir``).
    4. Resolve ``<model>`` via :func:`_resolve_model_by_key` (bare-name /
       unique_id / file-path).
    5. Canonicalise ``--schema`` via :func:`canonicalise_user_path`
       (→ :class:`CliPathError` on symlink/containment — DEC-005).
    6. ``read_schema(schema_path, model, project_dir=project_dir)`` —
       passing the ``Path`` so the full ingest typed-error surface fires.
    7. Skipped-test report (DEC-007) — summary + ``--verbose`` detail,
       suppressed by ``--quiet``.
    8. Load + override :class:`PruneConfig` (``--scope`` /
       ``--sample-strategy`` via ``model_validate`` — DEC-002).
    9. Build the warehouse adapter via :func:`_make_warehouse_adapter`
       (DEC-009); ``prune_tests`` owns the ``with adapter:`` block, so the
       adapter is passed un-entered.
    10. ``prune_tests(model, adapter, result.candidate, manifest, ...)``.
    11. Read the ``--schema`` text (UTF-8) for ``existing_schema``
        (DEC-004).
    12. Load + override :class:`DiffConfig` (``--format`` via
        ``model_validate``); ``--dry-run`` → ``write_sidecar=False``.
    13. ``render_diff(..., grading_report=None, existing_schema=<text>,
        write_sidecar=not dry_run, ...)`` — ``grading_report=None`` means
        the diff renders kept / kept-uncertain / dropped, never
        ``flagged`` (DEC-004 / #104 DEC-011).
    14. ``render_to_text`` → stdout (trailing newline as ``generate``).
    15. 3-stage progress to stderr (DEC-010): ``1/3 ingest``,
        ``2/3 prune``, ``3/3 diff``.
    """
    quiet = bool(getattr(args, "quiet", False))
    verbose = bool(getattr(args, "verbose", False))
    no_color = bool(getattr(args, "no_color", False))
    setup_logging(verbose=verbose, quiet=quiet)
    if no_color:
        os.environ["NO_COLOR"] = "1"
        os.environ.pop("FORCE_COLOR", None)

    progress_on = should_emit_progress(quiet=quiet, verbose=verbose)

    try:
        project_dir = _resolve_project_dir(args)

        manifest_override = canonicalise_user_path(args.manifest, project_dir)
        # ``--profiles-dir`` becomes an env var for the duration of the
        # call (mirrors generate.py — DEC-023). Intentionally NOT routed
        # through ``canonicalise_user_path`` because dbt's convention
        # places profiles.yml at ``~/.dbt/`` outside the project tree.
        if args.profiles_dir is not None:
            try:
                profiles_dir_resolved = Path(args.profiles_dir).expanduser().resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise CliPathError(
                    f"--profiles-dir {args.profiles_dir!r} could not be resolved: {exc}",
                    remediation="Pass an absolute or ~-prefixed path that exists.",
                ) from exc
            os.environ["DBT_PROFILES_DIR"] = str(profiles_dir_resolved)

        manifest = manifest_module.load(project_dir, manifest_path=manifest_override)
        model = _resolve_model_by_key(manifest, args.model)

        # ``--schema`` symlink/containment gate (DEC-005). The Path is
        # passed to ``read_schema`` so the full ingest typed-error surface
        # (IngestSchema{NotFound,Parse,TooLarge}Error) fires and is
        # exit-code-mapped; the same canonicalised path's UTF-8 text feeds
        # ``render_diff``'s ``existing_schema`` below.
        schema_path = canonicalise_user_path(args.schema, project_dir)
        assert schema_path is not None  # --schema is required (argparse)

        # ---- 1/3: ingest ------------------------------------------------
        if progress_on:
            emit_progress_entry(1, "ingest", "parsing schema.yml...", total=_TOTAL_STAGES)
        _t0 = time.monotonic()
        ingest_result = ingest_module.read_schema(schema_path, model, project_dir=project_dir)
        if progress_on:
            emit_progress_done(1, "ingest", time.monotonic() - _t0, total=_TOTAL_STAGES)

        # Skipped-test report (DEC-007) — operator info, suppressed by
        # ``--quiet``.
        if not quiet:
            _emit_skipped_report(ingest_result.skipped, verbose=verbose)

        # ---- 2/3: prune -------------------------------------------------
        # ``--scope`` / ``--sample-strategy`` overrides applied via
        # ``PruneConfig.model_validate`` (NOT ``model_copy``) so every
        # validator re-runs — mirrors generate.py (DEC-002).
        prune_config = prune_module.load_prune_config(project_dir)
        scope_override = getattr(args, "scope", None)
        sample_strategy_override = getattr(args, "sample_strategy", None)
        prune_overrides: dict[str, str] = {}
        if scope_override is not None:
            prune_overrides["scope"] = scope_override
        if sample_strategy_override is not None:
            prune_overrides["sample_strategy"] = sample_strategy_override
        if prune_overrides:
            prune_config = prune_module.PruneConfig.model_validate(
                {**prune_config.model_dump(), **prune_overrides}
            )

        candidate = ingest_result.candidate
        candidate_test_count = sum(len(c.tests) for c in candidate.columns) + len(candidate.tests)

        profile = warehouse_module.load_profile(project_dir)
        adapter = _make_warehouse_adapter(profile)

        if progress_on:
            emit_progress_entry(
                2,
                "prune",
                f"running {candidate_test_count} existing tests against warehouse...",
                total=_TOTAL_STAGES,
            )
        _t0 = time.monotonic()
        prune_result = prune_module.prune_tests(
            model,
            adapter,
            candidate,
            manifest,
            config=prune_config,
            project_dir=project_dir,
        )
        if progress_on:
            emit_progress_done(2, "prune", time.monotonic() - _t0, total=_TOTAL_STAGES)

        # ---- 3/3: diff --------------------------------------------------
        # Feed the operator's actual schema.yml as ``existing_schema``
        # (DEC-004) so the unified diff shows what to remove from THAT
        # file. ``grading_report=None`` → kept / kept-uncertain / dropped,
        # never ``flagged``.
        existing_schema_text = schema_path.read_text(encoding="utf-8")
        diff_config = diff_module.load_diff_config(project_dir)
        format_override = getattr(args, "format", None)
        if format_override is not None and format_override != diff_config.render_kind:
            diff_config = diff_module.DiffConfig.model_validate(
                {**diff_config.model_dump(), "render_kind": format_override}
            )
        dry_run = bool(getattr(args, "dry_run", False))

        if progress_on:
            emit_progress_entry(3, "diff", "rendering...", total=_TOTAL_STAGES)
        _t0 = time.monotonic()
        diff_report = diff_module.render_diff(
            model,
            candidate,
            prune_result,
            grading_report=None,
            existing_schema=existing_schema_text,
            config=diff_config,
            write_sidecar=not dry_run,
            project_dir=project_dir,
        )
        if progress_on:
            emit_progress_done(3, "diff", time.monotonic() - _t0, total=_TOTAL_STAGES)

        rendered = diff_module.render_to_text(
            diff_report, config=diff_config, project_dir=project_dir
        )
        print(rendered)
        return 0

    except Exception as exc:  # noqa: BLE001 — the boundary catch (DEC-016)
        message = format_error_to_stderr(exc)
        print_stderr(message)
        return map_exception_to_exit_code(exc)
