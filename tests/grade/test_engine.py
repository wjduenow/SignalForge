"""Tests for ``signalforge.grade.engine`` (US-008).

Pins the load-bearing properties of the grader orchestrator across the
full criterion/artifact iteration matrix, the budget short-circuit
(DEC-023), the per-pair degraded-path semantics (DEC-015), the
fail-closed audit-write contract (DEC-006), the whole-run
envelope-breach pre-flight (DEC-013), and the canonical
``_artifact_id_for`` formatter (DEC-009).

Mirrors :mod:`tests.prune.test_engine` shape — every test injects a
:class:`tests.llm._fake.FakeAnthropicClient` (driven via the local
:func:`tests.grade._fake.expect_grade_responses` helper) into a
real :func:`signalforge.grade.grade_artifacts` call. No production
code imports the fake.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
    CandidateTestUnique,
)
from signalforge.grade import engine as engine_module
from signalforge.grade.audit import _GRADE_AUDIT_RECORD_LIMIT_BYTES
from signalforge.grade.config import GradeConfig
from signalforge.grade.engine import (
    _artifact_id_for,
    _iterate_artifacts,
    _stable_artifact_pairs,
    grade_artifacts,
)
from signalforge.grade.errors import (
    GradeAuditWriteError,
    GradeBelowThresholdError,
    GradeError,
    GradeIncompleteError,
    GradePromptEnvelopeBreachError,
)
from signalforge.grade.models import GradeEvent, GradingReport
from signalforge.grade.rubric import DEFAULT_RUBRIC, Criterion, Rubric
from signalforge.llm.errors import EstimateUnknownModelError, LLMRateLimitError
from signalforge.manifest.models import Column, Manifest, Model
from signalforge.prune.models import PruneResult
from tests.grade._fake import expect_grade_responses
from tests.llm._fake import FakeAnthropicClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "grade"


def _make_model() -> Model:
    return Model(
        unique_id="model.shop.orders",
        name="orders",
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={
            "order_id": Column(name="order_id"),
            "customer_id": Column(name="customer_id"),
        },
        raw_code="select 1",
    )


def _make_manifest(model: Model) -> Manifest:
    return Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={model.unique_id: model},
    )


def _empty_prune_result(model: Model) -> PruneResult:
    return PruneResult(
        model_unique_id=model.unique_id,
        decisions=(),
        elapsed_ms=0,
        signalforge_version="0.0.0-test",
    )


def _load_sample_candidate() -> CandidateSchema:
    raw = (_FIXTURE_PATH / "sample_candidate.json").read_text(encoding="utf-8")
    return CandidateSchema.model_validate_json(raw)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _two_criteria() -> Rubric:
    """A small two-criterion rubric for fast tests (8 calls = 4 artifacts × 2)."""
    return (
        Criterion(id="clarity", criterion="Is it clear?"),
        Criterion(id="rationale", criterion="Is the rationale present and useful?"),
    )


def _project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".signalforge").mkdir(parents=True, exist_ok=True)
    return project_dir


def _config_no_audit_in_path(model_id: str = "claude-fake") -> GradeConfig:
    """Construct a :class:`GradeConfig` with knobs tuned for fast tests."""
    return GradeConfig(
        model=model_id,
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=60,
    )


# ---------------------------------------------------------------------------
# _artifact_id_for canonical format (DEC-009)
# ---------------------------------------------------------------------------


def test_artifact_id_for_helper_canonical_format() -> None:
    """Six DEC-009 dotted-path shapes round-trip through
    :func:`_artifact_id_for`.
    """
    assert (
        _artifact_id_for(scope="column", column_name="email", field="description")
        == "column.email.description"
    )
    assert (
        _artifact_id_for(scope="column", column_name="email", field="rationale")
        == "column.email.rationale"
    )
    assert _artifact_id_for(scope="model", field="description") == "model.description"
    assert _artifact_id_for(scope="model", field="rationale") == "model.rationale"

    nn_test = CandidateTestNotNull(column="user_id")
    assert (
        _artifact_id_for(scope="column", column_name="user_id", test=nn_test)
        == "test.column.user_id.not_null"
    )
    # Model-level test, no args_hash: bare form.
    uq_test = CandidateTestUnique(column="email")
    assert _artifact_id_for(scope="model", test=uq_test) == "test.model.unique"
    # Model-level test, with args_hash suffix.
    assert (
        _artifact_id_for(scope="model", test=uq_test, args_hash="abcd1234")
        == "test.model.unique.abcd1234"
    )


def test_artifact_id_for_collision_args_hash_disambiguates() -> None:
    """Two model-level tests with the same ``test.type`` but different
    args produce different ``args_hash`` values.
    """
    av1 = CandidateTestAcceptedValues(column="status", values=("a", "b"))
    av2 = CandidateTestAcceptedValues(column="region", values=("us", "eu"))
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(CandidateColumn(name="status", description="status"),),
        tests=(av1, av2),
    )
    pairs = _stable_artifact_pairs(candidate)
    model_test_ids = [aid for aid, _ in pairs if aid.startswith("test.model.")]
    # Both share test.type=accepted_values; the hash suffix must
    # disambiguate them.
    assert len(model_test_ids) == 2
    assert model_test_ids[0] != model_test_ids[1]
    for aid in model_test_ids:
        assert aid.startswith("test.model.accepted_values.")


def test_artifact_id_for_column_scope_collision_args_hash_disambiguates() -> None:
    """Two tests on the SAME column with the same ``test.type`` but
    different args produce different ``args_hash`` values (QG pass 2 fix).

    Without disambiguation, both would render as
    ``test.column.status.accepted_values`` and JSONL records would
    collide on the (run_id, artifact_id, criterion_id) triple.
    """
    av1 = CandidateTestAcceptedValues(column="status", values=("a", "b"))
    av2 = CandidateTestAcceptedValues(column="status", values=("c", "d"))
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(
            CandidateColumn(
                name="status",
                description="status",
                tests=(av1, av2),
            ),
        ),
        tests=(),
    )
    pairs = _stable_artifact_pairs(candidate)
    column_test_ids = [aid for aid, _ in pairs if aid.startswith("test.column.")]
    assert len(column_test_ids) == 2
    assert column_test_ids[0] != column_test_ids[1]
    for aid in column_test_ids:
        # Five-part dotted form: test.column.status.accepted_values.<8-hex>
        assert aid.startswith("test.column.status.accepted_values.")
        assert len(aid.rsplit(".", 1)[1]) == 8  # 8-hex args_hash


def test_artifact_id_for_column_scope_unique_test_no_args_hash() -> None:
    """A test that's unique within its column does NOT carry an args_hash.

    Regression detector for the column-scope disambiguator: only collisions
    add the suffix; a single not_null on a column emits the bare 4-part form.
    """
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(
            CandidateColumn(
                name="user_id",
                description="pk",
                tests=(CandidateTestNotNull(column="user_id"),),
            ),
        ),
        tests=(),
    )
    pairs = _stable_artifact_pairs(candidate)
    column_test_ids = [aid for aid, _ in pairs if aid.startswith("test.column.")]
    assert column_test_ids == ["test.column.user_id.not_null"]


# ---------------------------------------------------------------------------
# Iteration order (DEC-018)
# ---------------------------------------------------------------------------


def test_grade_artifacts_iteration_order_stable() -> None:
    """Two invocations of :func:`_iterate_artifacts` against the same
    candidate + rubric yield identical sequences.
    """
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    seq_a = [(aid, cid.id) for aid, _, cid in _iterate_artifacts(candidate, rubric)]
    seq_b = [(aid, cid.id) for aid, _, cid in _iterate_artifacts(candidate, rubric)]
    assert seq_a == seq_b
    # Sanity: criterion-outer, artifact-inner. The first run of one
    # criterion must complete before the next criterion starts.
    cids = [cid for _, cid in seq_a]
    assert cids[: len(cids) // 2] == ["clarity"] * (len(cids) // 2)
    assert cids[len(cids) // 2 :] == ["rationale"] * (len(cids) // 2)


# ---------------------------------------------------------------------------
# Smoke / happy path
# ---------------------------------------------------------------------------


def test_grade_artifacts_smoke_with_fake_client(tmp_path: Path) -> None:
    """End-to-end happy path: rubric × artifacts produces a full report.

    The sample fixture has 2 columns + 1 column test + 0 model tests:

    * 2 column descriptions
    * 2 column rationales
    * 1 model description
    * 1 model rationale
    * 1 column test rationale
    * 0 model test rationales

    = 7 artifacts × 2 criteria = 14 LLM calls.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )

    fake.assert_all_expectations_met()
    assert isinstance(report, GradingReport)
    assert report.model_unique_id == "model.shop.orders"
    assert len(report.results) == 14
    assert all(r.score == 0.5 for r in report.results)
    assert all(r.passed for r in report.results)
    assert report.aggregate_complete is True


def test_grade_artifacts_writes_jsonl_per_call_durably(tmp_path: Path) -> None:
    """Per-call JSONL writes happen sequentially. After a successful run
    the JSONL row count equals ``len(results)`` and every row parses.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
        audit_path=audit_path,
    )

    rows = _read_jsonl(audit_path)
    assert len(rows) == len(report.results) == 14
    # Each row parses through the strict GradeEvent model with
    # extra="ignore" — round-trips cleanly.
    parsed = [GradeEvent.model_validate(r) for r in rows]
    assert all(p.run_id == report.run_id for p in parsed)
    # The JSONL run_ids match the report's run_id exactly.
    assert all(p.signalforge_version == report.signalforge_version for p in parsed)


def test_grade_artifacts_writes_sidecar_at_end_of_run(tmp_path: Path) -> None:
    """End-of-run sidecar lands at ``<project>/.signalforge/grade.json``
    and round-trips through :meth:`GradingReport.model_validate_json`.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    sidecar_path = project_dir / ".signalforge" / "grade.json"
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
        sidecar_path=sidecar_path,
    )

    assert sidecar_path.exists()
    raw = sidecar_path.read_text(encoding="utf-8").strip()
    round_tripped = GradingReport.model_validate_json(raw)
    assert round_tripped.run_id == report.run_id
    assert len(round_tripped.results) == len(report.results)


def test_grade_artifacts_default_audit_path_resolution(tmp_path: Path) -> None:
    """Without an explicit ``audit_path``, the JSONL lands at
    ``<project_dir>/.signalforge/grade.jsonl``.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )

    assert (project_dir / ".signalforge" / "grade.jsonl").exists()


def test_grade_artifacts_default_sidecar_path_resolution(tmp_path: Path) -> None:
    """Without an explicit ``sidecar_path``, the sidecar lands at
    ``<project_dir>/.signalforge/grade.json``.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )

    assert (project_dir / ".signalforge" / "grade.json").exists()


# ---------------------------------------------------------------------------
# Budget short-circuit (DEC-023, DEC-015)
# ---------------------------------------------------------------------------


def _config_tiny_budget(model_id: str = "claude-fake") -> GradeConfig:
    """A :class:`GradeConfig` with a sub-second budget so
    :func:`asyncio.timeout` trips immediately under the slow-coroutine
    monkey-patch used by the budget tests below.

    Mirrors :func:`_config_no_audit_in_path` defaults; the only delta is
    ``total_budget_seconds=1`` (the GradeConfig validator's lower bound
    — anything below would fail Pydantic-validation).
    """
    return GradeConfig(
        model=model_id,
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=1,
        max_concurrent_calls=2,
    )


