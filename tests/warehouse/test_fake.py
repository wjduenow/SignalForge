"""Self-tests for the hand-rolled FakeBigQueryClient (US-007).

These tests exist so regressions in the fake itself don't masquerade as
adapter bugs further up the stack. Every test is capable of failing
(``testing-signal.md`` — no ``assert True``-shaped placeholders).

US-004 (issue #22) extends the surface with two helpers that mirror
``expect_query`` for the new materialise + abort code paths:

* :meth:`FakeBigQueryClient.expect_materialise_sample` — pin one
  ``CREATE TEMP TABLE _sf_sample_<run_id> ...`` round-trip and return a
  job carrying ``session_info.session_id`` so production captures it.
* :meth:`FakeBigQueryClient.expect_abort_session` — pin one
  ``CALL BQ.ABORT_SESSION();`` round-trip keyed by the session id
  carried in ``job_config.connection_properties``; ``returns=None``
  simulates a successful abort and ``returns=Exception(...)`` drives
  the swallow-and-warn DEC-014 path on US-003's ``__exit__``.

Each helper consumes one matching call; non-matching calls raise
``AssertionError("unexpected materialise_sample: ...")`` /
``"unexpected abort_session: ..."``.
"""

from __future__ import annotations

import pytest
from google.api_core.exceptions import BadRequest, NotFound

from signalforge.warehouse import (
    MaterialisationFailedError,
    PartitionFilter,
    TableRef,
)
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


# ---------------------------------------------------------------------------
# US-004 — expect_materialise_sample helper.
# ---------------------------------------------------------------------------


_SOURCE_REF = TableRef(project="fake_project", dataset="ds", name="src")
_RETURNED_TEMP = TableRef(project="fake_project", dataset="_SESSION", name="_sf_sample_abc123")


def test_expect_materialise_sample_consumes_one_call() -> None:
    """One registered expectation accepts one matching CTAS call; a
    second call raises the standard ``unexpected ...`` AssertionError.
    """
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_materialise_sample(
        _SOURCE_REF,
        sample_size=100,
        returns=_RETURNED_TEMP,
    )

    sql = (
        "CREATE TEMP TABLE _sf_sample_abc123 AS "
        "SELECT * FROM `fake_project.ds.src` AS t "
        "WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), 50) < 1 "
        "ORDER BY FARM_FINGERPRINT(TO_JSON_STRING(t)) "
        "LIMIT 100"
    )
    job = fake.query(sql)
    # The materialise path requires a session_info attribute carrying
    # the BigQuery-assigned session_id (production reads this after
    # ``.result()`` to capture the session).
    list(job.result())
    session_info = getattr(job, "session_info", None)
    assert session_info is not None
    assert session_info.session_id  # non-empty string

    # Second call must raise loudly — explicit-expectation pattern.
    with pytest.raises(AssertionError, match="unexpected"):
        fake.query(sql)


def test_expect_materialise_sample_returns_exception_propagates() -> None:
    """``returns=Exception(...)`` raises the exception when the matching
    CTAS call lands. Drives US-005's "materialisation failed → kept-without-evidence"
    branch via the typed error.
    """
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_materialise_sample(
        _SOURCE_REF,
        sample_size=100,
        returns=MaterialisationFailedError("boom"),
    )

    sql = (
        "CREATE TEMP TABLE _sf_sample_abc123 AS "
        "SELECT * FROM `fake_project.ds.src` AS t "
        "WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), 50) < 1 "
        "LIMIT 100"
    )
    with pytest.raises(MaterialisationFailedError, match="boom"):
        fake.query(sql)


def test_expect_materialise_sample_assert_all_expectations_met() -> None:
    """An unconsumed materialise expectation fails the final assertion
    so a test cannot silently skip the materialise call site.
    """
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_materialise_sample(
        _SOURCE_REF,
        sample_size=100,
        returns=_RETURNED_TEMP,
    )

    with pytest.raises(AssertionError) as excinfo:
        fake.assert_all_expectations_met()

    assert "materialise_sample" in str(excinfo.value)


