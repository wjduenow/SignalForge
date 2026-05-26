"""Tests for ``signalforge.cli._estimate.estimate`` (US-003 of issue #36).

These tests pin the load-bearing invariants of the estimate engine:

1. Exactly ``1 + N`` ``count_tokens`` calls (1 drafter + N grade criteria).
2. **Zero** ``messages.create`` calls (AC-4 of the ticket).
3. Exactly one ``estimate_query_bytes`` dry-run call.
4. USD math matches a hand-calculated reference to four decimal places.
5. Partial-failure degrade (DEC-005) when ``estimate_query_bytes`` raises.
6. All non-warehouse exceptions propagate (LLM auth, unknown model, ...).
7. ``__repr__`` omits per-criterion payloads (mirrors ``prune-engine.md``
   DEC-022).
8. The first-alphabetical artifact_id is used as the grade-criterion
   representative across all criteria.
9. The single INFO log carries the DEC-013 field set.
"""

from __future__ import annotations

import json
import logging

import pytest

from signalforge.cli._estimate import CriterionEstimate, EstimateReport, estimate, render
from signalforge.draft.config import DraftConfig
from signalforge.grade.config import GradeConfig
from signalforge.grade.rubric import DEFAULT_RUBRIC
from signalforge.llm.errors import EstimateUnknownModelError, LLMAuthError
from signalforge.llm.pricing import PRICE_TABLE_VERSION
from signalforge.llm.pricing import lookup as pricing_lookup
from signalforge.manifest.models import Column, Manifest, Model
from signalforge.prune.config import PruneConfig
from signalforge.warehouse import BigQueryAdapter, WarehouseAuthError
from signalforge.warehouse.adapters.snowflake import SnowflakeAdapter
from tests.llm._fake import FakeAnthropicClient, FakeCountTokensResponse
from tests.warehouse._fake import FakeBigQueryClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_model(*, name: str = "customers", columns: tuple[str, ...] = ("id", "email")) -> Model:
    return Model(
        unique_id=f"model.shop.{name}",
        name=name,
        resource_type="model",
        package_name="shop",
        original_file_path=f"models/{name}.sql",
        path=f"{name}.sql",
        database="fake_project",
        schema="analytics",  # type: ignore[call-arg]
        columns={c: Column(name=c, data_type="STRING") for c in columns},
        raw_code=f"select * from `fake_project.raw.{name}`",
    )


def _make_manifest(model: Model | None = None) -> Manifest:
    m = model or _make_model()
    return Manifest(metadata={"dbt_schema_version": "v12"}, nodes={m.unique_id: m})


@pytest.fixture
def model() -> Model:
    return _make_model()


@pytest.fixture
def manifest(model: Model) -> Manifest:
    return _make_manifest(model)


@pytest.fixture
def draft_config() -> DraftConfig:
    return DraftConfig()


@pytest.fixture
def grade_config() -> GradeConfig:
    return GradeConfig()


@pytest.fixture
def prune_config() -> PruneConfig:
    return PruneConfig()


@pytest.fixture
def fake_client() -> FakeBigQueryClient:
    return FakeBigQueryClient(project="fake_project")


@pytest.fixture
def adapter(fake_client: FakeBigQueryClient) -> BigQueryAdapter:
    return BigQueryAdapter(
        project="fake_project",
        location="US",
        max_bytes_billed=100_000_000,
        client=fake_client,
    )


@pytest.fixture
def fake_anthropic() -> FakeAnthropicClient:
    return FakeAnthropicClient(project="fake_project")


def _queue_count_tokens(
    fake: FakeAnthropicClient, *, draft: int, per_criterion: int, n_criteria: int
) -> None:
    """Queue one draft + N per-criterion ``count_tokens`` expectations.

    Uses a permissive ``lambda kwargs: True`` matcher so the test
    doesn't have to fully reproduce the production prompt bytes — the
    assertion that the count was issued, plus the assertion on call
    count, are the load-bearing invariants.
    """
    fake.expect_count_tokens(
        matching=lambda kwargs: True,
        returns=FakeCountTokensResponse(input_tokens=draft),
    )
    for _ in range(n_criteria):
        fake.expect_count_tokens(
            matching=lambda kwargs: True,
            returns=FakeCountTokensResponse(input_tokens=per_criterion),
        )


