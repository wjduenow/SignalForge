"""``signalforge generate <model>`` subcommand (US-005 / US-006).

Wires the v0.1 pipeline end-to-end: manifest → safety → draft → prune →
grade → diff. This is the CLI's reason to exist; everything else
(version, lint) is scaffolding around this entry point.

US-005 shipped the core orchestration. US-006 layered five runtime-knob
flags onto that orchestration: ``--mode`` (overrides safety policy via
:meth:`signalforge.safety.SafetyPolicy.with_mode` so validators re-run —
DEC-002), ``--min-score`` (reporting-only override of
:attr:`signalforge.grade.GradeConfig.min_mean_score`, the aggregate-verdict
threshold consumed by ``GradingReport.passed`` and (when
``grade.fail_on_below_threshold=true``) ``GradeBelowThresholdError``; never
affects exit code by itself per DEC-004 — the diff renderer's
``flagged`` tier is driven by per-criterion ``GradingResult.passed``,
not the aggregate threshold), ``--write`` (writes the proposed
``schema.yml`` to disk), ``--dry-run`` (full pipeline but writes
nothing — overrides DEC-002's default-on sidecar per DEC-010), and
``--format`` (selects :attr:`signalforge.diff.DiffConfig.render_kind` —
DEC-020). ``--write`` and ``--dry-run`` are mutually exclusive at the
argparse level. The observability flags (``--quiet``, ``--verbose``,
``--no-color``, progress) land in US-007. The exit-code AST scan that
verifies every typed exception in the layer maps to exactly one tier
lands in US-008.

Project-root resolution (DEC-001 + DEC-027):

* No flag → walk up from :func:`Path.cwd` to the nearest dir containing
  ``dbt_project.yml``. If we hit the filesystem root without finding one,
  exit 1 with remediation.
* ``--project-dir <PATH>`` → treat as an *absolute assertion*: the path
  must exist AND must contain ``dbt_project.yml``. The CLI does NOT walk
  up from the override (DEC-027) — passing the flag means "use this
  project, not whatever's above me".

Test-injection seam (DEC-013): two private factory functions
:func:`_make_anthropic_client` and :func:`_make_warehouse_adapter` are
patched by tests in ``tests/cli/test_generate.py`` to return
:class:`tests.llm._fake.FakeAnthropicClient` /
:class:`tests.warehouse._fake.FakeBigQueryClient`-backed adapters. Both
are ``_``-prefixed (DEC of safety-layer.md / llm-drafter.md / etc.) —
not part of the public CLI contract.

Stage-order test (DEC-025): ``test_generate_calls_stages_in_documented_order``
patches every stage entry point and asserts the documented
``safety → draft → prune → grade → diff`` ordering against
``parent.mock_calls``. Pins CLAUDE.md "Pipeline shape" as a contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from signalforge import diff as diff_module
from signalforge import draft as draft_module
from signalforge import grade as grade_module
from signalforge import manifest as manifest_module
from signalforge import prune as prune_module
from signalforge import safety as safety_module
from signalforge import warehouse as warehouse_module
from signalforge.cli._helpers import (
    canonicalise_user_path,
    emit_progress_done,
    emit_progress_entry,
    format_error_to_stderr,
    map_exception_to_exit_code,
    setup_logging,
    should_emit_progress,
)
from signalforge.cli.errors import CliInputError, CliPathError
from signalforge.grade.rubric import DEFAULT_RUBRIC
from signalforge.llm._client import _AnthropicClientProtocol
from signalforge.warehouse.base import WarehouseAdapter

__all__ = ["add_parser", "cmd_generate"]


_LOGGER = logging.getLogger("signalforge.cli")


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``generate`` subcommand on the top-level parser.

    US-005 shipped the positional + project-discovery flags. US-006
    extends with the runtime knob flags (``--mode``, ``--min-score``,
    ``--write``/``--dry-run`` mutex, ``--format``). Observability flags
    (``--quiet``, ``--verbose``, ``--no-color``) follow in US-007.
    """
    parser = subparsers.add_parser(
        "generate",
        help="Draft, prune, grade, and diff a dbt model's schema.yml.",
        description=(
            "Runs the full v0.1 pipeline against <model>: load manifest, "
            "build the safety policy, draft candidate artifacts via LLM, "
            "prune always-pass / known-clean-fail tests against warehouse "
            "samples, grade the survivors, and render a diff against any "
            "existing schema.yml."
        ),
    )
    parser.add_argument(
        "model",
        metavar="<model>",
        help=(
            "Model under draft. Accepts a dbt unique_id "
            "(e.g. 'model.proj.customers') or a file path "
            "(e.g. 'models/marts/customers.sql')."
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
    # US-006 / DEC-002 — safety mode override. Argparse-level choices
    # rejection produces exit 2 (the argparse default).
    parser.add_argument(
        "--mode",
        choices=("schema-only", "aggregate-only", "sample"),
        default=None,
        help=(
            "Override the safety sampling mode. Precedence: flag > "
            "safety.mode in signalforge.yml > library default. Applied "
            "via SafetyPolicy.with_mode so validators re-run."
        ),
    )
    # US-006 / DEC-004 — reporting-only min-score override. Out-of-range
    # validation runs inside ``cmd_generate`` because argparse cannot
    # natively range-check ``type=float``.
    parser.add_argument(
        "--min-score",
        metavar="N",
        type=float,
        default=None,
        help=(
            "Override grade.min_mean_score (closed [0.0, 1.0]). Sets the "
            "aggregate-verdict threshold consumed by GradingReport.passed "
            "and (when grade.fail_on_below_threshold=true in signalforge.yml) "
            "by GradeBelowThresholdError. Reporting-only by default — does "
            "NOT enable fail-on-below-threshold by itself."
        ),
    )
    # US-006 / DEC-002 / DEC-010 — write/dry-run mutex. Argparse rejects
    # the both-flags combination with its own usage error → exit 2.
    write_group = parser.add_mutually_exclusive_group()
    write_group.add_argument(
        "--write",
        action="store_true",
        help=(
            "Write the proposed schema.yml to disk under "
            "<project_dir>/<model_dir>/schema.yml. Sidecar JSON is "
            "still written to <project_dir>/.signalforge/diff.json."
        ),
    )
    write_group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "Run the full pipeline (LLM + warehouse + grade) and print "
            "the diff to stdout, but write nothing — neither the "
            "schema.yml nor the .signalforge/diff.json sidecar. "
            "Overrides the default-on sidecar (DEC-010)."
        ),
    )
    # US-006 / DEC-020 — diff renderer kind selection.
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
    # US-007 / DEC-014 / DEC-016 — observability flags. ``--quiet`` and
    # ``--verbose`` are mutually exclusive at argparse time (combining
    # them is a usage error → exit 2). ``--no-color`` flips the
    # ``NO_COLOR`` env var inside ``cmd_generate`` so the AnsiRenderer's
    # existing precedence chain (DEC-021 of #8) emits plain text;
    # ``DiffConfig`` carries no ``force_color`` field per DEC-023.
    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Suppress per-stage stderr progress lines and raise the log "
            "level to WARNING. Mutually exclusive with --verbose."
        ),
    )
    verbosity_group.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Raise the log level to DEBUG and surface panic-path "
            "tracebacks for unexpected errors. Mutually exclusive with "
            "--quiet."
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
    parser.set_defaults(func=cmd_generate)


