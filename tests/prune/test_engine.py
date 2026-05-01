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
import sys
from pathlib import Path
from typing import Any

import pytest

from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTestNotNull,
    CandidateTestRelationships,
)
from signalforge.manifest.models import Column, Manifest, Model
from signalforge.prune import engine as engine_module
from signalforge.prune.config import PruneConfig
from signalforge.prune.engine import prune_tests
from signalforge.prune.errors import (
    PruneAuditRecordTooLargeError,
    PruneAuditWriteError,
    PruneTrustedModelNotFoundError,
)
from signalforge.warehouse.adapters.bigquery import BigQueryAdapter
from signalforge.warehouse.errors import TableNotFoundError
from tests.warehouse._fake import FakeBigQueryClient

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
    config = PruneConfig(capture_failure_rows=0)

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
    config = PruneConfig(capture_failure_rows=0)  # untrusted by default

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
    config = PruneConfig(capture_failure_rows=0)

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
    config = PruneConfig(capture_failure_rows=0)

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
    config = PruneConfig(total_budget_seconds=1, capture_failure_rows=0)

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
    config = PruneConfig(trusted_models=("model.shop.nonexistent",))

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
    config = PruneConfig(capture_failure_rows=0)

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
    config = PruneConfig(capture_failure_rows=0)

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
    config = PruneConfig(capture_failure_rows=0)

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
    config = PruneConfig(capture_failure_rows=0)

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
    config = PruneConfig(capture_failure_rows=0)

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