# ---------------------------------------------------------------------------
# AC tests
# ---------------------------------------------------------------------------


def test_estimate_calls_count_tokens_for_draft_and_each_grade_criterion(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
) -> None:
    """AC-1 (call-count invariant). ``estimate(...)`` issues exactly
    ``1 + N`` ``count_tokens`` calls where ``N == len(rubric)``.
    """
    n_criteria = len(DEFAULT_RUBRIC)
    _queue_count_tokens(fake_anthropic, draft=1000, per_criterion=500, n_criteria=n_criteria)
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=10_000)

    estimate(
        model,
        manifest,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fake_anthropic,
    )

    assert len(fake_anthropic.count_calls) == 1 + n_criteria


def test_estimate_never_calls_messages_create(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
) -> None:
    """AC-4 of the ticket: ``len(fake.messages._create_calls) == 0``."""
    _queue_count_tokens(fake_anthropic, draft=100, per_criterion=50, n_criteria=len(DEFAULT_RUBRIC))
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=1024)

    estimate(
        model,
        manifest,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fake_anthropic,
    )

    assert len(fake_anthropic.create_calls) == 0


def test_estimate_calls_dry_run_once_for_warehouse_bytes(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
) -> None:
    """Exactly one ``expect_dry_run`` is consumed."""
    _queue_count_tokens(fake_anthropic, draft=100, per_criterion=50, n_criteria=len(DEFAULT_RUBRIC))
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=2048)

    estimate(
        model,
        manifest,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fake_anthropic,
    )

    fake_client.assert_all_expectations_met()


def test_estimate_total_llm_usd_matches_hand_calculation(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
) -> None:
    """Pin USD to four decimals against a hand-computed expected.

    Hand calculation with the default ``DraftConfig``/``GradeConfig``
    (``claude-sonnet-4-6`` for both, ``$3/MTok`` input, ``$15/MTok``
    output) and the default rubric (4 criteria):

    Draft input: 1_000_000 tokens (1 MTok) → $3.00.
    Draft output: 4096 tokens (default ``max_output_tokens``)
        → 4096 / 1e6 * 15 ≈ $0.06144.
    Draft USD ≈ 3.06144.

    Grade per criterion (4 criteria):
        artifact_count for our 2-column model:
            2*2 (column desc+rationale) + 2 (model desc+rationale) +
            int(3.5*2) (test rationales) = 4 + 2 + 7 = 13.
        Input tokens per call (queued) = 500 → 500 * 13 = 6500.
        Per-criterion input USD: 6500/1e6 * 3 = 0.0195.
        Per-criterion output USD: 50 * 13 / 1e6 * 15 = 650/1e6*15
            = 0.00975.
        Per-criterion total: 0.0195 + 0.00975 = 0.02925.
        Across 4 criteria: 4 * 0.02925 = 0.117.

    Grand total: 3.06144 + 0.117 = 3.17844.

    Test pins to 4 decimals.
    """
    n_criteria = len(DEFAULT_RUBRIC)
    _queue_count_tokens(fake_anthropic, draft=1_000_000, per_criterion=500, n_criteria=n_criteria)
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=10_000)

    report = estimate(
        model,
        manifest,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fake_anthropic,
    )

    pricing = pricing_lookup(draft_config.model)
    expected_draft = (1_000_000 / 1_000_000.0) * pricing.input_per_mtok + (
        4096 / 1_000_000.0
    ) * pricing.output_per_mtok
    artifact_count = 2 * 2 + 2 + int(3.5 * 2)
    per_crit_in = (500 * artifact_count) / 1_000_000.0 * pricing.input_per_mtok
    per_crit_out = (50 * artifact_count) / 1_000_000.0 * pricing.output_per_mtok
    expected_grade = n_criteria * (per_crit_in + per_crit_out)
    expected_total = expected_draft + expected_grade

    assert round(report.total_llm_usd, 4) == round(expected_total, 4)


