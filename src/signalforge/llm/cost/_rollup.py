"""Cost-rollup library function + frozen-dataclass result shapes (US-001 stub).

Established by issue #157 / DEC-002, DEC-004, DEC-005 of
plans/super/157-e2e-cost-and-parallel.md.

US-001 ships the public surface only:

* The three frozen-dataclass shapes (:class:`ModelRollup`,
  :class:`ProviderRollup`, :class:`CostReport`) so US-002's tests can
  pin against them and downstream consumers can type-annotate against
  the stable result type today.
* The :func:`rollup_audit_dir` signature with the keyword-only
  ``audit_dir`` parameter so callers can write against the final API
  while the implementation is a ``NotImplementedError`` stub.

US-002 fills in the body: walks ``<project_dir>/<audit_dir>/llm_responses.jsonl``
+ ``<project_dir>/<audit_dir>/grade.jsonl``, deserialises each line via
the existing :class:`signalforge.draft.LLMResponseEvent` /
:class:`signalforge.grade.GradeEvent` models, multiplies the four token
fields against :data:`signalforge.llm.pricing.PRICES`, and returns a
populated :class:`CostReport`.

**Why frozen dataclass, not Pydantic (DEC-004):** the rollup output is a
pure compute result, never serialised to a JSONL/sidecar that downstream
consumers read back. A frozen dataclass sidesteps the ``extra="ignore"``
+ drift-detector contract that :file:`manifest-readers.md` mandates for
any read-back Pydantic model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelRollup:
    """Token + USD rollup for a single ``(provider, model)`` pair.

    Aggregates every audit record referencing ``model`` across both
    JSONLs. ``call_count`` is the number of underlying audit records;
    the four token fields sum across them; ``total_usd`` is the dollar
    cost computed from those tokens × :data:`signalforge.llm.pricing.PRICES`.

    Cache fields (``cache_creation_input_tokens`` /
    ``cache_read_input_tokens``) are populated only for providers whose
    capability flags expose them — Anthropic populates both; OpenAI and
    Gemini leave them at zero. The cost arithmetic respects each
    provider's :class:`signalforge.llm.pricing.ModelPricing` cache rates
    (zero for OpenAI / Gemini), so an OpenAI record's zero cache tokens
    × zero cache rate contributes nothing to ``total_usd``.
    """

    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    total_usd: float
    call_count: int


@dataclass(frozen=True)
class ProviderRollup:
    """Per-provider rollup: every model from this provider seen across
    both audit JSONLs.

    ``per_model`` is keyed by the model id verbatim from the audit
    record (no normalisation). ``subtotal_usd`` is the sum of every
    contained :attr:`ModelRollup.total_usd`.
    """

    provider: str
    per_model: Mapping[str, ModelRollup]
    subtotal_usd: float


@dataclass(frozen=True)
class CostReport:
    """Top-level rollup result.

    ``per_provider`` keys are the canonical provider names registered in
    :mod:`signalforge.llm.providers` (``"anthropic"`` / ``"openai"`` /
    ``"gemini"``). ``total_usd`` is the sum of every
    :attr:`ProviderRollup.subtotal_usd`. ``pricing_table_version`` stamps
    :data:`signalforge.llm.pricing.PRICE_TABLE_VERSION` at rollup time so
    a saved report carries its own provenance.

    ``audit_files_consumed`` is the subset of ``("llm_responses.jsonl",
    "grade.jsonl")`` that were actually read — empty would mean
    :class:`CostRollupAuditMissingError` was raised instead, so the
    field always has at least one entry on a returned report.
    """

    per_provider: Mapping[str, ProviderRollup]
    total_usd: float
    pricing_table_version: str
    audit_files_consumed: tuple[str, ...]


def rollup_audit_dir(
    project_dir: Path | str,
    *,
    audit_dir: str = ".signalforge",
) -> CostReport:
    """Walk ``<project_dir>/<audit_dir>`` and roll up per-provider USD
    cost from the SignalForge audit JSONLs.

    **US-001 stub:** the public signature is defined so US-002's tests
    and any early callers can type-annotate against the final API; the
    body raises ``NotImplementedError`` until US-002 lands the
    implementation.

    Per DEC-005 of plans/super/157-e2e-cost-and-parallel.md, the helper
    is read-only: ``project_dir`` is canonicalised at entry via
    :func:`signalforge._common.path_safety.canonicalise_path` (catching
    the symlink-loop / containment failure modes per
    :file:`manifest-readers.md`), no fail-closed writer is registered,
    and no on-disk artefact is produced.
    """
    raise NotImplementedError("US-002 implements this; US-001 ships the public surface only")


__all__ = [
    "CostReport",
    "ModelRollup",
    "ProviderRollup",
    "rollup_audit_dir",
]