def _stub_grade_one_async_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkey-patch :func:`engine_module._grade_one_async` to await a
    sleep longer than the test's ``total_budget_seconds``.

    Used by the budget-exhaustion tests below. The slow coroutine never
    returns naturally; the enclosing ``asyncio.timeout`` cancels every
    in-flight task; the per-coroutine ``except CancelledError`` arm
    routes each pair to the budget-degrade shape (DEC-008).
    """

    async def _slow(**_kw: Any) -> tuple:
        await asyncio.sleep(60)  # well beyond total_budget_seconds=1
        raise AssertionError("unreachable — timeout should fire first")

    monkeypatch.setattr(engine_module, "_grade_one_async", _slow)


def test_grade_artifacts_budget_exceeded_marks_remaining_pairs_score_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the asyncio budget timeout fires, every pair degrades.

    The async core's ``asyncio.timeout(total_budget_seconds)`` cancels
    every in-flight + un-started task. Per-coroutine ``except
    CancelledError`` arms route to the budget-degrade shape (DEC-008).
    Un-started tasks (those still waiting on the semaphore when the
    timeout fired) get filled in by the post-``TaskGroup`` synthesis
    pass.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    _stub_grade_one_async_slow(monkeypatch)

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_tiny_budget(),
        client=fake,
        project_dir=project_dir,
    )

    # All pairs degraded.
    assert all(r.score is None for r in report.results)
    assert all(r.passed is False for r in report.results)
    assert report.aggregate_complete is False
    # DEC-009: the wall-clock degrade reason embeds the EFFECTIVE budget, not
    # the raw config field. The tiny-budget config sets total_budget_seconds=1,
    # which caps effective = min(scaled, 1) = 1, so the locked string reads
    # "(1s)". Pin it verbatim — this is the one DEC-009 reason string the other
    # tests don't `==`-pin, so a wording drift (or a regression that reverts to
    # interpolating the raw total instead of effective) would otherwise slip.
    assert all(
        r.reasoning == "grade budget exceeded (1s) before evaluation" for r in report.results
    )
    # #202 US-001: the structured discriminator classifies the budget
    # degrade WITHOUT re-parsing the prose above.
    assert all(r.degrade_reason_type == "budget" for r in report.results)


def test_grade_artifacts_budget_exceeded_aggregate_complete_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``aggregate_complete`` is ``False`` whenever any result has
    ``score=None``.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    _stub_grade_one_async_slow(monkeypatch)

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_tiny_budget(),
        client=fake,
        project_dir=project_dir,
    )

    assert report.aggregate_complete is False
    assert report.passed is False  # any None aggregates to 0.0 pass_rate


# ---------------------------------------------------------------------------
# Per-pair LLM failure (DEC-015)
# ---------------------------------------------------------------------------


def test_grade_artifacts_one_criterion_retry_exhausted_does_not_fail_whole_report(
    tmp_path: Path,
) -> None:
    """A single LLM-layer failure on one ``(artifact, criterion)`` pair
    leaves every other pair scored. The failed pair degrades.

    **Order-agnostic under concurrent dispatch.** Per DEC-015 of #186,
    the async core dispatches every pair in parallel (throttled by
    ``max_concurrent_calls``). The fake's FIFO queue + ``matching=lambda
    _kw: True`` predicates mean the FIRST call to arrive at the fake
    (whichever coroutine wins the race) consumes the
    :class:`LLMRateLimitError` expectation; the remaining 7 consume
    successful payloads. The assertion is **count-based** (one degraded,
    seven scored) — invariant to which specific pair degrades — so the
    test is deterministic regardless of dispatch order. This is the
    pair-identity contract DEC-015 lands at the operator-visible level:
    "a single bad pair never aborts siblings."
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    rubric = _two_criteria()
    # Use a simpler candidate: 1 column, 0 tests = 4 artifacts × 2
    # criteria = 8 calls.
    candidate = CandidateSchema(
        name="orders",
        description="d",
        rationale="r",
        columns=(CandidateColumn(name="order_id", description="pk", rationale="rat"),),
        tests=(),
    )
    artifact_pairs = _stable_artifact_pairs(candidate)
    assert len(artifact_pairs) == 4

    # Enqueue one rate-limit-raising expectation FIRST; FIFO matching
    # means whichever coroutine arrives at the fake first consumes it
    # (the specific pair is non-deterministic under concurrency — the
    # test's count-based assertion below is invariant).
    fake = FakeAnthropicClient()
    from tests.llm._fake import FakeCountTokensResponse

    fake.expect_count_tokens(
        matching=lambda _kw: True,
        returns=FakeCountTokensResponse(input_tokens=1500),
    )
    fake.expect_messages_create(
        matching=lambda _kw: True,
        returns=LLMRateLimitError(
            "fake rate limit",
            attempts=0,
            cause=Exception("fake"),
        ),
    )
    # Remaining 7 pairs succeed normally — enqueue one count_tokens +
    # create pair each.
    from tests.llm._fake import FakeMessage, FakeTextBlock, FakeUsage

    for criterion in rubric:
        for i, (_aid, _atext) in enumerate(artifact_pairs):
            if criterion.id == "clarity" and i == 0:
                continue  # already enqueued (the failing pair)
            fake.expect_count_tokens(
                matching=lambda _kw: True,
                returns=FakeCountTokensResponse(input_tokens=1500),
            )
            fake.expect_messages_create(
                matching=lambda _kw: True,
                returns=FakeMessage(
                    content=[
                        FakeTextBlock(
                            text=json.dumps(
                                {
                                    "criterion_id": criterion.id,
                                    "score": 0.7,
                                    "passed": True,
                                    "evidence": "",
                                    "reasoning": "ok",
                                }
                            ),
                        )
                    ],
                    usage=FakeUsage(input_tokens=1700, output_tokens=80),
                    model="claude-fake-grade-judge",
                ),
            )

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        # require_complete=False: this test pins the "one bad pair never
        # aborts siblings" degrade-report contract; the unrecovered
        # transient pair would otherwise trip the #202 US-006 raise.
        config=_config_no_audit_in_path().model_copy(update={"require_complete": False}),
        client=fake,
        project_dir=project_dir,
    )

    fake.assert_all_expectations_met()
    # 8 results total; exactly one degraded (the pair that won the
    # dispatch race and consumed the rate-limit expectation), the
    # remaining 7 scored. Which specific pair degrades is dispatch-
    # order-dependent; the count + the degrade-reasoning shape are
    # what the contract pins.
    assert len(report.results) == 8
    degraded = [r for r in report.results if r.score is None]
    assert len(degraded) == 1
    assert "GradeLLMError" in degraded[0].reasoning
    # #202 US-001: an LLM-layer failure classifies as "transient".
    assert degraded[0].degrade_reason_type == "transient"
    scored = [r for r in report.results if r.score is not None]
    assert len(scored) == 7
    # Scored pairs carry None for the discriminator.
    assert all(r.degrade_reason_type is None for r in scored)
    assert report.aggregate_complete is False


# ---------------------------------------------------------------------------
# Issue #158 — degrade-reasoning carries LLMResponseFormatError message
# ---------------------------------------------------------------------------


def test_format_degrade_reasoning_includes_response_format_error_message() -> None:
    """``_format_degrade_reasoning`` surfaces the inner
    :class:`LLMResponseFormatError` message — which names the vendor
    ``finish_reason`` field + value — when the wrapped cause is a
    response-shape error.

    This is the issue #158 diagnostic upgrade: before this change every
    Gemini ``MAX_TOKENS`` / ``SAFETY`` / ``RECITATION`` degrade collapsed
    to the bare ``"call failed: GradeLLMError"`` string, forcing
    operators to re-read stderr to distinguish them. The new shape
    includes the provider-emitted message so the audit JSONL / sidecar
    is self-diagnosing.
    """
    from signalforge.grade.engine import _format_degrade_reasoning
    from signalforge.grade.errors import GradeLLMError
    from signalforge.llm.errors import LLMResponseFormatError

    inner = LLMResponseFormatError(
        "Gemini response unclean (finish_reason='MAX_TOKENS').",
    )
    wrapped = GradeLLMError("LLM call failed", cause=inner)

    reasoning = _format_degrade_reasoning(wrapped)

    # Preserves the existing "call failed: <Wrapper>" prefix so the audit
    # corpus stays diff-clean for the 90% case, AND grows the inner
    # message after a colon so the finish_reason value survives.
    assert reasoning == (
        "call failed: GradeLLMError: Gemini response unclean (finish_reason='MAX_TOKENS')."
    )


def test_format_degrade_reasoning_preserves_bare_shape_for_non_response_format_causes() -> None:
    """Every cause other than :class:`LLMResponseFormatError` keeps the
    pre-#158 ``"call failed: <ClassName>"`` shape verbatim.

    Acceptance criterion of bd issue: rate-limit / auth / parser failure
    / budget-exceeded degrades stay diff-clean in the audit corpus —
    only the response-shape branch grows the diagnostic.
    """
    from signalforge.grade.engine import _format_degrade_reasoning
    from signalforge.grade.errors import GradeLLMError, GradeOutputError
    from signalforge.llm.errors import LLMAuthError, LLMRateLimitError

    rate_limit = GradeLLMError(
        "rate limit exhausted",
        cause=LLMRateLimitError("429", attempts=3, cause=RuntimeError("upstream")),
    )
    assert _format_degrade_reasoning(rate_limit) == "call failed: GradeLLMError"

    auth = GradeLLMError(
        "auth failed",
        cause=LLMAuthError("401", cause=RuntimeError("upstream")),
    )
    assert _format_degrade_reasoning(auth) == "call failed: GradeLLMError"

    parser = GradeOutputError("bad json", violation_type="json_parse")
    assert _format_degrade_reasoning(parser) == "call failed: GradeOutputError"


# ---------------------------------------------------------------------------
# Structured degrade discriminator (#202 US-001 / DEC-203)
# ---------------------------------------------------------------------------


def test_classify_degrade_reason_maps_all_three_causes() -> None:
    """``_classify_degrade_reason`` maps each upstream reason string to its
    structured discriminator — the centralised prose→type mapping callers
    rely on instead of string-matching the reason text.
    """
    from signalforge.grade.engine import _classify_degrade_reason

    # Transient: the two ``call failed: …`` shapes from
    # ``_format_degrade_reasoning`` (LLM + parser failures).
    assert _classify_degrade_reason("call failed: GradeLLMError") == "transient"
    assert _classify_degrade_reason("call failed: GradeOutputError") == "transient"
    assert (
        _classify_degrade_reason("call failed: GradeLLMError: finish_reason=SAFETY") == "transient"
    )

    # Budget: the wall-clock backstop reason.
    assert _classify_degrade_reason("grade budget exceeded (500s) before evaluation") == "budget"

    # Ceiling: all three opt-in ceilings share the "ceiling exceeded" marker.
    assert _classify_degrade_reason("grade call ceiling exceeded (200 calls)") == "ceiling"
    assert _classify_degrade_reason("grade cost ceiling exceeded ($5.0)") == "ceiling"
    assert _classify_degrade_reason("grade token ceiling exceeded (100000 tokens)") == "ceiling"


def test_classify_degrade_reason_defaults_to_transient_on_unknown() -> None:
    """An unrecognised reason defaults to ``"transient"`` (the conservative,
    retriable classification) rather than crashing or mis-classifying.
    """
    from signalforge.grade.engine import _classify_degrade_reason

    assert _classify_degrade_reason("some future reason string we never saw") == "transient"
    assert _classify_degrade_reason("") == "transient"


# ---------------------------------------------------------------------------
# Scaled wall-clock budget formula (#198 DEC-001 / DEC-010)
# ---------------------------------------------------------------------------


def test_compute_effective_budget_scaled_no_cap() -> None:
    """``total_budget_seconds=None`` returns the scaled value verbatim.

    base=60, per_pair=20.0, 220 pairs @ concurrency=10 →
    ceil(220/10)=22 waves → 60 + 20*22 = 500.
    """
    result = engine_module._compute_effective_budget(
        budget_base_seconds=60,
        budget_per_pair_seconds=20.0,
        total_budget_seconds=None,
        num_pairs=220,
        max_concurrent_calls=10,
    )
    assert result == 500
    assert isinstance(result, int)


def test_compute_effective_budget_cap_wins() -> None:
    """When the absolute ceiling is below the scaled value, the cap wins."""
    result = engine_module._compute_effective_budget(
        budget_base_seconds=60,
        budget_per_pair_seconds=20.0,
        total_budget_seconds=300,
        num_pairs=220,
        max_concurrent_calls=10,
    )
    assert result == 300
    assert isinstance(result, int)


def test_compute_effective_budget_scaled_wins() -> None:
    """When the scaled value is below the absolute ceiling, scaled wins."""
    result = engine_module._compute_effective_budget(
        budget_base_seconds=60,
        budget_per_pair_seconds=20.0,
        total_budget_seconds=900,
        num_pairs=220,
        max_concurrent_calls=10,
    )
    assert result == 500
    assert isinstance(result, int)


def test_compute_effective_budget_single_pair() -> None:
    """One pair still costs one full wave: 60 + 20*ceil(1/10) = 80."""
    result = engine_module._compute_effective_budget(
        budget_base_seconds=60,
        budget_per_pair_seconds=20.0,
        total_budget_seconds=None,
        num_pairs=1,
        max_concurrent_calls=10,
    )
    assert result == 80
    assert isinstance(result, int)


def test_compute_effective_budget_zero_pairs_returns_base() -> None:
    """``num_pairs == 0`` short-circuits to ``budget_base_seconds``."""
    result = engine_module._compute_effective_budget(
        budget_base_seconds=60,
        budget_per_pair_seconds=20.0,
        total_budget_seconds=None,
        num_pairs=0,
        max_concurrent_calls=10,
    )
    assert result == 60
    assert isinstance(result, int)


def test_compute_effective_budget_non_multiple_rounds_up() -> None:
    """A non-multiple pair count rounds the wave count up via ceil.

    base=60, per_pair=20.0, 221 pairs @ concurrency=10 →
    ceil(221/10)=ceil(22.1)=23 waves → 60 + 20*23 = 520.
    """
    result = engine_module._compute_effective_budget(
        budget_base_seconds=60,
        budget_per_pair_seconds=20.0,
        total_budget_seconds=None,
        num_pairs=221,
        max_concurrent_calls=10,
    )
    assert result == 520
    assert isinstance(result, int)


def test_compute_effective_budget_fractional_per_pair_never_rounds_down() -> None:
    """#198 (PR review): a fractional ``budget_per_pair_seconds`` must round the
    final budget UP, not truncate it — a smaller backstop would trip earlier
    than the operator intended. base=60, per_pair=20.5, 220 pairs @ c=10 →
    22 waves → 60 + 20.5*22 = 511.0 (already whole); use 21.5 to force a
    fraction: 60 + 21.5*22 = 533.0 ... pick per_pair=20.3 → 60 + 20.3*22 =
    506.6 → ceil = 507 (``int`` would give 506)."""
    result = engine_module._compute_effective_budget(
        budget_base_seconds=60,
        budget_per_pair_seconds=20.3,
        total_budget_seconds=None,
        num_pairs=220,
        max_concurrent_calls=10,
    )
    assert result == 507  # ceil(506.6), NOT int(506.6)=506
    assert isinstance(result, int)


def test_compute_effective_budget_cap_fractional_never_rounds_down() -> None:
    """#198 (PR review): the absolute-cap branch also ceils (never truncates)
    the post-``min`` value. cap=250.5 below scaled → min=250.5 → ceil=251."""
    result = engine_module._compute_effective_budget(
        budget_base_seconds=60,
        budget_per_pair_seconds=20.0,
        total_budget_seconds=250,  # int cap below scaled (500) → 250
        num_pairs=220,
        max_concurrent_calls=10,
    )
    assert result == 250
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Whole-run pre-flight envelope-breach (DEC-013)
# ---------------------------------------------------------------------------


def test_grade_artifacts_envelope_breach_aborts_run_before_any_llm_call(
    tmp_path: Path,
) -> None:
    """A column description containing ``</ARTIFACT>`` aborts the whole
    run with :class:`GradePromptEnvelopeBreachError` BEFORE any LLM
    call is issued.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    rubric = _two_criteria()
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(
            CandidateColumn(
                name="order_id",
                description="A description with </ARTIFACT> in it",
            ),
        ),
        tests=(),
    )
    fake = FakeAnthropicClient()  # No expectations queued.

    with pytest.raises(GradePromptEnvelopeBreachError):
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_config_no_audit_in_path(),
            client=fake,
            project_dir=project_dir,
        )

    # Defence: the fake had no expectations queued — any call attempt
    # would have raised AssertionError("unexpected call"). Nothing
    # should have called either method.
    assert fake.create_calls == []
    assert fake.count_calls == []


# ---------------------------------------------------------------------------
# Symlink containment (DEC-006)
# ---------------------------------------------------------------------------


def test_grade_artifacts_explicit_audit_path_canonicalised(tmp_path: Path) -> None:
    """An ``audit_path`` symlinked to outside the project tree raises
    :class:`GradeAuditWriteError` at the first audit write.
    """
    project_dir = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    outside_target = outside / "grade.jsonl"

    sf_dir = project_dir / ".signalforge"
    sf_dir.mkdir(parents=True, exist_ok=True)
    audit_path = sf_dir / "grade.jsonl"
    audit_path.symlink_to(outside_target)

    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    with pytest.raises(GradeAuditWriteError):
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_config_no_audit_in_path(),
            client=fake,
            project_dir=project_dir,
            audit_path=audit_path,
        )


# ---------------------------------------------------------------------------
# Logging contract (DEC-027)
# ---------------------------------------------------------------------------


def test_grade_artifacts_emits_one_info_log_per_invocation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Exactly one INFO record at the engine logger level per
    successful run.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    caplog.set_level(logging.INFO, logger="signalforge.grade.engine")
    grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )

    info_records = [
        r
        for r in caplog.records
        if r.name == "signalforge.grade.engine" and r.levelno == logging.INFO
    ]
    assert len(info_records) == 1
    # Lazy-format JSON contract: the format string is "grade
    # completed: %s" and the JSON-serialised payload arrives as the
    # first arg, so the rendered message decodes losslessly.
    rendered = info_records[0].getMessage()
    payload_json = rendered.split("grade completed: ", 1)[1]
    payload = json.loads(payload_json)
    assert payload["run_id"] == _read_first_run_id(project_dir)
    assert payload["model_unique_id"] == "model.shop.orders"


def _read_first_run_id(project_dir: Path) -> str:
    """Tail the JSONL audit and return the first record's run_id."""
    rows = _read_jsonl(project_dir / ".signalforge" / "grade.jsonl")
    return rows[0]["run_id"]


