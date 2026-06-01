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


# ---------------------------------------------------------------------------
# Singular custom_sql tests (issue #116) — standalone .sql files
# ---------------------------------------------------------------------------

from signalforge._common.artifact_id import model_test_args_hash  # noqa: E402
from signalforge.diff._emitter import emit_proposed_test_files  # noqa: E402
from signalforge.diff._test_file_writer import _GENERATED_MARKER_PREFIX  # noqa: E402
from signalforge.draft.models import CandidateTestCustomSQL  # noqa: E402


def test_custom_sql_is_skipped_from_schema_yml() -> None:
    """A kept ``custom_sql`` test must NOT appear in the proposed schema.yml.

    Singular business-rule tests are standalone ``.sql`` files (DEC-002 of
    #116), not schema.yml blocks. The YAML emitter must skip them cleanly —
    not crash (the pre-#116 ``_render_test`` raised on unknown types).
    """
    not_null = CandidateTestNotNull(column="id")
    custom = CandidateTestCustomSQL(sql="select * from {{ this }} where x < 0", column="id")
    candidate = CandidateSchema(
        name="customers",
        description="One row per customer.",
        columns=(CandidateColumn(name="id", description="PK.", tests=(not_null, custom)),),
    )
    result = _result(
        _decision(not_null),
        _decision(custom, test_anchor="column.id"),
    )

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)
    col = parsed["models"][0]["columns"][0]
    # Only the not_null survives in the YAML; custom_sql is excluded.
    assert col["tests"] == ["not_null"]


def test_column_with_only_custom_sql_omits_tests_key() -> None:
    """A column whose only kept test is ``custom_sql`` emits no ``tests:`` key."""
    custom = CandidateTestCustomSQL(sql="select 1", column="id")
    candidate = CandidateSchema(
        name="customers",
        description="d",
        columns=(CandidateColumn(name="id", description="PK.", tests=(custom,)),),
    )
    result = _result(_decision(custom, test_anchor="column.id"))

    parsed = yaml.safe_load(emit_proposed_yaml(candidate, result))
    col = parsed["models"][0]["columns"][0]
    assert "tests" not in col


def test_model_level_custom_sql_skipped_from_schema_yml() -> None:
    """A model-level ``custom_sql`` test does not produce a model ``tests:`` key."""
    custom = CandidateTestCustomSQL(sql="select 1", column=None)
    candidate = CandidateSchema(
        name="customers",
        description="d",
        columns=(CandidateColumn(name="id", description="PK."),),
        tests=(custom,),
    )
    result = _result(_decision(custom, test_anchor="model"))

    parsed = yaml.safe_load(emit_proposed_yaml(candidate, result))
    assert "tests" not in parsed["models"][0]


def test_emit_proposed_test_files_column_scoped() -> None:
    """A kept column-scoped ``custom_sql`` test yields one proposed ``.sql`` file."""
    sql = "select * from {{ ref('customers') }} where total < 0"
    custom = CandidateTestCustomSQL(sql=sql, column="total")
    candidate = CandidateSchema(
        name="customers",
        description="d",
        columns=(CandidateColumn(name="total", description="amount.", tests=(custom,)),),
    )
    result = _result(_decision(custom, test_anchor="column.total"))

    files = emit_proposed_test_files(candidate, result)
    assert len(files) == 1
    proposed = files[0]
    expected_hash = model_test_args_hash(custom)
    # Path is the safe relative tests/<model>__<descriptor>_<hash>.sql shape.
    assert proposed.path == f"tests/customers__total_custom_sql_{expected_hash}.sql"
    # SQL carries the generated-header marker + the original SQL body.
    assert proposed.sql.startswith(f"{_GENERATED_MARKER_PREFIX} {expected_hash}\n")
    assert sql in proposed.sql