def test_estimate_degrades_on_warehouse_auth_error_and_continues(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-005: ``WarehouseError`` from ``estimate_query_bytes`` is
    captured into ``warehouse_unavailable_reason``; the report is
    still produced with valid LLM-cost figures.
    """
    _queue_count_tokens(fake_anthropic, draft=100, per_criterion=50, n_criteria=len(DEFAULT_RUBRIC))
    boom = WarehouseAuthError("credentials invalid")
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=boom)

    report = estimate(
        model,
        manifest,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fake_anthropic,
    )

    assert report.warehouse_unavailable_reason is not None
    assert report.warehouse_unavailable_reason.startswith("WarehouseAuthError:")
    assert report.warehouse_bytes_per_row is None
    assert report.warehouse_total_bytes is None
    # The LLM-cost half of the report still computed.
    assert report.total_llm_usd > 0


def test_estimate_warehouse_unavailable_reason_carries_error_class_name(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-005 shape: ``f"{type(exc).__name__}: {str(exc)[:200]}"``."""
    _queue_count_tokens(fake_anthropic, draft=100, per_criterion=50, n_criteria=len(DEFAULT_RUBRIC))
    msg = "credentials invalid"
    boom = WarehouseAuthError(msg)
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=boom)

    report = estimate(
        model,
        manifest,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fake_anthropic,
    )

    assert report.warehouse_unavailable_reason is not None
    # Class name prefix is the load-bearing pin; the message body
    # may be truncated but the class-name boundary is exact.
    assert report.warehouse_unavailable_reason.startswith("WarehouseAuthError: ")


