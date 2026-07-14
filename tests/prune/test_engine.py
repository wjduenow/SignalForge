"""Tests for ``signalforge.prune.engine`` (US-009).

Pins the eleven load-bearing properties of the prune orchestrator across
the full DropReason routing matrix, the trusted-models entry-time
validation (DEC-008), the total-budget short-circuit (DEC-011), and the
fail-closed audit-write semantics (DEC-016). Every test injects a
:class:`tests.warehouse._fake.FakeBigQueryClient` into a real
:class:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter` — the
proven adapter+fake-client pair from US-002 / US-003. No production code
imports the fake.

The DropReason routing matrix (kept-without-evidence is ``decision="kept"``
because the test ships, conservatively, when we cannot evaluate it):

| compile result        | failure_count | trusted? | decision  | reason                       |
| --------------------- | ------------- | -------- | --------- | ---------------------------- |
| _RequiresFutureData   | n/a           | n/a      | dropped   | requires-future-data         |
| SQL string            | 0             | any      | dropped   | always-passes                |
| SQL string            | > 0           | yes      | dropped   | failed-on-known-clean-data   |
| SQL string            | > 0           | no       | kept      | kept                         |
| WarehouseError        | n/a           | n/a      | kept      | kept-without-evidence        |
| budget-exceeded       | n/a           | n/a      | kept      | kept-without-evidence        |
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTestCustomSQL,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestRowCountBetween,
    CandidateTestUniqueCombination,
)
from signalforge.manifest.models import Column, Manifest, Model
from signalforge.prune import engine as engine_module
from signalforge.prune.audit import PruneEvent
from signalforge.prune.config import PruneConfig
from signalforge.prune.engine import _resolve_sample_bucket, prune_tests
from signalforge.prune.errors import (
    PruneAuditRecordTooLargeError,
    PruneAuditWriteError,
    PruneError,
    PruneTrustedModelNotFoundError,
)
from signalforge.warehouse.adapters.bigquery import BigQueryAdapter
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import (
    BytesBilledExceededError,
    MaterialisationFailedError,
    SamplingRequiresPartitionFilterError,
    TableNotFoundError,
    UnknownTableSizeError,
)
from signalforge.warehouse.models import (
    ColumnStats,
    Dialect,
    PartitionFilter,
    TableRef,
    TestResult,
)
from tests.warehouse._fake import FakeBigQueryClient, FakeTable

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orders_model() -> Model:
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
            "id": Column(name="id"),
            "customer_id": Column(name="customer_id"),
            "status": Column(name="status"),
        },
        raw_code="select 1",
    )


def _make_manifest(model: Model) -> Manifest:
    return Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={model.unique_id: model},
    )


def _make_other_model() -> Model:
    """A second model so a multi-table custom_sql ``{{ ref('other_model') }}``
    resolves to a DISTINCT physical table from ``{{ this }}`` — exercising the
    multi-table classifier (DEC-006: multi-table is never sampled).
    """
    return Model(
        unique_id="model.shop.other_model",
        name="other_model",
        resource_type="model",
        package_name="shop",
        original_file_path="models/other_model.sql",
        path="other_model.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"id": Column(name="id"), "customer_id": Column(name="customer_id")},
        raw_code="select 1",
    )


def _make_manifest_with_other(model: Model) -> Manifest:
    """Manifest carrying both the model under prune AND ``other_model`` so a
    multi-table custom_sql test resolves both refs to qualified names.
    """
    other = _make_other_model()
    return Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={model.unique_id: model, other.unique_id: other},
    )


def _make_adapter(fake: FakeBigQueryClient) -> BigQueryAdapter:
    return BigQueryAdapter(
        project="fake_project",
        location="US",
        max_bytes_billed=100_000_000,
        client=fake,
    )


def _candidates_with_one_test(test_anchor_column: str) -> CandidateSchema:
    """Build a CandidateSchema with a single ``not_null`` test on the
    given column.
    """
    return CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name=test_anchor_column,
                description="The order's primary key.",
                tests=(CandidateTestNotNull(column=test_anchor_column),),
            ),
        ),
    )


def _candidates_with_n_tests(n: int) -> CandidateSchema:
    """Build a CandidateSchema with N ``not_null`` tests, all on
    ``customer_id``. Used by the budget test.
    """
    return CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="customer_id",
                description="FK to customers.",
                tests=tuple(CandidateTestNotNull(column="customer_id") for _ in range(n)),
            ),
        ),
    )


def _read_audit_lines(audit_path: Path) -> list[dict[str, Any]]:
    if not audit_path.exists():
        return []
    return [json.loads(line) for line in audit_path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# Routing matrix tests
# ---------------------------------------------------------------------------


def test_prune_tests_always_passes_drops_test(tmp_path: Path) -> None:
    """A test that returns ``failure_count=0`` is dropped with
    ``reason="always-passes"`` and the audit JSONL records it.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 1
    assert result.dropped_count == 1
    assert result.kept_count == 0
    decision = result.decisions[0]
    assert decision.decision == "dropped"
    assert decision.reason == "always-passes"
    assert decision.failures == 0
    assert decision.test_anchor == "column.id"
    fake.assert_all_expectations_met()

    audit_rows = _read_audit_lines(audit_path)
    assert len(audit_rows) == 1
    assert audit_rows[0]["reason"] == "always-passes"
    assert audit_rows[0]["model_unique_id"] == "model.shop.orders"


def test_prune_tests_kept_for_real_failure_untrusted_model(tmp_path: Path) -> None:
    """A test that fails on an untrusted model is kept with
    ``reason="kept"`` and the ``why`` mentions the failure count.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 3}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)  # untrusted by default

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.decision == "kept"
    assert decision.reason == "kept"
    assert decision.failures == 3
    assert "3 failures" in decision.why
    fake.assert_all_expectations_met()


def test_prune_tests_failed_on_known_clean_data_for_trusted_model(tmp_path: Path) -> None:
    """A test that fails on a trusted model is dropped with
    ``reason="failed-on-known-clean-data"`` (presumed buggy).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 7}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(
        scope="full",
        trusted_models=(model.unique_id,),
        capture_failure_rows=0,
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.decision == "dropped"
    assert decision.reason == "failed-on-known-clean-data"
    assert decision.failures == 7
    assert "trusted_models" in decision.why
    fake.assert_all_expectations_met()


def test_prune_tests_requires_future_data_for_unknown_relationships_parent(
    tmp_path: Path,
) -> None:
    """A ``relationships(to="nonexistent_model")`` is dropped with
    ``reason="requires-future-data"`` and NO warehouse call is issued.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Intentionally NO expect_query — any call is unexpected.
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="customer_id",
                description="FK to customers.",
                tests=(
                    CandidateTestRelationships(
                        column="customer_id",
                        to="nonexistent_model",
                        field="id",
                    ),
                ),
            ),
        ),
    )
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.decision == "dropped"
    assert decision.reason == "requires-future-data"
    # `why` carries the sentinel's reason — references the missing parent name.
    assert "nonexistent_model" in decision.why
    # No queries dispatched — verify by asserting all (zero) expectations met.
    fake.assert_all_expectations_met()


def test_prune_tests_custom_sql_unresolvable_ref_requires_future_data(
    tmp_path: Path,
) -> None:
    """US-019 regression: a ``custom_sql`` test referencing
    ``{{ ref('does_not_exist') }}`` no longer crashes ``prune_tests``. The
    compiler catches ``RefNotFoundError`` and returns ``_RequiresFutureData``,
    which the orchestrator routes to ``reason="requires-future-data"`` with
    NO warehouse call (the join makes it multi-table, but resolution fails
    before any SQL is built).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Intentionally NO expect_query — any call is unexpected.
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestCustomSQL(
                sql=(
                    "select o.id from {{ this }} o "
                    "join {{ ref('does_not_exist') }} d on o.id = d.id"
                ),
            ),
        ),
    )
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.decision == "dropped"
    assert decision.reason == "requires-future-data"
    assert "manifest-absent" in decision.why
    # No queries dispatched — resolution failed before any SQL was built.
    fake.assert_all_expectations_met()


def test_prune_tests_custom_sql_ambiguous_ref_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """US-019 regression: a ``custom_sql`` test whose bare ``{{ ref('orders') }}``
    matches two packages raises ``AmbiguousRefError`` — genuine user
    ambiguity, not future data. The compiler routes it to ``_InvalidIdentifier``
    and the orchestrator keeps it ``kept-without-evidence``, never crashing.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    # Two models named ``orders`` in different packages → ambiguous ref.
    other = Model(
        unique_id="model.other.orders",
        name="orders",
        resource_type="model",
        package_name="other",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"id": Column(name="id")},
        raw_code="select 1",
    )
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={model.unique_id: model, other.unique_id: other},
    )
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestCustomSQL(
                sql=("select a.id from {{ this }} a join {{ ref('orders') }} b on a.id = b.id"),
            ),
        ),
    )
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.decision == "kept"
    assert decision.reason == "kept-without-evidence"
    assert "ambiguous" in decision.why
    # No queries dispatched — resolution failed before any SQL was built.
    fake.assert_all_expectations_met()


def test_prune_tests_warehouse_error_routes_to_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """A typed :class:`WarehouseError` from the adapter routes to
    ``kept-without-evidence`` — conservative default keeps the test
    rather than silently dropping it.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(
        matching=r"SELECT COUNT\(\*\)",
        returns=TableNotFoundError(table="fake_project.dataset.orders"),
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.decision == "kept"
    assert decision.reason == "kept-without-evidence"
    assert "TableNotFoundError" in decision.why
    fake.assert_all_expectations_met()


def test_prune_tests_total_budget_exceeded_marks_remaining_kept_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once ``total_budget_seconds * 1000`` ms have elapsed, every
    remaining un-started test drains to ``kept-without-evidence`` with
    a budget-specific ``why`` and NO warehouse call is issued (DEC-011).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Only the first test runs (one expectation).
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(5)
    config = PruneConfig(scope="full", total_budget_seconds=1, capture_failure_rows=0)

    # Stub `_now_monotonic_ms` so the second iteration sees the budget
    # exhausted (advances past 1000 ms after the first call). The clock
    # returns:
    #   call 0 → 0 ms (start_ms)
    #   call 1 → 0 ms (first elapsed-total check, budget not exceeded)
    #   call 2 → 0 ms (test_start_ms for the first test)
    #   call 3 → 10 ms (after first test)
    #   call 4+ → 5000 ms (budget exhausted on iteration 2 onwards)
    timeline = iter([0, 0, 0, 10, 5000, 5000, 5000, 5000, 5000, 5000, 5000])

    def fake_clock() -> int:
        return next(timeline)

    monkeypatch.setattr(engine_module, "_now_monotonic_ms", fake_clock)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 5
    # First test ran (always-passes drop).
    assert result.decisions[0].reason == "always-passes"
    # Remaining four are kept-without-evidence due to budget.
    for decision in result.decisions[1:]:
        assert decision.decision == "kept"
        assert decision.reason == "kept-without-evidence"
        assert "Total prune budget" in decision.why
    # Exactly one warehouse call consumed (the first test's).
    fake.assert_all_expectations_met()


def test_prune_tests_trusted_models_validation_at_entry(tmp_path: Path) -> None:
    """A typo'd ``trusted_models`` unique_id raises
    :class:`PruneTrustedModelNotFoundError` at entry — BEFORE any
    warehouse call is issued (DEC-008).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Intentionally NO expect_query — any call is unexpected. If the
    # validation failed to fire at entry, the orchestrator would
    # dispatch to the fake and the assert_all_expectations_met below
    # would still pass; instead we verify by catching the typed
    # exception (the only path to NO call given the candidate set).
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", trusted_models=("model.shop.nonexistent",))

    with pytest.raises(PruneTrustedModelNotFoundError) as excinfo:
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    assert excinfo.value.unique_id == "model.shop.nonexistent"
    # No warehouse calls: zero expectations were registered, and zero
    # were consumed — assert via the fake's accounting.
    fake.assert_all_expectations_met()
    # No audit JSONL written either — entry-time validation aborts
    # before any decision is built.
    assert not audit_path.exists()


def test_prune_tests_audit_write_oserror_aborts_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An :class:`OSError` from :func:`_write_prune_event` aborts the
    run and surfaces as :class:`PruneAuditWriteError` with the original
    cause attached. NO :class:`PruneResult` is returned (DEC-016).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Three tests; only the first two get to dispatch before the second
    # write blows up.
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(3)
    config = PruneConfig(scope="full", capture_failure_rows=0)

    call_count = {"n": 0}
    boom = OSError("disk full")

    def fake_write(*args: Any, **kwargs: Any) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise boom

    monkeypatch.setattr(engine_module, "_write_prune_event", fake_write)

    with pytest.raises(PruneAuditWriteError) as excinfo:
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    # The OSError surfaces as the cause on the typed wrapper.
    assert excinfo.value.cause is boom
    # __cause__ chain is preserved per ``raise X from cause``.
    assert excinfo.value.__cause__ is boom


def test_prune_tests_audit_record_too_large_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """:class:`PruneAuditRecordTooLargeError` is already a typed
    :class:`PruneError` subclass; the orchestrator propagates it
    UNCHANGED rather than wrapping it as :class:`PruneAuditWriteError`.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    expected = PruneAuditRecordTooLargeError(size=5000, limit=4000)

    def fake_write(*args: Any, **kwargs: Any) -> None:
        raise expected

    monkeypatch.setattr(engine_module, "_write_prune_event", fake_write)

    with pytest.raises(PruneAuditRecordTooLargeError) as excinfo:
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    # Same object — NOT wrapped.
    assert excinfo.value is expected


def test_prune_tests_kept_rate_warning_fires_when_all_tests_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Default ``min_kept_rate_warn=0.0`` fires the WARNING when every
    candidate test is dropped (issue #51) — the "did we lose the whole
    LLM draft?" signal."""
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    assert result.kept_count == 0
    assert result.dropped_count == 1
    matching = [
        r for r in caplog.records if "kept rate at or below configured threshold" in r.getMessage()
    ]
    assert len(matching) == 1
    # Payload is lazy-format JSON (DEC-017); the rendered message
    # carries the structured fields.
    rendered = matching[0].getMessage()
    assert '"model_unique_id": "model.shop.orders"' in rendered
    assert '"total_tests": 1' in rendered
    assert '"kept": 0' in rendered
    assert '"kept_rate": 0.0' in rendered
    assert '"min_kept_rate_warn": 0.0' in rendered


def test_prune_tests_kept_rate_warning_silent_when_at_least_one_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Default ``min_kept_rate_warn=0.0`` does NOT fire when at least one
    candidate is kept (issue #51) — the WARNING is a signal of "every
    test dropped," not a routine end-of-run summary."""
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 3}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    assert result.kept_count == 1
    matching = [
        r for r in caplog.records if "kept rate at or below configured threshold" in r.getMessage()
    ]
    assert matching == []


def test_prune_tests_kept_rate_warning_respects_configured_threshold(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An operator-configured ``min_kept_rate_warn=0.5`` fires when the
    kept rate sits at or below ``0.5`` (issue #51) — not just on
    entirely-empty-kept runs."""
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Two candidates: first passes (dropped: always-passes); second
    # fails (kept). Kept rate = 0.5.
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 5}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="id",
                description="Primary key.",
                tests=(CandidateTestNotNull(column="id"),),
            ),
            CandidateColumn(
                name="customer_id",
                description="FK to customers.",
                tests=(CandidateTestNotNull(column="customer_id"),),
            ),
        ),
    )
    config = PruneConfig(
        scope="full",
        capture_failure_rows=0,
        min_kept_rate_warn=0.5,
    )

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    assert result.kept_count == 1
    assert result.dropped_count == 1
    matching = [
        r for r in caplog.records if "kept rate at or below configured threshold" in r.getMessage()
    ]
    assert len(matching) == 1
    rendered = matching[0].getMessage()
    assert '"kept_rate": 0.5' in rendered
    assert '"min_kept_rate_warn": 0.5' in rendered


def test_prune_tests_kept_rate_warning_silent_on_empty_candidate_set(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty candidate set is its own degenerate signal (the drafter
    produced nothing) and does NOT fire the kept-rate WARNING (issue #51).

    Skipping ``total == 0`` also avoids ``ZeroDivisionError`` on the
    ``kept / total`` computation."""
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    empty = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
    )
    config = PruneConfig(scope="full", capture_failure_rows=0)

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            empty,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    assert result.total_tests == 0
    matching = [
        r for r in caplog.records if "kept rate at or below configured threshold" in r.getMessage()
    ]
    assert matching == []


def test_prune_tests_empty_candidate_skips_warehouse_on_materialised_sample(
    tmp_path: Path,
) -> None:
    """An empty candidate set on the DEFAULT ``materialised`` + ``sample``
    path must NOT contact the warehouse (issue #105 ``prune-existing``
    all-unsupported case).

    Before the empty-candidate short-circuit, ``prune_tests`` entered
    ``with adapter:`` and issued a real ``materialise_sample`` (a
    ``CREATE TEMP TABLE ... AS SELECT``) to sample for ZERO tests —
    incurring warehouse cost for no signal. The fake has NO
    ``expect_materialise_sample`` / ``expect_get_table`` / ``expect_query``
    queued, so any warehouse call would raise an ``AssertionError`` for an
    unexpected query. A clean return with an empty ``PruneResult`` proves
    no warehouse contact happened.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    empty = CandidateSchema(name="orders", description="Order events.", columns=())
    # The default cost-optimisation path that would otherwise materialise.
    config = PruneConfig(scope="sample", sample_strategy="materialised")

    result = prune_tests(
        model,
        adapter,
        empty,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 0
    assert result.decisions == ()
    # No warehouse expectations were queued; assert none were consumed.
    fake.assert_all_expectations_met()
    # Fail-closed audit invariant holds trivially: zero decisions → no file
    # (or an empty one) — never a partial/garbage record.
    assert not audit_path.exists() or audit_path.read_text() == ""


def test_prune_tests_kept_rate_warning_fires_on_disabled_short_circuit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The kept-rate WARNING is wired into the ``enabled=False`` early-return
    site too (not just the normal completion path) — every candidate drains
    to ``kept-without-evidence`` (``decision="kept"``), so a configured
    ``min_kept_rate_warn=1.0`` MUST fire because ``kept_rate == 1.0 <= 1.0``.

    Locks in the "all three return paths" contract for issue #51 (CodeRabbit
    follow-up): disabled short-circuit, materialisation-failure, and normal
    completion all run through ``_maybe_emit_kept_rate_warning``.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(3)
    config = PruneConfig(enabled=False, min_kept_rate_warn=1.0)

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    # All candidates routed to kept-without-evidence; kept_rate == 1.0.
    assert result.total_tests == 3
    assert result.kept_count == 3
    matching = [
        r for r in caplog.records if "kept rate at or below configured threshold" in r.getMessage()
    ]
    assert len(matching) == 1
    rendered = matching[0].getMessage()
    assert '"model_unique_id": "model.shop.orders"' in rendered
    assert '"total_tests": 3' in rendered
    assert '"kept": 3' in rendered
    assert '"kept_rate": 1.0' in rendered
    assert '"min_kept_rate_warn": 1.0' in rendered


def test_prune_tests_kept_rate_warning_fires_on_materialisation_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The kept-rate WARNING fires on the materialisation-failure early-return
    site (issue #51, CodeRabbit follow-up). Every candidate routes to
    ``kept-without-evidence`` per DEC-009 of issue #22; with
    ``min_kept_rate_warn=1.0`` the helper fires because ``kept_rate == 1.0``.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=MaterialisationFailedError("simulated quota error"),
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(4)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
        min_kept_rate_warn=1.0,
    )

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    assert result.total_tests == 4
    assert result.kept_count == 4
    matching = [
        r for r in caplog.records if "kept rate at or below configured threshold" in r.getMessage()
    ]
    assert len(matching) == 1
    rendered = matching[0].getMessage()
    assert '"model_unique_id": "model.shop.orders"' in rendered
    assert '"total_tests": 4' in rendered
    assert '"kept": 4' in rendered
    assert '"kept_rate": 1.0' in rendered
    assert '"min_kept_rate_warn": 1.0' in rendered
    fake.assert_all_expectations_met()


def test_prune_tests_module_level_sleep_alias_is_reassignable() -> None:
    """:data:`signalforge.prune.engine._sleep` exists, IS callable, AND
    can be reassigned to a recording stub (DEC-019). Mirrors
    :data:`signalforge.llm.client._sleep` — the alias is reserved for
    future budget-loop work; this test pins the seam.
    """
    # Exists and is callable.
    assert callable(engine_module._sleep)

    # Reassignable: a recording stub replaces the alias and is
    # observable via the module attribute (mirrors how production code
    # would dispatch).
    calls: list[float] = []

    def recording_sleep(seconds: float) -> None:
        calls.append(seconds)

    original = engine_module._sleep
    try:
        engine_module._sleep = recording_sleep  # type: ignore[assignment]
        engine_module._sleep(0.5)
        assert calls == [0.5]
    finally:
        engine_module._sleep = original  # type: ignore[assignment]


def test_prune_tests_compute_config_hash_is_deterministic(tmp_path: Path) -> None:
    """Two calls with identical :class:`PruneConfig` produce identical
    ``config_hash`` in the audit JSONL.
    """
    audit_a = tmp_path / "prune_a.jsonl"
    audit_b = tmp_path / "prune_b.jsonl"

    fake_a = FakeBigQueryClient(project="fake_project")
    fake_a.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter_a = _make_adapter(fake_a)

    fake_b = FakeBigQueryClient(project="fake_project")
    fake_b.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter_b = _make_adapter(fake_b)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    prune_tests(
        model,
        adapter_a,
        candidates,
        manifest,
        config=config,
        audit_path=audit_a,
        project_dir=tmp_path,
    )
    prune_tests(
        model,
        adapter_b,
        candidates,
        manifest,
        config=config,
        audit_path=audit_b,
        project_dir=tmp_path,
    )

    rows_a = _read_audit_lines(audit_a)
    rows_b = _read_audit_lines(audit_b)
    assert len(rows_a) == 1 and len(rows_b) == 1
    assert rows_a[0]["config_hash"] == rows_b[0]["config_hash"]
    # 16-hex-char convention (same as policy_hash / config_hash elsewhere).
    assert len(rows_a[0]["config_hash"]) == 16


# ---------------------------------------------------------------------------
# QG fix-up: audit-path symlink-hardening (DEC-016) and project_dir-relative
# default resolution (mirrors the safety/draft layers' fix).
# ---------------------------------------------------------------------------


def test_prune_tests_default_audit_path_resolves_relative_to_project_dir(
    tmp_path: Path,
) -> None:
    """``audit_path=None`` resolves to
    ``<project_dir>/.signalforge/prune.jsonl`` (NOT cwd-relative).

    Regression guard for the same defect the safety + draft layers
    fixed: when the CLI is invoked from a sub-directory, the audit
    lands next to the project, not next to wherever the user happened
    to be.
    """
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        project_dir=tmp_path,
    )

    expected_audit = tmp_path / ".signalforge" / "prune.jsonl"
    assert expected_audit.exists()
    rows = _read_audit_lines(expected_audit)
    assert len(rows) == 1
    assert rows[0]["model_unique_id"] == "model.shop.orders"
    fake.assert_all_expectations_met()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink semantics differ on Windows")
def test_prune_tests_audit_path_symlink_outside_project_raises(
    tmp_path: Path,
) -> None:
    """A symlinked ``audit_path`` whose target escapes ``project_dir``
    raises :class:`PruneAuditWriteError` BEFORE any write hits disk
    (DEC-016).

    Defence-in-depth: a malicious or misconfigured
    ``.signalforge/prune.jsonl`` symlink could redirect writes to
    ``/etc/passwd`` or any other attacker-controlled location. The
    canonicalisation gate at orchestrator entry catches this and
    surfaces a typed prune error rather than a raw OSError.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    audit_dir = project_dir / ".signalforge"
    audit_dir.mkdir()

    outside_target = tmp_path / "outside" / "evil.jsonl"
    outside_target.parent.mkdir()
    # Symlink the audit file to a target outside the project tree.
    audit_symlink = audit_dir / "prune.jsonl"
    audit_symlink.symlink_to(outside_target)

    fake = FakeBigQueryClient(project="fake_project")
    # No expectations registered — the canonicalisation gate must fire
    # before any warehouse call.
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    with pytest.raises(PruneAuditWriteError):
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_symlink,
            project_dir=project_dir,
        )

    # The outside target was never created — canonicalisation aborts
    # before the writer opens any file.
    assert not outside_target.exists()
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# PR #20 review fix: sampling / partition_filter wiring.
#
# Pre-fix behavior: ``decision.scope = config.scope`` was advisory-only —
# every test ran against the FULL table regardless of ``prune.scope``.
# These tests pin the post-fix wiring: scope, sample_size, and
# partition_filter all reach the compiled SQL.
# ---------------------------------------------------------------------------


def test_prune_tests_sample_mode_wraps_sql_with_deterministic_sample_cte(
    tmp_path: Path,
) -> None:
    """``config.scope="sample"`` wraps the failing-rows test in a
    deterministic-sample CTE matching the warehouse adapter's
    :meth:`sample_rows` shape.

    The bucket is derived from ``num_rows / sample_size``. With
    ``num_rows=1_000_000`` and ``sample_size=100_000`` the bucket is 10.
    The compiled SQL the orchestrator dispatches must carry the CTE
    AND the hash-mod predicate.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Engine fetches num_rows once before the test loop.
    fake.expect_get_table(
        ref=TableRef(project="fake_project", dataset="dataset", name="orders"),
        returns=FakeTable(num_rows=1_000_000),
    )
    fake.expect_query(
        matching=(
            r"WITH sample AS \(SELECT \* FROM `fake_project\.dataset\.orders` "
            r"AS t WHERE MOD\(ABS\(FARM_FINGERPRINT\(TO_JSON_STRING\(t\)\)\), "
            r"10\) < 1 LIMIT 100000\)"
        ),
        returns=[{"failures": 0}],
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="oneshot",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 1
    assert result.decisions[0].decision == "dropped"
    assert result.decisions[0].reason == "always-passes"
    fake.assert_all_expectations_met()


def test_prune_tests_full_mode_does_not_wrap_with_cte(tmp_path: Path) -> None:
    """``config.scope="full"`` (the default for this test) emits the
    unwrapped failing-rows SELECT — no ``WITH sample`` CTE, no
    deterministic-sample predicate, no ``get_table`` lookup.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Note: NO expect_get_table — full-mode skips num_rows lookup.
    # The query expectation is anchored on a regex that REJECTS any
    # CTE wrapping by requiring the SELECT to start without ``WITH``.
    fake.expect_query(
        matching=(
            r"^SELECT COUNT\(\*\) AS failures FROM "
            r"\(SELECT `id` FROM `fake_project\.dataset\.orders` "
            r"WHERE `id` IS NULL\)"
        ),
        returns=[{"failures": 0}],
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )
    fake.assert_all_expectations_met()


def test_prune_tests_partition_filter_threads_through(tmp_path: Path) -> None:
    """``config.partition_filter`` reaches the compiled SQL — verifies
    the orchestrator threads the typed :class:`PartitionFilter` through
    :func:`_compile_test`. Sample-mode + partition_filter renders both
    predicates inside the deterministic-sample CTE.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_get_table(
        ref=TableRef(project="fake_project", dataset="dataset", name="orders"),
        returns=FakeTable(num_rows=1_000_000),
    )
    fake.expect_query(
        matching=(
            r"MOD\(ABS\(FARM_FINGERPRINT\(TO_JSON_STRING\(t\)\)\), 10\) < 1 "
            r"AND `dt` >= '2026-01-01'"
        ),
        returns=[{"failures": 0}],
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        partition_filter=PartitionFilter(column="dt", op=">=", value="2026-01-01"),
        sample_strategy="oneshot",
    )

    prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )
    fake.assert_all_expectations_met()


def test_prune_tests_sample_mode_relationships_samples_child_only(
    tmp_path: Path,
) -> None:
    """Sample-mode + relationships samples the CHILD only; the parent
    table stays at full.

    Documented asymmetry — an orphan detected in the child sample is not
    a false positive caused by the parent's missing-from-sample row.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_get_table(
        ref=TableRef(project="fake_project", dataset="dataset", name="orders"),
        returns=FakeTable(num_rows=1_000_000),
    )
    # The query carries a sample CTE wrapping the child AND a LEFT JOIN
    # against the full-qualified parent table (NOT another sample alias).
    fake.expect_query(
        matching=(
            r"WITH sample AS .* SELECT child\.`customer_id` "
            r"FROM sample AS child "
            r"LEFT JOIN `fake_project\.dataset\.customers` AS parent"
        ),
        returns=[{"failures": 0}],
    )
    adapter = _make_adapter(fake)

    # Build a manifest with both the orders model AND a customers parent.
    orders = _make_orders_model()
    customers = Model(
        unique_id="model.shop.customers",
        name="customers",
        resource_type="model",
        package_name="shop",
        original_file_path="models/customers.sql",
        path="customers.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"id": Column(name="id")},
        raw_code="select 1",
    )
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={
            orders.unique_id: orders,
            customers.unique_id: customers,
        },
    )
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="customer_id",
                description="FK to customers.",
                tests=(
                    CandidateTestRelationships(
                        column="customer_id",
                        to="customers",
                        field="id",
                    ),
                ),
            ),
        ),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="oneshot",
    )

    prune_tests(
        orders,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )
    fake.assert_all_expectations_met()


