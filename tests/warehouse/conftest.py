"""Shared fixtures for tests/warehouse/.

The FakeBigQueryClient (DEC-002) is the canonical injection target for
BigQueryAdapter unit tests; production tests must NEVER call out to real BQ.
Live tests live in test_bigquery_integration.py and are gated by SF_RUN_BQ.
"""

from __future__ import annotations

import pytest

from signalforge.warehouse import BigQueryAdapter, TableRef
from tests.warehouse._fake import FakeBigQueryClient, FakeTable


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
def table_ref() -> TableRef:
    return TableRef(project="fake_project", dataset="analytics", name="dim_users")


@pytest.fixture
def shakespeare_table() -> FakeTable:
    """Stand-in for the real BigQuery shakespeare table — used for sample/stats tests."""
    return FakeTable(
        num_rows=164_656,
        schema=[
            ("word", "STRING"),
            ("word_count", "INT64"),
            ("corpus", "STRING"),
            ("corpus_date", "INT64"),
        ],
    )
