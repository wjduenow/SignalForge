"""End-to-end prune integration test against ``bigquery-public-data``.

US-012 / ticket #6 acceptance criterion #7. Confirms the orchestrator
produces correct decisions when wired up to a real warehouse against the
public ``bigquery-public-data.iowa_liquor_sales.sales`` table:

  * ``not_null(invoice_and_item_number)`` — non-null in the source
    table, so this is the canonical "always-passes" case → dropped.
  * ``accepted_values(category_name, values=("VODKA", "GIN"))`` —
    the column has many more values than these two, so this is the
    canonical "real failure" case → kept.

Belt-and-suspenders gating (mirrors
``tests/warehouse/test_bigquery_integration.py`` DEC-011):

* ``@pytest.mark.bigquery`` — filtered out by the default
  ``addopts = "-m 'not bigquery and not anthropic'"`` so a bare
  ``pytest`` never collects this test, and
* ``@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), ...)`` — even
  ``pytest -m bigquery`` skips at runtime unless the maintainer has
  opted in by exporting ``SF_RUN_BQ=1`` and configured Application
  Default Credentials.

To run locally::

    gcloud auth application-default login
    SF_RUN_BQ=1 pytest -m bigquery tests/prune/test_integration_bigquery.py

The audit JSONL is written to ``tmp_path/.signalforge/prune.jsonl`` so
the integration test does not pollute the developer's working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
)
from signalforge.manifest.models import Column, Manifest, Model
from signalforge.prune import prune_tests
from signalforge.prune.config import PruneConfig
from signalforge.warehouse.adapters.bigquery import BigQueryAdapter

_SF_RUN_BQ_REASON = "requires SF_RUN_BQ=1 and ADC"


@pytest.mark.bigquery
@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), reason=_SF_RUN_BQ_REASON)
def test_prune_iowa_liquor_sales(tmp_path: Path) -> None:
    """End-to-end: two candidate tests against the public sales table.

    Test 1: ``not_null(invoice_and_item_number)`` — non-null in the source
    table, so this is the canonical "always-passes" case → dropped.

    Test 2: ``accepted_values(category_name, values=("VODKA", "GIN"))`` —
    the column has many more values than these two, so this is the
    canonical "real failure" case → kept.
    """
    # 1. Build an in-memory Manifest representing the public table.
    target_model = Model(
        unique_id="model.iowa.sales",
        name="sales",
        resource_type="model",
        package_name="iowa",
        original_file_path="models/sales.sql",
        path="sales.sql",
        database="bigquery-public-data",
        schema="iowa_liquor_sales",  # type: ignore[call-arg]
        alias="sales",
        raw_code="-- public dataset; raw_code unused by prune",
        description="Iowa liquor sales (public BQ dataset)",
        columns={
            "invoice_and_item_number": Column(
                name="invoice_and_item_number",
                data_type="STRING",
                description="non-null transaction id",
            ),
            "category_name": Column(
                name="category_name",
                data_type="STRING",
                description="liquor category — many values, not just two",
            ),
        },
    )
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12", "adapter_type": "bigquery"},
        nodes={target_model.unique_id: target_model},
    )

    # 2. Build CandidateSchema with two tests.
    candidates = CandidateSchema(
        name="sales",
        description="public iowa liquor sales",
        columns=(
            CandidateColumn(
                name="invoice_and_item_number",
                description="should never be null",
                tests=(CandidateTestNotNull(column="invoice_and_item_number"),),
            ),
            CandidateColumn(
                name="category_name",
                description="closed enum check",
                tests=(
                    CandidateTestAcceptedValues(
                        column="category_name",
                        # Intentionally narrow — the source table has
                        # dozens of category_name values, so this test
                        # is guaranteed to fail.
                        values=("VODKA", "GIN"),
                    ),
                ),
            ),
        ),
    )

    # 3. Build a real BigQueryAdapter using ambient ADC (mirrors
    # tests/warehouse/test_bigquery_integration.py — no explicit project,
    # the BigQuery client picks the billing project from ADC).
    adapter = BigQueryAdapter()
    config = PruneConfig(
        scope="sample",
        sample_size=10_000,
        test_timeout_seconds=60,
        total_budget_seconds=300,
        capture_failure_rows=3,
    )

    audit_path = tmp_path / ".signalforge" / "prune.jsonl"

    # 4. Run prune_tests end-to-end.
    result = prune_tests(
        target_model,
        adapter,
        candidates,
        manifest,
        config=config,
        audit_path=audit_path,
    )

    # 5. Assertions.
    assert len(result.decisions) == 2
    not_null_decision = next(
        d for d in result.decisions if d.test_anchor == "column.invoice_and_item_number"
    )
    accepted_decision = next(d for d in result.decisions if d.test_anchor == "column.category_name")

    # not_null should pass on the public dataset (column is non-null) →
    # dropped, always-passes.
    assert not_null_decision.decision == "dropped", (
        f"expected always-passes drop; got {not_null_decision.decision!r} "
        f"reason={not_null_decision.reason!r} failures={not_null_decision.failures}"
    )
    assert not_null_decision.reason == "always-passes"
    assert not_null_decision.failures == 0

    # accepted_values should fail (the table has way more category_names
    # than just VODKA / GIN) → kept, real failure.
    assert accepted_decision.decision == "kept", (
        f"expected kept; got {accepted_decision.decision!r} "
        f"reason={accepted_decision.reason!r} failures={accepted_decision.failures}"
    )
    assert accepted_decision.reason == "kept"
    assert accepted_decision.failures > 0

    # Audit JSONL should exist and have exactly two lines (one per
    # decision).
    assert audit_path.exists()
    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 2
