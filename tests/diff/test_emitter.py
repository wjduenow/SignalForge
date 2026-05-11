"""Tests for the canonical YAML emitter (US-005, AR-9).

The emitter takes a :class:`signalforge.draft.CandidateSchema` plus a
:class:`signalforge.prune.PruneResult` and produces a deterministic
dbt-style ``schema.yml`` document with only kept tests. The acceptance
contract this file enforces:

* Tests with ``decision != "kept"`` are filtered out.
* Column declaration order is preserved (NOT alphabetised).
* Tests within a column sort by ``(type, args_hash)`` for determinism.
* Model-level kept tests appear under the model's ``tests:`` key.
* Edge-case descriptions (``---``, ``!tag``, triple-backticks,
  embedded newlines — AR-9) round-trip through ``yaml.safe_load`` to
  identical strings.
"""

from __future__ import annotations

import hashlib
import json

import yaml

from signalforge.diff._emitter import emit_proposed_yaml
from signalforge.draft import CandidateColumn, CandidateSchema
from signalforge.draft.models import (
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestUnique,
)
from signalforge.prune.models import PruneDecision, PruneResult


def _args_hash(test) -> str:
    """Re-derive the args_hash the emitter uses, for round-trip checks."""
    if isinstance(test, (CandidateTestNotNull, CandidateTestUnique)):
        payload = {"type": test.type, "column": test.column}
    elif isinstance(test, CandidateTestAcceptedValues):
        payload = {
            "type": test.type,
            "column": test.column,
            "values": sorted(test.values),
        }
    elif isinstance(test, CandidateTestRelationships):
        payload = {
            "type": test.type,
            "column": test.column,
            "to": test.to,
            "field": test.field,
        }
    else:
        raise AssertionError(f"unhandled variant {type(test).__name__}")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=4).hexdigest()


def _decision(
    test,
    *,
    decision: str = "kept",
    reason: str = "kept",
    test_anchor: str | None = None,
) -> PruneDecision:
    """Build a synthetic :class:`PruneDecision` for a candidate test.

    The emitter only consumes ``decision``, ``test_anchor``, and
    ``test``; the remaining fields are populated with deterministic
    placeholders so the model validates without exercising warehouse
    behaviour.
    """
    if test_anchor is None:
        test_anchor = "model" if not hasattr(test, "column") else f"column.{test.column}"
    return PruneDecision(
        test_anchor=test_anchor,
        test=test,
        decision=decision,  # pyright: ignore[reportArgumentType]
        reason=reason,  # pyright: ignore[reportArgumentType]
        failures=0,
        sampled_rows=1000,
        scope="sample",
        elapsed_ms=42,
        compiled_sql_hash="0" * 16,
        compiled_sql="SELECT 1",
        why="synthetic",
    )


def _result(*decisions: PruneDecision, model_unique_id: str = "model.proj.m") -> PruneResult:
    return PruneResult(
        model_unique_id=model_unique_id,
        decisions=decisions,
        elapsed_ms=100,
        signalforge_version="0.0.0",
    )


# ---------------------------------------------------------------------------
# Basic kept-test emission
# ---------------------------------------------------------------------------


def test_emits_kept_tests_in_dbt_schema_yml_v2_shape() -> None:
    not_null = CandidateTestNotNull(column="id")
    unique = CandidateTestUnique(column="id")
    candidate = CandidateSchema(
        name="customers",
        description="One row per customer.",
        columns=(
            CandidateColumn(name="id", description="Surrogate PK.", tests=(not_null, unique)),
        ),
    )
    result = _result(_decision(not_null), _decision(unique))

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)

    assert parsed["version"] == 2
    assert len(parsed["models"]) == 1
    model = parsed["models"][0]
    assert model["name"] == "customers"
    assert model["description"] == "One row per customer."
    assert len(model["columns"]) == 1
    col = model["columns"][0]
    assert col["name"] == "id"
    assert col["description"] == "Surrogate PK."
    assert col["tests"] == ["not_null", "unique"]


