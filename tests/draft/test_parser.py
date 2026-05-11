"""Tests for ``signalforge.draft.parser`` (US-011).

Covers the two-stage parser: (1) JSON / Pydantic validation wrapping
raw LLM text into a typed :class:`CandidateSchema`, and (2) the
anchor-contract validator that rejects column references the input
model doesn't carry, parent-column mismatches on column-scoped tests,
and duplicate parameterless tests within a column.

The parser's whole-draft fail-loud contract (DEC-022) is exercised by
the ``collects_all_violations`` test: the validator must collect every
violation rather than short-circuiting on the first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from signalforge.draft.errors import (
    LLMOutputAnchorContractError,
    LLMOutputJSONError,
    LLMOutputValidationError,
)
from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestUnique,
)
from signalforge.draft.parser import (
    _LLMResultMeta,  # noqa: PLC2701  # private dataclass under test
    parse_draft_response,
)

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "draft"

_FCT_ORDERS_COLUMNS: frozenset[str] = frozenset({"order_id", "customer_id", "amount", "ordered_at"})


def _meta(
    *,
    prompt_version: str = "promptver000000a",
    model: str = "claude-haiku-4-5-20251001",
    cache_hit: bool = False,
    input_tokens: int = 1000,
    output_tokens: int = 500,
) -> _LLMResultMeta:
    return _LLMResultMeta(
        prompt_version=prompt_version,
        model=model,
        cache_hit=cache_hit,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _read(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


# ---------------------------------------------------------------------------
# Stage 1 — JSON / Pydantic validation
# ---------------------------------------------------------------------------


def test_parse_draft_response_happy_path() -> None:
    raw = _read("llm_response_valid.json")
    result = parse_draft_response(raw, _FCT_ORDERS_COLUMNS, llm_result_meta=_meta())
    assert isinstance(result, CandidateSchema)
    assert result.name == "fct_orders"
    assert {c.name for c in result.columns} == _FCT_ORDERS_COLUMNS


def test_parse_draft_response_truncated_raises_json_error() -> None:
    raw = _read("llm_response_truncated.json")
    with pytest.raises(LLMOutputJSONError) as excinfo:
        parse_draft_response(raw, _FCT_ORDERS_COLUMNS, llm_result_meta=_meta())
    err = excinfo.value
    assert err.parse_position is not None
    line, col = err.parse_position
    assert line >= 1
    assert col >= 1
    # Full raw_text preserved on the attribute even though __str__ truncates.
    assert err.raw_text == raw


def test_parse_draft_response_missing_field_raises_validation_error() -> None:
    raw = _read("llm_response_missing_field.json")
    with pytest.raises(LLMOutputValidationError) as excinfo:
        parse_draft_response(raw, _FCT_ORDERS_COLUMNS, llm_result_meta=_meta())
    err = excinfo.value
    # Not a JSON error — the payload parses fine, the schema rejects it.
    assert not isinstance(err, LLMOutputJSONError)
    assert isinstance(err.cause, ValidationError)
    assert err.parse_position is None


# ---------------------------------------------------------------------------
# Stage 2 — anchor-contract validator
# ---------------------------------------------------------------------------


def test_parse_draft_response_anchor_violation_column_test_raises() -> None:
    raw = _read("llm_response_anchor_violation.json")
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(raw, _FCT_ORDERS_COLUMNS, llm_result_meta=_meta())
    err = excinfo.value
    assert len(err.violations) >= 1
    # At least one violation should mention the bogus column name.
    assert any("customer_email" in v for v in err.violations)


def test_parse_draft_response_anchor_violation_model_level_test_raises() -> None:
    """Synthetic — model-level test cites a column not in ``model_columns``."""
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(CandidateColumn(name="order_id", description="pk", tests=()),),
        tests=(CandidateTestNotNull(column="phantom_col"),),
    )
    raw = candidate.model_dump_json()
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(raw, frozenset({"order_id"}), llm_result_meta=_meta())
    err = excinfo.value
    assert any("model-level test" in v and "phantom_col" in v for v in err.violations)


def test_parse_draft_response_anchor_violation_collects_all_violations() -> None:
    """Synthetic — multiple distinct violations must surface in one error.

    The parent-column mismatch ALSO counts as a nonexistent-column reference
    (Quality-Gate fix: parent-column-mismatch and nonexistent-column checks
    are independent so a hallucinated column name surfaces both).
    """
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(
            CandidateColumn(
                name="order_id",
                description="pk",
                tests=(
                    # Violations: parent-column mismatch + nonexistent column.
                    CandidateTestNotNull(column="customer_id"),
                ),
            ),
        ),
        # Violation: model-level test on nonexistent column.
        tests=(CandidateTestNotNull(column="phantom_col"),),
    )
    raw = candidate.model_dump_json()
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(raw, frozenset({"order_id"}), llm_result_meta=_meta())
    err = excinfo.value
    # Three: (1) parent-column mismatch, (2) nonexistent-column on the
    # column-scoped test, (3) nonexistent-column on the model-level test.
    assert len(err.violations) == 3
    assert any("parent" in v.lower() or "references 'customer_id'" in v for v in err.violations)
    assert any("phantom_col" in v for v in err.violations)


def test_parse_draft_response_column_test_must_match_parent_column() -> None:
    """Synthetic — column-scoped test cites a different column than its parent."""
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(
            CandidateColumn(
                name="a",
                description="...",
                tests=(CandidateTestNotNull(column="b"),),
            ),
        ),
    )
    raw = candidate.model_dump_json()
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(raw, frozenset({"a", "b"}), llm_result_meta=_meta())
    err = excinfo.value
    assert any("'a'" in v and "'b'" in v for v in err.violations)


def test_parse_draft_response_duplicate_test_within_column_raises() -> None:
    raw = _read("llm_response_duplicate_test.json")
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(raw, _FCT_ORDERS_COLUMNS, llm_result_meta=_meta())
    err = excinfo.value
    assert any("duplicate" in v for v in err.violations)


def test_parse_draft_response_duplicate_unique_within_column_raises() -> None:
    """Synthetic — two ``unique`` tests on the same column trip the rule."""
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(
            CandidateColumn(
                name="order_id",
                description="pk",
                tests=(
                    CandidateTestUnique(column="order_id"),
                    CandidateTestUnique(column="order_id"),
                ),
            ),
        ),
    )
    raw = candidate.model_dump_json()
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(raw, frozenset({"order_id"}), llm_result_meta=_meta())
    err = excinfo.value
    assert any("duplicate" in v and "unique" in v for v in err.violations)


# ---------------------------------------------------------------------------
# Envelope + excerpt
# ---------------------------------------------------------------------------


def test_parse_draft_response_envelope_carries_prompt_version_and_model_on_error() -> None:
    raw = _read("llm_response_truncated.json")
    meta = _meta(
        prompt_version="abc123def4567890",
        model="claude-haiku-4-5-20251001",
        cache_hit=True,
        input_tokens=1234,
        output_tokens=567,
    )
    with pytest.raises(LLMOutputJSONError) as excinfo:
        parse_draft_response(raw, _FCT_ORDERS_COLUMNS, llm_result_meta=meta)
    err = excinfo.value
    assert err.prompt_version == "abc123def4567890"
    assert err.model == "claude-haiku-4-5-20251001"
    assert err.cache_hit is True
    assert err.input_tokens == 1234
    assert err.output_tokens == 567


def test_parse_draft_response_excerpt_marks_offending_position() -> None:
    raw = _read("llm_response_truncated.json")
    with pytest.raises(LLMOutputJSONError) as excinfo:
        parse_draft_response(raw, _FCT_ORDERS_COLUMNS, llm_result_meta=_meta())
    err = excinfo.value
    assert err.excerpt
    # US-007 injects a ⟨HERE⟩ sentinel at the offending offset.
    assert "⟨HERE⟩" in err.excerpt


# ---------------------------------------------------------------------------
# Misc — relationships test does NOT trip the duplicate rule
# ---------------------------------------------------------------------------


def test_parse_draft_response_multiple_relationships_tests_on_same_column_allowed() -> None:
    """Two ``relationships`` tests on the same column are fine — they may
    cite different ``(to, field)`` pairs. The duplicate rule is
    deliberately scoped to parameterless tests only.
    """
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(
            CandidateColumn(
                name="customer_id",
                description="fk",
                tests=(
                    CandidateTestRelationships(
                        column="customer_id",
                        to="ref('dim_customers')",
                        field="customer_id",
                    ),
                    CandidateTestRelationships(
                        column="customer_id",
                        to="ref('dim_customers_v2')",
                        field="customer_id",
                    ),
                ),
            ),
        ),
    )
    raw = candidate.model_dump_json()
    parse_draft_response(raw, frozenset({"customer_id"}), llm_result_meta=_meta())


def test_parse_draft_response_hallucinated_candidate_column_name_raises() -> None:
    """Quality-Gate fix (Issue 1): a CandidateColumn whose ``name`` is not
    in ``model_columns`` is itself a violation, regardless of any tests it
    carries. Without this check the LLM could invent a column name + tests
    on it and pass anchor validation.
    """
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(
            CandidateColumn(
                name="hallucinated_col",
                description="LLM made this up",
                tests=(CandidateTestNotNull(column="hallucinated_col"),),
            ),
        ),
    )
    raw = candidate.model_dump_json()
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(raw, frozenset({"order_id"}), llm_result_meta=_meta())
    assert any(
        "CandidateColumn references nonexistent column 'hallucinated_col'" in v
        for v in excinfo.value.violations
    )
