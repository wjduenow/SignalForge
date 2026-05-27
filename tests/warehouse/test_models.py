"""Unit tests for the warehouse models module (US-004).

Covers the five public types (Dialect, TableRef, PartitionFilter,
ColumnStats, TestResult) plus the two private helper modules
(_sql_safety, _test_result_repr). Every test is capable of failing
(``testing-signal.md`` — no ``assert True``-shaped placeholders).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from signalforge.manifest.models import Model
from signalforge.warehouse._sql_safety import _strip_string_literals, validate_test_sql
from signalforge.warehouse._test_result_repr import compact_repr
from signalforge.warehouse.errors import (
    InvalidIdentifierError,
    ManifestProjectNotFoundError,
    QuerySyntaxError,
)
from signalforge.warehouse.models import (
    BIGQUERY_DIALECT,
    SNOWFLAKE_DIALECT,
    ColumnStats,
    Dialect,
    PartitionFilter,
    TableRef,
    TestResult,
)


def _minimal_model_dict(**overrides: Any) -> dict[str, Any]:
    """Build a minimal-but-valid Model payload for from_model tests."""
    base: dict[str, Any] = {
        "unique_id": "model.my_pkg.my_model",
        "name": "my_model",
        "resource_type": "model",
        "package_name": "my_pkg",
        "original_file_path": "models/my_model.sql",
        "path": "my_model.sql",
        "database": "my_project",
        "schema": "analytics",
        "raw_code": "select 1 as id",
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_bigquery_dialect_constant_is_frozen() -> None:
    """Mutating BIGQUERY_DIALECT raises FrozenInstanceError (DEC-003)."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        BIGQUERY_DIALECT.name = "snowflake"  # type: ignore[misc]


@pytest.mark.unit
def test_snowflake_dialect_values() -> None:
    """SNOWFLAKE_DIALECT carries the Snowflake capability flags (issue #119, DEC-004).

    ``identifier_case='upper'`` is the opposite of Postgres ('lower') and is
    load-bearing for the Snowflake compiler (issue #121).
    """
    assert isinstance(SNOWFLAKE_DIALECT, Dialect)
    assert SNOWFLAKE_DIALECT.name == "snowflake"
    assert SNOWFLAKE_DIALECT.quote_char == '"'
    assert SNOWFLAKE_DIALECT.identifier_case == "upper"
    assert SNOWFLAKE_DIALECT.supports_qualify is True
    assert SNOWFLAKE_DIALECT.supports_tablesample is True


@pytest.mark.unit
def test_snowflake_dialect_sql_fragment_fields() -> None:
    """SNOWFLAKE_DIALECT carries the issue-#121 SQL-fragment values (DEC-002).

    These five fields drive the prune compiler's warehouse-specific SQL
    without name-branching: the whole-row hash expression, the date/timestamp
    literal cast templates, per-component qualified-name quoting, and the
    sample-CTE alias.
    """
    assert SNOWFLAKE_DIALECT.sample_row_hash_expr == "ABS(HASH(*))"
    assert SNOWFLAKE_DIALECT.timestamp_literal_template == "'{value}'::TIMESTAMP"
    assert SNOWFLAKE_DIALECT.date_literal_template == "'{value}'::DATE"
    assert SNOWFLAKE_DIALECT.quote_qualified_per_component is True
    # ``SAMPLE`` is a Snowflake reserved keyword, so the deterministic-sample
    # CTE alias is the QUOTED ``"sample"`` (an unquoted ``WITH sample AS`` is a
    # syntax error on Snowflake).
    assert SNOWFLAKE_DIALECT.sample_cte_alias == '"sample"'
    # issue #139: HASH(*) is projection-only on Snowflake, so the sample SELECT
    # computes it in an inner projection and references the alias.
    assert SNOWFLAKE_DIALECT.sample_hash_in_projection is True
    assert SNOWFLAKE_DIALECT.sample_hash_alias == "_sf_sample_hash"


