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
    CandidateTestCustomSQL,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestUnique,
)
from signalforge.draft.parser import (
    _check_custom_sql_type_coherence,  # noqa: PLC2701  # private helper under test (#159 coverage)
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


def test_parse_draft_response_tolerates_prose_preamble() -> None:
    """Issue #144: claude-sonnet-4-6 narrates before the `{` on the
    business-rules path and the model rejects an assistant prefill, so the
    parser must strip the preamble and still parse the embedded JSON."""
    raw = (
        "I need to analyze the business rules carefully. The first rule is a "
        "tautology, so I'll only propose tests that add signal.\n\n"
    ) + _read("llm_response_valid.json")
    result = parse_draft_response(raw, _FCT_ORDERS_COLUMNS, llm_result_meta=_meta())
    assert isinstance(result, CandidateSchema)
    assert result.name == "fct_orders"
    assert {c.name for c in result.columns} == _FCT_ORDERS_COLUMNS


def test_parse_draft_response_pure_prose_still_raises_json_error() -> None:
    """A response with no JSON object at all still fails loud (the preamble
    tolerance must not mask a genuinely empty/garbage response)."""
    raw = "I cannot help with that request."
    with pytest.raises(LLMOutputJSONError):
        parse_draft_response(raw, _FCT_ORDERS_COLUMNS, llm_result_meta=_meta())


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


# ---------------------------------------------------------------------------
# custom_sql variant — anchor-contract handling (US-004, DEC-002)
# ---------------------------------------------------------------------------


def test_parse_custom_sql_column_scoped_valid() -> None:
    """A column-scoped custom_sql whose declared column exists passes."""
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(
            CandidateColumn(
                name="amount",
                description="order amount",
                tests=(
                    CandidateTestCustomSQL(
                        sql="select * from {{ this }} where amount < 0",
                        column="amount",
                    ),
                ),
            ),
        ),
    )
    raw = candidate.model_dump_json()
    result = parse_draft_response(raw, frozenset({"amount"}), llm_result_meta=_meta())
    assert isinstance(result, CandidateSchema)


def test_parse_custom_sql_model_level_column_none_valid() -> None:
    """A model-level custom_sql (column=None) is a business-rule assertion
    with no column checks — it passes."""
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(CandidateColumn(name="order_id", description="pk", tests=()),),
        tests=(
            CandidateTestCustomSQL(
                sql="select * from {{ this }} where amount > total",
                column=None,
            ),
        ),
    )
    raw = candidate.model_dump_json()
    result = parse_draft_response(raw, frozenset({"order_id"}), llm_result_meta=_meta())
    assert isinstance(result, CandidateSchema)


def test_parse_custom_sql_references_other_columns_not_rejected() -> None:
    """A column-scoped custom_sql is EXEMPT from the parent-column rule:
    its SQL may reference columns other than the one it is filed under, and
    its declared ``column`` only needs to be a real column (membership),
    not equal to the SQL's referenced columns."""
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(
            CandidateColumn(
                name="amount",
                description="order amount",
                tests=(
                    # SQL references `discount` and `total`, but the test is
                    # filed under `amount`. This must NOT be a violation.
                    CandidateTestCustomSQL(
                        sql="select * from {{ this }} where amount - discount > total",
                        column="amount",
                    ),
                ),
            ),
        ),
    )
    raw = candidate.model_dump_json()
    result = parse_draft_response(
        raw, frozenset({"amount", "discount", "total"}), llm_result_meta=_meta()
    )
    assert isinstance(result, CandidateSchema)


def test_parse_custom_sql_empty_sql_raises() -> None:
    """A custom_sql whose SQL is whitespace-only (passes the model's
    truthiness validator but is structurally empty) is a violation."""
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(
            CandidateColumn(
                name="amount",
                description="order amount",
                tests=(CandidateTestCustomSQL(sql="   \n\t  ", column="amount"),),
            ),
        ),
    )
    raw = candidate.model_dump_json()
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(raw, frozenset({"amount"}), llm_result_meta=_meta())
    assert any("empty sql" in v for v in excinfo.value.violations)


