"""Tests for ``SnowflakeAdapter.materialise_sample`` + ``run_test_sql`` (issue
#122, US-004).

``materialise_sample`` lands a deterministic sample into a session-scoped
``TEMPORARY TABLE`` (``_sf_sample_<run_id>``; ``run_id`` from the shared
:func:`signalforge.warehouse._sample_id._compute_run_id` recipe, byte-identical
to BigQuery — DEC-008) and pins the live connection so a follow-up
``run_test_sql`` reaches the temp table (DEC-002). ``run_test_sql`` wraps a
candidate failing-rows SELECT in a ``COUNT(*)`` aggregate and returns a typed
:class:`TestResult` (DEC-004).

The load-bearing AC pinned here is the #116 materialised-sample-substitution
gotcha: the prune compiler — fed the returned temp :class:`TableRef` with
:data:`SNOWFLAKE_DIALECT` — emits SQL referencing the temp table, NOT the
source. A self-FROM test type that bypassed this would silently full-scan
production under the cost-saving materialised strategy.

Uses :class:`FakeSnowflakeConnection` with explicit ``expect_execute``
round-trips — never a ``MagicMock`` (``testing-signal.md``). Determinism is
engineered by asserting the exact SQL bytes / run_id, not just behaviour.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from signalforge.warehouse._sample_id import _compute_run_id
from signalforge.warehouse.adapters.snowflake import SnowflakeAdapter
from signalforge.warehouse.errors import MaterialisationFailedError, QuerySyntaxError
from signalforge.warehouse.models import SNOWFLAKE_DIALECT, PartitionFilter, TableRef
from tests.warehouse._fake_snowflake import FakeSnowflakeConnection

# Source table: ``project`` is the database (GCP-style project-id grammar);
# ``dataset`` (schema) / ``name`` use the strict identifier regex (uppercase
# fine). The name ``ORDERS`` is engineered to be distinct from the
# ``_sf_sample_<hash>`` temp name so the #116 substitution test can assert the
# temp name appears and the source name does NOT.
_TABLE = TableRef(project="mydatabase", dataset="SCH", name="ORDERS")

_SIZE_QUERY = r"INFORMATION_SCHEMA\.TABLES"
_CTAS_QUERY = r"CREATE TEMPORARY TABLE"
_COUNT_QUERY = r"SELECT COUNT\(\*\) AS failures"


class _RecordingConnection(FakeSnowflakeConnection):
    """A :class:`FakeSnowflakeConnection` that records every executed SQL and
    which cursor object served it (so reachability — both queries on ONE
    connection — is observable)."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.executed: list[str] = []

    def _consume_execute(self, sql: str):  # type: ignore[override]
        self.executed.append(sql)
        return super()._consume_execute(sql)


def _make_adapter(conn: FakeSnowflakeConnection) -> SnowflakeAdapter:
    return SnowflakeAdapter(connection=conn)


def _expected_run_id() -> str:
    return _compute_run_id(table=_TABLE, n=100, partition_filter=None)


# ---------------------------------------------------------------------------
# materialise_sample — CTAS shape + deterministic temp name (DEC-006/007/008)
# ---------------------------------------------------------------------------


def test_materialise_ctas_sql_shape_and_temp_name() -> None:
    """The CTAS contains ``CREATE TEMPORARY TABLE``, the deterministic
    ``_sf_sample_<run_id>`` name (run_id byte-identical to the shared recipe),
    the ``MOD(ABS(HASH(*)), <bucket>) < 1`` predicate, ``ORDER BY
    ABS(HASH(*))``, ``LIMIT n``; the source is per-component quoted."""
    conn = _RecordingConnection()
    # num_rows=1000, n=100 → bucket = max(1000//100, 1) = 10.
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    adapter.materialise_sample(_TABLE, 100)

    ctas = conn.executed[1]
    run_id = _expected_run_id()
    temp_name = f"_sf_sample_{run_id}"

    assert ctas.startswith("CREATE TEMPORARY TABLE")
    assert temp_name in ctas
    # Deterministic hash-mod predicate (read from the dialect, not hard-coded).
    assert "MOD(ABS(HASH(*)), 10) < 1" in ctas
    assert "ORDER BY ABS(HASH(*))" in ctas
    assert ctas.rstrip().endswith("LIMIT 100")
    # Source is per-component quoted: "mydatabase"."SCH"."ORDERS".
    assert '"mydatabase"."SCH"."ORDERS"' in ctas
    # Temp table colocated with the source DB / schema, per-component quoted.
    assert f'"mydatabase"."SCH"."{temp_name}"' in ctas