def test_emit_proposed_test_files_model_level() -> None:
    """A kept model-level ``custom_sql`` test yields a ``custom_sql`` descriptor file."""
    custom = CandidateTestCustomSQL(sql="select 1 where false", column=None)
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="id", description="PK."),),
        tests=(custom,),
    )
    result = _result(_decision(custom, test_anchor="model"))

    files = emit_proposed_test_files(candidate, result)
    assert len(files) == 1
    expected_hash = model_test_args_hash(custom)
    assert files[0].path == f"tests/orders__custom_sql_{expected_hash}.sql"


def test_emit_proposed_test_files_excludes_dropped_custom_sql() -> None:
    """A DROPPED ``custom_sql`` test produces no proposed ``.sql`` file."""
    custom = CandidateTestCustomSQL(sql="select 1", column="id")
    candidate = CandidateSchema(
        name="customers",
        description="d",
        columns=(CandidateColumn(name="id", description="PK.", tests=(custom,)),),
    )
    result = _result(
        _decision(custom, decision="dropped", reason="always-passes", test_anchor="column.id")
    )

    assert emit_proposed_test_files(candidate, result) == ()


def test_emit_proposed_test_files_ignores_non_custom_sql() -> None:
    """Standard schema tests never produce proposed ``.sql`` files."""
    not_null = CandidateTestNotNull(column="id")
    candidate = CandidateSchema(
        name="customers",
        description="d",
        columns=(CandidateColumn(name="id", description="PK.", tests=(not_null,)),),
    )
    result = _result(_decision(not_null))
    assert emit_proposed_test_files(candidate, result) == ()


def test_emit_proposed_test_files_dedupes_same_path() -> None:
    """Two kept decisions resolving to the same ``.sql`` path collapse to one
    proposal — the dedupe ``continue`` in ``emit_proposed_test_files`` skips
    the second (defensive: identical SQL + anchor → identical filename)."""
    custom = CandidateTestCustomSQL(sql="select 1 where false", column="id")
    candidate = CandidateSchema(
        name="customers",
        description="d",
        columns=(CandidateColumn(name="id", description="PK.", tests=(custom,)),),
    )
    # Two decisions for the SAME test → same args_hash → same path.
    result = _result(
        _decision(custom, test_anchor="column.id"),
        _decision(custom, test_anchor="column.id"),
    )
    files = emit_proposed_test_files(candidate, result)
    assert len(files) == 1
    expected_hash = model_test_args_hash(custom)
    assert files[0].path == f"tests/customers__id_custom_sql_{expected_hash}.sql"


def test_emit_proposed_test_files_path_is_slug_safe_for_hostile_model_name() -> None:
    """A crafted model name cannot inject a path separator / traversal token."""
    custom = CandidateTestCustomSQL(sql="select 1", column=None)
    candidate = CandidateSchema(
        name="../../etc/passwd",
        description="d",
        columns=(CandidateColumn(name="id", description="PK."),),
        tests=(custom,),
    )
    result = _result(_decision(custom, test_anchor="model"))

    files = emit_proposed_test_files(candidate, result)
    assert len(files) == 1
    path = files[0].path
    assert path.startswith("tests/")
    # No traversal token, no extra separator below tests/.
    assert ".." not in path
    assert path.count("/") == 1


# ---------------------------------------------------------------------------
# row_count_between variant (US-010 of #169) — dbt-expectations YAML shape
# ---------------------------------------------------------------------------

from signalforge.draft.models import CandidateTestRowCountBetween  # noqa: E402


def test_row_count_between_renders_dbt_expectations_block_without_where() -> None:
    """No-where YAML shape: only ``min_value`` and ``max_value`` appear
    under the ``dbt_expectations.expect_table_row_count_to_be_between``
    key — null fields are omitted (DEC-002).

    Field-name mapping outbound (DEC-008): Python-side ``minimum`` /
    ``maximum`` map to the dbt-expectations macro names ``min_value`` /
    ``max_value``.
    """
    rcb = CandidateTestRowCountBetween(minimum=100, maximum=10000)
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="id", description="PK."),),
        tests=(rcb,),
    )
    result = _result(_decision(rcb, test_anchor="model"))

    parsed = yaml.safe_load(emit_proposed_yaml(candidate, result))
    model_tests = parsed["models"][0]["tests"]
    assert model_tests == [
        {
            "dbt_expectations.expect_table_row_count_to_be_between": {
                "min_value": 100,
                "max_value": 10000,
            }
        }
    ]