def test_parse_custom_sql_model_level_empty_sql_raises() -> None:
    """A model-level custom_sql with whitespace-only SQL is a violation."""
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(CandidateColumn(name="order_id", description="pk", tests=()),),
        tests=(CandidateTestCustomSQL(sql="  ", column=None),),
    )
    raw = candidate.model_dump_json()
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(raw, frozenset({"order_id"}), llm_result_meta=_meta())
    assert any("model-level custom_sql" in v and "empty sql" in v for v in excinfo.value.violations)


def test_parse_custom_sql_unknown_declared_column_raises() -> None:
    """A column-scoped custom_sql whose declared ``column`` is not a real
    model column is a violation (membership check still applies)."""
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(
            CandidateColumn(
                name="amount",
                description="order amount",
                tests=(
                    CandidateTestCustomSQL(
                        sql="select * from {{ this }} where x < 0",
                        column="phantom_col",
                    ),
                ),
            ),
        ),
    )
    raw = candidate.model_dump_json()
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(raw, frozenset({"amount"}), llm_result_meta=_meta())
    assert any(
        "custom_sql test references nonexistent column 'phantom_col'" in v
        for v in excinfo.value.violations
    )


def test_parse_custom_sql_model_level_unknown_declared_column_raises() -> None:
    """A model-level custom_sql with a non-None declared column that isn't on
    the model is a violation."""
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(CandidateColumn(name="order_id", description="pk", tests=()),),
        tests=(
            CandidateTestCustomSQL(
                sql="select * from {{ this }} where x < 0",
                column="phantom_col",
            ),
        ),
    )
    raw = candidate.model_dump_json()
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(raw, frozenset({"order_id"}), llm_result_meta=_meta())
    assert any(
        "model-level custom_sql test references nonexistent column 'phantom_col'" in v
        for v in excinfo.value.violations
    )


def test_parse_custom_sql_excluded_type_rejected() -> None:
    """When ``custom_sql`` is in ``exclude_tests`` the parser rejects a
    custom_sql candidate (defence-in-depth backstop)."""
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(
            CandidateColumn(
                name="amount",
                description="order amount",
                tests=(
                    CandidateTestCustomSQL(
                        sql="select * from {{ this }} where amount < 0",
                        column="amount",
                    ),
                ),
            ),
        ),
    )
    raw = candidate.model_dump_json()
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(
            raw,
            frozenset({"amount"}),
            llm_result_meta=_meta(),
            exclude_tests=frozenset({"custom_sql"}),
        )
    assert any("'custom_sql'" in v and "exclude_tests" in v for v in excinfo.value.violations)


def test_parse_custom_sql_model_level_excluded_type_rejected() -> None:
    """A model-level custom_sql is also rejected when ``custom_sql`` is
    excluded."""
    candidate = CandidateSchema(
        name="fct_orders",
        description="...",
        columns=(CandidateColumn(name="order_id", description="pk", tests=()),),
        tests=(CandidateTestCustomSQL(sql="select 1", column=None),),
    )
    raw = candidate.model_dump_json()
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(
            raw,
            frozenset({"order_id"}),
            llm_result_meta=_meta(),
            exclude_tests=frozenset({"custom_sql"}),
        )
    assert any(
        "model-level" in v and "'custom_sql'" in v and "exclude_tests" in v
        for v in excinfo.value.violations
    )


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


# ---------------------------------------------------------------------------
# Issue #159 — sqlglot type-coherence defence for custom_sql (DEC-003)
# ---------------------------------------------------------------------------


def _types_map(**kwargs: str | None) -> dict[str, str | None]:
    """Build a column-name → BigQuery data_type map for the type defence."""
    return dict(kwargs)


def _custom_sql_candidate(
    *,
    column_names: tuple[str, ...],
    sql: str,
    test_column: str | None = None,
    model_level: bool = False,
) -> CandidateSchema:
    """Build a synthetic CandidateSchema carrying one custom_sql test."""
    custom = CandidateTestCustomSQL(sql=sql, column=test_column)
    if model_level:
        return CandidateSchema(
            name="fct_test",
            description="...",
            columns=tuple(
                CandidateColumn(name=n, description="...", tests=()) for n in column_names
            ),
            tests=(custom,),
        )
    # Column-scoped: file under first declared column (arbitrary parent).
    parent = test_column or column_names[0]
    return CandidateSchema(
        name="fct_test",
        description="...",
        columns=tuple(
            CandidateColumn(
                name=n,
                description="...",
                tests=(custom,) if n == parent else (),
            )
            for n in column_names
        ),
    )


