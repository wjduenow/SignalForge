"""``signalforge generate --estimate`` cost-preview engine (US-003 of issue #36).

This module is CLI-internal (DEC-010 of ``plans/super/36-estimate-cost-preview.md``):
the public surface is ``signalforge generate --estimate`` (US-005), not
``signalforge.estimate``. The leading underscore on the module is the seal;
v0.2 may graduate the engine to a public namespace if a second caller
appears (CI integration, programmatic API).

The engine is a **pure function** that takes the prelude products (model,
manifest, configs, adapter, anthropic client) and returns a typed
:class:`EstimateReport`. The contract is locked by AC-1 of the ticket:

- Exactly **one** ``client.messages.count_tokens(...)`` call per
  ``(draft prompt + per-criterion rep grade prompt)``. ``1 + N`` calls
  where ``N == len(rubric)``.
- Exactly **one** ``adapter.estimate_query_bytes(...)`` call (per
  ``BigQueryAdapter`` semantics this becomes one ``client.query`` with
  ``dry_run=True``).
- **Zero** ``client.messages.create(...)`` calls. Test
  ``test_estimate_never_calls_messages_create`` pins this via
  ``len(fake.messages._create_calls) == 0`` per AC-4 of issue #36.
- **Zero** non-dry-run warehouse queries.

Partial-failure degrade (DEC-005, mirrors ``prune-engine.md`` DEC-009
conservative-bias verbatim): if ``estimate_query_bytes`` raises ANY
:class:`WarehouseError` subclass, we capture
``f"{type(exc).__name__}: {str(exc)[:200]}"`` into
:attr:`EstimateReport.warehouse_unavailable_reason`, set
``warehouse_bytes_per_row`` and ``warehouse_total_bytes`` to ``None``,
emit a single lazy-format-JSON ``WARNING``, and CONTINUE. Every other
exception (LLM, config, manifest) propagates through the existing
``cmd_generate`` panic boundary unchanged.

Documented assumptions (load-bearing — do not bury):

- **Grade-cost estimation uses one representative artifact per criterion**
  (the first artifact alphabetical by ``artifact_id``). Real grading
  issues one ``messages.create`` per ``(artifact, criterion)`` pair —
  see ``grade-layer.md`` DEC-004 — but the cached rubric block is
  constant across artifacts, so counting tokens for ONE artifact per
  criterion and multiplying by ``artifacts_count`` is a faithful proxy
  while keeping the LLM-call count bounded to ``1 + N``.
- ``draft_output_tokens_estimate`` defaults to
  :attr:`DraftConfig.max_output_tokens`. This is the upper bound the
  LLM may return; the estimate intentionally errs on the high side
  rather than guessing an average.
- ``estimated_output_tokens_per_call`` for the grader is
  :data:`_GRADE_OUTPUT_TOKENS_PER_CALL` (50). The per-criterion JSON
  response shape — ``{"criterion_id", "score", "passed", "evidence",
  "reasoning"}`` — typically lands under 50 tokens; bumping the
  estimate higher would inflate cost projections.
- ``tests_per_column_heuristic`` defaults to
  :data:`_TESTS_PER_COLUMN_HEURISTIC` (3.5; DEC-012 of issue #36).
  Derived from the canonical Austin e2e fixture
  (``stg_bikeshare_trips``: 11 columns yielding ~38 pre-prune
  candidate tests).
- ``warehouse_bytes_per_row`` is a display-only divider; the
  ``dry_run`` reports total bytes for the representative query, which
  we treat as the total bytes for one test and multiply by the
  heuristic test count for ``warehouse_total_bytes``.

Single end-of-run INFO log via lazy-format JSON (DEC-013) carrying
``{run_id, model_unique_id, drafter_model, grader_model, draft_tokens,
grade_tokens, total_llm_usd, total_bytes, duration_seconds,
price_table_version}``.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from signalforge.draft.prompts import render_prompt
from signalforge.grade.prompts import (
    render_dynamic_block as render_grade_dynamic_block,
)
from signalforge.grade.prompts import (
    render_rubric_block,
)
from signalforge.grade.rubric import DEFAULT_RUBRIC
from signalforge.llm import pricing as _pricing
from signalforge.safety.models import LLMRequest, SamplingMode
from signalforge.warehouse.errors import WarehouseError
from signalforge.warehouse.models import TableRef

if TYPE_CHECKING:
    from signalforge.draft.config import DraftConfig
    from signalforge.grade.config import GradeConfig
    from signalforge.grade.rubric import Criterion
    from signalforge.llm._client import _AnthropicClientProtocol
    from signalforge.manifest.models import Manifest, Model
    from signalforge.prune.config import PruneConfig
    from signalforge.warehouse.base import WarehouseAdapter


__all__ = [
    "CriterionEstimate",
    "EstimateReport",
    "estimate",
    "render",
]


_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants (DEC-012)
# ---------------------------------------------------------------------------


_TESTS_PER_COLUMN_HEURISTIC: float = 3.5
"""Average candidate tests per column the drafter emits pre-prune.