def test_row_count_between_includes_where_when_set() -> None:
    """With-where YAML shape: the ``where`` field is rendered verbatim
    under the macro block when non-null. ``yaml.safe_dump`` handles the
    string quoting.
    """
    rcb = CandidateTestRowCountBetween(
        minimum=100,
        maximum=10000,
        where="event_date >= '2024-01-01'",
    )
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="id", description="PK."),),
        tests=(rcb,),
    )
    result = _result(_decision(rcb, test_anchor="model"))

    parsed = yaml.safe_load(emit_proposed_yaml(candidate, result))
    [block] = parsed["models"][0]["tests"]
    body = block["dbt_expectations.expect_table_row_count_to_be_between"]
    assert body == {
        "min_value": 100,
        "max_value": 10000,
        "where": "event_date >= '2024-01-01'",
    }


def test_row_count_between_only_minimum_omits_max_value() -> None:
    """A test with only ``minimum`` set emits only ``min_value`` — the
    ``None``-valued ``max_value`` is dropped from the YAML shape so the
    block is minimal (DEC-002).
    """
    rcb = CandidateTestRowCountBetween(minimum=100)
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="id", description="PK."),),
        tests=(rcb,),
    )
    result = _result(_decision(rcb, test_anchor="model"))

    parsed = yaml.safe_load(emit_proposed_yaml(candidate, result))
    [block] = parsed["models"][0]["tests"]
    body = block["dbt_expectations.expect_table_row_count_to_be_between"]
    assert body == {"min_value": 100}
    assert "max_value" not in body
    assert "where" not in body


def test_row_count_between_only_maximum_omits_min_value() -> None:
    """A test with only ``maximum`` set emits only ``max_value``."""
    rcb = CandidateTestRowCountBetween(maximum=10000)
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="id", description="PK."),),
        tests=(rcb,),
    )
    result = _result(_decision(rcb, test_anchor="model"))

    parsed = yaml.safe_load(emit_proposed_yaml(candidate, result))
    [block] = parsed["models"][0]["tests"]
    body = block["dbt_expectations.expect_table_row_count_to_be_between"]
    assert body == {"max_value": 10000}
    assert "min_value" not in body


def test_row_count_between_hostile_where_is_yaml_safe() -> None:
    """A ``where`` clause containing multi-line content, embedded quotes,
    and YAML metacharacters round-trips through ``yaml.safe_load`` to
    the identical string — ``yaml.safe_dump`` picks whichever scalar
    style preserves it. The point is the bytes are safe / round-trip;
    NOT a specific quoting style.
    """
    hostile = (
        "event_date >= '2024-01-01'\nAND notes LIKE '%\"quoted\"%'\n"
        "AND id != 'x: y'  # not a yaml key"
    )
    rcb = CandidateTestRowCountBetween(minimum=1, maximum=10, where=hostile)
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="id", description="PK."),),
        tests=(rcb,),
    )
    result = _result(_decision(rcb, test_anchor="model"))

    out = emit_proposed_yaml(candidate, result)
    parsed = yaml.safe_load(out)
    [block] = parsed["models"][0]["tests"]
    body = block["dbt_expectations.expect_table_row_count_to_be_between"]
    assert body["where"] == hostile


def test_row_count_between_dropped_decision_filtered_out() -> None:
    """A dropped ``row_count_between`` is filtered before rendering —
    the model has no ``tests:`` key in the emitted YAML.
    """
    rcb = CandidateTestRowCountBetween(minimum=100, maximum=10000)
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="id", description="PK."),),
        tests=(rcb,),
    )
    result = _result(
        _decision(rcb, test_anchor="model", decision="dropped", reason="always-passes")
    )

    parsed = yaml.safe_load(emit_proposed_yaml(candidate, result))
    assert "tests" not in parsed["models"][0]


