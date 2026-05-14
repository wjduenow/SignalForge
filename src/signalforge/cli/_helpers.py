"""Shared CLI helpers (US-003).

Five helpers that subsequent stories build on:

* :func:`canonicalise_user_path` — symlink-hardened path-safety wrapper
  (DEC-007).
* :func:`setup_logging` — single :func:`logging.basicConfig` call wired
  to the verbose / quiet flags (DEC-016 partial — full panic-path lands
  with ``--verbose`` in US-007).
* :func:`format_error_to_stderr` — single source of truth for the stderr
  shape across every typed exception the CLI catches (DEC-008, DEC-017).
* :func:`map_exception_to_exit_code` — single mapping table from typed
  exception to one of the four exit-code tiers (DEC-008, DEC-019). The
  table is the load-bearing artefact for the AST scan US-008 will add to
  ``tests/test_audit_completeness.py``; this story populates it
  comprehensively across every exception currently exported from each
  stage's public surface.
* :func:`_safe_excepthook` — strips tracebacks from anything that escapes
  the main ``try / except`` (DEC-016).

The lazy-format JSON logger convention (``_LOGGER.info("...: %s",
json.dumps({...}))``) extends to the CLI: the grep gate at
``tests/llm/test_logger_grep_gate.py`` adds ``src/signalforge/cli/`` as
its 6th directory in this story.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Avoid a runtime circular import: ``signalforge.cli.generate`` imports
    # this module's helpers, and :func:`format_batch_summary` needs the
    # private ``_BatchOutcome`` dataclass shape only at type-check time.
    from signalforge.cli.generate import _BatchOutcome

from signalforge._common.ansi_safety import strip_ansi_escapes
from signalforge._common.path_safety import PathContainmentError, canonicalise_path
from signalforge.cli.errors import (
    CliError,
    CliInitDemoCopyError,
    CliInitDemoDestExistsError,
    CliInitDemoDestUnsafeError,
    CliInitDemoFixtureMissingError,
    CliInputError,
    CliPathError,
    CliSelectorNoMatchError,
    CliSelectorParseError,
)
from signalforge.demo import (
    DemoDestExistsError,
    DemoDestUnsafeError,
    DemoFixtureMissingError,
    DemoPathError,
)

# --- per-stage public-surface imports for the exit-code table ---------------
# Importing from each ``signalforge.<stage>`` package mirrors how the rest of
# the repo consumes typed exceptions (the stage __init__ is the public
# contract; private modules are an implementation detail). See DEC-013 of
# this ticket for the upstream alignment work.
from signalforge.diff import (
    DiffCandidateModelMismatchError,
    DiffError,
    DiffGradingReportModelMismatchError,
    DiffInputTooLargeError,
    DiffPruneResultModelMismatchError,
    DiffSidecarRecordTooLargeError,
    DiffSidecarWriteError,
)
from signalforge.draft import (
    DraftConfigInvalidError,
    DraftConfigNotFoundError,
    DraftError,
    LLMOutputAnchorContractError,
    LLMOutputError,
    LLMOutputJSONError,
    LLMOutputValidationError,
    LLMResponseAuditRecordTooLargeError,
    LLMResponseAuditWriteError,
    PromptEnvelopeBreachError,
)
from signalforge.grade import (
    GradeAuditRecordTooLargeError,
    GradeAuditWriteError,
    GradeBelowThresholdError,
    GradeBudgetExceededError,
    GradeConfigError,
    GradeError,
    GradeLLMError,
    GradeOutputError,
    GradePromptEnvelopeBreachError,
    GradeRubricError,
)
from signalforge.llm import (
    EstimateUnknownModelError,
    LLMAuthError,
    LLMCacheTooLargeError,
    LLMConnectionError,
    LLMError,
    LLMHelperError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMServerError,
)
from signalforge.manifest import (
    ManifestError,
    ManifestNotFoundError,
    ModelDisabledError,
    ModelMissingSqlError,
    ModelNotFoundError,
    ModelPathOutsideProjectError,
    SelectorParseError,
    UnsupportedManifestVersionError,
)
from signalforge.prune import (
    PruneAuditRecordTooLargeError,
    PruneAuditWriteError,
    PruneConfigError,
    PruneError,
    PruneTimeoutError,
    PruneTrustedModelNotFoundError,
)
from signalforge.safety import (
    AuditRecordTooLargeError,
    AuditWriteError,
    ColumnNotInModelError,
    ConfigNotFoundError,
    InvalidConfigError,
    InvalidPatternError,
    InvalidSamplingModeError,
    PolicyValidationError,
    SafetyError,
    UnknownConfigKeyError,
)
from signalforge.warehouse import (
    BytesBilledExceededError,
    ColumnNotFoundError,
    EstimateNotSupportedError,
    InvalidIdentifierError,
    ManifestProjectNotFoundError,
    ManifestSchemaNotFoundError,
    MaterialisationFailedError,
    MaterialisationNotSupportedError,
    ProfileEnvVarUnsetError,
    ProfileNotFoundError,
    ProfileTargetNotFoundError,
    QuerySyntaxError,
    SamplingError,
    SamplingRequiresPartitionFilterError,
    TableNotFoundError,
    UnknownTableSizeError,
    UnsupportedAuthMethodError,
    UnsupportedProfileTypeError,
    WarehouseAuthError,
    WarehouseError,
)

__all__ = [
    "canonicalise_user_path",
    "emit_batch_progress_entry",
    "emit_progress_done",
    "emit_progress_entry",
    "format_batch_summary",
    "format_elapsed",
    "format_error_to_stderr",
    "map_exception_to_exit_code",
    "setup_logging",
    "should_emit_progress",
]

# Issue #37 / US-005 / DEC-009 — failure list cap. Operators running a
# pathological batch should still see the first 50 failures named (so
# they can act on a representative sample) plus a single ``... and <K>
# more`` line for the overflow. The cap is documented and tested for
# stability.
_BATCH_SUMMARY_FAILURE_CAP: int = 50


# ---------------------------------------------------------------------------
# Exit-code taxonomy (DEC-008, DEC-019)
# ---------------------------------------------------------------------------
#
# Four tiers, ported from clauditor's ``llm-cli-exit-code-taxonomy.md`` and
# specialised to SignalForge's typed exception surface:
#
#     0 — success (no entry; that's :func:`main`'s default return).
#     1 — load: configuration / path / manifest / system not in a coherent
#         state to start work.
#     2 — input: caller-supplied data is wrong (model not found, anchor
#         contract violation, threshold-fail with ``fail_on_below_threshold``).
#     3 — API: external dependency unavailable (LLM, warehouse, audit
#         write durability).
#
# Subclasses inherit their parent's tier via the ``isinstance``-walk in
# :func:`map_exception_to_exit_code`. The table below lists every concrete
# leaf class plus the per-stage abstract base; future US-008 AST scan
# verifies every ``*Error`` declared in ``src/signalforge/*/errors.py``
# resolves to exactly one tier.

_EXCEPTION_TO_EXIT_CODE: dict[type[BaseException], int] = {
    # ---- Tier 1: load ------------------------------------------------------
    # Manifest layer — every error here means we couldn't get the project
    # in a state to start work.
    ManifestError: 1,
    ManifestNotFoundError: 1,
    UnsupportedManifestVersionError: 1,
    ModelPathOutsideProjectError: 1,
    ModelMissingSqlError: 1,
    # Warehouse profile / connection-shape config (auth lives in tier 3
    # because it's an external-dep state rather than a config-shape issue).
    ProfileNotFoundError: 1,
    ProfileEnvVarUnsetError: 1,
    ProfileTargetNotFoundError: 1,
    UnsupportedProfileTypeError: 1,
    UnsupportedAuthMethodError: 1,
    ManifestProjectNotFoundError: 1,
    ManifestSchemaNotFoundError: 1,
    # Per-stage config-load errors.
    ConfigNotFoundError: 1,
    InvalidConfigError: 1,
    InvalidPatternError: 1,
    UnknownConfigKeyError: 1,
    PolicyValidationError: 1,
    DraftConfigNotFoundError: 1,
    DraftConfigInvalidError: 1,
    PruneConfigError: 1,
    GradeConfigError: 1,
    GradeRubricError: 1,
    DiffError: 1,
    # CLI-layer load-shape errors.
    CliError: 1,
    CliPathError: 1,
    # init-demo broken-install / filesystem-failure wrappers (issue #47 /
    # DEC-012 of plans/super/47-init-demo.md). Tier 1 because both fire
    # before any user-content work has happened and represent state we
    # couldn't get into a coherent shape (missing wheel resource, generic
    # OSError during the copytree / rmtree).
    CliInitDemoFixtureMissingError: 1,
    CliInitDemoCopyError: 1,
    # Lower-level signalforge.demo typed errors (issue #47). The CLI
    # wraps these into the Cli* wrappers above, so under normal CLI
    # operation they never reach this mapping directly. They land in
    # the table anyway as defence-in-depth: the 7th AST scan
    # (tests/test_audit_completeness.py) gates every concrete *Error
    # under src/signalforge/*/errors.py; mapping them here means a
    # v0.2 contributor who adds a new Demo*Error and forgets to wire
    # the CLI wrapper still gets a sensible exit code via the MRO
    # walk in :func:`map_exception_to_exit_code`.
    DemoPathError: 1,
    DemoFixtureMissingError: 1,
    # ---- Tier 2: input ----------------------------------------------------
    # Manifest selection (the operator picked a model that doesn't exist or
    # is disabled — caller's fault, not load).
    ModelNotFoundError: 2,
    ModelDisabledError: 2,
    # Selector grammar (--select expression syntactically invalid; #37
    # DEC-007: tier 2 because the operator supplied a malformed input).
    SelectorParseError: 2,
    # Warehouse identifier-shape / table-target mistakes (DEC-012).
    InvalidIdentifierError: 2,
    TableNotFoundError: 2,
    ColumnNotFoundError: 2,
    # Safety policy applied to a bad model.
    ColumnNotInModelError: 2,
    InvalidSamplingModeError: 2,
    # LLM-output invariants (the response we got isn't a valid candidate
    # set — invariant violation, not an external-dep failure).
    LLMOutputError: 2,
    LLMOutputJSONError: 2,
    LLMOutputValidationError: 2,
    LLMOutputAnchorContractError: 2,
    PromptEnvelopeBreachError: 2,
    # Prune-config opt-in mistakes.
    PruneTrustedModelNotFoundError: 2,
    # Grade prompt-envelope breach (the artifact text contained the close
    # tag — operator-level data invariant).
    GradePromptEnvelopeBreachError: 2,
    GradeOutputError: 2,
    # Grade threshold-fail (graduated in US-002 of #9; CLI catches → 2).
    GradeBelowThresholdError: 2,
    # Diff boundary / input-shape errors.
    DiffCandidateModelMismatchError: 2,
    DiffPruneResultModelMismatchError: 2,
    DiffGradingReportModelMismatchError: 2,
    DiffInputTooLargeError: 2,
    # Drafter base-class catches (concrete leaves above already typed; the
    # base resolves here for any forward-compat subclass).
    DraftError: 2,
    # ``--estimate`` cost-preview: the operator picked a model the price
    # table doesn't know — input-shape error, not external-dep failure.
    # See US-001 of issue #36 and the AC tying tier 2 to "looked-up
    # identifier not in a static table" failures.
    EstimateUnknownModelError: 2,
    # CLI-layer input-shape errors.
    CliInputError: 2,
    # Selector-failure wrappers (issue #37 / DEC-007 — US-002): both
    # subclass ``CliInputError``; explicit entries here so the 7th AST
    # scan in ``tests/test_audit_completeness.py`` discovers them. Both
    # tier 2 (input-validation) — parse failure is malformed input,
    # zero-match mirrors ``ModelNotFoundError``'s tier.
    CliSelectorParseError: 2,
    CliSelectorNoMatchError: 2,
    # init-demo input-validation wrappers (issue #47 / DEC-013 of
    # plans/super/47-init-demo.md). Tier 2 because both fire on
    # operator-supplied dest values that conflict with project state —
    # mirrors the precedent set by ModelNotFoundError (tier 2 for "the
    # operator named something the project rejects").
    CliInitDemoDestExistsError: 2,
    CliInitDemoDestUnsafeError: 2,
    # Lower-level demo-layer counterparts — see the tier-1 demo block
    # above for the defence-in-depth rationale.
    DemoDestExistsError: 2,
    DemoDestUnsafeError: 2,
    # ---- Tier 3: API / external dep ---------------------------------------
    # LLM connectivity / quota / SDK issues.
    LLMError: 3,
    LLMHelperError: 3,
    LLMAuthError: 3,
    LLMRateLimitError: 3,
    LLMServerError: 3,
    LLMConnectionError: 3,
    LLMResponseFormatError: 3,
    LLMCacheTooLargeError: 3,
    # Warehouse connectivity / quota (auth, query syntax that came back
    # from a real query, billing limit).
    WarehouseError: 3,
    WarehouseAuthError: 3,
    BytesBilledExceededError: 3,
    QuerySyntaxError: 3,
    SamplingError: 3,
    SamplingRequiresPartitionFilterError: 3,
    UnknownTableSizeError: 3,
    # Sample-materialisation seam (issue #22 / DEC-008 of US-007 of the
    # plan): both errors are external-dep failures — the materialise
    # query failed at the SDK / network / quota seam, or the active
    # adapter does not support per-run materialisation. The orchestrator
    # routes every candidate to ``kept-without-evidence`` per the
    # conservative-bias rule, but the typed exception still surfaces at
    # the CLI when it propagates (e.g., outside the prune orchestrator's
    # catch surface, or via the lint subcommand).
    MaterialisationFailedError: 3,
    MaterialisationNotSupportedError: 3,
    # Query-bytes estimation seam (issue #36 / US-002): the active
    # adapter does not support ``estimate_query_bytes`` (any non-BigQuery
    # adapter in v0.2). External-dep tier so the ``--estimate`` CLI flow
    # surfaces the typed exception with its locked remediation rather
    # than misclassifying it as input-shape.
    EstimateNotSupportedError: 3,
    # Audit-write durability across every fail-closed seam — when any of
    # these fire the disk hand-off didn't happen, which is an external-dep
    # state we couldn't recover.
    AuditWriteError: 3,
    AuditRecordTooLargeError: 3,
    PruneTimeoutError: 3,
    PruneAuditWriteError: 3,
    PruneAuditRecordTooLargeError: 3,
    GradeLLMError: 3,
    GradeBudgetExceededError: 3,
    GradeAuditWriteError: 3,
    GradeAuditRecordTooLargeError: 3,
    LLMResponseAuditWriteError: 3,
    LLMResponseAuditRecordTooLargeError: 3,
    DiffSidecarWriteError: 3,
    DiffSidecarRecordTooLargeError: 3,
    # Grade base catches forward-compat subclasses to 3 (every grade-layer
    # leaf has been individually tier-mapped above).
    GradeError: 3,
    # SafetyError base — every leaf above; base resolves here for forward
    # compat.
    SafetyError: 3,
    # Prune base — every leaf above; base resolves here.
    PruneError: 3,
}


def map_exception_to_exit_code(exc: BaseException) -> int:
    """Map a typed exception to its CLI exit-code tier.

    Walks ``type(exc).__mro__`` against :data:`_EXCEPTION_TO_EXIT_CODE`
    so subclasses inherit their parent's tier. Untyped :class:`Exception`
    (or any class not registered) returns ``1`` per DEC-016 (the panic
    path: "system not in a coherent state").

    The MRO walk is the seam future stories use to catch a forward-compat
    subclass added by a stage without updating this table — the AST scan
    landing in US-008 is the contract that says "every concrete error
    must appear here explicitly," but this lookup gracefully falls back
    to the parent class's tier in the meantime.
    """
    for cls in type(exc).__mro__:
        if cls in _EXCEPTION_TO_EXIT_CODE:
            return _EXCEPTION_TO_EXIT_CODE[cls]
    return 1


def canonicalise_user_path(raw: str | Path | None, project_dir: Path) -> Path | None:
    """Wrap :func:`signalforge._common.path_safety.canonicalise_path`
    with a CLI-layer error type and a ``None`` passthrough.

    Returns ``None`` when ``raw`` is ``None`` so callers can express
    optional-flag plumbing without a per-call ``if`` ladder.

    The wrapped helper raises :class:`PathContainmentError` on its
    failure modes (symlink loop, escape from ``project_dir``, missing
    project directory). We re-raise as :class:`CliPathError` so the
    CLI's own try/except boundary gets a homogeneous catch surface —
    every CLI-originated path failure produces one error type, separate
    from the upstream stage exceptions.
    """
    if raw is None:
        return None
    try:
        return canonicalise_path(raw, project_dir)
    except PathContainmentError as exc:
        raise CliPathError(
            f"path {raw!r} failed safety check: {exc}",
            remediation=(
                "Verify the path exists, is inside the project directory, "
                "and does not traverse a symlink loop."
            ),
        ) from exc


def setup_logging(verbose: bool, quiet: bool) -> None:
    """Configure the root logger once for the CLI run.

    * ``--verbose`` → ``DEBUG``.
    * ``--quiet`` → ``WARNING``.
    * Otherwise → ``INFO``.

    The CLI is the orchestration layer (NOT stage-0 in the
    safety-layer.md sense), so it is allowed to emit logs. Every call
    site uses the lazy-format JSON convention enforced by
    ``tests/llm/test_logger_grep_gate.py``.
    """
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )


def format_error_to_stderr(exc: Exception) -> str:
    """Render a typed exception to the canonical CLI stderr shape.

    Two shapes (DEC-008):

    * Tier 1 / 3 errors and most tier 2 errors render as a single
      ``ERROR: <message>`` line followed by an optional
      ``  ↳ Remediation: <text>`` footer when the typed error carries
      one.
    * :class:`LLMOutputAnchorContractError` (DEC-017 / DEC-008) renders
      as a header line plus one ``  - <violation>`` bullet per entry in
      ``violations``. The header text is the exception's primary
      message; the bullets carry the per-column / per-test detail. CI
      parsers rely on this two-shape contract — see clauditor's source
      rule.

    The ``↳ Remediation:`` line is rendered when the typed error's
    ``__str__`` already carries it (every stage error class produced by
    the layer-base pattern in :mod:`signalforge.safety.errors` and its
    siblings emits it). The CLI does not add or strip the line; we just
    carry through what the layer produced.
    """
    # Multi-violation shape — used by the LLM drafter's whole-draft
    # fail-loud anchor contract (DEC-022 of #5). Bullets render as
    # ``  - <text>``; CI parsers key on the leading two-space dash.
    if isinstance(exc, LLMOutputAnchorContractError):
        violations = getattr(exc, "violations", ())
        # The base ``__str__`` includes the remediation footer; the
        # multi-violation shape replaces the body with a header + bullets
        # while preserving that footer if present.
        header = f"ERROR: {exc.message}"
        bullets = "\n".join(f"  - {v}" for v in violations)
        body = f"{header}\n{bullets}" if bullets else header
        # Render the remediation footer if the typed error carries one
        # (every drafter error in the layer-base pattern does).
        remediation = getattr(exc, "remediation", None)
        if remediation:
            return f"{body}\n  ↳ Remediation: {remediation}"
        return body
    # Single-line shape — every other typed error. ``str(exc)`` already
    # includes the ``↳ Remediation:`` line (when set) thanks to the
    # uniform layer-base pattern.
    return f"ERROR: {exc}"


def print_stderr(message: str, *, end: str = "\n", flush: bool = False) -> None:
    """Write ``message`` to stderr after stripping ANSI CSI escapes.

    Single stderr-write sink for ``signalforge.cli``. Mirrors the diff
    renderer's "escape at the sink" principle (.claude/rules/diff-renderer.md
    DEC-007) for the CLI: every stderr-bound string passes through
    :func:`signalforge._common.ansi_safety.strip_ansi_escapes` so an
    upstream-controlled value (a model unique_id, a path, a typed-error
    message body) carrying ``\\x1b[31m...`` cannot inject terminal-control
    sequences into the operator's scrollback.

    Idempotent on already-clean input — the strip is a no-op when no
    CSI bytes are present. The ``end`` and ``flush`` kwargs mirror
    :func:`print`'s — pass ``end=""`` for callsites whose ``message``
    already carries a trailing newline (e.g.
    :func:`format_batch_summary`); pass ``flush=True`` for progress-line
    callsites that need an immediate flush.

    This helper is the only place in :mod:`signalforge.cli` that
    writes to ``sys.stderr``. The AST scan at
    ``tests/cli/test_no_direct_stderr_print.py`` rejects every
    bypass form — ``print(..., file=sys.stderr)`` AND
    ``sys.stderr.write(...)`` / ``sys.stderr.flush()`` — anywhere
    else in :mod:`signalforge.cli`. Issue #60.
    """
    print(strip_ansi_escapes(message), file=sys.stderr, end=end, flush=flush)


# ---------------------------------------------------------------------------
# Progress lines (US-007 / DEC-014 / DEC-026)
# ---------------------------------------------------------------------------


def should_emit_progress(quiet: bool, verbose: bool) -> bool:
    """Return True iff stage-progress lines should be emitted to stderr.

    DEC-014: TTY-gated by default (both stderr AND stdout must be
    terminals). DEC-026: ``--quiet`` suppresses regardless of TTY;
    ``--verbose`` forces progress on regardless of TTY (the operator
    explicitly opted in).
    """
    if quiet:
        return False
    if verbose:
        return True
    try:
        return bool(sys.stderr.isatty()) and bool(sys.stdout.isatty())
    except (AttributeError, ValueError):  # pragma: no cover — defensive
        return False


def format_elapsed(elapsed_seconds: float) -> str:
    """Format a wall-clock duration for the ``done in <X>`` progress
    line. ``X.Xs`` below 60s; ``Xm Ys`` at or above 60s (DEC-026).
    """
    if elapsed_seconds < 60.0:
        return f"{elapsed_seconds:.1f}s"
    minutes = int(elapsed_seconds // 60)
    seconds = int(round(elapsed_seconds - minutes * 60))
    if seconds == 60:
        # Carry the rounded second so 59.5s → 1m 0s, never 0m 60s.
        minutes += 1
        seconds = 0
    return f"{minutes}m {seconds}s"


def emit_progress_entry(stage_n: int, stage_name: str, body: str) -> None:
    """Emit a single ``[N/5] <stage>: <body>`` line to stderr.

    Callers are responsible for the TTY gate via
    :func:`should_emit_progress`; this helper unconditionally writes when
    invoked. The callsite-level gate keeps the helper trivial and lets
    the orchestrator make a single decision once at startup.
    """
    print_stderr(f"[{stage_n}/5] {stage_name}: {body}", flush=True)


def emit_progress_done(stage_n: int, stage_name: str, elapsed_seconds: float) -> None:
    """Emit the paired ``[N/5] <stage>: done in <X>`` line."""
    print_stderr(
        f"[{stage_n}/5] {stage_name}: done in {format_elapsed(elapsed_seconds)}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Batch summary + per-model progress prefix (issue #37 / US-005 — DEC-005,
# DEC-009, DEC-014)
# ---------------------------------------------------------------------------


def format_batch_summary(outcome: _BatchOutcome) -> str:
    """Return the DEC-005 stderr summary for a finished :func:`_run_batch`.

    Headline (always emitted) is locked verbatim by
    ``test_format_batch_summary_headline_shape``:

    ::

        Generated <K> kept / <L> dropped / <J> flagged across <M> models in <T>s

    Failure block (emitted when ≥1 per-model outcome has ``exit_code != 0``):

    ::

        <N> models failed:
          - <model_unique_id>        exit <code>  (<ExceptionClass>)
          - ...

    The failure list is capped at :data:`_BATCH_SUMMARY_FAILURE_CAP` (50)
    entries; overflow renders ``  ... and <K> more`` (DEC-009).

    ``<T>`` is the wall-clock from :class:`_BatchOutcome.duration_seconds`
    formatted to one decimal place; the kept / dropped / flagged counts
    are summed across every per-model outcome (failed-model contributions
    are zero per :class:`_SingleModelOutcome`'s contract).

    The helper accepts the typed :class:`_BatchOutcome` for typing
    clarity; the contract is a pure-string return — callers own the
    stderr write so emission stays gated at the call site (mirrors
    :func:`emit_progress_entry`'s shape).
    """
    per_model = outcome.per_model
    kept = sum(o.kept_count for o in per_model)
    dropped = sum(o.dropped_count for o in per_model)
    flagged = sum(o.flagged_count for o in per_model)
    matched = len(per_model)
    duration = outcome.duration_seconds

    failures = tuple(o for o in per_model if o.exit_code != 0)
    failed_count = len(failures)

    lines: list[str] = [
        f"Generated {kept} kept / {dropped} dropped / {flagged} flagged "
        f"across {matched} models in {duration:.1f}s"
    ]
    if failed_count == 0:
        return "\n".join(lines) + "\n"

    lines.append(f"{failed_count} models failed:")
    # Cap at the documented limit; overflow gets one ``... and <K> more`` line.
    named = failures[:_BATCH_SUMMARY_FAILURE_CAP]
    # Column-align the bullet body for human readability. ``id`` is left
    # padded to the longest named-id length (capped at 50 to bound the
    # padding for pathological model names); narrower ids land in a
    # consistent column with ``exit <code>``.
    # Defense-in-depth: scrub newline / carriage-return / tab from the id
    # before measuring + emitting. Real dbt unique_ids never contain control
    # characters (they're validated by dbt itself + Pydantic-strict-typed at
    # manifest load), but the summary is a CI-parser-keyable surface and a
    # control character would corrupt the column geometry irrecoverably.
    safe_ids = [
        o.model_unique_id.replace("\n", " ").replace("\r", " ").replace("\t", " ") for o in named
    ]
    id_width = max((len(s) for s in safe_ids), default=0)
    id_width = min(id_width, 50)
    for o, safe_id in zip(named, safe_ids, strict=True):
        klass = o.exception_class_name or "Exception"
        lines.append(f"  - {safe_id:<{id_width}}  exit {o.exit_code}  ({klass})")
    overflow = failed_count - _BATCH_SUMMARY_FAILURE_CAP
    if overflow > 0:
        lines.append(f"  ... and {overflow} more")
    return "\n".join(lines) + "\n"


def emit_batch_progress_entry(model_unique_id: str, batch_index: int, batch_count: int) -> None:
    """Emit a single ``[i/N] <model_unique_id>`` line to stderr.

    Callers are responsible for the TTY gate via
    :func:`should_emit_progress`; this helper unconditionally writes when
    invoked, mirroring :func:`emit_progress_entry`'s contract. The
    callsite-level gate keeps the helper trivial and lets the batch
    driver make a single decision once at startup.
    """
    print_stderr(f"[{batch_index}/{batch_count}] {model_unique_id}", flush=True)


def _safe_excepthook(
    exc_type: type[BaseException],
    exc_value: BaseException,
    traceback: TracebackType | None,
) -> None:
    """Strip tracebacks from anything that escapes the main try/except.

    Belt-and-braces for DEC-016: even if a bug raises an exception
    inside an ``except`` clause and bypasses the CLI's own catch, this
    hook ensures the user sees the typed-error message instead of a
    Python traceback.

    ``traceback`` is intentionally ignored — that's the whole point.
    Exit code is left to whatever called us (the runtime invokes us on
    its panic path; the CLI's own boundary already handled the typed
    cases).
    """
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        # Preserve Python's default semantics for these — they're the
        # operator hitting Ctrl-C or the CLI itself exiting cleanly. The
        # traceback is forwarded unchanged so debuggers / log scrapers
        # see the actual frame.
        sys.__excepthook__(exc_type, exc_value, traceback)
        return
    del traceback  # explicitly unused on the strip path
    print_stderr(f"ERROR: {exc_value}")
