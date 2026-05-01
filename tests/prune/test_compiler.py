"""Tests for ``signalforge.prune.compiler`` (US-007).

Pinned snapshot fixtures verify the byte-exact failing-rows SELECT for the
four candidate-test variants (``not_null``, ``unique``, ``accepted_values``,
``relationships``) against the BigQuery dialect. The four NULL-exclusion
checks pin DEC-023 (matches dbt-core verbatim — diverging would cause
prune verdicts to disagree with ``dbt test`` runtime verdicts on the same
model). The escape-hardening tests pin DEC-024 (every adversarial value —
embedded single quotes, backslashes, newlines, ANSI escapes, full SQL
injection attempts — stays inside the quoted string and the wrapped SQL
passes :func:`signalforge.warehouse._sql_safety.validate_test_sql`). The
``relationships`` parent-resolution and dialect-dispatch tests pin
DEC-025 (custom :class:`Dialect` quote_char dispatches without a
warehouse-specific code path) and DEC-026 (manifest-absent parent
returns a sentinel rather than raising). The hash test pins DEC-005
(blake2b-8 / 16-hex-char convention shared with
:mod:`signalforge.draft.audit`).
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
from signalforge.manifest.models import Column, Manifest, Model
from signalforge.prune.compiler import (
    _compile_test,
    _compute_compiled_sql_hash,
    _InvalidIdentifier,
    _RequiresFutureData,
)
from signalforge.warehouse._sql_safety import validate_test_sql
from signalforge.warehouse.models import BIGQUERY_DIALECT, Dialect, PartitionFilter, TableRef

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "prune" / "compiled_sql"


def _read_fixture(name: str) -> str:
    """Read a snapshot fixture file as raw text (no normalisation)."""
    return (_FIXTURES_DIR / name).read_text(encoding="utf-8")


def _make_orders_table_ref() -> TableRef:
    return TableRef(project="fake_project", dataset="dataset", name="orders")


def _make_orders_model() -> Model:
    return Model(
        unique_id="model.shop.orders",
        name="orders",
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"customer_id": Column(name="customer_id")},
        raw_code="select 1",
    )


def _make_customers_model() -> Model:
    return Model(
        unique_id="model.shop.customers",
        name="customers",
        resource_type="model",
        package_name="shop",
        original_file_path="models/customers.sql",
        path="customers.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"id": Column(name="id")},
        raw_code="select 1",
    )


def _make_manifest(*, with_customers: bool = True) -> Manifest:
    nodes: dict[str, Model] = {"model.shop.orders": _make_orders_model()}
    if with_customers:
        nodes["model.shop.customers"] = _make_customers_model()
    return Manifest(metadata={"dbt_schema_version": "v12"}, nodes=nodes)


# ---------------------------------------------------------------------------
# Snapshot tests — pinned byte-exact output for each variant.
# ---------------------------------------------------------------------------


def test_compile_not_null_matches_snapshot() -> None:
    expected = _read_fixture("not_null.sql")
    test = CandidateTestNotNull(column="customer_id")
    actual = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert actual == expected


def test_compile_unique_matches_snapshot() -> None:
    expected = _read_fixture("unique.sql")
    test = CandidateTestUnique(column="customer_id")
    actual = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert actual == expected


def test_compile_accepted_values_matches_snapshot() -> None:
    expected = _read_fixture("accepted_values.sql")
    test = CandidateTestAcceptedValues(column="status", values=("placed", "shipped", "cancelled"))
    actual = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert actual == expected


def test_compile_relationships_matches_snapshot() -> None:
    expected = _read_fixture("relationships.sql")
    test = CandidateTestRelationships(column="customer_id", to="customers", field="id")
    actual = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert actual == expected


# ---------------------------------------------------------------------------
# Sample-mode snapshot tests — pinned byte-exact output for each variant
# wrapped in a deterministic-sample CTE. Confirms the post-PR-#20 review
# wiring threads scope/sample_size/sample_bucket/partition_filter through
# the compiler so ``prune.scope: sample`` actually samples in the SQL.
# ---------------------------------------------------------------------------


def test_compile_not_null_sample_mode_matches_snapshot() -> None:
    expected = _read_fixture("not_null_sample.sql")
    test = CandidateTestNotNull(column="customer_id")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert actual == expected


def test_compile_unique_sample_mode_matches_snapshot() -> None:
    expected = _read_fixture("unique_sample.sql")
    test = CandidateTestUnique(column="customer_id")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert actual == expected


def test_compile_accepted_values_sample_mode_matches_snapshot() -> None:
    expected = _read_fixture("accepted_values_sample.sql")
    test = CandidateTestAcceptedValues(column="status", values=("placed", "shipped", "cancelled"))
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert actual == expected


def test_compile_sample_mode_with_partition_filter_threads_predicate() -> None:
    """``partition_filter`` is rendered into the deterministic-sample CTE
    alongside the hash-mod predicate.

    The orchestrator threads ``PruneConfig.partition_filter`` through
    every per-test compile so the wrapped CTE matches the warehouse
    adapter's :meth:`sample_rows` shape. Required by the warehouse
    adapter for tables ≥ 100M rows.
    """
    test = CandidateTestNotNull(column="customer_id")
    pf = PartitionFilter(column="event_dt", op=">=", value="2026-01-01")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
        partition_filter=pf,
    )
    assert isinstance(actual, str)
    # The CTE carries both the hash-mod predicate AND the partition AND.
    expected_predicate = (
        "MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), 10) < 1 AND `event_dt` >= '2026-01-01'"
    )
    assert expected_predicate in actual
    # Test still runs against the sample alias.
    assert "FROM sample WHERE `customer_id` IS NULL" in actual


def test_compile_full_mode_with_partition_filter_wraps_table_in_subquery() -> None:
    """``scope="full"`` + ``partition_filter`` composes via derived table.

    The test runs against a partition-filtered subquery rather than the
    raw table. Composing via subquery (rather than editing the per-test
    WHERE clause) is uniform across all four test shapes so the wrapper
    stays a true wrapper.
    """
    test = CandidateTestNotNull(column="customer_id")
    pf = PartitionFilter(column="event_dt", op=">=", value="2026-01-01")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        scope="full",
        partition_filter=pf,
    )
    assert isinstance(actual, str)
    # The original table was wrapped in a derived-table partition filter.
    assert (
        "FROM (SELECT * FROM `fake_project.dataset.orders` WHERE `event_dt` >= '2026-01-01')"
        in actual
    )
    # No CTE — full mode does not wrap with WITH sample.
    assert "WITH sample" not in actual


def test_compile_full_mode_no_partition_filter_emits_unwrapped_sql() -> None:
    """``scope="full"`` + no partition_filter is byte-identical to the
    legacy unwrapped output. Snapshot fixtures pin the unwrapped shape.
    """
    test = CandidateTestNotNull(column="customer_id")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        scope="full",
        partition_filter=None,
    )
    assert actual == _read_fixture("not_null.sql")


def test_compile_relationships_sample_mode_matches_snapshot() -> None:
    """Sample-mode relationships samples the CHILD table only.

    The parent stays at full so an orphan detected in the child sample
    is not a false positive caused by the parent's missing-from-sample
    row. The pinned fixture asserts the WITH sample CTE wraps the child
    only; the LEFT JOIN target stays at the qualified parent table.
    """
    expected = _read_fixture("relationships_sample.sql")
    test = CandidateTestRelationships(column="customer_id", to="customers", field="id")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert actual == expected
    # Belt-and-braces: the parent table is NOT sampled — verify the
    # full-qualified parent identifier survives the wrap.
    assert isinstance(actual, str)
    assert "LEFT JOIN `fake_project.dataset.customers` AS parent" in actual


# ---------------------------------------------------------------------------
# DEC-023: NULL-exclusion conventions match dbt-core.
# ---------------------------------------------------------------------------


def test_unique_sql_includes_null_exclusion() -> None:
    test = CandidateTestUnique(column="customer_id")
    sql = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(sql, str)
    assert "IS NOT NULL" in sql


def test_accepted_values_sql_includes_null_exclusion() -> None:
    test = CandidateTestAcceptedValues(column="status", values=("a",))
    sql = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(sql, str)
    assert "IS NOT NULL" in sql
    assert "NOT IN" in sql


def test_relationships_sql_includes_child_null_exclusion() -> None:
    test = CandidateTestRelationships(column="customer_id", to="customers", field="id")
    sql = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(sql, str)
    # Child-side: filter rows where the foreign key is non-null (orphan candidates).
    assert "child.`customer_id` IS NOT NULL" in sql
    # Parent-side: the orphan check looks for parents that don't exist.
    assert "parent.`id` IS NULL" in sql


# ---------------------------------------------------------------------------
# DEC-024: accepted_values value escaping (every adversarial input stays inside
# the quoted literal; the wrapped SQL passes validate_test_sql).
# ---------------------------------------------------------------------------


def test_accepted_values_escapes_single_quote() -> None:
    test = CandidateTestAcceptedValues(column="name", values=("O'Brien",))
    sql = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(sql, str)
    # The escaped form is `O\'Brien` wrapped in single quotes: 'O\'Brien'.
    assert r"'O\'Brien'" in sql


def test_accepted_values_escapes_backslash() -> None:
    test = CandidateTestAcceptedValues(column="name", values=("a\\b",))
    sql = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(sql, str)
    # Backslash gets doubled; expect the literal `a\\b` (four chars in the
    # rendered SQL: 'a', '\\', '\\', 'b') wrapped in single quotes.
    assert "'a\\\\b'" in sql


def test_accepted_values_escapes_newline() -> None:
    test = CandidateTestAcceptedValues(column="name", values=("a\nb",))
    sql = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(sql, str)
    # The newline should NOT survive raw — it'd terminate the literal in BQ
    # standard SQL. escape_bq_string_literal turns it into `\n` (two chars).
    assert "\n" not in sql
    assert "'a\\nb'" in sql
    # The wrapped SQL (mirroring the adapter's run_test_sql wrap) must
    # pass the cheap-rejects validator.
    wrapped = f"SELECT COUNT(*) FROM ({sql}) AS t"
    validate_test_sql(wrapped)  # raises if rejected


def test_accepted_values_escapes_ansi() -> None:
    test = CandidateTestAcceptedValues(column="name", values=("\x1b[31mfoo",))
    sql = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(sql, str)
    # The ANSI-bytes string is opaque inside the BQ literal; the wrapped
    # SQL must still pass the cheap-rejects validator.
    wrapped = f"SELECT COUNT(*) FROM ({sql}) AS t"
    validate_test_sql(wrapped)  # raises if rejected


def test_accepted_values_escapes_sql_injection_attempt() -> None:
    """Verify the entire injection attempt stays inside the quoted literal.

    Strip the single-quoted segments out of the rendered SQL and confirm
    no ``;`` or ``--`` survives in the residue. Then pass the wrapped SQL
    through the adapter-level validator and confirm it does NOT raise.
    """
    payload = "; DROP TABLE x;--"
    test = CandidateTestAcceptedValues(column="name", values=(payload,))
    sql = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(sql, str)

    # Strip every single-quoted literal segment. The residue is the
    # SQL skeleton without any user-supplied bytes.
    residue: list[str] = []
    in_literal = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            if in_literal and i + 1 < len(sql) and sql[i + 1] == "'":
                # Doubled quote escape — stay inside the literal.
                i += 2
                continue
            in_literal = not in_literal
            i += 1
            continue
        if not in_literal:
            residue.append(ch)
        i += 1
    residue_str = "".join(residue)
    assert ";" not in residue_str
    assert "--" not in residue_str

    # Wrap the way the adapter does and confirm the cheap-rejects pass.
    wrapped = f"SELECT COUNT(*) FROM ({sql}) AS t"
    validate_test_sql(wrapped)


# ---------------------------------------------------------------------------
# DEC-026: relationships(to: unknown) returns a sentinel (not a string).
# ---------------------------------------------------------------------------


def test_relationships_returns_requires_future_data_when_parent_missing() -> None:
    test = CandidateTestRelationships(column="customer_id", to="nonexistent_model", field="id")
    result = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(with_customers=False),
    )
    assert isinstance(result, _RequiresFutureData)
    assert "nonexistent_model" in result.reason


def test_relationships_returns_requires_future_data_when_parent_ambiguous() -> None:
    """Multiple manifest models sharing ``Model.name`` (e.g. cross-package
    name collision) cannot be silently disambiguated by the compiler.

    Picking the first match would let the prune layer issue a wrong-table
    join — exactly the silent-failure mode the ``requires-future-data``
    branch exists to prevent. The reason text quantifies the ambiguity
    so the reviewer sees how many parents matched.
    """
    customers_a = Model(
        unique_id="model.shop_a.customers",
        name="customers",
        resource_type="model",
        package_name="shop_a",
        original_file_path="models/customers.sql",
        path="customers.sql",
        database="fake_project",
        schema="dataset_a",  # type: ignore[call-arg]
        columns={"id": Column(name="id")},
        raw_code="select 1",
    )
    customers_b = Model(
        unique_id="model.shop_b.customers",
        name="customers",
        resource_type="model",
        package_name="shop_b",
        original_file_path="models/customers.sql",
        path="customers.sql",
        database="fake_project",
        schema="dataset_b",  # type: ignore[call-arg]
        columns={"id": Column(name="id")},
        raw_code="select 1",
    )
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={
            "model.shop.orders": _make_orders_model(),
            "model.shop_a.customers": customers_a,
            "model.shop_b.customers": customers_b,
        },
    )
    test = CandidateTestRelationships(column="customer_id", to="customers", field="id")
    result = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, manifest)
    assert isinstance(result, _RequiresFutureData)
    assert "ambiguous" in result.reason
    assert "2 models" in result.reason


def test_relationships_resolves_parent_via_manifest_name_lookup() -> None:
    """Manifest indexes by ``unique_id``; ``to`` is a model ``name``.

    The lookup must scan ``manifest.nodes.values()`` to match
    ``model.name == to`` rather than a direct unique_id key lookup.
    """
    test = CandidateTestRelationships(column="customer_id", to="customers", field="id")
    result = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(result, str)
    # Parent table is rendered as `fake_project.dataset.customers` (drawn
    # from the parent model's database / schema_ / name).
    assert "`fake_project.dataset.customers`" in result


# ---------------------------------------------------------------------------
# DEC-025: dispatch on Dialect.quote_char (v0.2 readiness).
# ---------------------------------------------------------------------------


def test_compiler_dispatches_on_dialect_quote_char() -> None:
    """Custom :class:`Dialect` with ``quote_char='"'`` produces double-quoted SQL.

    v0.1 ships only :data:`BIGQUERY_DIALECT` (backtick-quoted); this
    test pins the dispatch so a v0.2 Snowflake / Postgres dialect drops
    in without compiler changes (DEC-025).
    """
    snowflake_like = Dialect(
        name="snowflake_like",
        supports_tablesample=True,
        supports_qualify=True,
        quote_char='"',
        identifier_case="preserve",
    )
    test = CandidateTestNotNull(column="customer_id")
    sql = _compile_test(test, _make_orders_table_ref(), snowflake_like, _make_manifest())
    assert isinstance(sql, str)
    assert '"customer_id"' in sql
    assert '"fake_project.dataset.orders"' in sql
    # No backticks anywhere — the BQ-specific quote should not appear.
    assert "`" not in sql


# ---------------------------------------------------------------------------
# Determinism + hash convention.
# ---------------------------------------------------------------------------


def test_compile_is_deterministic() -> None:
    test = CandidateTestAcceptedValues(column="status", values=("placed", "shipped", "cancelled"))
    a = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    b = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert a == b


def test_compute_compiled_sql_hash_is_blake2b_8() -> None:
    """16-hex-char digest from blake2b with ``digest_size=8``.

    Pinned expected value lets a regression in the hash-domain choice
    surface immediately (DEC-005 — must match draft.audit's convention).
    """
    sql = "SELECT 1"
    digest = _compute_compiled_sql_hash(sql)
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)
    # Pinned: blake2b("SELECT 1", digest_size=8).hexdigest()
    assert digest == "60e6b55ff57fdf38"


def test_compute_compiled_sql_hash_distinguishes_distinct_inputs() -> None:
    a = _compute_compiled_sql_hash("SELECT 1")
    b = _compute_compiled_sql_hash("SELECT 2")
    assert a != b


# ---------------------------------------------------------------------------
# DEC-013: identifier validation runs at TableRef construction, not in the
# compiler. Confirm by asserting that an invalid identifier raises at
# TableRef construction time and never reaches the compiler.
# ---------------------------------------------------------------------------


def test_validate_identifier_already_done_at_table_ref_construction() -> None:
    """The compiler trusts ``TableRef`` — invalid identifiers fail upstream.

    Confirms the safety boundary is at :class:`TableRef` construction
    (per DEC-013), not in the compiler. A would-be SQL-injection
    identifier never reaches :func:`_compile_test`; the constructor
    rejects it first.
    """
    from signalforge.warehouse.errors import InvalidIdentifierError

    with pytest.raises(InvalidIdentifierError):
        TableRef(project="fake_project", dataset="dataset", name="orders; DROP TABLE x")


# ---------------------------------------------------------------------------
# QG fix-up: defence-in-depth — adversarial column/field identifiers on
# CandidateTest variants return _InvalidIdentifier rather than producing
# a malformed SQL string.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_column",
    [
        "col with space",
        'col"-injection',
        "col`-backtick",
        "col;DROP",
    ],
)
def test_compile_not_null_rejects_adversarial_column(bad_column: str) -> None:
    """``CandidateTestNotNull.column`` failing the SQL-identifier shape
    check returns ``_InvalidIdentifier`` rather than producing SQL that
    breaks out of the backtick quoting."""
    test = CandidateTestNotNull(column=bad_column)
    result = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(result, _InvalidIdentifier)
    assert "invalid identifier" in result.reason


@pytest.mark.parametrize(
    "bad_column",
    [
        "col with space",
        'col"-injection',
        "col`-backtick",
        "col;DROP",
    ],
)
def test_compile_unique_rejects_adversarial_column(bad_column: str) -> None:
    test = CandidateTestUnique(column=bad_column)
    result = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(result, _InvalidIdentifier)


@pytest.mark.parametrize(
    "bad_column",
    [
        "col with space",
        'col"-injection',
        "col`-backtick",
        "col;DROP",
    ],
)
def test_compile_accepted_values_rejects_adversarial_column(bad_column: str) -> None:
    test = CandidateTestAcceptedValues(column=bad_column, values=("a",))
    result = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(result, _InvalidIdentifier)


@pytest.mark.parametrize(
    "bad_field",
    [
        "field with space",
        'field"-injection',
        "field`-backtick",
        "field;DROP",
    ],
)
def test_compile_relationships_rejects_adversarial_field(bad_field: str) -> None:
    """``CandidateTestRelationships.field`` is interpolated into a
    backtick-quoted identifier; an adversarial value must short-circuit
    via ``_InvalidIdentifier`` rather than reach the SQL string."""
    test = CandidateTestRelationships(column="customer_id", to="customers", field=bad_field)
    result = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(result, _InvalidIdentifier)
    # The field name is what's offending; the reason should reflect that.
    assert "field" in result.reason


def test_compile_relationships_rejects_adversarial_column() -> None:
    test = CandidateTestRelationships(
        column="child`-backtick",
        to="customers",
        field="id",
    )
    result = _compile_test(test, _make_orders_table_ref(), BIGQUERY_DIALECT, _make_manifest())
    assert isinstance(result, _InvalidIdentifier)
    assert "column" in result.reason
