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
    CandidateTestCustomSQL,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestUnique,
)
from signalforge.manifest.models import Column, Manifest, Model, Source
from signalforge.prune.compiler import (
    _compile_test,
    _compute_compiled_sql_hash,
    _InvalidIdentifier,
    _qualified_table_name,
    _quote,
    _RequiresFutureData,
)
from signalforge.warehouse._sql_safety import validate_test_sql
from signalforge.warehouse.models import (
    BIGQUERY_DIALECT,
    POSTGRES_DIALECT,
    SNOWFLAKE_DIALECT,
    Dialect,
    PartitionFilter,
    TableRef,
)

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "prune" / "compiled_sql"
_SNOWFLAKE_FIXTURES_DIR = _FIXTURES_DIR / "snowflake"


def _read_fixture(name: str) -> str:
    """Read a snapshot fixture file as raw text (no normalisation)."""
    return (_FIXTURES_DIR / name).read_text(encoding="utf-8")


def _read_snowflake_fixture(name: str) -> str:
    """Read a Snowflake snapshot fixture file as raw text (no normalisation)."""
    return (_SNOWFLAKE_FIXTURES_DIR / name).read_text(encoding="utf-8")


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


def _make_manifest_with_source() -> Manifest:
    """A manifest carrying a ``raw.events`` source so a custom_sql test can
    resolve ``{{ source('raw', 'events') }}`` to a qualified name."""
    return Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={"model.shop.orders": _make_orders_model()},
        sources={
            "source.shop.raw.events": Source(
                unique_id="source.shop.raw.events",
                source_name="raw",
                name="events",
                resource_type="source",
                database="fake_project",
                schema="raw_dataset",  # type: ignore[call-arg]
                identifier="events",
            )
        },
    )


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


# ---------------------------------------------------------------------------
# US-007: custom_sql (singular business-rule) test compilation.
#
# DEC-003 — arbitrary failing-rows SELECT; DEC-004 — bounded Jinja resolution;
# DEC-006 — single-table sample-CTE vs multi-table full-scan; DEC-008 — SQL
# safety pre-flight on the resolved SQL; DEC-009 — deterministic compiled
# envelope. Snapshot fixtures pin the byte-exact output.
# ---------------------------------------------------------------------------


def test_compile_custom_sql_single_table_full_matches_snapshot() -> None:
    """Single-table custom_sql, scope=full, no partition filter: the
    resolved SQL is returned unchanged (the adapter wraps it with the
    ``COUNT(*)`` envelope, like the four built-ins)."""
    expected = _read_fixture("custom_sql.sql")
    test = CandidateTestCustomSQL(sql="select order_id from {{ this }} where total < 0")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
    )
    assert actual == expected


def test_compile_custom_sql_single_table_sample_matches_snapshot() -> None:
    """Single-table custom_sql, scope=sample: the model's own qualified
    table name is substituted with the ``sample`` CTE alias and the
    deterministic-sample CTE is prepended (mirrors the built-ins)."""
    expected = _read_fixture("custom_sql_sample.sql")
    test = CandidateTestCustomSQL(sql="select order_id from {{ this }} where total < 0")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert actual == expected


def test_compile_custom_sql_multi_table_full_scan_matches_snapshot() -> None:
    """A custom_sql test with a JOIN runs full-scan (unsampled) even when
    scope=sample is requested — sampling one side of a join is semantically
    wrong (DEC-006). Both {{ this }} and {{ ref() }} resolve to qualified
    names in the compiled bytes."""
    expected = _read_fixture("custom_sql_fullscan.sql")
    test = CandidateTestCustomSQL(
        sql=(
            "select o.order_id from {{ this }} as o "
            "join {{ ref('customers') }} as c on o.customer_id = c.id "
            "where c.id is null"
        )
    )
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
        # Even with sample requested, the JOIN forces full-scan.
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert actual == expected
    # No CTE — the join ran full-scan.
    assert isinstance(actual, str)
    assert "WITH sample" not in actual


