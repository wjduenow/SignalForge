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

import json
import logging
from datetime import datetime, timezone
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


def test_grade_artifacts_budget_exceeded_marks_remaining_pairs_score_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkey-patch ``time.monotonic`` so the budget trips after a
    couple of pairs. Remaining pairs land as ``score=None``.
    """
    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _two_criteria()
    fake = FakeAnthropicClient()
    expect_grade_responses(fake, rubric=rubric, candidate=candidate)

    # Stub time.monotonic so the orchestrator believes the wall clock
    # has advanced 999 seconds at the start of the loop. The
    # GradeConfig default of total_budget_seconds=60 means every
    # iteration past the first budget-check trips to "exhausted".
    times = iter([0.0] + [999.0] * 200)
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: next(times))

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )

    # All pairs degraded.
    assert all(r.score is None for r in report.results)
    assert all(r.passed is False for r in report.results)
    assert report.aggregate_complete is False
    # No LLM calls should have been issued — the fake's expectations
    # remain queued (count_tokens + create per pair).
    assert len(fake.create_calls) == 0


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

    times = iter([0.0] + [999.0] * 200)
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: next(times))

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
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

    # Build the standard expectations BUT swap the FIRST messages.create
    # to raise an LLMRateLimitError instead of returning a payload. The
    # first call corresponds to (criterion=clarity, artifact=order_id
    # description).
    fake = FakeAnthropicClient()
    # Enqueue: count_tokens for call 1, then create -> raises LLMRateLimitError.
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
    # 8 results total; the first one degraded, the remaining 7 scored.
    assert len(report.results) == 8
    degraded = [r for r in report.results if r.score is None]
    assert len(degraded) == 1
    assert "GradeLLMError" in degraded[0].reasoning
    scored = [r for r in report.results if r.score is not None]
    assert len(scored) == 7
    assert report.aggregate_complete is False


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

    before = datetime.now(timezone.utc)
    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config_no_audit_in_path(),
        client=fake,
        project_dir=project_dir,
    )
    after = datetime.now(timezone.utc)

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