# ---------------------------------------------------------------------------
# Filter: tests with decision != "kept" are dropped
# ---------------------------------------------------------------------------


def test_dropped_tests_are_filtered_out() -> None:
    not_null = CandidateTestNotNull(column="id")
    unique = CandidateTestUnique(column="id")
    candidate = CandidateSchema(
        name="customers",
        description="d",
        columns=(CandidateColumn(name="id", description="d", tests=(not_null, unique)),),
    )
    # not_null kept, unique dropped.
    result = _result(
        _decision(not_null, decision="kept", reason="kept"),
        _decision(unique, decision="dropped", reason="always-passes"),
    )

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)

    assert parsed["models"][0]["columns"][0]["tests"] == ["not_null"]


def test_columns_with_no_kept_tests_omit_tests_key() -> None:
    not_null = CandidateTestNotNull(column="id")
    candidate = CandidateSchema(
        name="customers",
        description="d",
        columns=(CandidateColumn(name="id", description="d", tests=(not_null,)),),
    )
    result = _result(_decision(not_null, decision="dropped", reason="always-passes"))

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)

    col = parsed["models"][0]["columns"][0]
    assert "tests" not in col  # absent, not present-and-empty


# ---------------------------------------------------------------------------
# Column order preservation
# ---------------------------------------------------------------------------


def test_column_declaration_order_is_preserved() -> None:
    # zebra before alpha — alphabetisation would re-order to alpha first.
    candidate = CandidateSchema(
        name="t",
        description="d",
        columns=(
            CandidateColumn(name="zebra", description="z"),
            CandidateColumn(name="alpha", description="a"),
            CandidateColumn(name="middle", description="m"),
        ),
    )
    result = _result()  # no kept tests; we only care about column order.

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)

    assert [c["name"] for c in parsed["models"][0]["columns"]] == ["zebra", "alpha", "middle"]


# ---------------------------------------------------------------------------
# Test sort order: (type, args_hash) within a column
# ---------------------------------------------------------------------------


def test_tests_within_column_sort_by_type_then_args_hash() -> None:
    # Construction order: unique, not_null. Sorted-by-type order:
    # not_null, unique.
    not_null = CandidateTestNotNull(column="id")
    unique = CandidateTestUnique(column="id")
    candidate = CandidateSchema(
        name="t",
        description="d",
        columns=(CandidateColumn(name="id", description="d", tests=(unique, not_null)),),
    )
    result = _result(_decision(unique), _decision(not_null))

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)

    assert parsed["models"][0]["columns"][0]["tests"] == ["not_null", "unique"]


def test_two_accepted_values_tests_sort_by_args_hash() -> None:
    # Two accepted_values with different value lists land in args_hash
    # order — deterministic, not insertion-order.
    av_a = CandidateTestAcceptedValues(column="status", values=("active", "inactive"))
    av_b = CandidateTestAcceptedValues(column="status", values=("draft", "published"))
    candidate = CandidateSchema(
        name="t",
        description="d",
        columns=(CandidateColumn(name="status", description="d", tests=(av_a, av_b)),),
    )
    result = _result(_decision(av_a), _decision(av_b))

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)

    tests = parsed["models"][0]["columns"][0]["tests"]
    # Both accepted_values entries are present; the hashes determine
    # order. We assert the expected order computed from the hashes.
    expected_order = sorted([av_a, av_b], key=lambda t: ("accepted_values", _args_hash(t)))
    expected_values = [list(t.values) for t in expected_order]
    actual_values = [t["accepted_values"]["values"] for t in tests]
    assert actual_values == expected_values


# ---------------------------------------------------------------------------
# Test rendering shapes
# ---------------------------------------------------------------------------