def test_compile_custom_sql_resolves_source_into_compiled_bytes() -> None:
    """``{{ source('s', 't') }}`` resolves to the source's qualified name."""
    src_manifest = _make_manifest_with_source()
    test = CandidateTestCustomSQL(
        sql="select id from {{ source('raw', 'events') }} where id is null"
    )
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        src_manifest,
        model=_make_orders_model(),
    )
    assert isinstance(actual, str)
    # The source resolved to its qualified name; no Jinja survives.
    assert "{{" not in actual and "}}" not in actual
    assert "events" in actual


def test_compile_custom_sql_full_scan_with_partition_filter_wraps_own_table() -> None:
    """Multi-table full-scan applies a partition filter to the model's own
    table via a derived-table subquery; the joined parent is untouched."""
    test = CandidateTestCustomSQL(
        sql=(
            "select o.order_id from {{ this }} as o "
            "join {{ ref('customers') }} as c on o.customer_id = c.id "
            "where c.id is null"
        )
    )
    pf = PartitionFilter(column="event_dt", op=">=", value="2026-01-01")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
        scope="full",
        partition_filter=pf,
    )
    assert isinstance(actual, str)
    assert "(SELECT * FROM fake_project.dataset.orders WHERE `event_dt` >= '2026-01-01')" in actual
    # The joined parent table is NOT wrapped.
    assert "fake_project.dataset.customers as c" in actual


def test_compile_custom_sql_single_table_full_with_partition_filter_wraps_own_table() -> None:
    """Single-table custom_sql, scope=full, with a partition_filter: the
    model's own table is wrapped in a partition-filtered derived table
    (uniform with the built-ins). ``table_ref`` IS the model's own table
    (oneshot / full-strategy path), so no temp-table substitution happens —
    only the partition-filter derived-table wrap (compiler.py lines 718-722)."""
    test = CandidateTestCustomSQL(sql="select order_id from {{ this }} where total < 0")
    pf = PartitionFilter(column="event_dt", op=">=", value="2026-01-01")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
        scope="full",
        partition_filter=pf,
    )
    assert isinstance(actual, str)
    # The model's own table is replaced with a partition-filtered subquery.
    assert "(SELECT * FROM fake_project.dataset.orders WHERE `event_dt` >= '2026-01-01')" in actual
    # The original bare reference is gone; the predicate body survives.
    assert "where total < 0" in actual
    # No sample CTE — scope is full, not sample.
    assert "WITH sample" not in actual


def test_compile_custom_sql_sample_without_own_table_fails_closed() -> None:
    """Item-4 regression (single-table sample path): a single-table custom_sql
    whose resolved SQL never names the model's own table (e.g. it references
    only ``{{ ref('customers') }}``) cannot be bound to the deterministic
    sample CTE. The compiler must fail closed (``_InvalidIdentifier`` →
    kept-without-evidence) rather than full-scanning the wrong table."""
    test = CandidateTestCustomSQL(sql="select id from {{ ref('customers') }} where id is null")
    result = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert isinstance(result, _InvalidIdentifier)
    assert "does not reference the model's own table" in result.reason


def test_compile_custom_sql_materialised_without_own_table_fails_closed() -> None:
    """Item-4 regression (materialised-substitution path): under
    ``sample_strategy='materialised'`` the orchestrator passes the temp
    sample table as ``table_ref`` (≠ the model's own qualified name) and
    compiles as effective ``scope='full'``. A custom_sql whose resolved SQL
    never names the model's own table has nothing to rewrite to the sample,
    so running it as-is would read the wrong/source table. The compiler must
    fail closed rather than issue a warehouse query against the wrong table."""
    materialised_ref = TableRef(project=None, dataset="_SESSION", name="_sf_sample_deadbeef")
    test = CandidateTestCustomSQL(sql="select id from {{ ref('customers') }} where id is null")
    result = _compile_test(
        test,
        materialised_ref,
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
        scope="full",
    )
    assert isinstance(result, _InvalidIdentifier)
    assert "does not reference the model's own table" in result.reason


