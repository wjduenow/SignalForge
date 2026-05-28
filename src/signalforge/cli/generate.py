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

US-006 of #22 layers two more prune-stage runtime-knob flags onto
``cmd_generate``: ``--scope {sample,full}`` (overrides
:attr:`signalforge.prune.PruneConfig.scope`) and ``--sample-strategy
{oneshot,materialised}`` (overrides
:attr:`signalforge.prune.PruneConfig.sample_strategy`). Both are
optional and independent — set one, the other, both, or neither.
Precedence: flag > config-file value > library default. The override
is applied via :meth:`PruneConfig.model_validate` (NOT
``model_copy(update=...)``) so every Pydantic validator re-runs — DEC-012
of ``plans/super/22-temp-table-sample.md``. Mirrors
:meth:`SafetyPolicy.with_mode` (DEC-018 of ``safety-layer.md``) and
:attr:`DiffConfig.render_kind` (DEC-020 of #9 — the canonical project
pattern for "CLI flag overrides config-file value").

Project-root resolution (DEC-001 + DEC-027):

* No flag → walk up from :func:`Path.cwd` to the nearest dir containing
  ``dbt_project.yml``. If we hit the filesystem root without finding one,
  exit 1 with remediation.
* ``--project-dir <PATH>`` → treat as an *absolute assertion*: the path
  must exist AND must contain ``dbt_project.yml``. The CLI does NOT walk
  up from the override (DEC-027) — passing the flag means "use this
  project, not whatever's above me".

Test-injection seam (DEC-013): the private factory
:func:`_make_warehouse_adapter` is patched by tests in
``tests/cli/test_generate.py`` to return a
:class:`tests.warehouse._fake.FakeBigQueryClient`-backed adapter. It is
``_``-prefixed (DEC of safety-layer.md / llm-drafter.md / etc.) — not part
of the public CLI contract.

LLM-client construction (DEC-006 of #135): the real-run pipeline no longer
builds an Anthropic client in the CLI. ``draft_schema`` / ``grade_artifacts``
thread ``client=None`` into :func:`signalforge.llm.call_llm`, which
lazy-builds the real client via the provider strategy resolved from the
stage's registry-validated ``provider`` config field. Tests inject a fake by
patching the provider's ``make_client`` (e.g.
``AnthropicProvider.make_client``) rather than a CLI helper. The
``--estimate`` short-circuit, which needs a concrete client up front for its
Anthropic-specific ``count_tokens`` probe, builds one via
``provider_for(draft_config.provider).make_client()``.

Stage-order test (DEC-025): ``test_generate_calls_stages_in_documented_order``
patches every stage entry point and asserts the documented
``safety → draft → prune → grade → diff`` ordering against
``parent.mock_calls``. Pins CLAUDE.md "Pipeline shape" as a contract.