def test_materialise_returns_fully_qualified_temp_ref() -> None:
    """The returned :class:`TableRef` is fully-qualified via the source DB /
    schema with the deterministic temp name (DEC-007)."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    result = adapter.materialise_sample(_TABLE, 100)

    run_id = _expected_run_id()
    assert result == TableRef(
        project=_TABLE.project, dataset=_TABLE.dataset, name=f"_sf_sample_{run_id}"
    )


def test_materialise_pins_active_session_and_started_at() -> None:
    """``materialise_sample`` sets ``_active_session`` to the connection and
    stamps ``_session_started_at`` (DEC-002)."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    assert adapter._session_started_at is None
    adapter.materialise_sample(_TABLE, 100)

    assert adapter._active_session is conn
    assert adapter._session_started_at is not None


def test_materialise_run_id_byte_identical_across_calls() -> None:
    """Identical ``(table, n, partition_filter)`` → byte-identical temp-table
    name across two fresh adapters (DEC-006/008)."""
    names: list[str] = []
    for _ in range(2):
        conn = _RecordingConnection()
        conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
        conn.expect_execute(matching=_CTAS_QUERY, returns=[])
        adapter = _make_adapter(conn)
        names.append(adapter.materialise_sample(_TABLE, 100).name)
    assert names[0] == names[1]
    assert names[0] == f"_sf_sample_{_expected_run_id()}"


# ---------------------------------------------------------------------------
# Reachability — follow-up run_test_sql executes on the SAME connection (DEC-002)
# ---------------------------------------------------------------------------


def test_materialised_temp_table_is_reachable_via_same_connection() -> None:
    """After ``materialise_sample``, a follow-up ``run_test_sql`` executes on
    the SAME connection object — so the session-scoped temp table is reachable
    (DEC-002). The fake records both executes on one connection."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(0,)], description=[("FAILURES",)])
    adapter = _make_adapter(conn)

    temp_ref = adapter.materialise_sample(_TABLE, 100)
    # A test compiled against the temp ref runs on the same connection.
    test_sql = f'SELECT "ID" FROM "mydatabase"."SCH"."{temp_ref.name}" WHERE "ID" IS NULL'
    adapter.run_test_sql(test_sql)

    # Both the CTAS and the COUNT query were recorded on the one connection.
    assert len(conn.executed) == 3
    assert conn.executed[1].startswith("CREATE TEMPORARY TABLE")
    assert conn.executed[2].startswith("SELECT COUNT(*) AS failures")
    # The COUNT wrapper references the temp table, not the source.
    assert temp_ref.name in conn.executed[2]
    assert "ORDERS" not in conn.executed[2]
    assert adapter._active_session is conn


# ---------------------------------------------------------------------------
# #116 substitution — the compiler references the TEMP table, NOT the source
# ---------------------------------------------------------------------------


def test_compiler_substitutes_temp_table_not_source() -> None:
    """The #116 materialised-sample-substitution gotcha, exercised on the test
    type that can ACTUALLY bypass it: a self-FROM ``custom_sql`` singular test
    (``SELECT ... FROM {{ this }} ...``). Fed the materialised temp
    :class:`TableRef` with :data:`SNOWFLAKE_DIALECT` at ``scope="full"`` (the
    shape the engine uses after materialising), the compiler must rewrite the
    resolved ``{{ this }}`` source name to the ``_sf_sample_<run_id>`` temp
    table. A bypass here would silently full-scan production under the
    materialised strategy.

    (The four built-in variants — ``not_null`` etc. — always ``FROM`` the
    passed ``table_ref`` and so can never bypass substitution; only the
    self-FROM ``custom_sql`` path can, which is why the gotcha is pinned here.)
    """
    from signalforge.draft.models import CandidateTestCustomSQL
    from signalforge.manifest.models import Column, Manifest, Model
    from signalforge.prune.compiler import _compile_test

    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    temp_ref = adapter.materialise_sample(_TABLE, 100)

    # A model whose ``resolve_this()`` == the SOURCE table (mydatabase.SCH.ORDERS),
    # so the custom_sql ``{{ this }}`` resolves to the source and the compiler
    # must rewrite it to the temp ``table_ref`` (because temp != source).
    model = Model(
        unique_id="model.shop.orders",
        name="ORDERS",
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="mydatabase",
        schema="SCH",  # type: ignore[call-arg]
        columns={"AMOUNT": Column(name="AMOUNT")},
        raw_code="select 1",
    )
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={"model.shop.orders": model},
    )

    compiled = _compile_test(
        CandidateTestCustomSQL(sql="SELECT * FROM {{ this }} WHERE AMOUNT < 0"),
        temp_ref,
        SNOWFLAKE_DIALECT,
        manifest,
        model=model,
        scope="full",
    )

    assert isinstance(compiled, str)
    # The self-FROM ``{{ this }}`` was rewritten to the materialised temp table
    # (the compiler folds identifiers to UPPER for Snowflake) ...
    assert temp_ref.name.upper() in compiled.upper()
    # ... and the source table's bare name never leaks (a bypass would leave
    # the resolved source ``ORDERS`` here, full-scanning production).
    assert "ORDERS" not in compiled.upper()


# ---------------------------------------------------------------------------
# run_test_sql — COUNT(*) wrap + capture_failures (DEC-004)
# ---------------------------------------------------------------------------


def test_run_test_sql_zero_failures_passes() -> None:
    """Zero failing rows → ``passed=True``, ``failure_count=0``."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(0,)], description=[("FAILURES",)])
    adapter = _make_adapter(conn)

    result = adapter.run_test_sql('SELECT "ID" FROM "DB"."SCH"."T" WHERE "ID" IS NULL')

    assert result.passed is True
    assert result.failure_count == 0
    assert result.sample_failures is None
    assert result.row_schema is None