def test_row_count_between_does_not_appear_in_proposed_test_files() -> None:
    """``row_count_between`` ships as a YAML block, NOT as a standalone
    ``tests/*.sql`` file — only ``custom_sql`` flows to
    :func:`emit_proposed_test_files` (DEC-002 of #169).
    """
    rcb = CandidateTestRowCountBetween(minimum=100, maximum=10000)
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="id", description="PK."),),
        tests=(rcb,),
    )
    result = _result(_decision(rcb, test_anchor="model"))

    assert emit_proposed_test_files(candidate, result) == ()


# ---------------------------------------------------------------------------
# unique_combination variant (US-006 of #170) — dbt_utils YAML shape
# ---------------------------------------------------------------------------

from signalforge.draft.models import CandidateTestUniqueCombination  # noqa: E402


def test_unique_combination_renders_dbt_utils_block_without_where() -> None:
    """No-where YAML shape: only ``combination_of_columns`` appears under
    the ``dbt_utils.unique_combination_of_columns`` key.

    **Field-name mapping seam** (DEC-002 of #170): Pydantic-side
    ``columns`` maps to the dbt-utils macro key ``combination_of_columns``
    on emission. The internal model keeps the prefix-free name
    (matches the ``values`` / ``to`` / ``field`` precedent on the other
    variants); the macro naming lives only in the emitter.

    Emission preserves the order Pydantic carries — sorting is only for
    the canonical hash domain (DEC-011), NOT for YAML output, so the
    operator's review surface reflects the LLM's declared order.
    """
    uc = CandidateTestUniqueCombination(columns=("order_id", "line_no"))
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(
            CandidateColumn(name="order_id", description="PK."),
            CandidateColumn(name="line_no", description="Line no."),
        ),
        tests=(uc,),
    )
    result = _result(_decision(uc, test_anchor="model"))

    parsed = yaml.safe_load(emit_proposed_yaml(candidate, result))
    model_tests = parsed["models"][0]["tests"]
    assert model_tests == [
        {
            "dbt_utils.unique_combination_of_columns": {
                "combination_of_columns": ["order_id", "line_no"],
            }
        }
    ]


def test_unique_combination_includes_where_when_set() -> None:
    """With-where YAML shape: the ``where`` field is rendered verbatim
    under the macro block when non-null. ``yaml.safe_dump`` handles the
    string quoting.
    """
    uc = CandidateTestUniqueCombination(
        columns=("user_id", "event_date"),
        where="status = 'active'",
    )
    candidate = CandidateSchema(
        name="events",
        description="d",
        columns=(
            CandidateColumn(name="user_id", description="User."),
            CandidateColumn(name="event_date", description="Date."),
        ),
        tests=(uc,),
    )
    result = _result(_decision(uc, test_anchor="model"))

    parsed = yaml.safe_load(emit_proposed_yaml(candidate, result))
    [block] = parsed["models"][0]["tests"]
    body = block["dbt_utils.unique_combination_of_columns"]
    assert body == {
        "combination_of_columns": ["user_id", "event_date"],
        "where": "status = 'active'",
    }


def test_unique_combination_preserves_declared_column_order_not_sorted() -> None:
    """Emission preserves the order Pydantic carries — the canonical-hash
    sort (DEC-011) is for the artifact_id domain only. The YAML body
    surfaces the LLM's declared order to the operator review surface."""
    # Deliberately NOT alphabetic so a stray sort() would flip the order.
    uc = CandidateTestUniqueCombination(columns=("z_id", "a_id", "m_id"))
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(
            CandidateColumn(name="z_id", description="z"),
            CandidateColumn(name="a_id", description="a"),
            CandidateColumn(name="m_id", description="m"),
        ),
        tests=(uc,),
    )
    result = _result(_decision(uc, test_anchor="model"))

    parsed = yaml.safe_load(emit_proposed_yaml(candidate, result))
    [block] = parsed["models"][0]["tests"]
    body = block["dbt_utils.unique_combination_of_columns"]
    assert body["combination_of_columns"] == ["z_id", "a_id", "m_id"]