def test_compile_custom_sql_sample_missing_sample_args_raises() -> None:
    """``scope='sample'`` for a single-table custom_sql requires both
    ``sample_size`` and ``sample_bucket`` — the orchestrator computes these
    before calling. Absent them, the compiler raises ``ValueError`` rather
    than silently full-scanning (compiler.py line 682)."""
    test = CandidateTestCustomSQL(sql="select order_id from {{ this }} where total < 0")
    with pytest.raises(ValueError, match="requires both sample_size and sample_bucket"):
        _compile_test(
            test,
            _make_orders_table_ref(),
            BIGQUERY_DIALECT,
            _make_manifest(),
            model=_make_orders_model(),
            scope="sample",
            sample_size=None,
            sample_bucket=None,
        )


def test_compile_custom_sql_unsupported_jinja_returns_sentinel() -> None:
    """Control-flow Jinja (``{% if %}``) the bounded resolver can't evaluate
    routes to ``_InvalidIdentifier`` (conservative-bias: kept-without-evidence),
    never raises out of the compiler (DEC-006)."""
    test = CandidateTestCustomSQL(sql="select 1 from {{ this }} {% if true %}where 1=1{% endif %}")
    result = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
    )
    assert isinstance(result, _InvalidIdentifier)
    assert "Jinja" in result.reason


def test_compile_custom_sql_var_lookup_returns_sentinel() -> None:
    """``{{ var(...) }}`` is unsupported → ``_InvalidIdentifier`` sentinel."""
    test = CandidateTestCustomSQL(sql="select 1 from {{ this }} where x > {{ var('threshold') }}")
    result = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
    )
    assert isinstance(result, _InvalidIdentifier)


def test_compile_custom_sql_safety_reject_returns_sentinel() -> None:
    """A resolved SQL that fails the ``validate_test_sql`` pre-flight
    (here: an embedded ``;``) routes to ``_InvalidIdentifier`` rather than
    producing SQL the adapter would reject (DEC-008)."""
    test = CandidateTestCustomSQL(sql="select 1 from {{ this }}; drop table x")
    result = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
    )
    assert isinstance(result, _InvalidIdentifier)
    assert "safety" in result.reason


def test_compile_custom_sql_without_model_returns_sentinel() -> None:
    """When ``model`` is not threaded through, custom_sql can't be resolved
    and routes to ``_InvalidIdentifier`` (defensive — the engine always
    passes ``model=model``)."""
    test = CandidateTestCustomSQL(sql="select order_id from {{ this }} where total < 0")
    result = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=None,
    )
    assert isinstance(result, _InvalidIdentifier)


def test_compile_custom_sql_is_deterministic() -> None:
    test = CandidateTestCustomSQL(sql="select order_id from {{ this }} where total < 0")
    a = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
    )
    b = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
    )
    assert a == b


# ---------------------------------------------------------------------------
# US-019: a custom_sql whose ref()/source() target the resolver can't resolve
# must NEVER propagate a ManifestError out of the compiler. RefNotFoundError /
# SourceNotFoundError → _RequiresFutureData (the dependency simply isn't built
# yet — mirrors the relationships missing-target precedent, DEC-026);
# AmbiguousRefError → _InvalidIdentifier (genuine user ambiguity, not future
# data). Compilation stays total (DEC-006); DropReason stays the locked
# 5-value Literal (no 6th).
# ---------------------------------------------------------------------------


def _make_other_orders_model() -> Model:
    """A second model also named ``orders`` in a different package, so a bare
    ``{{ ref('orders') }}`` is ambiguous across packages."""
    return Model(
        unique_id="model.other.orders",
        name="orders",
        resource_type="model",
        package_name="other",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"customer_id": Column(name="customer_id")},
        raw_code="select 1",
    )


