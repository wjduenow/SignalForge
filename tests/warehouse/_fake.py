"""Hand-rolled fake for google.cloud.bigquery.Client (DEC-002, DEC-028).

Tests register expectations via expect_query / expect_get_table /
expect_list_rows / expect_materialise_sample / expect_abort_session.
The fake's query/get_table/list_rows methods consume one matching
expectation per call; unexpected calls raise loudly.

US-004 (issue #22) added two purpose-built helpers that mirror the
production-side surface of :meth:`BigQueryAdapter.materialise_sample`
and the ``CALL BQ.ABORT_SESSION();`` cleanup path:

* :meth:`FakeBigQueryClient.expect_materialise_sample` — pins one
  ``CREATE TEMP TABLE _sf_sample_<run_id> AS SELECT ...`` round-trip.
  Returns a job carrying ``session_info.session_id`` so the production
  code can capture the BigQuery-assigned session id after ``.result()``.
* :meth:`FakeBigQueryClient.expect_abort_session` — pins one
  ``CALL BQ.ABORT_SESSION();`` round-trip keyed by the session id
  carried in ``job_config.connection_properties`` (the production code
  routes the abort into the active session via that property).
  ``returns=None`` simulates a successful abort; ``returns=Exception``
  drives the DEC-014 swallow-and-warn path on
  :meth:`BigQueryAdapter.__exit__`.

Both helpers consume one matching call; non-matching calls raise the
standard ``AssertionError("unexpected ...")`` shape.

Lives in tests/warehouse/ (not in the package proper) — never imported by
production code.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from signalforge.warehouse.models import PartitionFilter, TableRef


@dataclass(frozen=True)
class FakeRow:
    """Stand-in for google.cloud.bigquery.Row (dict-indexable iteration target)."""

    values: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def items(self) -> Iterable[tuple[str, Any]]:
        return self.values.items()


@dataclass
class FakeTable:
    """Stand-in for google.cloud.bigquery.Table — only fields adapter reads."""

    num_rows: int | None = None
    schema: list[tuple[str, str]] = field(default_factory=list)  # (name, bq_type)


@dataclass
class _QueryExpectation:
    matching: re.Pattern[str]
    returns: list[dict[str, Any]] | Exception
    job_config_check: Any = None  # optional callable(job_config) -> bool


@dataclass
class _GetTableExpectation:
    ref: TableRef
    returns: FakeTable | Exception


@dataclass
class _ListRowsExpectation:
    ref: TableRef
    returns: list[dict[str, Any]] | Exception


@dataclass
class _MaterialiseSampleExpectation:
    """US-004 (issue #22) — one registered ``CREATE TEMP TABLE _sf_sample_...``
    expectation. Matches the production CTAS shape via three independent
    SQL-substring checks (CREATE TEMP TABLE prefix + source ref's
    qualified-name occurrence + LIMIT <sample_size>) plus an optional
    rendered partition_filter fragment when the registration carried one.
    """

    source_ref: TableRef
    sample_size: int
    partition_filter: PartitionFilter | None
    returns: TableRef | Exception


@dataclass
class _AbortSessionExpectation:
    """US-004 (issue #22) — one registered ``CALL BQ.ABORT_SESSION();``
    expectation, keyed by the session id the production code carries
    via ``job_config.connection_properties=[ConnectionProperty(key="session_id",
    value=<id>)]``. ``returns=None`` simulates a successful abort
    (job's ``.result()`` yields an empty rowset); ``returns=Exception``
    drives the DEC-014 swallow-and-warn path on US-003's ``__exit__``.
    """

    session_id: str
    returns: None | Exception


class _FakeQueryJob:
    """Stand-in for google.cloud.bigquery.QueryJob with a result() iterator."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        schema: list[tuple[str, str]] | None = None,
    ):
        self._rows = rows
        self._schema = schema or []

    def result(self) -> Iterable[FakeRow]:
        return [FakeRow(r) for r in self._rows]

    @property
    def total_rows(self) -> int:
        return len(self._rows)

    @property
    def schema(self) -> list[tuple[str, str]]:
        return self._schema


class _FakeSessionInfo:
    """Stand-in for ``bigquery.QueryJob.session_info``: single attr.

    The production :meth:`BigQueryAdapter.materialise_sample` reads
    ``job.session_info.session_id`` after ``.result()`` to capture the
    server-assigned session id. The
    :meth:`FakeBigQueryClient.expect_materialise_sample` helper builds
    one of these on the success path so production has something to
    capture (mirrors the dedicated stand-in in
    ``tests/warehouse/test_materialise_sample.py``).
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _FakeQueryJobWithSession(_FakeQueryJob):
    """``_FakeQueryJob`` carrying a populated ``session_info``.

    Used exclusively by the materialise-sample expectation path so the
    production code can read ``job.session_info.session_id`` after
    ``.result()`` exactly as it does against the real SDK.
    """

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        session_id: str,
    ) -> None:
        super().__init__(rows)
        self.session_info: _FakeSessionInfo = _FakeSessionInfo(session_id)


class FakeBigQueryClient:
    """Explicit fake; calls outside expectations raise AssertionError."""

    def __init__(self, project: str = "fake-project") -> None:
        self.project = project
        self._query_expectations: list[_QueryExpectation] = []
        self._get_table_expectations: list[_GetTableExpectation] = []
        self._list_rows_expectations: list[_ListRowsExpectation] = []
        self._materialise_sample_expectations: list[_MaterialiseSampleExpectation] = []
        self._abort_session_expectations: list[_AbortSessionExpectation] = []

    # ---- expectation API --------------------------------------------------

    def expect_query(
        self,
        *,
        matching: re.Pattern[str] | str,
        returns: list[dict[str, Any]] | Exception,
        job_config_check: Any = None,
    ) -> None:
        pattern = matching if isinstance(matching, re.Pattern) else re.compile(matching)
        self._query_expectations.append(
            _QueryExpectation(
                matching=pattern,
                returns=returns,
                job_config_check=job_config_check,
            )
        )

    def expect_get_table(self, *, ref: TableRef, returns: FakeTable | Exception) -> None:
        self._get_table_expectations.append(_GetTableExpectation(ref=ref, returns=returns))

    def expect_list_rows(self, *, ref: TableRef, returns: list[dict[str, Any]] | Exception) -> None:
        self._list_rows_expectations.append(_ListRowsExpectation(ref=ref, returns=returns))

    def expect_materialise_sample(
        self,
        source_ref: TableRef,
        sample_size: int,
        partition_filter: PartitionFilter | None = None,
        *,
        returns: TableRef | Exception,
    ) -> None:
        """US-004 (issue #22) — register one materialise-sample
        expectation mirroring :meth:`BigQueryAdapter.materialise_sample`.

        Matches a ``client.query(...)`` call whose SQL begins with
        ``CREATE TEMP TABLE _sf_sample_`` (the prefix the production
        helper builds from a deterministic ``run_id``), references
        ``source_ref``'s fully-qualified ``project.dataset.name`` (or
        the two-part ``dataset.name`` form when ``source_ref.project``
        is ``None``), and carries the literal ``LIMIT <sample_size>``
        clause. When ``partition_filter`` is supplied, the rendered
        BigQuery filter fragment must also appear in the SQL — this
        defends US-005 against silently dropping the filter from the
        CTAS WHERE clause.

        On match:
            * ``returns: TableRef`` — the matched call returns a
              :class:`_FakeQueryJobWithSession` carrying a deterministic
              ``session_info.session_id`` derived from the temp-table
              name in ``returns``. Production captures this id via
              ``job.session_info.session_id`` after ``.result()`` and
              constructs its own returned :class:`TableRef` from
              ``client.project + "_SESSION" + run_id`` (so test authors
              should pass the same ``run_id`` substring in
              ``returns.name`` to keep the assertions on the production
              return value consistent).
            * ``returns: Exception`` — the matched call raises the
              given exception. Drives the
              ``MaterialisationFailedError`` wrapping path in
              :meth:`BigQueryAdapter.materialise_sample` and the
              prune-orchestrator's "kept-without-evidence" branch in
              US-005.

        Args:
            source_ref: Source production table the CTAS reads from.
            sample_size: Target sample size; must appear as ``LIMIT
                <sample_size>`` in the matched SQL.
            partition_filter: Optional :class:`PartitionFilter` whose
                rendered fragment must appear in the matched SQL.
            returns: A :class:`TableRef` (success) or an
                :class:`Exception` (failure) instance.
        """
        if sample_size <= 0:
            raise AssertionError(
                f"expect_materialise_sample requires sample_size > 0; got {sample_size}"
            )
        self._materialise_sample_expectations.append(
            _MaterialiseSampleExpectation(
                source_ref=source_ref,
                sample_size=sample_size,
                partition_filter=partition_filter,
                returns=returns,
            )
        )

    def expect_abort_session(
        self,
        session_id: str,
        *,
        returns: None | Exception = None,
    ) -> None:
        """US-004 (issue #22) — register one abort-session expectation
        mirroring :meth:`BigQueryAdapter.__exit__`'s cleanup path.

        Matches a ``client.query(...)`` call whose SQL begins with
        ``CALL BQ.ABORT_SESSION`` AND whose ``job_config`` carries a
        ``connection_properties`` entry with ``key="session_id"`` and
        ``value == session_id``. The session-id keying is the load-bearing
        check: a ``__exit__`` that routed the abort into the wrong
        session would mismatch and raise the standard
        ``unexpected ...`` AssertionError.

        On match:
            * ``returns=None`` (the default) — the matched call returns
              an empty job that ``.result()`` iterates without raising.
              Drives the DEC-013 happy-path INFO log on
              :meth:`BigQueryAdapter.__exit__`.
            * ``returns=Exception(...)`` — the matched call raises the
              given exception. Drives the DEC-014 swallow-and-warn
              path (multi-line WARNING with raw session_id + manual
              ``bq query`` command + ``auto-expire in <N>s`` line).

        Args:
            session_id: The expected session id the production helper
                will route through ``connection_properties``.
            returns: ``None`` to simulate a successful abort (default)
                or an :class:`Exception` to simulate failure.
        """
        self._abort_session_expectations.append(
            _AbortSessionExpectation(session_id=session_id, returns=returns)
        )

    def assert_all_expectations_met(self) -> None:
        unconsumed: list[str] = []
        if self._query_expectations:
            unconsumed.append(f"{len(self._query_expectations)} query expectations")
        if self._get_table_expectations:
            unconsumed.append(f"{len(self._get_table_expectations)} get_table expectations")
        if self._list_rows_expectations:
            unconsumed.append(f"{len(self._list_rows_expectations)} list_rows expectations")
        if self._materialise_sample_expectations:
            unconsumed.append(
                f"{len(self._materialise_sample_expectations)} materialise_sample expectations"
            )
        if self._abort_session_expectations:
            unconsumed.append(f"{len(self._abort_session_expectations)} abort_session expectations")
        if unconsumed:
            raise AssertionError("Unconsumed expectations: " + ", ".join(unconsumed))

    # ---- google-cloud-bigquery surface -----------------------------------

    def query(self, sql: str, job_config: Any = None) -> _FakeQueryJob:
        # US-004 (issue #22) — when the materialise-sample / abort-session
        # queues have registered expectations, the matching SQL prefixes
        # short-circuit into those queues. When the queues are empty, the
        # generic ``expect_query`` queue still owns the dispatch — that
        # preserves US-003's existing tests which scaffold CTAS / abort
        # calls via raw ``expect_query`` matchers (the plan for US-004
        # explicitly says NOT to retrofit those tests).
        if self._materialise_sample_expectations and _is_materialise_sample_sql(sql):
            return self._consume_materialise_sample(sql)
        if self._abort_session_expectations and _is_abort_session_sql(sql):
            return self._consume_abort_session(sql, job_config)
        for i, exp in enumerate(self._query_expectations):
            if exp.matching.search(sql):
                if exp.job_config_check is not None and not exp.job_config_check(job_config):
                    raise AssertionError(f"job_config_check rejected job_config for query: {sql!r}")
                self._query_expectations.pop(i)
                if isinstance(exp.returns, Exception):
                    raise exp.returns
                return _FakeQueryJob(exp.returns)
        raise AssertionError(f"unexpected query: {sql!r}")

    def _consume_materialise_sample(self, sql: str) -> _FakeQueryJob:
        """Walk :attr:`_materialise_sample_expectations` for a match
        against ``sql``; consume one matching entry on success.

        A match requires (in order): the source ref's qualified-name
        substring is present, ``LIMIT <sample_size>`` is present, and
        — when the registration carried a ``partition_filter`` — the
        rendered filter fragment is present too. Non-match raises the
        standard ``unexpected materialise_sample: ...`` AssertionError.
        """
        for i, exp in enumerate(self._materialise_sample_expectations):
            if not _materialise_sample_matches(sql, exp):
                continue
            self._materialise_sample_expectations.pop(i)
            if isinstance(exp.returns, Exception):
                raise exp.returns
            # ``returns`` is a TableRef on the success path. Synthesise
            # a deterministic session_id from the temp-table name so
            # the production code's session_info.session_id capture is
            # observable; tests that need to pin a specific session_id
            # for the matching abort-session expectation can derive it
            # from this same helper.
            session_id = _derive_fake_session_id(exp.returns.name)
            return _FakeQueryJobWithSession(rows=[], session_id=session_id)
        raise AssertionError(f"unexpected materialise_sample: {sql!r}")

    def _consume_abort_session(self, sql: str, job_config: Any) -> _FakeQueryJob:
        """Walk :attr:`_abort_session_expectations` for a match against
        ``sql`` + ``job_config``; consume one matching entry on success.

        A match requires the session_id carried in
        ``job_config.connection_properties`` (key=``"session_id"``)
        equals the registered ``session_id``. Non-match raises the
        standard ``unexpected abort_session: ...`` AssertionError.
        """
        actual_session_id = _extract_session_id_from_job_config(job_config)
        for i, exp in enumerate(self._abort_session_expectations):
            if actual_session_id != exp.session_id:
                continue
            self._abort_session_expectations.pop(i)
            if isinstance(exp.returns, Exception):
                raise exp.returns
            return _FakeQueryJob(rows=[])
        raise AssertionError(
            f"unexpected abort_session: session_id={actual_session_id!r}, sql={sql!r}"
        )

    def get_table(self, ref: Any) -> FakeTable:
        # Accept either a TableRef or anything with project/dataset/name attrs.
        target_ref = ref if isinstance(ref, TableRef) else _coerce_to_tableref(ref)
        for i, exp in enumerate(self._get_table_expectations):
            if exp.ref == target_ref:
                self._get_table_expectations.pop(i)
                if isinstance(exp.returns, Exception):
                    raise exp.returns
                return exp.returns
        raise AssertionError(f"unexpected get_table call: {target_ref}")

    def list_rows(self, ref: Any, max_results: int | None = None) -> list[FakeRow]:
        target_ref = ref if isinstance(ref, TableRef) else _coerce_to_tableref(ref)
        for i, exp in enumerate(self._list_rows_expectations):
            if exp.ref == target_ref:
                self._list_rows_expectations.pop(i)
                if isinstance(exp.returns, Exception):
                    raise exp.returns
                rows = exp.returns
                if max_results is not None:
                    rows = rows[:max_results]
                return [FakeRow(r) for r in rows]
        raise AssertionError(f"unexpected list_rows call: {target_ref}")


def _coerce_to_tableref(ref: Any) -> TableRef:
    if isinstance(ref, str):
        parts = ref.split(".")
        if len(parts) == 3:
            project, dataset, name = parts
            return TableRef(project=project, dataset=dataset, name=name)
        if len(parts) == 2:
            dataset, name = parts
            return TableRef(project=None, dataset=dataset, name=name)
        raise AssertionError(
            "table reference strings must be 'dataset.table' or "
            f"'project.dataset.table', got: {ref!r}"
        )
    return TableRef(project=ref.project, dataset=ref.dataset_id, name=ref.table_id)


# ---------------------------------------------------------------------------
# US-004 (issue #22) — internal helpers for the new expectation queues.
# ---------------------------------------------------------------------------


_MATERIALISE_SAMPLE_PREFIX_RE = re.compile(
    r"^\s*CREATE\s+TEMP\s+TABLE\s+_sf_sample_", re.IGNORECASE
)
_ABORT_SESSION_PREFIX_RE = re.compile(r"^\s*CALL\s+BQ\.ABORT_SESSION", re.IGNORECASE)


def _is_materialise_sample_sql(sql: str) -> bool:
    return bool(_MATERIALISE_SAMPLE_PREFIX_RE.search(sql))


def _is_abort_session_sql(sql: str) -> bool:
    return bool(_ABORT_SESSION_PREFIX_RE.search(sql))


def _qualified_name_substring(ref: TableRef) -> str:
    """Mirror :meth:`BigQueryAdapter._quote`'s output shape — backtick-
    quoted ``project.dataset.name`` (or ``client_project.dataset.name``
    when ``ref.project`` is ``None``; we can't know the client's project
    here so fall back to ``dataset.name``).
    """
    if ref.project is not None:
        return f"`{ref.project}.{ref.dataset}.{ref.name}`"
    # When the registration omits ``project``, callers either scaffold
    # against the client's billing project (covered by the prefix +
    # LIMIT match alone) or pass an explicit ``ref.project`` that the
    # production CTAS will mirror byte-for-byte.
    return f".{ref.dataset}.{ref.name}`"


def _render_partition_filter_for_match(pf: PartitionFilter) -> str:
    """Mirror :meth:`BigQueryAdapter._render_partition_filter` so the
    rendered substring the matcher looks for is byte-equal to what
    production emits.
    """
    if isinstance(pf.value, datetime):
        rendered = f"TIMESTAMP('{pf.value.isoformat()}')"
    elif isinstance(pf.value, date):
        rendered = f"DATE('{pf.value.isoformat()}')"
    else:
        # Mirror the BigQuery escape rules used by
        # ``escape_bq_string_literal``. The meta-test uses a literal
        # ASCII date string with no escapable bytes, so a simple
        # ``str(value)`` is byte-equal to what production emits for
        # the most common case. If a future test passes a string with
        # backslashes / quotes / control bytes, this helper should be
        # extended to call ``escape_bq_string_literal`` directly.
        rendered = f"'{pf.value}'"
    return f"`{pf.column}` {pf.op} {rendered}"


def _materialise_sample_matches(sql: str, exp: _MaterialiseSampleExpectation) -> bool:
    if _qualified_name_substring(exp.source_ref) not in sql:
        return False
    if f"LIMIT {exp.sample_size}" not in sql:
        return False
    if exp.partition_filter is not None:
        rendered = _render_partition_filter_for_match(exp.partition_filter)
        if rendered not in sql:
            return False
    return True


def _extract_session_id_from_job_config(job_config: Any) -> str | None:
    """Read ``connection_properties[?key=="session_id"].value`` off
    a duck-typed job_config. Returns ``None`` when the job_config
    carries no connection_properties at all (a shape mismatch the
    matcher surfaces via the ``unexpected abort_session`` AssertionError).
    """
    props = getattr(job_config, "connection_properties", None) or []
    for prop in props:
        if getattr(prop, "key", None) == "session_id":
            return getattr(prop, "value", None)
    return None


def _derive_fake_session_id(temp_table_name: str) -> str:
    """Synthesise a deterministic session_id from a temp-table name so
    tests can correlate the materialise expectation's ``returns``
    TableRef with the abort expectation's ``session_id``.

    The production code reads this via ``job.session_info.session_id``
    after ``.result()``; the value is opaque to production so any
    deterministic string keyed off the temp-table name works.
    """
    return f"sess_{temp_table_name}"