def test_prune_tests_sample_mode_unknown_num_rows_raises_prune_error(
    tmp_path: Path,
) -> None:
    """Sample-mode requires ``Table.num_rows`` to size the deterministic
    bucket. When the warehouse returns ``None`` the orchestrator raises
    a typed :class:`PruneError` — silent degradation to "every row"
    would defeat US-003's cost model.
    """
    from signalforge.prune.errors import PruneError

    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_get_table(
        ref=TableRef(project="fake_project", dataset="dataset", name="orders"),
        returns=FakeTable(num_rows=None),
    )
    # No expect_query — the failure happens BEFORE any test dispatches.
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="oneshot",
    )

    with pytest.raises(PruneError):
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )
    fake.assert_all_expectations_met()


class _RowCountOnlyAdapter(WarehouseAdapter):
    """A non-BigQuery adapter that implements ONLY the vendor-neutral
    ``get_row_count`` seam and deliberately exposes NO ``_get_client``.

    Regression guard for issue #140: before the fix,
    :func:`_resolve_sample_bucket` reached for ``getattr(adapter,
    "_get_client")`` and raised on any adapter (e.g. Snowflake) that did
    not expose that BigQuery-internal seam. This stub proves the engine
    now sizes the bucket through ``get_row_count`` alone.
    """

    def __init__(self, num_rows: int | None) -> None:
        self._num_rows = num_rows
        self.get_row_count_calls: list[TableRef] = []

    def __enter__(self) -> WarehouseAdapter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def dialect(self) -> Dialect:
        from signalforge.warehouse.models import SNOWFLAKE_DIALECT

        return SNOWFLAKE_DIALECT

    def sample_rows(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("not exercised")

    def column_stats(self, table: TableRef, column: str) -> ColumnStats:
        raise NotImplementedError("not exercised")

    def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> TestResult:
        raise NotImplementedError("not exercised")

    def get_row_count(self, table: TableRef) -> int | None:
        self.get_row_count_calls.append(table)
        return self._num_rows


def test_resolve_sample_bucket_uses_vendor_neutral_get_row_count() -> None:
    """``_resolve_sample_bucket`` sizes the bucket through the vendor-neutral
    ``get_row_count`` seam — NOT a BigQuery-only ``_get_client`` (issue #140).

    The stub adapter has no ``_get_client`` attribute at all; before #140
    this raised ``PruneError`` on any non-BigQuery adapter. The bucket is
    ``max(num_rows // sample_size, 1)`` mirroring the adapter's own
    ``sample_rows`` derivation.
    """
    adapter = _RowCountOnlyAdapter(num_rows=1_000_000)
    assert not hasattr(adapter, "_get_client")  # the exact crack #140 removed
    table_ref = TableRef(project="fake_project", dataset="dataset", name="orders")

    bucket = _resolve_sample_bucket(
        adapter=adapter,
        table_ref=table_ref,
        scope="sample",
        sample_size=1000,
    )

    assert bucket == 1000
    assert adapter.get_row_count_calls == [table_ref]


def test_resolve_sample_bucket_full_scope_skips_row_count_lookup() -> None:
    """``scope="full"`` returns ``None`` and never calls ``get_row_count`` —
    full-scan prune does no deterministic-sample sizing (issue #140)."""
    adapter = _RowCountOnlyAdapter(num_rows=1_000_000)
    table_ref = TableRef(project="fake_project", dataset="dataset", name="orders")

    bucket = _resolve_sample_bucket(
        adapter=adapter,
        table_ref=table_ref,
        scope="full",
        sample_size=1000,
    )

    assert bucket is None
    assert adapter.get_row_count_calls == []


def test_resolve_sample_bucket_unknown_count_raises_prune_error() -> None:
    """An unknown row count (``None``) from the seam fails loud with a
    :class:`PruneError` rather than silently degrading to "every row"
    (issue #140 preserves the pre-existing fail-loud cost-model guard)."""
    adapter = _RowCountOnlyAdapter(num_rows=None)
    table_ref = TableRef(project="fake_project", dataset="dataset", name="orders")

    with pytest.raises(PruneError):
        _resolve_sample_bucket(
            adapter=adapter,
            table_ref=table_ref,
            scope="sample",
            sample_size=1000,
        )


# ---------------------------------------------------------------------------
# US-005 of issue #22 — sample_strategy dispatch + conservative routing.
#
# 15 new tests covering:
#   * dispatch on PruneConfig.sample_strategy ("materialised" calls
#     adapter.materialise_sample once before the per-test loop;
#     "oneshot" preserves the v0.1 path),
#   * compiled SQL references the materialised _SESSION temp table,
#   * materialisation failure routes ALL tests to kept-without-evidence
#     with the DEC-005 ``why`` shape and emits ONE DEC-009 WARNING
#     before the per-test audit writes,
#   * total budget includes materialisation (DEC-010 of #22),
#   * the orchestrator wraps the adapter in ``with`` so __exit__ fires.
# ---------------------------------------------------------------------------


def _make_materialised_ref(name: str = "_sf_sample_deadbeefcafe1234") -> TableRef:
    """Build a deterministic :class:`TableRef` with ``dataset="_SESSION"``
    that mirrors the adapter's ``materialise_sample`` return shape.
    """
    return TableRef(project="fake_project", dataset="_SESSION", name=name)


def test_prune_tests_with_materialised_strategy_calls_materialise_sample_once(
    tmp_path: Path,
) -> None:
    """``sample_strategy="materialised"`` calls
    :meth:`adapter.materialise_sample` exactly once BEFORE the per-test
    loop; the per-test queries route into the active session.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=materialised_ref,
    )
    # The single per-test query consumes the registered count expectation.
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    # __exit__ aborts the active session.
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 1
    # The materialise expectation MUST have been consumed — and exactly
    # once — for ``assert_all_expectations_met`` to pass.
    fake.assert_all_expectations_met()


def test_prune_tests_with_oneshot_strategy_skips_materialise_sample(
    tmp_path: Path,
) -> None:
    """``sample_strategy="oneshot"`` preserves the v0.1 path: NO call to
    ``adapter.materialise_sample`` is issued. The deterministic-sample
    CTE wraps every per-test failing-rows query as before.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_get_table(
        ref=TableRef(project="fake_project", dataset="dataset", name="orders"),
        returns=FakeTable(num_rows=1_000_000),
    )
    # The v0.1 path wraps the per-test SQL in ``WITH sample AS (SELECT *
    # FROM <source> ...)`` — NO ``CREATE TEMP TABLE`` is dispatched, so
    # the absence of an ``expect_materialise_sample`` registration is
    # itself the assertion.
    fake.expect_query(
        matching=(
            r"WITH sample AS \(SELECT \* FROM `fake_project\.dataset\.orders` "
            r"AS t WHERE MOD\(ABS\(FARM_FINGERPRINT\(TO_JSON_STRING\(t\)\)\), "
            r"10\) < 1 LIMIT 100000\)"
        ),
        returns=[{"failures": 0}],
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="oneshot",
    )

    prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )
    # NO materialise / abort expectations registered — a stray call would
    # raise the standard ``unexpected materialise_sample: ...`` shape.
    fake.assert_all_expectations_met()


def test_prune_tests_compiled_sql_references_temp_table_under_materialised(
    tmp_path: Path,
) -> None:
    """Under ``materialised`` strategy, every decision's ``compiled_sql``
    references ``_SESSION._sf_sample_<run_id>`` rather than the source
    production table.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=materialised_ref,
    )
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    # Production derives the temp-table name from
    # ``_compute_run_id(table, n, partition_filter)`` (DEC-001 of
    # issue #22) — the fake's ``returns=`` TableRef is informational
    # only, NOT the source of the actual temp-table name. Pin the
    # ``_SESSION._sf_sample_<16-hex>`` shape rather than the specific
    # name.
    assert "_SESSION._sf_sample_" in decision.compiled_sql
    assert re.search(r"_sf_sample_[0-9a-f]{16}", decision.compiled_sql) is not None
    # The source production table MUST NOT appear in the compiled SQL —
    # the whole point of materialisation is amortised cost via the temp
    # table.
    assert "`fake_project.dataset.orders`" not in decision.compiled_sql
    fake.assert_all_expectations_met()


def test_prune_tests_compiled_sql_hash_is_deterministic_under_materialised(
    tmp_path: Path,
) -> None:
    """Two runs with identical ``(model, candidates, config)`` produce
    byte-equal ``compiled_sql_hash`` (DEC-001 of issue #22 — the
    deterministic ``run_id`` keeps the temp-table name byte-identical
    across runs, which keeps the per-test compiled SQL byte-identical).
    """

    def _run(suffix: str) -> str:
        audit_path = tmp_path / f"prune_{suffix}.jsonl"
        fake = FakeBigQueryClient(project="fake_project")
        source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
        materialised_ref = _make_materialised_ref()
        fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
        fake.expect_materialise_sample(
            source_ref,
            sample_size=100_000,
            returns=materialised_ref,
        )
        fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
        fake.expect_abort_session(f"sess_{materialised_ref.name}")
        adapter = _make_adapter(fake)

        model = _make_orders_model()
        manifest = _make_manifest(model)
        candidates = _candidates_with_one_test("id")
        config = PruneConfig(
            scope="sample",
            sample_size=100_000,
            capture_failure_rows=0,
            sample_strategy="materialised",
        )

        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )
        fake.assert_all_expectations_met()
        return result.decisions[0].compiled_sql_hash

    hash_a = _run("a")
    hash_b = _run("b")
    assert hash_a == hash_b
    assert len(hash_a) == 16  # 16-hex blake2b-8 convention


def test_prune_tests_materialisation_failed_routes_all_to_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """When ``adapter.materialise_sample`` raises
    :class:`MaterialisationFailedError`, EVERY candidate test routes to
    ``decision="kept", reason="kept-without-evidence"`` with the DEC-005
    ``why`` shape.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=MaterialisationFailedError("boom from BQ"),
    )
    # No expect_query / expect_abort_session — the per-test loop never
    # runs and __exit__ short-circuits because no session was minted.
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(4)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 4
    assert result.kept_count == 4
    assert result.dropped_count == 0
    for decision in result.decisions:
        assert decision.decision == "kept"
        assert decision.reason == "kept-without-evidence"
        # DEC-005 ``why`` shape: prefix + class name + colon + truncated
        # message.
        assert decision.why.startswith("sample materialisation failed: ")
        assert "MaterialisationFailedError" in decision.why
        assert "boom from BQ" in decision.why
        assert decision.compiled_sql == ""
        assert decision.elapsed_ms == 0
    fake.assert_all_expectations_met()


def test_prune_tests_unknown_table_size_routes_all_to_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """When ``adapter.materialise_sample`` raises
    :class:`UnknownTableSizeError` (any :class:`WarehouseError` subclass),
    the conservative-bias rule still routes ALL tests to
    ``kept-without-evidence`` (DEC-009 of issue #22 generalises across
    the WarehouseError hierarchy).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=UnknownTableSizeError(table=source_ref.qualified_name),
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(3)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 3
    assert result.kept_count == 3
    for decision in result.decisions:
        assert decision.decision == "kept"
        assert decision.reason == "kept-without-evidence"
        assert decision.why.startswith("sample materialisation failed: ")
        # The BigQueryAdapter wraps every materialise failure (whether
        # the original was an :class:`UnknownTableSizeError`, an
        # :class:`InvalidIdentifierError`, or anything else) into a
        # :class:`MaterialisationFailedError` (DEC-008 of issue #22) —
        # the orchestrator's ``why`` shape carries the WRAPPED class
        # name, but the inner failure's truncated message still
        # surfaces in the str(...) tail so a reviewer can correlate.
        assert "MaterialisationFailedError" in decision.why
        # The inner ``UnknownTableSizeError`` message ("unknown num_rows")
        # survives in the truncated str(...) so a reviewer can correlate
        # the wrapped warning with the real cause.
        assert "unknown num_rows" in decision.why
    fake.assert_all_expectations_met()


def test_prune_tests_materialisation_failure_writes_one_audit_per_test(
    tmp_path: Path,
) -> None:
    """N candidate tests → N PruneEvent JSONL lines on the materialisation-
    failure path. Fail-closed audit (DEC-016 of #6) is preserved even
    when the per-test loop never runs.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=MaterialisationFailedError("network blip"),
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(5)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    audit_rows = _read_audit_lines(audit_path)
    assert len(audit_rows) == 5
    for row in audit_rows:
        assert row["decision"] == "kept"
        assert row["reason"] == "kept-without-evidence"
        assert row["why"].startswith("sample materialisation failed: ")
        assert row["model_unique_id"] == "model.shop.orders"
    fake.assert_all_expectations_met()


def test_prune_tests_total_budget_includes_materialisation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEC-010 of issue #22 — the total-budget watchdog ticks across
    BOTH the materialisation phase AND the per-test loop.

    Stub ``_now_monotonic_ms`` so:
      * call 0 → 0 ms (start_ms)
      * later calls → 5000 ms (already past 1s budget by the time the
        per-test loop checks elapsed_total).
    Materialisation succeeded (this test does not inject a failure
    there); but every test in the per-test loop sees the budget
    exhausted and routes to kept-without-evidence with the
    ``Total prune budget`` ``why`` shape.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=materialised_ref,
    )
    # Note: NO expect_query — every per-test dispatch is short-circuited
    # by the budget gate before the warehouse call. The abort still fires
    # at __exit__ time.
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(3)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        total_budget_seconds=1,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    # ``start_ms`` snapshot at 0, then every later call returns 5000 so
    # the per-test loop's first elapsed_total check sees the budget
    # already exhausted. The watchdog is checked BEFORE the per-test
    # warehouse call, so no expect_query is needed.
    timeline_iter = iter([0] + [5000] * 50)

    def fake_clock() -> int:
        return next(timeline_iter)

    monkeypatch.setattr(engine_module, "_now_monotonic_ms", fake_clock)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 3
    for decision in result.decisions:
        assert decision.decision == "kept"
        assert decision.reason == "kept-without-evidence"
        assert "Total prune budget" in decision.why
    fake.assert_all_expectations_met()


class _RecordingAdapterWrapper:
    """Wraps a :class:`BigQueryAdapter` to record __enter__/__exit__
    invocation counts. The orchestrator must call BOTH so DEC-013 of
    #22 cleanup (CALL BQ.ABORT_SESSION via ``__exit__``) ever fires.

    Forwards every other attribute to the underlying adapter so the
    production code path stays unchanged.
    """

    def __init__(self, inner: BigQueryAdapter) -> None:
        self._inner = inner
        self.enter_calls: int = 0
        self.exit_calls: int = 0

    def __enter__(self) -> Any:
        self.enter_calls += 1
        return self._inner.__enter__()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.exit_calls += 1
        self._inner.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_prune_tests_uses_adapter_as_context_manager(tmp_path: Path) -> None:
    """``prune_tests`` invokes ``adapter`` inside a ``with`` block so
    :meth:`WarehouseAdapter.__exit__` always runs (DEC-013 of #22 —
    explicit ``CALL BQ.ABORT_SESSION();`` cleanup). Without the
    ``with`` wrap, US-003's cleanup work is unreachable from the
    orchestrator.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    inner = _make_adapter(fake)
    wrapper = _RecordingAdapterWrapper(inner)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    prune_tests(
        model,
        wrapper,  # type: ignore[arg-type]
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert wrapper.enter_calls == 1
    assert wrapper.exit_calls == 1
    fake.assert_all_expectations_met()


def test_prune_tests_adapter_exit_fires_after_normal_completion(
    tmp_path: Path,
) -> None:
    """Exactly one ``__exit__`` invocation after a successful materialised
    run completes. Pin against accidental ``with`` removal.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=materialised_ref,
    )
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    inner = _make_adapter(fake)
    wrapper = _RecordingAdapterWrapper(inner)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    prune_tests(
        model,
        wrapper,  # type: ignore[arg-type]
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert wrapper.enter_calls == 1
    assert wrapper.exit_calls == 1
    fake.assert_all_expectations_met()


def test_prune_tests_adapter_exit_fires_after_materialisation_failure(
    tmp_path: Path,
) -> None:
    """``__exit__`` fires even on the materialisation-failure path that
    routes every test to ``kept-without-evidence``. Cleanup work runs
    on every exit path, not just the happy-path one.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=MaterialisationFailedError("simulated quota error"),
    )
    inner = _make_adapter(fake)
    wrapper = _RecordingAdapterWrapper(inner)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(2)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    prune_tests(
        model,
        wrapper,  # type: ignore[arg-type]
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    # ``__enter__`` and ``__exit__`` BOTH ran exactly once even though
    # the materialisation phase raised inside the ``with`` block.
    assert wrapper.enter_calls == 1
    assert wrapper.exit_calls == 1
    fake.assert_all_expectations_met()


def test_prune_tests_materialisation_failure_emits_orchestrator_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEC-009 of issue #22 — exactly ONE WARNING fires from
    :mod:`signalforge.prune.engine` on the materialisation-failure
    path, with the canonical JSON payload. Distinct from the per-decision
    ``why`` field (in-band signal) AND from the cleanup-failure WARNING
    DEC-014 (which fires from the warehouse layer, not the prune layer).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=MaterialisationFailedError("auth blew up"),
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(3)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    matching = [
        record
        for record in caplog.records
        if record.name == "signalforge.prune.engine"
        and "materialisation failed" in record.getMessage()
        and "routing all tests" in record.getMessage()
    ]
    assert len(matching) == 1
    record = matching[0]
    assert record.levelname == "WARNING"
    # The JSON payload sits in the ``%s`` slot — a lazy-format args
    # tuple per DEC-017. Parse it and pin every key.
    assert record.args is not None
    raw = record.args[0] if isinstance(record.args, tuple) else record.args
    assert isinstance(raw, str)
    payload = json.loads(raw)
    assert payload["model_unique_id"] == "model.shop.orders"
    assert payload["candidate_count"] == 3
    assert payload["error_class"] == "MaterialisationFailedError"
    # The original ``"auth blew up"`` message survives inside the
    # truncated ``str(exc)[:200]`` payload — the production adapter
    # re-wraps every materialisation failure once, prefixing with the
    # source table identifier; the inner exception's message lives at
    # the tail of the wrapped str(...) output.
    assert "auth blew up" in payload["error_message"]
    # The truncation cap holds — never wider than 200 chars.
    assert len(payload["error_message"]) <= 200


def test_prune_tests_orchestrator_warning_fires_before_per_test_audit_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The DEC-009 WARNING fires ONCE at the head of the failure path,
    BEFORE the N JSONL audit lines for the kept-without-evidence
    decisions. Pin the log-record-vs-audit-write ordering so a future
    refactor can't accidentally interleave them.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=MaterialisationFailedError("oh no"),
    )
    adapter = _make_adapter(fake)

    # Capture the chronological order of (warning, audit-write) events.
    ordering: list[str] = []

    original_warning = engine_module._LOGGER.warning

    def recording_warning(msg: str, *args: Any, **kwargs: Any) -> None:
        ordering.append("warning")
        original_warning(msg, *args, **kwargs)

    monkeypatch.setattr(engine_module._LOGGER, "warning", recording_warning)

    original_write = engine_module._write_prune_event

    def recording_write(*args: Any, **kwargs: Any) -> None:
        ordering.append("write")
        original_write(*args, **kwargs)

    monkeypatch.setattr(engine_module, "_write_prune_event", recording_write)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(4)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    # The warning must come FIRST. After that, exactly N writes follow.
    assert ordering[0] == "warning"
    assert ordering[1:] == ["write"] * 4


def test_prune_tests_budget_exhausted_during_materialisation_marks_all_kept_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_sleep`` reassignment isn't usable here (the orchestrator's
    happy-path doesn't sleep), but ``_now_monotonic_ms`` is the
    deterministic stand-in. Drive the clock so the budget trips
    AFTER materialisation succeeds but BEFORE the per-test loop's
    first iteration — every test then routes to kept-without-evidence
    with the budget ``why`` shape.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=materialised_ref,
    )
    # No expect_query — the budget watchdog short-circuits BEFORE the
    # first per-test warehouse call.
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(3)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        total_budget_seconds=1,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    # Clock returns 0 once (start_ms) then 5000 ms forever — past the
    # 1s budget by the time the per-test loop checks elapsed.
    timeline_iter = iter([0] + [5000] * 50)

    def fake_clock() -> int:
        return next(timeline_iter)

    monkeypatch.setattr(engine_module, "_now_monotonic_ms", fake_clock)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 3
    for decision in result.decisions:
        assert decision.decision == "kept"
        assert decision.reason == "kept-without-evidence"
        # Budget-exhausted ``why`` shape, NOT the materialisation-failed
        # shape — the materialisation succeeded; the budget tripped
        # afterwards.
        assert "Total prune budget" in decision.why
        assert "materialisation" not in decision.why
    fake.assert_all_expectations_met()


def test_prune_tests_materialised_strategy_against_pinned_fixture(
    tmp_path: Path,
) -> None:
    """End-to-end snapshot: a known ``(model, candidates, config)`` under
    ``materialised`` strategy produces audit JSONL whose per-row shape
    aligns with the committed fixture's ``materialised``-mode entry.

    The fixture has illustrative values; the snapshot here pins the
    runtime invariants:

      * every per-decision row carries the materialised
        ``compiled_sql`` (references ``_SESSION._sf_sample_*``),
      * ``decision.scope == "sample"`` (the user-facing config value,
        NOT the compiler's effective ``"full"``),
      * the JSONL is well-formed and matches the
        :class:`StrictPruneEvent` shape via the per-row drift detector.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=materialised_ref,
    )
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    audit_rows = _read_audit_lines(audit_path)
    assert len(audit_rows) == 1
    row = audit_rows[0]
    # End-to-end snapshot invariants (load-bearing rather than byte-equal):
    assert row["scope"] == "sample"
    assert row["model_unique_id"] == "model.shop.orders"
    assert "_SESSION" in row["compiled_sql"]
    assert re.search(r"_sf_sample_[0-9a-f]{16}", row["compiled_sql"]) is not None
    assert row["audit_schema_version"] == 4

    # Cross-check against the strict drift-detector mirror so the
    # in-memory snapshot remains valid against the read-back contract.
    from tests.prune.test_drift_detector import StrictPruneEvent

    StrictPruneEvent.model_validate(row)
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# Defence-in-depth: invalid SQL identifier on a CandidateTest column +
# WarehouseError during sample-mode size resolution.
# ---------------------------------------------------------------------------


def _make_orders_model_with_adversarial_column() -> Model:
    """Build a model whose manifest legitimately contains a column with
    a name that fails ``validate_identifier`` (whitespace).

    The manifest stores upstream identifiers verbatim — the prune layer
    is the seam that defends downstream SQL composition (DEC-024).
    """
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
            "id": Column(name="id"),
            "col with space": Column(name="col with space"),
        },
        raw_code="select 1",
    )