def test_run_test_sql_nonzero_failures_fails() -> None:
    """Non-zero failing rows → ``passed=False``, ``failure_count=N``."""
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=[(7,)], description=[("FAILURES",)])
    adapter = _make_adapter(conn)

    result = adapter.run_test_sql('SELECT "ID" FROM "DB"."SCH"."T" WHERE "ID" IS NULL')

    assert result.passed is False
    assert result.failure_count == 7


def test_run_test_sql_capture_failures_populates_samples() -> None:
    """``capture_failures > 0`` wraps with ``ARRAY_AGG(OBJECT_CONSTRUCT(*))``
    and populates ``sample_failures`` as a list of dicts."""
    conn = _RecordingConnection()
    samples = [{"ID": 1, "NAME": "a"}, {"ID": 2, "NAME": "b"}]
    conn.expect_execute(
        matching=r"ARRAY_AGG\(OBJECT_CONSTRUCT\(\*\)\)",
        returns=[(2, samples)],
        description=[("FAILURES",), ("SAMPLES",)],
    )
    adapter = _make_adapter(conn)

    result = adapter.run_test_sql(
        'SELECT "ID" FROM "DB"."SCH"."T" WHERE "ID" IS NULL', capture_failures=5
    )

    assert result.passed is False
    assert result.failure_count == 2
    assert result.sample_failures == samples
    # The wrapper LIMITs the sample subquery at capture_failures.
    assert "LIMIT 5" in conn.executed[0]


# ---------------------------------------------------------------------------
# Failure modes (DEC-007/009)
# ---------------------------------------------------------------------------


def test_materialise_rejects_non_positive_n() -> None:
    """``n <= 0`` → ``ValueError`` before any warehouse contact."""
    conn = FakeSnowflakeConnection()
    adapter = _make_adapter(conn)

    with pytest.raises(ValueError, match="n > 0"):
        adapter.materialise_sample(_TABLE, 0)

    # No query issued.
    conn.assert_all_expectations_met()