def test_estimate_degrades_on_snowflake_estimate_not_supported(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """DEC-005 (#123): a real ``SnowflakeAdapter`` inherits the ABC default
    ``estimate_query_bytes`` which raises ``EstimateNotSupportedError`` (a
    ``WarehouseError`` subclass). The engine must catch it, degrade the
    warehouse-bytes section into ``warehouse_unavailable_reason``, and still
    compute the LLM-cost half.

    This test would FAIL if the engine's ``except WarehouseError`` in
    ``signalforge/cli/_estimate.py`` were narrowed to exclude
    ``EstimateNotSupportedError`` (since it subclasses ``WarehouseError``,
    the current catch handles it). The assertion keys specifically on the
    ``EstimateNotSupportedError`` class name so a narrowing refactor breaks
    the test.

    No fake warehouse client is needed: ``SnowflakeAdapter.estimate_query_bytes``
    raises before any connection, and the ``--estimate`` path's
    ``_build_representative_sql`` only touches ``dialect()`` +
    ``TableRef.from_model`` — never the ``NotImplementedError`` skeleton ops.
    """
    n_criteria = len(DEFAULT_RUBRIC)
    _queue_count_tokens(fake_anthropic, draft=1000, per_criterion=500, n_criteria=n_criteria)
    adapter = SnowflakeAdapter()

    report = estimate(
        model,
        manifest,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fake_anthropic,
    )

    assert report.warehouse_unavailable_reason is not None
    assert report.warehouse_unavailable_reason.startswith("EstimateNotSupportedError:")
    assert report.warehouse_total_bytes is None
    assert report.warehouse_bytes_per_row is None
    # The LLM-cost half of the report is unaffected by the warehouse degrade.
    assert report.total_llm_usd > 0

    rendered = render(report)
    assert "<unavailable: EstimateNotSupportedError>" in rendered
    assert "Total estimated warehouse: <unknown>" in rendered

    # Strictness: a drift to FEWER count_tokens calls would leave queued
    # expectations unconsumed (extra calls already raise). Pin exact
    # consumption so the LLM-cost half stays load-bearing under refactor.
    fake_anthropic.assert_all_expectations_met()


def test_estimate_emits_warning_on_warehouse_degrade(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEC-005 (QG pass-4 B-3 fix): the engine emits exactly one WARNING
    via lazy-format JSON when ``estimate_query_bytes`` raises a
    ``WarehouseError``. Pins the WARNING shape so a refactor that
    silently drops the breadcrumb (or paraphrases the verb) fails loud.

    Without this, operators would see ``<unavailable: ...>`` in stdout
    but get no out-of-band signal that the run was degraded — the same
    failure mode the prune engine's DEC-009 warning defends against.
    """
    _queue_count_tokens(fake_anthropic, draft=100, per_criterion=50, n_criteria=len(DEFAULT_RUBRIC))
    boom = WarehouseAuthError("credentials invalid")
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=boom)

    with caplog.at_level(logging.WARNING, logger="signalforge.cli._estimate"):
        estimate(
            model,
            manifest,
            draft_config,
            grade_config,
            prune_config,
            adapter,
            fake_anthropic,
        )

    warning_records = [
        r
        for r in caplog.records
        if r.name == "signalforge.cli._estimate" and r.levelno == logging.WARNING
    ]
    assert len(warning_records) == 1
    msg = warning_records[0].getMessage()
    assert msg.startswith("warehouse-bytes unavailable: "), msg
    json_tail = msg.split("warehouse-bytes unavailable: ", 1)[1]
    parsed = json.loads(json_tail)
    assert parsed["model_unique_id"] == model.unique_id
    assert parsed["error_class"] == "WarehouseAuthError"
    assert "credentials invalid" in parsed["error_message"]
    assert "run_id" in parsed


def test_estimate_propagates_llm_auth_error(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
) -> None:
    """Non-degraded path: an LLM auth error from ``count_tokens``
    propagates rather than landing in
    ``warehouse_unavailable_reason``.
    """
    fake_anthropic.expect_count_tokens(
        matching=lambda kwargs: True,
        returns=LLMAuthError("invalid API key"),
    )
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=1024)

    with pytest.raises(LLMAuthError):
        estimate(
            model,
            manifest,
            draft_config,
            grade_config,
            prune_config,
            adapter,
            fake_anthropic,
        )


def test_estimate_propagates_estimateunknownmodelerror(
    model: Model,
    manifest: Manifest,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """An unknown drafter model name surfaces as
    ``EstimateUnknownModelError`` from ``pricing.lookup`` — propagates
    through ``estimate(...)``.
    """
    bad_draft = DraftConfig.model_validate(
        {**DraftConfig().model_dump(), "model": "unknown-model-xyz"}
    )

    with pytest.raises(EstimateUnknownModelError):
        estimate(
            model,
            manifest,
            bad_draft,
            grade_config,
            prune_config,
            adapter,
            fake_anthropic,
        )


def test_estimate_report_repr_omits_per_criterion_payloads(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
) -> None:
    """Custom ``__repr__`` (mirrors ``prune-engine.md`` DEC-022).

    The default-rubric criterion ids (``clarity``, ``consistency``,
    ``rationale``, ``no-redundant``) MUST NOT appear in ``repr(report)``.
    Per-criterion payloads stay accessible via field access; they just
    don't slip out the casual debug-print path.
    """
    _queue_count_tokens(fake_anthropic, draft=100, per_criterion=50, n_criteria=len(DEFAULT_RUBRIC))
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=1024)

    report = estimate(
        model,
        manifest,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fake_anthropic,
    )

    text = repr(report)
    for crit in DEFAULT_RUBRIC:
        assert crit.id not in text
    # And the field-name guard for the heavy field.
    assert "grade_per_criterion" not in text
    assert "input_tokens_per_call" not in text


def test_estimate_emits_single_info_log_with_expected_fields(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEC-013: single end-of-run INFO log via lazy-format JSON.

    Asserts exactly one record at INFO from ``signalforge.cli._estimate``
    whose formatted message contains all DEC-013 keys.
    """
    _queue_count_tokens(fake_anthropic, draft=100, per_criterion=50, n_criteria=len(DEFAULT_RUBRIC))
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=4096)

    with caplog.at_level(logging.INFO, logger="signalforge.cli._estimate"):
        estimate(
            model,
            manifest,
            draft_config,
            grade_config,
            prune_config,
            adapter,
            fake_anthropic,
        )

    info_records = [
        r
        for r in caplog.records
        if r.name == "signalforge.cli._estimate" and r.levelno == logging.INFO
    ]
    assert len(info_records) == 1
    msg = info_records[0].getMessage()
    for key in (
        "run_id",
        "model_unique_id",
        "drafter_model",
        "grader_model",
        "draft_tokens",
        "grade_tokens",
        "total_llm_usd",
        "total_bytes",
        "duration_seconds",
        "price_table_version",
    ):
        assert f'"{key}"' in msg, f"missing field {key!r} in log message: {msg!r}"
    # Sanity: the JSON tail of the message is parseable.
    json_tail = msg.split("estimate complete: ", 1)[1]
    parsed = json.loads(json_tail)
    assert parsed["model_unique_id"] == model.unique_id
    assert parsed["price_table_version"] == PRICE_TABLE_VERSION


def test_estimate_uses_first_artifact_alphabetical_as_grade_rep(
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
) -> None:
    """The grade-criterion ``count_tokens`` envelopes are rendered
    with the FIRST artifact_id alphabetically.

    Construct a model whose columns sorted alphabetically yield a
    distinctive first column name; assert every per-criterion
    ``count_tokens`` call's dynamic block carries
    ``"column.aaa.description"``.
    """
    model = _make_model(name="customers", columns=("zzz_last", "aaa_first", "mmm_middle"))
    mf = _make_manifest(model)

    n_criteria = len(DEFAULT_RUBRIC)
    fake_anthropic.expect_count_tokens(
        matching=lambda kwargs: True,
        returns=FakeCountTokensResponse(input_tokens=100),
    )
    for _ in range(n_criteria):
        fake_anthropic.expect_count_tokens(
            matching=lambda kwargs: True,
            returns=FakeCountTokensResponse(input_tokens=50),
        )
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=1024)

    estimate(
        model,
        mf,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fake_anthropic,
    )

    # Skip the first count call (drafter) — the rest are grade reps.
    grade_calls = fake_anthropic.count_calls[1:]
    assert len(grade_calls) == n_criteria
    for call in grade_calls:
        # The dynamic block carries ``artifact_id: column.aaa_first.description``
        # in its body. Walk the messages content block to find it.
        messages = call["messages"]
        assert len(messages) == 1
        blocks = messages[0]["content"]
        dynamic_block_text = blocks[1]["text"]
        assert "artifact_id: column.aaa_first.description" in dynamic_block_text


