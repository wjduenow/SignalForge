"""Tests for the non-classify helpers in :mod:`signalforge.safety.redact`.

Covers :func:`hash_column_name`, :func:`redact_rows`, and
:func:`redact_column_names`. The classify-matrix tests live in
``test_classify.py`` (separate file because the matrix is large).
"""

from __future__ import annotations

import re

import pytest

from signalforge.safety.models import RedactionRecord
from signalforge.safety.redact import (
    hash_column_name,
    redact_column_names,
    redact_rows,
)

pytestmark = pytest.mark.safety


# ---------------------------------------------------------------------------
# hash_column_name
# ---------------------------------------------------------------------------


def test_hash_column_name_deterministic() -> None:
    assert hash_column_name("foo") == hash_column_name("foo")


def test_hash_column_name_distinct_for_distinct_inputs() -> None:
    assert hash_column_name("foo") != hash_column_name("bar")


def test_hash_column_name_format() -> None:
    h = hash_column_name("customer_email")
    assert re.fullmatch(r"col_[0-9a-f]{8}", h) is not None


def test_hash_column_name_handles_unicode() -> None:
    # Should not raise on non-ASCII input.
    h = hash_column_name("café_ñ_测试")
    assert re.fullmatch(r"col_[0-9a-f]{8}", h) is not None


# ---------------------------------------------------------------------------
# redact_rows
# ---------------------------------------------------------------------------


def test_redact_rows_replaces_values() -> None:
    rows = ({"a": 1, "b": 2},)
    result = redact_rows(rows, {"a"})
    assert result == ({"a": "<REDACTED>", "b": 2},)


def test_redact_rows_does_not_mutate_input() -> None:
    rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    snapshot = [dict(r) for r in rows]
    redact_rows(rows, {"a"})
    assert rows == snapshot
    # And the inner dicts are still the original ones.
    assert rows[0] == snapshot[0]
    assert rows[1] == snapshot[1]


def test_redact_rows_returns_tuple() -> None:
    result = redact_rows([{"a": 1}], {"a"})
    assert result.__class__ is tuple


def test_redact_rows_empty_redacted_list() -> None:
    rows = ({"a": 1, "b": 2},)
    result = redact_rows(rows, frozenset())
    assert result == ({"a": 1, "b": 2},)


def test_redact_rows_all_redacted() -> None:
    rows = ({"a": 1, "b": 2},)
    result = redact_rows(rows, {"a", "b"})
    assert result == ({"a": "<REDACTED>", "b": "<REDACTED>"},)


def test_redact_rows_missing_column_in_row_silent() -> None:
    rows = ({"a": 1, "b": 2},)
    # "c" is in the redacted set but not in the rows; should silently no-op.
    result = redact_rows(rows, {"c"})
    assert result == ({"a": 1, "b": 2},)


def test_redact_rows_accepts_list_input() -> None:
    result = redact_rows([{"a": 1}], {"a"})
    assert result == ({"a": "<REDACTED>"},)


def test_redact_rows_empty_input() -> None:
    assert redact_rows((), {"a"}) == ()


# ---------------------------------------------------------------------------
# redact_column_names
# ---------------------------------------------------------------------------


def _record(name: str, *, redacted: bool, hashed: str | None = None) -> RedactionRecord:
    return RedactionRecord(
        column_name=name,
        hashed_name=hashed if hashed is not None else hash_column_name(name),
        redacted=redacted,
        reason="pattern_match",
    )


def test_redact_column_names_substitutes_hashed_for_redacted() -> None:
    columns = (("email", "STRING"), ("id", "INT64"))
    records = (_record("email", redacted=True),)
    result = redact_column_names(columns, records)
    expected_hash = hash_column_name("email")
    assert result == ((expected_hash, "STRING"), ("id", "INT64"))


def test_redact_column_names_preserves_non_redacted() -> None:
    columns = (("email", "STRING"), ("id", "INT64"))
    # A record with redacted=False should leave the name alone, even if
    # listed.
    records = (_record("email", redacted=False),)
    result = redact_column_names(columns, records)
    assert result == (("email", "STRING"), ("id", "INT64"))


def test_redact_column_names_returns_tuple_of_tuples() -> None:
    columns = [("email", "STRING")]
    records = (_record("email", redacted=True),)
    result = redact_column_names(columns, records)
    assert result.__class__ is tuple
    assert all(item.__class__ is tuple for item in result)


def test_redact_column_names_handles_no_records() -> None:
    columns = (("email", "STRING"),)
    result = redact_column_names(columns, ())
    assert result == (("email", "STRING"),)