def test_materialise_ctas_sdk_failure_wraps_in_materialisation_failed() -> None:
    """A CTAS SDK failure → :class:`MaterialisationFailedError` with the
    underlying exception preserved on ``cause`` (DEC-007/009)."""
    boom = RuntimeError("network blip during CTAS")
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=boom)
    adapter = _make_adapter(conn)

    with pytest.raises(MaterialisationFailedError) as exc_info:
        adapter.materialise_sample(_TABLE, 100)

    # The raw exception is preserved as cause AND in the raise-from chain.
    assert exc_info.value.cause is boom
    assert exc_info.value.__cause__ is boom
    assert "mydatabase.SCH.ORDERS" in str(exc_info.value)


def test_materialise_logs_hashed_session_id_never_raw(caplog) -> None:  # noqa: ANN001
    """The success INFO log carries ``session_id_hash`` (blake2b-4), never the
    raw connection ``session_id`` (DEC-003 redaction)."""
    import logging

    conn = _RecordingConnection(session_id="super-secret-session-xyz")
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    with caplog.at_level(logging.INFO, logger="signalforge.warehouse"):
        adapter.materialise_sample(_TABLE, 100)

    records = [r for r in caplog.records if "materialised sample" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "session_id_hash" in message
    assert "super-secret-session-xyz" not in message
    # The hash is deterministic over the raw id.
    from signalforge.warehouse._sample_id import _hash_session_id

    assert _hash_session_id("super-secret-session-xyz") in message


def test_run_test_sql_validates_sql_first() -> None:
    """``validate_test_sql`` rejects a SQL with a ``;`` before any execute."""
    conn = FakeSnowflakeConnection()
    adapter = _make_adapter(conn)

    with pytest.raises(Exception, match=re.compile(r"semicolon|;|safety", re.IGNORECASE)):
        adapter.run_test_sql("SELECT 1; DROP TABLE t")

    conn.assert_all_expectations_met()


def test_materialise_applies_partition_filter_in_ctas() -> None:
    """A ``PartitionFilter`` lands ONCE in the CTAS ``WHERE`` (rendered via the
    Snowflake dialect literal template) alongside the hash-mod predicate."""
    conn = _RecordingConnection()
    conn.expect_execute(matching=_SIZE_QUERY, returns=[(1000,)])
    conn.expect_execute(matching=_CTAS_QUERY, returns=[])
    adapter = _make_adapter(conn)

    pf = PartitionFilter(column="DT", op=">=", value=date(2026, 1, 1))
    adapter.materialise_sample(_TABLE, 100, partition_filter=pf)

    ctas = conn.executed[1]
    assert "MOD(ABS(HASH(*)), 10) < 1" in ctas
    # Rendered via SNOWFLAKE_DIALECT.date_literal_template: '{value}'::DATE.
    assert "'2026-01-01'::DATE" in ctas
    assert ctas.count("'2026-01-01'::DATE") == 1


def test_run_test_sql_programming_error_maps_to_query_syntax_error() -> None:
    """A connector ``ProgrammingError`` from the COUNT(*) wrap maps to
    :class:`QuerySyntaxError` (the ``run_test_sql`` mapped-error branch)."""
    pytest.importorskip("snowflake.connector")
    from snowflake.connector import errors as sfe

    conn = FakeSnowflakeConnection()
    conn.expect_execute(
        matching=_COUNT_QUERY,
        returns=sfe.ProgrammingError("SQL compilation error"),
    )
    adapter = _make_adapter(conn)

    with pytest.raises(QuerySyntaxError):
        adapter.run_test_sql('SELECT "ID" FROM "DB"."SCH"."T" WHERE "ID" IS NULL')


def test_run_test_sql_unmapped_error_passes_through_unchanged() -> None:
    """An exception ``map_snowflake_exception`` does not map is re-raised
    unchanged from ``run_test_sql`` — the passthrough branch."""
    sentinel = RuntimeError("transient network blip")
    conn = FakeSnowflakeConnection()
    conn.expect_execute(matching=_COUNT_QUERY, returns=sentinel)
    adapter = _make_adapter(conn)

    with pytest.raises(RuntimeError) as exc_info:
        adapter.run_test_sql('SELECT "ID" FROM "DB"."SCH"."T" WHERE "ID" IS NULL')
    assert exc_info.value is sentinel