def test_expect_materialise_sample_partition_filter_must_match() -> None:
    """When ``partition_filter`` is registered, the rendered filter
    fragment must appear in the matched SQL — otherwise the call is
    treated as unexpected. Defends against US-005 silently dropping
    the filter from the CTAS WHERE clause.
    """
    fake = FakeBigQueryClient(project="fake_project")
    pf = PartitionFilter(column="event_date", op=">=", value="2024-01-01")
    fake.expect_materialise_sample(
        _SOURCE_REF,
        sample_size=100,
        partition_filter=pf,
        returns=_RETURNED_TEMP,
    )

    # Missing the filter fragment → no match → AssertionError.
    sql_without_filter = (
        "CREATE TEMP TABLE _sf_sample_abc123 AS "
        "SELECT * FROM `fake_project.ds.src` AS t "
        "WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), 50) < 1 "
        "LIMIT 100"
    )
    with pytest.raises(AssertionError, match="unexpected"):
        fake.query(sql_without_filter)

    # SQL carrying the rendered filter consumes the expectation.
    sql_with_filter = (
        "CREATE TEMP TABLE _sf_sample_abc123 AS "
        "SELECT * FROM `fake_project.ds.src` AS t "
        "WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), 50) < 1 "
        "AND `event_date` >= '2024-01-01' "
        "LIMIT 100"
    )
    job = fake.query(sql_with_filter)
    assert getattr(job, "session_info", None) is not None


# ---------------------------------------------------------------------------
# US-004 — expect_abort_session helper.
# ---------------------------------------------------------------------------


_SESSION_A = "session_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_SESSION_B = "session_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class _FakeConnectionProperty:
    """Stand-in for ``google.cloud.bigquery.query.ConnectionProperty`` —
    only the two attributes the production helper sets / the fake reads.
    """

    def __init__(self, *, key: str, value: str) -> None:
        self.key = key
        self.value = value


class _FakeJobConfig:
    """Minimal stand-in carrying the ``connection_properties`` list the
    abort-session matcher reads. Mirrors the SDK's QueryJobConfig
    surface narrowly so the meta-tests don't pull in the real SDK.
    """

    def __init__(self, *, session_id: str | None = None) -> None:
        self.connection_properties: list[_FakeConnectionProperty] = (
            [_FakeConnectionProperty(key="session_id", value=session_id)]
            if session_id is not None
            else []
        )


def test_expect_abort_session_consumes_one_call() -> None:
    """One registered expectation accepts one matching abort call; a
    second call raises the standard ``unexpected ...`` AssertionError.
    """
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_abort_session(_SESSION_A)

    job = fake.query(
        "CALL BQ.ABORT_SESSION();",
        job_config=_FakeJobConfig(session_id=_SESSION_A),
    )
    list(job.result())  # success path returns an empty rowset

    with pytest.raises(AssertionError, match="unexpected"):
        fake.query(
            "CALL BQ.ABORT_SESSION();",
            job_config=_FakeJobConfig(session_id=_SESSION_A),
        )


def test_expect_abort_session_session_id_mismatch_raises() -> None:
    """An abort call carrying a different session_id than the one
    registered must NOT consume the expectation. Defends against
    US-003 routing the abort into the wrong session.
    """
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_abort_session(_SESSION_A)

    with pytest.raises(AssertionError, match="unexpected"):
        fake.query(
            "CALL BQ.ABORT_SESSION();",
            job_config=_FakeJobConfig(session_id=_SESSION_B),
        )


def test_expect_abort_session_returns_exception_propagates() -> None:
    """``returns=NotFound(...)`` raises the SDK exception so the caller
    can drive the DEC-014 swallow-and-warn path on US-003's ``__exit__``.
    """
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_abort_session(_SESSION_A, returns=NotFound("session vanished"))

    with pytest.raises(NotFound):
        fake.query(
            "CALL BQ.ABORT_SESSION();",
            job_config=_FakeJobConfig(session_id=_SESSION_A),
        )


def test_expect_abort_session_returns_none_succeeds() -> None:
    """The default ``returns=None`` simulates a successful abort: the
    matching call returns a job with no rows that ``.result()`` can
    iterate without raising.
    """
    fake = FakeBigQueryClient(project="fake_project")
    fake.expect_abort_session(_SESSION_A)  # returns=None default

    job = fake.query(
        "CALL BQ.ABORT_SESSION();",
        job_config=_FakeJobConfig(session_id=_SESSION_A),
    )
    rows = list(job.result())
    assert rows == []
    fake.assert_all_expectations_met()