# --- planted positives (MUST add a violation) ---


def test_custom_sql_int64_vs_string_comparison_is_rejected() -> None:
    """A direct INT64 <> STRING comparison is the canonical mismatch the
    parser defence exists to catch (#159 / DEC-003)."""
    candidate = _custom_sql_candidate(
        column_names=("int_col", "str_col"),
        sql="select * from {{ this }} where int_col <> str_col",
        test_column="int_col",
    )
    raw = candidate.model_dump_json()
    types = _types_map(int_col="INT64", str_col="STRING")
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(
            raw,
            frozenset({"int_col", "str_col"}),
            llm_result_meta=_meta(),
            model_columns_by_type=types,
        )
    assert any(
        "int_col" in v and "str_col" in v and "incompatible" in v for v in excinfo.value.violations
    )


def test_custom_sql_int64_vs_string_equality_is_rejected() -> None:
    """An equality comparison across incompatible types is rejected just
    like the inequality form."""
    candidate = _custom_sql_candidate(
        column_names=("int_col", "str_col"),
        sql="select * from {{ this }} where int_col = str_col",
        test_column="int_col",
    )
    raw = candidate.model_dump_json()
    types = _types_map(int_col="INT64", str_col="STRING")
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(
            raw,
            frozenset({"int_col", "str_col"}),
            llm_result_meta=_meta(),
            model_columns_by_type=types,
        )
    assert any(
        "int_col" in v and "str_col" in v and "incompatible" in v for v in excinfo.value.violations
    )


def test_custom_sql_int64_vs_date_comparison_is_rejected() -> None:
    """INT64 vs DATE is incompatible; the comparison is flagged."""
    candidate = _custom_sql_candidate(
        column_names=("i", "d"),
        sql="select * from {{ this }} where i > d",
        test_column="i",
    )
    raw = candidate.model_dump_json()
    types = _types_map(i="INT64", d="DATE")
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(
            raw,
            frozenset({"i", "d"}),
            llm_result_meta=_meta(),
            model_columns_by_type=types,
        )
    assert any("'i'" in v and "'d'" in v and "incompatible" in v for v in excinfo.value.violations)


# --- planted negatives (MUST NOT add a violation) ---