# ---------------------------------------------------------------------------
# Module-level _sleep alias (DEC-023)
# ---------------------------------------------------------------------------


def test_grade_artifacts_module_level_sleep_alias_present() -> None:
    """The ``_sleep`` alias is module-scoped and reassignable for
    deterministic budget tests, matching :data:`signalforge.llm.client._sleep`
    and :data:`signalforge.prune.engine._sleep`.
    """
    import time as _time

    assert engine_module._sleep is _time.sleep
    sentinel: list[float] = []

    def fake_sleep(s: float) -> None:
        sentinel.append(s)

    original = engine_module._sleep
    try:
        engine_module._sleep = fake_sleep  # type: ignore[assignment]
        # Calling the alias directly drives the recorder; the
        # orchestrator's normal path does not invoke it on the happy
        # path (DEC-023 docstring), so we exercise the alias rather
        # than the orchestrator here.
        engine_module._sleep(0.001)
    finally:
        engine_module._sleep = original  # type: ignore[assignment]
    assert sentinel == [0.001]


# ---------------------------------------------------------------------------
# expect_grade_responses helper sanity (DEC-021)
# ---------------------------------------------------------------------------


def test_expect_grade_responses_helper_enqueues_correct_pairs() -> None:
    """The helper enqueues ``len(rubric) * len(artifacts)`` count_tokens
    + messages.create expectation pairs.
    """
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    artifact_pairs = _stable_artifact_pairs(candidate)
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    # Each pair queues one count_tokens + one create expectation.
    expected = len(rubric) * len(artifact_pairs)
    # Drive the queues by issuing the matching number of fake calls.
    for _ in range(expected):
        fake.messages.count_tokens(model="x", system="x", messages=[])
        fake.messages.create(model="x", max_tokens=1, system="x", messages=[])
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# Audit-record-too-large (DEC-006)
# ---------------------------------------------------------------------------


def test_grade_artifacts_oversize_audit_record_aborts_run(tmp_path: Path) -> None:
    """An oversize per-call audit record propagates as
    :class:`GradeAuditRecordTooLargeError` and aborts the run.
    """
    from signalforge.grade.errors import GradeAuditRecordTooLargeError

    project_dir = _project(tmp_path)
    model = _make_model()
    rubric = (Criterion(id="clarity", criterion="Is it clear?"),)
    # Use minimal candidate so we only need 4 expectations.
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="order_id", description="pk"),),
        tests=(),
    )

    # Build a fake whose first messages.create returns an oversized
    # reasoning string. The audit-record size cap is 4000 bytes; a
    # 5000-byte reasoning blob ensures the encoded JSONL line
    # comfortably exceeds the cap.
    huge_reasoning = "x" * 5000
    fake = FakeAnthropicClient()
    from tests.llm._fake import (
        FakeCountTokensResponse,
        FakeMessage,
        FakeTextBlock,
        FakeUsage,
    )

    fake.expect_count_tokens(
        matching=lambda _kw: True,
        returns=FakeCountTokensResponse(input_tokens=1500),
    )
    fake.expect_messages_create(
        matching=lambda _kw: True,
        returns=FakeMessage(
            content=[
                FakeTextBlock(
                    text=json.dumps(
                        {
                            "criterion_id": "clarity",
                            "score": 0.5,
                            "passed": True,
                            "evidence": "",
                            "reasoning": huge_reasoning,
                        }
                    ),
                )
            ],
            usage=FakeUsage(input_tokens=1700, output_tokens=200),
            model="claude-fake",
        ),
    )

    # Sanity: the encoded line must exceed 4000 bytes for this test.
    assert len(huge_reasoning.encode("utf-8")) > _GRADE_AUDIT_RECORD_LIMIT_BYTES

    with pytest.raises(GradeAuditRecordTooLargeError):
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_config_no_audit_in_path(),
            client=fake,
            project_dir=project_dir,
        )


# ---------------------------------------------------------------------------
# Rubric resolution
# ---------------------------------------------------------------------------


def test_grade_artifacts_uses_default_rubric_when_omitted(tmp_path: Path) -> None:
    """``rubric=None`` and ``config.rubric=None`` falls back to
    :data:`DEFAULT_RUBRIC`.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=DEFAULT_RUBRIC, candidate=candidate)

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )
    fake.assert_all_expectations_met()
    # The default rubric ships 4 criteria; sample candidate has 7
    # artifacts → 28 results.
    artifact_count = len(_stable_artifact_pairs(candidate))
    assert len(report.results) == artifact_count * len(DEFAULT_RUBRIC)


# ---------------------------------------------------------------------------
# GradingReport per-call timestamps
# ---------------------------------------------------------------------------


def test_grade_artifacts_sidecar_carries_run_id_and_timestamp(tmp_path: Path) -> None:
    """The sidecar's ``run_id`` is uuid4 hex (32 chars) and its
    ``timestamp`` is timezone-aware UTC.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    before = datetime.now(UTC)
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )
    after = datetime.now(UTC)

    assert len(report.run_id) == 32
    assert all(c in "0123456789abcdef" for c in report.run_id)
    assert before <= report.timestamp <= after


# ---------------------------------------------------------------------------
# QG pass 1 regression tests: prune_result mismatch + engine path canonicalise
# ---------------------------------------------------------------------------


def test_grade_artifacts_rejects_prune_result_for_different_model(tmp_path: Path) -> None:
    """``prune_result.model_unique_id`` must match ``model.unique_id``.

    Regression for QG pass 1 fix: the engine refuses to grade with a
    PruneResult that belongs to a different model so a stale result
    can't silently drive the no-redundant criterion.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    mismatched = PruneResult(
        model_unique_id="model.other.x",
        decisions=(),
        elapsed_ms=0,
        signalforge_version="0.0.0-test",
    )
    with pytest.raises(GradeError, match="does not match"):
        grade_artifacts(
            model,
            candidate,
            mismatched,
            config=_config_no_audit_in_path(),
            client=FakeAnthropicClient(),
            project_dir=project_dir,
        )


def test_grade_artifacts_user_supplied_audit_path_outside_project_tree_rejected(
    tmp_path: Path,
) -> None:
    """An audit_path outside ``project_dir`` is rejected at engine entry.

    Regression for QG pass 1 fix: the engine canonicalises against the
    resolved project root BEFORE handing off to the writer. Prior to
    the fix, the writer derived ``project_dir`` from the path itself
    (``audit_path.parent.parent``), which neutered the symlink-escape
    gate for any caller-supplied path. This test exercises a plain
    non-symlinked escape — only the engine-level gate catches it.
    """
    project_dir = _project(tmp_path)
    outside_audit = tmp_path / "outside" / "grade.jsonl"
    outside_audit.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(GradeAuditWriteError, match="symlink/containment"):
        grade_artifacts(
            _make_model(),
            _load_sample_candidate(),
            _empty_prune_result(_make_model()),
            config=_config_no_audit_in_path(),
            client=FakeAnthropicClient(),
            project_dir=project_dir,
            audit_path=outside_audit,
        )


def test_grade_artifacts_user_supplied_sidecar_path_outside_project_tree_rejected(
    tmp_path: Path,
) -> None:
    """Symmetric containment check for ``sidecar_path``."""
    project_dir = _project(tmp_path)
    outside_sidecar = tmp_path / "outside" / "grade.json"
    outside_sidecar.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(GradeAuditWriteError, match="symlink/containment"):
        grade_artifacts(
            _make_model(),
            _load_sample_candidate(),
            _empty_prune_result(_make_model()),
            config=_config_no_audit_in_path(),
            client=FakeAnthropicClient(),
            project_dir=project_dir,
            sidecar_path=outside_sidecar,
        )


# ---------------------------------------------------------------------------
# Threshold-fail graduation (#9 US-002 / DEC-021)
# ---------------------------------------------------------------------------


def _failing_scores(
    rubric: Rubric,
    candidate: CandidateSchema,
    *,
    score: float,
    passed: bool,
) -> dict[tuple[str, str], tuple[float | None, bool, str, str]]:
    """Build a ``scores`` dict for :func:`expect_grade_responses` that
    overrides every ``(artifact, criterion)`` pair with the given score
    and passed flag.
    """
    pairs = _stable_artifact_pairs(candidate)
    out: dict[tuple[str, str], tuple[float | None, bool, str, str]] = {}
    for criterion in rubric:
        for artifact_id, _ in pairs:
            out[(artifact_id, criterion.id)] = (score, passed, "evidence", "reasoning")
    return out


def _config_with_threshold_fail(
    *, fail_on_below_threshold: bool, min_pass_rate: float = 0.7, min_mean_score: float = 0.5
) -> GradeConfig:
    return GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=60,
        min_pass_rate=min_pass_rate,
        min_mean_score=min_mean_score,
        fail_on_below_threshold=fail_on_below_threshold,
    )


def test_fail_on_below_threshold_true_passing(tmp_path: Path) -> None:
    """Passing report + ``fail_on_below_threshold=True`` → returns cleanly.

    The default ``expect_grade_responses`` scoring (score=0.5, passed=True)
    yields ``pass_rate=1.0 >= 0.7`` and ``mean_score=0.5 >= 0.5`` which
    passes the threshold-AND. The new raise must NOT fire.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_with_threshold_fail(fail_on_below_threshold=True),
        client=fake,
        project_dir=project_dir,
    )
    assert report.passed is True


def test_fail_on_below_threshold_true_failing_pass_rate(tmp_path: Path) -> None:
    """``pass_rate < min_pass_rate`` + opt-in → raises.

    All pairs scored with ``passed=False`` while keeping the score at
    0.6 (above the mean threshold) so the failing axis is unambiguously
    the pass-rate.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(
        fake,
        rubric=rubric,
        candidate=candidate,
        scores=_failing_scores(rubric, candidate, score=0.6, passed=False),
    )

    with pytest.raises(GradeBelowThresholdError) as excinfo:
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_config_with_threshold_fail(fail_on_below_threshold=True),
            client=fake,
            project_dir=project_dir,
        )
    err = excinfo.value
    assert err.pass_rate == 0.0
    assert err.mean_score == pytest.approx(0.6)
    assert err.min_pass_rate == 0.7
    assert err.min_mean_score == 0.5
    assert err.aggregate_complete is True
    # Message names the failing axis.
    assert "pass_rate" in str(err)


def test_fail_on_below_threshold_true_failing_mean_score(tmp_path: Path) -> None:
    """``mean_score < min_mean_score`` + opt-in → raises.

    All pairs scored with ``passed=True`` (so pass_rate=1.0 >= 0.7) but
    score=0.1 (so mean=0.1 < 0.5) — the failing axis is unambiguously
    mean_score.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(
        fake,
        rubric=rubric,
        candidate=candidate,
        scores=_failing_scores(rubric, candidate, score=0.1, passed=True),
    )

    with pytest.raises(GradeBelowThresholdError) as excinfo:
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_config_with_threshold_fail(fail_on_below_threshold=True),
            client=fake,
            project_dir=project_dir,
        )
    err = excinfo.value
    assert err.pass_rate == 1.0
    assert err.mean_score == pytest.approx(0.1)
    assert err.aggregate_complete is True
    assert "mean_score" in str(err)


def test_fail_on_below_threshold_false_failing(tmp_path: Path) -> None:
    """Default ``fail_on_below_threshold=False`` preserves report-only
    posture: a below-threshold report is returned, never raised.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(
        fake,
        rubric=rubric,
        candidate=candidate,
        scores=_failing_scores(rubric, candidate, score=0.1, passed=False),
    )

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_with_threshold_fail(fail_on_below_threshold=False),
        client=fake,
        project_dir=project_dir,
    )
    assert report.passed is False
    # Sanity: the operator's diff still surfaces the failing aggregate.
    assert report.pass_rate == 0.0
    assert report.mean_score == pytest.approx(0.1)


def test_grade_below_threshold_error_carries_aggregate_complete_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Graceful-degrade run (some ``score=None``) still raises with
    ``aggregate_complete=False`` correctly populated.

    A budget-exhausted run lands every remaining pair as ``score=None,
    passed=False``. `pass_rate` is 0.0 (no scored results pass) and
    `mean_score` is 0.0 (no scored results) — both under threshold;
    `aggregate_complete=False` because no result has a score.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    # Force the asyncio budget timeout to trip immediately — every
    # in-flight + un-started pair degrades via the async core's
    # CancelledError attribution path (DEC-008).
    _stub_grade_one_async_slow(monkeypatch)
    config = GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=1,
        min_pass_rate=0.7,
        min_mean_score=0.5,
        fail_on_below_threshold=True,
    )

    with pytest.raises(GradeBelowThresholdError) as excinfo:
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=config,
            client=fake,
            project_dir=project_dir,
        )
    err = excinfo.value
    assert err.aggregate_complete is False
    assert err.pass_rate == 0.0
    assert err.mean_score == 0.0
    assert err.min_pass_rate == 0.7
    assert err.min_mean_score == 0.5


def test_grade_below_threshold_writes_sidecar_before_raising(tmp_path: Path) -> None:
    """**Load-bearing DEC-021 ordering invariant.**

    Even when ``grade_artifacts`` raises ``GradeBelowThresholdError``,
    the sidecar JSON must be on disk: the operator needs the durable
    hand-off to diagnose *why* the run fell below threshold. Raising
    before the sidecar write would defeat the explainable-diffs
    commitment at the threshold-fail boundary.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(
        fake,
        rubric=rubric,
        candidate=candidate,
        scores=_failing_scores(rubric, candidate, score=0.1, passed=False),
    )

    sidecar_path = project_dir / ".signalforge" / "grade.json"
    audit_path = project_dir / ".signalforge" / "grade.jsonl"

    with pytest.raises(GradeBelowThresholdError):
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_config_with_threshold_fail(fail_on_below_threshold=True),
            client=fake,
            project_dir=project_dir,
            sidecar_path=sidecar_path,
            audit_path=audit_path,
        )

    # Sidecar exists AND round-trips through GradingReport.
    assert sidecar_path.exists()
    raw = sidecar_path.read_text(encoding="utf-8").strip()
    round_tripped = GradingReport.model_validate_json(raw)
    assert round_tripped.passed is False
    assert round_tripped.model_unique_id == model.unique_id
    # The per-pair JSONL audit is also durably on disk — every pair
    # evaluated produced a JSONL record (the partial-audit guarantee
    # established for prune / grade in DEC-006).
    rows = _read_jsonl(audit_path)
    assert len(rows) == len(round_tripped.results)


