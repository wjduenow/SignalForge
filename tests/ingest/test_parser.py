"""Test matrix for the dbt test-entry parser (US-003).

Each test pins exactly one entry shape: bare strings, single-key dicts with
inline and ``arguments:``-nested args, config-key tolerance, malformed
supported types, custom/namespaced tests, and the ``ref()`` / ``source()``
unwrap (DEC-009). No ``assert True``-shaped tests — every assertion can fail
on a real regression (``.claude/rules/testing-signal.md``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from signalforge.draft.models import (
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestRowCountBetween,
    CandidateTestUnique,
)
from signalforge.ingest.models import SkippedTest, SkipReason
from signalforge.ingest.parser import parse_test_entry

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "ingest"

# --- bare strings ---------------------------------------------------------


def test_bare_not_null_maps_to_not_null() -> None:
    result = parse_test_entry("not_null", column="id")
    assert isinstance(result, CandidateTestNotNull)
    assert result.column == "id"


def test_bare_unique_maps_to_unique() -> None:
    result = parse_test_entry("unique", column="id")
    assert isinstance(result, CandidateTestUnique)
    assert result.column == "id"


def test_bare_unsupported_string_skips_unsupported_type() -> None:
    result = parse_test_entry("some_custom_check", column="id")
    assert isinstance(result, SkippedTest)
    assert result.reason == "unsupported-test-type"
    assert result.test_name == "some_custom_check"
    assert result.column == "id"


# --- accepted_values ------------------------------------------------------


def test_accepted_values_inline() -> None:
    result = parse_test_entry({"accepted_values": {"values": ["a", "b", "c"]}}, column="status")
    assert isinstance(result, CandidateTestAcceptedValues)
    assert result.column == "status"
    assert result.values == ("a", "b", "c")


def test_accepted_values_under_arguments() -> None:
    result = parse_test_entry(
        {"accepted_values": {"arguments": {"values": ["x", "y"]}}}, column="kind"
    )
    assert isinstance(result, CandidateTestAcceptedValues)
    assert result.values == ("x", "y")


def test_accepted_values_missing_values_is_malformed() -> None:
    result = parse_test_entry({"accepted_values": {"severity": "warn"}}, column="kind")
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"
    assert result.test_name == "accepted_values"
    assert result.column == "kind"


def test_accepted_values_empty_values_is_malformed() -> None:
    result = parse_test_entry({"accepted_values": {"values": []}}, column="kind")
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"


# --- relationships --------------------------------------------------------


def test_relationships_inline() -> None:
    result = parse_test_entry(
        {"relationships": {"to": "ref('dim_customers')", "field": "id"}},
        column="customer_id",
    )
    assert isinstance(result, CandidateTestRelationships)
    assert result.column == "customer_id"
    assert result.to == "dim_customers"
    assert result.field == "id"


def test_relationships_under_arguments() -> None:
    result = parse_test_entry(
        {"relationships": {"arguments": {"to": "ref('orders')", "field": "order_id"}}},
        column="order_id",
    )
    assert isinstance(result, CandidateTestRelationships)
    assert result.to == "orders"
    assert result.field == "order_id"


def test_relationships_missing_field_is_malformed() -> None:
    result = parse_test_entry({"relationships": {"to": "ref('orders')"}}, column="order_id")
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"
    assert result.test_name == "relationships"


def test_relationships_missing_to_is_malformed() -> None:
    result = parse_test_entry({"relationships": {"field": "id"}}, column="order_id")
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"


# --- config-key tolerance -------------------------------------------------


def test_not_null_dict_with_config_keys_is_supported() -> None:
    result = parse_test_entry(
        {"not_null": {"config": {"where": "x is not null"}, "severity": "error"}},
        column="id",
    )
    assert isinstance(result, CandidateTestNotNull)
    assert result.column == "id"


def test_accepted_values_with_interleaved_config_keys() -> None:
    result = parse_test_entry(
        {
            "accepted_values": {
                "values": ["a"],
                "config": {"severity": "warn"},
                "where": "1=1",
                "tags": ["t"],
            }
        },
        column="status",
    )
    assert isinstance(result, CandidateTestAcceptedValues)
    assert result.values == ("a",)


# --- custom / namespaced --------------------------------------------------


def test_dbt_utils_namespaced_test_is_custom_skip() -> None:
    result = parse_test_entry(
        {"dbt_utils.unique_combination_of_columns": {"combination_of_columns": ["a", "b"]}},
        column=None,
    )
    assert isinstance(result, SkippedTest)
    assert result.reason == "custom-or-generic-test"
    assert result.test_name == "dbt_utils.unique_combination_of_columns"
    assert result.column is None


def test_dbt_expectations_namespaced_test_is_custom_skip() -> None:
    result = parse_test_entry(
        {"dbt_expectations.expect_column_values_to_be_in_set": {"value_set": [1, 2]}},
        column="n",
    )
    assert isinstance(result, SkippedTest)
    assert result.reason == "custom-or-generic-test"
    assert result.test_name == "dbt_expectations.expect_column_values_to_be_in_set"


# --- ref() / source() unwrap (DEC-009) ------------------------------------


def test_ref_single_arg_unwraps_to_model_name() -> None:
    result = parse_test_entry(
        {"relationships": {"to": "ref('my_model')", "field": "id"}}, column="fk"
    )
    assert isinstance(result, CandidateTestRelationships)
    assert result.to == "my_model"


def test_ref_two_args_unwraps_to_last_positional() -> None:
    result = parse_test_entry(
        {"relationships": {"to": 'ref("my_pkg", "my_model")', "field": "id"}},
        column="fk",
    )
    assert isinstance(result, CandidateTestRelationships)
    assert result.to == "my_model"


def test_source_unwraps_to_dotted_form() -> None:
    result = parse_test_entry(
        {"relationships": {"to": "source('raw', 'customers')", "field": "id"}},
        column="fk",
    )
    assert isinstance(result, CandidateTestRelationships)
    assert result.to == "raw.customers"


def test_bare_non_ref_to_string_carried_verbatim() -> None:
    result = parse_test_entry(
        {"relationships": {"to": "dim_customers", "field": "id"}}, column="fk"
    )
    assert isinstance(result, CandidateTestRelationships)
    assert result.to == "dim_customers"


# --- model-level supported tests are not representable (QG fix) ------------


def test_model_level_not_null_skips_malformed_not_validationerror() -> None:
    # A supported type at model level (column=None) must route to a structured
    # skip, NOT raise a Pydantic ValidationError out of the parser.
    result = parse_test_entry("not_null", column=None)
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"
    assert result.column is None
    assert result.test_name == "not_null"


def test_model_level_accepted_values_skips_malformed() -> None:
    result = parse_test_entry({"accepted_values": {"values": ["a", "b"]}}, column=None)
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"
    assert result.column is None


# --- non-mapping `arguments` is stripped, not leaked (PR review fix) -------


def test_inline_args_with_non_mapping_arguments_key_is_stripped() -> None:
    # `arguments: <non-dict>` must not leak into the inline args nor break the
    # required-arg extraction — the structural key is dropped.
    result = parse_test_entry(
        {"accepted_values": {"values": ["a", "b"], "arguments": 123}},
        column="status",
    )
    assert isinstance(result, CandidateTestAcceptedValues)
    assert result.values == ("a", "b")


# --- ref() / source() non-quoted + arity edge cases (DEC-009) -------------


def test_ref_with_unquoted_arg_is_carried_verbatim() -> None:
    # `ref(my_var)` matches the ref() shape but carries no QUOTED positional,
    # so the unwrap finds nothing and returns the string verbatim.
    result = parse_test_entry({"relationships": {"to": "ref(my_var)", "field": "id"}}, column="fk")
    assert isinstance(result, CandidateTestRelationships)
    assert result.to == "ref(my_var)"


def test_source_single_arg_unwraps_to_that_arg() -> None:
    # A one-positional `source('only')` returns that single arg (not dotted).
    result = parse_test_entry(
        {"relationships": {"to": "source('only')", "field": "id"}}, column="fk"
    )
    assert isinstance(result, CandidateTestRelationships)
    assert result.to == "only"


def test_source_with_unquoted_args_is_carried_verbatim() -> None:
    # `source(a, b)` matches the source() shape but carries no QUOTED args,
    # so neither the 2-arg nor 1-arg branch fires; returned verbatim.
    result = parse_test_entry({"relationships": {"to": "source(a, b)", "field": "id"}}, column="fk")
    assert isinstance(result, CandidateTestRelationships)
    assert result.to == "source(a, b)"


# --- non-dict bodies + non-conforming entry shapes ------------------------


def test_accepted_values_non_dict_body_is_malformed() -> None:
    # A non-dict body yields no extracted args, so the required `values` is
    # absent -> malformed-supported-test.
    result = parse_test_entry({"accepted_values": "not-a-mapping"}, column="status")
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"
    assert result.test_name == "accepted_values"


def test_multi_key_dict_entry_is_custom_skip() -> None:
    # A test entry is a single-key dict by dbt's grammar; a multi-key dict is
    # not a shape we model -> custom-or-generic-test, naming the first key.
    result = parse_test_entry({"foo": 1, "bar": 2}, column="id")
    assert isinstance(result, SkippedTest)
    assert result.reason == "custom-or-generic-test"
    assert result.test_name == "foo"
    assert result.column == "id"
    assert "single-key" in result.detail


def test_non_string_non_dict_entry_is_custom_skip() -> None:
    # An entry that is neither a string nor a mapping (e.g. an int) is skipped
    # and recorded, never silently dropped.
    result = parse_test_entry(123, column="id")  # type: ignore[arg-type]
    assert isinstance(result, SkippedTest)
    assert result.reason == "custom-or-generic-test"
    assert result.test_name == "123"
    assert result.column == "id"


def test_model_level_relationships_skips_malformed() -> None:
    # relationships at model level (column=None) is not representable -> skip.
    result = parse_test_entry(
        {"relationships": {"to": "ref('orders')", "field": "id"}}, column=None
    )
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"
    assert result.column is None
    assert result.test_name == "relationships"


# --- row_count_between (US-005 of #169) -----------------------------------

_RCB_NAME = "dbt_expectations.expect_table_row_count_to_be_between"


def test_row_count_between_inline_bounds_maps_to_variant() -> None:
    """Valid inline ``min_value`` + ``max_value`` map to the variant with the
    inbound name translation (DEC-008): ``min_value`` → ``minimum``,
    ``max_value`` → ``maximum``."""
    result = parse_test_entry({_RCB_NAME: {"min_value": 100, "max_value": 10000}}, column=None)
    assert isinstance(result, CandidateTestRowCountBetween)
    assert result.minimum == 100
    assert result.maximum == 10000
    assert result.where is None
    # The variant is model-level only.
    assert result.column is None


def test_row_count_between_with_where_carries_where_field() -> None:
    """``where`` is a first-class arg for this variant (NOT stripped as a
    generic test-config passthrough). The parser must carry it onto the
    variant verbatim."""
    result = parse_test_entry(
        {
            _RCB_NAME: {
                "min_value": 100,
                "max_value": 10000,
                "where": "event_date >= '2024-01-01'",
            }
        },
        column=None,
    )
    assert isinstance(result, CandidateTestRowCountBetween)
    assert result.where == "event_date >= '2024-01-01'"


def test_row_count_between_under_arguments_dbt_18() -> None:
    """The dbt 1.8+ ``arguments:`` nested shape is recognised (mirrors
    accepted_values / relationships precedent)."""
    result = parse_test_entry(
        {
            _RCB_NAME: {
                "arguments": {
                    "min_value": 1,
                    "max_value": 1_000_000,
                    "where": "ordered_at >= '2024-01-01'",
                }
            }
        },
        column=None,
    )
    assert isinstance(result, CandidateTestRowCountBetween)
    assert result.minimum == 1
    assert result.maximum == 1_000_000
    assert result.where == "ordered_at >= '2024-01-01'"


def test_row_count_between_only_min_value_is_valid() -> None:
    """At-least-one-bound suffices; ``max_value`` may be omitted."""
    result = parse_test_entry({_RCB_NAME: {"min_value": 1}}, column=None)
    assert isinstance(result, CandidateTestRowCountBetween)
    assert result.minimum == 1
    assert result.maximum is None


def test_row_count_between_only_max_value_is_valid() -> None:
    """At-least-one-bound suffices; ``min_value`` may be omitted."""
    result = parse_test_entry({_RCB_NAME: {"max_value": 1_000_000}}, column=None)
    assert isinstance(result, CandidateTestRowCountBetween)
    assert result.minimum is None
    assert result.maximum == 1_000_000


def test_row_count_between_missing_both_bounds_is_malformed() -> None:
    """Both bounds absent → malformed-supported-test (a no-bound test
    carries no signal; mirrors the model-level Pydantic invariant)."""
    result = parse_test_entry({_RCB_NAME: {"where": "1=1"}}, column=None)
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"
    assert result.test_name == _RCB_NAME
    assert result.column is None
    assert "min_value" in result.detail or "max_value" in result.detail


def test_row_count_between_min_greater_than_max_is_malformed() -> None:
    """``min_value > max_value`` is empty-range → malformed-supported-test."""
    result = parse_test_entry({_RCB_NAME: {"min_value": 5000, "max_value": 100}}, column=None)
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"
    assert "min_value" in result.detail and "max_value" in result.detail


def test_row_count_between_non_int_min_is_malformed() -> None:
    """Non-int bound (e.g. a float / string) → malformed-supported-test."""
    result = parse_test_entry({_RCB_NAME: {"min_value": "100", "max_value": 10000}}, column=None)
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"


def test_row_count_between_non_int_max_is_malformed() -> None:
    result = parse_test_entry({_RCB_NAME: {"min_value": 100, "max_value": 1.5}}, column=None)
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"


def test_row_count_between_negative_min_is_malformed() -> None:
    """Negative bounds are nonsensical for a row-count → malformed-supported-test."""
    result = parse_test_entry({_RCB_NAME: {"min_value": -1}}, column=None)
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"


def test_row_count_between_bool_min_is_malformed() -> None:
    """``isinstance(True, int) is True`` in Python — the parser must not
    silently coerce a bool to 0/1, which is rarely what the operator wrote."""
    result = parse_test_entry({_RCB_NAME: {"min_value": True, "max_value": 10000}}, column=None)
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"


def test_row_count_between_non_string_where_is_malformed() -> None:
    """``where`` must be a non-empty string when set."""
    result = parse_test_entry(
        {_RCB_NAME: {"min_value": 100, "max_value": 10000, "where": 42}},
        column=None,
    )
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"


def test_row_count_between_empty_string_where_is_malformed() -> None:
    """Whitespace-only ``where`` is unhelpful (mirrors the model-level
    Pydantic invariant) → malformed-supported-test."""
    result = parse_test_entry(
        {_RCB_NAME: {"min_value": 100, "max_value": 10000, "where": "   "}},
        column=None,
    )
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"


def test_row_count_between_column_scoped_is_malformed() -> None:
    """The variant is model-level only — a column-scoped usage routes to a
    structured skip with a descriptive ``detail`` rather than leaking a
    Pydantic ValidationError."""
    result = parse_test_entry({_RCB_NAME: {"min_value": 100, "max_value": 10000}}, column="amount")
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"
    assert result.test_name == _RCB_NAME
    assert result.column == "amount"
    assert "model-level" in result.detail


def test_row_count_between_non_dict_body_is_malformed() -> None:
    """A non-dict body has no args → no bounds → malformed-supported-test."""
    result = parse_test_entry({_RCB_NAME: "not-a-mapping"}, column=None)
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"
    assert result.test_name == _RCB_NAME


# Sibling macros stay in custom-or-generic-test (no behaviour change).


def test_expect_table_row_count_to_equal_is_custom_skip() -> None:
    """A *different* dbt-expectations macro is NOT promoted to the variant —
    it falls through to the existing namespaced custom-or-generic skip."""
    result = parse_test_entry(
        {"dbt_expectations.expect_table_row_count_to_equal": {"value": 1000}},
        column=None,
    )
    assert isinstance(result, SkippedTest)
    assert result.reason == "custom-or-generic-test"
    assert result.test_name == "dbt_expectations.expect_table_row_count_to_equal"


def test_row_count_between_bare_string_no_body_is_malformed() -> None:
    """A bare-string ``dbt_expectations.expect_table_row_count_to_be_between``
    (no body) is recognised by the dispatch arm, but with no body it has no
    bounds → malformed-supported-test (NOT the generic custom skip — the
    arm has already matched the macro name)."""
    result = parse_test_entry(_RCB_NAME, column=None)
    assert isinstance(result, SkippedTest)
    assert result.reason == "malformed-supported-test"


# SkipReason taxonomy is the closed 3-value Literal (DEC-011 of #169).


def test_skip_reason_literal_remains_three_values() -> None:
    """Adding a 4th skip cause requires explicit extension; #169 must NOT
    grow the closed taxonomy."""
    from typing import get_args

    values = get_args(SkipReason)
    assert set(values) == {
        "unsupported-test-type",
        "custom-or-generic-test",
        "malformed-supported-test",
    }
    assert len(values) == 3


# Fixture round-trip — the parser drives the fixture file's test entries.


def test_row_count_between_fixture_round_trips() -> None:
    """The fixture at ``tests/fixtures/ingest/row_count_between_schema.yml``
    exercises every documented kept + skipped case. Drive each model-level
    entry through ``parse_test_entry(..., column=None)`` and pin the
    classifications."""
    fixture = _FIXTURE_DIR / "row_count_between_schema.yml"
    data = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    # Locate the `orders` model + its model-level tests.
    orders = next(m for m in data["models"] if m["name"] == "orders")
    entries = orders["tests"]
    # Drive each entry; assert one classification per entry.
    classifications: list[tuple[str, str | None]] = []
    for entry in entries:
        result = parse_test_entry(cast(Any, entry), column=None)
        if isinstance(result, CandidateTestRowCountBetween):
            classifications.append(("kept", None))
        elif isinstance(result, SkippedTest):
            classifications.append(("skipped", result.reason))
        else:
            raise AssertionError(f"unexpected result type: {type(result).__name__}")
    assert classifications == [
        ("kept", None),  # inline bounds
        ("kept", None),  # arguments:-nested + where
        ("skipped", "malformed-supported-test"),  # missing both bounds
        ("skipped", "malformed-supported-test"),  # min > max
        ("skipped", "custom-or-generic-test"),  # sibling expect_table_row_count_to_equal
    ]