# ---------------------------------------------------------------------------
# Test-injection seams (DEC-013)
# ---------------------------------------------------------------------------


def _make_anthropic_client() -> _AnthropicClientProtocol | None:
    """Return the Anthropic client to inject into the draft / grade stages.

    Default implementation returns ``None`` so the underlying stage's
    own ``client = anthropic.Anthropic(...)`` lazy construction (gated
    by the single SDK seam at :mod:`signalforge.llm._client`) runs.
    Tests patch this to return a :class:`tests.llm._fake.FakeAnthropicClient`.
    """
    return None


def _make_warehouse_adapter(profile: warehouse_module.DbtProfileTarget) -> WarehouseAdapter:
    """Construct the :class:`WarehouseAdapter` for the resolved profile.

    Tests patch this to return a fake-backed adapter (e.g. a
    :class:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter`
    constructed with a :class:`tests.warehouse._fake.FakeBigQueryClient`).
    """
    return WarehouseAdapter.from_profile(profile)


# ---------------------------------------------------------------------------
# Project-root resolution (DEC-001 + DEC-027)
# ---------------------------------------------------------------------------


def _resolve_project_dir(args: argparse.Namespace) -> Path:
    """Resolve the dbt project root from the arguments.

    DEC-001 walk-up semantics: from cwd, ascend until ``dbt_project.yml``
    is found. DEC-027 absolute-assertion semantics: ``--project-dir`` is
    NOT a walk-up starting point; the supplied path must directly contain
    ``dbt_project.yml`` or the CLI exits 1.
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
    # ``Path.parents`` does not include the path itself — walk explicitly.
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
            "Run `signalforge generate` from inside a dbt project, or pass "
            "--project-dir <PATH> pointing at the directory that contains "
            "dbt_project.yml."
        ),
    )


# ---------------------------------------------------------------------------
# Subcommand entry point
# ---------------------------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> int:
    """Run the full pipeline for ``args.model``.

    Returns the integer exit code per the four-tier taxonomy in
    :data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`. No
    traceback ever leaks (DEC-016): every typed exception is caught at
    this boundary, formatted via :func:`format_error_to_stderr`, and
    routed to the right tier via :func:`map_exception_to_exit_code`. An
    untyped :class:`Exception` lands at the panic-path tier (1) with the
    same shape — no traceback, just the typed-error one-liner.

    Pipeline order is the documented ``safety → draft → prune → grade →
    diff`` (DEC-005). The stage-order test in
    ``tests/cli/test_generate.py`` pins this contract.
    """
    # US-007 — observability: configure logging and the progress gate
    # BEFORE any pipeline work. ``--quiet`` and ``--verbose`` are mutex
    # at argparse time so at most one is True. ``--no-color`` flips the
    # ``NO_COLOR`` env var so the AnsiRenderer's existing precedence
    # chain (DEC-021 of #8) emits plain text. ``FORCE_COLOR`` is
    # cleared belt-and-braces so an environmental override doesn't
    # defeat the operator's explicit opt-out.
    quiet = bool(getattr(args, "quiet", False))
    verbose = bool(getattr(args, "verbose", False))
    no_color = bool(getattr(args, "no_color", False))
    setup_logging(verbose=verbose, quiet=quiet)
    if no_color:
        os.environ["NO_COLOR"] = "1"
        os.environ.pop("FORCE_COLOR", None)
    progress_on = should_emit_progress(quiet=quiet, verbose=verbose)

    try:
        # US-006 — explicit range-check on ``--min-score``. Argparse's
        # ``type=float`` cannot natively bound-check; do it here so an
        # out-of-range value lands at exit 2 (CliInputError tier) BEFORE
        # any project-root resolution / file IO. ``None`` means
        # "fall back to grade.min_mean_score" — leave alone.
        min_score_override = getattr(args, "min_score", None)
        if min_score_override is not None and not (0.0 <= min_score_override <= 1.0):
            raise CliInputError(
                f"--min-score {min_score_override!r} is outside the closed interval [0.0, 1.0]",
                remediation=(
                    "Pass a float between 0.0 and 1.0 inclusive. The flag "
                    "overrides grade.min_mean_score, the aggregate-verdict "
                    "threshold for GradingReport.passed."
                ),
            )

        project_dir = _resolve_project_dir(args)

        manifest_override = canonicalise_user_path(args.manifest, project_dir)
        # ``--profiles-dir`` becomes an env var for the duration of the
        # call; the warehouse loader's three-path resolution honours
        # ``DBT_PROFILES_DIR`` first. It is intentionally NOT routed
        # through ``canonicalise_user_path`` — the dbt convention places
        # ``profiles.yml`` at ``~/.dbt/`` which lives outside the project
        # tree, and the symlink-containment gate would reject every
        # realistic value. Apply ``expanduser`` + ``resolve`` for
        # symlink-loop safety; ``profiles.yml`` itself is parsed by the
        # warehouse loader which has its own existence/shape gate.
        if args.profiles_dir is not None:
            try:
                profiles_dir_resolved = Path(args.profiles_dir).expanduser().resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise CliPathError(
                    f"--profiles-dir {args.profiles_dir!r} could not be resolved: {exc}",
                    remediation="Pass an absolute or ~-prefixed path that exists.",
                ) from exc
            os.environ["DBT_PROFILES_DIR"] = str(profiles_dir_resolved)

        # 1. Manifest load + model selection.
        manifest = manifest_module.load(project_dir, manifest_path=manifest_override)
        model = manifest.get_model(args.model)

        # 2. Warehouse profile + adapter.
        profile = warehouse_module.load_profile(project_dir)
        adapter = _make_warehouse_adapter(profile)

        # ---- 1/5: safety -----------------------------------------------
        # US-007 / DEC-014 / DEC-026 — progress lines wrap each stage
        # entry/exit with live values + a paired ``done in <X>`` measurement.
        # No hardcoded duration hints; the progress fact for each stage
        # is computed from objects already in scope (model id, candidate
        # test count, kept_count × criteria_count) so the operator sees
        # the size of the work that's about to happen rather than a
        # stale estimate.
        if progress_on:
            emit_progress_entry(1, "safety", "building LLM request...")
        _t0 = time.monotonic()
        # Safety policy (the first stage in the documented pipeline
        # order — DEC-025 / CLAUDE.md "Pipeline shape"). US-006: apply
        # ``--mode`` via :meth:`SafetyPolicy.with_mode` so the
        # validators (notably the sample-mode WARNING DEC-021 of
        # ``safety-layer.md``) re-run on the override.
        policy = safety_module.load_safety_config(project_dir)
        mode_override = getattr(args, "mode", None)
        if mode_override is not None:
            policy = policy.with_mode(safety_module.SamplingMode(mode_override))
        if progress_on:
            emit_progress_done(1, "safety", time.monotonic() - _t0)

        # ---- 2/5: draft -------------------------------------------------
        draft_config = draft_module.load_draft_config(project_dir)
        client = _make_anthropic_client()
        if progress_on:
            emit_progress_entry(2, "draft", f"calling LLM (model {draft_config.model})...")
        _t0 = time.monotonic()
        draft_outcome = draft_module.draft_schema(
            model,
            adapter,
            policy,
            manifest,
            config=draft_config,
            _client=client,
        )
        if progress_on:
            emit_progress_done(2, "draft", time.monotonic() - _t0)

        # ---- 3/5: prune -------------------------------------------------
        prune_config = prune_module.load_prune_config(project_dir)
        candidate = draft_outcome.candidate
        candidate_test_count = sum(len(c.tests) for c in candidate.columns) + len(candidate.tests)
        if progress_on:
            emit_progress_entry(
                3,
                "prune",
                f"running {candidate_test_count} candidate tests against warehouse...",
            )
        _t0 = time.monotonic()
        prune_result = prune_module.prune_tests(
            model,
            adapter,
            draft_outcome.candidate,
            manifest,
            config=prune_config,
            project_dir=project_dir,
        )
        if progress_on:
            emit_progress_done(3, "prune", time.monotonic() - _t0)

        # ---- 4/5: grade -------------------------------------------------
        # US-006 / DEC-004 — apply ``--min-score`` by re-validating the
        # frozen :class:`GradeConfig` with the override. Reporting-only:
        # we do NOT flip ``fail_on_below_threshold`` — the operator's
        # ``signalforge.yml`` owns that knob (DEC-011 path through the
        # grader's :class:`GradeBelowThresholdError`).
        grade_config = grade_module.load_grade_config(project_dir)
        if min_score_override is not None:
            grade_config = grade_module.GradeConfig.model_validate(
                {**grade_config.model_dump(), "min_mean_score": min_score_override}
            )
        kept_count = prune_result.kept_count
        # Honour ``GradeConfig.rubric`` overrides — the operator may
        # ship a custom rubric in ``signalforge.yml grade:`` (or via
        # ``--config``) that has a different criterion count than the
        # default. ``rubric is None`` means "use DEFAULT_RUBRIC" per
        # ``grade-layer.md`` DEC-016, so the progress count matches the
        # rubric the LLM judge will actually iterate over.
        active_rubric = grade_config.rubric or DEFAULT_RUBRIC
        criteria_count = len(active_rubric)
        total_calls = kept_count * criteria_count
        if progress_on:
            emit_progress_entry(
                4,
                "grade",
                (
                    f"scoring {kept_count} artifacts × {criteria_count} "
                    f"criteria ({total_calls} calls)..."
                ),
            )
        _t0 = time.monotonic()
        grade_report = grade_module.grade_artifacts(
            model,
            draft_outcome.candidate,
            prune_result,
            config=grade_config,
            client=client,
            project_dir=project_dir,
        )
        if progress_on:
            emit_progress_done(4, "grade", time.monotonic() - _t0)

        # ---- 5/5: diff --------------------------------------------------
        # US-006 / DEC-020 — apply ``--format`` by re-validating the
        # frozen :class:`DiffConfig` with the override (mirrors
        # ``SafetyPolicy.with_mode``: re-runs validators including the
        # soft-warn / hard-cap invariant).
        diff_config = diff_module.load_diff_config(project_dir)
        format_override = getattr(args, "format", None)
        if format_override is not None and format_override != diff_config.render_kind:
            diff_config = diff_module.DiffConfig.model_validate(
                {**diff_config.model_dump(), "render_kind": format_override}
            )

        # US-006 / DEC-002 / DEC-010 — write/dry-run plumbing. Default
        # (neither flag): write sidecar to <project_dir>/.signalforge/diff.json,
        # do NOT write schema.yml. ``--write``: pass ``output_path``
        # pointing at <project_dir>/<model_dir>/schema.yml (next to the
        # model's .sql file). ``--dry-run``: ``write_sidecar=False`` AND
        # no ``output_path`` — pipeline runs, diff prints to stdout, but
        # nothing lands on disk. The argparse mutex group already
        # forbids ``--write --dry-run`` together.
        dry_run = getattr(args, "dry_run", False)
        write = getattr(args, "write", False)
        output_path: Path | None = None
        if write:
            # ``model.original_file_path`` is the dbt-relative path to
            # the model's ``.sql`` file (e.g. ``models/marts/customers.sql``).
            # ``schema.yml`` lives in the same directory as the model.
            model_relpath = Path(model.original_file_path)
            output_path = (project_dir / model_relpath).parent / "schema.yml"

        if progress_on:
            emit_progress_entry(5, "diff", "rendering...")
        _t0 = time.monotonic()
        diff_report = diff_module.render_diff(
            model,
            draft_outcome.candidate,
            prune_result,
            grading_report=grade_report,
            config=diff_config,
            output_path=output_path,
            write_sidecar=not dry_run,
            project_dir=project_dir,
        )
        if progress_on:
            emit_progress_done(5, "diff", time.monotonic() - _t0)

        # 6. Render to stdout via the in-process helper (DEC-015). The
        #    JSON sidecar (when not ``--dry-run``) was written by
        #    :func:`render_diff` above. ``--format json`` routes through
        #    the same :func:`render_to_text` helper because it dispatches
        #    on ``diff_config.render_kind``.
        rendered = diff_module.render_to_text(
            diff_report, config=diff_config, project_dir=project_dir
        )
        print(rendered)
        return 0

    except Exception as exc:  # noqa: BLE001 — the boundary catch (DEC-016)
        message = format_error_to_stderr(exc)
        print(message, file=sys.stderr)
        return map_exception_to_exit_code(exc)
