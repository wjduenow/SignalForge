"""Tests for the ingest anchor-contract validator (US-004).

Pins the fail-loud, collect-all contract (DEC-002 / DEC-007): every
violation across the whole candidate is collected and surfaced in one
:class:`IngestAnchorContractError`; the validator never short-circuits on
the first violation. A clean candidate raises nothing.
"""

from __future__ import annotations

import pytest

from signalforge.draft.models import CandidateSchema
from signalforge.ingest.anchor import validate_anchor_contract
from signalforge.ingest.errors import IngestAnchorContractError

_MODEL_COLUMNS = frozenset({"id", "email", "region"})


def _candidate(*, columns: list[dict], tests: list[dict] | None = None) -> CandidateSchema:
    return CandidateSchema.model_validate(
        {
            "name": "stg_users",
            "description": "Staged users.",
            "columns": columns,
            "tests": tests or [],
        }
    )


def test_clean_candidate_does_not_raise() -> None:
    candidate = _candidate(
        columns=[
            {
                "name": "id",
                "description": "Primary key.",
                "tests": [
                    {"type": "not_null", "column": "id"},
                    {"type": "unique", "column": "id"},
                ],
            },
            {
                "name": "region",
                "description": "Region code.",
                "tests": [
                    {"type": "accepted_values", "column": "region", "values": ["us", "eu"]},
                ],
            },
        ],
        tests=[{"type": "not_null", "column": "email"}],
    )
    # Returns None; the absence of a raise is the assertion.
    assert validate_anchor_contract(candidate, _MODEL_COLUMNS) is None


def test_single_violation_nonexistent_column_name_raises() -> None:
    candidate = _candidate(
        columns=[
            {
                "name": "ghost",
                "description": "Hallucinated column.",
                "tests": [{"type": "not_null", "column": "ghost"}],
            },
        ],
    )
    with pytest.raises(IngestAnchorContractError) as exc_info:
        validate_anchor_contract(candidate, _MODEL_COLUMNS)
    violations = exc_info.value.violations
    # The column-name violation and the test-column violation both fire
    # for the same hallucinated name (independent checks, not elif).
    assert any("CandidateColumn references nonexistent column 'ghost'" in v for v in violations)


def test_parent_column_mismatch_raises() -> None:
    candidate = _candidate(
        columns=[
            {
                "name": "id",
                "description": "Primary key.",
                # Real column on the model, but test points at a sibling.
                "tests": [{"type": "not_null", "column": "email"}],
            },
        ],
    )
    with pytest.raises(IngestAnchorContractError) as exc_info:
        validate_anchor_contract(candidate, _MODEL_COLUMNS)
    assert any(
        "column test on column='id' references 'email'" in v for v in exc_info.value.violations
    )


def test_model_level_test_missing_column_raises() -> None:
    candidate = _candidate(
        columns=[
            {
                "name": "id",
                "description": "Primary key.",
                "tests": [{"type": "not_null", "column": "id"}],
            },
        ],
        tests=[{"type": "not_null", "column": "nope"}],
    )
    with pytest.raises(IngestAnchorContractError) as exc_info:
        validate_anchor_contract(candidate, _MODEL_COLUMNS)
    assert any(
        "model-level test references nonexistent column 'nope'" in v
        for v in exc_info.value.violations
    )


def test_multiple_violations_all_collected_no_short_circuit() -> None:
    """Pins collect-all: a candidate with four distinct violations raises
    ONE error whose ``violations`` contains every one of them."""
    candidate = _candidate(
        columns=[
            {
                # Violation 1: column name 'ghost' not on the model.
                # Violation 2: the test on 'ghost' references a nonexistent column.
                "name": "ghost",
                "description": "Hallucinated column.",
                "tests": [{"type": "not_null", "column": "ghost"}],
            },
            {
                # Violation 3: real column 'id', test references a sibling 'email'.
                "name": "id",
                "description": "Primary key.",
                "tests": [{"type": "not_null", "column": "email"}],
            },
        ],
        # Violation 4: model-level test references a nonexistent column.
        tests=[{"type": "not_null", "column": "phantom"}],
    )
    with pytest.raises(IngestAnchorContractError) as exc_info:
        validate_anchor_contract(candidate, _MODEL_COLUMNS)
    violations = exc_info.value.violations

    expected_substrings = [
        "CandidateColumn references nonexistent column 'ghost'",
        "test references nonexistent column 'ghost'",
        "column test on column='id' references 'email'",
        "model-level test references nonexistent column 'phantom'",
    ]
    for expected in expected_substrings:
        assert any(expected in v for v in violations), (
            f"missing expected violation substring: {expected!r}; got {violations!r}"
        )
    # Exactly the four distinct violations above — no more, no fewer.
    assert len(violations) == len(expected_substrings)
