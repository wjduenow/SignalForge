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
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Avoid a runtime circular import: ``signalforge.cli.generate`` imports
    # this module's helpers, and :func:`format_batch_summary` needs the
    # private ``_BatchOutcome`` dataclass shape only at type-check time.
    from signalforge.cli.generate import _BatchOutcome

from signalforge._common import palette
from signalforge._common.ansi_safety import strip_ansi_escapes
from signalforge._common.path_safety import PathContainmentError, canonicalise_path
from signalforge.cli.errors import (
    CliError,
    CliInitDemoCopyError,
    CliInitDemoDestExistsError,
    CliInitDemoDestUnsafeError,
    CliInitDemoFixtureMissingError,
    CliInputError,
    CliInstallSkillDestUnsafeError,
    CliInstallSkillPackageDataMissingError,
    CliInstallSkillPathError,
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
    DiffTestFileRecordTooLargeError,
    DiffTestFileWriteError,
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
    GradeCachePathError,
    GradeCacheReadError,
    GradeCacheWriteError,
    GradeConfigError,
    GradeError,
    GradeIncompleteError,
    GradeLLMError,
    GradeNestedEventLoopError,
    GradeOutputError,
    GradePromptEnvelopeBreachError,
    GradeRubricError,
)
from signalforge.ingest import (
    IngestAnchorContractError,
    IngestModelNotFoundError,
    IngestSchemaNotFoundError,
    IngestSchemaParseError,
    IngestSchemaTooLargeError,
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
    UnknownProviderError,
)
from signalforge.llm.cost import (
    CostError,
    CostRollupAuditMissingError,
    CostRollupMalformedRecordError,
    CostRollupUnknownModelError,
)
from signalforge.llm.errors import LLMProviderAsyncUnsupportedError
from signalforge.manifest import (
    AmbiguousRefError,
    Manifest,
    ManifestError,
    ManifestNotFoundError,
    Model,
    ModelDisabledError,
    ModelMissingSqlError,
    ModelNotFoundError,
    ModelPathOutsideProjectError,
    RefNotFoundError,
    SelectorParseError,
    SourceNotFoundError,
    TemplateResolutionError,
    UnsupportedJinjaError,
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
from signalforge.skill import (
    SkillDestPathError,
    SkillDestUnsafeError,
    SkillPackageDataMissingError,
)
from signalforge.warehouse import (
    BytesBilledExceededError,
    ColumnNotFoundError,
    EstimateNotSupportedError,
    EstimateUnavailableError,
    IncompleteProfileError,
    InvalidIdentifierError,
    ManifestProjectNotFoundError,
    ManifestSchemaNotFoundError,
    MaterialisationFailedError,
    MaterialisationNotSupportedError,
    ProfileEnvVarUnsetError,
    ProfileNotFoundError,
    ProfileTargetNotFoundError,
    QuerySyntaxError,
    RowCountNotSupportedError,
    SamplingError,
    SamplingRequiresPartitionFilterError,
    StatsQueryNotSupportedError,
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
    # Profile parsed but missing required keys for its type (#120 US-002 /
    # DEC-004). Tier 1 alongside UnsupportedAuthMethodError — both are
    # profile-config-shape failures that fire before any warehouse work.
    IncompleteProfileError: 1,
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
    # Nested-event-loop guard at ``grade_artifacts`` sync entry
    # (issue #186 DEC-009). Tier 1 — the operator's call site is wrong
    # (re-entered from inside an asyncio loop); same tier as
    # :class:`ManifestNotFoundError` (operator-environment-shape error).
    GradeNestedEventLoopError: 1,
    # Grade persistent-cache path-containment failure (issue #189 / DEC-017).
    # Tier 1 — the `.signalforge/grade-cache/` directory resolved outside
    # ``project_dir`` via a symlink. This is an operator-config problem
    # (the project tree has a symlink pointing elsewhere); same tier as
    # :class:`CliPathError` and :class:`ManifestNotFoundError`.
    GradeCachePathError: 1,
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
    # signalforge.skill typed errors (issue #141 / DEC-008). The CLI
    # wrappers will land in US-003 and re-raise these into
    # ``CliInstallSkill*Error`` at the handler boundary; the lib
    # concretes still appear here as defence-in-depth so the 7th AST
    # scan finds them and so an escaping raise gets a sensible exit code
    # via the MRO walk in :func:`map_exception_to_exit_code`. Like
    # ``DemoError`` and ``IngestError``, the concretes span tiers 1 and
    # 2, so ``SkillError`` itself has no single-tier fallback entry —
    # it lives only in ``_EXCEPTION_MAPPING_EXCLUDED_BASES``.
    SkillDestPathError: 1,
    SkillPackageDataMissingError: 1,
    # CLI wrappers for the skill-install handler boundary (issue #141 /
    # US-003 / DEC-008). Tier 1 for the two load-time failures: a
    # symlink-cycle resolve failure on ``<dest>`` and a broken-install
    # case where the bundled skill tree is missing. Mirrors the tiering
    # of the underlying ``Skill*Error`` lib concretes above.
    CliInstallSkillPathError: 1,
    CliInstallSkillPackageDataMissingError: 1,
    # Ingest layer (issue #104 / DEC-001 / US-001). The reader parses an
    # external dbt schema.yml into a CandidateSchema. These three are
    # load-tier: the schema file is missing, unparseable, or exceeds the
    # size cap applied before yaml.safe_load (DEC-005) — all "couldn't get
    # the input into a coherent state to start work." The two input-tier
    # concretes live in the Tier 2 block below. Like DemoError, the
    # IngestError base spans tiers 1 and 2, so it gets NO single fallback
    # entry — it lives only in _EXCEPTION_MAPPING_EXCLUDED_BASES; a forgotten
    # concrete falls through to tier 1 and the 7th AST scan catches the
    # missing per-class entry at test time.
    IngestSchemaNotFoundError: 1,
    IngestSchemaParseError: 1,
    IngestSchemaTooLargeError: 1,
    # ---- Tier 2: input ----------------------------------------------------
    # Manifest selection (the operator picked a model that doesn't exist or
    # is disabled — caller's fault, not load).
    ModelNotFoundError: 2,
    ModelDisabledError: 2,
    # Jinja-ref relation resolution (the SQL named a ref/source the manifest
    # doesn't know, or an ambiguous ref — operator-supplied input that the
    # warehouse can't act on; #116 DEC-005).
    RefNotFoundError: 2,
    AmbiguousRefError: 2,
    SourceNotFoundError: 2,
    # Bounded Jinja-ref resolution in singular-test SQL (the SQL used an
    # unsupported Jinja form, or left a reference unresolved — operator-supplied
    # input the bounded resolver can't act on; #116 US-002). ``UnsupportedJinjaError``
    # subclasses ``TemplateResolutionError`` and inherits its tier via MRO, but is
    # listed explicitly so the 7th AST scan finds a direct mapping for each.
    TemplateResolutionError: 2,
    UnsupportedJinjaError: 2,
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
    # Grade completeness contract (#202 US-006 / DEC-204 + DEC-207). A
    # non-exempt (artifact, criterion) pair stayed ungraded after the
    # bounded sweep AND ``require_complete=True``. Tier 2 (post-call
    # invariant — input-validation tier, same as ``GradeBelowThresholdError``):
    # an incomplete grade corpus is a structural failure the operator must
    # see. Raised AFTER the sidecar write (mirrors the threshold-fail
    # raise-after-sidecar ordering).
    GradeIncompleteError: 2,
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
    # Provider-registry: the operator selected a provider name not in the
    # registry — same "looked-up identifier not in a static table" input-shape
    # category as ``EstimateUnknownModelError`` (US-001 of issue #135).
    UnknownProviderError: 2,
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
    # signalforge.skill input-validation concrete (issue #141 / DEC-008).
    # Fires when ``dest`` is a regular file or when the existing
    # ``SKILL.md`` is a symlink — both operator-supplied input states
    # that conflict with the install contract; mirrors
    # ``DemoDestUnsafeError``'s tier.
    SkillDestUnsafeError: 2,
    # CLI wrapper for the skill-install dest-unsafe boundary (issue #141
    # / US-003 / DEC-008). Tier 2 (input-validation — the operator
    # supplied a destination state we refuse to write under); mirrors
    # ``CliInitDemoDestUnsafeError``'s tier.
    CliInstallSkillDestUnsafeError: 2,
    # Ingest layer (issue #104 / DEC-002 of US-001). Both fire on
    # operator-supplied input that conflicts with the manifest/schema:
    # the named model is absent from the schema.yml (mirrors
    # ModelNotFoundError's tier), or one or more tests reference a column
    # missing from the Model (whole-file collect-all anchor-contract
    # failure — the YAML is stale or wrong vs. the manifest).
    IngestModelNotFoundError: 2,
    IngestAnchorContractError: 2,
    # LLM cost-rollup layer (issue #157 / DEC-002 of US-001). The rollup
    # walks per-run audit JSONLs and turns token counts into USD via the
    # pricing table; all three concretes are input-shape failures (the
    # operator pointed the rollup at a directory missing the JSONLs, or
    # at a project whose JSONLs contain a malformed record / unknown
    # model id). ``CostError`` base is dual-registered at tier 2 below
    # as a single-tier safety net per cli-layer.md § "7th AST scan" —
    # mirrors the nine other single-tier base entries.
    CostRollupAuditMissingError: 2,
    CostRollupMalformedRecordError: 2,
    CostRollupUnknownModelError: 2,
    # ``CostError`` base dual-registration (safety net for forward-compat
    # subclasses) — every concrete is individually mapped above.
    CostError: 2,
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
    # Provider async-capability gate (issue #186 / US-002 / DEC-006;
    # tightened by QG Pass 1 Concern #2): the configured LLM provider
    # declares ``supports_async = False``. Raised at ``grade_artifacts``
    # orchestrator entry **before** ``asyncio.run``, **regardless of
    # ``grade.max_concurrent_calls``** (the engine consumes
    # ``call_llm_async`` exclusively post-#186; cap=1 is not an escape
    # hatch). An environment / configuration fact the operator must
    # resolve (pick an async-capable provider), so tier 3.
    LLMProviderAsyncUnsupportedError: 3,
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
    # Query-bytes estimation ran but produced nothing usable for THIS
    # query (issue #130 / DEC-003): the adapter supports estimation (e.g.
    # Snowflake EXPLAIN USING JSON) but the plan carried no parseable byte
    # figure. External-dep tier so the ``--estimate`` engine degrades to a
    # price-only preview and renders ``<unavailable: ...>`` rather than
    # misclassifying it as input-shape.
    EstimateUnavailableError: 3,
    # Row-count seam (issue #140): the active adapter does not expose a
    # ``get_row_count`` primitive (the Postgres stub, or any future
    # adapter that has not grown one). Sample-scope prune routes the
    # bucket-sizing lookup through this seam; the typed exception carries
    # its own ``prune.scope: full`` remediation. External-dep tier, like
    # the sibling ``*NotSupportedError`` adapter-capability signals.
    RowCountNotSupportedError: 3,
    # Stats-query seam (issue #171 / US-011): the active adapter does not
    # support the ``run_stats_query`` primitive the prune engine uses for
    # the ``row_count_anomaly_by_period`` variant (any non-BigQuery
    # adapter in v0.3). External-dep tier, like the sibling
    # ``*NotSupportedError`` adapter-capability signals.
    StatsQueryNotSupportedError: 3,
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
    # Grade persistent-cache disk-I/O errors (issue #189 / DEC-017).
    # Both tier 3 (external dependency, disk I/O) — same family as the
    # fail-closed audit-write durability errors above. ``GradeCacheReadError``
    # propagates from the engine's catch-and-warn read path; ``GradeCacheWriteError``
    # is registered for catch-and-warn diagnostics but never escapes
    # ``grade_artifacts`` (fail-soft per DEC-005 — write failure → WARNING).
    # ``GradeCacheRecordTooLargeError`` is a subclass of
    # ``GradeCacheWriteError`` and inherits tier 3 via the MRO walk in
    # :func:`map_exception_to_exit_code` — no explicit entry per DEC-017.
    GradeCacheReadError: 3,
    GradeCacheWriteError: 3,
    LLMResponseAuditWriteError: 3,
    LLMResponseAuditRecordTooLargeError: 3,
    DiffSidecarWriteError: 3,
    DiffSidecarRecordTooLargeError: 3,
    # Generated singular-test ``.sql`` writer (US-011 of #116). Both are
    # write-path durability errors raised inside the fail-closed writer
    # (mirrors the diff sidecar precedent above): write-durability and the
    # pre-open size cap are tier 3 (external-dep / fail-closed write).
    DiffTestFileWriteError: 3,
    DiffTestFileRecordTooLargeError: 3,
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


def _resolve_model_by_key(manifest: Manifest, key: str) -> Model:
    """Resolve ``key`` to a :class:`Model` across all three input shapes.

    Hoisted from :mod:`signalforge.cli.lint` (DEC-008 of issue #105) so
    every subcommand that takes a model argument (``lint``, the future
    ``prune-existing``) shares one resolver rather than copy-pasting the
    body. The behaviour is identical to the original ``lint``-local
    helper:

    * ``key.startswith("model.")`` → unique_id branch via
      :meth:`Manifest.get_model`.
    * ``"/" in key`` or ``key.endswith(".sql")`` → file-path branch via
      :meth:`Manifest.get_model`.
    * Else → bare-name branch: scan :meth:`Manifest.iter_models` for
      ``Model.name == key``. One match returns the model; zero matches
      raises :class:`ModelNotFoundError` with a hint suggesting the
      unique_id / file-path form; multiple matches raises
      :class:`ModelNotFoundError` with a disambiguation list (capped at
      five unique_ids to keep stderr readable).

    The bare-name branch sidesteps the ``Manifest.get_model`` gotcha
    pinned by ``testing-signal.md`` § "Multi-surface drift on user-facing
    model arguments" where bare names route through the file-path branch
    and surface a confusing :class:`ModelNotFoundError` even when the
    model exists under its unique_id. Per ``cli-layer.md`` § "Bare-name
    model resolution", the bare-name affordance lives in the CLI layer
    (NOT the manifest layer) because the CLI is the only surface where
    operators type model arguments by hand.
    """
    if key.startswith("model.") or "/" in key or key.endswith(".sql"):
        return manifest.get_model(key)

    matches = [m for m in manifest.iter_models() if m.name == key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        sample = ", ".join(m.unique_id for m in matches[:5])
        more = f" (+{len(matches) - 5} more)" if len(matches) > 5 else ""
        raise ModelNotFoundError(
            f"Bare model name {key!r} matches {len(matches)} enabled models: {sample}{more}",
            remediation=(
                "Disambiguate by passing the full unique_id "
                "(model.<pkg>.<name>) or the file path "
                "(models/path/to/<name>.sql)."
            ),
        )
    raise ModelNotFoundError(
        f"No enabled model with name {key!r} in the manifest.",
        remediation=(
            "Check the model name spelling, or pass the unique_id form "
            "(model.<pkg>.<name>) / file path (models/path/to/<name>.sql). "
            "Disabled models do not match bare-name lookup."
        ),
    )


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

    Three shapes (DEC-008 + DEC-007 of #186):

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
    * :class:`ExceptionGroup` (DEC-007 of #186) — belt-and-braces
      defence for the grade asyncio orchestrator. Multi-exception groups
      escaping the engine's ``BaseExceptionGroup`` unwrap (a hostile
      non-grade-typed exception slipping through the per-coroutine
      ``try/except``, or a fail-closed audit-write error pair) render as
      a header line plus one ``  - <ExcClass>: <repr-safe-msg>`` bullet
      per inner exception. Cap 10 bullets + ``  ... and K more``
      overflow line. Each inner exception's text is routed through
      :func:`repr` so ANSI / control bytes don't leak; the sink's
      :func:`print_stderr` also strips ANSI, but the repr-quote is the
      defence-in-depth layer (mirrors the safety / warehouse layers'
      ``_format_value`` helpers).

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
    # ExceptionGroup shape — DEC-007 of #186. Defence-in-depth for the
    # grade asyncio orchestrator (US-009): the engine's inner
    # ``BaseExceptionGroup`` unwrap re-raises single-exception groups as
    # the inner typed exception, so any group reaching this branch
    # carries two or more inner exceptions (typically a hostile
    # non-grade-typed exception slipping through the per-coroutine
    # ``try/except``, or paired fail-closed audit-write errors). Render
    # as ``ERROR: ...N concurrent failures:`` header + ``  - <Class>:
    # <repr(msg)>`` bullets, capped at 10 with an overflow line. NOTE:
    # ``ExceptionGroup`` is a subclass of ``Exception`` (3.11+);
    # ``BaseExceptionGroup`` (which also catches ``KeyboardInterrupt``
    # children) inherits from ``BaseException`` and never reaches the
    # ``cmd_<name>`` boundary catch — checking ``ExceptionGroup`` here
    # is the correct narrow surface for this typed renderer signature.
    if isinstance(exc, ExceptionGroup):
        inners = exc.exceptions
        n = len(inners)
        plural = "s" if n != 1 else ""
        header = f"ERROR: Grade orchestrator encountered {n} concurrent failure{plural}:"
        cap = 10
        bullet_lines = [f"  - {type(inner).__name__}: {repr(str(inner))}" for inner in inners[:cap]]
        if n > cap:
            bullet_lines.append(f"  ... and {n - cap} more")
        return "\n".join([header] + bullet_lines)
    # Single-line shape — every other typed error. ``str(exc)`` already
    # includes the ``↳ Remediation:`` line (when set) thanks to the
    # uniform layer-base pattern.
    return f"ERROR: {exc}"


def print_stderr(
    message: str, *, end: str = "\n", flush: bool = False, allow_sgr: bool = False
) -> None:
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

    ``allow_sgr=True`` (issue #210) SKIPS the strip — the narrow exception for
    the progress emitters, which compose a line from already-stripped
    user-content sub-parts wrapped in renderer-OWNED SGR codes (the spark
    glyph, the dim stage label). A caller passing ``allow_sgr=True`` MUST have
    stripped every user-controlled fragment itself before composing; the
    trusted SGR is then the only escape sequence that survives. Default
    ``False`` preserves the strip-everything contract for every other sink
    (the panic-path error renderer, the batch summary, lint output, ...).

    This helper is the only place in :mod:`signalforge.cli` that
    writes to ``sys.stderr``. The AST scan at
    ``tests/cli/test_no_direct_stderr_print.py`` rejects every
    bypass form — ``print(..., file=sys.stderr)`` AND
    ``sys.stderr.write(...)`` / ``sys.stderr.flush()`` — anywhere
    else in :mod:`signalforge.cli`. Issue #60.
    """
    text = message if allow_sgr else strip_ansi_escapes(message)
    print(text, file=sys.stderr, end=end, flush=flush)


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


# Spark-diamond glyph prefixing every COLOURED progress line — the brand
# "forged signal" mark (issue #210). Emitted only on the colour path; the plain
# path stays glyph-free and byte-identical to the pre-#210 output.
_PROGRESS_GLYPH = "◆"
# Stage labels pad to this width so the bodies line up in a column. ``safety``
# (6) is the longest of safety / draft / prune / grade / diff.
_STAGE_LABEL_WIDTH = 6
# Fallback width for right-aligning the done-line fact when the real terminal
# width can't be read.
_PROGRESS_FALLBACK_WIDTH = 80


@dataclass(frozen=True)
class ProgressStyle:
    """Resolved styling for the ``generate`` progress lines (issue #210).

    ``color`` — whether the ◆ glyph + brand colour are emitted at all.
    ``truecolor`` — whether the spark glyph uses the 24-bit brand amber
    (``#FFC24D``) vs the 16-colour yellow fallback.
    """

    color: bool
    truecolor: bool


def resolve_progress_style(verbose: bool) -> ProgressStyle:
    """Decide once whether progress lines carry the brand glyph + colour.

    Progress writes to STDERR, so the colour gate keys on stderr (not stdout):
    ``FORCE_COLOR`` forces on; ``NO_COLOR`` forces off; otherwise
    ``sys.stderr.isatty()``. ``--verbose`` forces *progress* on (DEC-026) but
    NOT colour — a ``--verbose`` run piped to a file stays plain, so the bool
    is accepted for symmetry with :func:`should_emit_progress` but only the
    env/TTY signals decide colour. When colour is on, the brand palette is used
    iff ``COLORTERM`` advertises truecolor (issue #209's detection, shared via
    ``_common.palette``).

    Mirrors the diff renderer's precedence (``FORCE_COLOR`` beats ``NO_COLOR``)
    so the two surfaces agree on when colour ships.
    """
    _ = verbose  # progress-on signal, not a colour signal (see docstring)
    if os.environ.get("FORCE_COLOR"):
        color = True
    elif "NO_COLOR" in os.environ:
        color = False
    else:
        try:
            color = bool(sys.stderr.isatty())
        except (AttributeError, ValueError):  # pragma: no cover — defensive
            color = False
    truecolor = palette.colorterm_is_truecolor() if color else False
    return ProgressStyle(color=color, truecolor=truecolor)


def _spark_glyph(style: ProgressStyle) -> str:
    """Return the coloured ◆ glyph + trailing space, or '' when colour off."""
    if not style.color:
        return ""
    code = palette.SPARK if style.truecolor else palette.YELLOW
    return f"{code}{_PROGRESS_GLYPH}{palette.RESET} "


def _dim(text: str, style: ProgressStyle) -> str:
    """Wrap ``text`` in the dim SGR when colour is on; else return as-is."""
    if not style.color:
        return text
    return f"{palette.DIM}{text}{palette.RESET}"


def _progress_terminal_width() -> int:
    """Best-effort terminal width for right-aligning the done-line fact."""
    try:
        return shutil.get_terminal_size().columns
    except (ValueError, OSError):  # pragma: no cover — defensive
        return _PROGRESS_FALLBACK_WIDTH


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


def emit_progress_entry(
    stage_n: int,
    stage_name: str,
    body: str,
    *,
    total: int = 5,
    style: ProgressStyle | None = None,
) -> None:
    """Emit a single stage-entry progress line to stderr.

    Two surfaces (issue #210). When ``style`` is ``None`` or ``style.color``
    is ``False`` (no TTY / ``NO_COLOR``), the plain ``[N/<total>] <stage>:
    <body>`` form ships — byte-identical to the pre-#210 output, so piped logs
    and existing tests are unchanged. When colour is on, the brand form ships:
    a spark-amber ``◆`` glyph, the ``[N/<total>]`` counter, a dim stage label
    padded to a column, then the body. ``body`` is ANSI-stripped on the colour
    path (it may carry a model id / path) before the trusted SGR is added.

    Callers own the TTY gate via :func:`should_emit_progress`; this helper
    writes unconditionally when invoked. ``total`` defaults to ``5`` (the
    ``generate`` pipeline's stage count); ``prune-existing`` passes ``total=3``
    for its ``ingest → prune → diff`` progress (DEC-010 of
    ``plans/super/105-prune-existing-cli.md``).
    """
    if style is None or not style.color:
        print_stderr(f"[{stage_n}/{total}] {stage_name}: {body}", flush=True)
        return
    glyph = _spark_glyph(style)
    stage = _dim(stage_name.ljust(_STAGE_LABEL_WIDTH), style)
    body_clean = strip_ansi_escapes(body)
    print_stderr(f"{glyph}[{stage_n}/{total}] {stage}  {body_clean}", flush=True, allow_sgr=True)


def emit_progress_done(
    stage_n: int,
    stage_name: str,
    elapsed_seconds: float,
    *,
    total: int = 5,
    fact: str = "",
    style: ProgressStyle | None = None,
) -> None:
    """Emit the paired stage-done line to stderr.

    Plain path (colour off) is byte-identical to pre-#210:
    ``[N/<total>] <stage>: done in <X>`` — no glyph, no fact. The colour path
    prefixes the spark ``◆`` glyph and, when ``fact`` is non-empty, appends it
    right-aligned to the terminal width — a dim one-glance summary (the model
    id, the mean grade, the kept/dropped counts) the caller computes from
    objects already in scope, never a hardcoded hint (DEC-026). ``fact`` is
    ANSI-stripped before styling.
    """
    timing = f"done in {format_elapsed(elapsed_seconds)}"
    if style is None or not style.color:
        print_stderr(f"[{stage_n}/{total}] {stage_name}: {timing}", flush=True)
        return
    glyph = _spark_glyph(style)
    stage_padded = stage_name.ljust(_STAGE_LABEL_WIDTH)
    # Visible (SGR-free) left segment, used only to measure the right-align pad.
    left_plain = f"{_PROGRESS_GLYPH} [{stage_n}/{total}] {stage_padded}  {timing}"
    left = f"{glyph}[{stage_n}/{total}] {_dim(stage_padded, style)}  {timing}"
    if not fact:
        print_stderr(left, flush=True, allow_sgr=True)
        return
    fact_clean = strip_ansi_escapes(fact)
    pad = max(2, _progress_terminal_width() - len(left_plain) - len(fact_clean))
    print_stderr(f"{left}{' ' * pad}{_dim(fact_clean, style)}", flush=True, allow_sgr=True)


# ---------------------------------------------------------------------------
# End-of-run footer — ``wrote …`` + ``✓ done in <X> · $cost`` (issue #211)
# ---------------------------------------------------------------------------

# Human-facing provider display names for the cost clause. Keyed by the
# canonical registry name (``signalforge.llm.providers``); an unmapped provider
# falls back to its raw key so a future vendor still renders.
_PROVIDER_DISPLAY: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Gemini",
}


def _check_glyph(style: ProgressStyle) -> str:
    """Return the coloured ``✓`` success glyph + trailing space, or '' when
    colour off. Signal-green (brand ``#2FCB7F`` truecolor / 16-colour green
    fallback) — the same hue the diff renderer paints the ``kept`` tier."""
    if not style.color:
        return ""
    code = palette.SIGNAL if style.truecolor else palette.GREEN
    return f"{code}✓{palette.RESET} "


def _format_usd(value: float) -> str:
    """Format a USD figure for the cost clause.

    ``$X.XX`` at or above one cent; ``<$0.01`` for a positive-but-sub-cent
    figure (a two-decimal ``$0.00`` would read as free when it is not).
    """
    if 0.0 < value < 0.01:
        return "<$0.01"
    return f"${value:.2f}"


def format_cost_clause(per_provider_usd: Mapping[str, float]) -> str:
    """Format the per-provider LLM cost clause for the ``✓ done`` line.

    ``{"anthropic": 0.13}`` → ``"$0.13 Anthropic"``; multiple providers join
    with `` · `` in sorted-key order for determinism. Providers with a
    zero/negative subtotal are omitted (a cache-only or no-call run shows no
    cost clause rather than ``$0.00``). Returns ``""`` when nothing is billable
    — the caller then emits a bare ``✓ done in <X>`` line.

    Warehouse cost is deliberately NOT included: there is no
    actual-bytes-scanned figure at end-of-run (only the ``--estimate``
    planner preview), so the clause stays LLM-only rather than fabricating a
    number (mirrors the supplementary-failure degrade of ``--estimate``,
    `cli-layer.md` DEC-005).
    """
    parts: list[str] = []
    for provider in sorted(per_provider_usd):
        usd = per_provider_usd[provider]
        if usd <= 0.0:
            continue
        # The known display names are literals, but the fallback echoes the
        # raw provider KEY from the audit JSONL — strip ANSI defensively since
        # the footer is emitted through ``print_stderr(allow_sgr=True)`` (the
        # emitter pre-strips every user-content fragment, cli-layer.md #210).
        display = strip_ansi_escapes(_PROVIDER_DISPLAY.get(provider, provider))
        parts.append(f"{_format_usd(usd)} {display}")
    return " · ".join(parts)


def build_run_footer(
    *,
    elapsed_seconds: float,
    written: Sequence[str],
    dry_run: bool,
    cost_clause: str,
    style: ProgressStyle,
) -> str:
    """Build the end-of-run footer (issue #211) for a single ``generate`` run.

    Two lines on the colour path::

        wrote schema.yml (8 kept) · .signalforge/diff.json · .signalforge/grade.json
        ✓ done in 5m12s · $0.13 Anthropic

    * The ``wrote`` line names the artifacts ACTUALLY written this run
      (``written`` is built by the caller from the ``--write`` / ``--dry-run``
      / grading state — honest, never a claim of a write that didn't happen).
      Empty ``written`` under ``--dry-run`` renders ``dry run — no files
      written``; empty otherwise omits the line.
    * The ``✓ done`` line carries the wall-clock + the LLM ``cost_clause``
      (omitted when empty).

    Colour-gated like the progress lines (issue #210): the ``✓`` glyph + the
    dim styling appear only when ``style.color``; the colour-off form is plain
    text (no glyph, no SGR). The whole footer is gated by the caller behind
    ``progress_on`` (so ``--quiet`` / non-TTY suppress it). Every fragment is
    internally constructed (artifact names, formatted USD, provider display) —
    no user-content interpolation — so it is safe to emit through
    ``print_stderr(..., allow_sgr=True)``.
    """
    lines: list[str] = []
    if written:
        lines.append(f"wrote {_dim(' · '.join(written), style)}")
    elif dry_run:
        lines.append(_dim("dry run — no files written", style))
    timing = f"done in {format_elapsed(elapsed_seconds)}"
    done = f"{_check_glyph(style)}{timing}"
    if cost_clause:
        done = f"{done} · {_dim(cost_clause, style)}"
    lines.append(done)
    return "\n".join(lines)


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


def emit_batch_progress_entry(
    model_unique_id: str,
    batch_index: int,
    batch_count: int,
    *,
    style: ProgressStyle | None = None,
) -> None:
    """Emit a single ``[i/N] <model_unique_id>`` batch-progress line to stderr.

    Plain path (``style`` ``None`` / colour off) is byte-identical to pre-#210;
    the colour path prefixes the spark ``◆`` glyph (issue #210).
    ``model_unique_id`` is ANSI-stripped on the colour path.

    Callers own the TTY gate via :func:`should_emit_progress`; this helper
    writes unconditionally when invoked, mirroring
    :func:`emit_progress_entry`'s contract.
    """
    if style is None or not style.color:
        print_stderr(f"[{batch_index}/{batch_count}] {model_unique_id}", flush=True)
        return
    glyph = _spark_glyph(style)
    mid = strip_ansi_escapes(model_unique_id)
    print_stderr(f"{glyph}[{batch_index}/{batch_count}] {mid}", flush=True, allow_sgr=True)


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