def test_accepted_values_renders_as_dict_with_values() -> None:
    av = CandidateTestAcceptedValues(column="status", values=("a", "b", "c"))
    candidate = CandidateSchema(
        name="t",
        description="d",
        columns=(CandidateColumn(name="status", description="d", tests=(av,)),),
    )
    result = _result(_decision(av))

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)

    [test] = parsed["models"][0]["columns"][0]["tests"]
    assert test == {"accepted_values": {"values": ["a", "b", "c"]}}


def test_relationships_renders_as_dict_with_to_and_field() -> None:
    rel = CandidateTestRelationships(column="customer_id", to="ref('customers')", field="id")
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="customer_id", description="d", tests=(rel,)),),
    )
    result = _result(_decision(rel))

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)

    [test] = parsed["models"][0]["columns"][0]["tests"]
    assert test == {"relationships": {"to": "ref('customers')", "field": "id"}}


# ---------------------------------------------------------------------------
# Model-level kept tests
# ---------------------------------------------------------------------------


def test_model_level_tests_appear_under_models_tests_key_when_kept() -> None:
    rel = CandidateTestRelationships(column="customer_id", to="ref('customers')", field="id")
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="customer_id", description="d"),),
        tests=(rel,),
    )
    result = _result(_decision(rel, test_anchor="model"))

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)

    assert parsed["models"][0]["tests"] == [
        {"relationships": {"to": "ref('customers')", "field": "id"}}
    ]


def test_model_level_tests_omitted_when_dropped() -> None:
    rel = CandidateTestRelationships(column="customer_id", to="ref('customers')", field="id")
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="customer_id", description="d"),),
        tests=(rel,),
    )
    result = _result(
        _decision(rel, test_anchor="model", decision="dropped", reason="always-passes")
    )

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)

    assert "tests" not in parsed["models"][0]


# ---------------------------------------------------------------------------
# AR-9: edge-case descriptions round-trip through yaml.safe_load
# ---------------------------------------------------------------------------


def test_ar9_edge_case_descriptions_round_trip() -> None:
    edge_cases = {
        "yaml_doc_marker": "---",
        "yaml_tag_lookalike": "!tag here",
        "triple_backtick": "fenced\n```\ncode block\n```\nafter",
        "embedded_newlines": "line one\nline two\nline three",
        "leading_quote": '"quoted-looking"',
        "leading_pipe": "| leading pipe",
        "leading_gt": "> leading gt",
        "unicode_text": "café ☕ 你好",
    }

    columns = tuple(
        CandidateColumn(name=f"col_{i}", description=desc)
        for i, desc in enumerate(edge_cases.values())
    )
    candidate = CandidateSchema(
        name="edge_cases",
        description="---\n!tag\n```\ntriple-backticks here\n```\n",
        columns=columns,
    )
    result = _result()

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)

    # Model description round-trips byte-identical.
    assert parsed["models"][0]["description"] == "---\n!tag\n```\ntriple-backticks here\n```\n"

    # Each column description round-trips byte-identical.
    for col, expected in zip(parsed["models"][0]["columns"], edge_cases.values(), strict=True):
        assert col["description"] == expected, (
            f"description for {col['name']!r} did not round-trip; "
            f"expected {expected!r}, got {col['description']!r}"
        )


# ---------------------------------------------------------------------------
# Determinism: same input → same bytes
# ---------------------------------------------------------------------------


def test_emit_is_deterministic_across_calls() -> None:
    av_a = CandidateTestAcceptedValues(column="status", values=("a", "b"))
    av_b = CandidateTestAcceptedValues(column="status", values=("c", "d"))
    candidate = CandidateSchema(
        name="t",
        description="d",
        columns=(
            CandidateColumn(name="status", description="d", tests=(av_a, av_b)),
            CandidateColumn(name="other", description="d"),
        ),
    )
    result = _result(_decision(av_a), _decision(av_b))

    first = emit_proposed_yaml(candidate, result)
    second = emit_proposed_yaml(candidate, result)
    assert first == second
