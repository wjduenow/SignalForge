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
    GradePromptEnvelopeBreachError,
)
from signalforge.grade.models import GradeEvent, GradingReport
from signalforge.grade.rubric import DEFAULT_RUBRIC, Criterion, Rubric
from signalforge.llm.errors import LLMRateLimitError
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
        config=_config_no_audit_in_path(),
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
    scored = [r for r in report.results if r.score is not None]
    assert len(scored) == 7
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

        # The message names both the offending provider and the cap.
        assert "sync-only-test-provider-186-us008" in str(excinfo.value)
        assert "max_concurrent_calls=5" in str(excinfo.value)
        # The remediation is locked verbatim by DEC-006 of #186.
        assert "grade.max_concurrent_calls: 1" in str(excinfo.value)
        # Defence-in-depth: no LLM call was attempted.
        assert fake.create_calls == []
    finally:
        providers_module._REGISTRY.clear()
        providers_module._REGISTRY.update(saved)


def test_grade_artifacts_sync_only_provider_with_cap_one_does_not_raise(
    tmp_path: Path,
) -> None:
    """The pair (``supports_async=False``, ``max_concurrent_calls=1``)
    is the documented escape hatch — pin the negative direction so a
    refactor that drops the ``> 1`` predicate (and hence rejects every
    sync provider unconditionally) breaks loud.

    Asserts the GUARD does not fire, by verifying that
    :class:`LLMProviderAsyncUnsupportedError` is NOT raised. We do NOT
    drive the full grade pipeline because the canned ``FakeNoCacheClient``
    runtime is mismatched with the ``FakeAnthropicClient`` test injection
    seam used by every other engine test; the load-bearing assertion here
    is purely "the typed pre-flight error was not raised".
    """
    from signalforge.llm import providers as providers_module
    from signalforge.llm.errors import LLMProviderAsyncUnsupportedError
    from tests.llm._fake_provider import FakeNoCacheProvider

    class _SyncOnlyButSerial(FakeNoCacheProvider):
        name = "sync-only-serial-test-provider-186-us008"
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
            provider="sync-only-serial-test-provider-186-us008",
            max_concurrent_calls=1,
        )

        # The guard must not raise ``LLMProviderAsyncUnsupportedError``.
        # The grade pipeline may surface a different error downstream
        # (FakeNoCacheClient vs FakeAnthropicClient runtime mismatch is
        # not the contract under test); the load-bearing assertion is
        # that the pre-flight guard let execution through.
        try:
            grade_artifacts(
                model,
                candidate,
                _empty_prune_result(model),
                rubric=rubric,
                config=config,
                client=fake,
                project_dir=project_dir,
            )
        except LLMProviderAsyncUnsupportedError:  # pragma: no cover - regression guard
            pytest.fail(
                "LLMProviderAsyncUnsupportedError raised with "
                "max_concurrent_calls=1; the > 1 predicate has regressed."
            )
        except Exception:
            # Any other exception is acceptable — the runtime mismatch
            # between FakeNoCacheClient and FakeAnthropicClient is not
            # the contract under test. The guard fired before the LLM
            # call; downstream failures only confirm execution proceeded
            # past it.
            pass
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
    locked JSON field set (DEC-018 of #186): ``run_id``,
    ``model_unique_id``, ``completed_count``, ``cancelled_count``,
    ``degraded_count``, ``total_budget_seconds``.

    External operator dashboards key on these field names; locking the
    shape via a pinned test is what makes the audit corpus a stable
    contract.
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
    # JSON field set locked verbatim (DEC-018 of #186).
    assert set(payload.keys()) == {
        "run_id",
        "model_unique_id",
        "completed_count",
        "cancelled_count",
        "degraded_count",
        "total_budget_seconds",
    }
    assert payload["model_unique_id"] == model.unique_id
    assert payload["total_budget_seconds"] == 1
    # Every pair degraded (completed + cancelled + degraded == total).
    candidate_pairs = len(_stable_artifact_pairs(candidate))
    total_pairs = len(rubric) * candidate_pairs
    assert (
        payload["completed_count"] + payload["cancelled_count"] + payload["degraded_count"]
        == total_pairs
    )


def test_grade_artifacts_module_level_async_sleep_alias_present() -> None:
    """The ``_async_sleep`` alias is module-scoped and reassignable for
    deterministic budget tests, mirroring :data:`signalforge.llm.client._async_sleep`
    (DEC-011 of #186).
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

    # If a multi-exception group reached the renderer, the US-010
    # header is in the rendered output. This is the documented happy
    # path for this test (2 criteria × ≥2 artifact pairs ⇒ ≥4
    # concurrent failures), so we pin it.
    if isinstance(excinfo.value, ExceptionGroup):
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
