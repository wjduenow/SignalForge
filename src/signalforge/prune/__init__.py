"""SignalForge prune layer — drop always-pass and known-clean-fail
candidate tests against warehouse data.

Public API (issue #6 ticket; ``v0.1``):

* :func:`prune_tests` — orchestrator; runs every candidate test against
  the warehouse and emits a :class:`PruneResult` with kept/dropped
  decisions and per-decision rationale.
* :class:`PruneResult`, :class:`PruneDecision` — typed result shapes.
* :class:`PruneConfig`, :func:`load_prune_config` — user config.
* :data:`DropReason`, :data:`Scope` — discriminator literals.
* :class:`PruneEvent` — fail-closed JSONL audit record (constructed
  ONLY by ``signalforge.prune.audit``; AST-gated per DEC-018).
* :class:`PruneError` and the five concrete error classes.

See ``plans/super/6-prune-engine.md`` for the full design.
"""

from signalforge.prune.audit import PruneEvent
from signalforge.prune.config import PruneConfig, load_prune_config
from signalforge.prune.engine import prune_tests
from signalforge.prune.errors import (
    PruneAuditRecordTooLargeError,
    PruneAuditWriteError,
    PruneConfigError,
    PruneError,
    PruneTimeoutError,
    PruneTrustedModelNotFoundError,
)
from signalforge.prune.models import (
    DropReason,
    PruneDecision,
    PruneResult,
    Scope,
)

__all__ = (
    # Public API
    "prune_tests",
    "PruneResult",
    "PruneDecision",
    "PruneConfig",
    "load_prune_config",
    "DropReason",
    "Scope",
    "PruneEvent",
    # Errors
    "PruneError",
    "PruneConfigError",
    "PruneTrustedModelNotFoundError",
    "PruneTimeoutError",
    "PruneAuditWriteError",
    "PruneAuditRecordTooLargeError",
)
