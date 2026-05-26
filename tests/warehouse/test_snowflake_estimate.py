"""Tests for the pure ``_parse_explain_json_bytes`` EXPLAIN-cell parser
(issue #130 US-002, DEC-001 / DEC-002 / DEC-006).

The fixtures under ``tests/fixtures/warehouse/snowflake/`` are hand-crafted —
workers/CI can't reach a live Snowflake. Engineered determinism: the parsed
``int`` is asserted EQUAL to the fixture's known ``bytesAssigned`` (a round
100 MiB), so the test fails on any real regression rather than rubber-stamping
whatever the parser returns. See that directory's README.md for the
maintainer regeneration command against a live ``EXPLAIN USING JSON``.

These tests need no connection — the parser is a module-level pure function.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from signalforge.warehouse.adapters.snowflake import _parse_explain_json_bytes
from signalforge.warehouse.errors import EstimateUnavailableError

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "warehouse" / "snowflake"

# The byte count the sample fixture encodes (100 MiB). Pinned so the parse
# assertion is mathematically guaranteed, not whatever the parser returns.
_EXPECTED_BYTES = 104_857_600


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_parses_fixture_str_to_expected_int() -> None:
    """str (JSON) input → exactly the fixture's bytesAssigned."""
    result = _parse_explain_json_bytes(_load("explain_using_json_sample.json"))
    assert result == _EXPECTED_BYTES


def test_parses_fixture_dict_to_expected_int() -> None:
    """A pre-parsed dict (the connector's other return shape) → same int."""
    document = json.loads(_load("explain_using_json_sample.json"))
    assert isinstance(document, dict)
    assert _parse_explain_json_bytes(document) == _EXPECTED_BYTES


def test_accepts_minimal_dict_input() -> None:
    """The navigation only needs GlobalStats.bytesAssigned, nothing else."""
    assert _parse_explain_json_bytes({"GlobalStats": {"bytesAssigned": 42}}) == 42


def test_zero_bytes_is_a_valid_estimate() -> None:
    """A genuine ``0`` (e.g. a fully-pruned plan) is a real estimate, not a
    degrade — distinct from a *missing* field (which raises)."""
    assert _parse_explain_json_bytes({"GlobalStats": {"bytesAssigned": 0}}) == 0


def test_missing_global_stats_raises() -> None:
    """No-stat fixture (GlobalStats absent) → EstimateUnavailableError."""
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_json_bytes(_load("explain_using_json_no_stats.json"))
    assert "GlobalStats" in excinfo.value.detail


def test_global_stats_not_a_mapping_raises() -> None:
    with pytest.raises(EstimateUnavailableError):
        _parse_explain_json_bytes({"GlobalStats": ["not", "a", "dict"]})


def test_missing_bytes_assigned_raises() -> None:
    """GlobalStats present but no bytesAssigned → raises (never returns 0)."""
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_json_bytes({"GlobalStats": {"partitionsTotal": 10, "partitionsAssigned": 2}})
    assert "bytesAssigned" in excinfo.value.detail


def test_malformed_json_str_raises() -> None:
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_json_bytes("{not valid json")
    assert "JSON" in excinfo.value.detail


def test_non_object_json_document_raises() -> None:
    """A JSON array / scalar at the top level is not a plan document."""
    with pytest.raises(EstimateUnavailableError):
        _parse_explain_json_bytes("[1, 2, 3]")


def test_non_int_bytes_assigned_raises() -> None:
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_json_bytes({"GlobalStats": {"bytesAssigned": "104857600"}})
    assert "integer" in excinfo.value.detail


def test_float_bytes_assigned_raises() -> None:
    """A float is not coerced — it must be an int (no silent truncation)."""
    with pytest.raises(EstimateUnavailableError):
        _parse_explain_json_bytes({"GlobalStats": {"bytesAssigned": 1.5}})


def test_bool_bytes_assigned_raises() -> None:
    """bool is an int subclass in Python, but ``True`` is not a byte count."""
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_json_bytes({"GlobalStats": {"bytesAssigned": True}})
    assert "integer" in excinfo.value.detail


def test_negative_bytes_assigned_raises() -> None:
    with pytest.raises(EstimateUnavailableError) as excinfo:
        _parse_explain_json_bytes({"GlobalStats": {"bytesAssigned": -1}})
    assert "negative" in excinfo.value.detail