def test_unique_combination_dropped_decision_filtered_out() -> None:
    """A dropped ``unique_combination`` is filtered before rendering —
    the model has no ``tests:`` key in the emitted YAML."""
    uc = CandidateTestUniqueCombination(columns=("a", "b"))
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(
            CandidateColumn(name="a", description="a"),
            CandidateColumn(name="b", description="b"),
        ),
        tests=(uc,),
    )
    result = _result(_decision(uc, test_anchor="model", decision="dropped", reason="always-passes"))

    parsed = yaml.safe_load(emit_proposed_yaml(candidate, result))
    assert "tests" not in parsed["models"][0]


def test_unique_combination_does_not_appear_in_proposed_test_files() -> None:
    """``unique_combination`` ships as a YAML block, NOT as a standalone
    ``tests/*.sql`` file — only ``custom_sql`` flows to
    :func:`emit_proposed_test_files`.
    """
    uc = CandidateTestUniqueCombination(columns=("a", "b"))
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(
            CandidateColumn(name="a", description="a"),
            CandidateColumn(name="b", description="b"),
        ),
        tests=(uc,),
    )
    result = _result(_decision(uc, test_anchor="model"))

    assert emit_proposed_test_files(candidate, result) == ()


# ---------------------------------------------------------------------------
# row_count_anomaly_by_period variant (US-014 of #171) — singular .sql file
# ---------------------------------------------------------------------------

from datetime import date  # noqa: E402

import pytest  # noqa: E402

from signalforge.diff._emitter import _SKIP, _render_test  # noqa: E402
from signalforge.draft.models import CandidateTestRowCountAnomalyByPeriod  # noqa: E402
from signalforge.manifest.models import Model  # noqa: E402
from signalforge.prune.compiler import (  # noqa: E402
    _compile_anomaly_singular_test_sql,
    _compile_anomaly_violation_query,
)
from signalforge.warehouse.models import BIGQUERY_DIALECT, TableRef  # noqa: E402


def _orders_model_for_anomaly() -> Model:
    """Minimal manifest :class:`Model` for the anomaly violation query.

    Carries ``database`` + ``schema_`` so ``TableRef.from_model(model)``
    resolves to a fully-qualified ``project.dataset.table`` triple — the
    compiler's :func:`_qualified_table_name` requires both.
    """
    from signalforge.manifest.models import Column

    return Model(
        unique_id="model.proj.orders",
        name="orders",
        resource_type="model",
        package_name="proj",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"ordered_at": Column(name="ordered_at")},
        raw_code="select 1",
    )


def test_row_count_anomaly_by_period_render_test_returns_skip() -> None:
    """``_render_test`` returns :data:`_SKIP` for the anomaly variant —
    singular ``tests/*.sql`` file emission, NOT a YAML block (Phase 1 B.7
    of #171 locks: no dbt-macro form exists for this primitive).
    Mirrors ``custom_sql``.
    """
    test = CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at")
    assert _render_test(test) is _SKIP


def test_row_count_anomaly_by_period_skipped_from_schema_yml() -> None:
    """An anomaly test does NOT appear in the proposed ``schema.yml`` —
    the YAML emitter drops every test that renders to :data:`_SKIP`.
    """
    test = CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at")
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="ordered_at", description="when"),),
        tests=(test,),
    )
    result = _result(_decision(test, test_anchor="model"))

    parsed = yaml.safe_load(emit_proposed_yaml(candidate, result))
    assert "tests" not in parsed["models"][0]


