"""DraftConfig.exclude_tests tests (issue #54).

Three layers of coverage:

* Config-load validation: unknown test types fail loud; valid types
  load + dedupe; empty defaults preserved.
* Prompt rendering: the system prompt's test catalogue + SCOPE line
  filter to remaining types; the version hash rotates per exclusion
  set; no exclusions preserves the historic snapshot.
* Parser anchor-contract: LLM responses containing excluded test
  types fail with :class:`LLMOutputAnchorContractError` carrying the
  per-violation strings.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from signalforge.draft.config import VALID_TEST_TYPES, DraftConfig
from signalforge.draft.errors import LLMOutputAnchorContractError
from signalforge.draft.parser import (
    _LLMResultMeta,
    _validate_anchor_contract,
    parse_draft_response,
)
from signalforge.draft.prompts import (
    _PROMPT_VERSION,
    _render_system_prompt,
    _SYSTEM_PROMPT,
    _prompt_version_for,
)


# ---------------------------------------------------------------------------
# DraftConfig validation
# ---------------------------------------------------------------------------


def test_exclude_tests_default_is_empty_tuple() -> None:
    config = DraftConfig()
    assert config.exclude_tests == ()


def test_exclude_tests_accepts_valid_test_types() -> None:
    config = DraftConfig(exclude_tests=("not_null", "unique"))
    assert config.exclude_tests == ("not_null", "unique")


def test_exclude_tests_accepts_yaml_list_form() -> None:
    """YAML lists arrive as lists; validator coerces to tuple."""
    config = DraftConfig.model_validate({"exclude_tests": ["accepted_values"]})
    assert config.exclude_tests == ("accepted_values",)


def test_exclude_tests_dedupes_preserving_first_seen_order() -> None:
    config = DraftConfig(exclude_tests=("unique", "not_null", "unique"))
    assert config.exclude_tests == ("unique", "not_null")


def test_exclude_tests_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError) as exc:
        DraftConfig(exclude_tests=("not_nul",))
    assert "not a valid test type" in str(exc.value)


def test_exclude_tests_rejects_bare_string() -> None:
    """Bare strings are a common config-typing footgun; reject them
    rather than treating each character as an entry."""
    with pytest.raises(ValidationError) as exc:
        DraftConfig(exclude_tests="not_null")  # type: ignore[arg-type]
    assert "list of test-type strings, not a single string" in str(exc.value)


def test_exclude_tests_rejects_non_string_entries() -> None:
    with pytest.raises(ValidationError) as exc:
        DraftConfig(exclude_tests=("not_null", 1))  # type: ignore[arg-type]
    assert "must be strings" in str(exc.value)


def test_valid_test_types_constant_matches_dbt_four() -> None:
    """Pin the canonical set so adding a new test type without updating
    VALID_TEST_TYPES (and the prompt catalogue) fails loud here."""
    assert VALID_TEST_TYPES == frozenset(
        {"not_null", "unique", "accepted_values", "relationships"}
    )


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_render_system_prompt_empty_exclude_returns_historic_constant() -> None:
    assert _render_system_prompt(()) == _SYSTEM_PROMPT


def test_render_system_prompt_drops_excluded_from_catalogue() -> None:
    prompt = _render_system_prompt(("accepted_values", "relationships"))
    assert '"type": "accepted_values"' not in prompt
    assert '"type": "relationships"' not in prompt
    # Surviving types still appear.
    assert '"type": "not_null"' in prompt
    assert '"type": "unique"' in prompt


def test_render_system_prompt_updates_scope_line() -> None:
    prompt = _render_system_prompt(("accepted_values", "relationships"))
    # SCOPE line names only the surviving types.
    assert "Propose only `not_null` and `unique` tests" in prompt
    assert "accepted_values" not in prompt
    assert "relationships" not in prompt


def test_render_system_prompt_single_remaining_type_uses_bare_form() -> None:
    prompt = _render_system_prompt(("unique", "accepted_values", "relationships"))
    assert "Propose only `not_null` tests" in prompt


def test_render_system_prompt_excluding_all_four_raises() -> None:
    with pytest.raises(ValueError) as exc:
        _render_system_prompt(
            ("not_null", "unique", "accepted_values", "relationships")
        )
    assert "at least one type must remain" in str(exc.value)


def test_prompt_version_for_empty_equals_base_constant() -> None:
    """Default (no exclusions) preserves the snapshot hash."""
    assert _prompt_version_for(()) == _PROMPT_VERSION


def test_prompt_version_for_with_exclusions_differs_from_base() -> None:
    v1 = _prompt_version_for(("not_null",))
    v2 = _prompt_version_for(("unique",))
    assert v1 != _PROMPT_VERSION
    assert v2 != _PROMPT_VERSION
    assert v1 != v2


def test_prompt_version_for_canonical_across_order_and_dupes() -> None:
    """Two exclusion sets with different order/dupes produce the same hash —
    the canonicalisation is sorted + deduped."""
    assert _prompt_version_for(("not_null", "unique")) == _prompt_version_for(
        ("unique", "not_null", "not_null")
    )


# ---------------------------------------------------------------------------
# Parser anchor-contract
# ---------------------------------------------------------------------------


_FAKE_META = _LLMResultMeta(
    prompt_version="0123456789abcdef",
    model="claude-sonnet-4-6",
    cache_hit=False,
    input_tokens=100,
    output_tokens=50,
)


def _candidate_with_test(test_type: str, *, column: str = "id") -> str:
    payload = {
        "schema_version": 1,
        "name": "orders",
        "description": "Orders fact.",
        "rationale": "rationale",
        "columns": [
            {
                "name": column,
                "description": "the id",
                "rationale": "primary key",
                "tests": [
                    {
                        "type": test_type,
                        "column": column,
                        "rationale": "x",
                        **(
                            {"values": ["a", "b"]}
                            if test_type == "accepted_values"
                            else {}
                        ),
                        **(
                            {"to": "ref('other')", "field": "id"}
                            if test_type == "relationships"
                            else {}
                        ),
                    }
                ],
            }
        ],
        "tests": [],
    }
    return json.dumps(payload)


def test_parser_rejects_excluded_column_level_test_type() -> None:
    raw = _candidate_with_test("unique")
    with pytest.raises(LLMOutputAnchorContractError) as exc:
        parse_draft_response(
            raw,
            frozenset({"id"}),
            llm_result_meta=_FAKE_META,
            exclude_tests=frozenset({"unique"}),
        )
    violations = exc.value.violations
    assert any("'unique'" in v and "exclude_tests" in v for v in violations)


def test_parser_rejects_excluded_model_level_test_type() -> None:
    payload = {
        "schema_version": 1,
        "name": "orders",
        "description": "Orders fact.",
        "rationale": "rationale",
        "columns": [],
        "tests": [
            {
                "type": "unique",
                "column": "id",
                "rationale": "model-level uniqueness",
            }
        ],
    }
    raw = json.dumps(payload)
    with pytest.raises(LLMOutputAnchorContractError) as exc:
        parse_draft_response(
            raw,
            frozenset({"id"}),
            llm_result_meta=_FAKE_META,
            exclude_tests=frozenset({"unique"}),
        )
    violations = exc.value.violations
    assert any(
        "model-level" in v and "'unique'" in v and "exclude_tests" in v
        for v in violations
    )


def test_parser_passes_when_excluded_not_present() -> None:
    """A response that respects the exclusion set parses cleanly."""
    raw = _candidate_with_test("not_null")
    candidate = parse_draft_response(
        raw,
        frozenset({"id"}),
        llm_result_meta=_FAKE_META,
        exclude_tests=frozenset({"unique"}),
    )
    assert candidate.name == "orders"


def test_parser_collects_excluded_alongside_other_violations() -> None:
    """Whole-draft fail-loud (DEC-022): excluded-type and
    hallucinated-column violations both surface in a single error."""
    payload = {
        "schema_version": 1,
        "name": "orders",
        "description": "x",
        "rationale": "x",
        "columns": [
            {
                "name": "phantom",
                "description": "x",
                "rationale": "x",
                "tests": [
                    {"type": "unique", "column": "phantom", "rationale": "x"}
                ],
            }
        ],
        "tests": [],
    }
    raw = json.dumps(payload)
    with pytest.raises(LLMOutputAnchorContractError) as exc:
        parse_draft_response(
            raw,
            frozenset({"id"}),
            llm_result_meta=_FAKE_META,
            exclude_tests=frozenset({"unique"}),
        )
    violations = exc.value.violations
    # Both the hallucinated-column AND the excluded-type violations land.
    assert any("phantom" in v for v in violations)
    assert any("exclude_tests" in v for v in violations)


def test_validate_anchor_contract_empty_exclude_does_not_add_violations() -> None:
    """Backwards-compat: the validator without exclude_tests matches v0.1."""
    from signalforge.draft.models import CandidateSchema

    raw = _candidate_with_test("unique")
    candidate = CandidateSchema.model_validate_json(raw)
    violations = _validate_anchor_contract(candidate, frozenset({"id"}))
    assert violations == ()