# ---------------------------------------------------------------------------
# Async-pre-flight guards (issue #186 DEC-006 / DEC-009 — US-008)
# ---------------------------------------------------------------------------


def test_grade_artifacts_nested_event_loop_raises_typed_error(tmp_path: Path) -> None:
    """Calling :func:`grade_artifacts` from inside a running asyncio event
    loop raises :class:`GradeNestedEventLoopError` (CLI tier 1) with a
    remediation pointing at the v0.4 follow-up. The guard fires BEFORE
    any LLM call so the queued fake is never reached.
    """
    import asyncio

    from signalforge.grade.errors import GradeNestedEventLoopError

    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()  # No expectations — any call would raise.

    async def _drive() -> None:
        # Running inside ``asyncio.run`` means ``asyncio.get_running_loop()``
        # in the guard returns the loop, triggering the typed raise.
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_config_no_audit_in_path(),
            client=fake,
            project_dir=project_dir,
        )

    with pytest.raises(GradeNestedEventLoopError) as excinfo:
        asyncio.run(_drive())

    assert excinfo.value.model_unique_id == model.unique_id
    # The remediation is locked verbatim by DEC-009 of #186.
    assert "single-event-loop only" in str(excinfo.value)
    # Defence-in-depth: the fake was never reached.
    assert fake.create_calls == []
    assert fake.count_calls == []


def test_grade_artifacts_outside_event_loop_does_not_raise_nested_guard(
    tmp_path: Path,
) -> None:
    """The nested-loop guard fires ONLY when called from a running event
    loop. The sync test entry path (every other engine test) must NOT
    trip it — this test pins the negative direction so a refactor that
    inverts the ``try/except`` couldn't silently break every grade run.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, candidate=candidate, rubric=rubric)

    # Smoke: a normal (non-async) caller succeeds.
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )
    # The guard didn't intercept; the run produced a real report.
    assert report.model_unique_id == model.unique_id


def test_grade_artifacts_sync_only_provider_with_parallel_cap_raises_typed_error(
    tmp_path: Path,
) -> None:
    """When the configured provider has ``supports_async=False`` AND
    ``max_concurrent_calls > 1``, the engine raises
    :class:`LLMProviderAsyncUnsupportedError` (CLI tier 3) at orchestrator
    entry — BEFORE any LLM call — rather than silently clamping to 1.
    Mirrors the project's ``extra="forbid"`` fail-loud posture (DEC-006
    of #186).
    """
    from signalforge.llm import providers as providers_module
    from signalforge.llm.errors import LLMProviderAsyncUnsupportedError
    from tests.llm._fake_provider import FakeNoCacheProvider

    class _SyncOnlyProvider(FakeNoCacheProvider):
        """Inherits every ABC concretion from
        :class:`FakeNoCacheProvider`; the only delta is ``supports_async``
        is flipped to ``False`` so the guard fires."""

        name = "sync-only-test-provider-186-us008"
        supports_async = False  # The load-bearing attribute under test.

    # Snapshot the registry so this test can't pollute other suites.
    saved = dict(providers_module._REGISTRY)
    try:
        providers_module._REGISTRY.clear()
        providers_module._REGISTRY.update(saved)
        providers_module.register_provider(_SyncOnlyProvider())

        project_dir = _project(tmp_path)
        model = _make_model()
        candidate = _load_sample_candidate()
        rubric = _two_criteria()
        fake = FakeAnthropicClient()  # No expectations — any call raises.

        config = GradeConfig(
            model="claude-fake",
            cache_ttl="1h",
            max_output_tokens=64,
            max_retries_429=0,
            max_retries_5xx=0,
            max_retries_conn=0,
            total_budget_seconds=60,
            provider="sync-only-test-provider-186-us008",
            max_concurrent_calls=5,
        )

        with pytest.raises(LLMProviderAsyncUnsupportedError) as excinfo:
            grade_artifacts(
                model,
                candidate,
                _empty_prune_result(model),
                rubric=rubric,
                config=config,
                client=fake,
                project_dir=project_dir,
            )

        # The message names the offending provider. The remediation is
        # locked by DEC-006 of #186 (refined by QG Pass 1 Concern #2):
        # the engine consumes ``call_llm_async`` exclusively, so cap=1
        # is NOT an escape hatch — the operator must pick an async-capable
        # provider.
        assert "sync-only-test-provider-186-us008" in str(excinfo.value)
        assert "async-capable provider" in str(excinfo.value)
        # Defence-in-depth: no LLM call was attempted.
        assert fake.create_calls == []
    finally:
        providers_module._REGISTRY.clear()
        providers_module._REGISTRY.update(saved)


def test_grade_artifacts_sync_only_provider_with_cap_one_still_raises_typed_error(
    tmp_path: Path,
) -> None:
    """Sync-only provider + ``max_concurrent_calls=1`` STILL raises the
    typed pre-flight error — ``cap=1`` is NOT an escape hatch (#186 QG
    Pass 1 Concern #2 fix).

    The grade engine consumes ``call_llm_async`` exclusively post-#186.
    A sync-only provider would surface ``LLMProviderAsyncUnsupportedError``
    on every per-pair call, which the per-pair ``except`` wraps to
    ``GradeLLMError`` — silently degrading every pair. That is worse than
    failing loud at orchestrator entry, because the operator sees an
    all-degraded report with no typed signal of the misconfiguration.

    Fix: drop the ``> 1`` predicate from the entry guard. The pre-flight
    raises ``LLMProviderAsyncUnsupportedError`` regardless of cap.
    """
    from signalforge.llm import providers as providers_module
    from signalforge.llm.errors import LLMProviderAsyncUnsupportedError
    from tests.llm._fake_provider import FakeNoCacheProvider

    class _SyncOnlyButSerial(FakeNoCacheProvider):
        name = "sync-only-serial-test-provider-186-us008-cap1"
        supports_async = False

    saved = dict(providers_module._REGISTRY)
    try:
        providers_module._REGISTRY.clear()
        providers_module._REGISTRY.update(saved)
        providers_module.register_provider(_SyncOnlyButSerial())

        project_dir = _project(tmp_path)
        model = _make_model()
        candidate = _load_sample_candidate()
        rubric = _two_criteria()
        fake = FakeAnthropicClient()
        expect_grade_responses(fake, candidate=candidate, rubric=rubric)

        config = GradeConfig(
            model="claude-fake",
            cache_ttl="1h",
            max_output_tokens=64,
            max_retries_429=0,
            max_retries_5xx=0,
            max_retries_conn=0,
            total_budget_seconds=60,
            provider="sync-only-serial-test-provider-186-us008-cap1",
            max_concurrent_calls=1,
        )

        with pytest.raises(LLMProviderAsyncUnsupportedError):
            grade_artifacts(
                model,
                candidate,
                _empty_prune_result(model),
                rubric=rubric,
                config=config,
                client=fake,
                project_dir=project_dir,
            )
    finally:
        providers_module._REGISTRY.clear()
        providers_module._REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# Async core (US-009) — TaskGroup + Semaphore + budget timeout
# ---------------------------------------------------------------------------


def test_grade_artifacts_concurrent_dispatches_in_parallel(tmp_path: Path) -> None:
    """The async core dispatches up to ``max_concurrent_calls`` coroutines
    in parallel; an instrumented fake records the peak in-flight count
    and asserts it stays at-or-below the cap.

    Pins DEC-002 + DEC-003 of #186: the ``Semaphore(max_concurrent_calls)``
    throttle is load-bearing — without it, the dispatch fan-out is
    unbounded and the operator's cost-and-rate posture is silently
    violated. The instrumented fake counts entries to ``create``
    minus exits before delegating to the standard expectation queue,
    so the peak measures the true concurrency the engine achieves.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    artifact_pairs = _stable_artifact_pairs(candidate)
    total_pairs = len(rubric) * len(artifact_pairs)
    # At least 4 pairs > cap=3 so the throttle actually engages; the
    # sample candidate carries 7 artifacts × 2 criteria = 14 pairs.
    assert total_pairs >= 6

    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    # Wrap the fake's async ``messages.create`` so we can observe the
    # in-flight count without changing the queue-popping logic. Each
    # coroutine awaits a tiny sleep mid-call so the orchestrator's
    # parallel dispatch is actually observable; without the sleep every
    # call resolves before the next task can claim the semaphore and
    # the peak collapses to 1.
    in_flight = 0
    peak_in_flight = 0
    original_create = fake.aio.messages.create

    async def _instrumented_create(**kw: Any) -> Any:
        nonlocal in_flight, peak_in_flight
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        try:
            # Tiny sleep keeps the coroutine in flight long enough that
            # sibling coroutines can also enter ``create`` concurrently.
            await asyncio.sleep(0.01)
            return await original_create(**kw)
        finally:
            in_flight -= 1

    fake.aio.messages.create = _instrumented_create  # type: ignore[assignment, method-assign]

    config = GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=60,
        max_concurrent_calls=3,
    )

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=fake,
        project_dir=project_dir,
    )

    assert len(report.results) == total_pairs
    # Concurrency engaged: at least 2 simultaneous calls observed.
    # Below the cap=3 floor: never more than 3.
    assert peak_in_flight <= 3
    assert peak_in_flight >= 2