@pytest.mark.unit
def test_bigquery_dialect_sql_fragment_field_defaults() -> None:
    """BIGQUERY_DIALECT carries the BigQuery-shaped defaults for the five
    issue-#121 fields (DEC-001) — these reproduce current BigQuery SQL
    byte-for-byte so the existing snapshot suite stays green."""
    assert BIGQUERY_DIALECT.sample_row_hash_expr == "ABS(FARM_FINGERPRINT(TO_JSON_STRING(t)))"
    assert BIGQUERY_DIALECT.timestamp_literal_template == "TIMESTAMP('{value}')"
    assert BIGQUERY_DIALECT.date_literal_template == "DATE('{value}')"
    assert BIGQUERY_DIALECT.quote_qualified_per_component is False
    # BigQuery's sample-CTE alias is the bare ``sample`` (not a reserved word
    # there) — keeps the existing BigQuery snapshots byte-identical.
    assert BIGQUERY_DIALECT.sample_cte_alias == "sample"
    # issue #139: BigQuery's FARM_FINGERPRINT hash is valid inline, so the
    # sample SELECT keeps the inline (non-projection) shape.
    assert BIGQUERY_DIALECT.sample_hash_in_projection is False
    assert BIGQUERY_DIALECT.sample_hash_alias == "_sf_sample_hash"


@pytest.mark.unit
def test_dialect_constructs_without_new_field_args() -> None:
    """Constructing a Dialect with ONLY the five original fields still
    succeeds — the five issue-#121 fields carry BigQuery defaults (DEC-001).

    This guards every pre-#121 construction site (e.g. the prune compiler's
    dispatch-test custom dialect) so they stay valid unedited.
    """
    d = Dialect(
        name="custom",
        supports_tablesample=True,
        supports_qualify=False,
        quote_char='"',
        identifier_case="preserve",
    )
    # Defaults are present and BigQuery-shaped.
    assert d.sample_row_hash_expr == "ABS(FARM_FINGERPRINT(TO_JSON_STRING(t)))"
    assert d.timestamp_literal_template == "TIMESTAMP('{value}')"
    assert d.date_literal_template == "DATE('{value}')"
    assert d.quote_qualified_per_component is False
    assert d.sample_cte_alias == "sample"
    assert d.sample_hash_in_projection is False
    assert d.sample_hash_alias == "_sf_sample_hash"


@pytest.mark.unit
def test_snowflake_dialect_importable_from_package_top_level() -> None:
    """SNOWFLAKE_DIALECT is re-exported from signalforge.warehouse."""
    from signalforge.warehouse import SNOWFLAKE_DIALECT as pkg_dialect

    assert pkg_dialect is SNOWFLAKE_DIALECT


@pytest.mark.unit
@pytest.mark.error
def test_tableref_rejects_invalid_dataset() -> None:
    """Hyphenated dataset name fails the DEC-013 identifier regex."""
    with pytest.raises(InvalidIdentifierError):
        TableRef(project="proj01", dataset="bad-name", name="t")


@pytest.mark.unit
@pytest.mark.error
def test_tableref_rejects_invalid_name() -> None:
    """Adversarial table name with `;` is rejected at construction time."""
    with pytest.raises(InvalidIdentifierError):
        TableRef(project="proj01", dataset="d", name="x;DROP")


@pytest.mark.unit
@pytest.mark.error
def test_tableref_rejects_invalid_project() -> None:
    """Adversarial project string (whitespace + ``!``) fails the project regex.

    Hyphens are intentionally allowed (real GCP project IDs use them); the
    rejection set is the SQL-injection-shaped inputs.
    """
    with pytest.raises(InvalidIdentifierError):
        TableRef(project="bad project!", dataset="d", name="t")


@pytest.mark.unit
def test_tableref_accepts_hyphenated_gcp_project() -> None:
    """Real GCP project IDs use hyphens (``my-co-prod-12345``); the project
    regex must accept them. Closes the QG-of-US-013 finding-1 gap."""
    ref = TableRef(project="my-co-prod-12345", dataset="d", name="t")
    assert ref.project == "my-co-prod-12345"


@pytest.mark.unit
def test_tableref_accepts_none_project() -> None:
    """``project=None`` is allowed (DEC-027): defer to BQ client default."""
    ref = TableRef(project=None, dataset="d", name="t")
    assert ref.project is None