def test_prune_tests_invalid_identifier_routes_to_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """Defence-in-depth: a CandidateTest whose ``column`` passes the
    drafter anchor contract (it IS in the manifest) but fails the
    SQL-identifier shape check at the compile seam routes to
    ``decision="kept", reason="kept-without-evidence"``.

    Conservative default — the test MAY still be signal-bearing once
    the operator fixes the upstream prompt / manifest. No warehouse
    call is issued; ``compiled_sql`` is the empty string.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # No expect_query — the compile rejects before any call dispatches.
    adapter = _make_adapter(fake)

    model = _make_orders_model_with_adversarial_column()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="col with space",
                description="adversarial.",
                tests=(CandidateTestNotNull(column="col with space"),),
            ),
        ),
    )
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 1
    assert result.kept_count == 1
    decision = result.decisions[0]
    assert decision.decision == "kept"
    assert decision.reason == "kept-without-evidence"
    assert decision.compiled_sql == ""
    assert "invalid identifier" in decision.why
    fake.assert_all_expectations_met()

    audit_rows = _read_audit_lines(audit_path)
    assert len(audit_rows) == 1
    assert audit_rows[0]["reason"] == "kept-without-evidence"


def test_prune_tests_sample_mode_warehouse_error_during_size_fetch_propagates(
    tmp_path: Path,
) -> None:
    """Sample-mode requires ``num_rows`` to size the bucket. When the
    adapter's :meth:`get_table` raises a :class:`WarehouseError` during
    that lookup, the engine propagates the typed error rather than
    silently degrading.

    The resulting fail-loud signal lands in front of the operator —
    swallowing it would defeat US-003's cost model.
    """
    from signalforge.warehouse.errors import TableNotFoundError

    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_get_table(
        ref=TableRef(project="fake_project", dataset="dataset", name="orders"),
        returns=TableNotFoundError(table="fake_project.dataset.orders"),
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="oneshot",
    )

    with pytest.raises(TableNotFoundError):
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# Issue #35 — `prune.enabled=false` short-circuit (US-005)
# ---------------------------------------------------------------------------


def test_prune_tests_short_circuits_when_enabled_false(tmp_path: Path) -> None:
    """``PruneConfig.enabled=False`` drains every candidate to
    ``kept-without-evidence`` with ``why="prune disabled in
    signalforge.yml"`` (DEC-003 stability gate), issues zero adapter /
    warehouse calls, and writes one ``PruneEvent`` per candidate to the
    audit JSONL (DEC-001 fail-closed audit preserved).

    Pins DEC-001, DEC-002, and DEC-003 of plans/super/35-prune-enabled-doc-reframe.md.
    """
    audit_path = tmp_path / "prune.jsonl"
    # Zero ``expect_*`` registrations: any adapter / SDK call would
    # surface as ``AssertionError("unexpected ...")``. ``__enter__`` is
    # NOT invoked by the disabled short-circuit (DEC-002), but even if a
    # future maintainer regressed that, ``assert_all_expectations_met()``
    # would still pass with an empty queue — the load-bearing assertion
    # is the absence of any ``query`` / ``get_table`` / ``list_rows`` /
    # ``materialise_sample`` / ``abort_session`` dispatch.
    fake = FakeBigQueryClient(project="fake_project")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(3)
    config = PruneConfig(enabled=False)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    # PruneResult: one decision per candidate, every one kept-without-evidence
    # with the locked ``why`` text (DEC-003 — a future maintainer who
    # renames the string sees this test break loudly).
    assert len(result.decisions) == 3
    assert result.model_unique_id == model.unique_id
    for decision in result.decisions:
        assert decision.decision == "kept"
        assert decision.reason == "kept-without-evidence"
        assert decision.why == "prune disabled in signalforge.yml"
        assert decision.compiled_sql == ""
        assert decision.failures == 0
        assert decision.elapsed_ms == 0
        assert decision.sampled_rows is None
        assert decision.sample_failures is None

    # Zero adapter / SDK calls consumed.
    fake.assert_all_expectations_met()

    # Audit JSONL: exactly N lines, each a valid ``PruneEvent`` carrying
    # the same ``reason`` / ``why`` (DEC-001 — one event per candidate
    # even on the fast path).
    audit_lines = _read_audit_lines(audit_path)
    assert len(audit_lines) == 3
    for raw in audit_lines:
        event = PruneEvent.model_validate(raw)
        assert event.model_unique_id == model.unique_id
        assert event.decision == "kept"
        assert event.reason == "kept-without-evidence"
        assert event.why == "prune disabled in signalforge.yml"
        assert event.compiled_sql == ""
        assert event.failures == 0
        assert event.elapsed_ms == 0
        assert event.sampled_rows is None


def test_prune_tests_disabled_does_not_validate_trusted_models(tmp_path: Path) -> None:
    """``PruneConfig.enabled=False`` short-circuits BEFORE
    ``_validate_trusted_models`` — a stale ``trusted_models`` entry must
    NOT raise ``PruneTrustedModelNotFoundError`` on the disabled path
    (DEC-002 of plans/super/35-prune-enabled-doc-reframe.md).

    An operator who disabled prune chose "stop talking to my warehouse";
    failing on a typo'd or stale trusted-models entry would defeat that
    UX promise.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_n_tests(2)
    config = PruneConfig(
        enabled=False,
        trusted_models=("model.proj.nonexistent",),
    )

    # No ``pytest.raises`` wrapper: the call must succeed despite the
    # ``trusted_models`` entry being absent from ``manifest.nodes``.
    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    # Same short-circuit invariants as the primary test.
    assert len(result.decisions) == 2
    for decision in result.decisions:
        assert decision.decision == "kept"
        assert decision.reason == "kept-without-evidence"
        assert decision.why == "prune disabled in signalforge.yml"
    fake.assert_all_expectations_met()


def test_prune_tests_disabled_with_empty_candidates_returns_empty_result(
    tmp_path: Path,
) -> None:
    """``PruneConfig.enabled=False`` AND an empty ``CandidateSchema``
    returns a zero-decision ``PruneResult`` and writes zero audit rows.

    Defence-in-depth for the fail-closed audit invariant (DEC-001): "one
    PruneEvent per candidate" with zero candidates means zero events;
    the disabled-path loop must NOT raise on an empty iterable, and the
    audit JSONL file must not be created (no `os.open` happens because
    the for-loop never runs the writer). A regression that wrapped the
    loop in ``if not pairs: raise SomeError(...)`` would break this test
    loudly.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    empty_candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(),
    )
    config = PruneConfig(enabled=False)

    result = prune_tests(
        model,
        adapter,
        empty_candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert len(result.decisions) == 0
    assert result.kept_count == 0
    assert result.dropped_count == 0
    # No candidates → no audit writes → no on-disk artefact.
    assert not audit_path.exists()
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# custom_sql routing matrix (US-008 of #116)
#
# The prune ENGINE's routing is deliberately test-type-agnostic: it
# dispatches on the compiler's return shape (`str` / `_InvalidIdentifier` /
# `_RequiresFutureData`), the warehouse `failure_count`, and any raised
# `WarehouseError`. These tests pin that `custom_sql` (the singular
# business-rule variant added in #116) flows through the SAME decision
# matrix as the four built-ins — no bespoke engine branch, the locked
# 5-value `DropReason` literal unchanged.
# ---------------------------------------------------------------------------


def _candidates_with_one_custom_sql_test(
    sql: str,
    *,
    column: str | None = None,
) -> CandidateSchema:
    """Build a CandidateSchema carrying a single model-level (or
    column-scoped) ``custom_sql`` business-rule test.

    Model-level (``column=None``) lands in :attr:`CandidateSchema.tests`;
    a column-scoped test lands under the named column so the engine
    iterates it with ``test_anchor=f"column.{column}"``.
    """
    if column is None:
        return CandidateSchema(
            name="orders",
            description="Order events.",
            columns=(),
            tests=(CandidateTestCustomSQL(sql=sql),),
        )
    return CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name=column,
                description="A column under a business-rule assertion.",
                tests=(CandidateTestCustomSQL(sql=sql, column=column),),
            ),
        ),
    )


def test_prune_tests_custom_sql_always_passes_drops_test(tmp_path: Path) -> None:
    """A ``custom_sql`` business-rule test that returns zero failing rows
    is dropped with ``reason="always-passes"`` — signal-over-volume applies
    to singular tests exactly as to the built-ins (no engine special-case).

    The fake returns ``failures=0`` so the assertion is mathematically
    guaranteed (testing-signal.md engineered determinism). The
    ``{{ this }}`` ref resolves to the model's qualified table so a single
    real SELECT is compiled and dispatched.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_custom_sql_test(
        "SELECT * FROM {{ this }} WHERE status NOT IN ('open', 'closed')"
    )
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 1
    decision = result.decisions[0]
    assert decision.test.type == "custom_sql"
    assert decision.decision == "dropped"
    assert decision.reason == "always-passes"
    assert decision.failures == 0
    assert decision.test_anchor == "model"
    fake.assert_all_expectations_met()

    audit_rows = _read_audit_lines(audit_path)
    assert len(audit_rows) == 1
    assert audit_rows[0]["reason"] == "always-passes"


def test_prune_tests_custom_sql_kept_for_real_failure_untrusted_model(
    tmp_path: Path,
) -> None:
    """A ``custom_sql`` test that returns failing rows on an untrusted
    model is kept with ``reason="kept"`` — real signal, reviewer should
    evaluate. Routes through the identical untrusted-failure arm of
    ``_decide_from_test_result`` as the built-ins.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 4}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_custom_sql_test(
        "SELECT * FROM {{ this }} WHERE customer_id IS NULL",
        column="customer_id",
    )
    config = PruneConfig(scope="full", capture_failure_rows=0)  # untrusted by default

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "custom_sql"
    assert decision.decision == "kept"
    assert decision.reason == "kept"
    assert decision.failures == 4
    assert "4 failures" in decision.why
    assert decision.test_anchor == "column.customer_id"
    fake.assert_all_expectations_met()


def test_prune_tests_custom_sql_invalid_identifier_routes_to_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """A ``custom_sql`` test carrying unsupported Jinja (here ``var()``)
    compiles to the ``_InvalidIdentifier`` sentinel and routes to
    ``kept-without-evidence`` with the existing identifier-rejected
    ``why`` — NO warehouse call is issued.

    Confirms custom_sql flows through the SAME sentinel branch the
    built-ins use for a malformed identifier (engine is test-type-agnostic).
    Unsupported Jinja surfaces from the compiler as ``UnsupportedJinjaError``
    (a ``TemplateResolutionError`` subclass), which ``_compile_custom_sql``
    catches and converts to the sentinel.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Intentionally NO expect_query — the sentinel short-circuits before
    # any warehouse dispatch; an unexpected query would fail the fake.
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    # ``{{ var(...) }}`` is unsupported control-flow Jinja → the resolver
    # raises UnsupportedJinjaError → _InvalidIdentifier sentinel.
    candidates = _candidates_with_one_custom_sql_test(
        "SELECT * FROM {{ this }} WHERE region = '{{ var('r') }}'"
    )
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "custom_sql"
    assert decision.decision == "kept"
    assert decision.reason == "kept-without-evidence"
    # The sentinel's `reason` surfaces verbatim as the decision `why`;
    # for an unresolvable custom_sql it names the resolution failure.
    assert "custom_sql" in decision.why.lower()
    assert decision.compiled_sql == ""
    fake.assert_all_expectations_met()


