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


def test_model_level_row_count_between_with_none_column_does_not_raise() -> None:
    """Issue #169 — ``row_count_between`` is model-level only and the
    Pydantic model fixes ``column = None``. ``None not in model_columns``
    would otherwise fire a spurious "references nonexistent column None"
    violation, blocking the variant through ``prune-existing``. The
    exemption mirrors the drafter-side anchor in
    ``signalforge.draft.parser._validate_anchor_contract``.
    """
    candidate = _candidate(
        columns=[
            {
                "name": "id",
                "description": "Primary key.",
                "tests": [{"type": "not_null", "column": "id"}],
            },
        ],
        tests=[
            # `column` defaults to None on the Pydantic model — operators
            # never provide it for this variant, and the discriminated-union
            # exemption must catch the None case.
            {
                "type": "row_count_between",
                "minimum": 1,
                "where": "1 = 0",
            },
            {
                "type": "row_count_between",
                "minimum": 0,
            },
        ],
    )
    # Returns None; the absence of a raise is the assertion.
    assert validate_anchor_contract(candidate, _MODEL_COLUMNS) is None


def test_model_level_unique_combination_with_none_column_does_not_raise() -> None:
    """Issue #170 — ``unique_combination`` is model-level only and the
    Pydantic model fixes ``column = None``. ``None not in model_columns``
    would otherwise fire a spurious "references nonexistent column None"
    violation, blocking the variant through ``prune-existing``. The
    exemption mirrors the drafter-side anchor in
    ``signalforge.draft.parser._validate_anchor_contract`` and the
    ``row_count_between`` exemption above.
    """
    candidate = _candidate(
        columns=[
            {
                "name": "id",
                "description": "Primary key.",
                "tests": [{"type": "not_null", "column": "id"}],
            },
        ],
        tests=[
            # `column` defaults to None on the Pydantic model — operators
            # never provide it for this variant, and the discriminated-union
            # exemption must catch the None case.
            {
                "type": "unique_combination",
                "columns": ["id", "email"],
            },
            {
                "type": "unique_combination",
                "columns": ["id", "email", "region"],
                "where": "region IS NOT NULL",
            },
        ],
    )
    # Returns None; the absence of a raise is the assertion.
    assert validate_anchor_contract(candidate, _MODEL_COLUMNS) is None


def test_model_level_unique_combination_with_hallucinated_column_raises_per_column() -> None:
    """CodeRabbit PR-180 finding (triangulated with QG Pass 1 C2 + Pass 2 #2
    + US-010 worker docstring) — the ingest anchor must validate each
    ``unique_combination.columns[i]`` against the model, mirroring the
    draft-parser side at
    ``signalforge.draft.parser._validate_anchor_contract``. Before this
    fix, a hand-authored ``dbt_utils.unique_combination_of_columns`` block
    referencing a nonexistent column passed the ingest anchor silently
    (the warehouse later rejected the SQL via the conservative-bias
    ``kept-without-evidence`` route, but with a generic "identifier
    rejected" message rather than a precise per-column violation).

    Asserts the collect-all contract — multiple hallucinated columns
    surface as multiple distinct violations in one error.
    """
    candidate = _candidate(
        columns=[
            {
                "name": "id",
                "description": "Primary key.",
                "tests": [{"type": "not_null", "column": "id"}],
            },
        ],
        tests=[
            {
                "type": "unique_combination",
                # Two hallucinated columns; one real (``id``).
                "columns": ["id", "nonexistent_a", "nonexistent_b"],
            },
        ],
    )
    with pytest.raises(IngestAnchorContractError) as excinfo:
        validate_anchor_contract(candidate, _MODEL_COLUMNS)
    violations = excinfo.value.violations
    # Collect-all: both hallucinated columns surface, real ``id`` does not.
    assert len(violations) == 2
    assert any(
        "unique_combination references nonexistent column 'nonexistent_a'" in v for v in violations
    )
    assert any(
        "unique_combination references nonexistent column 'nonexistent_b'" in v for v in violations
    )
    # The real column ``id`` MUST NOT appear as a violation HEADER. (The
    # "available: [...]" suffix legitimately lists every model column,
    # including ``id``, so a naive substring search yields a false
    # positive — pin the header shape instead.)
    assert not any("unique_combination references nonexistent column 'id'" in v for v in violations)