@pytest.mark.unit
def test_tableref_qualified_name_renders_dotted_form() -> None:
    """``qualified_name`` is the stable identifier used in error messages —
    dialect-neutral, no backticks, ``project`` omitted when ``None``
    (Copilot review feedback)."""
    assert TableRef(project="my-proj-01", dataset="d", name="t").qualified_name == "my-proj-01.d.t"
    assert TableRef(project=None, dataset="d", name="t").qualified_name == "d.t"


@pytest.mark.unit
def test_tableref_from_model_happy_path() -> None:
    """``from_model`` returns project/dataset/name from a populated Model."""
    model = Model.model_validate(_minimal_model_dict())
    ref = TableRef.from_model(model)
    assert (ref.project, ref.dataset, ref.name) == ("my_project", "analytics", "my_model")


@pytest.mark.unit
def test_tableref_from_model_uses_alias_over_name() -> None:
    """When ``alias`` is set, ``TableRef.name`` follows it (DEC-014)."""
    model = Model.model_validate(_minimal_model_dict(alias="x", name="y"))
    ref = TableRef.from_model(model)
    assert ref.name == "x"


@pytest.mark.unit
@pytest.mark.error
def test_tableref_from_model_raises_when_database_none() -> None:
    """Missing ``database`` field surfaces ManifestProjectNotFoundError."""
    model = Model.model_validate(_minimal_model_dict(database=None))
    with pytest.raises(ManifestProjectNotFoundError):
        TableRef.from_model(model)


@pytest.mark.unit
@pytest.mark.error
def test_partition_filter_rejects_invalid_column() -> None:
    """Adversarial column name on PartitionFilter is rejected (DEC-013)."""
    with pytest.raises(InvalidIdentifierError):
        PartitionFilter(column="bad-col", op="=", value="2024-01-01")


@pytest.mark.unit
def test_partition_filter_accepts_each_op() -> None:
    """All six operators in the PartitionOp Literal construct successfully."""
    ops = ("=", ">", ">=", "<", "<=", "!=")
    constructed = [PartitionFilter(column="dt", op=op, value="2024-01-01") for op in ops]
    assert [pf.op for pf in constructed] == list(ops)


@pytest.mark.unit
def test_column_stats_complex_type_min_max_none() -> None:
    """For complex BQ types, min/max default to None (DEC-016)."""
    cs = ColumnStats(count=10, distinct=10, nulls=0, data_type="GEOGRAPHY")
    assert cs.min is None and cs.max is None


@pytest.mark.unit
def test_test_result_explanation_passed() -> None:
    """A passing TestResult renders ``"passed"``."""
    tr = TestResult(passed=True, failure_count=0)
    assert tr.explanation() == "passed"


@pytest.mark.unit
def test_test_result_explanation_failed_no_samples() -> None:
    """Failing TestResult without samples renders the count-only string."""
    tr = TestResult(passed=False, failure_count=42)
    assert tr.explanation() == "42 rows failed"


@pytest.mark.unit
def test_test_result_explanation_failed_with_sample() -> None:
    """Failing TestResult with sample + schema renders a TIMESTAMP fragment."""
    tr = TestResult(
        passed=False,
        failure_count=3,
        sample_failures=[{"id": 7, "ts": "2024-01-01T00:00:00"}],
        row_schema=[("id", "INT64"), ("ts", "TIMESTAMP")],
    )
    rendered = tr.explanation()
    assert "example:" in rendered
    assert "TIMESTAMP('2024-01-01T00:00:00')" in rendered


@pytest.mark.unit
def test_compact_repr_truncates_long_strings() -> None:
    """String values longer than 40 chars are truncated with `...` (DEC-020)."""
    long_value = "x" * 100
    rendered = compact_repr({"col": long_value})
    # The rendered form is `col='xxx...'`; the inner value must be 40 chars
    # max (37 x's + '...'), wrapped in single quotes.
    assert "..." in rendered
    assert "x" * 41 not in rendered


@pytest.mark.unit
def test_compact_repr_escapes_quotes_and_backslashes() -> None:
    """String values render paste-safe — `'` and `\\` are escaped (DEC-020).

    Without this, a sample failure containing ``o'brien`` would render as
    ``name='o'brien'`` (unbalanced quote) — pasting into a WHERE clause
    is then either a parse error or, worse, an injection seam.
    """
    rendered = compact_repr({"name": "o'brien", "path": r"c:\windows"})
    # Single quotes inside the literal must be backslash-escaped.
    assert r"name='o\'brien'" in rendered
    # Backslashes must also be escaped so the trailing escape doesn't eat
    # the closing quote.
    assert r"path='c:\\windows'" in rendered


