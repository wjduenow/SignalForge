"""US-003 / issue #22 — BigQueryAdapter.materialise_sample tests.

22 tests exercising the materialise + run_test_sql + __exit__ surfaces
introduced by US-003 of plans/super/22-temp-table-sample.md. Every test
injects a :class:`FakeBigQueryClient` (DEC-002 of #3) — never reaches
real BigQuery.

The fake's ``expect_query`` is sufficient for v0.2 — US-004 will add
the explicit ``expect_materialise_sample`` / ``expect_abort_session``
helpers; this module deliberately uses raw query expectations and
job_config inspection so the tests don't pre-empt that surface.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
from google.api_core.exceptions import Forbidden, NotFound

from signalforge.warehouse import (
    BigQueryAdapter,
    MaterialisationFailedError,
    PartitionFilter,
    TableRef,
)
from signalforge.warehouse.adapters import bigquery as bq_module
from signalforge.warehouse.adapters.bigquery import _compute_run_id, _hash_session_id
from tests.warehouse._fake import FakeBigQueryClient, FakeTable, _FakeQueryJob

_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "warehouse" / "sample_materialise_v1.sql"
)


# ---------------------------------------------------------------------------
# Test infrastructure: FakeQueryJob with .session_info, capture helper.
# ---------------------------------------------------------------------------


class _FakeSessionInfo:
    """Stand-in for ``bigquery.QueryJob.session_info`` — single attr only."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _FakeQueryJobWithSession(_FakeQueryJob):
    """Extension of :class:`_FakeQueryJob` carrying a ``session_info``.

    The production code reads ``job.session_info.session_id`` after
    ``.result()`` to capture BigQuery's server-assigned session id.
    The base fake does not expose this attribute; this subclass adds
    it. Used only in the materialise path.
    """

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        session_id: str | None,
    ) -> None:
        super().__init__(rows)
        self.session_info: _FakeSessionInfo | None = (
            _FakeSessionInfo(session_id) if session_id is not None else None
        )


def _wrap_query_capture(fake_client: FakeBigQueryClient) -> dict[str, Any]:
    """Capture every SQL string + job_config the adapter passes to
    ``client.query``. Mirrors ``test_bigquery_unit._wrap_query_capture``
    but also retains the ``job_config`` so session-state assertions
    can introspect ``connection_properties``.
    """
    captured: dict[str, Any] = {"sqls": [], "job_configs": []}
    original = fake_client.query

    def wrapped(sql: str, job_config: Any = None) -> Any:
        captured["sqls"].append(sql)
        captured["job_configs"].append(job_config)
        return original(sql, job_config=job_config)

    fake_client.query = wrapped  # type: ignore[method-assign]
    return captured