def test_estimate_returns_frozen_estimate_report(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
) -> None:
    """The returned :class:`EstimateReport` is frozen."""
    _queue_count_tokens(fake_anthropic, draft=100, per_criterion=50, n_criteria=len(DEFAULT_RUBRIC))
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=1024)

    report = estimate(
        model,
        manifest,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fake_anthropic,
    )

    assert isinstance(report, EstimateReport)
    with pytest.raises(Exception):  # noqa: B017 (pydantic frozen raises ValidationError; broad on purpose)
        report.draft_usd = 0.0  # type: ignore[misc]
    # CriterionEstimate is also frozen.
    if report.grade_per_criterion:
        with pytest.raises(Exception):  # noqa: B017 (pydantic frozen raises ValidationError; broad on purpose)
            report.grade_per_criterion[0].usd = 0.0  # type: ignore[misc]


def test_estimate_not_exported_on_cli_init() -> None:
    """DEC-010: ``estimate`` is CLI-internal — not re-exported from
    :mod:`signalforge.cli`.
    """
    import signalforge.cli as cli_pkg

    assert not hasattr(cli_pkg, "estimate")
    assert not hasattr(cli_pkg, "EstimateReport")


def test_criterion_estimate_default_output_tokens_is_50() -> None:
    """Default ``estimated_output_tokens_per_call`` is 50 per
    ``_GRADE_OUTPUT_TOKENS_PER_CALL``.
    """
    ce = CriterionEstimate(
        criterion_id="x",
        criterion_text_truncated="x",
        calls=1,
        input_tokens_per_call=100,
        total_input_tokens=100,
        usd=0.01,
    )
    assert ce.estimated_output_tokens_per_call == 50


def test_estimate_report_tests_per_column_heuristic_is_3_point_5(
    model: Model,
    manifest: Manifest,
    draft_config: DraftConfig,
    grade_config: GradeConfig,
    prune_config: PruneConfig,
    adapter: BigQueryAdapter,
    fake_anthropic: FakeAnthropicClient,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-012: the heuristic is 3.5 tests/column."""
    _queue_count_tokens(fake_anthropic, draft=100, per_criterion=50, n_criteria=len(DEFAULT_RUBRIC))
    fake_client.expect_dry_run(sql_matching=r"SELECT", returns_bytes=1024)
    report = estimate(
        model,
        manifest,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fake_anthropic,
    )
    assert report.tests_per_column_heuristic == 3.5
