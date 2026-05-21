"""Test matrix for the dbt test-entry parser (US-003).

Each test pins exactly one entry shape: bare strings, single-key dicts with
inline and ``arguments:``-nested args, config-key tolerance, malformed
supported types, custom/namespaced tests, and the ``ref()`` / ``source()``
unwrap (DEC-009). No ``assert True``-shaped tests — every assertion can fail
on a real regression (``.claude/rules/testing-signal.md``).
"""

from __future__ import annotations

from signalforge.draft.models import (
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestUnique,
)
from signalforge.ingest.models import SkippedTest
from signalforge.ingest.parser import parse_test_entry

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
