"""Shared substrate for the #179 runtime-benchmark harness.

Provides the deterministic inputs the benchmark times the pipeline against:
a manifest :class:`Model`, a single-node :class:`Manifest`, a schema-only
:class:`SafetyPolicy`, a do-nothing warehouse adapter (schema-only mode never
invokes it — see ``safety-layer.md`` DEC-012(c)), and an empty
:class:`PruneResult` (the benchmark runs with ``prune.enabled: false``, so the
prune stage does no warehouse work and the grade stage receives an empty
decision tuple).

**Why an inlined model, not the real intuit_airflow slice.** The epic's
benchmark target is the ``weekly_query_cost.sql`` baseline + a 10-model retest
slice from ``~/Projects/intuit_airflow`` (see
:file:`docs/research/179-runtime-benchmark.md`). That repo + its synthesised
schema.yml are NOT reproducible in CI, so this skeleton ships ONE representative
inlined model (``mart_query_cost_daily``, 8 business columns — modelled on the
shape of the baseline target) so the harness is runnable on any maintainer
machine with only an ``ANTHROPIC_API_KEY``. Swapping in the real slice is the
documented maintainer step (``build_models`` TODO below); the timing scaffold
does not change.

The model count and column count drive the grade-stage call volume (one LLM
call per ``artifact × criterion`` — DEC-004 of #7), which is the dominant term
in the wall-clock the benchmark exists to measure. Keep the inlined model modest
so a skeleton run stays affordable; the real measurement uses the full slice.
"""

from __future__ import annotations

import signalforge as _sf
from signalforge.draft.config import DraftConfig
from signalforge.grade.config import GradeConfig
from signalforge.diff.config import DiffConfig
from signalforge.manifest.models import Column, Manifest, Model
from signalforge.prune.models import PruneResult
from signalforge.safety.policy import SafetyPolicy

# Inlined SQL modelled on the shape of the 2026-05-30 baseline target
# (``plugins/dbt/models/reporting/weekly_query_cost.sql`` — a per-signature
# cost rollup). Self-contained so the harness reproduces without intuit_airflow.
_QUERY_COST_SQL = """with daily as (
    select
        query_signature,
        query_type,
        date_trunc('day', start_time) as cost_date,
        count(*) as num_executions,
        sum(rows_inserted) as total_rows_inserted,
        sum(bytes_scanned) as total_bytes_scanned,
        sum(cost_usd) as total_cost,
        sum(cost_usd) / nullif(count(*), 0) as avg_cost_per_execution
    from {{ ref('query_cost') }}
    where start_time < current_date
    group by 1, 2, 3
)

select * from daily"""


def build_model() -> Model:
    """Return one representative manifest :class:`Model` for timing the pipeline.

    Constructed deterministically (no LLM, no warehouse). Eight business columns
    so the grade stage has a representative artifact count to time against.
    """
    return Model(
        unique_id="model.bench.mart_query_cost_daily",
        name="mart_query_cost_daily",
        resource_type="model",
        package_name="bench",
        original_file_path="models/reporting/mart_query_cost_daily.sql",
        path="reporting/mart_query_cost_daily.sql",
        database="bench-proj",
        schema="reporting",  # type: ignore[call-arg]
        columns={
            "query_signature": Column(name="query_signature", data_type="STRING"),
            "query_type": Column(name="query_type", data_type="STRING"),
            "cost_date": Column(name="cost_date", data_type="DATE"),
            "num_executions": Column(name="num_executions", data_type="INT64"),
            "total_rows_inserted": Column(name="total_rows_inserted", data_type="INT64"),
            "total_bytes_scanned": Column(name="total_bytes_scanned", data_type="INT64"),
            "total_cost": Column(name="total_cost", data_type="FLOAT64"),
            "avg_cost_per_execution": Column(name="avg_cost_per_execution", data_type="FLOAT64"),
        },
        raw_code=_QUERY_COST_SQL,
    )


def build_models() -> list[Model]:
    """Return the list of models the benchmark drafts + grades + diffs.

    Skeleton ships ONE inlined model so the harness runs without intuit_airflow.

    TODO(maintainer): to reproduce the epic's real measurement, replace this with
    the ``weekly_query_cost.sql`` baseline target + the 10-model retest slice from
    ``~/Projects/intuit_airflow/plugins/dbt`` (synthesise each model's columns via
    the AST schema synthesiser referenced in
    :file:`docs/research/179-test-primitive-expansion-retest.md` § Substrate, then
    ``manifest.load`` the project and select the slice). The timing scaffold in
    :mod:`test_runtime_benchmark` iterates whatever this returns — no other change
    needed.
    """
    return [build_model()]


def build_manifest(models: list[Model]) -> Manifest:
    """Return a :class:`Manifest` containing ``models`` (drafter neighbour lookup)."""
    return Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={m.unique_id: m for m in models},
    )


def schema_only_policy(audit_path) -> SafetyPolicy:
    """Return a schema-only :class:`SafetyPolicy` writing audit JSONL under ``audit_path``.

    Schema-only is the benchmark's safety mode (mirrors the 2026-05-30 baseline's
    ``safety.mode: schema-only``): no warehouse sampling, so the adapter is never
    invoked and the draft stage's wall-clock is pure LLM round-trip + parse.
    """
    return SafetyPolicy(audit_path=audit_path)


def null_adapter():
    """Return a do-nothing warehouse adapter for the schema-only draft path.

    Schema-only mode never invokes the adapter (``safety-layer.md`` DEC-012(c)),
    so the test-suite ``FakeAdapter`` is a sufficient stand-in. Imported lazily so
    importing this substrate has no dependency on the ``tests`` package at module
    load.
    """
    from tests.safety._fake_adapter import FakeAdapter

    return FakeAdapter()


def empty_prune_result(model: Model) -> PruneResult:
    """Return an empty :class:`PruneResult` linked to ``model``.

    Mirrors ``prune.enabled: false``: every candidate would route to
    ``kept-without-evidence`` with no warehouse contact. For the benchmark the
    grade + diff stages only need the model-id linkage and an empty decision
    tuple, so this is the minimal valid result.
    """
    return PruneResult(
        model_unique_id=model.unique_id,
        decisions=(),
        elapsed_ms=0,
        signalforge_version=_sf.__version__,
    )


def draft_config() -> DraftConfig:
    """Return the shipped-default :class:`DraftConfig` (anthropic provider).

    The benchmark measures the DEFAULTS that ship on ``dev`` — the point is the
    shipped efficiency, not a tuned config — so this is the bare default.
    """
    return DraftConfig()


def grade_config(model_override: str | None = None) -> GradeConfig:
    """Return the grade config to time against.

    ``model_override is None`` → the shipped anthropic default (Sonnet,
    post-#187). This captures #186 (the always-on asyncio refactor) but NOT #187
    (Haiku is opt-in). Pass ``model_override="claude-haiku-4-5"`` (the harness
    wires this from ``SF_BENCH_GRADE_MODEL``) to time the #187 opt-in path.
    """
    if model_override:
        return GradeConfig(model=model_override)
    return GradeConfig()


def diff_config() -> DiffConfig:
    """Return the shipped-default :class:`DiffConfig`."""
    return DiffConfig()