def test_compile_custom_sql_unresolvable_ref_returns_requires_future_data() -> None:
    """``{{ ref('does_not_exist') }}`` raises ``RefNotFoundError`` inside the
    bounded resolver; the compiler catches it and returns a
    ``_RequiresFutureData`` sentinel rather than letting the error propagate
    (which would crash ``prune_tests``). The orchestrator routes the sentinel
    to ``requires-future-data``."""
    test = CandidateTestCustomSQL(
        sql="select o.id from {{ this }} o join {{ ref('does_not_exist') }} d on o.id = d.id"
    )
    result = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
    )
    assert isinstance(result, _RequiresFutureData)
    assert "manifest-absent" in result.reason
    assert "RefNotFoundError" in result.reason


def test_compile_custom_sql_unresolvable_source_returns_requires_future_data() -> None:
    """``{{ source('raw', 'missing') }}`` raises ``SourceNotFoundError``; the
    compiler routes it to ``_RequiresFutureData`` (the source isn't defined
    yet), never raising out of the compiler."""
    test = CandidateTestCustomSQL(
        sql="select id from {{ this }} t join {{ source('raw', 'missing') }} s on t.id = s.id"
    )
    result = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        _make_manifest_with_source(),
        model=_make_orders_model(),
    )
    assert isinstance(result, _RequiresFutureData)
    assert "SourceNotFoundError" in result.reason


def test_compile_custom_sql_ambiguous_ref_returns_invalid_identifier() -> None:
    """A bare ``{{ ref('orders') }}`` that matches two packages raises
    ``AmbiguousRefError`` — genuine user ambiguity, not future data — so the
    compiler routes it to ``_InvalidIdentifier`` (kept-without-evidence),
    never raising out of the compiler."""
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={
            "model.shop.orders": _make_orders_model(),
            "model.other.orders": _make_other_orders_model(),
        },
    )
    test = CandidateTestCustomSQL(
        sql="select a.id from {{ this }} a join {{ ref('orders') }} b on a.id = b.id"
    )
    result = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        manifest,
        model=_make_orders_model(),
    )
    assert isinstance(result, _InvalidIdentifier)
    assert "ambiguous" in result.reason
    assert "AmbiguousRefError" in result.reason


# ---------------------------------------------------------------------------
# US-002 (#121): the compiler consumes the four new Dialect fragment fields +
# identifier_case so it emits Snowflake-correct SQL purely from the Dialect —
# no branching on dialect name. BigQuery output stays byte-identical (the 11
# snapshot tests above are the regression gate).
# ---------------------------------------------------------------------------


def test_quote_folds_and_quotes_for_snowflake() -> None:
    """``identifier_case='upper'`` folds the token to UPPER before wrapping
    in the Snowflake quote char (DEC-003)."""
    assert _quote("customer_id", SNOWFLAKE_DIALECT) == '"CUSTOMER_ID"'


def test_quote_preserves_and_backticks_for_bigquery() -> None:
    """BigQuery ``identifier_case='preserve'`` is a no-op fold; the token is
    wrapped in backticks unchanged — keeps the snapshots byte-identical."""
    assert _quote("customer_id", BIGQUERY_DIALECT) == "`customer_id`"


def test_quote_folds_lower_for_postgres_dialect() -> None:
    """``identifier_case='lower'`` folds the token to lower-case before
    wrapping in the quote char (DEC-003). POSTGRES_DIALECT is the lower-folding
    dialect; a mixed-case identifier exercises the fold rather than a no-op."""
    assert _quote("CustomerId", POSTGRES_DIALECT) == '"customerid"'


def test_qualified_table_name_per_component_for_snowflake() -> None:
    """Snowflake quotes each component separately and folds to UPPER so a
    dotted path is not read as one literal identifier named
    ``db.schema.table`` (DEC-002)."""
    ref = TableRef(project="prod_db", dataset="sch", name="orders")
    assert _qualified_table_name(ref, SNOWFLAKE_DIALECT) == '"PROD_DB"."SCH"."ORDERS"'


def test_qualified_table_name_per_component_two_part_for_snowflake() -> None:
    """``project=None`` yields a two-part per-component qualified name."""
    ref = TableRef(project=None, dataset="sch", name="orders")
    assert _qualified_table_name(ref, SNOWFLAKE_DIALECT) == '"SCH"."ORDERS"'