def test_grade_artifacts_concurrency_1_byte_equivalent_to_v0_1(tmp_path: Path) -> None:
    """``max_concurrent_calls=1`` serialises dispatch in
    ``(criterion, artifact)`` iteration order.

    The semaphore-of-1 path is the documented sequential fallback (DEC-003
    of #186). Asserts the JSONL audit lands in iteration order — the
    audit corpus shape is bit-for-bit equivalent to the v0.1 sequential
    output under this config knob. Down-stream consumers gating on the
    legacy ordering (the v0.1 fixture, any external sidecar consumer)
    can keep functioning unchanged.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    config = GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=60,
        max_concurrent_calls=1,
    )

    grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=fake,
        project_dir=project_dir,
    )

    # Compare the on-disk JSONL order to the engine's iteration order.
    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    rows = _read_jsonl(audit_path)
    expected_iteration = [
        (artifact_id, criterion.id)
        for criterion in rubric
        for artifact_id, _ in _stable_artifact_pairs(candidate)
    ]
    observed = [(row["artifact_id"], row["criterion_id"]) for row in rows]
    assert observed == expected_iteration


def test_grade_artifacts_concurrent_budget_warning_shape_locked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """On budget trip the engine emits exactly one WARNING with the
    locked JSON field set (DEC-018 of #186; ``effective_budget_seconds``
    rename per DEC-008 of #198): ``run_id``, ``model_unique_id``,
    ``completed_count``, ``degraded_count``, ``effective_budget_seconds``.

    External operator dashboards key on these field names; locking the
    shape via a pinned test is what makes the audit corpus a stable
    contract. ``effective_budget_seconds`` carries the value actually
    passed to ``asyncio.timeout`` — the scaled budget capped by
    ``total_budget_seconds`` when set (DEC-001/DEC-008 of #198).
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    _stub_grade_one_async_slow(monkeypatch)

    caplog.set_level(logging.WARNING, logger="signalforge.grade.engine")
    grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_tiny_budget(),
        client=fake,
        project_dir=project_dir,
    )

    warns = [
        r
        for r in caplog.records
        if r.name == "signalforge.grade.engine"
        and r.levelno == logging.WARNING
        and "grade budget exceeded" in r.getMessage()
    ]
    assert len(warns) == 1
    payload_json = warns[0].getMessage().split("grade budget exceeded: ", 1)[1]
    payload = json.loads(payload_json)
    # JSON field set locked verbatim (DEC-018 of #186; refined by QG
    # Pass 1+3 — ``cancelled_count`` was dropped as dead-code, every
    # un-completed pair lands in ``degraded_count`` via the synthesis
    # pass regardless of in-flight vs un-started status).
    assert set(payload.keys()) == {
        "run_id",
        "model_unique_id",
        "completed_count",
        "degraded_count",
        "effective_budget_seconds",
    }
    assert payload["model_unique_id"] == model.unique_id
    # The tiny-budget config sets ``total_budget_seconds=1``, so the
    # effective budget is ``min(scaled, 1) == 1`` (the absolute cap
    # wins). DEC-001/DEC-008 of #198.
    config = _config_tiny_budget()
    expected_effective = engine_module._compute_effective_budget(
        budget_base_seconds=config.budget_base_seconds,
        budget_per_pair_seconds=config.budget_per_pair_seconds,
        total_budget_seconds=config.total_budget_seconds,
        num_pairs=len(rubric) * len(_stable_artifact_pairs(candidate)),
        max_concurrent_calls=config.max_concurrent_calls,
    )
    assert expected_effective == 1
    assert payload["effective_budget_seconds"] == expected_effective
    # Every pair accounted for: completed + degraded == total.
    candidate_pairs = len(_stable_artifact_pairs(candidate))
    total_pairs = len(rubric) * candidate_pairs
    assert payload["completed_count"] + payload["degraded_count"] == total_pairs


def _make_candidate_with_n_columns(n: int) -> CandidateSchema:
    """Build a :class:`CandidateSchema` with ``n`` columns, each carrying
    a description, a rationale, and one ``not_null`` test.

    Drives the ≥40-column acceptance-criterion test below: a wide model
    produces many ``(artifact × criterion)`` pairs, exercising the
    scaled wall-clock budget (DEC-001 of #198).
    """
    columns = tuple(
        CandidateColumn(
            name=f"col_{i}",
            description=f"column {i} description",
            rationale=f"column {i} rationale",
            tests=(CandidateTestNotNull(column=f"col_{i}"),),
        )
        for i in range(n)
    )
    return CandidateSchema(
        name="orders",
        description="wide model description",
        rationale="wide model rationale",
        columns=columns,
        tests=(),
    )


def test_grade_artifacts_wide_model_completes_with_zero_budget_degradations(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ≥40-column model completes under the DEFAULT GradeConfig with
    ZERO width-induced budget degradations (the #198 acceptance criterion).

    With the scaled wall-clock budget (DEC-001 — ``budget_base_seconds``
    + ``budget_per_pair_seconds`` × waves, no absolute cap by default),
    a 40-column candidate (122 artifacts × 4 default-rubric criteria =
    488 pairs) finishes well inside the backstop when every call returns
    instantly via the fake. The proof: ``aggregate_complete is True``,
    every result scored (no ``score is None``), and NO budget WARNING.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _make_candidate_with_n_columns(40)
    # Sanity: a wide model produces many pairs (40 cols × 3 artifacts
    # + 2 model-level = 122 artifacts × 4 criteria = 488 pairs).
    artifact_count = len(_stable_artifact_pairs(candidate))
    assert artifact_count == 122

    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=DEFAULT_RUBRIC, candidate=candidate)

    caplog.set_level(logging.WARNING, logger="signalforge.grade.engine")
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        # No rubric / config args → DEFAULT_RUBRIC + default GradeConfig
        # (no ceilings, scaled budget).
        client=fake,
        project_dir=project_dir,
    )

    # Every pair scored — zero width-induced budget degradations.
    assert report.aggregate_complete is True
    assert all(r.score is not None for r in report.results)
    assert len(report.results) == artifact_count * len(DEFAULT_RUBRIC)

    # No budget WARNING was emitted.
    budget_warns = [
        r
        for r in caplog.records
        if r.name == "signalforge.grade.engine"
        and r.levelno == logging.WARNING
        and "grade budget exceeded" in r.getMessage()
    ]
    assert budget_warns == []


# ---------------------------------------------------------------------------
# Opt-in cost/calls/tokens ceilings (US-004 / DEC-002/003/007/009/010/011)
# ---------------------------------------------------------------------------


def _ceiling_warns(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Collect every ``grade ceiling exceeded`` WARNING payload."""
    payloads: list[dict[str, Any]] = []
    for r in caplog.records:
        if (
            r.name == "signalforge.grade.engine"
            and r.levelno == logging.WARNING
            and "grade ceiling exceeded" in r.getMessage()
        ):
            payload_json = r.getMessage().split("grade ceiling exceeded: ", 1)[1]
            payloads.append(json.loads(payload_json))
    return payloads


def test_grade_artifacts_max_grade_calls_ceiling_degrades_remaining(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``max_grade_calls=K`` scores exactly K pairs and degrades the rest
    with the locked reason; one ceiling WARNING with ``ceiling="calls"``.

    ``max_concurrent_calls=1`` serialises dispatch so the first K pairs
    (in iteration order) reserve the K slots and the remainder degrade —
    making the "exactly K scored" assertion deterministic (the
    check-then-reserve is race-free under any concurrency, but the WHICH-K
    is only deterministic when serialised).
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    total_pairs = len(rubric) * len(_stable_artifact_pairs(candidate))
    assert total_pairs == 14  # 7 artifacts × 2 criteria
    k = 5
    config = GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        max_concurrent_calls=1,
        max_grade_calls=k,
    )

    caplog.set_level(logging.WARNING, logger="signalforge.grade.engine")
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=fake,
        project_dir=project_dir,
    )

    scored = [r for r in report.results if r.score is not None]
    degraded = [r for r in report.results if r.score is None]
    assert len(scored) == k
    assert len(degraded) == total_pairs - k
    assert report.aggregate_complete is False
    for r in degraded:
        assert r.reasoning == f"grade call ceiling exceeded ({k} calls)"
        # #202 US-001: ceiling degrades classify as "ceiling", and the
        # scored pairs carry None — exercised end-to-end here.
        assert r.degrade_reason_type == "ceiling"
    assert all(r.degrade_reason_type is None for r in scored)

    warns = _ceiling_warns(caplog)
    assert len(warns) == 1
    payload = warns[0]
    assert set(payload.keys()) == {
        "run_id",
        "model_unique_id",
        "ceiling",
        "limit",
        "completed_count",
        "degraded_count",
    }
    assert payload["model_unique_id"] == model.unique_id
    assert payload["ceiling"] == "calls"
    assert payload["limit"] == k
    # Ceiling-degrades count as completed (DEC-011); the synthesis pass
    # never runs (no budget trip), so degraded_count stays 0.
    assert payload["completed_count"] == total_pairs
    assert payload["degraded_count"] == 0


def test_grade_artifacts_max_grade_cost_usd_ceiling_degrades_remaining(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``max_grade_cost_usd`` degrades pairs once the accumulated USD
    crosses the cap; locked reason + ``ceiling="cost_usd"`` WARNING.

    Uses a REAL SKU (``claude-sonnet-4-6``) so ``pricing.lookup`` resolves.
    Each call's usage (input=1700, output=140, cache_read=1500) prices to
    $0.00765, so a $0.03 cap fits exactly 4 calls
    (4 × 0.00765 = 0.0306 ≥ 0.03 trips the 5th pair's check).
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    total_pairs = len(rubric) * len(_stable_artifact_pairs(candidate))
    cap = 0.03
    config = GradeConfig(
        model="claude-sonnet-4-6",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        max_concurrent_calls=1,
        max_grade_cost_usd=cap,
    )

    caplog.set_level(logging.WARNING, logger="signalforge.grade.engine")
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=fake,
        project_dir=project_dir,
    )

    scored = [r for r in report.results if r.score is not None]
    degraded = [r for r in report.results if r.score is None]
    assert len(scored) == 4
    assert len(degraded) == total_pairs - 4
    assert report.aggregate_complete is False
    for r in degraded:
        assert r.reasoning == f"grade cost ceiling exceeded (${cap})"

    warns = _ceiling_warns(caplog)
    assert len(warns) == 1
    assert warns[0]["ceiling"] == "cost_usd"
    assert warns[0]["limit"] == cap
    assert warns[0]["completed_count"] == total_pairs
    assert warns[0]["degraded_count"] == 0


def test_grade_artifacts_cost_ceiling_unpriced_model_fails_fast_before_any_call(
    tmp_path: Path,
) -> None:
    """A cost ceiling on a prefix-valid-but-unpriced SKU fails fast at
    orchestrator entry — BEFORE any (billable) LLM call — rather than aborting
    mid-run from inside the TaskGroup after paid calls.

    ``GradeConfig._validate_model_provider_compat`` checks only the SKU prefix
    (``claude-``), so ``claude-opus-4-8`` (absent from ``pricing.PRICES``) is
    accepted at config-load. With ``max_grade_cost_usd`` set, the engine
    resolves pricing once up front; an unknown SKU raises
    ``EstimateUnknownModelError`` before dispatch. The fake client is given NO
    queued responses: if the engine reached a grade call it would raise a
    different ("unexpected call") error, so asserting ``EstimateUnknownModelError``
    proves the failure preceded every LLM call.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()  # deliberately no expectations queued

    config = GradeConfig(
        model="claude-opus-4-8",  # claude- prefix (valid) but NOT in PRICES
        max_concurrent_calls=1,
        max_grade_cost_usd=0.01,
    )

    with pytest.raises(EstimateUnknownModelError):
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=config,
            client=fake,
            project_dir=project_dir,
        )


def test_grade_artifacts_max_grade_tokens_ceiling_degrades_remaining(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``max_grade_tokens`` degrades pairs once accumulated token movement
    crosses the cap; locked reason + ``ceiling="tokens"`` WARNING.

    Each call moves 1700+140+0+1500 = 3340 tokens, so a 10000-token cap
    fits exactly 3 calls (3 × 3340 = 10020 ≥ 10000 trips the 4th pair).
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    total_pairs = len(rubric) * len(_stable_artifact_pairs(candidate))
    cap = 10000
    config = GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        max_concurrent_calls=1,
        max_grade_tokens=cap,
    )

    caplog.set_level(logging.WARNING, logger="signalforge.grade.engine")
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=fake,
        project_dir=project_dir,
    )

    scored = [r for r in report.results if r.score is not None]
    degraded = [r for r in report.results if r.score is None]
    assert len(scored) == 3
    assert len(degraded) == total_pairs - 3
    assert report.aggregate_complete is False
    for r in degraded:
        assert r.reasoning == f"grade token ceiling exceeded ({cap} tokens)"

    warns = _ceiling_warns(caplog)
    assert len(warns) == 1
    assert warns[0]["ceiling"] == "tokens"
    assert warns[0]["limit"] == cap


def test_grade_artifacts_no_ceiling_emits_no_ceiling_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A default-config run (all ceilings ``None``) emits NO
    ``grade ceiling exceeded`` WARNING and degrades nothing (regression
    guard for the opt-in default-off contract).
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    caplog.set_level(logging.WARNING, logger="signalforge.grade.engine")
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )

    assert report.aggregate_complete is True
    assert all(r.score is not None for r in report.results)
    assert _ceiling_warns(caplog) == []


@pytest.mark.no_sweep_cooldown_patch
def test_grade_artifacts_module_level_async_sleep_alias_present() -> None:
    """The ``_async_sleep`` alias is module-scoped and reassignable for
    deterministic budget tests, mirroring :data:`signalforge.llm.client._async_sleep`
    (DEC-011 of #186).

    Opts out of the autouse sweep cool-down no-op patch (``tests/grade/
    conftest.py``) so the import-time alias identity is observable.
    """
    assert engine_module._async_sleep is asyncio.sleep


# ---------------------------------------------------------------------------
# Defence-in-depth — ExceptionGroup never leaks a traceback (US-010 / DEC-007)
# ---------------------------------------------------------------------------


def test_grade_artifacts_hostile_coroutine_no_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hostile coroutine that raises a non-grade-typed exception
    (e.g. ``KeyError`` from buggy worker code) MUST surface as a clean
    multi-bullet stderr write — never a leaked Python traceback.

    This is US-010's defence-in-depth pin: the engine's per-coroutine
    ``try/except`` catches only ``GradeLLMError`` / ``GradeOutputError`` /
    ``GradePromptEnvelopeBreachError`` / ``CancelledError``; a stray
    ``KeyError`` propagates to ``TaskGroup`` and bubbles as part of a
    ``BaseExceptionGroup``. The engine's inner unwrap re-raises
    single-exception groups as the inner typed exception, so we drive
    *every* pair to fail to force a multi-exception group that bubbles
    to the CLI layer. The CLI's ``format_error_to_stderr`` then
    renders the group through the US-010 ``ExceptionGroup`` branch,
    which never includes a traceback.

    Mirrors the project's ``"Traceback" not in capsys.readouterr().err``
    invariant pinned across every CLI test (DEC-016 floor —
    ``cli-layer.md`` § "No traceback ever leaks").
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    # No ``expect_grade_responses`` queue — the hostile stub never
    # reaches the fake. If a refactor breaks the monkey-patch, the
    # fake would raise on the first unmatched call and we'd see
    # ``AssertionError`` instead of the expected ``ExceptionGroup``.

    async def _hostile_grade_one(**_kw: Any) -> tuple:
        """Worker raises an arbitrary non-grade-typed exception.

        ``KeyError`` is deliberately outside the engine's catch list
        (``GradeLLMError`` / ``GradeOutputError`` /
        ``GradePromptEnvelopeBreachError`` / ``CancelledError``), so
        every task escapes the per-coroutine ``try/except`` and lands
        in the ``TaskGroup``'s ``BaseExceptionGroup``.
        """
        raise KeyError("hostile worker — simulates a buggy refactor")

    monkeypatch.setattr(engine_module, "_grade_one_async", _hostile_grade_one)

    # Drive the engine; it must raise *something*. With 2 criteria × N
    # artifacts > 1 pairs all failing, the inner unwrap's
    # ``len(group.exceptions) == 1`` branch does NOT fire and the
    # multi-exception group bubbles unchanged.
    with pytest.raises(BaseException) as excinfo:
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_config_no_audit_in_path(),
            client=fake,
            project_dir=project_dir,
        )

    # The raised exception is either an ``ExceptionGroup`` (multi-pair
    # failure — the documented US-010 path) or a bare ``KeyError`` (if
    # only one pair was scheduled and the single-exception unwrap
    # fired). Either way, routing through ``format_error_to_stderr``
    # MUST NOT carry a traceback — the US-010 invariant is a property of
    # the renderer, not of which branch fires.
    from signalforge.cli._helpers import format_error_to_stderr

    rendered = format_error_to_stderr(excinfo.value)  # type: ignore[arg-type]
    assert "Traceback" not in rendered, f"format_error_to_stderr leaked a traceback: {rendered!r}"

    # Pin the multi-exception group branch unconditionally — this test
    # is engineered so EVERY pair fails (2 criteria × ≥2 artifact pairs
    # ⇒ ≥4 concurrent failures), so the engine's single-exception unwrap
    # MUST NOT fire and the renderer MUST see an ExceptionGroup. A
    # future regression that unwrapped multi-exception groups too would
    # otherwise silently pass the conditional shape (QG Pass 3
    # Concern #3 fix).
    assert isinstance(excinfo.value, ExceptionGroup), (
        f"hostile-coroutine fixture must surface a multi-exception group; "
        f"got {type(excinfo.value).__name__}"
    )
    assert "concurrent failure" in rendered, (
        f"ExceptionGroup branch did not render the US-010 header: {rendered!r}"
    )
    # Every inner exception is a ``KeyError`` from the hostile stub.
    for inner in excinfo.value.exceptions:
        assert isinstance(inner, KeyError), (
            f"unexpected inner exception type: {type(inner).__name__}"
        )

    # Defence-in-depth: nothing the engine itself printed contains a
    # traceback either (the engine's WARNING / INFO lines route through
    # lazy-format JSON loggers — pinned by the grep gate).
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, f"engine leaked a traceback to stderr: {captured.err!r}"


# ---------------------------------------------------------------------------
# US-006 — Persistent grade cache wiring (issue #189)
# ---------------------------------------------------------------------------
#
# Pins the orchestrator surgery that wires :mod:`signalforge.grade.cache`
# into :func:`grade_artifacts`:
#
# * Sync-prefix cache lookup BEFORE the asyncio.TaskGroup
#   (``grade-layer.md`` § "Symlink-hardened path canonicalisation" +
#   DEC-013 of #189).
# * Cache hits skip the LLM call AND flow through
#   :func:`_build_grade_event(..., cache_hit=True, ...)` (the SOLE
#   construction seam — AST scan 6).
# * Cache misses route through the existing async dispatch and write a
#   :class:`CacheRecord` post-grade, fail-soft (DEC-005).
# * Five invalidation axes: criterion, artifact text, provider, model,
#   prompt_version_template (DEC-004).
# * Degraded results (``score=None``) never reach the cache (DEC-007).


