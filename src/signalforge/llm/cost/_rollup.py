"""Cost-rollup library function + frozen-dataclass result shapes.

Established by issue #157 / DEC-002, DEC-004, DEC-005 of
plans/super/157-e2e-cost-and-parallel.md.

The three frozen-dataclass shapes (:class:`ModelRollup`,
:class:`ProviderRollup`, :class:`CostReport`) are the read-back surface
US-002's tests pin against and downstream consumers can type-annotate
against today. :func:`rollup_audit_dir` walks
``<project_dir>/<audit_dir>/llm_responses.jsonl`` +
``<project_dir>/<audit_dir>/grade.jsonl``, deserialises each line via
the existing :class:`signalforge.draft.LLMResponseEvent` /
:class:`signalforge.grade.GradeEvent` models, multiplies the four token
fields against :data:`signalforge.llm.pricing.PRICES`, and returns a
populated :class:`CostReport`.

**Why frozen dataclass, not Pydantic (DEC-004):** the rollup output is a
pure compute result, never serialised to a JSONL/sidecar that downstream
consumers read back. A frozen dataclass sidesteps the ``extra="ignore"``
+ drift-detector contract that :file:`manifest-readers.md` mandates for
any read-back Pydantic model.

**Provider derivation (DEC-002 of US-002):** :data:`_MODEL_TO_PROVIDER`
is derived statically from the keys of
:data:`signalforge.llm.pricing.PRICES` at import time — every priced SKU
is assigned to its registered provider by SKU-prefix dispatch. The
mapping has the same lifecycle as the pricing table: bumping
:data:`signalforge.llm.pricing.PRICE_TABLE_VERSION` for a new SKU
requires an entry in :data:`_PROVIDER_PREFIXES` if the new SKU does not
match an existing prefix. The mapping is sanity-checked at import time
(every PRICES key must resolve) so a forgotten prefix fails loud on
``import signalforge.llm.cost``.

**Read-only, no fail-closed writer (DEC-005):** ``project_dir`` is
canonicalised at entry via
:func:`signalforge._common.path_safety.canonicalise_path` (catching the
symlink-loop / containment failure modes per
:file:`manifest-readers.md`), no fail-closed writer is registered, and
no on-disk artefact is produced. Path-canonicalisation failure
(``PathContainmentError``) wraps as
:class:`signalforge.llm.cost.errors.CostRollupAuditMissingError` — the
operator-actionable surface — rather than introducing a new path-error
class.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from pydantic import ValidationError

from signalforge._common.path_safety import PathContainmentError, canonicalise_path
from signalforge.draft.audit import LLMResponseEvent
from signalforge.grade.models import GradeEvent
from signalforge.llm.cost.errors import (
    CostRollupAuditMissingError,
    CostRollupMalformedRecordError,
    CostRollupUnknownModelError,
)
from signalforge.llm.errors import EstimateUnknownModelError
from signalforge.llm.pricing import PRICE_TABLE_VERSION, PRICES, lookup

# ---------------------------------------------------------------------------
# Provider derivation. The pricing table groups entries by provider in
# its source comments but does not tag each row. We dispatch by SKU
# prefix and validate at import time that every priced SKU resolves.
# ---------------------------------------------------------------------------

# Prefix -> canonical provider name (matches the names registered in
# ``signalforge.llm.providers``). Order doesn't matter — prefixes are
# disjoint as of PRICE_TABLE_VERSION 2026-05-28.
_PROVIDER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("gemini-", "gemini"),
)


def _provider_for_model(model: str) -> str | None:
    """Return the canonical provider name for ``model`` or ``None``.

    ``None`` means the SKU does not match any known provider prefix —
    callers route this to :class:`CostRollupUnknownModelError` so the
    operator sees the cost-rollup-specific remediation.
    """
    for prefix, provider in _PROVIDER_PREFIXES:
        if model.startswith(prefix):
            return provider
    return None


# Sanity check: every SKU in ``PRICES`` has a known provider. A new SKU
# added to ``PRICES`` without a matching prefix in
# ``_PROVIDER_PREFIXES`` will trip this at ``import signalforge.llm.cost``
# rather than silently routing into ``CostRollupUnknownModelError`` at
# rollup time. The pricing table is the lagging artefact; the provider
# table must move in lockstep.
def _build_model_to_provider() -> Mapping[str, str]:
    """Build the model -> provider mapping, narrowing ``None`` away.

    The dict comprehension iterates ``PRICES`` and skips any key whose
    provider lookup returns ``None`` so the resulting mapping's value
    type is the load-bearing ``str``. The companion assertion below
    catches any priced SKU without a prefix entry at import time.
    """
    out: dict[str, str] = {}
    for model in PRICES:
        provider = _provider_for_model(model)
        if provider is not None:
            out[model] = provider
    return MappingProxyType(out)


def _verify_provider_prefix_coverage(
    prices: Mapping[str, object],
    model_to_provider: Mapping[str, str],
) -> None:
    """Raise if any model in ``prices`` is missing from ``model_to_provider``.

    The module body calls this at import time to fail loud when a new
    ``PRICES`` SKU lands without a matching ``_PROVIDER_PREFIXES`` entry.
    Extracted as a top-level callable (not just an inline ``if``) so the
    raise arm is unit-testable without an ``importlib.reload`` dance.

    Uses an explicit raise (not ``assert``) so the check still runs
    under ``python -O``, which strips assertions (PR #162 review). The
    check is load-bearing for provider-dispatch correctness — a missing
    prefix would silently route the SKU to ``CostRollupUnknownModelError``
    at rollup time instead of failing loud at
    ``import signalforge.llm.cost``.
    """
    missing = sorted(set(prices) - set(model_to_provider))
    if missing:
        raise RuntimeError(
            "every model id in signalforge.llm.pricing.PRICES must match a "
            "_PROVIDER_PREFIXES entry; a new SKU was added without updating "
            f"the provider-prefix table: missing {missing!r}"
        )


_MODEL_TO_PROVIDER: Mapping[str, str] = _build_model_to_provider()
_verify_provider_prefix_coverage(PRICES, _MODEL_TO_PROVIDER)


# ---------------------------------------------------------------------------
# Result shapes (DEC-004 — frozen dataclasses).
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Internal accumulator used during the JSONL walk. Not part of the
# public surface — only the frozen result shapes above are returned.
# ---------------------------------------------------------------------------


@dataclass
class _ModelAccumulator:
    """Running per-model totals used during JSONL ingestion.

    Mutable on purpose: the orchestrator walks records and accumulates
    into one of these per ``(provider, model)`` pair, then freezes the
    result into a :class:`ModelRollup` at the end.
    """

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_usd: float = 0.0
    call_count: int = 0

    def to_rollup(self) -> ModelRollup:
        return ModelRollup(
            model=self.model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
            total_usd=self.total_usd,
            call_count=self.call_count,
        )


def _compute_record_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
) -> float:
    """Compute the USD cost of a single audit record.

    Raises :class:`CostRollupUnknownModelError` (not
    :class:`EstimateUnknownModelError`) so the cost-rollup-specific
    remediation surfaces; the underlying ``EstimateUnknownModelError``
    is chained via ``raise from``.
    """
    try:
        pricing = lookup(model)
    except EstimateUnknownModelError as exc:
        raise CostRollupUnknownModelError(model_id=model) from exc
    return (
        input_tokens * pricing.input_per_mtok
        + output_tokens * pricing.output_per_mtok
        + cache_creation_input_tokens * pricing.cache_write_5m_per_mtok
        + cache_read_input_tokens * pricing.cache_read_per_mtok
    ) / 1_000_000


def _ingest_jsonl(
    path: Path,
    event_cls: type[LLMResponseEvent] | type[GradeEvent],
    accumulators: dict[str, dict[str, _ModelAccumulator]],
) -> None:
    """Walk ``path`` line-by-line, deserialise into ``event_cls``, and
    accumulate into ``accumulators`` keyed by ``(provider, model)``.

    JSON-decode failures and Pydantic validation failures both surface
    as :class:`CostRollupMalformedRecordError(path, line_num, reason)`
    with a one-indexed ``line_num`` matching what ``sed -n '<N>p'`` would
    select. Unknown SKU propagates :class:`CostRollupUnknownModelError`
    untouched.
    """
    import json  # local import: hot-path only, keeps module import lean

    with path.open("r", encoding="utf-8") as fh:
        for line_num, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped:
                # Empty trailing line is normal — skip without consuming
                # a line number adjustment (line_num still tracks the
                # one-indexed position in the file).
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise CostRollupMalformedRecordError(
                    path=str(path),
                    line_num=line_num,
                    reason=f"JSONDecodeError: {exc.msg}",
                ) from exc
            try:
                event = event_cls.model_validate(data)
            except ValidationError as exc:
                raise CostRollupMalformedRecordError(
                    path=str(path),
                    line_num=line_num,
                    # Take a short excerpt of the first error so the
                    # operator sees a one-line summary without the full
                    # Pydantic traceback.
                    reason=f"ValidationError: {exc.errors()[0]['msg']}",
                ) from exc

            model = event.model
            provider = _provider_for_model(model)
            if provider is None:
                raise CostRollupUnknownModelError(model_id=model)

            cache_creation = event.cache_creation_input_tokens
            cache_read = event.cache_read_input_tokens
            usd = _compute_record_usd(
                model=model,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cache_read,
            )

            provider_bucket = accumulators.setdefault(provider, {})
            acc = provider_bucket.get(model)
            if acc is None:
                acc = _ModelAccumulator(model=model)
                provider_bucket[model] = acc
            acc.input_tokens += event.input_tokens
            acc.output_tokens += event.output_tokens
            acc.cache_creation_input_tokens += cache_creation
            acc.cache_read_input_tokens += cache_read
            acc.total_usd += usd
            acc.call_count += 1


# ---------------------------------------------------------------------------
# Public surface.
# ---------------------------------------------------------------------------


def rollup_audit_dir(
    project_dir: Path | str,
    *,
    audit_dir: str = ".signalforge",
) -> CostReport:
    """Walk ``<project_dir>/<audit_dir>`` and roll up per-provider USD
    cost from the SignalForge audit JSONLs.

    Per DEC-005 of plans/super/157-e2e-cost-and-parallel.md, the helper
    is read-only: ``project_dir`` is canonicalised at entry via
    :func:`signalforge._common.path_safety.canonicalise_path` (catching
    the symlink-loop / containment failure modes per
    :file:`manifest-readers.md`), no fail-closed writer is registered,
    and no on-disk artefact is produced.

    Raises:
        CostRollupAuditMissingError: when neither
            ``<audit_dir>/llm_responses.jsonl`` nor
            ``<audit_dir>/grade.jsonl`` exists under the canonicalised
            project root, OR when path canonicalisation surfaces a
            :class:`PathContainmentError` (symlink loop, escape from
            ``project_dir``, missing root). Wrapping the path failure as
            the missing-audit error is per US-002 AC8 — there is no
            cost-rollup path-error class because the operator-actionable
            surface is the same: "no audit JSONLs found".
        CostRollupMalformedRecordError: when a JSONL line fails JSON
            decode or :class:`LLMResponseEvent`/:class:`GradeEvent`
            validation. Carries ``line_num`` (one-indexed) so the
            operator can ``sed -n '<N>p'`` the offending line.
        CostRollupUnknownModelError: when an audit record references a
            model id not in :data:`signalforge.llm.pricing.PRICES`.
    """
    project_path = Path(project_dir)

    # Canonicalise project_dir against itself — the helper canonicalises
    # both args via ``Path.resolve``, so passing ``project_path`` twice
    # just returns the canonical project root. Any symlink-cycle,
    # missing-root, or non-directory failure surfaces as
    # ``PathContainmentError`` which routes to the missing-audit error
    # per the AC. We pass the ORIGINAL ``project_dir`` string to the
    # raised error so the operator sees the path they typed.
    try:
        canonical_project = canonicalise_path(project_path, project_path)
    except PathContainmentError as exc:
        raise CostRollupAuditMissingError(
            project_dir=str(project_dir),
            audit_dir=audit_dir,
        ) from exc

    # Canonicalise the audit root and each candidate file under the
    # project. A ``.signalforge`` symlink pointing outside the project
    # is rejected here even though the path string starts under
    # ``canonical_project`` (``Path.relative_to`` does NOT follow
    # symlinks — the three-trap rule from ``manifest-readers.md``).
    # Containment failure routes to ``CostRollupAuditMissingError`` per
    # AC8: a symlinked-elsewhere audit dir is, for the rollup, a
    # missing audit dir.
    raw_drafter = canonical_project / audit_dir / "llm_responses.jsonl"
    raw_grader = canonical_project / audit_dir / "grade.jsonl"

    try:
        drafter_path = canonicalise_path(raw_drafter, canonical_project)
        grader_path = canonicalise_path(raw_grader, canonical_project)
    except PathContainmentError as exc:
        raise CostRollupAuditMissingError(
            project_dir=str(project_dir),
            audit_dir=audit_dir,
        ) from exc

    drafter_exists = drafter_path.is_file()
    grader_exists = grader_path.is_file()

    if not drafter_exists and not grader_exists:
        raise CostRollupAuditMissingError(
            project_dir=str(project_dir),
            audit_dir=audit_dir,
        )

    # accumulators[provider][model] -> running totals
    accumulators: dict[str, dict[str, _ModelAccumulator]] = {}

    consumed: list[str] = []
    if drafter_exists:
        _ingest_jsonl(drafter_path, LLMResponseEvent, accumulators)
        consumed.append("llm_responses.jsonl")
    if grader_exists:
        _ingest_jsonl(grader_path, GradeEvent, accumulators)
        consumed.append("grade.jsonl")

    # Freeze the accumulators into ProviderRollup objects, keyed by the
    # canonical provider name. ``MappingProxyType`` makes the resulting
    # per-model and per-provider mappings read-only on the public
    # result.
    per_provider: dict[str, ProviderRollup] = {}
    total_usd = 0.0
    for provider, models in accumulators.items():
        per_model: dict[str, ModelRollup] = {
            model_id: acc.to_rollup() for model_id, acc in models.items()
        }
        subtotal = sum(rollup.total_usd for rollup in per_model.values())
        per_provider[provider] = ProviderRollup(
            provider=provider,
            per_model=MappingProxyType(per_model),
            subtotal_usd=subtotal,
        )
        total_usd += subtotal

    return CostReport(
        per_provider=MappingProxyType(per_provider),
        total_usd=total_usd,
        pricing_table_version=PRICE_TABLE_VERSION,
        audit_files_consumed=tuple(consumed),
    )


__all__ = [
    "CostReport",
    "ModelRollup",
    "ProviderRollup",
    "rollup_audit_dir",
]
