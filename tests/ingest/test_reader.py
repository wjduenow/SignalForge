"""Tests for the ``read_schema`` orchestrator (US-005).

Exercises the public ingest entry point end-to-end: the happy path against a
dbt-codegen-shaped fixture (column + model-level tests, all four supported
types, every SkipReason, dedupe, ref() unwrap, both ``tests:`` /
``data_tests:`` keys), the str-vs-Path input contract, and each error path.

Note: ``IngestResult`` is produced in-process and handed to prune; it is NOT
read back from a JSONL/sidecar on disk, so no ``extra="forbid"`` drift
detector is needed here (see ``tests/ingest/test_models.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.draft.models import (
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestUnique,
)
from signalforge.ingest import (
    IngestAnchorContractError,
    IngestModelNotFoundError,
    IngestResult,
    IngestSchemaNotFoundError,
    IngestSchemaParseError,
    IngestSchemaTooLargeError,
    read_schema,
)
from signalforge.ingest.reader import _INGEST_SCHEMA_SIZE_LIMIT_BYTES
from signalforge.manifest.models import Column, Model

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "ingest" / "schema_codegen_shaped.yml"


def _make_orders_model() -> Model:
    """Build the ``orders`` Model whose columns match the fixture exactly."""
    return Model(
        unique_id="model.shop.orders",
        name="orders",
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={
            "order_id": Column(name="order_id"),
            "status": Column(name="status"),
            "customer_id": Column(name="customer_id"),
            "amount": Column(name="amount"),
            "created_at": Column(name="created_at"),
        },
        raw_code="select 1",
    )


def _column(result: IngestResult, name: str):
    matches = [c for c in result.candidate.columns if c.name == name]
    assert len(matches) == 1, f"expected exactly one {name!r} column, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_read_schema_happy_path_supported_tests() -> None:
    result = read_schema(_FIXTURE, _make_orders_model())
    assert isinstance(result, IngestResult)
    assert result.candidate.name == "orders"
    assert result.candidate.description == "Orders fact table."

    # order_id: not_null + unique; the `unique` duplicated under data_tests
    # must dedupe to a single CandidateTest (DEC-008).
    order_id = _column(result, "order_id")
    assert order_id.description == "Surrogate key for the order."
    types = sorted(t.type for t in order_id.tests)
    assert types == ["not_null", "unique"]
    assert sum(isinstance(t, CandidateTestUnique) for t in order_id.tests) == 1
    assert any(isinstance(t, CandidateTestNotNull) for t in order_id.tests)

    # status: accepted_values (inline args).
    status = _column(result, "status")
    av = [t for t in status.tests if isinstance(t, CandidateTestAcceptedValues)]
    assert len(av) == 1
    assert av[0].column == "status"
    assert av[0].values == ("placed", "shipped", "cancelled")

    # customer_id: relationships via arguments:-nested + ref() unwrap (DEC-009).
    customer_id = _column(result, "customer_id")
    rel = [t for t in customer_id.tests if isinstance(t, CandidateTestRelationships)]
    assert len(rel) == 1
    assert rel[0].to == "customers"  # ref('customers') unwrapped
    assert rel[0].field == "id"
    assert rel[0].column == "customer_id"

    # amount + created_at: present as columns with no supported tests.
    assert _column(result, "amount").tests == ()
    created_at = _column(result, "created_at")
    assert created_at.tests == ()
    assert created_at.description == ""  # absent description defaults to "" (DEC-010)


def test_read_schema_happy_path_skipped_tests() -> None:
    result = read_schema(_FIXTURE, _make_orders_model())

    by_name = {s.test_name: s for s in result.skipped}

    # Namespaced custom generic on the status column.
    assert "dbt_utils.unique_combination_of_columns" in by_name
    assert by_name["dbt_utils.unique_combination_of_columns"].reason == "custom-or-generic-test"
    assert by_name["dbt_utils.unique_combination_of_columns"].column == "status"

    # Bare-string unsupported test on the amount column.
    assert "positive" in by_name
    assert by_name["positive"].reason == "unsupported-test-type"
    assert by_name["positive"].column == "amount"

    # Model-level custom generic — column is None.
    assert "dbt_utils.expression_is_true" in by_name
    assert by_name["dbt_utils.expression_is_true"].reason == "custom-or-generic-test"
    assert by_name["dbt_utils.expression_is_true"].column is None


def test_read_schema_does_not_select_unrelated_model() -> None:
    # The fixture also declares `customers`; selecting `orders` must not pull
    # the customers `id` column into the candidate.
    result = read_schema(_FIXTURE, _make_orders_model())
    assert {c.name for c in result.candidate.columns} == {
        "order_id",
        "status",
        "customer_id",
        "amount",
        "created_at",
    }


# ---------------------------------------------------------------------------
# String-content input contract
# ---------------------------------------------------------------------------


def test_read_schema_accepts_raw_yaml_string() -> None:
    raw = _FIXTURE.read_text()
    from_str = read_schema(raw, _make_orders_model())
    from_path = read_schema(_FIXTURE, _make_orders_model())
    # Same candidate + same skip set regardless of input kind.
    assert from_str.candidate == from_path.candidate
    assert from_str.skipped == from_path.skipped


def test_read_schema_string_is_content_not_a_path() -> None:
    # A str that happens to look like a path is treated as YAML content, not
    # read from disk — so it parses (to None) and raises ModelNotFound, never
    # SchemaNotFound.
    with pytest.raises(IngestModelNotFoundError):
        read_schema("/no/such/file.yml", _make_orders_model())


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_read_schema_missing_model_name() -> None:
    model = _make_orders_model().model_copy(update={"name": "nonexistent"})
    with pytest.raises(IngestModelNotFoundError) as excinfo:
        read_schema(_FIXTURE, model)
    assert excinfo.value.model_name == "nonexistent"


def test_read_schema_malformed_yaml() -> None:
    bad = "models: [unterminated\n  - : :"
    with pytest.raises(IngestSchemaParseError):
        read_schema(bad, _make_orders_model())


def test_read_schema_oversize_content() -> None:
    # A valid-shaped doc padded past the byte cap; rejected BEFORE parse.
    padding = "#" + "x" * _INGEST_SCHEMA_SIZE_LIMIT_BYTES
    oversized = f"{padding}\nmodels:\n  - name: orders\n"
    with pytest.raises(IngestSchemaTooLargeError) as excinfo:
        read_schema(oversized, _make_orders_model())
    assert excinfo.value.limit == _INGEST_SCHEMA_SIZE_LIMIT_BYTES
    assert excinfo.value.size > _INGEST_SCHEMA_SIZE_LIMIT_BYTES


def test_read_schema_anchor_contract_violation() -> None:
    # A test referencing a column the model does not have fails loud.
    raw = (
        "models:\n"
        "  - name: orders\n"
        "    columns:\n"
        "      - name: ghost_column\n"
        "        tests:\n"
        "          - not_null\n"
    )
    with pytest.raises(IngestAnchorContractError) as excinfo:
        read_schema(raw, _make_orders_model())
    assert any("ghost_column" in v for v in excinfo.value.violations)


def test_read_schema_nonexistent_path() -> None:
    missing = _FIXTURE.parent / "does_not_exist.yml"
    with pytest.raises(IngestSchemaNotFoundError):
        read_schema(missing, _make_orders_model())


# ---------------------------------------------------------------------------
# Bonus: prune accepts the produced candidate end-to-end without a warehouse
# (disabled-prune path short-circuits before any warehouse call — see
# .claude/rules/prune-engine.md issue #35).
# ---------------------------------------------------------------------------


def test_produced_candidate_is_accepted_by_disabled_prune(tmp_path: Path) -> None:
    from signalforge.manifest.models import Manifest
    from signalforge.prune import PruneConfig, prune_tests

    model = _make_orders_model()
    result = read_schema(_FIXTURE, model)

    class _StubAdapter:
        """Adapter that must never be touched on the disabled-prune path."""

        def __enter__(self):  # pragma: no cover - defensive
            raise AssertionError("adapter entered: disabled prune must short-circuit")

        def __exit__(self, *exc):  # pragma: no cover - defensive
            return False

    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={model.unique_id: model},
    )

    prune_result = prune_tests(
        model,
        _StubAdapter(),  # type: ignore[arg-type]
        result.candidate,
        manifest,
        config=PruneConfig(enabled=False),
        audit_path=tmp_path / "prune.jsonl",
        project_dir=tmp_path,
    )
    # Every candidate routes to kept-without-evidence on the disabled path.
    assert prune_result.model_unique_id == model.unique_id
    assert prune_result.kept_count >= 1


# ---------------------------------------------------------------------------
# Dedupe: reordered accepted_values value sets collapse (QG fix, DEC-008)
# ---------------------------------------------------------------------------


def test_accepted_values_dedupe_is_order_insensitive() -> None:
    # Same value set in different order under tests: and data_tests: must
    # dedupe to ONE CandidateTestAcceptedValues (key uses sorted values).
    yaml_text = """
version: 2
models:
  - name: orders
    columns:
      - name: status
        tests:
          - accepted_values:
              values: ["placed", "shipped"]
        data_tests:
          - accepted_values:
              values: ["shipped", "placed"]
"""
    result = read_schema(yaml_text, _make_orders_model())
    status = _column(result, "status")
    av = [t for t in status.tests if isinstance(t, CandidateTestAcceptedValues)]
    assert len(av) == 1
    assert tuple(sorted(av[0].values)) == ("placed", "shipped")