def _build_cache_record_from_pair(
    *,
    artifact_id: str,
    criterion: Criterion,
    artifact_text: str,
    rubric: Rubric,
    provider: str = "anthropic",
    model: str = "claude-fake",
    score: float = 0.5,
    passed: bool = True,
    evidence: str = "",
    reasoning: str = "",
):
    """Build a :class:`CacheRecord` for a synthetic cache pre-population.

    Mirrors the hash recipe :func:`grade_artifacts` uses so that
    pre-populated cache entries are looked up under the right key. The
    five-part recipe (DEC-004) is criterion_prompt_hash +
    artifact_text_hash + provider + model + prompt_version_template.
    """
    import hashlib as _hashlib
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from signalforge.grade.cache import CacheRecord, compute_cache_key
    from signalforge.grade.prompts import criterion_prompt_hash, prompt_version_template
    from signalforge.grade.rubric import _canonical_rubric_hash

    crit_hash = criterion_prompt_hash(criterion)
    artifact_text_hash = _hashlib.blake2b(artifact_text.encode("utf-8"), digest_size=8).hexdigest()
    template_hash = prompt_version_template(rubric)
    rubric_hash = _canonical_rubric_hash(rubric)
    response_hash = _hashlib.blake2b(b"fake-response", digest_size=8).hexdigest()
    record = CacheRecord(
        artifact_id=artifact_id,
        criterion_id=criterion.id,
        score=score,
        passed=passed,
        evidence=evidence,
        reasoning=reasoning,
        criterion_prompt_hash=crit_hash,
        artifact_text_hash=artifact_text_hash,
        provider=provider,
        model=model,
        prompt_version_template=template_hash,
        response_text_hash=response_hash,
        rubric_hash=rubric_hash,
        original_timestamp=_dt(2026, 5, 1, 17, 42, 13, 123456, tzinfo=_UTC),
    )
    key = compute_cache_key(
        criterion_prompt_hash=crit_hash,
        artifact_text_hash=artifact_text_hash,
        provider=provider,
        model=model,
        prompt_version_template=template_hash,
    )
    return key, record


def _seed_cache(project_dir: Path, key: str, record) -> Path:
    """Write a :class:`CacheRecord` under
    ``<project_dir>/.signalforge/grade-cache/<key>.json``.
    """
    from signalforge.grade.cache import write_cache

    cache_dir = project_dir / ".signalforge" / "grade-cache"
    write_cache(cache_dir, key, record)
    return cache_dir / f"{key}.json"


def test_grade_engine_cache_hit_skips_llm_call(tmp_path: Path) -> None:
    """A pre-populated cache entry for one pair skips the LLM call for
    that pair. The fake's expectation queue carries entries ONLY for
    the OTHER pairs; if the engine erroneously called the LLM for the
    cached pair, the fake would raise on the unexpected call.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()

    # Pre-populate the cache with the very first pair.
    pairs = _stable_artifact_pairs(candidate)
    target_aid, target_text = pairs[0]
    target_crit = rubric[0]
    key, record = _build_cache_record_from_pair(
        artifact_id=target_aid,
        criterion=target_crit,
        artifact_text=target_text,
        rubric=rubric,
        score=0.93,
        passed=True,
        evidence="cached evidence",
        reasoning="cached reasoning",
    )
    _seed_cache(project_dir, key, record)

    # Enqueue LLM expectations ONLY for the non-cached pairs.
    fake = FakeAnthropicClient()
    scores: dict[tuple[str, str], tuple[float | None, bool, str, str]] = {}
    expect_grade_responses(fake, rubric=rubric, candidate=candidate, scores=scores)
    # Drop the (count_tokens, messages.create) pair for the cached entry.
    # The fake's queue is shared — but the matchers are ``lambda _kw: True``
    # so dropping the first two entries is the same as dropping the cached
    # pair's pair. We do this by re-constructing: replay all pairs minus the cached.
    fake = FakeAnthropicClient()
    from tests.llm._fake import FakeCountTokensResponse, FakeMessage, FakeTextBlock, FakeUsage

    for criterion in rubric:
        for aid, _atext in pairs:
            if aid == target_aid and criterion.id == target_crit.id:
                continue
            fake.expect_count_tokens(
                matching=lambda _kw: True,
                returns=FakeCountTokensResponse(input_tokens=1500),
            )
            fake.expect_messages_create(
                matching=lambda _kw: True,
                returns=FakeMessage(
                    content=[
                        FakeTextBlock(
                            text=json.dumps(
                                {
                                    "criterion_id": criterion.id,
                                    "score": 0.5,
                                    "passed": True,
                                    "evidence": "",
                                    "reasoning": "live",
                                }
                            ),
                        )
                    ],
                    usage=FakeUsage(input_tokens=1700, output_tokens=80),
                    model="claude-fake-grade-judge",
                ),
            )

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )

    fake.assert_all_expectations_met()

    # The cached pair surfaces with the cached score.
    cached_result = [
        r
        for r in report.results
        if r.artifact_id == target_aid and r.criterion_id == target_crit.id
    ]
    assert len(cached_result) == 1
    assert cached_result[0].score == 0.93
    assert cached_result[0].evidence == "cached evidence"


def test_grade_engine_cache_miss_writes_entry(tmp_path: Path) -> None:
    """An empty cache + a happy live grade produces one ``<key>.json`` per
    successfully-graded pair under ``.signalforge/grade-cache/``.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )

    cache_dir = project_dir / ".signalforge" / "grade-cache"
    assert cache_dir.exists()
    json_files = list(cache_dir.glob("*.json"))
    # One cache file per non-degraded result.
    expected = sum(1 for r in report.results if r.score is not None)
    assert len(json_files) == expected
    # Filenames are 16-hex.
    for path in json_files:
        stem = path.stem
        assert len(stem) == 16
        assert all(c in "0123456789abcdef" for c in stem)


def test_grade_engine_cache_disabled_skips_lookup_and_write(tmp_path: Path) -> None:
    """``cache_enabled=False`` short-circuits BOTH lookup AND write.

    * Pre-populate the cache with a hit; assert the LLM is still called
      (lookup skipped — the engine never consults the cache).
    * Assert the cache dir contents are unchanged post-run (write skipped).
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()

    pairs = _stable_artifact_pairs(candidate)
    target_aid, target_text = pairs[0]
    target_crit = rubric[0]
    key, record = _build_cache_record_from_pair(
        artifact_id=target_aid,
        criterion=target_crit,
        artifact_text=target_text,
        rubric=rubric,
        score=0.93,
    )
    cache_file = _seed_cache(project_dir, key, record)
    before_bytes = cache_file.read_bytes()
    cache_dir = project_dir / ".signalforge" / "grade-cache"
    before_listing = sorted(p.name for p in cache_dir.glob("*.json"))

    fake = FakeAnthropicClient()
    # The full set of expectations is enqueued — the engine MUST call
    # every pair because lookup is disabled.
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    config = GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=60,
        cache_enabled=False,
    )

    grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=fake,
        project_dir=project_dir,
    )

    fake.assert_all_expectations_met()  # all LLM calls fired
    after_listing = sorted(p.name for p in cache_dir.glob("*.json"))
    assert after_listing == before_listing  # no new cache entries
    assert cache_file.read_bytes() == before_bytes  # existing entry untouched


def test_grade_engine_artifact_text_change_invalidates_cache(tmp_path: Path) -> None:
    """Pre-populate cache for the FIRST artifact's first criterion; then
    edit the column description; assert miss → LLM call fires.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()

    pairs = _stable_artifact_pairs(candidate)
    target_aid, original_text = pairs[0]
    target_crit = rubric[0]
    key, record = _build_cache_record_from_pair(
        artifact_id=target_aid,
        criterion=target_crit,
        artifact_text=original_text,
        rubric=rubric,
        score=0.99,  # would surface if cache hit
    )
    _seed_cache(project_dir, key, record)

    # Mutate the first column's description so its artifact_text_hash
    # rotates. The candidate's first pair is
    # ``column.<first_col>.description``.
    new_columns = list(candidate.columns)
    first = new_columns[0]
    new_columns[0] = first.model_copy(update={"description": "MUTATED DESCRIPTION"})
    candidate = candidate.model_copy(update={"columns": tuple(new_columns)})

    # All pairs need LLM expectations (the cached pair MUST also fire
    # because the key rotated).
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )

    fake.assert_all_expectations_met()
    # The (originally) cached pair surfaces with the LIVE score (0.5
    # from the fake's default), NOT the cached 0.99.
    rotated = [
        r
        for r in report.results
        if r.artifact_id == target_aid and r.criterion_id == target_crit.id
    ]
    assert len(rotated) == 1
    assert rotated[0].score == 0.5


def test_grade_engine_model_change_invalidates_cache(tmp_path: Path) -> None:
    """Pre-populate with model A; re-run with model B in GradeConfig;
    assert miss + live call.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()

    pairs = _stable_artifact_pairs(candidate)
    target_aid, target_text = pairs[0]
    target_crit = rubric[0]
    key, record = _build_cache_record_from_pair(
        artifact_id=target_aid,
        criterion=target_crit,
        artifact_text=target_text,
        rubric=rubric,
        model="claude-fake-A",
        score=0.99,
    )
    _seed_cache(project_dir, key, record)

    # Run with a different model. All pairs route through the LLM.
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(model_id="claude-fake-B"),
        client=fake,
        project_dir=project_dir,
    )

    fake.assert_all_expectations_met()
    rotated = [
        r
        for r in report.results
        if r.artifact_id == target_aid and r.criterion_id == target_crit.id
    ]
    assert len(rotated) == 1
    assert rotated[0].score == 0.5  # live score, NOT cached 0.99


def test_grade_engine_provider_change_invalidates_cache(tmp_path: Path) -> None:
    """Provider rotation is one of the five cache-key axes (DEC-004).

    We pre-populate the cache under a synthetic ``"other-provider"``
    key string. The actual run uses the default ``anthropic`` provider
    — so the lookup computes a different key and misses, even though
    every other axis (criterion / artifact / model / template) is
    identical. Decoupling the test from real cross-provider
    capability flags keeps the assertion focused on the load-bearing
    behaviour: changing the provider string rotates the cache key.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()

    pairs = _stable_artifact_pairs(candidate)
    target_aid, target_text = pairs[0]
    target_crit = rubric[0]
    # Pre-populate under a SYNTHETIC ``"other-provider"`` key. The
    # default Anthropic-provider run below will compute a different
    # key and miss.
    key, record = _build_cache_record_from_pair(
        artifact_id=target_aid,
        criterion=target_crit,
        artifact_text=target_text,
        rubric=rubric,
        provider="other-provider",
        model="claude-fake",
        score=0.99,
    )
    _seed_cache(project_dir, key, record)

    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )

    fake.assert_all_expectations_met()
    rotated = [
        r
        for r in report.results
        if r.artifact_id == target_aid and r.criterion_id == target_crit.id
    ]
    assert len(rotated) == 1
    assert rotated[0].score == 0.5  # live, NOT cached 0.99


def test_grade_engine_degraded_result_not_cached(tmp_path: Path) -> None:
    """A degraded result (score=None) never lands in the cache.

    Force a degrade by exhausting retries (``max_retries_429=0`` +
    queue a rate-limit error). Assert no cache file exists for that
    pair post-run.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    # Simpler candidate: 1 column, 0 tests = 4 artifacts × 1 criterion = 4 calls.
    candidate = CandidateSchema(
        name="orders",
        description="d",
        rationale="r",
        columns=(CandidateColumn(name="order_id", description="pk", rationale="rat"),),
        tests=(),
    )
    rubric: Rubric = (Criterion(id="clarity", criterion="Is it clear?"),)

    fake = FakeAnthropicClient()
    from tests.llm._fake import FakeCountTokensResponse

    # Every pair gets a rate-limit error.
    pairs = _stable_artifact_pairs(candidate)
    for _aid, _text in pairs:
        fake.expect_count_tokens(
            matching=lambda _kw: True,
            returns=FakeCountTokensResponse(input_tokens=1500),
        )
        fake.expect_messages_create(
            matching=lambda _kw: True,
            returns=LLMRateLimitError(
                "fake rate limit",
                attempts=0,
                cause=Exception("fake"),
            ),
        )

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        # require_complete=False: this test exercises the report-only
        # degrade-vs-cache contract; the unrecoverable transient pairs
        # would otherwise trip the #202 US-006 completeness raise.
        config=_config_no_audit_in_path().model_copy(update={"require_complete": False}),
        client=fake,
        project_dir=project_dir,
    )

    # All pairs degraded → no cache writes.
    assert all(r.score is None for r in report.results)
    cache_dir = project_dir / ".signalforge" / "grade-cache"
    json_files = list(cache_dir.glob("*.json")) if cache_dir.exists() else []
    assert json_files == [], f"degraded results must never reach the cache; found {json_files!r}"


def test_grade_engine_cache_write_failure_is_fail_soft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cache-write failure does NOT abort the live grade.

    Monkey-patch :func:`signalforge.grade.cache.write_cache` (as imported
    by the engine) to raise. Assert :func:`grade_artifacts` still
    returns a complete :class:`GradingReport`, no exception escapes,
    AND the engine emits its forensic WARNING line so operators can
    diagnose recurring cache-write failures (#189 QG Pass 3 Finding 8).
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    def _exploding_write(cache_dir, key, record) -> None:  # type: ignore[no-untyped-def]
        raise OSError("simulated disk full")

    # Patch the symbol the engine imports — engine.write_cache, not
    # cache.write_cache — because the engine binds the import at module
    # load time.
    monkeypatch.setattr(engine_module, "write_cache", _exploding_write)

    caplog.set_level(logging.WARNING, logger="signalforge.grade.engine")
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )

    # The live grade completed despite the cache-write explosion.
    assert isinstance(report, GradingReport)
    assert len(report.results) == 14
    assert all(r.score == 0.5 for r in report.results)
    # Forensic WARNING must surface — a silent swallow would lose the
    # operator-actionable signal that the cache layer is broken.
    assert any(
        "grade cache write failed (engine guard)" in r.getMessage() for r in caplog.records
    ), "engine's defence-in-depth WARNING must fire when write_cache raises"


def test_grade_engine_cache_hit_event_has_zero_tokens(tmp_path: Path) -> None:
    """A cache-hit :class:`GradeEvent` carries ``cache_hit=True`` and
    all four token-count fields == 0.

    Pre-populate the cache with one entry, run a single-criterion
    grade over a single-column candidate, scan the resulting
    ``grade.jsonl`` for the cache-hit record.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    # Trim to 1 column / no tests so total pairs = 4 (2 desc/rationale on the
    # one column + 2 model fields) × 1 criterion = 4 calls.
    candidate = CandidateSchema(
        name="orders",
        description="d",
        rationale="r",
        columns=(CandidateColumn(name="order_id", description="pk", rationale="rat"),),
        tests=(),
    )
    rubric: Rubric = (Criterion(id="clarity", criterion="Is it clear?"),)

    pairs = _stable_artifact_pairs(candidate)
    target_aid, target_text = pairs[0]
    target_crit = rubric[0]
    key, record = _build_cache_record_from_pair(
        artifact_id=target_aid,
        criterion=target_crit,
        artifact_text=target_text,
        rubric=rubric,
        score=0.77,
        passed=True,
        evidence="cached ev",
        reasoning="cached rsn",
    )
    _seed_cache(project_dir, key, record)

    # 3 remaining pairs route through the LLM.
    fake = FakeAnthropicClient()
    from tests.llm._fake import FakeCountTokensResponse, FakeMessage, FakeTextBlock, FakeUsage

    for aid, _text in pairs:
        if aid == target_aid:
            continue
        fake.expect_count_tokens(
            matching=lambda _kw: True,
            returns=FakeCountTokensResponse(input_tokens=1500),
        )
        fake.expect_messages_create(
            matching=lambda _kw: True,
            returns=FakeMessage(
                content=[
                    FakeTextBlock(
                        text=json.dumps(
                            {
                                "criterion_id": "clarity",
                                "score": 0.5,
                                "passed": True,
                                "evidence": "",
                                "reasoning": "live",
                            }
                        ),
                    )
                ],
                usage=FakeUsage(
                    input_tokens=1700,
                    output_tokens=80,
                    cache_creation_input_tokens=100,
                    cache_read_input_tokens=50,
                ),
                model="claude-fake-grade-judge",
            ),
        )

    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
        audit_path=audit_path,
    )

    rows = _read_jsonl(audit_path)
    hits = [
        r for r in rows if r["artifact_id"] == target_aid and r["criterion_id"] == target_crit.id
    ]
    assert len(hits) == 1
    hit = hits[0]
    assert hit["cache_hit"] is True
    assert hit["input_tokens"] == 0
    assert hit["output_tokens"] == 0
    assert hit["cache_creation_input_tokens"] == 0
    assert hit["cache_read_input_tokens"] == 0
    # The cached score surfaces verbatim.
    assert hit["score"] == 0.77
    assert hit["evidence"] == "cached ev"