def test_emit_proposed_test_files_anomaly_basic_path_and_marker() -> None:
    """A kept anomaly test yields one proposed ``.sql`` file under the
    ``tests/<model>__row_count_anomaly_by_period_<hash>.sql`` shape, with
    the ``-- signalforge:generated <hash>`` header marker.
    """
    test = CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at")
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="ordered_at", description="when"),),
        tests=(test,),
    )
    result = _result(_decision(test, test_anchor="model"))
    model = _orders_model_for_anomaly()
    as_of = date(2026, 5, 30)

    files = emit_proposed_test_files(candidate, result, model=model, as_of=as_of)

    assert len(files) == 1
    proposed = files[0]
    expected_hash = model_test_args_hash(test)
    assert proposed.path == f"tests/orders__row_count_anomaly_by_period_{expected_hash}.sql"
    assert proposed.sql.startswith(f"{_GENERATED_MARKER_PREFIX} {expected_hash}\n")


def test_emit_proposed_test_files_anomaly_body_is_singular_test_sql() -> None:
    """The emitted SQL body is the FULL band-check SQL from
    :func:`signalforge.prune.compiler._compile_anomaly_singular_test_sql`
    — NOT the engine-side ``_compile_anomaly_violation_query`` (per #171
    Copilot findings #8 / #9). The violation query alone returns ALL rows
    in today's period (broken as a dbt singular test); the singular-test
    SQL combines history + stats CTEs + a band-violation predicate so the
    test returns 0 rows when in-band and >=1 row only when out-of-band.
    """
    test = CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at")
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="ordered_at", description="when"),),
        tests=(test,),
    )
    result = _result(_decision(test, test_anchor="model"))
    model = _orders_model_for_anomaly()
    as_of = date(2026, 5, 30)

    files = emit_proposed_test_files(candidate, result, model=model, as_of=as_of)

    expected_singular_sql = _compile_anomaly_singular_test_sql(
        test,
        TableRef.from_model(model),
        BIGQUERY_DIALECT,
        as_of=as_of,
    )
    # The body (after the header marker + blank line) is exactly the
    # compiler's singular-test SQL plus the trailing newline _with_marker
    # appends.
    expected_hash = model_test_args_hash(test)
    expected_body = f"{_GENERATED_MARKER_PREFIX} {expected_hash}\n\n{expected_singular_sql}\n"
    assert files[0].sql == expected_body
    # Defensive: the OLD violation-query shape must NOT appear in the
    # emitted SQL (regression guard for #171 Copilot findings #8 / #9).
    old_violation = _compile_anomaly_violation_query(
        test, TableRef.from_model(model), BIGQUERY_DIALECT, as_of=as_of
    )
    assert old_violation not in files[0].sql, (
        "emitter is shipping the engine-side violation query (returns ALL "
        "rows in as_of period) as the dbt singular test — that's the bug "
        "Copilot caught at #171 review (findings #8/#9). The emitted SQL "
        "must use the band-check shape that returns 0 rows when in-band."
    )


def test_emit_proposed_test_files_anomaly_filename_uses_args_hash() -> None:
    """The filename's hash suffix is the shared
    :func:`signalforge._common.artifact_id.model_test_args_hash` — two
    anomaly tests with different args produce different filenames; two
    with identical args dedupe.
    """
    test_a = CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at")
    test_b = CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at", period="week")
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="ordered_at", description="when"),),
        tests=(test_a, test_b),
    )
    result = _result(
        _decision(test_a, test_anchor="model"),
        _decision(test_b, test_anchor="model"),
    )
    model = _orders_model_for_anomaly()

    files = emit_proposed_test_files(candidate, result, model=model, as_of=date(2026, 5, 30))

    assert len(files) == 2
    hash_a = model_test_args_hash(test_a)
    hash_b = model_test_args_hash(test_b)
    assert hash_a != hash_b
    paths = {f.path for f in files}
    assert f"tests/orders__row_count_anomaly_by_period_{hash_a}.sql" in paths
    assert f"tests/orders__row_count_anomaly_by_period_{hash_b}.sql" in paths


def test_emit_proposed_test_files_anomaly_excludes_dropped() -> None:
    """A DROPPED anomaly test produces no proposed ``.sql`` file."""
    test = CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at")
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="ordered_at", description="when"),),
        tests=(test,),
    )
    result = _result(
        _decision(test, test_anchor="model", decision="dropped", reason="always-passes")
    )
    model = _orders_model_for_anomaly()

    assert emit_proposed_test_files(candidate, result, model=model, as_of=date(2026, 5, 30)) == ()