def test_prune_tests_custom_sql_warehouse_error_routes_to_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """A typed :class:`WarehouseError` raised while running a ``custom_sql``
    query routes to ``kept-without-evidence`` via the existing per-test
    error handler — conservative default keeps the test.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(
        matching=r"SELECT COUNT\(\*\)",
        returns=TableNotFoundError(table="fake_project.dataset.orders"),
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_custom_sql_test("SELECT * FROM {{ this }} WHERE amount < 0")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "custom_sql"
    assert decision.decision == "kept"
    assert decision.reason == "kept-without-evidence"
    assert "TableNotFoundError" in decision.why
    fake.assert_all_expectations_met()


def test_prune_tests_custom_sql_over_byte_cap_full_scan_routes_to_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """A multi-table ``custom_sql`` business-rule test runs full-scan
    (the compiler refuses to sample a JOIN — DEC-006); if that full scan
    exceeds ``maximum_bytes_billed`` the adapter raises
    :class:`BytesBilledExceededError` (a :class:`WarehouseError` subclass),
    which the existing per-test handler routes to
    ``kept-without-evidence``.

    DEC-007 ``why`` decision: the GENERIC per-test handler ``why`` is used
    (``"Test could not be evaluated: BytesBilledExceededError: ..."``). The
    typed class name is already in the ``why`` so a reviewer can correlate
    the byte-cap rejection without a bespoke locked string; adding a
    distinct ``why`` would require special-casing one WarehouseError
    subclass in the otherwise error-type-agnostic engine handler. The
    locked 5-value DropReason literal stays unchanged either way.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # The matching regex requires the full-scan JOIN shape AND rejects any
    # ``WITH sample`` CTE (mirrors
    # ``test_prune_tests_full_mode_does_not_wrap_with_cte``): the dispatched
    # SQL must start with the COUNT envelope wrapping a JOIN, NOT a sample
    # CTE. If the multi-table test were wrongly sampled, the dispatched SQL
    # would begin ``...FROM (WITH sample AS ...`` and this expectation would
    # not match — so the test fails loud rather than passing silently.
    fake.expect_query(
        matching=(
            r"^SELECT COUNT\(\*\) AS failures FROM "
            r"\(SELECT o\.id FROM fake_project\.dataset\.orders AS o "
            r"JOIN fake_project\.dataset\.other_model AS d "
            r"ON o\.customer_id = d\.customer_id WHERE o\.id <> d\.id\) AS t$"
        ),
        returns=BytesBilledExceededError(
            job_id="job_abc", bytes_billed=5_000_000_000, limit=100_000_000
        ),
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    # Two DISTINCT refs ({{ this }} + {{ ref('other_model') }}) genuinely
    # exercise the multi-table classifier (DEC-006). A JOIN survives
    # literal-stripping → compiler classifies multi-table → full-scan SQL is
    # compiled and dispatched (no sample CTE). The fake then simulates the
    # over-cap rejection on that full scan.
    manifest = _make_manifest_with_other(model)
    candidates = _candidates_with_one_custom_sql_test(
        "SELECT o.id FROM {{ this }} AS o "
        "JOIN {{ ref('other_model') }} AS d ON o.customer_id = d.customer_id "
        "WHERE o.id <> d.id"
    )
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "custom_sql"
    assert decision.decision == "kept"
    assert decision.reason == "kept-without-evidence"
    assert "BytesBilledExceededError" in decision.why
    fake.assert_all_expectations_met()


_MULTI_TABLE_FULLSCAN_RE = (
    r"^SELECT COUNT\(\*\) AS failures FROM "
    r"\(SELECT o\.id FROM fake_project\.dataset\.orders AS o "
    r"JOIN fake_project\.dataset\.other_model AS d "
    r"ON o\.customer_id = d\.customer_id WHERE o\.id <> d\.id\) AS t$"
)
_MULTI_TABLE_CUSTOM_SQL = (
    "SELECT o.id FROM {{ this }} AS o "
    "JOIN {{ ref('other_model') }} AS d ON o.customer_id = d.customer_id "
    "WHERE o.id <> d.id"
)


def test_prune_tests_custom_sql_multi_table_full_scan_zero_failures_drops(
    tmp_path: Path,
) -> None:
    """A MULTI-TABLE ``custom_sql`` business-rule test compiles to real
    full-scan SQL (no sample CTE — DEC-006), runs, and routes on
    ``failures=0 → dropped / always-passes`` exactly like the built-ins.

    The dispatched SQL is pinned to the full-scan JOIN shape: the matching
    regex requires the COUNT envelope wrapping the JOIN and would reject a
    ``WITH sample`` CTE, so a regression that wrongly sampled the join fails
    loud here rather than passing.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=_MULTI_TABLE_FULLSCAN_RE, returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest_with_other(model)
    candidates = _candidates_with_one_custom_sql_test(_MULTI_TABLE_CUSTOM_SQL)
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "custom_sql"
    assert decision.decision == "dropped"
    assert decision.reason == "always-passes"
    # Belt-and-braces: the compiled SQL is the full-scan join, not a sample.
    assert "WITH sample" not in decision.compiled_sql
    assert "fake_project.dataset.other_model" in decision.compiled_sql
    fake.assert_all_expectations_met()


def test_prune_tests_custom_sql_multi_table_full_scan_nonzero_failures_keeps(
    tmp_path: Path,
) -> None:
    """Sibling of the always-passes case: a MULTI-TABLE ``custom_sql`` test
    that returns non-zero failing rows on an untrusted model is real signal
    and routes to ``kept`` / ``kept`` (DropReason ``"kept"``). Same full-scan
    JOIN shape is dispatched (no sample CTE)."""
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=_MULTI_TABLE_FULLSCAN_RE, returns=[{"failures": 7}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest_with_other(model)
    candidates = _candidates_with_one_custom_sql_test(_MULTI_TABLE_CUSTOM_SQL)
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "custom_sql"
    assert decision.decision == "kept"
    assert decision.reason == "kept"
    assert "WITH sample" not in decision.compiled_sql
    fake.assert_all_expectations_met()


def test_prune_tests_custom_sql_single_table_references_temp_table_under_materialised(
    tmp_path: Path,
) -> None:
    """P0 fix: under ``sample_strategy="materialised"`` + ``scope="sample"``,
    a SINGLE-TABLE ``custom_sql`` test's compiled / dispatched SQL references
    the MATERIALISED temp table (``_SESSION._sf_sample_<run_id>``) and does
    NOT full-scan the source production table.

    Before the fix, ``_compile_custom_sql`` returned the resolved SQL
    unchanged on the single-table ``scope="full"`` + no-partition path, so the
    test silently read the source table even though the engine had
    materialised a sample — defeating the cost model. Mirrors
    ``test_prune_tests_compiled_sql_references_temp_table_under_materialised``
    (which only covers ``not_null``).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=materialised_ref,
    )
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_custom_sql_test(
        "SELECT * FROM {{ this }} WHERE status NOT IN ('open', 'closed')"
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "custom_sql"
    # The single-table custom_sql now reads the materialised temp table.
    assert "_SESSION._sf_sample_" in decision.compiled_sql
    assert re.search(r"_sf_sample_[0-9a-f]{16}", decision.compiled_sql) is not None
    # The source production table MUST NOT appear — the whole point of
    # materialisation is amortised cost via the temp table.
    assert "fake_project.dataset.orders" not in decision.compiled_sql
    fake.assert_all_expectations_met()


def test_prune_tests_custom_sql_audit_invariant_one_event_per_candidate(
    tmp_path: Path,
) -> None:
    """Fail-closed audit invariant holds for ``custom_sql``: exactly one
    :class:`PruneEvent` row per custom_sql candidate, with no new audit
    fields (the PruneEvent shape is unchanged by #116).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 2}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestCustomSQL(sql="SELECT * FROM {{ this }} WHERE amount < 0"),
            CandidateTestCustomSQL(sql="SELECT * FROM {{ this }} WHERE status = ''"),
        ),
    )
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 2
    audit_rows = _read_audit_lines(audit_path)
    assert len(audit_rows) == 2
    # Each row round-trips through the read-back PruneEvent unchanged —
    # confirms the audit shape carries no custom_sql-specific field.
    for row in audit_rows:
        event = PruneEvent.model_validate(row)
        assert event.model_unique_id == "model.shop.orders"
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# row_count_between routing matrix (US-008 of #169)
#
# DEC-011 of #169 locks the engine's routing as test-type-agnostic: the
# matrix dispatches on the compiler's return shape
# (``str`` / ``_InvalidIdentifier`` / ``_RequiresFutureData``), the warehouse
# ``failure_count``, and any raised :class:`WarehouseError`. These tests pin
# that ``row_count_between`` (the 6th first-class variant added in #169) flows
# through the SAME decision matrix as the four built-ins and ``custom_sql`` —
# no bespoke engine branch, the locked 5-value :data:`DropReason` literal
# unchanged. A regression that introduced a test-type-specific arm would
# break these tests loud.
# ---------------------------------------------------------------------------


def _candidates_with_one_row_count_test(
    *,
    minimum: int | None = 100,
    maximum: int | None = 10_000,
    where: str | None = None,
) -> CandidateSchema:
    """Build a CandidateSchema carrying a single model-level
    ``row_count_between`` test. The variant is model-level only
    (``column=None`` by Pydantic invariant, DEC-001 of #169).
    """
    return CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestRowCountBetween(
                minimum=minimum,
                maximum=maximum,
                where=where,
            ),
        ),
    )


def test_prune_tests_row_count_between_always_passes_drops_test(tmp_path: Path) -> None:
    """A ``row_count_between`` test that returns ``failures=0`` from the
    warehouse routes through the SAME ``always-passes`` arm as the four
    built-ins and ``custom_sql`` — the engine matrix is test-type-agnostic
    (DEC-011 of #169).

    Pins the conservative ``signal-over-volume`` posture for the 6th
    variant: regardless of which test type proposed the query, a zero
    failure count drops the test.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_row_count_test(minimum=1, maximum=1_000_000)
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 1
    decision = result.decisions[0]
    assert decision.test.type == "row_count_between"
    assert decision.decision == "dropped"
    assert decision.reason == "always-passes"
    assert decision.failures == 0
    # row_count_between is always model-level (DEC-001 of #169).
    assert decision.test_anchor == "model"
    fake.assert_all_expectations_met()

    audit_rows = _read_audit_lines(audit_path)
    assert len(audit_rows) == 1
    assert audit_rows[0]["reason"] == "always-passes"


def test_prune_tests_row_count_between_kept_for_real_failure_untrusted_model(
    tmp_path: Path,
) -> None:
    """A ``row_count_between`` test returning non-zero ``failures`` on an
    untrusted model routes to ``decision="kept", reason="kept"`` via the
    identical untrusted-failure arm of ``_decide_from_test_result`` the
    built-ins / ``custom_sql`` use (DEC-011 of #169).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 5}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_row_count_test(minimum=100)
    config = PruneConfig(scope="full", capture_failure_rows=0)  # untrusted

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "row_count_between"
    assert decision.decision == "kept"
    assert decision.reason == "kept"
    assert decision.failures == 5
    assert "5 failures" in decision.why
    assert decision.test_anchor == "model"
    fake.assert_all_expectations_met()


def test_prune_tests_row_count_between_failed_on_known_clean_data_for_trusted_model(
    tmp_path: Path,
) -> None:
    """A ``row_count_between`` test returning non-zero ``failures`` on a
    *trusted* model routes through the SAME ``failed-on-known-clean-data``
    arm the built-ins use — the model is presumed clean, so the test is
    presumed buggy and dropped (DEC-011 of #169).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 9}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_row_count_test(minimum=1, maximum=10)
    config = PruneConfig(
        scope="full",
        trusted_models=(model.unique_id,),
        capture_failure_rows=0,
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "row_count_between"
    assert decision.decision == "dropped"
    assert decision.reason == "failed-on-known-clean-data"
    assert decision.failures == 9
    assert "trusted_models" in decision.why
    fake.assert_all_expectations_met()


def test_prune_tests_row_count_between_invalid_identifier_routes_to_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """A ``row_count_between`` test whose ``where`` clause fails
    :func:`validate_test_sql` (here a stray ``;``) returns
    :class:`_InvalidIdentifier` from the compiler and routes to
    ``decision="kept", reason="kept-without-evidence"`` (DEC-005, DEC-011 of
    #169). NO warehouse call is issued — pinned by the fake having zero
    queued expectations.

    The locked ``why`` text names ``row_count_between`` explicitly so a
    reviewer reading the audit JSONL can correlate the rejection with the
    safety check.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Intentionally NO expect_query — the sentinel short-circuits before
    # any warehouse dispatch; an unexpected query would fail the fake.
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    # A stray ``;`` triggers QuerySyntaxError inside the compiler's
    # compose-then-validate pre-flight (DEC-005 of #169).
    candidates = _candidates_with_one_row_count_test(
        minimum=1,
        where="1=1; DROP TABLE users",
    )
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "row_count_between"
    assert decision.decision == "kept"
    assert decision.reason == "kept-without-evidence"
    # Locked ``why`` text — the sentinel's reason surfaces verbatim.
    assert "row_count_between" in decision.why
    assert "SQL safety check" in decision.why
    assert decision.compiled_sql == ""
    fake.assert_all_expectations_met()


def test_prune_tests_row_count_between_warehouse_error_routes_to_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """A typed :class:`WarehouseError` raised while running a
    ``row_count_between`` query routes to
    ``decision="kept", reason="kept-without-evidence"`` via the existing
    per-test error handler (DEC-011 of #169). Conservative default keeps
    the test — a transient adapter / table-not-found / auth blip must not
    silently lose a signal-bearing test.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(
        matching=r"SELECT COUNT\(\*\)",
        returns=TableNotFoundError(table="fake_project.dataset.orders"),
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_row_count_test(minimum=100, maximum=10_000)
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "row_count_between"
    assert decision.decision == "kept"
    assert decision.reason == "kept-without-evidence"
    assert "TableNotFoundError" in decision.why
    fake.assert_all_expectations_met()


def test_prune_tests_row_count_between_total_budget_exceeded_routes_to_kept_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the total prune budget is exhausted mid-run, every remaining
    un-started ``row_count_between`` test drains to
    ``decision="kept", reason="kept-without-evidence"`` with the
    budget-specific locked ``why`` text and NO warehouse call (DEC-011 of
    #169). Same arm used by the built-ins; mirrors
    :func:`test_prune_tests_total_budget_exceeded_marks_remaining_kept_without_evidence`.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Only the first test runs.
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestRowCountBetween(minimum=1, maximum=1_000_000),
            CandidateTestRowCountBetween(minimum=10, maximum=2_000_000),
            CandidateTestRowCountBetween(minimum=100, maximum=3_000_000),
        ),
    )
    config = PruneConfig(scope="full", total_budget_seconds=1, capture_failure_rows=0)

    # Stub the monotonic clock so the second iteration sees the budget
    # exhausted (mirrors the budget-test pattern earlier in this file).
    timeline = iter([0, 0, 0, 10, 5000, 5000, 5000, 5000, 5000, 5000])

    def fake_clock() -> int:
        return next(timeline)

    monkeypatch.setattr(engine_module, "_now_monotonic_ms", fake_clock)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 3
    # First test ran — always-passes drop.
    assert result.decisions[0].reason == "always-passes"
    # Remaining two are kept-without-evidence due to budget exhaustion.
    for decision in result.decisions[1:]:
        assert decision.test.type == "row_count_between"
        assert decision.decision == "kept"
        assert decision.reason == "kept-without-evidence"
        # Locked ``why`` text per DEC-011 budget arm.
        assert "Total prune budget" in decision.why
    fake.assert_all_expectations_met()


def test_prune_tests_row_count_between_empty_table_failing_count_is_kept(
    tmp_path: Path,
) -> None:
    """DEC-010 of #169 — degenerate-table carve-out, pinned at the engine
    routing level.

    An empty table with ``minimum=100`` produces a warehouse
    ``failure_count > 0`` (the COUNT(*)-wrap returns one row; the engine
    treats that non-zero result as a real failure on untrusted data). The
    matrix routes to ``decision="kept", reason="kept"`` because that IS
    what the test is meant to catch: the test fired correctly against a
    table that violates the bound. NO special-case in the engine — same
    untrusted-failure arm the built-ins use.

    Documented explicitly so an operator reading a ``kept`` decision
    against an empty production table understands the test fired correctly
    rather than treating the empty-input case as a defect.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Empty table → wrapped COUNT(*) returns one row with the inner count
    # (0 here, simulated as ``failures=1`` from the engine's perspective:
    # any non-zero failures value the warehouse emits routes through the
    # test-type-agnostic ``kept`` arm. We use a small positive value to
    # stay faithful to the engine's matrix without coupling to the
    # specific wrap-shape semantics).
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 1}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_row_count_test(minimum=100, maximum=None)
    config = PruneConfig(scope="full", capture_failure_rows=0)  # untrusted

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "row_count_between"
    # DEC-010: real signal — the bound was violated; ship the test.
    assert decision.decision == "kept"
    assert decision.reason == "kept"
    assert decision.failures == 1
    fake.assert_all_expectations_met()


def test_prune_tests_row_count_between_under_materialised_references_source_not_temp_table(
    tmp_path: Path,
) -> None:
    """QG-fix (post-US-007a) + CodeRabbit #176 Thread w2h: under
    ``sample_strategy="materialised"`` + ``scope="sample"``, when the
    only candidate is a ``row_count_between``, the engine short-circuits
    the entire materialised pre-work (no ``materialise_sample`` call, no
    session, no temp table) and compiles directly against the SOURCE
    table.

    Two load-bearing invariants:
      * **Semantic correctness:** a COUNT(*) against a materialised sample
        returns the sample size, not the model's true row count — bounds
        checked against sample size are meaningless. The compiled SQL
        must reference the source.
      * **Failure-mode containment:** before the short-circuit, if all
        candidates were ``row_count_between`` and ``materialise_sample``
        raised, every test routed to ``kept-without-evidence`` even
        though they could have run directly against source. The
        short-circuit removes that failure mode entirely.

    Pinned by asserting the fake adapter saw NO ``materialise_sample``
    call (no ``expect_materialise_sample`` queued) AND the compiled SQL
    references the source qualified name. A companion test
    ``test_prune_tests_mixed_row_count_between_uses_per_test_override_when_materialised``
    pins the other branch (mixed candidates: materialisation happens for
    the non-bypassing tests, per-test override still routes
    ``row_count_between`` to source).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # NO ``expect_get_table`` — the short-circuit skips the
    # ``_resolve_sample_bucket`` lookup too (it'd otherwise call
    # ``adapter.get_table``). NO ``expect_materialise_sample`` — the
    # all-bypass short-circuit skips it. NO ``expect_abort_session`` —
    # no session was opened.
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_row_count_test(minimum=100, maximum=10_000)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "row_count_between"
    # row_count_between MUST reference the SOURCE production table — a
    # COUNT(*) against the materialised sample would return the sample
    # size (100_000), not the model's true row count.
    assert "fake_project.dataset.orders" in decision.compiled_sql
    # The temp table MUST NOT appear; the per-test override routed past it.
    assert "_SESSION._sf_sample_" not in decision.compiled_sql
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# unique_combination sample-mode routing (#170 US-005b)
#
# DEC-006 of #170 — composite uniqueness on a sample is semantically
# approximate (false-negative risk: a duplicate pair may straddle the sampled
# and unsampled rows, so the sample looks unique while the full table has
# duplicates). The engine routes ``unique_combination`` to the SOURCE table
# under both ``sample_strategy="materialised"`` and ``sample_strategy="oneshot"``
# when ``scope="sample"`` — mirroring the #169 US-007a metadata-bypass pattern
# that ``row_count_between`` follows.
#
# These tests are the BEHAVIOURAL routing pin (load-bearing per
# ``.claude/rules/business-rule-tests.md`` § "Pin the engine-routing test, not
# just the compiler snapshot"). The snapshot suite (US-005a) certifies SQL
# shape; only this matrix certifies that the engine actually hands the
# compiler the source ``TableRef`` rather than a substituted temp table.
# ---------------------------------------------------------------------------


def _candidates_with_one_unique_combination_test(
    *,
    columns: tuple[str, ...] = ("id", "customer_id"),
    where: str | None = None,
) -> CandidateSchema:
    """Build a CandidateSchema carrying a single model-level
    ``unique_combination`` test. The variant is model-level only
    (``column=None`` by Pydantic invariant, DEC-001 of #170) and requires
    ``len(columns) >= 2`` (DEC-016).
    """
    return CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestUniqueCombination(
                columns=columns,
                where=where,
            ),
        ),
    )


@pytest.mark.parametrize("sample_strategy", ["materialised", "oneshot"])
def test_prune_tests_unique_combination_under_materialised_references_source_not_temp_table(
    tmp_path: Path,
    sample_strategy: str,
) -> None:
    """#170 US-005b / DEC-006 — under ``scope="sample"`` and EITHER
    ``sample_strategy="materialised"`` or ``sample_strategy="oneshot"``,
    when the only candidate is a ``unique_combination``, the engine
    short-circuits all sample pre-work (no ``materialise_sample``, no
    ``get_row_count`` bucket lookup, no session, no temp table) and
    compiles directly against the SOURCE table.

    Two load-bearing invariants:
      * **Semantic correctness:** composite uniqueness on a sample is
        semantically approximate — a duplicate pair may straddle the
        sampled and unsampled rows, so a sample-mode verdict carries
        false-negative risk. The compiled SQL must reference the source.
      * **Failure-mode containment:** before the short-circuit, if all
        candidates were ``unique_combination`` and ``materialise_sample``
        raised, every test routed to ``kept-without-evidence`` even
        though they could have run directly against source. The
        short-circuit removes that failure mode entirely.

    Mirrors the precedent test
    ``test_prune_tests_row_count_between_under_materialised_references_source_not_temp_table``
    for the 6th variant (``row_count_between``); this 7th variant
    follows the SAME metadata-bypass routing (cross-review consensus in
    #170's plan, Performance + Testing reviews converging on Option (iii)).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # NO ``expect_get_table`` — the all-bypass short-circuit skips the
    # ``_resolve_sample_bucket`` lookup. NO ``expect_materialise_sample``
    # — the short-circuit skips the CTAS too. NO ``expect_abort_session``
    # — no session opened.
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_unique_combination_test(columns=("id", "customer_id"))
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy=sample_strategy,  # type: ignore[arg-type]
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "unique_combination"
    # unique_combination MUST reference the SOURCE production table —
    # composite uniqueness on a sample is semantically approximate.
    assert "fake_project.dataset.orders" in decision.compiled_sql
    # The temp table MUST NOT appear; the engine override routed past it.
    assert "_SESSION._sf_sample_" not in decision.compiled_sql
    fake.assert_all_expectations_met()


def test_prune_tests_unique_combination_under_scope_full_references_source(
    tmp_path: Path,
) -> None:
    """Companion test — ``scope="full"`` already routes every variant to
    the source table trivially (no sampling at all). Guards against an
    accidental regression where extending the per-test override for
    ``unique_combination`` somehow broke the no-sample path. Belt-and-
    braces alongside the parametrised sample-mode pin above.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_unique_combination_test(columns=("id", "customer_id"))
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "unique_combination"
    assert "fake_project.dataset.orders" in decision.compiled_sql
    assert "_SESSION._sf_sample_" not in decision.compiled_sql
    fake.assert_all_expectations_met()


def test_prune_tests_mixed_candidates_per_test_override_routes_unique_combination_to_source(
    tmp_path: Path,
) -> None:
    """#170 US-005b / QG Pass 3 finding — when a candidate list contains a
    MIX of variants (one bypassing, one not), the engine's
    ``all_bypass_to_source`` short-circuit at ``engine.py`` MUST NOT fire,
    and the per-test ``per_test_table_ref`` override must route each test
    individually. This pins the second of the two-conditional pattern
    US-005b discovered.

    Without this test, dropping ``CandidateTestUniqueCombination`` from the
    per-test override (engine.py ``per_test_table_ref`` arm) while leaving
    it in ``all_bypass_to_source`` would PASS the single-variant pin above
    silently (because the short-circuit catches the all-``unique_combination``
    case before the per-test branch fires). The mixed candidate exercises
    the per-test arm directly — the two sites are conceptually independent
    and need independent coverage.

    Mixed shape: one column-scoped ``not_null`` on ``id`` (does NOT bypass —
    routes to the materialised sample temp table) plus one model-level
    ``unique_combination(columns=("id", "customer_id"))`` (bypasses — routes
    to the source). Two compile queries, two distinct routings, one fake
    expectation pair each.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    # NOT bypassed: ``not_null`` requires the sample to be materialised.
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=materialised_ref,
    )
    # Two per-test compile queries — order is candidate iteration order:
    # column-scoped ``not_null`` first (against sample), then model-level
    # ``unique_combination`` (against source).
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    # The engine builds the abort-session id from its own deterministic
    # `_sf_sample_*` hash (not the fake's returned ref), so use the fake's
    # session-id-from-returns convention rather than pinning a specific
    # hex string — the load-bearing claim is that abort_session fires,
    # not which specific session id.
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    # Construct the mixed candidate schema manually — column-scoped not_null
    # PLUS model-level unique_combination on the same CandidateSchema.
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="id",
                description="The order's primary key.",
                tests=(CandidateTestNotNull(column="id"),),
            ),
        ),
        tests=(CandidateTestUniqueCombination(columns=("id", "customer_id")),),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 2
    # Find each decision by test type so the assertion does not rely on
    # iteration order (which is an implementation detail of the engine's
    # per-test loop).
    by_type = {d.test.type: d for d in result.decisions}
    assert {"not_null", "unique_combination"} <= set(by_type)

    not_null_sql = by_type["not_null"].compiled_sql
    uc_sql = by_type["unique_combination"].compiled_sql

    # ``not_null`` routes to the MATERIALISED SAMPLE temp table — does NOT
    # bypass. The compiled SQL references the temp table by the
    # ``_SESSION._sf_sample_<run_id>`` shape; the specific run_id is the
    # engine's deterministic hash (independent of the fake's returns), so
    # pin the prefix that proves the temp routing, not the suffix.
    assert "_SESSION._sf_sample_" in not_null_sql
    # And the source table MUST NOT appear at table-position in the
    # not_null SQL — otherwise the engine routed it past the substitution.
    assert "fake_project.dataset.orders" not in not_null_sql

    # ``unique_combination`` routes to the SOURCE table (per-test override).
    # The compiled SQL must reference the source qualified name AND NOT
    # the temp table — load-bearing regression detector for the per-test arm.
    assert "fake_project.dataset.orders" in uc_sql
    assert "_SESSION._sf_sample_" not in uc_sql

    fake.assert_all_expectations_met()


def test_drop_reason_literal_still_exactly_five_values() -> None:
    """DEC-011 of #169 — closed-set lockdown.

    A 6th first-class variant (``row_count_between``) lands without
    growing the ``DropReason`` literal — conservative-bias routing reuses
    the existing five literals for every new failure mode. A regression
    that added a 6th literal (e.g. a bespoke ``row-count-out-of-bounds``
    bucket) would fail loud here. Cross-checked against the drift-detector
    fixture (``prune_event_v1.jsonl`` covers all five) so the two pins
    catch a regression independently.
    """
    from typing import get_args

    from signalforge.prune.models import DropReason

    args = get_args(DropReason)
    assert len(args) == 5, f"DropReason must remain a closed 5-value literal; got {args!r}"
    assert set(args) == {
        "always-passes",
        "requires-future-data",
        "failed-on-known-clean-data",
        "kept",
        "kept-without-evidence",
    }


# ---------------------------------------------------------------------------
# #171 US-009 — ``as_of`` threading + variant detection + INFO log
# ---------------------------------------------------------------------------


def _make_anomaly_candidates() -> CandidateSchema:
    """Build a CandidateSchema carrying ONE model-level
    :class:`CandidateTestRowCountAnomalyByPeriod` (and no other tests).

    Used by the US-009 ``as_of`` threading tests. The compiler arm for
    this variant lands in US-008; under #171 US-009 the dispatcher hits
    its closing ``NotImplementedError`` for the anomaly variant, so
    every test here either stubs :func:`_compile_test` or only asserts
    on behaviour that happens BEFORE compile (the INFO log fires right
    after ``_iter_candidate_tests`` — well before the per-test loop).
    """
    from signalforge.draft.models import CandidateTestRowCountAnomalyByPeriod

    return CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at"),),
    )


def test_as_of_none_with_anomaly_candidate_resolves_to_today_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#171 US-009 / DEC-001 — when ``as_of=None`` AND at least one
    candidate is the anomaly variant, the orchestrator resolves to
    :meth:`datetime.date.today` AND emits ONE INFO log line.

    Stubs :func:`_compile_test` so the dispatcher's still-unimplemented
    anomaly arm (US-008's job) doesn't raise. The stub additionally
    captures the threaded ``as_of`` kwarg so the per-test threading is
    verified end-to-end (engine → compiler) in the same test.
    """
    from datetime import date as _date

    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # The stub never issues a warehouse query, but the engine still
    # enters ``with adapter:`` and calls ``_resolve_sample_bucket`` /
    # ``materialise_sample`` when scope=sample. Pin scope=full so neither
    # path needs an expectation, and the stub short-circuits the rest.
    adapter = _make_adapter(fake)

    captured: dict[str, Any] = {}

    def _stub_compile(test: Any, table_ref: Any, dialect: Any, manifest: Any, **kwargs: Any) -> str:
        captured["as_of"] = kwargs.get("as_of")
        # Return a non-empty SQL so the engine continues to ``run_test_sql``.
        return "SELECT 1"

    monkeypatch.setattr(engine_module, "_compile_test", _stub_compile)
    # The stub returns SQL, so ``run_test_sql`` runs — queue an
    # always-passes result for it.
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _make_anomaly_candidates()
    config = PruneConfig(scope="full", capture_failure_rows=0)

    # Capture today() BEFORE prune_tests; combined with the post-call
    # capture below, this brackets the engine's resolution moment within
    # [before, after] and avoids midnight flakiness (per #171 CR feedback).
    today_before = _date.today()
    with caplog.at_level("INFO", logger="signalforge.prune.engine"):
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
            # as_of NOT supplied → engine resolves to date.today()
        )

    # Capture today() AFTER prune_tests has returned: this brackets the
    # resolution moment within [before, after] and avoids a midnight flake
    # (per #171 CodeRabbit finding) when the test runs across midnight.
    today_after = _date.today()
    # The "today" the engine resolved must be one of {today_before, today_after}
    # — same value unless we crossed midnight mid-call. Both branches accept.
    valid_today = {today_before, today_after}
    valid_today_iso = {d.isoformat() for d in valid_today}

    # Exactly one INFO line names the resolved as_of.
    info_records = [r for r in caplog.records if "anomaly: as_of resolved" in r.getMessage()]
    assert len(info_records) == 1, (
        f"expected exactly one INFO line; got {len(info_records)}: "
        f"{[r.getMessage() for r in info_records]}"
    )
    # The line embeds the resolved date as ISO-8601; matches today (within
    # the [before, after] window).
    assert any(iso in info_records[0].getMessage() for iso in valid_today_iso)
    # Lazy-format JSON pattern: the message stays the literal template
    # and the JSON payload rides on args.
    assert info_records[0].msg == "anomaly: as_of resolved: %s"
    payload = json.loads(info_records[0].args[0])  # type: ignore[index]
    assert payload["as_of"] in valid_today_iso
    assert payload["model_unique_id"] == model.unique_id

    # The threaded value reached the compiler stub as date.today() — the
    # engine→compiler plumbing carries ``as_of``, not just the log line.
    assert captured["as_of"] in valid_today


def test_as_of_none_no_anomaly_candidate_emits_no_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#171 US-009 / DEC-001 — when ``as_of=None`` AND NO candidate is
    the anomaly variant, the engine emits NO ``as_of resolved`` INFO
    line (and never reads :meth:`datetime.date.today`). This is the
    "zero impact on existing variants" guarantee.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    with caplog.at_level("INFO", logger="signalforge.prune.engine"):
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    matches = [r for r in caplog.records if "as_of resolved" in r.getMessage()]
    assert matches == [], (
        f"expected zero ``as_of resolved`` log lines on a no-anomaly run; "
        f"got {[r.getMessage() for r in matches]}"
    )
    fake.assert_all_expectations_met()


def test_as_of_supplied_uses_supplied_value(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#171 US-009 / DEC-001 — when the caller supplies ``as_of`` AND
    an anomaly candidate is present, the engine uses the supplied value
    verbatim (NOT :meth:`datetime.date.today`) and the INFO log surfaces
    it. Same threading contract reaches :func:`_compile_test` with the
    supplied value.
    """
    from datetime import date as _date

    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    adapter = _make_adapter(fake)

    captured: dict[str, Any] = {}

    def _stub_compile(test: Any, table_ref: Any, dialect: Any, manifest: Any, **kwargs: Any) -> str:
        captured["as_of"] = kwargs.get("as_of")
        return "SELECT 1"

    monkeypatch.setattr(engine_module, "_compile_test", _stub_compile)
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _make_anomaly_candidates()
    config = PruneConfig(scope="full", capture_failure_rows=0)

    supplied = _date(2026, 5, 1)
    with caplog.at_level("INFO", logger="signalforge.prune.engine"):
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
            as_of=supplied,
        )

    info_records = [r for r in caplog.records if "anomaly: as_of resolved" in r.getMessage()]
    assert len(info_records) == 1
    # The resolved value is 2026-05-01, NOT today's date.
    assert "2026-05-01" in info_records[0].getMessage()
    payload = json.loads(info_records[0].args[0])  # type: ignore[index]
    assert payload["as_of"] == "2026-05-01"
    # The threaded value reached the compiler stub verbatim — the engine
    # did NOT silently substitute date.today().
    assert captured["as_of"] == supplied


def test_as_of_supplied_no_anomaly_candidate_emits_no_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#171 US-009 / DEC-001 — even when the caller supplies ``as_of``,
    the INFO log fires ONLY when an anomaly candidate is present. A
    supplied-but-unused ``as_of`` on a no-anomaly run stays silent so
    the log signal correlates with "this run consulted anomaly state".
    """
    from datetime import date as _date

    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_test("id")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    with caplog.at_level("INFO", logger="signalforge.prune.engine"):
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
            as_of=_date(2026, 5, 1),
        )

    matches = [r for r in caplog.records if "as_of resolved" in r.getMessage()]
    assert matches == []
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# #171 US-010 / DEC-009 / DEC-010 — ``_test_requires_source_table`` helper
#
# The helper centralises the per-variant bypass-routing predicate that BOTH
# engine sites (``all_bypass_to_source`` short-circuit AND per-test
# ``per_test_table_ref`` override) consult, so the two sites can never drift
# out of lockstep. Unit-tested below across the full variant × sample_strategy
# matrix; the behaviour-pin tests further down certify the engine actually
# threads the helper to both arms.
# ---------------------------------------------------------------------------


def _bypass_variants() -> list[Any]:
    """Construct one instance of each metadata-aggregate variant the helper
    must route to source.
    """
    from signalforge.draft.models import (
        CandidateTestRowCountAnomalyByPeriod,
        CandidateTestRowCountBetween,
        CandidateTestUniqueCombination,
    )

    return [
        CandidateTestRowCountBetween(minimum=1, maximum=1_000),
        CandidateTestUniqueCombination(columns=("a", "b")),
        CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at"),
    ]


def _non_bypass_variants() -> list[Any]:
    """Construct one instance of each non-bypassing (row-level) variant.

    Every variant whose compiled SQL is a row-level failing-rows SELECT
    is fine to evaluate on a sampled / materialised temp; ONLY the
    metadata-aggregate variants need the source override.
    """
    from signalforge.draft.models import (
        CandidateTestAcceptedValues,
        CandidateTestCustomSQL,
        CandidateTestNotNull,
        CandidateTestRelationships,
        CandidateTestUnique,
    )

    return [
        CandidateTestNotNull(column="id"),
        CandidateTestUnique(column="id"),
        CandidateTestAcceptedValues(column="status", values=("a", "b")),
        CandidateTestRelationships(column="customer_id", to="ref('customers')", field="id"),
        CandidateTestCustomSQL(sql="SELECT 1 FROM {{ this }} WHERE id IS NULL"),
    ]


@pytest.mark.parametrize("sample_strategy", ["materialised", "oneshot"])
def test_test_requires_source_table_routes_metadata_aggregates_to_source_under_sampling(
    sample_strategy: str,
) -> None:
    """#171 DEC-010 — every metadata-aggregate variant
    (``row_count_between``, ``unique_combination``,
    ``row_count_anomaly_by_period``) must route to the source table under
    BOTH ``sample_strategy="materialised"`` and
    ``sample_strategy="oneshot"``. This is the DEC-010 codification: the
    bypass is no longer materialised-only.

    Pins the full bypass-variant set in one parametrize so a future
    variant added to the helper grows the test surface automatically
    via ``_bypass_variants``.
    """
    from signalforge.prune.engine import _test_requires_source_table

    for test in _bypass_variants():
        assert _test_requires_source_table(test, sample_strategy) is True, (
            f"{type(test).__name__} must require source under sample_strategy={sample_strategy!r}"
        )


@pytest.mark.parametrize("sample_strategy", ["materialised", "oneshot"])
def test_test_requires_source_table_leaves_row_level_variants_on_compile_ref(
    sample_strategy: str,
) -> None:
    """#171 DEC-010 — every non-metadata-aggregate variant
    (``not_null``, ``unique``, ``accepted_values``, ``relationships``,
    ``custom_sql``) MUST NOT bypass to source under sampling — they
    consume the substituted ``compile_table_ref`` (sampled temp under
    ``materialised`` / source under ``oneshot``) per the #22
    materialised-sample contract.
    """
    from signalforge.prune.engine import _test_requires_source_table

    for test in _non_bypass_variants():
        name = type(test).__name__
        assert _test_requires_source_table(test, sample_strategy) is False, (
            f"{name} must NOT bypass to source under sample_strategy={sample_strategy!r}"
        )


def test_test_requires_source_table_full_scope_never_bypasses() -> None:
    """#171 DEC-010 — when prune scope is ``full`` (no sampling at all),
    the helper receives ``sample_strategy=None`` and MUST return ``False``
    for EVERY variant. There is no temp table to bypass on the no-sample
    path; ``compile_table_ref`` already resolves to source, so the
    override is a no-op. Pinned so a future refactor that accidentally
    returns ``True`` here would surface (and would silently route
    full-scope runs into the unnecessary bypass branch).
    """
    from signalforge.prune.engine import _test_requires_source_table

    for test in [*_bypass_variants(), *_non_bypass_variants()]:
        assert _test_requires_source_table(test, None) is False, (
            f"{type(test).__name__} must return False under sample_strategy=None"
        )


# ---------------------------------------------------------------------------
# #171 DEC-010 — engine-routing pins for the helper at both sites
#
# Behavioural pins that the two engine sites actually consult the helper.
# Compiler snapshots alone certify SQL shape; only these tests certify the
# engine hands the compiler the right ``TableRef``. Mirror the
# ``test_prune_tests_unique_combination_under_*`` precedent shape (#170
# US-005b) — these are the routing pins, not the SQL-shape pins.
# ---------------------------------------------------------------------------


def test_prune_tests_row_count_between_under_oneshot_sample_now_bypasses_to_source(
    tmp_path: Path,
) -> None:
    """#171 DEC-010 codification — under ``scope="sample"`` +
    ``sample_strategy="oneshot"``, when the only candidate is a
    ``row_count_between``, the engine's ``all_bypass_to_source``
    short-circuit fires and the test compiles directly against the
    SOURCE table — NO ``get_row_count`` lookup, NO sample CTE wrapping,
    NO temp table.

    Pre-#171 the inline isinstance check was unconditional on
    sample_strategy, so this behaviour already worked in practice for
    the all-bypass case. #171 makes it an EXPLICIT contract via
    :func:`_test_requires_source_table` — a future tightening of the
    helper that accidentally dropped the ``oneshot`` arm would fail
    loud here.

    Mirrors the precedent test
    ``test_prune_tests_row_count_between_under_materialised_references_source_not_temp_table``
    but flips ``sample_strategy`` to ``"oneshot"`` so the new
    contract surface is independently pinned.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # NO ``expect_get_table`` — the short-circuit skips
    # ``_resolve_sample_bucket`` (which would otherwise call
    # ``adapter.get_row_count``). NO ``expect_materialise_sample`` —
    # ``oneshot`` strategy never materialises in the first place. NO
    # ``expect_abort_session`` — no session opened.
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_row_count_test(minimum=100, maximum=10_000)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="oneshot",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "row_count_between"
    # The compiled SQL MUST reference the source production table — a
    # sampled COUNT would return the sample size, not the model's true
    # row count.
    assert "fake_project.dataset.orders" in decision.compiled_sql
    # The deterministic-sample CTE prefix MUST NOT appear — the helper's
    # ``oneshot`` arm prevented the sample wrapping.
    assert "_SESSION._sf_sample_" not in decision.compiled_sql
    # And no oneshot deterministic-sample CTE either (the compiler would
    # wrap source in a ``WITH sample AS (SELECT ... MOD(..., bucket) <
    # 1)`` CTE if scope="sample" + sample_bucket were threaded through).
    assert "WITH sample AS" not in decision.compiled_sql
    fake.assert_all_expectations_met()


def test_prune_tests_unique_combination_under_oneshot_sample_short_circuit_pinned(
    tmp_path: Path,
) -> None:
    """#171 DEC-010 sibling pin — same shape as the
    ``row_count_between`` oneshot pin above, for the
    ``unique_combination`` variant. The parametrised existing
    ``test_prune_tests_unique_combination_under_materialised_references_source_not_temp_table``
    already covers oneshot; this is a dedicated repetition focused on
    pinning the SHORT-CIRCUIT path (no warehouse pre-work) — a future
    change to ``all_bypass_to_source`` that accidentally gated on
    ``materialised`` only would fail here.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # NO ``expect_get_table`` / ``expect_materialise_sample`` /
    # ``expect_abort_session`` — short-circuit skips them all.
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _candidates_with_one_unique_combination_test(columns=("id", "customer_id"))
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="oneshot",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "unique_combination"
    assert "fake_project.dataset.orders" in decision.compiled_sql
    assert "_SESSION._sf_sample_" not in decision.compiled_sql
    fake.assert_all_expectations_met()


def test_mixed_per_test_override_routes_row_count_between_to_source_under_oneshot(
    tmp_path: Path,
) -> None:
    """#171 DEC-010 — load-bearing two-conditional pattern (per memory
    ``prune-engine-two-conditional-routing-pattern``). When a candidate
    list MIXES one bypassing variant (``row_count_between``) and one
    row-level variant (``not_null``) under ``scope=sample`` +
    ``sample_strategy=oneshot``, the engine's ``all_bypass_to_source``
    short-circuit MUST NOT fire (mixed list fails the
    ``all(_test_requires_source_table(...))`` predicate), and the
    per-test ``per_test_table_ref`` override must route each test
    individually — ``not_null`` consumes the substituted
    ``compile_table_ref`` (source under oneshot, with a sample CTE
    wrapping), ``row_count_between`` routes past to source directly
    (no sample CTE).

    Mirrors
    ``test_prune_tests_mixed_candidates_per_test_override_routes_unique_combination_to_source``
    (#170 QG Pass 3) but for the ``oneshot`` strategy where the previous
    behaviour was a no-op (compile_table_ref already equalled source)
    and the routing-difference signal lives in scope/sample_bucket
    threading, not in the TableRef itself. The load-bearing claim:
    ``row_count_between``'s compiled SQL contains NO sample CTE (the
    helper's bypass result causes the engine to thread compile_scope =
    "full" / sample_bucket=None for THIS test), while ``not_null``'s
    compiled SQL DOES wrap source in the deterministic-sample CTE.

    Without this pin, a regression that dropped the per-test arm's call
    to the helper (relying only on the short-circuit) would PASS the
    single-variant pin above silently (the short-circuit catches the
    all-bypass case) but break the mixed-candidate case where the per-
    test branch is the only arm that fires.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    # Mixed → short-circuit does NOT fire → engine falls into the oneshot
    # else-branch → ``_resolve_sample_bucket`` runs → ``get_row_count``
    # query.
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    # Two per-test compile queries; order is candidate iteration order
    # (column-scoped ``not_null`` first, then model-level
    # ``row_count_between``).
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    # NO ``expect_abort_session`` — oneshot never opens a session.
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="id",
                description="The order's primary key.",
                tests=(CandidateTestNotNull(column="id"),),
            ),
        ),
        tests=(CandidateTestRowCountBetween(minimum=100, maximum=10_000),),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="oneshot",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 2
    by_type = {d.test.type: d for d in result.decisions}
    assert {"not_null", "row_count_between"} <= set(by_type)

    not_null_sql = by_type["not_null"].compiled_sql
    rcb_sql = by_type["row_count_between"].compiled_sql

    # ``not_null`` is NOT a bypass variant — under oneshot it consumes
    # the substituted compile_table_ref (= source, since oneshot does not
    # materialise) AND the compiler wraps it in the deterministic-sample
    # CTE. The wrap is the routing signal that proves the per-test
    # override did NOT incorrectly bypass.
    assert "fake_project.dataset.orders" in not_null_sql
    assert "WITH sample AS" in not_null_sql, (
        f"not_null under scope=sample + oneshot must be sample-CTE-wrapped; got: {not_null_sql!r}"
    )

    # ``row_count_between`` is a bypass variant — the per-test override
    # routes it past the sample wrapping; compiled SQL hits the source
    # directly with NO sample CTE.
    assert "fake_project.dataset.orders" in rcb_sql
    assert "WITH sample AS" not in rcb_sql, (
        f"row_count_between under oneshot must bypass sample wrapping; got: {rcb_sql!r}"
    )
    # And it MUST NOT reference the (never-created) temp table either.
    assert "_SESSION._sf_sample_" not in rcb_sql

    fake.assert_all_expectations_met()


def test_mixed_per_test_override_routes_row_count_anomaly_to_source_under_oneshot(
    tmp_path: Path,
) -> None:
    """#171 DEC-010 — sibling pin for the third metadata-aggregate
    variant (``row_count_anomaly_by_period``). Same shape as the
    row_count_between mixed-candidate test above; certifies the helper's
    third arm reaches the per-test override under ``oneshot``.

    Updated for US-011: the anomaly variant's compiler arm returns a
    TUPLE (``(stats_sql, violation_sql)``); the engine now runs BOTH the
    stats query AND the violation query in sequence. The load-bearing
    routing assertion remains on the per-test branch firing (proved by
    the anomaly decision landing in ``decisions``) AND the per-test
    override sending the anomaly variant to the SOURCE table (not the
    sampled compile ref) — which now manifests as the stats query
    referencing the source qualified name rather than a wrapped sample
    CTE.
    """
    from signalforge.draft.models import CandidateTestRowCountAnomalyByPeriod

    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    # Mixed → ``all_bypass_to_source`` does NOT fire → ``_resolve_sample_bucket``
    # runs (one ``expect_get_table``).
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    # First: the ``not_null`` test (always-passes via sample-wrap CTE).
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    # US-011: the anomaly variant compiles to (stats_sql, violation_sql).
    # The stats query (mad, non-seasonal default) lands first — return
    # enough periods to clear the cold-start gate (default
    # min_samples_per_bucket=3). Then the violation query — wrapped in
    # COUNT(*) — fires with zero failures (always-passes).
    fake.expect_query(
        matching=r"^WITH history AS",
        returns=[{"median": 100.0, "mad": 5.0, "n": 28}],
    )
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="id",
                description="The order's primary key.",
                tests=(CandidateTestNotNull(column="id"),),
            ),
        ),
        tests=(CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at"),),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="oneshot",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    # Both tests reached a decision — the engine did NOT crash on the
    # tuple compile result. Specific routing of the tuple lands in US-011.
    assert result.total_tests == 2
    by_type = {d.test.type: d for d in result.decisions}
    assert {"not_null", "row_count_anomaly_by_period"} <= set(by_type)

    # ``not_null`` is NOT a bypass variant — under oneshot it consumes
    # the substituted compile_table_ref (=source) AND the compiler wraps
    # it in the deterministic-sample CTE. The wrap is the routing signal
    # that proves the per-test override did NOT bypass for the row-level
    # variant.
    not_null_sql = by_type["not_null"].compiled_sql
    assert "fake_project.dataset.orders" in not_null_sql
    assert "WITH sample AS" in not_null_sql

    # US-011: the anomaly variant runs through the two-query path.
    # Stats returns 28 periods (clears the cold-start gate), violation
    # returns 0 failures → always-passes → dropped. The compiled SQL on
    # the decision is the violation SQL — the engine records that as the
    # "outcome-bearing" SQL. The per-test override sent the variant to
    # the SOURCE table; the stats query (issued internally) AND the
    # violation query both reference ``fake_project.dataset.orders``.
    anomaly_decision = by_type["row_count_anomaly_by_period"]
    assert anomaly_decision.decision == "dropped"
    assert anomaly_decision.reason == "always-passes"
    # The decision's stats are populated from the parsed stats-query
    # result — proves the discriminated-union dispatch reached MAD.
    assert anomaly_decision.stats is not None
    assert anomaly_decision.stats.method == "mad"
    assert anomaly_decision.stats.n_periods == 28
    # Violation SQL references the source table (per-test override fired).
    assert "fake_project.dataset.orders" in anomaly_decision.compiled_sql

    fake.assert_all_expectations_met()


def test_prune_tests_all_anomaly_under_oneshot_short_circuits_no_warehouse_prework(
    tmp_path: Path,
) -> None:
    """#171 DEC-010 — when every candidate is the anomaly variant under
    ``scope=sample`` + ``sample_strategy=oneshot``, the
    ``all_bypass_to_source`` short-circuit fires: NO ``get_row_count``
    call, NO ``materialise_sample`` call, NO session.

    Belt-and-braces with the unit test of the helper above; here we pin
    the engine actually CONSULTS the helper at the short-circuit site.
    Updated for US-011: the anomaly variant's compile result is a
    ``(stats_sql, violation_sql)`` tuple; the engine runs both queries
    against the SOURCE table (no temp table, no sample CTE) because the
    bypass routed past the substitution. The short-circuit avoids
    ``get_row_count`` / ``materialise_sample`` either way, which is the
    load-bearing contract for THIS site.
    """
    from signalforge.draft.models import CandidateTestRowCountAnomalyByPeriod

    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # NO ``expect_get_table`` — short-circuit skips ``_resolve_sample_bucket``.
    # NO ``expect_materialise_sample``. NO ``expect_abort_session``.
    # Two queries: stats (28 periods → clears cold-start) then violation
    # (0 failures → always-passes → dropped).
    fake.expect_query(
        matching=r"^WITH history AS",
        returns=[{"median": 100.0, "mad": 5.0, "n": 28}],
    )
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at"),),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="oneshot",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 1
    decision = result.decisions[0]
    assert decision.test.type == "row_count_anomaly_by_period"
    # US-011: two-query path runs end-to-end → always-passes drop.
    assert decision.decision == "dropped"
    assert decision.reason == "always-passes"
    # Stats populated on the decision (audit-of-record per DEC-006).
    assert decision.stats is not None
    assert decision.stats.method == "mad"
    assert decision.stats.n_periods == 28
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# #171 US-011 — two-query split + cold-start routing + DOW degrade.
#
# These tests pin the engine-side handling of the
# ``(stats_sql, violation_sql)`` tuple the compiler returns for
# ``row_count_anomaly_by_period``:
#
#   * Happy path: stats clears cold-start gate → violation runs → standard
#     decision routing; ``stats`` populated on PruneDecision AND PruneEvent.
#   * Cold-start: ``stats.n_periods < min_samples_per_bucket`` →
#     ``kept-without-evidence`` with structured ``why``; Query 2 SKIPPED
#     (no warehouse call). Pinned by the fake adapter's
#     ``assert_all_expectations_met`` (no unmet violation-query
#     expectation queued).
#   * DOW degrade: seasonality="dow" + any thin per-DOW bucket →
#     recompute stats query without DOW + ONE WARNING line + proceed.
#   * Adapter ``run_stats_query`` raises ``StatsQueryNotSupportedError``
#     → routes to ``kept-without-evidence`` via the standard
#     ``WarehouseError`` catch surface (Postgres / future no-anomaly
#     adapter contract).
# ---------------------------------------------------------------------------


def _make_anomaly_only_candidates(
    *,
    date_column: str = "ordered_at",
    method: str = "mad",
    seasonality: str = "none",
    min_samples_per_bucket: int = 3,
) -> CandidateSchema:
    """Build a CandidateSchema carrying one model-level
    ``row_count_anomaly_by_period`` test (and no other tests).
    """
    from signalforge.draft.models import CandidateTestRowCountAnomalyByPeriod

    return CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestRowCountAnomalyByPeriod(
                date_column=date_column,
                method=method,  # type: ignore[arg-type]
                seasonality=seasonality,  # type: ignore[arg-type]
                min_samples_per_bucket=min_samples_per_bucket,
            ),
        ),
    )


def test_us011_two_query_happy_path_runs_both_queries_and_populates_stats(
    tmp_path: Path,
) -> None:
    """US-011 happy path — the anomaly stats query clears the
    cold-start gate, the violation query runs, the decision carries
    ``stats`` (parsed into the typed :class:`MadStats` discriminated-
    union member via :func:`_parse_anomaly_stats`).

    Pins the load-bearing two-query order: stats first (matched against
    ``^WITH history AS``), violation second (the ``SELECT COUNT(*)``
    wrap around the violation SQL). Both queries reach
    :meth:`adapter.run_stats_query` and :meth:`adapter.run_test_sql`
    respectively; the fake's ``assert_all_expectations_met`` proves no
    third query fired (no spurious extra warehouse work).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(
        matching=r"^WITH history AS",
        returns=[{"median": 50.0, "mad": 2.0, "n": 14}],
    )
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _make_anomaly_only_candidates()
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 1
    decision = result.decisions[0]
    assert decision.test.type == "row_count_anomaly_by_period"
    # Violation returned 0 failures → always-passes drop.
    assert decision.decision == "dropped"
    assert decision.reason == "always-passes"
    # ``stats`` populated via discriminated-union dispatch on ``method``.
    assert decision.stats is not None
    assert decision.stats.method == "mad"
    assert decision.stats.n_periods == 14
    # The two queries hit (fake raises on unconsumed expectations).
    fake.assert_all_expectations_met()


def test_us011_cold_start_skips_violation_query_and_routes_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """US-011 cold-start gate — ``stats.n_periods <
    test.min_samples_per_bucket`` routes to ``kept-without-evidence``
    AND skips the violation query entirely (NO warehouse call for
    Query 2).

    The load-bearing assertion is that the fake adapter has NO violation-
    query expectation queued; ``assert_all_expectations_met`` would
    catch a spurious extra call.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Stats returns 1 period — below the default min_samples_per_bucket=3.
    fake.expect_query(
        matching=r"^WITH history AS",
        returns=[{"median": 100.0, "mad": 5.0, "n": 1}],
    )
    # Intentionally NO ``expect_query(SELECT COUNT(*))`` — the cold-start
    # gate must skip the violation query entirely. If the engine
    # erroneously issues it the fake raises "unexpected query".
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _make_anomaly_only_candidates()
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 1
    decision = result.decisions[0]
    assert decision.decision == "kept"
    assert decision.reason == "kept-without-evidence"
    # Structured ``why`` names the observed/required period counts so a
    # reviewer reading the diff sees the missing history at a glance.
    assert "insufficient history" in decision.why
    assert "1/3" in decision.why
    # ``stats`` carries the partial state (n_periods=1).
    assert decision.stats is not None
    assert decision.stats.method == "mad"
    assert decision.stats.n_periods == 1
    fake.assert_all_expectations_met()


def test_us011_dow_thin_per_bucket_recompiles_non_seasonal_emits_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """US-011 DOW degrade (DEC-003) — seasonality="dow" + at least one
    per-DOW bucket below ``min_samples_per_bucket`` triggers a
    recompile of the stats query WITHOUT DOW partitioning. Exactly
    ONE WARNING line is emitted; the engine proceeds with the non-
    seasonal stats and runs the violation query as normal.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # First stats query (seasonal) — returns per-DOW rows where DOW=3
    # has only 1 period (below default floor=3) → triggers degrade.
    seasonal_rows = [
        {"dow": 0, "median": 100.0, "mad": 5.0, "n": 4},
        {"dow": 1, "median": 105.0, "mad": 4.0, "n": 4},
        {"dow": 2, "median": 110.0, "mad": 6.0, "n": 4},
        {"dow": 3, "median": 90.0, "mad": 3.0, "n": 1},  # THIN
        {"dow": 4, "median": 120.0, "mad": 5.0, "n": 4},
        {"dow": 5, "median": 80.0, "mad": 2.0, "n": 4},
        {"dow": 6, "median": 95.0, "mad": 3.0, "n": 4},
    ]
    # Second stats query (degraded non-seasonal) — returns one row with
    # the aggregate baseline.
    non_seasonal_rows = [{"median": 100.0, "mad": 5.0, "n": 25}]
    # Engine issues two stats queries (seasonal then non-seasonal) plus
    # the violation query. The seasonal stats SQL has ``GROUP BY period,
    # dow``; the non-seasonal has ``GROUP BY period``. Distinguish via
    # the GROUP BY shape so the fake queues the right return.
    fake.expect_query(matching=r"GROUP BY period, dow", returns=seasonal_rows)
    fake.expect_query(matching=r"GROUP BY period\)", returns=non_seasonal_rows)
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _make_anomaly_only_candidates(seasonality="dow")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    # Exactly ONE DOW-degrade WARNING.
    degrade_records = [
        r for r in caplog.records if "DOW degraded to non-seasonal" in r.getMessage()
    ]
    assert len(degrade_records) == 1, (
        f"expected exactly one DOW-degrade WARNING; got {len(degrade_records)}: "
        f"{[r.getMessage() for r in degrade_records]}"
    )
    # Lazy-format JSON contract (DEC-017): the template uses %s, the
    # JSON payload rides on args.
    assert degrade_records[0].msg == (
        "anomaly: DOW degraded to non-seasonal due to thin per-DOW history: %s"
    )
    payload = json.loads(degrade_records[0].args[0])  # type: ignore[index]
    assert payload["min_samples_per_bucket"] == 3
    # The thin bucket's count appears in the per_dow_counts breakdown.
    # The fake adapter returned raw BigQuery DAYOFWEEK value `3` (Tuesday
    # under the Sun=1 convention); the engine normalises via
    # ``_normalize_dow_to_posix`` so dict keys land in POSIX space
    # (Mon=0..Sun=6). BQ raw=3 (Tue) → POSIX 1 (per #171 CodeRabbit
    # finding #10 — cross-dialect key consistency).
    assert payload["per_dow_counts"]["1"] == 1

    # The engine proceeded — the violation query ran and the decision
    # reflects always-passes (0 failures).
    decision = result.decisions[0]
    assert decision.decision == "dropped"
    assert decision.reason == "always-passes"
    # ``stats`` reflects the DEGRADED non-seasonal stats (n_periods=25),
    # NOT the seasonal aggregate.
    assert decision.stats is not None
    assert decision.stats.method == "mad"
    assert decision.stats.n_periods == 25
    assert decision.stats.per_dow is None

    fake.assert_all_expectations_met()


def test_us011_dow_healthy_buckets_use_seasonal_stats_no_degrade(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """US-011 DOW healthy path — every per-DOW bucket meets the floor →
    NO degrade WARNING fires, the engine proceeds with the seasonal
    stats, ``per_dow`` is populated.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Every per-DOW bucket >= min_samples_per_bucket=3.
    seasonal_rows = [{"dow": d, "median": 100.0 + d, "mad": 5.0, "n": 4} for d in range(7)]
    fake.expect_query(matching=r"GROUP BY period, dow", returns=seasonal_rows)
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _make_anomaly_only_candidates(seasonality="dow")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    # NO degrade warnings — every per-DOW bucket cleared the floor.
    degrade = [r for r in caplog.records if "DOW degraded" in r.getMessage()]
    assert degrade == [], f"expected no degrade WARNINGs; got {[r.getMessage() for r in degrade]}"

    decision = result.decisions[0]
    assert decision.decision == "dropped"
    assert decision.stats is not None
    assert decision.stats.method == "mad"
    assert decision.stats.per_dow is not None
    assert set(decision.stats.per_dow) == set(range(7))
    # Aggregate top-level n_periods is the sum across DOWs.
    assert decision.stats.n_periods == 28

    fake.assert_all_expectations_met()


def test_us011_stats_populated_on_prune_event_audit(tmp_path: Path) -> None:
    """US-011 / DEC-013 — :attr:`PruneEvent.stats` is the audit-of-record;
    the engine writes ``stats`` durably on every anomaly decision (the
    cold-start path, kept, and dropped routings).

    Reads the JSONL audit back and verifies the ``stats`` field is
    present with the discriminator (``method``) and ``n_periods``. The
    happy-path drop is sufficient as a representative — the cold-start
    audit pin and the routing-matrix coverage live in their own tests.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(
        matching=r"^WITH history AS",
        returns=[{"mean": 50.0, "stddev": 3.0, "n": 21}],
    )
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _make_anomaly_only_candidates(method="zscore")
    config = PruneConfig(scope="full", capture_failure_rows=0)

    prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1
    record = json.loads(audit_lines[0])
    assert record["test"]["type"] == "row_count_anomaly_by_period"
    assert record["stats"] is not None
    assert record["stats"]["method"] == "zscore"
    assert record["stats"]["mu"] == 50.0
    assert record["stats"]["sigma"] == 3.0
    assert record["stats"]["n_periods"] == 21

    fake.assert_all_expectations_met()


def test_us011_stats_populated_on_cold_start_audit(tmp_path: Path) -> None:
    """US-011 — cold-start path also persists ``stats`` to the JSONL
    audit (the partial state is the load-bearing audit signal: a
    reviewer needs to see what little history WAS observed).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(
        matching=r"^WITH history AS",
        returns=[{"median": 100.0, "mad": 5.0, "n": 2}],
    )
    # NO violation query expectation — cold-start skips it.
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _make_anomaly_only_candidates(min_samples_per_bucket=5)
    config = PruneConfig(scope="full", capture_failure_rows=0)

    prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1
    record = json.loads(audit_lines[0])
    assert record["reason"] == "kept-without-evidence"
    assert record["stats"] is not None
    assert record["stats"]["method"] == "mad"
    assert record["stats"]["n_periods"] == 2
    assert "insufficient history" in record["why"]
    fake.assert_all_expectations_met()


def test_us011_stats_query_warehouse_error_routes_kept_without_evidence(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """US-011 — when ``adapter.run_stats_query`` raises a
    :class:`WarehouseError`, the engine routes the anomaly test to
    ``kept-without-evidence`` (skips the violation query entirely).
    The standard ``WarehouseError`` catch surface + ``kept-without-
    evidence`` WARNING fire — same shape as the violation-query failure
    path, but distinguished by the ``phase`` log field.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(
        matching=r"^WITH history AS",
        returns=TableNotFoundError(table="fake_project.dataset.orders"),
    )
    # NO violation-query expectation — the stats-query failure short-
    # circuits the loop body.
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _make_anomaly_only_candidates()
    config = PruneConfig(scope="full", capture_failure_rows=0)

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    decision = result.decisions[0]
    assert decision.decision == "kept"
    assert decision.reason == "kept-without-evidence"
    assert "TableNotFoundError" in decision.why
    # The ``phase`` log field distinguishes stats vs violation failures.
    matches = [r for r in caplog.records if "kept-without-evidence" in r.getMessage()]
    assert len(matches) == 1
    payload = json.loads(matches[0].args[0])  # type: ignore[index]
    assert payload["phase"] == "anomaly_stats_query"
    fake.assert_all_expectations_met()


def test_us011_adapter_without_run_stats_query_routes_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """US-011 — when the active adapter has not grown a
    ``run_stats_query`` override (e.g. a Postgres stub), the ABC
    default raise of :class:`StatsQueryNotSupportedError` flows through
    the standard ``WarehouseError`` catch surface and routes the
    anomaly test to ``kept-without-evidence``. Conservative-bias
    contract: an anomaly variant against an adapter that cannot evaluate
    it is KEPT (with a structured ``why``), never silently dropped.
    """
    from signalforge.warehouse.errors import StatsQueryNotSupportedError
    from signalforge.warehouse.models import (
        BIGQUERY_DIALECT,
    )
    from signalforge.warehouse.models import (
        TestResult as _TestResult,
    )

    class _NoStatsAdapter(WarehouseAdapter):
        """Inherits the ABC default ``run_stats_query`` raise; supplies
        the minimum surface ``prune_tests`` consumes (dialect + context
        manager + ``run_test_sql``)."""

        def __enter__(self) -> WarehouseAdapter:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def dialect(self) -> Dialect:
            return BIGQUERY_DIALECT

        def sample_rows(
            self,
            table: TableRef,
            n: int,
            *,
            partition_filter: PartitionFilter | None = None,
        ) -> list[dict[str, object]]:
            raise NotImplementedError

        def column_stats(self, table: TableRef, column: str) -> ColumnStats:
            raise NotImplementedError

        def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> _TestResult:
            # Never called on this path — the stats-query failure short-
            # circuits before the violation query.
            raise AssertionError("run_test_sql should not be called after a stats-query failure")

    adapter = _NoStatsAdapter()

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _make_anomaly_only_candidates()
    config = PruneConfig(scope="full", capture_failure_rows=0)
    audit_path = tmp_path / "prune.jsonl"

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.decision == "kept"
    assert decision.reason == "kept-without-evidence"
    assert "StatsQueryNotSupportedError" in decision.why
    # The typed error name is also what an end-to-end CLI surface keys on.
    assert StatsQueryNotSupportedError.__name__ in decision.why


def test_us011_defensive_arm_no_longer_fires_for_anomaly_variant(
    tmp_path: Path,
) -> None:
    """US-011 — the pre-US-011 defensive arm ('two-query split not yet
    wired') is REPLACED. A successful two-query run never produces a
    ``kept-without-evidence`` reason whose ``why`` mentions
    ``"#171 US-011 pending"``. Guards against an accidental revert that
    re-introduces the defensive route on the happy path.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(
        matching=r"^WITH history AS",
        returns=[{"median": 100.0, "mad": 5.0, "n": 14}],
    )
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _make_anomaly_only_candidates()
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    # Real two-query handling → always-passes drop, NOT the stub.
    assert decision.reason == "always-passes"
    assert "US-011 pending" not in decision.why
    assert "two-query split not yet wired" not in decision.why
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# #171 US-017 — ``as_of`` reproducibility (unit determinism)
# ---------------------------------------------------------------------------


def test_as_of_reproducibility_byte_equal_compiled_sql(tmp_path: Path) -> None:
    """#171 US-017 / DEC-001 — same input + same ``as_of`` produces
    byte-equal :attr:`PruneEvent.compiled_sql` across runs; a different
    ``as_of`` produces different SQL.

    The :class:`row_count_anomaly_by_period` variant carves out the
    Architectural Commitment #5 ("same input → same prune decision")
    contract: the decision is *time-bound* by construction. The carve-out
    is executed by threading an explicit ``as_of`` through CLI →
    :func:`prune_tests` → compiled SQL → :attr:`PruneEvent.as_of`.
    Reproducibility is restored at the ``(model, as_of)`` granularity:
    same input + same ``as_of`` = same compiled SQL = same decision.

    This unit test pins both halves of the contract:

    1. **Determinism within an ``as_of``.** Two ``prune_tests`` calls
       with ``as_of=date(2026, 5, 1)`` produce byte-equal
       ``PruneEvent.compiled_sql``. The compiled SQL is the violation
       query (per the engine's happy-path audit record) which embeds the
       ``as_of`` literal via :attr:`Dialect.date_literal_template`.
    2. **Variance across ``as_of``.** A third ``prune_tests`` call with
       ``as_of=date(2026, 5, 2)`` produces a different
       ``PruneEvent.compiled_sql`` — proves the ``as_of`` value
       actually threads through to the compile step (vs. being
       accidentally ignored / shadowed somewhere in the engine).

    The fake adapter's expectation queue is consumed once per
    ``prune_tests`` call, so each run uses a fresh fake adapter + a
    fresh audit path. The canned stats (28 periods, MAD method) clear
    the cold-start gate so the violation query actually runs and the
    decision's ``compiled_sql`` field carries the violation SQL (the
    cold-start path would record the stats SQL instead — a parallel
    determinism contract but a different surface).
    """
    from datetime import date as _date

    candidates = _make_anomaly_only_candidates()
    model = _make_orders_model()
    manifest = _make_manifest(model)
    config = PruneConfig(scope="full", capture_failure_rows=0)

    def _run_once(audit_path: Path, as_of: _date) -> PruneEvent:
        """Prime a fresh fake + adapter, run prune_tests, read back the
        single PruneEvent from the audit JSONL.

        Each call returns the typed event so the caller can read
        ``compiled_sql`` (the violation SQL on the happy path) directly.
        Fresh fake per call because :meth:`FakeBigQueryClient.expect_query`
        consumes its expectations LIFO — sharing the fake across runs
        would either drain mid-test or require re-priming inside a tight
        loop.
        """
        fake = FakeBigQueryClient(project="fake_project")
        # Stats query — return 28 periods (well above the default
        # ``min_samples_per_bucket=3``) so the cold-start gate passes
        # and the violation query runs. The exact stat values do not
        # affect the violation SQL bytes (which is what the determinism
        # contract pins) — they only feed the band check downstream.
        fake.expect_query(
            matching=r"^WITH history AS",
            returns=[{"median": 100.0, "mad": 5.0, "n": 28}],
        )
        # Violation query — return zero failures (the decision routing
        # doesn't matter for THIS contract; the load-bearing surface is
        # ``compiled_sql`` bytes, which the engine records regardless of
        # the always-passes / kept verdict).
        fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
        adapter = _make_adapter(fake)

        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
            as_of=as_of,
        )
        fake.assert_all_expectations_met()

        # Read back the single PruneEvent via typed validation so the
        # ``compiled_sql`` / ``as_of`` field assertions key on the
        # production audit shape (not a hand-rolled dict).
        raw = _read_audit_lines(audit_path)
        assert len(raw) == 1, f"expected exactly one PruneEvent; got {len(raw)}"
        return PruneEvent.model_validate(raw[0])

    # --- Run 1 + Run 2 at the same ``as_of`` --------------------------
    as_of_a = _date(2026, 5, 1)
    event_run_1 = _run_once(tmp_path / "run1.jsonl", as_of_a)
    event_run_2 = _run_once(tmp_path / "run2.jsonl", as_of_a)

    # 1. Byte-equal compiled SQL across the two runs. Architectural
    #    Commitment #5 restored at ``(model, as_of)`` granularity.
    assert event_run_1.compiled_sql == event_run_2.compiled_sql, (
        "PruneEvent.compiled_sql must be byte-equal across two runs with the "
        "same ``as_of`` (same input + same ``as_of`` = same decision per "
        f"DEC-001). Run 1: {event_run_1.compiled_sql!r}; "
        f"Run 2: {event_run_2.compiled_sql!r}"
    )

    # 2. ``PruneEvent.as_of`` carries the supplied value verbatim (NOT
    #    ``None``; NOT ``date.today()``). The audit field is the
    #    operator-recovery surface — a missing/wrong value defeats the
    #    reproducibility carve-out.
    assert event_run_1.as_of == as_of_a
    assert event_run_2.as_of == as_of_a

    # 3. The compiled SQL embeds the ``as_of`` literal — sanity check
    #    that the determinism is not vacuously true (e.g. the compiler
    #    isn't producing a constant string). The ISO 8601 literal
    #    appears via the dialect's ``date_literal_template`` (BigQuery
    #    default: ``DATE('YYYY-MM-DD')``).
    assert as_of_a.isoformat() in event_run_1.compiled_sql, (
        f"expected the as_of literal {as_of_a.isoformat()!r} to appear in the "
        f"compiled SQL; got {event_run_1.compiled_sql!r} — the date_literal_template "
        "may have dropped the value or the compiler is ignoring as_of."
    )

    # --- Run 3 at a DIFFERENT ``as_of`` -------------------------------
    as_of_b = _date(2026, 5, 2)
    event_run_3 = _run_once(tmp_path / "run3.jsonl", as_of_b)

    # 4. Different ``as_of`` → different compiled SQL. Proves the
    #    threading is real: a refactor that silently dropped the kwarg
    #    or shadowed it with ``date.today()`` would produce a stable
    #    compiled SQL string here (the same as run 1 / 2) and this
    #    assertion would fail.
    assert event_run_3.compiled_sql != event_run_1.compiled_sql, (
        "PruneEvent.compiled_sql must differ when ``as_of`` differs (proves the "
        "threading from engine → compiler is intact; a regression that drops "
        "the kwarg silently would shadow with ``date.today()`` and produce the "
        f"same SQL for distinct as_of values). Both produced: "
        f"{event_run_1.compiled_sql!r}"
    )
    assert event_run_3.as_of == as_of_b
    assert as_of_b.isoformat() in event_run_3.compiled_sql


# ---------------------------------------------------------------------------
# #154 US-004: manifest-ingested custom_sql (from_manifest=True) prune routing.
#
# An ingested body is dbt's already-Jinja-resolved compiled_code referencing
# dbt's OWN quoted relation. DEC-007 — evaluated full-scope against the source
# under EITHER sample strategy (dbt's quoted relation cannot bind the
# {{ this }} sample substitution). DEC-013 — comment-tolerant validation.
# DEC-012 — determinism belt-and-braces fallback → kept-without-evidence.
# The DropReason stays the locked 5-value Literal; conservative-bias preserved.
# ---------------------------------------------------------------------------

# dbt quotes the relation with backticks — the shape that would degrade to
# kept-without-evidence under the {{ this }} sample substitution (#154 AR row 8).
_INGESTED_SOURCE_BODY = "select id\nfrom `fake_project`.`dataset`.`orders`\nwhere status = 'BAD'"


def _ingested_custom_sql_candidates(sql: str) -> CandidateSchema:
    """A model-level CandidateSchema carrying one manifest-ingested custom_sql
    (from_manifest=True) — the shape ``read_manifest_tests`` produces."""
    return CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(CandidateTestCustomSQL(sql=sql, from_manifest=True),),
    )


# --- #268 US-006 (DEC-014) — the ingested-routing observability -------------
#
# The engine logs lazy-format ``%s`` + ``json.dumps({...})`` (the DEC-017 grep
# gate), so a test reads the SIGNAL, not the prose: decode the payload and
# assert on the decoded dict. Keying on the message prefix keeps these helpers
# from picking up any sibling INFO/DEBUG the engine emits.
_INGESTED_ROUTING_INFO_PREFIX = "ingested custom_sql routing:"
_INGESTED_BYPASS_DEBUG_PREFIX = "ingested custom_sql bypassed to source:"


def _decode_log_payloads(
    caplog: pytest.LogCaptureFixture, *, level: str, prefix: str
) -> list[dict[str, object]]:
    """Decode the ``json.dumps`` payload of every matching engine log record."""
    payloads: list[dict[str, object]] = []
    for record in caplog.records:
        message = record.getMessage()
        if record.levelname != level or not message.startswith(prefix):
            continue
        payloads.append(json.loads(message[len(prefix) :].strip()))
    return payloads


def _ingested_routing_payloads(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """The decoded payload of every DEC-014 aggregate INFO (expected: exactly 1)."""
    return _decode_log_payloads(caplog, level="INFO", prefix=_INGESTED_ROUTING_INFO_PREFIX)


def _ingested_bypass_breadcrumbs(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """The decoded payload of every DEC-014 per-test bypass DEBUG breadcrumb."""
    return _decode_log_payloads(caplog, level="DEBUG", prefix=_INGESTED_BYPASS_DEBUG_PREFIX)


def test_prune_tests_ingested_custom_sql_tautology_dropped_always_passes(
    tmp_path: Path,
) -> None:
    """An ingested body that returns zero failing rows is dropped with
    ``reason="always-passes"`` — pruned exactly like any other candidate."""
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _ingested_custom_sql_candidates(_INGESTED_SOURCE_BODY)
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 1
    decision = result.decisions[0]
    assert decision.test_anchor == "model"
    assert decision.decision == "dropped"
    assert decision.reason == "always-passes"
    assert decision.failures == 0
    # The dispatched SQL is the compiled_code body verbatim.
    assert decision.compiled_sql == _INGESTED_SOURCE_BODY
    fake.assert_all_expectations_met()


def test_prune_tests_ingested_custom_sql_real_failure_kept(tmp_path: Path) -> None:
    """An ingested body that returns failing rows on an untrusted model is
    kept with ``reason="kept"`` — real signal survives the prune."""
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 4}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _ingested_custom_sql_candidates(_INGESTED_SOURCE_BODY)
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.decision == "kept"
    assert decision.reason == "kept"
    assert decision.failures == 4
    fake.assert_all_expectations_met()


def test_prune_tests_ingested_custom_sql_non_deterministic_kept_without_evidence(
    tmp_path: Path,
) -> None:
    """DEC-012 belt-and-braces: a non-deterministic ingested body reaching the
    compiler routes to ``kept-without-evidence`` (decision="kept") with NO
    warehouse call — the DropReason enum stays the locked 5-value Literal."""
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Intentionally NO expect_query — the compiler's determinism fallback
    # short-circuits before any dispatch; any warehouse call is unexpected.
    adapter = _make_adapter(fake)

    body = (
        "select id\nfrom `fake_project`.`dataset`.`orders`\nwhere created_at > CURRENT_TIMESTAMP()"
    )
    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _ingested_custom_sql_candidates(body)
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.decision == "kept"
    assert decision.reason == "kept-without-evidence"
    assert "non-deterministic" in decision.why
    assert decision.compiled_sql == ""
    fake.assert_all_expectations_met()


def test_prune_tests_ingested_custom_sql_under_sample_evaluates_full_scope(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#154 DEC-007, narrowed by #268 DEC-010 (behavioural, not just a decision
    snapshot): a LONE ingested body requested under ``scope=sample`` is still
    evaluated FULL-SCOPE against the source. Since #268 an ingested body CAN be
    relation-rewritten onto the materialised sample — but only from TWO samplable
    candidates up (the ``SELECT *`` CTAS reads every column and cannot pay for
    itself on one narrow test), so this single-candidate batch keeps bypassing.

    The dispatched SQL references the source relation and NEVER a
    ``_SESSION._sf_sample_*`` temp table, no ``materialise_sample`` is called
    (all candidates bypass to source), and exactly ONE DEC-014 aggregate INFO
    fires — reporting ``sampled_count=0`` and the ``below-min-samplable`` reason
    that DEC-010 demoted this lone candidate with (US-006 replaced the old #154
    "scope=sample requested" wording, which became a half-truth once a sibling
    candidate could be sampled)."""
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # Only expect the COUNT(*) — NO expect_materialise_sample / expect_get_table:
    # the all-bypass short-circuit skips sampling pre-work for the ingested body.
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _ingested_custom_sql_candidates(_INGESTED_SOURCE_BODY)
    # Default sample_strategy is "materialised"; scope=sample would normally
    # materialise a temp table — the ingested body must bypass that entirely.
    config = PruneConfig(scope="sample", sample_size=100_000, capture_failure_rows=0)

    with caplog.at_level("INFO", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    decision = result.decisions[0]
    # Behavioural assertion on the dispatched SQL (a decision snapshot alone
    # pins shape, not routing): the body runs as-is against the source.
    assert decision.compiled_sql == _INGESTED_SOURCE_BODY
    assert "`fake_project`.`dataset`.`orders`" in decision.compiled_sql
    assert "_SESSION" not in decision.compiled_sql
    assert "_sf_sample_" not in decision.compiled_sql
    # The verdict still lands (failures=0 → always-passes drop) — full-scope
    # evaluation actually ran, it did not degrade to kept-without-evidence.
    assert decision.decision == "dropped"
    assert decision.reason == "always-passes"

    payloads = _ingested_routing_payloads(caplog)
    assert len(payloads) == 1, (
        f"expected exactly ONE ingested-routing INFO; got {len(payloads)}: {payloads}"
    )
    assert payloads[0] == {
        "model_unique_id": model.unique_id,
        "sample_strategy": "materialised",
        "ingested_count": 1,
        "sampled_count": 0,
        "bypassed_to_source_count": 1,
        "bypass_reasons": {"below-min-samplable": 1},
    }
    fake.assert_all_expectations_met()


def test_prune_tests_mixed_ingested_and_drafted_per_test_routing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The per-test override arm (NOT just the all-bypass short-circuit): a
    batch mixing a drafted built-in (``not_null``, samples via the CTE) and a
    manifest-ingested custom_sql (full-scope against source) under
    ``scope=sample`` + ``oneshot`` routes each candidate independently. The
    #170 QG Pass 3 lesson — a single-candidate test only exercises the
    short-circuit; the mixed batch is what pins the per-test ``per_test_table_ref``
    override."""
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # oneshot samples the built-in → one num_rows lookup for the bucket.
    fake.expect_get_table(
        ref=TableRef(project="fake_project", dataset="dataset", name="orders"),
        returns=FakeTable(num_rows=1_000_000),
    )
    # Query 1: the not_null (sampled). Query 2: the ingested body (full-scope).
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="id",
                description="The order's primary key.",
                tests=(CandidateTestNotNull(column="id"),),
            ),
        ),
        tests=(CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY, from_manifest=True),),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="oneshot",
    )

    with caplog.at_level("INFO", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    # Iteration order: column tests first, then model-level tests.
    not_null_decision = result.decisions[0]
    ingested_decision = result.decisions[1]
    assert not_null_decision.test_anchor == "column.id"
    assert ingested_decision.test_anchor == "model"

    # The drafted built-in is sampled: its compiled SQL wraps the sample CTE.
    assert "WITH sample" in not_null_decision.compiled_sql

    # The ingested body is full-scope against source: verbatim, NO sample CTE,
    # NO temp table. This is the per-test routing divergence in action.
    assert ingested_decision.compiled_sql == _INGESTED_SOURCE_BODY
    assert "WITH sample" not in ingested_decision.compiled_sql
    assert "_SESSION" not in ingested_decision.compiled_sql
    assert "_sf_sample_" not in ingested_decision.compiled_sql

    # DEC-014 — the aggregate INFO explains WHY the ingested body was not
    # sampled: ``oneshot`` has no temp table to rewrite its relation TO (DEC-002).
    payloads = _ingested_routing_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0] == {
        "model_unique_id": model.unique_id,
        "sample_strategy": "oneshot",
        "ingested_count": 1,
        "sampled_count": 0,
        "bypassed_to_source_count": 1,
        "bypass_reasons": {"strategy-not-materialised": 1},
    }
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# #267 US-004 (DEC-006 / DEC-002) — manifest-ingested COUNT-scalar custom_sql:
# engineered-determinism verdict pins + the load-bearing mixed-candidate
# routing pin.
#
# US-003 restructures a scalar count body (`SELECT count(*) FROM … WHERE …`)
# into the failing-rows form
# ``SELECT sf_agg_value FROM (SELECT (<body>) AS sf_agg_value) AS sf_agg
# WHERE sf_agg_value <> 0`` so the adapter's ``SELECT COUNT(*) AS failures
# FROM (<sql>) AS t`` envelope yields 0 rows (pass) / 1 row (fail) instead of
# the always-1 scalar-collapse bug. Routing is already handled by
# ``_test_requires_source_table`` (a ``from_manifest`` custom_sql bypasses to
# the source under EVERY sample strategy) — so #267 adds NO new engine arm,
# only these behavioural pins (per plans/super/267 DEC-006).
#
# Assertions key on the DISPATCHED SQL (``decision.compiled_sql``), not merely
# the routed decision: a decision snapshot alone would pass even if the
# per-test ``per_test_table_ref`` override regressed and the restructure
# reached the sampled temp table.
# ---------------------------------------------------------------------------

# A bare top-level COUNT-of-rows scalar body carrying dbt's OWN backtick
# quoting on the source relation (the shape ``read_manifest_tests`` produces
# for a compiled singular test whose body is ``SELECT count(*) …``).
_INGESTED_COUNT_SCALAR_BODY = (
    "select count(*)\nfrom `fake_project`.`dataset`.`orders`\nwhere status = 'BAD'"
)

# The exact failing-rows restructure US-003's compiler emits for the body
# above (pinned in ``tests/prune/test_compiler.py``; re-pinned here at the
# engine level so a regression that changed the wrap OR dropped it is caught
# from both surfaces).
_EXPECTED_COUNT_SCALAR_RESTRUCTURE = (
    "SELECT sf_agg_value FROM "
    f"(SELECT ({_INGESTED_COUNT_SCALAR_BODY}) AS sf_agg_value) AS sf_agg "
    "WHERE sf_agg_value <> 0"
)


def test_prune_tests_ingested_count_scalar_tautology_dropped_always_passes(
    tmp_path: Path,
) -> None:
    """Engineered determinism (testing-signal.md): the restructured
    count-scalar body returns zero failing rows (``failures=0``) → the engine
    routes the decision to ``dropped`` / ``always-passes``.

    The fake matches on ``sf_agg_value`` (a fragment of the restructure, NOT
    the generic ``SELECT COUNT(*)`` envelope), so the match itself proves the
    US-003 failing-rows restructure actually reached warehouse dispatch — the
    scalar count did NOT slip through verbatim to the always-1 wrap.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"sf_agg_value <> 0", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _ingested_custom_sql_candidates(_INGESTED_COUNT_SCALAR_BODY)
    config = PruneConfig(scope="full", capture_failure_rows=0)

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 1
    decision = result.decisions[0]
    assert decision.test.type == "custom_sql"
    assert decision.test_anchor == "model"
    assert decision.decision == "dropped"
    assert decision.reason == "always-passes"
    assert decision.failures == 0
    # The dispatched SQL is the failing-rows restructure, run against the
    # SOURCE relation (dbt's backticked qualified name), never a sample temp.
    assert decision.compiled_sql == _EXPECTED_COUNT_SCALAR_RESTRUCTURE
    assert "sf_agg_value <> 0" in decision.compiled_sql
    assert "`fake_project`.`dataset`.`orders`" in decision.compiled_sql
    assert "_SESSION" not in decision.compiled_sql
    assert "_sf_sample_" not in decision.compiled_sql
    fake.assert_all_expectations_met()

    audit_rows = _read_audit_lines(audit_path)
    assert len(audit_rows) == 1
    assert audit_rows[0]["reason"] == "always-passes"


def test_prune_tests_ingested_count_scalar_real_failure_kept(tmp_path: Path) -> None:
    """Engineered determinism: the restructured count-scalar body returns a
    failing row (``failures=1``) on an untrusted model → the engine routes
    the decision to ``kept`` / ``reason="kept"`` (real signal survives).

    The restructured ``… WHERE sf_agg_value <> 0`` wrap yields the adapter
    ``failures`` count as **0 vs 1** (the row-count of the predicate, not the
    underlying scalar COUNT), so this mirrors ``…_tautology_dropped_always_passes``
    with the fake flipped to ``failures > 0``, bracketing both verdict outcomes.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"sf_agg_value <> 0", returns=[{"failures": 1}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = _ingested_custom_sql_candidates(_INGESTED_COUNT_SCALAR_BODY)
    config = PruneConfig(scope="full", capture_failure_rows=0)  # untrusted default

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "custom_sql"
    assert decision.decision == "kept"
    assert decision.reason == "kept"
    assert decision.failures == 1
    # The verdict lands on the SAME restructured, source-routed SQL — proving
    # the failing-rows form is what produced the kept decision.
    assert decision.compiled_sql == _EXPECTED_COUNT_SCALAR_RESTRUCTURE
    assert "`fake_project`.`dataset`.`orders`" in decision.compiled_sql
    assert "_SESSION" not in decision.compiled_sql
    assert "_sf_sample_" not in decision.compiled_sql
    fake.assert_all_expectations_met()


def test_prune_tests_mixed_ingested_count_scalar_and_drafted_materialised(
    tmp_path: Path,
) -> None:
    """LOAD-BEARING mixed-candidate routing pin (prune-engine.md § "#170
    lessons" / business-rule-tests.md § "Materialised-sample substitution —
    Direction 2"): one model, two candidates under ``sample_strategy=
    "materialised"`` —

      * 1× ingested COUNT-scalar (``from_manifest=True``) — bypasses to the
        SOURCE table AND carries the US-003 restructured ``<> 0`` wrap; and
      * 1× drafted built-in ``not_null`` (``from_manifest=False``) — routes to
        the MATERIALISED ``_SESSION._sf_sample_*`` temp table.

    The ``all_bypass_to_source`` short-circuit MUST NOT fire (the not_null does
    not bypass), so the per-test ``per_test_table_ref`` override is exercised
    directly. Assertions key on each DISPATCHED SQL (source vs temp table +
    the restructure shape), NOT the routed decision alone — a decision
    snapshot would pass even if the per-test override regressed and the
    ingested restructure reached the temp table.

    The two per-test COUNT queries carry DISTINCT matchers (``sf_agg_value``
    for the ingested restructure, ``IS NULL`` for the not_null), so the fake
    serves each regardless of candidate iteration order, and the verdicts are
    engineered per-test: the ingested body is failing (kept), the not_null is
    always-passing (dropped).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    # not_null does NOT bypass → the engine materialises the sample once.
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=materialised_ref,
    )
    # Distinct matchers make the pairing order-independent AND assert each
    # dispatched shape reached the warehouse:
    #   * the ingested restructure (``sf_agg_value <> 0``) → 1 failing row (kept)
    #   * the drafted not_null (``IS NULL``) → always-passing (dropped)
    fake.expect_query(matching=r"sf_agg_value <> 0", returns=[{"failures": 1}])
    fake.expect_query(matching=r"IS NULL", returns=[{"failures": 0}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="id",
                description="The order's primary key.",
                tests=(CandidateTestNotNull(column="id"),),
            ),
        ),
        tests=(CandidateTestCustomSQL(sql=_INGESTED_COUNT_SCALAR_BODY, from_manifest=True),),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 2
    # Route by anchor so the assertions do not depend on the engine's per-test
    # iteration order.
    by_anchor = {d.test_anchor: d for d in result.decisions}
    assert {"model", "column.id"} == set(by_anchor)

    ingested = by_anchor["model"]
    not_null = by_anchor["column.id"]

    # The ingested count-scalar bypasses to the SOURCE and carries the
    # restructured failing-rows wrap; it must NEVER touch the temp table.
    assert ingested.test.type == "custom_sql"
    assert ingested.compiled_sql == _EXPECTED_COUNT_SCALAR_RESTRUCTURE
    assert "sf_agg_value <> 0" in ingested.compiled_sql
    assert "`fake_project`.`dataset`.`orders`" in ingested.compiled_sql
    assert "_SESSION" not in ingested.compiled_sql
    assert "_sf_sample_" not in ingested.compiled_sql
    # Verdict lands on the restructured form (failing → kept).
    assert ingested.decision == "kept"
    assert ingested.reason == "kept"
    assert ingested.failures == 1

    # The drafted not_null routes to the MATERIALISED temp table (per-test
    # override did NOT bypass it), and the SOURCE qualified name must NOT
    # appear at table-position — else the override regressed.
    assert not_null.test.type == "not_null"
    assert "_SESSION._sf_sample_" in not_null.compiled_sql
    assert "fake_project.dataset.orders" not in not_null.compiled_sql
    assert not_null.decision == "dropped"
    assert not_null.reason == "always-passes"

    fake.assert_all_expectations_met()


def test_prune_tests_mixed_ingested_count_scalar_and_drafted_oneshot(
    tmp_path: Path,
) -> None:
    """Strategy coverage for the mixed-candidate routing pin under
    ``sample_strategy="oneshot"``: the ingested COUNT-scalar still bypasses to
    the SOURCE with its restructured ``<> 0`` wrap, while the drafted not_null
    samples via the ``WITH sample`` CTE. ``_test_requires_source_table``
    returns True for a ``from_manifest`` custom_sql under BOTH ``materialised``
    and ``oneshot``, so both strategies need independent per-test-override
    coverage (a regression could narrow the bypass to one strategy).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    # oneshot samples the not_null via the CTE → one num_rows lookup for the
    # sample bucket. The ingested body bypasses sampling entirely.
    fake.expect_get_table(
        ref=TableRef(project="fake_project", dataset="dataset", name="orders"),
        returns=FakeTable(num_rows=1_000_000),
    )
    # Distinct matchers, order-independent, engineered verdicts per-test.
    fake.expect_query(matching=r"sf_agg_value <> 0", returns=[{"failures": 1}])
    fake.expect_query(matching=r"IS NULL", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="id",
                description="The order's primary key.",
                tests=(CandidateTestNotNull(column="id"),),
            ),
        ),
        tests=(CandidateTestCustomSQL(sql=_INGESTED_COUNT_SCALAR_BODY, from_manifest=True),),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="oneshot",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 2
    by_anchor = {d.test_anchor: d for d in result.decisions}
    assert {"model", "column.id"} == set(by_anchor)

    ingested = by_anchor["model"]
    not_null = by_anchor["column.id"]

    # Ingested count-scalar: SOURCE-routed, restructured, no sample CTE / temp.
    assert ingested.compiled_sql == _EXPECTED_COUNT_SCALAR_RESTRUCTURE
    assert "`fake_project`.`dataset`.`orders`" in ingested.compiled_sql
    assert "WITH sample" not in ingested.compiled_sql
    assert "_SESSION" not in ingested.compiled_sql
    assert "_sf_sample_" not in ingested.compiled_sql
    assert ingested.decision == "kept"
    assert ingested.reason == "kept"
    assert ingested.failures == 1

    # Drafted not_null: sampled via the CTE (per-test override did NOT bypass).
    assert "WITH sample" in not_null.compiled_sql
    assert not_null.decision == "dropped"
    assert not_null.reason == "always-passes"

    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# #268 US-004 (DEC-008 / DEC-009 / DEC-010) — engine routing for SAMPLED
# manifest-ingested custom_sql.
#
# Since #268 a manifest-ingested body CAN be evaluated against the
# materialised sample — but only via a sqlglot relation-rewrite the engine
# precomputes (``plan_relation_rewrite``), splices (``_build_ingested_rewrite``)
# and PROVES (``verify_relation_rewrite``) before threading it into the
# compiler as ``ingested_sql_override``. Three gates narrow it:
#
#   * DEC-002 — ``materialised`` + ``scope="sample"`` only (``oneshot`` has no
#     temp table to rewrite the relation TO);
#   * DEC-003 — never a #267 count-of-rows scalar (an aggregate over a
#     hash-mod'd sample returns the SAMPLE SIZE, not the real count);
#   * DEC-010 — at least TWO samplable ingested candidates (the ``SELECT *``
#     CTAS reads every column; one narrow test can never pay for it).
#
# Plus DEC-009: when ``materialise_sample`` fails and NOTHING in the batch
# genuinely needs the sample, fall back to full-scope against the source
# rather than blanket-degrading every candidate to kept-without-evidence.
#
# Every assertion keys on the DISPATCHED SQL (``decision.compiled_sql``), not
# the routed decision alone — a decision snapshot passes even when the routing
# regressed and the body silently full-scanned production.
# ---------------------------------------------------------------------------

# A SECOND row-returning ingested body over the same relation, so a batch can
# carry the >= 2 samplable candidates DEC-010 requires. Distinct WHERE clause →
# a distinct fake matcher → order-independent pairing.
_INGESTED_SOURCE_BODY_2 = (
    "select id\nfrom `fake_project`.`dataset`.`orders`\nwhere customer_id is null"
)

# The production ``materialise_sample`` derives the temp table's 16-hex run_id
# itself (from the source ref + version + sample size + partition filter), so the
# dispatched SQL carries THAT name, not the fake's placeholder. Pin the SHAPE —
# ``_SESSION._sf_sample_<16hex>`` — exactly as the #22 / #116 precedents do.
_SAMPLE_TEMP_RE = re.compile(r"`_SESSION\._sf_sample_[0-9a-f]{16}`")


def _two_samplable_ingested_candidates() -> CandidateSchema:
    """Two row-returning manifest-ingested bodies over the model's own relation
    — the minimum DEC-010 admits for sampling."""
    return CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY_2, from_manifest=True),
        ),
    )


def test_prune_tests_two_samplable_ingested_dispatch_against_the_materialised_temp(
    tmp_path: Path,
) -> None:
    """THE #268 behavioural pin (Direction-1 shape, mirroring
    ``test_prune_tests_custom_sql_single_table_references_temp_table_under_materialised``):
    under ``materialised`` + ``scope="sample"``, two samplable ingested bodies
    are relation-rewritten and DISPATCHED against ``_SESSION._sf_sample_<16hex>``
    — the source relation appears in neither.

    A compiler snapshot certifies SQL shape, not routing: the bug this pins
    (a silent full-scan of production booked as an evidence-backed verdict at
    ``scope="sample"``) lives in the ENGINE. Both fake matchers key on each
    body's own WHERE clause, so the pairing is iteration-order independent and
    the match itself proves the rewritten body reached warehouse dispatch.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=materialised_ref,
    )
    fake.expect_query(matching=r"status = 'BAD'", returns=[{"failures": 0}])
    fake.expect_query(matching=r"customer_id is null", returns=[{"failures": 3}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        _two_samplable_ingested_candidates(),
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 2
    for decision in result.decisions:
        # The load-bearing assertions: the dispatched SQL binds the TEMP table
        # and not one byte of the production relation survives.
        assert _SAMPLE_TEMP_RE.search(decision.compiled_sql) is not None
        assert "orders" not in decision.compiled_sql
        assert "`fake_project`.`dataset`" not in decision.compiled_sql
        # dbt's own bytes outside the spliced relation survive verbatim.
        assert decision.compiled_sql.startswith("select id\nfrom ")

    by_sql = {d.test.sql: d for d in result.decisions}  # type: ignore[union-attr]
    assert by_sql[_INGESTED_SOURCE_BODY].decision == "dropped"
    assert by_sql[_INGESTED_SOURCE_BODY].reason == "always-passes"
    assert by_sql[_INGESTED_SOURCE_BODY_2].decision == "kept"
    assert by_sql[_INGESTED_SOURCE_BODY_2].reason == "kept"
    fake.assert_all_expectations_met()


def test_prune_tests_single_samplable_ingested_still_bypasses_no_materialise(
    tmp_path: Path,
) -> None:
    """DEC-010 — ONE samplable ingested candidate is below the CTAS break-even
    (``SELECT *`` reads every column; a narrow ingested test on a wide table is
    column-pruned at source), so it keeps bypassing to the source at full scope
    and ``materialise_sample`` is NOT called.

    The fake registers NO ``expect_materialise_sample`` / ``expect_get_table``:
    any sampling pre-work would raise ``unexpected query``.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        _ingested_custom_sql_candidates(_INGESTED_SOURCE_BODY),
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.compiled_sql == _INGESTED_SOURCE_BODY
    assert "_sf_sample_" not in decision.compiled_sql
    assert decision.decision == "dropped"
    assert decision.reason == "always-passes"
    fake.assert_all_expectations_met()


def test_prune_tests_all_count_scalar_ingested_never_materialises(tmp_path: Path) -> None:
    """DEC-003 — the exact regression #268 could silently introduce: a batch of
    manifest-ingested COUNT-of-rows scalars must NOT trigger a
    ``materialise_sample`` CTAS (billing bytes for a sample the batch cannot
    use), because an aggregate over a hash-mod'd sample returns the SAMPLE SIZE,
    not the model's real count.

    TWO count-scalars — enough to clear DEC-010's threshold if the
    ``is_row_returning`` gate were ever dropped — so this test fails loud if the
    count-scalar gate regresses, not merely because the batch was too small.
    The fake registers NO ``expect_materialise_sample``: a CTAS raises.
    """
    audit_path = tmp_path / "prune.jsonl"
    second_scalar = (
        "select count(*)\nfrom `fake_project`.`dataset`.`orders`\nwhere customer_id is null"
    )
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"status = 'BAD'", returns=[{"failures": 0}])
    fake.expect_query(matching=r"customer_id is null", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestCustomSQL(sql=_INGESTED_COUNT_SCALAR_BODY, from_manifest=True),
            CandidateTestCustomSQL(sql=second_scalar, from_manifest=True),
        ),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 2
    for decision in result.decisions:
        # Source-routed, carrying #267's failing-rows restructure. Never the temp.
        assert "sf_agg_value <> 0" in decision.compiled_sql
        assert "`fake_project`.`dataset`.`orders`" in decision.compiled_sql
        assert "_SESSION" not in decision.compiled_sql
        assert "_sf_sample_" not in decision.compiled_sql
        assert decision.decision == "dropped"
        assert decision.reason == "always-passes"
    # No CTAS was ever queued; had one been issued the fake would have raised.
    fake.assert_all_expectations_met()


def test_prune_tests_mixed_samplable_ingested_count_scalar_and_drafted_routing(
    tmp_path: Path,
) -> None:
    """LOAD-BEARING mixed-candidate routing pin (prune-engine.md § "#170
    lessons" — a single-variant batch only exercises the ``all_bypass_to_source``
    short-circuit; only a MIXED batch exercises the per-test
    ``per_test_table_ref`` override). One model, FOUR candidates under
    ``materialised`` + ``scope="sample"``:

      * 2× samplable manifest-ingested custom_sql → relation-rewritten onto the
        MATERIALISED temp (clears DEC-010's >= 2 threshold);
      * 1× manifest-ingested COUNT-scalar → bypasses to SOURCE with #267's
        restructure (DEC-003);
      * 1× drafted ``not_null`` → the MATERIALISED temp via the #22 substitution.

    Three different destinations in one batch, all driven off the SAME
    precomputed plan the short-circuit consulted.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=materialised_ref,
    )
    # Distinct matchers → order-independent pairing; each match proves that
    # dispatched shape actually reached the warehouse.
    # The samplable body and the count-scalar SHARE the ``status = 'BAD'``
    # predicate, so the samplable matcher anchors on the REWRITTEN relation
    # (the temp table) — the count-scalar keeps dbt's source relation and can
    # never match it. Ambiguous matchers would let the queue order decide the
    # pairing and quietly hide a routing regression.
    fake.expect_query(
        matching=r"_sf_sample_[0-9a-f]{16}`\nwhere status = 'BAD'", returns=[{"failures": 0}]
    )
    fake.expect_query(matching=r"customer_id is null", returns=[{"failures": 2}])
    fake.expect_query(matching=r"sf_agg_value <> 0", returns=[{"failures": 0}])
    fake.expect_query(matching=r"IS NULL", returns=[{"failures": 0}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="id",
                description="The order's primary key.",
                tests=(CandidateTestNotNull(column="id"),),
            ),
        ),
        tests=(
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY_2, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_COUNT_SCALAR_BODY, from_manifest=True),
        ),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 4
    not_null = next(d for d in result.decisions if d.test.type == "not_null")
    by_sql = {
        d.test.sql: d  # type: ignore[union-attr]
        for d in result.decisions
        if d.test.type == "custom_sql"
    }

    # The two samplable ingested bodies bind the TEMP table.
    for body in (_INGESTED_SOURCE_BODY, _INGESTED_SOURCE_BODY_2):
        sampled = by_sql[body]
        assert _SAMPLE_TEMP_RE.search(sampled.compiled_sql) is not None
        assert "orders" not in sampled.compiled_sql

    # The count-scalar bypasses to SOURCE, restructured (DEC-003).
    scalar = by_sql[_INGESTED_COUNT_SCALAR_BODY]
    assert scalar.compiled_sql == _EXPECTED_COUNT_SCALAR_RESTRUCTURE
    assert "`fake_project`.`dataset`.`orders`" in scalar.compiled_sql
    assert "_sf_sample_" not in scalar.compiled_sql

    # The drafted built-in binds the TEMP table via the #22 substitution.
    assert "_SESSION._sf_sample_" in not_null.compiled_sql
    assert "fake_project.dataset.orders" not in not_null.compiled_sql

    fake.assert_all_expectations_met()


def test_prune_tests_materialisation_failure_falls_back_to_source_for_ingested_batch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """DEC-009 (the AR-B5 regression) — ``materialise_sample`` raises
    ``SamplingRequiresPartitionFilterError`` (BigQuery's pre-CTAS refusal on a
    >100M-row unpartitioned model) on an all-ingested batch. Every candidate has
    a perfectly good full-scope form — its own unrewritten ``compiled_code``
    against the source — so the engine falls back and each gets a REAL VERDICT,
    NOT the blanket ``kept-without-evidence``.

    Without this, ``prune-existing --scope=sample`` on exactly the tables
    operators care most about would go from N real verdicts to ZERO pruning.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=SamplingRequiresPartitionFilterError(
            table="fake_project.dataset.orders", num_rows=200_000_000
        ),
    )
    fake.expect_query(matching=r"status = 'BAD'", returns=[{"failures": 0}])
    fake.expect_query(matching=r"customer_id is null", returns=[{"failures": 5}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            _two_samplable_ingested_candidates(),
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    assert result.total_tests == 2
    by_sql = {d.test.sql: d for d in result.decisions}  # type: ignore[union-attr]
    # REAL verdicts against the source — never kept-without-evidence.
    assert by_sql[_INGESTED_SOURCE_BODY].decision == "dropped"
    assert by_sql[_INGESTED_SOURCE_BODY].reason == "always-passes"
    assert by_sql[_INGESTED_SOURCE_BODY_2].decision == "kept"
    assert by_sql[_INGESTED_SOURCE_BODY_2].reason == "kept"
    for decision in result.decisions:
        assert decision.reason != "kept-without-evidence"
        # Dispatched verbatim against the source (no temp table to rewrite to).
        assert "`fake_project`.`dataset`.`orders`" in decision.compiled_sql
        assert "_sf_sample_" not in decision.compiled_sql

    matching = [
        record
        for record in caplog.records
        if record.name == "signalforge.prune.engine"
        and "falling back to full-scope against source" in record.getMessage()
    ]
    assert len(matching) == 1
    raw = matching[0].args[0] if isinstance(matching[0].args, tuple) else matching[0].args
    assert isinstance(raw, str)
    payload = json.loads(raw)
    assert payload["fallback"] == "source"
    # The adapter wraps the sizing refusal in ``MaterialisationFailedError``; the
    # engine branches on ``WarehouseError``, so the class name is incidental —
    # the CAUSE is what the operator needs, and it survives in ``error_message``.
    assert payload["error_class"] == "MaterialisationFailedError"
    assert "PartitionFilter" in payload["error_message"]
    assert payload["candidate_count"] == 2
    fake.assert_all_expectations_met()


def test_prune_tests_materialisation_failure_preserves_pre_materialisation_bypass_reasons(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """DEC-014 + the QG fix — when materialisation fails and the batch falls back
    to source, only the SAMPLABLE plans demote to ``materialisation-failed``. A
    plan already refused at plan time (here a ``multi-relation`` body) KEEPS its
    reason, so the aggregate INFO histogram reports the true cause instead of
    over-writing every entry with ``materialisation-failed``.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=SamplingRequiresPartitionFilterError(
            table="fake_project.dataset.orders", num_rows=200_000_000
        ),
    )
    # After the fallback, every ingested body runs verbatim against the source.
    fake.expect_query(matching=r"status = 'BAD'", returns=[{"failures": 0}])
    fake.expect_query(matching=r"customer_id is null", returns=[{"failures": 0}])
    fake.expect_query(
        matching=r"join `fake_project`\.`dataset`\.`customers`", returns=[{"failures": 0}]
    )
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY_2, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_MULTI_RELATION_BODY, from_manifest=True),
        ),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    with caplog.at_level("INFO", logger="signalforge.prune.engine"):
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    payloads = _ingested_routing_payloads(caplog)
    assert len(payloads) == 1
    # The two samplable bodies demote to ``materialisation-failed``; the
    # multi-relation body KEEPS its plan-time reason (the bug was over-writing it).
    assert payloads[0]["bypass_reasons"] == {"materialisation-failed": 2, "multi-relation": 1}
    assert payloads[0]["sampled_count"] == 0
    assert payloads[0]["bypassed_to_source_count"] == 3
    fake.assert_all_expectations_met()


def test_prune_tests_materialisation_failure_with_drafted_test_still_degrades(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """DEC-009's boundary — the SAME materialisation failure, but a DRAFTED
    row-level test is in the batch. A drafted ``not_null`` genuinely needs the
    sample and has no defined source fallback (running it full-scope would change
    its cost profile without the operator asking), so the blanket
    conservative-bias path stands unchanged: EVERY candidate routes to
    ``kept-without-evidence`` and the original WARNING fires.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=SamplingRequiresPartitionFilterError(
            table="fake_project.dataset.orders", num_rows=200_000_000
        ),
    )
    # NO expect_query: the degraded path dispatches nothing.
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="id",
                description="The order's primary key.",
                tests=(CandidateTestNotNull(column="id"),),
            ),
        ),
        tests=(
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY_2, from_manifest=True),
        ),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    with caplog.at_level("WARNING", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    assert result.total_tests == 3
    for decision in result.decisions:
        assert decision.decision == "kept"
        assert decision.reason == "kept-without-evidence"
        assert decision.why.startswith("sample materialisation failed: ")

    matching = [
        record
        for record in caplog.records
        if record.name == "signalforge.prune.engine"
        and "routing all tests to kept-without-evidence" in record.getMessage()
    ]
    assert len(matching) == 1
    fake.assert_all_expectations_met()


def test_prune_tests_ingested_multi_relation_body_never_samples(tmp_path: Path) -> None:
    """DEC-006 — an ingested body touching a SECOND physical relation is refused
    by ``plan_relation_rewrite`` (``multi-relation``) and keeps bypassing to the
    source, so ``materialise_sample`` is never called even alongside a samplable
    sibling that alone cannot clear DEC-010's >= 2 threshold.

    Sampling one leg of a join can produce a false pass → ``always-passes`` → a
    real test is DROPPED. Enforced on the AST, never a ``\\bjoin\\b`` regex.
    """
    audit_path = tmp_path / "prune.jsonl"
    join_body = (
        "select o.id\nfrom `fake_project`.`dataset`.`orders` as o\n"
        "join `fake_project`.`dataset`.`other_model` as x on x.id = o.id\n"
        "where o.status = 'BAD'"
    )
    fake = FakeBigQueryClient(project="fake_project")
    # NO expect_materialise_sample: both candidates bypass → no CTAS.
    fake.expect_query(matching=r"other_model", returns=[{"failures": 0}])
    fake.expect_query(matching=r"status = 'BAD'", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestCustomSQL(sql=join_body, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY, from_manifest=True),
        ),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 2
    for decision in result.decisions:
        assert "_sf_sample_" not in decision.compiled_sql
        assert "`fake_project`.`dataset`.`orders`" in decision.compiled_sql
        assert decision.reason == "always-passes"
    fake.assert_all_expectations_met()


def test_test_requires_source_table_samplable_kwarg_is_pure_and_defaults_false() -> None:
    """DEC-008 — ``samplable`` is a PURE keyword arg (default ``False``): the
    helper never parses SQL, so its existing 2-positional-arg unit tests stay
    valid and the sqlglot parse happens ONCE per body in
    ``_plan_ingested_samples`` rather than five times at routing.

    An ingested body bypasses to source unless the engine hands it
    ``samplable=True``; a DRAFTED custom_sql never bypasses either way (gate on
    ``from_manifest``, never ``type == "custom_sql"``).
    """
    from signalforge.prune.engine import _test_requires_source_table

    ingested = CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY, from_manifest=True)
    drafted = CandidateTestCustomSQL(sql="SELECT 1 FROM {{ this }} WHERE id IS NULL")

    assert _test_requires_source_table(ingested, "materialised") is True
    assert _test_requires_source_table(ingested, "materialised", samplable=False) is True
    assert _test_requires_source_table(ingested, "materialised", samplable=True) is False
    # scope=full has no temp table to bind: nothing bypasses, samplable or not.
    assert _test_requires_source_table(ingested, None, samplable=True) is False
    # A drafted body is never bypassed — and ``samplable`` cannot make it so.
    assert _test_requires_source_table(drafted, "materialised", samplable=True) is False
    # A metadata-aggregate variant ignores ``samplable`` entirely (DEC-003 kin).
    assert (
        _test_requires_source_table(
            CandidateTestRowCountBetween(minimum=1, maximum=10), "materialised", samplable=True
        )
        is True
    )


# --- #268 DEC-011: bypassed_to_source is set at the decision site ------------


def test_prune_tests_bypassed_to_source_distinguishes_sampled_from_bypassed(
    tmp_path: Path,
) -> None:
    """#268 DEC-011 — THE audit-legibility pin. In ONE batch at
    ``scope="sample"`` + ``materialised``, three destinations produce three
    honest ``bypassed_to_source`` values:

      * 2× samplable manifest-ingested custom_sql → rewritten onto the temp →
        ``False`` (they really did run against the sample);
      * 1× manifest-ingested COUNT-scalar → routed past the sample to the
        SOURCE → ``True``;
      * 1× drafted ``not_null`` → the temp via the #22 substitution → ``False``.

    ``scope`` is copied from ``config.scope``, so EVERY one of these four
    decisions records ``scope="sample"`` — including the one that full-scanned
    production. Without this flag a reviewer cannot tell them apart, which cuts
    against Architectural Commitment #5. A single-variant batch would only
    exercise the ``all_bypass_to_source`` short-circuit; the mixed batch is what
    pins the per-test ``per_test_table_ref`` arm (the #170 two-conditional rule).
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(source_ref, sample_size=100_000, returns=materialised_ref)
    fake.expect_query(
        matching=r"_sf_sample_[0-9a-f]{16}`\nwhere status = 'BAD'", returns=[{"failures": 0}]
    )
    fake.expect_query(matching=r"customer_id is null", returns=[{"failures": 2}])
    fake.expect_query(matching=r"sf_agg_value <> 0", returns=[{"failures": 0}])
    fake.expect_query(matching=r"IS NULL", returns=[{"failures": 0}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="id",
                description="The order's primary key.",
                tests=(CandidateTestNotNull(column="id"),),
            ),
        ),
        tests=(
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY_2, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_COUNT_SCALAR_BODY, from_manifest=True),
        ),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    assert result.total_tests == 4
    by_sql = {
        d.test.sql: d  # type: ignore[union-attr]
        for d in result.decisions
        if d.test.type == "custom_sql"
    }
    not_null = next(d for d in result.decisions if d.test.type == "not_null")

    # Sampled → False. Cross-checked against the SQL that actually dispatched,
    # so the flag cannot drift away from the routing it claims to describe.
    for body in (_INGESTED_SOURCE_BODY, _INGESTED_SOURCE_BODY_2):
        sampled = by_sql[body]
        assert sampled.bypassed_to_source is False
        assert _SAMPLE_TEMP_RE.search(sampled.compiled_sql) is not None
    assert not_null.bypassed_to_source is False
    assert "_SESSION._sf_sample_" in not_null.compiled_sql

    # Bypassed → True, and the SQL confirms it hit the production relation.
    scalar = by_sql[_INGESTED_COUNT_SCALAR_BODY]
    assert scalar.bypassed_to_source is True
    assert "`fake_project`.`dataset`.`orders`" in scalar.compiled_sql
    assert "_sf_sample_" not in scalar.compiled_sql

    # The lie the flag exists to expose: scope reads "sample" on all four.
    assert {d.scope for d in result.decisions} == {"sample"}

    # And it reaches the audit-of-record, not just the in-memory result.
    audit_rows = _read_audit_lines(audit_path)
    assert len(audit_rows) == 4
    assert sum(1 for row in audit_rows if row["bypassed_to_source"]) == 1
    assert all(row["scope"] == "sample" for row in audit_rows)
    assert all(row["audit_schema_version"] == 4 for row in audit_rows)

    fake.assert_all_expectations_met()


def test_prune_tests_row_count_between_records_bypassed_to_source(tmp_path: Path) -> None:
    """#268 DEC-011 also fixes the LATENT LIE the metadata-aggregate variants
    have carried since #169: ``row_count_between`` always routes past the sample
    to the source (a ``COUNT(*)`` over a hash-mod'd sample returns the sample
    size, not the model's real row count) — yet it was recorded as
    ``scope="sample"`` with nothing to say otherwise.

    Under ``scope="sample"`` the decision now honestly reports
    ``bypassed_to_source=True``.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    result = prune_tests(
        _make_orders_model(),
        adapter,
        _candidates_with_one_row_count_test(minimum=100, maximum=10_000),
        _make_manifest(_make_orders_model()),
        config=PruneConfig(
            scope="sample",
            sample_size=100_000,
            capture_failure_rows=0,
            sample_strategy="materialised",
        ),
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.test.type == "row_count_between"
    assert decision.bypassed_to_source is True
    assert decision.scope == "sample"  # the field that was, alone, misleading
    assert "fake_project.dataset.orders" in decision.compiled_sql
    assert _read_audit_lines(audit_path)[0]["bypassed_to_source"] is True
    fake.assert_all_expectations_met()


def test_prune_tests_full_scope_never_reports_a_bypass(tmp_path: Path) -> None:
    """Under ``scope="full"`` there is no sample to bypass, so
    ``bypassed_to_source`` is ``False`` even for a metadata-aggregate variant
    that would bypass under a sample scope.

    Guards against the lazy implementation that keys the flag on the test's
    VARIANT rather than on the routing the engine actually took.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    result = prune_tests(
        _make_orders_model(),
        adapter,
        _candidates_with_one_row_count_test(minimum=100, maximum=10_000),
        _make_manifest(_make_orders_model()),
        config=PruneConfig(scope="full", capture_failure_rows=0),
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    decision = result.decisions[0]
    assert decision.scope == "full"
    assert decision.bypassed_to_source is False
    assert _read_audit_lines(audit_path)[0]["bypassed_to_source"] is False
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# #268 US-006 (DEC-014) — ingested-routing observability.
#
# The #154 INFO ("scope=sample requested; evaluating full-scope against source")
# became a LIE for the samplable subset once #268 let an ingested body bind the
# materialised temp table. It is replaced by ONE aggregate INFO per
# ``prune_tests`` call carrying a ``{reason: count}`` bypass histogram, plus one
# DEBUG breadcrumb per bypassed ingested candidate.
#
# Assertions decode the ``json.dumps`` payload (the DEC-017 lazy-format logger
# gate forbids f-strings anywhere in a ``_LOGGER`` call's argument subtree) —
# they pin the SIGNAL, never the prose.
# ---------------------------------------------------------------------------

# A body joining a SECOND physical relation. ``plan_relation_rewrite`` refuses it
# on the AST (``"multi-relation"``, DEC-006): rewriting only the model's own
# relation would silently join a SAMPLE against a FULL sibling.
_INGESTED_MULTI_RELATION_BODY = (
    "select o.id\n"
    "from `fake_project`.`dataset`.`orders` as o\n"
    "join `fake_project`.`dataset`.`customers` as c on o.customer_id = c.id\n"
    "where c.id is null"
)


def test_prune_tests_ingested_routing_info_reports_sampled_bypassed_and_reasons(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """DEC-014 — the aggregate INFO on a REAL mixed batch: two samplable ingested
    bodies (sampled onto the temp), one COUNT-scalar (``aggregate-scalar``), one
    multi-relation body (``multi-relation``), plus a drafted ``not_null`` (not
    ingested — it must not be counted at all).

    Fires EXACTLY ONCE and reports ``sampled_count=2``,
    ``bypassed_to_source_count=2`` and the ``{reason: count}`` histogram, so an
    operator gets the SHAPE of the bypass without dropping to DEBUG. A per-batch
    aggregate is the whole point: a single-candidate batch would pass a naive
    implementation that emitted one INFO per test.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(source_ref, sample_size=100_000, returns=materialised_ref)
    # Distinct matchers → order-independent pairing. The samplable body and the
    # count-scalar share the ``status = 'BAD'`` predicate, so the samplable one
    # anchors on the REWRITTEN relation (the count-scalar keeps dbt's source).
    fake.expect_query(
        matching=r"_sf_sample_[0-9a-f]{16}`\nwhere status = 'BAD'", returns=[{"failures": 0}]
    )
    fake.expect_query(matching=r"where customer_id is null", returns=[{"failures": 1}])
    fake.expect_query(matching=r"sf_agg_value <> 0", returns=[{"failures": 0}])
    fake.expect_query(
        matching=r"join `fake_project`\.`dataset`\.`customers`", returns=[{"failures": 0}]
    )
    fake.expect_query(matching=r"IS NULL", returns=[{"failures": 0}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="id",
                description="The order's primary key.",
                tests=(CandidateTestNotNull(column="id"),),
            ),
        ),
        tests=(
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY_2, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_COUNT_SCALAR_BODY, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_MULTI_RELATION_BODY, from_manifest=True),
        ),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    with caplog.at_level("INFO", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    assert result.total_tests == 5

    payloads = _ingested_routing_payloads(caplog)
    assert len(payloads) == 1, (
        f"expected exactly ONE aggregate INFO per prune_tests call; got {len(payloads)}"
    )
    assert payloads[0] == {
        "model_unique_id": model.unique_id,
        "sample_strategy": "materialised",
        # The drafted not_null is NOT an ingested candidate — 4, not 5.
        "ingested_count": 4,
        "sampled_count": 2,
        "bypassed_to_source_count": 2,
        "bypass_reasons": {"aggregate-scalar": 1, "multi-relation": 1},
    }
    fake.assert_all_expectations_met()


def test_prune_tests_ingested_bypass_debug_breadcrumb_per_non_sampled_candidate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """DEC-014 — one DEBUG breadcrumb per BYPASSED ingested candidate, carrying
    its ``test_anchor`` + machine-readable ``reason``. The two SAMPLED bodies get
    no breadcrumb (there is nothing to explain), and the drafted ``not_null``
    gets none either.

    DEBUG, not INFO: a wide model with 40 ingested tests would otherwise emit 40
    INFO lines — the aggregate already carries the counts.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(source_ref, sample_size=100_000, returns=materialised_ref)
    fake.expect_query(
        matching=r"_sf_sample_[0-9a-f]{16}`\nwhere status = 'BAD'", returns=[{"failures": 0}]
    )
    fake.expect_query(matching=r"where customer_id is null", returns=[{"failures": 0}])
    fake.expect_query(matching=r"sf_agg_value <> 0", returns=[{"failures": 0}])
    fake.expect_query(
        matching=r"join `fake_project`\.`dataset`\.`customers`", returns=[{"failures": 0}]
    )
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY_2, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_COUNT_SCALAR_BODY, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_MULTI_RELATION_BODY, from_manifest=True),
        ),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    with caplog.at_level("DEBUG", logger="signalforge.prune.engine"):
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    breadcrumbs = _ingested_bypass_breadcrumbs(caplog)
    # Two bypassed candidates → two breadcrumbs. The two sampled ones get none.
    assert len(breadcrumbs) == 2
    reasons = {str(crumb["reason"]) for crumb in breadcrumbs}
    assert reasons == {"aggregate-scalar", "multi-relation"}
    for crumb in breadcrumbs:
        assert crumb["model_unique_id"] == model.unique_id
        assert crumb["test_anchor"] == "model"
    fake.assert_all_expectations_met()


def test_prune_tests_ingested_routing_signals_silent_under_full_scope(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``scope="full"`` run has nothing to explain — every candidate runs
    full-scope by design — so neither the aggregate INFO nor any breadcrumb
    fires. Preserves the pre-#268 log-silence on the full path."""
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)

    with caplog.at_level("DEBUG", logger="signalforge.prune.engine"):
        prune_tests(
            model,
            adapter,
            _ingested_custom_sql_candidates(_INGESTED_SOURCE_BODY),
            manifest,
            config=PruneConfig(scope="full", capture_failure_rows=0),
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    assert _ingested_routing_payloads(caplog) == []
    assert _ingested_bypass_breadcrumbs(caplog) == []
    fake.assert_all_expectations_met()


def test_prune_tests_ingested_routing_signals_silent_without_ingested_candidates(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``scope="sample"`` batch carrying NO manifest-ingested candidate stays
    log-silent too — the aggregate reports on ingested routing, and there is
    none. Guards against an implementation that fires an empty INFO on every
    sample-mode run."""
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(source_ref, sample_size=100_000, returns=materialised_ref)
    fake.expect_query(matching=r"SELECT COUNT\(\*\)", returns=[{"failures": 0}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(
            CandidateColumn(
                name="id",
                description="The order's primary key.",
                tests=(CandidateTestNotNull(column="id"),),
            ),
        ),
        tests=(),
    )

    with caplog.at_level("DEBUG", logger="signalforge.prune.engine"):
        prune_tests(
            model,
            adapter,
            candidates,
            manifest,
            config=PruneConfig(
                scope="sample",
                sample_size=100_000,
                capture_failure_rows=0,
                sample_strategy="materialised",
            ),
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    assert _ingested_routing_payloads(caplog) == []
    assert _ingested_bypass_breadcrumbs(caplog) == []
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# #268 US-007 (DEC-004 / DEC-006 / DEC-016) — the remaining behavioural routing
# pins.
#
# ``business-rule-tests.md``: "Pin the ENGINE-routing test, not just the compiler
# snapshot." A snapshot certifies SQL shape; in #170 and #171 the bug lived in
# the engine while the compiler snapshot stayed green. Every assertion below
# keys on the DISPATCHED ``decision.compiled_sql`` and on the audit's
# ``bypassed_to_source`` — the two surfaces that, together, say what the
# warehouse actually ran.
#
# These are NECESSARY, NOT SUFFICIENT: a fake accepts SQL a live warehouse may
# refuse (#226 found three such bugs). US-008's gated BigQuery live cert is the
# merge gate (DEC-016).
# ---------------------------------------------------------------------------

# The relation appears TWICE. A rewrite that landed on only ONE occurrence would
# join a SAMPLE against PRODUCTION and still parse cleanly — the sharpest hazard
# in the epic, and one no syntax check can catch.
_INGESTED_SELF_JOIN_BODY = (
    "select a.id\n"
    "from `fake_project`.`dataset`.`orders` as a\n"
    "join `fake_project`.`dataset`.`orders` as b\n"
    "  on a.customer_id = b.customer_id and a.id <> b.id\n"
    "where a.status = 'BAD'"
)


def test_prune_tests_ingested_self_join_rewrites_both_occurrences_to_one_temp(
    tmp_path: Path,
) -> None:
    """DEC-004 / DEC-006 — a self-join ingested body: BOTH occurrences of the
    model relation are rewritten, to the SAME temp table, and the production
    relation appears ZERO times in the dispatched SQL.

    A self-join is a SINGLE distinct physical relation, so DEC-006's
    single-relation rule admits it — but it yields TWO spans. A partial rewrite
    (one span spliced, one missed) produces SQL that parses, executes, and
    silently joins the sample against production: a plausible false pass →
    ``always-passes`` → a real test is dropped. Only the DEC-004 AST
    post-condition (``verify_relation_rewrite``: zero residual source tables) can
    refuse it, and only an END-TO-END assertion on the dispatched bytes proves
    the engine actually applied it.

    The two temp references must be the same table: two different samples joined
    together would be meaningless.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(source_ref, sample_size=100_000, returns=materialised_ref)
    fake.expect_query(matching=r"a\.id <> b\.id", returns=[{"failures": 0}])
    fake.expect_query(matching=r"customer_id is null", returns=[{"failures": 4}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            # Two samplable bodies — the DEC-010 >= 2 threshold.
            CandidateTestCustomSQL(sql=_INGESTED_SELF_JOIN_BODY, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY_2, from_manifest=True),
        ),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    by_sql = {d.test.sql: d for d in result.decisions}  # type: ignore[union-attr]
    self_join = by_sql[_INGESTED_SELF_JOIN_BODY]

    temps = _SAMPLE_TEMP_RE.findall(self_join.compiled_sql)
    assert len(temps) == 2  # BOTH spans spliced — a partial rewrite fails here.
    assert len(set(temps)) == 1  # ...and onto the SAME sample, not two.
    # Not one byte of the production relation survives.
    assert "orders" not in self_join.compiled_sql
    assert "`fake_project`.`dataset`" not in self_join.compiled_sql
    # dbt's aliases and predicate survive the splice verbatim.
    assert " as a\njoin " in self_join.compiled_sql
    assert "on a.customer_id = b.customer_id and a.id <> b.id" in self_join.compiled_sql

    assert self_join.bypassed_to_source is False
    assert self_join.decision == "dropped"
    assert self_join.reason == "always-passes"
    assert by_sql[_INGESTED_SOURCE_BODY_2].decision == "kept"
    fake.assert_all_expectations_met()


def test_prune_tests_ingested_verify_failure_never_dispatches_unproven_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DEC-004 — when the AST post-condition REFUSES a rewrite, the engine must
    never dispatch it. The candidate is demoted to a source bypass and still gets
    a REAL verdict (its own unrewritten body, full-scope against the source) —
    it is not degraded to ``kept-without-evidence``, and it is certainly not sent
    to the warehouse unproven.

    ``verify_relation_rewrite`` is stubbed to ``False`` — the only way to reach
    this arm without shipping a deliberately broken splice. That is the point:
    the branch exists precisely so that a FUTURE bug in ``plan_relation_rewrite``
    or ``_build_ingested_rewrite`` (one that produced a mis-aimed or partial
    rewrite) fails CLOSED. This test proves the fail-closed wiring is live, not
    that today's splice is broken.

    ``materialise_sample`` IS called (the batch cleared DEC-010 before the proof
    ran), so this genuinely exercises the post-materialisation refusal — not the
    ``all_bypass_to_source`` short-circuit.
    """
    audit_path = tmp_path / "prune.jsonl"
    monkeypatch.setattr(engine_module, "verify_relation_rewrite", lambda *_a, **_k: False)

    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(source_ref, sample_size=100_000, returns=materialised_ref)
    fake.expect_query(matching=r"status = 'BAD'", returns=[{"failures": 0}])
    fake.expect_query(matching=r"customer_id is null", returns=[{"failures": 7}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    with caplog.at_level("DEBUG", logger="signalforge.prune.engine"):
        result = prune_tests(
            model,
            adapter,
            _two_samplable_ingested_candidates(),
            manifest,
            config=config,
            audit_path=audit_path,
            project_dir=tmp_path,
        )

    assert result.total_tests == 2
    by_sql = {d.test.sql: d for d in result.decisions}  # type: ignore[union-attr]
    for body in (_INGESTED_SOURCE_BODY, _INGESTED_SOURCE_BODY_2):
        decision = by_sql[body]
        # Dispatched VERBATIM against the source — never the temp table.
        assert decision.compiled_sql == body
        assert "_sf_sample_" not in decision.compiled_sql
        assert "`fake_project`.`dataset`.`orders`" in decision.compiled_sql
        # A real verdict, and the audit says honestly that it bypassed.
        assert decision.reason != "kept-without-evidence"
        assert decision.bypassed_to_source is True
    assert by_sql[_INGESTED_SOURCE_BODY].reason == "always-passes"
    assert by_sql[_INGESTED_SOURCE_BODY_2].reason == "kept"

    # The observability agrees: nothing was sampled, both bypassed, and the
    # reason names the post-condition refusal rather than a plan-time reject.
    payloads = _ingested_routing_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0]["sampled_count"] == 0
    assert payloads[0]["bypassed_to_source_count"] == 2
    assert payloads[0]["bypass_reasons"] == {"verify-failed": 2}
    assert [crumb["reason"] for crumb in _ingested_bypass_breadcrumbs(caplog)] == [
        "verify-failed",
        "verify-failed",
    ]

    audit_rows = _read_audit_lines(audit_path)
    assert all(row["bypassed_to_source"] is True for row in audit_rows)
    fake.assert_all_expectations_met()


def test_prune_tests_bypassed_to_source_is_true_for_a_refused_ingested_body(
    tmp_path: Path,
) -> None:
    """DEC-011's lie-detector, on the routing class the DEC-011 pin does not
    reach: an ingested body REFUSED at plan time (``multi-relation``, DEC-006).

    The existing ``bypassed_to_source`` pin covers sampled-ingested / count-scalar
    / drafted-row-level / ``row_count_between`` / full-scope. A body refused by
    ``plan_relation_rewrite`` is a distinct routing class, and it is the one an
    implementation keyed on the test VARIANT (rather than on the routing the
    engine actually took) would get wrong — a ``custom_sql`` that IS sampled and a
    ``custom_sql`` that is NOT are the same variant.

    The flag is cross-checked against the SQL that actually dispatched, so it
    cannot itself lie.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    materialised_ref = _make_materialised_ref()
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(source_ref, sample_size=100_000, returns=materialised_ref)
    # Two samplable siblings clear DEC-010, so the batch really does materialise
    # a sample — the refused body bypasses it on its own merits, not because the
    # whole batch short-circuited.
    fake.expect_query(
        matching=r"_sf_sample_[0-9a-f]{16}`\nwhere status = 'BAD'", returns=[{"failures": 0}]
    )
    fake.expect_query(matching=r"customer_id is null", returns=[{"failures": 0}])
    fake.expect_query(matching=r"customers", returns=[{"failures": 0}])
    fake.expect_abort_session(f"sess_{materialised_ref.name}")
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    candidates = CandidateSchema(
        name="orders",
        description="Order events.",
        columns=(),
        tests=(
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_SOURCE_BODY_2, from_manifest=True),
            CandidateTestCustomSQL(sql=_INGESTED_MULTI_RELATION_BODY, from_manifest=True),
        ),
    )
    config = PruneConfig(
        scope="sample",
        sample_size=100_000,
        capture_failure_rows=0,
        sample_strategy="materialised",
    )

    result = prune_tests(
        model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    by_sql = {d.test.sql: d for d in result.decisions}  # type: ignore[union-attr]
    refused = by_sql[_INGESTED_MULTI_RELATION_BODY]
    assert refused.bypassed_to_source is True
    assert "_sf_sample_" not in refused.compiled_sql
    assert "`fake_project`.`dataset`.`orders`" in refused.compiled_sql

    # ...while its two samplable siblings, SAME variant, report the opposite —
    # so the flag is tracking the routing, not the type.
    for body in (_INGESTED_SOURCE_BODY, _INGESTED_SOURCE_BODY_2):
        sibling = by_sql[body]
        assert sibling.bypassed_to_source is False
        assert _SAMPLE_TEMP_RE.search(sibling.compiled_sql) is not None

    audit_rows = _read_audit_lines(audit_path)
    assert sum(1 for row in audit_rows if row["bypassed_to_source"]) == 1
    fake.assert_all_expectations_met()


def test_prune_tests_materialisation_fallback_records_bypassed_to_source(
    tmp_path: Path,
) -> None:
    """DEC-009 + DEC-011 — the LAST routing class: the materialisation-failure
    fallback. Every candidate was planned as samplable, then the CTAS refused, so
    each ran full-scope against the source instead.

    ``scope`` still reads ``"sample"`` (it is copied from ``config.scope``), so
    ``bypassed_to_source`` is the only field that tells a reviewer these verdicts
    came from a FULL SCAN of production and not from the sample they asked for.
    An implementation that computed the flag once, before the fallback re-routed
    everything, would report ``False`` here — and the audit would claim a sample
    that was never taken.
    """
    audit_path = tmp_path / "prune.jsonl"
    fake = FakeBigQueryClient(project="fake_project")
    source_ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    fake.expect_get_table(ref=source_ref, returns=FakeTable(num_rows=1_000_000))
    fake.expect_materialise_sample(
        source_ref,
        sample_size=100_000,
        returns=SamplingRequiresPartitionFilterError(
            table="fake_project.dataset.orders", num_rows=200_000_000
        ),
    )
    fake.expect_query(matching=r"status = 'BAD'", returns=[{"failures": 0}])
    fake.expect_query(matching=r"customer_id is null", returns=[{"failures": 1}])
    adapter = _make_adapter(fake)

    model = _make_orders_model()
    manifest = _make_manifest(model)
    result = prune_tests(
        model,
        adapter,
        _two_samplable_ingested_candidates(),
        manifest,
        config=PruneConfig(
            scope="sample",
            sample_size=100_000,
            capture_failure_rows=0,
            sample_strategy="materialised",
        ),
        audit_path=audit_path,
        project_dir=tmp_path,
    )

    for decision in result.decisions:
        assert decision.scope == "sample"  # the field that, alone, misleads
        assert decision.bypassed_to_source is True
        assert "`fake_project`.`dataset`.`orders`" in decision.compiled_sql
        assert "_sf_sample_" not in decision.compiled_sql

    audit_rows = _read_audit_lines(audit_path)
    assert len(audit_rows) == 2
    assert all(row["bypassed_to_source"] is True for row in audit_rows)
    fake.assert_all_expectations_met()