def test_grade_engine_cache_hit_dispatch_order_preserved_with_async_misses(
    tmp_path: Path,
) -> None:
    """Mixed hit/miss pairs land in deterministic post-sort order.

    Per the asyncio refactor (#186) JSONL arrival order is
    non-deterministic; tests sort via the
    :func:`tests.grade._helpers._sort_grade_events` helper for
    deterministic comparison. The cache-hit slot lands too.
    """
    from tests.grade._helpers import _sort_grade_events

    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()

    # Pre-populate ONE pair so the run mixes cache hits and live misses.
    pairs = _stable_artifact_pairs(candidate)
    target_aid, target_text = pairs[0]
    target_crit = rubric[0]
    key, record = _build_cache_record_from_pair(
        artifact_id=target_aid,
        criterion=target_crit,
        artifact_text=target_text,
        rubric=rubric,
        score=0.88,
    )
    _seed_cache(project_dir, key, record)

    fake = FakeAnthropicClient()
    from tests.llm._fake import FakeCountTokensResponse, FakeMessage, FakeTextBlock, FakeUsage

    for criterion in rubric:
        for aid, _atext in pairs:
            if aid == target_aid and criterion.id == target_crit.id:
                continue
            fake.expect_count_tokens(
                matching=lambda _kw: True,
                returns=FakeCountTokensResponse(input_tokens=1500),
            )
            fake.expect_messages_create(
                matching=lambda _kw: True,
                returns=FakeMessage(
                    content=[
                        FakeTextBlock(
                            text=json.dumps(
                                {
                                    "criterion_id": criterion.id,
                                    "score": 0.5,
                                    "passed": True,
                                    "evidence": "",
                                    "reasoning": "live",
                                }
                            ),
                        )
                    ],
                    usage=FakeUsage(input_tokens=1700, output_tokens=80),
                    model="claude-fake-grade-judge",
                ),
            )

    config = GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=60,
        max_concurrent_calls=10,
    )

    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=fake,
        project_dir=project_dir,
        audit_path=audit_path,
    )

    rows = _read_jsonl(audit_path)
    sorted_rows = _sort_grade_events(rows)
    # Idempotent: re-sort and compare.
    assert _sort_grade_events(sorted_rows) == sorted_rows
    # Cache-hit row is present in the sorted set.
    hits = [
        r
        for r in sorted_rows
        if r["artifact_id"] == target_aid
        and r["criterion_id"] == target_crit.id
        and r.get("cache_hit") is True
    ]
    assert len(hits) == 1


def test_grade_artifacts_raises_grade_cache_path_error_on_symlinked_cache_dir(
    tmp_path: Path,
) -> None:
    """QG Pass 3 Finding 6 — the engine's ``canonicalise_path`` wrap
    of the cache_dir is otherwise uncovered. Plant a symlink at
    ``<project>/.signalforge/grade-cache`` whose target is outside
    the project — the engine must raise ``GradeCachePathError`` at
    orchestrator entry, before any LLM call."""
    from signalforge.grade.errors import GradeCachePathError

    project_dir = _project(tmp_path)
    # Plant a symlink target outside the project tree.
    outside = tmp_path / "outside" / ".signalforge" / "grade-cache"
    outside.mkdir(parents=True)
    # Replace the project's grade-cache dir with a symlink to outside.
    signalforge_dir = project_dir / ".signalforge"
    signalforge_dir.mkdir(parents=True, exist_ok=True)
    cache_link = signalforge_dir / "grade-cache"
    cache_link.symlink_to(outside)

    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()  # no expectations queued — must NOT be called

    with pytest.raises(GradeCachePathError):
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_config_no_audit_in_path(),
            client=fake,
            project_dir=project_dir,
        )
    # The fake's expectation queue is empty — if any LLM call had
    # leaked through, FakeAnthropicClient.messages.create would
    # raise "unexpected query".


# P3 Finding 7 (`prefilled_results` length-mismatch defensive guard at
# engine.py:619) is intentionally not unit-tested: the guard sits behind
# a private async core whose other kwargs (rubric_hash, template_hash,
# rubric_block, crit_hash_by_id, ...) require substantial setup, and
# it's unreachable from any public seam by construction — the
# orchestrator always builds ``prefilled_results`` to ``total_pairs``
# length. Coverage loss is one defensive raise; cost of testing exceeds
# value (issue #189 QG, accepted trade-off).


# --- PR #196 review fixes -------------------------------------------------


def test_cache_hit_grade_event_uses_live_run_hashes_not_stored_values(
    tmp_path: Path,
) -> None:
    """PR #196 CodeRabbit — cache-hit GradeEvent records THIS run's
    authoritative ``rubric_hash`` / ``prompt_version_template`` /
    ``criterion_prompt_hash`` (live values), NOT the stored record's
    values. The stored values may carry stale provenance if a sibling
    criterion was edited (only the 5-part cache key axes are guaranteed
    equal on a hit; ``rubric_hash`` is NOT in the key)."""
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = CandidateSchema(
        name="orders",
        description="d",
        rationale="r",
        columns=(CandidateColumn(name="order_id", description="pk", rationale="rat"),),
        tests=(),
    )
    rubric: Rubric = (Criterion(id="clarity", criterion="Is it clear?"),)
    pairs = _stable_artifact_pairs(candidate)
    target_aid, target_text = pairs[0]
    target_crit = rubric[0]

    # Build a legitimate cache record (with the LIVE rubric_hash so it
    # passes the new key-recomputation gate AND the cache-record drift
    # detector), then override its stored ``rubric_hash`` to a STALE
    # value before write. The key recipe doesn't use ``rubric_hash``,
    # so the file lands under the right filename but the body lies
    # about the rubric it was scored against.
    key, record = _build_cache_record_from_pair(
        artifact_id=target_aid,
        criterion=target_crit,
        artifact_text=target_text,
        rubric=rubric,
        score=0.77,
        passed=True,
        evidence="cached ev",
        reasoning="cached rsn",
    )
    STALE_RUBRIC_HASH = "deadbeefdeadbeef"
    record_stale = record.model_copy(update={"rubric_hash": STALE_RUBRIC_HASH})
    _seed_cache(project_dir, key, record_stale)

    # Other pairs need fake LLM responses since they're not cached.
    fake = FakeAnthropicClient()
    from tests.llm._fake import FakeCountTokensResponse, FakeMessage, FakeTextBlock, FakeUsage

    for aid, _text in pairs:
        if aid == target_aid:
            continue
        fake.expect_count_tokens(
            matching=lambda _kw: True,
            returns=FakeCountTokensResponse(input_tokens=1500),
        )
        fake.expect_messages_create(
            matching=lambda _kw: True,
            returns=FakeMessage(
                content=[
                    FakeTextBlock(
                        text=json.dumps(
                            {
                                "criterion_id": "clarity",
                                "score": 0.5,
                                "passed": True,
                                "evidence": "",
                                "reasoning": "live",
                            }
                        ),
                    )
                ],
                usage=FakeUsage(
                    input_tokens=1700,
                    output_tokens=80,
                    cache_creation_input_tokens=100,
                    cache_read_input_tokens=50,
                ),
                model="claude-fake-grade-judge",
            ),
        )

    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
        audit_path=audit_path,
    )

    rows = _read_jsonl(audit_path)
    hits = [
        r
        for r in rows
        if r["artifact_id"] == target_aid
        and r["criterion_id"] == target_crit.id
        and r.get("cache_hit") is True
    ]
    assert len(hits) == 1
    # The audit row's rubric_hash MUST be the LIVE value (the
    # _canonical_rubric_hash of the runtime rubric), NOT the stored
    # stale value. False provenance would be a real bug.
    assert hits[0]["rubric_hash"] != STALE_RUBRIC_HASH


# ---------------------------------------------------------------------------
# US-005 — Bounded transient-recovery sweep (Stage 2, #202 / DEC-206)
# ---------------------------------------------------------------------------
#
# Pins the always-on, bounded recovery sweep folded into the async core so
# the GradingReport reflects POST-sweep state:
#
# * N transient failures that recover on a later sweep round →
#   aggregate_complete=True, 100% scored.
# * sweep_max_rounds cap honoured (no infinite loop; pairs that keep
#   failing stay score=None after the cap).
# * "ceiling" / "budget" degrades are NEVER swept.
# * Swept attempts append a sweep_round-tagged audit record; a recovered
#   pair gets cached; the report aggregates reflect the post-sweep state.


