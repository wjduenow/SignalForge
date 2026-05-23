"""Tests for the ``read_test_files`` singular-test reader (US-013).

Exercises the stage-0 ``tests/*.sql`` reader end-to-end: a ``.sql`` referencing
the target model → a model-level ``CandidateTestCustomSQL``; a ``.sql``
referencing a *different* model → not included (and not skip-recorded); an
unsupported-Jinja ``.sql`` → ``SkippedTest(malformed-supported-test)``; dedupe
against a schema.yml-sourced custom_sql of identical SQL; and the
size-cap-before-read defence.

``read_test_files`` returns an ``IngestResult`` produced in-process and handed
to prune; it is NOT read back from disk, so no ``extra="forbid"`` drift
detector is needed (see ``tests/ingest/test_models.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from signalforge.draft.models import CandidateSchema, CandidateTestCustomSQL
from signalforge.ingest import (
    IngestResult,
    IngestSchemaNotFoundError,
    IngestSchemaTooLargeError,
    SkippedTest,
    read_test_files,
)
from signalforge.ingest.parser import classify_singular_test
from signalforge.ingest.reader import _INGEST_SCHEMA_SIZE_LIMIT_BYTES
from signalforge.manifest import Manifest, Model

# ``TableRef`` validates ``project`` against GCP's 6–30-char grammar.
_PROJECT = "my_project_dev"
_DATASET = "analytics"


def _model_dict(unique_id: str, *, name: str, package: str) -> dict[str, Any]:
    return {
        "unique_id": unique_id,
        "name": name,
        "resource_type": "model",
        "package_name": package,
        "original_file_path": f"models/{name}.sql",
        "path": f"{name}.sql",
        "database": _PROJECT,
        "schema": _DATASET,
        "alias": name,
        "raw_code": "select 1 as id",
    }


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "metadata": {},
            "nodes": {
                "model.shop.orders": _model_dict(
                    "model.shop.orders", name="orders", package="shop"
                ),
                "model.shop.customers": _model_dict(
                    "model.shop.customers", name="customers", package="shop"
                ),
            },
        }
    )


def _orders(manifest: Manifest) -> Model:
    return manifest.get_model("model.shop.orders")


def _write(tests_dir: Path, name: str, body: str) -> None:
    (tests_dir / name).write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# classify_singular_test — the pure per-file classifier
# ---------------------------------------------------------------------------


def test_classify_associated_when_sql_references_target_model() -> None:
    manifest = _manifest()
    sql = "select * from {{ ref('orders') }} where amount <= 0"
    out = classify_singular_test(
        sql, file_name="amt.sql", model=_orders(manifest), manifest=manifest
    )
    assert isinstance(out, CandidateTestCustomSQL)
    assert out.type == "custom_sql"
    assert out.column is None  # singular tests are model-level
    assert out.sql == sql  # raw body carried verbatim
    assert out.rationale is None


def test_classify_unrelated_when_sql_references_other_model() -> None:
    manifest = _manifest()
    sql = "select * from {{ ref('customers') }} where email is null"
    out = classify_singular_test(
        sql, file_name="email.sql", model=_orders(manifest), manifest=manifest
    )
    assert out is None  # unrelated → not included, not recorded


def test_classify_unsupported_jinja_is_skip_recorded() -> None:
    manifest = _manifest()
    sql = "{% set t = 0 %}\nselect * from {{ ref('orders') }} where amount < {{ t }}"
    out = classify_singular_test(
        sql, file_name="macro.sql", model=_orders(manifest), manifest=manifest
    )
    assert isinstance(out, SkippedTest)
    assert out.reason == "malformed-supported-test"
    assert out.test_name == "macro.sql"
    assert out.column is None


def test_classify_var_lookup_is_skip_recorded() -> None:
    # {{ var(...) }} is unsupported Jinja the bounded resolver can't evaluate.
    manifest = _manifest()
    sql = "select * from {{ ref('orders') }} where region = '{{ var('region') }}'"
    out = classify_singular_test(
        sql, file_name="var.sql", model=_orders(manifest), manifest=manifest
    )
    assert isinstance(out, SkippedTest)
    assert out.reason == "malformed-supported-test"


def test_classify_ref_to_unknown_model_is_unrelated_not_skip() -> None:
    # A well-formed ref() to a model absent from the manifest is not THIS
    # model's test — unrelated (None), not skip-recorded.
    manifest = _manifest()
    sql = "select * from {{ ref('ghost_model') }} where 1=1"
    out = classify_singular_test(
        sql, file_name="ghost.sql", model=_orders(manifest), manifest=manifest
    )
    assert out is None


def test_classify_this_resolves_to_target_and_associates() -> None:
    # {{ this }} resolves to the model itself, so a test using it associates.
    manifest = _manifest()
    sql = "select * from {{ this }} where amount is null"
    out = classify_singular_test(
        sql, file_name="this.sql", model=_orders(manifest), manifest=manifest
    )
    assert isinstance(out, CandidateTestCustomSQL)
    assert out.sql == sql


# ---------------------------------------------------------------------------
# read_test_files — the directory reader
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "ingest" / "custom_sql_files"


def test_read_test_files_associates_and_skips_and_excludes() -> None:
    manifest = _manifest()
    result = read_test_files(_FIXTURE_DIR, _orders(manifest), manifest)
    assert isinstance(result, IngestResult)

    # The orders-referencing well-formed test is included.
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert len(custom) == 1
    assert "ref('orders')" in custom[0].sql
    assert "amount <= 0" in custom[0].sql

    # The customers-referencing file is NOT included and NOT skip-recorded.
    assert all("customers" not in t.sql for t in custom)

    # The macro file referencing orders is skip-recorded.
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "malformed-supported-test"
    assert result.skipped[0].test_name == "assert_orders_with_macro.sql"

    # Candidate is model-level only: no columns.
    assert result.candidate.columns == ()
    assert result.candidate.name == "orders"


def test_read_test_files_dedupes_identical_sql_files(tmp_path: Path) -> None:
    manifest = _manifest()
    body = "select * from {{ ref('orders') }} where amount <= 0\n"
    _write(tmp_path, "a.sql", body)
    _write(tmp_path, "b.sql", body)  # byte-identical → must collapse
    result = read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert len(custom) == 1


def test_read_test_files_dedupes_against_existing_schema_yml_custom_sql(tmp_path: Path) -> None:
    # DEC-013: a .sql duplicating a schema.yml-sourced custom_sql collapses to
    # one when ``existing`` is supplied.
    manifest = _manifest()
    body = "select * from {{ ref('orders') }} where amount <= 0\n"
    _write(tmp_path, "dup.sql", body)
    existing = CandidateSchema(
        name="orders",
        description="",
        columns=(),
        tests=(CandidateTestCustomSQL(column=None, sql=body, rationale=None),),
    )
    result = read_test_files(
        tmp_path, _orders(manifest), manifest, project_dir=tmp_path, existing=existing
    )
    # The .sql collapses into the existing one → zero NEW custom_sql tests.
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert custom == []


def test_read_test_files_distinct_sql_against_existing_is_kept(tmp_path: Path) -> None:
    manifest = _manifest()
    _write(tmp_path, "new.sql", "select * from {{ ref('orders') }} where amount is null\n")
    existing = CandidateSchema(
        name="orders",
        description="",
        columns=(),
        tests=(
            CandidateTestCustomSQL(
                column=None,
                sql="select * from {{ ref('orders') }} where amount <= 0\n",
                rationale=None,
            ),
        ),
    )
    result = read_test_files(
        tmp_path, _orders(manifest), manifest, project_dir=tmp_path, existing=existing
    )
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert len(custom) == 1
    assert "amount is null" in custom[0].sql


def test_read_test_files_empty_dir_yields_empty_candidate(tmp_path: Path) -> None:
    manifest = _manifest()
    result = read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    assert result.candidate.tests == ()
    assert result.skipped == ()


def test_read_test_files_ignores_non_sql_files(tmp_path: Path) -> None:
    manifest = _manifest()
    _write(tmp_path, "readme.md", "not a test")
    _write(tmp_path, "notes.txt", "select * from {{ ref('orders') }}")
    _write(tmp_path, "real.sql", "select * from {{ ref('orders') }} where amount <= 0\n")
    result = read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert len(custom) == 1


def test_read_test_files_oversize_file_rejected_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DEC-005: the cap is enforced from stat().st_size BEFORE the file is read.
    monkeypatch.setattr("signalforge.ingest.reader._INGEST_SCHEMA_SIZE_LIMIT_BYTES", 10)
    manifest = _manifest()
    big = tmp_path / "big.sql"
    big.write_text("select * from {{ ref('orders') }} where amount <= 0\n")  # > 10 bytes
    with pytest.raises(IngestSchemaTooLargeError) as excinfo:
        read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    assert excinfo.value.limit == 10
    assert excinfo.value.size == big.stat().st_size


def test_read_test_files_real_cap_constant_is_5mb() -> None:
    # Guard the cap matches read_schema's (mirrors diff DEC-006 order of magnitude).
    assert _INGEST_SCHEMA_SIZE_LIMIT_BYTES == 5_000_000


def test_read_test_files_missing_dir_raises_not_found(tmp_path: Path) -> None:
    manifest = _manifest()
    missing = tmp_path / "no_such_dir"
    with pytest.raises(IngestSchemaNotFoundError):
        read_test_files(missing, _orders(manifest), manifest, project_dir=tmp_path)


def test_read_test_files_deterministic_sorted_order(tmp_path: Path) -> None:
    # Two distinct associated tests come back in sorted-filename order.
    manifest = _manifest()
    _write(tmp_path, "z_last.sql", "select * from {{ ref('orders') }} where amount is null\n")
    _write(tmp_path, "a_first.sql", "select * from {{ ref('orders') }} where amount <= 0\n")
    result = read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert len(custom) == 2
    # a_first.sql (amount <= 0) sorts before z_last.sql (amount is null).
    assert "amount <= 0" in custom[0].sql
    assert "amount is null" in custom[1].sql