Issue #37 / US-003 refactor (bd_1-scaffolding-4v1.3): the pipeline body
moved into :func:`_run_single_model` (returns :class:`_SingleModelOutcome`
— rendered text + per-model exit code + counts + exception class on
failure). :func:`_run_batch` iterates :func:`signalforge.manifest.select_models`
matches and calls ``_run_single_model`` per match with a FRESH
:class:`WarehouseAdapter` per iteration (DEC-010 of
``plans/super/37-multi-model-select.md`` — avoids ``_active_session_id``
bleed across in-process iterations). :func:`cmd_generate` is now a thin
dispatcher: ``args.select`` set → ``_run_batch``; else ``_run_single_model``
once. US-004 wires the ``--select`` argparse flag onto this seam (this
bead does NOT modify argparse).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from signalforge import diff as diff_module
from signalforge import draft as draft_module
from signalforge import grade as grade_module
from signalforge import manifest as manifest_module
from signalforge import prune as prune_module
from signalforge import safety as safety_module
from signalforge import warehouse as warehouse_module
from signalforge.cli import _estimate as estimate_module
from signalforge.cli._helpers import (
    canonicalise_user_path,
    emit_batch_progress_entry,
    emit_progress_done,
    emit_progress_entry,
    format_batch_summary,
    format_error_to_stderr,
    map_exception_to_exit_code,
    print_stderr,
    setup_logging,
    should_emit_progress,
)
from signalforge.cli.errors import (
    CliInputError,
    CliPathError,
    CliSelectorNoMatchError,
    CliSelectorParseError,
)
from signalforge.diff._test_file_writer import (
    _GENERATED_MARKER_PREFIX,
    write_test_file,
)
from signalforge.diff.models import DiffReport, ProposedTestFile
from signalforge.grade.rubric import DEFAULT_RUBRIC
from signalforge.llm import AnthropicClientProtocol
from signalforge.llm.providers import provider_for
from signalforge.manifest import select_models
from signalforge.manifest.errors import SelectorParseError
from signalforge.manifest.models import Manifest, Model
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
    US-006 of #22 adds the prune-stage runtime knobs ``--scope`` and
    ``--sample-strategy``; both override the same-named
    :class:`PruneConfig` fields via :meth:`model_validate` so validators
    re-run (DEC-012 of ``plans/super/22-temp-table-sample.md``).

    US-012 of #116 adds ``--force`` (DEC-010/DEC-014): a modifier on
    ``--write`` that governs whether an existing
    ``-- signalforge:generated``-marked ``.sql`` test file is overwritten.
    Hand-authored (unmarked) files are never overwritten, even with
    ``--force``. No-op without ``--write``.
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
    # Issue #37 / US-004 / DEC-001 / DEC-002 — positional ``<model>`` and
    # ``--select`` form a ``mutually_exclusive_group(required=True)``: the
    # operator MUST supply exactly one. Argparse rejects both-supplied
    # and neither-supplied combinations with its own usage error (exit
    # 2). The positional uses ``nargs="?"`` so it is optional INSIDE the
    # mutex; ``default=None`` keeps ``args.model`` falsy when ``--select``
    # is supplied so the dispatcher in ``cmd_generate`` reads it
    # accurately. ``--select`` help pins the grammar, three examples
    # (tag, path glob, comma-union — DEC-001), and the
    # sidecar-overwrite caveat (DEC-016) verbatim per the 5-surface
    # parity rule (cli-layer.md "Multi-surface parity for behaviour
    # changes"). The cookbook docs land in US-008.
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "model",
        metavar="<model>",
        nargs="?",
        default=None,
        help=(
            "Model under draft. Accepts a dbt unique_id "
            "(e.g. 'model.proj.customers') or a file path "
            "(e.g. 'models/marts/customers.sql')."
        ),
    )
    target_group.add_argument(
        "--select",
        metavar="<expr>",
        default=None,
        help=(
            "Run generate across multiple models in one process. "
            "<expr> is a comma-separated union of atoms: "
            "tag:<name>, path:<glob> (shell-style fnmatch), "
            "or a bare unique_id / file path. Examples: "
            "tag:staging | path:models/marts/* | "
            "tag:staging,path:models/marts/*. "
            "Caveat: multi-model runs overwrite .signalforge/grade.json "
            "and .signalforge/diff.json per model; only the last model's "
            "sidecars persist. Use the shell-loop pattern "
            "(docs/cli-ops.md § Running across many models) for "
            "per-model sidecars."
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
            "<project_dir>/<model_dir>/schema.yml. ADDITIONALLY writes each "
            "proposed singular .sql business-rule test to its tests/ path "
            "(DEC-010/DEC-014 of #116). Sidecar JSON is still written to "
            "<project_dir>/.signalforge/diff.json."
        ),
    )
    # US-012 of #116 / DEC-010 — overwrite policy for proposed singular
    # .sql test files written by --write. NOT mutually exclusive with the
    # write/dry-run group (a bare modifier on --write); a no-op without
    # --write. New files always write. An existing file carrying our
    # ``-- signalforge:generated`` marker is overwritten ONLY with --force
    # (skipped + WARNING otherwise). A file WITHOUT the marker
    # (hand-authored) is NEVER overwritten, even with --force.
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "With --write, overwrite an existing proposed .sql test file "
            "ONLY when it carries SignalForge's '-- signalforge:generated' "
            "header marker. Hand-authored .sql files (no marker) are NEVER "
            "overwritten, even with --force. No-op without --write "
            "(DEC-010 of #116)."
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
    # US-005 of #36 — pre-flight cost preview. Mutually exclusive with
    # --write / --dry-run (the argparse group already enforces this →
    # SystemExit(2) which ``main`` converts to exit code 2). Runs the
    # full prelude (configs + profile + adapter + anthropic client) and
    # the in-CLI estimate engine + renderer; no billable Anthropic or
    # warehouse calls are made.
    write_group.add_argument(
        "--estimate",
        action="store_true",
        help=(
            "Print pre-flight cost estimate (uses count_tokens + "
            "BigQuery dryRun only; no billable Anthropic or warehouse "
            "calls); mutually exclusive with --write / --dry-run."
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
    # US-006 of #22 / DEC-011 / DEC-012 — prune scope and sample-strategy
    # overrides. Argparse-level ``choices`` rejection produces exit 2 (the
    # argparse default). Both are ``default=None`` sentinels so we know
    # whether the operator set the flag (override) or not (config-file
    # value applies). The override is applied via
    # :meth:`PruneConfig.model_validate` (NOT ``model_copy(update=...)``)
    # so every Pydantic validator re-runs — mirrors
    # :meth:`SafetyPolicy.with_mode` (DEC-018 of ``safety-layer.md``)
    # and the ``DiffConfig.render_kind`` graduation in #9 (DEC-020).
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


def _make_warehouse_adapter(profile: warehouse_module.DbtProfileTarget) -> WarehouseAdapter:
    """Construct the :class:`WarehouseAdapter` for the resolved profile.

    Tests patch this to return a fake-backed adapter (e.g. a
    :class:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter`
    constructed with a :class:`tests.warehouse._fake.FakeBigQueryClient`).
    """
    return WarehouseAdapter.from_profile(profile)


# ---------------------------------------------------------------------------
# Proposed-test-file write path (US-012 of #116 — DEC-010 / DEC-014)
# ---------------------------------------------------------------------------


def _split_marked_sql(marked_sql: str) -> tuple[str, str]:
    """Split a marker-prefixed ``ProposedTestFile.sql`` into ``(args_hash, body)``.

    :class:`signalforge.diff.models.ProposedTestFile.sql` is emitted with the
    ``-- signalforge:generated <hash>`` header marker already prepended (via
    :func:`signalforge.diff._test_file_writer._with_marker`), so the sidecar
    carries exactly the bytes the writer would persist. But
    :func:`signalforge.diff._test_file_writer.write_test_file` re-prepends the
    marker from the ``args_hash`` kwarg — passing the marked content verbatim
    would double the marker. This helper recovers the bare body + the hash so
    the writer reconstructs the identical marked output.

    The marker line is ``"-- signalforge:generated <hash>"`` followed by a
    single blank line then the body. A defensive fallback handles a payload
    that (for any forward-compat reason) does NOT carry the marker: the whole
    string is treated as the body and an empty hash is returned, so
    ``write_test_file`` still produces valid (if hash-less) marked SQL rather
    than crashing.
    """
    first_newline = marked_sql.find("\n")
    first_line = marked_sql if first_newline == -1 else marked_sql[:first_newline]
    if not first_line.startswith(_GENERATED_MARKER_PREFIX):
        # No recognisable marker — treat the whole payload as the body.
        return "", marked_sql
    args_hash = first_line[len(_GENERATED_MARKER_PREFIX) :].strip()
    # Drop the marker line. ``_with_marker`` writes "<marker>\n\n<body>" so a
    # leading blank line follows the marker; strip exactly one to recover the
    # bare body. ``write_test_file`` re-adds the "\n\n" separator.
    remainder = marked_sql[first_newline + 1 :] if first_newline != -1 else ""
    if remainder.startswith("\n"):
        remainder = remainder[1:]
    return args_hash, remainder


def _existing_file_is_signalforge_generated(path: Path) -> bool:
    """Return ``True`` iff the on-disk file at ``path`` carries our marker.

    A file is "ours" when its content begins with the
    ``-- signalforge:generated`` header marker (DEC-010). Hand-authored test
    files do not carry it, so this is the gate that protects human-written
    SQL from ever being clobbered (even with ``--force``). A read failure
    (permissions, decode error) is treated conservatively as "not ours" so
    the write path refuses to overwrite rather than risk clobbering content
    it could not inspect.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            head = handle.read(len(_GENERATED_MARKER_PREFIX))
    except (OSError, UnicodeDecodeError):
        return False
    return head == _GENERATED_MARKER_PREFIX


def _write_proposed_test_files(
    report: DiffReport,
    *,
    project_dir: Path,
    force: bool,
) -> None:
    """Materialise ``report.proposed_test_files`` to disk under ``--write``.

    Implements the DEC-010 overwrite policy for the singular ``.sql``
    business-rule tests proposed by the diff render. Called by
    :func:`_run_single_model` ONLY on the ``--write`` path (NOT ``--dry-run``,
    which writes nothing). For each :class:`ProposedTestFile`:

    * the relative ``tests/<...>.sql`` ``path`` flows through
      :func:`signalforge.cli._helpers.canonicalise_user_path` (symlink /
      containment → :class:`CliPathError`) so a crafted proposal can never
      escape the project tree;
    * if the target does NOT exist → write it;
    * if it EXISTS and carries our ``-- signalforge:generated`` marker:
      overwrite when ``force`` is set; otherwise skip with a stderr WARNING
      (refuse to clobber our own output without ``--force``);
    * if it EXISTS WITHOUT the marker (hand-authored) → NEVER overwrite, even
      with ``force``; skip with a clear stderr WARNING.

    The actual byte-write delegates to
    :func:`signalforge.diff._test_file_writer.write_test_file` (the project's
    sixth fail-closed writer); the marker is reconstructed from the hash
    recovered by :func:`_split_marked_sql` so the on-disk content matches the
    sidecar's ``ProposedTestFile.sql`` byte-for-byte. The writer's own typed
    errors (``DiffTestFileWriteError`` / ``DiffTestFileRecordTooLargeError``)
    propagate to ``_run_single_model``'s single boundary catch, which maps
    them via the exit-code table — no new error class needed.

    Lazy-format JSON logging throughout (logger grep-gate at
    ``tests/llm/test_logger_grep_gate.py``).
    """
    proposed: tuple[ProposedTestFile, ...] = report.proposed_test_files
    if not proposed:
        return
    for entry in proposed:
        # Canonicalise the proposed relative path against the project root.
        # ``anchor_to_filename`` already slugged every component, but the
        # symlink/containment gate is defence-in-depth (mirrors the writer's
        # own second check). A failure re-raises as ``CliPathError`` from
        # ``canonicalise_user_path``.
        canonical = canonicalise_user_path(entry.path, project_dir)
        # ``canonicalise_user_path`` returns ``None`` only for a ``None``
        # input; ``entry.path`` is always a non-empty str, so ``canonical``
        # is a ``Path`` here. The assert documents the invariant for pyright.
        assert canonical is not None

        if canonical.exists():
            is_ours = _existing_file_is_signalforge_generated(canonical)
            if not is_ours:
                # Hand-authored — never clobber, even with --force.
                print_stderr(
                    f"WARNING: refusing to overwrite hand-authored {entry.path} "
                    "(no '-- signalforge:generated' marker); skipping."
                )
                _LOGGER.warning(
                    "skipping write of proposed .sql test (hand-authored, no marker): %s",
                    json.dumps({"path": entry.path, "force": force}),
                )
                continue
            if not force:
                # Marked but no --force — refuse to clobber our own output.
                print_stderr(
                    f"WARNING: {entry.path} already exists; pass --force to "
                    "overwrite SignalForge-generated tests. Skipping."
                )
                _LOGGER.warning(
                    "skipping overwrite of marked proposed .sql test (--force not set): %s",
                    json.dumps({"path": entry.path}),
                )
                continue
            # Marked AND --force — fall through to overwrite.

        args_hash, body = _split_marked_sql(entry.sql)
        written = write_test_file(
            body,
            relative_path=entry.path,
            args_hash=args_hash,
            project_dir=project_dir,
        )
        _LOGGER.info(
            "wrote proposed .sql test: %s",
            json.dumps({"path": str(written)}),
        )


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
# Per-model + batch outcome dataclasses (issue #37 / US-003 — DEC-010)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SingleModelOutcome:
    """Result of one :func:`_run_single_model` invocation.

    Fields:

    * ``model_unique_id`` — the model that was run.
    * ``exit_code`` — ``0`` on success, ``1`` / ``2`` / ``3`` per the
      four-tier taxonomy in :data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`
      on failure.
    * ``kept_count`` / ``dropped_count`` / ``flagged_count`` — pulled from
      the :class:`signalforge.diff.DiffReport` on success; all zero on
      failure (the diff stage may not have run).
    * ``rendered_text`` — exact stdout content for this model (including
      the trailing newline that ``print(rendered)`` produces). Empty
      string on failure so the dispatcher's ``if r.rendered_text`` check
      naturally skips it.
    * ``duration_seconds`` — wall-clock from ``_run_single_model`` entry
      to exit (success or failure).
    * ``exception_class_name`` — ``None`` on success; on failure, the
      ``type(exc).__name__`` of the exception that was caught at the
      ``_run_single_model`` boundary. Surfaces in the US-005 batch summary
      so failed-model rows render ``(LLMRateLimitError)`` etc.

    Frozen so test assertions can rely on stable field values. Carries no
    user-content payloads that would warrant a custom ``__repr__`` (the
    rendered text is bounded by the diff renderer's own truncation
    invariants — DEC-005 of ``diff-renderer.md``).
    """

    model_unique_id: str
    exit_code: int
    kept_count: int
    dropped_count: int
    flagged_count: int
    rendered_text: str
    duration_seconds: float
    exception_class_name: str | None


@dataclass(frozen=True)
class _BatchOutcome:
    """Result of one :func:`_run_batch` invocation.

    Fields:

    * ``per_model`` — outcomes in invocation order (matches the order
      :func:`signalforge.manifest.select_models` returns matches, which
      is ``unique_id`` lexicographic per its DEC-012).
    * ``total_exit_code`` — ``max(o.exit_code for o in per_model)``, with
      a default of ``0`` for the empty-tuple case. The empty tuple should
      never reach this dataclass — :func:`_run_batch` raises
      :class:`CliSelectorNoMatchError` before constructing one — but the
      default keeps the math safe if a future refactor changes the
      empty-match path.
    * ``duration_seconds`` — wall-clock from ``_run_batch`` entry to exit.

    Frozen for the same reasons :class:`_SingleModelOutcome` is.
    """

    per_model: tuple[_SingleModelOutcome, ...]
    total_exit_code: int
    duration_seconds: float


# ---------------------------------------------------------------------------
# Single-model pipeline body (issue #37 / US-003 — extracted from cmd_generate)
# ---------------------------------------------------------------------------


def _run_single_model(
    model: Model,
    manifest: Manifest,
    profile: warehouse_module.DbtProfileTarget,
    args: argparse.Namespace,
    *,
    project_dir: Path,
    batch_index: int | None = None,
    batch_count: int | None = None,
) -> _SingleModelOutcome:
    """Run the full safety → draft → prune → grade → diff pipeline for one model.

    Returns a :class:`_SingleModelOutcome` whose ``exit_code`` is mapped
    via :func:`signalforge.cli._helpers.map_exception_to_exit_code` per
    the four-tier taxonomy. The boundary catch lives INSIDE this function
    (mirrors the previous ``cmd_generate`` shape) so the dispatcher in
    :func:`cmd_generate` and the batch driver in :func:`_run_batch` both
    treat this helper as the unit of work — they read ``exit_code`` from
    the outcome rather than catching exceptions themselves. Per-model
    stderr formatting also happens here so batch mode preserves stderr
    ordering across iterations (each model's error follows its own
    progress lines, not a coalesced dump at end-of-batch).

    Constructs its OWN :class:`WarehouseAdapter` via
    :func:`_make_warehouse_adapter` (DEC-010 of
    ``plans/super/37-multi-model-select.md`` — fresh adapter per model
    in batch mode avoids ``_active_session_id`` bleed; single-model
    invocations also route through this helper so the seam stays single
    source of truth).

    ``batch_index`` / ``batch_count`` drive the per-model progress prefix
    ``[i/N] <unique_id>`` emitted at the head of the pipeline when BOTH
    are non-``None`` — i.e., only when :func:`_run_batch` is the caller
    (US-005 / DEC-014). The single-model positional path leaves both at
    their default ``None`` so the prefix is suppressed and the v0.1
    capsys-pinned stderr shape is preserved byte-for-byte.

    Inherits :func:`cmd_generate`'s per-flag override precedence and the
    ``--estimate`` short-circuit (DEC-009 of #36) — see
    :func:`cmd_generate`'s docstring for the full rules; this helper
    consumes the resulting ``args`` namespace verbatim.
    """
    quiet = bool(getattr(args, "quiet", False))
    verbose = bool(getattr(args, "verbose", False))
    progress_on = should_emit_progress(quiet=quiet, verbose=verbose)

    # US-005 / DEC-014 — per-model progress prefix. Only fires when this
    # helper is invoked from :func:`_run_batch` (both kwargs non-``None``);
    # the positional single-model path passes ``None`` for both and emits
    # NEITHER the prefix here NOR the summary in :func:`_run_batch`. TTY
    # / quiet / verbose gating delegates to :func:`should_emit_progress`
    # — same rules as the stage-N progress lines that follow.
    if progress_on and batch_index is not None and batch_count is not None:
        emit_batch_progress_entry(model.unique_id, batch_index, batch_count)

    start = time.monotonic()
    try:
        # 2. Warehouse adapter (manifest + profile resolved by caller).
        adapter = _make_warehouse_adapter(profile)

        # ---- US-005 of #36 --- --estimate short-circuit -------------
        # DEC-009: run the FULL prelude (every config load + anthropic
        # client) before dispatching to the estimate engine, so the
        # operator catches typos in signalforge.yml / --profiles-dir
        # BEFORE any LLM call. Skip ONLY the four pipeline orchestrators
        # (draft_schema / prune_tests / grade_artifacts / render_diff).
        # The engine handles its own partial-failure degrade for the
        # warehouse-bytes step (DEC-005); every other failure (LLM auth,
        # bad config, unknown model) propagates through the existing
        # try/except Exception boundary below.
        if getattr(args, "estimate", False):
            # DEC-009 + B-1 (QG pass 1) — apply the same CLI flag overrides
            # the real-run prelude applies, so the projected cost matches
            # the run the operator would actually issue. ``--mode`` flows
            # through ``SafetyPolicy.with_mode`` (re-runs validators per
            # ``safety-layer.md`` DEC-018); ``--scope`` / ``--sample-strategy``
            # flow into ``PruneConfig.model_validate`` (extra="forbid", so
            # typos fail loud). ``--min-score`` is intentionally NOT applied
            # — it gates the post-run verdict, not the cost projection.
            policy = safety_module.load_safety_config(project_dir)
            mode_override = getattr(args, "mode", None)
            if mode_override is not None:
                policy = policy.with_mode(safety_module.SamplingMode(mode_override))
            draft_config = draft_module.load_draft_config(project_dir)
            grade_config = grade_module.load_grade_config(project_dir)
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
            diff_module.load_diff_config(project_dir)
            # DEC-006 of #135 — the four pipeline orchestrators now let
            # ``call_llm`` lazy-build the client via the configured provider,
            # so the CLI no longer constructs one for the real-run path. The
            # ``--estimate`` engine takes the same shape: each stage's
            # ``count`` call dispatches through
            # ``provider_for(config.provider).estimate_input_tokens(...)``
            # (#136 US-005 DEC-003), so a non-Anthropic provider's
            # ``--estimate`` path now works too — OpenAI counts locally via
            # ``tiktoken`` and ignores the threaded client.
            #
            # The engine still accepts a single optional client object so
            # Anthropic's per-call SDK construction is avoidable when the
            # CLI already has one in scope. For non-Anthropic providers we
            # pass ``None`` and the provider handles it (no-op for OpenAI;
            # error for any provider that genuinely needs an SDK client and
            # was given none — only Anthropic does today). The single-client
            # design still requires the two stages' providers to match so
            # we don't silently project grade-stage cost through the
            # drafter's vendor when they diverge.
            if draft_config.provider != grade_config.provider:
                raise CliInputError(
                    "--estimate requires draft.provider and grade.provider to match "
                    f"(got {draft_config.provider!r} and {grade_config.provider!r}).",
                    remediation=(
                        "Set the same provider for both stages, or run without "
                        "--estimate until per-provider estimation support lands."
                    ),
                )
            # Build a concrete client only for providers that need one
            # (Anthropic). Providers that count locally (OpenAI) ignore
            # the kwarg; passing ``None`` keeps us from constructing an
            # SDK client + requiring its API key purely for a local count.
            client: object | None
            if draft_config.provider == "anthropic":
                client = cast(
                    "AnthropicClientProtocol",
                    provider_for(draft_config.provider).make_client(),
                )
            else:
                client = None
            report = estimate_module.estimate(
                model,
                manifest,
                draft_config,
                grade_config,
                prune_config,
                adapter,
                client,
                project_dir=project_dir,
            )
            print(estimate_module.render(report))
            # ``_run_single_model`` returns a typed outcome; the estimate
            # short-circuit treats the run as exit-0 success with no
            # rendered diff (stdout already carries the estimate body
            # above). Batch mode (``--select``) inherits this shape via
            # ``_run_batch``'s outcome aggregation.
            return _SingleModelOutcome(
                model_unique_id=model.unique_id,
                exit_code=0,
                kept_count=0,
                dropped_count=0,
                flagged_count=0,
                rendered_text="",
                duration_seconds=time.monotonic() - start,
                exception_class_name=None,
            )

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
        # DEC-006 of #135 — the CLI no longer constructs an Anthropic client
        # for the real-run path. ``draft_schema`` threads ``_client=None``
        # into ``call_llm``, which lazy-builds the real client via the
        # provider strategy resolved from ``draft_config.provider`` (the
        # registry-validated config field, DEC-007). Tests inject a fake by
        # patching the provider's ``make_client`` rather than a CLI helper.
        draft_config = draft_module.load_draft_config(project_dir)
        if progress_on:
            emit_progress_entry(2, "draft", f"calling LLM (model {draft_config.model})...")
        _t0 = time.monotonic()
        draft_outcome = draft_module.draft_schema(
            model,
            adapter,
            policy,
            manifest,
            config=draft_config,
            _client=None,
        )
        if progress_on:
            emit_progress_done(2, "draft", time.monotonic() - _t0)

        # ---- 3/5: prune -------------------------------------------------
        # US-006 of #22 / DEC-011 / DEC-012 — apply ``--scope`` and
        # ``--sample-strategy`` by re-validating the frozen
        # :class:`PruneConfig` with the overrides. ``model_validate`` (NOT
        # ``model_copy(update=...)``) so every Pydantic validator re-runs:
        # the ``Literal`` validators on ``scope`` / ``sample_strategy``
        # reject typos like ``"materialized"`` (US spelling), and the
        # ``_positive`` field validator re-fires on the rest of the dump.
        # Mirrors :meth:`SafetyPolicy.with_mode` (DEC-018 of
        # ``safety-layer.md``) and ``DiffConfig.render_kind`` (DEC-020 of
        # #9). Both flags are independent — set one, the other, both, or
        # neither; the unset axis falls through to the config-file value.
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
        candidate = draft_outcome.candidate
        candidate_test_count = sum(len(c.tests) for c in candidate.columns) + len(candidate.tests)
        # DEC-004 of #35 — operator-visible signal when the prune-stage
        # short-circuit (US-002) fires. The engine drains every candidate
        # to ``kept-without-evidence`` when ``prune.enabled=false``; this
        # INFO line surfaces that fact at the prune-stage progress block
        # so the operator sees it in stage-order with the existing
        # ``emit_progress_entry`` narrative. Lazy-format JSON per
        # ``prune-engine.md`` DEC-017 (the grep gate at
        # ``tests/llm/test_logger_grep_gate.py`` rejects f-strings in
        # ``_LOGGER`` calls). Not a WARNING: the operator explicitly
        # opted in via config — surfacing a WARNING on every run would
        # be nagging, not signal.
        if not prune_config.enabled:
            _LOGGER.info(
                "prune disabled in signalforge.yml; routing all candidates to "
                "kept-without-evidence: %s",
                json.dumps(
                    {
                        "model_unique_id": model.unique_id,
                        "candidate_count": candidate_test_count,
                    }
                ),
            )
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
        min_score_override = getattr(args, "min_score", None)
        if min_score_override is not None:
            grade_config = grade_module.GradeConfig.model_validate(
                {**grade_config.model_dump(), "min_mean_score": min_score_override}
            )
        # Count artifacts the grader will actually iterate over. The
        # grade engine's ``_stable_artifact_pairs`` (DEC-018) yields one
        # entry per (column.description, column.rationale, model.description,
        # model.rationale, column-scoped test rationale, model-scoped test
        # rationale) — independent of which tests prune kept. Earlier CLI
        # versions used ``prune_result.kept_count`` here, which conflated
        # "tests surviving prune" with "artifacts visible to the grader"
        # and emitted "0 artifacts" runs when prune dropped everything
        # (issue #10 follow-up).
        candidate = draft_outcome.candidate
        artifact_count = (
            2 * len(candidate.columns)  # column description + rationale per column
            + 2  # model description + rationale
            + sum(len(c.tests) for c in candidate.columns)  # column-scoped test rationales
            + len(candidate.tests)  # model-scoped test rationales
        )
        # Honour ``GradeConfig.rubric`` overrides — the operator may
        # ship a custom rubric in ``signalforge.yml grade:`` (or via
        # ``--config``) that has a different criterion count than the
        # default. ``rubric is None`` means "use DEFAULT_RUBRIC" per
        # ``grade-layer.md`` DEC-016, so the progress count matches the
        # rubric the LLM judge will actually iterate over.
        active_rubric = grade_config.rubric or DEFAULT_RUBRIC
        criteria_count = len(active_rubric)
        total_calls = artifact_count * criteria_count
        if progress_on:
            emit_progress_entry(
                4,
                "grade",
                (
                    f"scoring {artifact_count} artifacts × {criteria_count} "
                    f"criteria ({total_calls} calls)..."
                ),
            )
        _t0 = time.monotonic()
        # DEC-006 of #135 — ``client=None`` lets ``grade_artifacts`` thread it
        # into ``call_llm``, which lazy-builds via the provider resolved from
        # ``grade_config.provider`` (independent of the drafter's provider).
        grade_report = grade_module.grade_artifacts(
            model,
            draft_outcome.candidate,
            prune_result,
            config=grade_config,
            client=None,
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
        # model's .sql file) AND materialise every proposed singular .sql
        # business-rule test (US-012 of #116 — handled after render_diff
        # returns, see ``_write_proposed_test_files``). ``--dry-run``:
        # ``write_sidecar=False`` AND no ``output_path`` — pipeline runs,
        # diff prints to stdout, but nothing lands on disk (no schema.yml,
        # no sidecar, no .sql files). The argparse mutex group already
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

        # US-012 of #116 / DEC-010 / DEC-014 — on ``--write`` (NOT
        # ``--dry-run``), additionally materialise every proposed singular
        # ``.sql`` business-rule test to its ``tests/`` path. The overwrite
        # policy (new → write; marked + --force → overwrite; marked w/o
        # --force → skip+WARNING; unmarked/hand-authored → never overwrite)
        # lives in :func:`_write_proposed_test_files`. ``write_sidecar`` was
        # already gated off for ``--dry-run`` above; ``write`` is the same
        # boolean used to gate ``output_path``, so the ``.sql`` writes ride
        # the identical ``--write`` gate.
        if write:
            force = getattr(args, "force", False)
            _write_proposed_test_files(diff_report, project_dir=project_dir, force=force)

        # 6. Render to stdout via the in-process helper (DEC-015). The
        #    JSON sidecar (when not ``--dry-run``) was written by
        #    :func:`render_diff` above. ``--format json`` routes through
        #    the same :func:`render_to_text` helper because it dispatches
        #    on ``diff_config.render_kind``.
        rendered = diff_module.render_to_text(
            diff_report, config=diff_config, project_dir=project_dir
        )
        # ``print(rendered)`` appends ``\n`` to match the v0.1 stdout
        # shape; the dispatcher does ``sys.stdout.write(outcome.rendered_text)``
        # so we pre-build the trailing newline here. ``print``'s default
        # ``end="\n"`` is what every existing snapshot test pins.
        rendered_text = f"{rendered}\n"
        return _SingleModelOutcome(
            model_unique_id=model.unique_id,
            exit_code=0,
            kept_count=diff_report.kept_count,
            dropped_count=diff_report.dropped_count,
            flagged_count=diff_report.flagged_count,
            rendered_text=rendered_text,
            duration_seconds=time.monotonic() - start,
            exception_class_name=None,
        )

    except Exception as exc:  # noqa: BLE001 — the boundary catch (DEC-016)
        message = format_error_to_stderr(exc)
        print_stderr(message)
        return _SingleModelOutcome(
            model_unique_id=model.unique_id,
            exit_code=map_exception_to_exit_code(exc),
            kept_count=0,
            dropped_count=0,
            flagged_count=0,
            rendered_text="",
            duration_seconds=time.monotonic() - start,
            exception_class_name=type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Batch driver (issue #37 / US-003 — DEC-010)
# ---------------------------------------------------------------------------


def _run_batch(
    manifest: Manifest,
    profile: warehouse_module.DbtProfileTarget,
    args: argparse.Namespace,
    *,
    project_dir: Path,
) -> _BatchOutcome:
    """Iterate :func:`signalforge.manifest.select_models` and call
    :func:`_run_single_model` per match.

    Pre-flight gates:

    * :class:`signalforge.manifest.SelectorParseError` → re-raised as
      :class:`CliSelectorParseError(expr, cause=...)` so the CLI's
      exception ladder catches a single :class:`CliInputError` subclass
      (DEC-007 of ``plans/super/37-multi-model-select.md``).
    * Empty match tuple → :class:`CliSelectorNoMatchError(expr)` (DEC-006).

    Both fire BEFORE any model iteration. After the gates, each
    per-model call to :func:`_run_single_model` is treated as
    independent: that helper carries its own boundary catch (mirroring
    :ref:`cli-layer.md` DEC-016) and returns an outcome with the per-model
    exit code, so one model failing does not abort the batch.

    ``total_exit_code`` is ``max(o.exit_code for o in per_model)`` —
    the four-tier taxonomy is conveniently a severity rank (0 < 1 < 2 <
    3), so ``max`` is the right aggregator (DEC-004 of #37).

    A FRESH adapter is constructed per model (inside
    :func:`_run_single_model`) — see DEC-010 of #37 for the
    ``_active_session_id`` bleed avoidance rationale.

    NOTE: this bead (US-003) wires the helper but does NOT modify
    argparse. US-004 adds the ``--select`` flag; this driver becomes
    reachable from :func:`cmd_generate`'s dispatcher when
    ``args.select`` is set.
    """
    start = time.monotonic()
    expr = getattr(args, "select", None)
    assert expr is not None, "_run_batch must only be invoked when args.select is set"

    try:
        matched = select_models(manifest, expr)
    except SelectorParseError as exc:
        raise CliSelectorParseError(expr=expr, cause=exc) from exc

    if not matched:
        raise CliSelectorNoMatchError(expr=expr)

    total = len(matched)
    outcomes: list[_SingleModelOutcome] = []
    for index, model in enumerate(matched, start=1):
        outcome = _run_single_model(
            model,
            manifest,
            profile,
            args,
            project_dir=project_dir,
            batch_index=index,
            batch_count=total,
        )
        outcomes.append(outcome)

    per_model = tuple(outcomes)
    total_exit_code = max((o.exit_code for o in per_model), default=0)
    batch_outcome = _BatchOutcome(
        per_model=per_model,
        total_exit_code=total_exit_code,
        duration_seconds=time.monotonic() - start,
    )

    # US-005 / DEC-005 — aggregated summary to stderr. Emit when (a) ≥2
    # models matched OR (b) ≥1 model failed. The (single-match AND zero
    # failures) case suppresses the summary so the UX matches the v0.1
    # single-model positional path in that degenerate case. The summary
    # is "always emit when the condition fires" (DEC-005) — NOT
    # TTY-gated, because the failure list is operator-actionable signal
    # even in CI / pipeline logs. ``--quiet`` still suppresses it (the
    # operator explicitly asked for less output); ``--verbose`` is a
    # no-op on this path (the summary already always fires when the
    # condition holds).
    quiet = bool(getattr(args, "quiet", False))
    failed_count = sum(1 for o in per_model if o.exit_code != 0)
    should_summarise = len(per_model) >= 2 or failed_count >= 1
    if should_summarise and not quiet:
        # ``format_batch_summary`` returns a string ending in ``\n``;
        # pass ``end=""`` so :func:`print_stderr` doesn't add another.
        print_stderr(format_batch_summary(batch_outcome), end="", flush=True)

    return batch_outcome


# ---------------------------------------------------------------------------
# Subcommand entry point
# ---------------------------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> int:
    """Run the full pipeline for ``args.model`` (single-model) or for every
    model matched by ``args.select`` (batch).

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

    Per-flag override precedence (US-006 / US-006 of #22):

    * ``--mode`` > ``safety.mode`` in ``signalforge.yml`` > library default.
    * ``--min-score`` > ``grade.min_mean_score`` > library default.
    * ``--format`` > ``diff.render_kind`` > library default (``"ansi"``).
    * ``--scope`` > ``prune.scope`` > library default (``"sample"``).
    * ``--sample-strategy`` > ``prune.sample_strategy`` > library default
      (``"materialised"``).

    The prune overrides apply via :meth:`PruneConfig.model_validate`
    (NOT ``model_copy(update=...)``) so every Pydantic validator
    re-runs — DEC-012 of ``plans/super/22-temp-table-sample.md``.

    Issue #37 / US-003 (bd_1-scaffolding-4v1.3) split the per-model
    pipeline body into :func:`_run_single_model`. This function is now a
    thin dispatcher:

    * ``args.select`` set → :func:`_run_batch` (US-004 wires the flag).
    * else → :func:`_run_single_model` once with ``args.model``.

    The outer try/except in this function catches errors from the
    pre-pipeline scaffolding (project-root resolution, manifest load,
    profile load, ``--min-score`` range check, selector parse / no-match
    raised by ``_run_batch``); per-model failures are caught INSIDE
    :func:`_run_single_model` so batch mode's stderr ordering is
    preserved.
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

        # 1. Manifest load. Model selection is deferred to the dispatch
        #    branches below — single-model uses ``manifest.get_model``;
        #    batch uses ``select_models`` inside ``_run_batch``.
        manifest = manifest_module.load(project_dir, manifest_path=manifest_override)

        # 2. Warehouse profile. The adapter is constructed inside
        #    :func:`_run_single_model` (DEC-010 of #37 — fresh adapter
        #    per model in batch mode; the same seam runs in single-model
        #    mode so the construction path stays single source of truth).
        profile = warehouse_module.load_profile(project_dir)

        # ---- Dispatch (issue #37 / US-003) ---------------------------------
        # US-004 will add the ``--select`` argparse flag; until then,
        # ``args.select`` is undefined unless a test injects it via
        # ``argparse.Namespace`` directly. ``getattr`` with a default of
        # ``None`` is the safe accessor that survives both worlds.
        # ``is not None`` (not truthiness) so an empty-string ``--select ""``
        # — which argparse accepts and the mutex group treats as "provided" —
        # routes through ``_run_batch`` and surfaces as ``CliSelectorParseError``
        # (DEC-007) rather than silently falling through to the single-model
        # branch where ``args.model`` is ``None``.
        select_expr = getattr(args, "select", None)
        if select_expr is not None:
            outcome = _run_batch(manifest, profile, args, project_dir=project_dir)
            # Write each model's rendered text to stdout in invocation
            # order (US-005 owns the ``[i/N]`` prefix on stderr and the
            # aggregated summary emission; this bead only wires the
            # plumbing).
            for r in outcome.per_model:
                if r.rendered_text:
                    sys.stdout.write(r.rendered_text)
            return outcome.total_exit_code

        # Single-model path. ``manifest.get_model`` may raise
        # :class:`signalforge.manifest.errors.ModelNotFoundError` (tier 2);
        # the outer try catches it.
        model = manifest.get_model(args.model)
        single_outcome = _run_single_model(model, manifest, profile, args, project_dir=project_dir)
        if single_outcome.rendered_text:
            sys.stdout.write(single_outcome.rendered_text)
        return single_outcome.exit_code

    except Exception as exc:  # noqa: BLE001 — the boundary catch (DEC-016)
        message = format_error_to_stderr(exc)
        print_stderr(message)
        return map_exception_to_exit_code(exc)