def test_qualified_table_name_whole_path_for_bigquery_unchanged() -> None:
    """BigQuery wraps the whole dotted path in one backtick pair — byte-
    identical to the legacy behaviour."""
    ref = TableRef(project="fake_project", dataset="dataset", name="orders")
    assert _qualified_table_name(ref, BIGQUERY_DIALECT) == "`fake_project.dataset.orders`"


def test_snowflake_sample_cte_uses_hash_not_farm_fingerprint() -> None:
    """The Snowflake sample predicate is rendered from
    ``sample_row_hash_expr`` → ``MOD(ABS(HASH(*)), <bucket>) < 1``; the
    BigQuery ``FARM_FINGERPRINT`` form never appears (DEC-002)."""
    test = CandidateTestNotNull(column="customer_id")
    sql = _compile_test(
        test,
        _make_orders_table_ref(),
        SNOWFLAKE_DIALECT,
        _make_manifest(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert isinstance(sql, str)
    assert "MOD(ABS(HASH(*)), 10) < 1" in sql
    assert "FARM_FINGERPRINT" not in sql
    # Folded + per-component quoted identifiers throughout; no backticks.
    assert '"CUSTOMER_ID"' in sql
    assert "`" not in sql


def test_snowflake_datetime_partition_filter_uses_cast_form() -> None:
    """A ``datetime`` partition filter under Snowflake renders the
    ``'...'::TIMESTAMP`` cast form, not BigQuery's ``TIMESTAMP('...')``
    function form (DEC-002)."""
    from datetime import datetime

    test = CandidateTestNotNull(column="customer_id")
    pf = PartitionFilter(column="event_ts", op=">=", value=datetime(2026, 1, 1, 0, 0, 0))
    sql = _compile_test(
        test,
        _make_orders_table_ref(),
        SNOWFLAKE_DIALECT,
        _make_manifest(),
        scope="full",
        partition_filter=pf,
    )
    assert isinstance(sql, str)
    assert "'2026-01-01T00:00:00'::TIMESTAMP" in sql
    assert "TIMESTAMP(" not in sql
    # The partition column is folded + quoted the Snowflake way.
    assert '"EVENT_TS" >=' in sql


def test_snowflake_date_partition_filter_uses_cast_form() -> None:
    """A ``date`` partition filter under Snowflake renders ``'...'::DATE``."""
    from datetime import date as _date

    test = CandidateTestNotNull(column="customer_id")
    pf = PartitionFilter(column="event_dt", op=">=", value=_date(2026, 1, 1))
    sql = _compile_test(
        test,
        _make_orders_table_ref(),
        SNOWFLAKE_DIALECT,
        _make_manifest(),
        scope="full",
        partition_filter=pf,
    )
    assert isinstance(sql, str)
    assert "'2026-01-01'::DATE" in sql
    assert "DATE(" not in sql


def test_snowflake_relationships_per_component_and_folded() -> None:
    """End-to-end relationships under Snowflake: both child and parent
    tables are per-component quoted + UPPER-folded; columns folded+quoted."""
    test = CandidateTestRelationships(column="customer_id", to="customers", field="id")
    sql = _compile_test(test, _make_orders_table_ref(), SNOWFLAKE_DIALECT, _make_manifest())
    assert isinstance(sql, str)
    assert '"FAKE_PROJECT"."DATASET"."ORDERS"' in sql
    assert '"FAKE_PROJECT"."DATASET"."CUSTOMERS"' in sql
    assert 'child."CUSTOMER_ID"' in sql
    assert 'parent."ID"' in sql
    assert "`" not in sql


# ---------------------------------------------------------------------------
# US-003 (#121): byte-exact Snowflake snapshot fixtures + tests for all four
# built-in test types (full + sample modes) and custom_sql (single-table full,
# single-table sample, multi-table full-scan). These mirror the BigQuery
# snapshot set but assert against ``tests/fixtures/prune/compiled_sql/snowflake/``.
# Fixtures are captured from real compiler output with SNOWFLAKE_DIALECT — they
# pin the ``"``-quoted, per-component-qualified, UPPER-folded, ``HASH(*)``-sample
# shape (DEC-002, DEC-003).
# ---------------------------------------------------------------------------


def test_compile_not_null_snowflake_matches_snapshot() -> None:
    expected = _read_snowflake_fixture("not_null.sql")
    test = CandidateTestNotNull(column="customer_id")
    actual = _compile_test(test, _make_orders_table_ref(), SNOWFLAKE_DIALECT, _make_manifest())
    assert actual == expected


def test_compile_unique_snowflake_matches_snapshot() -> None:
    expected = _read_snowflake_fixture("unique.sql")
    test = CandidateTestUnique(column="customer_id")
    actual = _compile_test(test, _make_orders_table_ref(), SNOWFLAKE_DIALECT, _make_manifest())
    assert actual == expected


def test_compile_accepted_values_snowflake_matches_snapshot() -> None:
    expected = _read_snowflake_fixture("accepted_values.sql")
    test = CandidateTestAcceptedValues(column="status", values=("placed", "shipped", "cancelled"))
    actual = _compile_test(test, _make_orders_table_ref(), SNOWFLAKE_DIALECT, _make_manifest())
    assert actual == expected


def test_compile_relationships_snowflake_matches_snapshot() -> None:
    expected = _read_snowflake_fixture("relationships.sql")
    test = CandidateTestRelationships(column="customer_id", to="customers", field="id")
    actual = _compile_test(test, _make_orders_table_ref(), SNOWFLAKE_DIALECT, _make_manifest())
    assert actual == expected


def test_compile_not_null_sample_snowflake_matches_snapshot() -> None:
    expected = _read_snowflake_fixture("not_null_sample.sql")
    test = CandidateTestNotNull(column="customer_id")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        SNOWFLAKE_DIALECT,
        _make_manifest(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert actual == expected


def test_compile_unique_sample_snowflake_matches_snapshot() -> None:
    expected = _read_snowflake_fixture("unique_sample.sql")
    test = CandidateTestUnique(column="customer_id")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        SNOWFLAKE_DIALECT,
        _make_manifest(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert actual == expected


def test_compile_accepted_values_sample_snowflake_matches_snapshot() -> None:
    expected = _read_snowflake_fixture("accepted_values_sample.sql")
    test = CandidateTestAcceptedValues(column="status", values=("placed", "shipped", "cancelled"))
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        SNOWFLAKE_DIALECT,
        _make_manifest(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert actual == expected


def test_compile_relationships_sample_snowflake_matches_snapshot() -> None:
    """Sample-mode relationships samples the CHILD table only; the parent
    stays at the full per-component-quoted qualified name."""
    expected = _read_snowflake_fixture("relationships_sample.sql")
    test = CandidateTestRelationships(column="customer_id", to="customers", field="id")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        SNOWFLAKE_DIALECT,
        _make_manifest(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert actual == expected
    # Belt-and-braces: the parent table is NOT sampled — verify the
    # full per-component-quoted parent identifier survives the wrap.
    assert isinstance(actual, str)
    assert 'LEFT JOIN "FAKE_PROJECT"."DATASET"."CUSTOMERS" AS parent' in actual


def test_compile_custom_sql_single_table_full_snowflake_matches_snapshot() -> None:
    """Single-table custom_sql, scope=full: the resolved SQL is returned
    unchanged (the adapter wraps it with the ``COUNT(*)`` envelope). The
    ``{{ this }}`` resolves to the unquoted qualified name via the bounded
    Jinja resolver (mirrors the BigQuery custom_sql fixture shape)."""
    expected = _read_snowflake_fixture("custom_sql.sql")
    test = CandidateTestCustomSQL(sql="select order_id from {{ this }} where total < 0")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        SNOWFLAKE_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
    )
    assert actual == expected


def test_compile_custom_sql_single_table_sample_snowflake_matches_snapshot() -> None:
    """Single-table custom_sql, scope=sample: the model's own qualified table
    name is substituted with the ``sample`` CTE alias and the deterministic-
    sample CTE (Snowflake-quoted, ``HASH(*)``) is prepended. The #116
    materialised-sample substitution invariant under the Snowflake quote char:
    the body references the ``sample`` CTE alias and NEVER the source table."""
    expected = _read_snowflake_fixture("custom_sql_sample.sql")
    test = CandidateTestCustomSQL(sql="select order_id from {{ this }} where total < 0")
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        SNOWFLAKE_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert actual == expected
    assert isinstance(actual, str)
    # The CTE definition references the source table; the test body after the
    # CTE must read from the (Snowflake-quoted) ``"sample"`` CTE alias, never
    # re-name the source table. The alias is quoted because ``SAMPLE`` is a
    # Snowflake reserved keyword (an unquoted ``WITH sample AS`` is a syntax
    # error there).
    assert 'select order_id from "sample" where total < 0' in actual
    body = actual.split(") select", 1)[1]
    assert "orders" not in body
    assert "fake_project.dataset.orders" not in body


def test_compile_custom_sql_multi_table_full_scan_snowflake_matches_snapshot() -> None:
    """A custom_sql test with a JOIN runs full-scan (unsampled) even when
    scope=sample is requested (DEC-006). Both ``{{ this }}`` and ``{{ ref() }}``
    resolve to qualified names; no sample CTE is emitted."""
    expected = _read_snowflake_fixture("custom_sql_fullscan.sql")
    test = CandidateTestCustomSQL(
        sql=(
            "select o.order_id from {{ this }} as o "
            "join {{ ref('customers') }} as c on o.customer_id = c.id "
            "where c.id is null"
        )
    )
    actual = _compile_test(
        test,
        _make_orders_table_ref(),
        SNOWFLAKE_DIALECT,
        _make_manifest(),
        model=_make_orders_model(),
        scope="sample",
        sample_size=100_000,
        sample_bucket=10,
    )
    assert actual == expected
    assert isinstance(actual, str)
    assert "WITH sample" not in actual


@pytest.mark.parametrize(
    "fixture_name",
    [
        "not_null.sql",
        "unique.sql",
        "accepted_values.sql",
        "relationships.sql",
        "not_null_sample.sql",
        "unique_sample.sql",
        "accepted_values_sample.sql",
        "relationships_sample.sql",
        "custom_sql.sql",
        "custom_sql_sample.sql",
        "custom_sql_fullscan.sql",
    ],
)
def test_snowflake_fixtures_use_double_quote_never_backtick(fixture_name: str) -> None:
    """Guard: every Snowflake fixture uses the ``"`` quote char and never a
    backtick. A backtick would be a sign the BigQuery quote leaked into the
    Snowflake snapshot (DEC-002/003)."""
    text = _read_snowflake_fixture(fixture_name)
    assert "`" not in text
    # custom_sql full + fullscan resolve {{ this }}/{{ ref() }} to unquoted
    # qualified names (the bounded-Jinja resolver, not _qualified_table_name),
    # so they carry no quote char at all; the other nine carry ``"``.
    if fixture_name not in {"custom_sql.sql", "custom_sql_fullscan.sql"}:
        assert '"' in text


@pytest.mark.parametrize(
    "fixture_name",
    [
        "not_null_sample.sql",
        "unique_sample.sql",
        "accepted_values_sample.sql",
        "relationships_sample.sql",
        "custom_sql_sample.sql",
    ],
)
def test_snowflake_sample_fixtures_use_hash_never_farm_fingerprint(fixture_name: str) -> None:
    """Guard: every Snowflake sample fixture renders the ``HASH(*)`` row-hash
    predicate and never BigQuery's ``FARM_FINGERPRINT`` (DEC-002)."""
    text = _read_snowflake_fixture(fixture_name)
    assert "MOD(ABS(HASH(*)), 10) < 1" in text
    assert "FARM_FINGERPRINT" not in text