@pytest.mark.unit
@pytest.mark.error
def test_validate_test_sql_rejects_semicolon() -> None:
    """A trailing ``;`` in candidate test SQL is rejected (DEC-013)."""
    with pytest.raises(QuerySyntaxError):
        validate_test_sql("select 1 from t;")


@pytest.mark.unit
@pytest.mark.error
def test_validate_test_sql_rejects_statement_stacking_masked_by_backtick() -> None:
    """A real top-level ``;`` masked by a stray quote inside a backtick identifier.

    Without backtick-awareness, the lone ``'`` inside `` `it's` `` opens a
    phantom single-quoted literal that swallows the real top-level ``;``, so
    the statement-stacking check never sees it. Backtick stripping neutralises
    the backtick span first, exposing the ``;`` to the token scan (DEC-008).
    """
    sql = "SELECT * FROM `it's`; DROP TABLE x WHERE a='b'"
    with pytest.raises(QuerySyntaxError):
        validate_test_sql(sql)


@pytest.mark.unit
@pytest.mark.error
def test_validate_test_sql_rejects_semicolon_inside_backtick_then_stacked() -> None:
    """A ``;`` inside a backtick identifier followed by a stacked statement.

    The backtick span `` `weird;name` `` is stripped, but the genuine
    statement-separating ``;`` outside it must still be rejected.
    """
    sql = "SELECT * FROM `weird;name`; DROP TABLE x"
    with pytest.raises(QuerySyntaxError):
        validate_test_sql(sql)


@pytest.mark.unit
def test_validate_test_sql_allows_benign_backtick_identifier() -> None:
    """A benign backtick-quoted identifier (no top-level tokens) is accepted.

    Even a ``;`` *inside* the backtick span is content, not a statement
    separator — once the span is stripped, nothing forbidden remains.
    """
    # Plain identifier — must not raise.
    validate_test_sql("SELECT `col` FROM `proj.dataset.tbl`")
    # A ``;`` that lives ENTIRELY inside the backtick identifier is content,
    # so once the span is stripped there is no top-level ``;`` left.
    validate_test_sql("SELECT `weird;name` FROM `t`")


@pytest.mark.unit
def test_validate_test_sql_ignores_comment_markers_inside_quotes_and_backticks() -> None:
    """``--`` / ``/* */`` inside string literals AND backticks are ignored."""
    # Comment markers inside single/double-quoted literals: allowed.
    validate_test_sql("SELECT '--not a comment' AS a, \"/* nope */\" AS b FROM t")
    # Comment markers inside a backtick identifier: also ignored once stripped.
    validate_test_sql("SELECT `--col` FROM `/* tbl */`")


@pytest.mark.unit
@pytest.mark.error
def test_validate_test_sql_balanced_parens_unaffected_by_backticks() -> None:
    """The balanced-paren check still fires; backtick spans don't perturb it."""
    # Balanced parens with a backtick identifier present: accepted.
    validate_test_sql("SELECT COUNT(*) FROM (SELECT `c` FROM `t`) AS x")
    # A paren hidden inside a backtick must NOT count toward the depth — the
    # outer SQL is genuinely unbalanced and must be rejected.
    with pytest.raises(QuerySyntaxError):
        validate_test_sql("SELECT * FROM `t` WHERE a = (1")


@pytest.mark.unit
def test_strip_string_literals_strips_backtick_spans() -> None:
    """The helper neutralises backtick-quoted content alongside quotes."""
    # Backtick span content is removed; surrounding skeleton survives.
    assert _strip_string_literals("a `;--/*` b") == "a  b"
    # Doubled backtick is an escape and stays inside the span.
    assert _strip_string_literals("`a``b`c") == "c"
    # Single/double quote handling is preserved.
    assert _strip_string_literals("x '; y' z") == "x  z"
    assert _strip_string_literals('p "; q" r') == "p  r"
