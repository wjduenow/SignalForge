"""Hand-rolled fake for google.cloud.bigquery.Client (DEC-002, DEC-028).

Tests register expectations via expect_query / expect_get_table /
expect_list_rows. The fake's query/get_table/list_rows methods consume one
matching expectation per call; unexpected calls raise loudly.

Lives in tests/warehouse/ (not in the package proper) — never imported by
production code.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from signalforge.warehouse.models import TableRef


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


class FakeBigQueryClient:
    """Explicit fake; calls outside expectations raise AssertionError."""

    def __init__(self, project: str = "fake-project") -> None:
        self.project = project
        self._query_expectations: list[_QueryExpectation] = []
        self._get_table_expectations: list[_GetTableExpectation] = []
        self._list_rows_expectations: list[_ListRowsExpectation] = []

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

    def assert_all_expectations_met(self) -> None:
        unconsumed: list[str] = []
        if self._query_expectations:
            unconsumed.append(f"{len(self._query_expectations)} query expectations")
        if self._get_table_expectations:
            unconsumed.append(f"{len(self._get_table_expectations)} get_table expectations")
        if self._list_rows_expectations:
            unconsumed.append(f"{len(self._list_rows_expectations)} list_rows expectations")
        if unconsumed:
            raise AssertionError("Unconsumed expectations: " + ", ".join(unconsumed))

    # ---- google-cloud-bigquery surface -----------------------------------

    def query(self, sql: str, job_config: Any = None) -> _FakeQueryJob:
        for i, exp in enumerate(self._query_expectations):
            if exp.matching.search(sql):
                if exp.job_config_check is not None and not exp.job_config_check(job_config):
                    raise AssertionError(f"job_config_check rejected job_config for query: {sql!r}")
                self._query_expectations.pop(i)
                if isinstance(exp.returns, Exception):
                    raise exp.returns
                return _FakeQueryJob(exp.returns)
        raise AssertionError(f"unexpected query: {sql!r}")

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
        project, dataset, name = ref.split(".")
        return TableRef(project=project, dataset=dataset, name=name)
    return TableRef(project=ref.project, dataset=ref.dataset_id, name=ref.table_id)