def test_emit_proposed_test_files_anomaly_uses_decision_as_of_when_kwarg_omitted() -> None:
    """When the orchestrator omits ``as_of``, the emitter prefers
    ``decision.as_of`` (set by the engine's US-009 resolution) so the
    generated file matches the date the engine evaluated against.
    """
    test = CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at")
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="ordered_at", description="when"),),
        tests=(test,),
    )
    engine_as_of = date(2026, 4, 1)
    decision = PruneDecision(
        test_anchor="model",
        test=test,
        decision="kept",
        reason="kept",
        failures=0,
        sampled_rows=1000,
        scope="full",
        elapsed_ms=42,
        compiled_sql_hash="0" * 16,
        compiled_sql="",
        why="synthetic",
        as_of=engine_as_of,
    )
    result = _result(decision)
    model = _orders_model_for_anomaly()

    files = emit_proposed_test_files(candidate, result, model=model)

    expected_singular_sql = _compile_anomaly_singular_test_sql(
        test, TableRef.from_model(model), BIGQUERY_DIALECT, as_of=engine_as_of
    )
    assert expected_singular_sql in files[0].sql


def test_emit_proposed_test_files_anomaly_kwarg_overrides_decision_as_of() -> None:
    """The ``as_of`` kwarg wins over ``decision.as_of`` — operator can
    re-generate against a different evaluation date.
    """
    test = CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at")
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="ordered_at", description="when"),),
        tests=(test,),
    )
    engine_as_of = date(2026, 4, 1)
    operator_as_of = date(2025, 1, 15)
    decision = PruneDecision(
        test_anchor="model",
        test=test,
        decision="kept",
        reason="kept",
        failures=0,
        sampled_rows=1000,
        scope="full",
        elapsed_ms=42,
        compiled_sql_hash="0" * 16,
        compiled_sql="",
        why="synthetic",
        as_of=engine_as_of,
    )
    result = _result(decision)
    model = _orders_model_for_anomaly()

    files = emit_proposed_test_files(candidate, result, model=model, as_of=operator_as_of)

    expected = _compile_anomaly_singular_test_sql(
        test, TableRef.from_model(model), BIGQUERY_DIALECT, as_of=operator_as_of
    )
    assert expected in files[0].sql
    # The engine's as_of must NOT appear (defensive — confirms the
    # kwarg actually wins).
    engine_sql = _compile_anomaly_singular_test_sql(
        test, TableRef.from_model(model), BIGQUERY_DIALECT, as_of=engine_as_of
    )
    assert engine_sql not in files[0].sql


def test_emit_proposed_test_files_anomaly_raises_without_model() -> None:
    """An anomaly kept decision without a ``model`` kwarg fails loud —
    the violation query cannot be compiled without
    :meth:`TableRef.from_model`. Fail-loud is the right shape here: a
    silent skip would surface the test in the YAML/diff table but
    produce no on-disk file, which the operator would discover only on
    the next ``dbt test`` run.
    """
    test = CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at")
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="ordered_at", description="when"),),
        tests=(test,),
    )
    result = _result(_decision(test, test_anchor="model"))

    with pytest.raises(ValueError, match="row_count_anomaly_by_period"):
        emit_proposed_test_files(candidate, result)