def _materialise_response_query(
    fake_client: FakeBigQueryClient,
    *,
    session_id: str = "session_abcdef0123456789abcdef0123456789",
    matching: str = r"^CREATE TEMP TABLE _sf_sample_",
) -> None:
    """Register a fake-client expectation that returns a job carrying
    ``session_info.session_id = <session_id>``.

    Implemented by patching the underlying expectation's return path so
    the resulting ``_FakeQueryJob`` carries ``session_info``. The
    fake's ``query`` constructs a base ``_FakeQueryJob``; we
    monkey-patch the module's ``_FakeQueryJob`` reference so the
    matched expectation produces our session-aware subclass.
    """
    fake_client.expect_query(matching=matching, returns=[])

    # Replace the _FakeQueryJob constructor for this single matched
    # call by wrapping the fake's query method one extra time.
    original = fake_client.query

    def wrapped(sql: str, job_config: Any = None) -> Any:
        result = original(sql, job_config=job_config)
        return _FakeQueryJobWithSession(getattr(result, "_rows", []), session_id=session_id)

    fake_client.query = wrapped  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Common fixtures (the conftest already provides ``adapter`` / ``fake_client``
# / ``table_ref`` / ``shakespeare_table`` — we reuse those).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 1-12: materialise_sample core surface.
# ---------------------------------------------------------------------------


def test_materialise_sample_returns_tableref_with_session_dataset(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """DEC-001/002 — return value must be ``TableRef(project=None,
    dataset='_SESSION', name='_sf_sample_<16-hex>')``.

    ``project=None`` is load-bearing: BigQuery rejects the three-part
    ``<project>._SESSION.<name>`` form even inside the owning session
    ("Use of _SESSION is not allowed here"). Caught during the
    maintainer probe-run on 2026-05-08; the fix is in
    ``BigQueryAdapter.materialise_sample`` returning ``project=None`` so
    ``TableRef.qualified_name`` renders the two-part
    ``_SESSION._sf_sample_<run_id>`` form that BigQuery accepts.
    """
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    _materialise_response_query(fake_client)

    result = adapter.materialise_sample(table_ref, n=100)

    assert isinstance(result, TableRef)
    assert result.dataset == "_SESSION"
    assert result.project is None
    assert re.fullmatch(r"_sf_sample_[0-9a-f]{16}", result.name)


def test_materialise_sample_temp_table_name_passes_validate_identifier(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """C3 of #22 — the temp-table name must pass the strict identifier
    regex. Production code already calls validate_identifier; this test
    pins the contract by feeding the returned TableRef back into a fresh
    instantiation (which re-runs the regex via TableRef's
    __post_init__).
    """
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    _materialise_response_query(fake_client)

    result = adapter.materialise_sample(table_ref, n=100)

    # Re-construct via TableRef to assert the regex passes.
    rebuilt = TableRef(project=result.project, dataset=result.dataset, name=result.name)
    assert rebuilt.name == result.name


def test_materialise_sample_run_id_is_deterministic_per_inputs(
    table_ref: TableRef,
) -> None:
    """DEC-001 — same (table, n, partition_filter) → same run_id.

    The temp-table name embeds the run_id, so this also pins the
    ``compiled_sql_hash`` reproducibility invariant the prune layer
    depends on.
    """
    a = _compute_run_id(table=table_ref, n=100, partition_filter=None)
    b = _compute_run_id(table=table_ref, n=100, partition_filter=None)
    assert a == b
    # Same table, different n → distinct run_id.
    c = _compute_run_id(table=table_ref, n=200, partition_filter=None)
    assert a != c


def test_materialise_sample_run_id_changes_with_signalforge_version(
    table_ref: TableRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEC-001 — pin signalforge_version's role in the hash.

    A SignalForge upgrade invalidates any cached materialisation —
    because compiled SQL referencing the temp-table name shifts when
    the version bumps. Without the version in the hash, an old
    cached temp-table would silently survive across upgrades.
    """
    import signalforge

    a = _compute_run_id(table=table_ref, n=100, partition_filter=None)
    monkeypatch.setattr(signalforge, "__version__", "9.9.9.fake0")
    b = _compute_run_id(table=table_ref, n=100, partition_filter=None)
    assert a != b


def test_materialise_sample_create_temp_table_sql_byte_equal_fixture(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """Snapshot-pin the CTAS SQL byte-for-byte against
    ``tests/fixtures/warehouse/sample_materialise_v1.sql``.

    Substitutes the deterministic ``{run_id}`` placeholder with the
    test-time computed value because the run_id depends on
    ``signalforge.__version__`` — pinning a literal would force a
    fixture refresh on every version bump.
    """
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    _materialise_response_query(fake_client)
    captured = _wrap_query_capture(fake_client)

    adapter.materialise_sample(table_ref, n=100)

    expected_run_id = _compute_run_id(table=table_ref, n=100, partition_filter=None)
    expected_sql = (
        _FIXTURE_PATH.read_text(encoding="utf-8").rstrip("\n").format(run_id=expected_run_id)
    )
    assert captured["sqls"][0] == expected_sql


def test_materialise_sample_routes_through_default_job_config_with_correct_stage_label(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """DEC-015 of #3 — stage label must be ``warehouse_sample_materialise``.

    Distinct from ``warehouse_sample`` (oneshot) so v0.2
    INFORMATION_SCHEMA cost attribution can split the bytes-billed
    by strategy. Pinned via the labels dict on the captured job_config.
    """
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    _materialise_response_query(fake_client)
    captured = _wrap_query_capture(fake_client)

    adapter.materialise_sample(table_ref, n=100)

    job_config = captured["job_configs"][0]
    labels = dict(job_config.labels)
    assert labels["signalforge_stage"] == "warehouse_sample_materialise"


def test_materialise_sample_applies_partition_filter_in_where_clause(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """Q5 of #22 — the partition filter lands in the materialisation
    WHERE clause, NOT in per-test queries against the temp table.
    """
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    _materialise_response_query(fake_client)
    captured = _wrap_query_capture(fake_client)

    pf = PartitionFilter(column="corpus_date", op="=", value="2020-01-01")
    adapter.materialise_sample(table_ref, n=100, partition_filter=pf)

    sql = captured["sqls"][0]
    assert "`corpus_date` = '2020-01-01'" in sql
    # And it must be inside the WHERE clause, after MOD(...).
    assert sql.index("MOD(") < sql.index("`corpus_date`")


def test_materialise_sample_uses_create_session_in_job_config(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """DEC-002 — the materialise job_config must carry
    ``create_session=True`` so BigQuery mints a session_id server-side.
    Replaces the ``test_materialise_sample_uses_connection_properties_for_session``
    test from the plan TDD list — the materialise call itself does NOT
    yet have a session_id (BigQuery assigns it in the response), so the
    materialise's job_config carries ``create_session`` while subsequent
    ``run_test_sql`` calls carry ``connection_properties``.
    """
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    _materialise_response_query(fake_client)
    captured = _wrap_query_capture(fake_client)

    adapter.materialise_sample(table_ref, n=100)

    job_config = captured["job_configs"][0]
    assert getattr(job_config, "create_session", False) is True


def test_materialise_sample_wraps_warehouse_sdk_errors_as_materialisation_failed(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """DEC-008 of #22 — every SDK / network / quota failure during the
    CTAS surfaces as :class:`MaterialisationFailedError` with the
    original exception preserved on ``cause``.
    """
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    forbidden = Forbidden("permission denied")
    fake_client.expect_query(matching=r"^CREATE TEMP TABLE _sf_sample_", returns=forbidden)

    with pytest.raises(MaterialisationFailedError) as exc_info:
        adapter.materialise_sample(table_ref, n=100)
    assert exc_info.value.cause is forbidden


def test_run_test_sql_uses_active_session_id_after_materialise(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """DEC-002 — once ``materialise_sample`` has minted a session, every
    subsequent ``run_test_sql`` query routes into it via
    ``connection_properties``. The fake's job_config inspection pins
    the ConnectionProperty's session_id matches the captured value.
    """
    session_id = "session_abcdef0123456789abcdef0123456789"
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    _materialise_response_query(fake_client, session_id=session_id)
    fake_client.expect_query(matching=r"SELECT COUNT", returns=[{"failures": 0}])
    captured = _wrap_query_capture(fake_client)

    adapter.materialise_sample(table_ref, n=100)
    adapter.run_test_sql("SELECT * FROM `_SESSION._sf_sample_x` WHERE 1=0")

    test_job_config = captured["job_configs"][1]
    cps = list(getattr(test_job_config, "connection_properties", []))
    assert len(cps) == 1
    cp = cps[0]
    assert cp.key == "session_id"
    assert cp.value == session_id


def test_materialise_sample_logs_session_id_hash_not_raw(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEC-003 — the success INFO log contains ``session_id_hash`` only;
    the raw 32-hex session_id never appears.
    """
    session_id = "session_abcdef0123456789abcdef0123456789"
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    _materialise_response_query(fake_client, session_id=session_id)

    caplog.set_level(logging.INFO, logger="signalforge.warehouse")
    adapter.materialise_sample(table_ref, n=100)

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    rendered = info_records[0].getMessage()
    expected_hash = _hash_session_id(session_id)
    assert expected_hash in rendered
    # The raw session_id MUST NOT leak; the message contains ONLY the
    # 8-char hash. Search for the raw value to be sure.
    assert session_id not in rendered


def test_materialise_sample_default_job_config_use_query_cache_is_false(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    table_ref: TableRef,
    shakespeare_table: FakeTable,
) -> None:
    """DEC-015 of #3 — every QueryJobConfig must carry
    ``use_query_cache=False``. Materialisation does NOT get a cache
    bypass (Architectural Commitment #5: explainable diffs).
    """
    fake_client.expect_get_table(ref=table_ref, returns=shakespeare_table)
    _materialise_response_query(fake_client)
    captured = _wrap_query_capture(fake_client)

    adapter.materialise_sample(table_ref, n=100)

    job_config = captured["job_configs"][0]
    assert job_config.use_query_cache is False


# ---------------------------------------------------------------------------
# 13-22: __exit__ cleanup surface (DEC-013 / DEC-014).
# ---------------------------------------------------------------------------


def test_bigquery_adapter_exit_closes_active_session(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-013 — ``__exit__`` must issue ``CALL BQ.ABORT_SESSION();`` in
    the same session via ``connection_properties``.
    """
    session_id = "session_abcdef0123456789abcdef0123456789"
    adapter._active_session_id = session_id
    adapter._session_started_at = bq_module._monotonic()
    adapter._session_ttl_seconds = 3600

    fake_client.expect_query(matching=r"^CALL BQ\.ABORT_SESSION", returns=[])
    captured = _wrap_query_capture(fake_client)

    adapter.__exit__(None, None, None)

    assert any("CALL BQ.ABORT_SESSION" in s for s in captured["sqls"])
    abort_job_config = captured["job_configs"][0]
    cps = list(getattr(abort_job_config, "connection_properties", []))
    assert len(cps) == 1 and cps[0].value == session_id


def test_bigquery_adapter_exit_no_op_when_no_active_session(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """``__exit__`` without prior materialise — no ABORT_SESSION call."""
    captured = _wrap_query_capture(fake_client)

    adapter.__exit__(None, None, None)

    assert captured["sqls"] == []


def test_bigquery_adapter_exit_runs_after_materialise_failure_set_session_state(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-013 — even if a partial materialise left ``_active_session_id``
    set before raising, ``__exit__`` still issues the abort.

    Simulates the production failure path where the CTAS succeeds but
    a follow-up step fails AFTER state lands. The cleanup contract is
    "if state is set, clean it up regardless of why we exited".
    """
    adapter._active_session_id = "session_partial"
    adapter._session_started_at = bq_module._monotonic()
    adapter._session_ttl_seconds = 3600

    fake_client.expect_query(matching=r"^CALL BQ\.ABORT_SESSION", returns=[])

    adapter.__exit__(MaterialisationFailedError, MaterialisationFailedError("x"), None)

    assert adapter._active_session_id is None


def test_bigquery_adapter_exit_swallows_close_errors(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-013 — abort failures never propagate. Cleanup never blocks
    the user; their actual work succeeded. State must reset to None.
    """
    adapter._active_session_id = "session_xyz"
    adapter._session_started_at = bq_module._monotonic()
    adapter._session_ttl_seconds = 3600

    fake_client.expect_query(
        matching=r"^CALL BQ\.ABORT_SESSION", returns=NotFound("session not found")
    )

    # No exception propagates.
    adapter.__exit__(None, None, None)

    assert adapter._active_session_id is None


def test_bigquery_adapter_exit_logs_warning_with_raw_session_id_on_failure(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEC-014 — the deliberate exception to DEC-003. The cleanup-failure
    WARNING contains the raw 32-hex session_id because the manual
    ``bq query --connection_property=session_id=...`` command is
    unconstructable without it.
    """
    session_id = "session_abcdef0123456789abcdef0123456789"
    adapter._active_session_id = session_id
    adapter._session_started_at = bq_module._monotonic()
    adapter._session_ttl_seconds = 3600

    fake_client.expect_query(
        matching=r"^CALL BQ\.ABORT_SESSION", returns=NotFound("session vanished")
    )

    caplog.set_level(logging.WARNING, logger="signalforge.warehouse")
    adapter.__exit__(None, None, None)

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1
    body = warning_records[0].getMessage()
    assert session_id in body


def test_bigquery_adapter_exit_warning_contains_manual_kill_command(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEC-014 — WARNING body contains the operator-runnable
    ``bq query --connection_property=session_id=<raw>
    --use_legacy_sql=false "CALL BQ.ABORT_SESSION();"`` line.
    """
    session_id = "session_actionable12345"
    adapter._active_session_id = session_id
    adapter._session_started_at = bq_module._monotonic()
    adapter._session_ttl_seconds = 3600

    fake_client.expect_query(matching=r"^CALL BQ\.ABORT_SESSION", returns=NotFound("404"))

    caplog.set_level(logging.WARNING, logger="signalforge.warehouse")
    adapter.__exit__(None, None, None)

    body = caplog.records[0].getMessage()
    assert (
        f"bq query --connection_property=session_id={session_id} "
        f'--use_legacy_sql=false "CALL BQ.ABORT_SESSION();"' in body
    )


def test_bigquery_adapter_exit_warning_mentions_ttl_fallback(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEC-014 — ``auto-expire in <N>s`` line; ``N >= 1`` (the floor
    avoids ``auto-expire in 0s`` confusion).
    """
    adapter._active_session_id = "session_id_x"
    adapter._session_started_at = bq_module._monotonic()
    adapter._session_ttl_seconds = 7200

    fake_client.expect_query(matching=r"^CALL BQ\.ABORT_SESSION", returns=NotFound("404"))

    caplog.set_level(logging.WARNING, logger="signalforge.warehouse")
    adapter.__exit__(None, None, None)

    body = caplog.records[0].getMessage()
    m = re.search(r"auto-expire in (\d+)s", body)
    assert m is not None
    assert int(m.group(1)) >= 1


def test_bigquery_adapter_exit_warning_mentions_exception_class_name(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEC-014 — exception class name in the WARNING body so the
    operator knows what kind of failure they're dealing with.
    """
    adapter._active_session_id = "session_x"
    adapter._session_started_at = bq_module._monotonic()
    adapter._session_ttl_seconds = 3600

    fake_client.expect_query(matching=r"^CALL BQ\.ABORT_SESSION", returns=NotFound("404"))

    caplog.set_level(logging.WARNING, logger="signalforge.warehouse")
    adapter.__exit__(None, None, None)

    body = caplog.records[0].getMessage()
    assert "NotFound" in body


def test_bigquery_adapter_exit_resets_session_state_in_finally_even_on_close_failure(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
) -> None:
    """DEC-013 — the ``finally`` block resets every session-state field
    to None regardless of whether abort succeeded or failed. A
    second ``__exit__`` call must be a no-op.
    """
    adapter._active_session_id = "session_x"
    adapter._session_started_at = 1.0
    adapter._session_ttl_seconds = 3600

    fake_client.expect_query(matching=r"^CALL BQ\.ABORT_SESSION", returns=NotFound("404"))

    adapter.__exit__(None, None, None)

    assert adapter._active_session_id is None
    assert adapter._session_started_at is None
    assert adapter._session_ttl_seconds is None


def test_bigquery_adapter_exit_success_logs_session_id_hash_only(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEC-003 happy-path invariant — the successful close INFO log uses
    ``session_id_hash``; the raw value never appears.
    """
    session_id = "session_abcdef0123456789abcdef0123456789"
    adapter._active_session_id = session_id
    adapter._session_started_at = bq_module._monotonic()
    adapter._session_ttl_seconds = 3600

    fake_client.expect_query(matching=r"^CALL BQ\.ABORT_SESSION", returns=[])

    caplog.set_level(logging.INFO, logger="signalforge.warehouse")
    adapter.__exit__(None, None, None)

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    body = info_records[0].getMessage()
    assert _hash_session_id(session_id) in body
    assert session_id not in body


def test_bigquery_adapter_exit_success_does_not_emit_warning(
    adapter: BigQueryAdapter,
    fake_client: FakeBigQueryClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Happy-path emits INFO only; no WARNING.

    The operator's stderr (when --quiet is in effect) should be silent
    on a clean cleanup. The cleanup WARNING is reserved for the
    DEC-014 manual-recovery case.
    """
    adapter._active_session_id = "session_x"
    adapter._session_started_at = bq_module._monotonic()
    adapter._session_ttl_seconds = 3600

    fake_client.expect_query(matching=r"^CALL BQ\.ABORT_SESSION", returns=[])

    caplog.set_level(logging.WARNING, logger="signalforge.warehouse")
    adapter.__exit__(None, None, None)

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records == []