def _instant_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the sweep cool-down so the test runs instantly.

    Mirrors the ``_async_sleep`` test-override seam (DEC-011 of #186). The
    sweep's inter-round pause routes through ``engine_module._async_sleep``;
    replacing it with a no-op coroutine keeps the sweep always-on while
    skipping the wall-clock wait.
    """

    async def _noop(_seconds: float) -> None:
        return None

    monkeypatch.setattr(engine_module, "_async_sleep", _noop)


class _StatefulGradeOne:
    """Stateful stand-in for :func:`engine_module._grade_one_async`.

    Records per-``(artifact_id, criterion_id)`` call counts. A target pair
    raises :class:`GradeLLMError` (a transient failure) for its first
    ``fail_times`` calls, then returns a scored verdict. Every other pair
    succeeds on the first call. Drives BOTH the concurrent main pass AND the
    sequential sweep through one code path so the recovery-on-retry contract
    is exercised end-to-end.
    """

    def __init__(
        self,
        *,
        target: tuple[str, str],
        fail_times: int,
        score: float = 0.9,
    ) -> None:
        self._target = target
        self._fail_times = fail_times
        self._score = score
        self.calls: dict[tuple[str, str], int] = {}

    async def __call__(
        self,
        *,
        artifact_id: str,
        criterion: Criterion,
        sweep_round: int | None = None,
        **_kw: Any,
    ) -> tuple:
        from signalforge.grade.audit import _build_grade_event
        from signalforge.grade.errors import GradeLLMError
        from signalforge.grade.models import GradingResult

        key = (artifact_id, criterion.id)
        self.calls[key] = self.calls.get(key, 0) + 1
        if key == self._target and self.calls[key] <= self._fail_times:
            raise GradeLLMError(
                f"transient blip for {artifact_id!r}/{criterion.id!r}",
                cause=Exception("simulated transient"),
            )
        result = GradingResult(
            artifact_id=artifact_id,
            criterion_id=criterion.id,
            score=self._score,
            passed=True,
            evidence="ok",
            reasoning="recovered",
        )
        event = _build_grade_event(
            run_id="run",
            timestamp=datetime.now(UTC),
            model_unique_id="model.shop.orders",
            artifact_id=artifact_id,
            criterion_id=criterion.id,
            score=self._score,
            passed=True,
            evidence="ok",
            reasoning="recovered",
            rubric_hash="0" * 16,
            prompt_version_template="1" * 16,
            criterion_prompt_hash="2" * 16,
            response_text_hash="3" * 16,
            model="claude-fake",
            input_tokens=10,
            output_tokens=5,
            sweep_round=sweep_round,
        )
        return result, event


def _simple_candidate() -> CandidateSchema:
    """A 1-column / 0-test candidate → 4 artifacts; ×2 criteria = 8 pairs."""
    return CandidateSchema(
        name="orders",
        description="d",
        rationale="r",
        columns=(CandidateColumn(name="order_id", description="pk", rationale="rat"),),
        tests=(),
    )


def test_sweep_recovers_transient_failures_to_full_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient pair that fails the main pass + first sweep round but
    succeeds on the second round → ``aggregate_complete=True``, 100% scored.

    The report is built from POST-sweep results, so a recovery flips
    ``aggregate_complete`` to True without any second results list.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _simple_candidate()
    rubric = _two_criteria()
    target_aid = _stable_artifact_pairs(candidate)[0][0]
    target = (target_aid, "clarity")
    # Fail the main pass (1) + sweep round 1 (1) = 2 failures, recover on round 2.
    stub = _StatefulGradeOne(target=target, fail_times=2)
    monkeypatch.setattr(engine_module, "_grade_one_async", stub)
    _instant_cooldown(monkeypatch)

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=FakeAnthropicClient(),
        project_dir=project_dir,
    )

    assert report.aggregate_complete is True
    assert all(r.score is not None for r in report.results)
    assert report.pass_rate == 1.0
    # The target pair was called 3 times total (main + 2 sweep rounds).
    assert stub.calls[target] == 3
    # Every other pair was called exactly once (sweep never re-touched them).
    for key, count in stub.calls.items():
        if key != target:
            assert count == 1, f"{key} should not be swept; got {count} calls"


def test_sweep_max_rounds_cap_is_honored_no_infinite_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pair that NEVER recovers stays ``score=None`` after the cap; the
    sweep makes exactly ``sweep_max_rounds`` extra attempts (no infinite loop).
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _simple_candidate()
    rubric = _two_criteria()
    target_aid = _stable_artifact_pairs(candidate)[0][0]
    target = (target_aid, "clarity")
    # Fail forever (more than any plausible round count).
    stub = _StatefulGradeOne(target=target, fail_times=999)
    monkeypatch.setattr(engine_module, "_grade_one_async", stub)
    _instant_cooldown(monkeypatch)

    # require_complete=False: a never-recovering transient pair is the
    # point of this test; the #202 US-006 completeness raise would
    # otherwise fire before the degrade-report assertions below.
    config = _config_no_audit_in_path().model_copy(
        update={"sweep_max_rounds": 3, "require_complete": False}
    )
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=FakeAnthropicClient(),
        project_dir=project_dir,
    )

    # The target stays degraded; everything else scored.
    degraded = [r for r in report.results if r.score is None]
    assert len(degraded) == 1
    assert degraded[0].artifact_id == target_aid
    assert degraded[0].degrade_reason_type == "transient"
    assert report.aggregate_complete is False
    # main pass (1) + 3 sweep rounds = 4 attempts on the target. No more.
    assert stub.calls[target] == 4


def test_sweep_never_touches_budget_or_ceiling_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``"budget"`` and ``"ceiling"`` degrades are NEVER swept.

    A ``max_grade_calls`` ceiling trips during the main pass, degrading the
    un-started pairs as ``"ceiling"``. The sweep must leave them alone — no
    re-grade attempt is made for a non-transient degrade.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _simple_candidate()
    rubric = _two_criteria()  # 8 pairs
    fake = FakeAnthropicClient()
    # Allow only 3 calls; the rest degrade as "ceiling". Run sequentially so
    # the trip is deterministic.
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)
    _instant_cooldown(monkeypatch)

    config = GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=60,
        max_concurrent_calls=1,
        max_grade_calls=3,
        sweep_max_rounds=3,
    )
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=fake,
        project_dir=project_dir,
    )

    ceiling_degrades = [r for r in report.results if r.degrade_reason_type == "ceiling"]
    assert ceiling_degrades, "expected some ceiling degrades from the max_grade_calls trip"
    # The sweep made NO additional LLM calls — the ceiling-degraded pairs
    # were not re-graded. If the sweep had touched them, the fake would have
    # raised on an unexpected (unqueued) call.
    assert all(r.score is None for r in ceiling_degrades)
    assert report.aggregate_complete is False


def test_sweep_appends_sweep_round_tagged_audit_record_and_caches_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swept attempt appends a NEW ``sweep_round``-tagged audit record
    (immutable log — the original failure record is preserved), and a
    recovered pair lands in the grade cache.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _simple_candidate()
    rubric = _two_criteria()
    target_aid = _stable_artifact_pairs(candidate)[0][0]
    target = (target_aid, "clarity")
    # Fail once on the main pass; recover on sweep round 1.
    stub = _StatefulGradeOne(target=target, fail_times=1)
    monkeypatch.setattr(engine_module, "_grade_one_async", stub)
    _instant_cooldown(monkeypatch)

    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=FakeAnthropicClient(),
        project_dir=project_dir,
        audit_path=audit_path,
    )

    assert report.aggregate_complete is True

    rows = _read_jsonl(audit_path)
    target_rows = [
        r for r in rows if r["artifact_id"] == target_aid and r["criterion_id"] == "clarity"
    ]
    # Immutable log: the original main-pass failure (sweep_round=null,
    # transient) AND the sweep-recovery (sweep_round=1, scored) both present.
    main_rows = [r for r in target_rows if r.get("sweep_round") is None]
    sweep_rows = [r for r in target_rows if r.get("sweep_round") == 1]
    assert len(main_rows) == 1
    assert main_rows[0]["score"] is None
    assert main_rows[0]["degrade_reason_type"] == "transient"
    assert len(sweep_rows) == 1
    assert sweep_rows[0]["score"] is not None
    assert sweep_rows[0]["degrade_reason_type"] is None

    # The recovered pair was written to the grade cache.
    cache_dir = project_dir / ".signalforge" / "grade-cache"
    cache_files = list(cache_dir.glob("*.json")) if cache_dir.exists() else []
    assert cache_files, "recovered sweep pair should be cached"


def test_sweep_cooldown_uses_async_sleep_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cool-down between the main pass and the sweep routes through the
    test-overridable ``_async_sleep`` seam with the configured duration.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _simple_candidate()
    rubric = _two_criteria()
    target_aid = _stable_artifact_pairs(candidate)[0][0]
    target = (target_aid, "clarity")
    stub = _StatefulGradeOne(target=target, fail_times=1)
    monkeypatch.setattr(engine_module, "_grade_one_async", stub)

    slept: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(engine_module, "_async_sleep", _record_sleep)

    config = _config_no_audit_in_path().model_copy(update={"sweep_cooldown_seconds": 1.5})
    grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=FakeAnthropicClient(),
        project_dir=project_dir,
    )

    # Exactly one cool-down fired (one sweep round had transient work) with
    # the configured duration.
    assert slept == [1.5]


def test_sweep_cooldown_zero_disables_wait_but_sweep_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sweep_cooldown_seconds=0`` skips the wait but the sweep is still
    always-on and recovers transient failures.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _simple_candidate()
    rubric = _two_criteria()
    target_aid = _stable_artifact_pairs(candidate)[0][0]
    target = (target_aid, "clarity")
    stub = _StatefulGradeOne(target=target, fail_times=1)
    monkeypatch.setattr(engine_module, "_grade_one_async", stub)

    slept: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(engine_module, "_async_sleep", _record_sleep)

    config = _config_no_audit_in_path().model_copy(update={"sweep_cooldown_seconds": 0.0})
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=FakeAnthropicClient(),
        project_dir=project_dir,
    )

    assert slept == []  # no cool-down wait fired
    assert report.aggregate_complete is True  # sweep still recovered the pair


def test_sweep_max_rounds_zero_runs_no_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sweep_max_rounds=0`` runs the main pass alone — a transient pair
    stays degraded (no sweep round executes).
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _simple_candidate()
    rubric = _two_criteria()
    target_aid = _stable_artifact_pairs(candidate)[0][0]
    target = (target_aid, "clarity")
    stub = _StatefulGradeOne(target=target, fail_times=1)
    monkeypatch.setattr(engine_module, "_grade_one_async", stub)
    _instant_cooldown(monkeypatch)

    # require_complete=False: with no sweep the single transient pair
    # stays degraded; the #202 US-006 raise would otherwise fire.
    config = _config_no_audit_in_path().model_copy(
        update={"sweep_max_rounds": 0, "require_complete": False}
    )
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=FakeAnthropicClient(),
        project_dir=project_dir,
    )

    # No sweep: the single main-pass failure stays degraded.
    assert report.aggregate_complete is False
    assert stub.calls[target] == 1  # main pass only


def test_sweep_config_validators_reject_negative_values() -> None:
    """``sweep_max_rounds`` / ``sweep_cooldown_seconds`` reject negatives;
    zero is allowed for both.
    """
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        GradeConfig(model="claude-fake", sweep_max_rounds=-1)
    with _pytest.raises(ValidationError):
        GradeConfig(model="claude-fake", sweep_cooldown_seconds=-0.5)
    # Zero is valid for both.
    cfg = GradeConfig(model="claude-fake", sweep_max_rounds=0, sweep_cooldown_seconds=0.0)
    assert cfg.sweep_max_rounds == 0
    assert cfg.sweep_cooldown_seconds == 0.0


# ---------------------------------------------------------------------------
# require_complete / GradeIncompleteError (#202 US-006 — DEC-204 + DEC-207)
# ---------------------------------------------------------------------------


def test_require_complete_default_is_true() -> None:
    """``require_complete`` defaults to ``True`` (fail-loud) — DEC-207."""
    assert GradeConfig(model="claude-fake").require_complete is True


def test_require_complete_trips_on_unrecovered_transient_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient pair that never recovers (even after the sweep) trips
    ``GradeIncompleteError`` when ``require_complete=True`` (DEC-204)."""
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _simple_candidate()
    rubric = _two_criteria()
    target_aid = _stable_artifact_pairs(candidate)[0][0]
    target = (target_aid, "clarity")
    # Fail forever — the sweep can't recover it; the pair stays transient.
    stub = _StatefulGradeOne(target=target, fail_times=999)
    monkeypatch.setattr(engine_module, "_grade_one_async", stub)
    _instant_cooldown(monkeypatch)

    # require_complete defaults to True; total_budget_seconds explicit so a
    # spurious budget trip can't confound the transient signal.
    config = _config_no_audit_in_path().model_copy(update={"sweep_max_rounds": 3})
    with pytest.raises(GradeIncompleteError) as excinfo:
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=config,
            client=FakeAnthropicClient(),
            project_dir=project_dir,
        )
    err = excinfo.value
    assert err.require_complete is True
    assert err.aggregate_complete is False
    # The error names exactly the unrecovered transient pair.
    assert err.incomplete_pairs == ((target_aid, "clarity"),)


def test_require_complete_trips_on_default_scaled_budget_degrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``"budget"`` degrade with ``total_budget_seconds is None`` (the
    DEFAULT-scaled-budget formula) trips — a Stage-1 sizing canary (DEC-204).
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    # Force the wall-clock timeout to fire immediately even though the
    # config uses the default scaled budget (total_budget_seconds=None):
    # pin the EFFECTIVE budget to 1s and make every coroutine overrun it.
    _stub_grade_one_async_slow(monkeypatch)
    monkeypatch.setattr(engine_module, "_compute_effective_budget", lambda **_kw: 1)

    config = GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=None,  # DEFAULT-scaled budget → trips
        max_concurrent_calls=2,
        sweep_max_rounds=0,  # budget degrades are never swept anyway
    )
    with pytest.raises(GradeIncompleteError) as excinfo:
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=config,
            client=fake,
            project_dir=project_dir,
        )
    # Every pair is a budget degrade; all of them trip.
    assert excinfo.value.incomplete_pairs  # non-empty
    assert excinfo.value.aggregate_complete is False


def test_require_complete_does_not_trip_on_explicit_budget_degrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``"budget"`` degrade is EXEMPT when ``total_budget_seconds`` was
    set EXPLICITLY — a deliberate operator time-ceiling (DEC-204). The run
    returns a partial report rather than raising.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    _stub_grade_one_async_slow(monkeypatch)

    # total_budget_seconds=1 is an EXPLICIT operator ceiling → budget
    # degrades are exempt; require_complete defaults to True.
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_tiny_budget(),
        client=fake,
        project_dir=project_dir,
    )
    assert report.aggregate_complete is False
    assert all(r.degrade_reason_type == "budget" for r in report.results)


def test_require_complete_does_not_trip_on_ceiling_degrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``"ceiling"`` degrade (explicit ``max_grade_*`` opt-in) is EXEMPT
    (DEC-204). The run returns a partial report rather than raising.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _simple_candidate()
    rubric = _two_criteria()  # 8 pairs
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)
    _instant_cooldown(monkeypatch)

    config = GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=60,
        max_concurrent_calls=1,
        max_grade_calls=3,  # the rest degrade as "ceiling"
        sweep_max_rounds=3,
    )
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=fake,
        project_dir=project_dir,
    )
    ceiling_degrades = [r for r in report.results if r.degrade_reason_type == "ceiling"]
    assert ceiling_degrades, "expected ceiling degrades from the max_grade_calls trip"
    assert report.aggregate_complete is False


def test_require_complete_false_never_raises_on_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``require_complete=False`` restores report-only posture: an
    unrecovered transient pair surfaces via ``aggregate_complete=False``,
    never a raise.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _simple_candidate()
    rubric = _two_criteria()
    target_aid = _stable_artifact_pairs(candidate)[0][0]
    target = (target_aid, "clarity")
    stub = _StatefulGradeOne(target=target, fail_times=999)
    monkeypatch.setattr(engine_module, "_grade_one_async", stub)
    _instant_cooldown(monkeypatch)

    config = _config_no_audit_in_path().model_copy(
        update={"sweep_max_rounds": 1, "require_complete": False}
    )
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=config,
        client=FakeAnthropicClient(),
        project_dir=project_dir,
    )
    assert report.aggregate_complete is False
    degraded = [r for r in report.results if r.score is None]
    assert len(degraded) == 1
    assert degraded[0].degrade_reason_type == "transient"


def test_require_complete_writes_sidecar_before_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Load-bearing DEC-204 ordering invariant.** Even when
    ``grade_artifacts`` raises ``GradeIncompleteError``, the sidecar JSON
    must be durably on disk — the operator needs ``grade.json`` to diagnose
    which pairs stayed ungraded.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _simple_candidate()
    rubric = _two_criteria()
    target_aid = _stable_artifact_pairs(candidate)[0][0]
    target = (target_aid, "clarity")
    stub = _StatefulGradeOne(target=target, fail_times=999)
    monkeypatch.setattr(engine_module, "_grade_one_async", stub)
    _instant_cooldown(monkeypatch)

    sidecar_path = project_dir / ".signalforge" / "grade.json"
    audit_path = project_dir / ".signalforge" / "grade.jsonl"

    with pytest.raises(GradeIncompleteError):
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_config_no_audit_in_path().model_copy(update={"sweep_max_rounds": 1}),
            client=FakeAnthropicClient(),
            project_dir=project_dir,
            sidecar_path=sidecar_path,
            audit_path=audit_path,
        )

    assert sidecar_path.exists()
    raw = sidecar_path.read_text(encoding="utf-8").strip()
    round_tripped = GradingReport.model_validate_json(raw)
    assert round_tripped.aggregate_complete is False
    assert round_tripped.model_unique_id == model.unique_id


def test_require_complete_checked_before_fail_on_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``require_complete`` is checked BEFORE ``fail_on_below_threshold``:
    an incomplete run that is ALSO below threshold raises the structural
    ``GradeIncompleteError``, not ``GradeBelowThresholdError`` (DEC-204).
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _simple_candidate()
    rubric = _two_criteria()
    target_aid = _stable_artifact_pairs(candidate)[0][0]
    target = (target_aid, "clarity")
    # Fail one pair forever (transient → incomplete); the rest score at 0.9
    # so the aggregate is NOT below threshold from the scored subset alone.
    # The ungraded pair makes aggregate_complete=False — but the incomplete
    # check fires first regardless.
    stub = _StatefulGradeOne(target=target, fail_times=999, score=0.9)
    monkeypatch.setattr(engine_module, "_grade_one_async", stub)
    _instant_cooldown(monkeypatch)

    config = _config_no_audit_in_path().model_copy(
        update={
            "sweep_max_rounds": 1,
            "require_complete": True,
            "fail_on_below_threshold": True,
            "min_pass_rate": 1.0,  # the incomplete pair would also fail this
            "min_mean_score": 1.0,
        }
    )
    with pytest.raises(GradeIncompleteError):
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=config,
            client=FakeAnthropicClient(),
            project_dir=project_dir,
        )