def test_emit_proposed_test_files_anomaly_matches_snapshot_fixture() -> None:
    """Pin the happy-path emission against
    ``tests/fixtures/diff/proposed_test_files/anomaly/`` so a regression
    on the violation-query bytes (compiler change, dialect default
    change, marker shape change) fails loud against the fixture.

    Single-fixture canary: when the contents drift, regenerate by
    re-running this test's setup with ``--force-regen`` or by inspecting
    ``files[0].sql`` against the fixture text.
    """
    from pathlib import Path as _Path

    test = CandidateTestRowCountAnomalyByPeriod(
        date_column="ordered_at",
        period="day",
        lookback_periods=28,
        method="mad",
        threshold=3.0,
        rationale="Detect daily order-volume anomalies in the rolling 28-day window.",
    )
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="ordered_at", description="when"),),
        tests=(test,),
    )
    decision = PruneDecision(
        test_anchor="model",
        test=test,
        decision="kept",
        reason="kept",
        failures=0,
        sampled_rows=1000,
        scope="full",
        elapsed_ms=42,
        compiled_sql_hash="0" * 16,
        compiled_sql="",
        why="detected anomalous daily count",
        as_of=date(2026, 5, 30),
    )
    result = _result(decision)
    model = _orders_model_for_anomaly()

    files = emit_proposed_test_files(candidate, result, model=model, as_of=date(2026, 5, 30))
    assert len(files) == 1
    proposed = files[0]
    fixture_path = (
        _Path(__file__).parent.parent
        / "fixtures"
        / "diff"
        / "proposed_test_files"
        / "anomaly"
        / "orders__row_count_anomaly_by_period_8e4d6245.sql"
    )
    assert fixture_path.exists(), f"fixture missing at {fixture_path}"
    assert proposed.path == f"tests/{fixture_path.name}"
    assert proposed.sql == fixture_path.read_text(encoding="utf-8")


def test_emit_proposed_test_files_anomaly_alongside_custom_sql() -> None:
    """Both singular-SQL variants can land in the same call — emission
    order follows ``prune_result.kept_decisions`` order and both ship as
    proposed files (no dedupe across variants because the hash domains
    are disjoint).
    """
    anomaly = CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at")
    custom = CandidateTestCustomSQL(sql="select 1 where false", column=None)
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="ordered_at", description="when"),),
        tests=(anomaly, custom),
    )
    result = _result(
        _decision(anomaly, test_anchor="model"),
        _decision(custom, test_anchor="model"),
    )
    model = _orders_model_for_anomaly()

    files = emit_proposed_test_files(candidate, result, model=model, as_of=date(2026, 5, 30))

    assert len(files) == 2
    paths = [f.path for f in files]
    anomaly_hash = model_test_args_hash(anomaly)
    custom_hash = model_test_args_hash(custom)
    assert f"tests/orders__row_count_anomaly_by_period_{anomaly_hash}.sql" in paths
    assert f"tests/orders__custom_sql_{custom_hash}.sql" in paths


def test_emit_proposed_test_files_anomaly_skips_hostile_where_clause() -> None:
    """Per #171 CodeRabbit finding #11: the emitter must re-run the
    compiler's safety checks (``validate_identifier`` + ``validate_test_sql``)
    before writing the singular-test SQL to disk. Without this, a kept
    anomaly decision whose ``where`` clause was crafted to break out of
    the SELECT context (stray ``;``, ``--`` comment-out, unbalanced parens)
    could land in operator-shipped dbt SQL. Skipping at the emitter is the
    right call: the engine separately routes the case to
    kept-without-evidence via _InvalidIdentifier; the emitter just refuses
    to write the broken SQL.

    Defensive test — a ``where`` containing a stray ``;`` is the smallest
    payload that trips ``validate_test_sql``. Real-world adversarial input
    would be more elaborate; the gate's job is to refuse anything that
    fails the same checks the engine ran.
    """
    test = CandidateTestRowCountAnomalyByPeriod(
        date_column="ordered_at",
        where="status = 'a'; DROP TABLE orders --",
    )
    candidate = CandidateSchema(
        name="orders",
        description="d",
        columns=(CandidateColumn(name="ordered_at", description="when"),),
        tests=(test,),
    )
    result = _result(_decision(test, test_anchor="model"))
    model = _orders_model_for_anomaly()

    files = emit_proposed_test_files(candidate, result, model=model, as_of=date(2026, 5, 30))

    # The hostile-where test was kept in the prune result but the emitter
    # MUST refuse to write its SQL to disk (validate_test_sql trips).
    assert files == (), (
        "emitter should skip emission when validate_test_sql rejects the "
        "compiled SQL (hostile where clause)"
    )
