"""SignalForge LLM cost-rollup subpackage.

Walks the per-run audit JSONLs under ``<project_dir>/.signalforge/`` and
turns the captured token counts into per-provider per-model USD via
:data:`signalforge.llm.pricing.PRICES`. Established by issue #157 /
plans/super/157-e2e-cost-and-parallel.md to give the maintainer a
re-runnable cost-measurement seam after the live e2e suite — keeps the
"~$0.30/full-suite run" figure in docs honest by computing it from real
audit data rather than reasoning about it.

US-001 ships the public surface only (typed errors + frozen-dataclass
result shapes + the :func:`rollup_audit_dir` signature). US-002 fills in
the implementation.

This is the first sub-stage ``errors.py`` under
``src/signalforge/<stage>/<sub>/`` — scan-7 of
``tests/test_audit_completeness.py`` was extended in US-001 to walk
depth-2 paths in lockstep.
"""

from signalforge.llm.cost._rollup import (
    CostReport,
    ModelRollup,
    ProviderRollup,
    rollup_audit_dir,
)
from signalforge.llm.cost.errors import (
    CostError,
    CostRollupAuditMissingError,
    CostRollupMalformedRecordError,
    CostRollupUnknownModelError,
)

__all__ = (
    "CostError",
    "CostReport",
    "CostRollupAuditMissingError",
    "CostRollupMalformedRecordError",
    "CostRollupUnknownModelError",
    "ModelRollup",
    "ProviderRollup",
    "rollup_audit_dir",
)
