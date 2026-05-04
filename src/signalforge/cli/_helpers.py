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

from signalforge.cli.errors import CliError, CliInputError, CliPathError

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
    LLMAuthError,
    LLMCacheTooLargeError,
    LLMCacheTooSmallError,
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
    InvalidIdentifierError,
    ManifestProjectNotFoundError,
    ManifestSchemaNotFoundError,
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
from signalforge.warehouse._path_safety import canonicalise_path

__all__ = [
    "canonicalise_user_path",
    "format_error_to_stderr",
    "map_exception_to_exit_code",
    "setup_logging",
]


_LOGGER = logging.getLogger("signalforge.cli")


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
    # ---- Tier 2: input ----------------------------------------------------
    # Manifest selection (the operator picked a model that doesn't exist or
    # is disabled — caller's fault, not load).
    ModelNotFoundError: 2,
    ModelDisabledError: 2,
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
    # CLI-layer input-shape errors.
    CliInputError: 2,
    # ---- Tier 3: API / external dep ---------------------------------------
    # LLM connectivity / quota / SDK issues.
    LLMError: 3,
    LLMHelperError: 3,
    LLMAuthError: 3,
    LLMRateLimitError: 3,
    LLMServerError: 3,
    LLMConnectionError: 3,
    LLMResponseFormatError: 3,
    LLMCacheTooSmallError: 3,
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
    """Wrap :func:`signalforge.warehouse._path_safety.canonicalise_path`
    with a CLI-layer error type and a ``None`` passthrough.

    Returns ``None`` when ``raw`` is ``None`` so callers can express
    optional-flag plumbing without a per-call ``if`` ladder.

    The wrapped helper raises :class:`ProfileNotFoundError` on its
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
    except Exception as exc:  # noqa: BLE001
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
    del traceback  # explicitly unused
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        # Preserve Python's default semantics for these — they're the
        # operator hitting Ctrl-C or the CLI itself exiting cleanly.
        sys.__excepthook__(exc_type, exc_value, None)
        return
    print(f"ERROR: {exc_value}", file=sys.stderr)
