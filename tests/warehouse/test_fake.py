"""Self-tests for the hand-rolled FakeBigQueryClient (US-007).

These tests exist so regressions in the fake itself don't masquerade as
adapter bugs further up the stack. Every test is capable of failing
(``testing-signal.md`` — no ``assert True``-shaped placeholders).
"""

from __future__ import annotations

import pytest
from google.api_core.exceptions import BadRequest

from tests.warehouse._fake import FakeBigQueryClient


def test_expect_query_match_returns_rows() -> None:
    fake = FakeBigQueryClient()
    fake.expect_query(matching=r"SELECT 1", returns=[{"x": 1}])

    job = fake.query("SELECT 1")
    rows = list(job.result())

    assert len(rows) == 1
    assert rows[0]["x"] == 1


def test_unexpected_query_raises_assertion_error() -> None:
    fake = FakeBigQueryClient()
    with pytest.raises(AssertionError, match="unexpected query"):
        fake.query("SELECT 1")


def test_expectation_can_return_exception() -> None:
    fake = FakeBigQueryClient()
    fake.expect_query(matching=r"DROP", returns=BadRequest("blocked"))

    with pytest.raises(BadRequest):
        fake.query("DROP TABLE foo")


def test_assert_all_expectations_met_passes_when_consumed() -> None:
    fake = FakeBigQueryClient()
    fake.expect_query(matching=r"SELECT 1", returns=[{"x": 1}])
    fake.query("SELECT 1")

    # Should not raise.
    fake.assert_all_expectations_met()


def test_assert_all_expectations_met_fails_when_unconsumed() -> None:
    fake = FakeBigQueryClient()
    fake.expect_query(matching=r"SELECT 1", returns=[{"x": 1}])

    with pytest.raises(AssertionError) as excinfo:
        fake.assert_all_expectations_met()

    assert str(excinfo.value)
    assert "query expectations" in str(excinfo.value)


def test_regex_matching_supports_partial_match() -> None:
    fake = FakeBigQueryClient()
    fake.expect_query(matching=r"FARM_FINGERPRINT", returns=[{"h": 42}])

    job = fake.query("SELECT FARM_FINGERPRINT(CAST(id AS STRING)) AS h FROM t")
    rows = list(job.result())

    assert len(rows) == 1
    assert rows[0]["h"] == 42