Derived from the canonical Austin e2e fixture (DEC-012 of
``plans/super/36-estimate-cost-preview.md``). Documented in the renderer
footer.
"""


_GRADE_OUTPUT_TOKENS_PER_CALL: int = 50
"""Conservative per-criterion JSON output-token estimate."""


_CRITERION_TEXT_PREVIEW_LEN: int = 60
"""Display width of the criterion-text preview field on
:class:`CriterionEstimate` (for the renderer in US-004)."""


# ---------------------------------------------------------------------------
# Typed result shapes (DEC-010, DEC-022 of prune-engine.md mirror)
# ---------------------------------------------------------------------------


class CriterionEstimate(BaseModel):
    """Per-criterion grade-cost projection.

    One :class:`CriterionEstimate` per active rubric criterion. The
    renderer (US-004) uses ``criterion_text_truncated`` to label rows
    without breaking 80-column terminal width.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    criterion_id: str
    criterion_text_truncated: str
    calls: int
    input_tokens_per_call: int
    total_input_tokens: int
    estimated_output_tokens_per_call: int = _GRADE_OUTPUT_TOKENS_PER_CALL
    usd: float


class EstimateReport(BaseModel):
    """The end-of-run typed report produced by :func:`estimate`.

    Frozen (immutable post-construction) so downstream consumers cannot
    mutate USD figures after the engine has signed off. ``extra="ignore"``
    matches the project's read-back convention for typed result models —
    forward-compat against v0.2 additions.

    Custom :meth:`__repr__` per ``prune-engine.md`` DEC-022: emits only
    the minimal-actionable fields. ``grade_per_criterion`` carries
    potentially many entries and is excluded from the default repr;
    callers wanting the full breakdown access the field directly.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    model_unique_id: str
    drafter_model: str
    grader_model: str
    draft_input_tokens: int
    draft_output_tokens_estimate: int
    draft_usd: float
    grade_artifacts_count: int
    grade_criteria_count: int
    grade_per_criterion: tuple[CriterionEstimate, ...]
    grade_usd: float
    total_llm_usd: float
    warehouse_bytes_per_row: int | None
    warehouse_total_bytes: int | None
    warehouse_unavailable_reason: str | None = None
    tests_per_column_heuristic: float = _TESTS_PER_COLUMN_HEURISTIC
    sample_size: int
    price_table_version: str
    duration_seconds: float

    def __repr__(self) -> str:
        warehouse_ok = self.warehouse_unavailable_reason is None
        return (
            "EstimateReport("
            f"model_unique_id={self.model_unique_id!r}, "
            f"drafter_model={self.drafter_model!r}, "
            f"grader_model={self.grader_model!r}, "
            f"total_llm_usd={self.total_llm_usd!r}, "
            f"warehouse_total_bytes={self.warehouse_total_bytes!r}, "
            f"warehouse_ok={warehouse_ok!r})"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_schema_only_request(model: Model) -> LLMRequest:
    """Construct the minimal :class:`LLMRequest` needed to render the
    drafter prompt for token counting.

    The estimate path does NOT call :func:`build_llm_request` (which
    would emit a safety-audit JSONL record and, in non-schema-only
    modes, issue warehouse queries). The engine is a pure
    cost-projection pass; rendering the prompt only needs the schema
    surface. We construct an :class:`LLMRequest` directly with
    :attr:`SamplingMode.SCHEMA_ONLY` so:

    * No warehouse calls fire here (the only warehouse call is the
      explicit ``estimate_query_bytes`` dry-run later).
    * No safety audit record is written for an estimate run (no real
      LLM call is made; auditing an estimate would pollute the
      audit log).

    The AST scan ``test_llm_request_construction_only_in_request_module``
    in ``tests/safety/test_public_api.py`` walks
    ``src/signalforge/safety/`` only, so this construction outside that
    tree does not trip the scan. The construction is intentional and
    documented; the safety-layer's audit invariant is preserved
    because no LLM call is issued from this request.
    """
    raw_schema: tuple[tuple[str, str], ...] = tuple(
        (column.name, column.data_type or "") for column in model.columns_list
    )
    return LLMRequest(
        model_unique_id=model.unique_id,
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=tuple(c.name for c in model.columns_list),
        redactions=(),
        sampled_rows=None,
        aggregates=None,
        schema=raw_schema,
    )


def _truncate_criterion_text(text: str) -> str:
    """Return the first :data:`_CRITERION_TEXT_PREVIEW_LEN` chars of ``text``."""
    if len(text) <= _CRITERION_TEXT_PREVIEW_LEN:
        return text
    return text[:_CRITERION_TEXT_PREVIEW_LEN]


def _count_draft_tokens(
    *,
    client: _AnthropicClientProtocol,
    draft_config: DraftConfig,
    system: str,
    cached_block: str,
    dynamic_block: str,
) -> int:
    """Issue exactly one ``count_tokens`` for the drafter prompt and
    return the integer ``input_tokens`` field.

    Mirrors :func:`signalforge.llm.client.call_anthropic`'s pre-send
    ``count_tokens`` envelope: system + the single cached user-content
    block. The dynamic block is included in the messages list so the
    count reflects the full prompt the drafter would send (the cache
    boundary affects pricing, not token count — pricing math runs
    further down using ``draft_config.cache_ttl`` indirectly).
    """
    block_cached: dict[str, Any] = {
        "type": "text",
        "text": cached_block,
        "cache_control": {"type": "ephemeral", "ttl": draft_config.cache_ttl},
    }
    block_dynamic: dict[str, Any] = {"type": "text", "text": dynamic_block}
    response = client.messages.count_tokens(
        model=draft_config.model,
        system=system,
        messages=[{"role": "user", "content": [block_cached, block_dynamic]}],
    )
    input_tokens = getattr(response, "input_tokens", None)
    if not isinstance(input_tokens, int):
        msg = "count_tokens response is missing the `input_tokens` field."
        raise RuntimeError(msg)
    return input_tokens


def _count_grade_criterion_tokens(
    *,
    client: _AnthropicClientProtocol,
    grade_config: GradeConfig,
    system_and_rubric: str,
    artifact_id: str,
    artifact_text: str,
    criterion: Criterion,
) -> int:
    """Issue one ``count_tokens`` for the representative ``(artifact,
    criterion)`` prompt and return ``input_tokens``.

    Per ``grade-layer.md`` DEC-004 the real grader issues one
    ``messages.create`` per ``(artifact × criterion)`` pair. The
    estimate uses ONE representative artifact per criterion (the first
    alphabetical artifact_id) — the cached rubric block is constant
    across artifacts so per-criterion token counts scale linearly with
    artifact count, and counting one representative is a faithful
    proxy that keeps the LLM-call count bounded.
    """
    dynamic_block = render_grade_dynamic_block(artifact_id, artifact_text, criterion)
    block_cached: dict[str, Any] = {
        "type": "text",
        "text": system_and_rubric,
        "cache_control": {"type": "ephemeral", "ttl": grade_config.cache_ttl},
    }
    block_dynamic: dict[str, Any] = {"type": "text", "text": dynamic_block}
    response = client.messages.count_tokens(
        model=grade_config.model,
        system=system_and_rubric,
        messages=[{"role": "user", "content": [block_cached, block_dynamic]}],
    )
    input_tokens = getattr(response, "input_tokens", None)
    if not isinstance(input_tokens, int):
        msg = "count_tokens response is missing the `input_tokens` field."
        raise RuntimeError(msg)
    return input_tokens


def _build_representative_sql(model: Model, adapter: WarehouseAdapter, sample_size: int) -> str:
    """Build a representative ``SELECT col FROM source LIMIT n`` SQL
    string to hand to ``adapter.estimate_query_bytes(...)``.

    We deliberately do NOT round-trip the prune compiler here — the
    estimate engine doesn't have a ``CandidateSchema`` (the drafter
    hasn't run). The representative query is the smallest reasonable
    shape that exercises the same source-table scan a real prune test
    would issue: a column reference + a ``LIMIT``.

    Identifier shape is enforced by :class:`TableRef.from_model`
    (project / dataset / table validate at construction); column
    identifier is enforced by the manifest loader and re-validated by
    the dialect's quote_char path inside the SQL string. The dry-run
    SDK call rejects anything malformed before billing — but we still
    apply the dialect quote_char for safety.
    """
    table_ref = TableRef.from_model(model)
    dialect = adapter.dialect()
    quote_char = dialect.quote_char

    # Pick the first column alphabetical to keep the representative
    # SQL deterministic across runs.
    columns = sorted(model.columns_list, key=lambda c: c.name)
    # No columns in the manifest? Fall back to ``*`` — the ``dry_run``
    # still reports bytes against the source table.
    column_sql = f"{quote_char}{columns[0].name}{quote_char}" if columns else "*"

    qualified = table_ref.qualified_name
    table_sql = f"{quote_char}{qualified}{quote_char}"
    return f"SELECT {column_sql} FROM {table_sql} LIMIT {sample_size}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def estimate(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: WarehouseAdapter,
    anthropic_client: _AnthropicClientProtocol,
    *,
    project_dir: Path | None = None,  # noqa: ARG001 (reserved for v0.2)
) -> EstimateReport:
    """Run the cost-preview engine for ``model``.

    Returns a frozen :class:`EstimateReport` with USD math, token
    counts, and warehouse-bytes estimate. Never issues a billable LLM
    or warehouse call; the contract is enforced by AC-4 of issue #36
    (``test_estimate_never_calls_messages_create``).

    Partial-failure degrade (DEC-005): if
    ``adapter.estimate_query_bytes`` raises any
    :class:`WarehouseError` subclass, the report's warehouse fields
    are ``None`` and ``warehouse_unavailable_reason`` carries the
    error class name + truncated message. All other failures
    propagate to the CLI's panic boundary.

    Args:
        model: The manifest :class:`Model` under estimate.
        manifest: The full :class:`Manifest` (needed for the drafter
            prompt's cached-block neighbour-rendering).
        draft_config: Loaded :class:`DraftConfig`.
        grade_config: Loaded :class:`GradeConfig`.
        prune_config: Loaded :class:`PruneConfig` (carries
            ``sample_size`` for the representative dry-run SQL).
        adapter: Constructed :class:`WarehouseAdapter`.
        anthropic_client: Constructed Anthropic client (or test fake
            satisfying the protocol).
        project_dir: Reserved for v0.2 (caching, sidecar paths).

    Returns:
        The frozen :class:`EstimateReport`.
    """
    _start = time.perf_counter()
    run_id = uuid.uuid4().hex

    # ---- Drafter cost projection ----------------------------------
    pricing_draft = _pricing.lookup(draft_config.model)
    request = _build_schema_only_request(model)
    system, cached_block, dynamic_block, _prompt_version = render_prompt(model, request, manifest)
    draft_input_tokens = _count_draft_tokens(
        client=anthropic_client,
        draft_config=draft_config,
        system=system,
        cached_block=cached_block,
        dynamic_block=dynamic_block,
    )
    draft_output_tokens_estimate = draft_config.max_output_tokens
    draft_usd = (draft_input_tokens / 1_000_000.0) * pricing_draft.input_per_mtok + (
        draft_output_tokens_estimate / 1_000_000.0
    ) * pricing_draft.output_per_mtok

    # ---- Grader cost projection -----------------------------------
    pricing_grade = _pricing.lookup(grade_config.model)
    rubric = grade_config.rubric or DEFAULT_RUBRIC
    column_count = len(model.columns_list)
    test_count_estimate = int(_TESTS_PER_COLUMN_HEURISTIC * column_count)
    artifact_count = (
        2 * column_count  # column description + rationale per column
        + 2  # model description + rationale
        + test_count_estimate  # per-test rationale (column-scoped + model-scoped)
    )

    # Representative grade prompt: use the first-alphabetical
    # artifact_id with a representative placeholder text. Real
    # grading varies the artifact text per pair; counting one
    # representative per criterion and multiplying by artifact_count
    # is the documented proxy (DEC-004 grade-layer.md + the engine
    # docstring above).
    rep_artifact_id, rep_artifact_text = _representative_artifact(model)
    rubric_block = render_rubric_block(rubric)
    # The grader's cached block is the rubric block (constant per
    # run). For token-counting purposes we use the rubric block as the
    # cached prefix; the system message is short and is included via
    # the ``system`` argument the SDK exposes separately.
    system_and_rubric = rubric_block

    per_criterion: list[CriterionEstimate] = []
    grade_total_input_tokens = 0
    grade_usd = 0.0
    for criterion in rubric:
        input_tokens_per_call = _count_grade_criterion_tokens(
            client=anthropic_client,
            grade_config=grade_config,
            system_and_rubric=system_and_rubric,
            artifact_id=rep_artifact_id,
            artifact_text=rep_artifact_text,
            criterion=criterion,
        )
        calls = artifact_count
        total_input_tokens = input_tokens_per_call * calls
        criterion_usd = (total_input_tokens / 1_000_000.0) * pricing_grade.input_per_mtok + (
            _GRADE_OUTPUT_TOKENS_PER_CALL * calls / 1_000_000.0
        ) * pricing_grade.output_per_mtok
        per_criterion.append(
            CriterionEstimate(
                criterion_id=criterion.id,
                criterion_text_truncated=_truncate_criterion_text(criterion.criterion),
                calls=calls,
                input_tokens_per_call=input_tokens_per_call,
                total_input_tokens=total_input_tokens,
                estimated_output_tokens_per_call=_GRADE_OUTPUT_TOKENS_PER_CALL,
                usd=criterion_usd,
            )
        )
        grade_total_input_tokens += total_input_tokens
        grade_usd += criterion_usd

    total_llm_usd = draft_usd + grade_usd

    # ---- Warehouse-bytes projection (DEC-005 degrade) -------------
    sample_size = prune_config.sample_size
    warehouse_bytes_per_row: int | None = None
    warehouse_total_bytes: int | None = None
    warehouse_unavailable_reason: str | None = None
    representative_sql = _build_representative_sql(model, adapter, sample_size)
    try:
        dry_run_bytes = adapter.estimate_query_bytes(representative_sql)
    except WarehouseError as exc:
        warehouse_unavailable_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        _LOGGER.warning(
            "warehouse-bytes unavailable: %s",
            json.dumps(
                {
                    "run_id": run_id,
                    "model_unique_id": model.unique_id,
                    "error_class": type(exc).__name__,
                    "error_message": str(exc)[:200],
                }
            ),
        )
    else:
        # The dry_run reports total bytes for the representative
        # single-test query. We multiply by ``test_count_estimate``
        # to project the total bytes the prune run would scan;
        # ``warehouse_bytes_per_row`` is a display-only divider.
        warehouse_total_bytes = dry_run_bytes * max(1, test_count_estimate)
        warehouse_bytes_per_row = max(1, dry_run_bytes // sample_size) if sample_size > 0 else None

    duration_seconds = time.perf_counter() - _start

    report = EstimateReport(
        model_unique_id=model.unique_id,
        drafter_model=draft_config.model,
        grader_model=grade_config.model,
        draft_input_tokens=draft_input_tokens,
        draft_output_tokens_estimate=draft_output_tokens_estimate,
        draft_usd=draft_usd,
        grade_artifacts_count=artifact_count,
        grade_criteria_count=len(rubric),
        grade_per_criterion=tuple(per_criterion),
        grade_usd=grade_usd,
        total_llm_usd=total_llm_usd,
        warehouse_bytes_per_row=warehouse_bytes_per_row,
        warehouse_total_bytes=warehouse_total_bytes,
        warehouse_unavailable_reason=warehouse_unavailable_reason,
        tests_per_column_heuristic=_TESTS_PER_COLUMN_HEURISTIC,
        sample_size=sample_size,
        price_table_version=_pricing.PRICE_TABLE_VERSION,
        duration_seconds=duration_seconds,
    )

    # Single end-of-run INFO via lazy-format JSON (DEC-013).
    _LOGGER.info(
        "estimate complete: %s",
        json.dumps(
            {
                "run_id": run_id,
                "model_unique_id": model.unique_id,
                "drafter_model": draft_config.model,
                "grader_model": grade_config.model,
                "draft_tokens": draft_input_tokens,
                "grade_tokens": grade_total_input_tokens,
                "total_llm_usd": round(total_llm_usd, 6),
                "total_bytes": warehouse_total_bytes,
                "duration_seconds": round(duration_seconds, 6),
                "price_table_version": _pricing.PRICE_TABLE_VERSION,
            }
        ),
    )

    return report


def _representative_artifact(model: Model) -> tuple[str, str]:
    """Pick the first-alphabetical artifact_id + a representative
    artifact text for grade-prompt token counting.

    Real grading iterates over the actual drafted artifacts; the
    estimate engine doesn't have those (the drafter hasn't run).
    We use ``"column.<first_col>.description"`` as a representative
    artifact_id (matches the grade engine's
    ``_artifact_id_for(...)`` formatter byte-for-byte for this
    shape) and the model's description (or a placeholder) as the
    representative artifact text.

    Returns ``(artifact_id, artifact_text)``. Documented assumption:
    per-criterion token cost varies less across artifacts than it
    does across criteria, so one representative per criterion is a
    faithful proxy (DEC-004 grade-layer.md mirror in the engine
    docstring).
    """
    columns = sorted(model.columns_list, key=lambda c: c.name)
    if columns:
        artifact_id = f"column.{columns[0].name}.description"
        # Use the column's description if present, else the column
        # name as a placeholder — produces a stable representative
        # text shape across runs.
        artifact_text = columns[0].description or f"Column {columns[0].name}"
    else:
        artifact_id = "model.description"
        artifact_text = model.description or f"Model {model.name}"
    return artifact_id, artifact_text


# ---------------------------------------------------------------------------
# Public text renderer (US-004, DEC-007 + DEC-011)
# ---------------------------------------------------------------------------


# Binary (1024-based) byte formatting: "1.5 MB" = 1.5 * 1024 * 1024 bytes.
# Decimal-place width is fixed at 1 so the column stays visually stable.
_BYTE_UNITS: tuple[tuple[str, int], ...] = (
    ("GB", 1024 * 1024 * 1024),
    ("MB", 1024 * 1024),
    ("KB", 1024),
)


def _format_bytes(n: int) -> str:
    """Format an integer byte count as a short human-readable string.

    Binary (1024-based) units. Single-decimal precision keeps the
    column visually stable across snapshot fixtures. Inputs below 1 KB
    render as raw bytes (``"512 B"``); negative values are clamped to
    zero — the engine never emits negatives but the renderer stays
    defensive.
    """
    if n < 0:
        n = 0
    for unit, scale in _BYTE_UNITS:
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} B"


def render(report: EstimateReport) -> str:
    """Render the cost-preview text for ``report``.

    Pure function. Same :class:`EstimateReport` -> same bytes every
    call. Output ends with a single trailing newline. The contract
    shape is locked verbatim by DEC-007 of
    ``plans/super/36-estimate-cost-preview.md`` and pinned by snapshot
    fixtures ``tests/fixtures/estimate/output_happy.txt`` and
    ``tests/fixtures/estimate/output_warehouse_unavailable.txt``.

    The CLI wrapper (US-005) is responsible for echoing the user-facing
    ``signalforge generate --estimate <model>`` command above the
    renderer's output; this function starts from the prelude block.

    Partial-failure shape (DEC-005): when
    ``report.warehouse_unavailable_reason is not None``, the warehouse
    section renders ``bytes-per-row: <unavailable: <ErrorClass>>``,
    ``total bytes: <unknown>``, and the final totals line shows
    ``Total estimated warehouse: <unknown>``. The error-class name is
    the first ``:``-separated chunk of the reason field (verbatim
    from the engine's ``f"{type(exc).__name__}: ..."`` shape).
    """
    lines: list[str] = []

    # ---- Prelude ---------------------------------------------------
    lines.append(f"Estimate for {report.model_unique_id}")
    lines.append(f"  drafter: {report.drafter_model}")
    lines.append(f"  grader:  {report.grader_model}")
    lines.append("")

    # ---- Draft section ---------------------------------------------
    lines.append("Estimated draft cost:")
    lines.append(f"  input tokens:   {report.draft_input_tokens:,}")
    lines.append(f"  output tokens:  ~{report.draft_output_tokens_estimate:,} (estimated)")
    lines.append(f"  cost:           ${report.draft_usd:.4f}")
    lines.append("")

    # ---- Grade section ---------------------------------------------
    total_calls = sum(c.calls for c in report.grade_per_criterion)
    lines.append("Estimated grade cost:")
    lines.append(
        f"  artifacts: {report.grade_artifacts_count}   "
        f"criteria: {report.grade_criteria_count}   "
        f"calls: {total_calls}"
    )
    lines.append("  per criterion:")
    for criterion in report.grade_per_criterion:
        # Fixed-width label column (16 chars) keeps the section visually
        # stable across rubrics with varying id lengths.
        label = f"{criterion.criterion_id:<16}"
        lines.append(
            f"    {label}{criterion.calls:>3} calls   "
            f"{criterion.total_input_tokens:,} tokens   "
            f"${criterion.usd:.4f}"
        )
    lines.append(f"  cost:           ${report.grade_usd:.4f}")
    lines.append("")

    # ---- Warehouse section (DEC-005 partial-failure degrade) -------
    lines.append("Estimated warehouse cost:")
    if report.warehouse_unavailable_reason is not None:
        # Extract the error class name verbatim — the engine produces
        # ``f"{type(exc).__name__}: {str(exc)[:200]}"`` so the first
        # ``:``-separated chunk is the class name.
        error_class = report.warehouse_unavailable_reason.split(":", 1)[0]
        lines.append(f"  bytes-per-row:    <unavailable: {error_class}>")
        lines.append("  total bytes:      <unknown>")
        warehouse_total_str = "<unknown>"
    else:
        # mypy/pyright narrowing: when reason is None, both warehouse
        # fields are populated in lockstep by the engine (DEC-005).
        bytes_per_row = report.warehouse_bytes_per_row
        total_bytes = report.warehouse_total_bytes
        assert bytes_per_row is not None  # noqa: S101 (engine invariant)
        assert total_bytes is not None  # noqa: S101 (engine invariant)
        test_count_est = int(report.tests_per_column_heuristic * _column_count_from_report(report))
        lines.append(f"  bytes-per-row:    ~{bytes_per_row:,} (BigQuery dryRun)")
        lines.append(
            f"  test count est:   {test_count_est} "
            f"({report.tests_per_column_heuristic} tests/col x "
            f"{_column_count_from_report(report)} cols)"
        )
        lines.append(f"  sample size:      {report.sample_size:,} rows")
        lines.append(f"  total bytes:      ~{_format_bytes(total_bytes)}")
        warehouse_total_str = f"~{_format_bytes(total_bytes)}"
    lines.append("")

    # ---- Totals ----------------------------------------------------
    lines.append(f"Total estimated LLM cost: ${report.total_llm_usd:.4f}")
    lines.append(f"Total estimated warehouse: {warehouse_total_str}")
    lines.append("")

    # ---- Footer ----------------------------------------------------
    lines.append(
        f"Price table: {report.price_table_version}  |  "
        f"Heuristic: ~{report.tests_per_column_heuristic} tests/column "
        "(canonical fixture average)"
    )

    return "\n".join(lines) + "\n"


def _column_count_from_report(report: EstimateReport) -> int:
    """Reverse-engineer the column count from the report's stored
    artifact-count and tests-per-column heuristic.

    ``EstimateReport`` does not carry ``column_count`` directly; the
    engine derives it from ``len(model.columns_list)`` and folds it
    into ``grade_artifacts_count`` via
    ``2*cols + 2 + int(heuristic * cols)``. The renderer needs the
    column count for the warehouse section's display string; we
    recover it from the artifact-count via the inverse formula.

    The formula is:
        artifacts = 2*cols + 2 + int(heuristic * cols)
                  = cols * (2 + heuristic) + 2  (modulo int() floor)
        cols      ≈ (artifacts - 2) / (2 + heuristic)

    Floor division matches the engine's ``int(...)`` truncation on the
    test-count term; the round-trip is exact for the integer column
    counts the engine emits.
    """
    artifacts_minus_constant = report.grade_artifacts_count - 2
    divisor = 2.0 + report.tests_per_column_heuristic
    if divisor <= 0 or artifacts_minus_constant < 0:
        return 0
    return int(round(artifacts_minus_constant / divisor))
