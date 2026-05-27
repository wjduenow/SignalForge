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
    assert row["audit_schema_version"] == 2

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