def test_custom_sql_int64_vs_float64_accepted_numeric_coercion() -> None:
    """BigQuery accepts implicit INT64 ↔ FLOAT64 coercion; do NOT flag it."""
    candidate = _custom_sql_candidate(
        column_names=("i", "f"),
        sql="select * from {{ this }} where i <> f",
        test_column="i",
    )
    raw = candidate.model_dump_json()
    types = _types_map(i="INT64", f="FLOAT64")
    # No raise — accepted numeric coercion.
    result = parse_draft_response(
        raw,
        frozenset({"i", "f"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_custom_sql_numeric_vs_bignumeric_accepted() -> None:
    """NUMERIC ↔ BIGNUMERIC is a legitimate same-family coercion."""
    candidate = _custom_sql_candidate(
        column_names=("n", "bn"),
        sql="select * from {{ this }} where n <> bn",
        test_column="n",
    )
    raw = candidate.model_dump_json()
    types = _types_map(n="NUMERIC", bn="BIGNUMERIC")
    result = parse_draft_response(
        raw,
        frozenset({"n", "bn"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_custom_sql_cast_around_string_skipped() -> None:
    """A CAST coerces explicitly; the parser defence does NOT second-guess
    an explicit cast (DEC-006 skip-when-uncertain)."""
    candidate = _custom_sql_candidate(
        column_names=("int_col", "str_col"),
        sql="select * from {{ this }} where CAST(int_col AS STRING) <> str_col",
        test_column="int_col",
    )
    raw = candidate.model_dump_json()
    types = _types_map(int_col="INT64", str_col="STRING")
    result = parse_draft_response(
        raw,
        frozenset({"int_col", "str_col"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_custom_sql_coalesce_skipped() -> None:
    """A COALESCE wraps the column in a function call; skip per DEC-006."""
    candidate = _custom_sql_candidate(
        column_names=("int_col", "str_col"),
        sql="select * from {{ this }} where COALESCE(int_col, 0) <> str_col",
        test_column="int_col",
    )
    raw = candidate.model_dump_json()
    types = _types_map(int_col="INT64", str_col="STRING")
    result = parse_draft_response(
        raw,
        frozenset({"int_col", "str_col"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_custom_sql_safe_cast_skipped() -> None:
    """BigQuery SAFE_CAST is an explicit coercion shape; skip per DEC-006."""
    candidate = _custom_sql_candidate(
        column_names=("int_col", "str_col"),
        sql="select * from {{ this }} where SAFE_CAST(int_col AS STRING) <> str_col",
        test_column="int_col",
    )
    raw = candidate.model_dump_json()
    types = _types_map(int_col="INT64", str_col="STRING")
    result = parse_draft_response(
        raw,
        frozenset({"int_col", "str_col"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_custom_sql_null_comparison_skipped() -> None:
    """``IS NOT NULL`` is not a binary Column<op>Column comparison; skip."""
    candidate = _custom_sql_candidate(
        column_names=("int_col",),
        sql="select * from {{ this }} where int_col IS NOT NULL",
        test_column="int_col",
    )
    raw = candidate.model_dump_json()
    types = _types_map(int_col="INT64")
    result = parse_draft_response(
        raw,
        frozenset({"int_col"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_custom_sql_literal_compare_skipped() -> None:
    """A column-vs-literal comparison has a non-Column right side; skip."""
    candidate = _custom_sql_candidate(
        column_names=("int_col",),
        sql="select * from {{ this }} where int_col <> 0",
        test_column="int_col",
    )
    raw = candidate.model_dump_json()
    types = _types_map(int_col="INT64")
    result = parse_draft_response(
        raw,
        frozenset({"int_col"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_custom_sql_function_call_skipped() -> None:
    """LENGTH(str_col) yields a non-Column left side; skip per DEC-006."""
    candidate = _custom_sql_candidate(
        column_names=("str_col",),
        sql="select * from {{ this }} where LENGTH(str_col) > 0",
        test_column="str_col",
    )
    raw = candidate.model_dump_json()
    types = _types_map(str_col="STRING")
    result = parse_draft_response(
        raw,
        frozenset({"str_col"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_custom_sql_subquery_skipped() -> None:
    """A subquery on the right side is not a bare Column; skip."""
    candidate = _custom_sql_candidate(
        column_names=("col",),
        sql="select * from {{ this }} where col IN (select id from other)",
        test_column="col",
    )
    raw = candidate.model_dump_json()
    types = _types_map(col="INT64")
    result = parse_draft_response(
        raw,
        frozenset({"col"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


# --- robustness ---


def test_custom_sql_unparseable_sql_is_silent() -> None:
    """sqlglot ParseError must NOT raise out of the defence; the existing
    structural checks still run (and pass for this candidate)."""
    candidate = _custom_sql_candidate(
        column_names=("int_col",),
        sql="this is @@@ not {{{{ valid sql }}}}",
        test_column="int_col",
    )
    raw = candidate.model_dump_json()
    types = _types_map(int_col="INT64")
    # The SQL body is non-empty; structural checks pass; type defence skips.
    result = parse_draft_response(
        raw,
        frozenset({"int_col"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_custom_sql_unknown_column_type_skipped() -> None:
    """Both columns have data_type=None → the type-coherence arm is a
    whole-map no-op (the catalog merge in US-001 hasn't filled types)."""
    candidate = _custom_sql_candidate(
        column_names=("a", "b"),
        sql="select * from {{ this }} where a <> b",
        test_column="a",
    )
    raw = candidate.model_dump_json()
    types = _types_map(a=None, b=None)
    result = parse_draft_response(
        raw,
        frozenset({"a", "b"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_custom_sql_partial_unknown_skipped() -> None:
    """Only one column has a known type — per-pair skip preserves
    conservative-bias (we can't reject what we can't compare)."""
    candidate = _custom_sql_candidate(
        column_names=("a", "b"),
        sql="select * from {{ this }} where a <> b",
        test_column="a",
    )
    raw = candidate.model_dump_json()
    types = _types_map(a="INT64", b=None)
    result = parse_draft_response(
        raw,
        frozenset({"a", "b"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_custom_sql_model_columns_by_type_none_skips_arm_entirely() -> None:
    """Passing model_columns_by_type=None skips the type-coherence arm
    while leaving structural anchor checks running."""
    candidate = _custom_sql_candidate(
        column_names=("int_col", "str_col"),
        sql="select * from {{ this }} where int_col <> str_col",
        test_column="int_col",
    )
    raw = candidate.model_dump_json()
    # No types passed — type defence is a no-op even though this SQL is
    # type-incoherent. The structural checks pass.
    result = parse_draft_response(
        raw,
        frozenset({"int_col", "str_col"}),
        llm_result_meta=_meta(),
        model_columns_by_type=None,
    )
    assert isinstance(result, CandidateSchema)


def test_validate_anchor_contract_collects_type_and_structural_violations() -> None:
    """Whole-draft collect-all invariant: a candidate carrying BOTH a
    structural violation (hallucinated column) AND a type-coherence
    violation produces BOTH in one ``LLMOutputAnchorContractError``.
    """
    candidate = CandidateSchema(
        name="fct_test",
        description="...",
        columns=(
            CandidateColumn(
                name="int_col",
                description="...",
                tests=(
                    CandidateTestCustomSQL(
                        sql="select * from {{ this }} where int_col <> str_col",
                        column="int_col",
                    ),
                ),
            ),
            CandidateColumn(name="str_col", description="...", tests=()),
            # Structural violation: hallucinated CandidateColumn name.
            CandidateColumn(name="phantom_col", description="...", tests=()),
        ),
    )
    raw = candidate.model_dump_json()
    types = _types_map(int_col="INT64", str_col="STRING")
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(
            raw,
            frozenset({"int_col", "str_col"}),
            llm_result_meta=_meta(),
            model_columns_by_type=types,
        )
    # Both surfaces present.
    assert any("phantom_col" in v for v in excinfo.value.violations)
    assert any(
        "int_col" in v and "str_col" in v and "incompatible" in v for v in excinfo.value.violations
    )


# ---------------------------------------------------------------------------
# Coverage gates for #159 (US-005 codecov follow-up).
#
# These tests pin the defensive branches inside
# `_check_custom_sql_type_coherence` (lines 121, 126, 168-171, 174, 184-187,
# 209-210 in parser.py at #159 baseline) and the model-level call site at
# lines 353-354. Each branch is either a "skip-when-uncertain" silent
# degrade required by DEC-006 or a fail-soft catch around sqlglot internals;
# regression tests pin the behaviour even though the existing planted-
# positive/negative tests don't exercise these specific code paths
# organically.
# ---------------------------------------------------------------------------


def test_custom_sql_same_type_comparison_skipped() -> None:
    """INT64 vs INT64 routes through the `a == b` short-circuit in
    `_types_compatible` (parser.py line 121). Same-type comparisons are
    legitimate SQL; the defence must not flag them."""
    candidate = _custom_sql_candidate(
        column_names=("a", "b"),
        sql="select * from {{ this }} where a <> b",
        test_column="a",
    )
    raw = candidate.model_dump_json()
    types = _types_map(a="INT64", b="INT64")
    result = parse_draft_response(
        raw,
        frozenset({"a", "b"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_custom_sql_reverse_coerce_direction_accepted() -> None:
    """FLOAT64 vs INT64 (swap of the canonical INT64 vs FLOAT64 case) hits
    the reverse-direction `a in coerces_to.get(b)` branch in
    `_types_compatible` (parser.py line 126). Order of operands must not
    change the accept decision for cross-numeric coercion."""
    candidate = _custom_sql_candidate(
        column_names=("f", "i"),
        sql="select * from {{ this }} where f <> i",
        test_column="f",
    )
    raw = candidate.model_dump_json()
    types = _types_map(f="FLOAT64", i="INT64")
    result = parse_draft_response(
        raw,
        frozenset({"f", "i"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_check_custom_sql_type_coherence_parser_returns_none_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sqlglot.parse_one` documents that it can return None for some
    pathological inputs (empty / comment-only / whitespace, depending on
    version). The helper's `if parsed is None: return ()` guard at
    parser.py line 174 must skip the defence cleanly in that case.

    Monkeypatched because the input that triggers `parsed is None` varies
    across sqlglot releases (current sqlglot raises ParseError on the
    whitespace input that older releases returned None for); pinning the
    contract via monkeypatch keeps the test stable across upgrades."""
    from signalforge.draft import parser as parser_mod

    def returns_none(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(parser_mod.sqlglot, "parse_one", returns_none)
    result = _check_custom_sql_type_coherence(
        "select * from t where a <> b",
        model_columns_by_type={"a": "INT64", "b": "STRING"},
        dialect_name="bigquery",
    )
    assert result == ()


def test_custom_sql_invalid_type_string_skipped() -> None:
    """An opaque/invalid type string (`DataType.build` raises) hits the
    broad-except at parser.py lines 209-210. Conservative-bias: skip
    silently — the data_type came from the manifest, and rejecting on a
    vendor-specific type string would block legitimate drafts on adapters
    we don't yet model."""
    candidate = _custom_sql_candidate(
        column_names=("x", "y"),
        sql="select * from {{ this }} where x <> y",
        test_column="x",
    )
    raw = candidate.model_dump_json()
    # ``"DEFINITELY_NOT_A_SQL_TYPE"`` exercises ``DataType.build`` raising.
    types = _types_map(x="DEFINITELY_NOT_A_SQL_TYPE", y="INT64")
    result = parse_draft_response(
        raw,
        frozenset({"x", "y"}),
        llm_result_meta=_meta(),
        model_columns_by_type=types,
    )
    assert isinstance(result, CandidateSchema)


def test_model_level_custom_sql_type_mismatch_is_rejected() -> None:
    """Model-level custom_sql (column=None) must also flow through the
    type-coherence arm (parser.py lines 353-354). Without this, a
    drafted model-level business-rule that compares mismatched types
    would silently slip past the parser and hit warehouse rejection
    later. Mirrors the column-scoped INT64 vs STRING positive."""
    candidate = _custom_sql_candidate(
        column_names=("int_col", "str_col"),
        sql="select * from {{ this }} where int_col <> str_col",
        test_column=None,
        model_level=True,
    )
    raw = candidate.model_dump_json()
    types = _types_map(int_col="INT64", str_col="STRING")
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(
            raw,
            frozenset({"int_col", "str_col"}),
            llm_result_meta=_meta(),
            model_columns_by_type=types,
        )
    assert any(
        "int_col" in v and "str_col" in v and "incompatible" in v for v in excinfo.value.violations
    )


def test_check_custom_sql_type_coherence_sqlglot_non_parse_error_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sqlglot.parse_one` can raise a non-ParseError SqlglotError
    (e.g. tokeniser errors on certain pathological inputs). The defence
    is belt-and-braces; the broad sibling `except sqlglot.errors.SqlglotError`
    at parser.py lines 168-171 must skip silently so we never block a
    candidate on a parser-internals bug."""
    import sqlglot
    import sqlglot.errors

    from signalforge.draft import parser as parser_mod

    def boom(*_args: object, **_kwargs: object) -> object:
        raise sqlglot.errors.SqlglotError("synthetic non-parse sqlglot failure")

    monkeypatch.setattr(parser_mod.sqlglot, "parse_one", boom)
    result = _check_custom_sql_type_coherence(
        "select * from t where a <> b",
        model_columns_by_type={"a": "INT64", "b": "STRING"},
        dialect_name="bigquery",
    )
    # The defensive `except sqlglot.errors.SqlglotError` clause must have
    # swallowed the synthetic non-ParseError raise and returned an empty
    # tuple — never re-raise sqlglot internals out of the defence.
    assert result == ()


def test_check_custom_sql_type_coherence_annotate_types_failure_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`annotate_types` is a third-party optimizer pass with a wide
    exception surface; a future sqlglot release could regress and raise
    on a corner-case SQL we draft. The fail-soft `except Exception`
    catch at parser.py lines 184-187 keeps the defence belt-and-braces.
    Pinned via monkeypatch — the real annotator handles current inputs."""
    from signalforge.draft import parser as parser_mod

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic annotate_types regression")

    monkeypatch.setattr(parser_mod, "annotate_types", boom)
    result = _check_custom_sql_type_coherence(
        "select * from t where a <> b",
        model_columns_by_type={"a": "INT64", "b": "STRING"},
        dialect_name="bigquery",
    )
    assert result == ()


# ---------------------------------------------------------------------------
# Issue #163 US-002 — parser cardinality gate (business_rules → custom_sql)
# ---------------------------------------------------------------------------


def _candidate_with_custom_sql_counts(
    *,
    column_names: tuple[str, ...] = ("amount",),
    model_level_count: int = 0,
    column_level_count: int = 0,
) -> CandidateSchema:
    """Build a synthetic CandidateSchema with the requested number of
    ``custom_sql`` tests split between model-level (``candidate.tests``)
    and column-level (``columns[0].tests``).
    """
    parent = column_names[0]
    column_tests: tuple[CandidateTestCustomSQL, ...] = tuple(
        CandidateTestCustomSQL(
            sql=f"select * from {{{{ this }}}} where {parent} = {i}",
            column=parent,
        )
        for i in range(column_level_count)
    )
    model_tests: tuple[CandidateTestCustomSQL, ...] = tuple(
        CandidateTestCustomSQL(
            sql=f"select * from {{{{ this }}}} where {parent} = {i}",
            column=None,
        )
        for i in range(model_level_count)
    )
    return CandidateSchema(
        name="fct_test",
        description="...",
        columns=tuple(
            CandidateColumn(
                name=n,
                description="...",
                tests=column_tests if n == parent else (),
            )
            for n in column_names
        ),
        tests=model_tests,
    )


def test_cardinality_gate_rejects_under_coverage() -> None:
    """Two declared business rules + one custom_sql test → violation."""
    candidate = _candidate_with_custom_sql_counts(column_level_count=1)
    raw = candidate.model_dump_json()
    rules = (
        "(model) every trip must start and end at the same station",
        "(column amount) amount must be non-negative",
    )
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(
            raw,
            frozenset({"amount"}),
            llm_result_meta=_meta(),
            business_rules=rules,
        )
    assert any(
        "Expected" in v and "custom_sql" in v and "got 1" in v for v in excinfo.value.violations
    )


def test_cardinality_gate_accepts_coverage_match() -> None:
    """Two declared business rules + two custom_sql tests → accept."""
    candidate = _candidate_with_custom_sql_counts(column_level_count=2)
    raw = candidate.model_dump_json()
    rules = (
        "(model) rule one",
        "(column amount) rule two",
    )
    result = parse_draft_response(
        raw,
        frozenset({"amount"}),
        llm_result_meta=_meta(),
        business_rules=rules,
    )
    assert isinstance(result, CandidateSchema)


def test_cardinality_gate_accepts_over_coverage() -> None:
    """Two declared business rules + three custom_sql tests → accept
    (excess is legitimate multi-test decomposition; DEC-002)."""
    candidate = _candidate_with_custom_sql_counts(column_level_count=3)
    raw = candidate.model_dump_json()
    rules = (
        "(model) rule one",
        "(column amount) rule two",
    )
    result = parse_draft_response(
        raw,
        frozenset({"amount"}),
        llm_result_meta=_meta(),
        business_rules=rules,
    )
    assert isinstance(result, CandidateSchema)


def test_cardinality_gate_noop_when_no_rules_zero_custom_sql() -> None:
    """Empty business_rules + zero custom_sql tests → accept
    (preserves the inferred-fallback path's silent-no-rules behaviour)."""
    candidate = _candidate_with_custom_sql_counts(column_level_count=0)
    raw = candidate.model_dump_json()
    result = parse_draft_response(
        raw,
        frozenset({"amount"}),
        llm_result_meta=_meta(),
        business_rules=(),
    )
    assert isinstance(result, CandidateSchema)


def test_cardinality_gate_noop_when_no_rules_with_custom_sql() -> None:
    """Empty business_rules + custom_sql tests present → accept
    (preserves the inferred-fallback path where the LLM volunteers
    custom_sql tests without operator-declared rules)."""
    candidate = _candidate_with_custom_sql_counts(column_level_count=2)
    raw = candidate.model_dump_json()
    result = parse_draft_response(
        raw,
        frozenset({"amount"}),
        llm_result_meta=_meta(),
        business_rules=(),
    )
    assert isinstance(result, CandidateSchema)


def test_cardinality_gate_noop_when_custom_sql_excluded() -> None:
    """``exclude_tests=("custom_sql",)`` + two business rules + zero
    custom_sql → accept the cardinality side (DEC-008). The operator
    forbade custom_sql; the rules just aren't going to be enforced."""
    # NB: candidate carries no custom_sql at all, so the exclude_tests
    # backstop on per-test rejection has nothing to fire on either.
    candidate = CandidateSchema(
        name="fct_test",
        description="...",
        columns=(CandidateColumn(name="amount", description="...", tests=()),),
    )
    raw = candidate.model_dump_json()
    rules = (
        "(model) rule one",
        "(column amount) rule two",
    )
    result = parse_draft_response(
        raw,
        frozenset({"amount"}),
        llm_result_meta=_meta(),
        exclude_tests=frozenset({"custom_sql"}),
        business_rules=rules,
    )
    assert isinstance(result, CandidateSchema)


def test_cardinality_gate_counts_model_level_custom_sql() -> None:
    """One declared rule + one model-level custom_sql (on candidate.tests)
    → accept (model-level tests count toward the cardinality total)."""
    candidate = _candidate_with_custom_sql_counts(model_level_count=1)
    raw = candidate.model_dump_json()
    rules = ("(model) rule one",)
    result = parse_draft_response(
        raw,
        frozenset({"amount"}),
        llm_result_meta=_meta(),
        business_rules=rules,
    )
    assert isinstance(result, CandidateSchema)


def test_cardinality_gate_counts_column_level_custom_sql() -> None:
    """One declared rule + one column-level custom_sql → accept
    (column-level tests count toward the cardinality total)."""
    candidate = _candidate_with_custom_sql_counts(column_level_count=1)
    raw = candidate.model_dump_json()
    rules = ("(column amount) rule one",)
    result = parse_draft_response(
        raw,
        frozenset({"amount"}),
        llm_result_meta=_meta(),
        business_rules=rules,
    )
    assert isinstance(result, CandidateSchema)


def test_cardinality_gate_counts_mixed_model_and_column_custom_sql() -> None:
    """Two declared rules + one model-level + one column-level → accept
    (mixed counts sum across both scopes)."""
    candidate = _candidate_with_custom_sql_counts(model_level_count=1, column_level_count=1)
    raw = candidate.model_dump_json()
    rules = (
        "(model) rule one",
        "(column amount) rule two",
    )
    result = parse_draft_response(
        raw,
        frozenset({"amount"}),
        llm_result_meta=_meta(),
        business_rules=rules,
    )
    assert isinstance(result, CandidateSchema)


def test_cardinality_violation_message_includes_all_declared_rules() -> None:
    """The violation message pins (DEC-006): names every declared rule
    verbatim via ``repr()``, reports actual and minimum count."""
    candidate = _candidate_with_custom_sql_counts(column_level_count=0)
    raw = candidate.model_dump_json()
    rules = (
        "(model) every trip must start and end at the same station",
        "(column amount) amount must be non-negative",
    )
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(
            raw,
            frozenset({"amount"}),
            llm_result_meta=_meta(),
            business_rules=rules,
        )
    matching = [
        v
        for v in excinfo.value.violations
        if "Expected" in v and "custom_sql" in v and "got 0" in v
    ]
    assert len(matching) == 1
    msg = matching[0]
    # Pinned shape: count, both rules quoted via repr, and the "Declared rules:" prefix.
    assert "Expected ≥2 custom_sql test(s)" in msg
    assert "got 0" in msg
    assert "Declared rules:" in msg
    assert repr(rules[0]) in msg
    assert repr(rules[1]) in msg


def test_cardinality_gate_collect_all_with_other_violations() -> None:
    """A candidate with a hallucinated column AND a cardinality miss must
    produce BOTH violations in one ``LLMOutputAnchorContractError`` —
    the cardinality gate appends, never short-circuits."""
    candidate = CandidateSchema(
        name="fct_test",
        description="...",
        columns=(
            CandidateColumn(
                name="hallucinated",
                description="LLM made this up",
                tests=(),
            ),
        ),
    )
    raw = candidate.model_dump_json()
    rules = ("(model) rule one",)
    with pytest.raises(LLMOutputAnchorContractError) as excinfo:
        parse_draft_response(
            raw,
            frozenset({"order_id"}),
            llm_result_meta=_meta(),
            business_rules=rules,
        )
    violations = excinfo.value.violations
    assert any(
        "CandidateColumn references nonexistent column 'hallucinated'" in v for v in violations
    )
    assert any("Expected" in v and "custom_sql" in v and "got 0" in v for v in violations)
